"""Pure interfaces -- forge-ir-v3-schema.md §8, forge-pipeline-v1.md §2.

No logic here, ever. Every concrete implementation lives in ingest/, connectors/,
or orchestration/ and imports its Protocol from here -- this file is the single
place that says "what must X implement," so different layers can be built by
different people without needing to read each other's code first.
"""
from __future__ import annotations

from typing import Any, Iterator, Protocol

from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.requirement import Requirement, RequirementStatus


class SourceAdapter(Protocol):
    """Fetches raw, source-shaped data. Never touches Forge types -- see
    forge-pipeline-v1.md §2 for why fetch and compilation are deliberately
    separate concerns."""

    def fetch(self, query: dict) -> Iterator[Any]: ...  # yields raw SourceRecord objects
    def capabilities(self) -> Any: ...                   # -> ConnectorCapabilities
    def manifest(self) -> Any: ...                        # -> SourceManifest


class SourceParser(Protocol):
    def parse(self, record: Any) -> Iterator[Any]: ...  # SourceRecord -> SourceObject(s)


class MappingCompiler(Protocol):
    def compile(self, obj: Any, spec: Any) -> Iterator[ForgeNode | ForgeEdge]: ...


class IdentityResolver(Protocol):
    def resolve(self, node: ForgeNode) -> Any: ...  # -> IdentityResolution


class Verifier(Protocol):
    def verify(self, nodes: list[ForgeNode], edges: list[ForgeEdge]) -> Any: ...  # -> VerificationReport


class RequirementEvaluator(Protocol):
    def evaluate(self, requirement: Requirement, observation: dict | None) -> tuple[RequirementStatus, dict]: ...


class GraphCompiler(Protocol):
    def compile(self, nodes: list[ForgeNode], edges: list[ForgeEdge]) -> Any: ...  # -> CompiledGraph


class ScopeResolver(Protocol):
    def resolve(self, anchor: str | list[str], scope: str) -> Any: ...  # -> ResolutionPlan


class ConnectorPlanner(Protocol):
    def plan(self, resolution: Any, policy: Any) -> list[Any]: ...  # -> list[FetchTask]
    def check_residency(self, anchor: str) -> bool: ...
    def check_freshness(self, node: ForgeNode, threshold_days: float) -> bool: ...
