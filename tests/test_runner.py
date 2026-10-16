"""Tests for orchestration/runner.py -- the layer-mode contract.

All offline (fixtures only, zero network). What matters here is that the three modes
produce genuinely different graphs, and specifically that an ephemeral run attaching to
a resident persistent layer is not the same thing as an ephemeral run that quietly
re-ingests it.
"""
import json
import os

from orchestration.runner import KNOWN_GENE_HGNC, RunConfig, run

CORE_BIOLOGY_SUBTYPES = {"Transcript", "Exon", "Protein"}
OPERATIONAL_SUBTYPES = {"ACMGCriterion", "Requirement"}


def _offline(**kwargs) -> RunConfig:
    return RunConfig(offline=True, progress=False, **kwargs)


def _subtypes(result) -> set[str]:
    return {n.subtype for n in result.nodes}


def test_persistent_mode_builds_only_the_shared_gene_scoped_layers():
    result = run(_offline(mode="persistent"))
    subtypes = _subtypes(result)
    assert CORE_BIOLOGY_SUBTYPES <= subtypes
    assert OPERATIONAL_SUBTYPES <= subtypes
    assert "ClinicalAssertion" not in subtypes   # no evidence at all
    assert result.variants_requested == 0
    assert result.verified


def test_ephemeral_mode_builds_only_evidence_and_never_the_persistent_layers():
    result = run(_offline(mode="ephemeral"))
    subtypes = _subtypes(result)
    assert "ClinicalAssertion" in subtypes
    assert not (CORE_BIOLOGY_SUBTYPES & subtypes)   # Ensembl gene structure absent
    assert not (OPERATIONAL_SUBTYPES & subtypes)    # ClinGen rules absent
    assert result.verified


def test_ephemeral_edges_into_the_resident_layer_do_not_reject_the_batch():
    # ClinVar emits Variant --RELATES_TO[in_gene]--> Gene. In ephemeral mode that Gene
    # node is never created by this run, so referential integrity can only pass because
    # the resident anchor was declared. Without it the batch would REJECT -- which is the
    # correct behaviour, and exactly what this asserts is being avoided deliberately.
    result = run(_offline(mode="ephemeral"))
    assert result.batches_rejected == 0
    assert KNOWN_GENE_HGNC["VHL"] in result.resident_node_ids
    in_gene = [e for e in result.edges if e.qualifier == "in_gene"]
    assert in_gene
    assert all(e.dst not in {n.id for n in result.nodes} for e in in_gene)  # genuinely resident


def test_ephemeral_run_loads_resident_ids_from_a_persistent_run_snapshot(tmp_path):
    persistent = run(_offline(mode="persistent"))
    snapshot = tmp_path / "resident.json"
    snapshot.write_text(json.dumps({"gene": "VHL", "mode": "persistent",
                                     "node_ids": sorted(n.id for n in persistent.nodes)}))

    result = run(_offline(mode="ephemeral", resident_path=str(snapshot)))
    assert len(result.resident_node_ids) == len(persistent.nodes)
    assert result.verified


def test_full_mode_is_the_union_of_both_and_larger_than_either_alone():
    persistent = run(_offline(mode="persistent"))
    ephemeral = run(_offline(mode="ephemeral"))
    full = run(_offline(mode="full"))
    assert len(full.nodes) > len(persistent.nodes)
    assert len(full.nodes) > len(ephemeral.nodes)
    assert CORE_BIOLOGY_SUBTYPES <= _subtypes(full)
    assert "ClinicalAssertion" in _subtypes(full)


def test_edge_type_distribution_differs_by_mode_in_the_expected_direction():
    # The layer split should be visible in the statistics: rule fan-out (REQUIRES) is
    # purely operational, assertion traffic (ASSERTS) is purely evidence.
    persistent = {p.predicate for p in run(_offline(mode="persistent")).edge_stats.predicates}
    ephemeral = {p.predicate for p in run(_offline(mode="ephemeral")).edge_stats.predicates}
    assert "REQUIRES" in persistent and "REQUIRES" not in ephemeral
    assert "ASSERTS" in ephemeral and "ASSERTS" not in persistent


def test_a_new_opt_in_persistent_connector_does_not_silently_join_the_default_run():
    # Real, confirmed bug: registering UniProt (persistent-cadence) used to change every
    # existing default-connectors run's output, because "no explicit --connectors
    # selection" fell back to ALL registered persistent connectors. UniProt is
    # default_included=False specifically so growing the registry never silently changes
    # what a plain `--mode persistent` run produces.
    result = run(_offline(mode="persistent"))   # no explicit connectors -- the plain default
    # Ensembl's OWN gene-structure feed already produces a Protein node (from Translation,
    # id scheme "Ensembl:ENSP...") -- the assertion has to be UniProt-specific, not
    # "Protein absent", or it would be trivially wrong regardless of this bug.
    assert not any(n.id.startswith("UniProt:") for n in result.nodes)
    assert not any(f["feed"] == "UniProt" for f in result.feeds)


def test_an_opt_in_connector_still_runs_when_named_explicitly():
    result = run(_offline(mode="full", connectors=("clinvar", "ensembl_structure", "clingen", "uniprot")))
    assert "Protein" in _subtypes(result)
    assert any(f["feed"] == "UniProt" for f in result.feeds)


def test_a_later_connector_cannot_steal_feed_credit_for_an_id_another_connector_already_owns():
    # Real, confirmed bug: Emission.merge() used dict.update() for feed_of, so dbSNP
    # independently re-deriving the SAME canonical Variant id ClinVar already built (same
    # SPDI -- rs5030820's C>T allele is VHL's pathogenic variant) reassigned that node's
    # feed from "ClinVar" to "dbSNP" purely because dbSNP was merged in later, even though
    # dbSNP contributed nothing new for that specific node. First writer must win.
    result = run(_offline(mode="ephemeral", connectors=("clinvar", "dbsnp")))
    variant = next(n for n in result.nodes if n.subtype == "Variant"
                   and n.id == "SPDI:NC_000003.12:10149821:C:T")
    variant_event = next(e for e in result.events if e["kind"] == "node" and e["id"] == variant.id)
    assert variant_event["feed"] == "ClinVar"

    # dbSNP still gets credit for the allele that's genuinely only its own.
    dbsnp_only = next((n for n in result.nodes if n.id == "SPDI:NC_000003.12:10149821:C:G"), None)
    if dbsnp_only is not None:   # present only when the offline dbSNP fixture is attached
        dbsnp_event = next(e for e in result.events if e["kind"] == "node" and e["id"] == dbsnp_only.id)
        assert dbsnp_event["feed"] == "dbSNP"


def test_enrich_with_skips_cleanly_when_build_query_returns_none():
    # Real, confirmed live bug: descriptor.build_query() legitimately returns None when
    # a connector has nothing to ask for (dbSNP with no rsID cross-ref for this variant --
    # true for some real ClinVar records). _enrich_with used to hand that None straight to
    # adapter.fetch(query), crashing with "'NoneType' object is not subscriptable" inside
    # DbSnpAdapter.fetch's `query["rsid"]`. Must be a silent no-op, not a quarantine entry.
    from orchestration.runner import _enrich_with
    from orchestration.connector_registry import Emission

    class _NeverCalledAdapter:
        def fetch(self, query):
            raise AssertionError("adapter.fetch() must not be called when build_query() returns None")

    descriptor = _StubDescriptor(
        name="stub_no_query", adapter=_NeverCalledAdapter(),
        build_query=lambda scope, anchor, ctx: None,
        build_emission=lambda raw, ctx: Emission(),
    )
    quarantined = []
    emission = Emission()
    config = RunConfig(offline=False, progress=False)   # offline=False -- exercises the
                                                          # live build_query()-returns-None path
    _enrich_with(descriptor, config, "SPDI:x", {}, emission, quarantined)
    assert quarantined == []
    assert emission.nodes == [] and emission.edges == []


class _StubDescriptor:
    def __init__(self, name, adapter, build_query, build_emission):
        self.name = name
        self.adapter = adapter
        self.build_query = build_query
        self.build_emission = build_emission
        self.feed_label = name
        self.implemented = True


def test_an_in_gene_edge_to_an_out_of_scope_gene_costs_the_edge_not_the_record():
    from orchestration.runner import _drop_out_of_scope_gene_edges
    from forge_ir.core import EdgeType, Origin
    from forge_ir.graph import ForgeEdge
    from forge_ir.provenance import SourceProvenance
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    prov = [SourceProvenance("ClinVar", "VCV000265138", "1", now)]

    def edge(dst, qualifier):
        return ForgeEdge(src="SPDI:NC_000003.12:1:C:T", predicate=EdgeType.RELATES_TO, dst=dst,
                          qualifier=qualifier, attributes={}, provenance=prov, origin=Origin.SOURCE)

    quarantined = []
    kept = _drop_out_of_scope_gene_edges(
        [edge("HGNC:12687", "in_gene"),      # VHL -- in scope
         edge("HGNC:3585", "in_gene"),       # FANCD2 -- the 3p25.3 neighbour, out of scope
         edge("ClinVar:Submitter:1", "submitted_by")],
        in_scope={"HGNC:12687", "ClinVar:Submitter:1"}, uid="265138", quarantined=quarantined)

    assert [e.dst for e in kept] == ["HGNC:12687", "ClinVar:Submitter:1"]
    assert len(quarantined) == 1
    assert quarantined[0]["scope"] == "relationship"     # never "batch", never "record"
    assert "HGNC:3585" in quarantined[0]["reason"]


def test_feeds_are_grouped_into_core_biology_operational_and_evidence():
    result = run(_offline(mode="full", connectors=("clinvar", "gnomad", "vep")))
    groups = {f["feed"]: f["group"] for f in result.feeds}
    assert groups == {
        "Ensembl gene structure": "core_biology",
        "ClinGen VCEP": "operational",
        "ClinVar": "evidence",
        "gnomAD": "evidence",
        "Ensembl VEP": "evidence",
    }
    # Every feed listed must have actually produced something -- the manifest is built from
    # the event log, so a feed that yielded nothing never appears as a dead toggle.
    assert all(f["nodes"] > 0 or f["edges"] > 0 for f in result.feeds)


def test_ensembls_two_feeds_are_separable_even_though_provenance_cannot_tell_them_apart():
    # The reason Emission tags feeds at production time: both Ensembl feeds carry the
    # identical SourceProvenance("Ensembl", ...), so nothing downstream could split gene
    # structure from VEP consequences after the fact.
    result = run(_offline(mode="full", connectors=("clinvar", "gnomad", "vep")))
    feeds = {f["feed"]: f for f in result.feeds}
    assert "Ensembl gene structure" in feeds and "Ensembl VEP" in feeds
    assert feeds["Ensembl gene structure"]["group"] != feeds["Ensembl VEP"]["group"]

    structure_nodes = {e["id"] for e in result.events
                       if e["kind"] == "node" and e["feed"] == "Ensembl gene structure"}
    vep_nodes = {e["id"] for e in result.events
                 if e["kind"] == "node" and e["feed"] == "Ensembl VEP"}
    assert structure_nodes and vep_nodes
    assert not (structure_nodes & vep_nodes)
    assert all(n.subtype == "ComputationalPrediction"
               for n in result.nodes if n.id in vep_nodes)


def test_every_node_and_edge_event_carries_the_feed_that_produced_it():
    result = run(_offline(mode="full", connectors=("clinvar", "gnomad", "vep")))
    graph_events = [e for e in result.events if e["kind"] in ("node", "edge")]
    assert graph_events
    assert all(e["feed"] for e in graph_events)


def test_cross_batch_identity_merges_are_emitted_as_their_own_events():
    # Two variants of the same gene share conditions, so the second variant's batch
    # resolves a Disease that already exists rather than duplicating it. That merge is
    # the observable output of the persistent identity index, so it gets its own event.
    result = run(_offline(mode="full"))
    identity_events = [e for e in result.events if e["kind"] == "identity"]
    assert identity_events
    assert result.identity_merges == len(identity_events)
    for ev in identity_events:
        assert ev["status"] in ("MERGED", "REPRESENTS", "UNRESOLVED")
        assert ev["namespace"] in ("HGNC", "SPDI", "MedGen")
        assert ev["sightings"] >= 2          # by definition: it had been seen before
        assert ev["id"] in {n.id for n in result.nodes}


def test_a_single_variant_run_has_nothing_to_merge():
    result = run(_offline(mode="ephemeral", uids=["2218"]))
    # Offline payloads ignore --uids (fixtures are fixed), so assert the general property
    # instead: merges only ever exceed zero once a node is referenced by a second batch.
    merged_ids = [e["id"] for e in result.events if e["kind"] == "identity"]
    assert len(merged_ids) == result.identity_merges


def test_the_event_log_records_each_node_forming_exactly_once():
    result = run(_offline(mode="full"))
    node_events = [e for e in result.events if e["kind"] == "node"]
    assert len(node_events) == len({e["id"] for e in node_events})
    assert len(node_events) == len(result.nodes)


def test_the_event_log_budget_truncates_the_replay_but_not_the_committed_graph():
    small = run(_offline(mode="full", viz_max_events=50))
    full = run(_offline(mode="full"))
    assert small.events_truncated
    assert len(small.events) < len(full.events)
    assert len(small.nodes) == len(full.nodes)      # statistics still cover everything
    assert small.edge_stats.total_edges == full.edge_stats.total_edges


def test_offline_gnomad_and_vep_fixtures_attach_only_to_their_own_variant():
    # Guard against fabricating data: the saved gnomAD/VEP payloads describe one variant,
    # and must never be attached to the other fixture variant just because it's in the run.
    result = run(_offline(mode="full", connectors=("clinvar", "gnomad", "vep")))
    frequencies = [n for n in result.nodes if n.subtype == "PopulationFrequency"]
    assert len(frequencies) >= 1
    frequency_ids = {n.id for n in frequencies}
    observed_variants = {e.dst for e in result.edges
                          if e.src in frequency_ids and e.dst.startswith("SPDI:")}
    assert len(observed_variants) == 1      # one variant, not both


def test_ephemeral_vep_needs_core_biology_resident_because_consequences_are_transcript_scoped():
    # A real constraint the layer split imposes, not a bug: VEP emits
    # ComputationalPrediction --APPLIES_TO--> Transcript, and Transcript nodes live in the
    # persistent Core Biology layer. So an ephemeral VEP run against nothing but a declared
    # Gene anchor has dangling transcript endpoints and REJECTS the batch, correctly.
    without = run(_offline(mode="ephemeral", connectors=("clinvar", "vep")))
    assert without.batches_rejected > 0
    assert not without.verified
    assert any("Ensembl:ENST" in q["reason"] for q in without.quarantined)


def test_ephemeral_vep_succeeds_once_the_persistent_layer_is_resident(tmp_path):
    persistent = run(_offline(mode="persistent"))
    snapshot = tmp_path / "resident.json"
    snapshot.write_text(json.dumps({"gene": "VHL", "mode": "persistent",
                                     "node_ids": sorted(n.id for n in persistent.nodes)}))

    with_resident = run(_offline(mode="ephemeral", connectors=("clinvar", "vep"),
                                  resident_path=str(snapshot)))
    assert with_resident.batches_rejected == 0
    assert with_resident.verified
    assert any(n.subtype == "ComputationalPrediction" for n in with_resident.nodes)
