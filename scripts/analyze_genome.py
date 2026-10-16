"""Match a genome/gene query to a large Forge graph and write an explanation.

Examples:
    python scripts/analyze_genome.py --graph demo/graph_data.json --gene VHL
    python scripts/analyze_genome.py --graph demo/graph_data.json --variant SPDI:NC_000003.12:10149821:C:T

The command is offline by default.  It uses the deterministic retrieval
baseline in ``analysis.graph_rag``; an external LLM can consume the emitted
compact evidence context later, but no credential or network call is needed
to produce the plot and plain-language report.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.graph_rag import GraphRetriever, GraphSnapshot, render_report_html, plain_language_summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph", default="demo/graph_data.json",
                        help="event-log JSON or Forge JSONL gzip dump")
    query = parser.add_mutually_exclusive_group(required=True)
    query.add_argument("--gene", help="gene symbol or gene identifier to match")
    query.add_argument("--variant", help="variant identifier, HGVS text, or a distinctive allele string")
    query.add_argument("--query", help="free-text node query")
    parser.add_argument("--lod", type=int, choices=(1, 2, 3), default=2, help="1 summary, 2 standard, 3 deep")
    parser.add_argument("--budget", type=int, default=12000,
                        help="approximate serialization-token budget (default: 12000)")
    parser.add_argument("--max-hops", type=int, default=4, help="maximum admissible path depth")
    parser.add_argument("--out-html", default=None, help="HTML/SVG report path (default: runs/graph_report.html)")
    parser.add_argument("--out-json", default=None, help="machine-readable report path (default: runs/graph_report.json)")
    parser.add_argument("--print-context", action="store_true", help="print compact evidence context after the summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    graph_path = Path(args.graph)
    if not graph_path.is_absolute():
        graph_path = ROOT / graph_path
    if not graph_path.exists():
        print(f"error: graph file not found: {graph_path}", file=sys.stderr)
        return 2
    terms = [args.gene or args.variant or args.query]
    try:
        snapshot = GraphSnapshot.from_json(graph_path)
        evidence = GraphRetriever().retrieve(
            snapshot, terms, lod=args.lod, token_budget=args.budget, max_hops=args.max_hops,
            seed_subtype="Gene" if args.gene else "Variant" if args.variant else None,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    html_path = Path(args.out_html or "runs/graph_report.html")
    json_path = Path(args.out_json or "runs/graph_report.json")
    if not html_path.is_absolute():
        html_path = ROOT / html_path
    if not json_path.is_absolute():
        json_path = ROOT / json_path
    html_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_report_html(evidence), encoding="utf-8")
    report = {
        "snapshot_id": evidence.snapshot_id,
        "query": list(evidence.query_terms),
        "summary": plain_language_summary(evidence),
        "lod": evidence.lod,
        "grammar": evidence.grammar_name,
        "seeds": list(evidence.seeds),
        "nodes": [{"id": n.id, "label": n.label, "subtype": n.subtype,
                   "role": n.role, "rwr_score": evidence.rwr_scores.get(n.id, 0),
                   "head_scores": dict(evidence.head_scores.get(n.id, {}))} for n in evidence.nodes],
        "edges": [{"edge_id": e.edge_id, "src": e.src, "predicate": e.predicate,
                   "qualifier": e.qualifier, "dst": e.dst, "source_ids": list(e.source_ids)} for e in evidence.edges],
        "paths": [{"path_id": p.path_id, "node_ids": list(p.node_ids), "edge_ids": list(p.edge_ids)} for p in evidence.paths],
        "budget_exhausted": evidence.budget_exhausted,
        "compact_context": evidence.serialize(),
        "ai_note": "Deterministic heuristic attention baseline; no clinical diagnosis or external LLM claim is made.",
    }
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(plain_language_summary(evidence))
    print(f"Plot/report: {html_path.relative_to(ROOT) if html_path.is_relative_to(ROOT) else html_path}")
    print(f"JSON evidence: {json_path.relative_to(ROOT) if json_path.is_relative_to(ROOT) else json_path}")
    if args.print_context:
        print("\n" + evidence.serialize())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
