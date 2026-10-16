"""Generates real graph-formation event data for the presentation visualization.

Reuses the exact same connector code the E2E test uses -- no synthetic/mocked data.
Runs the real pipeline across a "range" of two real ClinVar VHL variants (the clean
VCV000002218 and the conflicting VCV004558169) against the shared persistent Core
Biology (Ensembl) and Operational (ClinGen) layers, capturing every node/edge creation
as an ordered, wave-tagged event for the frontend to replay.

Output: demo/graph_data.json
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forge_ir.graph import ForgeEdge, ForgeNode
from connectors.clingen import CRITERIA_MAPPING_SPEC, ClinGenParser
from connectors.clinvar import ClinVarParser, _build_trait_crosswalk, build_mapping_spec
from connectors.common import build_variant_entity
from connectors.ensembl import EnsemblParser, GENE_MAPPING_SPEC, vep_consequence_to_ir
from connectors.gnomad import GnomadParser, frequency_to_ir
from ingest.compiler import StagingCompiler
from ingest.identity import IdentityIndex
from ingest.mapping import apply_mapping_spec

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")
NOW = datetime.now(timezone.utc)

VARIANT_1_ID = "SPDI:NC_000003.12:10149821:C:T"   # VCV000002218 -- clean, c.499C>T / p.Arg167Trp
VARIANT_2_ID = "SPDI:NC_000003.12:10142087:C:A"   # VCV004558169 -- conflicting, c.241C>A / p.Pro81Thr


def _load_json(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def _load_text(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


def _node_event(n: ForgeNode, wave: str, order: int) -> dict:
    label = n.attributes.get("symbol") or n.attributes.get("label") or n.attributes.get("accession") \
        or n.attributes.get("name") or n.id
    return {
        "kind": "node", "order": order, "wave": wave,
        "id": n.id, "role": n.role.value, "subtype": n.subtype, "layer": n.layer.value,
        "origin": n.origin.value, "label": str(label)[:60], "attributes": _safe_attrs(n.attributes),
    }


def _edge_event(e: ForgeEdge, by_id: dict, wave: str, order: int) -> dict:
    src_node, dst_node = by_id.get(e.src), by_id.get(e.dst)
    return {
        "kind": "edge", "order": order, "wave": wave,
        "src": e.src, "dst": e.dst, "predicate": e.predicate.value, "qualifier": e.qualifier,
        "origin": e.origin.value,
        "src_layer": src_node.layer.value if src_node else None,
        "dst_layer": dst_node.layer.value if dst_node else None,
    }


def _safe_attrs(attrs: dict) -> dict:
    out = {}
    for k, v in attrs.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list) and len(v) <= 6:
            out[k] = v
        else:
            out[k] = f"<{type(v).__name__}, {len(v) if hasattr(v, '__len__') else '?'} items>"
    return out


def main():
    compiler = StagingCompiler(IdentityIndex(), run_id="demo")
    events: list[dict] = []
    order = 0
    by_id: dict[str, ForgeNode] = {}

    def emit(wave: str, nodes: list[ForgeNode], edges: list[ForgeEdge]):
        nonlocal order
        for n in nodes:
            by_id[n.id] = n
            events.append(_node_event(n, wave, order))
            order += 1
        for e in edges:
            events.append(_edge_event(e, by_id, wave, order))
            order += 1
        compiler.stage(nodes, edges)

    # --- Wave 1: Core Biology (persistent, shared by every variant of this gene) ---
    gene_raw = _load_json("ensembl_gene_VHL.json")
    gene = EnsemblParser().parse_gene(gene_raw)
    gene_results = apply_mapping_spec(gene, GENE_MAPPING_SPEC, primary_node_id=None, source="Ensembl",
                                       source_record_id=gene.ensembl_gene_id, source_version=gene.source_version,
                                       retrieved_at=NOW)
    emit("core_biology", [r for r in gene_results if isinstance(r, ForgeNode)],
         [r for r in gene_results if isinstance(r, ForgeEdge)])

    # --- Wave 2: Operational (persistent, shared -- the full real VHL VCEP rule set) ---
    clingen_raw = _load_json("clingen_vhl_vcep_GN078_v1.1.json")
    for criterion in ClinGenParser().parse(clingen_raw):
        results = apply_mapping_spec(criterion, CRITERIA_MAPPING_SPEC, primary_node_id=None, source="ClinGen",
                                      source_record_id=criterion.source_record_id, source_version=criterion.version,
                                      retrieved_at=NOW)
        emit("operational", [r for r in results if isinstance(r, ForgeNode)],
             [r for r in results if isinstance(r, ForgeEdge)])

    # --- Wave 3: Evidence for variant 1 (VCV000002218 -- clean) ---
    v1 = build_variant_entity("NC_000003.12", 10149821, "C", "T", "GRCh38", "ClinVar", "VCV000002218", "44", NOW)
    emit("evidence_v1", [v1], [])

    vep_raw = _load_json("ensembl_vep_c499CT.json")
    for consequence in EnsemblParser().parse_vep(vep_raw):
        results = vep_consequence_to_ir(consequence, VARIANT_1_ID)
        emit("evidence_v1", [r for r in results if isinstance(r, ForgeNode)],
             [r for r in results if isinstance(r, ForgeEdge)])

    gnomad_raw = _load_json("gnomad_variant_3-10149822-C-T.json")
    for freq in GnomadParser().parse(gnomad_raw):
        results = frequency_to_ir(freq, VARIANT_1_ID)
        emit("evidence_v1", [r for r in results if isinstance(r, ForgeNode)],
             [r for r in results if isinstance(r, ForgeEdge)])

    clinvar_xml_1 = _load_text("clinvar_efetch_VCV2218.xml")
    clinvar_summary_1 = _load_json("clinvar_esummary_VCV2218.json")
    p1 = ClinVarParser()
    vcv1, rcvs1, scvs1 = p1.parse_vcv(clinvar_xml_1), list(p1.parse_rcvs(clinvar_xml_1)), list(p1.parse_scvs(clinvar_xml_1))
    crosswalk1 = _build_trait_crosswalk(clinvar_summary_1)
    spec1 = build_mapping_spec(vcv1, rcvs1, scvs1, crosswalk1)
    results1 = apply_mapping_spec(vcv1, spec1, primary_node_id=None, source="ClinVar",
                                   source_record_id=vcv1.accession, source_version=vcv1.version, retrieved_at=NOW)
    emit("evidence_v1", [r for r in results1 if isinstance(r, ForgeNode)],
         [r for r in results1 if isinstance(r, ForgeEdge)])

    # --- Wave 4: Evidence for variant 2 (VCV004558169 -- conflicting) ---
    v2 = build_variant_entity("NC_000003.12", 10142087, "C", "A", "GRCh38", "ClinVar", "VCV004558169", "2", NOW)
    emit("evidence_v2", [v2], [])

    clinvar_xml_2 = _load_text("clinvar_efetch_VCV4558169_conflicting.xml")
    clinvar_summary_2 = _load_json("clinvar_esummary_VCV4558169_conflicting.json")
    p2 = ClinVarParser()
    vcv2, rcvs2, scvs2 = p2.parse_vcv(clinvar_xml_2), list(p2.parse_rcvs(clinvar_xml_2)), list(p2.parse_scvs(clinvar_xml_2))
    crosswalk2 = _build_trait_crosswalk(clinvar_summary_2)
    spec2 = build_mapping_spec(vcv2, rcvs2, scvs2, crosswalk2)
    results2 = apply_mapping_spec(vcv2, spec2, primary_node_id=None, source="ClinVar",
                                   source_record_id=vcv2.accession, source_version=vcv2.version, retrieved_at=NOW)
    emit("evidence_v2", [r for r in results2 if isinstance(r, ForgeNode)],
         [r for r in results2 if isinstance(r, ForgeEdge)])

    result = compiler.commit()
    if not result.committed:
        raise SystemExit(f"commit failed: {result.rejection_reason}")

    stats = {
        "total_nodes": len(result.nodes), "total_edges": len(result.edges),
        "by_layer": {}, "by_wave": {},
    }
    for ev in events:
        if ev["kind"] == "node":
            stats["by_layer"].setdefault(ev["layer"], 0)
            stats["by_layer"][ev["layer"]] += 1
            stats["by_wave"].setdefault(ev["wave"], {"nodes": 0, "edges": 0})
            stats["by_wave"][ev["wave"]]["nodes"] += 1
        else:
            stats["by_wave"].setdefault(ev["wave"], {"nodes": 0, "edges": 0})
            stats["by_wave"][ev["wave"]]["edges"] += 1

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "graph_data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"events": events, "stats": stats, "verified": result.verification.valid}, f, indent=1)

    print(f"Wrote {len(events)} events ({stats['total_nodes']} nodes, {stats['total_edges']} edges) to {out_path}")
    print(f"Committed and verified: {result.verification.valid}")
    print("By layer:", stats["by_layer"])
    print("By wave:", stats["by_wave"])


if __name__ == "__main__":
    main()
