"""Injects demo/graph_data.json into demo/template.html, producing the self-contained
demo/index.html that gets published.

The only transformation applied is truncating long attribute strings. At whole-family
scale the event log is dominated by ACMG interpretation prose on ClinicalAssertion nodes
(single comments run to thousands of characters), which nothing in the visualisation can
display anyway -- a 1,053-variant run drops from ~9.4 MB to well under half that. This is
a DISPLAY concern and lives here on purpose: demo/graph_data.json keeps full fidelity, and
so does the committed graph the statistics are computed over.
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_ATTR_CHARS = 200
DEFAULT_MAX_EVENTS = 11_000   # override: python scripts/build_demo_html.py <max_events>


def cap(events: list[dict], limit: int) -> tuple[list[dict], bool]:
    """Caps the replay at `limit` events. The committed-graph totals, the layer counts and
    the edge-type statistics all come from the run's own report and are unaffected -- only
    the number of formation steps the page animates changes, and the footer says so."""
    if len(events) <= limit:
        return events, False
    return events[:limit], True


def trim(events: list[dict]) -> tuple[list[dict], int]:
    trimmed = 0
    for event in events:
        attrs = event.get("attributes")
        if not attrs:
            continue
        for key, value in attrs.items():
            if isinstance(value, str) and len(value) > MAX_ATTR_CHARS:
                attrs[key] = value[:MAX_ATTR_CHARS] + f"… (+{len(value) - MAX_ATTR_CHARS} chars)"
                trimmed += 1
    return events, trimmed


with open(os.path.join(BASE, "demo", "graph_data.json"), encoding="utf-8") as f:
    data = json.load(f)

limit = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MAX_EVENTS
data["events"], trimmed_count = trim(data.get("events", []))
data["events"], capped = cap(data["events"], limit)
if capped:
    data.setdefault("stats", {})["events_truncated"] = True

with open(os.path.join(BASE, "demo", "template.html"), encoding="utf-8") as f:
    template = f.read()

out = template.replace("__GRAPH_DATA_JSON__", json.dumps(data, separators=(",", ":")))

out_path = os.path.join(BASE, "demo", "index.html")
with open(out_path, "w", encoding="utf-8") as f:
    f.write(out)

print(f"Wrote {out_path} ({len(out) / 1024:.0f} KB, "
      f"{len(data['events'])} events, {trimmed_count} attributes truncated for display)")
