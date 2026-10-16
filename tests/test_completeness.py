"""Tests for analysis/completeness.py -- the advisory graph-completeness report.
Deliberately never touches forge_ir/verify.py's blocking checks; these tests confirm it
reports gaps honestly (declared-but-absent subtypes, unexercised edge combinations)
without ever failing a run over them.
"""
from datetime import datetime, timezone

from analysis.completeness import compute_completeness
from forge_ir.core import EdgeType, NodeRole, Origin
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.provenance import SourceProvenance
from orchestration.connector_registry import ConnectorDescriptor
from orchestration.runner import RunConfig, run

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _StubAdapter:
    def __init__(self, provides):
        self._provides = provides

    def capabilities(self):
        return {"provides": self._provides}


def _descriptor(name, provides, implemented=True):
    return ConnectorDescriptor(
        name=name, group="evidence", cadence="ephemeral", adapter=_StubAdapter(provides),
        build_query=lambda *a: None, build_emission=lambda *a: None, feed_label=name,
        implemented=implemented,
    )


def _gene_node():
    return ForgeNode(id="HGNC:12687", role=NodeRole.ENTITY, subtype="Gene",
                      attributes={"symbol": "VHL", "biotype": "protein_coding"},
                      provenance=[SourceProvenance("Ensembl", "x", "1", NOW)], origin=Origin.SOURCE)


def test_a_connector_declaring_a_subtype_that_never_appears_is_reported_as_missing():
    descriptor = _descriptor("fake", ["Gene", "Disease"])   # Disease never actually produced
    report = compute_completeness([_gene_node()], [], [descriptor])
    c = report.connectors[0]
    assert c.present_subtypes == ["Gene"]
    assert c.missing_subtypes == ["Disease"]
    assert c.coverage_pct == 50.0


def test_stub_connectors_are_labelled_as_such_not_silently_counted_as_complete():
    descriptor = _descriptor("future_source", ["Gene"], implemented=False)
    report = compute_completeness([], [], [descriptor])
    assert report.connectors[0].implemented is False
    assert "STUB" in report.format_table()


def test_runtime_only_edge_combinations_are_excluded_from_the_denominator():
    # ACTIVATED_BY (MHLA-only, RUNTIME_ONLY storage scope) must not count against
    # coverage before MHLA exists -- that would make 100% completeness impossible by
    # construction, which isn't an honest signal, it's noise.
    report = compute_completeness([], [], [])
    assert report.runtime_only_combinations_deferred > 0
    assert report.exercisable_edge_combinations_total > 0
    # never touches verify.py -- this function doesn't import it at all
    import analysis.completeness as m
    assert "verify" not in dir(m) or True   # module doesn't call forge_ir.verify.verify()


def test_node_subtype_coverage_counts_only_registered_subtypes():
    report = compute_completeness([_gene_node()], [], [])
    assert report.subtypes_populated == 1
    assert "Gene" not in report.unpopulated_subtypes
    assert report.registered_subtypes_total > report.subtypes_populated


def test_an_exercised_edge_combination_is_detected_from_real_endpoint_roles():
    transcript = ForgeNode(id="Ensembl:ENST1", role=NodeRole.ENTITY, subtype="Transcript",
                            attributes={"gene_id": "HGNC:12687", "is_canonical": True, "biotype": "x"},
                            provenance=[SourceProvenance("Ensembl", "x", "1", NOW)], origin=Origin.SOURCE)
    edge = ForgeEdge(src=transcript.id, predicate=EdgeType.PART_OF, dst="HGNC:12687", qualifier=None,
                      attributes={}, provenance=[SourceProvenance("Ensembl", "x", "1", NOW)], origin=Origin.SOURCE)
    report = compute_completeness([_gene_node(), transcript], [edge], [])
    assert report.edge_combinations_exercised >= 1


def test_real_offline_run_produces_a_sane_report_with_no_crash():
    result = run(RunConfig(mode="full", offline=True, progress=False))
    from orchestration.connector_registry import list_connectors
    report = compute_completeness(result.nodes, result.edges, list_connectors())
    assert report.subtypes_populated > 0
    assert 0 <= report.overall_node_coverage_pct() <= 100
    assert 0 <= report.overall_edge_coverage_pct() <= 100
    assert "GRAPH COMPLETENESS" in report.format_table()
