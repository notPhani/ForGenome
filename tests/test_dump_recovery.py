"""A harvest dump must stay readable when the harvest is still writing it, or was killed.

Real failure this pins: reading a live harvest's `graph.jsonl.gz` raised
`zlib.error: invalid block type` and returned NOTHING -- not a truncated tail, the whole
file. gzip is block-compressed, so an incomplete final block fails in the decompressor
before any line reaches the JSON parser, which is why the existing partial-line guard did
not help. A nine-hour run that cannot be read until it finishes defeats the point of
streaming it to disk in the first place.
"""
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from build_viz_dataset import read_dump


def _write_dump(path, node_count):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for i in range(node_count):
            f.write(json.dumps({"kind": "node", "id": f"n{i}", "subtype": "Variant",
                                 "layer": "EVIDENCE"}) + "\n")
            f.write(json.dumps({"kind": "edge", "src": f"n{i}", "dst": "g",
                                 "predicate": "RELATES_TO"}) + "\n")


def test_a_complete_dump_reads_back_whole(tmp_path):
    path = str(tmp_path / "graph.jsonl.gz")
    _write_dump(path, 50)
    nodes, edges, dropped = read_dump(path)
    assert (len(nodes), len(edges), dropped) == (50, 50, 0)


def test_a_dump_truncated_mid_block_still_returns_what_was_written(tmp_path):
    path = str(tmp_path / "graph.jsonl.gz")
    _write_dump(path, 400)
    intact = os.path.getsize(path)
    with open(path, "r+b") as f:          # simulate the kill / concurrent-read case
        f.truncate(int(intact * 0.6))

    nodes, edges, dropped = read_dump(path)
    assert nodes, "a truncated gzip stream must not lose the entire dump"
    assert len(nodes) < 400, "and it should genuinely be missing the tail"
    assert dropped >= 1, "the loss must be reported, not swallowed"
    assert all(n["kind"] == "node" for n in nodes)


def test_a_dump_truncated_mid_line_costs_only_that_record(tmp_path):
    path = str(tmp_path / "graph.jsonl.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for i in range(10):
            f.write(json.dumps({"kind": "node", "id": f"n{i}", "subtype": "Variant",
                                 "layer": "EVIDENCE"}) + "\n")
        f.write('{"kind": "node", "id": "n10", "subty')   # killed mid-record
    nodes, _edges, dropped = read_dump(path)
    assert len(nodes) == 10
    assert dropped == 1
