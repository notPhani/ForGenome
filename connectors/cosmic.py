"""COSMIC connector -- Evidence (somatic cancer variant catalog, a different Evidence
flavor than ClinVar's germline focus). STUB, not implemented.

Real endpoint: COSMIC (Catalogue of Somatic Mutations in Cancer,
cancer.sanger.ac.uk/cosmic) exposes data via a licensed download + REST API that
requires a registered account and a per-session auth token (confirmed from COSMIC's own
access documentation) -- unlike Ensembl/ClinVar/gnomAD/ClinGen/dbSNP/UniProt, there is
no anonymous public endpoint to call.
"""
from __future__ import annotations

from typing import Iterator

from orchestration.connector_registry import ConnectorDescriptor, Emission, register_connector

BASE_URL = "https://cancer.sanger.ac.uk/cosmic"


class CosmicAdapter:
    def fetch(self, query: dict) -> Iterator[dict]:
        raise NotImplementedError(
            "COSMIC connector is registered but not implemented -- COSMIC requires a "
            "licensed account and per-session auth token (cancer.sanger.ac.uk/cosmic), "
            "not a public endpoint like this project's other sources."
        )

    def capabilities(self) -> dict:
        return {
            "scopes": ["variant", "gene"], "provides": ["ClinicalAssertion"],
            "bulk": True, "live_query": True, "batch": True,
            "hydration_lods": [1, 2], "identity_types": ["COSMIC ID"],
        }

    def manifest(self) -> dict:
        return {"source": "COSMIC", "dataset": "COSMIC (licensed)", "schema_version": "1"}


def _build_query(scope: str, anchor: str, context: dict) -> dict | None:
    return None


def _build_emission(raw: dict, context: dict) -> Emission:
    raise NotImplementedError("COSMIC connector has no real build_emission -- see CosmicAdapter.fetch")


register_connector(ConnectorDescriptor(
    name="cosmic", group="evidence", cadence="ephemeral",
    adapter=CosmicAdapter(), build_query=_build_query, build_emission=_build_emission,
    feed_label="COSMIC", implemented=False, requires=("clinvar",),
    description="Somatic cancer variant catalog -- requires a licensed account, no public "
               "endpoint (cancer.sanger.ac.uk/cosmic)",
))
