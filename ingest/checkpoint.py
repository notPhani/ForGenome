"""IngestionCheckpoint -- forge-pipeline-v1.md §7.

Scope note: the contract exists now so it isn't retrofitted against a live system
later, but it is NOT exercised for real crash-recovery at VHL scale (a few hundred
records) -- that gets stress-tested when bulk ClinVar ingestion actually runs.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IngestionCheckpoint:
    run_id: str
    partition_id: str
    source_cursor: str
    last_record_id: str | None
    status: str
    checksum: str | None
