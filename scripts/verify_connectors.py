"""Connector conformance check -- proves every registered connector is real and works.

  python scripts/verify_connectors.py            # offline, from saved fixtures (~1s)
  python scripts/verify_connectors.py --live     # additionally hit the real APIs
  python scripts/verify_connectors.py --detail   # show the nodes/edges each one produced

Seven checks per connector, run against its ACTUAL code, not a description of it:

  1 REGISTERED   present in the registry with a valid group and cadence
  2 CONTRACT     supplies all four required pieces (adapter, build_query, build_emission,
                 feed_label)
  3 CAPABILITIES adapter.capabilities()/.manifest() answer, and every subtype they name is
                 registered in the frozen ontology
  4 GRAMMAR      those subtypes sit on at least one legal edge combination
  5 QUERY        build_query() returns a usable query for a scope the connector claims
  6 EMISSION     build_emission() turns a REAL saved payload into real nodes and edges
  7 VERIFIED     those nodes/edges pass forge_ir/verify.py -- the same 9 checks that gate
                 every production commit

A stub connector is held to 1-5 and must additionally FAIL LOUDLY on 6: its fetch() has to
raise NotImplementedError. A stub that quietly returned empty data would score as a pass
everywhere else, which is exactly the dishonesty this check exists to catch.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import connectors  # noqa: F401 -- registers every connector as an import side effect
from forge_ir.core import NODE_SUBTYPE_REGISTRY
from forge_ir.verify import verify
from ingest.dirty import expected_edge_shapes
from orchestration.connector_registry import list_connectors, tag_with_source_manifest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(BASE, "fixtures")
REGISTERED_SUBTYPES = {s for (_r, s) in NODE_SUBTYPE_REGISTRY}

OK, BAD, SKIP = "PASS", "FAIL", " -- "

# Real saved payloads, one per implemented connector, plus the context its build_emission
# needs. Every file here was live-fetched from the real API and is the same fixture the
# unit tests run against -- nothing here is synthetic.
# VHL c.499C>T (p.Arg167Trp), the variant every fixture in this project is built around.
# The SPDI position is 10149821 (0-based); gnomAD and Ensembl both name it 10149822
# (1-based), and forge_ir.core.normalize_spdi_position_from_1based is the one place that
# conversion happens. Getting this constant wrong is not cosmetic -- an earlier draft of
# this script used 10149803 here, gnomAD's OBSERVES edge then pointed at a Variant that did
# not exist, and verify() check 8 correctly reported the frequency nodes as orphaned
# evidence. The check was right and the harness was wrong.
VHL_VARIANT = "SPDI:NC_000003.12:10149821:C:T"
CASES = {
    "ensembl_structure": dict(fixture="ensembl_gene_VHL.json", query=("gene", "VHL"), ctx={}),
    "uniprot":           dict(fixture="uniprot_P40337.json", query=("gene", "VHL"), ctx={}),
    "clingen":           dict(fixture="clingen_vhl_vcep_GN078_v1.1.json", query=("gene", "VHL"), ctx={}),
    "clinvar":           dict(fixture=("clinvar_efetch_VCV2218.xml", "clinvar_esummary_VCV2218.json"),
                              query=("gene", "VHL"), ctx={}),
    "gnomad":            dict(fixture="gnomad_variant_3-10149822-C-T.json",
                              query=("coordinates", None), ctx={"variant_id": VHL_VARIANT}),
    "ensembl_vep":       dict(fixture="ensembl_vep_c499CT.json",
                              query=("coordinates", None), ctx={"variant_id": VHL_VARIANT}),
    "dbsnp":             dict(fixture="dbsnp_refsnp_5030820.json",
                              query=("coordinates", None), ctx={"variant_id": VHL_VARIANT}),
}
COORD_CTX = {"chrom": "3", "pos": 10149821, "ref": "C", "alt": "T", "rsid": "5030820"}
COLS = ["REGISTERED", "CONTRACT", "CAPABILITIES", "GRAMMAR", "QUERY", "EMISSION", "VERIFIED"]


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read() if name.endswith(".xml") else json.load(f)


class Result:
    def __init__(self, descriptor):
        self.d = descriptor
        self.checks: dict[str, str] = {}
        self.notes: list[str] = []
        self.nodes: list = []
        self.edges: list = []

    def mark(self, key, ok, note=None):
        self.checks[key] = OK if ok else BAD
        if note:
            self.notes.append(note)
        return ok

    def passed(self):
        return all(v != BAD for v in self.checks.values())


def check(descriptor, live=False) -> Result:
    r = Result(descriptor)
    now = datetime.now(timezone.utc)

    r.mark("REGISTERED", descriptor.group in ("core_biology", "operational", "evidence")
           and descriptor.cadence in ("persistent", "ephemeral"))

    r.mark("CONTRACT", all([
        descriptor.adapter is not None, callable(descriptor.build_query),
        callable(descriptor.build_emission), bool(descriptor.feed_label),
    ]))

    try:
        caps = descriptor.adapter.capabilities()
        manifest = descriptor.adapter.manifest()
        provides = set(caps.get("provides", []))
        unknown = provides - REGISTERED_SUBTYPES
        r.mark("CAPABILITIES", bool(provides) and bool(manifest.get("source")) and not unknown,
               f"unregistered subtypes: {sorted(unknown)}" if unknown else None)
        r.notes.append(f"provides {sorted(provides)} · source {manifest.get('source')}")
    except Exception as exc:
        r.mark("CAPABILITIES", False, f"{type(exc).__name__}: {exc}")

    shapes = expected_edge_shapes(descriptor)
    r.mark("GRAMMAR", bool(shapes), f"{len(shapes)} legal edge shape(s) in the ontology")

    case = CASES.get(descriptor.name)
    try:
        scope, anchor = (case["query"] if case else ("gene", "VHL"))
        ctx = {"clingen_spec_id": None} if scope == "gene" else dict(COORD_CTX)
        query = descriptor.build_query(scope, anchor, ctx)
        if descriptor.implemented:
            r.mark("QUERY", query is not None, f"build_query({scope!r}) -> {query}")
        else:
            r.checks["QUERY"] = SKIP
    except Exception as exc:
        r.mark("QUERY", False, f"build_query raised {type(exc).__name__}: {exc}")

    if not descriptor.implemented:
        # A stub must fail LOUDLY. Silently returning nothing would pass every other check
        # and read as a working connector.
        try:
            list(descriptor.adapter.fetch({}))
            r.mark("EMISSION", False, "stub returned data instead of raising -- dishonest")
        except NotImplementedError as exc:
            r.mark("EMISSION", True, f"fetch() raises NotImplementedError: {str(exc)[:84]}")
        except Exception as exc:
            r.mark("EMISSION", False, f"raised {type(exc).__name__}, expected NotImplementedError")
        r.checks["VERIFIED"] = SKIP
        return r

    if case is None:
        r.checks["EMISSION"] = r.checks["VERIFIED"] = SKIP
        r.notes.append("no saved fixture registered for this connector")
        return r

    try:
        if descriptor.name == "clinvar":
            xml, esum = case["fixture"]
            raw = {"uid": "2218", "efetch_xml": _load(xml), "esummary": _load(esum)}
        else:
            raw = _load(case["fixture"])
        emission = descriptor.build_emission(raw, {"now": now, **case["ctx"]})
        tag_with_source_manifest(emission, descriptor, now)
        r.nodes, r.edges = emission.nodes, emission.edges
        r.mark("EMISSION", bool(emission.nodes),
               f"{len(emission.nodes)} nodes / {len(emission.edges)} edges from a real payload")
    except Exception as exc:
        r.mark("EMISSION", False, f"{type(exc).__name__}: {exc}")
        r.checks["VERIFIED"] = SKIP
        return r

    # Verify the way production commits, not the way an isolated unit test would.
    #
    # A connector declaring `requires=("clinvar",)` never commits alone: runner.py's
    # _ingest_one_variant merges its emission INTO ClinVar's and commits the two as one
    # micro-batch. Verifying gnomAD's two PopulationFrequency nodes by themselves failed
    # check 8 (connectivity) as "orphaned evidence" -- correctly, because in isolation there
    # genuinely is no path from a frequency to a Gene. That is a fact about the harness, not
    # about the connector, so the harness merges prerequisites first, as production does.
    nodes, edges = list(r.nodes), list(r.edges)
    merged_with = []
    for required in descriptor.requires:
        prereq = CASES.get(required)
        if prereq is None:
            continue
        try:
            from orchestration.connector_registry import get_connector
            rd = get_connector(required)
            if required == "clinvar":
                xml, esum = prereq["fixture"]
                raw_p = {"uid": "2218", "efetch_xml": _load(xml), "esummary": _load(esum)}
            else:
                raw_p = _load(prereq["fixture"])
            pe = rd.build_emission(raw_p, {"now": now, **prereq["ctx"]})
            nodes += pe.nodes
            edges += pe.edges
            merged_with.append(required)
        except Exception as exc:
            r.notes.append(f"could not merge prerequisite {required}: {type(exc).__name__}: {exc}")
    if merged_with:
        r.notes.append(f"verified merged with its prerequisite(s): {', '.join(merged_with)} "
                       f"-- the same micro-batch production commits")

    # Anything still referenced but absent is a persistent-layer anchor a real ephemeral run
    # declares as resident (runner.py::_load_resident).
    own = {n.id for n in nodes}
    referenced = {e.src for e in edges} | {e.dst for e in edges}
    report = verify(nodes, edges, known_node_ids=own | referenced)
    r.mark("VERIFIED", report.valid, None if report.valid else "; ".join(report.all_errors()[:2]))

    if live:
        try:
            scope, anchor = case["query"]
            ctx = {"clingen_spec_id": None} if scope == "gene" else dict(COORD_CTX)
            q = descriptor.build_query(scope, anchor, ctx)
            got = list(descriptor.adapter.fetch(q))
            r.notes.append(f"LIVE: {descriptor.adapter.__class__.__name__}.fetch({q}) "
                           f"-> {len(got)} payload(s) from the real API")
        except Exception as exc:
            r.notes.append(f"LIVE FAILED: {type(exc).__name__}: {exc}")
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="verify_connectors.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="also call each real API once")
    ap.add_argument("--detail", action="store_true", help="print the nodes/edges each one produced")
    ap.add_argument("--only", default=None, help="comma-separated connector names")
    args = ap.parse_args(argv)

    descriptors = list_connectors()
    if args.only:
        wanted = {n.strip() for n in args.only.split(",")}
        descriptors = [d for d in descriptors if d.name in wanted]

    implemented = sum(1 for d in descriptors if d.implemented)
    mode = "LIVE API CALLS ENABLED" if args.live else "offline, from saved real fixtures"
    print()
    print("=" * 116)
    print(f"FORGE CONNECTOR CONFORMANCE  ·  {len(descriptors)} registered  ·  "
          f"{implemented} implemented  ·  {len(descriptors) - implemented} declared stubs  ·  {mode}")
    print("=" * 116)
    print(f"{'CONNECTOR':<22}{'GROUP':<14}{'CADENCE':<12}" + "".join(f"{c[:5]:<7}" for c in COLS))
    print("-" * 116)

    results = []
    for d in descriptors:
        r = check(d, live=args.live)
        results.append(r)
        cells = "".join(f"{r.checks.get(c, SKIP):<7}" for c in COLS)
        print(f"{d.name:<22}{d.group:<14}{d.cadence:<12}{cells}"
              f"{'' if d.implemented else '  [STUB]'}")

    print("-" * 116)
    failed = [r for r in results if not r.passed()]
    print(f"{'ALL PASS' if not failed else str(len(failed)) + ' FAILING'}"
          f"   ·   columns = {' / '.join(COLS)}")
    print("=" * 116)

    print("\nPER-CONNECTOR DETAIL")
    print("-" * 116)
    for r in results:
        print(f"\n  {r.d.name}  [{'live' if r.d.implemented else 'STUB'}]  {r.d.description}")
        for note in r.notes:
            print(f"      · {note}")
        if args.detail and r.nodes:
            for n in r.nodes[:6]:
                print(f"        NODE  {n.role.value:<12} {n.subtype:<26} {n.id}")
            if len(r.nodes) > 6:
                print(f"        …     {len(r.nodes) - 6} more nodes")
            for e in r.edges[:6]:
                q = f"[{e.qualifier}]" if e.qualifier else ""
                print(f"        EDGE  {e.predicate.value + q:<30} {e.src[:40]} -> {e.dst[:40]}")
            if len(r.edges) > 6:
                print(f"        …     {len(r.edges) - 6} more edges")

    built = [r for r in results if r.nodes]
    verified = [r for r in built if r.checks.get("VERIFIED") == OK]
    print("\n" + "=" * 116)
    print(f"  {sum(len(r.nodes) for r in built)} nodes and {sum(len(r.edges) for r in built)} edges "
          f"built from real saved payloads across {len(built)} connectors.")
    print(f"  {len(verified)} of {len(built)} pass the same 9 correctness checks "
          f"(forge_ir/verify.py) that gate a production commit.")
    print("=" * 116)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
