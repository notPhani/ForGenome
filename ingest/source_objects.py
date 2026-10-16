"""SourceObjectBase -- forge-pipeline-v1.md §2.

Every per-source SourceObject (ClinVarSCV, EnsemblVariation, ...) subclasses this.
Centralizing these fields here means a single SourceObject instance carries enough
information to reproduce or audit its own transformation, instead of that metadata
being scattered across SourceRecord, SourceManifest, and eventual node Provenance.
Concrete per-source subclasses live in connectors/*.py, next to the parsers that build
them -- keeping the stable base separate from source-specific shape.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SourceObjectBase:
    source: str
    record_type: str
    source_record_id: str
    source_version: str | None
    snapshot_ref: str    # which SourceSnapshot this was parsed from -- traces to the exact
                          # raw bytes without duplicating response_hash per-record (one raw
                          # response can hold many records, e.g. one ClinVar XML -> 22 SCVs)
    payload_hash: str    # hash of THIS record's own parsed content
