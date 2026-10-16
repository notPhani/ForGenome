import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _events():
    payload = json.loads((ROOT / "demo" / "graph_data.json").read_text(encoding="utf-8"))
    return payload["events"]


def test_hardcoded_gemma_guides_use_nodes_and_edges_from_demo_graph():
    guides = json.loads((ROOT / "demo" / "gemma_guides.json").read_text(encoding="utf-8"))
    events = _events()
    node_ids = {event["id"] for event in events if event.get("kind") == "node"}
    edge_orders = {event["order"] for event in events if event.get("kind") == "edge"}

    assert guides["model"] == "Gemma 4"
    assert len(guides["guides"]) >= 3
    for guide in guides["guides"]:
        assert guide["question"]
        assert guide["answer"]
        assert set(guide["nodes"]).issubset(node_ids)
        assert {edge["event_order"] for edge in guide["edges"]}.issubset(edge_orders)


def test_guides_are_cautious_about_clinical_interpretation():
    guides = json.loads((ROOT / "demo" / "gemma_guides.json").read_text(encoding="utf-8"))
    text = " ".join(guide["answer"] for guide in guides["guides"]).lower()
    assert "not a clinical" in text or "not an independent diagnosis" in text
    assert "does not" in text
