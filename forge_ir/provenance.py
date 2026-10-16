"""Provenance types, split by Origin per forge-ir-v3-schema.md §3 / §7.7 and
review-round 3's origin-branched hydration-integrity fix: a SOURCE-origin node
has a real external record to point at; a DERIVED-origin node has inputs it
was computed from; a RUNTIME-origin node has an investigation it was created
for. Forcing all three into one shape is what caused the original hydration
check to wrongly demand a source pointer on nodes that never had one.
"""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from datetime import datetime


class Provenance(ABC):
    """Base type. Never instantiated directly -- always one of the three
    subclasses below, selected by the owning node/edge's `origin`."""


@dataclass(frozen=True)
class SourceProvenance(Provenance):
    source: str                    # "ClinVar" | "Ensembl" | "gnomAD" | "ClinGen"
    source_record_id: str          # e.g. "SCV001473258", "ENST00000256474"
    source_version: str | None     # None when the source doesn't self-report one
                                    # (confirmed real for Ensembl/gnomAD -- ingest/manifest.py
                                    # is the authoritative fallback in that case)
    retrieved_at: datetime
    snapshot_ref: str | None = None  # which SourceSnapshot this was pulled from, if known


@dataclass(frozen=True)
class DerivedProvenance(Provenance):
    derivation_rule: str           # e.g. "requirement_extraction_v1"
    input_node_ids: tuple[str, ...]
    input_edge_ids: tuple[str, ...]
    compiler_version: str
    derived_at: datetime


@dataclass(frozen=True)
class RuntimeProvenance(Provenance):
    investigation_id: str
    activation_reason: str
    timestamp: datetime
