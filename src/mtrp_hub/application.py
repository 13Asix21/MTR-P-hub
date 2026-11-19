"""RouteQueryService orchestration with access rejection before Fixture/Adapter access."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mtrp_hub.adapters.base import FakeExecutionContext, ReadOnlyAdapter
from mtrp_hub.adapters.fake import FakeAdapter
from mtrp_hub.audit.models import DecisionEventType, DecisionPayload, DecisionRecord, DecisionStep
from mtrp_hub.audit.sqlite import SQLiteDecisionLog
from mtrp_hub.canonical import sha256_json
from mtrp_hub.config.loader import ConfigLoader
from mtrp_hub.domain.enums import Intent, StepStatus, StoreKind
from mtrp_hub.domain.models import CanonicalResult, Evidence, ProjectCoordinate, Relation, Resource
from mtrp_hub.domain.requests import ReadPlan, RouteQueryRequest, RouteQueryResponse, StepResult
from mtrp_hub.errors import MtrpError, internal_error
from mtrp_hub.fixtures.loader import FixtureLoader, validate_fixture_for_plan
from mtrp_hub.registry.projects import ProjectRegistry
from mtrp_hub.routing.planner import ReadPolicy
from mtrp_hub.security import RequestAccessGuard


class RouteQueryService:
    """Application service implementing the exact validation and execution order."""

    def __init__(
        self,
        *,
        config_loader: ConfigLoader | None = None,
        fixture_loader: FixtureLoader | None = None,
        adapter_factory: Callable[[StoreKind], ReadOnlyAdapter] | None = None,
        decision_log_factory: Callable[[Path], SQLiteDecisionLog] | None = None,
    ) -> None:
        self._config_loader = config_loader or ConfigLoader()
        self._fixture_loader = fixture_loader or FixtureLoader()
        self._adapter_factory = adapter_factory or FakeAdapter
        self._decision_log_factory = decision_log_factory or SQLiteDecisionLog
        self._guard = RequestAccessGuard()

    def query(self, request: RouteQueryRequest) -> RouteQueryResponse:
        """Run bootstrap → lexical → Registry → plan → Fixture → Fake exactly once."""
        self._guard.validate_bootstrap(request)
        log = self._decision_log_factory(Path(request.config_dir) / ".mtrp" / "decision-log.sqlite3")
        events: list[DecisionRecord] = []
        audit_request_hash = self._guard.rejected_request_hash(request)
        try:
            self._guard.validate_lexical_coordinates(request)
            audit_request_hash = self._guard.successful_request_hash(request)
            loaded = self._config_loader.load(request.config_dir)
            coordinate = self._resolve_coordinate(request, loaded.projects)
            access = self._guard.validate_registry_coordinate(request, coordinate)
            plan = ReadPolicy(loaded.policy).create_plan(request, coordinate, loaded.stores, loaded.projects)
            if plan.request_hash != access.request_hash:
                raise internal_error()
            audit_request_hash = plan.request_hash
            events.append(self._plan_record(plan, access.request_hash))

            bundle = self._fixture_loader.load(request.fixture)
            validate_fixture_for_plan(bundle, plan)
            fixture_hash = sha256_json(bundle)
            adapters: dict[StoreKind, ReadOnlyAdapter] = {}
            context = FakeExecutionContext(
                request_hash=plan.request_hash,
                plan_hash=plan.plan_hash,
                fixture_hash=fixture_hash,
                access_decision=access,
            )
            resources: list[Resource] = []
            relations: list[Relation] = []
            evidence: list[Evidence] = []
            step_results: list[StepResult] = []
            for index, step in enumerate(plan.steps):
                if step.store not in adapters:
                    adapters[step.store] = self._adapter_factory(step.store)
                execution = adapters[step.store].execute(step, bundle, context)
                next_step_id = plan.steps[index + 1].step_id if index + 1 < len(plan.steps) else None
                step_result = execution.step_result.model_copy(update={"next_step_id": next_step_id})
                step_results.append(step_result)
                resources.extend(execution.resources)
                relations.extend(execution.relations)
                evidence.extend(execution.evidence)
                events.append(
                    DecisionRecord(
                        event_type=DecisionEventType.STEP_FINISHED,
                        request_hash=plan.request_hash,
                        plan_hash=plan.plan_hash,
                        status=step_result.status.value,
                        reason_code=step_result.reason_code.value if step_result.reason_code else None,
                        payload=DecisionPayload(
                            step_id=step.step_id,
                            store=step.store,
                            next_step_id=next_step_id,
                            resource_count=len(execution.resources),
                            evidence_count=len(execution.evidence),
                        ),
                    )
                )
            result = CanonicalResult.create(resources, relations, evidence)
            response = RouteQueryResponse(
                plan=plan,
                fixture_id=bundle.fixture_id,
                fixture_hash=fixture_hash,
                step_results=tuple(step_results),
                result=result,
                write_proposal=None,
            )
            final_status = self._final_status(step_results)
            events.append(
                DecisionRecord(
                    event_type=DecisionEventType.QUERY_FINISHED,
                    request_hash=plan.request_hash,
                    plan_hash=plan.plan_hash,
                    status=final_status,
                    payload=DecisionPayload(
                        fixture_id=bundle.fixture_id,
                        fixture_hash=fixture_hash,
                        resource_count=len(result.resources),
                        relation_count=len(result.relations),
                        evidence_count=len(result.evidence),
                        final_status=final_status,
                    ),
                )
            )
            log.append_many(events)
            return response
        except MtrpError as exc:
            if exc.error_code == "DECISION_LOG_WRITE_FAILED":
                raise
            self._record_rejection(log, events, audit_request_hash, request, exc)
            raise
        except Exception as exc:
            error = internal_error()
            self._record_rejection(log, events, audit_request_hash, request, error)
            raise error from exc

    def _resolve_coordinate(
        self,
        request: RouteQueryRequest,
        projects: ProjectRegistry,
    ) -> ProjectCoordinate | None:
        needs_p = request.intent in (Intent.PROJECT_CONTEXT, Intent.DECISION_HISTORY)
        if request.project_id is None and not needs_p:
            return None
        if request.project_id is None or request.repo_id is None:
            raise MtrpError("INVALID_INPUT", "Project and repo IDs are required.", field="project")
        coordinate = projects.resolve_default(request.project_id, request.repo_id)
        if request.workspace_id is not None:
            workspace = projects.resolve_workspace(request.project_id, request.repo_id, request.workspace_id)
            self._guard.validate_registry_coordinate(request, workspace)
            raise MtrpError(
                "WORKSPACE_QUERY_DISABLED",
                "Workspace query is disabled in stage 0+1.",
                field="workspace",
            )
        return coordinate

    @staticmethod
    def _plan_record(plan: ReadPlan, request_hash: str) -> DecisionRecord:
        return DecisionRecord(
            event_type=DecisionEventType.PLAN_CREATED,
            request_hash=request_hash,
            plan_hash=plan.plan_hash,
            status="planned",
            payload=DecisionPayload(
                intent=plan.intent.value,
                rule_version=plan.rule_version,
                policy_hash=plan.policy_hash,
                registry_hash=plan.registry_hash,
                plan_hash=plan.plan_hash,
                include_r=plan.include_r,
                coordinate=plan.project_coordinate,
                steps=tuple(
                    DecisionStep(
                        step_id=step.step_id,
                        store=step.store,
                        operation=step.operation.value,
                        view_tag=step.view_tag,
                        on_empty=step.on_empty.value,
                        on_unavailable=step.on_unavailable.value,
                    )
                    for step in plan.steps
                ),
            ),
        )

    @staticmethod
    def _final_status(results: list[StepResult]) -> str:
        if any(item.status is StepStatus.DEGRADED for item in results):
            return "degraded"
        if any(item.status is StepStatus.OK for item in results):
            return "ok"
        return "empty"

    @staticmethod
    def _record_rejection(
        log: SQLiteDecisionLog,
        events: list[DecisionRecord],
        request_hash: str,
        request: RouteQueryRequest,
        error: MtrpError,
    ) -> None:
        events.append(
            DecisionRecord(
                event_type=DecisionEventType.REQUEST_REJECTED,
                request_hash=request_hash,
                plan_hash=next((event.plan_hash for event in reversed(events) if event.plan_hash), None),
                status="rejected",
                reason_code=error.error_code,
                payload=DecisionPayload(
                    intent=request.intent.value,
                    error_code=error.error_code,
                    field=error.field,
                    workspace_supplied=request.workspace_id is not None,
                    final_status="rejected",
                ),
            )
        )
        log.append_many(events)
