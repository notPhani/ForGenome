"""Evidence-grounded graph retrieval for genome reports.

This module is intentionally dependency-light.  It provides a deterministic
baseline for the Graph RAG design described in the project documentation:

* typed graph loading from the existing visualizer/event-log format;
* hard metapath filtering before attention/RWR ranking;
* three interpretable attention heads (rarity, functional, clinical);
* budget-aware evidence selection and compact serialization;
* structured-claim auditing against immutable edge identifiers; and
* a self-contained HTML/SVG report for a selected genome neighbourhood.

It is not a trained GNN and it does not make a clinical diagnosis.  The
attention heads are transparent heuristics until task-specific training and
calibration data are available.  Keeping the first implementation
deterministic makes it useful offline and makes safety failures testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import gzip
from html import escape
import json
import math
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _text(value).lower()).strip()


@dataclass(frozen=True)
class GraphNode:
    id: str
    subtype: str = "Unknown"
    label: str = ""
    role: str = ""
    layer: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)
    provenance: tuple[Mapping[str, Any], ...] = ()

    @property
    def searchable_text(self) -> str:
        values = [self.id, self.label, self.subtype, self.role]
        values.extend(_text(v) for v in self.attributes.values())
        return _normal(" ".join(values))


@dataclass(frozen=True)
class GraphEdge:
    edge_id: str
    src: str
    predicate: str
    dst: str
    qualifier: str | None = None
    origin: str = "SOURCE"
    attributes: Mapping[str, Any] = field(default_factory=dict)
    provenance: tuple[Mapping[str, Any], ...] = ()

    @property
    def source_ids(self) -> tuple[str, ...]:
        result: list[str] = []
        for p in self.provenance:
            if not isinstance(p, Mapping):
                continue
            source = p.get("source") or p.get("source_id")
            record = p.get("record_id")
            version = p.get("version")
            if source:
                result.append(":".join(_text(v) for v in (source, record, version) if v))
        return tuple(dict.fromkeys(result))


@dataclass(frozen=True)
class MetapathRule:
    src_type: str | None
    predicate: str
    qualifier: str | None
    dst_type: str | None

    def matches(self, src: GraphNode, edge: GraphEdge, dst: GraphNode) -> bool:
        return (
            (self.src_type is None or self.src_type == src.subtype)
            and self.predicate == edge.predicate
            and (self.qualifier is None or self.qualifier == edge.qualifier)
            and (self.dst_type is None or self.dst_type == dst.subtype)
        )


class MetapathGrammar:
    """Deterministic transition grammar.

    ``rules`` are checked before an edge can enter the retrieval frontier.
    Supplying ``sequence`` makes acceptance strict: a path must contain the
    exact node-type sequence.  This supports workflows such as
    Variant -> Gene -> Phenotype -> ACMGCriterion -> Disease without asking
    an LLM to enforce them.
    """

    def __init__(
        self,
        rules: Iterable[MetapathRule],
        sequence: Sequence[str] | None = None,
        name: str = "clinical-default",
    ) -> None:
        self.rules = tuple(rules)
        self.sequence = tuple(sequence) if sequence else None
        self.name = name

    @classmethod
    def default(cls) -> "MetapathGrammar":
        # These are the typed relationships emitted by the current Forge IR.
        # They are deliberately narrower than "all graph edges".
        rules = [
            MetapathRule("Variant", "RELATES_TO", "in_gene", "Gene"),
            MetapathRule("Variant", "RELATES_TO", "has_consequence", "Transcript"),
            MetapathRule("Transcript", "PART_OF", None, "Gene"),
            MetapathRule("Transcript", "RELATES_TO", "encodes", "Protein"),
            MetapathRule("Observation", "OBSERVES", None, "Variant"),
            MetapathRule("CaseObservation", "SUPPORTS", "corroborates", "ClinicalAssertion"),
            MetapathRule("Observation", "SUPPORTS", "satisfies_requirement", "Requirement"),
            MetapathRule("ClinicalAssertion", "ASSERTS", None, "Variant"),
            MetapathRule("AggregateClassification", "ASSERTS", None, "Variant"),
            MetapathRule("ClinicalAssertion", "RELATES_TO", "for_condition", "Disease"),
            MetapathRule("AggregateClassification", "RELATES_TO", "for_condition", "Disease"),
            MetapathRule("ACMGCriterion", "APPLIES_TO", None, "Gene"),
            MetapathRule("ACMGCriterion", "REQUIRES", None, "Requirement"),
            # Optional complete diagnostic route.  It is intentionally not
            # present in every Forge snapshot; absent links must remain an
            # explicit evidence gap rather than being inferred.
            MetapathRule("Gene", "ASSOCIATED_WITH", None, "Phenotype"),
            MetapathRule("Phenotype", "SUPPORTS_CRITERION", None, "ACMGCriterion"),
            MetapathRule("ACMGCriterion", "SUPPORTS", None, "Disease"),
        ]
        return cls(rules, sequence=("Variant", "Gene", "Phenotype", "ACMGCriterion", "Disease"),
                   name="forge-clinical-default")

    @classmethod
    def from_sequence(cls, node_types: Sequence[str], transitions: Sequence[Mapping[str, Any]]) -> "MetapathGrammar":
        rules = [MetapathRule(
            item.get("src_type"), item["predicate"], item.get("qualifier"), item.get("dst_type")
        ) for item in transitions]
        return cls(rules, sequence=node_types, name="configured-clinical")

    def allows(self, src: GraphNode, edge: GraphEdge, dst: GraphNode) -> bool:
        return any(rule.matches(src, edge, dst) for rule in self.rules)

    def accepts(self, nodes: Sequence[GraphNode], edges: Sequence[GraphEdge]) -> bool:
        if not edges or len(nodes) != len(edges) + 1:
            return False
        if self.sequence is not None:
            return tuple(n.subtype for n in nodes) == self.sequence
        return all(self.allows(nodes[i], edges[i], nodes[i + 1]) for i in range(len(edges)))


def _default_label(raw: Mapping[str, Any]) -> str:
    attrs = raw.get("attributes") or {}
    return _text(raw.get("label") or attrs.get("symbol") or attrs.get("name") or raw.get("id"))


class GraphSnapshot:
    """Immutable-in-use graph view with stable edge IDs for one input snapshot."""

    def __init__(self, nodes: Sequence[GraphNode], edges: Sequence[GraphEdge], snapshot_id: str = "local") -> None:
        self.nodes = tuple(nodes)
        self.edges = tuple(edges)
        self.snapshot_id = snapshot_id
        self.node_by_id = {n.id: n for n in self.nodes}
        self.edge_by_id = {e.edge_id: e for e in self.edges}
        self.outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
        self.incident: dict[str, list[GraphEdge]] = defaultdict(list)
        for e in self.edges:
            self.outgoing[e.src].append(e)
            self.incident[e.src].append(e)
            self.incident[e.dst].append(e)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | Sequence[Mapping[str, Any]], snapshot_id: str = "local") -> "GraphSnapshot":
        events = payload.get("events", []) if isinstance(payload, Mapping) else payload
        raw_nodes: dict[str, Mapping[str, Any]] = {}
        raw_edges: list[Mapping[str, Any]] = []
        for item in events:
            if item.get("kind") == "node":
                raw_nodes[item["id"]] = item
            elif item.get("kind") == "edge":
                raw_edges.append(item)
        nodes = [GraphNode(
            id=_text(raw["id"]), subtype=_text(raw.get("subtype") or raw.get("type") or "Unknown"),
            label=_default_label(raw), role=_text(raw.get("role")), layer=_text(raw.get("layer")),
            attributes=raw.get("attributes") or {},
            provenance=tuple(raw.get("provenance") or ()),
        ) for raw in raw_nodes.values()]
        edges: list[GraphEdge] = []
        for index, raw in enumerate(raw_edges, 1):
            edge_id = _text(raw.get("edge_id") or raw.get("id") or f"E{index:06d}")
            provenance = raw.get("provenance") or (
                [{"source": raw.get("feed")}] if raw.get("feed") else []
            )
            edges.append(GraphEdge(
                edge_id=edge_id, src=_text(raw["src"]), dst=_text(raw["dst"]),
                predicate=_text(raw.get("predicate") or raw.get("relation")),
                qualifier=raw.get("qualifier"), origin=_text(raw.get("origin") or "SOURCE"),
                attributes=raw.get("attributes") or {}, provenance=tuple(provenance),
            ))
        # Event logs can point into a resident persistent snapshot.  Keep those
        # endpoints visible in the report, but label them as unresolved rather
        # than silently pretending their attributes were loaded.
        known = {n.id for n in nodes}
        for e in edges:
            for node_id in (e.src, e.dst):
                if node_id not in known:
                    nodes.append(GraphNode(node_id, label=node_id, subtype="Resident"))
                    known.add(node_id)
        return cls(nodes, edges, snapshot_id=snapshot_id)

    @classmethod
    def from_json(cls, path: str | Path) -> "GraphSnapshot":
        path = Path(path)
        if path.suffix == ".gz":
            # Large harvests are written as replayable JSONL gzip dumps.  The
            # visualizer builder uses the same record shape; accepting it here
            # lets the analyzer work before a separate downsample step.
            events = []
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        events.append(json.loads(line))
            payload: Mapping[str, Any] | Sequence[Mapping[str, Any]] = events
        else:
            with path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
        return cls.from_payload(payload, snapshot_id=path.name)

    def match(self, terms: Sequence[str], subtype: str | None = None) -> list[GraphNode]:
        wanted = [_normal(term) for term in terms if _normal(term)]
        if not wanted:
            return []
        pool = [n for n in self.nodes if subtype is None or n.subtype == subtype]
        if subtype == "Gene":
            # A gene query should anchor on the Gene record, not every variant
            # whose condition, provenance, or annotation happens to mention the
            # same symbol.  Prefer canonical symbol/label/id matches.
            exact = [n for n in pool if any(
                term == _normal(n.label)
                or term == _normal(n.attributes.get("symbol"))
                or term == _normal(n.id)
                for term in wanted
            )]
            if exact:
                return exact
        if subtype == "Variant":
            exact = [n for n in pool if any(term == _normal(n.id) or term == _normal(n.label) for term in wanted)]
            if exact:
                return exact
        return [n for n in pool if any(term in n.searchable_text for term in wanted)]

    def related_seed_nodes(self, seeds: Sequence[GraphNode], max_hops: int = 1) -> list[GraphNode]:
        """Add nearby nodes to a query seed without treating co-occurrence as evidence."""
        seen = {n.id for n in seeds}
        queue = deque((n.id, 0) for n in seeds)
        while queue:
            node_id, depth = queue.popleft()
            if depth >= max_hops:
                continue
            for edge in self.incident.get(node_id, ()):
                other = edge.dst if edge.src == node_id else edge.src
                if other not in seen and other in self.node_by_id:
                    seen.add(other)
                    queue.append((other, depth + 1))
        return [self.node_by_id[node_id] for node_id in seen]


def _numeric(attrs: Mapping[str, Any], names: Sequence[str]) -> float | None:
    for key, value in attrs.items():
        if any(name in _normal(key).replace(" ", "_") for name in names):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _softmax(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    peak = max(values)
    exps = [math.exp(max(-50.0, min(50.0, v - peak))) for v in values]
    total = sum(exps) or 1.0
    return [v / total for v in exps]


class GraphRetriever:
    """Typed, constrained, heuristic multi-head retrieval baseline."""

    RELATION_WEIGHT = {
        "ASSERTS": 1.15, "RELATES_TO": 1.0, "SUPPORTS": 1.1,
        "APPLIES_TO": 1.1, "OBSERVES": 0.95, "REQUIRES": 0.8,
        "PART_OF": 0.7,
    }

    def __init__(self, grammar: MetapathGrammar | None = None, restart: float = 0.25, temperature: float = 1.0) -> None:
        if not 0 < restart <= 1:
            raise ValueError("restart must be in (0, 1]")
        self.grammar = grammar or MetapathGrammar.default()
        self.restart = restart
        self.temperature = temperature

    def _head_node_score(self, node: GraphNode, head: str) -> float:
        subtype = _normal(node.subtype)
        attrs = node.attributes
        if head == "rarity":
            af = _numeric(attrs, ("af", "allele_frequency", "frequency"))
            if af is not None:
                return max(0.0, min(1.0, 1.0 - af))
            return 0.85 if "population" in subtype or "frequency" in subtype else 0.2
        if head == "functional":
            signal = sum(term in subtype for term in ("functional", "prediction", "protein", "transcript", "consequence"))
            return min(1.0, 0.25 + 0.2 * signal)
        signal = sum(term in subtype for term in ("clinical", "assertion", "criterion", "disease", "requirement", "drug"))
        return min(1.0, 0.2 + 0.18 * signal)

    def _edge_logits(self, snapshot: GraphSnapshot, edges: Sequence[GraphEdge]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for e in edges:
            src, dst = snapshot.node_by_id[e.src], snapshot.node_by_id[e.dst]
            values = {}
            for head in ("rarity", "functional", "clinical"):
                values[head] = (
                    0.6 * self._head_node_score(dst, head)
                    + 0.25 * self._head_node_score(src, head)
                    + math.log(self.RELATION_WEIGHT.get(e.predicate, 0.6))
                )
            out[e.edge_id] = values
        return out

    def _valid_edges(self, snapshot: GraphSnapshot, node_id: str) -> list[GraphEdge]:
        return [e for e in snapshot.outgoing.get(node_id, ())
                if e.src in snapshot.node_by_id and e.dst in snapshot.node_by_id
                and self.grammar.allows(snapshot.node_by_id[e.src], e, snapshot.node_by_id[e.dst])]

    def retrieve(self, snapshot: GraphSnapshot, terms: Sequence[str], lod: int = 2,
                 token_budget: int = 12000, max_hops: int = 4,
                 seed_subtype: str | None = None) -> "EvidenceSubgraph":
        matched = snapshot.match(terms, subtype=seed_subtype)
        if not matched:
            raise ValueError(f"No graph nodes matched query terms: {', '.join(terms)}")
        # Expand a gene/variant query by one hop, but only when that hop is
        # admissible in at least one direction of the grammar.  A generic
        # incident-neighbour expansion would pull unrelated protein noise into
        # the evidence subgraph before constraints had a chance to act.
        seed_ids = {n.id for n in matched}
        for node in matched:
            for edge in snapshot.incident.get(node.id, ()):
                other_id = edge.dst if edge.src == node.id else edge.src
                if other_id not in snapshot.node_by_id:
                    continue
                other = snapshot.node_by_id[other_id]
                if ((edge.src == node.id and self.grammar.allows(node, edge, other))
                        or (edge.dst == node.id and self.grammar.allows(other, edge, node))):
                    seed_ids.add(other_id)
        seeds = [snapshot.node_by_id[node_id] for node_id in seed_ids]
        seed_ids = {n.id for n in seeds}
        logits = self._edge_logits(snapshot, snapshot.edges)
        # RWR over grammar-admissible outgoing transitions only.
        ids = [n.id for n in snapshot.nodes]
        scores = {node_id: (1.0 / len(seed_ids) if node_id in seed_ids else 0.0) for node_id in ids}
        for _ in range(40):
            next_scores = {node_id: 0.0 for node_id in ids}
            for node_id in ids:
                candidates = self._valid_edges(snapshot, node_id)
                if candidates:
                    combined = [sum(logits[e.edge_id].values()) / 3 for e in candidates]
                    probs = _softmax([self.temperature * x for x in combined])
                    for e, prob in zip(candidates, probs):
                        next_scores[e.dst] += (1 - self.restart) * scores[node_id] * prob
                else:
                    next_scores[node_id] += (1 - self.restart) * scores[node_id]
                if node_id in seed_ids:
                    next_scores[node_id] += self.restart / len(seed_ids)
            if max(abs(next_scores[k] - scores[k]) for k in ids) < 1e-10:
                scores = next_scores
                break
            scores = next_scores

        ranked_nodes = sorted(snapshot.nodes, key=lambda n: (scores[n.id], n.id), reverse=True)
        candidates = {n.id for n in ranked_nodes if scores[n.id] > 0 or n.id in seed_ids}
        # Ensure selected seed neighbourhood is plot-visible even when a seed is a sink.
        for n in seeds:
            candidates.add(n.id)
        paths = self._find_paths(snapshot, seeds, max_hops=max_hops)
        selected_edges = [e for e in snapshot.edges if e.src in candidates and e.dst in candidates
                          and self.grammar.allows(snapshot.node_by_id[e.src], e, snapshot.node_by_id[e.dst])]
        selected_edges.sort(key=lambda e: (sum(logits[e.edge_id].values()), scores[e.dst], e.edge_id), reverse=True)
        selected_nodes = [n for n in ranked_nodes if n.id in candidates]
        selected_nodes, selected_edges, budget_exhausted = _budget_select(
            selected_nodes, selected_edges, paths, snapshot, token_budget, lod
        )
        included = {n.id for n in selected_nodes}
        paths = [p for p in paths if all(node_id in included for node_id in p.node_ids)
                 and all(edge_id in {e.edge_id for e in selected_edges} for edge_id in p.edge_ids)]
        heads = {n.id: {head: self._head_node_score(n, head) for head in ("rarity", "functional", "clinical")}
                 for n in selected_nodes}
        return EvidenceSubgraph(
            snapshot_id=snapshot.snapshot_id, query_terms=tuple(terms), lod=lod,
            seeds=tuple(n.id for n in seeds), nodes=tuple(selected_nodes), edges=tuple(selected_edges),
            paths=tuple(paths), rwr_scores={n.id: scores[n.id] for n in selected_nodes},
            head_scores=heads, budget_exhausted=budget_exhausted, grammar_name=self.grammar.name,
        )

    def _find_paths(self, snapshot: GraphSnapshot, seeds: Sequence[GraphNode], max_hops: int) -> list["EvidencePath"]:
        found: list[EvidencePath] = []

        def walk(nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
            if edges and self.grammar.accepts(nodes, edges):
                found.append(EvidencePath(f"P{len(found) + 1:04d}", tuple(n.id for n in nodes), tuple(e.edge_id for e in edges)))
                if self.grammar.sequence is not None:
                    return
            if len(edges) >= max_hops:
                return
            for edge in self._valid_edges(snapshot, nodes[-1].id):
                if edge.dst in {n.id for n in nodes}:
                    continue
                walk(nodes + [snapshot.node_by_id[edge.dst]], edges + [edge])

        for seed in seeds:
            walk([seed], [])
        return found


@dataclass(frozen=True)
class EvidencePath:
    path_id: str
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceSubgraph:
    snapshot_id: str
    query_terms: tuple[str, ...]
    lod: int
    seeds: tuple[str, ...]
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    paths: tuple[EvidencePath, ...]
    rwr_scores: Mapping[str, float]
    head_scores: Mapping[str, Mapping[str, float]]
    budget_exhausted: bool
    grammar_name: str

    @property
    def edge_by_id(self) -> dict[str, GraphEdge]:
        return {e.edge_id: e for e in self.edges}

    def serialize(self) -> str:
        """Compact typed triples suitable as an LLM evidence context."""
        lines = [f"Q:{','.join(_safe_token(t) for t in self.query_terms)}",
                 f"SNAPSHOT:{_safe_token(self.snapshot_id)}", f"LOD:{self.lod}",
                 f"SEEDS:{','.join(self.seeds)}", "NODES:"]
        for n in self.nodes:
            lines.append(f"{n.id}|{n.subtype}|{_safe_token(n.label)}")
        lines.append("EDGES:")
        for e in self.edges:
            sources = ",".join(e.source_ids) or "UNATTRIBUTED"
            qualifier = e.qualifier or "-"
            lines.append(f"{e.edge_id}|{e.src}|{e.predicate}[{qualifier}]|{e.dst}|{sources}")
        lines.append("PATHS:")
        lines.extend(f"{p.path_id}={' > '.join(p.edge_ids)}" for p in self.paths)
        lines.append("SCORES:")
        lines.extend(f"{n.id}|rwr={self.rwr_scores.get(n.id, 0):.5f}" for n in self.nodes[:20])
        return "\n".join(lines)


def _safe_token(value: Any) -> str:
    return re.sub(r"[|\n\r]", " ", _text(value)).strip()


def _budget_select(nodes: Sequence[GraphNode], edges: Sequence[GraphEdge], paths: Sequence[EvidencePath],
                  snapshot: GraphSnapshot, budget: int, lod: int) -> tuple[list[GraphNode], list[GraphEdge], bool]:
    if budget <= 0:
        return [], [], True
    # Approximate tokens conservatively from serialized characters.  LoD only
    # changes how much evidence is allowed, not the clinical grammar.
    scale = {1: 0.55, 2: 1.0, 3: 1.8}.get(lod, 1.0)
    effective = max(80, int(budget * scale))
    node_map = {n.id: n for n in nodes}
    chosen_nodes: list[GraphNode] = []
    chosen_edges: list[GraphEdge] = []
    used = 0
    mandatory = {edge_id for p in paths for edge_id in p.edge_ids}
    edge_map = {e.edge_id: e for e in edges}

    def add_edge(edge: GraphEdge) -> bool:
        nonlocal used
        if edge.edge_id in {e.edge_id for e in chosen_edges}:
            return True
        needed = 30 + len(edge.src) + len(edge.dst) + len(edge.predicate) + len(edge.source_ids) * 15
        if used + needed > effective:
            return False
        for node_id in (edge.src, edge.dst):
            if node_id not in {n.id for n in chosen_nodes} and node_id in node_map:
                chosen_nodes.append(node_map[node_id])
                used += 10 + len(node_map[node_id].label)
        chosen_edges.append(edge)
        used += needed
        return True

    exhausted = False
    for edge_id in mandatory:
        if edge_id in edge_map and not add_edge(edge_map[edge_id]):
            exhausted = True
    for edge in edges:
        if not add_edge(edge):
            exhausted = True
    for node in nodes:
        if node.id not in {n.id for n in chosen_nodes}:
            needed = 10 + len(node.label)
            if used + needed <= effective:
                chosen_nodes.append(node)
                used += needed
            else:
                exhausted = True
    return chosen_nodes, chosen_edges, exhausted


@dataclass(frozen=True)
class AuditResult:
    accepted: bool
    code: str
    message: str
    claim_id: str


class ClaimAuditor:
    """Independent deterministic gate for generated structured claims."""

    CLAIM_TYPES = {"direct_observation", "source_assertion", "graph_derived_inference", "unresolved"}
    CAUSAL_RELATIONS = {"CAUSES", "CAUSAL", "TREATS", "CONTRAINDICATED_FOR"}

    def __init__(self, evidence: EvidenceSubgraph, grammar: MetapathGrammar | None = None) -> None:
        self.evidence = evidence
        self.grammar = grammar or MetapathGrammar.default()
        self.nodes = {n.id: n for n in evidence.nodes}
        self.edges = evidence.edge_by_id
        self.paths = {p.path_id: p for p in evidence.paths}
        self.sources = {source for e in evidence.edges for source in e.source_ids}

    def audit(self, claim: Mapping[str, Any]) -> AuditResult:
        claim_id = _text(claim.get("claim_id") or "UNKNOWN")
        claim_type = _text(claim.get("claim_type"))
        if not claim.get("text") or claim_type not in self.CLAIM_TYPES:
            return AuditResult(False, "INVALID_SCHEMA", "claim text or claim_type is invalid", claim_id)
        edge_ids = list(claim.get("supporting_edge_ids") or [])
        if claim_type != "unresolved" and not edge_ids:
            return AuditResult(False, "MISSING_EVIDENCE", "supported claims need Edge IDs", claim_id)
        edges: list[GraphEdge] = []
        for edge_id in edge_ids:
            if edge_id not in self.edges:
                return AuditResult(False, f"UNKNOWN_EDGE:{edge_id}", "the Edge ID is absent from this evidence snapshot", claim_id)
            edges.append(self.edges[edge_id])
        relation = claim.get("relation") or {}
        if relation:
            if not isinstance(relation, Mapping):
                return AuditResult(False, "INVALID_RELATION", "relation must be an object", claim_id)
            if not any(
                relation.get("src") == e.src and relation.get("dst") == e.dst
                and relation.get("predicate") == e.predicate
                and (relation.get("qualifier") is None or relation.get("qualifier") == e.qualifier)
                for e in edges
            ):
                return AuditResult(False, "RELATION_MISMATCH", "claim relation does not match a cited edge", claim_id)
        cited_sources = set(claim.get("source_ids") or [])
        if not cited_sources.issubset(self.sources):
            return AuditResult(False, "INVALID_SOURCE", "one or more source IDs are not in the evidence snapshot", claim_id)
        for path_id in claim.get("path_ids") or []:
            path = self.paths.get(path_id)
            if path is None:
                return AuditResult(False, "UNKNOWN_PATH", "the cited path is absent from this evidence snapshot", claim_id)
            if not set(path.edge_ids).issubset(set(edge_ids)):
                return AuditResult(False, "PATH_EDGE_MISSING", "a cited path contains an uncited edge", claim_id)
            if not self._path_is_connected(path):
                return AuditResult(False, "DISCONNECTED_PATH", "path edges do not connect in order", claim_id)
            nodes = [self.nodes.get(node_id) for node_id in path.node_ids]
            if any(n is None for n in nodes) or not self.grammar.accepts(nodes, [self.edges[e] for e in path.edge_ids]):
                return AuditResult(False, "INVALID_CLINICAL_PATH", "path fails the deterministic metapath grammar", claim_id)
        lower = _normal(claim["text"])
        if any(word in lower.split() for word in ("causes", "causal", "treats", "cures")) \
                and not any(e.predicate in self.CAUSAL_RELATIONS for e in edges):
            return AuditResult(False, "SEMANTICS_TOO_STRONG", "wording claims causation/actionability not present in cited relations", claim_id)
        return AuditResult(True, "ACCEPTED", "claim is traceable to the supplied evidence", claim_id)

    def _path_is_connected(self, path: EvidencePath) -> bool:
        if len(path.node_ids) != len(path.edge_ids) + 1:
            return False
        return all(self.edges[eid].src == path.node_ids[i] and self.edges[eid].dst == path.node_ids[i + 1]
                   for i, eid in enumerate(path.edge_ids))


def build_system_prompt() -> str:
    """Return the stateless prompt contract; the application still audits output."""
    return ("You are an evidence-constrained genome knowledge-graph reporting engine. "
            "Use only the query, nodes, edges, paths and sources supplied in this request. "
            "Every factual graph claim must cite existing Edge IDs; cite every edge in a multi-hop path. "
            "Do not infer causation from association, invent identifiers, or fill evidence gaps with general knowledge. "
            "Return JSON claims with claim_id, text, claim_type, supporting_edge_ids, path_ids, source_ids, "
            "and limitations. Uncertainty and contradictions must be preserved. A deterministic auditor decides release.")


def explain_terms() -> dict[str, str]:
    return {
        "graph": "A set of connected records: nodes are things such as genes or variants, and edges are typed relationships.",
        "Edge ID": "A stable identifier for one relationship. Citing it lets a reviewer trace a statement back to the graph.",
        "metapath": "A permitted sequence of node and relationship types. It prevents biologically plausible but clinically invalid wandering.",
        "attention head": "One relevance lens. This baseline uses rarity, functional impact, and clinical evidence lenses; these are ranking signals, not diagnoses.",
        "RWR score": "A relevance score from repeatedly walking along allowed edges while returning to the query seeds.",
        "provenance": "Where a record came from, including source, record identifier, and version when available.",
        "LoD": "Level of detail: 1 is a concise overview, 2 is standard evidence, and 3 retains deeper evidence within the budget.",
    }


def plain_language_summary(evidence: EvidenceSubgraph) -> str:
    predicates: dict[str, int] = defaultdict(int)
    for edge in evidence.edges:
        predicates[edge.predicate] += 1
    top = ", ".join(f"{count} {name.replace('_', ' ').lower()} relationship(s)" for name, count in sorted(predicates.items(), key=lambda x: (-x[1], x[0]))[:4]) or "no relationships"
    path_note = f" {len(evidence.paths)} rule-valid path(s) were found." if evidence.paths else " No complete rule-valid clinical path was found."
    limit = " The requested detail was larger than the configured budget, so some evidence was left out." if evidence.budget_exhausted else ""
    return (f"The report selected {len(evidence.nodes)} records around {', '.join(evidence.query_terms)} and kept {top}."
            f"{path_note}{limit} This is an evidence map, not a diagnosis or a prediction that a treatment will work.")


def render_report_html(evidence: EvidenceSubgraph, title: str | None = None) -> str:
    """Render a portable report with an SVG plot; no browser package is required."""
    title = title or f"Genome graph report: {', '.join(evidence.query_terms)}"
    nodes = list(evidence.nodes)
    width, height = 1100, max(520, 180 + 100 * ((len(nodes) + 5) // 6))
    columns = 3
    positions: dict[str, tuple[float, float]] = {}
    for i, node in enumerate(nodes):
        col, row = i % columns, i // columns
        positions[node.id] = (190 + col * 350, 100 + row * 100)
    svg = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Evidence graph">',
           '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#777"/></marker></defs>']
    for e in evidence.edges:
        if e.src not in positions or e.dst not in positions:
            continue
        x1, y1 = positions[e.src]; x2, y2 = positions[e.dst]
        svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#9b9b9b" stroke-width="1.5" marker-end="url(#arrow)"/>')
        svg.append(f'<text x="{(x1+x2)/2:.0f}" y="{(y1+y2)/2-5:.0f}" class="edge">{escape(e.edge_id)} · {escape(e.predicate)}</text>')
    for node in nodes:
        x, y = positions[node.id]
        score = evidence.rwr_scores.get(node.id, 0.0)
        radius = 18 + min(12, score * 100)
        svg.append(f'<circle cx="{x}" cy="{y}" r="{radius:.1f}" fill="#4d8c82" fill-opacity=".78" stroke="#254c47"/>')
        svg.append(f'<text x="{x}" y="{y-4:.0f}" text-anchor="middle" class="node">{escape(node.label[:24])}</text>')
        svg.append(f'<text x="{x}" y="{y+12:.0f}" text-anchor="middle" class="sub">{escape(node.subtype)}</text>')
    svg.append("</svg>")
    glossary = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in explain_terms().items())
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)}</title>
<style>body{{font:15px system-ui,sans-serif;background:#f7f6f2;color:#292722;margin:28px}}h1{{margin-bottom:4px}}.muted{{color:#69645b}}svg{{width:100%;max-height:680px;background:#fff;border:1px solid #d8d4c9}}.node{{font-size:12px;font-weight:650}}.sub,.edge{{font-size:9px;fill:#4d4a43}}section{{background:#fff;border:1px solid #d8d4c9;padding:16px;margin-top:18px}}pre{{white-space:pre-wrap;font:12px ui-monospace,monospace;background:#f1efe9;padding:12px;overflow:auto}}dt{{font-weight:700;margin-top:8px}}dd{{margin-left:0;color:#555}}</style></head><body>
<h1>{escape(title)}</h1><p class="muted">{escape(plain_language_summary(evidence))}</p>
<section>{''.join(svg)}</section><section><h2>Terms in plain language</h2><dl>{glossary}</dl></section>
<section><h2>Auditable evidence context</h2><pre>{escape(evidence.serialize())}</pre></section></body></html>"""
