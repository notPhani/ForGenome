"""Tests for ingest/population.py against the real gnomAD fixture's full 32-row
population breakdown -- the "population normalization" ask. Confirms every one of the
real rows is accounted for (nothing silently dropped) and the sex-only rows (bare "XX"/
"XY", no ancestry breakdown) are handled distinctly from ancestry+sex rows ("afr_XX").
"""
import json
import os

from ingest.population import POPULATION_TAXONOMY, normalize_population_code, normalize_populations

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def _real_populations() -> list[dict]:
    with open(os.path.join(FIXTURES, "gnomad_variant_3-10149822-C-T.json")) as f:
        data = json.load(f)
    return data["data"]["variant"]["genome"]["populations"]


def test_ancestry_and_sex_are_split_apart():
    result = normalize_population_code("afr_XX")
    assert result.population == "African/African American"
    assert result.population_code == "afr"
    assert result.sex == "XX"
    assert result.mapped


def test_bare_ancestry_code_has_no_sex():
    result = normalize_population_code("nfe")
    assert result.population == "Non-Finnish European"
    assert result.sex is None
    assert result.mapped


def test_sex_only_row_has_no_ancestry():
    result = normalize_population_code("XY")
    assert result.population is None
    assert result.population_code is None
    assert result.sex == "XY"
    assert result.mapped   # still a legitimate, known row -- not an unmapped code


def test_unrecognized_code_is_reported_not_silently_dropped():
    # Real scenario this guards against: gnomAD v4 added "ami"/"mid" that didn't exist
    # in v2 -- an unrecognized future code must be visible (mapped=False), not silently
    # bucketed into the wrong group or thrown away.
    result = normalize_population_code("totally_unknown_code")
    assert result.mapped is False
    assert result.raw_code == "totally_unknown_code"


def test_every_real_fixture_row_normalizes_without_loss():
    raw = _real_populations()
    normalized = normalize_populations(raw)
    assert len(normalized) == len(raw) == 32   # confirmed real count -- nothing added or dropped

    unmapped = [row for row in normalized if not row["mapped"]]
    assert unmapped == []   # every real gnomAD v4 code in this fixture is in our taxonomy

    # ac/an values are preserved exactly, not just the codes
    raw_by_id = {r["id"]: r for r in raw}
    for row in normalized:
        assert row["ac"] == raw_by_id[row["raw_code"]]["ac"]
        assert row["an"] == raw_by_id[row["raw_code"]]["an"]


def test_every_taxonomy_entry_actually_appears_in_the_real_fixture():
    # The reverse check: confirms POPULATION_TAXONOMY isn't over-broad (entries that
    # don't correspond to anything gnomAD v4 actually returns).
    raw_bases = {row["id"].removesuffix("_XX").removesuffix("_XY") for row in _real_populations()}
    for code in POPULATION_TAXONOMY:
        assert code in raw_bases, f"{code!r} in POPULATION_TAXONOMY but never seen in the real fixture"


def test_normalize_populations_is_wired_into_the_real_connector():
    from connectors.gnomad import GnomadParser, frequency_to_ir
    with open(os.path.join(FIXTURES, "gnomad_variant_3-10149822-C-T.json")) as f:
        raw = json.load(f)
    genome = next(f for f in GnomadParser().parse(raw) if f.dataset_scope == "genome")
    results = frequency_to_ir(genome, "SPDI:NC_000003.12:10149821:C:T")
    node = next(r for r in results if hasattr(r, "attributes"))
    populations = node.attributes["populations"]
    assert all("population" in row and "raw_code" in row for row in populations)
    assert any(row["population"] == "African/African American" for row in populations)
