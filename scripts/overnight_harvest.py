"""Overnight panel harvest -- the long, unattended run.

  # the real thing: whole panel, 50k variants, stop cleanly after 9 hours
  python scripts/overnight_harvest.py --out runs/panel_50k

  # see the plan without fetching a single archive (one ESearch per gene, nothing else)
  python scripts/overnight_harvest.py --dry-run

  # resume an interrupted run -- same command, same --out; it picks up at the last
  # committed batch. Resume is the default; --no-resume starts over.
  python scripts/overnight_harvest.py --out runs/panel_50k

Afterwards, turn the dump into something a browser can actually render:

  python scripts/build_viz_dataset.py runs/panel_50k --out demo/graph_data.json
  python scripts/build_demo_html.py

Connector selection defaults to EVERY implemented connector in the registry, persistent
and ephemeral -- that is the point of the run. Stubs are excluded automatically (their
fetch() raises by design) and reported as such, never silently counted as covered.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import connectors  # noqa: F401 -- registers every connector
from orchestration.connector_registry import list_connectors
from orchestration.harvest import HarvestConfig, allocate_budget, harvest, load_panel

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="overnight_harvest.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="runs/panel_50k", help="output directory (default: runs/panel_50k)")
    p.add_argument("--target", type=int, default=50_000, help="total variant budget (default: 50000)")
    p.add_argument("--genes", default=None,
                   help="comma-separated gene symbols, overriding data/gene_panel.json")
    p.add_argument("--panel", default=None, help="path to a gene panel JSON")
    p.add_argument("--connectors", default=None,
                   help="comma-separated connector names (default: every implemented connector)")
    p.add_argument("--enrich-every", type=int, default=10,
                   help="run per-variant enrichment (gnomAD/VEP/dbSNP) on 1 in N variants; "
                        "the rest are marked dirty with a retryable ticket. 0 disables it, "
                        "1 enriches everything (default: 10)")
    p.add_argument("--batch-size", type=int, default=100,
                   help="ClinVar records per E-utilities request (default: 100)")
    p.add_argument("--deadline-hours", type=float, default=9.0,
                   help="stop cleanly after this many hours (default: 9)")
    p.add_argument("--no-resume", action="store_true", help="ignore any existing checkpoint")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve every gene's real ClinVar family size and print the budget "
                        "allocation, then exit without fetching any archives")
    p.add_argument("--run-id", default=None, help="identity-index run id (default: derived from --out)")
    return p


def implemented_connector_names() -> list[str]:
    return [d.name for d in list_connectors() if d.implemented]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = os.path.join(BASE, args.out) if not os.path.isabs(args.out) else args.out
    os.makedirs(out_dir, exist_ok=True)

    if args.genes:
        genes = [g.strip().upper() for g in args.genes.split(",") if g.strip()]
        panel_doc = {"name": "cli", "groups": []}
    else:
        genes, panel_doc = load_panel(args.panel)

    connector_names = ([c.strip() for c in args.connectors.split(",") if c.strip()]
                       if args.connectors else implemented_connector_names())
    stubs = [d.name for d in list_connectors() if not d.implemented]

    log_path = os.path.join(out_dir, "harvest.log")
    log_file = open(log_path, "a", encoding="utf-8")

    def say(msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}"
        if sys.stdout.isatty():
            try:
                import tqdm
                tqdm.tqdm.write(line)
            except Exception:
                print(line, flush=True)
        else:
            print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    say("=" * 92)
    say(f"FORGE OVERNIGHT HARVEST  ·  panel={panel_doc.get('name', 'cli')}  ·  "
        f"{len(genes)} genes  ·  target {args.target} variants")
    say(f"  connectors  {', '.join(connector_names)}")
    say(f"  stubs       {', '.join(stubs)}  (registered, not implemented -- excluded, not counted)")
    say(f"  enrichment  1 in {args.enrich_every} variants" if args.enrich_every else
        "  enrichment  disabled")
    say(f"  output      {out_dir}")
    say("=" * 92)

    if args.dry_run:
        return _dry_run(genes, args.target, say, out_dir)

    config = HarvestConfig(
        genes=tuple(genes), target_variants=args.target, out_dir=out_dir,
        connectors=tuple(connector_names), enrich_every=args.enrich_every,
        batch_size=args.batch_size, run_id=args.run_id or os.path.basename(out_dir),
        deadline_hours=args.deadline_hours, resume=not args.no_resume,
        # None, NOT the VHL default. A single explicit spec id would be handed to EVERY
        # gene in the panel (connectors/clingen.py::_build_query prefers the context value
        # over its own per-gene table), attaching VHL's VCEP criteria to BRCA1 and 80
        # others. ClinGen VCEP specs are per-gene and we only have VHL's, so let the
        # per-gene table answer and let every other gene legitimately get no ClinGen data.
        clingen_spec_id=None,
    )

    started = time.time()
    result = harvest(config, say=say)
    _report(result, say)
    _write_summary(result, out_dir, panel_doc, connector_names, stubs)
    say(f"Wrote {os.path.join(out_dir, 'summary.json')}")
    say(f"Graph dump → {os.path.join(out_dir, 'graph.jsonl.gz')}")
    say(f"Dirty ledger → {os.path.join(out_dir, 'dirty_ledger.jsonl')}")
    say(f"Total wall clock: {(time.time() - started) / 3600:.2f}h")
    log_file.close()
    return 0


def _dry_run(genes: list[str], target: int, say, out_dir: str) -> int:
    from connectors.clinvar import ClinVarAdapter
    adapter = ClinVarAdapter()
    sizes: dict[str, int] = {}
    for i, gene in enumerate(genes, 1):
        try:
            uids = adapter.search_variation_ids(f"{gene}[gene]")
            sizes[gene] = len(uids)
            say(f"  [{i:>2}/{len(genes)}] {gene:<10} {len(uids):>6,} ClinVar records")
        except Exception as exc:
            say(f"  [{i:>2}/{len(genes)}] {gene:<10} ESearch FAILED ({type(exc).__name__}: {exc})")
            sizes[gene] = 0
        time.sleep(0.34)
    allocation = allocate_budget(sizes, target)
    say("")
    say(f"{'GENE':<10} {'CLINVAR FAMILY':>15} {'ALLOCATED':>11}   COVERAGE")
    say("-" * 60)
    for gene in sorted(genes, key=lambda g: -sizes.get(g, 0)):
        size, budget = sizes.get(gene, 0), allocation.get(gene, 0)
        pct = (100 * budget / size) if size else 0.0
        flag = "  UNRESOLVED" if size == 0 else ("  complete" if budget >= size else "")
        say(f"{gene:<10} {size:>15,} {budget:>11,}   {pct:5.1f}%{flag}")
    say("-" * 60)
    say(f"{'TOTAL':<10} {sum(sizes.values()):>15,} {sum(allocation.values()):>11,}")
    with open(os.path.join(out_dir, "dry_run_plan.json"), "w", encoding="utf-8") as f:
        json.dump({"family_sizes": sizes, "allocation": allocation, "target": target}, f, indent=1)
    return 0


def _report(r, say) -> None:
    say("")
    say("=" * 92)
    say(f"HARVEST SUMMARY  ·  {r.elapsed_seconds / 3600:.2f}h  ·  "
        f"{len(r.genes_completed)}/{len(r.allocations)} genes completed")
    say("=" * 92)
    say(f"  variants allocated   {r.variants_requested:,}")
    say(f"  variants ingested    {r.variants_ingested:,}")
    say(f"  committed graph      {r.nodes_written:,} nodes / {r.edges_written:,} edges")
    say(f"  micro-batches        {r.batches_committed:,} committed / {r.batches_rejected:,} rejected")
    say(f"  identity merges      {r.identity_merges:,}")
    say(f"  dirty nodes          {r.dirty_nodes:,} "
        f"({r.dirty_summary['open']:,} open retry tickets of {r.dirty_summary['total']:,} filed)")
    if r.stopped_early:
        say(f"  STOPPED EARLY        {r.stopped_early}")
    if r.dirty_summary["by_connector"]:
        say("  retry tickets by connector:")
        for name, reasons in sorted(r.dirty_summary["by_connector"].items()):
            detail = ", ".join(f"{k}={v:,}" for k, v in sorted(reasons.items()))
            say(f"      {name:<16} {detail}")
    if r.feeds:
        say("  connector feeds:")
        for feed in r.feeds:
            say(f"      {feed['group']:<13} {feed['feed']:<24} {feed['nodes']:>8,} nodes")
    if r.quarantined:
        reasons: dict[str, int] = {}
        for q in r.quarantined:
            key = str(q["reason"]).split(":")[-1].strip()[:60]
            reasons[key] = reasons.get(key, 0) + 1
        say(f"  quarantined          {len(r.quarantined):,}")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:6]:
            say(f"      {count:>7,}  {reason}")
    say("=" * 92)
    try:
        say(r.edge_stats.format_table(shapes_per_predicate=2))
    except Exception as exc:   # a report is never worth losing a nine-hour run's summary over
        say(f"  (edge statistics table unavailable: {type(exc).__name__}: {exc})")


def _reason_counts(quarantined: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for q in quarantined:
        key = f"{q.get('scope', '?')}: {str(q.get('reason'))[:120]}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _write_summary(r, out_dir: str, panel_doc: dict, connector_names: list[str],
                    stubs: list[str]) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "panel": panel_doc.get("name"),
        "genes_completed": r.genes_completed,
        "stopped_early": r.stopped_early,
        "connectors_run": connector_names,
        "connectors_stubbed": stubs,
        "totals": {
            "nodes": r.nodes_written, "edges": r.edges_written,
            "variants_requested": r.variants_requested, "variants_ingested": r.variants_ingested,
            "batches_committed": r.batches_committed, "batches_rejected": r.batches_rejected,
            "identity_merges": r.identity_merges, "dirty_nodes": r.dirty_nodes,
            "quarantined": len(r.quarantined), "elapsed_seconds": r.elapsed_seconds,
        },
        # The reasons, not just the count. A count says something went wrong; only the
        # reason says whether it was one bad record or a systematic mapping error.
        "quarantine_reasons": _reason_counts(r.quarantined),
        "rejected_batches": [q for q in r.quarantined if q.get("scope") == "batch"][:50],
        "dirty": r.dirty_summary,
        "allocations": [
            {"gene": a.gene, "family_size": a.family_size, "budget": a.budget,
             "resolved": a.resolved}
            for a in r.allocations
        ],
        "feeds": r.feeds,
        "edge_stats": r.edge_stats.to_dict(),
        "coverage": {name: rep.mapping_origin_summary() for name, rep in r.coverage.items()},
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    with open(os.path.join(out_dir, "events.json"), "w", encoding="utf-8") as f:
        json.dump({"events": r.events, "feeds": r.feeds}, f, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
