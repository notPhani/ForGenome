"""ClinGen Gene-Disease Validity / Dosage Sensitivity connector -- Operational
(gene-level curation, distinct from the variant-level VCEP criteria
`connectors/clingen.py` already implements against the real, confirmed
`cspec.genome.network/cspec/SequenceVariantInterpretation/id/{spec_id}` JSON endpoint).
STUB, not implemented.

Real access point checked live for this project: `search.clinicalgenome.org/kb/genes/
{HGNC_ID}` returns 200 with a real page (confirmed: HGNC:12687 -> "VHL curation
results"), but it is the human-facing curation KB (`text/html`), not JSON --
`/kb/genes/{id}.json` and a guessed `/api/genes/{id}` were both checked live and neither
returns real gene-validity data (the first still returns HTML, the second 404s). Unlike
`connectors/clingen.py`'s VCEP cspec endpoint, no real machine-readable endpoint for
Gene-Disease Validity / Dosage Sensitivity was confirmed in this pass -- registered
honestly as unconfirmed rather than guessing at an undocumented API shape.
"""
from __future__ import annotations

from typing import Iterator

from orchestration.connector_registry import ConnectorDescriptor, Emission, register_connector

KB_GENES_URL = "https://search.clinicalgenome.org/kb/genes"   # real, confirmed reachable;
                                                                # HTML, not JSON -- see docstring


class ClinGenGeneDiseaseAdapter:
    def fetch(self, query: dict) -> Iterator[dict]:
        raise NotImplementedError(
            "ClinGen Gene-Disease Validity/Dosage connector is registered but not "
            "implemented -- no confirmed JSON endpoint (search.clinicalgenome.org/kb/genes "
            "serves HTML, not an API); would need either an HTML scrape or ClinGen's own "
            "bulk curation-data download, neither built in this pass."
        )

    def capabilities(self) -> dict:
        return {
            "scopes": ["gene"], "provides": ["Association"],
            "bulk": False, "live_query": True, "batch": False,
            "hydration_lods": [1], "identity_types": ["HGNC"],
        }

    def manifest(self) -> dict:
        return {"source": "ClinGen", "dataset": "Gene-Disease Validity / Dosage Sensitivity",
                "schema_version": "1"}


def _build_query(scope: str, anchor: str, context: dict) -> dict | None:
    return None


def _build_emission(raw: dict, context: dict) -> Emission:
    raise NotImplementedError(
        "ClinGen Gene-Disease Validity/Dosage connector has no real build_emission -- "
        "see ClinGenGeneDiseaseAdapter.fetch")


register_connector(ConnectorDescriptor(
    name="clingen_gene_disease", group="operational", cadence="persistent",
    adapter=ClinGenGeneDiseaseAdapter(), build_query=_build_query, build_emission=_build_emission,
    feed_label="ClinGen Gene-Disease Validity", implemented=False, default_included=False,
    description="Gene-level curation (validity/dosage), distinct from VCEP variant criteria -- "
               "no confirmed JSON endpoint (checked live, HTML only)",
))
