"""OMIM connector -- Core Biology (Mendelian gene-disease associations). STUB, not
implemented -- registered so the connector registry reports it honestly rather than
pretending it doesn't exist.

Real endpoint: `https://api.omim.org/api/entry?mimNumber={number}&include=geneMap&format=json`.
Real access friction, confirmed by reading OMIM's own API documentation: requests an API
key via a registration form (https://omim.org/api) with manual approval, not available
by simply calling the endpoint the way Ensembl/ClinVar/gnomAD/ClinGen/dbSNP/UniProt all
are. VHL's real MIM number is 193300; the gene entry is 608537.
"""
from __future__ import annotations

from typing import Iterator

from orchestration.connector_registry import ConnectorDescriptor, Emission, register_connector

ENTRY_URL = "https://api.omim.org/api/entry"


class OmimAdapter:
    def fetch(self, query: dict) -> Iterator[dict]:
        raise NotImplementedError(
            "OMIM connector is registered but not implemented -- requires an API key "
            "issued via https://omim.org/api's manual-approval registration form, "
            "unlike every other connector in this stack."
        )

    def capabilities(self) -> dict:
        return {
            "scopes": ["gene"], "provides": ["Disease"],
            "bulk": False, "live_query": True, "batch": False,
            "hydration_lods": [1], "identity_types": ["MIM number"],
        }

    def manifest(self) -> dict:
        return {"source": "OMIM", "dataset": "OMIM API", "schema_version": "1"}


def _build_query(scope: str, anchor: str, context: dict) -> dict | None:
    return None   # not implemented -- never produces a real task


def _build_emission(raw: dict, context: dict) -> Emission:
    raise NotImplementedError("OMIM connector has no real build_emission -- see OmimAdapter.fetch")


register_connector(ConnectorDescriptor(
    name="omim", group="core_biology", cadence="persistent",
    adapter=OmimAdapter(), build_query=_build_query, build_emission=_build_emission,
    feed_label="OMIM", implemented=False, default_included=False,
    description="Mendelian gene-disease associations (GET /api/entry -- requires an API key, "
               "see https://omim.org/api)",
))
