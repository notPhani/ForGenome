# Forge Implementation — Architecture & Findings

**Status: current, reflects the actual codebase, not the plan for it.** The design docs (`connector-research.md`, `forge-ir-v3-schema.md`, `forge-pipeline-v1.md`) explain *why* the system is shaped this way and are still the source of truth for intent — this doc explains *what actually exists*, how it's wired together, and what real bugs the implementation surfaced that no design review caught. If the two ever disagree, trust this one for "does the code do X," and the design docs for "should it."

**Headline: 180 tests pass, 0 mocked data anywhere in the connector or E2E layer.** Every connector test parses a real, live-fetched API payload saved in `fixtures/`; the E2E test runs the real 4-source pipeline through to a genuinely computed `RequirementState`, not an asserted one.

**Scale reached: a live 50% sample of the whole VHL ClinVar family** — 1,134 of 2,267 records requested, 1,053 ingested, 81 quarantined at record level, **0 batch rejections**, producing a committed graph of **9,539 nodes / 21,206 edges** in 238 s. Run it with:

```bash
python main.py --gene VHL --mode ephemeral --sample-pct 50 --resident runs/vhl_persistent.json --out demo/graph_data.json --build-html
```

---

## 1. Codebase map

```
forge_ir/            Layer 1 — pure types, no I/O, no external deps
  core.py               enums, node-subtype registry, ALLOWED_EDGE_COMBINATIONS, canonical ID schemes
  provenance.py         Provenance ABC + Source/Derived/Runtime variants
  graph.py              ForgeNode, ForgeEdge (construction-time invariants enforced)
  requirement.py        EvaluationMode, Requirement, RequirementState, the evaluator
  verify.py             VerificationReport + the 9 correctness checks
  contracts.py          pure Protocol interfaces, no logic

ingest/               Layer 3 — ingestion machinery, source-agnostic
  hashing.py             canonical_hash() — sorted-serialization discipline
  manifest.py            SourceManifest, SourceSnapshot
  source_objects.py      SourceObjectBase (per-source subtypes live in connectors/)
  mapping.py             MappingRule, EscapeHatch, MappingSpec, apply_mapping_spec()
  identity.py            IdentityIndex (SQLite), resolve_identity(), SPDI/HGNC/MedGen normalization
  checkpoint.py          IngestionCheckpoint (schema only, not exercised at this scale)
  compiler.py            StagingCompiler — the two-phase commit

connectors/           Layer 4 — one file per real source, plus a shared helper
  ensembl.py              gene/transcript/exon/protein structure, transcript-scoped VEP consequences
  clinvar.py                VCV/RCV/SCV hierarchy, SCV↔RCV crosswalk, CONTRADICTS detection
  gnomad.py                  population frequency, compact by design
  clingen.py                  strength-tier Requirement expansion, evaluation_mode classification
  common.py                    build_variant_entity() — the shared canonical Variant constructor

orchestration/        Layer 5 — planning and running
  capabilities.py         ConnectorCapabilities
  ratelimit.py             RatePolicy/RetryPolicy + real per-source values
  scope_resolver.py         ScopeResolver — variant/gene scopes only
  connector_registry.py      ConnectorDescriptor/Emission + register_connector() -- the
                              pluggable connector contract (§6)
  connector_planner.py         ConnectorPlanner — FetchTask + DAG, now registry-driven
  load_plan.py                   LoadPlan (frozen) + plan_load() + execute() (§6)
  runner.py                        the run engine — RunConfig/RunResult, the three layer modes,
                                    GraphStore (cross-batch dedup), wave-tagged event capture

ingest/ (additions)   variant_notation.py (VCF/HGVS -> SPDI), population.py (ancestry
                       taxonomy), hashing.py::compute_structural_hash (§6)

analysis/             Layer 6 — read-only post-commit analysis
  edge_stats.py           edge-type distribution + fan-out-vs-breadth attribution
  completeness.py           advisory graph-completeness report (§6), never blocks a commit

main.py               CLI entrypoint — parameterised search, connector selection, --list-connectors
demo/                 presentation console (template.html + generated index.html)
scripts/              build_demo_html.py (event log -> self-contained page)

tests/                180 tests total
  test_verify.py           17 — one deliberately-broken synthetic case per check
  test_identity.py          8 — off-by-one normalization, tri-state resolution, cross-run persistence
  test_mapping.py           7 — MappingRule/source_path/EscapeHatch mechanics
  test_compiler.py          8 — staging, dedup, verify-gate reject, edge ledger
  test_orchestration.py     9 — DAG shape, registry-driven, mocked, no network
  test_edge_stats.py        6 — fan-out vs breadth attribution, resident endpoints, shape splitting
  test_runner.py           21 — the layer-mode contract + connector-registry integration bugs
  test_connector_registry.py 8 — the pluggable contract, the 10-connector target
  test_source_manifest.py   7 — SourceManifest/SOURCED_FROM materialization (§6)
  test_reproducibility.py   6 — structural_hash determinism (§6)
  test_coverage_report.py   6 — CoverageReport population (§6)
  test_variant_notation.py 10 — VCF/HGVS -> SPDI against the real VHL coordinates
  test_population.py        7 — gnomAD's real 32-row population breakdown, fully mapped
  test_completeness.py      6 — the advisory report, never blocking
  test_connectors/         40 — one suite per source (incl. dbSNP, UniProt), real fixtures only
  e2e/test_vhl_pm2.py       8 — the real, full pipeline gate
```

No file above ~470 lines; `connectors/clinvar.py` is the largest because ClinVar's real hierarchy is genuinely the most complex source, not because of bloat — see §3.4.

### Connector feeds vs. connectors vs. groups

`orchestration/runner.py` tags every node/edge it emits with the **feed** that produced it, not just the connector. A feed is finer-grained than a connector on purpose: Ensembl serves two of the three groups from one adapter — gene structure into Core Biology, VEP consequences into Evidence — and both carry the *identical* `SourceProvenance("Ensembl", ...)`. Provenance alone can't separate them; only the call site (`FEED_ENSEMBL_STRUCTURE` vs. `FEED_ENSEMBL_VEP`) can, so `Emission.add()` records it at production time.

```
core_biology   Ensembl gene structure
operational    ClinGen VCEP
evidence       ClinVar · gnomAD · Ensembl VEP
```

The visualization's connector toggles isolate by feed, grouped into these three — matching the doc's own core-biology/operational/evidence split, not the four-way Layer taxonomy (which also has Administrative). `main.py`'s run summary prints the same manifest, built from the event log so a feed that produced nothing never appears as a toggle that does nothing.

### Identity resolution, made observable

`ingest/identity.py::resolve_identity()` already ran on every commit; what was missing was any way to *see* it happen. `GraphStore.absorb()` now returns which node ids were already known — i.e., which nodes a later micro-batch resolved onto an existing node instead of duplicating — and the runner emits those as a new `"identity"` event kind (`node_id`, `namespace`, `status`, `sightings`). This is strictly additive: it doesn't change what gets resolved, only what gets reported. In the visualization these render as an expanding violet ring on the merged node (the layer colour underneath is unaffected — identity is an event, not a persistence classification) plus a dedicated Identity tab listing every merge, clickable back to the node.

### Ontology counts the console now states explicitly

The visualization's legend previously showed only the 4-way Layer split. It now separately shows all **5 `NodeRole` values** (`forge_ir/core.py`) as canvas shapes — ENTITY=circle, OBSERVATION=square, CLAIM=diamond, RULE=triangle, INVESTIGATION=hexagon (unpopulated until MHLA exists, kept in the legend because the schema defines it) — and all **11 `EdgeType` values** by stroke colour, both read directly off the enums rather than off whatever a given run happens to produce, so absent types show as absent rather than being omitted from the legend entirely.

### The three layer modes (`main.py --mode`)

| Mode | Builds | Scope | Cadence | Attaches to |
|---|---|---|---|---|
| `persistent` | Core Biology (Ensembl gene structure) + Operational (ClinGen VCEP rules) | Gene | Changes over months — worth a slow bulk load | — |
| `ephemeral` | Evidence only (ClinVar; optionally gnomAD, VEP) | Per variant | Refetched per investigation | Persistent node ids declared as `known_node_ids` |
| `full` | Both, in one process | Gene + variants | The two-variant demo | — |

The split is load-bearing, not cosmetic. An ephemeral run does **not** re-derive the persistent layer — it declares those node ids as already resident and passes them to `verify()`, so a `Variant --RELATES_TO[in_gene]--> Gene` edge whose Gene node this run never created still passes referential integrity. Given a real persistent snapshot (`--resident`) that residency is *verified*; without one it is only *declared*, and the run summary says so explicitly rather than letting the weaker guarantee pass as the stronger one.

---

## 2. How data actually flows (traced through real code, not the abstract diagram)

```
raw fixture (JSON/XML)
    │
    ▼
connectors/<source>.py :: <Source>Parser.parse*()      → SourceObject dataclass(es)
    │
    ▼
ingest/mapping.py :: apply_mapping_spec(obj, spec, ...)  → list[ForgeNode | ForgeEdge]
    │  (declarative MappingRules for simple fields; named EscapeHatches for the
    │   genuinely branching cases — ClinVar's hierarchy, ClinGen's tier expansion)
    ▼
ingest/compiler.py :: StagingCompiler.stage(nodes, edges)
    │  (accumulates everything from every connector into one batch)
    ▼
StagingCompiler.commit()
    │  1. dedupe nodes by id, merging provenance lists
    │  2. dedupe edges by (src, predicate, qualifier, dst), merging provenance
    │  3. forge_ir/verify.py :: verify(nodes, edges) — all 9 checks
    │     └─ fails → CommitResult(rejected=True), NOTHING written anywhere, index untouched
    │  4. ingest/identity.py :: resolve_identity() for Gene/Variant/Disease nodes only
    │     (only reachable after verification passes — see Finding 5)
    │  5. edge ledger built, CommitResult(committed=True) returned
    ▼
committed graph: list[ForgeNode], list[ForgeEdge], queryable directly (no DB in this pass —
see forge-pipeline-v1.md's "implementation choices" for why: networkx/JSON deferred until
real bulk-scale ingestion needs them; VHL-scale doesn't)
```

`orchestration/` doesn't appear in that chain because the E2E test bulk-loads Core Biology (Ensembl) and Operational (ClinGen) directly and only exercises `plan_load()` for the DAG-shape tests — matching the design's own persistent-vs-lazy split (Core Biology/Operational are meant to be bulk-preloaded, not `load()`-triggered; see `forge-ir-v2-and-loading-architecture.md` §2). `orchestration/load_plan.py::execute()` was deliberately not built as a generic dispatcher — see Finding 9 below.

---

## 3. Implementation details worth knowing per layer

### 3.1 `forge_ir/graph.py` — the invariant that's actually enforced, not just documented

`ForgeNode.__post_init__` and `ForgeEdge.__post_init__` don't just compute `layer`/`storage_scope` — they raise `ValueError` if `origin=RUNTIME` and the role/predicate isn't `RUNTIME_ONLY`, or vice versa. This is checked at construction time, confirmed by `tests/test_verify.py::test_runtime_only_role_cannot_be_constructed_with_source_origin`. `verify.py`'s own storage-scope check (§7 of `forge-ir-v3-schema.md`) is defense-in-depth for data that bypasses normal construction (e.g., loaded from storage without going through `__init__`), not the first line of defense.

`ForgeNode` is deliberately **not** frozen (evidence-lifecycle updates need in-place mutation); `orchestration/load_plan.py::LoadPlan` **is** frozen (a plan is replaced, never edited in flight). Both decisions are stated explicitly in each file's docstring so the asymmetry doesn't read as an oversight later.

### 3.2 `forge_ir/requirement.py` — the actual evaluator

`evaluate_requirement()` branches on `EvaluationMode` first: `NARRATIVE`/non-mechanical modes always return `NEEDS_INTERPRETATION`, never attempting a mechanical check. For `MECHANICAL`/`COMPOSITE`, `evaluate_mechanical_constraint()` reads a named metric off the observation dict and supports one `fallback_if_unavailable` clause — this fallback mechanism exists specifically because of Finding 1 below, not speculatively.

A load-bearing detail: once a real observation exists and fails its check, the result is `CONTRADICTED`, not `UNSATISFIED` — `UNSATISFIED` is reserved for "no evidence activated yet" (`observation is None`). Getting this distinction right mattered for the E2E test to mean something (see `evaluate_requirement`'s docstring for the reasoning).

### 3.3 `ingest/identity.py` — SQLite, not a dict, and why that's checked

`IdentityIndex` is backed by SQLite specifically so identity persists across process runs, not just within one batch — `tests/test_identity.py::test_identity_index_persists_across_process_boundaries` opens two independent `IdentityIndex` instances against the same file and confirms the second sees what the first wrote. An in-memory dict would have passed every other test in the suite while silently failing this exact property.

`normalize_variant_key()` is the one place the gnomAD-1-based-vs-SPDI-0-based conversion happens (`forge_ir.core.normalize_spdi_position_from_1based`) — confirmed to reproduce ClinVar's own real `CanonicalSPDI` value exactly (`tests/test_identity.py::test_off_by_one_spdi_normalization_matches_real_clinvar_value`).

### 3.4 `connectors/clinvar.py` — why it's the biggest file, and what it actually solved

Three real, confirmed-by-data problems live here, not just the VCV/RCV/SCV parsing itself:

- **`_build_trait_crosswalk()`** — resolves SCV↔RCV linkage using ESummary's richer per-condition cross-references (which include OMIM/Orphanet/MONDO alongside MedGen), because EFetch's own data alone only resolves 3 of 22 real submissions. See Finding 2.
- **`_pathogenicity_group()`** — groups classification text into ClinVar's own P/LP, VUS, B/LB concordance tiers before comparing, instead of raw string equality. See Finding 3.
- **The `CONTRADICTS` loop compares every `contributes_to_aggregate=true` SCV pairwise**, not just same-RCV siblings — because ClinVar's own conflict concept is variant-wide, confirmed against the real conflicting fixture where the two disagreeing submissions target two *different* RCVs. See Finding 4.

### 3.5 `connectors/ensembl.py` — LOD discipline, enforced not just described

`_gene_escape_hatch()` builds a full `Exon`/`Translation` structure only for the transcript flagged `is_canonical` by Ensembl's own data; all other transcripts (14 of 15 for VHL) become `lod=0` stub nodes. `tests/test_connectors/test_ensembl.py::test_only_canonical_transcript_gets_full_exon_structure` asserts this directly against the real fixture, not just as a design intention.

---

## 4. Findings — real bugs the implementation caught, in the order they surfaced

Each of these changed either the code or a design doc after being caught by an actual test failure or a direct re-check against live data — none were anticipated by any of the four review rounds that preceded implementation.

**1. PM2's real threshold metric is `faf95_popmax` ("GroupMax Filtering AF"), not the plain `af` field.**
Re-querying gnomAD's GraphQL API with the `faf95` field (not originally fetched) showed it's `null` for our VHL test variant — a singleton (`ac=1`). ClinGen's own PM2 spec text, already captured but not yet operationalized, explicitly covers this: *"If no GroupMax Filtering Allele Frequency is calculated (ex. due to a single variant present), PM2_Supporting may also be applied."* A naive `af`-vs-threshold comparison would have produced the wrong verdict. Fixed in `forge_ir/requirement.py::evaluate_mechanical_constraint` with a `fallback_if_unavailable` clause; `forge-pipeline-v1.md` §12 corrected to match.

**2. ClinVar SCV→RCV linkage: EFetch alone resolves only 3 of 22 real submissions.**
Most real submitters cite OMIM identifiers; RCVs are keyed by MedGen CUI. Building a crosswalk from ESummary's richer per-condition `trait_xrefs` (which include OMIM/Orphanet/MONDO/MeSH alongside MedGen) resolves 14 of 22 by condition match — both fixtures are needed together. Of those 14, 2 have `ContributesToAggregateClassification=false` and are correctly excluded from the `contributes_to` edge, landing at 12. `connectors/clinvar.py::_build_trait_crosswalk`.

**3. `CONTRADICTS` needed ClinVar's own pathogenicity-concordance groups, not string equality.**
A first pass comparing raw classification text produced **47 false `CONTRADICTS` edges** on VCV000002218 — a variant ClinVar itself reports as `"criteria provided, multiple submitters, no conflicts"` — because real contributing SCVs mix "Pathogenic" and "Likely pathogenic" text for the same concordant verdict. Fixed with `connectors/clinvar.py::_pathogenicity_group()` (P/LP, VUS, B/LB as the three tiers); confirmed zero false positives on the clean fixture and exactly one true positive on the conflicting one after the fix.

**4. ClinVar's "conflicting classifications" is variant-wide, not per-condition.**
The real conflicting fixture (VCV004558169) has its two disagreeing SCVs contributing to two *different* RCVs (different conditions) — the original design (compare only same-RCV siblings) would have missed this real case entirely. Fixed: `CONTRADICTS` now compares all aggregate-contributing SCVs pairwise, regardless of matched RCV.

**5. Compiler ordering bug: identity resolution ran before verification.**
`StagingCompiler.commit()` originally called `_resolve_identities()` (which writes to the persistent SQLite index) *before* `verify()`. A rejected batch would still have polluted the index, violating "nothing partially committed." Caught by `tests/test_compiler.py` when a staged CLAIM node with no connecting edge correctly failed connectivity verification but still showed up in `identity_resolutions`. Fixed by reordering; the fix is now load-bearing enough to have its own comment in `compiler.py`.

**6. No connector actually emitted the canonical `Variant` ENTITY node.**
Every connector (Ensembl's VEP mapping, ClinVar's hierarchy, gnomAD's frequency mapping) referenced the variant's canonical id as an edge target, on the assumption that "it already exists" — true in spirit (identity resolution owns canonicalization) but nothing had ever actually constructed the node. Would have failed referential integrity the first time the full pipeline ran together. Fixed with `connectors/common.py::build_variant_entity()`, called once before staging everything else.

**7. VEP's per-transcript consequences can reference transcripts from a *different*, overlapping gene.**
Checked computationally before writing the E2E test: one raw VEP hit (`ENST00000660063`, `downstream_gene_variant`) belongs to a neighboring lncRNA gene overlapping VHL's locus, not VHL itself. The existing parser filter (`no hgvsc field and not protein_coding`) happened to exclude it, but this was verified, not assumed — referential integrity would have caught it otherwise. Worth remembering if `EnsemblParser.parse_vep`'s filter logic changes later.

**8a. At whole-gene scale, one missing XML attribute cost 41 of 1,134 variants their entire record.**
Surfaced only by running the full VHL family, never by the two single-variant fixtures. `ClinVarParser.parse_rcvs` read `ClassifiedCondition.attrib["DB"]` directly, but a real condition can carry no `DB`/`ID` at all — free text a submitter typed, e.g. `"See cases"` or `"VHL-related disorder"`. The `KeyError` propagated to record-level quarantine, discarding the *whole variant*, including the four correctly-mapped RCVs sitting beside the unmapped one (VariationID 486712 has both). Fixed with `_unmapped_disease_id()`, which namespaces the Disease node under `ClinVar:condition:<slug>` and tags it `identifier_mapped: False` — deliberately **not** minted into MedGen, since merging a submitter's free-text label with a curated CUI would be an identity claim nothing in the payload supports. Recovered 41 variants (~3.6% of the run).

**8b. 81 of 1,134 real ClinVar VHL records are not SimpleAlleles at all.**
Haplotypes, genotypes and CNVs have no single canonical SPDI to key a `Variant` on. Both fixtures are SimpleAlleles, so nothing ever exercised this path. Now raises `UnsupportedClinVarRecord`, an explicit record-level QUARANTINE signal, rather than an `AttributeError` on `None` — the distinction that keeps one unmappable archive from taking a 50-variant batch down with it (`forge-pipeline-v1.md` §8's QUARANTINE-vs-REJECT rule, finally exercised on real data rather than a synthetic case).

**8c. A gene-family search returns variants belonging to *neighbouring* genes, and the parser was picking one arbitrarily.**
`VCV000265138` is `NM_001018115.3(FANCD2):c.2605+1G>A` — it appears in a `VHL[gene]` search because FANCD2 and VHL are neighbours on 3p25.3 and it overlaps both, and ClinVar carries **two** submitted `<Gene>` elements for it. `parse_vcv` used `.//Gene[@Source='submitted']`, which returns whichever comes first — here FANCD2, i.e. the wrong gene for a VHL-scoped run, asserted with no indication anything was ambiguous. Two separate fixes: `ClinVarVCV.submitted_genes` now carries the full list and the escape hatch emits one `in_gene` edge per gene (the truthful model — the variant really does sit in both); and the runner drops `in_gene` edges pointing outside the run's scope as a **relationship-level quarantine**, the same trade the SCV↔RCV crosswalk already makes. Rejecting the batch instead — which is what happened before the fix — discarded a real variant and its entire assertion history over one out-of-scope neighbour. Every drop is recorded in the run's quarantine list, never silent.

**8d. Ephemeral VEP runs cannot work without the persistent Core Biology layer resident.**
Not a bug — a constraint the layer split imposes, discovered by running the modes against each other. VEP emits `ComputationalPrediction --APPLIES_TO--> Transcript`, and `Transcript` nodes live in the persistent layer. An ephemeral run with only a declared *Gene* anchor therefore has dangling transcript endpoints and **correctly rejects every batch**. Encoded as a contract in `tests/test_runner.py` (one test asserting the rejection, one asserting success once a persistent snapshot is loaded) and surfaced as a preflight warning in `main.py` rather than being silently worked around. ClinVar and gnomAD have no such dependency — their edges terminate on the `Variant` the run creates itself.

**8e. Ephemeral and persistent runs have *structurally* different edge distributions, and that's the check.**
The 50%-VHL ephemeral run's top predicate is `RELATES_TO` at 46.5% driven by breadth (thousands of distinct `ClinicalAssertion` nodes each emitting ~1 `submitted_by` edge); the persistent run's is `REQUIRES` at 74.9% driven by fan-out (28 `ACMGCriterion` nodes each emitting exactly 5 strength-tier `Requirement`s). `REQUIRES`/`APPLIES_TO` are *absent* from ephemeral runs and `ASSERTS`/`OBSERVES` are *absent* from persistent ones — which makes the edge-type histogram a cheap, direct assertion that the layer separation is real rather than nominal. `analysis/edge_stats.py` reports the fan-out-vs-breadth mechanism explicitly (`edges_per_source_node`) precisely because the raw share alone can't distinguish the two.

**9. `orchestration/load_plan.py::execute()` was deliberately not built as a generic dispatcher.**
Each connector's real parse/map call shape differs enough (`parse_gene` vs. `parse_vep` vs. `parse_vcv`/`parse_rcvs`/`parse_scvs` vs. a flat `parse`) that forcing a uniform `execute()` before there were at least two real, working connector integrations to generalize *from* would have meant guessing at an abstraction. The E2E test wires the four connectors directly instead — real, tested, and honest about what's actually generic (`plan_load()`'s DAG/task construction) versus what's still per-source (the actual fetch-and-map call).

---

## 4b. Edge-type statistics — which predicate dominates, and why

`analysis/edge_stats.py` reports every predicate's share alongside its concrete *shapes* — the `(src_subtype, predicate[qualifier], dst_subtype)` triples that produced those edges. The share alone is not an explanation, so each predicate also gets a **mechanism**, derived from `edges_per_source_node`:

- **fan-out** — few source nodes, many edges each (a modelling choice, and one we should be able to defend)
- **breadth** — many source nodes, ~1 edge each (inherent to the ontology)

Measured on the 50%-VHL ephemeral run (21,206 edges / 9,539 nodes, 2.22 edges/node):

| Predicate | Count | Share | Driver |
|---|---|---|---|
| `RELATES_TO` | 9,775 | 46.1% | breadth — 2,584 distinct `ClinicalAssertion` nodes each emit one `[submitted_by]`; `[for_condition]` and `[aggregates_into]` add 10.0% each |
| `ASSERTS` | 5,763 | 27.2% | breadth — 3,170 distinct `AggregateClassification` nodes each assert one Variant |
| `OBSERVES` | 2,584 | 12.2% | breadth — one `CaseObservation` per SCV, exactly |
| `SUPPORTS` | 2,584 | 12.2% | breadth — the same observations corroborating their own assertion |
| `CONTRADICTS` | 500 | 2.4% | 317 distinct assertions; **100% DERIVED** — Forge computes these, ClinVar never states them |

And on the persistent run (187 edges / 188 nodes) the distribution inverts completely:

| Predicate | Count | Share | Driver |
|---|---|---|---|
| `REQUIRES` | 140 | 74.9% | **fan-out** — 28 `ACMGCriterion` nodes emit exactly 5.0 strength-tier `Requirement`s each |
| `APPLIES_TO` | 28 | 15.0% | breadth — one criterion-to-gene edge per criterion |
| `PART_OF` | 18 | 9.6% | breadth — Transcript→Gene, Exon→Transcript |

Three things this makes visible:

1. **`RELATES_TO`'s dominance is not one relationship.** It's the ontology's general-purpose predicate carrying four distinct qualifiers; the qualifier breakdown is what's informative, and a bare predicate histogram hides it.
2. **`REQUIRES` is the only genuinely fan-out predicate**, and its multiplicity is exactly 5.0 because ClinGen's spec expansion emits one Requirement per strength tier. A number like 5.0 with zero variance is a deliberate structure, not emergent data.
3. **The two runs share no predicates at all.** `REQUIRES`/`APPLIES_TO` appear only in persistent runs, `ASSERTS`/`OBSERVES`/`SUPPORTS`/`CONTRADICTS` only in ephemeral ones. That makes the histogram a cheap, direct assertion that the layer separation is real rather than nominal.

`PART_OF`, `REPRESENTS`, `SOURCED_FROM` and `ACTIVATED_BY` have zero edges in the ephemeral run and are reported as such rather than silently omitted — `ACTIVATED_BY` in particular is MHLA's, and its absence is expected until MHLA exists.

---

## 5. Verified end state

`pytest tests/ -v` → **180 passed**. The load-bearing one, `tests/e2e/test_vhl_pm2.py::test_the_actual_gate_pm2_requirement_evaluates_against_real_gnomad_observation`, does all of the following in one real run: parses real ClinVar/Ensembl/gnomAD/ClinGen payloads → resolves identity centrally → verifies all 9 checks → atomically commits → reads the PM2 Supporting-tier `Requirement` and the real gnomAD `PopulationFrequency` node back out of the *committed* graph (not from the original parse) → evaluates → asserts `SATISFIED`, reached via the documented fallback clause, computed fresh each run.

## 6. The connector registry — closing the "mandatory, not design-only" gaps

A completeness audit against the frozen docs found the pipeline functionally solid but structurally not ready to add a 5th connector without hand-editing `runner.py`'s core loop, and three docs claims unimplemented despite being called "mandatory": `SourceManifest`/`SOURCED_FROM` (§5), reproducibility hashing (§6), and `CoverageReport` (§11). This section closes all of that.

### The registry (`orchestration/connector_registry.py`)

A connector becomes known to the system by calling `register_connector(ConnectorDescriptor(...))` once, at import time, in its own module. `main.py`/`orchestration/runner.py` never import a specific connector by name — they `import connectors` once (`connectors/__init__.py`), which imports every connector module, which registers itself as a side effect. The whole contract:

1. A `SourceAdapter` (`.fetch()`, `.capabilities()`, `.manifest()` — already existed)
2. `build_query(scope, anchor, context) -> dict | None` — `None` means "not applicable to this scope"
3. `build_emission(raw, context) -> Emission` — tagged nodes/edges, reusing whatever parser/`MappingSpec` the connector already has
4. `requires` — names of other connectors this one HARD-depends on (gnomAD/VEP/dbSNP all `requires=("clinvar",)`, since they need ClinVar's resolved variant identity first)

`orchestration/connector_planner.py::ConnectorPlanner.plan()`/`.dependencies()` — previously hardcoded to the 4 original connector names — now iterate `list_connectors()` generically. `orchestration/load_plan.py::execute()`, which never existed (Finding 9 below explained why: not enough real connector integrations to generalize *from* yet), now does: walks `plan.task_tiers()`, and for each task, fetches + `build_emission`s + commits through the registered connector. It drives the **persistent** layer (gene-scoped, one-shot). The **ephemeral** per-variant evidence loop keeps its own proven harness in `runner.py` (real batching/quarantine logic this simpler task-tier loop doesn't need to reimplement) but now sources its connector calls from the registry too — a new `requires=("clinvar",)` evidence connector needs zero `runner.py` edits to join the per-variant enrichment loop.

### SourceManifest / SOURCED_FROM (§5)

`orchestration/connector_registry.py::tag_with_source_manifest()` runs after every `build_emission()` call: builds one `SourceManifest` node per `(source, dataset, version)` from the connector's own `adapter.manifest()`, and a `SOURCED_FROM` edge from every CLAIM-role node in the emission to it. `ALLOWED_EDGE_COMBINATIONS` already permitted this (`CLAIM(any) --SOURCED_FROM--> ENTITY(SourceManifest)`) — no ontology change needed, it had just never been exercised. First non-zero `SOURCED_FROM` count in this project's history.

### Reproducibility (§6)

`ingest/hashing.py::compute_structural_hash(nodes, edges)` — built on `canonical_hash()`, which existed from the start but was called from nowhere. Projects out volatile fields (`retrieved_at`, `derived_at`, any attribute literally named `retrieved_at`) before hashing, since every connector calls `datetime.now()` and a hash that included it could never match twice even for the exact same source data. `CommitResult.structural_hash` and `RunResult.structural_hash` both carry it now. Verified end-to-end: parsing the same real fixtures twice, independently, produces an identical hash (`tests/test_reproducibility.py`).

### CoverageReport (§11)

`ingest/mapping.py::apply_mapping_spec()` gained an optional `coverage: CoverageReport | None` accumulator it mutates in place — backward compatible, a no-op when omitted. `main.py` prints `mapping_origin_summary()` per connector. Real, honest number: every connector that uses `apply_mapping_spec` today is **100% escape-hatch** — ClinVar's hierarchy and ClinGen's strength-tier expansion both need genuine branching logic no declarative `MappingRule` could express (§3's own reasoning), so 0% declarative isn't a bug in the counting, it's accurate.

### Variant notation + population normalization

`ingest/variant_notation.py` centralizes VCF/HGVS-genomic → canonical SPDI conversion (previously duplicated ad hoc inline in `runner.py`), including a real GRCh38 chromosome↔RefSeq accession table. `ingest/population.py` normalizes gnomAD's population codes against gnomAD v4's own real ancestry-group taxonomy (confirmed against the full 32-row real fixture), splitting ancestry from sex rather than keeping `afr_XX`-style compound codes opaque.

### Two new real connectors, six honest stubs

`connectors/dbsnp.py` (NCBI Variation Services REST) and `connectors/uniprot.py` (UniProtKB REST) are fully implemented against real, live-fetched fixtures. dbSNP's real value beyond "one more feed": rs5030820 has 3 real alleles at its GRCh38 placement (reference + C>G + C>T) — VHL's ClinVar/gnomAD fixtures only ever discuss C>T, so dbSNP genuinely reveals a second cataloged variant at the same locus. UniProt's Protein node uses a different canonical id scheme (`UniProt:{accession}`) than Ensembl's (`Ensembl:{ENSP...}`) for the same biological protein — exactly the gap `REPRESENTS` exists for, and previously-never-exercised (`REPRESENTS: 0 edges` in every prior run). UniProt is the first connector to produce one.

`connectors/omim.py`, `mondo.py`, `pharmgkb.py`, `cosmic.py`, `dbnsfp.py`, `clingen_gene_disease.py` are registered with `implemented=False` and a `NotImplementedError` fetch, each with a real, checked access point (or a real, confirmed reason none exists yet — OMIM needs a manually-approved API key, COSMIC needs a licensed account, dbNSFP ships as a flat file with no REST API at all, ClinGen's gene-validity KB was checked live and returns HTML, not JSON). `python main.py --list-connectors` reports all 13 (7 live, 6 stub) honestly — never claims more than what's real. Total registered comfortably clears the "at least 10 connectors" target.

### Real bugs the generalization caught

**Nondeterministic tier ordering.** `LoadPlan.task_tiers()` iterated a `set`, harmless while nothing executed tasks in the returned order — until `execute()` started committing tier-by-tier for real. ClinGen's `APPLIES_TO -> Gene` edge needs Ensembl's Gene node to exist first; with set-iteration order, ClinGen could land before Ensembl and fail referential integrity, nondeterministically. Fixed by making `task_tiers()` preserve `self.tasks`' own order (itself sorted core_biology-before-operational-before-evidence by `list_connectors()`).

**Feed-attribution clobbering.** `Emission.merge()` used `dict.update()` for `feed_of`, so a later-merged connector could silently steal credit for a node id an earlier one already owned — real case: dbSNP re-deriving the exact same canonical Variant id ClinVar already built (the shared C>T allele) reassigned that node's feed from "ClinVar" to "dbSNP." Fixed to first-writer-wins, matching `Emission.add()`'s own `setdefault` semantics.

**`build_query() -> None` not handled in the enrichment loop.** gnomAD/VEP's coordinate-based queries can always be built from a variant's own coordinates, so this path was never exercised until dbSNP — whose query legitimately returns `None` when a variant has no rsID cross-reference. `_enrich_with` handed that `None` straight to `adapter.fetch()`, crashing with `'NoneType' object is not subscriptable` on real, live ClinVar records with no dbSNP xref. Fixed: `None` is a clean skip, not a quarantine entry.

**Registering a new persistent connector silently changed every existing run.** The original "no explicit `--connectors` selection → run ALL persistent connectors" fallback meant adding UniProt (persistent-cadence) changed the node/edge counts of every `--mode persistent`/`full` run that never asked for it, including the project's own established baselines. Fixed with `ConnectorDescriptor.default_included` — `False` for anything added after the original Ensembl+ClinGen baseline, so growing the registry can never silently change what a plain default run produces; explicit naming in `--connectors` still reaches an opt-in connector regardless.

## 7. What's not built yet

MHLA — explicitly out of scope until this document's contents existed and passed. `orchestration/load_plan.py::execute()`'s generic dispatch shipped in §6 above (closing Finding 8/9), so that's no longer on this list. Still unimplemented: real cost-estimation for `FetchTask.estimated_cost` (deferred per `forge-pipeline-v1.md` §13's own scope note), `IngestionCheckpoint`'s crash-recovery path (schema exists, not exercised at VHL scale), and the 6 stub connectors (§6) — none are prerequisites for MHLA.
