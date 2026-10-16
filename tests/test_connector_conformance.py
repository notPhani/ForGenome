"""The connector conformance check (scripts/verify_connectors.py) as a test.

Same seven checks, same real saved fixtures, no network. The script exists to be run in
front of people; this exists so the checks cannot quietly rot between demos.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import connectors  # noqa: F401 -- registers every connector
from orchestration.connector_registry import list_connectors
from verify_connectors import OK, check


@pytest.mark.parametrize("descriptor", list_connectors(), ids=lambda d: d.name)
def test_connector_passes_conformance(descriptor):
    result = check(descriptor, live=False)
    failures = {k: v for k, v in result.checks.items() if v != OK and v.strip()}
    assert result.passed(), f"{descriptor.name} failed {list(failures)}: {'; '.join(result.notes)}"


def test_every_implemented_connector_builds_a_real_graph():
    """Not just "does not crash" -- it has to produce actual nodes from a real payload."""
    for d in list_connectors():
        if not d.implemented:
            continue
        result = check(d, live=False)
        assert result.nodes, f"{d.name} produced no nodes from its saved fixture"
        assert result.checks["VERIFIED"] == OK, f"{d.name} emitted nodes that fail verify()"


def test_every_stub_refuses_loudly():
    for d in list_connectors():
        if d.implemented:
            continue
        with pytest.raises(NotImplementedError):
            list(d.adapter.fetch({}))
