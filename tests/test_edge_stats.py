"""Tests for analysis/edge_stats.py.

The point of these is the *derivation*, not the arithmetic: the report has to
distinguish an edge type that dominates through fan-out (few sources, many edges each)
from one that dominates through breadth (many sources, one edge each), because those are
different facts about the graph and only one of them is a modelling choice we'd defend.
"""
from datetime import datetime, timezone

from analysis.edge_stats import compute_edge_stats
from forge_ir.core import EdgeType, NodeRole, Origin
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.provenance import DerivedProvenance, SourceProvenance

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _prov(source="ClinVar"):
    return [SourceProvenance(source, "rec", "1", NOW)]


def _criterion(n: int) -> ForgeNode:
    return ForgeNode(id=f"ClinGen:GN078:PM{n}:1.1", role=NodeRole.RULE, subtype="ACMGCriterion",
                      attributes={"label": f"PM{n}", "guideline_id": "GN078"},
                      provenance=_prov("ClinGen"), origin=Origin.SOURCE)


def _requirement(criterion: str, ordinal: int) -> ForgeNode:
    return ForgeNode(id=f"{criterion}:req:{ordinal}", role=NodeRole.RULE, subtype="Requirement",
                      attributes={"evaluation_mode": "MECHANICAL"},
                      provenance=_prov("ClinGen"), origin=Origin.SOURCE)


def _assertion(n: int) -> ForgeNode:
    return ForgeNode(id=f"ClinVar:SCV{n}", role=NodeRole.CLAIM, subtype="ClinicalAssertion",
                      attributes={"review_status": "x", "classification": "Pathogenic"},
                      provenance=_prov(), origin=Origin.SOURCE)


def _variant() -> ForgeNode:
    return ForgeNode(id="SPDI:NC_000003.12:10149821:C:T", role=NodeRole.ENTITY, subtype="Variant",
                      attributes={"assembly": "GRCh38", "ref": "C", "alt": "T"},
                      provenance=_prov(), origin=Origin.SOURCE)


def _edge(src, predicate, dst, qualifier=None, origin=Origin.SOURCE, prov=None):
    return ForgeEdge(src=src, predicate=predicate, dst=dst, qualifier=qualifier, attributes={},
                      provenance=prov or _prov(), origin=origin)


def _fan_out_graph():
    """3 criteria x 5 strength-tier Requirements each = 15 REQUIRES edges from 3 sources."""
    criteria = [_criterion(i) for i in (1, 2, 3)]
    requirements, edges = [], []
    for c in criteria:
        for ordinal in range(5):
            r = _requirement(c.id, ordinal)
            requirements.append(r)
            edges.append(_edge(c.id, EdgeType.REQUIRES, r.id))
    return criteria + requirements, edges


def test_fan_out_and_breadth_are_reported_as_different_mechanisms():
    nodes, edges = _fan_out_graph()
    variant = _variant()
    assertions = [_assertion(i) for i in range(12)]
    nodes += [variant] + assertions
    edges += [_edge(a.id, EdgeType.ASSERTS, variant.id) for a in assertions]

    report = compute_edge_stats(nodes, edges)
    by_predicate = {p.predicate: p for p in report.predicates}

    requires = by_predicate["REQUIRES"]
    assert requires.count == 15
    assert requires.shapes[0].distinct_src_nodes == 3
    assert requires.shapes[0].edges_per_source_node == 5.0
    assert "fan-out" in requires.driver()

    asserts = by_predicate["ASSERTS"]
    assert asserts.count == 12
    assert asserts.shapes[0].distinct_src_nodes == 12
    assert asserts.shapes[0].edges_per_source_node == 1.0
    assert "breadth" in asserts.driver()


def test_predicates_are_ranked_by_edge_count():
    nodes, edges = _fan_out_graph()
    variant = _variant()
    nodes.append(variant)
    edges.append(_edge(nodes[0].id, EdgeType.APPLIES_TO, variant.id))
    report = compute_edge_stats(nodes, edges)
    assert [p.predicate for p in report.predicates] == ["REQUIRES", "APPLIES_TO"]
    assert report.predicates[0].share_pct > report.predicates[1].share_pct


def test_unused_ontology_predicates_are_named():
    nodes, edges = _fan_out_graph()
    report = compute_edge_stats(nodes, edges)
    assert "CONTRADICTS" in report.unused_predicates
    assert "REQUIRES" not in report.unused_predicates


def test_shapes_separate_the_same_predicate_by_qualifier_and_endpoint_subtype():
    variant, assertion = _variant(), _assertion(1)
    submitter = ForgeNode(id="ClinVar:Submitter:500060", role=NodeRole.ENTITY, subtype="Submitter",
                           attributes={"name": "OMIM"}, provenance=_prov(), origin=Origin.SOURCE)
    disease = ForgeNode(id="MedGen:C0019562", role=NodeRole.ENTITY, subtype="Disease",
                         attributes={"name": "von Hippel-Lindau syndrome"},
                         provenance=_prov(), origin=Origin.SOURCE)
    aggregate = ForgeNode(id="ClinVar:RCV000002331", role=NodeRole.CLAIM, subtype="AggregateClassification",
                           attributes={"scope": "condition", "review_status": "x", "description": "Pathogenic"},
                           provenance=_prov(), origin=Origin.SOURCE)
    nodes = [variant, assertion, submitter, disease, aggregate]
    edges = [
        _edge(assertion.id, EdgeType.RELATES_TO, submitter.id, "submitted_by"),
        _edge(assertion.id, EdgeType.RELATES_TO, aggregate.id, "contributes_to"),
        _edge(aggregate.id, EdgeType.RELATES_TO, disease.id, "for_condition"),
    ]
    report = compute_edge_stats(nodes, edges)
    relates = report.predicates[0]
    assert relates.predicate == "RELATES_TO"
    assert relates.count == 3
    assert len(relates.shapes) == 3   # one shape per (qualifier, endpoint subtype) combination
    assert set(relates.qualifiers) == {"submitted_by", "contributes_to", "for_condition"}


def test_edges_into_the_resident_persistent_layer_are_labelled_not_dropped():
    # An ephemeral run points at Gene nodes it never created. Those endpoints aren't in
    # the node list, and the stats must still attribute the edge rather than silently
    # skipping it -- otherwise an ephemeral run's edge counts wouldn't reconcile.
    variant = _variant()
    edges = [_edge(variant.id, EdgeType.RELATES_TO, "HGNC:12687", "in_gene")]
    report = compute_edge_stats([variant], edges)
    assert report.total_edges == 1
    shape = report.predicates[0].shapes[0]
    assert shape.dst_subtype == "<resident>"
    # Variant is an ENTITY, so layer_for() puts it in CORE_BIOLOGY -- the *claims about*
    # a variant are Evidence, the variant itself is not.
    assert report.predicates[0].layer_pairs == {"CORE_BIOLOGY→RESIDENT": 1}


def test_derived_origin_edges_are_counted_separately_from_source_edges():
    a, b = _assertion(1), _assertion(2)
    contradicts = _edge(a.id, EdgeType.CONTRADICTS, b.id, origin=Origin.DERIVED,
                         prov=[DerivedProvenance("diff", (a.id, b.id), (), "v1", NOW)])
    report = compute_edge_stats([a, b], [contradicts])
    assert report.derived_edges == 1
    assert report.predicates[0].origins == {"DERIVED": 1}
