"""Requirement / RequirementState -- forge-ir-v3-schema.md §6.1, forge-pipeline-v1.md §12.

This is the actual control mechanism the whole pipeline exists to feed:
MHLA (later) decides what to activate by asking which Requirements are
UNSATISFIED, not by classifying nodes into layers. Getting the state machine
right here matters more than almost anything else in this codebase.
"""
from __future__ import annotations

import operator as _operator
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class EvaluationMode(Enum):
    MECHANICAL = "MECHANICAL"  # clean numeric/boolean predicate -- e.g. PM2's real AF threshold
    COMPOSITE = "COMPOSITE"    # AND/OR of mechanical sub-checks -- e.g. BP4's SpliceAI-and-VarSeak
    NARRATIVE = "NARRATIVE"    # prose decision tree, not executable as written -- e.g. PVS1, BS4
    HYBRID = "HYBRID"          # narrative with some embedded numeric sub-conditions


RequirementStatus = Literal[
    "UNSATISFIED", "EVALUATING", "SATISFIED", "CONTRADICTED", "STALE", "NEEDS_INTERPRETATION"
]

# States a MECHANICAL/COMPOSITE requirement can mechanically reach.
# NARRATIVE can only ever land on NEEDS_INTERPRETATION -- it cannot auto-satisfy or
# auto-contradict, by definition (forge-ir-v3-schema.md §6.1's evaluator-behavior table).
_MECHANICALLY_EVALUABLE = {EvaluationMode.MECHANICAL, EvaluationMode.COMPOSITE}

_OPERATORS = {
    "<=": _operator.le,
    "<": _operator.lt,
    ">=": _operator.ge,
    ">": _operator.gt,
    "==": _operator.eq,
}


@dataclass(frozen=True)
class Requirement:
    """One strength-tier of one ACMG criterion -- RULE subtype, per core.py's registry.
    Every applicable-or-not tier gets its own Requirement (forge-pipeline-v1.md §12);
    flattening to one Requirement per criterion was correct by accident for PM2 only."""
    criterion_id: str        # "ClinGen:GN078:PM2:1.1"
    requirement_id: str      # "ClinGen:GN078:PM2:1.1:req:01"
    ordinal: int
    strength: str            # "Supporting" | "Moderate" | "Strong" | "Very Strong" | "Stand Alone"
    applicability: str       # "Applicable" | "Not Applicable" | "Not Applicable for this VCEP"
    evaluation_mode: EvaluationMode
    constraints: dict | None  # populated only when evaluation_mode allows mechanical checking
    source_text: str


@dataclass
class RequirementState:
    requirement_id: str
    investigation_id: str
    status: RequirementStatus
    matched_node_ids: list[str] = field(default_factory=list)
    result_strength: str | None = None  # can differ from the matched Requirement's own `strength`
                                          # under a downgrade (real pattern: PVS1's documented
                                          # "10% downgrade to Moderate" -- N/A for VHL specifically,
                                          # but a real ACMG mechanism, not hypothetical)
    transition_reason: dict = field(default_factory=dict)


def _fallback_condition_met(fallback: dict, observation: dict) -> bool:
    condition = fallback["condition"]
    if condition == "ac_le_1":
        # The real PM2 case: gnomAD doesn't compute GroupMax Filtering AF for a singleton
        # (ac=1) at all -- ClinGen's own spec says PM2_Supporting may still apply then,
        # not that the result is inconclusive. Confirmed against the real fixture: our
        # VHL test variant is exactly this case (faf95.popmax is null, ac=1).
        return observation.get("ac", 0) <= 1
    raise ValueError(f"unknown fallback condition: {condition!r}")


def evaluate_mechanical_constraint(constraints: dict, observation: dict) -> tuple[bool, dict]:
    """Evaluates a Requirement's constraints against an observation's attribute dict
    (e.g. a PopulationFrequency observation's {af, ac, an, faf95_popmax, ...}), not a
    bare scalar -- PM2's real constraint needs more than one field: its primary metric
    (GroupMax Filtering AF) plus a documented fallback for when that metric doesn't
    exist at all. Modeling this as a single-scalar threshold check would silently give
    the wrong answer on our own real test variant (see the comment on `fallback_if_unavailable`
    below and _fallback_condition_met)."""
    metric = constraints["metric"]
    observed = observation.get(metric)

    if observed is None:
        fallback = constraints.get("fallback_if_unavailable")
        if fallback and _fallback_condition_met(fallback, observation):
            return True, {
                "constraint": f"{metric} unavailable -- fallback applied: {fallback['reason']}",
                "observed": observation,
            }
        return False, {"constraint": f"{metric} unavailable, no applicable fallback", "observed": observation}

    op_fn = _OPERATORS[constraints["operator"]]
    satisfied = op_fn(observed, constraints["value"])
    return satisfied, {
        "constraint": f"{metric} {constraints['operator']} {constraints['value']}",
        "observed": observed,
    }


def evaluate_requirement(requirement: Requirement, observation: dict | None, source: str | None = None,
                          source_version: str | None = None) -> tuple[RequirementStatus, dict]:
    """The evaluator-behavior table from forge-ir-v3-schema.md §6.1, as code.
    `observation` is the candidate evidence node's relevant attributes as a dict,
    or None when no candidate evidence was found/activated yet."""
    if requirement.evaluation_mode not in _MECHANICALLY_EVALUABLE:
        # NARRATIVE or HYBRID's narrative remainder -- cannot auto-satisfy, ever.
        return "NEEDS_INTERPRETATION", {
            "reason": f"evaluation_mode={requirement.evaluation_mode.value} cannot be "
                      "mechanically evaluated; needs structured interpretation or an LLM pass",
            "source_text": requirement.source_text,
        }

    if observation is None:
        return "UNSATISFIED", {"reason": "no candidate observation activated for this requirement"}

    if requirement.constraints is None:
        raise ValueError(
            f"Requirement {requirement.requirement_id!r} is {requirement.evaluation_mode.value} "
            "but has no constraints -- cannot mechanically evaluate"
        )

    satisfied, reason = evaluate_mechanical_constraint(requirement.constraints, observation)
    reason["source"] = source
    reason["source_version"] = source_version
    # UNSATISFIED means "no evidence yet" (handled above, observation is None).
    # Once a real observation exists and fails the check, that's positive evidence the
    # criterion does not apply -- CONTRADICTED, not "still waiting for proof."
    return ("SATISFIED" if satisfied else "CONTRADICTED"), reason
