"""Panel-scale harvest: many genes, one graph, resumable, bounded memory.

`orchestration/runner.py::run()` builds one gene's graph and holds all of it in memory,
which is exactly right up to the ~9,700-node VHL family it was validated on. A panel
harvest targeting tens of thousands of variants across ~90 genes is a different problem
in three specific ways, and this module exists for those three and nothing else:

  MEMORY.  Half a million ForgeNode objects with their attribute dicts (ClinVar
    interpretation comments alone run to thousands of characters each) plus a million-odd
    edges do not belong in one process's heap. So the committed graph streams to a
    gzipped JSONL dump as it is produced and is never all resident: what stays in memory
    is node ids (for referential integrity), a compact per-node (subtype, layer, feed,
    wave) tuple, edge-key hashes (for cross-batch dedup) and an incremental statistics
    accumulator. That is a few hundred MB regardless of how long the run goes.

  INTERRUPTION.  A nine-hour run WILL be interrupted -- laptop sleep, a dropped VPN, an
    NCBI outage. Progress is checkpointed per gene and per batch, so a restart resumes at
    the last completed batch instead of re-fetching everything. This is also why the dump
    is append-only JSONL and not one big JSON document: a killed process leaves a file
    that is still readable up to its last complete line.

  PARTIAL DATA.  Per-variant enrichment (gnomAD, VEP, dbSNP) is one HTTP round-trip each,
    so it cannot cover every variant inside one night at this scale. Rather than pretend
    the un-enriched variants were never meant to have that data, every skipped or failed
    enrichment marks its Variant node dirty and files a retry ticket carrying the exact
    query (ingest/dirty.py). "Missing because we ran out of night" and "missing because
    gnomAD has no such variant" stay distinguishable, which is the whole point.

What this module deliberately does NOT do is reimplement ingestion. Per-gene persistent
loading is `runner.load_persistent`, per-variant evidence is `runner._ingest_one_variant`,
committing is `StagingCompiler`, verification is the same 9 checks. If a connector works
in a single-gene run it works here, unchanged.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connectors  # noqa: F401 -- registers every connector as an import side effect
from analysis.edge_stats import EdgeStatsAccumulator
from connectors.clinvar import ClinVarAdapter
from forge_ir.graph import ForgeEdge, ForgeNode
from ingest.compiler import CoverageReport, StagingCompiler
from ingest.dirty import DirtyLedger
from ingest.identity import IdentityIndex
from orchestration.connector_registry import Emission, list_connectors
from orchestration.runner import (
    CONNECTOR_ALIASES, RunConfig, _ingest_one_variant, load_persistent,
    persistent_descriptors_for,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class HarvestConfig:
    genes: tuple[str, ...] = ()
    target_variants: int = 50_000
    out_dir: str = "runs/harvest"
    connectors: tuple[str, ...] = ()          # empty = every implemented connector
    enrich_every: int = 0                      # run per-variant enrichment on 1 in N variants;
                                                # 0 disables it entirely, 1 enriches every variant
    batch_size: int = 100                      # ClinVar ids per E-utilities request
    run_id: str = "harvest"
    deadline_hours: float | None = None        # stop cleanly after this long, mid-gene if needed
    resume: bool = True
    fetch_workers: int = 3       # concurrent ClinVar batch fetches in flight
    progress_every: int = 250
    clingen_spec_id: str | None = None   # None on purpose -- see scripts/overnight_harvest.py

    def enrich_connector_names(self) -> set[str]:
        """The ephemeral, per-variant connectors this harvest may call. gnomAD/VEP/dbSNP
        today; a new one joins by registering, with nothing changed here."""
        selected = {CONNECTOR_ALIASES.get(c, c) for c in self.connectors} if self.connectors else None
        out = set()
        for d in list_connectors():
            if d.cadence != "ephemeral" or not d.implemented or "clinvar" not in d.requires:
                continue
            if selected is None or d.name in selected:
                out.add(d.name)
        return out


@dataclass
class GeneAllocation:
    gene: str
    family_size: int            # what ClinVar's own ESearch reports for "<gene>[gene]"
    budget: int                 # how many of them this harvest will take
    uids: list[str] = field(default_factory=list)
    resolved: bool = True       # False = ClinVar returned nothing for this symbol


# ---------------------------------------------------------------------------
# Budget allocation
# ---------------------------------------------------------------------------

def allocate_budget(family_sizes: dict[str, int], target: int) -> dict[str, int]:
    """Max-min fair allocation of `target` variants across genes.

    Every gene gets an equal share; a gene whose whole family is smaller than its share
    takes only what it has and hands the remainder back to be re-divided among the genes
    that can still use it. Repeat until nothing more can be redistributed.

    The point is coverage shape, not arithmetic elegance. Flat per-gene caps waste budget
    on small families and starve nothing; pure proportional allocation does the opposite,
    handing BRCA2 (tens of thousands of ClinVar records) most of the night and leaving the
    small, well-curated families with a handful of variants each. Max-min gives every
    small gene a COMPLETE census and splits what is left evenly among the large ones."""
    remaining_genes = {g: n for g, n in family_sizes.items() if n > 0}
    allocation = {g: 0 for g in family_sizes}
    remaining_budget = target

    while remaining_genes and remaining_budget > 0:
        share = remaining_budget // len(remaining_genes)
        if share == 0:
            # Fewer variants left than genes -- hand them out one each, largest families
            # last so the leftovers land where they are scarcest.
            for gene in sorted(remaining_genes, key=lambda g: remaining_genes[g])[:remaining_budget]:
                allocation[gene] += 1
                remaining_budget -= 1
            break
        satisfied = {g: n for g, n in remaining_genes.items() if n <= share}
        if not satisfied:
            for gene in remaining_genes:
                allocation[gene] += share
                remaining_budget -= share
            break
        for gene, n in satisfied.items():
            allocation[gene] += n
            remaining_budget -= n
            del remaining_genes[gene]
    return allocation


def load_panel(path: str | None = None, interleave: bool = True) -> tuple[list[str], dict]:
    """Reads data/gene_panel.json and returns the gene order to harvest in.

    Interleaved across groups by default -- one gene from hereditary cancer, then one from
    cardiomyopathy, then arrhythmia, and around again. Genes are processed to completion in
    order, and the run has a wall-clock deadline it will stop cleanly at, so the order IS
    the answer to "what does a run that only gets two thirds of the way through contain".
    Grouped order answers that with "every cancer gene and no cardiac ones"; interleaved
    order answers it with "two thirds of every category", which is the graph worth waking
    up to. Duplicates are dropped keeping first appearance, so VHL -- the baseline the rest
    of this project is validated against -- still leads."""
    path = path or os.path.join(BASE, "data", "gene_panel.json")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)

    groups: list[list[str]] = []
    seen: set[str] = set()
    for group in doc["groups"]:
        members = [g for g in group["genes"] if not (g in seen or seen.add(g))]
        if members:
            groups.append(members)

    if not interleave:
        return [g for members in groups for g in members], doc

    genes: list[str] = []
    for i in range(max(len(m) for m in groups)):
        for members in groups:
            if i < len(members):
                genes.append(members[i])
    return genes, doc


# ---------------------------------------------------------------------------
# Streaming store
# ---------------------------------------------------------------------------

def _edge_key(e: ForgeEdge) -> tuple:
    return (e.src, e.predicate.value, e.qualifier, e.dst)


class StreamingStore:
    """Cross-batch dedup and referential integrity WITHOUT holding the graph.

    The equivalent of runner.GraphStore, minus the part that does not fit: committed nodes
    and edges are written straight to a gzipped JSONL dump and dropped. What remains is
    the minimum needed to keep committing correctly -- ids, so an edge into an
    already-committed node still resolves; edge-key hashes, so the same edge is not
    written twice; and a compact per-node tuple for the statistics and the visualisation.

    Edge keys are stored as 64-bit hashes rather than the tuples themselves. At a million
    edges the tuples are hundreds of MB of strings that already exist elsewhere; the
    hashes are a few tens. A hash collision would drop one real edge out of a million --
    an accepted, quantified trade, recorded here rather than left implicit."""

    def __init__(self, dump_path: str, append: bool = False) -> None:
        # Every resume writes its OWN part file rather than appending to the previous one.
        #
        # Real, measured data loss this fixes: appending produced one file holding two gzip
        # members. The first session was killed mid-block, so its trailing member was
        # truncated -- and because a reader hits that corruption before ever reaching the
        # second member, the ENTIRE second session became unreadable too. A checkpoint
        # recording 1,212 ingested variants read back as 299. Separate parts mean a damaged
        # part costs only its own tail; every other part still reads clean.
        os.makedirs(os.path.dirname(os.path.abspath(dump_path)), exist_ok=True)
        if append:
            stem = dump_path[:-len(".jsonl.gz")] if dump_path.endswith(".jsonl.gz") else dump_path
            index = 1
            while os.path.exists(f"{stem}.part{index:03d}.jsonl.gz"):
                index += 1
            dump_path = f"{stem}.part{index:03d}.jsonl.gz"
        self.dump_path = dump_path
        self._fh = gzip.open(dump_path, "wt", encoding="utf-8")
        self.node_ids: set[str] = set()
        self.node_meta: dict[str, tuple] = {}    # id -> (subtype, layer, feed, wave, role)
        self._edge_hashes: set[int] = set()
        self.stats = EdgeStatsAccumulator()
        self.nodes_written = 0
        self.edges_written = 0
        self.sightings: dict[str, int] = {}
        self.dirty_node_ids: set[str] = set()

    def absorb(self, nodes: list[ForgeNode], edges: list[ForgeEdge], emission: Emission,
                wave: str) -> tuple[list[dict], list[dict], list[str]]:
        """Writes the genuinely new nodes/edges to the dump and returns them as plain
        dicts (for the event log), plus the ids of nodes that were already known -- the
        observable cross-batch identity resolution, same contract as GraphStore.absorb."""
        new_nodes, new_edges, merged_ids = [], [], []
        for n in nodes:
            self.sightings[n.id] = self.sightings.get(n.id, 0) + 1
            if n.id in self.node_ids:
                merged_ids.append(n.id)
                continue
            feed = emission.feed_for_node(n)
            self.node_ids.add(n.id)
            self.node_meta[n.id] = (n.subtype, n.layer.value, feed, wave, n.role.value)
            self.stats.observe_node(n.id, n.subtype, n.layer.value)
            if n.attributes.get("dirty"):
                self.dirty_node_ids.add(n.id)
            record = _node_record(n, feed, wave)
            self._write(record)
            self.nodes_written += 1
            new_nodes.append(record)
        for e in edges:
            h = hash(_edge_key(e))
            if h in self._edge_hashes:
                continue
            self._edge_hashes.add(h)
            record = _edge_record(e, emission.feed_for_edge(e), wave, self.node_meta)
            self.stats.observe_edge(e.src, e.predicate.value, e.qualifier, e.dst, e.origin.value)
            self._write(record)
            self.edges_written += 1
            new_edges.append(record)
        return new_nodes, new_edges, merged_ids

    def mark_dirty_id(self, node_id: str) -> None:
        """A node can go dirty AFTER it was written (its first enrichment succeeded, a
        later one failed). The dump keeps the record as written; this set is what the
        summary and the visualisation read, and it is rebuilt from the ledger on resume
        so it survives a restart."""
        self.dirty_node_ids.add(node_id)

    def _write(self, record: dict) -> None:
        self._fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _safe_attrs(attrs: dict, max_chars: int = 2000) -> dict:
    """JSON-safe attributes for the dump. Long strings are capped -- ClinVar interpretation
    comments run to many KB and multiply by ~120,000 assertions at panel scale, which is
    most of the dump's size for text nothing downstream reads in full. The cap is far more
    generous than the visualisation's own (200 chars, applied later in
    scripts/build_demo_html.py) so the dump stays the richer artifact of the two."""
    out = {}
    for k, v in attrs.items():
        if isinstance(v, str) and len(v) > max_chars:
            out[k] = v[:max_chars] + f"… (+{len(v) - max_chars} chars)"
        elif isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, dict)):
            try:
                encoded = json.dumps(v, default=str)
                out[k] = v if len(encoded) <= max_chars else f"<{type(v).__name__}, {len(v)} items>"
            except (TypeError, ValueError):
                out[k] = f"<{type(v).__name__}>"
        else:
            out[k] = str(v)[:max_chars]
    return out


def _node_record(n: ForgeNode, feed: str | None, wave: str) -> dict:
    label = n.attributes.get("symbol") or n.attributes.get("label") or n.attributes.get("accession") \
        or n.attributes.get("name") or n.id
    return {
        "kind": "node", "id": n.id, "role": n.role.value, "subtype": n.subtype,
        "layer": n.layer.value, "origin": n.origin.value, "label": str(label)[:60],
        "feed": feed, "wave": wave, "lod": n.lod,
        "dirty": bool(n.attributes.get("dirty")),
        "attributes": _safe_attrs(n.attributes),
        "provenance": [
            {"source": getattr(p, "source", None) or getattr(p, "derivation_rule", None)
                        or getattr(p, "investigation_id", None),
             "record_id": getattr(p, "source_record_id", None),
             "version": getattr(p, "source_version", None)}
            for p in n.provenance[:4]
        ],
    }


def _edge_record(e: ForgeEdge, feed: str | None, wave: str, node_meta: dict) -> dict:
    src_meta, dst_meta = node_meta.get(e.src), node_meta.get(e.dst)
    return {
        "kind": "edge", "src": e.src, "dst": e.dst, "predicate": e.predicate.value,
        "qualifier": e.qualifier, "origin": e.origin.value, "feed": feed, "wave": wave,
        "src_layer": src_meta[1] if src_meta else "RESIDENT",
        "dst_layer": dst_meta[1] if dst_meta else "RESIDENT",
    }


# ---------------------------------------------------------------------------
# Checkpointing
# ---------------------------------------------------------------------------

class Checkpoint:
    """Per-gene, per-batch progress, rewritten atomically after every committed batch.

    Atomic (write-temp-then-replace) rather than in-place: the whole reason this file
    exists is to survive a process being killed, and a checkpoint half-overwritten at the
    moment of the kill would be worse than none at all."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.state: dict = {
            "started_at": None, "genes_completed": [], "gene_cursor": {},
            "variants_ingested": 0, "variants_attempted": 0, "allocation": {},
            "family_sizes": {}, "last_updated": None,
        }
        # The resolved uid lists live in their OWN file, written once. They are by far the
        # biggest thing the plan produces (BRCA1 alone resolves to 16,061 ids; the whole
        # panel is hundreds of thousands) and they never change after planning, while the
        # checkpoint is rewritten after every committed batch. Keeping them together meant
        # re-serialising several MB of static ids ~500 times over a night -- gigabytes of
        # pointless disk writes, and a much wider window for a kill to land mid-write.
        self.uids_path = os.path.join(os.path.dirname(os.path.abspath(path)), "uids.json")
        self.uids: dict[str, list[str]] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.state.update(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass   # unreadable checkpoint = start fresh; never crash the harvest on it
        if os.path.exists(self.uids_path):
            try:
                with open(self.uids_path, encoding="utf-8") as f:
                    self.uids = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.uids = {}

    def save_uids(self, uids: dict[str, list[str]]) -> None:
        self.uids = uids
        tmp = self.uids_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(uids, f, separators=(",", ":"))
        os.replace(tmp, self.uids_path)

    def save(self) -> None:
        self.state["last_updated"] = datetime.now(timezone.utc).isoformat()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.path)

    def cursor(self, gene: str) -> int:
        return int(self.state["gene_cursor"].get(gene, 0))

    def advance(self, gene: str, index: int) -> None:
        self.state["gene_cursor"][gene] = index

    def complete(self, gene: str) -> None:
        if gene not in self.state["genes_completed"]:
            self.state["genes_completed"].append(gene)


# ---------------------------------------------------------------------------
# The harvest
# ---------------------------------------------------------------------------

@dataclass
class HarvestResult:
    config: HarvestConfig
    allocations: list[GeneAllocation]
    nodes_written: int
    edges_written: int
    variants_requested: int
    variants_ingested: int
    batches_committed: int
    batches_rejected: int
    quarantined: list[dict]
    identity_merges: int
    dirty_nodes: int
    dirty_summary: dict
    coverage: dict
    edge_stats: object
    elapsed_seconds: float
    genes_completed: list[str]
    stopped_early: str | None = None
    events: list[dict] = field(default_factory=list)
    feeds: list[dict] = field(default_factory=list)


def harvest(config: HarvestConfig, say=print) -> HarvestResult:
    started = time.time()
    now = datetime.now(timezone.utc)
    deadline = (now + timedelta(hours=config.deadline_hours)) if config.deadline_hours else None

    os.makedirs(config.out_dir, exist_ok=True)
    dump_path = os.path.join(config.out_dir, "graph.jsonl.gz")
    checkpoint = Checkpoint(os.path.join(config.out_dir, "checkpoint.json"))
    ledger = DirtyLedger(os.path.join(config.out_dir, "dirty_ledger.jsonl"))
    resuming = config.resume and bool(checkpoint.state["genes_completed"] or
                                       checkpoint.state["gene_cursor"])
    if not resuming:
        checkpoint.state["started_at"] = now.isoformat()

    store = StreamingStore(dump_path, append=resuming)
    store.dirty_node_ids |= ledger.dirty_node_ids()
    identity_index = IdentityIndex(os.path.join(config.out_dir, "identity.sqlite"))
    coverage_reports: dict[str, CoverageReport] = {}
    quarantined: list[dict] = []
    waves: list[dict] = []
    batches_committed = batches_rejected = identity_merges = 0
    variants_ingested = checkpoint.state.get("variants_ingested", 0)
    variants_attempted = checkpoint.state.get("variants_attempted", 0)
    enrich_names = config.enrich_connector_names()
    stopped_early: str | None = None

    # The event log for the visualisation. Capped hard and low: the FULL graph is on disk
    # in the dump, and scripts/build_viz_dataset.py rebuilds a proper, density-filtered
    # replay from that. This in-memory log exists only so a harvest that is never
    # post-processed still produces something viewable.
    events: list[dict] = []
    event_budget = 40_000

    def record_events(wave: str, new_nodes: list[dict], new_edges: list[dict],
                       identities: list[dict]) -> None:
        if len(events) >= event_budget:
            return
        order = len(events)
        for record in new_nodes:
            events.append({**record, "order": order}); order += 1
        for record in new_edges:
            events.append({**record, "order": order}); order += 1
        for ident in identities:
            events.append({"kind": "identity", "order": order, "wave": wave, **ident}); order += 1

    def commit(emission: Emission, wave: str, context: str) -> bool:
        nonlocal batches_committed, batches_rejected, identity_merges
        compiler = StagingCompiler(identity_index, run_id=config.run_id)
        compiler.stage(emission.nodes, emission.edges)
        result = compiler.commit(known_node_ids=store.node_ids)
        if not result.committed:
            batches_rejected += 1
            quarantined.append({"scope": "batch", "context": context,
                                 "reason": result.rejection_reason})
            # Say it out loud. A rejected batch is the pipeline refusing its own output --
            # the single most interesting event a run can produce -- and a harvest that
            # counts 25 of them without recording WHY is exactly the silent failure this
            # project exists to not have.
            say(f"  ! batch REJECTED ({context}): {result.rejection_reason}")
            return False
        batches_committed += 1
        new_nodes, new_edges, merged_ids = store.absorb(result.nodes, result.edges, emission, wave)

        resolution_status = {r["node_id"]: r for r in result.identity_resolutions}
        identities = []
        for node_id in merged_ids:
            resolution = resolution_status.get(node_id)
            if resolution is None:
                continue
            identity_merges += 1
            if len(identities) < 40:   # bounded: at panel scale merges run to the millions
                identities.append({"id": node_id, "namespace": resolution["namespace"],
                                    "status": resolution["status"],
                                    "sightings": store.sightings[node_id]})
        record_events(wave, new_nodes, new_edges, identities)
        return True

    # ---- plan: resolve every gene's real ClinVar family, then allocate ----
    adapter = ClinVarAdapter()
    family_sizes: dict[str, int] = dict(checkpoint.state.get("family_sizes") or {})
    uid_lists: dict[str, list[str]] = {g: list(v) for g, v in checkpoint.uids.items()}
    unresolved: list[str] = []

    for gene in config.genes:
        if gene in uid_lists:
            continue
        try:
            uids = adapter.search_variation_ids(f"{gene}[gene]")
        except Exception as exc:
            say(f"[plan] {gene}: ESearch failed ({type(exc).__name__}: {exc}) — skipped")
            family_sizes[gene] = 0
            uid_lists[gene] = []
            unresolved.append(gene)
            continue
        # Ascending VariationID: deterministic, reproducible, and it favours
        # longer-established records over the newest submissions. See data/gene_panel.json.
        uids = sorted(uids, key=lambda u: int(u) if u.isdigit() else 0)
        uid_lists[gene] = uids
        family_sizes[gene] = len(uids)
        if not uids:
            unresolved.append(gene)
        say(f"[plan] {gene}: {len(uids)} ClinVar records")
        time.sleep(0.34)   # NCBI unauthenticated rate limit is 3 requests/second

    checkpoint.state["family_sizes"] = family_sizes
    checkpoint.save_uids(uid_lists)
    allocation = checkpoint.state.get("allocation") or allocate_budget(family_sizes, config.target_variants)
    checkpoint.state["allocation"] = allocation
    checkpoint.save()

    allocations = [GeneAllocation(gene=g, family_size=family_sizes.get(g, 0),
                                   budget=allocation.get(g, 0), uids=uid_lists.get(g, []),
                                   resolved=g not in unresolved)
                   for g in config.genes]
    total_planned = sum(a.budget for a in allocations)
    say(f"[plan] {len(config.genes)} genes · {sum(family_sizes.values())} ClinVar records available "
        f"· {total_planned} allocated (target {config.target_variants})"
        + (f" · {len(unresolved)} unresolved: {', '.join(unresolved)}" if unresolved else ""))

    pbar = None
    if sys.stdout.isatty():
        try:
            from tqdm import tqdm
            pbar = tqdm(total=total_planned, initial=variants_attempted,
                        desc="Harvesting variants", unit="var", dynamic_ncols=True)
        except Exception:
            pbar = None

    # ---- walk the panel ---------------------------------------------------
    for allocation_entry in allocations:
        gene = allocation_entry.gene
        if gene in checkpoint.state["genes_completed"]:
            continue
        if allocation_entry.budget == 0:
            checkpoint.complete(gene)
            continue
        if deadline and datetime.now(timezone.utc) >= deadline:
            stopped_early = f"deadline reached before {gene}"
            break

        selected = allocation_entry.uids[:allocation_entry.budget]
        cursor = checkpoint.cursor(gene) if config.resume else 0
        if cursor >= len(selected):
            checkpoint.complete(gene)
            continue

        gene_config = RunConfig(
            gene=gene, mode="full", connectors=config.connectors or ("clinvar",),
            batch_size=config.batch_size, clingen_spec_id=config.clingen_spec_id,
            run_id=config.run_id, progress=False,
        )

        # Persistent layers first: this gene's Core Biology + Operational rules must exist
        # before its variants' edges can resolve against them. Only on a fresh start for
        # this gene -- on resume they are already committed and in store.node_ids.
        if cursor == 0:
            try:
                load_persistent(gene_config, gene, commit, lambda m: None, waves,
                                 coverage_reports, datetime.now(timezone.utc))
            except Exception as exc:
                say(f"[{gene}] persistent layer failed ({type(exc).__name__}: {exc}) — "
                    f"continuing with evidence only")
                quarantined.append({"scope": "gene", "context": gene,
                                     "reason": f"persistent load failed: {type(exc).__name__}: {exc}"})

        say(f"[{gene}] {len(selected)} variants allocated of {allocation_entry.family_size} "
            f"in family · resuming at {cursor}" if cursor else
            f"[{gene}] {len(selected)} variants allocated of {allocation_entry.family_size} in family")

        wave_id = f"gene_{gene}"
        if not any(w["id"] == wave_id for w in waves):
            waves.append({"id": wave_id, "label": f"EVIDENCE · {gene}", "layer": "EVIDENCE"})

        index = cursor
        # Batches are fetched CONCURRENTLY, a few ahead of ingestion, and consumed strictly
        # in submission order.
        #
        # Measured, not assumed: parse+commit costs ~9 ms per variant while the whole loop
        # was taking ~1.24 s per variant, so ~99% of the wall clock was a socket waiting for
        # E-utilities to return a ~100-record EFetch (BRCA1 archives are large). Serially
        # that caps a day's run near 16,000 variants; overlapping the waits lifts it roughly
        # `fetch_workers`-fold for no extra CPU.
        #
        # This does NOT meaningfully raise the request RATE: each EFetch is in flight for
        # ~100 s, so three concurrent ones is ~0.03 req/s against NCBI's 3 req/s
        # unauthenticated ceiling. What binds here is bandwidth, not politeness.
        #
        # Consumed in submission order, never as-completed, on purpose: `checkpoint.advance`
        # means "everything before this index is committed", and honouring a late-finishing
        # batch out of order would let a resume skip it.
        starts = list(range(cursor, len(selected), config.batch_size))

        def _fetch_batch(start_index: int):
            batch = selected[start_index:start_index + config.batch_size]
            try:
                return start_index, batch, list(adapter.fetch_many(batch, batch_size=config.batch_size)), None
            except Exception as exc:
                return start_index, batch, [], exc

        pool = ThreadPoolExecutor(max_workers=config.fetch_workers)
        pending = []
        try:
            for s in starts[:config.fetch_workers]:
                pending.append(pool.submit(_fetch_batch, s))
            next_to_submit = min(config.fetch_workers, len(starts))

            while pending:
                if deadline and datetime.now(timezone.utc) >= deadline:
                    stopped_early = f"deadline reached during {gene}"
                    for f in pending:
                        f.cancel()
                    break
                start, chunk, payloads, error = pending.pop(0).result()
                if next_to_submit < len(starts):
                    pending.append(pool.submit(_fetch_batch, starts[next_to_submit]))
                    next_to_submit += 1
                if error is not None:
                    say(f"[{gene}] batch fetch failed at {start} "
                        f"({type(error).__name__}: {error}) — skipping batch")
                    quarantined.append({"scope": "batch", "context": f"{gene}@{start}",
                                         "reason": f"clinvar fetch failed: {type(error).__name__}: {error}"})
                    index = start + len(chunk)
                    checkpoint.advance(gene, index)
                    checkpoint.save()
                    continue

                for payload in payloads:
                    variants_attempted += 1
                    if pbar is not None:
                        pbar.update(1)
                        pbar.set_postfix({
                            "gene": gene,
                            "nodes": f"{store.nodes_written:,}",
                            "edges": f"{store.edges_written:,}",
                            "dirty": len(ledger.open_tickets()),
                        })
                    enrich_this = bool(enrich_names) and config.enrich_every > 0 \
                        and (variants_attempted % config.enrich_every == 0)
                    ingested = _ingest_one_variant(
                        gene_config, payload, wave_id, commit, quarantined,
                        datetime.now(timezone.utc), store.node_ids, coverage_reports,
                        dirty=ledger, gene=gene,
                        enrich_names=enrich_names if enrich_this else set(),
                    )
                    if ingested:
                        variants_ingested += 1
                    if variants_attempted % config.progress_every == 0:
                        say(f"  … {variants_attempted} variants · {store.nodes_written} nodes · "
                            f"{store.edges_written} edges · {len(ledger.open_tickets())} dirty tickets")

                index = start + len(chunk)
                checkpoint.advance(gene, index)
                checkpoint.state["variants_ingested"] = variants_ingested
                checkpoint.state["variants_attempted"] = variants_attempted
                checkpoint.save()
                store.flush()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        if stopped_early:
            break
        checkpoint.complete(gene)
        checkpoint.save()

    if pbar is not None:
        pbar.close()

    store.dirty_node_ids |= ledger.dirty_node_ids()
    store.close()
    ledger.close()
    identity_index.close()

    return HarvestResult(
        config=config, allocations=allocations, nodes_written=store.nodes_written,
        edges_written=store.edges_written, variants_requested=total_planned,
        variants_ingested=variants_ingested, batches_committed=batches_committed,
        batches_rejected=batches_rejected, quarantined=quarantined,
        identity_merges=identity_merges, dirty_nodes=len(store.dirty_node_ids),
        dirty_summary=ledger.summary(), coverage=coverage_reports,
        edge_stats=store.stats.report(), elapsed_seconds=round(time.time() - started, 1),
        genes_completed=list(checkpoint.state["genes_completed"]),
        stopped_early=stopped_early, events=events,
        feeds=_feed_manifest(store.node_meta, events),
    )


def _feed_manifest(node_meta: dict, events: list[dict]) -> list[dict]:
    """Per-feed node/edge counts over the WHOLE committed graph for nodes (node_meta is
    complete) and over the retained event log for edges. Stated that way rather than
    silently mixing the two."""
    from orchestration.connector_registry import group_for_feed_label
    counts: dict[str, dict] = {}
    for _id, meta in node_meta.items():
        feed = meta[2]
        if not feed:
            continue
        bucket = counts.setdefault(feed, {"feed": feed, "group": group_for_feed_label(feed),
                                           "nodes": 0, "edges": 0})
        bucket["nodes"] += 1
    for ev in events:
        if ev.get("kind") != "edge" or not ev.get("feed"):
            continue
        bucket = counts.setdefault(ev["feed"], {"feed": ev["feed"],
                                                 "group": group_for_feed_label(ev["feed"]),
                                                 "nodes": 0, "edges": 0})
        bucket["edges"] += 1
    order = {"core_biology": 0, "operational": 1, "evidence": 2}
    return sorted(counts.values(), key=lambda f: (order.get(f["group"], 9), -f["nodes"]))
