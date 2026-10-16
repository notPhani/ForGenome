"""Tests against the REAL ClinGen VHL VCEP fixture -- 28 criteria, 5 strength tiers
each, PM2's real single-applicable-tier structure and its documented singleton fallback."""
import json
import os

from forge_ir.core import EdgeType, NodeRole
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.requirement import EvaluationMode
from connectors.clingen import ClinGenParser, CRITERIA_MAPPING_SPEC
from ingest.mapping import apply_mapping_spec
from datetime import datetime, timezone

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")


def _load():
    with open(os.path.join(FIXTURES, "clingen_vhl_vcep_GN078_v1.1.json"), encoding="utf-8") as f:
        return json.load(f)


def test_parses_all_28_real_criteria():
    criteria = list(ClinGenParser().parse(_load()))
    assert len(criteria) == 28
    labels = {c.label for c in criteria}
    assert "PM2" in labels and "PVS1" in labels and "BP4" in labels


def _apply(criterion):
    return apply_mapping_spec(criterion, CRITERIA_MAPPING_SPEC, primary_node_id=None,
                               source="ClinGen", source_record_id=criterion.source_record_id,
                               source_version=criterion.version, retrieved_at=datetime.now(timezone.utc))


def test_pm2_preserves_all_5_tiers_including_inapplicable_ones():
    pm2 = next(c for c in ClinGenParser().parse(_load()) if c.label == "PM2")
    results = _apply(pm2)
    requirements = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "Requirement"]
    assert len(requirements) == 5  # confirmed real count -- not just the 1 applicable tier

    applicable = [r for r in requirements if r.attributes["applicability"] == "Applicable"]
    assert len(applicable) == 1
    assert applicable[0].attributes["strength"] == "Supporting"


def test_pm2_requirement_is_mechanical_with_real_constraints_and_fallback():
    pm2 = next(c for c in ClinGenParser().parse(_load()) if c.label == "PM2")
    results = _apply(pm2)
    supporting = next(
        r for r in results if isinstance(r, ForgeNode) and r.subtype == "Requirement"
        and r.attributes["strength"] == "Supporting"
    )
    assert supporting.attributes["evaluation_mode"] == "MECHANICAL"
    constraints = supporting.attributes["constraints"]
    assert constraints["metric"] == "faf95_popmax"
    assert constraints["value"] == 0.00000156
    assert constraints["fallback_if_unavailable"]["condition"] == "ac_le_1"


def test_pvs1_is_narrative_not_pretending_to_be_mechanical():
    pvs1 = next(c for c in ClinGenParser().parse(_load()) if c.label == "PVS1")
    results = _apply(pvs1)
    requirements = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "Requirement"]
    assert len(requirements) == 5
    assert all(r.attributes["evaluation_mode"] == "NARRATIVE" for r in requirements)
    assert all(r.attributes["constraints"] is None for r in requirements)
    # source_text preserved even though it's not mechanically evaluable
    assert any(len(r.attributes["source_text"]) > 0 for r in requirements)


def test_bp4_classified_composite():
    bp4 = next(c for c in ClinGenParser().parse(_load()) if c.label == "BP4")
    results = _apply(bp4)
    requirements = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "Requirement"]
    assert all(r.attributes["evaluation_mode"] == "COMPOSITE" for r in requirements)


def test_criterion_applies_to_gene_and_requires_each_requirement():
    pm2 = next(c for c in ClinGenParser().parse(_load()) if c.label == "PM2")
    results = _apply(pm2)

    applies_to = [r for r in results if isinstance(r, ForgeEdge) and r.predicate == EdgeType.APPLIES_TO]
    assert len(applies_to) == 1
    assert applies_to[0].dst == "HGNC:12687"

    requires = [r for r in results if isinstance(r, ForgeEdge) and r.predicate == EdgeType.REQUIRES]
    assert len(requires) == 5

    criterion_node = next(r for r in results if isinstance(r, ForgeNode) and r.subtype == "ACMGCriterion")
    assert criterion_node.id == "ClinGen:GN078:PM2:1.1"
    assert all(e.src == criterion_node.id for e in requires)
