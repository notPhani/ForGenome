"""The run engine: turns a RunConfig into a committed graph plus a replayable event log.

Three modes, and the split between them is the whole point rather than a convenience:

  persistent  Core Biology (Ensembl gene structure) + Operational (ClinGen VCEP rules).
              Gene-scoped, changes on the order of months, shared by every variant of
              the gene. This is the layer worth a slow overnight bulk load.

  ephemeral   Evidence only (ClinVar assertions, optionally gnomAD frequencies and VEP
              consequences). Per-variant, refetched per investigation. It does NOT
              re-derive the persistent layer -- it *declares* the persistent node ids as
              already resident and hands them to verify() as `known_node_ids`, so an
              edge pointing at a Gene node this run never created still passes
              referential integrity instead of rejecting the batch.

  full        Both, in one process. What the two-variant presentation demo does.

Ephemeral runs commit in micro-batches (one per fetch batch), not one giant atomic
commit: a single bad archive should quarantine one record, and a genuinely broken batch
should reject ~50 variants, not 1,100. GraphStore below is what accumulates the
committed micro-batches into the one deduped graph the statistics run over.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connectors  # noqa: F401 -- registers every connector as an import side effect
from analysis.edge_stats import EdgeStatsReport, compute_edge_stats
from connectors.clinvar import ClinVarAdapter, UnsupportedClinVarRecord
from orchestration.connector_planner import ConnectorPlanner, Policy
from orchestration.connector_registry import (
    Emission, get_connector, group_for_feed_label, list_connectors, tag_with_source_manifest,
)
from orchestration.load_plan import execute, plan_load
from orchestration.scope_resolver import ScopeResolver
from forge_ir.core import gene_canonical_id
from forge_ir.graph import ForgeEdge, ForgeNode
from ingest.compiler import CoverageReport, StagingCompiler
from ingest.hashing import compute_structural_hash
from ingest.identity import IdentityIndex
from ingest.variant_notation import refseq_to_chromosome

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(BASE, "fixtures")

# Genes we can run offline (fixtures on disk) and whose HGNC id we can declare as a
# resident anchor without a lookup round-trip. Live runs read the HGNC id out of the
# ClinVar payload itself, so this is a convenience table, not a gate.
KNOWN_GENE_HGNC = {"VHL": "HGNC:12687"}

# The saved gnomAD and VEP fixtures are for this variant and only this variant.
OFFLINE_VARIANT_WITH_GNOMAD_VEP = "VCV000002218"

# Backward-compatible short names for --connectors, resolved to the connector registry's
# real names -- "vep" reads better on the CLI than "ensembl_vep", and this is the one
# place that mapping lives, not scattered across main.py/runner.py.
CONNECTOR_ALIASES = {"vep": "ensembl_vep"}

# Fixture files each connector's offline mode reads instead of a live fetch, and the
# gnomAD/VEP raw-response shape each descriptor's build_query expects when driving a
# per-variant coordinate-based fetch (kept here, not in each connector module, because
# fixture filenames are a demo-data/testing concern, not a connector's own business).
OFFLINE_FIXTURES = {
    "ensembl_structure": "ensembl_gene_VHL.json",
    "clingen": "clingen_vhl_vcep_GN078_v1.1.json",
    "gnomad": "gnomad_variant_3-10149822-C-T.json",
    "ensembl_vep": "ensembl_vep_c499CT.json",
    "dbsnp": "dbsnp_refsnp_5030820.json",
    "uniprot": "uniprot_P40337.json",
}

GROUP_LABELS = {
    "core_biology": "Core Biology (persistent)",
    "operational": "Operational (persistent)",
    "evidence": "Evidence (ephemeral)",
}


# ---------------------------------------------------------------------------
# Config / result
# ---------------------------------------------------------------------------

@dataclass
class RunConfig:
    gene: str = "VHL"
    term: str | None = None                     # raw ClinVar ESearch term; defaults to "<gene>[gene]"
    uids: list[str] = field(default_factory=list)   # explicit VariationIDs; skips ESearch
    mode: str = "ephemeral"                     # ephemeral | persistent | full
    connectors: tuple[str, ...] = ("clinvar",)
    sample_pct: float = 100.0
    limit: int | None = None
    seed: int = 62
    offline: bool = False
    batch_size: int = 50
    clingen_spec_id: str = "GN078"
    resident_path: str | None = None            # persistent-layer snapshot to attach to
    viz_max_events: int = 24_000
    run_id: str = "run"
    progress: bool = True

    def search_term(self) -> str:
        return self.term or f"{self.gene}[gene]"


@dataclass
class RunResult:
    config: RunConfig
    events: list[dict]
    nodes: list[ForgeNode]
    edges: list[ForgeEdge]
    edge_stats: EdgeStatsReport
    waves: list[dict]
    resident_node_ids: list[str]
    quarantined: list[dict]
    batches_committed: int
    batches_rejected: int
    variants_requested: int
    variants_ingested: int
    events_truncated: bool
    elapsed_seconds: float
    verified: bool
    identity_merges: int = 0
    feeds: list[dict] = field(default_factory=list)
    structural_hash: str | None = None   # forge-pipeline-v1.md §6 -- over the whole committed graph
    coverage: dict[str, CoverageReport] = field(default_factory=dict)   # forge-pipeline-v1.md §11,
                                                                          # keyed by connector name


# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------
# Emission itself now lives in orchestration/connector_registry.py -- it's a shared
# contract type every connector's build_emission returns, not a runner-private detail.

# A node's provenance list is bounded, not unbounded. Real, measured problem at harvest
# scale: `absorb` below merges provenance every time a node is re-sighted, and a Submitter
# like "Invitae" is legitimately re-sighted on tens of thousands of variants across a gene
# panel. Unbounded, that single node's provenance list alone grows to tens of thousands of
# SourceProvenance objects -- gigabytes across the graph, for a list nothing ever reads
# past the first few entries of. Capped and deduped by (source, record_id, version), so
# what's kept is the DISTINCT contributing sources, which is what multi-provenance is
# actually for (forge-ir-v3-schema.md §3) -- not one entry per sighting.
MAX_PROVENANCE_PER_NODE = 8


def _provenance_key(p) -> tuple:
    """Identity of a provenance entry for dedup, ignoring the wall-clock retrieval time --
    the same record fetched twice is one source, not two."""
    for fields in (("source", "source_record_id", "source_version"),
                   ("derivation_rule", "input_node_ids", "compiler_version"),
                   ("investigation_id", "activation_reason")):
        if all(hasattr(p, f) for f in fields):
            return (type(p).__name__,) + tuple(getattr(p, f) for f in fields)
    return (type(p).__name__, repr(p))


def merge_provenance(existing: list, incoming: list, cap: int = MAX_PROVENANCE_PER_NODE) -> list:
    """Distinct-source merge under a hard cap. Returns the list to keep."""
    if len(existing) >= cap:
        return existing
    seen = {_provenance_key(p) for p in existing}
    for p in incoming:
        key = _provenance_key(p)
        if key in seen:
            continue
        existing.append(p)
        seen.add(key)
        if len(existing) >= cap:
            break
    return existing


class GraphStore:
    """Deduped accumulation of many committed micro-batches. Dedup here is not
    redundant with the compiler's: the compiler dedupes *within* a batch, this dedupes
    *across* them -- the same Disease node legitimately arrives once per variant, and at
    whole-gene scale that's the difference between 1,100 Disease nodes and the ~40 real ones."""

    def __init__(self) -> None:
        self.nodes: dict[str, ForgeNode] = {}
        self.edges: dict[tuple, ForgeEdge] = {}
        self.sightings: dict[str, int] = {}   # node id -> how many micro-batches referenced it

    def absorb(self, nodes: list[ForgeNode], edges: list[ForgeEdge]
                ) -> tuple[list[ForgeNode], list[ForgeEdge], list[str]]:
        """Returns the genuinely new nodes/edges -- so the event log records each node
        forming once rather than re-forming on every variant that references it -- plus
        the ids of nodes that were ALREADY known. That third list is the observable
        cross-batch identity resolution: a Disease or Gene arriving again from a different
        variant's batch and being merged onto the existing node instead of duplicated."""
        new_nodes, new_edges, merged_ids = [], [], []
        for n in nodes:
            self.sightings[n.id] = self.sightings.get(n.id, 0) + 1
            if n.id in self.nodes:
                merge_provenance(self.nodes[n.id].provenance, n.provenance)
                merged_ids.append(n.id)
            else:
                self.nodes[n.id] = n
                new_nodes.append(n)
        for e in edges:
            key = (e.src, e.predicate, e.qualifier, e.dst)
            if key in self.edges:
                merge_provenance(self.edges[key].provenance, e.provenance)
            else:
                self.edges[key] = e
                new_edges.append(e)
        return new_nodes, new_edges, merged_ids

    def node_list(self) -> list[ForgeNode]:
        return list(self.nodes.values())

    def edge_list(self) -> list[ForgeEdge]:
        return list(self.edges.values())


# ---------------------------------------------------------------------------
# Event capture
# ---------------------------------------------------------------------------

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


def _node_event(n: ForgeNode, wave: str, order: int, feed: str | None) -> dict:
    label = n.attributes.get("symbol") or n.attributes.get("label") or n.attributes.get("accession") \
        or n.attributes.get("name") or n.id
    return {
        "kind": "node", "order": order, "wave": wave,
        "id": n.id, "role": n.role.value, "subtype": n.subtype, "layer": n.layer.value,
        "origin": n.origin.value, "label": str(label)[:60], "feed": feed,
        "attributes": _safe_attrs(n.attributes),
    }


def _edge_event(e: ForgeEdge, by_id: dict, wave: str, order: int, feed: str | None) -> dict:
    src_node, dst_node = by_id.get(e.src), by_id.get(e.dst)
    return {
        "kind": "edge", "order": order, "wave": wave,
        "src": e.src, "dst": e.dst, "predicate": e.predicate.value, "qualifier": e.qualifier,
        "origin": e.origin.value, "feed": feed,
        "src_layer": src_node.layer.value if src_node else "RESIDENT",
        "dst_layer": dst_node.layer.value if dst_node else "RESIDENT",
    }


def _identity_event(node_id: str, namespace: str, status: str, sightings: int, wave: str,
                     order: int) -> dict:
    return {
        "kind": "identity", "order": order, "wave": wave,
        "id": node_id, "namespace": namespace, "status": status, "sightings": sightings,
    }


class EventLog:
    def __init__(self, budget: int) -> None:
        self.events: list[dict] = []
        self.budget = budget
        self.truncated = False
        self._order = 0

    def record(self, wave: str, nodes: list[ForgeNode], edges: list[ForgeEdge], by_id: dict,
                emission: Emission, identities: list[dict]) -> None:
        if len(self.events) >= self.budget:
            self.truncated = True
            return
        for n in nodes:
            self.events.append(_node_event(n, wave, self._order, emission.feed_for_node(n)))
            self._order += 1
        for e in edges:
            self.events.append(_edge_event(e, by_id, wave, self._order, emission.feed_for_edge(e)))
            self._order += 1
        for ident in identities:
            self.events.append(_identity_event(
                ident["node_id"], ident["namespace"], ident["status"], ident["sightings"],
                wave, self._order))
            self._order += 1


# ---------------------------------------------------------------------------
# Waves
# ---------------------------------------------------------------------------

def _wave(id_: str, label: str, layer: str) -> dict:
    return {"id": id_, "label": label, "layer": layer}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(config: RunConfig) -> RunResult:
    started = time.time()
    now = datetime.now(timezone.utc)
    store = GraphStore()
    log = EventLog(config.viz_max_events)
    waves: list[dict] = []
    quarantined: list[dict] = []
    batches_committed = batches_rejected = identity_merges = 0
    identity_index = IdentityIndex()
    coverage_reports: dict[str, CoverageReport] = {}

    resident_node_ids = _load_resident(config)

    def say(msg: str) -> None:
        if config.progress:
            print(msg, flush=True)

    def commit(emission: Emission, wave: str, context: str) -> bool:
        nonlocal batches_committed, batches_rejected, identity_merges
        compiler = StagingCompiler(identity_index, run_id=config.run_id)
        compiler.stage(emission.nodes, emission.edges)
        known = set(store.nodes) | set(resident_node_ids)
        result = compiler.commit(known_node_ids=known)
        if not result.committed:
            batches_rejected += 1
            quarantined.append({"scope": "batch", "context": context,
                                 "reason": result.rejection_reason})
            say(f"  ! batch REJECTED ({context}): {result.rejection_reason}")
            return False
        batches_committed += 1
        new_nodes, new_edges, merged_ids = store.absorb(result.nodes, result.edges)

        # Cross-batch identity resolution, made observable. The compiler resolves every
        # Gene/Variant/Disease against the persistent SQLite index; `merged_ids` is the
        # subset that had already been seen, i.e. the ones that were merged onto an
        # existing node rather than creating a duplicate.
        resolution_status = {r["node_id"]: r for r in result.identity_resolutions}
        identities = []
        for node_id in merged_ids:
            resolution = resolution_status.get(node_id)
            if resolution is None:
                continue      # not an identity-resolvable role -- plain exact-id dedup
            identity_merges += 1
            identities.append({"node_id": node_id, "namespace": resolution["namespace"],
                                "status": resolution["status"],
                                "sightings": store.sightings[node_id]})

        log.record(wave, new_nodes, new_edges, store.nodes, emission, identities)
        return True

    # ---- persistent layers -------------------------------------------------
    # Driven generically through plan_load()/execute() (orchestration/load_plan.py) --
    # no connector is named here. `config.connectors` acts as an allowlist across
    # cadence="persistent" descriptors when it names any of them; otherwise every
    # registered persistent connector runs, preserving the pre-registry default
    # ("--mode persistent" with no --connectors flag has always meant "everything
    # gene-scoped and bulk-preloadable").
    if config.mode in ("persistent", "full"):
        load_persistent(config, config.gene, commit, say, waves, coverage_reports, now)

    # ---- ephemeral / evidence layer ---------------------------------------
    variants_requested = variants_ingested = 0
    if config.mode in ("ephemeral", "full"):
        uids, offline_payloads = select_evidence_uids(config, say)
        variants_requested = len(offline_payloads) if config.offline else len(uids)
        payloads = (iter(offline_payloads) if config.offline
                    else stream_evidence_payloads(config, uids, say))
        per_variant_waves = variants_requested <= 8

        for index, (batch_no, payload) in enumerate(payloads):
            wave_id = (f"evidence_{payload['uid']}" if per_variant_waves else f"evidence_b{batch_no}")
            if not any(w["id"] == wave_id for w in waves):
                label = (f"EVIDENCE · ClinVar variant {payload['uid']}" if per_variant_waves
                         else f"EVIDENCE · ClinVar batch {batch_no}")
                waves.append(_wave(wave_id, label, "EVIDENCE"))

            in_scope = set(store.nodes) | set(resident_node_ids)
            ingested = _ingest_one_variant(config, payload, wave_id, commit, quarantined, now,
                                            in_scope, coverage_reports)
            if ingested:
                variants_ingested += 1
            if config.progress and (index + 1) % 100 == 0:
                say(f"  … {index + 1}/{variants_requested} variants · "
                    f"{len(store.nodes)} nodes · {len(store.edges)} edges")

    nodes, edges = store.node_list(), store.edge_list()
    stats = compute_edge_stats(nodes, edges)

    return RunResult(
        config=config, events=log.events, nodes=nodes, edges=edges, edge_stats=stats,
        waves=waves, resident_node_ids=sorted(resident_node_ids), quarantined=quarantined,
        batches_committed=batches_committed, batches_rejected=batches_rejected,
        variants_requested=variants_requested, variants_ingested=variants_ingested,
        events_truncated=log.truncated, elapsed_seconds=round(time.time() - started, 1),
        verified=batches_rejected == 0, identity_merges=identity_merges,
        feeds=_feed_manifest(log.events), structural_hash=compute_structural_hash(nodes, edges),
        coverage=coverage_reports,
    )


def persistent_descriptors_for(config: RunConfig) -> list:
    """Which persistent connectors a given selection runs. `config.connectors` acts as an
    allowlist across cadence="persistent" descriptors when it names any of them; otherwise
    every registered persistent connector marked `default_included` runs, preserving the
    pre-registry default ("--mode persistent" with no --connectors flag has always meant
    "everything gene-scoped and bulk-preloadable")."""
    requested = {CONNECTOR_ALIASES.get(c, c) for c in config.connectors}
    every_persistent = [d for d in list_connectors() if d.cadence == "persistent" and d.implemented]
    default_persistent = [d for d in every_persistent if d.default_included]
    # Explicit naming always works, opt-in or not (uniprot: default_included=False, but
    # `--connectors uniprot` still finds it here); the default_included filter only shrinks
    # what "no explicit persistent selection" falls back to.
    requested_persistent = [d for d in every_persistent if d.name in requested]
    return requested_persistent or default_persistent


def load_persistent(config: RunConfig, gene: str, commit, say, waves: list[dict],
                     coverage_reports: dict, now) -> list:
    """Builds the Core Biology + Operational layers for ONE gene, driven generically
    through plan_load()/execute() (orchestration/load_plan.py) -- no connector is named
    here. Extracted from run() so the multi-gene harvest (orchestration/harvest.py) drives
    exactly this code per gene rather than reimplementing it; run() is still its only
    caller in single-gene mode. Returns the descriptors that ran."""
    persistent_descriptors = persistent_descriptors_for(config)
    persistent_names = {d.name for d in persistent_descriptors}

    for descriptor in persistent_descriptors:
        layer = "CORE_BIOLOGY" if descriptor.group == "core_biology" else "OPERATIONAL"
        say(f"[persistent] {layer} — {descriptor.feed_label} for {gene}")
        if not any(w["id"] == descriptor.group for w in waves):
            waves.append(_wave(descriptor.group,
                                f"{layer.replace('_', ' ')} · {descriptor.feed_label}", layer))

    resolver = ScopeResolver()
    planner = ConnectorPlanner(IdentityIndex(), rate_policies={})
    policy = Policy(name="run", lod_ceiling=2, clingen_spec_id=config.clingen_spec_id)
    plan = plan_load(gene, "gene", policy, resolver, planner)
    persistent_plan = replace(plan, tasks=tuple(t for t in plan.tasks if t.connector in persistent_names))

    def _persistent_fetch(descriptor, query):
        if config.offline:
            fixture = OFFLINE_FIXTURES.get(descriptor.name)
            if fixture is None:
                raise ValueError(f"no offline fixture registered for connector {descriptor.name!r}")
            return iter([_load_json(fixture)])
        return descriptor.adapter.fetch(query)

    def _persistent_commit(emission: Emission, connector_name: str, task_id: str) -> bool:
        return commit(emission, get_connector(connector_name).group, task_id)

    execute(persistent_plan, {"now": now}, _persistent_commit, fetch=_persistent_fetch,
            coverage_reports=coverage_reports)
    return persistent_descriptors


def _feed_manifest(events: list[dict]) -> list[dict]:
    """The connector feeds this run actually produced, with their group and node/edge
    counts -- what drives the visualisation's per-connector toggles. Built from the event
    log rather than declared up front so a feed that yielded nothing never appears as a
    toggle that does nothing when clicked. `group_for_feed_label` is the registry's own
    join from a feed's display label back to its connector's group -- new connectors
    need no changes here at all."""
    counts: dict[str, dict] = {}
    for ev in events:
        feed = ev.get("feed")
        if not feed:
            continue
        bucket = counts.setdefault(
            feed, {"feed": feed, "group": group_for_feed_label(feed), "nodes": 0, "edges": 0})
        bucket["nodes" if ev["kind"] == "node" else "edges"] += 1
    order = {"core_biology": 0, "operational": 1, "evidence": 2}
    return sorted(counts.values(), key=lambda f: (order.get(f["group"], 9), -f["nodes"]))


# ---------------------------------------------------------------------------
# Per-variant evidence ingestion
# ---------------------------------------------------------------------------

def _drop_out_of_scope_gene_edges(edges: list[ForgeEdge], in_scope: set[str], uid: str,
                                   quarantined: list[dict]) -> list[ForgeEdge]:
    """Relationship-level quarantine for in_gene edges pointing outside the run's scope.

    A gene-family search returns variants that overlap NEIGHBOURING genes too: real case,
    VCV000265138 is NM_001018115.3(FANCD2):c.2605+1G>A and carries submitted Gene elements
    for both FANCD2 and VHL, because they sit together on 3p25.3. The FANCD2 Gene node is
    genuinely not part of a VHL-scoped run, so the edge to it cannot resolve.

    Dropping the edge and keeping the record is the same trade the SCV<->RCV crosswalk
    already makes (connectors/clinvar.py) -- quarantine the relationship, not the record.
    Rejecting the batch instead would discard a real variant and its entire assertion
    history over one out-of-scope neighbour, and every drop is recorded, never silent."""
    kept = []
    for e in edges:
        if e.qualifier == "in_gene" and e.dst not in in_scope:
            quarantined.append({"scope": "relationship", "uid": uid,
                                 "reason": f"in_gene edge to out-of-scope gene {e.dst} "
                                           f"(variant overlaps a neighbouring gene)"})
            continue
        kept.append(e)
    return kept


def _ingest_one_variant(config: RunConfig, payload: dict, wave_id: str, commit,
                         quarantined: list[dict], now, in_scope: set[str],
                         coverage_reports: dict[str, CoverageReport],
                         dirty=None, enrich_names: set[str] | None = None,
                         gene: str | None = None) -> bool:
    """ClinVar's build_emission does the parse + Variant-entity construction; any
    OTHER registered ephemeral connector that `requires=("clinvar",)` (today: gnomAD,
    VEP; a new per-variant evidence connector needs nothing changed here to join this
    loop) then enriches the same variant before it's committed as one micro-batch --
    exactly what QUARANTINE-vs-REJECT calls for: a bad gnomAD/VEP fetch quarantines
    just its own contribution, not the ClinVar data already parsed successfully."""
    clinvar = get_connector("clinvar")
    coverage = coverage_reports.setdefault("clinvar", CoverageReport(source="clinvar"))
    coverage.records_read += 1
    context: dict = {"now": now, "coverage": coverage}
    try:
        emission = clinvar.build_emission(payload, context)
        tag_with_source_manifest(emission, clinvar, now)
    except UnsupportedClinVarRecord as exc:
        coverage.records_quarantined += 1
        quarantined.append({"scope": "record", "uid": payload["uid"], "reason": str(exc)})
        return False
    except Exception as exc:   # any other parse failure is still record-level, never batch-level
        coverage.records_quarantined += 1
        quarantined.append({"scope": "record", "uid": payload["uid"],
                             "reason": f"{type(exc).__name__}: {exc}"})
        return False

    variant_id = context["variant_id"]   # set by clinvar.build_emission
    variant_node = next(n for n in emission.nodes if n.id == variant_id)
    attrs = variant_node.attributes
    accession = variant_node.provenance[0].source_record_id   # the vcv.accession

    # Offline, the gnomAD and VEP fixtures belong to ONE specific variant. Attaching them
    # to any other variant in the run would be fabricated frequency and consequence data
    # wearing a real variant's id -- far worse than simply having no gnomAD node.
    fixture_ok = (not config.offline) or accession == OFFLINE_VARIANT_WITH_GNOMAD_VEP
    enabled = {CONNECTOR_ALIASES.get(c, c) for c in config.connectors}
    coord_context = {"chrom": refseq_to_chromosome(attrs["seq_accession"]),
                      "pos": attrs["spdi_position"], "ref": attrs["ref"], "alt": attrs["alt"],
                      "rsid": context.get("dbsnp_rsid")}

    if fixture_ok:
        for descriptor in list_connectors():
            if descriptor.cadence != "ephemeral" or "clinvar" not in descriptor.requires:
                continue
            if descriptor.name not in enabled or not descriptor.implemented:
                continue
            if enrich_names is not None and descriptor.name not in enrich_names:
                # Selected out of THIS pass by the harvest's enrichment budget, not
                # broken. The variant still commits with its ClinVar evidence; the
                # missing enrichment becomes a retryable ticket rather than silently
                # never having existed.
                _defer(dirty, descriptor, variant_id, coord_context, gene, variant_node)
                continue
            _enrich_with(descriptor, config, variant_id, coord_context, emission, quarantined,
                          dirty=dirty, variant_node=variant_node, gene=gene)

    emission.edges = _drop_out_of_scope_gene_edges(
        emission.edges, in_scope | {n.id for n in emission.nodes}, payload["uid"], quarantined)

    committed = commit(emission, wave_id, f"clinvar:{accession}")
    if not committed:
        coverage.records_rejected_batches += 1
    return committed


def _ticket(descriptor, variant_id: str, coord_context: dict, reason: str, detail: str,
             gene: str | None, query: dict | None = None):
    from ingest.dirty import RetryTicket, expected_edge_shapes
    return RetryTicket(
        node_id=variant_id, connector=descriptor.name, reason=reason, detail=detail,
        scope="coordinates", context=dict(coord_context), query=query,
        expected_edges=expected_edge_shapes(descriptor), gene=gene,
    )


def _defer(dirty, descriptor, variant_id: str, coord_context: dict, gene: str | None,
            variant_node=None) -> None:
    """Records an enrichment the run chose not to attempt. No-op without a ledger, so
    single-gene runs behave exactly as before."""
    if dirty is None:
        return
    from ingest.dirty import DEFERRED, mark_dirty
    dirty.record(_ticket(descriptor, variant_id, coord_context, DEFERRED,
                          "enrichment budget exhausted for this pass", gene))
    if variant_node is not None:
        mark_dirty(variant_node, descriptor.name, DEFERRED,
                   "enrichment budget exhausted for this pass")


def _enrich_with(descriptor, config: RunConfig, variant_id: str, coord_context: dict,
                  emission: Emission, quarantined: list[dict], dirty=None,
                  variant_node=None, gene: str | None = None) -> None:
    """Runs one requires=("clinvar",) connector against a single variant's coordinates
    and merges its Emission into the caller's -- the generalized replacement for the old
    connector-specific _add_gnomad/_add_vep functions.

    With a `dirty` ledger attached (harvest runs), a failure additionally marks the
    Variant node and files a retry ticket carrying the exact query, so the missing data
    is recoverable later instead of only being counted. Without one (single-gene runs,
    every existing test), behaviour is unchanged: quarantine the contribution, keep the
    ClinVar evidence, move on."""
    from ingest.dirty import FETCH_FAILED, NOT_APPLICABLE, mark_dirty

    query = None
    try:
        if config.offline:
            fixture = OFFLINE_FIXTURES.get(descriptor.name)
            if fixture is None:
                raise ValueError(f"no offline fixture registered for connector {descriptor.name!r}")
            raw_iter = iter([_load_json(fixture)])
        else:
            query = descriptor.build_query("coordinates", None, coord_context)
            if query is None:
                # Legitimate "not applicable to this variant" -- e.g. dbSNP with no rsID
                # cross-reference for this record. Not an error, so not a quarantine entry,
                # and filed as NOT_APPLICABLE rather than pending: there is nothing to retry.
                if dirty is not None:
                    dirty.record(_ticket(descriptor, variant_id, coord_context, NOT_APPLICABLE,
                                          "build_query() returned None for this variant", gene))
                return
            raw_iter = descriptor.adapter.fetch(query)
        for raw in raw_iter:
            sub_emission = descriptor.build_emission(raw, {"variant_id": variant_id})
            tag_with_source_manifest(sub_emission, descriptor, datetime.now(timezone.utc))
            emission.merge(sub_emission)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        quarantined.append({"scope": "record", "uid": variant_id,
                             "reason": f"{descriptor.feed_label} unavailable: {detail}"})
        if dirty is not None:
            dirty.record(_ticket(descriptor, variant_id, coord_context, FETCH_FAILED,
                                  detail, gene, query=query))
            if variant_node is not None:
                mark_dirty(variant_node, descriptor.name, FETCH_FAILED, detail)




# ---------------------------------------------------------------------------
# Fetching / sampling
# ---------------------------------------------------------------------------

def select_evidence_uids(config: RunConfig, say) -> tuple[list[str], list[tuple[int, dict]]]:
    """Resolves WHICH variants this run covers, without fetching any of them.

    Split from the fetch (`stream_evidence_payloads`) so the count is known up front --
    progress lines and budgets need it -- while the archives themselves stay unfetched
    until iterated. Returns (uids, offline_payloads); exactly one of the two is populated."""
    if config.offline:
        payloads = [
            (1, {"uid": "2218", "efetch_xml": _load_text("clinvar_efetch_VCV2218.xml"),
                 "esummary": _load_json("clinvar_esummary_VCV2218.json")}),
            (1, {"uid": "4558169", "efetch_xml": _load_text("clinvar_efetch_VCV4558169_conflicting.xml"),
                 "esummary": _load_json("clinvar_esummary_VCV4558169_conflicting.json")}),
        ]
        say(f"[evidence] offline — {len(payloads)} fixture variants")
        return [], payloads

    adapter = ClinVarAdapter()
    uids = list(config.uids)
    if not uids:
        say(f"[evidence] ESearch: {config.search_term()!r}")
        uids = adapter.search_variation_ids(config.search_term())
        say(f"[evidence] family size: {len(uids)} ClinVar records")

    uids = _sample(uids, config)
    say(f"[evidence] selected {len(uids)} variants "
        f"(sample_pct={config.sample_pct}, limit={config.limit}, seed={config.seed})")
    return uids, []


def stream_evidence_payloads(config: RunConfig, uids: list[str], say):
    """Yields (batch_no, payload) one variant at a time, fetching a batch only when the
    previous one has been consumed.

    A generator rather than a list on purpose, and it is not a micro-optimisation: the
    old version materialised every ClinVar archive before ingesting any of them. At the
    1,134-variant VHL scale that was a few hundred MB and merely wasteful; at the 50,000
    scale of a panel harvest it is tens of GB of XML held live for no reason, since each
    payload is finished with the moment its micro-batch commits. Streaming also means a
    run killed at hour six has already committed everything up to that point instead of
    dying with the whole family still buffered in memory."""
    adapter = ClinVarAdapter()
    batch_no = 0
    fetched = 0
    for start in range(0, len(uids), config.batch_size):
        batch_no += 1
        chunk = uids[start:start + config.batch_size]
        say(f"[evidence] fetching batch {batch_no} ({len(chunk)} variants)…")
        for payload in adapter.fetch_many(chunk, batch_size=config.batch_size):
            fetched += 1
            yield batch_no, payload
    say(f"[evidence] fetched {fetched} archives")


def _sample(uids: list[str], config: RunConfig) -> list[str]:
    """Deterministic sampling: same seed + same family = same subset, so a 50% run is
    reproducible and comparable against the next one rather than a different graph each time."""
    selected = uids
    if config.sample_pct < 100.0:
        k = max(1, round(len(uids) * config.sample_pct / 100.0))
        rng = random.Random(config.seed)
        selected = sorted(rng.sample(uids, k), key=uids.index)
    if config.limit is not None:
        selected = selected[:config.limit]
    return selected


def _load_resident(config: RunConfig) -> set[str]:
    """Node ids the ephemeral run may point at without creating them.

    Loaded from a real persistent-run snapshot when one exists -- that's the verified
    path. Falling back to a declared gene anchor is explicitly weaker and is reported as
    such in the run summary: it asserts residency rather than confirming it."""
    if config.mode not in ("ephemeral",):
        return set()
    if config.resident_path and os.path.exists(config.resident_path):
        with open(config.resident_path, encoding="utf-8") as f:
            return set(json.load(f)["node_ids"])
    hgnc = KNOWN_GENE_HGNC.get(config.gene.upper())
    return {gene_canonical_id(hgnc)} if hgnc else set()


def _load_json(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def _load_text(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()
