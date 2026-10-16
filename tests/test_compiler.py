"""Tests for ingest/compiler.py -- staging, dedup with provenance merge, the
verify-gate (batch REJECT, nothing partially committed), the edge ledger, and
identity-index integration for deterministic-canonical-ID node roles."""
from datetime import datetime, timezone

from forge_ir.core import EdgeType, NodeRole, Origin
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.provenance import DerivedProvenance, SourceProvenance
from ingest.compiler import StagingCompiler
from ingest.identity import IdentityIndex

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _gene_node(source: str) -> ForgeNode:
    return ForgeNode(
        id="HGNC:12687", role=NodeRole.ENTITY, subtype="Gene",
        attributes={"symbol": "VHL", "biotype": "protein_coding"},
        provenance=[SourceProvenance(source, "gene-rec", "v1", NOW)], origin=Origin.SOURCE,
    )


def _variant_node() -> ForgeNode:
    return ForgeNode(
        id="SPDI:x:1:C:T", role=NodeRole.ENTITY, subtype="Variant",
        attributes={"assembly": "GRCh38", "ref": "C", "alt": "T"},
        provenance=[SourceProvenance("ClinVar", "var-rec", "44", NOW)], origin=Origin.SOURCE,
    )


def _in_gene_edge() -> ForgeEdge:
    return ForgeEdge(
        src="SPDI:x:1:C:T", predicate=EdgeType.RELATES_TO, dst="HGNC:12687", qualifier="in_gene",
        attributes={}, provenance=[SourceProvenance("ClinVar", "var-rec", "44", NOW)], origin=Origin.SOURCE,
    )


def test_commit_succeeds_for_a_valid_batch():
    compiler = StagingCompiler(IdentityIndex(), run_id="run1")
    compiler.stage([_gene_node("Ensembl"), _variant_node()], [_in_gene_edge()])
    result = compiler.commit()
    assert result.committed and not result.rejected
    assert len(result.nodes) == 2
    assert len(result.edges) == 1
    assert result.verification.valid


def test_commit_rejects_the_whole_batch_on_a_dangling_edge():
    compiler = StagingCompiler(IdentityIndex(), run_id="run1")
    bad_edge = ForgeEdge(
        src="SPDI:x:1:C:T", predicate=EdgeType.RELATES_TO, dst="HGNC:does-not-exist", qualifier="in_gene",
        attributes={}, provenance=[SourceProvenance("ClinVar", "var-rec", "44", NOW)], origin=Origin.SOURCE,
    )
    compiler.stage([_variant_node()], [bad_edge])
    result = compiler.commit()
    assert not result.committed
    assert result.rejected
    assert result.nodes == [] and result.edges == []  # nothing partially committed


def test_dedup_merges_provenance_for_the_same_node_id_from_two_sources():
    compiler = StagingCompiler(IdentityIndex(), run_id="run1")
    compiler.stage([_gene_node("Ensembl"), _gene_node("ClinVar")], [])
    result = compiler.commit()
    assert result.committed
    assert len(result.nodes) == 1  # deduped by id, not two separate Gene nodes
    sources = {p.source for p in result.nodes[0].provenance}
    assert sources == {"Ensembl", "ClinVar"}  # both sources' provenance kept, not overwritten


def test_dedup_merges_provenance_for_duplicate_edges():
    compiler = StagingCompiler(IdentityIndex(), run_id="run1")
    compiler.stage([_gene_node("Ensembl"), _variant_node()], [_in_gene_edge(), _in_gene_edge()])
    result = compiler.commit()
    assert len(result.edges) == 1
    assert len(result.edges[0].provenance) == 2


def test_edge_ledger_is_populated_and_auditable():
    compiler = StagingCompiler(IdentityIndex(), run_id="run1")
    compiler.stage([_gene_node("Ensembl"), _variant_node()], [_in_gene_edge()])
    result = compiler.commit()
    assert len(result.edge_ledger) == 1
    entry = result.edge_ledger[0]
    assert entry["src"] == "SPDI:x:1:C:T"
    assert entry["predicate"] == "RELATES_TO"
    assert entry["qualifier"] == "in_gene"
    assert entry["dst"] == "HGNC:12687"


def test_identity_resolution_runs_for_gene_and_variant_but_not_claim_or_observation():
    compiler = StagingCompiler(IdentityIndex(), run_id="run1")
    claim = ForgeNode(
        id="ClinVar:SCV001", role=NodeRole.CLAIM, subtype="ClinicalAssertion",
        attributes={"review_status": "x", "classification": "x"},
        provenance=[SourceProvenance("ClinVar", "SCV001", "44", NOW)], origin=Origin.SOURCE,
    )
    # The claim needs to actually connect to the graph (ASSERTS -> Variant) or the batch
    # gets REJECTED on connectivity grounds before identity resolution ever runs -- that's
    # the correct behavior (see the ordering fix in compiler.py), just needs a valid fixture here.
    asserts_edge = ForgeEdge(
        src=claim.id, predicate=EdgeType.ASSERTS, dst="SPDI:x:1:C:T", qualifier=None,
        attributes={}, provenance=[SourceProvenance("ClinVar", "SCV001", "44", NOW)], origin=Origin.SOURCE,
    )
    compiler.stage([_gene_node("Ensembl"), _variant_node(), claim], [asserts_edge])
    result = compiler.commit()
    assert result.committed, result.rejection_reason
    resolved_ids = {log["node_id"] for log in result.identity_resolutions}
    assert resolved_ids == {"HGNC:12687", "SPDI:x:1:C:T"}  # not the CLAIM -- already uniquely
                                                             # keyed by its own stable SCV accession


def test_edge_ledger_handles_derived_origin_edges_not_just_source():
    # Regression test: the edge ledger builder assumed every edge's provenance[0] was a
    # SourceProvenance (has .source); a real DERIVED edge (e.g. ClinVar's CONTRADICTS,
    # which Forge computes itself, not something ClinVar hands us) crashed it the first
    # time a batch actually contained one -- never exercised by earlier tests since none
    # staged a DERIVED-origin edge until the presentation demo data generator did.
    compiler = StagingCompiler(IdentityIndex(), run_id="run1")
    claim_a = ForgeNode(
        id="ClinVar:SCVA", role=NodeRole.CLAIM, subtype="ClinicalAssertion",
        attributes={"review_status": "x", "classification": "Pathogenic"},
        provenance=[SourceProvenance("ClinVar", "SCVA", "1", NOW)], origin=Origin.SOURCE,
    )
    claim_b = ForgeNode(
        id="ClinVar:SCVB", role=NodeRole.CLAIM, subtype="ClinicalAssertion",
        attributes={"review_status": "x", "classification": "Benign"},
        provenance=[SourceProvenance("ClinVar", "SCVB", "1", NOW)], origin=Origin.SOURCE,
    )
    asserts_a = ForgeEdge(src=claim_a.id, predicate=EdgeType.ASSERTS, dst="SPDI:x:1:C:T", qualifier=None,
                           attributes={}, provenance=[SourceProvenance("ClinVar", "SCVA", "1", NOW)],
                           origin=Origin.SOURCE)
    asserts_b = ForgeEdge(src=claim_b.id, predicate=EdgeType.ASSERTS, dst="SPDI:x:1:C:T", qualifier=None,
                           attributes={}, provenance=[SourceProvenance("ClinVar", "SCVB", "1", NOW)],
                           origin=Origin.SOURCE)
    contradicts = ForgeEdge(
        src=claim_a.id, predicate=EdgeType.CONTRADICTS, dst=claim_b.id, qualifier=None, attributes={},
        provenance=[DerivedProvenance("test_diff", (claim_a.id, claim_b.id), (), "v1", NOW)],
        origin=Origin.DERIVED,
    )
    compiler.stage([_variant_node(), claim_a, claim_b], [asserts_a, asserts_b, contradicts])
    result = compiler.commit()
    assert result.committed, result.rejection_reason
    contradicts_entry = next(e for e in result.edge_ledger if e["predicate"] == "CONTRADICTS")
    assert contradicts_entry["source"] == "derived:test_diff"


def test_identity_index_persists_a_cross_run_merge():
    index = IdentityIndex()
    compiler1 = StagingCompiler(index, run_id="run1")
    compiler1.stage([_gene_node("Ensembl")], [])
    compiler1.commit()

    entry_after_run1 = index.lookup("HGNC", "HGNC:12687")
    assert entry_after_run1.first_seen_run == "run1"

    compiler2 = StagingCompiler(index, run_id="run2")
    compiler2.stage([_gene_node("ClinVar")], [])
    compiler2.commit()

    entry_after_run2 = index.lookup("HGNC", "HGNC:12687")
    assert entry_after_run2.first_seen_run == "run1"   # unchanged
    assert entry_after_run2.last_seen_run == "run2"    # updated by the second, independent run
