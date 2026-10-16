"""Dirty nodes and the retry ledger -- what happens when a connector gets stuck.

At single-gene scale a failed enrichment fetch could just be quarantined and forgotten:
one gnomAD timeout out of 1,053 variants is noise. At 50,000-variant scale across a
whole gene panel it stops being noise -- a source that rate-limits for twenty minutes
silently costs thousands of variants their frequency data, and nothing in the committed
graph records that the data was *supposed* to be there.

So a stuck connector does three things instead of one:

  1. Marks the anchor node DIRTY in place -- `attributes["dirty"] = True` plus a
     per-connector reason list. Attributes are free-form (forge_ir/graph.py), so this
     needs no ontology change, and ForgeNode is deliberately not frozen precisely so
     in-place attribute updates like this are legal (forge-ir-v3-schema.md §6.3).
  2. Writes a RetryTicket to a persistent JSONL ledger holding the EXACT query that
     failed, so a retry pass re-issues the same request rather than re-deriving it.
  3. Records the edge shapes that connector WOULD have contributed, derived from the
     ontology rather than hand-listed (see `expected_edge_shapes`).

On point 3 -- what "connect all the edge types" can honestly mean here. It is tempting
to pre-create the missing edges as placeholders so the graph looks structurally
complete. We do not, for two reasons that are the same reason: a placeholder edge needs
a destination node id, and we do not have one (that id is exactly what the failed fetch
was going to tell us). Inventing it would fabricate data AND immediately fail verify()'s
referential-integrity check (forge_ir/verify.py check 2), which is the check doing its
job. So the *shape* is recorded on the ticket, not in the graph. A retry then has
everything it needs to close the loop in either direction:

  - the fetch succeeds with data  -> ADD the real edges, clear the dirty flag
  - the fetch succeeds with none  -> resolve as `empty`; the shape was legitimately
                                     never going to exist, stop retrying it
  - the fetch fails again         -> increment attempts, keep it pending until
                                     `max_attempts`, then `abandoned`

`empty` and `abandoned` are tracked separately on purpose. "gnomAD has no record of this
variant" and "gnomAD would not answer us" produce the same missing edge but mean
completely different things, and collapsing them would quietly turn an outage into a
biological claim.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from forge_ir.core import ALLOWED_EDGE_COMBINATIONS, NODE_SUBTYPE_REGISTRY, StorageScope, \
    edge_storage_scope
from forge_ir.graph import ForgeNode

PENDING = "pending"          # not yet retried, or retried and failed under max_attempts
RESOLVED = "resolved"        # retried, data arrived, edges added
EMPTY = "empty"              # retried, source authoritatively has nothing -- never retry again
ABANDONED = "abandoned"      # retried max_attempts times, still failing

OPEN_STATES = (PENDING,)
CLOSED_STATES = (RESOLVED, EMPTY, ABANDONED)

# Why a node went dirty. Kept as a small closed vocabulary rather than free text so the
# run summary can count them, and so a retry pass can decide what is even worth retrying:
# FETCH_FAILED and DEFERRED are, NOT_APPLICABLE never is.
FETCH_FAILED = "fetch_failed"        # the request was made and blew up (timeout, 5xx, parse error)
DEFERRED = "deferred_budget"         # never attempted -- the run's enrichment budget ran out
NOT_APPLICABLE = "not_applicable"    # the connector itself said it has nothing to ask for this
                                      # variant (e.g. dbSNP with no rsID cross-reference)


@dataclass
class RetryTicket:
    """One (node, connector) pair that did not get the data it was supposed to."""
    node_id: str
    connector: str
    reason: str                                  # FETCH_FAILED | DEFERRED | NOT_APPLICABLE
    detail: str = ""                             # exception text, or why it was deferred
    scope: str = "coordinates"                   # the build_query scope to re-issue under
    context: dict = field(default_factory=dict)  # what build_query needs (chrom/pos/ref/alt/rsid)
    query: dict | None = None                    # the resolved query, when we got that far
    expected_edges: list[dict] = field(default_factory=list)   # see expected_edge_shapes()
    state: str = PENDING
    attempts: int = 0
    first_seen: str = ""
    last_seen: str = ""
    gene: str | None = None                      # which gene's harvest pass produced this

    def key(self) -> tuple[str, str]:
        return (self.node_id, self.connector)

    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    def is_retryable(self) -> bool:
        return self.state == PENDING and self.reason in (FETCH_FAILED, DEFERRED)


# ---------------------------------------------------------------------------
# Expected edge shapes, derived from the ontology
# ---------------------------------------------------------------------------

_SUBTYPE_ROLE = {subtype: role for (role, subtype) in NODE_SUBTYPE_REGISTRY}


def expected_edge_shapes(descriptor) -> list[dict]:
    """The edge shapes this connector would have contributed, had it answered.

    Derived, not declared: the connector already tells us which node subtypes it
    provides (`adapter.capabilities()["provides"]` -- the same field the advisory
    completeness report reads), and the ontology already says which edges those subtypes
    are allowed to sit on either end of. Cross-referencing the two is the honest answer
    to "what is missing", and it stays correct automatically when a connector's
    capabilities change or the ontology grows -- a hand-written per-connector table
    would go stale silently, which is the failure mode this whole ledger exists to stop.

    RUNTIME_ONLY predicates are excluded: those are MHLA's investigation-scoped working
    state and no connector may ever emit one (forge_ir/verify.py check 9), so listing
    them as "missing" would be permanently, misleadingly unsatisfiable."""
    try:
        provides = set(descriptor.adapter.capabilities().get("provides", []))
    except Exception:
        return []

    shapes: list[dict] = []
    seen: set[tuple] = set()
    for combo in ALLOWED_EDGE_COMBINATIONS:
        if edge_storage_scope(combo.predicate) == StorageScope.RUNTIME_ONLY:
            continue
        for subtype, direction in ((combo.src_subtype, "out"), (combo.dst_subtype, "in")):
            # A None subtype in the combination is a wildcard across that role -- it
            # matches any provided subtype whose role lines up.
            if subtype is not None:
                if subtype not in provides:
                    continue
            else:
                role = combo.src_role if direction == "out" else combo.dst_role
                if not any(_SUBTYPE_ROLE.get(p) == role for p in provides):
                    continue
            shape = {
                "predicate": combo.predicate.value, "qualifier": combo.qualifier,
                "src": combo.src_subtype or f"{combo.src_role.value}:*",
                "dst": combo.dst_subtype or f"{combo.dst_role.value}:*",
                "provides": direction,   # which END of this edge the connector supplies
            }
            # Keyed WITHOUT `provides`: a connector supplying both ends of a shape (VEP
            # provides Transcript and Gene, so it sits on both sides of Transcript-PART_OF
            # ->Gene) still describes one missing edge, not two.
            fingerprint = (shape["predicate"], shape["qualifier"], shape["src"], shape["dst"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            shapes.append(shape)
    return shapes


# ---------------------------------------------------------------------------
# Marking a node
# ---------------------------------------------------------------------------

def mark_dirty(node: ForgeNode, connector: str, reason: str, detail: str = "") -> None:
    """Flags a node in place. Idempotent per (connector, reason): re-marking the same
    node for the same connector on a later pass updates the detail rather than growing
    the list forever, which matters because a Variant node can be re-marked once per
    retry attempt over many nights."""
    node.attributes["dirty"] = True
    reasons = node.attributes.setdefault("dirty_reasons", [])
    for existing in reasons:
        if existing.get("connector") == connector:
            existing["reason"] = reason
            existing["detail"] = detail[:300]
            return
    reasons.append({"connector": connector, "reason": reason, "detail": detail[:300]})


def clear_dirty(node: ForgeNode, connector: str) -> None:
    """Removes one connector's mark. The node stops being dirty only once NO connector
    is still pending on it -- a variant missing both gnomAD and VEP that gets its gnomAD
    data back is still, correctly, dirty."""
    reasons = [r for r in node.attributes.get("dirty_reasons", [])
               if r.get("connector") != connector]
    if reasons:
        node.attributes["dirty_reasons"] = reasons
    else:
        node.attributes.pop("dirty_reasons", None)
        node.attributes["dirty"] = False


def is_dirty(node: ForgeNode) -> bool:
    return bool(node.attributes.get("dirty"))


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

class DirtyLedger:
    """Append-only JSONL, one ticket per line, latest line per (node_id, connector) wins.

    Append-only rather than rewrite-in-place because an overnight run that gets killed
    mid-write must not be able to corrupt the ledger it spent nine hours building. A
    truncated final line is dropped on load (see `load`) and costs one ticket, not all
    of them. `compact()` collapses the history when it gets long, and is only ever
    called between runs, never during one.

    Thread-safe: the harvest's enrichment workers write concurrently."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self._tickets: dict[tuple[str, str], RetryTicket] = {}
        self._lock = threading.Lock()
        self._fh = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            if os.path.exists(path):
                self.load(path)
            self._fh = open(path, "a", encoding="utf-8")

    # -- reading -----------------------------------------------------------
    def load(self, path: str) -> int:
        dropped = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    dropped += 1   # a partial final line from a killed run -- see class docstring
                    continue
                ticket = RetryTicket(**raw)
                self._tickets[ticket.key()] = ticket
        return dropped

    def tickets(self) -> list[RetryTicket]:
        return list(self._tickets.values())

    def open_tickets(self) -> list[RetryTicket]:
        return [t for t in self._tickets.values() if t.is_open()]

    def retryable(self) -> list[RetryTicket]:
        return [t for t in self._tickets.values() if t.is_retryable()]

    def dirty_node_ids(self) -> set[str]:
        return {t.node_id for t in self._tickets.values() if t.is_open()}

    def summary(self) -> dict:
        """Counts by (connector, reason, state) -- what the run summary prints and what
        the visualisation shows, so "what did not make it in" is a first-class number
        rather than something you have to grep the log for."""
        by_state: dict[str, int] = {}
        by_connector: dict[str, dict[str, int]] = {}
        for t in self._tickets.values():
            by_state[t.state] = by_state.get(t.state, 0) + 1
            bucket = by_connector.setdefault(t.connector, {})
            label = t.reason if t.is_open() else t.state
            bucket[label] = bucket.get(label, 0) + 1
        return {"total": len(self._tickets), "open": len(self.open_tickets()),
                "by_state": by_state, "by_connector": by_connector}

    # -- writing -----------------------------------------------------------
    def record(self, ticket: RetryTicket) -> RetryTicket:
        """Files a ticket, merging with any existing one for the same (node, connector)
        so `attempts` and `first_seen` survive across runs."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            existing = self._tickets.get(ticket.key())
            if existing is not None:
                ticket.attempts = existing.attempts
                ticket.first_seen = existing.first_seen
            else:
                ticket.first_seen = now
            ticket.last_seen = now
            self._tickets[ticket.key()] = ticket
            self._append(ticket)
        return ticket

    def resolve(self, node_id: str, connector: str, state: str = RESOLVED,
                 detail: str = "") -> RetryTicket | None:
        """Closes a ticket after a successful retry. `state` distinguishes RESOLVED
        (real data arrived) from EMPTY (the source authoritatively has nothing) -- see
        the module docstring on why those must not collapse into each other."""
        with self._lock:
            ticket = self._tickets.get((node_id, connector))
            if ticket is None:
                return None
            ticket.state = state
            ticket.detail = detail or ticket.detail
            ticket.last_seen = datetime.now(timezone.utc).isoformat()
            self._append(ticket)
            return ticket

    def fail(self, node_id: str, connector: str, detail: str, max_attempts: int = 3
              ) -> RetryTicket | None:
        with self._lock:
            ticket = self._tickets.get((node_id, connector))
            if ticket is None:
                return None
            ticket.attempts += 1
            ticket.detail = detail[:300]
            ticket.last_seen = datetime.now(timezone.utc).isoformat()
            if ticket.attempts >= max_attempts:
                ticket.state = ABANDONED
            self._append(ticket)
            return ticket

    def _append(self, ticket: RetryTicket) -> None:
        if self._fh is None:
            return
        self._fh.write(json.dumps(asdict(ticket), separators=(",", ":")) + "\n")
        self._fh.flush()

    def compact(self) -> int:
        """Rewrites the file with one line per ticket. Between runs only."""
        if not self.path:
            return 0
        tmp = self.path + ".compacting"
        with open(tmp, "w", encoding="utf-8") as f:
            for ticket in self._tickets.values():
                f.write(json.dumps(asdict(ticket), separators=(",", ":")) + "\n")
        if self._fh is not None:
            self._fh.close()
        os.replace(tmp, self.path)
        self._fh = open(self.path, "a", encoding="utf-8")
        return len(self._tickets)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
