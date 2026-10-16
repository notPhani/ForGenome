# Forge — Blind System Map

*Compiled by reading the code, running the test suite, and running the offline CLI myself — not by summarizing the docs in `docs/*.md`. Where a doc's claim could be checked against code or tests, I checked it; where it disagreed with what I found, I say so explicitly below rather than repeating it.*

---

## 1. What this project appears to do, and for whom

Forge is a pipeline that turns public genomics/clinical-genetics data (ClinVar clinical assertions, Ensembl gene/transcript structure, gnomAD population-frequency data, ClinGen ACMG variant-interpretation rules, plus several not-yet-wired sources) into one typed, provenance-tracked graph, on disk or in memory. Each fact — "this gene has this transcript," "this lab classified this variant as pathogenic," "this population never observed this allele" — becomes a strongly-typed node or edge with an explicit source citation, and every batch of incoming data is verified against nine structural-correctness rules before it is allowed to join the graph at all.

The evident target user is not a clinician directly. It reads as infrastructure for a *future* system: a query-time reasoning/retrieval layer referred to throughout the docs and code only by the acronym **MHLA** (never spelled out anywhere in this codebase) that would traverse this graph to answer questions like "has this variant satisfied ACMG criterion PM2 for this gene." The comments are explicit that Forge's job stops at "produce a graph MHLA can trust," and MHLA itself does not exist yet — no code in the repository builds or queries an `INVESTIGATION`-role node or an `ACTIVATED_BY` edge except as verification-rule scaffolding and tests. Concretely, then: this is a *genomics knowledge-graph ETL pipeline for ACMG variant classification evidence*, built by and for whoever is going to build the reasoning layer on top of it next — most plausibly the same author, continuing the project.

The domain is narrow and specific: the entire pipeline is validated end-to-end against exactly one variant (VHL gene, ClinVar VCV000002218, `NC_000003.12:g.10149822C>T` / `p.Arg167Trp`), with a second conflicting-classification variant and a 79-gene "actionable panel" used to stress-test it at larger scale.

---

## 2. End-to-end architecture

### 2.1 The pipeline, as it actually runs (traced through `orchestration/runner.py` and `ingest/compiler.py`, not just the design doc's diagram)

```
                         CLI (main.py) / harvest (orchestration/harvest.py)
                                            │
                          RunConfig{gene, mode, connectors, offline, sample_pct, ...}
                                            │
                    ┌───────────────────────┼────────────────────────┐
                    │  mode=persistent/full │       mode=ephemeral/full
                    ▼                       │                        ▼
        orchestration/load_plan.py    ScopeResolver          ClinVarAdapter.search_variation_ids()
        plan_load() -> LoadPlan            │ (variant|gene scope only)   -> list of VariationIDs, sampled
        execute(plan, ...)                 ▼                              deterministically (seed)
                    │              ConnectorPlanner.plan()                        │
                    │              (asks every registered connector's             ▼
                    │               build_query(); DAG via `requires`)   fetch_many() batches ESummary+EFetch
                    ▼                                                            │
      one raw SourceRecord per connector call (JSON / XML — connectors/*.py)     │
                    │                                                            │
                    ▼                                                            ▼
        <Source>Parser.parse*()  ──────────────────►  SourceObject dataclass(es)
        (ingest/source_objects.py base + per-connector subclasses in connectors/*.py)
                    │
                    ▼
        ingest/mapping.py :: apply_mapping_spec(obj, MappingSpec)
          - declarative MappingRule (simple field -> node/edge attribute)
          - named/versioned EscapeHatch (genuine branching logic: ClinVar's
            SCV<->RCV linkage, ClinGen's per-tier Requirement expansion, ...)
                    │
                    ▼           list[ForgeNode | ForgeEdge]  (forge_ir/graph.py)
        orchestration/connector_registry.py :: Emission
          (nodes/edges tagged with which connector FEED produced them)
                    │
                    ▼
        ingest/compiler.py :: StagingCompiler.stage() -> .commit()
          1. dedupe nodes by id (merge provenance)
          2. dedupe edges by (src, predicate, qualifier, dst) (merge provenance)
          3. forge_ir/verify.py :: verify() — ALL 9 checks, over the batch +
             already-committed/resident node ids
                → fails  = CommitResult(rejected=True) — NOTHING written, index untouched
          4. ingest/identity.py :: resolve_identity() for Gene/Variant/Disease
             ONLY (only reachable after verification passes)
          5. edge ledger built; ingest/hashing.py::compute_structural_hash()
                    │
                    ▼
        orchestration/runner.py :: GraphStore.absorb() (single-gene runs)
             or orchestration/harvest.py :: StreamingStore.absorb() (panel-scale:
             streams straight to gzip JSONL, never holds the full graph in memory)
                    │
                    ▼
        COMMITTED GRAPH  =  list[ForgeNode], list[ForgeEdge]
          (in-memory for a single gene; runs/<name>/graph.jsonl.gz for a panel harvest)
                    │
           ┌────────┴─────────────────────────────┐
           ▼                                       ▼
   analysis/edge_stats.py                  analysis/completeness.py
   analysis/downsample.py (viz sampling)   (advisory only, never blocks a commit)
           │
           ▼
   scripts/build_viz_dataset.py → demo/graph_data.json → scripts/build_demo_html.py → demo/index.html
```

Two things worth stating precisely because the design docs are easy to over-read on this point:

- **`forge_ir/requirement.py`'s `RequirementState` machinery (PM2 evaluation, etc.) is exercised by the E2E test as a plain function call (`evaluate_requirement(...)`), not wired into the committed-graph pipeline above at all.** No code path in `orchestration/` or `main.py` ever constructs an `INVESTIGATION`-role node or evaluates a `Requirement` against the committed graph automatically. It is real, tested logic, sitting one layer below where MHLA would eventually call it.
- **`orchestration/load_plan.py::execute()` only drives the *persistent* (Core Biology + Operational) connectors.** The ephemeral, per-variant evidence loop (ClinVar + its enrichers) is a separate, hand-built harness in `orchestration/runner.py` (`_ingest_one_variant`) with its own batching/quarantine logic — the docs (`docs/forge-implementation-and-findings.md` Finding 9) say this was a deliberate choice, not an oversight, and the code matches that claim.

### 2.2 Subsystems

| Directory | Responsibility |
|---|---|
| `forge_ir/` | The type system. Pure dataclasses/enums, no I/O: `ForgeNode`/`ForgeEdge` (`graph.py`), the frozen node-subtype and edge-combination registries (`core.py`), provenance variants (`provenance.py`), the 9 verification checks (`verify.py`), the ACMG `Requirement`/`RequirementState` evaluator (`requirement.py`), and pure `Protocol` interfaces (`contracts.py`). |
| `ingest/` | Source-agnostic ingestion machinery: canonical hashing (`hashing.py`), the declarative-mapping engine (`mapping.py`), the persistent SQLite identity resolver (`identity.py`), the two-phase staging compiler (`compiler.py`), the dirty/retry ledger (`dirty.py`), variant-notation normalization (`variant_notation.py`), population/ancestry normalization (`population.py`), `SourceObjectBase` (`source_objects.py`), and two design-only, never-instantiated dataclasses (`checkpoint.py`, and `SourceSnapshot` in `manifest.py` — see §4). |
| `connectors/` | One file per external source: fetch + parse + map to Forge IR. 7 real/implemented (`clinvar`, `ensembl` [two feeds], `gnomad`, `clingen`, `dbsnp`, `uniprot`) and 6 registered-but-stub (`omim`, `mondo`, `pharmgkb`, `cosmic`, `dbnsfp`, `clingen_gene_disease`). `common.py` and `http.py` are shared helpers. |
| `orchestration/` | Planning and running: the pluggable connector registry (`connector_registry.py`), the DAG-based fetch planner (`connector_planner.py`), the (variant\|gene)-only scope resolver (`scope_resolver.py`), the frozen `LoadPlan`/`plan_load()`/`execute()` (`load_plan.py`), the single-gene run engine (`runner.py`), and the panel-scale streaming harvest (`harvest.py`). `capabilities.py` and `ratelimit.py` define types that are largely unused in practice (see §4). |
| `analysis/` | Read-only, post-commit reporting: edge-type distribution and fan-out/breadth attribution (`edge_stats.py`), the advisory (never-blocking) completeness report (`completeness.py`), and Gaussian-density graph downsampling for visualization (`downsample.py`). |
| `main.py` | CLI entry point: one command, three modes (`ephemeral`/`persistent`/`full`), `--offline` fixture mode, `--list-connectors`. |
| `scripts/` | `overnight_harvest.py` (panel-scale CLI over `orchestration/harvest.py`), `build_viz_dataset.py` (downsamples a harvest dump into a browser-sized JSON), `build_demo_html.py` (injects that JSON into a static HTML page), `generate_graph_demo_data.py` (small, hand-picked 2-variant demo dataset), `verify_connectors.py` (a 7-point conformance check per connector, runnable standalone). |
| `demo/` | `template.html` + generated `index.html`/`graph_data.json` — a self-contained, replayable visualization of a run's graph-formation events. |
| `fixtures/` | Real, live-fetched API payloads (not synthetic) that back every connector test and the offline CLI mode. |
| `runs/` | Output of actual harvest/demo runs (checkpoints, dumps, logs) — a background harvest was actively writing here during this review; not part of the source tree proper. |
| `docs/` | Five design documents (`connector-research.md` → `forge-ir-v2...md` → `forge-ir-v3-schema.md` → `forge-pipeline-v1.md`, each superseding parts of the last) plus `forge-implementation-and-findings.md` (a retrospective on what was actually built) and `stub-connectors-research-and-ir-mapping.md` (a since-partially-invalidated proposal — see §4). |
| `tests/` | 263 tests (see §5) — described inline with the modules they cover, not repeated file-by-file here. |

---

## 3. Per-module responsibility

### `forge_ir/` — the type system

- **`core.py`** — Defines `NodeRole`, `EdgeType`, `Layer`, `StorageScope`, `Origin`; the `NODE_SUBTYPE_REGISTRY` (22 registered `(role, subtype)` pairs, each with a canonical-ID scheme and required attributes); `ALLOWED_EDGE_COMBINATIONS` (21 legal edge shapes, 4 of them `RUNTIME_ONLY`/reserved for MHLA); and the canonical-ID constructor functions (SPDI, HGNC, Ensembl, MedGen, etc.), including the one place the gnomAD-1-based-vs-SPDI-0-based off-by-one conversion happens. Everything downstream treats this file as the frozen ontology.
- **`graph.py`** — `ForgeNode`/`ForgeEdge` dataclasses. `__post_init__` derives `layer`/`storage_scope` from the registry (never settable directly) and *raises `ValueError` at construction time* if `origin=RUNTIME` doesn't pair with a `RUNTIME_ONLY` role/predicate, or vice versa — this is a real, enforced invariant, not just documented intent (confirmed by `tests/test_verify.py::test_runtime_only_role_cannot_be_constructed_with_source_origin`, which passes).
- **`provenance.py`** — Three frozen provenance dataclasses (`SourceProvenance`, `DerivedProvenance`, `RuntimeProvenance`), keyed off `Origin`, plus an abstract base never instantiated directly.
- **`requirement.py`** — `EvaluationMode` (MECHANICAL/COMPOSITE/NARRATIVE/HYBRID), `Requirement`, `RequirementState`, and `evaluate_requirement()`, the actual evaluator: NARRATIVE/HYBRID-without-mechanical-remainder always returns `NEEDS_INTERPRETATION`; MECHANICAL/COMPOSITE reads a named metric off an observation dict, supports exactly one `fallback_if_unavailable` clause, and distinguishes "no evidence yet" (`UNSATISFIED`) from "evidence exists and fails the check" (`CONTRADICTED`). This is real, working logic — not a stub — but it is only ever invoked as a standalone function call in tests and demo scripts; nothing in the committed pipeline calls it automatically.
- **`verify.py`** — The 9 correctness checks (type correctness, referential integrity, layer-tag consistency, multi-provenance integrity, hydration-pointer integrity, connectivity, storage-scope integrity, plus record-conservation as a standalone function and dedup as the compiler's job). `verify()` itself runs 7 of the 9 checks; record conservation (`check_record_conservation`) is deliberately not part of `verify()` because it operates on raw record counts, not `ForgeNode`/`ForgeEdge` objects — it's called from the runner's own summary logic, not from the compiler.
- **`contracts.py`** — Pure `Protocol` interfaces, no logic, matching the docs' explicit intent ("no logic here, ever").

### `ingest/` — source-agnostic machinery

- **`hashing.py`** — `canonical_hash()` (sorted-key JSON + SHA256) and `compute_structural_hash()`, which projects out volatile fields (`retrieved_at` and similar) before hashing so that re-parsing the same fixtures twice produces an identical hash. Confirmed exercised by `tests/test_reproducibility.py`, including a test that parses the real fixtures twice independently and checks the hashes match.
- **`mapping.py`** — `MappingRule`/`EscapeHatch`/`MappingSpec` dataclasses and `apply_mapping_spec()`, the declarative-plus-named-exception transformation engine. Every real connector in this codebase uses **zero** `MappingRule`s and **100% `EscapeHatch`** — confirmed by the coverage report printed on an actual offline run (see §5) and by `tests/test_coverage_report.py::test_real_offline_run_reports_honest_100_percent_escape_hatch_for_every_connector`. The declarative-rule path is real code, unit-tested (`tests/test_mapping.py`), but never exercised by production data: every real source's mapping logic needs genuine branching (hierarchy linking, tier expansion) that the design doc itself predicted would require escape hatches.
- **`identity.py`** — SQLite-backed `IdentityIndex` and `resolve_identity()`, the single centralized place SPDI/HGNC/MedGen identity gets resolved, tri-state (`MERGED`/`REPRESENTS`/`UNRESOLVED`). Confirmed persistent across process boundaries by `tests/test_identity.py::test_identity_index_persists_across_process_boundaries`, which opens two independent `IdentityIndex` instances against the same file. The module's own docstring is honest that `REPRESENTS` is "not yet exercised by real data" for Gene/Variant (both have deterministic shared keys) — but it *is* exercised, by `connectors/uniprot.py`, which the identity module's docstring predates (see §4).
- **`compiler.py`** — `StagingCompiler`: two-phase stage/commit, node/edge dedup with provenance merging, calls `verify()` before identity resolution (an intentional ordering fix — see `docs/forge-implementation-and-findings.md` Finding 5, which the code confirms), builds the auditable edge ledger, and computes the structural hash on every successful commit.
- **`dirty.py`** — `RetryTicket`/`DirtyLedger`: marks a node's attributes dirty in place when a per-variant enrichment (gnomAD/VEP/dbSNP) fails or is deferred at scale, writes an append-only JSONL ledger of exactly what would need to be retried, and derives the *expected* edge shapes a failed connector would have contributed from the ontology itself (`expected_edge_shapes()`) rather than a hand-maintained table. Distinguishes `EMPTY` (source authoritatively has nothing) from `ABANDONED` (retried out) from `PENDING`/`DEFERRED`.
- **`checkpoint.py`** — `IngestionCheckpoint` dataclass only. Grep-confirmed: **never instantiated anywhere in the codebase**, no test file references it. The module docstring itself says this is intentional ("exists now so it isn't retrofitted later... NOT exercised for real crash-recovery at VHL scale") — an honest, self-declared stub.
- **`manifest.py`** — `SourceManifest` (real, used — see `connectors/common.py::build_source_manifest_node`) and `SourceSnapshot` (defined, **never constructed anywhere** — grep-confirmed zero instantiations outside its own docstring). The per-record `snapshot_ref` field that would point at a `SourceSnapshot` is always populated with an empty string or left generic; the actual snapshot bookkeeping the design doc describes doesn't exist in running code.
- **`variant_notation.py`** — VCF/HGVS-genomic → canonical SPDI conversion, with a real GRCh38 chromosome↔RefSeq accession table. `hgvs_genomic_to_spdi` deliberately handles only the one HGVS shape seen in real payloads and raises on anything else — a scoped, honest limitation, not a silent gap.
- **`source_objects.py`** — `SourceObjectBase`, the common fields every per-connector `SourceObject` subclass carries.
- **`population.py`** — gnomAD ancestry/sex code normalization against gnomAD v4's real 10-group taxonomy; splits `afr_XX`-style compound codes into ancestry + sex, reports unrecognized codes rather than dropping them.

### `connectors/` — one file per source

- **`clinvar.py`** (575 lines, the largest file) — ESearch→ESummary→EFetch adapter; parses the real VCV/RCV/SCV three-level hierarchy (never "one record = one Claim"); builds an SCV↔RCV crosswalk from ESummary trait cross-references because EFetch alone only resolves 3 of 22 real submissions; computes `CONTRADICTS` edges by ClinVar's own pathogenicity-concordance groups (not string equality) to avoid the 47-false-positive bug the docs describe; handles multi-gene variants and unmapped free-text conditions. This is the most complex and most heavily-fixed module in the codebase, per its own inline commentary.
- **`ensembl.py`** (314 lines) — Registers **two** connector feeds off one shared adapter: `ensembl_structure` (Core Biology: Gene→Transcript→Exon→Protein, full expansion only for the canonical transcript, others stubbed at `lod=0`) and `ensembl_vep` (Evidence: per-transcript consequences + SIFT/PolyPhen). `clinical_significance` is deliberately never parsed at all (not even into a `SourceObject` field) because it mirrors ClinVar and isn't authoritative here.
- **`gnomad.py`** — GraphQL adapter; one `PopulationFrequency` OBSERVATION per dataset (genome/exome), correctly handles the real `{"data": {"variant": null}}` not-found shape as "nothing to emit," not a parse error; captures `faf95.popmax` explicitly because it — not plain `af` — is PM2's real threshold metric.
- **`clingen.py`** — Fetches ClinGen's VCEP cspec JSON (with an offline-fixture fallback baked directly into the adapter for when the live endpoint's anti-automation 403 fires); expands every strength tier of every criterion into its own `Requirement` node via a named escape hatch; only `PM2` (MECHANICAL) and `BP4` (COMPOSITE) are individually confirmed against real spec text — everything else defaults to `NARRATIVE`, an honest, explicitly-stated conservative default rather than a guess.
- **`dbsnp.py`** — NCBI Variation Services REST adapter; confirms and cross-references rsIDs, correctly skips the "no-change" reference allele, and (per its own docstring) reveals a second real cataloged variant (C>G) at the VHL test locus that ClinVar/gnomAD's fixtures never discuss.
- **`uniprot.py`** — UniProtKB REST adapter (search-by-gene or direct-by-accession); the first connector in the codebase to actually produce a `REPRESENTS` edge (Ensembl protein id vs. UniProt accession for the same biological protein), resolved by reading the real cross-reference block rather than a hardcoded table. `default_included=False` (opt-in only).
- **`common.py`** — `build_variant_entity()` (the one place any connector actually constructs the canonical `Variant` ENTITY node — see Finding 6 discussion in §4/§6) and `build_source_manifest_node()`.
- **`http.py`** — Shared retry wrapper (429/5xx + connection/timeout retried with jittered exponential backoff honoring `Retry-After`; 4xx passed straight through as permanent failures).
- **Stubs — `cosmic.py`, `dbnsfp.py`, `mondo.py`, `omim.py`, `pharmgkb.py`, `clingen_gene_disease.py`** — Each registers a real `ConnectorDescriptor` with `implemented=False`, a `capabilities()`/`manifest()` that returns real (checked) metadata, and a `fetch()` that unconditionally `raise`s `NotImplementedError` with a specific, source-appropriate reason (no public API without a paid/approved key, flat-file-only distribution, HTML-only endpoint checked live and confirmed non-JSON, etc.). These are honestly self-declared, verified-not-to-lie stubs — see §4 and §5 for how they're mechanically checked rather than trusted.

### `orchestration/` — planning and running

- **`connector_registry.py`** — `ConnectorDescriptor`/`register_connector()`/`list_connectors()` (the pluggable-connector contract) and `Emission` (nodes/edges tagged with the producing feed, first-writer-wins on feed credit — a real bug fix per its own comment). `tag_with_source_manifest()` runs after every `build_emission()` and adds the `SourceManifest` node + `SOURCED_FROM` edges automatically.
- **`connector_planner.py`** — `ConnectorPlanner`: asks every registered connector's `build_query()` for a scope/anchor, builds the `FetchTask` list and the HARD-dependency DAG from each descriptor's declared `requires`. Reads a `rate_policies` dict in its constructor that is **always passed empty** at every real call site (see §4) — the field exists but rate limiting is actually enforced elsewhere (`connectors/http.py`'s retry wrapper and hardcoded `time.sleep()` calls in `clinvar.py`/`harvest.py`), not through this mechanism.
- **`scope_resolver.py`** — `ScopeResolver`: only `variant`/`gene` scopes implemented; `pathway`/`domain` raise `NotImplementedError` with "deferred — no backing source," `gene_family`/`region` raise `NotImplementedError` with "experimental/unverified" — both are honest, self-documenting `NotImplementedError`s rather than silent no-ops.
- **`load_plan.py`** — `LoadPlan` (frozen dataclass), `plan_load()` (no I/O, pure planning), `execute()` (the only function that touches the network for the *persistent* connectors; walks dependency tiers, fetches, maps, commits). Explicitly does not drive the ephemeral evidence connectors.
- **`runner.py`** — The single-gene run engine. `RunConfig`/`RunResult`, the three-mode split (persistent/ephemeral/full), `GraphStore` (in-memory cross-batch dedup with a hard provenance-list cap of 8 per node to avoid unbounded growth from re-sighted Submitters), `EventLog` (budget-capped replay events for the visualization), and the per-variant ingestion loop (`_ingest_one_variant`) that runs ClinVar then enriches with any `requires=("clinvar",)` connector before committing one micro-batch per variant.
- **`harvest.py`** — Panel-scale variant of the same pipeline: `StreamingStore` (writes committed nodes/edges straight to gzipped JSONL, never holding the full graph in memory), `Checkpoint` (atomic write-temp-then-replace, per-gene per-batch resumability), `allocate_budget()` (max-min-fair variant budget across genes), concurrent ClinVar batch fetching via a thread pool. Explicitly reuses `runner.load_persistent`/`runner._ingest_one_variant`/`StagingCompiler` rather than reimplementing ingestion.
- **`capabilities.py`** — `ConnectorCapabilities` frozen dataclass with a `from_dict()` classmethod. Grep-confirmed: **`from_dict()` is never called anywhere in the codebase.** Every real connector's `.capabilities()` returns a plain `dict`, and every caller (`analysis/completeness.py`, `ingest/dirty.py`) reads that dict directly rather than constructing this dataclass. Design-only, not wired in.
- **`ratelimit.py`** — `RatePolicy`/`RetryPolicy` dataclasses plus four real, documented per-source constants (`ENSEMBL_RATE_POLICY`, etc.). Grep-confirmed: **none of the four named constants is referenced anywhere outside this file.** The type exists, the real numbers are recorded, but nothing in `orchestration/` or `connectors/` reads them at runtime.

### `analysis/` — read-only reporting

- **`edge_stats.py`** — `EdgeStatsAccumulator`/`compute_edge_stats()`: predicate counts, qualifier/shape/layer-pair breakdowns, and a fan-out-vs-breadth "driver" explanation per predicate (`edges_per_source_node`). Has both a batch (`compute_edge_stats`) and a streaming (`EdgeStatsAccumulator`) form so the harvest doesn't need the full graph resident.
- **`completeness.py`** — `compute_completeness()`: cross-references what each connector *declares* it can produce against what the committed graph *actually contains*, and what fraction of the ontology's legal edge shapes were exercised. Explicitly advisory — never blocks a commit, unlike `verify.py`.
- **`downsample.py`** — Gaussian-kernel-over-hop-distance density downsampling for the browser visualization, with deterministic (hash-based, not RNG-based) keep/drop decisions, protected "always-keep" categories (small persistent-layer subtypes, dirty nodes up to a cap, rare subtypes, spanning hubs), and a two-pass neighborhood-growing step so the sampled graph stays connected rather than becoming scattered dots. Heavily commented with specific bug-fix history (e.g., a fixed-threshold-vs-fraction-of-graph bug, a hub-traversal density-inversion bug) — reads as real, iterated-on code, not a first draft.

---

## 4. Completeness inventory

### Fully implemented and tested (cite + confirmed passing)

- **Node/edge type system & the 9 verification checks** — `forge_ir/core.py`, `graph.py`, `verify.py`. Tested by all 17 tests in `tests/test_verify.py` (one deliberately-broken synthetic case per check), all passing. Construction-time invariant enforcement confirmed by `test_runtime_only_role_cannot_be_constructed_with_source_origin`.
- **Two-phase staging compiler (stage → dedupe → verify → identity-resolve → commit)** — `ingest/compiler.py`. Tested by 8 tests in `tests/test_compiler.py`, including the verify-before-identity-resolution ordering (`test_identity_resolution_runs_for_gene_and_variant_but_not_claim_or_observation`) and cross-run persistence, all passing.
- **Persistent identity resolution (SQLite-backed, tri-state)** — `ingest/identity.py`. 8 tests in `tests/test_identity.py`, including real cross-process persistence, all passing.
- **Structural-hash reproducibility** — `ingest/hashing.py`. 6 tests in `tests/test_reproducibility.py`, including parsing the real fixtures twice independently and comparing hashes, all passing.
- **ClinVar VCV/RCV/SCV hierarchy, SCV↔RCV crosswalk, CONTRADICTS via concordance grouping, multi-gene handling, malformed-condition handling** — `connectors/clinvar.py`. 10 + 7 tests across `tests/test_connectors/test_clinvar.py` and `test_clinvar_batch.py`, plus exercised directly by the E2E test, all passing.
- **Ensembl gene/transcript/exon/protein structure with canonical-transcript-only full expansion, and transcript-scoped VEP consequences** — `connectors/ensembl.py`. 7 tests in `tests/test_connectors/test_ensembl.py`, including a direct assertion that only the canonical transcript gets full exon structure, all passing.
- **gnomAD population-frequency parsing, including the real null-`faf95_popmax`/singleton case and the `{"variant": null}` not-found case** — `connectors/gnomad.py`. 5 tests, all passing.
- **ClinGen strength-tier `Requirement` expansion and `evaluate_requirement()`'s MECHANICAL/fallback logic for PM2** — `connectors/clingen.py`, `forge_ir/requirement.py`. 6 connector tests plus the E2E gate test, all passing.
- **dbSNP and UniProt connectors, including UniProt's `REPRESENTS` edge construction** — `connectors/dbsnp.py`, `uniprot.py`. 5 + 6 tests, all passing.
- **The full VHL/PM2 end-to-end gate** — `tests/e2e/test_vhl_pm2.py`, 8 tests: real 4-source parse → map → stage → identity-resolve → verify → atomic commit → PM2 `Requirement` found in the *committed* graph → evaluated against the real gnomAD observation → asserts `SATISFIED` via the documented singleton fallback (not hardcoded), and a companion test that PVS1's 5 tiers all correctly land on `NEEDS_INTERPRETATION`. All 8 pass. This is the one test that exercises the claimed "whole invariant chain."
- **Connector registry pluggability (register once, no core-file edits needed)** — `orchestration/connector_registry.py`. 8 tests in `tests/test_connector_registry.py`, including `test_a_custom_connector_needs_no_core_file_edits_to_register` and `test_the_real_registry_reaches_the_ten_connector_target`, all passing.
- **DAG-based connector planning (dependency tiers, HARD deps)** — `orchestration/connector_planner.py`, `load_plan.py`. 9 tests in `tests/test_orchestration.py`, all passing.
- **Three-mode run engine (persistent/ephemeral/full), cross-batch identity-merge events, feed attribution, resident-layer verification vs. declaration** — `orchestration/runner.py`. 21 tests in `tests/test_runner.py`, all passing, including the ephemeral-VEP-needs-persistent-Core-Biology contract (both the rejection and the success-once-resident case).
- **Dirty/retry ledger (mark, clear, JSONL persistence, truncated-line recovery, expected-edge-shape derivation)** — `ingest/dirty.py`. 13 tests in `tests/test_dirty.py`, all passing.
- **Gaussian-density visualization downsampling** — `analysis/downsample.py`. 13 tests in `tests/test_downsample.py`, all passing, including determinism and "nothing is invented, only selected."
- **Gzip-JSONL dump recovery from a truncated file** — `orchestration/harvest.py` / `scripts/build_viz_dataset.py::read_dump`. 3 tests in `tests/test_dump_recovery.py`, all passing.
- **Connector capability/ontology grammar checking, and the seven-point connector conformance check (`scripts/verify_connectors.py`)** — 39 + 15 tests, all passing. Notably, this check mechanically *caught real errors* in `docs/stub-connectors-research-and-ir-mapping.md`'s proposed mappings (15 of 19 proposed edge combinations were invalid against the frozen ontology) — a concrete, cited example of a doc's claim not matching what the code actually enforces.
- **SourceManifest/SOURCED_FROM materialization, mapping-coverage reporting, edge-type statistics, advisory completeness reporting, variant-notation conversion, population normalization** — each has its own dedicated 6-10 test file (`test_source_manifest.py`, `test_coverage_report.py`, `test_edge_stats.py`, `test_completeness.py`, `test_variant_notation.py`, `test_population.py`), all passing.
- **The offline CLI path end-to-end** — I ran `python main.py --mode full --offline --connectors clinvar,gnomad,vep --quiet` myself (see §5): it completed successfully, produced a 289-node/393-edge committed graph, a completeness report, an edge-stats table, and a valid event-log JSON file, with exit code 0.

### Partially implemented

- **`orchestration/load_plan.py::execute()` and the DAG planner** — Real and tested (`tests/test_orchestration.py`), but only drives the *persistent* connectors (Core Biology + Operational). The ephemeral evidence connectors go through a separate, hand-written harness (`runner._ingest_one_variant`) that duplicates some of the same batching/quarantine concerns rather than being unified under `execute()`. The docs are explicit and honest about this being deliberate (not enough real connector variety existed to generalize a single dispatcher from), but it does mean "one generic execution engine" is not actually true of the whole pipeline.
- **`orchestration.scope_resolver.ScopeResolver`** — `variant`/`gene` scopes work and are tested; `gene_family`/`region` are explicitly `NotImplementedError`("experimental/unverified"); `pathway`/`domain` are explicitly `NotImplementedError`("deferred, no backing source"). Honest partial coverage, not a silent gap — but real functionality nonetheless only covers 2 of the 8 scopes the design docs describe (`atomic`/`variant`/`gene`/`gene_set`/`gene_family`/`pathway`/`region`/`domain-system`).
- **`RequirementState` / MHLA-facing machinery** — The state machine and evaluator (`forge_ir/requirement.py`) are real and tested, but only as a library function called directly by tests/demo scripts. Nothing in the production pipeline (`orchestration/runner.py`, `harvest.py`, `main.py`) ever constructs a `RequirementState` node or runs the evaluator automatically over a committed graph. The `NODE_SUBTYPE_REGISTRY` entries for `INVESTIGATION:Query` and `INVESTIGATION:RequirementState` are populated in the schema but literally never instantiated by any run — confirmed by the completeness report I generated myself (see §5), which lists both as "Unpopulated subtypes" on a real run.
- **Connector rate limiting via `orchestration/ratelimit.py`** — The `RatePolicy` dataclass and four real per-source constants exist and are documented, but (per §3/§4 grep evidence) are never actually read at runtime; `ConnectorPlanner.rate_policies` is always passed `{}`. Real rate limiting happens through a different, unrelated mechanism (`connectors/http.py`'s retry backoff, and hardcoded `time.sleep()` calls in `clinvar.py`/`harvest.py`). The type is a faithful design artifact that the runtime doesn't actually consult.

### Designed but not implemented / stubbed (cite the evidence)

- **6 of 13 registered connectors are honest stubs**: `omim.py`, `mondo.py`, `pharmgkb.py`, `cosmic.py`, `dbnsfp.py`, `clingen_gene_disease.py` — each registers `implemented=False` and a `fetch()` whose entire body is `raise NotImplementedError(...)` with a specific, checked reason (e.g. `connectors/omim.py:22-26`, `connectors/cosmic.py:21-25`). Confirmed to fail loudly, not silently, by `tests/test_connector_conformance.py::test_every_stub_refuses_loudly` and `tests/test_connector_registry.py::test_every_stub_connector_fails_loudly_not_silently`, both passing.
- **`ingest/checkpoint.py::IngestionCheckpoint`** — Defined, documented as design-only in its own docstring, **never instantiated anywhere in the codebase** (grep-confirmed), no test file exists for it. Real crash-recovery for ingestion is not implemented; only the harvest's own separate `Checkpoint` class in `orchestration/harvest.py` (a different, actually-used mechanism) provides resumability.
- **`ingest/manifest.py::SourceSnapshot`** — Defined with real fields (`request_hash`, `response_hash`, etc.), **never constructed anywhere in the codebase** (grep-confirmed). The per-record `snapshot_ref` string field this class was meant to back is carried around but never populated from a real `SourceSnapshot` instance.
- **`orchestration/capabilities.py::ConnectorCapabilities.from_dict()`** — Defined, **never called anywhere** (grep-confirmed). Every real caller reads the plain `dict` a connector's `.capabilities()` returns directly.
- **`orchestration/ratelimit.py`'s four real rate-policy constants** — Defined with real, cited per-source numbers, **never referenced outside their own file** (grep-confirmed).
- **`FetchTask.estimated_cost`/`estimated_records`** — Present as fields on `orchestration/connector_planner.py::FetchTask` per the design doc's own admission that this is "the seed of MHLA's eventual cost-aware retrieval, not a finished cost engine" — confirmed: `plan()` always sets `estimated_records=None`, no cost model exists anywhere in the code.
- **MHLA itself** — Referenced constantly across every design doc and in code comments as the reason certain fields/hooks exist (`ACTIVATED_BY`, `INVESTIGATION`, `RequirementState`, cost estimation, `REPRESENTS` confidence thresholds for future non-deterministic-ID sources) but **zero lines of MHLA code exist in this repository.** This is the single largest, most explicitly-acknowledged gap in the whole project, and the codebase is honest about it (`docs/forge-implementation-and-findings.md` §7: "MHLA — explicitly out of scope").
- **`docs/stub-connectors-research-and-ir-mapping.md`'s proposed IR mappings** — This document proposes specific node/edge mappings for the 6 stub connectors and asserts (in its own closing table) that all 19 proposed edge combinations are "Allowed in Schema? YES." `tests/test_connector_capability_grammar.py`'s docstring states this was checked mechanically and found **false for 15 of 19** (4 invented node subtypes, 6 invented edge qualifiers, 2 wrong-role SOURCED_FROM assumptions, 3 over-broad predicate-domain assumptions). This is a concrete, cited instance of a docs file's claim not matching what the code actually allows — the actually-registered stub connectors (`pharmgkb.py`, `clingen_gene_disease.py`, etc.) all use safer, schema-legal mappings (e.g. `Association`) rather than the invalidated proposals, so the *code* is internally consistent even though the *doc describing the plan for it* is stale/wrong.
- **`docs/forge-implementation-and-findings.md`'s own test count** — States "180 tests total" with an itemized per-file breakdown (§1 of that doc). The test suite I ran has **263 tests**; the doc's own itemized list is missing five files that exist in the repo (`test_downsample.py`, `test_dirty.py`, `test_dump_recovery.py`, `test_connector_capability_grammar.py`, `test_connector_conformance.py` — 83 tests together). This doc is stale relative to the current codebase, not fabricated — the 180 tests it does list all genuinely exist and pass — but it was not updated after later work landed.

---

## 5. Test suite — what I actually observed

Ran directly in the repo root:

```
python -m pytest -q
........................................................................ [ 27%]
........................................................................ [ 54%]
........................................................................ [ 82%]
...............................................                          [100%]
263 passed in 1.89s
```

**263 passed, 0 failed, 0 errored, 0 skipped.** Breakdown by file (via `pytest --collect-only`):

| File | Tests | | File | Tests |
|---|---|---|---|---|
| `test_connector_capability_grammar.py` | 39 | | `test_source_manifest.py` | 7 |
| `test_runner.py` | 21 | | `test_population.py` | 7 |
| `test_verify.py` | 17 | | `test_mapping.py` | 7 |
| `test_connector_conformance.py` | 15 | | `test_connectors/test_ensembl.py` | 7 |
| `test_downsample.py` | 13 | | `test_connectors/test_clinvar_batch.py` | 7 |
| `test_dirty.py` | 13 | | `test_reproducibility.py` | 6 |
| `test_variant_notation.py` | 10 | | `test_edge_stats.py` | 6 |
| `test_connectors/test_clinvar.py` | 10 | | `test_coverage_report.py` | 6 |
| `test_orchestration.py` | 9 | | `test_connectors/test_uniprot.py` | 6 |
| `test_identity.py` | 8 | | `test_connectors/test_clingen.py` | 6 |
| `test_connector_registry.py` | 8 | | `test_completeness.py` | 6 |
| `test_compiler.py` | 8 | | `test_connectors/test_gnomad.py` | 5 |
| `e2e/test_vhl_pm2.py` | 8 | | `test_connectors/test_dbsnp.py` | 5 |
| | | | `test_dump_recovery.py` | 3 |

No mocks were found anywhere in the connector or E2E test layer — every connector test loads a real file from `fixtures/`, and the fixtures themselves are stated (and, spot-checked, appear) to be live-fetched real API responses, not hand-written synthetic data.

**Offline CLI run** (the documented `--offline` no-network path): I ran

```
python main.py --mode full --offline --connectors clinvar,gnomad,vep --quiet
```

myself. It completed successfully (exit code 0, `verified: True`, `batches_rejected: 0`), producing a **289-node / 393-edge** committed graph from the bundled ClinVar+gnomAD+VEP+Ensembl+ClinGen fixtures, printed a correct edge-type-statistics table and a graph-completeness report, and wrote a valid event-log JSON file when given a writable output path. This confirms the `--offline` flag genuinely avoids all network calls (no errors, no hangs, fast — under a few seconds) and that the CLI's advertised "everything in one process, offline from the saved fixtures" mode works as documented.

I did **not** run the live `--mode ephemeral --sample-pct 50` VHL run or the `overnight_harvest.py` panel harvest myself (both require live network access, which I avoided per the task's ground rules), so the specific node/edge counts `docs/forge-implementation-and-findings.md` cites for that run (9,539 nodes / 21,206 edges) are unverified by me directly — though `runs/panel_50k/summary.json`, apparently produced by a real, currently-in-progress background harvest process, shows plausible, consistent numbers of the same shape (51,560 nodes / 144,383 edges across a 5-gene partial panel run, 25 of 2,157 batches rejected, 91 records quarantined), which is at least circumstantial evidence the pipeline behaves as described at larger, real scale too.

---

## 6. Independent verdict on overall completeness

**What's built is real and well-tested, not decorative.** Every claim I could mechanically check — the 9 verification checks, structural-hash reproducibility, the SCV↔RCV crosswalk numbers, the PM2 fallback logic, the stub connectors' honesty about failing loudly — held up under both reading the code and running the tests myself. The 263-test suite is not padding: it includes tests specifically designed to catch the project's own past mistakes (the 47-false-CONTRADICTS bug, the identity-resolution-before-verify ordering bug, the feed-attribution-clobbering bug, the fixed-vs-fractional hub-threshold bug), which is a stronger signal of genuine engineering rigor than a green checkmark alone would be.

**Rough fraction of documented capability that is actually built and verifiable: I'd put it around two-thirds to three-quarters, unevenly distributed.** The core pipeline — parse → map → stage → verify → identity-resolve → commit, for 7 real sources, at both single-gene and panel scale — is fully built, fully tested, and (per the live background harvest I observed writing to `runs/panel_50k/`) is actively being run at real, non-trivial scale right now. The scaffolding around it (rate policy types, checkpoint types, snapshot types, capability dataclasses, most of the scope-resolver's scope vocabulary) is disproportionately design-only relative to how prominently the docs discuss it — several of these are single-file, single-purpose dataclasses that exist, are internally consistent with their design doc, and are simply never called from anywhere else in the program.

**The single biggest gap between claimed capability and demonstrated capability is MHLA.** The entire architecture — the `INVESTIGATION` role, `ACTIVATED_BY` edges, `RequirementState`'s runtime-only storage scope, the frozen `LoadPlan`'s stated purpose of eventually being "diffed against MHLA's actual activated subgraph," the cost-estimation fields on `FetchTask`, the `REPRESENTS` edge's confidence machinery — exists to feed a consumer that is referenced in nearly every design doc and several code comments but has zero implementation anywhere in this repository. Every doc is honest that this is deliberate and out of scope for what's been built so far ("MHLA — explicitly out of scope until this document's contents existed and passed" — `docs/forge-implementation-and-findings.md` §7), so this isn't a case of overclaiming; it's a case of a large, real amount of the architecture's *justification* pointing at something that doesn't exist yet. A reader who only skimmed the design docs' diagrams (which draw MHLA as the graph's actual purpose) would reasonably overestimate how much of "the system" — as opposed to "the pipeline that feeds the system" — is done.

The secondary gap worth naming: **`docs/forge-implementation-and-findings.md` is stale.** It's clearly the most carefully-maintained and most self-critical of the docs (it's the one that documents real bugs it found and fixed, with test citations), but its own test inventory (180 tests) is now short by 83 tests / 5 files relative to the actual repository (263 tests). That's a small, mechanical gap, not a trust-eroding one — every number it does state checks out — but it means even the "this doc reflects the actual codebase" doc needs a fresh read against the code, not a fresh read against itself, which is exactly the discipline this whole review was asked to apply.

---

## 7. Glossary

| Term | Meaning, as best I could reconstruct it |
|---|---|
| **Forge / Forge IR** | The project's name for its own typed graph representation — `ForgeNode`/`ForgeEdge`, the "committed graph." |
| **MHLA** | Never spelled out anywhere in the codebase or docs. Referenced consistently as the not-yet-built query-time reasoning/retrieval/traversal system that will eventually consume this graph — decides what to "activate" for an investigation, evaluates `Requirement`s, drives hydration escalation. Treat it as "the intended downstream consumer, out of scope here." |
| **SPDI** | Sequence-Position-Deletion-Insertion — NCBI's 0-based canonical variant notation (`SPDI:NC_000003.12:10149821:C:T`); the canonical ID scheme for `Variant` nodes. Distinct from VCF/HGVS's 1-based convention; the conversion between them is centralized in `forge_ir/core.py`. |
| **VCV / RCV / SCV** | ClinVar's three-level record hierarchy: SCV = one submitter's individual clinical assertion; RCV = a per-condition rollup aggregating SCVs; VCV = a per-variant rollup aggregating RCVs. Modeled as three distinct `CLAIM` subtypes, linked by `RELATES_TO[contributes_to]`/`[aggregates_into]`, never `PART_OF`. |
| **VCEP / cspec** | ClinGen's Variant Curation Expert Panel and its published rule specification ("cspec") — a gene-specific set of ACMG criteria with numeric/narrative thresholds, fetched from `cspec.genome.network`. |
| **ACMG criterion (e.g. PM2, PVS1, BP4, BS4)** | A named rule from the American College of Medical Genetics variant-classification framework (e.g. "PM2" = absent/rare in population databases). Each becomes a `RULE:ACMGCriterion` node with one `RULE:Requirement` child per applicable-or-not strength tier. |
| **faf95 / faf95_popmax ("GroupMax Filtering AF")** | A specific gnomAD population-frequency statistic (95% confidence filtering allele frequency, max across populations) — PM2's real threshold metric, deliberately not the plain `af` field; can be `null` for singleton variants, which triggers a documented fallback rule. |
| **LOD (Level of Detail)** | A 0-4 integer on every node describing how fully hydrated/expanded it is (e.g. a non-canonical Ensembl transcript is stubbed at `lod=0`; the canonical one is fully expanded). Distinct from `Layer`. |
| **Layer (CORE_BIOLOGY / EVIDENCE / OPERATIONAL / ADMINISTRATIVE / NOT_APPLICABLE)** | An orthogonal tag derived deterministically from `(role, subtype)`, used to filter "biology backbone" vs. "evidence" vs. "bookkeeping" — a routing/subsystem classification, explicitly documented as *not* a trust/quality signal. |
| **Origin (SOURCE / DERIVED / RUNTIME)** | Whether a node/edge mirrors an external record, was computed by Forge itself (e.g. `CONTRADICTS` edges), or was created during a runtime investigation (MHLA-only, never by a connector). |
| **StorageScope (PERSISTENT / RUNTIME_ONLY)** | Whether a role/predicate is ever allowed in the committed, persistent graph, or exists only in MHLA's ephemeral working state (`INVESTIGATION`, `ACTIVATED_BY`). |
| **QUARANTINE vs. REJECT** | QUARANTINE = one bad record is dropped, its batch continues (record-level). REJECT = a whole micro-batch fails and nothing in it commits (batch/pipeline-integrity-level). Kept as a deliberately crisp distinction throughout `ingest/` and `orchestration/`. |
| **Escape hatch** | A named, versioned, described function used in a `MappingSpec` when the source→IR transformation needs genuine branching logic that a declarative `MappingRule` can't express (e.g. ClinVar's hierarchy linking, ClinGen's tier expansion). Every real connector in this codebase is currently 100% escape-hatch, 0% declarative rule. |
| **REPRESENTS edge** | A weak, non-merging cross-source identity link ("these two nodes probably describe the same real-world thing, not confident enough to merge") — distinct from a full identity merge (one node, multiple provenance entries). First real, non-test instance produced by `connectors/uniprot.py` (Ensembl protein id vs. UniProt accession). |
| **Feed vs. connector vs. group** | A *connector* is a registered Python module (e.g. `ensembl_structure`); a *feed* is the finer-grained label attached to nodes/edges at production time (Ensembl serves two feeds — "Ensembl gene structure" and "Ensembl VEP" — off one connector, since they can't be told apart by provenance alone); a *group* is one of `core_biology`/`operational`/`evidence`, the coarse three-way split the visualization toggles by. |
| **Dirty node** | A node whose `attributes["dirty"]` is `True` because at least one connector that should have enriched it (gnomAD, VEP, dbSNP) failed or was deliberately deferred under a harvest's enrichment budget; tracked with a retryable ticket carrying the exact query to re-issue. |
| **Structural hash** | A SHA256 over a canonically-serialized, volatile-field-stripped projection of the whole committed graph — the mechanism backing the "same source data → same hash regardless of fetch order or wall-clock time" reproducibility claim. |
| **Panel / harvest** | A "panel" is a curated list of genes (`data/gene_panel.json`); a "harvest" is a long-running, resumable, streaming run of the pipeline across an entire panel (`orchestration/harvest.py`, `scripts/overnight_harvest.py`), as distinct from a single-gene `main.py` run. |
