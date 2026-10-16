"""ConnectorCapabilities -- forge-pipeline-v1.md §13.

Formalizes what each connector's own `capabilities()` dict returns, so
ScopeResolver/ConnectorPlanner can discover what's available instead of
hardcoding `if gene: use_clinvar(); use_gnomad()` -- this is the actual
mechanism that makes "generalized connector framework" true, not aspirational.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectorCapabilities:
    scopes: tuple[str, ...]
    provides: tuple[str, ...]
    bulk: bool
    live_query: bool
    batch: bool
    hydration_lods: tuple[int, ...]
    identity_types: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: dict) -> "ConnectorCapabilities":
        return cls(
            scopes=tuple(d["scopes"]), provides=tuple(d["provides"]), bulk=d["bulk"],
            live_query=d["live_query"], batch=d["batch"], hydration_lods=tuple(d["hydration_lods"]),
            identity_types=tuple(d["identity_types"]),
        )
