"""Population/ancestry-group normalization -- the second normalization gap: gnomAD's
`populations` breakdown (connectors/gnomad.py) kept gnomAD's own internal codes
(`afr`, `nfe`, `afr_XX`, ...) completely verbatim. That's fine as long as gnomAD is the
only frequency source, but a second one (TOPMed, 1000 Genomes, a future connector) would
use a different vocabulary with nothing here to compare the two against.

`POPULATION_TAXONOMY` below is gnomAD v4's own real, documented ancestry-group set
(confirmed against the real gnomAD fixture's full 32-row population list: 10 ancestry
groups x {overall, XX, XY} = 30, plus 2 sex-only rows with no ancestry breakdown = 32) --
used as the canonical vocabulary since gnomAD is the only real frequency source in the
stack today. Adopting a source's own real vocabulary as the starting canonical set,
rather than inventing a new one, is deliberate: it's the set every other allele-frequency
source's population codes will eventually need to be *mapped onto*, not replaced by.
"""
from __future__ import annotations

from dataclasses import dataclass

# gnomAD v4's real top-level ancestry groups (connector-research.md §2.3 / the real
# fixture) -- gnomad.broadinstitute.org's own labels, not invented ones.
POPULATION_TAXONOMY: dict[str, str] = {
    "afr": "African/African American",
    "ami": "Amish",
    "amr": "Admixed American",
    "asj": "Ashkenazi Jewish",
    "eas": "East Asian",
    "fin": "Finnish",
    "mid": "Middle Eastern",
    "nfe": "Non-Finnish European",
    "remaining": "Remaining individuals",
    "sas": "South Asian",
}

_SEX_SUFFIXES = ("XX", "XY")


@dataclass(frozen=True)
class NormalizedPopulation:
    raw_code: str                  # gnomAD's own code, kept verbatim -- never discarded
    population: str | None         # canonical ancestry-group name, or None for sex-only rows
    population_code: str | None    # the canonical code POPULATION_TAXONOMY is keyed by
    sex: str | None                 # "XX" | "XY" | None (the row covers both sexes)
    mapped: bool                    # False if raw_code didn't match any known taxonomy entry --
                                     # surfaced rather than silently dropped or mis-bucketed


def normalize_population_code(raw_code: str) -> NormalizedPopulation:
    """"afr_XX" -> ancestry=African/African American, sex=XX. "XX" -> ancestry=None
    (sex-only row, no ancestry breakdown). An unrecognized code (a future gnomAD release
    adding a group, or a different source's vocabulary entirely) is reported with
    `mapped=False` rather than guessed at or dropped -- real, confirmed case:
    gnomAD v4 added `ami`/`mid` groups that didn't exist in v2, and a hardcoded v2-era
    list would have silently mis-handled them."""
    code = raw_code.strip()

    if code in _SEX_SUFFIXES:
        return NormalizedPopulation(raw_code=raw_code, population=None, population_code=None,
                                     sex=code, mapped=True)

    sex = None
    base = code
    for suffix in _SEX_SUFFIXES:
        if code.endswith(f"_{suffix}"):
            sex = suffix
            base = code[: -(len(suffix) + 1)]
            break

    if base in POPULATION_TAXONOMY:
        return NormalizedPopulation(raw_code=raw_code, population=POPULATION_TAXONOMY[base],
                                     population_code=base, sex=sex, mapped=True)
    return NormalizedPopulation(raw_code=raw_code, population=None, population_code=None,
                                 sex=sex, mapped=False)


def normalize_populations(raw_populations: list[dict]) -> list[dict]:
    """Applied to gnomAD's raw `populations` list when building a PopulationFrequency
    node's attributes -- each `{id, ac, an}` row becomes `{raw_code, population,
    population_code, sex, mapped, ac, an}`. Every row is kept (mapped or not); nothing
    here filters, it only annotates."""
    out = []
    for row in raw_populations:
        normalized = normalize_population_code(row["id"])
        out.append({
            "raw_code": normalized.raw_code, "population": normalized.population,
            "population_code": normalized.population_code, "sex": normalized.sex,
            "mapped": normalized.mapped, "ac": row.get("ac"), "an": row.get("an"),
        })
    return out
