"""ForgeNode / ForgeEdge -- forge-ir-v3-schema.md §3, with the fixes agreed
across the pipeline review rounds:

- `layer` and `storage_scope` are always derived from (role, subtype) via
  core.py's registry, never settable directly by a connector.
- `provenance` is a list on BOTH nodes and edges -- a merged node/edge can
  carry more than one contributing source (real case: ClinVar's own record
  carries both GRCh38 and GRCh37 coordinates for the same gene).
- origin <-> storage_scope invariant: a RUNTIME-origin node/edge must be
  RUNTIME_ONLY storage_scope, and vice versa -- nothing created as purely
  investigation-scoped working state should silently leak into the persistent
  graph (review round 4, point 1).
- ForgeNode is deliberately NOT frozen: forge-ir-v3-schema.md §6.3's evidence
  lifecycle state machine requires in-place attribute updates for minor
  changes (a citation added, a comment reworded) without creating a new node.
  (Contrast with LoadPlan in orchestration/load_plan.py, which IS frozen --
  different question, different answer, not a blanket immutability stance.)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from forge_ir.core import (
    EdgeType,
    Layer,
    NodeRole,
    Origin,
    StorageScope,
    edge_storage_scope,
    is_allowed_edge,
    layer_for,
    storage_scope_for,
)
from forge_ir.provenance import Provenance


@dataclass
class ForgeNode:
    id: str
    role: NodeRole
    subtype: str
    attributes: dict
    provenance: list[Provenance]
    origin: Origin
    lod: int = 1  # 0-4, per connector-research.md §3

    layer: Layer = field(init=False)
    storage_scope: StorageScope = field(init=False)

    def __post_init__(self) -> None:
        storage_scope = storage_scope_for(self.role)
        self.storage_scope = storage_scope
        self.layer = layer_for(self.role, self.subtype) if storage_scope == StorageScope.PERSISTENT \
            else Layer.NOT_APPLICABLE

        if self.origin == Origin.RUNTIME and storage_scope != StorageScope.RUNTIME_ONLY:
            raise ValueError(
                f"Node {self.id!r}: origin=RUNTIME requires a RUNTIME_ONLY role "
                f"(got role={self.role.value}, storage_scope={storage_scope.value})"
            )
        if self.origin != Origin.RUNTIME and storage_scope == StorageScope.RUNTIME_ONLY:
            raise ValueError(
                f"Node {self.id!r}: RUNTIME_ONLY role {self.role.value} requires origin=RUNTIME "
                f"(got origin={self.origin.value})"
            )


@dataclass
class ForgeEdge:
    src: str
    predicate: EdgeType
    dst: str
    qualifier: str | None
    attributes: dict
    provenance: list[Provenance]
    origin: Origin

    # REPRESENTS-only fields -- required together, meaningless otherwise (review round 3, point 9)
    confidence: float | None = None
    match_method: str | None = None
    resolver_version: str | None = None

    storage_scope: StorageScope = field(init=False)

    def __post_init__(self) -> None:
        self.storage_scope = edge_storage_scope(self.predicate)

        if self.predicate == EdgeType.REPRESENTS:
            missing = [f for f in ("confidence", "match_method", "resolver_version")
                       if getattr(self, f) is None]
            if missing:
                raise ValueError(f"REPRESENTS edge {self.src!r}->{self.dst!r} missing required fields: {missing}")

        if self.origin == Origin.RUNTIME and self.storage_scope != StorageScope.RUNTIME_ONLY:
            raise ValueError(
                f"Edge {self.src!r}->{self.dst!r}: origin=RUNTIME requires a RUNTIME_ONLY predicate "
                f"(got predicate={self.predicate.value})"
            )
        if self.origin != Origin.RUNTIME and self.storage_scope == StorageScope.RUNTIME_ONLY:
            raise ValueError(
                f"Edge {self.src!r}->{self.dst!r}: RUNTIME_ONLY predicate {self.predicate.value} "
                f"requires origin=RUNTIME (got origin={self.origin.value})"
            )

    def check_type_legal(self, src_role: NodeRole, src_subtype: str, dst_role: NodeRole, dst_subtype: str) -> bool:
        """Checked by forge_ir/verify.py against the actual endpoint nodes' roles/subtypes --
        an edge cannot validate this itself since it only holds node IDs, not node objects."""
        return is_allowed_edge(src_role, src_subtype, self.predicate, self.qualifier, dst_role, dst_subtype)
