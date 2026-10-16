"""Tests against the REAL saved Ensembl fixtures -- no synthetic data at this layer,
per forge-pipeline-v1.md's own testing discipline (connector-research.md §5's mapping
table is exactly what this asserts against)."""
import json
import os

from forge_ir.core import EdgeType, NodeRole
from forge_ir.graph import ForgeEdge, ForgeNode
from connectors.ensembl import EnsemblParser, GENE_MAPPING_SPEC, vep_consequence_to_ir
from ingest.mapping import apply_mapping_spec
from datetime import datetime, timezone

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def test_parses_real_gene_fixture_and_extracts_hgnc():
    raw = _load("ensembl_gene_VHL.json")
    parser = EnsemblParser()
    gene = parser.parse_gene(raw)
    assert gene.ensembl_gene_id == "ENSG00000134086"
    assert gene.symbol == "VHL"
    assert gene.hgnc_id == "12687"
    assert len(gene.transcripts) == 15  # confirmed real count


def test_gene_hierarchy_escape_hatch_produces_correct_canonical_gene_node():
    raw = _load("ensembl_gene_VHL.json")
    gene = EnsemblParser().parse_gene(raw)
    results = apply_mapping_spec(gene, GENE_MAPPING_SPEC, primary_node_id=None,
                                  source="Ensembl", source_record_id=gene.ensembl_gene_id,
                                  source_version=gene.source_version, retrieved_at=datetime.now(timezone.utc))
    gene_nodes = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "Gene"]
    assert len(gene_nodes) == 1
    assert gene_nodes[0].id == "HGNC:12687"


def test_only_canonical_transcript_gets_full_exon_structure():
    raw = _load("ensembl_gene_VHL.json")
    gene = EnsemblParser().parse_gene(raw)
    results = apply_mapping_spec(gene, GENE_MAPPING_SPEC, primary_node_id=None,
                                  source="Ensembl", source_record_id=gene.ensembl_gene_id,
                                  source_version=gene.source_version, retrieved_at=datetime.now(timezone.utc))
    transcript_nodes = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "Transcript"]
    exon_nodes = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "Exon"]

    assert len(transcript_nodes) == 15  # all transcripts get a stub node
    canonical = [t for t in transcript_nodes if t.attributes["is_canonical"]]
    assert len(canonical) == 1
    assert canonical[0].id == "Ensembl:ENST00000256474"
    assert canonical[0].lod == 2

    non_canonical = [t for t in transcript_nodes if not t.attributes["is_canonical"]]
    assert all(t.lod == 0 for t in non_canonical)  # stubs, not fully hydrated

    # exons only exist for the canonical transcript's own exon set
    assert len(exon_nodes) == 3  # confirmed real count for ENST00000256474
    assert all(e.attributes["transcript_id"] == "Ensembl:ENST00000256474" for e in exon_nodes)


def test_transcript_part_of_gene_edges_use_correct_predicate():
    raw = _load("ensembl_gene_VHL.json")
    gene = EnsemblParser().parse_gene(raw)
    results = apply_mapping_spec(gene, GENE_MAPPING_SPEC, primary_node_id=None,
                                  source="Ensembl", source_record_id=gene.ensembl_gene_id,
                                  source_version=gene.source_version, retrieved_at=datetime.now(timezone.utc))
    part_of_edges = [r for r in results if isinstance(r, ForgeEdge) and r.predicate == EdgeType.PART_OF
                      and r.dst == "HGNC:12687"]
    assert len(part_of_edges) == 15


def test_canonical_transcript_encodes_protein():
    raw = _load("ensembl_gene_VHL.json")
    gene = EnsemblParser().parse_gene(raw)
    results = apply_mapping_spec(gene, GENE_MAPPING_SPEC, primary_node_id=None,
                                  source="Ensembl", source_record_id=gene.ensembl_gene_id,
                                  source_version=gene.source_version, retrieved_at=datetime.now(timezone.utc))
    encodes = [r for r in results if isinstance(r, ForgeEdge) and r.qualifier == "encodes"]
    assert len(encodes) == 1
    assert encodes[0].src == "Ensembl:ENST00000256474"
    assert encodes[0].dst == "Ensembl:ENSP00000256474"


def test_vep_consequences_are_transcript_scoped_not_collapsed():
    raw = _load("ensembl_vep_c499CT.json")
    parser = EnsemblParser()
    consequences = list(parser.parse_vep(raw))
    variant_id = "SPDI:NC_000003.12:10149821:C:T"

    all_results = []
    for c in consequences:
        all_results.extend(vep_consequence_to_ir(c, variant_id))

    consequence_edges = [r for r in all_results if isinstance(r, ForgeEdge) and r.qualifier == "has_consequence"]
    assert len(consequence_edges) >= 6  # multiple real transcripts, confirmed >1

    # the two known real transcripts with DIFFERENT amino-acid outcomes for the same
    # genomic change -- transcript ambiguity, not a bug
    by_transcript = {e.dst: e.attributes["hgvsp"] for e in consequence_edges}
    assert by_transcript["Ensembl:ENST00000256474"] == "ENSP00000256474.3:p.Arg167Trp"
    assert by_transcript["Ensembl:ENST00000345392"] == "ENSP00000344757.2:p.Arg126Trp"


def test_sift_and_polyphen_become_computational_prediction_observations():
    raw = _load("ensembl_vep_c499CT.json")
    consequences = list(EnsemblParser().parse_vep(raw))
    canonical = next(c for c in consequences if c.transcript_id == "ENST00000256474")
    results = vep_consequence_to_ir(canonical, "SPDI:NC_000003.12:10149821:C:T")

    predictions = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "ComputationalPrediction"]
    tools = {p.attributes["tool"] for p in predictions}
    assert tools == {"SIFT", "PolyPhen2"}

    sift = next(p for p in predictions if p.attributes["tool"] == "SIFT")
    assert sift.attributes["prediction"] == "deleterious"
    polyphen = next(p for p in predictions if p.attributes["tool"] == "PolyPhen2")
    assert polyphen.attributes["prediction"] == "probably_damaging"

    observes_edges = [r for r in results if isinstance(r, ForgeEdge) and r.predicate == EdgeType.OBSERVES]
    assert len(observes_edges) == 2
    assert all(e.dst == "SPDI:NC_000003.12:10149821:C:T" for e in observes_edges)
