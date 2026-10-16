"""Tests for ingest/variant_notation.py -- the shared VCF/HGVS -> canonical SPDI
normalizer (the "variant notation normalization" ask). Checked against the VHL test
case's known real coordinates: SPDI NC_000003.12:10149821:C:T <-> VCF-style
3-10149822-C-T <-> HGVS NC_000003.12:g.10149822C>T (the +1/-1 offset between SPDI's
0-based and VCF/HGVS's 1-based conventions is the whole point of this module existing).
"""
import pytest

from ingest.variant_notation import (
    GRCH38_CHROM_TO_REFSEQ, chromosome_to_refseq, hgvs_genomic_to_spdi, normalize_variant_notation,
    refseq_to_chromosome, vcf_to_spdi,
)

REAL_SPDI = "SPDI:NC_000003.12:10149821:C:T"


def test_chromosome_to_refseq_known_real_accession():
    assert chromosome_to_refseq("3") == "NC_000003.12"
    assert chromosome_to_refseq("chr3") == "NC_000003.12"   # "chr" prefix tolerated
    assert chromosome_to_refseq("X") == "NC_000023.11"
    assert chromosome_to_refseq("MT") == "NC_012920.1"


def test_chromosome_to_refseq_rejects_unknown_chromosome():
    with pytest.raises(ValueError, match="unknown GRCh38 chromosome"):
        chromosome_to_refseq("99")


def test_chromosome_to_refseq_rejects_non_grch38_assembly():
    with pytest.raises(ValueError, match="only GRCh38"):
        chromosome_to_refseq("3", assembly="GRCh37")


def test_refseq_to_chromosome_is_the_exact_inverse():
    for chrom, accession in GRCH38_CHROM_TO_REFSEQ.items():
        assert refseq_to_chromosome(accession) == chrom


def test_refseq_to_chromosome_rejects_unknown_accession():
    with pytest.raises(ValueError, match="unknown RefSeq accession"):
        refseq_to_chromosome("NC_999999.1")


def test_vcf_to_spdi_matches_the_real_vhl_test_case():
    # Real, confirmed coordinates from this project's own VHL fixtures:
    # gnomAD's 1-based variant_id "3-10149822-C-T" is the SAME variant as ClinVar/
    # Ensembl's 0-based SPDI "NC_000003.12:10149821:C:T".
    assert vcf_to_spdi("3", 10149822, "C", "T") == REAL_SPDI


def test_hgvs_genomic_to_spdi_matches_the_real_vhl_test_case():
    assert hgvs_genomic_to_spdi("NC_000003.12:g.10149822C>T") == REAL_SPDI


def test_hgvs_genomic_to_spdi_rejects_unsupported_notation():
    # Real limitation, stated up front: this is a substitution-only parser, not a
    # general HGVS engine. An indel/duplication must raise, never guess.
    with pytest.raises(ValueError, match="unsupported HGVS expression"):
        hgvs_genomic_to_spdi("NC_000003.12:g.10149822_10149823del")


def test_normalize_variant_notation_dispatches_all_three_kinds():
    assert normalize_variant_notation(("3", 10149822, "C", "T"), kind="vcf") == REAL_SPDI
    assert normalize_variant_notation("NC_000003.12:g.10149822C>T", kind="hgvs_genomic") == REAL_SPDI
    assert normalize_variant_notation(REAL_SPDI, kind="spdi") == REAL_SPDI


def test_normalize_variant_notation_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown variant notation kind"):
        normalize_variant_notation("x", kind="bogus")
