"""The VHL / PM2 end-to-end gate -- forge-pipeline-v1.md §15/§16.

Real 4 connectors, real fixtures, full pipeline: parse -> map -> stage -> identity
resolve -> verify -> atomic commit -> find PM2's Requirement and the real gnomAD
observation in the COMMITTED graph -> RequirementEvaluator -> RequirementState reaching
a terminal status computed from the real numbers, not hardcoded.

This is the boundary the whole architecture exists to guarantee: every node MHLA will
ever see has already survived a deterministic, verified, source-to-semantic compilation.
"""
import json
import os
from datetime import datetime, timezone

import pytest

from forge_ir.core import EdgeType, NodeRole
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.requirement import EvaluationMode, Requirement, evaluate_requirement
from connectors.clingen import CRITERIA_MAPPING_SPEC, ClinGenParser
from connectors.clinvar import ClinVarParser, _build_trait_crosswalk, build_mapping_spec
from connectors.common import build_variant_entity
from connectors.ensembl import EnsemblParser, GENE_MAPPING_SPEC, vep_consequence_to_ir
from connectors.gnomad import GnomadParser, frequency_to_ir
from ingest.compiler import StagingCompiler
from ingest.identity import IdentityIndex
from ingest.mapping import apply_mapping_spec

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")
NOW = datetime.now(timezone.utc)

# Confirmed real identity for this test case, established across every prior stage of
# this project (connector-research.md, forge-ir-v3-schema.md, forge-pipeline-v1.md):
# VCV000002218.44, NM_000551.4:c.499C>T / p.Arg167Trp, GRCh38 3:10149822 C>T, rs5030820.
VARIANT_ID = "SPDI:NC_000003.12:10149821:C:T"


def _load_json(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def _load_text(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


@pytest.fixture(scope="module")
def committed_graph():
    compiler = StagingCompiler(IdentityIndex(), run_id="vhl_pm2_e2e")

    variant_node = build_variant_entity(
        "NC_000003.12", 10149821, "C", "T", "GRCh38",
        source="ClinVar", source_record_id="VCV000002218", source_version="44", retrieved_at=NOW,
    )
    compiler.stage([variant_node], [])

    # --- Ensembl: gene structure + transcript-scoped VEP consequences ---
    gene_raw = _load_json("ensembl_gene_VHL.json")
    gene = EnsemblParser().parse_gene(gene_raw)
    gene_results = apply_mapping_spec(gene, GENE_MAPPING_SPEC, primary_node_id=None, source="Ensembl",
                                       source_record_id=gene.ensembl_gene_id, source_version=gene.source_version,
                                       retrieved_at=NOW)
    compiler.stage([r for r in gene_results if isinstance(r, ForgeNode)],
                    [r for r in gene_results if isinstance(r, ForgeEdge)])

    vep_raw = _load_json("ensembl_vep_c499CT.json")
    for consequence in EnsemblParser().parse_vep(vep_raw):
        vep_results = vep_consequence_to_ir(consequence, VARIANT_ID)
        compiler.stage([r for r in vep_results if isinstance(r, ForgeNode)],
                        [r for r in vep_results if isinstance(r, ForgeEdge)])

    # --- ClinVar: full VCV/RCV/SCV hierarchy for the real test variant ---
    clinvar_xml = _load_text("clinvar_efetch_VCV2218.xml")
    clinvar_summary = _load_json("clinvar_esummary_VCV2218.json")
    clinvar_parser = ClinVarParser()
    vcv = clinvar_parser.parse_vcv(clinvar_xml)
    rcvs = list(clinvar_parser.parse_rcvs(clinvar_xml))
    scvs = list(clinvar_parser.parse_scvs(clinvar_xml))
    crosswalk = _build_trait_crosswalk(clinvar_summary)
    clinvar_spec = build_mapping_spec(vcv, rcvs, scvs, crosswalk)
    clinvar_results = apply_mapping_spec(vcv, clinvar_spec, primary_node_id=None, source="ClinVar",
                                          source_record_id=vcv.accession, source_version=vcv.version,
                                          retrieved_at=NOW)
    compiler.stage([r for r in clinvar_results if isinstance(r, ForgeNode)],
                    [r for r in clinvar_results if isinstance(r, ForgeEdge)])

    # --- gnomAD: population frequency for the real test variant ---
    gnomad_raw = _load_json("gnomad_variant_3-10149822-C-T.json")
    for freq in GnomadParser().parse(gnomad_raw):
        freq_results = frequency_to_ir(freq, VARIANT_ID)
        compiler.stage([r for r in freq_results if isinstance(r, ForgeNode)],
                        [r for r in freq_results if isinstance(r, ForgeEdge)])

    # --- ClinGen: full VHL VCEP rule set (Operational, bulk-loaded, gene-scoped) ---
    clingen_raw = _load_json("clingen_vhl_vcep_GN078_v1.1.json")
    for criterion in ClinGenParser().parse(clingen_raw):
        criterion_results = apply_mapping_spec(criterion, CRITERIA_MAPPING_SPEC, primary_node_id=None,
                                                source="ClinGen", source_record_id=criterion.source_record_id,
                                                source_version=criterion.version, retrieved_at=NOW)
        compiler.stage([r for r in criterion_results if isinstance(r, ForgeNode)],
                        [r for r in criterion_results if isinstance(r, ForgeEdge)])

    result = compiler.commit()
    return result


def test_pipeline_commits_successfully(committed_graph):
    assert committed_graph.committed, committed_graph.rejection_reason
    assert committed_graph.verification.valid, committed_graph.verification.all_errors()


def test_exactly_one_canonical_variant_node(committed_graph):
    variants = [n for n in committed_graph.nodes if n.role == NodeRole.ENTITY and n.subtype == "Variant"]
    assert len(variants) == 1
    assert variants[0].id == VARIANT_ID


def test_variant_connects_to_ensembl_biology(committed_graph):
    consequence_edges = [
        e for e in committed_graph.edges
        if e.src == VARIANT_ID and e.qualifier == "has_consequence"
    ]
    assert len(consequence_edges) == 15  # confirmed real count, all 15 VHL transcripts


def test_variant_connects_to_clinvar_claims(committed_graph):
    scv_claims = [n for n in committed_graph.nodes if n.subtype == "ClinicalAssertion"]
    assert len(scv_claims) == 22
    asserts_edges = [e for e in committed_graph.edges if e.predicate == EdgeType.ASSERTS and e.dst == VARIANT_ID]
    assert len(asserts_edges) >= 22  # SCVs + RCV/VCV rollups all ASSERT on the variant


def test_variant_connects_to_gnomad_evidence(committed_graph):
    freq_obs = [n for n in committed_graph.nodes if n.subtype == "PopulationFrequency"]
    assert len(freq_obs) == 2  # genome + exome
    genome_obs = next(n for n in freq_obs if n.attributes["dataset"] == "genome")
    assert genome_obs.attributes["ac"] == 1
    assert genome_obs.attributes["faf95_popmax"] is None  # the real, confirmed null


def test_pm2_criterion_and_all_five_requirements_present(committed_graph):
    pm2_criterion = next(n for n in committed_graph.nodes
                          if n.subtype == "ACMGCriterion" and n.attributes["label"] == "PM2")
    assert pm2_criterion.id == "ClinGen:GN078:PM2:1.1"

    pm2_requirements = [
        n for n in committed_graph.nodes
        if n.subtype == "Requirement" and n.id.startswith("ClinGen:GN078:PM2:1.1:req:")
    ]
    assert len(pm2_requirements) == 5

    applies_to = [e for e in committed_graph.edges if e.src == pm2_criterion.id
                  and e.predicate == EdgeType.APPLIES_TO]
    assert applies_to[0].dst == "HGNC:12687"


def test_the_actual_gate_pm2_requirement_evaluates_against_real_gnomad_observation(committed_graph):
    """The real gate. Everything above just proves the graph is correctly shaped --
    THIS is the test that proves the whole invariant chain end to end: ClinGen rule ->
    Requirement -> real gnomAD observation -> RequirementEvaluator -> terminal status,
    computed fresh from real values, not asserted in advance."""
    supporting_req_node = next(
        n for n in committed_graph.nodes
        if n.subtype == "Requirement" and n.id.startswith("ClinGen:GN078:PM2:1.1:req:")
        and n.attributes["strength"] == "Supporting"
    )
    assert supporting_req_node.attributes["applicability"] == "Applicable"

    requirement = Requirement(
        criterion_id=supporting_req_node.attributes["criterion_id"],
        requirement_id=supporting_req_node.id,
        ordinal=supporting_req_node.attributes["ordinal"],
        strength=supporting_req_node.attributes["strength"],
        applicability=supporting_req_node.attributes["applicability"],
        evaluation_mode=EvaluationMode(supporting_req_node.attributes["evaluation_mode"]),
        constraints=supporting_req_node.attributes["constraints"],
        source_text=supporting_req_node.attributes["source_text"],
    )

    genome_obs_node = next(
        n for n in committed_graph.nodes
        if n.subtype == "PopulationFrequency" and n.attributes["dataset"] == "genome"
    )
    observation = {
        "af": genome_obs_node.attributes["af"],
        "ac": genome_obs_node.attributes["ac"],
        "an": genome_obs_node.attributes["an"],
        "faf95_popmax": genome_obs_node.attributes["faf95_popmax"],
    }

    status, reason = evaluate_requirement(requirement, observation, source="gnomAD", source_version="r4")

    # Computed, not hardcoded: PM2's real threshold metric (faf95_popmax) is null for
    # this real singleton variant (ac=1), and ClinGen's own spec text provides a documented
    # fallback for exactly that case -- the correct real answer is SATISFIED, discovered
    # during implementation, not assumed beforehand (see forge-pipeline-v1.md §12).
    assert status == "SATISFIED"
    assert "fallback applied" in reason["constraint"]
    assert reason["observed"]["ac"] == 1


def test_pvs1_requirements_cannot_auto_satisfy(committed_graph):
    pvs1_requirements = [
        n for n in committed_graph.nodes
        if n.subtype == "Requirement" and n.id.startswith("ClinGen:GN078:PVS1:1.1:req:")
    ]
    assert len(pvs1_requirements) == 5
    for req_node in pvs1_requirements:
        requirement = Requirement(
            criterion_id=req_node.attributes["criterion_id"], requirement_id=req_node.id,
            ordinal=req_node.attributes["ordinal"], strength=req_node.attributes["strength"],
            applicability=req_node.attributes["applicability"],
            evaluation_mode=EvaluationMode(req_node.attributes["evaluation_mode"]),
            constraints=req_node.attributes["constraints"], source_text=req_node.attributes["source_text"],
        )
        status, _ = evaluate_requirement(requirement, {"anything": 1})
        assert status == "NEEDS_INTERPRETATION"
