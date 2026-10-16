"""PharmGKB connector -- Evidence (pharmacogenomic variant-drug-response annotations).
STUB, not implemented.

Real endpoint: PharmGKB's public REST API,
`https://api.pharmgkb.org/v1/data/variant/{rsid}` (no API key required -- confirmed
from PharmGKB's own published API documentation at api.pharmgkb.org). Not implemented
in this pass: what is missing is a parser and a fixture, not a schema decision.

On the schema decision, which this connector previously deferred by declaring NO subtypes
at all: a PharmGKB clinical annotation is an assertion that a variant is associated with a
drug-response phenotype, and `Association` is exactly the registered CLAIM subtype for
"X is associated with Y" -- the same one connectors/clingen_gene_disease.py uses for
gene-disease validity. So this maps onto the frozen ontology without extending it.

The cost of that mapping, stated rather than hidden: the ontology has no `Drug` ENTITY, so
the drug is carried as an attribute of the Association rather than as its own node, and
"which drug" is therefore not joinable across sources. That is the same LOD trade gnomAD's
population breakdown and UniProt's 172 sequence features already make. A richer model --
a `Drug` ENTITY plus RELATES_TO[modulates_response_to] -- is proposed in
docs/stub-connectors-research-and-ir-mapping.md and needs the schema unfrozen first; it is
a real option, not a rejected one.

Declaring nothing was worse than declaring this. An empty `provides` made
analysis/completeness.py report "0/0 declared subtypes present", which scores as a vacuous
0% rather than as the gap it is, and left the retry ledger unable to derive a single
expected edge shape for this connector.
"""
from __future__ import annotations

from typing import Iterator

from orchestration.connector_registry import ConnectorDescriptor, Emission, register_connector

VARIANT_URL = "https://api.pharmgkb.org/v1/data/variant"


class PharmGkbAdapter:
    def fetch(self, query: dict) -> Iterator[dict]:
        raise NotImplementedError(
            "PharmGKB connector is registered but not implemented -- it needs a parser and "
            "a real saved fixture. The IR mapping is settled: clinical annotations become "
            "CLAIM:Association, with the drug as an attribute (see module docstring)."
        )

    def capabilities(self) -> dict:
        return {
            "scopes": ["variant"], "provides": ["Association"],   # see the docstring on why
                                                                   # Association and not a new subtype
            "bulk": False, "live_query": True, "batch": False,
            "hydration_lods": [1], "identity_types": ["PharmGKB accession", "rsID"],
        }

    def manifest(self) -> dict:
        return {"source": "PharmGKB", "dataset": "PharmGKB API", "schema_version": "1"}


def _build_query(scope: str, anchor: str, context: dict) -> dict | None:
    return None


def _build_emission(raw: dict, context: dict) -> Emission:
    raise NotImplementedError("PharmGKB connector has no real build_emission -- see PharmGkbAdapter.fetch")


register_connector(ConnectorDescriptor(
    name="pharmgkb", group="evidence", cadence="ephemeral",
    adapter=PharmGkbAdapter(), build_query=_build_query, build_emission=_build_emission,
    feed_label="PharmGKB", implemented=False, requires=("clinvar",),
    description="Pharmacogenomic variant-drug-response annotations as CLAIM:Association, "
               "drug carried as an attribute (GET /v1/data/variant/{rsid})",
))
