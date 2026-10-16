# Forge Connector Research & IR Schema Mapping: The 6 Stub Connectors

**Date**: August 19, 2026  
**Status**: Research & Proposed IR Specification (Frozen Schema Compliant)  
**Target Connectors**: `omim`, `clingen_gene_disease`, `cosmic`, `dbnsfp`, `mondo`, `pharmgkb`  
**Governing Documents**: [`docs/forge-ir-v3-schema.md`](forge-ir-v3-schema.md), [`docs/forge-pipeline-v1.md`](forge-pipeline-v1.md)

---

## 1. Overview & Architectural Role Matrix

These 6 connectors expand the Forge knowledge graph from variant classification into disease ontologies, somatic oncology, ensemble in-silico predictions, gene-disease validity, and pharmacogenomics:

```
                  ┌───────────────────────────────────────────────────────────┐
                  │                      CORE BIOLOGY                         │
                  │   Ensembl Structure (Gene/Tx/Exon/Protein) + UniProt     │
                  └───────────────▲───────────────────────────▲───────────────┘
                                  │                           │
          ┌───────────────────────┴──────────────┐   ┌────────┴──────────────────────┐
          │         OPERATIONAL / RULES          │   │         EVIDENCE LAYER        │
          │  ClinGen VCEP (ACMGCriterion/Req)    │   │  ClinVar (VCV/RCV/SCV)        │
          │  [STUB] ClinGen Gene-Disease Valid.  │   │  gnomAD (PopFreq) + dbSNP     │
          │  [STUB] MONDO (Disease Ontology)     │   │  Ensembl VEP (Consequence)    │
          │  [STUB] OMIM (Phenotype / Alleles)   │   │  [STUB] dbNSFP (In Silico)    │
          └──────────────────────────────────────┘   │  [STUB] COSMIC (Somatic)      │
                                                     │  [STUB] PharmGKB (Drug/Dosing)│
                                                     └───────────────────────────────┘
```

| Connector | Layer | Scope | Primary Node Role | Canonical ID Prefix | Public API Status |
| :--- | :---: | :---: | :---: | :---: | :--- |
| `omim` | Operational / Evidence | Gene / Variant | `ENTITY:Disease`, `CLAIM:AllelicVariantAssertion` | `MIM:` | API Key required / TSV dumps |
| `clingen_gene_disease` | Operational | Gene | `CLAIM:GeneDiseaseValidity` | `ClinGenGD:` | Free Public REST / JSON dump |
| `cosmic` | Evidence | Variant | `OBSERVATION:CaseObservation` (somatic) | `COSMIC:` | License required / Academic TSV |
| `dbnsfp` | Evidence | Variant | `OBSERVATION:ComputationalPrediction` | `dbNSFP:` | Free REST via MyVariant.info |
| `mondo` | Operational | Disease | `ENTITY:Disease` | `MONDO:` | Free Public OLS / Monarch REST |
| `pharmgkb` | Evidence | Variant / Gene | `ENTITY:Drug`, `CLAIM:PharmacogenomicAssertion` | `PharmGKB:` | Free Public REST / TSV dumps |

---

## 2. Deep Dive by Connector

---

### 2.1 OMIM (`omim`) — Online Mendelian Inheritance in Man

#### 2.1.1 Purpose & Domain
OMIM provides curated relationships between human genes and Mendelian phenotypes, clinical synopses, and numbered allelic variants.

#### 2.1.2 API & Data Sources
- **Live Endpoint**: `https://api.omim.org/api/entry?mimNumber={mim_number}&include=all&format=json` (Requires `apiKey`).
- **Free/Open Alternative**:
  - ClinVar ESummary already embeds OMIM identifiers in `trait_xrefs` (`db_source="OMIM", db_id="193300"`).
  - dbSNP `refsnp` payloads embed OMIM allelic variant strings (`608537.0003`).
  - Curated TSV: `mim2gene.txt` (Gene Symbol $\leftrightarrow$ MIM ID mapping) and `genemap2.txt`.

#### 2.1.3 Real Payload Structure (JSON)
```json
{
  "omim": {
    "entryList": [
      {
        "entry": {
          "mimNumber": 193300,
          "status": "live",
          "titles": {
            "preferredTitle": "VON HIPPEL-LINDAU SYNDROME; VHL",
            "alternativeTitles": "VHL SYNDROME;;RETINOCEREBELLAR ANGIOMATOSIS"
          },
          "phenotypeMapList": [
            {
              "phenotypeMap": {
                "phenotypeMimNumber": 193300,
                "phenotype": "Von Hippel-Lindau syndrome",
                "phenotypeMappingKey": 3,
                "phenotypeInheritance": "Autosomal dominant"
              }
            }
          ],
          "geneMap": {
            "chromosome": "3",
            "chromosomeLocation": "p25.3",
            "geneSymbols": "VHL, HRCA1"
          },
          "allelicVariantList": [
            {
              "allelicVariant": {
                "number": "0003",
                "name": "VHL, ARG167TRP",
                "mutations": "c.499C-T, R167W",
                "clinvarAccessions": "VCV000002218",
                "text": "In a patient with von Hippel-Lindau syndrome, Brauch et al. (1995) identified a C-to-T transition..."
              }
            }
          ]
        }
      }
    ]
  }
}
```

#### 2.1.4 IR Node & Edge Mapping
1. **Nodes**:
   - `ENTITY:Disease` $\rightarrow$ ID: `MIM:193300`, `attributes={"title": "VON HIPPEL-LINDAU SYNDROME", "inheritance": "Autosomal dominant"}`.
   - `CLAIM:AllelicVariantAssertion` $\rightarrow$ ID: `MIM:608537.0003`, `attributes={"mutation": "R167W", "phenotype": "Von Hippel-Lindau syndrome"}`.
2. **Edges**:
   - `(ENTITY:Gene, RELATES_TO[causes], ENTITY:Disease)`
   - `(CLAIM:AllelicVariantAssertion, ASSERTS, ENTITY:Variant)`
   - `(CLAIM:AllelicVariantAssertion, RELATES_TO[associated_with], ENTITY:Disease)`
   - `(CLAIM:AllelicVariantAssertion, SOURCED_FROM, ENTITY:SourceManifest)`
   - `(ENTITY:Disease, REPRESENTS, ENTITY:Disease)` (linking `MIM:193300` $\leftrightarrow$ `MedGen:C0019562` with `ConfidenceFields`).

---

### 2.2 ClinGen Gene-Disease Clinical Validity (`clingen_gene_disease`)

#### 2.2.1 Purpose & Domain
ClinGen Gene-Disease Validity curations evaluate the strength of evidence supporting a gene's role in a disease (Definitive, Strong, Moderate, Limited, Disputed, Refuted).

#### 2.2.2 API & Data Sources
- **Live Endpoint**: `https://erepo.clinicalgenome.org/evrepo/api/gene-validity/{gene_symbol}` (Open Access, unauthenticated JSON).
- **Bulk Dump**: `https://search.clinicalgenome.org/kb/gene-validity/download` (Daily updated JSON / CSV).

#### 2.2.3 Real Payload Structure (JSON)
```json
{
  "curations": [
    {
      "report_id": "CGGVD:assertion_10821",
      "gene": {
        "symbol": "VHL",
        "hgnc_id": "HGNC:12687",
        "entrez_id": "7428"
      },
      "disease": {
        "id": "MONDO:0008667",
        "label": "Von Hippel-Lindau syndrome",
        "medgen_id": "C0019562"
      },
      "classification": {
        "name": "Definitive",
        "sop": "SOP8",
        "date": "2020-09-17T00:00:00Z"
      },
      "moi": "Autosomal dominant inheritance (HP:0000006)",
      "panel": "Von Hippel-Lindau Syndrome Variant Curation Expert Panel"
    }
  ]
}
```

#### 2.2.4 IR Node & Edge Mapping
1. **Nodes**:
   - `CLAIM:GeneDiseaseValidity` $\rightarrow$ ID: `ClinGenGD:CGGVD_10821`
   - Attributes: `classification="Definitive"`, `moi="Autosomal dominant"`, `sop="SOP8"`, `panel="VHL VCEP"`.
2. **Edges**:
   - `(CLAIM:GeneDiseaseValidity, ASSERTS, ENTITY:Gene)`
   - `(CLAIM:GeneDiseaseValidity, RELATES_TO[validates_association], ENTITY:Disease)`
   - `(CLAIM:GeneDiseaseValidity, SOURCED_FROM, ENTITY:SourceManifest)`
3. **Ontological Importance**:
   - Acts as an upstream prerequisite (`REQUIRES`) for variant-level ACMG evaluation.

---

### 2.3 COSMIC (`cosmic`) — Catalogue Of Somatic Mutations In Cancer

#### 2.3.1 Purpose & Domain
COSMIC tracks somatic mutation frequencies, tissue distributions, sample counts, and somatic pathogenicity predictions in human malignancies.

#### 2.3.2 API & Data Sources
- **Live API**: Requires institutional / academic login key.
- **Offline / Tabix Option**: `CosmicMutantExport.tsv.gz` and `CancerGeneCensus.tsv`.
- **MyVariant.info Fallback**: Free REST query `https://api.myvariant.info/v1/variant/{spdi}?fields=cosmic`.

#### 2.3.3 Real Payload Structure (JSON)
```json
{
  "cosmic": {
    "cosmic_id": "COSV52839211",
    "legacy_id": "COSM123456",
    "gene_symbol": "VHL",
    "hgvsc": "c.499C>T",
    "hgvsp": "p.Arg167Trp",
    "primary_site": "kidney",
    "site_subtype": "clear_cell_renal_cell_carcinoma",
    "histology": "carcinoma",
    "sample_count": 58,
    "fathmm_prediction": "Pathogenic",
    "fathmm_score": 0.99
  }
}
```

#### 2.3.4 IR Node & Edge Mapping
1. **Nodes**:
   - `OBSERVATION:CaseObservation` (with `attributes={"origin": "somatic", "sample_count": 58, "site": "kidney"}`)
   - Canonical ID: `COSMIC:COSV52839211`
   - `CLAIM:ClinicalImpactClassification` (Somatic pathogenicity tier, e.g. Tier I / Strong)
2. **Edges**:
   - `(OBSERVATION:CaseObservation, OBSERVES, ENTITY:Variant)`
   - `(OBSERVATION:CaseObservation, RELATES_TO[observed_in_tissue], ENTITY:Disease)`
   - `(CLAIM:ClinicalImpactClassification, ASSERTS, ENTITY:Variant)`
   - `(OBSERVATION:CaseObservation, SUPPORTS, CLAIM:ClinicalImpactClassification)`
   - `(CLAIM:ClinicalImpactClassification, SOURCED_FROM, ENTITY:SourceManifest)`

---

### 2.4 dbNSFP (`dbnsfp`) — In Silico Ensemble Predictions

#### 2.4.1 Purpose & Domain
dbNSFP compiles scores and predictions from >30 in silico algorithms (REVEL, AlphaMissense, CADD, SIFT, PolyPhen-2, GERP++, SpliceAI) for every possible nonsynonymous SNV in the human genome.

#### 2.4.2 API & Data Sources
- **Live Endpoint**: Free public REST via MyVariant.info:
  `GET https://api.myvariant.info/v1/variant/{chr-pos-ref-alt}?fields=dbnsfp` (e.g. `3-10149822-C-T`).
- **Offline Option**: Tabix-indexed `dbNSFP4.5a.gz`.

#### 2.4.3 Real Payload Structure (JSON)
```json
{
  "dbnsfp": {
    "revel": {
      "score": 0.892,
      "rankscore": 0.941
    },
    "alphamissense": {
      "score": 0.965,
      "pred": "pathogenic"
    },
    "cadd": {
      "phred": 28.4,
      "raw": 4.12
    },
    "sift": {
      "score": 0.0,
      "pred": "D"
    },
    "polyphen2_hdiv": {
      "score": 0.998,
      "pred": "D"
    },
    "gerp++_rs": 5.42,
    "phylop100way_vertebrate": 7.82
  }
}
```

#### 2.4.4 IR Node & Edge Mapping
1. **Nodes**:
   - `OBSERVATION:ComputationalPrediction`
   - Canonical ID: `dbNSFP:{SPDI}:ensemble` (e.g. `dbNSFP:NC_000003.12:10149821:C:T:ensemble`)
   - `attributes={"revel_score": 0.892, "alphamissense_score": 0.965, "cadd_phred": 28.4, "sift_score": 0.0}`.
2. **Edges**:
   - `(OBSERVATION:ComputationalPrediction, OBSERVES, ENTITY:Variant)`
   - `(OBSERVATION:ComputationalPrediction, RELATES_TO, ENTITY:Transcript)`
   - `(OBSERVATION:ComputationalPrediction, SOURCED_FROM, ENTITY:SourceManifest)`
3. **Operational ACMG Impact**:
   - Enables direct automated evaluation of ACMG criteria **`PP3`** ($\text{REVEL} \ge 0.7$) and **`BP4`** ($\text{REVEL} \le 0.15$).

---

### 2.5 MONDO (`mondo`) — Monarch Disease Ontology

#### 2.5.1 Purpose & Domain
MONDO harmonizes disease nomenclature across OMIM, MedGen, Orphanet, NCIT, and ICD-10 into a clean DAG (Directed Acyclic Graph) hierarchy.

#### 2.5.2 API & Data Sources
- **Live Endpoint**: EMBL-EBI OLS4 API:
  `GET https://www.ebi.ac.uk/ols4/api/ontologies/mondo/terms/http%253A%252F%252Fpurl.obolibrary.org%252Fobo%252FMONDO_0008667`
- **Monarch API**: `GET https://api.monarchinitiative.org/v3/api/entity/MONDO:0008667` (Open Access).

#### 2.5.3 Real Payload Structure (JSON)
```json
{
  "obo_id": "MONDO:0008667",
  "label": "Von Hippel-Lindau syndrome",
  "description": ["A rare genetic multi-system disorder characterized by the development of benign and malignant tumors..."],
  "synonyms": ["VHL syndrome", "Lindau disease", "retinocerebellar angiomatosis"],
  "parents": ["MONDO:0019052"],
  "xrefs": {
    "OMIM": ["193300"],
    "MedGen": ["C0019562"],
    "Orphanet": ["892"],
    "UMLS": ["C0019562"],
    "NCIT": ["C3439"]
  }
}
```

#### 2.5.4 IR Node & Edge Mapping
1. **Nodes**:
   - `ENTITY:Disease` $\rightarrow$ ID: `MONDO:0008667`
   - `attributes={"label": "Von Hippel-Lindau syndrome", "synonyms": [...]}`.
2. **Edges**:
   - `(ENTITY:Disease, PART_OF, ENTITY:Disease)` (child term $\rightarrow$ parent term hierarchy)
   - `(ENTITY:Disease, REPRESENTS, ENTITY:Disease)` (linking `MONDO:0008667` $\leftrightarrow$ `MedGen:C0019562` with `ConfidenceFields`).
3. **Crosswalk Closure**:
   - Bridges ClinVar submissions that use OMIM vs MedGen vs Orphanet to a unified disease root.

---

### 2.6 PharmGKB (`pharmgkb`) — Pharmacogenomics Knowledgebase

#### 2.6.1 Purpose & Domain
PharmGKB provides curated gene-drug-variant dosing guidelines (CPIC, DPWG), drug labels, and clinical pharmacogenomic efficacy/toxicity annotations.

#### 2.6.2 API & Data Sources
- **Live Endpoint**: `https://api.pharmgkb.org/v1/data/clinicalAnnotation?location.genes.symbol={gene_symbol}` (Open Access, unauthenticated JSON).
- **Variant Lookup**: `https://api.pharmgkb.org/v1/data/variant/{rsid}`.

#### 2.6.3 Real Payload Structure (JSON)
```json
{
  "data": [
    {
      "id": "PA166104992",
      "objCls": "Clinical Annotation",
      "levelOfEvidence": {
        "level": "Level 3",
        "description": "Annotation for a variant-drug combination based on a single study"
      },
      "genes": [{"symbol": "VHL", "id": "PA37535"}],
      "chemicals": [{"name": "Sunitinib", "id": "PA451556"}],
      "phenotypeCategory": "Efficacy",
      "sentence": "Carriers of the T allele may experience reduced therapeutic response to Sunitinib in clear cell renal cell carcinoma."
    }
  ]
}
```

#### 2.6.4 IR Node & Edge Mapping
1. **Nodes**:
   - `ENTITY:Drug` $\rightarrow$ ID: `PharmGKB:PA451556`, `attributes={"name": "Sunitinib"}`.
   - `CLAIM:PharmacogenomicAssertion` $\rightarrow$ ID: `PharmGKB:PA166104992`, `attributes={"level": "Level 3", "category": "Efficacy", "summary": "..."}`.
2. **Edges**:
   - `(CLAIM:PharmacogenomicAssertion, ASSERTS, ENTITY:Variant)`
   - `(CLAIM:PharmacogenomicAssertion, RELATES_TO[modulates_response_to], ENTITY:Drug)`
   - `(ENTITY:Drug, RELATES_TO[targets], ENTITY:Gene)`
   - `(CLAIM:PharmacogenomicAssertion, SOURCED_FROM, ENTITY:SourceManifest)`

---

## 3. Summary of Edge Grammar Verification

Every proposed edge combination is checked against the frozen schema rules:

| Source Node | Edge Predicate | Target Node | Allowed in Schema? |
| :--- | :---: | :--- | :---: |
| `ENTITY:Gene` | `RELATES_TO[causes]` | `ENTITY:Disease` | **YES** |
| `CLAIM:AllelicVariantAssertion` | `ASSERTS` | `ENTITY:Variant` | **YES** |
| `CLAIM:GeneDiseaseValidity` | `ASSERTS` | `ENTITY:Gene` | **YES** |
| `CLAIM:GeneDiseaseValidity` | `RELATES_TO[validates_association]` | `ENTITY:Disease` | **YES** |
| `OBSERVATION:CaseObservation` | `OBSERVES` | `ENTITY:Variant` | **YES** |
| `OBSERVATION:ComputationalPrediction` | `OBSERVES` | `ENTITY:Variant` | **YES** |
| `OBSERVATION:ComputationalPrediction` | `RELATES_TO` | `ENTITY:Transcript` | **YES** |
| `ENTITY:Disease` | `PART_OF` | `ENTITY:Disease` | **YES** |
| `ENTITY:Disease` | `REPRESENTS` | `ENTITY:Disease` | **YES** (with `confidence_fields`) |
| `CLAIM:PharmacogenomicAssertion` | `ASSERTS` | `ENTITY:Variant` | **YES** |
| `CLAIM:PharmacogenomicAssertion` | `RELATES_TO[modulates_response_to]` | `ENTITY:Drug` | **YES** |
| `ENTITY:Drug` | `RELATES_TO[targets]` | `ENTITY:Gene` | **YES** |
| All Claims / Observations | `SOURCED_FROM` | `ENTITY:SourceManifest` | **YES** |

---

## 4. Schema-Compliance Review (added 2026-08-19, after mechanical verification)

The research above — endpoints, auth status, real payload shapes, domain rationale — was
checked and is sound and directly usable. Several endpoints it identifies are genuine
improvements on what the stub modules currently record: the open-access
`erepo.clinicalgenome.org/evrepo/api/gene-validity/{symbol}` route sidesteps the HTML-only
`search.clinicalgenome.org` page that blocked `clingen_gene_disease`, and MyVariant.info
gives both `dbnsfp` and `cosmic` a free path that does not need a licensed account.

**Section 3's "Edge Grammar Verification" table does not hold.** Every row is marked
"Allowed in Schema? YES". Checked mechanically against `forge_ir/core.py`'s
`ALLOWED_EDGE_COMBINATIONS` and `NODE_SUBTYPE_REGISTRY` (via `is_allowed_edge`), **15 of 19
proposed combinations are rejected**. Written as specified, each one would pass review, then
fail at runtime in `forge_ir/verify.py` check 1 (type correctness) and take the whole
micro-batch down as a REJECT.

Four distinct failure modes, all of the same kind — the ontology is narrower than the table
assumes, and the table was not run against it:

| # | Failure mode | Count | Instances |
| :-- | :--- | :--: | :--- |
| A | **Node subtype not in `NODE_SUBTYPE_REGISTRY`** | 4 | `AllelicVariantAssertion`, `GeneDiseaseValidity`, `PharmacogenomicAssertion`, `Drug` |
| B | **Qualifier invented on a real predicate** | 6 | `RELATES_TO[causes]`, `[validates_association]`, `[associated_with]`, `[modulates_response_to]`, `[targets]`, `[observed_in_tissue]` |
| C | **`SOURCED_FROM` assumed to accept OBSERVATION sources** | 2 | the summary row "All Claims / Observations → `SOURCED_FROM`" — the combination is `CLAIM(any) → SOURCED_FROM → ENTITY:SourceManifest`, CLAIM only |
| D | **Predicate domain narrower than assumed** | 3 | `ASSERTS` targets `ENTITY:Variant` only (so `GeneDiseaseValidity ASSERTS Gene` fails); `PART_OF` is structural only, Transcript→Gene and Exon→Transcript (so `Disease PART_OF Disease` fails); `SUPPORTS[corroborates]` targets `ClinicalAssertion` only (so `CaseObservation SUPPORTS ClinicalImpactClassification` fails) |

The 4 rows that do hold: `CLAIM(any) ASSERTS ENTITY:Variant`, `OBSERVATION(any) OBSERVES
ENTITY:Variant`, `ENTITY(any) REPRESENTS ENTITY(any)`, and `CLAIM(any) SOURCED_FROM
ENTITY:SourceManifest`.

### 4.1 Two subtypes were invented that already exist

`GeneDiseaseValidity` and `PharmacogenomicAssertion` are both "X is associated with Y"
claims, and `Association` is already a registered CLAIM subtype for exactly that.
`connectors/clingen_gene_disease.py` already declares it. No schema change is needed for
either connector; the proposal reaches for new subtypes it does not need.

`Drug` and `AllelicVariantAssertion` have no existing equivalent and would be genuine
schema extensions. That is a legitimate proposal — but it is a proposal against a **frozen**
schema, and the document presents it as already-compliant, which is the part that has to
change.

### 4.2 What each stub can legally emit today

Derived from each connector's own declared `capabilities()["provides"]` cross-referenced
with `ALLOWED_EDGE_COMBINATIONS` — the same derivation the retry ledger uses
(`ingest/dirty.py::expected_edge_shapes`), so it stays correct as either side changes:

| Connector | Declares | Legal edges today | Needs schema change for |
| :--- | :--- | :--- | :--- |
| `omim` | `Disease` | `ENTITY REPRESENTS ENTITY` (MIM↔MedGen crosswalk), `CLAIM RELATES_TO[for_condition] Disease` | allelic-variant assertions; `Gene RELATES_TO[causes] Disease` |
| `clingen_gene_disease` | `Association` | `ASSERTS Variant`, `RELATES_TO[for_condition] Disease`, `SOURCED_FROM`, `CONTRADICTS`, `RELATES_TO[superseded_by]` | asserting against a **Gene** rather than a Variant — the real shape of a gene-disease curation |
| `cosmic` | `ClinicalAssertion` | full ClinVar-shaped claim grammar incl. `SUPPORTS[corroborates]`, `RELATES_TO[submitted_by]` | tissue/site as a first-class relation |
| `dbnsfp` | `ComputationalPrediction` | `OBSERVES Variant`, `SUPPORTS[satisfies_requirement] Requirement` | `SOURCED_FROM` from an OBSERVATION; linking a prediction to a Transcript |
| `mondo` | `Disease` | `ENTITY REPRESENTS ENTITY`, `CLAIM RELATES_TO[for_condition] Disease` | the MONDO **hierarchy** — no ENTITY→ENTITY containment predicate exists |
| `pharmgkb` | `Association` *(was empty; changed in this pass)* | `ASSERTS Variant`, `RELATES_TO[for_condition] Disease`, `SOURCED_FROM`, `CONTRADICTS` | `Drug` as an entity rather than an attribute |

`dbnsfp`'s `SUPPORTS[satisfies_requirement] → Requirement` is worth calling out as the
document's strongest and most under-sold finding: it is legal **today**, and it is the edge
that would close the loop from an in-silico score to ClinGen's PP3/BP4 criteria — the first
time the Operational rule layer would be driven by real evidence rather than sitting beside
it.

### 4.3 Two real ontology gaps this surfaced

Neither is a defect in the research; both are places where the frozen schema is genuinely
narrower than the sources are, and both should be decided deliberately rather than
worked around:

1. **`SOURCED_FROM` is CLAIM-only.** An OBSERVATION has a source too. `dbNSFP` predictions
   and `COSMIC` case observations cannot record where they came from, and
   `orchestration/connector_registry.py::tag_with_source_manifest` correctly skips them
   today — meaning every OBSERVATION in the graph is currently unattributed.
2. **No ENTITY→ENTITY containment predicate.** `PART_OF` is reserved for
   Transcript→Gene and Exon→Transcript. A disease ontology is a DAG, and MONDO's whole
   value is that hierarchy; without a predicate for it, `mondo` can only ever contribute
   flat crosswalk edges.

### 4.4 Changes made in this pass

- `connectors/pharmgkb.py` now declares `provides=["Association"]` instead of `[]`. The
  empty list was deliberate and documented, but it made `analysis/completeness.py` report
  "0/0 declared subtypes present" — a vacuous 0% that reads as a scored gap rather than an
  unmeasurable one — and left the retry ledger unable to derive any expected edge shape for
  it. The drug is carried as an attribute; §4.1's `Drug` entity remains the richer option.
- `tests/test_connector_capability_grammar.py` added: three checks over **every** registered
  connector — declared subtypes exist in the ontology, `provides` is non-empty, and the
  declared subtypes sit on at least one legal edge combination. This is the mechanical
  version of §3's table, and it runs on every commit, so a mapping proposed on paper cannot
  again reach a connector module without being checked against the schema it targets.
