"""Orchestration tests -- mocked identity lookups only, no real network calls.
Confirms plan_load() produces the expected DAG shape (Ensembl/ClinVar/ClinGen
parallel, gnomAD depending on resolved identity) purely by inspection, before
any I/O happens."""
import pytest

from orchestration.connector_planner import STANDARD_POLICY, ConnectorPlanner
from orchestration.load_plan import plan_load
from orchestration.scope_resolver import ScopeResolver
from ingest.identity import IdentityIndex


def test_scope_resolver_resolves_known_anchor():
    resolver = ScopeResolver(identity_lookup={"VHL": "HGNC:12687"})
    plan = resolver.resolve("VHL", scope="gene")
    assert plan.canonical_anchors == ["HGNC:12687"]
    assert plan.gap_fill_required == []


def test_scope_resolver_flags_unresolved_anchor_as_gap_fill():
    resolver = ScopeResolver(identity_lookup={})
    plan = resolver.resolve("SOMENEWGENE", scope="gene")
    assert plan.canonical_anchors == ["SOMENEWGENE"]
    assert plan.gap_fill_required == ["SOMENEWGENE"]


def test_scope_resolver_rejects_deferred_scope():
    resolver = ScopeResolver()
    with pytest.raises(NotImplementedError, match="deferred"):
        resolver.resolve("VHL", scope="pathway")


def test_scope_resolver_rejects_experimental_scope():
    resolver = ScopeResolver()
    with pytest.raises(NotImplementedError, match="experimental"):
        resolver.resolve("VHL", scope="gene_family")


def test_gene_scope_plan_includes_biology_evidence_and_operational_connectors():
    resolver = ScopeResolver(identity_lookup={"VHL": "HGNC:12687"})
    planner = ConnectorPlanner(IdentityIndex(), rate_policies={})
    plan = plan_load("VHL", "gene", STANDARD_POLICY, resolver, planner)

    connectors_used = {t.connector for t in plan.tasks}
    # "ensembl" split into "ensembl_structure" (Core Biology) vs "ensembl_vep" (Evidence,
    # gene-scope N/A -- see connectors/ensembl.py::_vep_build_query) once the registry made
    # Ensembl's two feeds two separate connectors instead of one name covering both.
    assert connectors_used == {"ensembl_structure", "clinvar", "clingen"}  # gnomAD/VEP are variant-level


def test_gene_scope_plan_has_no_dependencies_all_independent():
    resolver = ScopeResolver(identity_lookup={"VHL": "HGNC:12687"})
    planner = ConnectorPlanner(IdentityIndex(), rate_policies={})
    plan = plan_load("VHL", "gene", STANDARD_POLICY, resolver, planner)
    assert plan.dependencies == ()
    assert len(plan.task_tiers()) == 1  # everything runs in one tier -- no ordering needed


def test_variant_scope_plan_makes_gnomad_hard_depend_on_identity():
    resolver = ScopeResolver(identity_lookup={"rs5030820": "SPDI:NC_000003.12:10149821:C:T"})
    planner = ConnectorPlanner(IdentityIndex(), rate_policies={})
    plan = plan_load("rs5030820", "variant", STANDARD_POLICY, resolver, planner)

    connectors_used = {t.connector for t in plan.tasks}
    assert connectors_used == {"ensembl_vep", "clinvar", "gnomad"}

    gnomad_task = next(t for t in plan.tasks if t.connector == "gnomad")
    hard_deps = [d for d in plan.dependencies if d[0] == gnomad_task.task_id and d[2] == "HARD"]
    # Reads connectors/gnomad.py's own `requires=("clinvar",)` now, rather than the old
    # hardcoded "depends on every ensembl/clinvar task" special case -- gnomAD only
    # actually needs ClinVar's resolved variant identity, not Ensembl's structure, so
    # this is 1 real dependency, not 2.
    assert len(hard_deps) == 1

    tiers = plan.task_tiers()
    assert len(tiers) == 2  # clinvar first, {ensembl_vep, gnomad} second (both require clinvar)
    tier0_connectors = {t.connector for t in tiers[0]}
    assert tier0_connectors == {"clinvar"}
    tier1_connectors = {t.connector for t in tiers[1]}
    assert tier1_connectors == {"ensembl_vep", "gnomad"}


def test_plan_is_frozen_and_carries_audit_metadata():
    resolver = ScopeResolver(identity_lookup={"VHL": "HGNC:12687"})
    planner = ConnectorPlanner(IdentityIndex(), rate_policies={})
    plan = plan_load("VHL", "gene", STANDARD_POLICY, resolver, planner)

    assert plan.plan_id and plan.created_at and plan.planner_version == "v1"
    with pytest.raises(Exception):  # frozen dataclass -- FrozenInstanceError
        plan.scope = "variant"


def test_gap_fill_anchors_are_flagged_with_correct_reason():
    resolver = ScopeResolver(identity_lookup={})  # VHL not yet resident
    planner = ConnectorPlanner(IdentityIndex(), rate_policies={})
    plan = plan_load("VHL", "gene", STANDARD_POLICY, resolver, planner)
    assert all(t.reason == "gap_fill" for t in plan.tasks)
