"""Canonical hashing -- forge-pipeline-v1.md §6: "same inputs -> same graph" requires
canonical/sorted serialization before hashing. Dict key order and parallel-fetch response
ordering are real, easy ways to silently break reproducibility if this isn't centralized.

`canonical_hash()` existed from the start of this project but was never called from
anywhere -- §6's claim had no code path that actually computed or compared a hash.
`compute_structural_hash()` below is that path: it hashes a graph's STRUCTURE (node/edge
identity, roles, subtypes, attributes, and which source/record/version backs each one),
deliberately excluding provenance timestamps (`retrieved_at`/`derived_at`/`timestamp`)
and any attribute literally named `retrieved_at`. Every connector calls
`datetime.now()`, so a hash that included those would never match twice even when
re-parsing the exact same fixture -- the opposite of what "same inputs -> same graph"
is supposed to let you check.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.provenance import DerivedProvenance, Provenance, RuntimeProvenance, SourceProvenance

_VOLATILE_ATTRIBUTE_KEYS = {"retrieved_at"}


def canonical_hash(obj: Any) -> str:
    canonical = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _project_attributes(attrs: dict) -> dict:
    return {k: v for k, v in attrs.items() if k not in _VOLATILE_ATTRIBUTE_KEYS}


def _project_provenance(provenance: list[Provenance]) -> list[dict]:
    projected = []
    for p in provenance:
        if isinstance(p, SourceProvenance):
            projected.append({"kind": "source", "source": p.source,
                               "source_record_id": p.source_record_id, "source_version": p.source_version})
        elif isinstance(p, DerivedProvenance):
            projected.append({"kind": "derived", "derivation_rule": p.derivation_rule,
                               "input_node_ids": sorted(p.input_node_ids),
                               "input_edge_ids": sorted(p.input_edge_ids),
                               "compiler_version": p.compiler_version})
        elif isinstance(p, RuntimeProvenance):
            projected.append({"kind": "runtime", "investigation_id": p.investigation_id,
                               "activation_reason": p.activation_reason})
    return sorted(projected, key=lambda d: json.dumps(d, sort_keys=True, default=str))


def compute_structural_hash(nodes: list[ForgeNode], edges: list[ForgeEdge]) -> str:
    """Deterministic across repeated parses of the same source data, regardless of
    wall-clock time or fetch/dict ordering -- see tests/test_reproducibility.py."""
    node_repr = sorted((
        {"id": n.id, "role": n.role.value, "subtype": n.subtype,
         "attributes": _project_attributes(n.attributes), "origin": n.origin.value,
         "provenance": _project_provenance(n.provenance)}
        for n in nodes
    ), key=lambda d: d["id"])
    edge_repr = sorted((
        {"src": e.src, "predicate": e.predicate.value, "qualifier": e.qualifier, "dst": e.dst,
         "attributes": _project_attributes(e.attributes), "origin": e.origin.value,
         "provenance": _project_provenance(e.provenance)}
        for e in edges
    ), key=lambda d: (d["src"], d["predicate"], d["qualifier"] or "", d["dst"]))
    return canonical_hash({"nodes": node_repr, "edges": edge_repr})
