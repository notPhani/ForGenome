"""Tests for ingest/identity.py -- off-by-one SPDI normalization, tri-state
resolution, and genuine cross-run persistence (a fresh IdentityIndex instance
pointed at the same SQLite file must see what a prior instance wrote)."""
import tempfile
import os

from ingest.identity import IdentityIndex, normalize_gene_key, normalize_variant_key, resolve_identity


def test_off_by_one_spdi_normalization_matches_real_clinvar_value():
    # Real values: gnomAD variant_id "3-10149822-C-T" (1-based) must normalize to the
    # SAME SPDI ClinVar itself reports: "NC_000003.12:10149821:C:T" (0-based).
    namespace, key = normalize_variant_key(
        "gnomAD", "NC_000003.12", 10149822, "C", "T", position_is_1based=True,
    )
    assert namespace == "SPDI"
    assert key == "SPDI:NC_000003.12:10149821:C:T"


def test_clinvar_and_ensembl_already_0based_need_no_conversion():
    namespace, key = normalize_variant_key("ClinVar", "NC_000003.12", 10149821, "C", "T",
                                            position_is_1based=False)
    assert key == "SPDI:NC_000003.12:10149821:C:T"


def test_gnomad_and_clinvar_normalize_to_the_same_key():
    _, gnomad_key = normalize_variant_key("gnomAD", "NC_000003.12", 10149822, "C", "T", position_is_1based=True)
    _, clinvar_key = normalize_variant_key("ClinVar", "NC_000003.12", 10149821, "C", "T", position_is_1based=False)
    assert gnomad_key == clinvar_key


def test_resolve_identity_first_seen_is_merged():
    index = IdentityIndex()
    res = resolve_identity(index, "HGNC", "HGNC:12687", "HGNC:12687",
                            match_method="direct", confidence=1.0, resolver_version="v1", run_id="run1")
    assert res.status == "MERGED"
    assert res.canonical_id == "HGNC:12687"


def test_resolve_identity_same_key_same_id_stays_merged():
    index = IdentityIndex()
    resolve_identity(index, "HGNC", "HGNC:12687", "HGNC:12687", "direct", 1.0, "v1", "run1")
    res2 = resolve_identity(index, "HGNC", "HGNC:12687", "HGNC:12687", "direct", 1.0, "v1", "run2")
    assert res2.status == "MERGED"

    entry = index.lookup("HGNC", "HGNC:12687")
    assert entry.first_seen_run == "run1"
    assert entry.last_seen_run == "run2"  # updated on the second (cross-run) resolution


def test_resolve_identity_conflicting_id_high_confidence_represents():
    index = IdentityIndex()
    resolve_identity(index, "Ensembl", "ENSP1", "ENSP1", "direct", 1.0, "v1", "run1")
    res = resolve_identity(index, "Ensembl", "ENSP1", "UniProt:P40337", "xref_guess", 0.9, "v1", "run1")
    assert res.status == "REPRESENTS"
    assert res.represents_edge is not None
    assert res.represents_edge.confidence == 0.9
    assert res.represents_edge.match_method == "xref_guess"


def test_resolve_identity_conflicting_id_low_confidence_unresolved():
    index = IdentityIndex()
    resolve_identity(index, "Ensembl", "ENSP1", "ENSP1", "direct", 1.0, "v1", "run1")
    res = resolve_identity(index, "Ensembl", "ENSP1", "UniProt:P40337", "weak_guess", 0.3, "v1", "run1")
    assert res.status == "UNRESOLVED"
    assert res.canonical_id is None
    assert res.represents_edge is None


def test_identity_index_persists_across_process_boundaries():
    # A real, meaningful test: an IdentityIndex backed by a file (not :memory:) must let a
    # SECOND, independent instance see what a first one wrote -- this is the whole point
    # of making it SQLite-backed instead of an in-memory dict (review round 3's finding).
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "identity.db")

        index1 = IdentityIndex(db_path)
        resolve_identity(index1, "SPDI", "SPDI:x:1:C:T", "SPDI:x:1:C:T", "direct", 1.0, "v1", "run1")
        index1.close()

        index2 = IdentityIndex(db_path)
        entry = index2.lookup("SPDI", "SPDI:x:1:C:T")
        assert entry is not None
        assert entry.canonical_id == "SPDI:x:1:C:T"
        assert entry.first_seen_run == "run1"
        index2.close()
