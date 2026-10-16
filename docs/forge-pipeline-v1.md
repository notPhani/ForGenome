# Forge Pipeline v1 — Final Ingestion Architecture

**Status: implementation-ready. This is what gets built.** Node/edge schema, state machines, and verification predicates live in [`forge-ir-v3-schema.md`](forge-ir-v3-schema.md) and aren't repeated here — this doc is everything *around* that schema: how a source payload becomes verified, committed graph, reproducibly and resumably. Mapping *decisions* (which field → which node/edge) live in [`connector-research.md`](connector-research.md) §5.

**One decision reversed from a prior proposal, deliberately:** `MappingSpec` stays Python dataclasses, not YAML — see rationale in §3. Everything else here reflects agreement across four independent review rounds. **FROZEN — this is the version to build against.** No further architecture passes planned; the next source of design feedback is the actual code and the VHL/PM2 E2E test, not more speculative review.

---

## 1. Full pipeline

```
                       LOAD PLAN (forge.plan_load — inspectable, no I/O yet)
                           │
                           ▼
                    SCOPE RESOLVER
                           │
                           ▼
                  CONNECTOR PLANNER  (DAG, not fixed sequence — §9)
                           │
               ┌───────────┼───────────┐
               ▼           ▼           ▼
           Ensembl      ClinVar      ClinGen        (gnomAD waits on
           adapter       adapter      adapter         resolved canonical
               │           │           │               variant IDs — §9)
               ▼           ▼           ▼
          SourceRecord (raw, per source — never a ForgeNode)
               │
          SourceParser → SourceObject (typed, source-shaped, stable)
               │
          MappingSpec (dataclass) → ForgeNode | ForgeEdge  (STAGING — temp IDs)
               │
               ▼
          IDENTITY RESOLVER  ◄──────────────────┐
       ┌───────┼────────┬─────────────┐          │ consults before resolving,
     MERGED  REPRESENTS  UNRESOLVED               │ updates after resolving
       │         │         │                       │
       ▼         ▼         ▼                       │
   canonical  weak-link  quarantine                 │
     ID        edge      (not indexed)              │
       │         │                                   │
       └────┬────┘                                    │
            ▼                                          │
   PERSISTENT IDENTITY INDEX  ─────────────────────────┘   (cross-batch, cross-run — §4)
       │
       ▼
   rewrite all staged edges: temp IDs → canonical IDs
       │
       ▼
   VERIFY  (forge-ir-v3-schema.md §7)  +  COVERAGE REPORT (§11)
       │
       ▼
   ATOMIC COMMIT (micro-batch, e.g. 10k records — §8)
       │
       ▼
   FORGE GRAPH
       │
  ┌────┴────┐
  ▼         ▼
persistent  runtime
knowledge   RequirementState → MHLA
```

---

## 2. Connector contract — fetch is not compilation

```python
class SourceAdapter(Protocol):
    def fetch(self, query: dict) -> Iterator["SourceRecord"]: ...   # raw, source-shaped, never a ForgeNode
    def capabilities(self) -> "ConnectorCapabilities": ...
    def manifest(self) -> "SourceManifest": ...

class SourceParser(Protocol):
    def parse(self, record: "SourceRecord") -> Iterator["SourceObject"]: ...

class MappingCompiler(Protocol):
    def compile(self, obj: "SourceObject", spec: "MappingSpec") -> Iterator[ForgeNode | ForgeEdge]: ...
```

The adapter never touches Forge types. This is what stops a ClinVar module from becoming a single 2000-line "parse XML and build graph" function — parsing (stable, changes rarely) and mapping (evolves as the ontology does) are different concerns with different change rates, and ClinVar's real three-level hierarchy (confirmed: 1 VCV, 7 RCVs, 22 SCVs for our test variant, each level with distinct fields) is exactly complex enough to need the separation now, not later.

`SourceObject` examples, shaped directly from the real fixtures. Every one carries a common, self-describing base — right now that metadata is scattered across `SourceRecord`, `SourceManifest`, and eventual node `Provenance`; centralizing it on `SourceObject` means a single object has enough information to reproduce or audit its own transformation without reconstructing context from three places:

```python
@dataclass
class SourceObjectBase:
    source: str
    record_type: str
    source_record_id: str
    source_version: str | None
    snapshot_ref: str              # which SourceSnapshot (§5) this was parsed from — traces back to
                                    # the exact raw bytes without duplicating response_hash per-record,
                                    # which would be fragile: one raw response can hold many records
                                    # (one ClinVar XML response → 22 SCVs), so there's no clean
                                    # "raw hash of just this one record" without byte-offset slicing
    payload_hash: str              # hash of THIS record's parsed content — backs §6's reproducibility
                                    # and §9's idempotent-retry check

@dataclass
class ClinVarSCV(SourceObjectBase):
    accession: str                 # "SCV001473258"
    version: int
    variation_id: int
    classification: str            # "Pathogenic"
    review_status: str
    comment: str
    contributes_to_aggregate: bool # from ContributesToAggregateClassification
    submitter: SubmitterRef
    observed_in: list[ObservedInEntry]
    trait_refs: list[MedGenRef]

@dataclass
class ClinVarRCV(SourceObjectBase):
    accession: str                 # "RCV000002302"
    condition: MedGenRef
    germline_classification: ClassificationSummary | None
    somatic_clinical_impact: ClassificationSummary | None   # present on RCV000002303, absent on most
    member_scv_accessions: list[str]

@dataclass
class EnsemblVariation(SourceObjectBase):
    rsid: str
    synonyms: list[str]            # VCV/RCV/OMIM xrefs — feeds identity resolution, not new nodes
    mappings: list[GenomicMapping]
    most_severe_consequence: str

@dataclass
class ClinGenCriterion(SourceObjectBase):
    label: str                     # "PM2"
    guideline_id: str              # "GN078"
    version: str                   # "1.1"
    evidence_category: str
    original_summary: str
    instructions_to_use: str
    strength_tiers: list[StrengthTier]   # every tier, applicable or not — see §12
```

`SourceObjectBase.source`/`source_record_id`/`source_version` get **copied forward**, not recomputed, into the eventual `SourceProvenance` when `MappingCompiler` emits `ForgeNode`s — one fact, carried through two pipeline layers, not two independent sources of truth that could drift apart.

---

## 3. `MappingSpec` stays Python dataclasses — not YAML

A prior proposal in this design process suggested declarative YAML per connector. Reversing that back to the earlier explicit decision, for a concrete technical reason confirmed by the real payloads: mapping ClinVar's hierarchy and ClinGen's strength tiers both require genuine control flow (loop over `strengthDescriptor[]`, branch on presence of `SomaticClinicalImpact`), which YAML expresses badly without turning into an embedded scripting language. Dataclasses keep this typed and still verifier-inspectable (a `MappingSpec` instance can be introspected exactly like a parsed YAML doc, without giving up flexibility):

```python
@dataclass
class MappingRule:
    target_role: NodeRole | None          # None for edge-only rules
    target_subtype: str | None
    predicate: EdgeType | None
    qualifier: str | None
    id_template: str                       # e.g. "ClinVar:{accession}"
    source_path: str                       # dot-path into a (possibly nested) SourceObject,
                                            # e.g. "SCV.ObservedInList.ObservedIn", "RCV.ClassifiedCondition"
    field_map: dict[str, str]              # SourceObject attr -> ForgeNode attribute key
    action: Literal["EMIT", "DROP"] = "EMIT"
    drop_reason: str | None = None         # required when action == "DROP" (§14)

@dataclass
class EscapeHatch:
    name: str                              # "clinvar_scv_rcv_linking" — not anonymous
    version: str                           # "v1" — a hatch can be bug-fixed independently of the
                                            # overall MappingSpecVersion; run reproducibility (§6)
                                            # needs "clinvar_scv_rcv_linking@v1", not just the name
    description: str                       # what it does, in one sentence, declaratively
    fn: Callable[["SourceObject"], list[ForgeNode | ForgeEdge]]

@dataclass
class MappingSpec:
    source: str
    record_type: str                       # "ClinicalAssertion" | "RCV" | "Gene" | ...
    rules: list[MappingRule]
    escape_hatches: list[EscapeHatch] = field(default_factory=list)
```

An earlier proposal here was a plain, anonymous `Callable` escape hatch — objected to, correctly: it can silently become "whatever arbitrary graph-generation code the connector wants," which defeats the point of an inspectable spec. I looked at replacing it with a fixed set of typed operators (`ITERATE`/`CONDITIONAL`/`EXPAND`) instead, and don't think that survives the real complexity: an SCV's own `TraitSet` MedGen ID sometimes differs from its parent RCV's canonical condition (confirmed, connector-research.md §5), so linking them correctly is a matching/lookup step, not a fixed primitive — a closed operator set either can't express that or needs a "CUSTOM" escape valve anyway, recreating the same problem one level down. **Resolution: `MappingRule` + `source_path` handles simple, even nested, field mapping declaratively (most of any given spec); genuinely branching/matching logic (ClinVar's SCV→RCV linking, ClinGen's per-tier `Requirement` expansion) stays as Python, but every hatch must be named and described**, and §11's coverage report now tracks what fraction of emitted nodes came from declarative rules vs. named hatches, and which hatches ran how often — auditability through visibility into how much non-declarative logic exists and what it claims to do, not through pretending all of it can be data.

---

## 4. Identity resolution — tri-state, graph-neutral

Strengthened from v3's binary `merge: bool` to a proper three-way result, since "definitely the same," "probably the same, don't merge," and "no idea" are genuinely different outcomes that downstream logic treats differently:

```python
@dataclass
class IdentityEvidence:
    match_method: str              # "exact_spdi" | "hgnc_xref" | "medgen_cui" | ...
    confidence: float
    resolver_version: str
    matched_identifiers: list[str]

@dataclass
class IdentityResolution:
    canonical_id: str | None
    status: Literal["MERGED", "REPRESENTS", "UNRESOLVED"]
    represents_edge: ForgeEdge | None   # set only when status == REPRESENTS
    evidence: IdentityEvidence
```

`UNRESOLVED` → quarantine, never forced. All canonicalization is centralized here — **no connector normalizes its own SPDI, HGNC, or MedGen identifiers.** This is the fix for a real, already-found bug: gnomAD's `variant_id` (`3-10149822-C-T`, 1-based) and ClinVar/Ensembl's SPDI (`10149821`, 0-based) disagree by exactly the offset a per-connector normalization would risk fixing inconsistently in two different places. One resolver, one conversion, done once.

**The resolver must consult a persistent identity index, not just the current staging batch.** The two-phase compiler in §9, as originally written, only resolved identity *within* one batch — a real gap given how this architecture actually runs: Core Biology is bulk-loaded once, but Evidence arrives across many separate `load()` calls over time, so a second run touching a variant already in the graph (say, a gnomAD refresh weeks after the initial ClinVar ingestion) needs to merge into the *existing* canonical node, not create a duplicate. The index stores more than a bare lookup — resolver evidence lives in the index itself, so "why does ClinVar's VCV000002218 and Ensembl's rs5030820 map to the same Forge Variant" is answerable by reading the index, not by rerunning the resolver six months later:

```python
@dataclass
class IdentityIndexEntry:
    canonical_id: str
    namespace: str                 # "SPDI" | "HGNC" | "MedGen" | ...
    normalized_key: str            # the exact key used to look this up, post-normalization
                                    # (e.g. gnomAD's variant_id after the off-by-one SPDI conversion)
    resolver_version: str
    confidence: float
    match_method: str
    first_seen_run: str
    last_seen_run: str
```

Same pattern as `SourceObject` → `SourceProvenance` (§2): a live computation result (`IdentityEvidence`, produced fresh each resolution) gets persisted into a durable, indexed form (`IdentityIndexEntry`) rather than discarded once the immediate resolution completes.

This is a different question from the Connector Planner's residency check (§13 / v2 §4.3), easy to conflate with it: residency answers *"should we fetch this anchor at all"* (anchor-granularity, pre-fetch); the identity index answers *"does this specific canonical entity already exist as a node"* (per-node granularity, during compilation). Both matter, at different points in the pipeline.

---

## 5. `SourceManifest` — mandatory, not design-only

```python
@dataclass
class SourceManifest:
    source: str
    dataset: str
    source_version: str | None     # null when the source doesn't self-report one
    release_id: str | None
    retrieved_at: datetime
    checksum: str | None
    endpoint: str | None
    schema_version: str
```

Confirmed necessary by re-checking our own fixtures, not assumed: ClinVar (`Version="44"`) and ClinGen (`"version": "1.1"`) self-report cleanly in-payload; Ensembl's REST responses carry per-object versions (gene v12, transcript v3) but no release-level version; gnomAD's response doesn't echo which dataset build actually served the query. `SourceProvenance.source_version` is `str | None` for exactly this reason — `SourceManifest` is the authoritative fallback.

`SourceManifest` answers *"what source/version/configuration is this"* — one per source+release, relatively static. It's not the same question as *"what exact bytes did we consume for this specific fetch,"* which one manifest can back many answers to (a single ClinVar release backs thousands of individual ESummary queries). That's `SourceSnapshot`:

```python
@dataclass
class SourceSnapshot:
    manifest_ref: str              # which SourceManifest this snapshot was taken under
    request_hash: str              # hash of the outbound query — this is what backs
                                    # §9's idempotent-retry cache
    response_hash: str | None      # hash of the raw response, for bulk files: file checksum instead
    endpoint: str | None
    request_parameters: dict
    retrieved_at: datetime
```

One API response (one `SourceSnapshot`) can contain multiple records, each becoming its own `SourceObject` with its own `payload_hash` (§2) — snapshot and payload hash operate at different levels (raw fetch vs. individual parsed record) and both are needed for the "same four inputs → same graph" claim in §6 to actually hold, not just be asserted.

---

## 6. Run identity and reproducibility

Every ingestion run carries `RunID`, `Source`, `Dataset`, `SourceVersion`, `MappingSpecVersion`, `CompilerVersion`. Same four inputs → same canonical graph, verified by comparing IR hashes. Determinism requires one easy-to-miss discipline: **canonical/sorted serialization before hashing** — dict key order and parallel-fetch response ordering are real, easy ways to silently break "same inputs, same hash" without anyone noticing until a diff looks wrong.

---

## 7. Resumability

```python
@dataclass
class IngestionCheckpoint:
    run_id: str
    partition_id: str
    source_cursor: str
    last_record_id: str | None
    status: str
    checksum: str | None
```

Scope note: the contract exists now so it isn't retrofitted against a live system later, but the VHL E2E test (a few hundred records) doesn't need to exercise real crash-recovery — that gets stress-tested when bulk ClinVar ingestion actually runs.

---

## 8. Batch atomicity + quarantine vs. reject

Micro-batches (~10k source records), not one transaction per 4.5M-record source. Within a batch:

| | Meaning | Example |
|---|---|---|
| **QUARANTINE** | Local, single-record problem — batch continues | One SCV with an unresolvable condition ID |
| **REJECT** | Batch-level invariant broken — nothing partially committed | An edge endpoint missing from the whole batch, record-conservation count mismatch, mapping registry itself malformed |

`QUARANTINE = local data problem. REJECT = pipeline integrity problem.` Keeping this distinction crisp is what makes failures debuggable instead of "ingestion failed, somewhere."

---

## 9. Two-phase compiler + DAG-scheduled connectors

**Compiler must be two-phase**, and this closes a real gap that wasn't explicit before: nodes/edges emitted during normalization reference *temporary* IDs, because identity resolution (which can merge a node into an existing canonical one) hasn't run yet when an edge is first created. Phase A (staging) allows unresolved references; Phase B rewrites every staged edge's src/dst to canonical IDs once identity resolution completes for the whole batch, then dedupes, verifies, commits.

**Connector scheduling is a DAG, not a fixed sequence**, because the real dependency structure demands it: Ensembl, ClinVar, and ClinGen can run concurrently (nothing in `gene`-scope loading needs one before another), but gnomAD genuinely needs the canonical variant identity that Ensembl/ClinVar identity resolution produces before it can query — a rigid pipeline can't express "these three are parallel, that one depends on their output" correctly.

Dependency edges in the DAG carry a `dependency_type: Literal["HARD", "SOFT"]` — HARD blocks (gnomAD on canonical variant identity, confirmed necessary above), SOFT allows optimistic concurrent start where a task *may* use another's output but doesn't require it (e.g. a future UniProt enrichment that can use resolved Variant identity if available but doesn't need to wait for all ClinVar evidence to finish first). **Scope note:** every dependency in the current 4-connector build is HARD — gnomAD→identity is the only real edge, and Ensembl/ClinVar/ClinGen are mutually independent. `SOFT` is schema-ready (the field costs nothing to add now) but has no real exerciser until UniProt or PubMed connectors exist, so no differentiated scheduling logic gets built around it yet — just the field, so it isn't a breaking change later.

```python
@dataclass
class RatePolicy:
    requests_per_second: float | None = None   # no core-level defaults, ever — None until the
    max_concurrency: int | None = None          # connector's own manifest/config supplies it
    batch_size: int | None = None
    retry_policy: "RetryPolicy | None" = None

@dataclass
class RetryPolicy:
    retryable: set[Literal["transient", "rate_limit"]]   # NOT "auth", "malformed_payload", "schema_mismatch"
    max_attempts: int
    request_hash_cache: bool = True   # idempotent retries — same request_hash, no duplicate parse/commit
```

Every connector declares its own values (Ensembl's own documented limits, ClinVar/NCBI's documented 3–10 req/s, gnomAD's batched-GraphQL characteristics, ClinGen's low volume) — the core `RatePolicy` type carries no defaults and no orchestration-level assumptions about any specific source, so Forge stays generic as APIs and their limits change over time.

---

## 10. Edge ledger

Every committed edge is independently auditable:

```
edge_id | src | predicate | qualifier | dst | source | source_record_id | mapping_rule_id | origin | compiler_version
```

Answers "why does this edge exist" without reading code — most valuable for `REPRESENTS` edges and any `DERIVED`-origin edge (e.g. a `RULE --SUPPORTS--> ExpertPanelClassification` edge Forge computed itself vs. one mirrored directly from ClinGen's erepo).

---

## 11. Mapping coverage report (per connector run)

```
ClinVar ─────────────────────────
records read/parsed/emitted/quarantined:  22 / 22 / 22 / 0
field coverage:    variation_id 100%, classification 100%, condition 99.8%, submitter 100%
edge coverage:     ASSERTS 100%, for_condition 99.8%, submitted_by 100%
mapping origin:    declarative rules 71%, escape hatches 29%
                     └─ clinvar_scv_rcv_linking: 22 records, clinvar_condition_trait_match: 22 records
```

More diagnostic than a boolean "ingestion succeeded" — directly extends the connector-certification concept from the original architecture notes.

One subtlety worth being precise about: under §8's semantics, REJECT is batch-level ("nothing partially committed"), so a per-record `rejected` count in a *committed* batch's report is always zero by construction — a batch containing a rejected record never reaches this report at all. The meaningful version of "read = parsed + quarantined + rejected" lives one level up, at the **run** level, across batches: `batches_attempted == batches_committed + batches_rejected`. That's the honest place for the invariant, given rejection is batch-granular, not record-granular, in this design.

---

## 12. Rule/Requirement IR — evaluation_mode and strength-tier preservation

```python
class EvaluationMode(Enum):
    MECHANICAL = "MECHANICAL"    # clean numeric/boolean predicate
    COMPOSITE  = "COMPOSITE"     # AND/OR of mechanical sub-checks
    NARRATIVE  = "NARRATIVE"     # prose decision tree, not executable as written
    HYBRID     = "HYBRID"        # narrative with some embedded numeric sub-conditions

@dataclass
class Requirement:                     # RULE subtype, per forge-ir-v3-schema.md §2
    criterion_id: str                  # "ClinGen:GN078:PM2:1.1"
    requirement_id: str                # "ClinGen:GN078:PM2:1.1:req:01"
    ordinal: int
    strength: str                      # "Supporting" | "Moderate" | "Strong" | ...
    applicability: str                 # "Applicable" | "Not Applicable" | "Not Applicable for this VCEP"
    evaluation_mode: EvaluationMode
    constraints: dict | None           # populated only when evaluation_mode allows mechanical checking
    source_text: str
```

**Preserve every strength tier**, applicable or not — real fixture confirms every criterion has up to 5 (`Stand Alone`/`Very Strong`/`Strong`/`Moderate`/`Supporting`), and VHL's PM2 having only `Supporting` applicable is a property of *this gene*, not a property of the schema. Flattening to one `Requirement` per criterion was correct by accident for PM2, not correct in general.

Real classification, confirmed against the actual fixtures, not assumed:

| Criterion | Mode | Why |
|---|---|---|
| `PM2` | `MECHANICAL` | `GroupMax Filtering AF (faf95_popmax) <= 0.00000156`, **plus a documented fallback**: ClinGen's own spec text says PM2_Supporting may still apply when no Filtering AF is calculated at all (e.g. a singleton, ac=1) — confirmed necessary during implementation, not a hypothetical edge case: our actual VHL test variant IS this case (`faf95.popmax` is `null` in the real gnomAD response, `ac=1`), and a naive single-threshold comparison (treating plain `af ≈ 6.57e-6` as if it were the Filtering AF and comparing it to the 1.56e-6 threshold) gives the *wrong* verdict. Real, implemented result: PM2_Supporting **SATISFIED** via the fallback clause, not CONTRADICTED as a naive reading of the raw numbers would suggest. |
| `BP4` | `COMPOSITE` | Requires SpliceAI (≤0.1) **and** VarSeak (Class 1/2) concordance — a real AND, but not evaluable with the current 4-source stack (no SpliceAI/VarSeak connector) — classified correctly, not yet operationally testable |
| `PVS1` | `NARRATIVE` | Multi-paragraph decision tree (NMD windows, domain boundaries, exon-skipping caveats) — not decomposed logic in the source |
| `BS4` | `NARRATIVE` | Pedigree/segregation narrative ("Danish Criteria," age thresholds, phenocopy caveats) |

`Requirement.constraints`, corrected to match the real PM2 spec text exactly (not a simplified single-threshold guess):
```python
{
    "evidence_type": "PopulationFrequency",
    "metric": "faf95_popmax",           # NOT plain `af` -- a different, specific gnomAD statistic
    "operator": "<=",
    "value": 0.00000156,
    "fallback_if_unavailable": {
        "condition": "ac_le_1",
        "reason": "GroupMax Filtering AF not calculated due to single variant present (ClinGen GN078 PM2 spec, verbatim)",
    },
}
```

Don't pretend prose has become executable logic. `NARRATIVE`/`HYBRID` rules get `source_text` preserved and `constraints = None` — a later `DERIVED`-origin structured interpretation can be added without touching the original source-mirrored `RULE`.

---

## 13. `ConnectorCapabilities` and `plan_load()`

```python
@dataclass
class ConnectorCapabilities:
    scopes: list[str]                  # ["variant", "gene", "region"]
    provides: list[str]                # node/observation/claim subtypes this source can produce
    bulk: bool
    live_query: bool
    batch: bool
    hydration_lods: list[int]
    identity_types: list[str]          # ["SPDI", "HGVS", "rsID"]

@dataclass
class FetchTask:
    connector: str
    query: dict
    lod_ceiling: int
    reason: Literal["not_resident", "stale", "gap_fill"]
    estimated_cost: float | None       # present for inspectability; real cost model deferred (see below)
    estimated_records: int | None
```

`ScopeResolver`/`ConnectorPlanner` discover what's available from `capabilities()` rather than hardcoding `if gene: use_clinvar(); use_gnomad()` — this is the actual mechanism that makes "generalized connector framework" true rather than aspirational.

```python
@dataclass(frozen=True)
class LoadPlan:
    plan_id: str
    created_at: datetime
    planner_version: str
    anchor: str | list[str]
    scope: str
    policy: Policy
    tasks: tuple[FetchTask, ...]
    dependencies: tuple[tuple[str, str, Literal["HARD", "SOFT"]], ...]  # (task_id, depends_on_task_id,
                                                                          # dependency_type) — DAG from §9
```

`created_at` + `planner_version` make a plan fully self-describing for audit ("Plan 8e91…, created 13:45, planner v0.3, policy standard, scope gene") and set up the comparison this is eventually for: deterministic planner plan vs. MHLA's actual activated subgraph, once MHLA exists to compare against.

`forge.plan_load(anchor, scope, policy) -> LoadPlan` returns the full plan — scope resolution, identity anchors, residency check, required connectors, fetch tasks, LOD ceiling, expected new nodes, expected API calls — **before any network I/O**. `forge.execute(plan)` runs it. `LoadPlan` is frozen and its task list a tuple, deliberately: once computed, a plan is never mutated — if requirements change mid-execution, a *new* plan gets generated, referencing the old one as superseded, rather than editing a plan already in flight. (This is the opposite call from `ForgeNode`, which explicitly must stay mutable per v3 §6.3's in-place evidence updates — different questions, different answers, not a blanket immutability stance.) A frozen plan serializes cleanly for logging/audit before execution, so "why did Forge fetch 2,000 ClinVar records" is answerable by inspecting the stored plan, not by reconstructing the planner's reasoning after the fact — and it's the natural artifact to diff against once MHLA's own retrieval policy exists, to compare what the deterministic planner would have fetched against what MHLA actually activated.

Scope note: `estimated_cost`/`estimated_records` are present as fields now so the plan is inspectable end-to-end, but the actual cost model (bytes vs. request count vs. latency weighting) is deferred until there's real throughput data to calibrate against — this is the seed of MHLA's eventual cost-aware retrieval, not a finished cost engine.

**Layering rule, stated explicitly so it doesn't erode over time:** the planner decides *what to fetch and how deep* (`lod_ceiling` on `FetchTask`); the mapper decides *what a `SourceObject` semantically means* (§2–§3). A connector's mapping code must never branch on LOD (`if lod >= 2: emit(...)`) — that turns LOD into a source-specific mess duplicated across every connector. The full separation, one line per layer: Planner → what to fetch. Parser → what it means, structurally. Mapper → how it becomes Forge IR. Identity → who it is. Verifier → whether it's legal. Compiler → where it goes.

---

## 14. Per-source rules made explicit, not implicit

Decisions we already made get written into the mapping spec as auditable entries, not silent omissions:

```python
MappingRule(
    field_map={"clinical_significance": None},
    action="DROP",
    drop_reason="duplicate_non_authoritative_clinical_assertion",  # mirrors ClinVar; don't double-count
)
```

- **ClinVar**: dedicated hierarchy parser (`VCV → ClassifiedRecord → {SimpleAllele, RCVList[RCV → condition, classification], SCVs[submitter, classification, ObservedIn, attributes]}`) — never "one record → one Claim." Confirmed structure: 1 VCV, 7 RCVs (one with a distinct `SomaticClinicalImpact` axis), 22 SCVs for our test variant.
- **Ensembl**: `clinical_significance` dropped, reason declared inline as above. VEP consequences stay **transcript-scoped** — one `RELATES_TO[has_consequence]` edge per transcript with its own attributes, never collapsed into one "the" consequence (confirmed real: 9 different transcripts, different amino-acid outcomes for the same genomic change on at least two of them).
- **gnomAD**: stays compact by default — `PopulationFrequency[genome]` / `PopulationFrequency[exome]`, ~30-row population breakdown kept as a nested attribute, never exploded into per-population nodes unless a policy explicitly asks. This is exactly the graph-explosion case the LOD system exists to prevent.

---

## 15. Pre-commit fixture suite

| # | Fixture | Status |
|---|---|---|
| 1 | One ClinVar VCV | ✅ have it (VCV000002218) |
| 2 | One RCV + multiple SCVs | ✅ have it (same fixture) |
| 3 | Conflicting SCVs | ✅ have it — VCV004558169 (VHL c.241C>A / p.Pro81Thr), `review_status: "criteria provided, conflicting classifications"`, 2 SCVs genuinely disagreeing (SCV007155876: Uncertain significance vs. SCV007485152: Likely pathogenic) |
| 4 | One VHL Ensembl record, multiple transcripts | ✅ have it |
| 5 | gnomAD genome + exome | ✅ have it |
| 6 | ClinGen PM2 (mechanical) | ✅ have it |
| 7 | ClinGen PVS1 (narrative) | ✅ have it |

Then the full E2E: ClinVar + Ensembl + gnomAD + ClinGen → canonical IR → identity resolution → verification → graph → PM2 `Requirement` → gnomAD `PopulationFrequency` observation → `RequirementEvaluator` → `RequirementState`, asserting whichever terminal state the real observation actually produces via `evaluate_requirement` (§12) — computed fresh from the real `faf95_popmax`/`ac`/`af` values each run, not hardcoded to a specific status. (Implemented and confirmed: this resolves to `SATISFIED` via PM2's documented singleton fallback, not the `CONTRADICTED` a naive plain-`af`-vs-threshold comparison would have wrongly produced — see §12.)

---

## 16. Implementation order

1. Fetch the missing conflicting-SCV fixture (§15, item 3) — the one real gap in test data.
2. `forge_ir/core.py` — v3 §1–§5 as real code (enums, registries, dataclasses).
3. `forge_ir/verify.py` — v3 §7 checks, tested against deliberately-broken fixtures first.
4. `SourceObject` base + per-source types (§2), `MappingSpec`/`MappingCompiler` (§3), persistent `IdentityIndex` + `IdentityResolver` (§4), `SourceManifest`/`SourceSnapshot` (§5), two-phase staging compiler (§9).
5. Four source adapters (`fetch()` + `capabilities()` + `manifest()` only — §2, §5, §13).
6. `ScopeResolver` / `ConnectorPlanner` / `plan_load()` returning a frozen `LoadPlan` (§13), `variant` and `gene` scopes first.
7. VHL end-to-end: full pipeline through `RequirementState` reaching a terminal status on the real PM2/gnomAD numbers.
8. Only then: MHLA touches the graph.

Every node MHLA will ever see will have already survived a deterministic, verified, source-to-semantic compilation — that's the property this whole document exists to guarantee.

---

## 17. Terminology

Different people will build different layers of this — these terms need to mean one thing each:

| Term | Meaning |
|---|---|
| `SourceRecord` | Raw source response unit — exactly what the adapter got back, untouched |
| `SourceSnapshot` | The exact network/file retrieval event — request/response hashes, endpoint, params (§5) |
| `SourceObject` | Typed, parsed, source-shaped semantic object — stable structure, not yet Forge IR (§2) |
| `MappingSpec` / `MappingRule` / `EscapeHatch` | The declarative-plus-named-exception transformation from `SourceObject` to Forge IR (§3) |
| `ForgeNode` / `ForgeEdge` | Canonical graph IR — the only thing that gets committed (`forge-ir-v3-schema.md` §3) |
| `IdentityIndex` / `IdentityIndexEntry` | Persistent, cross-batch, cross-run canonical-identity lookup and its audit trail (§4) |
| `LoadPlan` | A frozen, inspectable, pre-execution fetch plan (§13) |
| `Requirement` | One strength-tier of one ACMG criterion, with its own constraints and `evaluation_mode` (§12) |
| `RequirementState` | Runtime, per-investigation tracking of whether a `Requirement` is satisfied (`forge-ir-v3-schema.md` §6.1) |
| `CompiledGraph` | The verified, persistent graph representation a `GraphCompiler` produces (`forge-ir-v3-schema.md` §8) |
