"""Every registered connector's DECLARED capabilities must be expressible in the frozen
ontology -- checked mechanically, because a mapping proposed on paper is not checkable by
reading it.

This exists because of a real, specific failure. `docs/stub-connectors-research-and-ir-mapping.md`
proposed IR mappings for the six stub connectors and closed with a table asserting that all
19 proposed edge combinations were "Allowed in Schema? YES". Checked against
`forge_ir/core.py`, 15 of the 19 were not: four invented node subtypes that
NODE_SUBTYPE_REGISTRY does not contain (AllelicVariantAssertion, GeneDiseaseValidity,
PharmacogenomicAssertion, Drug), six invented qualifiers on real predicates
(RELATES_TO[causes], [validates_association], [associated_with], [modulates_response_to],
[targets], [observed_in_tissue]), two assumed SOURCED_FROM accepts OBSERVATION sources when
ALLOWED_EDGE_COMBINATIONS permits only CLAIM, and three used a predicate whose real domain
is narrower than assumed (ASSERTS is Variant-only; PART_OF is structural only; SUPPORTS
[corroborates] targets ClinicalAssertion only).

Every one of those would have passed code review and then rejected the batch at runtime, in
forge_ir/verify.py check 1. The tests below are the cheap version of that discovery.
"""
import pytest

import connectors  # noqa: F401 -- registers every connector
from forge_ir.core import NODE_SUBTYPE_REGISTRY
from ingest.dirty import expected_edge_shapes
from orchestration.connector_registry import list_connectors

REGISTERED_SUBTYPES = {subtype for (_role, subtype) in NODE_SUBTYPE_REGISTRY}


@pytest.mark.parametrize("descriptor", list_connectors(), ids=lambda d: d.name)
def test_declared_subtypes_all_exist_in_the_ontology(descriptor):
    """`capabilities()["provides"]` is what the advisory completeness report and the retry
    ledger's expected-edge derivation both read. A subtype named there that the ontology
    does not define is a claim nothing can ever satisfy."""
    provides = set(descriptor.adapter.capabilities().get("provides", []))
    unknown = provides - REGISTERED_SUBTYPES
    assert not unknown, (
        f"{descriptor.name} declares subtype(s) absent from NODE_SUBTYPE_REGISTRY: "
        f"{sorted(unknown)}. The ontology is frozen -- either map onto an existing subtype "
        f"or the schema change comes first.")


@pytest.mark.parametrize("descriptor", list_connectors(), ids=lambda d: d.name)
def test_every_connector_declares_at_least_one_subtype(descriptor):
    """An empty `provides` makes the completeness report say "0/0 subtypes present", which
    scores as a vacuous pass rather than the hole it is."""
    provides = descriptor.adapter.capabilities().get("provides", [])
    assert provides, (
        f"{descriptor.name} declares no subtypes at all, so nothing can measure whether it "
        f"delivered anything")


@pytest.mark.parametrize("descriptor", list_connectors(), ids=lambda d: d.name)
def test_every_connector_has_at_least_one_legal_edge_shape(descriptor):
    """A node subtype the ontology allows no edges to or from cannot attach to the graph,
    and would fail the connectivity audit (verify.py check 8) as orphaned."""
    assert expected_edge_shapes(descriptor), (
        f"{descriptor.name}'s declared subtypes sit on no edge combination in "
        f"ALLOWED_EDGE_COMBINATIONS -- its nodes could never connect to anything")
