"""gnomAD connector -- Evidence (population frequency). Real payload shape confirmed
against fixtures/gnomad_variant_3-10149822-C-T.json.

Stays compact by design (forge-pipeline-v1.md §14): one PopulationFrequency observation
per dataset (genome/exome), the ~30-row population breakdown kept as a nested attribute
rather than exploded into per-population nodes -- exactly the graph-explosion case the
LOD system exists to prevent. `faf95.popmax` (GroupMax Filtering AF) is captured
explicitly because it's PM2's actual real threshold metric, not the plain `af` field.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from connectors import http

from forge_ir.core import EdgeType, NodeRole, Origin, spdi_position_to_1based
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.provenance import SourceProvenance
from ingest.population import normalize_populations
from ingest.source_objects import SourceObjectBase
from orchestration.connector_registry import ConnectorDescriptor, Emission, register_connector

API_URL = "https://gnomad.broadinstitute.org/api"

_QUERY = """
query VariantQuery($variantId: String!, $datasetId: DatasetId!) {
  variant(variantId: $variantId, dataset: $datasetId) {
    variant_id reference_genome chrom pos ref alt rsids
    genome { ac an af faf95 { popmax popmax_population } populations { id ac an } }
    exome { ac an af faf95 { popmax popmax_population } }
  }
}
"""


@dataclass
class GnomadFrequency(SourceObjectBase):
    variant_id: str
    dataset_scope: str  # "genome" | "exome"
    ac: int
    an: int
    af: float
    faf95_popmax: float | None
    populations: list[dict]


class GnomadAdapter:
    def fetch(self, query: dict) -> Iterator[dict]:
        resp = http.post(API_URL, json={
            "query": _QUERY,
            "variables": {"variantId": query["variant_id"], "datasetId": query.get("dataset_id", "gnomad_r4")},
        })
        yield resp.json()

    def capabilities(self) -> dict:
        return {
            "scopes": ["variant"], "provides": ["PopulationFrequency"],
            "bulk": False, "live_query": True, "batch": True,
            "hydration_lods": [1], "identity_types": ["gnomAD variant_id"],
        }

    def manifest(self) -> dict:
        return {"source": "gnomAD", "dataset": "gnomad_r4", "schema_version": "1"}


class GnomadParser:
    def parse(self, raw: dict, snapshot_ref: str = "") -> Iterator[GnomadFrequency]:
        v = raw["data"]["variant"]
        if v is None:
            # Real, common case at scale: gnomAD returns {"data": {"variant": null}} for
            # any variant it has no record for at all -- not malformed, not an error, just
            # "never observed in gnomAD" (true for most rare/novel ClinVar submissions).
            # Confirmed live: 4/4 real variants in a small VHL sample hit this path. Prior
            # to this check, `v.get(scope)` below raised AttributeError on the None, which
            # the runner correctly quarantined -- but that mislabels a legitimate negative
            # result as a parse failure. Yielding nothing is the correct behavior: no
            # PopulationFrequency node, no OBSERVES edge, no quarantine entry.
            return
        for scope in ("genome", "exome"):
            data = v.get(scope)
            if data is None:
                continue
            faf = data.get("faf95") or {}
            yield GnomadFrequency(
                source="gnomAD", record_type="PopulationFrequency", source_record_id=v["variant_id"],
                source_version=None,  # gnomAD doesn't self-report a dataset build in the response --
                                       # confirmed real (forge-pipeline-v1.md §5) -- SourceManifest is authoritative
                snapshot_ref=snapshot_ref, payload_hash=str(hash(str(data))),
                variant_id=v["variant_id"], dataset_scope=scope,
                ac=data["ac"], an=data["an"], af=data["af"],
                faf95_popmax=faf.get("popmax"), populations=data.get("populations", []),
            )


def frequency_to_ir(obj: GnomadFrequency, variant_id: str) -> list[ForgeNode | ForgeEdge]:
    now = datetime.now(timezone.utc)
    prov = [SourceProvenance("gnomAD", obj.variant_id, obj.source_version, now)]
    obs_id = f"gnomAD:{obj.variant_id}:{obj.dataset_scope}"

    node = ForgeNode(
        id=obs_id, role=NodeRole.OBSERVATION, subtype="PopulationFrequency",
        attributes={
            "ac": obj.ac, "an": obj.an, "af": obj.af, "faf95_popmax": obj.faf95_popmax,
            "dataset": obj.dataset_scope,
            # Normalized against ingest/population.py's canonical ancestry-group taxonomy
            # (a second frequency source's own codes would map onto the SAME taxonomy,
            # not require its own bespoke label set) -- kept compact as an attribute,
            # never exploded into nodes; raw_code preserved on every row, nothing dropped.
            "populations": normalize_populations(obj.populations),
        },
        provenance=prov, origin=Origin.SOURCE,
    )
    edge = ForgeEdge(
        src=obs_id, predicate=EdgeType.OBSERVES, dst=variant_id, qualifier=None,
        attributes={}, provenance=prov, origin=Origin.SOURCE,
    )
    return [node, edge]


# ---------------------------------------------------------------------------
# Connector registry -- see orchestration/connector_registry.py
# ---------------------------------------------------------------------------

def _build_query(scope: str, anchor: str, context: dict) -> dict | None:
    if scope == "variant":
        return {"variant_id": anchor}   # DAG-planning path (tests/test_orchestration.py)
    if scope == "coordinates":
        pos = spdi_position_to_1based(context["pos"])
        return {"variant_id": f"{context['chrom']}-{pos}-{context['ref']}-{context['alt']}"}
    return None   # gene scope: not applicable


def _build_emission(raw: dict, context: dict) -> Emission:
    emission = Emission()
    for freq in GnomadParser().parse(raw):
        emission.add("gnomAD", frequency_to_ir(freq, context["variant_id"]))
    return emission


register_connector(ConnectorDescriptor(
    name="gnomad", group="evidence", cadence="ephemeral",
    adapter=GnomadAdapter(), build_query=_build_query, build_emission=_build_emission,
    feed_label="gnomAD", requires=("clinvar",),
    description="Population frequency (faf95_popmax), one request per variant (GraphQL)",
))
