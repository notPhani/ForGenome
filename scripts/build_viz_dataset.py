"""Turns a harvest's full graph dump into a dataset a browser can render.

  python scripts/build_viz_dataset.py runs/panel_50k --out demo/graph_data.json
  python scripts/build_demo_html.py

The harvest writes every committed node and edge to `graph.jsonl.gz` -- hundreds of
thousands of nodes, over a million edges, far past what any page can draw. This step
applies the Gaussian density filter (analysis/downsample.py) and rebuilds a replayable
event log from what survives.

Rebuilding beats truncating. The runner's own event log stops at a fixed budget, which at
panel scale means the page shows the first few genes in full and the remaining eighty not
at all -- a picture that is accurate about nothing except the alphabet. Filtering by
density keeps the whole panel represented and states exactly what fraction of each subtype
survived, so the page can say what it is showing instead of implying it is showing
everything.

Dump order is preserved, which is what keeps the replay valid: the harvest writes each
micro-batch's nodes before its edges, so filtering a prefix-ordered stream can never
produce an edge that animates before its endpoints.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
import zlib
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import connectors  # noqa: F401 -- registers every connector
from analysis.downsample import downsample
from analysis.edge_stats import EdgeStatsAccumulator
from ingest.dirty import DirtyLedger
from orchestration.connector_registry import list_connectors

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_dump(path: str) -> tuple[list[dict], list[dict], int]:
    """Streams the gzipped JSONL dump, tolerating a dump that is still being written.

    Two independent kinds of damage, and the second is the one that actually bites:

      a partial LINE   -- the writer was interrupted between a record and its newline.
                          Caught by the JSONDecodeError guard; costs one record.

      a partial BLOCK  -- the writer was interrupted mid-gzip-block, or the file is being
                          read while a live harvest still holds it open. gzip is
                          block-compressed, so this raises `zlib.error: invalid block type`
                          from the DECOMPRESSOR, before a single line has been parsed. Not
                          caught by any amount of JSON guarding.

    That second case was found by reading a live harvest's dump and getting nothing at all
    back -- a total loss, from a design whose whole point was that an interrupted nine-hour
    run stays readable. Everything up to the damage is intact and is what gets returned;
    only the truncated tail is lost, and the count is reported rather than swallowed."""
    nodes: list[dict] = []
    edges: list[dict] = []
    dropped = 0
    truncated = False
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            while True:
                try:
                    line = f.readline()
                except (EOFError, zlib.error, gzip.BadGzipFile):
                    truncated = True
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    dropped += 1
                    continue
                # Stamp the file position. Splitting into two lists loses the interleaving,
                # and the interleaving IS the replay: the harvest writes each micro-batch's
                # nodes then its edges, so without this the page animates all 5,000 nodes
                # into existence and only then draws any edge between them -- which is not
                # what the pipeline did and reads as a completely different process.
                record["_seq"] = len(nodes) + len(edges)
                (nodes if record.get("kind") == "node" else edges).append(record)
    except (EOFError, zlib.error, gzip.BadGzipFile):
        truncated = True
    if truncated:
        dropped += 1
    return nodes, edges, dropped


def connector_manifest(feeds_seen: set[str], summary: dict) -> list[dict]:
    """Every REGISTERED connector with what actually happened to it in this run.

    The visualisation previously listed only feeds that emitted events, which quietly
    conflated three very different situations: a connector that ran and produced data, one
    that was selected and produced nothing, and one that is a documented stub with no
    implementation at all. Naming all three is the honest version -- a reader can see that
    six of the thirteen are scaffolding without having to notice an absence."""
    ran = set(summary.get("connectors_run") or [])
    out = []
    for d in list_connectors():
        if not d.implemented:
            state = "stub"
        elif d.feed_label in feeds_seen:
            state = "live"
        elif d.name in ran:
            state = "selected_no_data"
        else:
            state = "not_selected"
        out.append({
            "name": d.name, "feed": d.feed_label, "group": d.group, "cadence": d.cadence,
            "implemented": d.implemented, "state": state, "description": d.description,
            "requires": list(d.requires),
        })
    order = {"core_biology": 0, "operational": 1, "evidence": 2}
    return sorted(out, key=lambda c: (order.get(c["group"], 9), c["name"]))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="build_viz_dataset.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir", help="a harvest output directory (containing graph.jsonl.gz)")
    p.add_argument("--out", default="demo/graph_data.json")
    p.add_argument("--budget", type=int, default=9_000, help="node budget for the replay (default: 9000)")
    p.add_argument("--sigma", type=float, default=1.0, help="Gaussian width in hops (default: 1.0)")
    p.add_argument("--alpha", type=float, default=1.0,
                   help="how hard density suppresses; >1 thins crowded regions harder (default: 1.0)")
    p.add_argument("--hops", type=int, default=2, help="neighbourhood radius in hops (default: 2)")
    args = p.parse_args(argv)

    run_dir = args.run_dir if os.path.isabs(args.run_dir) else os.path.join(BASE, args.run_dir)
    # A resumed harvest writes one part per session (see StreamingStore) -- read them all,
    # in order, so a resume is invisible here and one damaged part cannot hide the others.
    parts = [os.path.join(run_dir, "graph.jsonl.gz")]
    parts += sorted(glob.glob(os.path.join(run_dir, "graph.part*.jsonl.gz")))
    parts = [p for p in parts if os.path.exists(p)]
    if not parts:
        print(f"error: no graph dump in {run_dir}", file=sys.stderr)
        return 1

    nodes, edges, dropped = [], [], 0
    for part in parts:
        part_nodes, part_edges, part_dropped = read_dump(part)
        nodes += part_nodes
        edges += part_edges
        dropped += part_dropped
        print(f"Read {os.path.basename(part)}: {len(part_nodes):,} nodes · "
              f"{len(part_edges):,} edges" + (f" · {part_dropped} truncated" if part_dropped else ""))
    print(f"  total {len(nodes):,} nodes · {len(edges):,} edges across {len(parts)} part(s)")

    summary = _load_json(os.path.join(run_dir, "summary.json"), {})
    ledger_path = os.path.join(run_dir, "dirty_ledger.jsonl")
    dirty_summary = {}
    if os.path.exists(ledger_path):
        ledger = DirtyLedger()
        ledger.load(ledger_path)
        dirty_summary = ledger.summary()
        dirty_ids = ledger.dirty_node_ids()
        # The dump records `dirty` as it was at first write; a node can go dirty later
        # (its first enrichment succeeded, a second one failed). The ledger is the
        # authority, so reconcile against it before the density filter runs -- dirty
        # nodes bypass that filter, and the filter must see the final state.
        relit = 0
        for n in nodes:
            if n["id"] in dirty_ids and not n.get("dirty"):
                n["dirty"] = True
                relit += 1
        print(f"  dirty ledger: {dirty_summary.get('open', 0):,} open tickets"
              + (f" · {relit:,} node(s) re-flagged from the ledger" if relit else ""))

    print(f"Downsampling to ~{args.budget:,} nodes "
          f"(sigma={args.sigma}, alpha={args.alpha}, hops={args.hops}) …")
    result = downsample(nodes, edges, budget=args.budget, sigma=args.sigma,
                         alpha=args.alpha, hops=args.hops)
    print(result.report.format_table())

    kept_nodes, kept_edges = result.nodes, result.edges
    kept_ids = {n["id"] for n in kept_nodes}

    # Rebuild the replay in dump order: nodes and edges interleaved exactly as committed.
    events: list[dict] = []
    for record in sorted(kept_nodes + kept_edges, key=lambda r: r.get("_seq", 0)):
        record = {k: v for k, v in record.items() if k != "_seq"}
        events.append({**record, "order": len(events)})

    # Identity-resolution events come from the harvest's own in-memory log (the dump holds
    # committed graph state, not the resolution moments). Kept only for surviving nodes.
    for ev in _load_json(os.path.join(run_dir, "events.json"), {}).get("events", []):
        if ev.get("kind") == "identity" and ev.get("id") in kept_ids:
            events.append({**ev, "order": len(events)})

    feeds_seen = {n["feed"] for n in kept_nodes if n.get("feed")}
    feeds = _rebuild_feeds(kept_nodes, kept_edges, result.report)
    waves = _rebuild_waves(kept_nodes)

    payload = {
        "events": events,
        "waves": waves,
        "feeds": feeds,
        "connectors": connector_manifest(feeds_seen, summary),
        "downsample": result.report.to_dict(),
        "dirty": dirty_summary,
        "stats": {
            "total_nodes": len(nodes), "total_edges": len(edges),
            "by_layer": dict(Counter(n["layer"] for n in nodes).most_common()),
            "by_wave": {},
            "by_role": dict(Counter(n["role"] for n in nodes).most_common()),
            "by_subtype": dict(Counter(n["subtype"] for n in nodes).most_common()),
            "variants_ingested": summary.get("totals", {}).get("variants_ingested", 0),
            "variants_requested": summary.get("totals", {}).get("variants_requested", 0),
            "quarantined": summary.get("totals", {}).get("quarantined", 0),
            "identity_merges": summary.get("totals", {}).get("identity_merges", 0),
            "dirty_nodes": summary.get("totals", {}).get("dirty_nodes", 0),
            "events_truncated": False,   # density-filtered, not truncated -- see `downsample`
            "elapsed_seconds": summary.get("totals", {}).get("elapsed_seconds", 0),
        },
        "edge_stats": _edge_stats(nodes, edges),
        "run": {
            "mode": "harvest", "gene": _gene_label(summary), "offline": False,
            # Fall back to the live registry when summary.json does not exist yet --
            # a page built mid-run should still name what is feeding it.
            "connectors": summary.get("connectors_run") or sorted(feeds_seen),
            "genes_completed": summary.get("genes_completed", []),
            "panel": summary.get("panel"),
            "resident_node_ids": [],
        },
        "verified": summary.get("totals", {}).get("batches_rejected", 0) == 0,
    }

    out_path = args.out if os.path.isabs(args.out) else os.path.join(BASE, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"Wrote {out_path} ({size_mb:.1f} MB, {len(events):,} events)")
    if size_mb > 12:
        print(f"  warning: {size_mb:.1f} MB is close to the 16 MB artifact ceiling once the "
              f"template is added — rerun with a smaller --budget if the publish is rejected")
    return 0


def _edge_stats(nodes: list[dict], edges: list[dict]) -> dict:
    """Edge statistics over the FULL committed graph, computed here rather than read from
    summary.json.

    Two reasons, one of which was a live failure: summary.json only exists once a harvest
    has finished, so building a page from a run still in progress produced `edge_stats: {}`
    and the page died on `edgeStatsData.predicates is not iterable`. And even when it does
    exist, recomputing costs nothing -- every node and edge is already in memory here.

    Deliberately computed over the full graph, not the density-filtered subset: the counts
    describe what was COMMITTED. The filter only decides what gets drawn, and a statistics
    panel that silently reported the drawn sample as the graph would misstate the run."""
    acc = EdgeStatsAccumulator()
    for n in nodes:
        acc.observe_node(n["id"], n.get("subtype", "?"), n.get("layer", "NOT_APPLICABLE"))
    for e in edges:
        acc.observe_edge(e["src"], e["predicate"], e.get("qualifier"), e["dst"],
                          e.get("origin", "SOURCE"))
    return acc.report().to_dict()


def _gene_label(summary: dict) -> str:
    genes = summary.get("genes_completed") or []
    if not genes:
        return "panel"
    return f"{len(genes)} genes" if len(genes) > 3 else ", ".join(genes)


def _rebuild_feeds(kept_nodes: list[dict], kept_edges: list[dict], report) -> list[dict]:
    """Per-feed counts for the legend, carrying BOTH what is drawn and what was committed,
    so a toggle can show "1,204 shown of 96,551" rather than implying the smaller number
    is the whole feed."""
    from orchestration.connector_registry import group_for_feed_label
    counts: dict[str, dict] = {}
    for n in kept_nodes:
        feed = n.get("feed")
        if not feed:
            continue
        bucket = counts.setdefault(feed, {"feed": feed, "group": group_for_feed_label(feed),
                                           "nodes": 0, "edges": 0, "total_nodes": 0})
        bucket["nodes"] += 1
    for e in kept_edges:
        feed = e.get("feed")
        if not feed:
            continue
        bucket = counts.setdefault(feed, {"feed": feed, "group": group_for_feed_label(feed),
                                           "nodes": 0, "edges": 0, "total_nodes": 0})
        bucket["edges"] += 1
    for feed, bucket in counts.items():
        kept_total = report.by_feed.get(feed)
        bucket["total_nodes"] = kept_total[1] if kept_total else bucket["nodes"]
    order = {"core_biology": 0, "operational": 1, "evidence": 2}
    return sorted(counts.values(), key=lambda f: (order.get(f["group"], 9), -f["nodes"]))


def _rebuild_waves(kept_nodes: list[dict]) -> list[dict]:
    waves: list[dict] = []
    seen: set[str] = set()
    for n in kept_nodes:
        wave = n.get("wave")
        if not wave or wave in seen:
            continue
        seen.add(wave)
        if wave == "core_biology":
            waves.append({"id": wave, "label": "Core Biology · persistent", "layer": "CORE_BIOLOGY"})
        elif wave == "operational":
            waves.append({"id": wave, "label": "Operational rules · persistent", "layer": "OPERATIONAL"})
        else:
            waves.append({"id": wave, "label": f"EVIDENCE · {wave.replace('gene_', '')}",
                           "layer": "EVIDENCE"})
    return waves


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


if __name__ == "__main__":
    raise SystemExit(main())
