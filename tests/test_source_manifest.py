"""Tests for SourceManifest/SOURCED_FROM materialization (forge-pipeline-v1.md §5:
"mandatory, not design-only") -- forge_ir/core.py::source_manifest_canonical_id,
connectors/common.py::build_source_manifest_node, and
orchestration/connector_registry.py::tag_with_source_manifest, the function that wires
the two into every connector's Emission after build_emission() runs.
"""
from datetime import datetime, timezone

from forge_ir.core import EdgeType, NodeRole, Origin, source_manifest_canonical_id
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.provenance import SourceProvenance
from connectors.common import build_source_manifest_node
from orchestration.connector_registry import ConnectorDescriptor, Emission, tag_with_source_manifest

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_source_manifest_id_is_stable_for_the_same_source_dataset_version():
    a = source_manifest_canonical_id("ClinVar", "E-utilities", "44")
    b = source_manifest_canonical_id("ClinVar", "E-utilities", "44")
    assert a == b
    assert a.startswith("Source:ClinVar:E-utilities:")


def test_source_manifest_id_handles_a_source_that_never_self_reports_a_version():
    # Real, confirmed: Ensembl/gnomAD responses carry no release-level version.
    manifest_id = source_manifest_canonical_id("gnomAD", "gnomad_r4", None)
    assert manifest_id == "Source:gnomAD:gnomad_r4:unversioned"


def test_build_source_manifest_node_is_the_right_subtype_and_layer():
    node = build_source_manifest_node("ClinVar", "E-utilities", "44", NOW)
    assert node.role == NodeRole.ENTITY
    assert node.subtype == "SourceManifest"
    assert node.attributes["source"] == "ClinVar"
    assert node.attributes["source_version"] == "44"


def _claim(claim_id: str) -> ForgeNode:
    return ForgeNode(id=claim_id, role=NodeRole.CLAIM, subtype="ClinicalAssertion",
                      attributes={"review_status": "x", "classification": "x"},
                      provenance=[SourceProvenance("ClinVar", claim_id, "44", NOW)], origin=Origin.SOURCE)


def _entity(entity_id: str, subtype: str) -> ForgeNode:
    return ForgeNode(id=entity_id, role=NodeRole.ENTITY, subtype=subtype,
                      attributes={"symbol": "VHL", "biotype": "protein_coding"} if subtype == "Gene" else {},
                      provenance=[SourceProvenance("Ensembl", entity_id, "12", NOW)], origin=Origin.SOURCE)


def _descriptor(name: str, manifest: dict) -> ConnectorDescriptor:
    class _StubAdapter:
        def manifest(self):
            return manifest

    return ConnectorDescriptor(
        name=name, group="evidence", cadence="ephemeral", adapter=_StubAdapter(),
        build_query=lambda *a: None, build_emission=lambda *a: Emission(), feed_label=name,
    )


def test_every_claim_gets_exactly_one_sourced_from_edge():
    emission = Emission()
    emission.add("ClinVar", [_claim("ClinVar:SCV1"), _claim("ClinVar:SCV2")])
    descriptor = _descriptor("clinvar", {"source": "ClinVar", "dataset": "E-utilities", "schema_version": "1"})

    tag_with_source_manifest(emission, descriptor, NOW)

    sourced_from = [e for e in emission.edges if e.predicate == EdgeType.SOURCED_FROM]
    assert len(sourced_from) == 2
    assert {e.src for e in sourced_from} == {"ClinVar:SCV1", "ClinVar:SCV2"}
    manifest_ids = {e.dst for e in sourced_from}
    assert len(manifest_ids) == 1   # both claims point at the SAME manifest node
    manifest_node = next(n for n in emission.nodes if n.id in manifest_ids)
    assert manifest_node.subtype == "SourceManifest"


def test_non_claim_roles_get_no_sourced_from_edge():
    # Real and correct: ALLOWED_EDGE_COMBINATIONS only permits CLAIM -> SOURCED_FROM ->
    # SourceManifest. Ensembl's gene-structure feed is all ENTITY (Gene/Transcript/...),
    # so it must produce zero SOURCED_FROM edges, not a verify()-rejecting ontology violation.
    emission = Emission()
    emission.add("Ensembl gene structure", [_entity("HGNC:12687", "Gene")])
    descriptor = _descriptor("ensembl_structure", {"source": "Ensembl", "dataset": "REST API",
                                                    "schema_version": "1"})

    tag_with_source_manifest(emission, descriptor, NOW)

    assert [e for e in emission.edges if e.predicate == EdgeType.SOURCED_FROM] == []
    assert all(n.subtype != "SourceManifest" for n in emission.nodes)   # no manifest built at all


def test_sourced_from_is_a_legal_edge_combination_per_the_frozen_ontology():
    from forge_ir.core import is_allowed_edge
    assert is_allowed_edge(NodeRole.CLAIM, "ClinicalAssertion", EdgeType.SOURCED_FROM, None,
                            NodeRole.ENTITY, "SourceManifest")


def test_feed_tagging_covers_the_manifest_node_and_its_edges():
    emission = Emission()
    emission.add("ClinVar", [_claim("ClinVar:SCV1")])
    descriptor = _descriptor("clinvar", {"source": "ClinVar", "dataset": "E-utilities", "schema_version": "1"})

    tag_with_source_manifest(emission, descriptor, NOW)

    manifest_node = next(n for n in emission.nodes if n.subtype == "SourceManifest")
    assert emission.feed_for_node(manifest_node) == "clinvar"
    sourced_from = next(e for e in emission.edges if e.predicate == EdgeType.SOURCED_FROM)
    assert emission.feed_for_edge(sourced_from) == "clinvar"
