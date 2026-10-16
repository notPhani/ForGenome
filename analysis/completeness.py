"""Advisory graph-completeness report -- cross-references what a run's registered
connectors DECLARE they provide (`SourceAdapter.capabilities()["provides"]`) and what the
frozen ontology DEFINES (`NODE_SUBTYPE_REGISTRY`, `ALLOWED_EDGE_COMBINATIONS`) against
what the committed graph ACTUALLY contains.

Deliberately advisory only, never blocking (per project decision): this reports gaps,
it never rejects a batch over one. `forge_ir/verify.py`'s 9 checks are the blocking
gate; this is a separate, read-only lens for "how much of what we could produce did we
actually produce," which is a completeness question, not a correctness one -- a graph
can be 100% correct (every check in verify.py passes) while covering a small fraction of
what the ontology defines, simply because MHLA doesn't exist yet or a connector wasn't
enabled for this run. Reporting that honestly is the whole point.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from forge_ir.core import ALLOWED_EDGE_COMBINATIONS, NODE_SUBTYPE_REGISTRY, StorageScope, edge_storage_scope
from forge_ir.graph import ForgeEdge, ForgeNode
from orchestration.connector_registry import ConnectorDescriptor


@dataclass
class ConnectorCompleteness:
    connector: str
    implemented: bool
    declared_subtypes: list[str]     # from adapter.capabilities()["provides"]
    present_subtypes: list[str]      # of those, which actually appear in the graph
    missing_subtypes: list[str]      # declared but absent this run
    coverage_pct: float


@dataclass
class GraphCompletenessReport:
    connectors: list[ConnectorCompleteness] = field(default_factory=list)
    registered_subtypes_total: int = 0
    subtypes_populated: int = 0
    unpopulated_subtypes: list[str] = field(default_factory=list)
    exercisable_edge_combinations_total: int = 0   # excludes RUNTIME_ONLY (MHLA-only) combos
    edge_combinations_exercised: int = 0
    runtime_only_combinations_deferred: int = 0    # ACTIVATED_BY etc. -- expected absent pre-MHLA

    def overall_node_coverage_pct(self) -> float:
        return round(100 * self.subtypes_populated / self.registered_subtypes_total, 1) \
            if self.registered_subtypes_total else 0.0

    def overall_edge_coverage_pct(self) -> float:
        return round(100 * self.edge_combinations_exercised / self.exercisable_edge_combinations_total, 1) \
            if self.exercisable_edge_combinations_total else 0.0

    def format_table(self) -> str:
        lines = [
            "", "=" * 88,
            "GRAPH COMPLETENESS (advisory -- forge_ir/verify.py's 9 checks are the real gate)",
            "=" * 88,
            f"Node subtypes populated:  {self.subtypes_populated}/{self.registered_subtypes_total} "
            f"({self.overall_node_coverage_pct()}%)",
            f"Edge combinations used:   {self.edge_combinations_exercised}/{self.exercisable_edge_combinations_total} "
            f"({self.overall_edge_coverage_pct()}%)  "
            f"[{self.runtime_only_combinations_deferred} more reserved for MHLA, not counted against this]",
        ]
        if self.unpopulated_subtypes:
            lines.append(f"Unpopulated subtypes: {', '.join(sorted(self.unpopulated_subtypes))}")
        lines.append("-" * 88)
        for c in self.connectors:
            status = "" if c.implemented else "  [STUB -- not yet implemented]"
            lines.append(f"{c.connector:<20} {c.coverage_pct:>5.1f}%  "
                         f"({len(c.present_subtypes)}/{len(c.declared_subtypes)} declared subtypes present)"
                         f"{status}")
            if c.missing_subtypes:
                lines.append(f"{'':<20}   missing: {', '.join(c.missing_subtypes)}")
        lines.append("=" * 88)
        return "\n".join(lines)


def compute_completeness(nodes: list[ForgeNode], edges: list[ForgeEdge],
                          descriptors: list[ConnectorDescriptor]) -> GraphCompletenessReport:
    present_subtypes = {n.subtype for n in nodes}
    all_registered_subtypes = {subtype for (_role, subtype) in NODE_SUBTYPE_REGISTRY}

    connectors = []
    for descriptor in descriptors:
        try:
            declared = list(descriptor.adapter.capabilities().get("provides", []))
        except Exception:
            declared = []   # a stub connector's adapter may not implement capabilities() meaningfully
        present = [s for s in declared if s in present_subtypes]
        missing = [s for s in declared if s not in present_subtypes]
        coverage = round(100 * len(present) / len(declared), 1) if declared else 0.0
        connectors.append(ConnectorCompleteness(
            connector=descriptor.name, implemented=descriptor.implemented,
            declared_subtypes=declared, present_subtypes=present, missing_subtypes=missing,
            coverage_pct=coverage,
        ))

    exercisable = [c for c in ALLOWED_EDGE_COMBINATIONS if edge_storage_scope(c.predicate) != StorageScope.RUNTIME_ONLY]
    runtime_only_count = len(ALLOWED_EDGE_COMBINATIONS) - len(exercisable)

    by_id = {n.id: n for n in nodes}
    exercised = 0
    for combo in exercisable:
        hit = False
        for e in edges:
            src, dst = by_id.get(e.src), by_id.get(e.dst)
            if src is None or dst is None:
                continue
            if combo.matches(src.role, src.subtype, e.predicate, e.qualifier, dst.role, dst.subtype):
                hit = True
                break
        if hit:
            exercised += 1

    return GraphCompletenessReport(
        connectors=connectors,
        registered_subtypes_total=len(all_registered_subtypes),
        subtypes_populated=len(present_subtypes & all_registered_subtypes),
        unpopulated_subtypes=sorted(all_registered_subtypes - present_subtypes),
        exercisable_edge_combinations_total=len(exercisable),
        edge_combinations_exercised=exercised,
        runtime_only_combinations_deferred=runtime_only_count,
    )
