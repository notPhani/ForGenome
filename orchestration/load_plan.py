"""LoadPlan / plan_load() / execute() -- forge-pipeline-v1.md §13.

`LoadPlan` is frozen and its task list a tuple, deliberately: once computed, a plan is
never mutated -- if requirements change mid-execution, a NEW plan is generated. This is
the opposite call from ForgeNode, which must stay mutable (forge-ir-v3-schema.md §6.3) --
different questions, different answers.

`plan_load()` does no I/O -- it only resolves scope and builds the task/dependency
graph, so "why did Forge fetch N records" is answerable by inspecting the returned
plan before anything runs. `execute()` is the only function that touches the network --
it didn't exist until this generalization: with only 4 connectors, each hand-wired
directly into orchestration/runner.py, there wasn't enough real variety to generalize
*from* (see docs/forge-implementation-and-findings.md Finding 9). Once every connector
is registered (orchestration/connector_registry.py) with its own `build_query`/
`build_emission`, walking `plan.task_tiers()` and calling each task's registered
connector generically is what `execute()` below actually does.

Today `execute()` drives the *persistent*, gene-scoped connectors (Core Biology,
Operational) -- run once, shared by every variant of the gene. The ephemeral,
per-variant evidence connectors (ClinVar, gnomAD, VEP, ...) go through
orchestration/runner.py's own harness instead, which has real batching/quarantine
logic (QUARANTINE one bad record vs. REJECT a whole fetch batch, per-variant scope
filtering) this simpler task-tier loop doesn't need to reimplement.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Literal

from ingest.compiler import CoverageReport
from orchestration.connector_planner import ConnectorPlanner, FetchTask, Policy
from orchestration.connector_registry import Emission, get_connector, tag_with_source_manifest
from orchestration.scope_resolver import ScopeResolver

PLANNER_VERSION = "v1"


@dataclass(frozen=True)
class LoadPlan:
    plan_id: str
    created_at: datetime
    planner_version: str
    anchor: str | tuple[str, ...]
    scope: str
    policy: Policy
    tasks: tuple[FetchTask, ...]
    dependencies: tuple[tuple[str, str, Literal["HARD", "SOFT"]], ...]

    def task_tiers(self) -> list[list[FetchTask]]:
        """Groups tasks into dependency tiers for sequential-safe execution: tier 0 has
        no HARD dependencies, tier 1 depends only on tier 0, etc. -- a simple topological
        sort. Real concurrency within a tier is a future optimization, not needed for
        this to be correct.

        Order within a tier is `self.tasks`' own order, not set-iteration order. That
        distinction is only cosmetic while nothing actually executes tasks in the
        returned order -- it stopped being cosmetic once `execute()` (below) started
        committing tier-by-tier, task-by-task: two tasks in the same tier (e.g. Ensembl
        structure and ClinGen, mutually independent) can still have a REAL one-way
        commit-order need even without a HARD dependency between them -- ClinGen's
        `APPLIES_TO -> Gene` edge needs Ensembl's Gene node to already be committed, a
        real referential-integrity failure caught by running this for real, not by any
        DAG-shape test (those only ever inspected the returned tiers, never iterated them)."""
        by_id = {t.task_id: t for t in self.tasks}
        hard_deps: dict[str, set[str]] = {t.task_id: set() for t in self.tasks}
        for task_id, depends_on, dep_type in self.dependencies:
            if dep_type == "HARD":
                hard_deps[task_id].add(depends_on)

        tiers: list[list[FetchTask]] = []
        placed: set[str] = set()
        remaining_ids = [t.task_id for t in self.tasks]
        while remaining_ids:
            tier_ids = [tid for tid in remaining_ids if hard_deps[tid] <= placed]
            if not tier_ids:
                raise ValueError(f"dependency cycle detected among: {remaining_ids}")
            tiers.append([by_id[tid] for tid in tier_ids])
            placed |= set(tier_ids)
            remaining_ids = [tid for tid in remaining_ids if tid not in placed]
        return tiers


def plan_load(
    anchor: str | list[str], scope: str, policy: Policy,
    scope_resolver: ScopeResolver, connector_planner: ConnectorPlanner,
) -> LoadPlan:
    resolution = scope_resolver.resolve(anchor, scope)
    tasks = connector_planner.plan(resolution, policy)
    deps = connector_planner.dependencies(tasks)
    return LoadPlan(
        plan_id=str(uuid.uuid4()), created_at=datetime.now(timezone.utc), planner_version=PLANNER_VERSION,
        anchor=tuple(resolution.canonical_anchors), scope=scope, policy=policy,
        tasks=tuple(tasks), dependencies=tuple(deps),
    )


def execute(
    plan: LoadPlan,
    context: dict,
    commit: Callable[[Emission, str, str], bool],
    fetch: Callable[[Any, dict], Iterator[Any]] | None = None,
    coverage_reports: dict[str, CoverageReport] | None = None,
) -> None:
    """Walks `plan.task_tiers()` in dependency order; for each task, looks up the task's
    connector in the registry, fetches (via `fetch(descriptor, query)` if given -- the
    hook `orchestration/runner.py` uses to substitute saved fixtures in `--offline` mode
    -- else the connector's own real `adapter.fetch()`), builds an `Emission` from each
    raw payload via the connector's own `build_emission`, and commits it.

    `context` is threaded into every `build_emission` call, extended per-task with that
    task's own `CoverageReport` (forge-pipeline-v1.md §11) under `context["coverage"]`
    when `coverage_reports` is given -- one report accumulates across every task for the
    same connector (many gene-scope fetches -> one report per connector), which is why
    it's looked up by `task.connector` rather than created fresh per task. `commit` is
    the caller's own commit function (orchestration/runner.py's, in every current use),
    so this stays agnostic to how atomicity/GraphStore/event-logging actually work."""
    for tier in plan.task_tiers():
        for task in tier:
            descriptor = get_connector(task.connector)
            raw_iter = fetch(descriptor, task.query) if fetch is not None else descriptor.adapter.fetch(task.query)
            task_context = context
            report = None
            if coverage_reports is not None:
                report = coverage_reports.setdefault(task.connector, CoverageReport(source=task.connector))
                task_context = {**context, "coverage": report}
            for raw in raw_iter:
                if report is not None:
                    report.records_read += 1
                emission = descriptor.build_emission(raw, task_context)
                tag_with_source_manifest(emission, descriptor, context.get("now") or datetime.now(timezone.utc))
                commit(emission, task.connector, task.task_id)
