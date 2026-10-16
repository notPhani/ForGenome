"""Identity resolution -- forge-pipeline-v1.md §4.

All canonicalization is centralized here: no connector normalizes its own SPDI, HGNC,
or MedGen identifiers (the off-by-one fix between gnomAD's 1-based variant_id and
SPDI's 0-based convention lives in forge_ir.core, called from exactly one place).

The index is SQLite-backed, not an in-memory dict -- it must be genuinely persistent
across batches and process runs (review round 3's finding: the original two-phase
compiler only resolved identity within one batch, which breaks the moment evidence
gets loaded across separate `load()` calls over time, which is how this architecture
is actually supposed to run).

Honest scope note: for the current 4-connector build, Gene (HGNC) and Variant (SPDI)
both have deterministic canonical ID schemes shared across every source, so most real
resolutions land as MERGED via exact key match, not fuzzy matching. REPRESENTS becomes
a real, exercised path once a source without a deterministic shared key joins (e.g.
UniProt's protein accession vs. Ensembl's ENSP id) -- not fabricated as tested-but-unused
code, but not yet exercised by real data either. This mirrors the same honesty standard
used for EvaluationMode.COMPOSITE/NARRATIVE elsewhere in this codebase.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from forge_ir.core import (
    EdgeType,
    Origin,
    disease_canonical_id,
    gene_canonical_id,
    normalize_spdi_position_from_1based,
    variant_canonical_id,
)
from forge_ir.graph import ForgeEdge
from forge_ir.provenance import SourceProvenance


@dataclass
class IdentityEvidence:
    match_method: str              # "exact_spdi" | "hgnc_xref" | "medgen_cui" | ...
    confidence: float
    resolver_version: str
    matched_identifiers: list[str]


@dataclass
class IdentityResolution:
    canonical_id: str | None
    status: Literal["MERGED", "REPRESENTS", "UNRESOLVED"]
    represents_edge: ForgeEdge | None   # set only when status == REPRESENTS
    evidence: IdentityEvidence


@dataclass
class IdentityIndexEntry:
    canonical_id: str
    namespace: str                 # "SPDI" | "HGNC" | "MedGen" | ...
    normalized_key: str            # the exact key used to look this up, post-normalization
    resolver_version: str
    confidence: float
    match_method: str
    first_seen_run: str
    last_seen_run: str


class IdentityIndex:
    """Persistent canonical_id lookup, keyed by (namespace, normalized_key). Answers
    "why does ClinVar's VCV000002218 and Ensembl's rs5030820 map to the same Forge
    Variant" by reading the index, not by rerunning the resolver."""

    def __init__(self, db_path: str = ":memory:"):
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS identity_index (
                namespace TEXT NOT NULL,
                normalized_key TEXT NOT NULL,
                canonical_id TEXT NOT NULL,
                resolver_version TEXT NOT NULL,
                confidence REAL NOT NULL,
                match_method TEXT NOT NULL,
                first_seen_run TEXT NOT NULL,
                last_seen_run TEXT NOT NULL,
                PRIMARY KEY (namespace, normalized_key)
            )
            """
        )
        self._conn.commit()

    def lookup(self, namespace: str, normalized_key: str) -> IdentityIndexEntry | None:
        row = self._conn.execute(
            "SELECT namespace, normalized_key, canonical_id, resolver_version, confidence, "
            "match_method, first_seen_run, last_seen_run FROM identity_index "
            "WHERE namespace=? AND normalized_key=?",
            (namespace, normalized_key),
        ).fetchone()
        if row is None:
            return None
        return IdentityIndexEntry(
            canonical_id=row[2], namespace=row[0], normalized_key=row[1], resolver_version=row[3],
            confidence=row[4], match_method=row[5], first_seen_run=row[6], last_seen_run=row[7],
        )

    def upsert(self, entry: IdentityIndexEntry, run_id: str) -> None:
        existing = self.lookup(entry.namespace, entry.normalized_key)
        first_seen = existing.first_seen_run if existing else run_id
        self._conn.execute(
            "INSERT INTO identity_index (namespace, normalized_key, canonical_id, resolver_version, "
            "confidence, match_method, first_seen_run, last_seen_run) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(namespace, normalized_key) DO UPDATE SET "
            "canonical_id=excluded.canonical_id, resolver_version=excluded.resolver_version, "
            "confidence=excluded.confidence, match_method=excluded.match_method, "
            "last_seen_run=excluded.last_seen_run",
            (entry.namespace, entry.normalized_key, entry.canonical_id, entry.resolver_version,
             entry.confidence, entry.match_method, first_seen, run_id),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def resolve_identity(
    index: IdentityIndex,
    namespace: str,
    normalized_key: str,
    candidate_canonical_id: str,
    match_method: str,
    confidence: float,
    resolver_version: str,
    run_id: str,
    represents_confidence_threshold: float = 0.7,
) -> IdentityResolution:
    """The one place identity gets resolved -- connectors never decide this themselves,
    the compiler calls this uniformly for every candidate node before commit."""
    existing = index.lookup(namespace, normalized_key)
    evidence = IdentityEvidence(match_method, confidence, resolver_version, [normalized_key])

    if existing is None or existing.canonical_id == candidate_canonical_id:
        index.upsert(
            IdentityIndexEntry(candidate_canonical_id, namespace, normalized_key, resolver_version,
                                confidence, match_method, run_id, run_id),
            run_id,
        )
        return IdentityResolution(candidate_canonical_id, "MERGED", None, evidence)

    # A different canonical_id is already indexed for this key -- never silently overwrite.
    if confidence >= represents_confidence_threshold:
        edge = ForgeEdge(
            src=candidate_canonical_id, predicate=EdgeType.REPRESENTS, dst=existing.canonical_id,
            qualifier=None, attributes={},
            provenance=[SourceProvenance("IdentityResolver", normalized_key, resolver_version,
                                          datetime.now(timezone.utc))],
            origin=Origin.SOURCE, confidence=confidence, match_method=match_method,
            resolver_version=resolver_version,
        )
        return IdentityResolution(None, "REPRESENTS", edge, evidence)

    return IdentityResolution(None, "UNRESOLVED", None, evidence)


def normalize_variant_key(source: str, seq_accession: str, position: int, ref: str, alt: str,
                           position_is_1based: bool = False) -> tuple[str, str]:
    """Centralized SPDI normalization -- the off-by-one fix (gnomAD 1-based vs. SPDI
    0-based) happens here, exactly once, regardless of which connector is calling."""
    pos = normalize_spdi_position_from_1based(position) if position_is_1based else position
    key = variant_canonical_id(seq_accession, pos, ref, alt)
    return "SPDI", key


def normalize_gene_key(hgnc_id: str) -> tuple[str, str]:
    return "HGNC", gene_canonical_id(hgnc_id)


def normalize_disease_key(medgen_cui: str) -> tuple[str, str]:
    return "MedGen", disease_canonical_id(medgen_cui)
