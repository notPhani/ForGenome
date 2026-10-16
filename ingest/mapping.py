"""MappingRule / EscapeHatch / MappingSpec -- forge-pipeline-v1.md §3.

Kept as Python dataclasses, not YAML (deliberate reversal of a proposal made mid-project --
ClinVar's real hierarchy and ClinGen's strength tiers need genuine control flow that YAML
expresses badly). MappingRule + source_path covers simple, even nested, field mapping
declaratively; anything genuinely branching (ClinVar's SCV<->RCV linking, ClinGen's
per-tier Requirement expansion) goes through a named, versioned, described EscapeHatch
instead of an anonymous callable, so the mapping stays auditable even where it isn't pure
data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Literal

from forge_ir.core import EdgeType, NodeRole, Origin
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.provenance import SourceProvenance

if TYPE_CHECKING:
    from ingest.compiler import CoverageReport


def resolve_source_path(obj: Any, path: str) -> Any:
    """Dot-path traversal into a SourceObject via plain attribute access (our SourceObjects
    are typed dataclasses, not raw dicts/JSON, so this is getattr-chaining, not JSONPath).
    Returns a scalar, a nested object, or a list -- callers iterate when it's a list."""
    if not path:
        return obj
    current = obj
    for part in path.split("."):
        if current is None:
            return None
        current = getattr(current, part)
    return current


@dataclass
class MappingRule:
    target_role: NodeRole | None          # set -> this rule emits a node; None -> emits an edge
    target_subtype: str | None
    predicate: EdgeType | None            # set -> this rule emits an edge from the record's
                                           # own primary node to a target built from id_template
    qualifier: str | None
    id_template: str                      # node rule: this node's own id. edge rule: the edge's
                                           # destination id. Formatted from the resolved item's
                                           # field_map values, e.g. "ClinVar:{accession}"
    source_path: str                      # dot-path into the SourceObject, e.g.
                                           # "observed_in", "" (the object itself)
    field_map: dict[str, str]             # source attribute name -> Forge attribute key
    action: Literal["EMIT", "DROP"] = "EMIT"
    drop_reason: str | None = None        # required when action == "DROP" (forge-pipeline-v1.md §14)


@dataclass
class EscapeHatch:
    name: str                              # "clinvar_scv_rcv_linking" -- never anonymous
    version: str                           # "v1" -- can be bug-fixed independently of the
                                            # overall MappingSpec, run reproducibility needs
                                            # "name@version", not just the name
    description: str                       # what it does, in one sentence, declaratively
    fn: Callable[[Any], list]              # SourceObject -> list[ForgeNode | ForgeEdge]


@dataclass
class MappingSpec:
    source: str
    record_type: str                       # "ClinicalAssertion" | "RCV" | "Gene" | ...
    rules: list[MappingRule] = field(default_factory=list)
    escape_hatches: list[EscapeHatch] = field(default_factory=list)


def _extract_fields(item: Any, field_map: dict[str, str]) -> dict:
    result = {}
    for source_attr, target_key in field_map.items():
        if isinstance(item, dict):
            result[target_key] = item.get(source_attr)
        else:
            result[target_key] = getattr(item, source_attr, None)
    return result


def apply_mapping_spec(
    obj: Any,
    spec: MappingSpec,
    primary_node_id: str | None,
    source: str,
    source_record_id: str,
    source_version: str | None,
    retrieved_at: datetime,
    coverage: "CoverageReport | None" = None,
) -> list[ForgeNode | ForgeEdge]:
    """Runs every declarative MappingRule, then every EscapeHatch, over one SourceObject.

    `coverage`, if given, is mutated in place -- forge-pipeline-v1.md §11's "mandatory
    per connector run" mapping-coverage report (declarative-vs-escape-hatch fraction).
    One `CoverageReport` accumulates across every `apply_mapping_spec()` call for a
    single connector's run (many records -> one report), which is why this mutates
    rather than returns a fresh report per call. Counting is at the NODE level only
    (matching `CoverageReport.declarative_node_count`/`escape_hatch_node_count`'s own
    shape) -- edges aren't separately counted here since a rule/hatch's node-vs-edge
    split isn't itself the question §11 asks; "how much of the graph came from
    declarative rules vs. named escape hatches" is."""
    results: list[ForgeNode | ForgeEdge] = []
    provenance = [SourceProvenance(source, source_record_id, source_version, retrieved_at)]
    if coverage is not None:
        coverage.records_parsed += 1

    for rule in spec.rules:
        if rule.action == "DROP":
            continue
        value = resolve_source_path(obj, rule.source_path)
        items = value if isinstance(value, list) else ([value] if value is not None else [])
        for item in items:
            fields = _extract_fields(item, rule.field_map)
            if rule.target_role is not None:
                node_id = rule.id_template.format(**fields)
                results.append(ForgeNode(
                    id=node_id, role=rule.target_role, subtype=rule.target_subtype,
                    attributes=fields, provenance=list(provenance), origin=Origin.SOURCE,
                ))
                if coverage is not None:
                    coverage.declarative_node_count += 1
            elif rule.predicate is not None:
                if primary_node_id is None:
                    raise ValueError(f"edge rule (predicate={rule.predicate}) needs a primary_node_id")
                dst_id = rule.id_template.format(**fields)
                results.append(ForgeEdge(
                    src=primary_node_id, predicate=rule.predicate, dst=dst_id, qualifier=rule.qualifier,
                    attributes=fields, provenance=list(provenance), origin=Origin.SOURCE,
                ))

    for hatch in spec.escape_hatches:
        hatch_results = hatch.fn(obj)
        results.extend(hatch_results)
        if coverage is not None:
            coverage.escape_hatch_node_count += sum(1 for r in hatch_results if isinstance(r, ForgeNode))

    if coverage is not None:
        coverage.records_emitted += len(results)

    return results
