"""Tests for ingest/mapping.py against synthetic SourceObjects -- no real connector
data needed here, this is purely mechanical: does the declarative rule engine map
fields correctly, does source_path resolve nested/list-valued paths, do escape hatches
run alongside declarative rules."""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from forge_ir.core import EdgeType, NodeRole
from ingest.mapping import EscapeHatch, MappingRule, MappingSpec, apply_mapping_spec, resolve_source_path

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass
class FakeObserved:
    origin: str
    method_type: str


@dataclass
class FakeRecord:
    accession: str
    classification: str
    observed_in: list  # list[FakeObserved]


def test_resolve_source_path_flat_attribute():
    obj = FakeRecord(accession="X1", classification="Pathogenic", observed_in=[])
    assert resolve_source_path(obj, "classification") == "Pathogenic"


def test_resolve_source_path_empty_path_returns_object_itself():
    obj = FakeRecord(accession="X1", classification="Pathogenic", observed_in=[])
    assert resolve_source_path(obj, "") is obj


def test_resolve_source_path_list_valued():
    obs = [FakeObserved("germline", "clinical testing")]
    obj = FakeRecord(accession="X1", classification="Pathogenic", observed_in=obs)
    assert resolve_source_path(obj, "observed_in") == obs


def test_apply_mapping_spec_emits_a_node_from_a_declarative_rule():
    obj = FakeRecord(accession="SCV001", classification="Pathogenic", observed_in=[])
    spec = MappingSpec(
        source="TestSource", record_type="ClinicalAssertion",
        rules=[
            MappingRule(
                target_role=NodeRole.CLAIM, target_subtype="ClinicalAssertion",
                predicate=None, qualifier=None,
                id_template="ClinVar:{accession}", source_path="",
                field_map={"accession": "accession", "classification": "classification"},
            ),
        ],
    )
    results = apply_mapping_spec(obj, spec, primary_node_id=None, source="ClinVar",
                                  source_record_id="SCV001", source_version="44", retrieved_at=NOW)
    assert len(results) == 1
    node = results[0]
    assert node.id == "ClinVar:SCV001"
    assert node.role == NodeRole.CLAIM and node.subtype == "ClinicalAssertion"
    assert node.attributes == {"accession": "SCV001", "classification": "Pathogenic"}


def test_apply_mapping_spec_emits_one_edge_per_list_item():
    obs = [FakeObserved("germline", "clinical testing"), FakeObserved("somatic", "research")]
    obj = FakeRecord(accession="SCV001", classification="Pathogenic", observed_in=obs)
    spec = MappingSpec(
        source="ClinVar", record_type="ClinicalAssertion",
        rules=[
            MappingRule(
                target_role=None, target_subtype=None,
                predicate=EdgeType.OBSERVES, qualifier=None,
                id_template="obs:{origin}", source_path="observed_in",
                field_map={"origin": "origin", "method_type": "method_type"},
            ),
        ],
    )
    results = apply_mapping_spec(obj, spec, primary_node_id="ClinVar:SCV001", source="ClinVar",
                                  source_record_id="SCV001", source_version="44", retrieved_at=NOW)
    assert len(results) == 2
    assert {e.dst for e in results} == {"obs:germline", "obs:somatic"}
    assert all(e.src == "ClinVar:SCV001" and e.predicate == EdgeType.OBSERVES for e in results)


def test_drop_action_rule_is_skipped():
    obj = FakeRecord(accession="X1", classification="Pathogenic", observed_in=[])
    spec = MappingSpec(
        source="Ensembl", record_type="Variation",
        rules=[
            MappingRule(
                target_role=NodeRole.CLAIM, target_subtype="ClinicalAssertion",
                predicate=None, qualifier=None, id_template="x:{accession}", source_path="",
                field_map={"accession": "accession"},
                action="DROP", drop_reason="duplicate_non_authoritative_clinical_assertion",
            ),
        ],
    )
    results = apply_mapping_spec(obj, spec, primary_node_id=None, source="Ensembl",
                                  source_record_id="X1", source_version=None, retrieved_at=NOW)
    assert results == []


def test_escape_hatch_runs_and_is_named_and_versioned():
    calls = []

    def hatch_fn(obj):
        calls.append(obj.accession)
        return []

    hatch = EscapeHatch(name="test_hatch", version="v1", description="records the accession seen", fn=hatch_fn)
    obj = FakeRecord(accession="SCV001", classification="Pathogenic", observed_in=[])
    spec = MappingSpec(source="ClinVar", record_type="ClinicalAssertion", rules=[], escape_hatches=[hatch])

    apply_mapping_spec(obj, spec, primary_node_id=None, source="ClinVar",
                        source_record_id="SCV001", source_version="44", retrieved_at=NOW)
    assert calls == ["SCV001"]
    assert hatch.name == "test_hatch" and hatch.version == "v1"
