"""Forge IR core types: enums, node-subtype registry, allowed-edge-combination
registry, canonical ID schemes.

Implements forge-ir-v3-schema.md §1, §2, §4, §5. One deliberate fix applied
here that the doc's prose agreed to but never wrote into the ALLOWED_EDGE_COMBINATIONS
code block: SUPPORTS edges carry a qualifier distinguishing "observation backs a
claim" from "observation satisfies a requirement" (forge-pipeline-v1.md review
round 3, point 4) -- keeping code and doc in sync rather than reproducing the gap.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class NodeRole(Enum):
    ENTITY = "ENTITY"
    OBSERVATION = "OBSERVATION"
    CLAIM = "CLAIM"
    RULE = "RULE"
    INVESTIGATION = "INVESTIGATION"


class EdgeType(Enum):
    RELATES_TO = "RELATES_TO"
    PART_OF = "PART_OF"
    REPRESENTS = "REPRESENTS"
    ASSERTS = "ASSERTS"
    OBSERVES = "OBSERVES"
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    SOURCED_FROM = "SOURCED_FROM"
    REQUIRES = "REQUIRES"
    APPLIES_TO = "APPLIES_TO"
    ACTIVATED_BY = "ACTIVATED_BY"


class Layer(Enum):
    CORE_BIOLOGY = "CORE_BIOLOGY"
    EVIDENCE = "EVIDENCE"
    OPERATIONAL = "OPERATIONAL"
    ADMINISTRATIVE = "ADMINISTRATIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # RUNTIME_ONLY roles -- layer is a persistence-tier
                                        # concept and doesn't mean anything for these


class StorageScope(Enum):
    PERSISTENT = "PERSISTENT"
    RUNTIME_ONLY = "RUNTIME_ONLY"


class Origin(Enum):
    SOURCE = "SOURCE"      # mirrors an external record
    DERIVED = "DERIVED"    # computed by Forge from other nodes/edges
    RUNTIME = "RUNTIME"    # created during an investigation, not persisted as knowledge


ROLE_STORAGE_SCOPE: dict[NodeRole, StorageScope] = {
    NodeRole.ENTITY: StorageScope.PERSISTENT,
    NodeRole.OBSERVATION: StorageScope.PERSISTENT,
    NodeRole.CLAIM: StorageScope.PERSISTENT,
    NodeRole.RULE: StorageScope.PERSISTENT,
    NodeRole.INVESTIGATION: StorageScope.RUNTIME_ONLY,
}

EDGE_STORAGE_SCOPE: dict[EdgeType, StorageScope] = {
    EdgeType.ACTIVATED_BY: StorageScope.RUNTIME_ONLY,
    # all other predicates default to PERSISTENT
}


def edge_storage_scope(predicate: EdgeType) -> StorageScope:
    return EDGE_STORAGE_SCOPE.get(predicate, StorageScope.PERSISTENT)


# ---------------------------------------------------------------------------
# Node subtype registry -- forge-ir-v3-schema.md §2
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeSubtypeEntry:
    role: NodeRole
    subtype: str
    layer: Layer
    canonical_id_scheme: str
    required_attributes: tuple[str, ...]
    confirmed: bool  # confirmed against a real fetched payload, vs. provisional/design-only


NODE_SUBTYPE_REGISTRY: dict[tuple[NodeRole, str], NodeSubtypeEntry] = {}


def _register(
    role: NodeRole,
    subtype: str,
    layer: Layer,
    canonical_id_scheme: str,
    required_attributes: list[str],
    confirmed: bool = True,
) -> None:
    NODE_SUBTYPE_REGISTRY[(role, subtype)] = NodeSubtypeEntry(
        role, subtype, layer, canonical_id_scheme, tuple(required_attributes), confirmed
    )


# ENTITY -- confirmed sources: Ensembl, ClinVar, gnomAD
_register(NodeRole.ENTITY, "Gene", Layer.CORE_BIOLOGY, "HGNC:{id}", ["symbol", "biotype"])
_register(NodeRole.ENTITY, "Transcript", Layer.CORE_BIOLOGY, "Ensembl:{ENST...}",
          ["gene_id", "is_canonical", "biotype"])
_register(NodeRole.ENTITY, "Exon", Layer.CORE_BIOLOGY, "Ensembl:{ENSE...}",
          ["transcript_id", "start", "end"])
_register(NodeRole.ENTITY, "Protein", Layer.CORE_BIOLOGY, "Ensembl:{ENSP...} | UniProt:{accession}",
          ["length"])
_register(NodeRole.ENTITY, "Variant", Layer.CORE_BIOLOGY, "SPDI:{seq}:{pos}:{ref}:{alt}",
          ["assembly", "ref", "alt"])
_register(NodeRole.ENTITY, "Disease", Layer.CORE_BIOLOGY, "MedGen:{CUI}", ["name"])
_register(NodeRole.ENTITY, "Submitter", Layer.ADMINISTRATIVE, "ClinVarOrg:{OrgID}",
          ["name", "organization_category"])
_register(NodeRole.ENTITY, "SourceManifest", Layer.ADMINISTRATIVE, "Source:{name}:{version}",
          ["retrieved_at"])  # confirmed=True (default) as of connectors/common.py::build_source_manifest_node
                              # -- real, wired, produced by every connector run, not design-only anymore

# OBSERVATION
_register(NodeRole.OBSERVATION, "PopulationFrequency", Layer.EVIDENCE,
          "derived: gnomAD:{variant_id}:{dataset}", ["ac", "an", "af", "dataset"])
_register(NodeRole.OBSERVATION, "ComputationalPrediction", Layer.EVIDENCE,
          "derived: {tool}:{variant_id}:{transcript_id}", ["tool", "score", "prediction"])
_register(NodeRole.OBSERVATION, "CaseObservation", Layer.EVIDENCE,
          "derived: {scv_accession}:observed_in:{index}", ["origin", "method_type"])
_register(NodeRole.OBSERVATION, "SegregationResult", Layer.EVIDENCE, "derived", [], confirmed=False)
_register(NodeRole.OBSERVATION, "FunctionalResult", Layer.EVIDENCE, "derived", [], confirmed=False)

# CLAIM
_register(NodeRole.CLAIM, "ClinicalAssertion", Layer.EVIDENCE, "ClinVar:{SCV accession}",
          ["review_status", "classification"])
_register(NodeRole.CLAIM, "AggregateClassification", Layer.EVIDENCE, "ClinVar:{RCV|VCV accession}",
          ["scope", "description", "review_status"])
_register(NodeRole.CLAIM, "ClinicalImpactClassification", Layer.EVIDENCE, "ClinVar:{RCV accession}:impact",
          ["tier"])
_register(NodeRole.CLAIM, "ExpertPanelClassification", Layer.EVIDENCE, "ClinGen:{agent id}",
          ["outcome"], confirmed=False)
_register(NodeRole.CLAIM, "Association", Layer.EVIDENCE, "derived", [], confirmed=False)

# RULE
_register(NodeRole.RULE, "ACMGCriterion", Layer.OPERATIONAL, "ClinGen:{guideline_id}:{label}:{version}",
          ["label", "base_strength", "default_strength", "evidence_category"])
_register(NodeRole.RULE, "Requirement", Layer.OPERATIONAL, "{criterion_id}:req:{ordinal}",
          ["constraints", "evidence_type"])

# INVESTIGATION -- RUNTIME_ONLY, never populated by connectors
_register(NodeRole.INVESTIGATION, "Query", Layer.NOT_APPLICABLE, "investigation:{uuid}",
          ["goal", "created_at"])
_register(NodeRole.INVESTIGATION, "RequirementState", Layer.NOT_APPLICABLE,
          "{investigation_id}:{requirement_id}",
          ["requirement_id", "status", "matched_node_ids", "result_strength", "transition_reason"])


def layer_for(role: NodeRole, subtype: str) -> Layer:
    key = (role, subtype)
    if key not in NODE_SUBTYPE_REGISTRY:
        raise ValueError(
            f"Unregistered (role, subtype): ({role.value}, {subtype!r}) -- "
            "not in NODE_SUBTYPE_REGISTRY. No implicit subtypes (forge-ir-v3-schema.md §2)."
        )
    return NODE_SUBTYPE_REGISTRY[key].layer


def storage_scope_for(role: NodeRole) -> StorageScope:
    return ROLE_STORAGE_SCOPE[role]


# ---------------------------------------------------------------------------
# Allowed edge combinations -- forge-ir-v3-schema.md §4
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EdgeCombination:
    src_role: NodeRole
    src_subtype: str | None  # None = wildcard, any subtype of src_role
    predicate: EdgeType
    qualifier: str | None    # None = bare predicate, no qualifier expected
    dst_role: NodeRole
    dst_subtype: str | None

    def matches(
        self,
        src_role: NodeRole,
        src_subtype: str,
        predicate: EdgeType,
        qualifier: str | None,
        dst_role: NodeRole,
        dst_subtype: str,
    ) -> bool:
        if self.src_role != src_role or self.dst_role != dst_role or self.predicate != predicate:
            return False
        if self.src_subtype is not None and self.src_subtype != src_subtype:
            return False
        if self.dst_subtype is not None and self.dst_subtype != dst_subtype:
            return False
        if self.qualifier is not None and self.qualifier != qualifier:
            return False
        return True


def _c(
    src_role: NodeRole,
    src_subtype: str | None,
    predicate: EdgeType,
    qualifier: str | None,
    dst_role: NodeRole,
    dst_subtype: str | None,
) -> EdgeCombination:
    return EdgeCombination(src_role, src_subtype, predicate, qualifier, dst_role, dst_subtype)


ALLOWED_EDGE_COMBINATIONS: list[EdgeCombination] = [
    # --- Core Biology structure ---
    _c(NodeRole.ENTITY, "Transcript", EdgeType.PART_OF, None, NodeRole.ENTITY, "Gene"),
    _c(NodeRole.ENTITY, "Exon", EdgeType.PART_OF, None, NodeRole.ENTITY, "Transcript"),
    _c(NodeRole.ENTITY, "Transcript", EdgeType.RELATES_TO, "encodes", NodeRole.ENTITY, "Protein"),
    _c(NodeRole.ENTITY, "Variant", EdgeType.RELATES_TO, "has_consequence", NodeRole.ENTITY, "Transcript"),
    _c(NodeRole.ENTITY, "Variant", EdgeType.RELATES_TO, "in_gene", NodeRole.ENTITY, "Gene"),

    # --- weak cross-source identity (never a merge) ---
    _c(NodeRole.ENTITY, None, EdgeType.REPRESENTS, None, NodeRole.ENTITY, None),

    # --- Evidence: Observations ---
    _c(NodeRole.OBSERVATION, None, EdgeType.OBSERVES, None, NodeRole.ENTITY, "Variant"),
    _c(NodeRole.OBSERVATION, "CaseObservation", EdgeType.SUPPORTS, "corroborates",
       NodeRole.CLAIM, "ClinicalAssertion"),
    _c(NodeRole.OBSERVATION, None, EdgeType.SUPPORTS, "satisfies_requirement",
       NodeRole.RULE, "Requirement"),

    # --- Evidence: Claims (SCV/RCV/VCV hierarchy -- RELATES_TO, never PART_OF) ---
    _c(NodeRole.CLAIM, "ClinicalAssertion", EdgeType.RELATES_TO, "contributes_to",
       NodeRole.CLAIM, "AggregateClassification"),
    _c(NodeRole.CLAIM, "AggregateClassification", EdgeType.RELATES_TO, "aggregates_into",
       NodeRole.CLAIM, "AggregateClassification"),  # RCV -> VCV
    _c(NodeRole.CLAIM, None, EdgeType.ASSERTS, None, NodeRole.ENTITY, "Variant"),
    _c(NodeRole.CLAIM, None, EdgeType.RELATES_TO, "for_condition", NodeRole.ENTITY, "Disease"),
    _c(NodeRole.CLAIM, "ClinicalAssertion", EdgeType.RELATES_TO, "submitted_by",
       NodeRole.ENTITY, "Submitter"),
    _c(NodeRole.CLAIM, None, EdgeType.SOURCED_FROM, None, NodeRole.ENTITY, "SourceManifest"),
    _c(NodeRole.CLAIM, None, EdgeType.CONTRADICTS, None, NodeRole.CLAIM, None),
    _c(NodeRole.CLAIM, None, EdgeType.RELATES_TO, "superseded_by", NodeRole.CLAIM, None),

    # --- Operational: Rules ---
    _c(NodeRole.RULE, "ACMGCriterion", EdgeType.APPLIES_TO, None, NodeRole.ENTITY, "Gene"),
    _c(NodeRole.RULE, "ACMGCriterion", EdgeType.REQUIRES, None, NodeRole.RULE, "Requirement"),
    _c(NodeRole.RULE, "ACMGCriterion", EdgeType.SUPPORTS, "applied_criterion",
       NodeRole.CLAIM, "ExpertPanelClassification"),
    _c(NodeRole.RULE, "ACMGCriterion", EdgeType.RELATES_TO, "superseded_by",
       NodeRole.RULE, "ACMGCriterion"),

    # --- Runtime only (storage_scope=RUNTIME_ONLY; never emitted by a connector) ---
    _c(NodeRole.ENTITY, None, EdgeType.ACTIVATED_BY, None, NodeRole.INVESTIGATION, "Query"),
    _c(NodeRole.OBSERVATION, None, EdgeType.ACTIVATED_BY, None, NodeRole.INVESTIGATION, "Query"),
    _c(NodeRole.CLAIM, None, EdgeType.ACTIVATED_BY, None, NodeRole.INVESTIGATION, "Query"),
    _c(NodeRole.RULE, None, EdgeType.ACTIVATED_BY, None, NodeRole.INVESTIGATION, "Query"),
]


def is_allowed_edge(
    src_role: NodeRole,
    src_subtype: str,
    predicate: EdgeType,
    qualifier: str | None,
    dst_role: NodeRole,
    dst_subtype: str,
) -> bool:
    return any(
        combo.matches(src_role, src_subtype, predicate, qualifier, dst_role, dst_subtype)
        for combo in ALLOWED_EDGE_COMBINATIONS
    )


# ---------------------------------------------------------------------------
# Canonical ID schemes -- forge-ir-v3-schema.md §5
# ---------------------------------------------------------------------------

def normalize_spdi_position_from_1based(one_based_position: int) -> int:
    """The real, confirmed off-by-one: gnomAD's 1-based variant_id position
    (e.g. "3-10149822-C-T") vs. SPDI's 0-based convention used by ClinVar/Ensembl
    (e.g. "NC_000003.12:10149821:C:T"). Centralized here so the conversion
    happens exactly once, never re-derived per connector (forge-pipeline-v1.md §4)."""
    return one_based_position - 1


def spdi_position_to_1based(spdi_position: int) -> int:
    """The reverse of `normalize_spdi_position_from_1based` -- used when a connector's
    QUERY needs 1-based coordinates (gnomAD's variant_id, Ensembl's region endpoint)
    built FROM an already-canonical SPDI position, not the other way around."""
    return spdi_position + 1


def variant_canonical_id(seq_accession: str, spdi_position: int, ref: str, alt: str) -> str:
    """`spdi_position` must already be 0-based SPDI convention -- callers with
    1-based coordinates (gnomAD) must call normalize_spdi_position_from_1based first."""
    return f"SPDI:{seq_accession}:{spdi_position}:{ref}:{alt}"


def gene_canonical_id(hgnc_id: str) -> str:
    return f"HGNC:{hgnc_id.removeprefix('HGNC:')}"


def transcript_canonical_id(ensembl_transcript_id: str) -> str:
    return f"Ensembl:{ensembl_transcript_id}"


def exon_canonical_id(ensembl_exon_id: str) -> str:
    return f"Ensembl:{ensembl_exon_id}"


def protein_canonical_id_ensembl(ensembl_protein_id: str) -> str:
    return f"Ensembl:{ensembl_protein_id}"


def protein_canonical_id_uniprot(accession: str) -> str:
    return f"UniProt:{accession}"


def disease_canonical_id(medgen_cui: str) -> str:
    return f"MedGen:{medgen_cui}"


def submitter_canonical_id(org_id: str) -> str:
    return f"ClinVarOrg:{org_id}"


def acmg_criterion_canonical_id(guideline_id: str, label: str, version: str) -> str:
    return f"ClinGen:{guideline_id}:{label}:{version}"


def requirement_canonical_id(criterion_id: str, ordinal: int) -> str:
    return f"{criterion_id}:req:{ordinal:02d}"


def source_manifest_canonical_id(source: str, dataset: str, version: str | None) -> str:
    """One SourceManifest node per (source, dataset, version) -- forge-pipeline-v1.md §5:
    "answers 'what source/version/configuration is this' -- one per source+release,
    relatively static." Every CLAIM this source+version produced SOURCED_FROM the same
    node, so this id must be stable across every call within one run (and across runs,
    for the same release)."""
    return f"Source:{source}:{dataset}:{version or 'unversioned'}"
