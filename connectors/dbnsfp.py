"""dbNSFP connector -- Evidence (aggregated in-silico predictors, extending VEP's
SIFT/PolyPhen with more predictor tools). STUB, not implemented.

Real access shape, confirmed from dbNSFP's own distribution (Liu et al., maintained at
https://sites.google.com/site/jpopgen/dbNSFP): it is NOT a REST API at all -- it ships
as a ~30 GB tab-delimited flat-file database (bgzip-compressed, tabix-indexed) meant to
be queried locally or through a genome browser's own annotation service (e.g. Ensembl
VEP's own dbNSFP plugin). This is a structurally different integration than every other
connector in this stack (`SourceAdapter.fetch()`'s `Iterator[Any]` contract assumes a
live per-query round-trip); registered here so the registry reports the real constraint
rather than pretending an HTTP endpoint exists where none does.
"""
from __future__ import annotations

from typing import Iterator

from orchestration.connector_registry import ConnectorDescriptor, Emission, register_connector


class DbNsfpAdapter:
    def fetch(self, query: dict) -> Iterator[dict]:
        raise NotImplementedError(
            "dbNSFP connector is registered but not implemented -- dbNSFP is a downloadable "
            "tabix-indexed flat-file database, not a REST API; integrating it needs a local "
            "file-lookup adapter, not an HTTP SourceAdapter like every other connector here."
        )

    def capabilities(self) -> dict:
        return {
            "scopes": ["variant"], "provides": ["ComputationalPrediction"],
            "bulk": True, "live_query": False, "batch": True,
            "hydration_lods": [1], "identity_types": ["SPDI"],
        }

    def manifest(self) -> dict:
        return {"source": "dbNSFP", "dataset": "dbNSFP (local flat-file)", "schema_version": "1"}


def _build_query(scope: str, anchor: str, context: dict) -> dict | None:
    return None


def _build_emission(raw: dict, context: dict) -> Emission:
    raise NotImplementedError("dbNSFP connector has no real build_emission -- see DbNsfpAdapter.fetch")


register_connector(ConnectorDescriptor(
    name="dbnsfp", group="evidence", cadence="ephemeral",
    adapter=DbNsfpAdapter(), build_query=_build_query, build_emission=_build_emission,
    feed_label="dbNSFP", implemented=False, requires=("clinvar",),
    description="Aggregated in-silico predictors -- local tabix-indexed flat-file, not a REST API",
))
