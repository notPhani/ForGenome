"""Tests against the REAL dbSNP fixture (fixtures/dbsnp_refsnp_5030820.json, live-fetched
from https://api.ncbi.nlm.nih.gov/variation/v0/refsnp/5030820 -- the VHL test case's real
rsID, cross-referenced out of ClinVar's own VCV XML). Real, confirmed: rs5030820 has
exactly 3 alleles at its primary GRCh38 placement -- the reference "no-change" allele plus
2 real alternates (C>G, C>T), only one of which (C>T) is the pathogenic VHL variant this
project's other fixtures discuss.
"""
import json
import os

from forge_ir.core import NodeRole
from forge_ir.graph import ForgeNode
from connectors.dbsnp import DbSnpParser, allele_to_ir

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")


def _load():
    with open(os.path.join(FIXTURES, "dbsnp_refsnp_5030820.json")) as f:
        return json.load(f)


def test_parses_all_three_real_alleles_including_the_reference_one():
    alleles = list(DbSnpParser().parse(_load()))
    assert len(alleles) == 3
    assert sum(1 for a in alleles if a.is_reference_allele) == 1
    assert sum(1 for a in alleles if not a.is_reference_allele) == 2


def test_real_alternate_alleles_match_the_known_vhl_coordinates():
    alleles = list(DbSnpParser().parse(_load()))
    alts = {(a.ref, a.alt) for a in alleles if not a.is_reference_allele}
    assert alts == {("C", "G"), ("C", "T")}
    for a in alleles:
        assert a.seq_accession == "NC_000003.12"
        assert a.spdi_position == 10149821   # real, confirmed 0-based SPDI position
        assert a.rsid == "5030820"


def test_reference_allele_produces_no_variant_node():
    alleles = list(DbSnpParser().parse(_load()))
    reference = next(a for a in alleles if a.is_reference_allele)
    assert allele_to_ir(reference) == []


def test_real_alternate_allele_produces_a_canonical_variant_entity():
    alleles = list(DbSnpParser().parse(_load()))
    c_to_t = next(a for a in alleles if not a.is_reference_allele and a.alt == "T")
    results = allele_to_ir(c_to_t)
    assert len(results) == 1
    node = results[0]
    assert isinstance(node, ForgeNode)
    assert node.role == NodeRole.ENTITY
    assert node.subtype == "Variant"
    # Same canonical id scheme as ClinVar's own Variant node for this exact allele --
    # this is what lets the two dedupe into ONE node rather than two, when both are
    # committed in the same batch.
    assert node.id == "SPDI:NC_000003.12:10149821:C:T"


def test_the_c_to_g_allele_is_genuinely_new_information_not_in_clinvar_or_gnomad():
    # The real value dbSNP adds beyond just "another feed for the same variant": VHL's
    # ClinVar/gnomAD fixtures only ever discuss the C>T allele. dbSNP alone reveals that
    # C>G is ALSO a cataloged variant at the exact same locus.
    alleles = list(DbSnpParser().parse(_load()))
    c_to_g = next(a for a in alleles if not a.is_reference_allele and a.alt == "G")
    node = allele_to_ir(c_to_g)[0]
    assert node.id == "SPDI:NC_000003.12:10149821:C:G"
    assert node.id != "SPDI:NC_000003.12:10149821:C:T"
