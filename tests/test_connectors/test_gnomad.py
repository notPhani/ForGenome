"""Tests against the REAL gnomAD fixture -- including the faf95/GroupMax Filtering AF
field whose null value (singleton, ac=1) turned out to matter for PM2's real evaluation."""
import json
import os

from forge_ir.core import EdgeType
from forge_ir.graph import ForgeEdge, ForgeNode
from connectors.gnomad import GnomadParser, frequency_to_ir

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")


def _load():
    with open(os.path.join(FIXTURES, "gnomad_variant_3-10149822-C-T.json")) as f:
        return json.load(f)


def test_parses_real_genome_and_exome_frequencies():
    freqs = list(GnomadParser().parse(_load()))
    scopes = {f.dataset_scope for f in freqs}
    assert scopes == {"genome", "exome"}

    genome = next(f for f in freqs if f.dataset_scope == "genome")
    assert genome.ac == 1
    assert genome.an == 152140
    assert abs(genome.af - 6.572893387669252e-06) < 1e-12
    assert genome.faf95_popmax is None  # the real, confirmed null -- singleton variant


def test_population_breakdown_stays_compact_not_exploded_into_nodes():
    genome = next(f for f in GnomadParser().parse(_load()) if f.dataset_scope == "genome")
    assert len(genome.populations) == 32  # real count, confirmed

    results = frequency_to_ir(genome, "SPDI:NC_000003.12:10149821:C:T")
    nodes = [r for r in results if isinstance(r, ForgeNode)]
    assert len(nodes) == 1  # ONE PopulationFrequency node, not 32 -- exactly the graph-explosion
                             # case the LOD system is designed to prevent
    assert len(nodes[0].attributes["populations"]) == 32  # data kept, just as an attribute


def test_observes_edge_targets_the_variant():
    genome = next(f for f in GnomadParser().parse(_load()) if f.dataset_scope == "genome")
    results = frequency_to_ir(genome, "SPDI:NC_000003.12:10149821:C:T")
    edges = [r for r in results if isinstance(r, ForgeEdge)]
    assert len(edges) == 1
    assert edges[0].predicate == EdgeType.OBSERVES
    assert edges[0].dst == "SPDI:NC_000003.12:10149821:C:T"


def test_source_version_is_null_gnomad_does_not_self_report_it():
    genome = next(f for f in GnomadParser().parse(_load()) if f.dataset_scope == "genome")
    assert genome.source_version is None  # confirmed real -- SourceManifest is the fallback


def test_a_variant_absent_from_gnomad_yields_nothing_not_a_crash():
    # Real, confirmed live: gnomAD returns {"data": {"variant": null}} for any variant it
    # has no record for -- true for most rare/novel ClinVar submissions, not malformed.
    # Regression: `v.get(scope)` on the None used to raise AttributeError, which the
    # runner quarantined as a parse failure -- mislabeling a legitimate negative result.
    absent = {"data": {"variant": None}}
    assert list(GnomadParser().parse(absent)) == []
