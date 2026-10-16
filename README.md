# ForGenome (Forge POC)

> **A strongly-typed, provenance-tracked genomics knowledge graph ETL pipeline for ACMG variant classification evidence.**

Forge ingests heterogeneous public biomedical and genomics data (ClinVar, Ensembl, gnomAD, ClinGen, dbSNP, UniProt), standardizes and normalizes them into a unified Intermediate Representation (**Forge IR v3**), validates every batch against **9 structural correctness checks**, and compiles them into a queryable knowledge graph.

The system is designed as foundational infrastructure for clinical reasoning engines (such as MHLA) to traverse evidence graphs and evaluate ACMG/AMP variant interpretation criteria (such as PM2, PVS1, PS4) with strict provenance and auditability.

---

## Key Highlights

- **Multi-Source Ingestion**: Ingests clinical assertions, gene/transcript models, population allele frequencies, and expert variant curation rules from NCBI ClinVar, Ensembl REST, gnomAD GraphQL, ClinGen, dbSNP, and UniProt.
- **Strict Verification Guardrails**: Every incoming micro-batch is audited against **9 correctness rules** before joining the graph. Batches with referential flaws, dangling edges, schema mismatches, or missing provenance are rejected atomically.
- **Stratified Knowledge Layers**:
  - `CORE_BIOLOGY` *(Persistent)*: Fundamental biology (genes, transcripts, exons, proteins).
  - `OPERATIONAL` *(Persistent)*: Interpretation rules, ClinGen VCEP specifications, ACMG criteria.
  - `EVIDENCE` *(Persistent/Ephemeral)*: Submissions, clinical assertions, observations, allele frequencies.
  - `ADMINISTRATIVE` & `INVESTIGATION` *(Audit/Runtime)*: Source record manifests, execution telemetry, and runtime reasoning traces.
- **Deterministic Identity Resolution**: Cross-source entity resolution across Genes (HGNC/Ensembl/Entrez), Variants (GRCh38 canonical SPDI/HGVS), and Diseases (Mondo, OMIM, MedGen), eliminating duplicated entity nodes.
- **Resilient & Fault-Tolerant Harvesting**:
  - Streaming store writing gzip-compressed JSONL chunks with crash-safe atomic checkpoints.
  - Dirty state machine (`PENDING`, `RESOLVED`, `EMPTY`, `ABANDONED`) and retry ledgers for rate limits, network faults, and deferred variant enrichments.
  - Mid-stream gzip block truncation recovery (`scripts/build_viz_dataset.py::read_dump`).
- **Zero-Network Offline Mode**: Ships with complete test fixtures for local testing, development, and CI without requiring network access.
- **Interactive Graph Visualizer**: Standalone HTML/Canvas visualization dashboard (`demo/index.html`) to explore and inspect generated knowledge graphs.

---

## Architecture Overview

```
                     CLI (main.py) / Overnight Harvest (scripts/overnight_harvest.py)
                                           │
                        RunConfig { gene, mode, connectors, offline, ... }
                                           │
                 ┌─────────────────────────┴────────────────────────┐
                 │ mode=persistent/full                             │ mode=ephemeral/full
                 ▼                                                  ▼
     orchestration/load_plan.py                           ClinVar / ESearch query
     plan_load() -> LoadPlan                              Fetch batch variation IDs
                 │                                                  │
                 └─────────────────────────┬────────────────────────┘
                                           ▼
                               Connectors (connectors/*.py)
              (ClinVar, Ensembl, gnomAD, ClinGen, dbSNP, UniProt)
                                           │
                                           ▼
                                  Raw Source Records
                               (JSON / XML / GraphQL)
                                           │
                                           ▼
                           ingest/source_objects.py Parsers
                                           │
                                           ▼
                                   ingest/mapping.py
                     Declarative Mapping Rules & Escape Hatches
                                           │
                                           ▼
                             list[ForgeNode | ForgeEdge]
                                           │
                                           ▼
                                 ingest/compiler.py
                              StagingCompiler.stage()
                                           │
                                           ▼
                              forge_ir/verify.py :: verify()
                               ┌──────────────────────┐
                               │ 9 Correctness Checks │
                               └──────────┬───────────┘
                                          │
                         ┌────────────────┴────────────────┐
                    PASS │                            FAIL │
                         ▼                                 ▼
              ingest/identity.py                  Atomically Rejected
           Cross-Source Normalization             Nothing Committed to Graph
                         │
                         ▼
             Structural Hash & Commit
                         │
                         ▼
        orchestration/runner.py (GraphStore) / harvest.py (StreamingStore)
                         │
                         ▼
        Committed Graph (In-Memory or runs/<panel>/graph.jsonl.gz)
```

---

## The 9 Correctness Checks

Every micro-batch must pass all 9 rules in `forge_ir/verify.py` before any node or edge is committed:

1. **Type Conformance**: Every node and edge matches a known subtype in `NODE_SUBTYPE_REGISTRY` and `ALLOWED_EDGE_COMBINATIONS`.
2. **Referential Integrity**: Source and destination nodes of all edges exist within the current batch or pre-exist in the resident graph.
3. **Record Conservation**: Every ingested raw source record must produce at least one graph node or edge.
4. **Layer Stratification**: Validates permissible relationships across layer boundaries (e.g. Core Biology cannot depend on ephemeral Evidence).
5. **Provenance Completeness**: All nodes and edges must carry explicit `SourceProvenance`, `DerivedProvenance`, or `RuntimeProvenance`.
6. **Hydration**: All mandatory schema attributes for a given node subtype are populated and non-empty.
7. **Connectivity**: Enforces network traversal limits to avoid orphan or isolated subgraphs.
8. **Storage Scope Separation**: `PERSISTENT` entities are never gated on or dependent on `RUNTIME_ONLY` entities.
9. **Predicate & Qualifier Matching**: Edge predicates, qualifiers, and node role combinations must satisfy semantic constraints.

---

## Repository Structure

```
ForGenome/
├── main.py                     # Unified pipeline entry point (persistent, ephemeral, full)
├── requirements.txt            # Minimal runtime dependencies (requests, networkx, pytest)
├── analysis/                   # Graph completeness, downsampling, and edge statistics
│   ├── completeness.py
│   ├── downsample.py
│   └── edge_stats.py
├── connectors/                 # External biomedical API connectors and parsers
│   ├── clinvar.py              # NCBI ClinVar (ESearch, ESummary, EFetch VCV/RCV/SCV)
│   ├── ensembl.py              # Ensembl REST (gene models, transcripts, exons, VEP)
│   ├── gnomad.py               # gnomAD v4 GraphQL API (population allele frequencies)
│   ├── clingen.py              # ClinGen Evidence Repository & CSPEC rules
│   ├── clingen_gene_disease.py # ClinGen Gene-Disease validity curation
│   ├── dbsnp.py                # NCBI dbSNP (RefSNP API)
│   ├── uniprot.py              # UniProtKB REST (proteins, domains)
│   └── [cosmic, dbnsfp, ...]   # Documented connector stubs
├── data/                       # Curated gene panels (e.g., forge-actionable-panel-v1)
│   └── gene_panel.json
├── demo/                       # Interactive browser visualizer and sample graph datasets
│   ├── index.html              # Standalone bundled interactive graph explorer
│   ├── template.html           # Visualizer HTML/JS template
│   └── graph_data.json         # Pre-compiled graph data for demonstration
├── docs/                       # Specifications, IR schema, system maps, and verification audit
│   ├── blind-system-map.md
│   ├── forge-ir-v3-schema.md
│   ├── forge-pipeline-v1.md
│   └── verification-audit.md
├── fixtures/                   # Deterministic offline test fixtures for all connectors
├── forge_ir/                   # Forge Intermediate Representation core definitions
│   ├── core.py                 # Enums, node/edge registries, allowed edge combinations
│   ├── graph.py                # ForgeNode, ForgeEdge dataclasses
│   ├── provenance.py           # SourceProvenance, DerivedProvenance, RuntimeProvenance
│   ├── requirement.py          # ACMG requirement evaluation model
│   └── verify.py               # 9-rule atomic verification engine
├── ingest/                     # Ingestion, mapping, compilation, and dirty tracking
│   ├── compiler.py             # Atomic StagingCompiler
│   ├── dirty.py                # Dirty state ledger and retry tracking
│   ├── hashing.py              # Deterministic structural graph hashing
│   ├── identity.py             # Canonical entity resolution (Gene, Variant, Disease)
│   ├── mapping.py              # Declarative mapping engine & escape hatches
│   └── variant_notation.py     # HGVS, SPDI, and coordinate normalizers
├── orchestration/              # Workflow execution, planning, and harvesting
│   ├── connector_planner.py    # DAG dependency planner for connector queries
│   ├── connector_registry.py   # Registry of implemented connectors and capabilities
│   ├── harvest.py              # Multi-gene panel streaming harvester with budget allocation
│   ├── load_plan.py            # Layered load planning
│   └── runner.py               # Single-gene execution pipeline runner
├── runs/                       # Output artifacts, run logs, checkpoints, and benchmark harvests
├── scripts/                    # Utility scripts
│   ├── build_demo_html.py      # Inlines graph data into standalone viewer
│   ├── build_viz_dataset.py    # Downsamples and prepares panel dumps for browser rendering
│   ├── overnight_harvest.py    # Long-running unattended panel harvester
│   └── verify_connectors.py    # Connector validation script
└── tests/                      # Full test suite (263 tests passing)
    ├── e2e/                    # End-to-end criteria tests (e.g. VHL PM2 criteria)
    └── test_connectors/        # Connector-specific unit tests
```

---

## Getting Started

### 1. Requirements & Installation

- Python 3.10+
- Install dependencies:

```bash
pip install -r requirements.txt
```

*(Core dependencies: `requests`, `networkx`, `pytest`)*

### 2. Running the Pipeline

`main.py` provides a single entry point for all modes:

#### Option A: Run Full Pipeline Offline (Recommended for First Run)
Runs the complete pipeline end-to-end using local fixtures (no network required):

```bash
python main.py --mode full --offline --out demo/graph_data.json --build-html
```

#### Option B: Stratified Two-Phase Execution

1. **Persistent Layer Run** (Builds Core Biology + Operational interpretation rules once):
   ```bash
   python main.py --gene VHL --mode persistent --save-resident runs/vhl_persistent.json
   ```

2. **Ephemeral Layer Run** (Ingests dynamic evidence and binds to resident persistent layers):
   ```bash
   python main.py --gene VHL --mode ephemeral --sample-pct 50 \
       --resident runs/vhl_persistent.json \
       --out demo/graph_data.json --build-html
   ```

#### Option C: Overnight Actionable Panel Harvest
To execute an unattended harvest across the curated 79-gene actionable panel (`data/gene_panel.json`) with automated budget allocation, checkpointing, and retry handling:

```bash
# Dry run: view the plan and fair budget allocation without network requests
python scripts/overnight_harvest.py --dry-run

# Run full harvest with resume capability
python scripts/overnight_harvest.py --out runs/panel_50k --target 50000
```

To convert a harvested panel dump into a browser-renderable format:
```bash
python scripts/build_viz_dataset.py runs/panel_50k --out demo/graph_data.json
python scripts/build_demo_html.py
```

---

## Interactive Visualizer

Open `demo/index.html` in any modern web browser to interactively explore the generated knowledge graph:
- Filter nodes by Layer (`CORE_BIOLOGY`, `OPERATIONAL`, `EVIDENCE`) or Role (`ENTITY`, `CLAIM`, `OBSERVATION`, `RULE`).
- Inspect node details, connected edges, and complete provenance metadata.
- View real-time edge type statistics and graph connectivity.
- Open the **Gemma 4** tab for three hardcoded VHL questions, evidence-grounded answers, plain-language explanations, and highlighted subgraphs drawn from the same demo graph.

The Gemma 4 tab is a reviewable model footprint, not a live API call: the
questions and answers are stored in `demo/gemma_guides.json`, and each guide
references exact node IDs and event-order edge IDs from `demo/graph_data.json`.
This keeps the demo reproducible while the live model adapter is being
evaluated. The cards explicitly distinguish source assertions from conclusions
and include limitations so they are not presented as clinical diagnoses.

---

## Running the Test Suite

Forge includes a comprehensive suite of 266 unit, integration, and E2E tests:

```bash
python -m pytest
```

The test suite covers:
- Atomic batch compiler rejection on referential errors (`test_compiler.py`)
- Dirty state machine and retry ledgers (`test_dirty.py`)
- Truncated gzip block stream recovery (`test_dump_recovery.py`)
- Canonical identity resolution for Genes, Variants, and Diseases (`test_identity.py`)
- Deterministic sampling and structural graph hashing (`test_reproducibility.py`, `test_hashing.py`)
- End-to-end evaluation of ACMG PM2 evidence on VHL (`tests/e2e/test_vhl_pm2.py`)
- Conformance and capability checks for all connectors (`tests/test_connectors/`)
- Constrained graph retrieval, claim auditing, and report summaries (`tests/test_graph_rag.py`)

## Genome graph matching and plain-language reports

Forge also includes an offline, evidence-grounded Graph RAG baseline for
exploring a gene or variant in a large graph. It applies typed metapath
constraints before ranking evidence, uses transparent rarity/functional/
clinical relevance heads with attention-biased random walk with restart, and
keeps the selected subgraph within a configurable detail budget. It does not
claim pathogenicity or treatment efficacy and it does not require an LLM API
key.

```bash
python scripts/analyze_genome.py \
  --graph demo/graph_data.json \
  --gene VHL \
  --lod 2 \
  --out-html runs/vhl_graph_report.html \
  --out-json runs/vhl_graph_report.json
```

The command writes a portable HTML/SVG plot with a plain-language explanation
and machine-readable evidence containing compact typed triples, stable
`Edge ID`s, provenance, scores, paths and budget status. Structured generated
claims can be checked with `analysis.graph_rag.ClaimAuditor` before any prose
is shown to a user. Unknown edges, reversed relations, disconnected paths,
invalid clinical metapaths and overstated causal wording are rejected. The
current graph event format does not always include explicit edge IDs, so the
analyzer assigns deterministic one-based IDs (`E000001`, ... ) within the
loaded graph snapshot.

---

## Documentation

For in-depth technical specifications and findings, refer to the `docs/` directory:
- [`docs/blind-system-map.md`](docs/blind-system-map.md): Deep architectural map and verified system capabilities.
- [`docs/forge-ir-v3-schema.md`](docs/forge-ir-v3-schema.md): IR v3 node/edge specifications, enums, and schema contracts.
- [`docs/forge-pipeline-v1.md`](docs/forge-pipeline-v1.md): End-to-end pipeline design and data flow.
- [`docs/verification-audit.md`](docs/verification-audit.md): Systematic audit of contracts, live panel data, and recovery mechanisms.
