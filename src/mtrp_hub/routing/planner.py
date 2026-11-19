"""Pure ReadPlan materialization with fixed R placement and terminal actions."""

from __future__ import annotations

from mtrp_hub.canonical import request_fingerprint, sha256_json
from mtrp_hub.domain.enums import EmptyAction, Intent, StoreKind
from mtrp_hub.domain.models import ProjectCoordinate
from mtrp_hub.domain.requests import ReadPlan, ReadStep, RouteQueryRequest
from mtrp_hub.errors import MtrpError
from mtrp_hub.registry.projects import ProjectRegistry
from mtrp_hub.registry.stores import StoreRegistry
from mtrp_hub.routing.policy import PolicyStep, RoutingPolicy


class ReadPolicy:
    """Create deterministic plans solely from validated policy and registries."""

    def __init__(self, policy: RoutingPolicy) -> None:
        self._policy = policy

    def create_plan(
        self,
        request: RouteQueryRequest,
        coordinate: ProjectCoordinate | None,
        stores: StoreRegistry,
        projects: ProjectRegistry,
    ) -> ReadPlan:
        """Materialize steps in v1 order, optionally appending R exactly once."""
        rule = self._policy.for_intent(request.intent)
        include_r = request.include_r or rule.implicit_r
        raw_steps = list(rule.steps)
        if include_r and all(step.store is not StoreKind.R for step in raw_steps):
            raw_steps.append(PolicyStep(store=StoreKind.R, operation=self._policy.optional_r.operation))
        request_hash = request_fingerprint(
            query=request.query,
            intent=request.intent.value,
            project_id=request.project_id,
            repo_id=request.repo_id,
            workspace_id=request.workspace_id,
            include_r=request.include_r,
            registered_ids=True,
        )
        steps = self._materialize_steps(tuple(raw_steps), coordinate, stores, projects, request)
        steps = self._set_terminal_actions(steps)
        registry_hash = sha256_json(
            {"stores": stores.fingerprint_payload(), "projects": projects.fingerprint_payload()}
        )
        plan = ReadPlan(
            schema_version="1",
            intent=request.intent,
            rule_version="v1",
            policy_hash=self._policy.fingerprint(),
            registry_hash=registry_hash,
            request_hash=request_hash,
            plan_hash="0" * 64,
            include_r=include_r,
            read_only=True,
            data_mode="fake",
            project_coordinate=coordinate,
            steps=steps,
            write_proposal=None,
        )
        return plan.model_copy(update={"plan_hash": sha256_json(plan.model_dump(mode="json", exclude={"plan_hash"}))})

    def _materialize_steps(
        self,
        raw_steps: tuple[PolicyStep, ...],
        coordinate: ProjectCoordinate | None,
        stores: StoreRegistry,
        projects: ProjectRegistry,
        request: RouteQueryRequest,
    ) -> tuple[ReadStep, ...]:
        result: list[ReadStep] = []
        for index, raw in enumerate(raw_steps, start=1):
            store = stores.get(raw.store)
            is_p = raw.store is StoreKind.P
            if is_p and coordinate is None:
                raise MtrpError("INVALID_INPUT", "P route requires a resolved coordinate.", field="project")
            adr_roots = projects.get_adr_paths(coordinate.project_id, coordinate.repo_id) if is_p and coordinate else ()
            result.append(
                ReadStep(
                    step_id=f"s{index:02d}-{raw.store.value.lower()}",
                    store=raw.store,
                    store_id=store.store_id,
                    operation=raw.operation,
                    view_tag=store.view_tag,
                    coordinate=coordinate if is_p else None,
                    adr_roots=adr_roots if is_p else (),
                    on_empty=EmptyAction.CONTINUE,
                    on_unavailable=EmptyAction.CONTINUE,
                    read_only=True,
                )
            )
        return tuple(result)

    def _set_terminal_actions(self, steps: tuple[ReadStep, ...]) -> tuple[ReadStep, ...]:
        """Make only the final step return_empty, including after R is appended."""
        if not steps:
            return ()
        return tuple(
            step.model_copy(
                update={
                    "on_empty": self._policy.terminal.on_empty if index == len(steps) - 1 else EmptyAction.CONTINUE,
                    "on_unavailable": self._policy.terminal.on_unavailable if index == len(steps) - 1 else EmptyAction.CONTINUE,
                }
            )
            for index, step in enumerate(steps)
        )
