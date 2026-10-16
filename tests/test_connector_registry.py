"""Tests for orchestration/connector_registry.py -- the pluggable connector contract,
plus the real registry state after Stage H (7 live connectors + 6 honest stubs, 13
total -- comfortably past the "at least 10 connectors" target).
"""
import pytest

import connectors  # noqa: F401 -- registers everything
from orchestration.connector_registry import (
    ConnectorDescriptor, Emission, _unregister_for_tests, get_connector, is_registered,
    list_connectors, register_connector,
)


def test_registering_a_duplicate_name_raises():
    with pytest.raises(ValueError, match="already registered"):
        register_connector(ConnectorDescriptor(
            name="clinvar", group="evidence", cadence="ephemeral", adapter=object(),
            build_query=lambda *a: None, build_emission=lambda *a: None, feed_label="dup",
        ))


def test_get_connector_raises_a_helpful_error_for_an_unknown_name():
    with pytest.raises(KeyError, match="no connector registered"):
        get_connector("not_a_real_connector")


def test_registering_an_unknown_group_or_cadence_is_rejected():
    with pytest.raises(ValueError, match="unknown group"):
        register_connector(ConnectorDescriptor(
            name="_test_bad_group", group="not_a_real_group", cadence="ephemeral",
            adapter=object(), build_query=lambda *a: None, build_emission=lambda *a: None,
            feed_label="x",
        ))
    with pytest.raises(ValueError, match="unknown cadence"):
        register_connector(ConnectorDescriptor(
            name="_test_bad_cadence", group="evidence", cadence="not_a_real_cadence",
            adapter=object(), build_query=lambda *a: None, build_emission=lambda *a: None,
            feed_label="x",
        ))


def test_a_custom_connector_needs_no_core_file_edits_to_register():
    # The actual claim under test: register_connector() is the WHOLE contract. No import
    # of connector_registry.py, runner.py, or main.py needs to change for a new source
    # to become visible to list_connectors()/get_connector().
    register_connector(ConnectorDescriptor(
        name="_test_custom_connector", group="evidence", cadence="ephemeral",
        adapter=object(), build_query=lambda *a: None,
        build_emission=lambda *a: Emission(), feed_label="TestSource",
        requires=("clinvar",), description="a custom connector added purely by this test",
    ))
    try:
        assert is_registered("_test_custom_connector")
        assert get_connector("_test_custom_connector").feed_label == "TestSource"
        assert any(d.name == "_test_custom_connector" for d in list_connectors())
    finally:
        _unregister_for_tests("_test_custom_connector")


def test_the_real_registry_reaches_the_ten_connector_target():
    descriptors = list_connectors()
    assert len(descriptors) >= 10
    implemented = [d for d in descriptors if d.implemented]
    stubs = [d for d in descriptors if not d.implemented]
    assert len(implemented) >= 6   # Ensembl(x2)/ClinVar/gnomAD/dbSNP/UniProt/ClinGen
    assert len(stubs) >= 6         # OMIM/MONDO/PharmGKB/COSMIC/dbNSFP/ClinGen Gene-Disease


def test_every_stub_connector_fails_loudly_not_silently():
    # A stub must be honest about not working -- calling its fetch() has to raise, never
    # return fabricated data or silently no-op.
    for d in list_connectors():
        if d.implemented:
            continue
        with pytest.raises(NotImplementedError):
            list(d.adapter.fetch({}))


def test_every_stub_still_declares_real_capabilities_and_manifest():
    # Even unimplemented, a stub's adapter.capabilities()/.manifest() must work -- the
    # completeness report (analysis/completeness.py) calls these on every registered
    # connector, implemented or not.
    for d in list_connectors():
        if d.implemented:
            continue
        caps = d.adapter.capabilities()
        assert "provides" in caps
        manifest = d.adapter.manifest()
        assert manifest["source"]


def test_group_and_cadence_values_are_always_one_of_the_known_set():
    for d in list_connectors():
        assert d.group in ("core_biology", "operational", "evidence")
        assert d.cadence in ("persistent", "ephemeral")
