"""Offline GitHub snapshot normalization with no local Git or dirty-tree input."""

from __future__ import annotations

from pathlib import PurePosixPath

from mtrp_hub.adapters.stage2_base import Stage2ExecutionContext
from mtrp_hub.domain.enums import ReadOperation, RepositoryState, ResourceType, SnapshotKind
from mtrp_hub.domain.models import ProjectCoordinate, SnapshotRef, SourceRef
from mtrp_hub.domain.models_v2 import EvidenceV2, ResourceV2
from mtrp_hub.domain.requests import ReadStep
from mtrp_hub.domain.requests_v2 import OperationResult, StepExecutionV2, StepResultV2, StepStatusV2
from mtrp_hub.execution import identity
from mtrp_hub.execution.envelope import ExecutionReceipt
from mtrp_hub.execution.models import ManifestStep, Stage2Operation
from mtrp_hub.execution.profile import ParsedGitHubFile, ResponseContractError


class GitHubPAdapter:
    def normalize(self, route_step: ReadStep, receipts: tuple[ExecutionReceipt, ...], context: Stage2ExecutionContext) -> StepExecutionV2:
        if route_step.coordinate is None:
            raise ValueError("P normalization requires a Registry coordinate")
        binding = context.config.github_p.binding_for(route_step.coordinate)
        manifest_steps = [step for step in context.manifest.steps if step.route_step_id == route_step.step_id]
        by_id = {receipt.step_id: receipt for receipt in receipts}
        list_step = next(step for step in manifest_steps if step.operation is Stage2Operation.GITHUB_LIST_BRANCHES)
        resolve_step = next(step for step in manifest_steps if step.operation is Stage2Operation.GITHUB_RESOLVE_REF)
        list_receipt = by_id[list_step.step_id]
        resolve_receipt = by_id[resolve_step.step_id]
        resources: list[ResourceV2] = []
        evidence_items: list[EvidenceV2] = []
        resources_by_operation = {step.step_id: 0 for step in manifest_steps}
        skipped_by_operation = {step.step_id: 0 for step in manifest_steps}
        failures: list[str] = []
        coordinate = route_step.coordinate
        if list_receipt.status is StepStatusV2.EMPTY:
            coordinate = coordinate.model_copy(update={"commit": None, "path": None, "repository_state": RepositoryState.EMPTY})
            return self._result(route_step, context, manifest_steps, by_id, StepStatusV2.EMPTY, "EMPTY_REPOSITORY", resources, evidence_items, resources_by_operation, skipped_by_operation, coordinate)
        if list_receipt.status is StepStatusV2.DEGRADED:
            failures.append(list_receipt.reason_code or "UNAVAILABLE")
        commit = resolve_receipt.binding_data.get("commit") if resolve_receipt.status is StepStatusV2.OK else None
        if resolve_receipt.status is not StepStatusV2.OK or not isinstance(commit, str):
            failures.append(resolve_receipt.reason_code or "REF_UNRESOLVED")
            commit = None
        if commit is not None:
            target = next((item for item in list_receipt.binding_data.get("branches", []) if item.get("name") == route_step.coordinate.ref), None)
            if target is not None and target.get("sha") != commit:
                failures.append("REF_SNAPSHOT_MISMATCH")
                commit = None
        if commit is not None:
            for step in manifest_steps:
                if step.operation is not Stage2Operation.GITHUB_READ_FILE:
                    if step.operation is Stage2Operation.GITHUB_READ_REPOSITORY_ROOT and by_id[step.step_id].status is StepStatusV2.DEGRADED:
                        failures.append(by_id[step.step_id].reason_code or "FILE_READ_FAILED")
                    continue
                receipt = by_id[step.step_id]
                if receipt.status is not StepStatusV2.OK or not receipt.calls:
                    skipped_by_operation[step.step_id] += 1
                    failures.append(receipt.reason_code or "FILE_READ_FAILED")
                    continue
                try:
                    parsed = context.profile.parse_file(receipt.calls[0].response, context.mode)
                    self._validate_file(step, parsed, context.config.github_p.max_file_bytes)
                    resource, evidence = self._canonical(route_step, step, context, binding.owner, binding.repo, commit, parsed)
                except (ResponseContractError, ValueError):
                    skipped_by_operation[step.step_id] += 1
                    failures.append("FILE_READ_FAILED")
                    continue
                resources.append(resource)
                evidence_items.append(evidence)
                resources_by_operation[step.step_id] += 1
        if resources:
            status = StepStatusV2.DEGRADED if failures else StepStatusV2.OK
            reason = "P_PARTIAL_FAILURE" if failures else None
        elif failures:
            status = StepStatusV2.DEGRADED
            reason = failures[0]
        else:
            status = StepStatusV2.EMPTY
            reason = "ADR_MISSING" if route_step.operation is ReadOperation.ADR_SEARCH else "P_EMPTY"
        result_coordinate = coordinate
        if commit is not None:
            result_coordinate = coordinate.model_copy(update={"commit": commit, "repository_state": RepositoryState.READY})
        return self._result(route_step, context, manifest_steps, by_id, status, reason, resources, evidence_items, resources_by_operation, skipped_by_operation, result_coordinate)

    @staticmethod
    def _validate_file(step: ManifestStep, parsed: ParsedGitHubFile, maximum: int) -> None:
        if len(parsed.content) > maximum:
            raise ResponseContractError()
        planned_path = step.arguments["path"]
        if parsed.path is not None and parsed.path != planned_path:
            raise ResponseContractError()

    def _canonical(
        self,
        route_step: ReadStep,
        operation_step: ManifestStep,
        context: Stage2ExecutionContext,
        owner: str,
        repo: str,
        commit: str,
        parsed: ParsedGitHubFile,
    ) -> tuple[ResourceV2, EvidenceV2]:
        path = operation_step.arguments["path"]
        snapshot = SnapshotRef(kind=SnapshotKind.GIT_COMMIT, value=commit)
        source = SourceRef(store=route_step.store, store_id=route_step.store_id, logical_key=f"{owner}/{repo}:{path}")
        coordinate = ProjectCoordinate(
            project_id=route_step.coordinate.project_id,
            repo_id=route_step.coordinate.repo_id,
            location_id=route_step.coordinate.location_id,
            location_kind=route_step.coordinate.location_kind,
            ref=route_step.coordinate.ref,
            commit=commit,
            path=path,
            workspace_id=None,
            repository_state=RepositoryState.READY,
        )
        evidence_id = identity.evidence_id(
            {
                "source": source,
                "snapshot": snapshot,
                "content_sha256": parsed.content_sha256,
                "request_id": context.manifest.request_id,
                "route_id": context.manifest.route_id,
                "execution_id": context.manifest.execution_id,
                "trace_id": context.trace_id,
                "step_id": route_step.step_id,
                "operation_id": operation_step.step_id,
            }
        )
        evidence = EvidenceV2(
            evidence_id=evidence_id,
            source_ref=source,
            snapshot_ref=snapshot,
            project_coordinate=coordinate,
            path=path,
            content_sha256=parsed.content_sha256,
            request_hash=context.manifest.request_hash,
            plan_hash=context.manifest.plan_hash,
            step_id=route_step.step_id,
            operation_id=operation_step.step_id,
            request_id=context.manifest.request_id,
            route_id=context.manifest.route_id,
            execution_id=context.manifest.execution_id,
            trace_id=context.trace_id,
            content_hash_scope=parsed.content_hash_scope,
            content_format=parsed.content_format,
        )
        resource_id = identity.resource_id(
            {
                "source": source,
                "snapshot": snapshot,
                "content_sha256": parsed.content_sha256,
                "step_id": route_step.step_id,
                "operation_id": operation_step.step_id,
                "trace_id": context.trace_id,
            }
        )
        resource = ResourceV2(
            resource_id=resource_id,
            resource_type=ResourceType.ADR if route_step.operation is ReadOperation.ADR_SEARCH else ResourceType.PROJECT_FILE,
            title=PurePosixPath(path).name,
            content_ref=identity.content_ref({"content_sha256": parsed.content_sha256, "source": source}),
            source_ref=source,
            snapshot_ref=snapshot,
            project_coordinate=coordinate,
            evidence_ids=(evidence_id,),
            score=None,
            score_source="not_provided",
            trace_id=context.trace_id,
            step_id=route_step.step_id,
            operation_id=operation_step.step_id,
        )
        return resource, evidence

    @staticmethod
    def _result(
        route_step: ReadStep,
        context: Stage2ExecutionContext,
        manifest_steps: list[ManifestStep],
        by_id: dict[str, ExecutionReceipt],
        status: StepStatusV2,
        reason: str | None,
        resources: list[ResourceV2],
        evidence: list[EvidenceV2],
        resources_by_operation: dict[str, int],
        skipped_by_operation: dict[str, int],
        coordinate: ProjectCoordinate,
    ) -> StepExecutionV2:
        operations = tuple(
            OperationResult(
                step_id=step.step_id,
                status=by_id[step.step_id].status,
                reason_code=by_id[step.step_id].reason_code,
                resource_count=resources_by_operation[step.step_id],
                skipped_count=skipped_by_operation[step.step_id],
                tool_call_count=len(by_id[step.step_id].calls),
                receipt_hash=by_id[step.step_id].receipt_hash,
            )
            for step in manifest_steps
        )
        result = StepResultV2(
            step_id=route_step.step_id,
            store=route_step.store,
            status=status,
            reason_code=reason,
            coordinate=coordinate,
            resource_ids=tuple(item.resource_id for item in resources),
            evidence_ids=tuple(item.evidence_id for item in evidence),
            resource_count=len(resources),
            evidence_count=len(evidence),
            request_id=context.manifest.request_id,
            route_id=context.manifest.route_id,
            execution_id=context.manifest.execution_id,
            trace_id=context.trace_id,
            operations=operations,
            skipped_count=sum(skipped_by_operation.values()),
        )
        return StepExecutionV2(step_result=result, resources=tuple(resources), evidence=tuple(evidence))


__all__ = ["GitHubPAdapter"]
