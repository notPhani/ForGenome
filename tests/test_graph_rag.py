from analysis.graph_rag import (
    ClaimAuditor,
    GraphRetriever,
    GraphSnapshot,
    MetapathGrammar,
    MetapathRule,
    plain_language_summary,
)


def _clinical_graph():
    return {
        "events": [
            {"kind": "node", "id": "V1", "subtype": "Variant", "label": "c.1A>G"},
            {"kind": "node", "id": "G1", "subtype": "Gene", "label": "GENE1"},
            {"kind": "node", "id": "P1", "subtype": "Phenotype", "label": "phenotype alpha"},
            {"kind": "node", "id": "A1", "subtype": "ACMGCriterion", "label": "PM2"},
            {"kind": "node", "id": "D1", "subtype": "Disease", "label": "Disease 1"},
            {"kind": "node", "id": "NOISE", "subtype": "Protein", "label": "unrelated protein"},
            {"kind": "edge", "src": "V1", "predicate": "LOCATED_IN", "qualifier": None, "dst": "G1",
             "provenance": [{"source": "Guide", "record_id": "r1", "version": "1"}]},
            {"kind": "edge", "src": "G1", "predicate": "ASSOCIATED_WITH", "qualifier": None, "dst": "P1",
             "provenance": [{"source": "Guide", "record_id": "r2", "version": "1"}]},
            {"kind": "edge", "src": "P1", "predicate": "SUPPORTS_CRITERION", "qualifier": None, "dst": "A1",
             "provenance": [{"source": "Guide", "record_id": "r3", "version": "1"}]},
            {"kind": "edge", "src": "A1", "predicate": "SUPPORTS", "qualifier": None, "dst": "D1",
             "provenance": [{"source": "Guide", "record_id": "r4", "version": "1"}]},
            {"kind": "edge", "src": "V1", "predicate": "RELATES_TO", "qualifier": "noise", "dst": "NOISE"},
        ]
    }


def _grammar():
    sequence = ("Variant", "Gene", "Phenotype", "ACMGCriterion", "Disease")
    rules = [
        MetapathRule("Variant", "LOCATED_IN", None, "Gene"),
        MetapathRule("Gene", "ASSOCIATED_WITH", None, "Phenotype"),
        MetapathRule("Phenotype", "SUPPORTS_CRITERION", None, "ACMGCriterion"),
        MetapathRule("ACMGCriterion", "SUPPORTS", None, "Disease"),
    ]
    return MetapathGrammar(rules, sequence=sequence, name="test-clinical")


def test_retrieval_enforces_metapath_and_excludes_noise():
    snapshot = GraphSnapshot.from_payload(_clinical_graph(), snapshot_id="fixture")
    evidence = GraphRetriever(_grammar()).retrieve(snapshot, ["V1"], token_budget=2000)
    assert evidence.paths
    assert evidence.paths[0].node_ids == ("V1", "G1", "P1", "A1", "D1")
    assert "NOISE" not in {n.id for n in evidence.nodes}
    assert "E000000" not in evidence.serialize()  # IDs are stable and one-based.


def test_gene_query_anchors_on_gene_record_not_annotation_text():
    payload = {"events": [
        {"kind": "node", "id": "G1", "subtype": "Gene", "label": "GENE1",
         "attributes": {"symbol": "GENE1"}},
        {"kind": "node", "id": "V1", "subtype": "Variant", "label": "variant",
         "attributes": {"condition": "GENE1-related disease"}},
    ]}
    snapshot = GraphSnapshot.from_payload(payload)
    assert [node.id for node in snapshot.match(["GENE1"], subtype="Gene")] == ["G1"]


def test_auditor_rejects_unknown_and_reversed_edges_but_accepts_traceable_claim():
    snapshot = GraphSnapshot.from_payload(_clinical_graph(), snapshot_id="fixture")
    grammar = _grammar()
    evidence = GraphRetriever(grammar).retrieve(snapshot, ["V1"], token_budget=2000)
    path = evidence.paths[0]
    source_ids = [evidence.edge_by_id[eid].source_ids[0] for eid in path.edge_ids]
    auditor = ClaimAuditor(evidence, grammar)
    accepted = auditor.audit({
        "claim_id": "C1", "text": "The supplied evidence follows the configured clinical path.",
        "claim_type": "graph_derived_inference", "supporting_edge_ids": list(path.edge_ids),
        "path_ids": [path.path_id], "source_ids": source_ids,
    })
    assert accepted.accepted
    unknown = auditor.audit({"claim_id": "C2", "text": "x", "claim_type": "direct_observation",
                             "supporting_edge_ids": ["E999999"]})
    assert unknown.code.startswith("UNKNOWN_EDGE")
    reversed_relation = auditor.audit({
        "claim_id": "C3", "text": "The supplied evidence links the gene to the variant.",
        "claim_type": "direct_observation", "supporting_edge_ids": [path.edge_ids[0]],
        "relation": {"src": "G1", "dst": "V1", "predicate": "LOCATED_IN"},
    })
    assert reversed_relation.code == "RELATION_MISMATCH"


def test_summary_explains_budget_limit_and_is_not_a_diagnosis():
    snapshot = GraphSnapshot.from_payload(_clinical_graph())
    evidence = GraphRetriever(_grammar()).retrieve(snapshot, ["V1"], token_budget=1)
    summary = plain_language_summary(evidence)
    assert "evidence map" in summary
    assert "diagnosis" in summary
    assert evidence.budget_exhausted
