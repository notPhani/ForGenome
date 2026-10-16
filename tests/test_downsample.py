"""Tests for analysis/downsample.py -- the Gaussian density filter that decides which
nodes the visualisation draws."""
from analysis.downsample import (
    RARE_SUBTYPE_CEILING, build_adjacency, downsample, gaussian_density, spanning_threshold,
    stable_unit,
)


def _chain(prefix: str, n: int, subtype: str = "ClinicalAssertion"):
    nodes = [{"id": f"{prefix}{i}", "subtype": subtype, "layer": "EVIDENCE", "feed": "ClinVar"}
             for i in range(n)]
    edges = [{"src": f"{prefix}{i}", "dst": f"{prefix}{i + 1}"} for i in range(n - 1)]
    return nodes, edges


def _star(center: str, n: int, subtype: str = "ClinicalAssertion"):
    nodes = [{"id": center, "subtype": "Variant", "layer": "EVIDENCE", "feed": "ClinVar"}]
    nodes += [{"id": f"{center}:{i}", "subtype": subtype, "layer": "EVIDENCE", "feed": "ClinVar"}
              for i in range(n)]
    edges = [{"src": f"{center}:{i}", "dst": center} for i in range(n)]
    return nodes, edges


def test_the_keep_coin_is_stable_across_processes():
    # Python's hash() is salted per process; using it here would silently reshuffle the
    # picture on every rebuild, which is the one property this must not have.
    assert stable_unit("SPDI:NC_000003.12:10149803:C:T") == \
        stable_unit("SPDI:NC_000003.12:10149803:C:T")
    assert 0.0 <= stable_unit("anything") < 1.0
    assert stable_unit("a") != stable_unit("b")


def test_density_is_higher_in_a_crowded_neighbourhood_than_a_sparse_one():
    nodes, edges = _star("hot", 300)
    chain_nodes, chain_edges = _chain("cold", 300)
    nodes += chain_nodes
    edges += chain_edges
    density = gaussian_density(build_adjacency({n["id"] for n in nodes}, edges), )
    assert density["hot:0"] > density["cold5"], \
        "a node among 300 siblings must score denser than one on a chain"


def test_the_spanning_threshold_scales_with_the_graph():
    # Real bug this guards: at a FIXED threshold a Variant carrying 400 clinical
    # assertions was classified the same as a submitter spanning the whole panel, which
    # cut its assertions off from the 2-hop term -- the exact signal marking them
    # redundant -- and made crowded assertions survive at a HIGHER rate than sparse ones.
    assert spanning_threshold(1_000) == 1_500        # floor applies to small graphs
    assert spanning_threshold(460_000) == 2_300      # 0.5% of the graph
    assert spanning_threshold(460_000, hub_degree=99) == 99   # explicit override wins

    # The floor must sit above the hottest LOCAL anchor and below any real aggregator.
    # Measured on a real harvest: Variant peaked at 210, SourceManifest at 847 and rising
    # linearly with the run. A floor that catches crowded Variants is the bug this guards.
    assert spanning_threshold(1_000) > 500


def test_crowded_regions_thin_harder_than_sparse_ones():
    nodes, edges = _star("hot", 400)
    for k in range(40):
        n, e = _star(f"cold{k}", 2)
        nodes += n
        edges += e
    result = downsample(nodes, edges, budget=250)
    kept = {n["id"] for n in result.nodes}
    hot = sum(1 for i in range(400) if f"hot:{i}" in kept) / 400
    cold = sum(1 for k in range(40) for i in range(2) if f"cold{k}:{i}" in kept) / 80
    assert cold > hot, f"sparse should survive better than crowded (cold={cold:.2f} hot={hot:.2f})"


def test_persistent_layer_nodes_are_never_dropped():
    nodes, edges = _star("v", 900)
    nodes += [{"id": f"ENST{i}", "subtype": "Transcript", "layer": "CORE_BIOLOGY", "feed": "Ensembl"}
              for i in range(20)]
    result = downsample(nodes, edges, budget=100)
    kept = {n["id"] for n in result.nodes}
    assert all(f"ENST{i}" in kept for i in range(20)), "the Core Biology spine is the graph's skeleton"


def test_dirty_nodes_are_never_dropped():
    # Without this the density filter would preferentially delete exactly the nodes the
    # retry ledger exists to make visible.
    nodes, edges = _star("v", 900)
    nodes[400]["dirty"] = True
    dirty_id = nodes[400]["id"]
    result = downsample(nodes, edges, budget=80)
    assert dirty_id in {n["id"] for n in result.nodes}


def test_rare_subtypes_survive_whole():
    nodes, edges = _star("v", 2000)
    nodes += [{"id": f"rare{i}", "subtype": "ClinicalImpactClassification", "layer": "EVIDENCE",
               "feed": "ClinVar"} for i in range(5)]
    edges += [{"src": f"rare{i}", "dst": "v"} for i in range(5)]
    result = downsample(nodes, edges, budget=200)
    kept = {n["id"] for n in result.nodes}
    assert all(f"rare{i}" in kept for i in range(5))
    assert result.report.by_subtype["ClinicalImpactClassification"] == (5, 5)


def test_every_kept_edge_has_both_endpoints_kept():
    nodes, edges = _star("v", 900)
    result = downsample(nodes, edges, budget=200)
    kept = {n["id"] for n in result.nodes}
    assert all(e["src"] in kept and e["dst"] in kept for e in result.edges)


def test_downsampling_is_deterministic():
    nodes, edges = _star("v", 900)
    a = downsample(nodes, edges, budget=200)
    b = downsample(nodes, edges, budget=200)
    assert [n["id"] for n in a.nodes] == [n["id"] for n in b.nodes]


def test_nothing_is_invented_only_selected():
    nodes, edges = _star("v", 500)
    result = downsample(nodes, edges, budget=100)
    original_ids = {n["id"] for n in nodes}
    assert {n["id"] for n in result.nodes} <= original_ids
    assert result.report.kept_nodes <= result.report.total_nodes


def test_a_graph_under_budget_is_returned_whole():
    nodes, edges = _star("v", 50)
    result = downsample(nodes, edges, budget=9_000)
    assert result.report.kept_nodes == result.report.total_nodes
    assert result.report.node_ratio() == 1.0


def test_the_structural_floor_wins_over_the_budget():
    # Keeping the spine and saying so beats silently dropping it to hit a display number.
    nodes = [{"id": f"ENST{i}", "subtype": "Transcript", "layer": "CORE_BIOLOGY", "feed": "Ensembl"}
             for i in range(300)]
    result = downsample(nodes, [], budget=10)
    assert result.report.kept_nodes == 300
    assert result.report.always_kept == 300


def test_report_states_what_was_kept_per_subtype():
    nodes, edges = _star("v", 900)
    result = downsample(nodes, edges, budget=200)
    kept, total = result.report.by_subtype["ClinicalAssertion"]
    assert total == 900
    assert kept < total
    assert result.report.to_dict()["node_ratio"] < 1.0
