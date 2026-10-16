"""SourceManifest / SourceSnapshot -- forge-pipeline-v1.md §5.

Manifest answers "what source/version/configuration is this" (one per source+release,
relatively static). Snapshot answers "what exact bytes did we consume for this specific
fetch" (one manifest backs many snapshots -- a single ClinVar release backs thousands of
individual ESummary queries).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SourceManifest:
    source: str
    dataset: str
    source_version: str | None  # null when the source doesn't self-report one (confirmed
                                 # real for Ensembl/gnomAD -- this manifest is the fallback)
    release_id: str | None
    retrieved_at: datetime
    checksum: str | None
    endpoint: str | None
    schema_version: str


@dataclass(frozen=True)
class SourceSnapshot:
    manifest_ref: str          # which SourceManifest this snapshot was taken under
    request_hash: str          # hash of the outbound query -- backs idempotent-retry caching
    response_hash: str | None  # hash of the raw response; for bulk files, use the file checksum
    endpoint: str | None
    request_parameters: dict
    retrieved_at: datetime
