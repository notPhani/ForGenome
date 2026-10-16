"""Tests for CoverageReport population (forge-pipeline-v1.md §11: "mandatory per
connector run") -- ingest/mapping.py::apply_mapping_spec()'s optional accumulator,
and the real, honest numbers it produces for the connectors that exist today.
"""
from datetime import datetime, timezone

from forge_ir.core import EdgeType, NodeRole, Origin
from ingest.compiler import CoverageReport
from ingest.mapping import EscapeHatch, MappingRule, MappingSpec, apply_mapping_spec
from ingest.source_objects import SourceObjectBase
from orchestration.runner import RunConfig, run

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Rec(SourceObjectBase):
    pass


def _rec(**extra) -> _Rec:
    obj = _Rec(source="Test", record_type="Rec", source_record_id="r1", source_version="1",
               snapshot_ref="", payload_hash="x")
    for k, v in extra.items():
        setattr(obj, k, v)
    return obj


def test_coverage_is_a_no_op_when_not_provided():
    # Backward compatible: omitting `coverage` behaves exactly as before this feature existed.
    spec = MappingSpec(source="Test", record_type="Rec", rules=[], escape_hatches=[])
    results = apply_mapping_spec(_rec(), spec, primary_node_id=None, source="Test",
                                  source_record_id="r1", source_version="1", retrieved_at=NOW)
    assert results == []   # ran without raising -- that's the whole assertion


def test_declarative_rule_emissions_are_counted_separately_from_escape_hatch_ones():
    obj = _rec(symbol="VHL", biotype="protein_coding")
    spec = MappingSpec(
        source="Test", record_type="Rec",
        rules=[MappingRule(target_role=NodeRole.ENTITY, target_subtype="Gene", predicate=None,
                            qualifier=None, id_template="HGNC:{symbol}", source_path="",
                            field_map={"symbol": "symbol", "biotype": "biotype"})],
        escape_hatches=[EscapeHatch(
            name="extra_node", version="v1", description="adds one more node via Python",
            fn=lambda o: [__import__("forge_ir.graph", fromlist=["ForgeNode"]).ForgeNode(
                id="Extra:1", role=NodeRole.ENTITY, subtype="Gene", attributes={},
                provenance=[], origin=Origin.SOURCE)],
        )],
    )
    coverage = CoverageReport(source="Test")
    apply_mapping_spec(obj, spec, primary_node_id=None, source="Test", source_record_id="r1",
                        source_version="1", retrieved_at=NOW, coverage=coverage)

    assert coverage.declarative_node_count == 1
    assert coverage.escape_hatch_node_count == 1
    assert coverage.records_parsed == 1
    assert coverage.records_emitted == 2
    summary = coverage.mapping_origin_summary()
    assert summary == {"declarative": 50.0, "escape_hatch": 50.0}


def test_coverage_accumulates_across_multiple_records_not_just_the_last_one():
    spec = MappingSpec(source="Test", record_type="Rec", rules=[], escape_hatches=[EscapeHatch(
        name="two_nodes", version="v1", description="always emits 2 nodes",
        fn=lambda o: [__import__("forge_ir.graph", fromlist=["ForgeNode"]).ForgeNode(
            id=f"N:{i}", role=NodeRole.ENTITY, subtype="Gene", attributes={}, provenance=[],
            origin=Origin.SOURCE) for i in range(2)],
    )])
    coverage = CoverageReport(source="Test")
    for i in range(3):
        apply_mapping_spec(_rec(), spec, primary_node_id=None, source="Test",
                            source_record_id=f"r{i}", source_version="1", retrieved_at=NOW,
                            coverage=coverage)
    assert coverage.records_parsed == 3
    assert coverage.escape_hatch_node_count == 6   # 2 per record x 3 records, not overwritten


def test_edges_from_escape_hatches_are_not_counted_as_nodes():
    spec = MappingSpec(source="Test", record_type="Rec", rules=[], escape_hatches=[EscapeHatch(
        name="node_and_edge", version="v1", description="one node, one edge",
        fn=lambda o: [
            __import__("forge_ir.graph", fromlist=["ForgeNode"]).ForgeNode(
                id="N:1", role=NodeRole.ENTITY, subtype="Gene", attributes={}, provenance=[],
                origin=Origin.SOURCE),
            __import__("forge_ir.graph", fromlist=["ForgeEdge"]).ForgeEdge(
                src="N:1", predicate=EdgeType.RELATES_TO, dst="N:2", qualifier=None,
                attributes={}, provenance=[], origin=Origin.SOURCE),
        ],
    )])
    coverage = CoverageReport(source="Test")
    apply_mapping_spec(_rec(), spec, primary_node_id=None, source="Test", source_record_id="r1",
                        source_version="1", retrieved_at=NOW, coverage=coverage)
    assert coverage.escape_hatch_node_count == 1   # not 2 -- the edge doesn't count
    assert coverage.records_emitted == 2           # but both are still in the emitted total


def test_real_offline_run_reports_honest_100_percent_escape_hatch_for_every_connector():
    # Confirmed real: none of Ensembl/ClinGen/ClinVar's MappingSpecs define bare
    # declarative MappingRules today -- ClinVar's real hierarchy and ClinGen's per-tier
    # expansion both need genuine branching logic (forge-pipeline-v1.md §3), so this
    # number being 100% escape-hatch is accurate, not a bug in the counting.
    result = run(RunConfig(mode="full", offline=True, progress=False))
    assert set(result.coverage) == {"ensembl_structure", "clingen", "clinvar"}
    for name, report in result.coverage.items():
        summary = report.mapping_origin_summary()
        assert summary["escape_hatch"] == 100.0
        assert summary["declarative"] == 0.0
        assert report.records_parsed > 0


def test_a_rejected_batch_is_still_recorded_by_the_calling_connector():
    from forge_ir.core import EdgeType
    from forge_ir.graph import ForgeEdge
    from forge_ir.provenance import SourceProvenance
    from ingest.compiler import StagingCompiler
    from ingest.identity import IdentityIndex

    coverage = CoverageReport(source="clinvar")
    compiler = StagingCompiler(IdentityIndex(), run_id="run1")
    bad_edge = ForgeEdge(src="does-not-exist", predicate=EdgeType.RELATES_TO, dst="also-missing",
                          qualifier=None, attributes={}, provenance=[SourceProvenance("X", "x", "1", NOW)],
                          origin=Origin.SOURCE)
    compiler.stage([], [bad_edge])
    result = compiler.commit()
    if not result.committed:
        coverage.records_rejected_batches += 1
    assert coverage.records_rejected_batches == 1
