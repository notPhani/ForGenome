"""Forgenome pipeline entrypoint.

One command drives every kind of run; the layer split is a flag, not a different script.

  # Overnight: build the persistent layers once (Core Biology + Operational rules)
  python main.py --gene VHL --mode persistent --save-resident runs/vhl_persistent.json

  # Fast, repeatable: evidence only, attaching to the layers already resident
  python main.py --gene VHL --mode ephemeral --sample-pct 50 \
      --resident runs/vhl_persistent.json --out demo/graph_data.json --build-html

  # Everything in one process, offline from the saved fixtures (no network at all)
  python main.py --mode full --offline --out demo/graph_data.json --build-html
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The stats tables use box-drawing and arrow glyphs; the default Windows console codepage
# (cp1252) can't encode them and raises mid-print, losing the whole report.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from orchestration.runner import RunConfig, RunResult, run

BASE = os.path.dirname(os.path.abspath(__file__))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py", description="Forgenome connector-to-graph pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )

    search = p.add_argument_group("search — what to ingest")
    search.add_argument("--gene", default="VHL", help="gene symbol (default: VHL)")
    search.add_argument("--term", default=None,
                        help="raw ClinVar ESearch term, overrides --gene "
                             "(e.g. \"VHL[gene] AND single_gene[prop]\")")
    search.add_argument("--uids", default=None,
                        help="comma-separated ClinVar VariationIDs, skips ESearch entirely")
    search.add_argument("--sample-pct", type=float, default=100.0,
                        help="deterministically sample this %% of the matched family (default: 100)")
    search.add_argument("--limit", type=int, default=None, help="hard cap on variants after sampling")
    search.add_argument("--seed", type=int, default=62, help="sampling seed (default: 62)")

    pipeline = p.add_argument_group("pipeline — which layers and sources")
    pipeline.add_argument("--mode", choices=["ephemeral", "persistent", "full"], default="ephemeral",
                          help="ephemeral=evidence only · persistent=core biology + operational rules "
                               "· full=both (default: ephemeral)")
    pipeline.add_argument("--connectors", default="clinvar",
                          help="comma-separated evidence connectors: clinvar,gnomad,vep "
                               "(default: clinvar — gnomad/vep are one request per variant, "
                               "so only sane for small runs)")
    pipeline.add_argument("--offline", action="store_true",
                          help="use the saved fixtures, make no network calls")
    pipeline.add_argument("--batch-size", type=int, default=50,
                          help="ClinVar records per E-utilities request (default: 50)")
    pipeline.add_argument("--clingen-spec", default="GN078", help="ClinGen VCEP spec id (default: GN078)")
    pipeline.add_argument("--resident", default=None,
                          help="persistent-layer snapshot the ephemeral run attaches to")
    pipeline.add_argument("--run-id", default="run", help="run id recorded in the identity index")

    out = p.add_argument_group("output")
    out.add_argument("--out", default=None, help="write the replayable event log here (JSON)")
    out.add_argument("--save-resident", default=None,
                     help="write this run's node ids for a later ephemeral run to attach to")
    out.add_argument("--stats-json", default=None, help="write the edge-type statistics here (JSON)")
    out.add_argument("--build-html", action="store_true",
                     help="rebuild demo/index.html from the event log after the run")
    out.add_argument("--viz-max-events", type=int, default=24_000,
                     help="event-log budget for the visualisation (default: 24000). Statistics are "
                          "always computed over the FULL committed graph, never the truncated log.")
    out.add_argument("--top-shapes", type=int, default=3,
                     help="edge shapes to print per predicate (default: 3)")
    out.add_argument("--completeness-report", action="store_true",
                     help="print the advisory graph-completeness report (never blocks a commit)")
    out.add_argument("--quiet", action="store_true", help="suppress progress lines")

    misc = p.add_argument_group("misc")
    misc.add_argument("--list-connectors", action="store_true",
                      help="print every registered connector (group/cadence/implemented) and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_connectors:
        _print_connector_list()
        return 0

    config = RunConfig(
        gene=args.gene,
        term=args.term,
        uids=[u.strip() for u in args.uids.split(",") if u.strip()] if args.uids else [],
        mode=args.mode,
        connectors=tuple(c.strip() for c in args.connectors.split(",") if c.strip()),
        sample_pct=args.sample_pct,
        limit=args.limit,
        seed=args.seed,
        offline=args.offline,
        batch_size=args.batch_size,
        clingen_spec_id=args.clingen_spec,
        resident_path=args.resident,
        viz_max_events=args.viz_max_events,
        run_id=args.run_id,
        progress=not args.quiet,
    )

    for warning in _preflight_warnings(config):
        print(f"warning: {warning}", file=sys.stderr)

    result = run(config)
    print(_run_summary(result))
    print(result.edge_stats.format_table(shapes_per_predicate=args.top_shapes))
    if args.completeness_report:
        from analysis.completeness import compute_completeness
        from orchestration.connector_registry import list_connectors
        print(compute_completeness(result.nodes, result.edges, list_connectors()).format_table())

    if args.out:
        _write_event_log(result, args.out)
        print(f"Event log → {args.out} ({len(result.events)} events"
              f"{', TRUNCATED for display' if result.events_truncated else ''})")
    if args.save_resident:
        _write_resident(result, args.save_resident)
        print(f"Resident node ids → {args.save_resident} ({len(result.nodes)} ids)")
    if args.stats_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.stats_json)), exist_ok=True)
        with open(args.stats_json, "w", encoding="utf-8") as f:
            json.dump(result.edge_stats.to_dict(), f, indent=1)
        print(f"Edge statistics → {args.stats_json}")
    if args.build_html:
        subprocess.run([sys.executable, os.path.join(BASE, "scripts", "build_demo_html.py")], check=True)

    return 0 if result.verified else 1


def _print_connector_list() -> None:
    import connectors  # noqa: F401 -- registers every connector as a side effect
    from orchestration.connector_registry import list_connectors
    descriptors = list_connectors()
    print(f"\n{len(descriptors)} connectors registered:\n")
    print(f"{'GROUP':<14} {'CADENCE':<12} {'NAME':<26} {'STATUS':<10} DESCRIPTION")
    print("-" * 100)
    for d in descriptors:
        status = "live" if d.implemented else "STUB"
        requires = f"  (requires: {', '.join(d.requires)})" if d.requires else ""
        print(f"{d.group:<14} {d.cadence:<12} {d.name:<26} {status:<10} {d.description}{requires}")


def _preflight_warnings(config: RunConfig) -> list[str]:
    """Configurations that will run but produce a worse result than the user expects.
    Warnings, never errors -- the run is legal, it just won't do what was probably meant."""
    warnings = []
    has_resident = bool(config.resident_path and os.path.exists(config.resident_path))
    if config.mode == "ephemeral" and "vep" in config.connectors and not has_resident:
        warnings.append(
            "--connectors includes vep but no --resident snapshot was given. VEP consequences "
            "point at Transcript nodes, which live in the persistent Core Biology layer, so "
            "every batch will REJECT on dangling endpoints. Run --mode persistent "
            "--save-resident first, or use --mode full.")
    if config.mode == "ephemeral" and not has_resident:
        warnings.append(
            "no --resident snapshot: the persistent layer is being DECLARED rather than "
            "verified, so residency is asserted, not confirmed.")
    if config.mode != "persistent" and not config.offline and config.sample_pct > 20 \
            and ({"gnomad", "vep"} & set(config.connectors)):
        warnings.append(
            "gnomad/vep issue one request per variant, unlike ClinVar's batched E-utilities. "
            "At this sample size expect the run to be dominated by those round-trips.")
    return warnings


def _run_summary(r: RunResult) -> str:
    c = r.config
    lines = [
        "",
        "=" * 88,
        f"RUN SUMMARY  ·  mode={c.mode}  ·  gene={c.gene}  ·  {'OFFLINE' if c.offline else 'LIVE'}"
        f"  ·  {r.elapsed_seconds}s",
        "=" * 88,
        f"  connectors           {', '.join(f['feed'] for f in r.feeds) if r.feeds else '(none ran)'}",
        f"  variants requested   {r.variants_requested}",
        f"  variants ingested    {r.variants_ingested}",
        f"  micro-batches        {r.batches_committed} committed / {r.batches_rejected} rejected",
        f"  quarantined records  {len([q for q in r.quarantined if q['scope'] == 'record'])}",
        f"  committed graph      {len(r.nodes)} nodes / {len(r.edges)} edges",
        f"  identity merges      {r.identity_merges} "
        f"(cross-batch: a Gene/Variant/Disease resolved onto an existing node)",
        f"  structural hash      {r.structural_hash} "
        f"(same source data -> same hash, regardless of fetch order or wall-clock time)",
    ]
    if r.coverage:
        lines.append("  mapping coverage (forge-pipeline-v1.md §11):")
        for name, report in sorted(r.coverage.items()):
            summary = report.mapping_origin_summary()
            lines.append(f"      {name:<16} {summary['declarative']:>5.1f}% declarative / "
                         f"{summary['escape_hatch']:>5.1f}% escape-hatch "
                         f"({report.records_parsed} records parsed, {report.records_emitted} nodes/edges emitted)")
    if r.feeds:
        lines.append("  connector feeds:")
        for feed in r.feeds:
            lines.append(f"      {feed['group']:<13} {feed['feed']:<24} "
                         f"{feed['nodes']:>6} nodes  {feed['edges']:>6} edges")
    if c.mode == "ephemeral":
        verified = bool(c.resident_path and os.path.exists(c.resident_path))
        lines.append(f"  resident anchors     {len(r.resident_node_ids)} "
                     f"({'loaded from a persistent run — verified' if verified else 'DECLARED, not verified — no persistent snapshot given'})")
    if r.quarantined:
        reasons: dict[str, int] = {}
        for q in r.quarantined:
            key = q["reason"].split(":")[-1].strip()[:60]
            reasons[key] = reasons.get(key, 0) + 1
        lines.append("  quarantine reasons:")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"      {count:>5}  {reason}")
    lines.append("=" * 88)
    return "\n".join(lines)


def _count(items, key) -> dict:
    out: dict[str, int] = {}
    for item in items:
        k = key(item)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _write_event_log(r: RunResult, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    by_layer: dict[str, int] = {}
    by_wave: dict[str, dict] = {}
    for ev in r.events:
        bucket = by_wave.setdefault(ev["wave"], {"nodes": 0, "edges": 0})
        if ev["kind"] == "node":
            by_layer[ev["layer"]] = by_layer.get(ev["layer"], 0) + 1
            bucket["nodes"] += 1
        else:
            bucket["edges"] += 1

    from orchestration.connector_registry import list_connectors
    feeds_seen = {f["feed"] for f in r.feeds}
    manifest = []
    for d in list_connectors():
        state = "stub" if not d.implemented else ("live" if d.feed_label in feeds_seen else "idle")
        manifest.append({
            "name": d.name, "feed": d.feed_label, "group": d.group, "cadence": d.cadence,
            "implemented": d.implemented, "state": state, "description": d.description,
            "requires": list(d.requires),
        })

    payload = {
        "events": r.events,
        "waves": r.waves,
        "feeds": r.feeds,
        "connectors": manifest,
        "stats": {
            "total_nodes": len(r.nodes), "total_edges": len(r.edges),
            "by_layer": by_layer, "by_wave": by_wave,
            "by_role": _count(r.nodes, lambda n: n.role.value),
            "by_subtype": _count(r.nodes, lambda n: n.subtype),
            "variants_ingested": r.variants_ingested,
            "variants_requested": r.variants_requested,
            "quarantined": len(r.quarantined),
            "identity_merges": r.identity_merges,
            "events_truncated": r.events_truncated,
            "elapsed_seconds": r.elapsed_seconds,
        },
        "edge_stats": r.edge_stats.to_dict(),
        "run": {
            "mode": r.config.mode, "gene": r.config.gene, "offline": r.config.offline,
            "connectors": list(r.config.connectors), "sample_pct": r.config.sample_pct,
            "seed": r.config.seed, "term": r.config.search_term(),
            "resident_node_ids": r.resident_node_ids,
        },
        "verified": r.verified,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)


def _write_resident(r: RunResult, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "gene": r.config.gene, "mode": r.config.mode,
            "node_ids": sorted(n.id for n in r.nodes),
        }, f, indent=1)


if __name__ == "__main__":
    raise SystemExit(main())
