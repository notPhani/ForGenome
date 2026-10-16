"""ScopeResolver -- forge-pipeline-v1.md §13, forge-ir-v2 §3.

Only `variant` and `gene` scopes are implemented -- `gene_family`/`region` were marked
buildable-but-unverified and `pathway`/`domain` deferred (no pathway source in the
stack) in the review history; not building resolvers for scopes we can't back with a
real source. Resolves anchors to canonical IDs via a CHEAP identity lookup only --
never a full connector fetch (that's the Connector Planner's job, after the plan is
known). The identity lookup is injectable so orchestration tests stay network-free;
a real deployment would back it with a minimal live symbol/rsID lookup.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SUPPORTED_SCOPES = {"variant", "gene"}
DEFERRED_SCOPES = {"pathway", "domain"}          # no source to back these yet
EXPERIMENTAL_SCOPES = {"gene_family", "region"}  # buildable in principle, not yet verified against real data


@dataclass
class ResolutionPlan:
    canonical_anchors: list[str]
    scope: str
    gap_fill_required: list[str] = field(default_factory=list)  # anchors not yet resident in
                                                                   # persistent Core Biology


class ScopeResolver:
    def __init__(self, identity_lookup: dict[str, str] | None = None):
        self._identity_lookup = identity_lookup or {}

    def resolve(self, anchor: str | list[str], scope: str) -> ResolutionPlan:
        if scope in DEFERRED_SCOPES:
            raise NotImplementedError(f"scope={scope!r} deferred -- no backing source in the current stack")
        if scope in EXPERIMENTAL_SCOPES:
            raise NotImplementedError(
                f"scope={scope!r} is experimental/unverified -- not built until checked against real data"
            )
        if scope not in SUPPORTED_SCOPES:
            raise ValueError(f"unknown scope: {scope!r}")

        anchors = anchor if isinstance(anchor, list) else [anchor]
        canonical: list[str] = []
        gap_fill: list[str] = []
        for a in anchors:
            resolved = self._identity_lookup.get(a)
            if resolved is None:
                gap_fill.append(a)
                canonical.append(a)  # left as-is; Connector Planner's gap-fill path resolves it
            else:
                canonical.append(resolved)
        return ResolutionPlan(canonical_anchors=canonical, scope=scope, gap_fill_required=gap_fill)
