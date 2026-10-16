"""Tests for the whole-family batch path in connectors/clinvar.py.

Everything here is offline: multi-record E-utilities responses are assembled by
concatenating the two real single-record fixtures, which is exactly the shape NCBI
returns for a comma-separated id list. What's under test is the re-split -- the step
that lets 1,100-variant runs reuse parsers written and verified against one record.
"""
import json
import os
import xml.etree.ElementTree as ET

import pytest

from connectors.clinvar import (
    ClinVarParser, UnsupportedClinVarRecord, _build_trait_crosswalk, _unmapped_disease_id,
    build_mapping_spec, split_archives, split_esummary,
)
from forge_ir.graph import ForgeNode
from ingest.mapping import apply_mapping_spec
from datetime import datetime, timezone

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _archive_xml(name: str) -> str:
    with open(os.path.join(FIXTURES, name)) as f:
        root = ET.fromstring(f.read())
    return ET.tostring(root.find(".//VariationArchive"), encoding="unicode")


def _multi_record_response() -> str:
    return ("<ClinVarResult-Set>"
            + _archive_xml("clinvar_efetch_VCV2218.xml")
            + _archive_xml("clinvar_efetch_VCV4558169_conflicting.xml")
            + "</ClinVarResult-Set>")


def test_split_archives_yields_one_parsable_record_per_variation_id():
    pieces = list(split_archives(_multi_record_response()))
    assert len(pieces) == 2
    assert {vid for vid, _ in pieces} == {"2218", "4558169"}

    # each piece must survive the *unmodified* single-record parser -- the whole reason
    # the split re-wraps in a <ClinVarResult-Set> envelope rather than yielding bare
    # <VariationArchive> roots (ElementTree's `.//` never matches the root itself)
    for _, xml in pieces:
        vcv = ClinVarParser().parse_vcv(xml)
        assert vcv.accession.startswith("VCV")
        assert vcv.canonical_spdi.count(":") == 3


def test_split_archives_preserves_the_full_record_not_just_its_header():
    by_id = dict(split_archives(_multi_record_response()))
    parser = ClinVarParser()
    scvs = list(parser.parse_scvs(by_id["2218"]))
    assert len(scvs) == 22          # same count the single-record fixture test asserts
    assert len(list(parser.parse_rcvs(by_id["2218"]))) == 7


def test_split_esummary_produces_a_crosswalk_identical_to_the_single_uid_one():
    with open(os.path.join(FIXTURES, "clinvar_esummary_VCV2218.json")) as f:
        single = json.load(f)
    with open(os.path.join(FIXTURES, "clinvar_esummary_VCV4558169_conflicting.json")) as f:
        other = json.load(f)

    merged = {"result": {"uids": ["2218", "4558169"],
                          "2218": single["result"]["2218"],
                          "4558169": other["result"]["4558169"]}}
    per_uid = split_esummary(merged)
    assert set(per_uid) == {"2218", "4558169"}
    assert _build_trait_crosswalk(per_uid["2218"]) == _build_trait_crosswalk(single)


def test_a_record_with_no_simple_allele_quarantines_instead_of_crashing():
    # 81 of 1,134 real VHL records are haplotypes/genotypes/CNVs with no single canonical
    # SPDI. They must raise the record-level signal, never take the batch down with them.
    xml = ("<ClinVarResult-Set><VariationArchive Accession='VCV000000001' Version='1' "
           "VariationID='1'><ClassifiedRecord><Haplotype/></ClassifiedRecord>"
           "</VariationArchive></ClinVarResult-Set>")
    with pytest.raises(UnsupportedClinVarRecord):
        ClinVarParser().parse_vcv(xml)


def test_a_condition_with_no_db_mapping_still_produces_a_disease_node():
    # Real: ClinVar conditions like "See cases" / "VHL-related disorder" carry no DB or ID
    # attribute at all. Reading attrib["DB"] directly cost 41 of 1,134 VHL variants their
    # whole record -- including the correctly-mapped RCVs sitting next to the unmapped one.
    with open(os.path.join(FIXTURES, "clinvar_efetch_VCV2218.xml")) as f:
        xml = f.read()
    xml = xml.replace('<ClassifiedCondition DB="MedGen" ID="C0019562">',
                       '<ClassifiedCondition>', 1)
    with open(os.path.join(FIXTURES, "clinvar_esummary_VCV2218.json")) as f:
        summary = json.load(f)

    parser = ClinVarParser()
    vcv = parser.parse_vcv(xml)
    rcvs, scvs = list(parser.parse_rcvs(xml)), list(parser.parse_scvs(xml))
    spec = build_mapping_spec(vcv, rcvs, scvs, _build_trait_crosswalk(summary))
    results = apply_mapping_spec(vcv, spec, primary_node_id=None, source="ClinVar",
                                 source_record_id=vcv.accession, source_version=vcv.version,
                                 retrieved_at=NOW)

    diseases = [r for r in results if isinstance(r, ForgeNode) and r.subtype == "Disease"]
    assert len(diseases) == 7                     # nothing lost -- all 7 RCV conditions present
    unmapped = [d for d in diseases if d.attributes["identifier_mapped"] is False]
    assert len(unmapped) == 1
    assert unmapped[0].id.startswith("ClinVar:condition:")   # never faked into MedGen
    assert all(d.attributes["identifier_mapped"] for d in diseases if d is not unmapped[0])


def test_a_variant_submitted_against_two_genes_produces_an_in_gene_edge_for_each():
    # Real: VCV000265138 is NM_001018115.3(FANCD2):c.2605+1G>A and carries submitted Gene
    # elements for BOTH FANCD2 (HGNC:3585) and VHL (HGNC:12687) -- 3p25.3 neighbours it
    # overlaps. Taking `.//Gene[@Source='submitted']` (the first match) silently attributed
    # it to one gene and, in a VHL-scoped run, to the WRONG one.
    with open(os.path.join(FIXTURES, "clinvar_efetch_VCV2218.xml")) as f:
        xml = f.read()
    xml = xml.replace('<Gene Symbol="VHL"', '<Gene HGNC_ID="HGNC:3585" Symbol="FANCD2" '
                                             'Source="submitted"/><Gene Symbol="VHL"', 1)
    vcv = ClinVarParser().parse_vcv(xml)
    assert {hgnc for _, hgnc in vcv.submitted_genes} == {"HGNC:3585", "HGNC:12687"}

    with open(os.path.join(FIXTURES, "clinvar_esummary_VCV2218.json")) as f:
        summary = json.load(f)
    parser = ClinVarParser()
    spec = build_mapping_spec(vcv, list(parser.parse_rcvs(xml)), list(parser.parse_scvs(xml)),
                              _build_trait_crosswalk(summary))
    results = apply_mapping_spec(vcv, spec, primary_node_id=None, source="ClinVar",
                                 source_record_id=vcv.accession, source_version=vcv.version,
                                 retrieved_at=NOW)
    in_gene = [r for r in results if not isinstance(r, ForgeNode) and r.qualifier == "in_gene"]
    assert {e.dst for e in in_gene} == {"HGNC:3585", "HGNC:12687"}


def test_unmapped_disease_ids_are_stable_and_namespaced():
    assert _unmapped_disease_id("VHL-related disorder") == "ClinVar:condition:vhl-related-disorder"
    assert _unmapped_disease_id("See cases") == "ClinVar:condition:see-cases"
    assert _unmapped_disease_id(None).startswith("ClinVar:condition:")
