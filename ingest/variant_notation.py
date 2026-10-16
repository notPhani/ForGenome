"""Variant notation normalization -- the shared place every connector converts its own
native coordinate system to canonical SPDI (forge_ir/core.py::variant_canonical_id).

Real gap this closes: the SPDI/1-based fix for gnomAD lived in forge_ir/core.py as a
single narrow function (`normalize_spdi_position_from_1based`), and the VCF-style
region math for Ensembl's VEP endpoint was duplicated inline in
orchestration/runner.py's `_chromosome_from_refseq` + ad hoc arithmetic in
`_add_vep`/`_add_gnomad`. Neither was reusable by a new connector supplying VCF-style
or HGVS-genomic coordinates (dbSNP, COSMIC, LOVD, DECIPHER all plausibly do). This
module is that reusable place -- connectors/gnomad.py and connectors/ensembl.py's
`build_query(scope="coordinates", ...)` now call into it instead of doing their own math.

Scoped deliberately narrow: `hgvs_genomic_to_spdi` handles the one HGVS shape actually
seen in real payloads (`NC_000003.12:g.10149822C>T`, a simple substitution) -- this is
not a general HGVS parser, and doesn't try to be one. A real, unsupported HGVS
expression (indel/duplication/range notation) raises rather than guessing.
"""
from __future__ import annotations

import re

from forge_ir.core import normalize_spdi_position_from_1based, variant_canonical_id

# GRCh38 primary-assembly chromosome -> RefSeq accession. Standard, stable NCBI
# GRCh38.p14 accessions (unchanged across GRCh38 patch releases for the primary
# chromosomes) -- real values, not placeholders. MT is the standard human
# mitochondrial rCRS accession used across every GRCh38-based annotation source.
GRCH38_CHROM_TO_REFSEQ: dict[str, str] = {
    "1": "NC_000001.11", "2": "NC_000002.12", "3": "NC_000003.12", "4": "NC_000004.12",
    "5": "NC_000005.10", "6": "NC_000006.12", "7": "NC_000007.14", "8": "NC_000008.11",
    "9": "NC_000009.12", "10": "NC_000010.11", "11": "NC_000011.10", "12": "NC_000012.12",
    "13": "NC_000013.11", "14": "NC_000014.9", "15": "NC_000015.10", "16": "NC_000016.10",
    "17": "NC_000017.11", "18": "NC_000018.10", "19": "NC_000019.10", "20": "NC_000020.11",
    "21": "NC_000021.9", "22": "NC_000022.11", "X": "NC_000023.11", "Y": "NC_000024.10",
    "MT": "NC_012920.1",
}
GRCH38_REFSEQ_TO_CHROM: dict[str, str] = {v.split(".")[0]: k for k, v in GRCH38_CHROM_TO_REFSEQ.items()}

_HGVS_GENOMIC_SUBSTITUTION_RE = re.compile(
    r"^(?P<accession>NC_\d+\.\d+):g\.(?P<pos>\d+)(?P<ref>[ACGT])>(?P<alt>[ACGT])$"
)


def chromosome_to_refseq(chrom: str, assembly: str = "GRCh38") -> str:
    """"3" -> "NC_000003.12". The one place a plain chromosome name becomes a RefSeq
    accession -- the reverse of `refseq_to_chromosome` below."""
    if assembly != "GRCh38":
        raise ValueError(f"only GRCh38 is supported (got assembly={assembly!r})")
    chrom = chrom.upper().removeprefix("CHR")
    if chrom not in GRCH38_CHROM_TO_REFSEQ:
        raise ValueError(f"unknown GRCh38 chromosome {chrom!r}")
    return GRCH38_CHROM_TO_REFSEQ[chrom]


def refseq_to_chromosome(seq_accession: str) -> str:
    """"NC_000003.12" -> "3". Ensembl's region endpoint and VCF-style notation both
    speak chromosome names; ClinVar's CanonicalSPDI speaks RefSeq accessions."""
    core = seq_accession.split(".")[0]
    if core not in GRCH38_REFSEQ_TO_CHROM:
        raise ValueError(f"unknown RefSeq accession {seq_accession!r}")
    return GRCH38_REFSEQ_TO_CHROM[core]


def vcf_to_spdi(chrom: str, pos_1based: int, ref: str, alt: str, assembly: str = "GRCh38") -> str:
    """VCF-style (chrom, 1-based pos, ref, alt) -> canonical SPDI. Real, confirmed
    off-by-one (forge_ir/core.py's own docstring): VCF/gnomAD are 1-based, SPDI is
    0-based -- centralized in `normalize_spdi_position_from_1based`, called from here
    rather than re-derived per caller."""
    seq_accession = chromosome_to_refseq(chrom, assembly)
    spdi_position = normalize_spdi_position_from_1based(pos_1based)
    return variant_canonical_id(seq_accession, spdi_position, ref, alt)


def hgvs_genomic_to_spdi(hgvs: str) -> str:
    """`NC_000003.12:g.10149822C>T` -> canonical SPDI. Only the simple-substitution
    genomic form -- the one shape confirmed present in real payloads
    (connector-research.md §2.1). Raises on anything else (indel/dup/range notation)
    rather than silently producing a wrong SPDI from a pattern that doesn't apply."""
    match = _HGVS_GENOMIC_SUBSTITUTION_RE.match(hgvs.strip())
    if not match:
        raise ValueError(f"unsupported HGVS expression (only simple genomic substitutions "
                         f"are handled): {hgvs!r}")
    spdi_position = int(match.group("pos")) - 1   # HGVS is 1-based, SPDI is 0-based
    return variant_canonical_id(match.group("accession"), spdi_position,
                                 match.group("ref"), match.group("alt"))


def normalize_variant_notation(raw: str | tuple, kind: str, assembly: str = "GRCh38") -> str:
    """Dispatcher: `kind` is "vcf" (raw is a (chrom, pos, ref, alt) tuple), "hgvs_genomic"
    (raw is the HGVS string), or "spdi" (raw is already a canonical SPDI string, returned
    unchanged -- lets a caller route through one function regardless of what notation a
    given connector happens to hand it)."""
    if kind == "spdi":
        return raw
    if kind == "vcf":
        chrom, pos, ref, alt = raw
        return vcf_to_spdi(chrom, pos, ref, alt, assembly)
    if kind == "hgvs_genomic":
        return hgvs_genomic_to_spdi(raw)
    raise ValueError(f"unknown variant notation kind: {kind!r}")
