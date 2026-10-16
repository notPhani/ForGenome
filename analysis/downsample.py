"""Gaussian density downsampling -- deciding which nodes a browser actually draws.

A panel harvest commits on the order of half a million nodes and over a million edges.
Nothing renders that: the published page is capped at 16 MB, and a force-directed layout
over 500k bodies would not settle on any hardware a reader has. So the visualisation
shows a SAMPLE, and the only question worth arguing about is which one.

Uniform random sampling is the obvious answer and the wrong one. This graph is extremely
non-uniform: ~93% of it is the same handful of shapes repeated per variant (an
AggregateClassification, a couple of ClinicalAssertions, a CaseObservation, a
SourceManifest link), while the parts that are actually interesting -- the Ensembl
transcript spine, ClinGen's rule tree, a variant whose submitters CONTRADICT each other,
anything a stub connector left dirty -- are rare. Sample uniformly and you get a picture
that is 93% the boring shape, which is exactly the picture you already had.

So: filter by local density instead. Each node's density is a Gaussian kernel evaluated
over graph hop-distance,

    rho(v) = sum over u within `hops` of  exp(-h(u,v)^2 / 2*sigma^2)

which is the standard Gaussian blur, with hop count standing in for pixel distance --
this graph has no coordinates at build time (the browser's physics assigns those), and
its own topology is a better notion of "near" than any layout would be anyway. A node
keeps its place with probability

    p(v) = min(1, (rho_target / rho(v))^alpha)

so crowded neighbourhoods thin aggressively and sparse ones survive intact. `rho_target`
is solved by bisection to hit the requested node budget rather than being a magic number.

Two properties that matter more than the maths:

  DETERMINISTIC.  The keep/drop coin is a stable hash of the node id, not an RNG. The
    same graph downsamples to the same picture on every machine and every rebuild, so a
    node someone linked to yesterday is still there today.

  HONEST.  Nothing is invented. Every surviving node and edge is a real committed one,
    and `DownsampleReport` states exactly what fraction of each subtype and each feed
    survived, so the page can say "showing 8,000 of 486,000" instead of implying the
    graph is small.

Structural nodes bypass the filter entirely (`_always_keep`): the persistent layers, any
subtype that is rare enough to vanish, high-degree hubs, and every dirty node. Without
that last rule the density filter would preferentially delete exactly the nodes the
retry ledger exists to make visible.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

# What counts as a graph-SPANNING node: one whose neighbourhood is a meaningful fraction
# of the entire graph. A Submitter like Invitae, or a connector's single SourceManifest,
# is attached to a large share of every claim in the harvest. Spanning nodes are always
# kept AND are never traversed through when expanding neighbourhoods -- otherwise "shares
# a submitter with" counts as proximity and every assertion in the graph is 2 hops from
# every other one, which flattens the density field into a constant and reduces this whole
# filter to uniform sampling with extra steps. Not traversing them is also what keeps the
# cost at O(|E|·avg_degree) rather than O(|V|^2).
#
# The threshold is a FRACTION of the graph, not a fixed degree, and that distinction was a
# real bug: at a fixed 250 a Variant carrying 400 clinical assertions was classified the
# same as a submitter spanning the whole panel, so its assertions were cut off from the
# 2-hop term -- the exact signal marking them as redundant -- and the crowded variant's
# assertions ended up surviving at a HIGHER rate than sparse ones. Scaling with |V| keeps
# a locally crowded anchor local and only excludes genuinely global ones.
#
# The floor is set from a measurement, not a guess. Max degree by subtype on a real
# 2-gene, 24-variant harvest: SourceManifest 847 (and it grows linearly with the run --
# hundreds of thousands at panel scale), Variant 210, Gene 71, Submitter 28 (which also
# grows without bound across genes, since the large labs submit to everything). So the
# real separation is between local anchors in the low hundreds and aggregators in the tens
# of thousands, and the floor belongs in the wide gap between them -- well above the
# hottest ClinVar variant, far below any genuine aggregator.
SPAN_FRACTION = 0.005      # 0.5% of all nodes
SPAN_FLOOR = 1_500         # ...but never call something spanning below this

# Kept for callers that want to pin an absolute threshold; None = derive it.
HUB_DEGREE = None


def spanning_threshold(node_count: int, hub_degree: int | None = None) -> int:
    if hub_degree is not None:
        return hub_degree
    return max(SPAN_FLOOR, int(SPAN_FRACTION * node_count))

# A subtype with fewer nodes than this is kept whole. Rare subtypes are the ones that
# carry information (a ClinicalImpactClassification, an Exon, a Query) and are exactly
# what a density filter would round away, leaving a legend listing subtypes the picture
# does not contain.
RARE_SUBTYPE_CEILING = 400

PERSISTENT_LAYERS = ("CORE_BIOLOGY", "OPERATIONAL")

# ...but only while that subtype stays small. The persistent layers hold two very
# different kinds of thing, and treating them alike was a real bug caught by a capacity
# test at 11,000 variants: `Variant` is CORE_BIOLOGY, so a blanket "never drop a persistent
# node" rule force-kept all 11,000 of them, blew straight past a 9,000-node display budget,
# and collapsed the whole filter into "keep the spine, drop everything else".
#
# The distinction that matters is not layer, it is whether the subtype SCALES WITH THE RUN.
# Gene, Transcript, Exon, Protein, ACMGCriterion, Requirement are a fixed structural
# skeleton -- roughly constant per gene, ~1,200 across a 79-gene panel, and the graph is
# unreadable without them. Variant grows one-for-one with every record ingested. So a
# persistent subtype is protected only while it is small enough to BE a skeleton; past that
# it is bulk, and bulk is exactly what the density filter is for.
PERSISTENT_KEEP_CEILING = 2_000


@dataclass
class DownsampleReport:
    total_nodes: int
    kept_nodes: int
    total_edges: int
    kept_edges: int
    rho_target: float
    sigma: float
    alpha: float
    hops: int
    always_kept: int
    isolated_kept: int                                    # kept nodes that lost every edge
    by_subtype: dict[str, tuple[int, int]] = field(default_factory=dict)   # subtype -> (kept, total)
    by_feed: dict[str, tuple[int, int]] = field(default_factory=dict)      # feed -> (kept, total)

    def node_ratio(self) -> float:
        return self.kept_nodes / self.total_nodes if self.total_nodes else 0.0

    def to_dict(self) -> dict:
        return {
            "total_nodes": self.total_nodes, "kept_nodes": self.kept_nodes,
            "total_edges": self.total_edges, "kept_edges": self.kept_edges,
            "node_ratio": round(self.node_ratio(), 5),
            "rho_target": round(self.rho_target, 4), "sigma": self.sigma,
            "alpha": self.alpha, "hops": self.hops, "always_kept": self.always_kept,
            "isolated_kept": self.isolated_kept,
            "by_subtype": {k: list(v) for k, v in sorted(self.by_subtype.items())},
            "by_feed": {k: list(v) for k, v in sorted(self.by_feed.items())},
            "method": "gaussian kernel over graph hop-distance; keep probability "
                      "min(1, (rho_target/rho)^alpha), deterministic by node id",
        }

    def format_table(self) -> str:
        lines = [
            "=" * 88,
            f"GAUSSIAN DENSITY DOWNSAMPLE  ·  {self.kept_nodes:,} of {self.total_nodes:,} nodes "
            f"({100 * self.node_ratio():.2f}%)  ·  {self.kept_edges:,} of {self.total_edges:,} edges",
            "=" * 88,
            f"  sigma={self.sigma}  alpha={self.alpha}  hops={self.hops}  "
            f"rho_target={self.rho_target:.3f}",
            f"  always kept (structural/rare/hub/dirty): {self.always_kept:,}",
            f"  kept but isolated after edge filtering:  {self.isolated_kept:,}",
            "",
            f"  {'SUBTYPE':<32} {'KEPT':>9} {'TOTAL':>10}   SHARE",
            "  " + "-" * 70,
        ]
        for subtype, (kept, total) in sorted(self.by_subtype.items(), key=lambda kv: -kv[1][1]):
            share = 100 * kept / total if total else 0.0
            lines.append(f"  {subtype:<32} {kept:>9,} {total:>10,}   {share:5.1f}%")
        lines.append("=" * 88)
        return "\n".join(lines)


@dataclass
class DownsampleResult:
    nodes: list[dict]
    edges: list[dict]
    report: DownsampleReport


def stable_unit(node_id: str) -> float:
    """A node's coin flip in [0, 1), stable across processes and Python versions.

    Python's built-in hash() is salted per process (PYTHONHASHSEED), so using it here
    would silently produce a different picture on every rebuild -- the one property this
    function exists to prevent."""
    digest = hashlib.blake2b(node_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def build_adjacency(node_ids: set[str], edges: list[dict]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {n: set() for n in node_ids}
    for e in edges:
        src, dst = e["src"], e["dst"]
        if src in adjacency and dst in adjacency and src != dst:
            adjacency[src].add(dst)
            adjacency[dst].add(src)
    return adjacency


def gaussian_density(adjacency: dict[str, set[str]], sigma: float = 1.0, hops: int = 2,
                      hub_degree: int | None = None) -> dict[str, float]:
    """rho(v) for every node: a Gaussian kernel summed over graph hop-distance.

    Only hops 0..`hops` contribute, which is not a shortcut -- at sigma=1 the hop-3 weight
    is exp(-4.5) = 0.011, three orders of magnitude under the centre, so the truncation is
    below the noise of the counts themselves."""
    weights = [math.exp(-(h * h) / (2 * sigma * sigma)) for h in range(hops + 1)]
    threshold = spanning_threshold(len(adjacency), hub_degree)
    hubs = {n for n, nbrs in adjacency.items() if len(nbrs) > threshold}
    density: dict[str, float] = {}

    for node, neighbors in adjacency.items():
        near = neighbors - hubs
        # Hubs count as neighbours (1-hop) but are not expanded THROUGH (2-hop).
        #
        # Counting them matters, and the first synthetic test of this function is why: with
        # hubs dropped from both terms, a node whose only edges go to hubs scored rho=1.0 --
        # the sparsest possible value, i.e. maximally protected -- when it is in fact the
        # most redundant kind of node in the graph. In a 600-leaf star the leaves came out
        # MORE likely to survive than nodes on a sparse chain, the exact inverse of the
        # intent. Not expanding through them is still right, for the reason in HUB_DEGREE.
        rho = weights[0] + weights[1] * len(neighbors)
        if hops >= 2 and near:
            two_hop: set[str] = set()
            for neighbor in near:
                two_hop |= adjacency[neighbor]
            two_hop -= near
            two_hop -= hubs
            two_hop.discard(node)
            rho += weights[2] * len(two_hop)
        density[node] = rho
    return density


def _always_keep(nodes: list[dict], adjacency: dict[str, set[str]],
                  hub_degree: int | None, rare_ceiling: int,
                  dirty_cap: int = 10 ** 9) -> set[str]:
    threshold = spanning_threshold(len(nodes), hub_degree)
    subtype_counts: dict[str, int] = {}
    for n in nodes:
        subtype_counts[n.get("subtype", "?")] = subtype_counts.get(n.get("subtype", "?"), 0) + 1

    keep: set[str] = set()
    # Dirty nodes are protected, but not without limit. This run produced 3,077 of them
    # against a 7,000-node display budget -- so "never drop a dirty node" alone consumed
    # 44% of the picture and starved everything else, most visibly VEP, whose 5,432
    # ComputationalPrediction nodes came out at ZERO. Protecting a signal must not mean
    # letting it crowd out the graph it is a signal about; a deterministic sample of them
    # makes the same point.
    dirty_kept: set[str] = set()
    for n in nodes:
        node_id = n["id"]
        subtype = n.get("subtype", "?")
        if n.get("layer") in PERSISTENT_LAYERS                 and subtype_counts.get(subtype, 0) <= PERSISTENT_KEEP_CEILING:
            keep.add(node_id)                     # the structural spine -- see the constant
        elif n.get("dirty") and len(dirty_kept) < dirty_cap:
            keep.add(node_id)                     # the retry ledger's whole point is visibility
            dirty_kept.add(node_id)
        elif subtype_counts.get(n.get("subtype", "?"), 0) <= rare_ceiling:
            keep.add(node_id)                     # rare subtypes survive whole
        elif len(adjacency.get(node_id, ())) > threshold:
            keep.add(node_id)                     # spanning nodes carry the graph's real shape
    return keep


def downsample(nodes: list[dict], edges: list[dict], budget: int = 9_000,
                sigma: float = 1.0, alpha: float = 1.0, hops: int = 2,
                hub_degree: int | None = HUB_DEGREE,
                rare_ceiling: int = RARE_SUBTYPE_CEILING,
                seed_fraction: float = 0.35,
                dirty_fraction: float = 0.15) -> DownsampleResult:
    """Filters a committed graph down to roughly `budget` nodes, plus every edge whose
    endpoints both survived. Node dicts need `id`, `subtype`, `layer`; `feed` and `dirty`
    are used when present."""
    node_ids = {n["id"] for n in nodes}
    adjacency = build_adjacency(node_ids, edges)
    density = gaussian_density(adjacency, sigma=sigma, hops=hops, hub_degree=hub_degree)
    # "Rare" has to be relative to what is being DRAWN, not an absolute count. At a
    # 2,500-node budget an absolute ceiling of 400 made five separate subtypes
    # unconditionally forced, half the budget went to the structural floor before any
    # density filtering ran, and the picture came out as 2,291 edges over 2,524 nodes with
    # 714 of them completely unconnected. A subtype occupying 16% of the frame is not rare.
    effective_rare = min(rare_ceiling, max(20, budget // 20))
    forced = _always_keep(nodes, adjacency, hub_degree, effective_rare,
                           dirty_cap=max(50, int(budget * dirty_fraction)))

    coins = {n["id"]: stable_unit(n["id"]) for n in nodes}
    candidates = [n["id"] for n in nodes if n["id"] not in forced]

    def kept_at(rho_target: float) -> set[str]:
        out = set(forced)
        for node_id in candidates:
            rho = density.get(node_id, 1.0)
            p = 1.0 if rho <= rho_target else (rho_target / rho) ** alpha
            if coins[node_id] < p:
                out.add(node_id)
        return out

    # Bisect rho_target so the kept count lands on the budget. Monotone in rho_target
    # (a higher target keeps more), so plain bisection is exact enough in ~40 steps;
    # the bracket's top is the densest node in the graph, where everything is kept.
    # Seeds take only a fraction of the budget; the rest is spent growing their
    # neighbourhoods (below). Bisecting straight to `budget` filled it entirely with
    # seeds, leaving nothing to expand with -- which is how the connectivity collapse
    # survived its first fix.
    # Additive, not max(): with a forced floor of 3,314 against a 7,000 budget, a
    # max() made the seed target equal to the floor itself, so the density filter
    # selected NOTHING and the whole picture came from expanding the forced set.
    seed_budget = min(budget, len(forced) + int(budget * seed_fraction))
    low, high = 0.0, max(density.values()) if density else 1.0
    keep = kept_at(high)
    if len(keep) > seed_budget:
        for _ in range(40):
            mid = (low + high) / 2
            keep = kept_at(mid)
            if len(keep) > seed_budget:
                high = mid
            else:
                low = mid
        keep = kept_at(low)
        if len(forced) > budget:
            # The structural floor alone exceeds the budget. Honest outcome: keep it and
            # say so, rather than silently dropping the spine to hit a display number.
            keep = set(forced)

    # Grow the selection outward until the budget is spent, so what is drawn is connected
    # NEIGHBOURHOODS rather than scattered points.
    #
    # Independent per-node sampling is what the density filter gives you, and on its own it
    # shreds the edges: an edge survives only if BOTH endpoints do, so at ~12% node
    # retention only ~1.4% of edges live (measured: 5,165 of 177,475), and 4,278 of the
    # 9,000 drawn nodes had no edge left at all. A graph visualisation whose nodes are
    # mostly unconnected dots is not a smaller version of the graph, it is a different and
    # false picture of it.
    #
    # So the density selection above is treated as SEEDS, and the remaining budget is spent
    # pulling in each seed's own neighbours -- lowest-density (most informative) first.
    # Seeds still come from the Gaussian, so crowded regions still contribute far fewer of
    # them; what changes is that the ones chosen arrive with their context attached.
    # Pass 1 -- connectivity first. Every kept node that currently has no kept neighbour
    # gets one, before any node gets a second. Without this the general expansion below
    # spends its whole budget thickening the neighbourhoods it reaches first and simply
    # never arrives at the rest, which is why a 2,500-node frame still had 411 nodes
    # sitting alone. An unconnected node in a graph drawing is not a small amount of
    # information, it is misinformation.
    kept_adj = {i: adjacency.get(i, set()) for i in keep}
    for node_id in sorted(keep, key=lambda i: density.get(i, 0.0)):
        if len(keep) >= budget:
            break
        if any(n in keep for n in kept_adj[node_id]):
            continue
        candidates = sorted(kept_adj[node_id], key=lambda i: density.get(i, 0.0))
        if candidates:
            keep.add(candidates[0])

    # Pass 2 -- spend whatever is left thickening those neighbourhoods.
    frontier = sorted(keep, key=lambda i: density.get(i, 0.0))
    while frontier and len(keep) < budget:
        nxt = []
        for node_id in frontier:
            for neighbor in sorted(adjacency.get(node_id, ()), key=lambda i: density.get(i, 0.0)):
                if neighbor in keep:
                    continue
                keep.add(neighbor)
                nxt.append(neighbor)
                if len(keep) >= budget:
                    break
            if len(keep) >= budget:
                break
        frontier = nxt

    kept_nodes = [n for n in nodes if n["id"] in keep]
    kept_edges = [e for e in edges if e["src"] in keep and e["dst"] in keep]

    still_connected = set()
    for e in kept_edges:
        still_connected.add(e["src"])
        still_connected.add(e["dst"])

    by_subtype: dict[str, list[int]] = {}
    by_feed: dict[str, list[int]] = {}
    for n in nodes:
        subtype = n.get("subtype", "?")
        bucket = by_subtype.setdefault(subtype, [0, 0])
        bucket[1] += 1
        if n["id"] in keep:
            bucket[0] += 1
        feed = n.get("feed")
        if feed:
            fb = by_feed.setdefault(feed, [0, 0])
            fb[1] += 1
            if n["id"] in keep:
                fb[0] += 1

    report = DownsampleReport(
        total_nodes=len(nodes), kept_nodes=len(kept_nodes),
        total_edges=len(edges), kept_edges=len(kept_edges),
        rho_target=low, sigma=sigma, alpha=alpha, hops=hops,
        always_kept=len(forced), isolated_kept=len(keep - still_connected),
        by_subtype={k: (v[0], v[1]) for k, v in by_subtype.items()},
        by_feed={k: (v[0], v[1]) for k, v in by_feed.items()},
    )
    return DownsampleResult(nodes=kept_nodes, edges=kept_edges, report=report)
