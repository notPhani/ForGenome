"""MONDO/HPO connector -- Evidence (disease/phenotype ontology, normalizing Disease
identity beyond MedGen CUIs). STUB, not implemented.

Real endpoint: the Monarch Initiative's public REST API,
`https://api.monarchinitiative.org/api/entity/{mondo_id}` (no API key required, unlike
OMIM) -- real and reachable, but not implemented in this pass: mapping ClinVar's MedGen
CUIs onto MONDO ids needs a real cross-reference step (Monarch's own
`/api/entity/{id}/mappings` or the EBI OLS4 MONDO ontology's `xrefs`) that hasn't been
verified against a real payload yet, and this project's own discipline is to not write a
mapping from an unconfirmed shape.
"""
from __future__ import annotations

from typing import Iterator

from orchestration.connector_registry import ConnectorDescriptor, Emission, register_connector

ENTITY_URL = "https://api.monarchinitiative.org/api/entity"


class MondoAdapter:
    def fetch(self, query: dict) -> Iterator[dict]:
        raise NotImplementedError(
            "MONDO connector is registered but not implemented -- the real endpoint "
            "(api.monarchinitiative.org) needs no API key, but the MedGen-CUI -> MONDO-id "
            "crosswalk this project would need hasn't been confirmed against a real payload yet."
        )

    def capabilities(self) -> dict:
        return {
            "scopes": ["variant"], "provides": ["Disease"],
            "bulk": False, "live_query": True, "batch": False,
            "hydration_lods": [1], "identity_types": ["MONDO", "HPO"],
        }

    def manifest(self) -> dict:
        return {"source": "MONDO", "dataset": "Monarch Initiative API", "schema_version": "1"}


def _build_query(scope: str, anchor: str, context: dict) -> dict | None:
    return None


def _build_emission(raw: dict, context: dict) -> Emission:
    raise NotImplementedError("MONDO connector has no real build_emission -- see MondoAdapter.fetch")


register_connector(ConnectorDescriptor(
    name="mondo", group="evidence", cadence="ephemeral",
    adapter=MondoAdapter(), build_query=_build_query, build_emission=_build_emission,
    feed_label="MONDO/HPO", implemented=False, requires=("clinvar",),
    description="Disease/phenotype ontology, normalizing Disease identity beyond MedGen CUIs "
               "(GET /api/entity/{mondo_id})",
))
