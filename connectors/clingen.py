"""ClinGen connector -- Operational (the RULE layer). Real payload shape confirmed
against fixtures/clingen_vhl_vcep_GN078_v1.1.json: 28 criteria for VHL v1.1, each with
up to 5 strength tiers (Stand Alone/Very Strong/Strong/Moderate/Supporting).

Every tier gets its own Requirement, applicable or not (forge-pipeline-v1.md §12) --
PM2's real structure only has one applicable tier for VHL, but that's a property of
this gene, not a property of the schema; flattening to one Requirement per criterion
would be correct by accident.

`evaluation_mode` classification is honest about what's actually been checked against
real text: only PM2 (MECHANICAL) and BP4 (COMPOSITE) have been individually verified.
Everything else defaults to NARRATIVE until someone reads its real strengthDescriptor
text and confirms otherwise -- same discipline as the OBSERVATION/CLAIM subtype
registry's confirmed-vs-provisional split in forge_ir/core.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from connectors import http

from forge_ir.core import EdgeType, NodeRole, Origin, acmg_criterion_canonical_id, gene_canonical_id, \
    requirement_canonical_id
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.provenance import SourceProvenance
from forge_ir.requirement import EvaluationMode
from ingest.mapping import EscapeHatch, MappingSpec, apply_mapping_spec
from ingest.source_objects import SourceObjectBase
from orchestration.connector_registry import ConnectorDescriptor, Emission, register_connector

CSPEC_URL = "https://cspec.genome.network/cspec/SequenceVariantInterpretation/id"

# Individually confirmed against real cspec text -- see forge-pipeline-v1.md §12.
# Anything not listed here is NARRATIVE by default, not silently assumed evaluable.
_KNOWN_MECHANICAL: dict[str, EvaluationMode] = {
    "PM2": EvaluationMode.MECHANICAL,
    "BP4": EvaluationMode.COMPOSITE,
}

# PM2's real constraint, transcribed verbatim from the fixture's strengthDescriptor text
# (see forge-pipeline-v1.md §12 for the full derivation, including why the fallback
# clause is not optional -- our own VHL test variant IS the singleton case it covers).
_PM2_CONSTRAINTS = {
    "evidence_type": "PopulationFrequency",
    "metric": "faf95_popmax",
    "operator": "<=",
    "value": 0.00000156,
    "fallback_if_unavailable": {
        "condition": "ac_le_1",
        "reason": "GroupMax Filtering AF not calculated due to single variant present (ClinGen GN078 PM2 spec)",
    },
}


@dataclass
class ClinGenCriterion(SourceObjectBase):
    label: str                 # "PM2"
    guideline_id: str          # "GN078"
    version: str                # "1.1"
    gene_symbol: str
    evidence_category: str
    base_strength: str
    default_strength: str
    original_summary: str
    instructions_to_use: str
    strength_tiers: list[dict] = field(default_factory=list)  # raw strengthDescriptor entries


class ClinGenAdapter:
    def fetch(self, query: dict) -> Iterator[dict]:
        try:
            resp = http.get(f"{CSPEC_URL}/{query['spec_id']}")
            resp.raise_for_status()
            yield resp.json()
        except Exception:
            # CSPEC registry blocks external unauthenticated automated REST requests with 403.
            # Fall back to the verified bundled fixture for GN078 (VHL VCEP v1.1) when available.
            import json, os
            fixture_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "fixtures", "clingen_vhl_vcep_GN078_v1.1.json")
            if query.get("spec_id") == "GN078" and os.path.exists(fixture_path):
                with open(fixture_path, encoding="utf-8") as f:
                    yield json.load(f)
            else:
                raise

    def capabilities(self) -> dict:
        return {
            "scopes": ["gene"], "provides": ["ACMGCriterion", "Requirement"],
            "bulk": True, "live_query": True, "batch": False,
            "hydration_lods": [1, 2], "identity_types": ["ClinGen guideline_id"],
        }

    def manifest(self) -> dict:
        return {"source": "ClinGen", "dataset": "cspec registry", "schema_version": "1"}


class ClinGenParser:
    def parse(self, raw: dict, snapshot_ref: str = "") -> Iterator[ClinGenCriterion]:
        guideline_id = raw["data"]["entId"]
        version = str(raw["data"]["entContent"]["version"])
        for entry in raw["data"]["ld"]["CriteriaCode"]:
            c = entry["entContent"]
            gene_list = c.get("gene") or [""]
            yield ClinGenCriterion(
                source="ClinGen", record_type="CriteriaCode", source_record_id=entry["ldhId"],
                source_version=version, snapshot_ref=snapshot_ref, payload_hash=str(hash(str(c))),
                label=c["label"], guideline_id=guideline_id, version=version, gene_symbol=gene_list[0],
                evidence_category=c["evidenceCategory"], base_strength=c["baseStrength"],
                default_strength=c["defaultStrength"], original_summary=c.get("originalACMGSummary", ""),
                instructions_to_use=c.get("instructionsToUse") or "",
                strength_tiers=c.get("strengthDescriptor", []),
            )


def _criterion_escape_hatch(obj: ClinGenCriterion) -> list[ForgeNode | ForgeEdge]:
    """Expands every strength tier into its own Requirement (ordinal-numbered), applicable
    or not -- a plain field-mapping rule can't express "iterate this list and number the
    results," which is exactly the kind of thing escape hatches exist for."""
    now = datetime.now(timezone.utc)
    results: list[ForgeNode | ForgeEdge] = []

    criterion_id = acmg_criterion_canonical_id(obj.guideline_id, obj.label, obj.version)
    gene_id = gene_canonical_id("12687") if obj.gene_symbol == "VHL" else f"GeneSymbol:{obj.gene_symbol}"
    prov = [SourceProvenance("ClinGen", obj.source_record_id, obj.version, now)]

    evaluation_mode = _KNOWN_MECHANICAL.get(obj.label, EvaluationMode.NARRATIVE)

    results.append(ForgeNode(
        id=criterion_id, role=NodeRole.RULE, subtype="ACMGCriterion",
        attributes={
            "label": obj.label, "base_strength": obj.base_strength, "default_strength": obj.default_strength,
            "evidence_category": obj.evidence_category, "original_summary": obj.original_summary,
            "instructions_to_use": obj.instructions_to_use, "evaluation_mode": evaluation_mode.value,
        },
        provenance=prov, origin=Origin.SOURCE,
    ))
    results.append(ForgeEdge(
        src=criterion_id, predicate=EdgeType.APPLIES_TO, dst=gene_id, qualifier=None,
        attributes={}, provenance=prov, origin=Origin.SOURCE,
    ))

    ordinal = 0
    for tier in obj.strength_tiers:
        ordinal += 1
        req_id = requirement_canonical_id(criterion_id, ordinal)
        constraints = _PM2_CONSTRAINTS if obj.label == "PM2" else None
        results.append(ForgeNode(
            id=req_id, role=NodeRole.RULE, subtype="Requirement",
            attributes={
                "criterion_id": criterion_id, "ordinal": ordinal, "strength": tier.get("strength"),
                "applicability": tier.get("applicability"), "evaluation_mode": evaluation_mode.value,
                "constraints": constraints, "source_text": tier.get("text") or obj.original_summary,
            },
            provenance=prov, origin=Origin.SOURCE,
        ))
        results.append(ForgeEdge(
            src=criterion_id, predicate=EdgeType.REQUIRES, dst=req_id, qualifier=None,
            attributes={}, provenance=prov, origin=Origin.SOURCE,
        ))

    return results


CRITERIA_MAPPING_SPEC = MappingSpec(
    source="ClinGen", record_type="CriteriaCode", rules=[],
    escape_hatches=[EscapeHatch(
        name="clingen_strength_tier_expansion", version="v1",
        description="One ACMGCriterion RULE node + one Requirement per strength tier, applicable or not",
        fn=_criterion_escape_hatch,
    )],
)


# ---------------------------------------------------------------------------
# Connector registry -- see orchestration/connector_registry.py
# ---------------------------------------------------------------------------

# ClinGen VCEP specs are keyed by a spec id (e.g. "GN078" for VHL), not derivable from the
# gene symbol via any formula -- this is a lookup table, same pattern as
# orchestration/runner.py::KNOWN_GENE_HGNC. `context["clingen_spec_id"]` (an explicit
# run configuration) always wins when given; this is only the fallback for planning-only
# callers (e.g. the DAG-shape tests) that don't set one.
_KNOWN_GENE_SPEC: dict[str, str] = {"VHL": "GN078", "HGNC:12687": "GN078"}   # keyed by both the
                                    # raw gene symbol (unresolved/gap-fill anchors) and the
                                    # resolved HGNC canonical id (resident anchors) -- plan()
                                    # iterates resolution.canonical_anchors, which is whichever
                                    # form ScopeResolver produced for this anchor


def _build_query(scope: str, anchor: str, context: dict) -> dict | None:
    if scope != "gene":
        return None
    spec_id = context.get("clingen_spec_id") or _KNOWN_GENE_SPEC.get(anchor)
    return {"spec_id": spec_id} if spec_id else None


def _build_emission(raw: dict, context: dict) -> Emission:
    emission = Emission()
    for criterion in ClinGenParser().parse(raw):
        results = apply_mapping_spec(criterion, CRITERIA_MAPPING_SPEC, primary_node_id=None,
                                      source="ClinGen", source_record_id=criterion.source_record_id,
                                      source_version=criterion.version, retrieved_at=context["now"],
                                      coverage=context.get("coverage"))
        emission.add("ClinGen VCEP", results)
    return emission


register_connector(ConnectorDescriptor(
    name="clingen", group="operational", cadence="persistent",
    adapter=ClinGenAdapter(), build_query=_build_query, build_emission=_build_emission,
    feed_label="ClinGen VCEP",
    description="ACMG criteria + strength-tier Requirements (GET /cspec/.../id/{spec_id})",
))
