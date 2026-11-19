"""Stage 2 application orchestration with expected-recompile and Core barriers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from mtrp_hub.adapters.github import GitHubPAdapter
from mtrp_hub.adapters.lexiang import LexiangMAdapter
from mtrp_hub.adapters.obsidian import ObsidianAdapter
from mtrp_hub.adapters.stage2_base import Stage2ExecutionContext
from mtrp_hub.audit.sqlite_stage2 import Stage2DecisionLog
from mtrp_hub.canonical import sha256_json
from mtrp_hub.config.stage2 import LoadedStage2Config, Stage2ConfigLoader, Stage2Mode
from mtrp_hub.domain.enums import Intent, StoreKind
from mtrp_hub.domain.models_v2 import CanonicalResultV2
from mtrp_hub.domain.requests import RouteQueryRequest
from mtrp_hub.domain.requests_v2 import QueryResponseV2, StepExecutionV2, StepResultV2, StepStatusV2
from mtrp_hub.errors import MtrpError, internal_error
from mtrp_hub.execution.binding import BindingResolver
from mtrp_hub.execution.compiler import ManifestCompiler
from mtrp_hub.execution.envelope import (
    ContinuationOutput,
    CoreStepReceipt,
    EnvelopeVerifier,
    ExecutionEnvelope,
    envelope_with_receipts,
)
from mtrp_hub.execution.executor import WorkBuddyManifestExecutor
from mtrp_hub.execution.models import ExecutionManifest, ExecutionMode, ExecutorKind, Stage2RoutePlan
from mtrp_hub.execution.models import ManifestVerification
from mtrp_hub.execution.verifier import ManifestVerifier
from mtrp_hub.fixtures.stage2_schemas import Stage2Fixture
from mtrp_hub.fixtures.stage2_transport import Stage2FixtureTransport
from mtrp_hub.routing.planner import ReadPolicy
from mtrp_hub.security import RequestAccessGuard


@dataclass(frozen=True)
class CompiledStage2Request:
    config: LoadedStage2Config
    route: Stage2RoutePlan
    manifest: ExecutionManifest
    binding: BindingResolver
    verifier: ManifestVerifier


class ManifestService:
    """Generate and verify Manifests without Vault target I/O or external calls."""

    def __init__(
        self,
        *,
        config_loader: Stage2ConfigLoader | None = None,
        decision_log_factory: Callable[..., Stage2DecisionLog] | None = None,
    ) -> None:
        self._config_loader = config_loader or Stage2ConfigLoader()
        self._decision_log_factory = decision_log_factory or Stage2DecisionLog
        self._guard = RequestAccessGuard()

    def compile_request(self, request: RouteQueryRequest, mode: ExecutionMode | Stage2Mode) -> CompiledStage2Request:
        self._guard.validate_bootstrap(request)
        self._guard.validate_lexical_coordinates(request)
        config = self._config_loader.load(request.config_dir)
        coordinate = self._resolve_coordinate(request, config)
        access = self._guard.validate_registry_coordinate(request, coordinate)
        v1_plan = ReadPolicy(config.policy).create_plan(request, coordinate, config.stores, config.projects)
        if v1_plan.request_hash != access.request_hash:
            raise internal_error()
        compiler = ManifestCompiler(config)
        route = compiler.create_route(request, v1_plan, mode)
        manifest = compiler.compile(request, route)
        binding = BindingResolver(
            max_entry_reads=config.workbuddy_manifest.max_entry_reads,
            lexiang_timeout_seconds=config.lexiang_m.timeout_seconds,
            github_timeout_seconds=config.github_p.timeout_seconds,
        )
        verifier = ManifestVerifier(binding_resolver=binding)
        verifier.verify_intrinsic(manifest)
        return CompiledStage2Request(config, route, manifest, binding, verifier)

    def create(self, request: RouteQueryRequest, mode: ExecutionMode | Stage2Mode) -> ExecutionManifest:
        compiled = self.compile_request(request, mode)
        self._decision_log_factory(compiled.config.audit_path).record_plan(compiled.manifest, compiled.route)
        return compiled.manifest

    def verify(
        self,
        request: RouteQueryRequest,
        manifest: ExecutionManifest,
        mode: ExecutionMode | Stage2Mode,
    ) -> ManifestVerification:
        compiled = self.compile_request(request, mode)
        return compiled.verifier.verify_expected(manifest, compiled.manifest)

    @staticmethod
    def _resolve_coordinate(request: RouteQueryRequest, config: LoadedStage2Config):  # type: ignore[no-untyped-def]
        needs_p = request.intent in (Intent.PROJECT_CONTEXT, Intent.DECISION_HISTORY)
        if request.project_id is None and not needs_p:
            return None
        if request.project_id is None or request.repo_id is None:
            raise MtrpError("INVALID_INPUT", "Project and repo IDs are required.", field="project")
        coordinate = config.projects.resolve_default(request.project_id, request.repo_id)
        if request.workspace_id is not None:
            config.projects.resolve_workspace(request.project_id, request.repo_id, request.workspace_id)
            raise MtrpError("WORKSPACE_QUERY_DISABLED", "Workspace and local dirty query are disabled.", field="workspace")
        return coordinate


class QueryFinalizationService:
    """Verify a controlled envelope, execute Core barriers, and normalize Canonical v2."""

    def __init__(
        self,
        *,
        manifest_service: ManifestService | None = None,
        obsidian_factory: Callable[[], ObsidianAdapter] | None = None,
        decision_log_factory: Callable[..., Stage2DecisionLog] | None = None,
    ) -> None:
        self._manifest_service = manifest_service or ManifestService()
        self._obsidian_factory = obsidian_factory or ObsidianAdapter
        self._decision_log_factory = decision_log_factory or Stage2DecisionLog
        self._lexiang = LexiangMAdapter()
        self._github = GitHubPAdapter()

    def finalize(
        self,
        request: RouteQueryRequest,
        expected_manifest_hash: str,
        envelope: ExecutionEnvelope,
        mode: ExecutionMode | Stage2Mode | None = None,
    ) -> QueryResponseV2 | ContinuationOutput:
        selected_mode = envelope.mode if mode is None else mode
        compiled = self._manifest_service.compile_request(request, selected_mode)
        manifest = compiled.manifest
        compiled.verifier.verify_before_call(manifest, expected_manifest_hash)
        if envelope.manifest_hash != expected_manifest_hash:
            raise MtrpError("MANIFEST_CONTEXT_CHANGED", "The envelope belongs to a different Manifest.", field="manifest_hash")
        envelope_verifier = EnvelopeVerifier(binding=compiled.binding)
        envelope_verifier.verify_prefix(manifest, envelope)
        log = self._decision_log_factory(compiled.config.audit_path)
        run = log.continue_run(envelope) if envelope.core_receipts else log.begin_run(envelope)
        current = envelope
        step_ids = [step.step_id for step in manifest.steps]
        while current.next_step_id is not None:
            index = step_ids.index(current.next_step_id)
            step = manifest.steps[index]
            if step.executor is not ExecutorKind.CORE:
                break
            route_step = next(item for item in compiled.route.steps if item.step_id == step.route_step_id)
            context = Stage2ExecutionContext(compiled.config, manifest, route_step)
            execution = self._obsidian_factory().execute(step, context)
            core_values = {
                "step_id": step.step_id,
                "route_step_id": step.route_step_id,
                "execution_id": manifest.execution_id,
                "scope_sha256": step.arguments["scope_sha256"],
                "execution": execution,
                "receipt_hash": "0" * 64,
            }
            provisional = CoreStepReceipt.model_validate(core_values)
            core_receipt = CoreStepReceipt.model_validate({**core_values, "receipt_hash": sha256_json(provisional.hashed_payload())})
            next_step_id = step_ids[index + 1] if index + 1 < len(step_ids) else None
            current = envelope_with_receipts(
                current,
                receipts=current.receipts,
                core_receipts=current.core_receipts + (core_receipt,),
                next_step_id=next_step_id,
            )
        if current.next_step_id is not None:
            log.update_continuation(run, current)
            return ContinuationOutput(envelope=current)
        response = self._normalize_completed(compiled, current)
        log.finish_run(run, response, current, manifest=manifest)
        return response

    def _normalize_completed(self, compiled: CompiledStage2Request, envelope: ExecutionEnvelope) -> QueryResponseV2:
        manifest = compiled.manifest
        remote_by_route: dict[str, list] = {}
        for receipt in envelope.receipts:
            remote_by_route.setdefault(receipt.route_step_id, []).append(receipt)
        core_by_route = {receipt.route_step_id: receipt for receipt in envelope.core_receipts}
        executions: list[StepExecutionV2] = []
        for route_step in compiled.route.steps:
            context = Stage2ExecutionContext(compiled.config, manifest, route_step)
            if route_step.store is StoreKind.M:
                execution = self._lexiang.normalize(route_step, tuple(remote_by_route.get(route_step.step_id, [])), context)
            elif route_step.store is StoreKind.P:
                execution = self._github.normalize(route_step, tuple(remote_by_route.get(route_step.step_id, [])), context)
            else:
                core = core_by_route[route_step.step_id]
                execution = core.execution
                operations = tuple(
                    operation.model_copy(update={"receipt_hash": core.receipt_hash})
                    for operation in execution.step_result.operations
                )
                execution = execution.model_copy(
                    update={"step_result": execution.step_result.model_copy(update={"operations": operations})}
                )
            executions.append(execution)
        step_results: list[StepResultV2] = []
        for index, execution in enumerate(executions):
            next_step_id = compiled.route.steps[index + 1].step_id if index + 1 < len(compiled.route.steps) else None
            step_results.append(execution.step_result.model_copy(update={"next_step_id": next_step_id}))
        result = CanonicalResultV2.create(
            (resource for execution in executions for resource in execution.resources),
            (relation for execution in executions for relation in execution.relations),
            (evidence for execution in executions for evidence in execution.evidence),
        )
        status = self._final_status(step_results)
        local_mode = "obsidian-local" if manifest.mode is ExecutionMode.WORKBUDDY_MANIFEST and envelope.core_receipts else None
        return QueryResponseV2(
            schema_version="2",
            data_mode=manifest.mode,
            local_data_mode=local_mode,
            request_id=manifest.request_id,
            route_id=manifest.route_id,
            execution_id=manifest.execution_id,
            trace_id=manifest.trace_id(),
            manifest_hash=manifest.manifest_hash,
            status=status,
            step_results=tuple(step_results),
            result=result,
        )

    @staticmethod
    def _final_status(steps: list[StepResultV2]) -> StepStatusV2:
        if any(step.status is StepStatusV2.REJECTED for step in steps):
            return StepStatusV2.REJECTED
        if any(step.status is StepStatusV2.DEGRADED for step in steps):
            return StepStatusV2.DEGRADED
        if any(step.status is StepStatusV2.OK for step in steps):
            return StepStatusV2.OK
        return StepStatusV2.EMPTY

    def run_fixture(self, request: RouteQueryRequest, fixture: Stage2Fixture | None) -> QueryResponseV2:
        compiled = self._manifest_service.compile_request(request, ExecutionMode.FIXTURE)
        self._decision_log_factory(compiled.config.audit_path).record_plan(compiled.manifest, compiled.route)
        transport = Stage2FixtureTransport(compiled.config, fixture)
        executor = WorkBuddyManifestExecutor(
            transport,
            binding=compiled.binding,
            verifier=compiled.verifier,
        )
        envelope = executor.execute(compiled.manifest, expected_manifest_hash=compiled.manifest.manifest_hash)
        for _ in range(len(compiled.manifest.steps) + 1):
            output = self.finalize(request, compiled.manifest.manifest_hash, envelope, ExecutionMode.FIXTURE)
            if isinstance(output, QueryResponseV2):
                return output
            envelope = executor.execute(
                compiled.manifest,
                output.envelope,
                expected_manifest_hash=compiled.manifest.manifest_hash,
            )
        raise internal_error("EXECUTION_RUN_CONFLICT")


__all__ = ["CompiledStage2Request", "ManifestService", "QueryFinalizationService"]
