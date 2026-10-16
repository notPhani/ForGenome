"""Shared node-construction helpers across connectors.

Real gap found while wiring the E2E pipeline together: every connector (Ensembl's VEP,
ClinVar's hierarchy, gnomAD's frequency) references the canonical Variant id as an edge
target, but none of them actually emits the Variant ENTITY node itself -- each treats it
as something that "already exists," which is correct in spirit (connector-research.md §5:
ClinVar's CanonicalSPDI "resolves/confirms the Variant ENTITY, not a new node") but means
something has to actually create it once. This is that one place, called during
identity resolution / plan execution rather than improvised per-connector or ad hoc in
a test.
"""
from __future__ import annotations

from datetime import datetime

from forge_ir.core import NodeRole, Origin, source_manifest_canonical_id, variant_canonical_id
from forge_ir.graph import ForgeNode
from forge_ir.provenance import SourceProvenance


def build_variant_entity(
    seq_accession: str, spdi_position: int, ref: str, alt: str, assembly: str,
    source: str, source_record_id: str, source_version: str | None, retrieved_at: datetime,
) -> ForgeNode:
    variant_id = variant_canonical_id(seq_accession, spdi_position, ref, alt)
    return ForgeNode(
        id=variant_id, role=NodeRole.ENTITY, subtype="Variant",
        attributes={"assembly": assembly, "ref": ref, "alt": alt, "seq_accession": seq_accession,
                    "spdi_position": spdi_position},
        provenance=[SourceProvenance(source, source_record_id, source_version, retrieved_at)],
        origin=Origin.SOURCE,
    )


def build_source_manifest_node(
    source: str, dataset: str, version: str | None, retrieved_at: datetime,
    schema_version: str = "1", endpoint: str | None = None,
) -> ForgeNode:
    """forge-pipeline-v1.md §5: "mandatory, not design-only." One node per
    (source, dataset, version) -- every CLAIM that source+version produced gets a
    SOURCED_FROM edge to this same node (never a fresh one per record), so a caller
    building many CLAIMs from one manifest should build this once and reuse the id;
    the id is deterministic (source_manifest_canonical_id), so re-constructing it
    repeatedly is harmless -- the graph's normal id-based dedup absorbs the duplicates."""
    manifest_id = source_manifest_canonical_id(source, dataset, version)
    return ForgeNode(
        id=manifest_id, role=NodeRole.ENTITY, subtype="SourceManifest",
        attributes={"source": source, "dataset": dataset, "source_version": version,
                    "schema_version": schema_version, "endpoint": endpoint,
                    "retrieved_at": retrieved_at.isoformat()},
        provenance=[SourceProvenance(source, dataset, version, retrieved_at)],
        origin=Origin.SOURCE,
    )
