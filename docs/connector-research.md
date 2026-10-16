# Forge Connector Research — APIs, Payloads, and Node/Edge Mapping

**Status: research/design only — no pipeline code written yet.**
**Test case used throughout:** VHL, ClinVar VCV000002218.44, `NM_000551.4:c.499C>T` / `p.Arg167Trp`, GRCh38 `3:10149822 C>T`, rs5030820, ClinGen CAID `CA020450`, UniProt `P40337`.

All payloads referenced below are real, live-fetched responses saved under [`fixtures/`](../fixtures) — not samples from documentation. File paths are given per section.

---

## 1. Frozen ontology (recap, for reference while reading the mapping tables)

**Node roles:** `ENTITY`, `OBSERVATION`, `CLAIM`, `RULE`, `INVESTIGATION`
**Edge predicates:** `RELATES_TO`, `PART_OF`, `REPRESENTS`, `ASSERTS`, `OBSERVES`, `SUPPORTS`, `CONTRADICTS`, `SOURCED_FROM`, `REQUIRES`, `APPLIES_TO`, `ACTIVATED_BY`

---

## 2. Per-source API surface

### 2.1 Ensembl (Core Biology — structure/annotation, not clinical truth)

| Call | Endpoint | Used for |
|---|---|---|
| Gene lookup | `GET /lookup/symbol/homo_sapiens/VHL?expand=1` | Gene → Transcript → Exon → Translation structure |
| Variation lookup | `GET /variation/human/rs5030820` | rsID identity, cross-refs, genomic mapping |
| VEP (consequence) | `GET /vep/human/hgvs/NM_000551.4:c.499C>T` | Per-transcript consequence, SIFT/PolyPhen, HGVS |

Fixtures: `ensembl_gene_VHL.json`, `ensembl_variation_rs5030820.json`, `ensembl_vep_c499CT.json`.

**Real fields observed (gene lookup):** `id` (`ENSG00000134086`), `display_name`, `biotype`, `seq_region_name`, `start`/`end`, `strand`, `description` (carries HGNC xref: `"...Acc:HGNC:12687"`), nested `Transcript[]` each with `id` (`ENST00000256474`), `display_name`, `is_canonical`, `biotype`, nested `Exon[]` (`id`, `start`, `end`), and `Translation` (`id` = `ENSP00000256474`, `start`/`end` in AA coords).

**Real fields observed (variation lookup):** `name` (rsID), `synonyms` — a flat list that actually contains **VCV/RCV accessions** (`"VCV000002218"`, `"RCV000466046"`, …) and OMIM allelic variant IDs (`"608537.0003"`) — this is a cross-reference goldmine for identity resolution. Also `mappings[]` (chrom/pos/allele_string/assembly), `most_severe_consequence`, `clinical_significance` (mirrors ClinVar — **do not treat as authoritative**, see §5 trap), `var_class`, `evidence[]`.

**Real fields observed (VEP):** `transcript_consequences[]`, each with `transcript_id`, `gene_id`, `consequence_terms[]`, `impact`, `hgvsc`, `hgvsp`, `amino_acids` (`R/W`), `protein_start`/`protein_end`, `codons`, `sift_score`/`sift_prediction`, `polyphen_score`/`polyphen_prediction`. One VHL variant maps to **9 different transcripts** with different consequences (canonical transcript ENST00000256474 gives `p.Arg167Trp`; a non-canonical transcript gives `p.Arg126Trp` for the *same* genomic change) — this is a real transcript-ambiguity case, not hypothetical.

---

### 2.2 ClinVar (Evidence — clinical assertions)

| Call | Endpoint | Used for |
|---|---|---|
| ESearch | `GET eutils/esearch.fcgi?db=clinvar&term=VCV000002218` | Resolve accession → internal UID |
| ESummary | `GET eutils/esummary.fcgi?db=clinvar&id=2218` | Fast VCV-level index/metadata |
| EFetch | `GET eutils/efetch.fcgi?db=clinvar&id=2218&rettype=vcv` | Full VCV→RCV→SCV nested record (this is where CLAIM/OBSERVATION actually come from) |

Fixtures: `clinvar_esummary_VCV2218.json`, `clinvar_efetch_VCV2218.xml` (151KB, 22 submissions).

**ESummary** gives a fast but *flattened* view: one top-level `germline_classification` (`"Pathogenic/Likely pathogenic"`, review_status, `trait_set[]` covering all conditions at once), `variation_xrefs` (ClinGen CAID, UniProtKB, OMIM, dbSNP — used for identity resolution), `allele_freq_set` (ExAC/gnomAD, low-fidelity), and `supporting_submissions.rcv[]` / `.scv[]` (just accession lists). **Good for the index/hydration-trigger layer; not granular enough for real CLAIM generation** — it collapses per-condition nuance.

**EFetch XML** gives the real hierarchy, confirmed by inspecting the raw structure:
```
VariationArchive (VCV000002218, VariationID=2218)
 └─ ClassifiedRecord
     ├─ SimpleAllele (CanonicalSPDI, GeneList → Gene[Symbol=VHL, HGNC_ID])
     ├─ RCVList
     │   └─ RCVAccession (RCV000002302, Accession, Version)
     │       ├─ ClassifiedConditionList → ClassifiedCondition (DB=MedGen, ID=C0019562, "Von Hippel-Lindau syndrome")
     │       └─ RCVClassifications
     │           ├─ GermlineClassification (ReviewStatus, Description="Pathogenic", SubmissionCount, DateLastEvaluated)
     │           └─ SomaticClinicalImpact (present on RCV000002303 only — Tier I - Strong; a DISTINCT classification axis, not a duplicate)
     └─ ClinicalAssertion×22 (SCV, e.g. SCV001473258)
         ├─ ClinVarAccession (Accession, SubmitterName, OrgID, OrganizationCategory)
         ├─ Classification (ReviewStatus, GermlineClassification="Pathogenic", Comment=<full free-text rationale, arrives inline — no separate hydration call needed>)
         ├─ ContributesToAggregateClassification="true"  ← explicit signal for the PART_OF edge to RCV
         ├─ ObservedInList → ObservedIn (Sample/Origin=germline, Method/MethodType=clinical testing, ObservedData)
         ├─ TraitSet → Trait → XRef(DB=MedGen, ID=CN235283)   ← this submission's own condition, not always identical to RCV's canonical grouping
         └─ AttributeSet → Citation → URL (assertion method document)
```
Confirmed: **VCV, RCV, and SCV are three distinct real structures with different fields** — the plan's "don't collapse them" commitment is directly validated by this payload, not just theoretical.

Distinct `Attribute Type` values seen across the 22 SCVs (controlled vocabulary worth knowing): `AssertionMethod`, `Description`, `GARD id`, `HGVS`, `ModeOfInheritance`, `SingleHeterozygote`, `VariantAlleles`, `VariantChromosomes`, `disease mechanism`, `keyword`, `public definition`.

---

### 2.3 gnomAD (Evidence — population frequency)

Single call: GraphQL `POST https://gnomad.broadinstitute.org/api`, `variant(variantId: "3-10149822-C-T", dataset: gnomad_r4)`.

Fixture: `gnomad_variant_3-10149822-C-T.json`.

Real result: `genome{ac:1, an:152140, af:0.00000657…, populations[30 entries]}`, `exome{ac:0, an:1461858, af:0, populations[26 entries]}`, `rsids:["rs5030820"]`. The `populations[]` breakdown is granular (per-ancestry × per-sex, ~30 rows) — too granular for one node each without exploding the graph (see §4 LOD decision).

---

### 2.4 ClinGen (Operational — the RULE layer)

Two genuinely different endpoints that serve two different purposes:

**(a) `cspec.genome.network` — the actual VCEP rule specification document.**
`GET https://cspec.genome.network/cspec/SequenceVariantInterpretation/id/1527855755` returns embedded JSON: `title: "ClinGen VHL Expert Panel Specifications to the ACMG/AMP Variant Interpretation Guidelines for VHL Version 1.1"`, `entId: "GN078"`, and a `CriteriaCode[]` array — **one entry per ACMG criterion** (PM2, PVS1, BS4, PS4, BP4, BP7, …), each with real, usable fields:

```
label: "PM2"
evidenceCategory: "Population Data"
baseStrength: "Pathogenic"
defaultStrength: "Pathogenic Moderate"
originalACMGSummary: "Absent from controls... in Exome Sequencing Project, 1000 Genomes or ExAC."
instructionsToUse: "Follow all SVI general guidance on applying population filters."
strengthDescriptor: [
  { strength: "Supporting", applicability: "Applicable",
    text: "PM2_Supporting can be applied for variants either absent from gnomAD or with <= 0.00000156
           (0.000156%) GroupMax Filtering Allele Frequency in gnomAD (based on gnomAD v4 release)." }
  ...
]
```
This is a **machine-checkable numeric threshold**, not just prose — confirmed real, not assumed. PVS1's entry, by contrast, is a long free-text decision tree (domain boundaries, NMD rules, exon-specific caveats) with no single numeric threshold — confirmed criteria genuinely differ in how mechanical vs. narrative they are, which the RULE node schema needs to tolerate (see §4).

Fixture: `clingen_vhl_vcep_GN078_v1.1.json` (34 criteria entries total for VHL v1.1).

**(b) `erepo.clinicalgenome.org` — individually curated per-variant classifications.**
`GET https://erepo.clinicalgenome.org/evrepo/api/classifications?gene=VHL` returns ClinGen's own worked examples: a variant, the criteria codes that were `"status":"Met"` for it (e.g. `PM2_Supporting`, `PS4_Moderate`, `PVS1`), and the resulting outcome (`"Pathogenic"`).

**Important real finding:** our exact test variant (VariationID 2218 / CAID CA020450) is **not** among ClinGen's ~25 individually-curated VHL variants — confirmed by querying the full listing and grep-matching for both identifiers, zero hits. This makes sense (it's such a well-established pathogenic variant it doesn't need expert re-review) but it means **the E2E test will only exercise the RULE-generation path from source (a), not the CLAIM-generation path from source (b)** — flagged now so it isn't mistaken for a pipeline bug later.

Fixture: `clingen_erepo_vhl_curated_examples.json`.

---

## 3. What's compact metadata (resident in graph) vs. deferred to hydration

Per your instruction — we don't need fully-flushed records until the hydrate step. Here's the actual tiering, based on what each API call above already returns "for free" vs. what would need a separate deeper fetch:

| Tier | Contents | Source examples |
|---|---|---|
| **Resident (LOD0/1)** — always in graph | IDs, symbols, accessions, coordinates, cross-refs, classification labels, review status, criterion labels/strengths, af/ac/an numbers | Ensembl gene/transcript/exon IDs; ClinVar accessions + `GermlineClassification` description; gnomAD `ac/an/af`; ClinGen `label`/`baseStrength` |
| **Resident but larger (LOD2)** | Free-text rationale that arrives *in the same call* — no extra round-trip needed | ClinVar SCV `Comment` (can be a full paragraph with inline citations); ClinGen `originalACMGSummary` / `instructionsToUse` |
| **Deferred (LOD3/4)** — fetched only on hydrate | Full external documents; the long tail of non-canonical transcripts' exon/consequence detail; raw source XML fragment; full PubMed text | PubMed full text behind ClinVar citations; ClinGen's PVS1 decision-tree PDF (linked, not embedded); Ensembl transcripts beyond the canonical one — kept as lightweight ENTITY stubs (id + biotype + is_canonical flag) until specifically expanded |

Concretely: the VHL gene lookup returned **9 transcripts**; only `ENST00000256474` (canonical) gets full Exon/Translation structure materialized at LOD1 for this test — the other 8 become stub ENTITY nodes, upgraded to full structure only if something (a rule, a query) actually needs them.

---

## 4. Canonical IR shape (dataclasses — described here, not yet implemented)

```
ForgeNode:
  id: str                     # canonical ID (see §5)
  role: NodeRole               # ENTITY | OBSERVATION | CLAIM | RULE | INVESTIGATION
  subtype: str                 # e.g. "Variant", "Gene", "ClinicalAssertion", "PopulationFrequency", "ACMGCriterion"
  attributes: dict             # subtype-specific fields, e.g. {af: 0.0000066, review_status: "..."}
  provenance: Provenance
  lod: int                     # 0-4, per §3

ForgeEdge:
  src: str                     # node id
  predicate: EdgeType           # one of the 11 frozen predicates
  dst: str                      # node id
  qualifier: str | None         # e.g. predicate="RELATES_TO", qualifier="encodes" / "for_condition" / "in_gene"
  attributes: dict              # e.g. VEP consequence detail on a has_consequence edge
  provenance: Provenance

Provenance:
  source: str                   # "ClinVar" | "Ensembl" | "gnomAD" | "ClinGen"
  source_record_id: str         # e.g. "SCV001473258", "ENST00000256474"
  source_version: str           # e.g. ClinVar release date, cspec "1.1"
  retrieved_at: datetime
  origin: str                   # SOURCE | DERIVED | RUNTIME
```

`qualifier` is how we get ClinVar's/Ensembl's specific relationships (`encodes`, `in_gene`, `has_consequence`, `for_condition`) out of the 11 frozen predicates without inventing new edge types — matches the "detailed semantics live in predicates/attributes" rule from the frozen ontology.

---

## 5. Node/edge mapping table (the core deliverable — derived from the real payloads above)

### Ensembl → IR

| Source structure | Forge output |
|---|---|
| `Gene{id, display_name, biotype, ...}` | `ENTITY:Gene`, canonical id keyed on HGNC (`HGNC:12687`) |
| `Transcript{id, display_name, is_canonical, biotype}` | `ENTITY:Transcript` — `PART_OF` → `Gene` |
| `Exon{id, start, end}` | `ENTITY:Exon` — `PART_OF` → `Transcript` |
| `Translation{id, start, end}` | `ENTITY:Protein` — edge `Transcript` `RELATES_TO[encodes]` → `Protein` |
| VEP `transcript_consequences[]` | edge `Variant` `RELATES_TO[has_consequence]` → `Transcript`, attrs = `{consequence_terms, impact, hgvsc, hgvsp, protein_position, amino_acids}` |
| VEP `sift_score/prediction` | `OBSERVATION:ComputationalPrediction` (tool=SIFT) — `OBSERVES` → `Variant` |
| VEP `polyphen_score/prediction` | `OBSERVATION:ComputationalPrediction` (tool=PolyPhen2) — `OBSERVES` → `Variant` |
| `clinical_significance` (variation lookup) | **dropped** — mirrors ClinVar; materializing it would double-count evidence that ClinVar already asserts authoritatively (flagged trap, see below) |

### ClinVar → IR

| Source structure | Forge output |
|---|---|
| `VariationArchive` / `CanonicalSPDI` | resolves/confirms the `Variant` `ENTITY` (identity, not a new node); VCV accession stored as a cross-ref attribute |
| `RCVAccession.ClassifiedCondition` (MedGen) | `ENTITY:Disease`, canonical id = `MedGen:C0019562` |
| `RCVAccession.RCVClassifications.GermlineClassification` | `CLAIM:AggregateClassification` (scope=`condition`) — `ASSERTS`→`Variant`, `RELATES_TO[for_condition]`→`Disease`, `SOURCED_FROM`→ClinVar source manifest |
| `RCVAccession.RCVClassifications.SomaticClinicalImpact` (when present) | **separate** `CLAIM:ClinicalImpactClassification` node — not merged into the germline one, since it's a distinct classification axis |
| ESummary top-level `germline_classification` (VCV-wide) | `CLAIM:AggregateClassification` (scope=`variant`) — one level up from the per-condition RCV claims |
| `ClinicalAssertion` (SCV) | `CLAIM:ClinicalAssertion` — `PART_OF` → the matching RCV-scope `AggregateClassification` (using the record's own `ContributesToAggregateClassification` flag) |
| `ClinVarAccession{SubmitterName, OrgID, OrganizationCategory}` | `ENTITY:Submitter`, canonical id = OrgID — SCV claim `SOURCED_FROM` → `Submitter` |
| `ObservedInList.ObservedIn` | `OBSERVATION:CaseObservation` — `OBSERVES`→`Variant`, `SUPPORTS`→ the SCV `CLAIM` |
| SCV `TraitSet.Trait.XRef` (MedGen) | `RELATES_TO[for_condition]` edge from SCV `CLAIM` → `Disease` (may differ slightly from the RCV's own condition grouping — kept as-is, not forced to match) |

This gives the full three-level hierarchy without collapsing: `SCV --PART_OF--> RCV-aggregate --PART_OF--> VCV-aggregate`, all as distinct `CLAIM` nodes distinguished by `subtype`+`scope`.

**Not yet observed but must be handled:** two SCVs under the same RCV disagreeing in `GermlineClassification` — this variant's review status is "no conflicts" so it didn't appear in our sample. When it happens, both SCV `CLAIM`s get an explicit `CONTRADICTS` edge to each other, in addition to both being `PART_OF` the same RCV aggregate.

### gnomAD → IR

| Source structure | Forge output |
|---|---|
| `genome{ac, an, af}` | one `OBSERVATION:PopulationFrequency` node (scope=`genome`) — `OBSERVES` → `Variant` |
| `exome{ac, an, af}` | one `OBSERVATION:PopulationFrequency` node (scope=`exome`) — `OBSERVES` → `Variant` |
| `populations[]` (~30 per-ancestry rows) | **not** exploded into per-population nodes — stored as a nested attribute list on the scope-level `OBSERVATION` node (avoids node-count explosion; still queryable) |

### ClinGen → IR

| Source structure | Forge output |
|---|---|
| `CriteriaCode` entry (e.g. `PM2`) | `RULE:ACMGCriterion` — `APPLIES_TO` → `Gene` (VHL), canonical id = `RULE:ClinGen:GN078:PM2` (versioned by cspec `1.1`) |
| `evidenceCategory` (e.g. `"Population Data"`) | drives a `REQUIRES` edge from the `RULE` to the *type* of Observation needed — controlled vocabulary: `Population Data`→`PopulationFrequency`, `Computational And Predictive Data`→`ComputationalPrediction`, `Segregation Data`→`SegregationResult` (more categories will surface as we ingest more criteria/sources — table grows, doesn't get redesigned) |
| `strengthDescriptor[]` | stored as a structured attribute list on the `RULE` node (applicability/strength/text per strength level) — not separate nodes, since these are strength *variants of the same rule*, not new rules |
| erepo `evidenceCodes[{label, status:"Met"}]` (when a curated example exists) | `CLAIM:ExpertPanelClassification` — the specific `RULE`s that were "Met" get a `SUPPORTS` edge → this `CLAIM`; `CLAIM` `ASSERTS` → `Variant` *(this path does not fire for our exact test variant — see §2.4)* |

---

## 6. Identity resolution — where cross-source merges actually happen

Confirmed merge points from real data (not hypothetical):

- **Variant**: Ensembl's `mappings[].seq_region_name/start/allele_string` and ClinVar's `CanonicalSPDI` both resolve to `NC_000003.12:10149821:C:T` — same SPDI, one canonical `Variant` node. gnomAD's `variant_id: "3-10149822-C-T"` (1-based) reconciles to the same SPDI (0-based) with a +1 offset — **a real off-by-one convention difference between sources that identity resolution must normalize**, not a hypothetical edge case.
- **Gene**: Ensembl's gene `id`/description carries `HGNC:12687`; ClinVar's `GeneList/Gene[Symbol=VHL]` carries `HGNC_ID="HGNC:12687"` directly; ClinGen's criteria carry `"gene": ["VHL"]` (symbol only, needs a symbol→HGNC lookup step). All three converge on one `Gene` node keyed by HGNC ID.
- **Disease/Condition**: ClinVar's RCV and SCV both cite MedGen IDs directly (`C0019562`, `CN235283`) — no separate MedGen API call is even needed for this test case since ClinVar already embeds the MedGen identifier; MedGen connector becomes useful when a source gives only a free-text condition name with no ID.
- **Protein**: ClinVar's `variation_xrefs` includes `UniProtKB: P40337#VAR_005762` directly — confirms the UniProt merge key is already available from ClinVar's own payload, without needing a separate UniProt ID-mapping round-trip for this variant.

---

## 7. Structural correctness — what gets checked, and how it's measured

Per your scope request (light, but must guarantee everything lands in the right node / connects with the right edge / to the right target):

1. **Type correctness** — every node's `role` ∈ the frozen 5, `subtype` ∈ a known registry per role (e.g. `CLAIM` subtypes: `ClinicalAssertion`, `AggregateClassification`, `ClinicalImpactClassification`, `ExpertPanelClassification`). Every edge's `predicate` ∈ the frozen 11, and its `(src_role, predicate, dst_role[, qualifier])` triple must appear in an `ALLOWED_EDGE_COMBINATIONS` registry — built directly from the mapping tables in §5, e.g.:
   - `(ENTITY:Transcript, PART_OF, ENTITY:Gene)`
   - `(ENTITY:Variant, RELATES_TO[has_consequence], ENTITY:Transcript)`
   - `(CLAIM:ClinicalAssertion, PART_OF, CLAIM:AggregateClassification)`
   - `(CLAIM:*, ASSERTS, ENTITY:Variant)`
   - `(RULE:ACMGCriterion, APPLIES_TO, ENTITY:Gene)`
   - `(RULE:ACMGCriterion, REQUIRES, ObservationTypeRef)`
   - `(OBSERVATION:*, OBSERVES, ENTITY:Variant)`
   - `(OBSERVATION:CaseObservation, SUPPORTS, CLAIM:ClinicalAssertion)`
   - `(CLAIM:ClinicalAssertion, SOURCED_FROM, ENTITY:Submitter)`

   This registry is a direct byproduct of §5, not a separately-invented spec — any edge the connectors emit that isn't in this table fails verification immediately.

2. **Referential integrity** — every edge's `src`/`dst` id must resolve to a node actually present in the same ingestion batch (or already in the merged graph). Since our real ClinVar record has 22 SCVs, 7 RCVs, and 1 VCV-level rollup, this check alone would have caught things like a stray `SCV` claim whose `PART_OF` target RCV wasn't emitted.

3. **Record conservation** — per connector run: `records_in_source == nodes/edges_emitted + rejected + quarantined`, nothing silently dropped. E.g. for ClinVar this means: 22 `ClinicalAssertion` elements in → 22 `CLAIM:ClinicalAssertion` nodes out (or an accounted-for rejection), not "somewhere between 15 and 22."

No semantic/domain-rule checking in this pass (e.g., not verifying "PM2's threshold was correctly evaluated against this variant's actual af") — that's an MHLA/query-time concern, not an ingestion-time one.

---

## 8. Open questions for you before implementation starts

1. **VCV-level rollup CLAIM** (§5, "one level up from RCV"): worth materializing as its own node, or is RCV-level granularity sufficient and the VCV-level description just becomes an attribute/cache on the `Variant` entity? I lean toward materializing it (keeps the PART_OF chain complete and mirrors ClinVar's own model exactly), but it's an extra node type per variant.
2. **ClinGen `evidenceCategory` → Observation-subtype table** (§5): I mapped the 3 categories we actually saw (Population Data, Computational And Predictive Data, Segregation Data). VHL's spec has 34 criteria total — do you want me to enumerate the rest now (from the fixture we already have) before writing code, or handle unmapped categories as "quarantine + flag" when we hit them during ingestion?
3. Confirm the **Ensembl `clinical_significance` field is dropped**, not stored even as a non-authoritative attribute — my recommendation, but flagging since it's a place we're intentionally discarding data Ensembl offers.
