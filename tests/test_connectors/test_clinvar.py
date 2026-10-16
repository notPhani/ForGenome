"""Tests against the REAL ClinVar fixtures -- the clean VCV000002218 (1 VCV / 7 RCV /
22 SCV, no conflicts) and the conflicting VCV004558169 (2 SCVs, genuinely disagreeing,
confirmed to span two DIFFERENT RCVs -- ClinVar's own conflict concept is variant-wide,
not per-condition, which is exactly what the CONTRADICTS logic below has to get right."""
import json
import os

from forge_ir.core import EdgeType, NodeRole
from forge_ir.graph import ForgeEdge, ForgeNode
from connectors.clinvar import ClinVarParser, _build_trait_crosswalk, build_mapping_spec
from ingest.mapping import apply_mapping_spec
from datetime import datetime, timezone

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")


def _load_clean():
    with open(os.path.join(FIXTURES, "clinvar_efetch_VCV2218.xml")) as f:
        xml = f.read()
    with open(os.path.join(FIXTURES, "clinvar_esummary_VCV2218.json")) as f:
        summary = json.load(f)
    return xml, summary


def _load_conflicting():
    with open(os.path.join(FIXTURES, "clinvar_efetch_VCV4558169_conflicting.xml")) as f:
        xml = f.read()
    with open(os.path.join(FIXTURES, "clinvar_esummary_VCV4558169_conflicting.json")) as f:
        summary = json.load(f)
    return xml, summary


def _apply(xml, summary):
    parser = ClinVarParser()
    vcv = parser.parse_vcv(xml)
    rcvs = list(parser.parse_rcvs(xml))
    scvs = list(parser.parse_scvs(xml))
    crosswalk = _build_trait_crosswalk(summary)
    spec = build_mapping_spec(vcv, rcvs, scvs, crosswalk)
    return apply_mapping_spec(vcv, spec, primary_node_id=None, source="ClinVar",
                               source_record_id=vcv.accession, source_version=vcv.version,
                               retrieved_at=datetime.now(timezone.utc)), vcv, rcvs, scvs


def test_parses_real_hierarchy_counts():
    xml, summary = _load_clean()
    parser = ClinVarParser()
    vcv = parser.parse_vcv(xml)
    rcvs = list(parser.parse_rcvs(xml))
    scvs = list(parser.parse_scvs(xml))
    assert vcv.accession == "VCV000002218"
    assert vcv.canonical_spdi == "NC_000003.12:10149821:C:T"
    assert vcv.hgnc_id == "HGNC:12687"
    assert len(rcvs) == 7
    assert len(scvs) == 22


def test_hierarchy_uses_relates_to_never_part_of():
    results, *_ = _apply(*_load_clean())
    for e in results:
        if isinstance(e, ForgeEdge) and e.qualifier in ("contributes_to", "aggregates_into"):
            assert e.predicate == EdgeType.RELATES_TO


def test_three_distinct_claim_levels_not_collapsed():
    results, *_ = _apply(*_load_clean())
    claims = [r for r in results if isinstance(r, ForgeNode) and r.role == NodeRole.CLAIM]
    by_subtype = {}
    for c in claims:
        by_subtype.setdefault(c.subtype, 0)
        by_subtype[c.subtype] += 1
    assert by_subtype["ClinicalAssertion"] == 22          # SCV level
    assert by_subtype.get("AggregateClassification", 0) == 8  # 7 RCV + 1 VCV rollup, both same
                                                                 # subtype (scope attribute distinguishes them)


def test_scope_attribute_distinguishes_vcv_from_rcv_rollups():
    results, *_ = _apply(*_load_clean())
    aggregates = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "AggregateClassification"]
    scopes = {r.attributes["scope"] for r in aggregates}
    assert scopes == {"variant", "condition"}
    assert sum(1 for r in aggregates if r.attributes["scope"] == "variant") == 1
    assert sum(1 for r in aggregates if r.attributes["scope"] == "condition") == 7


def test_somatic_clinical_impact_is_a_separate_claim_not_merged():
    results, *_ = _apply(*_load_clean())
    impact_claims = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "ClinicalImpactClassification"]
    assert len(impact_claims) == 1  # confirmed real: only RCV000002303 has this axis
    assert impact_claims[0].attributes["tier"] == "Tier I - Strong"


def test_scv_rcv_linkage_coverage_matches_real_crosswalk_result():
    results, *_ = _apply(*_load_clean())
    contributes_edges = [r for r in results if isinstance(r, ForgeEdge) and r.qualifier == "contributes_to"]
    # 14 SCVs match a condition via the ESummary crosswalk (vs. 3 with EFetch's own xrefs alone),
    # but 2 of those have ContributesToAggregateClassification=false -- correctly excluded, since
    # emitting a "contributes_to" edge for a submission that doesn't contribute would be wrong.
    assert len(contributes_edges) == 12


def test_unmatched_scvs_still_fully_emitted_just_without_rcv_link():
    results, *_ = _apply(*_load_clean())
    scv_claims = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "ClinicalAssertion"]
    assert len(scv_claims) == 22  # all 22, not just the 14 that matched an RCV

    asserts_edges = [r for r in results if isinstance(r, ForgeEdge) and r.predicate == EdgeType.ASSERTS
                      and r.src.startswith("ClinVar:SCV")]
    assert len(asserts_edges) == 22  # every SCV still asserts on the Variant regardless of RCV match


def test_submitters_and_case_observations_created():
    results, *_ = _apply(*_load_clean())
    submitters = {r.id for r in results if isinstance(r, ForgeNode) and r.subtype == "Submitter"}
    assert len(submitters) >= 15  # 21 distinct real submitters, some may share an org id
    observations = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "CaseObservation"]
    assert len(observations) == 22


def test_clean_variant_has_no_contradicts_edges():
    results, *_ = _apply(*_load_clean())
    contradicts = [r for r in results if isinstance(r, ForgeEdge) and r.predicate == EdgeType.CONTRADICTS]
    assert contradicts == []  # confirmed real: this variant's review_status is "no conflicts"


def test_conflicting_variant_produces_contradicts_edge_across_different_rcvs():
    results, *_ = _apply(*_load_conflicting())
    contradicts = [r for r in results if isinstance(r, ForgeEdge) and r.predicate == EdgeType.CONTRADICTS]
    assert len(contradicts) == 1
    edge = contradicts[0]
    assert edge.origin.value == "DERIVED"  # computed by Forge, not literally in ClinVar's data
    values = {edge.attributes["classification_a"], edge.attributes["classification_b"]}
    assert values == {"Uncertain significance", "Likely pathogenic"}  # the real, confirmed disagreement

    # confirm this really does span two different RCVs, not one -- the whole point of
    # broadening CONTRADICTS beyond same-RCV siblings
    contributes = [r for r in results if isinstance(r, ForgeEdge) and r.qualifier == "contributes_to"]
    rcv_targets = {e.dst for e in contributes}
    assert len(rcv_targets) == 2
