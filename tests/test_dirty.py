"""Tests for ingest/dirty.py -- the dirty-node marks and the retry ledger."""
import json
import os

from datetime import datetime, timezone

import connectors  # noqa: F401 -- registers every connector
from forge_ir.core import NodeRole, Origin
from forge_ir.graph import ForgeNode
from forge_ir.provenance import SourceProvenance
from ingest.dirty import (
    ABANDONED, DEFERRED, EMPTY, FETCH_FAILED, RESOLVED, DirtyLedger, RetryTicket,
    clear_dirty, expected_edge_shapes, is_dirty, mark_dirty,
)
from orchestration.connector_registry import get_connector


def _node(node_id: str = "SPDI:NC_000003.12:10149803:C:T") -> ForgeNode:
    return ForgeNode(
        id=node_id, role=NodeRole.ENTITY, subtype="Variant", attributes={},
        provenance=[SourceProvenance("ClinVar", "VCV000002218", "1", datetime.now(timezone.utc))],
        origin=Origin.SOURCE,
    )


def test_marking_a_node_dirty_records_the_connector_and_reason():
    node = _node()
    assert not is_dirty(node)
    mark_dirty(node, "gnomad", FETCH_FAILED, "HTTPError: 503")
    assert is_dirty(node)
    assert node.attributes["dirty_reasons"] == [
        {"connector": "gnomad", "reason": FETCH_FAILED, "detail": "HTTPError: 503"}]


def test_remarking_the_same_connector_updates_rather_than_appends():
    # A variant can be re-marked once per retry attempt across many nights; without this
    # the reason list would grow without bound on exactly the nodes that fail most.
    node = _node()
    mark_dirty(node, "gnomad", DEFERRED, "budget")
    mark_dirty(node, "gnomad", FETCH_FAILED, "HTTPError: 503")
    assert len(node.attributes["dirty_reasons"]) == 1
    assert node.attributes["dirty_reasons"][0]["reason"] == FETCH_FAILED


def test_a_node_stays_dirty_until_every_connector_is_cleared():
    node = _node()
    mark_dirty(node, "gnomad", FETCH_FAILED, "boom")
    mark_dirty(node, "ensembl_vep", FETCH_FAILED, "boom")
    clear_dirty(node, "gnomad")
    assert is_dirty(node), "still missing VEP -- clearing one connector must not clear the node"
    clear_dirty(node, "ensembl_vep")
    assert not is_dirty(node)


def test_expected_edge_shapes_are_derived_from_the_ontology_not_hardcoded():
    shapes = expected_edge_shapes(get_connector("gnomad"))
    # gnomAD provides PopulationFrequency (an OBSERVATION); the ontology allows
    # OBSERVATION --OBSERVES--> Variant, so that shape must be derivable without anything
    # in dirty.py naming gnomAD or PopulationFrequency.
    assert any(s["predicate"] == "OBSERVES" and s["dst"] == "Variant" for s in shapes)


def test_expected_edge_shapes_never_include_runtime_only_predicates():
    # ACTIVATED_BY is MHLA's investigation-scoped state; no connector may ever emit one
    # (forge_ir/verify.py check 9), so listing it as "missing" would be unsatisfiable.
    for descriptor in (get_connector("gnomad"), get_connector("ensembl_vep"),
                       get_connector("dbsnp")):
        assert all(s["predicate"] != "ACTIVATED_BY" for s in expected_edge_shapes(descriptor))


def test_expected_edge_shapes_deduplicate_when_a_connector_supplies_both_ends():
    shapes = expected_edge_shapes(get_connector("ensembl_vep"))
    keys = [(s["predicate"], s["qualifier"], s["src"], s["dst"]) for s in shapes]
    assert len(keys) == len(set(keys)), "one missing edge shape, not one per end supplied"


def test_ledger_round_trips_through_the_file(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    ledger = DirtyLedger(path)
    ledger.record(RetryTicket(node_id="v1", connector="gnomad", reason=FETCH_FAILED,
                               detail="503", query={"variant_id": "3-10149822-C-T"}))
    ledger.close()

    reloaded = DirtyLedger(path)
    tickets = reloaded.tickets()
    assert len(tickets) == 1
    assert tickets[0].query == {"variant_id": "3-10149822-C-T"}
    assert reloaded.dirty_node_ids() == {"v1"}
    reloaded.close()


def test_a_truncated_final_line_costs_one_ticket_not_the_whole_ledger(tmp_path):
    # The exact failure mode an overnight run has: killed mid-write.
    path = str(tmp_path / "ledger.jsonl")
    ledger = DirtyLedger(path)
    for i in range(3):
        ledger.record(RetryTicket(node_id=f"v{i}", connector="gnomad", reason=FETCH_FAILED))
    ledger.close()
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"node_id": "v9", "connect')   # a kill, mid-write

    reloaded = DirtyLedger(path)
    assert len(reloaded.tickets()) == 3
    reloaded.close()


def test_resolved_and_empty_are_tracked_separately(tmp_path):
    # "gnomAD has no record of this variant" and "gnomAD would not answer us" produce the
    # same missing edge and mean completely different things.
    ledger = DirtyLedger(str(tmp_path / "l.jsonl"))
    ledger.record(RetryTicket(node_id="v1", connector="gnomad", reason=FETCH_FAILED))
    ledger.record(RetryTicket(node_id="v2", connector="gnomad", reason=FETCH_FAILED))
    ledger.resolve("v1", "gnomad", RESOLVED)
    ledger.resolve("v2", "gnomad", EMPTY, "gnomAD returned variant: null")

    summary = ledger.summary()
    assert summary["by_state"] == {RESOLVED: 1, EMPTY: 1}
    assert summary["open"] == 0
    ledger.close()


def test_a_ticket_is_abandoned_only_after_max_attempts(tmp_path):
    ledger = DirtyLedger(str(tmp_path / "l.jsonl"))
    ledger.record(RetryTicket(node_id="v1", connector="gnomad", reason=FETCH_FAILED))
    for _ in range(2):
        ticket = ledger.fail("v1", "gnomad", "503", max_attempts=3)
        assert ticket.state != ABANDONED
        assert ticket.is_retryable()
    ticket = ledger.fail("v1", "gnomad", "503", max_attempts=3)
    assert ticket.state == ABANDONED
    assert not ticket.is_retryable()
    ledger.close()


def test_not_applicable_tickets_are_never_retried(tmp_path):
    # dbSNP with no rsID cross-reference has nothing to ask for -- retrying it forever
    # would be pure load for a permanently identical answer.
    from ingest.dirty import NOT_APPLICABLE
    ledger = DirtyLedger(str(tmp_path / "l.jsonl"))
    ledger.record(RetryTicket(node_id="v1", connector="dbsnp", reason=NOT_APPLICABLE))
    assert ledger.retryable() == []
    assert len(ledger.open_tickets()) == 1     # still visible, just not retryable
    ledger.close()


def test_attempts_and_first_seen_survive_across_runs(tmp_path):
    path = str(tmp_path / "l.jsonl")
    ledger = DirtyLedger(path)
    ledger.record(RetryTicket(node_id="v1", connector="gnomad", reason=FETCH_FAILED))
    ledger.fail("v1", "gnomad", "503", max_attempts=5)
    first_seen = ledger.tickets()[0].first_seen
    ledger.close()

    resumed = DirtyLedger(path)
    resumed.record(RetryTicket(node_id="v1", connector="gnomad", reason=FETCH_FAILED))
    ticket = resumed.tickets()[0]
    assert ticket.attempts == 1, "a re-filed ticket must not reset its attempt count"
    assert ticket.first_seen == first_seen
    resumed.close()


def test_compact_collapses_history_to_one_line_per_ticket(tmp_path):
    path = str(tmp_path / "l.jsonl")
    ledger = DirtyLedger(path)
    ledger.record(RetryTicket(node_id="v1", connector="gnomad", reason=FETCH_FAILED))
    for _ in range(4):
        ledger.fail("v1", "gnomad", "503", max_attempts=99)
    with open(path, encoding="utf-8") as f:
        assert len(f.readlines()) == 5      # append-only: one line per state change

    assert ledger.compact() == 1
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["attempts"] == 4
    ledger.close()
