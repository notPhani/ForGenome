"""Tests for ingest/hashing.py -- forge-pipeline-v1.md §6: "same four inputs -> same
canonical graph, verified by comparing IR hashes." Offline and deterministic: real
fixtures parsed twice, independently, in-process -- no live network involved, so this
can actually assert equality rather than just hoping two runs happen to agree.
"""
from datetime import datetime, timezone

from forge_ir.core import EdgeType, NodeRole, Origin
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.provenance import SourceProvenance
from ingest.hashing import compute_structural_hash
from ingest.compiler import StagingCompiler
from ingest.identity import IdentityIndex
from orchestration.runner import RunConfig, run

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _gene_node(retrieved_at: datetime) -> ForgeNode:
    return ForgeNode(id="HGNC:12687", role=NodeRole.ENTITY, subtype="Gene",
                      attributes={"symbol": "VHL", "biotype": "protein_coding"},
                      provenance=[SourceProvenance("Ensembl", "ENSG00000134086", "12", retrieved_at)],
                      origin=Origin.SOURCE)


def test_the_same_node_hashes_identically_even_with_a_different_retrieved_at():
    # This is the whole point: every connector calls datetime.now(), so two structurally
    # identical parses a second apart must still hash the same, or "same inputs -> same
    # graph" could never be verified against anything but a frozen clock.
    a = compute_structural_hash([_gene_node(NOW)], [])
    b = compute_structural_hash([_gene_node(datetime(2027, 6, 1, tzinfo=timezone.utc))], [])
    assert a == b


def test_a_genuinely_different_attribute_changes_the_hash():
    a = compute_structural_hash([_gene_node(NOW)], [])
    different = _gene_node(NOW)
    different.attributes = {"symbol": "VHL", "biotype": "lncRNA"}   # a real content change
    b = compute_structural_hash([different], [])
    assert a != b


def test_node_and_edge_order_does_not_affect_the_hash():
    n1 = _gene_node(NOW)
    n2 = ForgeNode(id="Ensembl:ENST00000256474", role=NodeRole.ENTITY, subtype="Transcript",
                   attributes={"gene_id": "HGNC:12687", "is_canonical": True, "biotype": "protein_coding"},
                   provenance=[SourceProvenance("Ensembl", "ENST00000256474", "3", NOW)], origin=Origin.SOURCE)
    edge = ForgeEdge(src=n2.id, predicate=EdgeType.PART_OF, dst=n1.id, qualifier=None,
                      attributes={}, provenance=[SourceProvenance("Ensembl", "ENST00000256474", "3", NOW)],
                      origin=Origin.SOURCE)

    forward = compute_structural_hash([n1, n2], [edge])
    reversed_order = compute_structural_hash([n2, n1], [edge])
    assert forward == reversed_order


def test_source_manifest_retrieved_at_is_excluded_even_though_it_is_only_in_attributes():
    # build_source_manifest_node stores retrieved_at as an ISO string INSIDE attributes
    # (not as a Provenance field), so this exercises the attribute-level strip, not just
    # the provenance-level one.
    from connectors.common import build_source_manifest_node
    a = build_source_manifest_node("ClinVar", "E-utilities", "44", NOW)
    b = build_source_manifest_node("ClinVar", "E-utilities", "44", datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert compute_structural_hash([a], []) == compute_structural_hash([b], [])


def test_two_independent_parses_of_the_same_real_fixtures_hash_identically():
    """The real, end-to-end version of the claim: run the whole offline pipeline twice,
    independently (fresh IdentityIndex, fresh StagingCompiler, fresh timestamps each
    time), and confirm the committed graph hashes the same both times."""
    def _offline_run():
        return run(RunConfig(mode="full", offline=True, progress=False))

    first = _offline_run()
    second = _offline_run()
    assert first.structural_hash == second.structural_hash
    assert first.structural_hash is not None
    assert len(first.nodes) == len(second.nodes)


def test_commit_result_carries_a_hash_and_a_rejected_batch_carries_none():
    compiler = StagingCompiler(IdentityIndex(), run_id="run1")
    compiler.stage([_gene_node(NOW)], [])
    result = compiler.commit()
    assert result.committed
    assert result.structural_hash is not None

    bad_edge = ForgeEdge(src="HGNC:12687", predicate=EdgeType.PART_OF, dst="does-not-exist",
                          qualifier=None, attributes={}, provenance=[SourceProvenance("Ensembl", "x", "1", NOW)],
                          origin=Origin.SOURCE)
    compiler2 = StagingCompiler(IdentityIndex(), run_id="run1")
    compiler2.stage([], [bad_edge])
    rejected = compiler2.commit()
    assert not rejected.committed
    assert rejected.structural_hash is None
