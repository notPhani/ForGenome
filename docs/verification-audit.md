# Forgenome verification audit

Date: 2026-08-19. Scope: `forge_ir/`, `ingest/`, `orchestration/`, `connectors/`, `analysis/`, `scripts/`, `tests/`, `demo/graph_data.json`, `runs/panel_50k/`. No git exists in this repo, so every edit below followed the stated backup policy.

## Files modified, and why

All eight are dead-import removals (pyflakes-confirmed, zero behavior change, full test suite re-run green after each). No logic was changed anywhere. Backups are verbatim pre-edit copies.

| File edited | Change | Backup |
|---|---|---|
| `forge_ir/verify.py` | removed unused `EdgeType`, `NodeRole` imports | `docs/_deep_clean_backups/forge_ir__verify.py.orig` |
| `ingest/compiler.py` | removed unused `check_record_conservation`, `normalize_disease_key`, `normalize_gene_key` imports | `docs/_deep_clean_backups/ingest__compiler.py.orig` |
| `orchestration/connector_registry.py` | removed unused `field` import | `docs/_deep_clean_backups/orchestration__connector_registry.py.orig` |
| `orchestration/connector_planner.py` | removed unused `field` import | `docs/_deep_clean_backups/orchestration__connector_planner.py.orig` |
| `connectors/dbsnp.py` | removed unused `field` import | `docs/_deep_clean_backups/connectors__dbsnp.py.orig` |
| `connectors/ensembl.py` | removed unused `Any` import | `docs/_deep_clean_backups/connectors__ensembl.py.orig` |
| `connectors/clinvar.py` | removed unused `import requests` (module uses `connectors.http` exclusively) | `docs/_deep_clean_backups/connectors__clinvar.py.orig` |
| `scripts/generate_graph_demo_data.py` | removed unused `Layer` import | `docs/_deep_clean_backups/scripts__generate_graph_demo_data.py.orig` |

Verification after edits: `python -m pytest -q` → **263 passed** (same as the pre-edit baseline), `python scripts/verify_connectors.py` → **7/7 implemented connectors PASS, 287 nodes / 370 edges built**. Nothing else in the repository was modified — no synthetic connector was left registered (see §A.2), no production data files were altered (§B worked from copies in the scratch directory).

Two additional dead-code smells were found and deliberately **not** fixed (see §Deep-clean findings below): a computed-but-discarded `top` variable in `connectors/clinvar.py:258` and a computed-but-discarded `gene_id` variable at `connectors/clinvar.py:350`. The first looks like an unfinished feature (VCV-level top classification is hardcoded to `None` right next to where it's computed); fixing it would change committed graph content and needs real-fixture verification, which is outside this audit's "obvious, low-risk, single fix" bar.

---

## A. Connector → node/edge correctness, adversarially

### A.1 Baseline

`python -m pytest -q` (pre-edit): **263 passed**. `python scripts/verify_connectors.py`: all 7 implemented connectors (`ensembl_structure`, `uniprot`, `clingen`, `clinvar`, `gnomad`, `ensembl_vep`, `dbsnp`) pass all 7 checks (REGISTERED/CONTRACT/CAPABILITIES/GRAMMAR/QUERY/EMISSION/VERIFIED); the 6 stub connectors (`omim`, `pharmgkb`, `mondo`, `cosmic`, `dbnsfp`, `clingen_gene_disease`) correctly raise `NotImplementedError` on `fetch()`.

### A.2 Adversarial payloads against real connectors

Scratch script: `.../scratchpad/adversarial_payloads.py` (run against `gnomad` and `dbsnp` via `descriptor.build_emission(...)` directly, no network).

**gnomAD** (`connectors/gnomad.py`):
- `af=NaN`, `ac=-5`, `an=0`, `faf95.popmax=None`, `populations` missing → **no exception, no rejection**: `GnomadParser.parse` and `frequency_to_ir` build a `PopulationFrequency` node carrying the NaN/negative values verbatim. `verify()` only rejected it in isolation because the observation had no path to a `CORE_BIOLOGY` node (connectivity check, incidental, not a value check).
- `af=Infinity`, `faf95.popmax=Infinity` → same: passes straight through into node attributes.
- `af` present but `ac`/`an` missing → **`KeyError: 'ac'`** raised by `frequency_to_ir` (`connectors/gnomad.py:92`, `data["ac"]`). Confirmed this is caught at record level in production (`orchestration/runner.py:636-644`, blanket `except Exception`) and filed as a `FETCH_FAILED` retry ticket — but see §C's row on this exact case: a malformed-but-reachable payload is filed under the same retry reason as a genuine network timeout, so it will retry, fail identically, and eventually get `ABANDONED` rather than being recognized immediately as a permanent parse mismatch.
- `genome=None, exome=None` (both scopes null) → correctly yields nothing (this is the documented real "gnomAD never saw this variant" case, `connectors/gnomad.py:74-80`); confirmed intentional, not a bug.

**Follow-up: do the NaN/negative values actually commit?** Yes — confirmed with `.../scratchpad/adversarial_gnomad_commit.py`, which merges the malformed gnomAD emission with a legitimate Gene+Variant (exactly how production merges a `requires=("clinvar",)` connector's emission before committing, per `scripts/verify_connectors.py`'s own documented merge behavior):

```
committed: True
COMMITTED NODE: gnomAD:3-10149822-C-T:genome {'ac': -999999, 'an': 0, 'af': nan, 'faf95_popmax': inf, 'dataset': 'genome', 'populations': []}
```

**This is a real, demonstrated gap**: `forge_ir/verify.py`'s 9 checks are purely structural/typing (is this (role,subtype) legal, is this edge shape legal, does provenance exist, is the layer tag right, is the graph connected) — there is **no check anywhere that a numeric attribute is in a sane domain range**. A population allele count of −999,999, an allele number of 0 with a nonzero count, and a frequency of `NaN`/`Infinity` all commit cleanly with a valid `structural_hash`. Nothing in the 9 checks, the completeness report (`analysis/completeness.py`), or the compiler validates attribute *values*, only attribute *presence-as-a-dict*.

**dbSNP** (`connectors/dbsnp.py`):
- Two placements both marked `is_ptlp: true` (ambiguous "top-level placement" — shouldn't happen per dbSNP's own model, but the parser doesn't assume it can't) → `DbSnpParser.parse` uses `next((p for p in placements if p.get("is_ptlp")), None)` (`connectors/dbsnp.py:65`), silently taking the **first** one in list order and discarding the second with no warning, no quarantine entry, no log. If the discarded one were the correct placement, the wrong Variant node would be built and nothing downstream would ever know.
- Empty `alleles` list on the top-level placement → correctly yields nothing.
- Missing `primary_snapshot_data` entirely → correctly yields nothing (`.get({}, {})` chain).

### A.3 The real gap found: verify()'s type-correctness check can be bypassed across batches

This was the most significant finding of the whole audit. `forge_ir/verify.py::_check_type_correctness` builds its `by_id` lookup **only from the nodes passed in the current batch** (`verify.py:96-98`, `by_id = {n.id: n for n in nodes}`). If an edge's `src` or `dst` id is not in this batch's node list — because it was already committed in an earlier micro-batch, and is only known via the `known_node_ids` set of bare id strings — `by_id.get(e.src)` returns `None`, and the comment at `verify.py:104-105` explicitly defers to referential integrity ("avoid double-reporting"). But referential integrity (`_check_referential_integrity`) only checks that the id string is a member of `all_ids = set(by_id) | known_node_ids` — it never re-validates the *role/subtype* of an id that came from `known_node_ids` only, because `known_node_ids` carries no role/subtype information, just strings.

Net effect: **an edge whose source node is a real, already-committed node from a prior batch can carry any predicate/qualifier at all, and neither type-correctness nor referential-integrity will catch it**, as long as the destination is a legally-shaped new node.

Proof, `.../scratchpad/adversarial_known_ids2.py`:
```python
known_node_ids = {"SPDI:x:1:C:T"}          # a Variant "already committed" in a prior batch
gene = ForgeNode(id="HGNC:99999", role=ENTITY, subtype="Gene", ...)   # new, legal node
illegal_edge = ForgeEdge(src="SPDI:x:1:C:T", predicate=EdgeType.PART_OF, dst=gene.id, ...)
# Variant --PART_OF--> Gene is NOT in ALLOWED_EDGE_COMBINATIONS under any circumstance
# (PART_OF is legal ONLY Transcript->Gene and Exon->Transcript)
report = verify([gene], [illegal_edge], known_node_ids=known_node_ids)
```
Output:
```
valid: True
ALL ERRORS: []
--- StagingCompiler path ---
committed: True rejected: False
EDGES COMMITTED: [('SPDI:x:1:C:T', 'PART_OF', 'HGNC:99999')]
```

**This is not a contrived corner case — it is exactly how production runs.** `orchestration/runner.py:318,328-329`:
```python
resident_node_ids = _load_resident(config)
...
known = set(store.nodes) | set(resident_node_ids)
result = compiler.commit(known_node_ids=known)
```
Every ephemeral run's every micro-batch after the first passes `known_node_ids` populated exactly this way (`store.nodes`, the accumulated ids from every prior commit in the same run, plus `resident_node_ids` declaring the persistent layer resident — see `runner.py:1-14`'s own docstring: *"an edge pointing at a Gene node this run never created still passes referential integrity instead of rejecting the batch"*). That design choice is intentional for referential integrity — the bug is that it silently also disables type-correctness for the referencing edge, which was never the intent (nothing in the docs describes this trade-off). A malformed connector (or a bug in `build_emission`) that emits `Variant --PART_OF--> Gene` on a node it did NOT also re-emit this batch would commit undetected.

I did not fix this: closing it correctly requires a design decision (does `known_node_ids` need to become `known_nodes: dict[str, (role, subtype)]`, or does the type check need to special-case cross-batch edges as unverifiable and reject them outright?) that is out of scope for a "safe, obvious, single fix," and touches the hot path of every real ephemeral commit. Flagging it precisely is the deliverable.

### A.4 Second gap: `required_attributes` is declared but never enforced

`forge_ir/core.py:86` defines `NodeSubtypeEntry.required_attributes` and populates it for every registered subtype (e.g. Gene needs `["symbol", "biotype"]`, Variant needs `["assembly", "ref", "alt"]`, Requirement needs `["constraints", "evidence_type"]`). `grep -rn required_attributes` across the whole repo shows it is **read nowhere** — not in `forge_ir/verify.py`'s 9 checks, not in the compiler, not anywhere. Proof, `.../scratchpad/adversarial_required_attrs.py`:
```
Gene w/ empty attributes -> valid: True errors: []
Variant missing ref/alt -> valid: True errors: []
```
A node can declare the right `(role, subtype)` and pass every one of the 9 checks with a completely empty `attributes` dict.

This connects to a second, independent finding from the real harvest data (§B): the `Requirement` subtype's own declared `required_attributes = ["constraints", "evidence_type"]` (`core.py:148`) is **not actually satisfied by real Requirement nodes** — every real `Requirement` node inspected (140 of them, from `connectors/clingen.py`, confirmed against `runs/panel_50k`) has `evidence_type` nested *inside* `attributes["constraints"]["evidence_type"]`, never as a top-level `attributes["evidence_type"]` key. The registry's own contract for its own data is violated in production, and nothing catches it, because nothing checks it.

### A.5 Synthetic connector — positive control confirming the *in-batch* defenses work correctly

Per the task, a standalone synthetic connector (`.../scratchpad/synthetic_connector.py`) was built following the real `ConnectorDescriptor` contract, registered via the real `register_connector()`, exercised, then unregistered via `orchestration.connector_registry._unregister_for_tests()`. It was never placed under `connectors/` and never left registered.

Three cases, run through the real `StagingCompiler`:
1. **Legal emission** (`OBSERVATION:CaseObservation --OBSERVES--> Variant`) → `committed=True`.
2. **Bad qualifier**: `OBSERVATION --SUPPORTS[maybe_satisfies]--> RULE:Requirement` (the real qualifier is `satisfies_requirement`; `maybe_satisfies` doesn't exist in the ontology) → **`committed=False, rejected=True`**, `rejection_reason` names the exact illegal combination. Confirms the in-batch type-correctness check works exactly as designed when the source node is present in the same batch (contrast with §A.3).
3. **Bad subtype**: `ENTITY:SuperVariant` (not in `NODE_SUBTYPE_REGISTRY`) → raised `ValueError` at `ForgeNode.__post_init__` **before it could even become part of an emission** — confirms the double defense-in-depth already asserted by `tests/test_verify.py`'s own comments (construction-time refusal is the first line of defense, `verify()`'s check is defense-in-depth for data that bypassed normal construction, e.g. deserialization).

Conclusion for A: **a correctly-written connector reliably produces a correct graph, and an incorrectly-written connector that emits illegal shapes WITHIN its own emission is reliably rejected** — the standard, single-batch path works exactly as advertised. But the pipeline's stronger claim — that *nothing* illegal can ever reach a committed graph — is false in two independent, demonstrated ways: (1) cross-batch edges from already-known nodes bypass type-correctness entirely (§A.3), and (2) attribute-value sanity and declared `required_attributes` are never checked at all (§A.4, and the NaN/negative-count gnomAD case in §A.2).

---

## B. Subgraph → context-building capability

### Does any code already do this?

Searched broadly (`grep -rniE "context_builder|build_context|attention|ContextBuilder|to_context|for_model|RequirementEvaluator"` etc. across the whole repo) before concluding. Findings:

- `forge_ir/requirement.py::evaluate_requirement()` is real, tested, working code that answers "is this ONE Requirement satisfied by THIS ONE observation dict" for `MECHANICAL`/`COMPOSITE` evaluation modes (`NARRATIVE`/`HYBRID` always return `NEEDS_INTERPRETATION` by design — they cannot auto-satisfy). It is a pure function: it takes an already-selected `observation: dict | None` as an argument. **It does not select that observation itself, and nothing else in the repo does either.**
- `tests/e2e/test_vhl_pm2.py::test_the_actual_gate_pm2_requirement_evaluates_against_real_gnomad_observation` is the one place the full chain is exercised end to end, and it hand-picks the observation: `genome_obs_node = next(n for n in committed_graph.nodes if n.subtype == "PopulationFrequency" and n.attributes["dataset"] == "genome")` (lines 181-190). This works only because the fixture graph has exactly one variant and exactly one genome-scope frequency node — a human, reading the ClinGen PM2 spec text, already knows PM2's `constraints.metric` (`faf95_popmax`) lives on a `PopulationFrequency` node, and wrote the filter by hand.
- `analysis/completeness.py` is an honest, real, advisory report of which `(subtype, edge-combination)` pairs are populated vs. declared-but-empty — this is the closest thing to graph-introspection tooling that exists, but it reports aggregate coverage percentages, not per-node candidate retrieval.
- `orchestration/connector_planner.py`, `orchestration/scope_resolver.py` plan *what to fetch*, not *what to retrieve from an already-committed graph for a model*.
- Nothing named `attention`, `embedding`, `vector`, `rank`, `relevance`, or any Phase-2/Phase-3 term from the companion doc appears anywhere in `.py` files except inside comments/docstrings acknowledging the concept exists in the design docs, not in code.

**Verdict: the companion document's claim is correct.** Phase 2 (attention/retrieval-decisioning) and Phase 3 (context building) are designed in the docs (`forge-ir-v3-schema.md` §8's `RequirementEvaluator` Protocol, `forge-pipeline-v1.md`) but have no implementation beyond the single-requirement, single-observation evaluator function, which is necessary but nowhere near sufficient.

### The actual traversal experiment

Two scripts were written and run, at two different scales:

1. `.../scratchpad/traversal_context_experiment.py` — against `demo/graph_data.json` (confirmed to be a **downsampled visualization export**, `data["downsample"]["kept_nodes"]=2524` of `total_nodes=57433`, produced by `analysis/downsample.py` — not the full graph).
2. `.../scratchpad/traversal_context_full.py` — against the **full, real, live** `runs/panel_50k/` harvest (95,578 unique deduped nodes / 270,262 unique deduped edges after merging `graph.jsonl.gz` + `graph.part001.jsonl.gz` + `graph.part002.jsonl.gz`). Per the task's own warning, this was a live-harvest-in-progress: `graph.jsonl.gz` and `graph.part002.jsonl.gz` both hit `zlib.error: invalid block type` mid-read (a truncated gzip block from concurrent writing). I used the repo's **own already-tested reader**, `scripts/build_viz_dataset.py::read_dump` (covered by `tests/test_dump_recovery.py`, 3 passing tests including exactly this truncated-mid-block scenario), rather than reimplementing recovery — it returned everything readable and reported the drop count (`dropped=1` on each of the two truncated files), exactly as designed. This is itself a positive finding for §C: truncated-dump recovery is real, tested, and it worked correctly on a live file in this audit.

Question asked of the graph: *for VHL's real test variant (`SPDI:NC_000003.12:10149821:C:T`), what does Core Biology + Operational tell me is required to evaluate it, and what Evidence currently satisfies or fails that, using only graph traversal?*

**What worked, by pure BFS/edge-walk, no LLM:**
```
Variant --RELATES_TO[in_gene]--> Gene (HGNC:12687)
Gene <--APPLIES_TO-- 28 ACMGCriterion nodes           (all ACMG criteria ClinGen's VHL VCEP defines)
Criterion --REQUIRES--> 140 Requirement nodes          (5 strength-tiers x 28 criteria)
```
This is a complete, correct answer to "what is required," discovered by three real, populated edge types, at both the small-demo and full-harvest scale (identical counts: 28 criteria / 140 requirements both times).

**What did not work, by pure traversal:**
```
SUPPORTS[satisfies_requirement] edges pointing at ANY of the 140 Requirement nodes: 0
SUPPORTS[satisfies_requirement] edges anywhere in the full 270,262-edge real harvest: 0
```
`OBSERVATION --SUPPORTS[satisfies_requirement]--> RULE:Requirement` is legal per `ALLOWED_EDGE_COMBINATIONS` (`forge_ir/core.py:231-232`) — it is in the ontology precisely to let a graph walk answer "what satisfies this requirement" — but **it is never emitted by any of the 13 registered connectors**, and appears exactly zero times in the real, live, 51,560+-node panel_50k harvest (confirmed independently via `demo/graph_data.json`'s own `edge_stats.predicates` entry for `SUPPORTS`: 17,905 real edges, 100% of them `qualifier="corroborates"` — `CaseObservation -> ClinicalAssertion`, none `satisfies_requirement`).

**A naive fallback (subtype-name matching) doesn't just fail to generalize — it silently returns the WRONG answer.** At 3-hop BFS distance from the variant in the full harvest, 15 `PopulationFrequency` nodes are reachable — but **none of them is this variant's own gnomAD observation** (they belong to 15 *other* VHL variants, reached because gnomAD nodes connect back to Gene-adjacent structures through shared Submitter/ClinicalAssertion paths, not directly). Naively picking `next(n for n in subgraph if n.subtype == "PopulationFrequency" and n.attributes["dataset"] == "genome")` and feeding it to `evaluate_requirement()` produced:
```
status: CONTRADICTED
reason: {'constraint': 'faf95_popmax <= 1.56e-06', 'observed': 4.88e-06, ...}
```
— a **false CONTRADICTED verdict**, because the observation picked belongs to variant `3-10149861-A-G`, not `3-10149821-C-T`. The *correct* approach — filtering by the real `OBSERVES` edge into the exact variant id — found **zero PopulationFrequency observations** for this variant in the live snapshot at all. Checking why: the Variant node's own `attributes["dirty"] = True`, with `dirty_reasons: [{"connector": "gnomad", "reason": "deferred_budget", ...}]` (confirmed via `ingest/dirty.py`'s real mechanism, `mark_dirty`). The correct, honest answer traversal gives is *"this variant's gnomAD enrichment has not landed yet — check back"*, which only the dirty flag on the node itself communicates, and nothing in evidence-selection code currently reads that flag before selecting a candidate.

### What's concretely missing

What data **is** available on real nodes/edges (fields actually observed, not aspirational):
- Every `ForgeNode`: `id`, `role`, `subtype`, `layer`, `origin`, `lod`, `attributes` (free-form dict, source-specific), `provenance` (list of `SourceProvenance`/`DerivedProvenance`/`RuntimeProvenance`), `storage_scope`. No embedding/vector field of any kind, anywhere, on any node type.
- Every `ForgeEdge`: `src`, `dst`, `predicate`, `qualifier`, `attributes`, `provenance`, `origin`, plus `confidence`/`match_method`/`resolver_version` **only** on `REPRESENTS` edges (identity-resolution confidence, not a general edge-weight/relevance score).
- `Requirement` nodes carry `constraints: {metric, operator, value, fallback_if_unavailable, evidence_type}` — `evidence_type` (e.g. `"PopulationFrequency"`) is exactly the field a generic selector would need to match a Requirement to a candidate node subtype, and it is *present* in the real data (confirmed: `ClinGen:GN078:PM2:1.1:req:...` has `constraints.evidence_type == "PopulationFrequency"`) — but nothing reads it today, and (per §A.4) it isn't even validated as required at the location the ontology claims it should be.

What would concretely need to be added, to make traversal-to-context real (not hand-waved):
1. **A candidate-retrieval function**, e.g. `find_candidate_evidence(requirement_node, graph) -> list[ForgeNode]`, that reads `requirement.attributes["constraints"]["evidence_type"]`, matches it against reachable node subtypes **via the Variant's real edges** (`OBSERVES`, `ASSERTS`), not blind subtype-scanning within N hops — this is the single missing function that closes the gap demonstrated above, and the data to build it (`evidence_type`) already exists on every real Requirement node.
2. **Either populate `SUPPORTS[satisfies_requirement]` edges** (have the requirement-evaluation step write them back as `DERIVED`-origin edges once it runs — the ontology already permits this, it's simply never invoked) **or accept it will never be populated** and remove/reclassify that combination in the ontology so it stops silently implying a capability that doesn't exist.
3. **A relevance/ranking function** over multiple candidates (e.g. two `PopulationFrequency` nodes for genome vs. exome scope, or multiple `ClinicalAssertion`s of differing review status) — none exists; `evaluate_requirement` takes exactly one `observation: dict`, so something upstream has to have already chosen.
4. **A context-serialization function/module** — nothing in the repo turns a chosen `{Requirement, [candidate Evidence]}` bundle into a model-ready string/JSON payload with citations. The closest artifacts are `ingest/hashing.py::compute_structural_hash` (a hash, not a serialization for reading) and the demo visualization's event log (display-only, drops fields like full provenance chains).
5. **Dirty-awareness in context assembly** — a context builder needs to check `node.attributes.get("dirty")` and surface "evaluation incomplete, pending X" rather than silently treating absence-of-evidence as evidence-of-absence; the flag exists and is correctly set (confirmed above) but nothing reads it at query time today.

None of items 1-5 exist as code anywhere in this repository, including `forge_ir/requirement.py`, `orchestration/`, `analysis/`, or `scripts/`.

---

## C. Edge-case handling audit

| Scenario | Where handled | How to observe/test it | Automated test? |
|---|---|---|---|
| Batch fails a correctness check | `ingest/compiler.py::commit()` rejects the whole batch atomically, before identity resolution runs (`compiler.py:85-97`) | `.../scratchpad/adversarial_known_ids2.py`; real production log: `runs/panel_50k/harvest.log`, 63 `REJECTED` lines, e.g. `14:43:53 ! batch REJECTED (clinvar:VCV000237860): ... edge dst 'Ensembl:ENST00000252444' does not resolve to any known node` | **PASS** — `tests/test_compiler.py::test_commit_rejects_the_whole_batch_on_a_dangling_edge` |
| Single bad ClinVar record inside an otherwise-good run | Record-level QUARANTINE, `orchestration/runner.py:524-535`, catches `UnsupportedClinVarRecord` and any other parse exception, does not fail the batch | `connectors/clinvar.py`'s `UnsupportedClinVarRecord` (haplotype/genotype archives, no `ClassifiedRecord`) | **PASS** — exercised via `ClinVarParser` unit tests; real production: `summary.json` reports `quarantined: 91` out of 3,361 ingested |
| Connector enrichment (gnomAD/VEP/dbSNP) fails for one variant | `orchestration/runner.py:600-644::_enrich_with`, catches any exception, quarantines the *contribution* (keeps ClinVar evidence), optionally files a `RetryTicket` | force a `KeyError` via malformed gnomAD payload (§A.2) | **No dedicated test** for the malformed-payload path specifically (existing tests cover network-style failures, not shape-mismatch payloads) |
| Retry/dirty ledger: fetch failed vs. deferred vs. not-applicable vs. abandoned | `ingest/dirty.py` — `FETCH_FAILED`/`DEFERRED`/`NOT_APPLICABLE`, `PENDING`→`RESOLVED`/`EMPTY`/`ABANDONED` state machine | real production: `runs/panel_50k/summary.json`, `dirty.total=9215`, all `pending`, `by_connector: {dbsnp: deferred_budget=3069, ensembl_vep: {deferred_budget=3069, fetch_failed=8}, gnomad: deferred_budget=3069}` | **PASS** — `tests/test_dirty.py`, 10 tests covering marking, remarking, clearing, resolve/empty distinction, max-attempts→abandoned, not-applicable-never-retried |
| Malformed-but-reachable payload conflated with transient network failure | **Not distinguished** — both land as `FETCH_FAILED` in `runner.py:636-644`'s blanket `except Exception` | §A.2's gnomAD `KeyError: 'ac'` case: files a retry ticket that will deterministically fail identically on every retry until `ABANDONED`, wasting `max_attempts` retries on something a network retry can never fix | **No test** — genuine gap, not previously documented anywhere I found |
| Truncated JSONL retry ledger (killed mid-write) | `ingest/dirty.py::DirtyLedger.load()`, drops a partial trailing line, keeps the rest (`dirty.py:217-231`) | | **PASS** — `tests/test_dirty.py::test_a_truncated_final_line_costs_one_ticket_not_the_whole_ledger` |
| Truncated gzip *block* (not just a partial line) in a live harvest dump | `scripts/build_viz_dataset.py::read_dump` — handles `zlib.error` mid-stream, salvages everything read so far, reports a drop count | Directly hit this live during this audit: copying `runs/panel_50k/graph.jsonl.gz` and `graph.part002.jsonl.gz` while the harvest was still writing them produced exactly the documented `zlib.error: invalid block type`; `read_dump` recovered 4,951/39,380 nodes respectively with `dropped=1` each, rather than returning nothing | **PASS** — `tests/test_dump_recovery.py`, 3 tests, one of which is this exact scenario (`test_a_dump_truncated_mid_block_still_returns_what_was_written`) |
| Concurrent writers to the dirty ledger | `ingest/dirty.py::DirtyLedger` uses a `threading.Lock()` around `record`/`resolve`/`fail`/`_append` (`dirty.py:203-274`) | code inspection; docstring states "the harvest's enrichment workers write concurrently" | **No dedicated concurrency test** (no test spins up threads and checks for lost updates or interleaved writes) |
| Atomic checkpoint write (crash mid-write) | `orchestration/harvest.py::Checkpoint` — write-temp-then-`os.replace` (`harvest.py:335-341`) | code inspection; used in every real harvest run (`runs/panel_50k/checkpoint.json` exists and is current) | **No dedicated test found** for the atomic-replace behavior itself |
| Cross-run/cross-batch identity resolution (same canonical id from two sources) | `ingest/identity.py::resolve_identity` — MERGED (same id), REPRESENTS (different id, confidence ≥ 0.7), UNRESOLVED (different id, confidence < 0.7) | `.../scratchpad` not needed — real, exercised in production: `summary.json` reports `identity_merges: 8287` | **PASS** — `tests/test_identity.py`, but the confidence **boundary itself (exactly 0.7)** is untested — existing tests use 0.9 and 0.3, not 0.7 |
| Duplicate node/edge within one micro-batch | `ingest/compiler.py::_dedupe_nodes`/`_dedupe_edges` — dedupes by id/(src,predicate,qualifier,dst), **unconditionally `.extend()`s provenance with no cap and no duplicate-key filter** | stage the same node twice from two sources | **PASS for dedup itself** (`tests/test_compiler.py::test_dedup_merges_provenance_for_the_same_node_id_from_two_sources`), but **no cap** unlike the cross-batch path below — inconsistent |
| Duplicate node/edge across micro-batches (whole run) | `orchestration/runner.py::GraphStore.absorb` + `merge_provenance()` — dedupes by key **and** caps at `MAX_PROVENANCE_PER_NODE` (`runner.py:143-178`) | code inspection: "A node's provenance list is bounded, not unbounded. Real, measured problem at harvest scale" | **Likely tested** (not exhaustively re-verified here) but demonstrates the in-batch compiler path (§ above) lacks the same protection a single ClinVar record with many duplicate escape-hatch-derived provenance entries in one batch would not be capped |
| Genomic coordinate systems: gnomAD 1-based vs. SPDI 0-based | `forge_ir/core.py::normalize_spdi_position_from_1based`/`spdi_position_to_1based`, called from exactly one place (`ingest/identity.py::normalize_variant_key`) | | **PASS** — `tests/test_identity.py::test_off_by_one_spdi_normalization_matches_real_clinvar_value`, `test_clinvar_and_ensembl_already_0based_need_no_conversion`, `test_gnomad_and_clinvar_normalize_to_the_same_key` |
| Ambiguous "top-level placement" (dbSNP returns 2+ records marked primary) | **Not handled** — `connectors/dbsnp.py:65`, `next((p for p in placements if p.get("is_ptlp")), None)` silently takes the first, no warning | §A.2 | **No test** |
| Attribute-value domain sanity (negative counts, NaN/Infinity frequencies, coordinates out of range) | **Not handled anywhere** — `verify()`'s 9 checks are purely structural | §A.2 / §A.4 | **No test** — genuine, confirmed gap |
| Declared `required_attributes` per (role, subtype) | **Declared in the ontology, never enforced** (`forge_ir/core.py:86`) | §A.4 | **No test** — genuine, confirmed gap |
| Cross-batch edge from a `known_node_ids`-only node bypasses type-correctness | **Not handled** — see §A.3 | §A.3 | **No test** — genuine, confirmed gap, exists in the real production code path |
| Empty API response (gnomAD `variant: null`) | Handled explicitly and correctly, `connectors/gnomad.py:70-80`, with a detailed comment on why this is a legitimate negative result, not an error | | **PASS** — implicit in `test_connector_conformance.py`; confirmed live in real harvest (documented in the module's own docstring as "4/4 real variants in a small VHL sample hit this path") |
| Rate limiting / transient upstream failure | `connectors/http.py::request_with_retry` — retries 429/500/502/503/504 and connection/read timeouts, honors `Retry-After`, exponential backoff with jitter; explicitly does **not** retry 400/404/422 | | **No dedicated unit test for `http.py` found** (`tests/` has no `test_http.py`); behavior is documented and exercised implicitly by every live connector call |
| Ingestion checkpoint/crash-recovery contract | `ingest/checkpoint.py::IngestionCheckpoint` is a pure dataclass with **zero implementation** and its own docstring says so explicitly ("NOT exercised for real crash-recovery at VHL scale") — the actual, real, working checkpoint used in production is the unrelated `orchestration/harvest.py::Checkpoint` class | `grep -rn IngestionCheckpoint` finds it referenced nowhere outside its own file and design docs | **N/A — dead/placeholder code**, distinct from the real (tested) `Checkpoint` above; naming collision risk for a future reader |
| Reproducibility: same input → same graph, independent of dict-order/wall-clock | `ingest/hashing.py::compute_structural_hash`, excludes `retrieved_at`/timestamps, sorts nodes/edges canonically | | **PASS** — `tests/test_reproducibility.py`, 6 tests including two independent real-fixture parses hashing identically |

### Edge cases I identified from domain knowledge that appear genuinely unhandled anywhere

- **Genome build mismatch (GRCh37 vs GRCh38)**: `SourceProvenance` has a `snapshot_ref` field and the provenance docstring in `forge_ir/graph.py:6-8` explicitly cites "ClinVar's own record carries both GRCh38 and GRCh37 coordinates for the same gene" as a real motivating case, but `ForgeNode.attributes` for `Variant` only records a single `assembly` string (confirmed: `assembly, ref, alt` are the required attributes, `forge_ir/core.py:115`) — there is no code path that reconciles or even flags two SPDI ids for the same biological variant under different assemblies as the same variant; they would canonicalize to two entirely different `SPDI:` ids and never merge.
- **Multi-mapping / ambiguous transcripts**: `connectors/clinvar.py` explicitly handles a variant submitted against two genes (VHL/FANCD2 example, `clinvar.py:96-101`) by emitting multiple `in_gene` edges — this one IS handled correctly and is a positive finding, not a gap.
- **Somatic vs. germline conflation**: `CaseObservation.origin` is captured (`"somatic"` observed in real data, confirmed: `SCV007104365:observed_in:0` has `origin: 'somatic'` in the panel_50k harvest, §B), but ACMG requirement evaluation (`forge_ir/requirement.py`) has no origin-awareness at all — a somatic observation could be selected as evidence for a germline-only ACMG criterion with nothing in the codebase distinguishing the two at evaluation time. This is consistent with the broader §B finding (no real candidate-selection logic exists yet), but worth calling out specifically since ACMG guidelines are explicit that germline/somatic evidence must not mix.
- **REPRESENTS confidence boundary** at exactly 0.7 — untested (noted in the table above).

---

## Summary of A/B/C severity

- **A**: two real, demonstrated, currently-exploitable gaps in the "verify() catches every illegal graph" claim: (1) cross-batch type-correctness bypass (§A.3, present in the real production commit path), (2) no attribute-value sanity checking anywhere, compounded by `required_attributes` being declared but dead code (§A.4). The single-batch, single-connector defense (the case the existing test suite actually exercises) works correctly and was reconfirmed here.
- **B**: Phase 2/3 (attention, context-building) are confirmed **not implemented**, matching the companion document's claim. The "what's required" half of context-building works today by pure graph traversal (no gaps found). The "what evidence satisfies it" half has zero graph-native support — the enabling edge type exists in the ontology but has never been populated by any connector, in real production data at any scale — and a naive fallback was shown to produce an actively wrong answer (evidence from a different variant), not just an incomplete one, until edge-filtered correctly, at which point it correctly surfaces "no evidence yet, this variant is dirty" instead.
- **C**: most edge cases in the domain (rate limiting, quarantine, dirty/retry, truncated-file recovery — twice over, including a genuinely live truncation encountered during this audit) are handled and tested well. The gaps are the three flagged as "No test" + "genuine, confirmed gap" rows, plus the two identified from domain knowledge (assembly-mismatch identity, somatic/germline conflation) that have no handling anywhere.
