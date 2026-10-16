"""Edge-type statistics over a committed graph.

The question this answers is "which predicate dominates, which don't, and why" -- and
the "why" has to be *derived*, not asserted. A raw predicate histogram tells you
RELATES_TO is 60% of edges; it does not tell you that's because a single ACMG criterion
fans out to one Requirement per strength tier, which is a completely different fact from
"variants relate to genes".

So every predicate's share is reported alongside its concrete *shapes* -- the
(src_subtype, predicate[qualifier], dst_subtype) triples that actually produced those
edges, ranked by count. The dominance explanation is then just the top shape(s) with
their multiplicity per source node, which is a measured number, not an opinion.

Multiplicity is the load-bearing statistic: an edge type is dominant either because
many nodes emit one of it (breadth) or because few nodes emit many of it (fan-out).
`edges_per_source_node` separates those two causes, and they call for different
things -- breadth is inherent to the ontology, fan-out is a modelling choice worth
being able to defend.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from forge_ir.core import EdgeType
from forge_ir.graph import ForgeEdge, ForgeNode


@dataclass
class ShapeStat:
    """One concrete (src_subtype) --predicate[qualifier]--> (dst_subtype) triple."""
    src_subtype: str
    predicate: str
    qualifier: str | None
    dst_subtype: str
    count: int
    distinct_src_nodes: int

    @property
    def edges_per_source_node(self) -> float:
        return round(self.count / self.distinct_src_nodes, 2) if self.distinct_src_nodes else 0.0

    def label(self) -> str:
        q = f"[{self.qualifier}]" if self.qualifier else ""
        return f"{self.src_subtype} —{self.predicate}{q}→ {self.dst_subtype}"

    def to_dict(self) -> dict:
        return {
            "label": self.label(), "src_subtype": self.src_subtype, "predicate": self.predicate,
            "qualifier": self.qualifier, "dst_subtype": self.dst_subtype, "count": self.count,
            "distinct_src_nodes": self.distinct_src_nodes,
            "edges_per_source_node": self.edges_per_source_node,
        }


@dataclass
class PredicateStat:
    predicate: str
    count: int
    share_pct: float
    qualifiers: dict[str, int]
    shapes: list[ShapeStat]
    origins: dict[str, int]
    layer_pairs: dict[str, int]

    def driver(self) -> str:
        """Why this predicate lands where it does in the ranking, stated in terms of the
        single shape that contributes most of its edges and how that shape multiplies."""
        if not self.shapes:
            return "no edges"
        top = self.shapes[0]
        top_share = round(100 * top.count / self.count, 1)
        if top.edges_per_source_node >= 2:
            mechanism = (f"fan-out: each {top.src_subtype} emits "
                         f"{top.edges_per_source_node} on average")
        else:
            mechanism = (f"breadth: {top.distinct_src_nodes} distinct {top.src_subtype} nodes "
                         f"emit ~1 each")
        return f"{top_share}% of them are {top.label()} — {mechanism}"

    def to_dict(self) -> dict:
        return {
            "predicate": self.predicate, "count": self.count, "share_pct": self.share_pct,
            "qualifiers": self.qualifiers, "origins": self.origins, "layer_pairs": self.layer_pairs,
            "driver": self.driver(), "shapes": [s.to_dict() for s in self.shapes],
        }


@dataclass
class EdgeStatsReport:
    total_edges: int
    total_nodes: int
    predicates: list[PredicateStat]          # ranked, most edges first
    unused_predicates: list[str]             # in the ontology, absent from this graph
    edges_per_node: float
    cross_layer_edges: int
    derived_edges: int
    node_subtypes: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total_edges": self.total_edges, "total_nodes": self.total_nodes,
            "edges_per_node": self.edges_per_node, "cross_layer_edges": self.cross_layer_edges,
            "derived_edges": self.derived_edges, "node_subtypes": self.node_subtypes,
            "unused_predicates": self.unused_predicates,
            "predicates": [p.to_dict() for p in self.predicates],
        }

    def format_table(self, shapes_per_predicate: int = 3) -> str:
        lines = [
            "",
            "=" * 88,
            f"EDGE TYPE STATISTICS  ·  {self.total_edges} edges over {self.total_nodes} nodes "
            f"({self.edges_per_node} edges/node)",
            "=" * 88,
            f"{'PREDICATE':<14} {'COUNT':>7} {'SHARE':>7}  DOMINANT SHAPE / DRIVER",
            "-" * 88,
        ]
        for p in self.predicates:
            bar = "█" * max(1, round(p.share_pct / 3))
            lines.append(f"{p.predicate:<14} {p.count:>7} {p.share_pct:>6.1f}%  {bar}")
            lines.append(f"{'':<14} {'':>7} {'':>7}  {p.driver()}")
            for s in p.shapes[1:shapes_per_predicate]:
                lines.append(f"{'':<14} {s.count:>7} {100 * s.count / self.total_edges:>6.1f}%  "
                             f"  · {s.label()}  ({s.edges_per_source_node}/src)")
            lines.append("")
        if self.unused_predicates:
            lines.append(f"Ontology predicates with zero edges in this run: "
                         f"{', '.join(self.unused_predicates)}")
        lines.append(f"Cross-layer edges: {self.cross_layer_edges} "
                     f"({round(100 * self.cross_layer_edges / self.total_edges, 1) if self.total_edges else 0}%)"
                     f"  ·  DERIVED-origin edges: {self.derived_edges}")
        lines.append("=" * 88)
        return "\n".join(lines)


class EdgeStatsAccumulator:
    """Incremental edge statistics -- `observe_node`/`observe_edge` as they commit, then
    `report()` once at the end.

    Exists because `compute_edge_stats(nodes, edges)` requires the ENTIRE graph resident
    in memory as ForgeNode/ForgeEdge objects, which is fine for a 9,700-node gene family
    and not fine for a half-million-node panel harvest (orchestration/harvest.py streams
    the real objects straight to disk and never holds them all). Everything the statistics
    actually need from a node is its subtype and layer -- two short strings -- so the
    accumulator keeps those and nothing else.

    `compute_edge_stats` below is now a thin wrapper over this, so there is exactly one
    implementation of the counting rather than a batch version and a streaming version
    that drift apart."""

    def __init__(self) -> None:
        self._subtype: dict[str, str] = {}
        self._layer: dict[str, str] = {}
        self._node_subtypes: Counter[str] = Counter()
        self.shape_counts: Counter[tuple[str, str, str | None, str]] = Counter()
        self.shape_sources: defaultdict[tuple[str, str, str | None, str], set[str]] = defaultdict(set)
        self.predicate_counts: Counter[str] = Counter()
        self.predicate_qualifiers: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self.predicate_origins: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self.predicate_layer_pairs: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self.total_edges = 0
        self.total_nodes = 0
        self.cross_layer_edges = 0
        self.derived_edges = 0

    def observe_node(self, node_id: str, subtype: str, layer: str) -> None:
        if node_id in self._subtype:
            return
        self._subtype[node_id] = subtype
        self._layer[node_id] = layer
        self._node_subtypes[subtype] += 1
        self.total_nodes += 1

    def observe_edge(self, src: str, predicate: str, qualifier: str | None, dst: str,
                      origin: str) -> None:
        # "<resident>"/"RESIDENT" is a persistent-layer anchor declared by a prior run --
        # a real, expected case in ephemeral mode, not a missing node.
        src_st = self._subtype.get(src, "<resident>")
        dst_st = self._subtype.get(dst, "<resident>")
        key = (src_st, predicate, qualifier, dst_st)
        self.shape_counts[key] += 1
        self.shape_sources[key].add(src)
        self.predicate_counts[predicate] += 1
        self.predicate_qualifiers[predicate][qualifier or "—"] += 1
        self.predicate_origins[predicate][origin] += 1

        src_layer = self._layer.get(src, "RESIDENT")
        dst_layer = self._layer.get(dst, "RESIDENT")
        self.predicate_layer_pairs[predicate][f"{src_layer}→{dst_layer}"] += 1
        if src_layer != dst_layer:
            self.cross_layer_edges += 1
        if origin == "DERIVED":
            self.derived_edges += 1
        self.total_edges += 1

    def report(self) -> EdgeStatsReport:
        total = self.total_edges
        predicates: list[PredicateStat] = []
        for predicate, count in self.predicate_counts.most_common():
            shapes = [
                ShapeStat(src_subtype=k[0], predicate=k[1], qualifier=k[2], dst_subtype=k[3],
                          count=c, distinct_src_nodes=len(self.shape_sources[k]))
                for k, c in self.shape_counts.most_common() if k[1] == predicate
            ]
            predicates.append(PredicateStat(
                predicate=predicate,
                count=count,
                share_pct=round(100 * count / total, 1) if total else 0.0,
                qualifiers=dict(self.predicate_qualifiers[predicate].most_common()),
                shapes=shapes,
                origins=dict(self.predicate_origins[predicate]),
                layer_pairs=dict(self.predicate_layer_pairs[predicate].most_common(4)),
            ))

        return EdgeStatsReport(
            total_edges=total,
            total_nodes=self.total_nodes,
            predicates=predicates,
            unused_predicates=[t.value for t in EdgeType if t.value not in self.predicate_counts],
            edges_per_node=round(total / self.total_nodes, 2) if self.total_nodes else 0.0,
            cross_layer_edges=self.cross_layer_edges,
            derived_edges=self.derived_edges,
            node_subtypes=dict(self._node_subtypes.most_common()),
        )


def compute_edge_stats(nodes: list[ForgeNode], edges: list[ForgeEdge]) -> EdgeStatsReport:
    acc = EdgeStatsAccumulator()
    for n in nodes:
        acc.observe_node(n.id, n.subtype, n.layer.value)
    for e in edges:
        acc.observe_edge(e.src, e.predicate.value, e.qualifier, e.dst, e.origin.value)
    return acc.report()
