# Forge IR v3 — Node/Edge Schema, State Machines, Planner Contracts

**Status: implementation-ready spec.** This is the doc to build `.py` modules against. Supersedes the schema fragments in `forge-ir-v2-and-loading-architecture.md` (kept as historical rationale — see its §7 for how we got here). Node/edge *mapping* decisions (which source field becomes which node/edge) still live in `connector-research.md` §5 and aren't repeated here — this doc is the types those mappings produce, plus the runtime machinery around them.

---

## 1. Enums

```python
class NodeRole(Enum):
    ENTITY = "ENTITY"
    OBSERVATION = "OBSERVATION"
    CLAIM = "CLAIM"
    RULE = "RULE"
    INVESTIGATION = "INVESTIGATION"

class EdgeType(Enum):
    RELATES_TO = "RELATES_TO"
    PART_OF = "PART_OF"
    REPRESENTS = "REPRESENTS"
    ASSERTS = "ASSERTS"
    OBSERVES = "OBSERVES"
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    SOURCED_FROM = "SOURCED_FROM"
    REQUIRES = "REQUIRES"
    APPLIES_TO = "APPLIES_TO"
    ACTIVATED_BY = "ACTIVATED_BY"

class Layer(Enum):
    CORE_BIOLOGY = "CORE_BIOLOGY"
    EVIDENCE = "EVIDENCE"
    OPERATIONAL = "OPERATIONAL"
    ADMINISTRATIVE = "ADMINISTRATIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"      # RUNTIME_ONLY roles — layer is a persistence-tier
                                            # concept and doesn't mean anything for these

class StorageScope(Enum):
    PERSISTENT = "PERSISTENT"
    RUNTIME_ONLY = "RUNTIME_ONLY"

class Origin(Enum):
    SOURCE = "SOURCE"      # mirrors an external record
    DERIVED = "DERIVED"    # computed by Forge from other nodes/edges
    RUNTIME = "RUNTIME"    # created during an investigation, not persisted as knowledge
```

```python
ROLE_STORAGE_SCOPE: dict[NodeRole, StorageScope] = {
    NodeRole.ENTITY:        StorageScope.PERSISTENT,
    NodeRole.OBSERVATION:   StorageScope.PERSISTENT,
    NodeRole.CLAIM:         StorageScope.PERSISTENT,
    NodeRole.RULE:          StorageScope.PERSISTENT,
    NodeRole.INVESTIGATION: StorageScope.RUNTIME_ONLY,
}

EDGE_STORAGE_SCOPE: dict[EdgeType, StorageScope] = {
    EdgeType.ACTIVATED_BY: StorageScope.RUNTIME_ONLY,
    # all other predicates default to PERSISTENT
}
```

---

## 2. Node subtype registry

The single source of truth for `layer` derivation, canonical ID scheme, and required attributes. `ForgeNode.__post_init__` looks itself up here — a connector cannot set `layer` directly (v2 §1.1 / review point 8).

| Role | Subtype | Layer | Canonical ID scheme | Required attributes | Confirmed vs. provisional |
|---|---|---|---|---|---|
| ENTITY | `Gene` | CORE_BIOLOGY | `HGNC:{id}` | `symbol`, `biotype` | ✅ Ensembl, ClinVar |
| ENTITY | `Transcript` | CORE_BIOLOGY | `Ensembl:{ENST...}` | `gene_id`, `is_canonical`, `biotype` | ✅ Ensembl |
| ENTITY | `Exon` | CORE_BIOLOGY | `Ensembl:{ENSE...}` | `transcript_id`, `start`, `end` | ✅ Ensembl |
| ENTITY | `Protein` | CORE_BIOLOGY | `Ensembl:{ENSP...}` or `UniProt:{accession}` | `length` | ✅ Ensembl (Translation); UniProt pending |
| ENTITY | `Variant` | CORE_BIOLOGY | `SPDI:{seq}:{pos}:{ref}:{alt}` | `assembly`, `ref`, `alt` | ✅ Ensembl, ClinVar, gnomAD |
| ENTITY | `Disease` | CORE_BIOLOGY | `MedGen:{CUI}` | `name` | ✅ ClinVar |
| ENTITY | `Submitter` | ADMINISTRATIVE | `ClinVarOrg:{OrgID}` | `name`, `organization_category` | ✅ ClinVar |
| ENTITY | `SourceManifest` | ADMINISTRATIVE | `Source:{name}:{version}` | `retrieved_at` | design-only |
| OBSERVATION | `PopulationFrequency` | EVIDENCE | derived: `gnomAD:{variant_id}:{dataset}` | `ac`, `an`, `af`, `dataset` | ✅ gnomAD |
| OBSERVATION | `ComputationalPrediction` | EVIDENCE | derived: `{tool}:{variant_id}:{transcript_id}` | `tool`, `score`, `prediction` | ✅ Ensembl VEP (SIFT/PolyPhen) |
| OBSERVATION | `CaseObservation` | EVIDENCE | derived: `{scv_accession}:observed_in:{index}` | `origin`, `method_type` | ✅ ClinVar |
| OBSERVATION | `SegregationResult` | EVIDENCE | derived | — | ⚠️ provisional, no instance seen yet |
| OBSERVATION | `FunctionalResult` | EVIDENCE | derived | — | ⚠️ provisional |
| CLAIM | `ClinicalAssertion` | EVIDENCE | `ClinVar:{SCV accession}` | `review_status`, `classification` | ✅ ClinVar |
| CLAIM | `AggregateClassification` | EVIDENCE | `ClinVar:{RCV\|VCV accession}` | `scope` (`condition`\|`variant`), `description`, `review_status` | ✅ ClinVar |
| CLAIM | `ClinicalImpactClassification` | EVIDENCE | `ClinVar:{RCV accession}:impact` | `tier` | ✅ ClinVar (`SomaticClinicalImpact`) |
| CLAIM | `ExpertPanelClassification` | EVIDENCE | `ClinGen:{agent id}` | `outcome` | endpoint confirmed, no instance for VHL test variant |
| CLAIM | `Association` | EVIDENCE | — | — | ⚠️ provisional (future ClinGen gene-disease validity) |
| RULE | `ACMGCriterion` | OPERATIONAL | `ClinGen:{guideline_id}:{label}:{version}` | `label`, `base_strength`, `default_strength`, `evidence_category` | ✅ ClinGen cspec |
| RULE | `Requirement` | OPERATIONAL | derived: `{criterion_id}:requirement` | `constraints: dict`, `evidence_type: str` | ✅ derivable now — PM2's real threshold is the worked example |
| INVESTIGATION | `Query` | NOT_APPLICABLE | derived: `investigation:{uuid}` | `goal`, `created_at` | runtime |
| INVESTIGATION | `RequirementState` | NOT_APPLICABLE | derived: `{investigation_id}:{requirement_id}` | `requirement_id`, `status`, `matched_node_ids`, `result_strength`, `transition_reason` | runtime — see §6.1 |

A node whose `(role, subtype)` isn't in this table fails type-correctness verification (§7) outright — no implicit subtypes.

---

## 3. Dataclasses

```python
from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime

class Provenance(ABC):
    """Base type. Never instantiated directly — always one of the three below,
    selected by the node/edge's `origin`."""

@dataclass
class SourceProvenance(Provenance):
    source: str                # "ClinVar" | "Ensembl" | "gnomAD" | "ClinGen"
    source_record_id: str      # e.g. "SCV001473258", "ENST00000256474"
    source_version: str        # e.g. ClinVar release date, cspec "1.1"
    retrieved_at: datetime

@dataclass
class DerivedProvenance(Provenance):
    derivation_rule: str        # e.g. "requirement_extraction_v1"
    input_node_ids: list[str]
    input_edge_ids: list[str]
    compiler_version: str
    derived_at: datetime

@dataclass
class RuntimeProvenance(Provenance):
    investigation_id: str
    activation_reason: str
    timestamp: datetime
```

```python
@dataclass
class ForgeNode:
    id: str
    role: NodeRole
    subtype: str
    attributes: dict
    provenance: list[Provenance]     # >1 entry when merged across sources (v2 §1.4)
    origin: Origin
    lod: int = 1                      # 0-4, per connector-research.md §3

    layer: Layer = field(init=False)
    storage_scope: StorageScope = field(init=False)

    def __post_init__(self):
        entry = NODE_SUBTYPE_REGISTRY[(self.role, self.subtype)]
        self.layer = entry.layer if self.storage_scope_is_persistent() else Layer.NOT_APPLICABLE
        self.storage_scope = ROLE_STORAGE_SCOPE[self.role]

    def storage_scope_is_persistent(self) -> bool:
        return ROLE_STORAGE_SCOPE[self.role] == StorageScope.PERSISTENT
```

```python
@dataclass
class ForgeEdge:
    src: str
    predicate: EdgeType
    dst: str
    qualifier: str | None       # e.g. "encodes", "in_gene", "has_consequence",
                                  # "contributes_to", "aggregates_into", "submitted_by",
                                  # "for_condition", "superseded_by"
    attributes: dict
    provenance: Provenance
    origin: Origin

    # REPRESENTS-only fields — required together, meaningless otherwise (review point 9)
    confidence: float | None = None
    match_method: str | None = None
    resolver_version: str | None = None

    def __post_init__(self):
        if self.predicate == EdgeType.REPRESENTS:
            missing = [f for f in ("confidence", "match_method", "resolver_version")
                       if getattr(self, f) is None]
            if missing:
                raise ValueError(f"REPRESENTS edge missing required fields: {missing}")
```

`RULE:Requirement.attributes["constraints"]` is where PM2's real threshold lives, concretely:
```python
{
    "evidence_type": "PopulationFrequency",
    "constraints": {
        "gnomad_version": "v4",
        "metric": "group_max_filtering_af",
        "operator": "<=",
        "value": 0.00000156,
    },
}
```

---

## 4. `ALLOWED_EDGE_COMBINATIONS` registry

Every triple the connectors or the runtime are permitted to emit. Anything not listed fails verification (§7.1). `*` means any subtype of that role.

```python
ALLOWED_EDGE_COMBINATIONS = [
    # --- Core Biology structure ---
    ("ENTITY:Transcript",  "PART_OF",  "ENTITY:Gene"),
    ("ENTITY:Exon",        "PART_OF",  "ENTITY:Transcript"),
    ("ENTITY:Transcript",  "RELATES_TO[encodes]",           "ENTITY:Protein"),
    ("ENTITY:Variant",     "RELATES_TO[has_consequence]",   "ENTITY:Transcript"),
    ("ENTITY:Variant",     "RELATES_TO[in_gene]",            "ENTITY:Gene"),

    # --- weak cross-source identity (not a merge) ---
    ("ENTITY:*",           "REPRESENTS",  "ENTITY:*"),

    # --- Evidence: Observations ---
    ("OBSERVATION:*",              "OBSERVES",  "ENTITY:Variant"),
    ("OBSERVATION:CaseObservation", "SUPPORTS", "CLAIM:ClinicalAssertion"),
    ("OBSERVATION:*",              "SUPPORTS",  "RULE:Requirement"),   # requirement satisfaction

    # --- Evidence: Claims (fixed per review points 1-2) ---
    ("CLAIM:ClinicalAssertion",        "RELATES_TO[contributes_to]",  "CLAIM:AggregateClassification"),
    ("CLAIM:AggregateClassification",  "RELATES_TO[aggregates_into]", "CLAIM:AggregateClassification"),  # RCV -> VCV
    ("CLAIM:*",                        "ASSERTS",                     "ENTITY:Variant"),
    ("CLAIM:*",                        "RELATES_TO[for_condition]",   "ENTITY:Disease"),
    ("CLAIM:ClinicalAssertion",        "RELATES_TO[submitted_by]",    "ENTITY:Submitter"),
    ("CLAIM:*",                        "SOURCED_FROM",                "ENTITY:SourceManifest"),
    ("CLAIM:*",                        "CONTRADICTS",                 "CLAIM:*"),          # disagreeing SCVs under one RCV
    ("CLAIM:*",                        "RELATES_TO[superseded_by]",   "CLAIM:*"),          # reclassification over time

    # --- Operational: Rules ---
    ("RULE:ACMGCriterion", "APPLIES_TO",                 "ENTITY:Gene"),
    ("RULE:ACMGCriterion", "REQUIRES",                   "RULE:Requirement"),
    ("RULE:ACMGCriterion", "SUPPORTS",                   "CLAIM:ExpertPanelClassification"),
    ("RULE:ACMGCriterion", "RELATES_TO[superseded_by]",  "RULE:ACMGCriterion"),  # spec version bump

    # --- Runtime only (never emitted by connectors — storage_scope=RUNTIME_ONLY) ---
    ("ENTITY:* | OBSERVATION:* | CLAIM:* | RULE:*", "ACTIVATED_BY", "INVESTIGATION:Query"),
]
```

---

## 5. Canonical ID schemes (consolidated)

| Entity | Scheme | Real example |
|---|---|---|
| Variant | SPDI | `SPDI:NC_000003.12:10149821:C:T` |
| Gene | HGNC | `HGNC:12687` |
| Transcript | Ensembl stable ID | `Ensembl:ENST00000256474` |
| Protein | Ensembl or UniProt | `Ensembl:ENSP00000256474` / `UniProt:P40337` |
| Disease | MedGen CUI | `MedGen:C0019562` |
| Submitter | ClinVar OrgID | `ClinVarOrg:25969` |
| ACMGCriterion | guideline+label+version | `ClinGen:GN078:PM2:1.1` |

Off-by-one note carried forward from connector-research.md §6: gnomAD's `variant_id` (`3-10149822-C-T`, 1-based) must be normalized to SPDI's 0-based `10149821` before comparison — identity resolution owns this conversion, not each connector individually.

---

## 6. State machines

### 6.1 `RequirementState` — the central control loop (review points 14–15)

```
States: UNSATISFIED → EVALUATING → SATISFIED
                    ↘ EVALUATING → CONTRADICTED
                    ↘ EVALUATING → NEEDS_INTERPRETATION
        SATISFIED → STALE  (retrieved_at of a matched node exceeds staleness_threshold)
        STALE → EVALUATING (refresh triggered)
        CONTRADICTED → EVALUATING (new evidence arrives)
        NEEDS_INTERPRETATION → EVALUATING (structured interpretation later added, DERIVED-origin)
```

Behavior branches on the `Requirement`'s `evaluation_mode` (forge-pipeline-v1.md §12) — this is what stops a `NARRATIVE` criterion from sitting indistinguishably in `UNSATISFIED` forever, as if it just hadn't found evidence yet, when actually no amount of evidence will let it auto-resolve:

| `evaluation_mode` | Evaluator behavior |
|---|---|
| `MECHANICAL` | May automatically reach `SATISFIED`/`CONTRADICTED` by checking `constraints` directly |
| `COMPOSITE` | Recursively evaluates child constraints (AND/OR), same terminal states |
| `NARRATIVE` | Cannot auto-satisfy — `EVALUATING` always resolves to `NEEDS_INTERPRETATION`, never `SATISFIED`/`CONTRADICTED` |
| `HYBRID` | Mechanical sub-conditions evaluated normally; if they alone are insufficient to reach a terminal state, resolves to `NEEDS_INTERPRETATION` for the narrative remainder, not `UNSATISFIED` |

| From | To | Trigger |
|---|---|---|
| `UNSATISFIED` | `EVALUATING` | MHLA activates a candidate `OBSERVATION`/`CLAIM` node reachable via `SUPPORTS → Requirement` |
| `EVALUATING` | `SATISFIED` | Candidate's attributes satisfy `Requirement.constraints` (e.g. `af <= 0.00000156`) — `MECHANICAL`/`COMPOSITE` only |
| `EVALUATING` | `CONTRADICTED` | Candidate's attributes actively violate the constraint in a way that rules out the criterion (not just "doesn't help") |
| `EVALUATING` | `NEEDS_INTERPRETATION` | `evaluation_mode` is `NARRATIVE`, or `HYBRID` with an unresolved narrative remainder — mechanical evaluation cannot proceed further |
| `EVALUATING` | `UNSATISFIED` | No candidate found / candidate doesn't satisfy, no further reachable transitions |

```python
@dataclass
class RequirementState:
    requirement_id: str
    investigation_id: str
    status: Literal["UNSATISFIED", "EVALUATING", "SATISFIED", "CONTRADICTED", "STALE", "NEEDS_INTERPRETATION"]
    matched_node_ids: list[str]
    result_strength: str | None       # the strength actually achieved — usually equals the matched
                                       # Requirement's own `strength`, but can differ under a downgrade
                                       # (real case: PVS1's "10% downgrade to Moderate" rule — N/A for
                                       # VHL specifically, but a real ACMG pattern, not hypothetical)
    transition_reason: dict           # e.g. {"constraint": "group_max_filtering_af <= 1.56e-6",
                                       #        "observed": 6.57e-6, "source": "gnomAD", "source_version": "r4"}
```
| `SATISFIED` | `STALE` | The satisfying node's `Provenance.retrieved_at` exceeds `staleness_threshold` |
| `STALE` | `EVALUATING` | Refresh fetch triggered (via Connector Planner residency+freshness check) |
| `CONTRADICTED` | `EVALUATING` | New evidence arrives (e.g. a re-`load()`) |

`RequirementState.status ∈ {UNSATISFIED, EVALUATING, SATISFIED, CONTRADICTED, STALE}`, `matched_node_ids: list[str]` populated on entering `SATISFIED`/`CONTRADICTED`.

### 6.2 Node hydration (LOD)

```
LOD0 → LOD1 → LOD2 → LOD3 → LOD4
```

Monotonic — hydration only escalates, never downgrades (a node is never "un-hydrated" to save space; it's just not re-fetched). Transition `LOD(n) → LOD(n+1)` requires: `origin == SOURCE` (only source-mirrored nodes get hydrated further; DERIVED/RUNTIME nodes don't have a deeper source form to fetch) and a resolvable `SourceProvenance.source_record_id`. This is the concrete form of "hydration-pointer integrity" (§7.4).

### 6.3 Evidence node lifecycle (freshness / versioning)

```
ACTIVE → STALE → { ACTIVE (refresh: unchanged) | SUPERSEDED (refresh: classification changed) }
ACTIVE → SUPERSEDED   (direct, on a proactive resync that finds a change without a prior STALE flag)
```

| From | To | Trigger |
|---|---|---|
| `ACTIVE` | `STALE` | `now - Provenance.retrieved_at > staleness_threshold` |
| `STALE` | `ACTIVE` | Refresh fetch confirms no material change (only `retrieved_at` bumped) |
| `STALE` | `SUPERSEDED` | Refresh fetch finds a changed classification/value → new `CLAIM`/`RULE` node created, `RELATES_TO[superseded_by]` edge from old → new |
| `ACTIVE` | `SUPERSEDED` | Same as above, found via scheduled resync rather than a staleness trigger |

`SUPERSEDED` nodes are never deleted — they stay queryable for "why did this change" reasoning (review points 3/6.4 in the confirm-deny cycle).

### 6.4 Ingestion pipeline run (per batch, per connector)

```
PENDING → FETCHING → NORMALIZING → RESOLVING_IDENTITY → VERIFYING → { COMMITTED | QUARANTINED | REJECTED }
```

| State | What happens | Failure exit |
|---|---|---|
| `FETCHING` | `SourceAdapter.fetch()` | network/auth error → `REJECTED`, batch-level, retryable |
| `NORMALIZING` | `Normalizer.normalize()` via `MappingSpec` | unmapped field/shape → per-record `QUARANTINED`, batch continues |
| `RESOLVING_IDENTITY` | `IdentityResolver.resolve()` per node | no canonical id resolvable → `QUARANTINED` (never force-merged) |
| `VERIFYING` | `Verifier.verify()` over the whole batch | any hard failure (§7.1–7.4) → batch `REJECTED`, nothing partially committed |
| `COMMITTED` | `GraphCompiler.compile()` writes nodes/edges | — |

Record-conservation (connector-research.md §7) is checked at the `COMMITTED` boundary: `records_in == committed + quarantined + rejected`, exactly.

---

## 7. Verification — formal checks

Carried forward from connector-research.md §7 and forge-ir-v2 §5, restated as concrete predicates over `ForgeNode`/`ForgeEdge` batches:

1. **Type correctness** — `(node.role, node.subtype) ∈ NODE_SUBTYPE_REGISTRY` and `(src_role:subtype, predicate[qualifier], dst_role:subtype) ∈ ALLOWED_EDGE_COMBINATIONS` for every edge.
2. **Referential integrity** — `edge.src` and `edge.dst` resolve to nodes present in the batch or already committed.
3. **Record conservation** — tracked in *record* units, not conflated with node/edge counts (one source record legitimately produces several nodes and edges, so `records != nodes + edges`): `source_records_in == committed_source_records + quarantined_source_records + rejected_source_records`. Node/edge emission is tracked separately as a non-conserved downstream quantity — see `forge-pipeline-v1.md` §11's coverage report for the field/edge-coverage percentages that make sense of it.
4. **Layer-tag consistency** — `node.layer == NODE_SUBTYPE_REGISTRY[(node.role, node.subtype)].layer`, recomputed, never trusted from connector output.
5. **Multi-provenance integrity** — every `Provenance` entry in `node.provenance` independently valid for its `Origin` type (see §3's three dataclasses); conflicting attribute values across entries are retained with a distinguishing qualifier, never silently overwritten.
6. **`REPRESENTS` confidence integrity** — `confidence`, `match_method`, `resolver_version` all present (enforced in `ForgeEdge.__post_init__`); a `REPRESENTS` edge never collapses two nodes into one during identity resolution.
7. **Hydration-pointer integrity, origin-branched** —
   - `origin == SOURCE` → must have `SourceProvenance` with resolvable `source_record_id`.
   - `origin == DERIVED` → must have `DerivedProvenance` with non-empty `input_node_ids`.
   - `origin == RUNTIME` → must have `RuntimeProvenance` with a valid `investigation_id`.
8. **Connectivity integrity** (new, from the live-ingestion question) — every `EVIDENCE`-layer node has a path (≤2 hops) to a `CORE_BIOLOGY` node; orphaned evidence fails.
9. **Storage-scope integrity** — no `RUNTIME_ONLY` role/edge (`INVESTIGATION`, `ACTIVATED_BY`) appears in a connector-sourced (`origin == SOURCE`) batch.

```python
@dataclass
class VerificationReport:
    valid: bool
    type_errors: list[str]
    referential_errors: list[str]
    conservation_errors: list[str]
    layer_errors: list[str]
    provenance_errors: list[str]
    hydration_errors: list[str]
    connectivity_errors: list[str]
    storage_scope_errors: list[str]
```

---

## 8. Planner contracts

Interfaces only — no implementation. These are what the Phase 2 connector contract (from the original approved plan) and the `load()` orchestration (v2 §4) actually compile down to.

```python
from typing import Protocol

@dataclass
class ResolutionPlan:
    canonical_anchors: list[str]
    scope: str
    gap_fill_required: list[str]     # anchors not yet resident in persistent Core Biology

class ScopeResolver(Protocol):
    def resolve(self, anchor: str | list[str], scope: str) -> ResolutionPlan: ...


@dataclass
class FetchTask:
    connector: str
    query: dict
    lod_ceiling: int
    reason: str          # "not_resident" | "stale" | "gap_fill"

@dataclass
class Policy:
    name: str             # "index_only" | "standard" | "clinical" | "evidence_heavy"
    lod_ceiling: int
    clinvar_efetch_filter: str | None   # None = all; else e.g. "review_status != no_assertion"

class ConnectorPlanner(Protocol):
    def plan(self, resolution: ResolutionPlan, policy: Policy) -> list[FetchTask]: ...
    def check_residency(self, anchor: str) -> bool: ...
    def check_freshness(self, node: ForgeNode, threshold: timedelta) -> bool: ...


class SourceAdapter(Protocol):
    def fetch(self, query: dict) -> "SourceRecord": ...
    def capabilities(self) -> "ConnectorCapabilities": ...
    def manifest(self) -> "SourceManifest": ...

class Normalizer(Protocol):
    def normalize(self, record: "SourceRecord", spec: "MappingSpec") -> list[ForgeNode | ForgeEdge]: ...


@dataclass
class IdentityResolution:
    canonical_id: str | None            # None => quarantine, never force-merged
    merge: bool                          # True => attach as additional Provenance to existing node
    represents_edge: ForgeEdge | None    # set when merge=False but a weak link is warranted

class IdentityResolver(Protocol):
    def resolve(self, node: ForgeNode) -> IdentityResolution: ...


class Verifier(Protocol):
    def verify(self, nodes: list[ForgeNode], edges: list[ForgeEdge]) -> VerificationReport: ...


@dataclass
class RequirementStateTransition:
    new_status: str                      # one of §6.1's states
    matched_node_ids: list[str]

class RequirementEvaluator(Protocol):
    def evaluate(self, requirement: ForgeNode, candidates: list[ForgeNode]) -> RequirementStateTransition: ...


class GraphCompiler(Protocol):
    def compile(self, nodes: list[ForgeNode], edges: list[ForgeEdge]) -> "CompiledGraph": ...
```

---

## 9. What's next

This is the last design doc before code. Implementation order, matching the already-approved plan's phases but now against this exact schema:

1. `forge_ir/core.py` — §1–§3 (enums, registries, dataclasses) as actual code.
2. `forge_ir/verify.py` — §7 checks, unit-tested against deliberately-broken fixtures (bad edge combo, orphaned evidence, missing REPRESENTS fields, etc.) before ever touching real data.
3. `connectors/{ensembl,clinvar,gnomad,clingen}.py` — Phase 2 adapters, now emitting the v3 types directly, mapping tables from connector-research.md §5 with the PART_OF/SOURCED_FROM fixes applied.
4. `orchestration/{scope_resolver,connector_planner}.py` — §8 contracts, `variant`/`gene`/`gene_set`/`region` scopes first (`gene_family` stays experimental per v2 §7 point 13).
5. `tests/test_vhl_e2e.py` — the original plan's E2E assertions, now also asserting: a `Requirement` node exists for PM2 with the real constraint, gnomAD's `PopulationFrequency` observation correctly evaluates against it (§6.1's `EVALUATING → SATISFIED/UNSATISFIED` transition, testable directly since we already know the real numbers: observed `af ≈ 6.57e-6` vs. threshold `1.56e-6` — worth confirming which way that resolves once the evaluator exists, rather than assuming), and `RequirementState` reaches a terminal state.

Then optimization and the broader benchmark suite, per the original architecture's evaluation topic — not before this passes on the VHL vertical slice.
