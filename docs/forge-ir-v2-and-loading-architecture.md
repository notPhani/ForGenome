# Forge IR v2 — Ontology Scrutiny, Persistent Loading Architecture, Orchestration, and Correctness

**Status: FROZEN as historical reasoning — §7 below records the final resolved decisions after a full architecture review. The implementation-ready schema is [`forge-ir-v3-schema.md`](forge-ir-v3-schema.md); read that for anything you're about to build against. This doc stays useful for the *why*.**

**Status: design only, no code.** Builds directly on [`connector-research.md`](connector-research.md) — this doc doesn't re-derive the payload evidence, it applies it. Read §5–§7 of that doc first if you need the raw field-level grounding for any claim made here.

---

## 1. Ontology scrutiny — verdict

I tested every one of the 5 roles and 11 edges against the real mapping table (§5 of connector-research.md) instead of eyeballing the list abstractly. Result: **no roles added or removed, no edges added or removed** — but two real gaps surfaced, and I'm fixing both without touching the frozen counts.

### 1.1 The gap: ENTITY was doing two jobs

`ENTITY` currently holds `Gene`/`Variant`/`Protein` (core biology) *and* `Submitter`/`SourceManifest` (administrative bookkeeping) with nothing distinguishing them except subtype string. That's fine for the graph compiler, but it's exactly the thing an LLM traversing the graph needs a cheap signal for — "is this node something biologically real, or is it plumbing?" Rather than splitting `ENTITY` into two roles (which breaks the frozen-5 commitment and creates the same ambiguity one level down — is a `Submitter` really not an "entity"? it is one, just not a biological one), I'm adding an **orthogonal `layer` tag**, independent of `role`:

```
layer ∈ { CORE_BIOLOGY, EVIDENCE, OPERATIONAL, ADMINISTRATIVE }
```

This is the machine-checkable version of the three-tier source split you defined at the start (biology / evidence / operational) plus a fourth bucket for provenance/bookkeeping nodes that aren't part of any of the three. Every node gets exactly one `layer`, derived deterministically from its `role`+`subtype` via a registry (§1.2), never set ad hoc by a connector. This is what lets an LLM (or MHLA) filter "give me the biology backbone" vs "give me only evidence" vs "skip administrative nodes entirely" without inspecting subtypes one by one.

### 1.2 Node subtype registry (formalized from real payloads)

| Role | Subtype | Layer | Confirmed from real payload? |
|---|---|---|---|
| ENTITY | Gene | CORE_BIOLOGY | ✅ Ensembl, ClinVar |
| ENTITY | Transcript | CORE_BIOLOGY | ✅ Ensembl |
| ENTITY | Exon | CORE_BIOLOGY | ✅ Ensembl |
| ENTITY | Protein | CORE_BIOLOGY | ✅ Ensembl (Translation); UniProt pending |
| ENTITY | Variant | CORE_BIOLOGY | ✅ Ensembl, ClinVar, gnomAD |
| ENTITY | Disease | CORE_BIOLOGY | ✅ ClinVar (MedGen-keyed) |
| ENTITY | Submitter | ADMINISTRATIVE | ✅ ClinVar |
| ENTITY | SourceManifest / ResourceRef | ADMINISTRATIVE | design-only so far, not yet instantiated |
| OBSERVATION | PopulationFrequency | EVIDENCE | ✅ gnomAD |
| OBSERVATION | ComputationalPrediction | EVIDENCE | ✅ Ensembl VEP (SIFT/PolyPhen) |
| OBSERVATION | CaseObservation | EVIDENCE | ✅ ClinVar `ObservedInList` |
| OBSERVATION | SegregationResult | EVIDENCE | *provisional* — named by ClinGen's `evidenceCategory` but no instance seen yet |
| OBSERVATION | FunctionalResult | EVIDENCE | *provisional* — expected once a "Functional Data" criterion or an SCV with functional-assay evidence is ingested |
| CLAIM | ClinicalAssertion | EVIDENCE | ✅ ClinVar SCV |
| CLAIM | AggregateClassification | EVIDENCE | ✅ ClinVar RCV (scope=condition) and VCV (scope=variant) |
| CLAIM | ClinicalImpactClassification | EVIDENCE | ✅ ClinVar (`SomaticClinicalImpact` on RCV000002303) |
| CLAIM | ExpertPanelClassification | EVIDENCE | design-only — real endpoint confirmed (erepo), but not populated for our test variant |
| CLAIM | Association | EVIDENCE | *provisional* — reserved for future ClinGen Gene-Disease Validity curations, not sourced yet |
| RULE | ACMGCriterion | OPERATIONAL | ✅ ClinGen cspec (34 criteria for VHL v1.1) |

Subtypes marked *provisional* are kept in the registry now (not invented later) precisely so verification (§5) can distinguish "a connector emitted an unregistered subtype — reject/quarantine" from "a known-but-not-yet-seen subtype — accept."

### 1.3 Edge predicates: the two "unused" ones, resolved

Of the 11 frozen predicates, 9 were directly exercised by the real mapping (§5 of the research doc). Two weren't — I ran both down instead of leaving them decorative:

- **`ACTIVATED_BY`** — confirmed runtime-only. No connector ever emits it; it's created by MHLA at query time, linking a node into a specific `INVESTIGATION`. Same is true of `INVESTIGATION` itself as a role — connectors never populate it. Stating this explicitly matters because it means the *persistent* graph (this doc's subject) never contains `INVESTIGATION` nodes or `ACTIVATED_BY` edges at all; they exist only in the ephemeral per-query working subgraph.

- **`REPRESENTS`** — genuinely unused in the mapping, but not decorative once I looked for the actual gap it should fill: **weak, unmerged cross-source identity linkage.** Example, real and current: Ensembl's `Translation` (`ENSP00000256474`) and UniProt's `Protein` (`P40337`) very likely refer to the same real-world protein, but we haven't ingested UniProt yet, and even once we do, matching an Ensembl protein ID to a UniProt accession isn't always 1:1-certain (isoforms, versioning). Force-merging them into one node the moment they *look* related is exactly the "false merges are more dangerous than unresolved identity" trap your original architecture flagged. `REPRESENTS` becomes the edge for exactly this: `ENTITY:Translation --REPRESENTS[confidence=0.9]--> ENTITY:Protein`, kept as two distinct nodes, linked but not merged. This is different from full identity merge (one node, multiple `Provenance` entries — see §1.4) and different from `RELATES_TO` (a genuine, confirmed biological relationship). Concretely: `RELATES_TO` = "these are different things that relate," `REPRESENTS` = "these might be the same thing, not confirmed enough to merge," merge = "these are the same thing."

**Verdict: ontology stays at 5 roles / 11 edges.** The fix was precision (a layer tag, a subtype registry, a real definition for `REPRESENTS`), not expansion.

### 1.4 Concrete IR schema diffs from v1 (connector-research.md §4)

```diff
 ForgeNode:
   id: str
   role: NodeRole
   subtype: str
+  layer: Layer                  # derived from (role, subtype) via the §1.2 registry — never set directly by a connector
   attributes: dict
-  provenance: Provenance
+  provenance: list[Provenance]   # a merged node (e.g. Gene seen by both Ensembl and ClinVar) carries one entry per contributing source
   lod: int

 ForgeEdge:
   src: str
   predicate: EdgeType
   dst: str
   qualifier: str | None
   attributes: dict
+  confidence: float | None       # required when predicate == REPRESENTS; meaningless/omitted otherwise
   provenance: Provenance
```

`provenance` becoming a list is not cosmetic — it's required by a real thing already seen in the payloads: ClinVar's `RCVAccession` carries **both** a `GermlineClassification` (GRCh38-relative) and, on `VariationArchive`, sequence locations for **both** GRCh38 and GRCh37 assemblies for the same gene. When two sources (or two assembly versions from the same source) disagree or add complementary detail about the same canonical node, we keep both contributions attributed, not silently overwrite one — directly reusing the "conflicting claims stay separate" principle, extended to node-level attribute provenance, not just CLAIM nodes.

---

## 2. Persistent layer architecture — what's bulk-preloaded vs. `load()`-driven

This is the answer to "persistent core biology and operational layer ingestion": **Core Biology and Operational are not scope-driven at all — they get bulk-ingested independently of any specific `load()` call, because they're small, stable, and finite. Evidence is the only layer that stays lazy/anchor-driven, because it's the layer that's too large to bulk-preload.**

| Layer | Source(s) | Ingestion mode | Why |
|---|---|---|---|
| **CORE_BIOLOGY** | Ensembl | **Bulk, persistent.** Full gene/transcript/exon/translation structure for the reference genome, ingested once, refreshed on Ensembl release cadence (few times/year). | ~20K genes total — bounded, small, low churn. There's no reason to gate this behind a per-anchor `load()` call; it's cheaper to have it all resident than to keep re-resolving it. |
| **OPERATIONAL** | ClinGen (cspec) | **Bulk, persistent.** All VCEP specifications for all genes with a published expert panel, refreshed when ClinGen publishes a new spec version. | ~295 gene-specific specifications total (as of the current ClinGen VCEP roster) — trivially bulk-loadable, and rules need to be resident *before* any evidence-scoped `load()` runs, since evidence gets evaluated against them. |
| **EVIDENCE** | ClinVar, gnomAD | **`load()`-driven, lazy, anchor/scope-scoped.** Only pulled for anchors something has actually asked about. | ClinVar alone is ~4.5M variant records / ~6.9M submissions — bulk-materializing all of it as fully-hydrated CLAIM/OBSERVATION nodes contradicts the "compact index + lazy hydration" principle from the original architecture. This is the layer `load(anchor, scope, policy)` actually governs. |
| **ADMINISTRATIVE** | derived from all of the above | **Created as a side-effect of ingestion**, never bulk- or lazy-loaded on its own — a `Submitter` node is created the first time some SCV references it, from any layer's ingestion pass. | Bookkeeping, not a first-class thing to "load." |

Practical consequence: by the time anyone calls `load("rs5030820", scope="variant")`, the Gene/Transcript/Exon backbone for VHL and the ACMG rule set for VHL are **already sitting in the graph** — the load only has to resolve identity against what's resident and pull the ClinVar/gnomAD evidence. This is also why the scope resolver (§4) can assume Core Biology + Operational are always available and never needs a "policy" knob for them — policy only modulates how much *evidence* gets pulled.

---

## 3. Loading hierarchy — atomic → family/system

### 3.1 The lattice, defined concretely for genomics

| Level | Meaning | Genomics example | Resolver needed |
|---|---|---|---|
| `atomic` | One fact, not a full node neighborhood | "the gnomAD genome AF for rs5030820" | none — direct attribute fetch, used mostly for MHLA-time hydration escalation, not initial `load()` |
| `variant` (entity) | One canonical node + its direct structural/evidence footprint | `load("rs5030820", scope="variant")` | identity resolver (rsID/SPDI → canonical id) |
| `gene` (entity) | One canonical node + everything under it | `load("VHL", scope="gene")` | gene resolver (symbol/HGNC → canonical id) |
| `gene_set` | Explicit, caller-specified list, no inferred membership | `load(["VHL","EPAS1","HIF1A"], scope="gene_set")` | none — just batches the entity resolver per member |
| `gene_family` | Inferred membership by **sequence homology** (true paralogs) | VHL's actual paralog group, if any, via HGNC gene-family grouping | HGNC/Ensembl gene-family resolver — **buildable now**, no new source needed |
| `pathway` | Inferred membership by **functional/pathway relationship** | VHL + EPAS1 + HIF1A (hypoxia response pathway) | needs a pathway source (Reactome/KEGG) — **not in the current stack, deferred** |
| `region` | Inferred membership by **genomic coordinate window** | everything in `chr3:10,140,000-10,160,000` | Ensembl `/overlap/region/...` — **buildable now**, Ensembl-only |
| `domain/system` | Broadest — organ system, disease system | e.g. "renal cancer predisposition genes" | no concrete resolver yet, stays conceptual |

This directly resolves the ambiguity flagged earlier: your original `gene_family` example (VHL/EPAS1/HIF1A) is actually a **pathway**-scope query, not a **gene_family**-scope one. Keeping both as distinct, precisely-defined scopes avoids a resolver silently returning "whatever it felt like."

**Build now:** `variant`, `gene`, `gene_set`, `gene_family`, `region` — all resolvable with the current 4-source stack.
**Defer:** `pathway`, `domain/system` — stub the scope name in the API (so callers don't hit a hard error) but wire it to `NotImplementedError` with a clear message rather than silently returning nothing or misusing `gene_family`.

### 3.2 Three independent knobs, not one

`load(anchor, scope, policy, depth)` — worked example to keep these from blurring together, since they answer three different questions:

- **`scope`** — *which* connectors/entities get pulled at all (e.g. `gene` scope pulls Ensembl+ClinVar+gnomAD+ClinGen for VHL; `variant` scope pulls a tight per-variant slice).
- **`depth`** — *how many relational hops from the anchor* the connector planner walks while assembling the ingestion frontier — an **ingestion-time** knob. `load("VHL", scope="gene", depth=1)` pulls VHL's own structure/variants/rules; `depth=2` would also pull, say, one hop out — VHL's paralogs' basic identity, or genes overlapping the same region — *without* fully expanding them.
- **`policy`** — *how much detail* gets materialized for whatever `scope`+`depth` selected, expressed as an LOD/connector-selection preset (§4.4). This is explicitly **not** a fourth independent axis — it's a named bundle over the LOD tiers already defined in connector-research.md §3.

MHLA's later query-time activation/stopping decision (the sufficiency head from the original architecture) is a **fourth, separate concept**, deliberately not named `depth` to avoid the collision — it operates on the *already-compiled* graph, deciding what to activate for a specific investigation, whereas `load()`'s `depth` operates at *ingestion time*, deciding what to materialize at all.

---

## 4. Connector search/orchestration pipeline

```
load(anchor, scope, policy, depth)
        │
        ▼
┌───────────────────┐
│  Scope Resolver     │  1. resolve anchor → canonical id (hits identity xrefs, e.g. symbol→HGNC, rsID→SPDI)
│                     │  2. if scope requires membership inference (gene_family/region/pathway),
│                     │     resolve membership set now
│                     │  3. emit a Resolution Plan: {canonical anchors[], scope, depth, policy}
└─────────┬───────────┘
          ▼
┌───────────────────┐
│ Connector Planner   │  1. check what's already resident (Core Biology / Operational are ~always resident, §2 —
│                     │     skip re-fetching them; Evidence checked against freshness/TTL)
│                     │  2. expand policy → concrete per-source fetch calls + LOD ceiling (§4.4)
│                     │  3. order calls: biology/identity resolution FIRST (needed to scope evidence/rule calls),
│                     │     then evidence + rules in parallel where sources allow batching
│                     │  4. dedupe + batch (reuse the parallelism principles already established:
│                     │     no per-record API calls, respect source rate limits)
└─────────┬───────────┘
          ▼
   Source Adapters (Ensembl / ClinVar / gnomAD / ClinGen / future) — fetch() per connector contract
          ▼
   Normalize → Mapping Spec (connector-research.md §5)
          ▼
   Identity Resolution (§6 of research doc; now also emitting REPRESENTS edges for weak matches,
                         and multi-entry Provenance lists for confirmed merges)
          ▼
   Verification (§5 below)
          ▼
   Dedup + Graph Compiler
          ▼
   Persistent Graph  (Core Biology + Operational already resident; Evidence now updated/added at requested LOD)
```

### 4.2–4.3 Resolver/Planner responsibilities, stated as invariants

- The Scope Resolver **never** calls a source adapter's full `fetch()` — only whatever minimal identity-lookup call is needed to resolve the anchor (e.g. Ensembl symbol lookup, ClinVar ESearch). Heavy fetching is the Connector Planner's job, after the plan is fully known — this keeps identity resolution cheap even for large scopes.
- The Connector Planner is the **only** component allowed to decide "do we already have this" — connectors themselves stay stateless and source-only, per the original connector contract (Phase 2 of the approved plan). This keeps the "resident vs re-fetch" logic in one place instead of scattered across every adapter.

### 4.4 Policy presets, mapped to LOD (not a parallel concept)

| Policy | LOD ceiling | Behavior at `gene` scope (the expensive case) |
|---|---|---|
| `index_only` | LOD0–1 | ClinVar: ESummary only, no EFetch (no SCV-level CLAIMs at all, just the VCV rollup). gnomAD: genome/exome totals, no per-population breakdown. Fastest, cheapest — good for wide scans across many genes. |
| `standard` *(default)* | LOD0–2 | ClinVar: full EFetch (SCV/RCV/VCV hierarchy) **only** for variants with `review_status` ≠ "no assertion" or with conflicting/multiple submitters — i.e., the clinically interesting subset — others stay at ESummary level. gnomAD: totals only. |
| `clinical` | LOD0–2, ClinVar-prioritized | Full EFetch for **every** matching variant regardless of review status (a clinical use case cares about even single-submitter, uncertain-significance variants). Ensembl stays canonical-transcript-only. |
| `evidence_heavy` | LOD0–3 | `clinical`, plus gnomAD per-subpopulation breakdown expanded, plus all Ensembl transcripts (not just canonical) fully expanded, plus UniProt features once that connector exists. |

This directly prevents the anti-pattern flagged earlier: a naive `load("VHL", scope="gene")` does **not** mean "EFetch all ~2,200 ClinVar VHL variants" unless `policy="clinical"` or `"evidence_heavy"` was explicitly requested.

---

## 5. Correctness evaluation v2

Carries forward everything from connector-research.md §7 (type correctness, referential integrity, record conservation) and adds four checks that only exist because of the v2 schema changes in §1.4:

1. **Layer-tag consistency** — every node's `layer` must match the `(role, subtype)` → `layer` registry in §1.2 exactly. A connector cannot set `layer` directly; verification recomputes it from `(role, subtype)` and flags any mismatch as a hard failure (this is what stops a future connector from, say, mislabeling a `Submitter` as `CORE_BIOLOGY` by accident).
2. **Multi-provenance integrity** — for any node with `len(provenance) > 1`, each entry must independently satisfy the existing Provenance-completeness check (source, source_record_id, retrieved_at), and if two entries assert conflicting values for the *same* attribute key (e.g. two different genome assembly coordinates), both must be retained with a distinguishing qualifier (e.g. `assembly=GRCh38` vs `assembly=GRCh37`) — silently picking one is a verification failure, not a merge decision.
3. **REPRESENTS confidence integrity** — every `REPRESENTS` edge must carry a `confidence` value, and downstream identity resolution must never treat a `REPRESENTS` edge as equivalent to a full node merge (i.e., two nodes linked by `REPRESENTS` must still each independently pass referential integrity as distinct nodes — merging them silently would be exactly the false-merge failure mode the architecture already rejects).
4. **Hydration-pointer integrity** — any node with `lod < 4` must have at least one `Provenance` entry with a resolvable `(source, source_record_id)` pair — i.e., no node is allowed to exist at a partial LOD without a concrete pointer something can later hydrate from. This is the specific check that guarantees the graph stays "traversable by an LLM collecting/hydrating payloads": if hydration integrity holds, an LLM (or hydration worker) can always take any node in the compiled graph and resolve "where do I go to get more" without hitting a dead end.

---

## 6. Open questions (resolved in §7)

1. **`gene_family` (HGNC/Ensembl paralogs) resolver** — build now alongside `variant`/`gene`/`region`, or defer until we actually have a query that needs it? VHL specifically may not have a large/useful paralog group, so this scope might sit unused in the VHL vertical slice either way.
2. **Provisional OBSERVATION/CLAIM subtypes** (`SegregationResult`, `FunctionalResult`, `Association`, `ExpertPanelClassification`) — keep them in the registry now as "known but not yet sourced" (my recommendation, so verification doesn't need a schema change the first time one shows up), or strip them until a connector actually emits one?
3. **`policy` defaults** — I set `standard` as the default for `gene` scope specifically to avoid the full-EFetch-everything trap. Does that match your intent, or should `standard` already mean "clinical-grade, fetch everything," with a cheaper `index_only` as the one you'd reach for explicitly when scanning wide?

---

## 7. Review resolution (frozen)

A full architectural review (15 points) was run against this doc before any code was written. Result: **the 5 roles / 11 edges survive unchanged.** Every fix below is a semantics/precision correction, not an ontology expansion. This section is the authoritative summary — full reasoning for each point lived in conversation, not restated here; [`forge-ir-v3-schema.md`](forge-ir-v3-schema.md) is where these become actual types.

| # | Issue found in this doc | Resolution |
|---|---|---|
| 1 | `PART_OF` used for both structural containment (exon-in-transcript) and aggregation (SCV-in-RCV-in-VCV) | `PART_OF` = structural containment only. SCV/RCV/VCV hierarchy uses `RELATES_TO[contributes_to]` / `RELATES_TO[aggregates_into]`. |
| 2 | `SOURCED_FROM → Submitter` conflated "who asserted this" with "where did this record come from" | `RELATES_TO[submitted_by] → Submitter`. `SOURCED_FROM` reserved strictly for source/record provenance pointers. |
| 3 | §2 said persistent Core Biology = "full Ensembl structure," contradicting this doc's own §3 LOD tiering | Persistent Core Biology = compact backbone (all genes, all transcript IDs as stubs, canonical transcript fully expanded, xrefs). Full alternate-transcript structure stays lazy LOD expansion, consistent with §3. |
| 4 | `load(depth=...)` risked becoming a second traversal/reasoning engine alongside MHLA | Renamed to `prefetch_radius`, documented as storage/network optimization only. Defaulted to `0` / not exposed as a tunable until a concrete need appears — MHLA is the only real traversal mechanism in the system. |
| 5 | `scope="gene"` defined as "everything under it" — too vague for the planner | Scope = a deterministic, per-connector membership predicate (e.g. "ClinVar: all assertions whose canonical variant maps to VHL"), never an open-ended "everything." |
| 6 | `standard` policy's conflict-detection read as circular (deciding EFetch based on data only EFetch provides) | Not circular — ClinVar's `review_status` field (confirmed in the real ESummary fixture: `"criteria provided, multiple submitters, no conflicts"`) is controlled vocabulary available at ESummary tier, before any EFetch. Funnel: ESummary `review_status` → EFetch-or-not decision → EFetch confirms/expands. |
| 7 | `RULE --REQUIRES--> ObservationType` (a bare subtype label) discarded the actual constraint data (e.g. PM2's real numeric AF threshold) | New RULE subtype `Requirement` carries `constraints: dict`. `ACMGCriterion --REQUIRES--> Requirement`; `Observation --SUPPORTS--> Requirement`. No new edges needed. |
| 8 | Risk that `layer` gets read as a trust/quality signal | Documented explicitly: `layer` is subsystem/routing classification only. Trust/confidence lives in provenance, `review_status`, and `REPRESENTS.confidence` — never in `layer`. |
| 9 | `REPRESENTS.confidence` alone isn't reproducible/auditable | `REPRESENTS` edges require `confidence` + `match_method` + `resolver_version`. |
| 10–11 | Hydration-integrity check required a source pointer on every LOD<4 node, breaking for DERIVED/RUNTIME-origin nodes | Provenance and hydration checks branch by `origin` (SOURCE/DERIVED/RUNTIME), each with distinct required fields (see v3 §3). |
| 12 | `INVESTIGATION` read as part of the persistent ontology despite being runtime-only | Added `storage_scope: PERSISTENT \| RUNTIME_ONLY` as a first-class registry field on roles and edges. `INVESTIGATION` and `ACTIVATED_BY` are `RUNTIME_ONLY`. |
| 13 | `gene_family` scope claimed "buildable now" without actually verifying Ensembl's homology API against real data (unlike every other claim in this doc) | Downgraded to ⚠️ unverified/experimental until real-payload-checked, matching this doc's own verification standard. |
| 14–15 | No concrete mechanism for *how* MHLA decides what to activate without hardcoding domain logic or leaning on `layer` as a relevance signal | **Structural addition:** `RequirementState` (an `INVESTIGATION`-role, `RUNTIME_ONLY` node) tracks per-investigation satisfaction of a `Requirement`. MHLA activation is driven by unsatisfied requirements, not layer classification. This becomes the organizing loop for the whole retrieval pipeline — see v3. |

**Two follow-up questions this review surfaced, also resolved:**
- *How do live-ingested (Evidence) nodes connect to the persistent layer?* Through identity resolution against the persistent backbone's canonical IDs (SPDI/HGNC/MedGen) as the join key — not a special edge type. Added a **connectivity integrity** check: every `EVIDENCE`-layer node must have a path back to a `CORE_BIOLOGY` node, or it's orphaned and fails verification. Gap-fill case: if `load()`'s anchor isn't yet resident in Core Biology, the Scope Resolver triggers a targeted single-anchor biology ingestion first, which gets written into the *persistent* layer, not treated as evidence.
- *How is staleness/update handled?* Persistent layers (Core Biology + Operational) use scheduled full-resync/diff (cheap — both are small and finite); `RULE` nodes are versioned explicitly (`RELATES_TO[superseded_by]` on spec version bumps, e.g. ClinGen 1.0→1.1) rather than overwritten. Evidence-layer nodes carry a `staleness_threshold` checked against `Provenance.retrieved_at`, both on re-`load()` and right before a `RequirementState` marks itself satisfied off that node. A changed classification value (not just a metadata edit) is preserved via `RELATES_TO[superseded_by]` rather than overwritten, so "why did this change" stays answerable.
