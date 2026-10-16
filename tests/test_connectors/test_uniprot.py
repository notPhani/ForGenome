"""Tests against the REAL UniProt fixture (fixtures/uniprot_P40337.json, live-fetched
from https://rest.uniprot.org/uniprotkb/P40337.json -- VHL's real, confirmed UniProt
accession). Confirms the LOD discipline (172 real features kept as a compact 10-item
summary, never exploded) and the REPRESENTS edge to Ensembl's protein id -- the first
connector in this project to ever produce one.
"""
import json
import os

from forge_ir.core import EdgeType, NodeRole
from forge_ir.graph import ForgeEdge, ForgeNode
from connectors.uniprot import UniProtParser, entry_to_ir

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")


def _load():
    with open(os.path.join(FIXTURES, "uniprot_P40337.json")) as f:
        return json.load(f)


def test_parses_real_protein_metadata():
    entry = UniProtParser().parse(_load())
    assert entry.accession == "P40337"
    assert entry.gene_symbol == "VHL"
    assert entry.protein_name == "von Hippel-Lindau disease tumor suppressor"
    assert entry.sequence_length == 213   # real, confirmed real VHL protein length
    assert entry.mol_weight > 0


def test_real_feature_count_kept_compact_not_exploded_into_172_nodes():
    entry = UniProtParser().parse(_load())
    assert entry.feature_count == 172   # real, confirmed count
    assert len(entry.named_regions) <= 10   # LOD discipline: compact summary, not every feature

    results = entry_to_ir(entry)
    nodes = [r for r in results if isinstance(r, ForgeNode)]
    assert len(nodes) == 1   # ONE Protein node, not 172
    assert nodes[0].attributes["feature_count"] == 172
    assert len(nodes[0].attributes["named_regions"]) == len(entry.named_regions)


def test_protein_node_uses_the_uniprot_canonical_id_scheme():
    entry = UniProtParser().parse(_load())
    node = entry_to_ir(entry)[0]
    assert node.id == "UniProt:P40337"
    assert node.role == NodeRole.ENTITY
    assert node.subtype == "Protein"


def test_represents_edge_to_ensembl_protein_when_given():
    entry = UniProtParser().parse(_load())
    results = entry_to_ir(entry, represents_ensembl_protein_id="ENSP00000256474")
    edges = [r for r in results if isinstance(r, ForgeEdge)]
    assert len(edges) == 1
    edge = edges[0]
    assert edge.predicate == EdgeType.REPRESENTS
    assert edge.src == "UniProt:P40337"
    assert edge.dst == "Ensembl:ENSP00000256474"
    # REPRESENTS-only fields required together (forge_ir/graph.py's own construction check)
    assert edge.confidence == 1.0
    assert edge.match_method is not None
    assert edge.resolver_version is not None


def test_no_represents_edge_when_no_ensembl_protein_id_given():
    entry = UniProtParser().parse(_load())
    results = entry_to_ir(entry, represents_ensembl_protein_id=None)
    assert [r for r in results if isinstance(r, ForgeEdge)] == []


def test_represents_is_a_legal_edge_combination_per_the_frozen_ontology():
    from forge_ir.core import is_allowed_edge
    assert is_allowed_edge(NodeRole.ENTITY, "Protein", EdgeType.REPRESENTS, None,
                            NodeRole.ENTITY, "Protein")
