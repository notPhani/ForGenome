"""Deliberately-broken synthetic fixtures, one per verification check.
No real connector data here on purpose -- forge-pipeline-v1.md's own stated
discipline is that verify.py proves it catches engineered failures BEFORE it
ever touches a real payload.
"""
from datetime import datetime, timezone

import pytest

from forge_ir.core import EdgeType, NodeRole, Origin
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.provenance import DerivedProvenance, RuntimeProvenance, SourceProvenance
from forge_ir.verify import check_record_conservation, verify

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _source_node(id_, role, subtype, lod=1, source_record_id="rec1", attrs=None) -> ForgeNode:
    return ForgeNode(
        id=id_, role=role, subtype=subtype, attributes=attrs or {},
        provenance=[SourceProvenance("TestSource", source_record_id, "v1", NOW)],
        origin=Origin.SOURCE, lod=lod,
    )


def _valid_gene_and_variant() -> tuple[ForgeNode, ForgeNode]:
    gene = _source_node("HGNC:12687", NodeRole.ENTITY, "Gene", attrs={"symbol": "VHL", "biotype": "protein_coding"})
    variant = _source_node("SPDI:x:1:C:T", NodeRole.ENTITY, "Variant", attrs={"assembly": "GRCh38", "ref": "C", "alt": "T"})
    return gene, variant


def test_valid_batch_passes_all_checks():
    gene, variant = _valid_gene_and_variant()
    edge = ForgeEdge(
        src=variant.id, predicate=EdgeType.RELATES_TO, dst=gene.id, qualifier="in_gene",
        attributes={}, provenance=[SourceProvenance("TestSource", "rec1", "v1", NOW)], origin=Origin.SOURCE,
    )
    report = verify([gene, variant], [edge])
    assert report.valid, report.all_errors()


def test_type_correctness_catches_unregistered_subtype():
    # Construction itself already refuses an unregistered subtype (ForgeNode.__post_init__
    # calls layer_for(), which raises) -- that's the first line of defense. verify()'s own
    # type-correctness check is defense-in-depth for data that bypassed normal construction
    # (e.g. deserialized from storage), so we simulate that by building the object without
    # going through __init__/__post_init__.
    bad = object.__new__(ForgeNode)
    bad.id, bad.role, bad.subtype = "bad:1", NodeRole.ENTITY, "NotARealSubtype"
    bad.attributes, bad.provenance, bad.origin, bad.lod = {}, [SourceProvenance("TestSource", "rec1", "v1", NOW)], Origin.SOURCE, 1
    bad.layer, bad.storage_scope = None, None

    report = verify([bad], [])
    assert not report.valid
    assert any("unregistered" in e for e in report.type_errors)


def test_construction_itself_refuses_unregistered_subtype():
    with pytest.raises(ValueError, match="Unregistered"):
        _source_node("bad:1", NodeRole.ENTITY, "NotARealSubtype")


def test_type_correctness_catches_disallowed_edge_combo():
    # SCV -> RCV must be RELATES_TO[contributes_to], never PART_OF (review round: fixed exactly
    # because this was the wrong predicate in an earlier draft) -- confirm the wrong one is rejected.
    scv = _source_node("scv:1", NodeRole.CLAIM, "ClinicalAssertion", attrs={"review_status": "x", "classification": "x"})
    rcv = _source_node("rcv:1", NodeRole.CLAIM, "AggregateClassification",
                        attrs={"scope": "condition", "description": "x", "review_status": "x"})
    bad_edge = ForgeEdge(
        src=scv.id, predicate=EdgeType.PART_OF, dst=rcv.id, qualifier=None,
        attributes={}, provenance=[SourceProvenance("ClinVar", "rec1", "v1", NOW)], origin=Origin.SOURCE,
    )
    report = verify([scv, rcv], [bad_edge])
    assert not report.valid
    assert any("not in" in e for e in report.type_errors)


def test_referential_integrity_catches_dangling_edge():
    gene, variant = _valid_gene_and_variant()
    dangling = ForgeEdge(
        src=variant.id, predicate=EdgeType.RELATES_TO, dst="ENTITY:does-not-exist", qualifier="in_gene",
        attributes={}, provenance=[SourceProvenance("TestSource", "rec1", "v1", NOW)], origin=Origin.SOURCE,
    )
    report = verify([gene, variant], [dangling])
    assert not report.valid
    assert any("does not resolve" in e for e in report.referential_errors)


def test_record_conservation_catches_mismatch():
    assert check_record_conservation(records_in=22, committed=22, quarantined=0, rejected=0) == []
    errors = check_record_conservation(records_in=22, committed=20, quarantined=0, rejected=0)
    assert len(errors) == 1
    assert "violated" in errors[0]


def test_layer_consistency_catches_tampered_layer():
    gene, _ = _valid_gene_and_variant()
    gene.layer = None  # simulate corrupted/deserialized-without-revalidation data
    report = verify([gene], [])
    assert not report.valid
    assert any("recomputed layer" in e for e in report.layer_errors)


def test_multi_provenance_integrity_catches_empty_provenance():
    node = ForgeNode(
        id="bad:1", role=NodeRole.ENTITY, subtype="Gene", attributes={}, provenance=[], origin=Origin.SOURCE,
    )
    report = verify([node], [])
    assert not report.valid
    assert any("provenance list is empty" in e for e in report.provenance_errors)


def test_multi_provenance_integrity_catches_wrong_provenance_type():
    node = ForgeNode(
        id="bad:1", role=NodeRole.ENTITY, subtype="Gene", attributes={},
        provenance=[RuntimeProvenance("inv:1", "test", NOW)],  # wrong type for origin=SOURCE
        origin=Origin.SOURCE,
    )
    report = verify([node], [])
    assert not report.valid
    assert any("expected SourceProvenance" in e for e in report.provenance_errors)


def test_represents_edge_requires_confidence_fields_at_construction():
    # Enforced in ForgeEdge.__post_init__, not verify() -- a REPRESENTS edge missing
    # confidence/match_method/resolver_version can't even be constructed.
    with pytest.raises(ValueError, match="REPRESENTS edge"):
        ForgeEdge(
            src="ENTITY:a", predicate=EdgeType.REPRESENTS, dst="ENTITY:b", qualifier=None,
            attributes={}, provenance=[SourceProvenance("TestSource", "rec1", "v1", NOW)], origin=Origin.SOURCE,
        )


def test_hydration_pointer_integrity_catches_missing_source_record_id():
    node = ForgeNode(
        id="bad:1", role=NodeRole.ENTITY, subtype="Gene", attributes={},
        provenance=[SourceProvenance("TestSource", "", "v1", NOW)],  # empty source_record_id
        origin=Origin.SOURCE, lod=1,
    )
    report = verify([node], [])
    assert not report.valid
    assert any("no resolvable source_record_id" in e for e in report.hydration_errors)


def test_hydration_pointer_integrity_derived_needs_lineage():
    node = ForgeNode(
        id="derived:1", role=NodeRole.RULE, subtype="Requirement",
        attributes={"constraints": {}, "evidence_type": "x"},
        provenance=[DerivedProvenance("rule_v1", (), (), "compiler_v1", NOW)],  # empty input_node_ids
        origin=Origin.DERIVED, lod=1,
    )
    report = verify([node], [])
    assert not report.valid
    assert any("no derivation lineage" in e for e in report.hydration_errors)


def test_hydration_pointer_integrity_skips_fully_hydrated_nodes():
    node = ForgeNode(
        id="ok:1", role=NodeRole.ENTITY, subtype="Gene", attributes={},
        provenance=[SourceProvenance("TestSource", "", "v1", NOW)],  # would fail if lod < 4
        origin=Origin.SOURCE, lod=4,
    )
    report = verify([node], [])
    assert report.hydration_errors == []


def test_connectivity_catches_orphaned_evidence():
    obs = _source_node("obs:1", NodeRole.OBSERVATION, "PopulationFrequency",
                        attrs={"ac": 1, "an": 1, "af": 0.1, "dataset": "x"})
    # no edges at all -- this OBSERVATION (EVIDENCE layer) has no path to any CORE_BIOLOGY node
    report = verify([obs], [])
    assert not report.valid
    assert any("orphaned evidence" in e for e in report.connectivity_errors)


def test_connectivity_passes_when_evidence_observes_a_variant():
    gene, variant = _valid_gene_and_variant()
    obs = _source_node("obs:1", NodeRole.OBSERVATION, "PopulationFrequency",
                        attrs={"ac": 1, "an": 1, "af": 0.1, "dataset": "x"})
    edge = ForgeEdge(
        src=obs.id, predicate=EdgeType.OBSERVES, dst=variant.id, qualifier=None,
        attributes={}, provenance=[SourceProvenance("gnomAD", "rec1", "v1", NOW)], origin=Origin.SOURCE,
    )
    report = verify([gene, variant, obs], [edge])
    assert report.connectivity_errors == []


def test_storage_scope_integrity_catches_runtime_only_role_with_source_origin():
    gene, _ = _valid_gene_and_variant()
    gene.role = NodeRole.INVESTIGATION  # simulate corrupted/deserialized data bypassing __post_init__
    report = verify([gene], [])
    assert not report.valid
    assert any("RUNTIME_ONLY role" in e for e in report.storage_scope_errors)


def test_runtime_only_role_cannot_be_constructed_with_source_origin():
    # The construction-time invariant in ForgeNode.__post_init__ is the first line of
    # defense -- verify()'s storage-scope check (tested above) is defense-in-depth for
    # data that bypasses normal construction (e.g. loaded from storage).
    with pytest.raises(ValueError, match="RUNTIME_ONLY"):
        ForgeNode(
            id="inv:1", role=NodeRole.INVESTIGATION, subtype="Query",
            attributes={}, provenance=[SourceProvenance("TestSource", "rec1", "v1", NOW)],
            origin=Origin.SOURCE,
        )
