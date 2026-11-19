"""Offline Lexiang receipt normalization; no connector, credential, or network code."""

from __future__ import annotations

from mtrp_hub.adapters.stage2_base import Stage2ExecutionContext
from mtrp_hub.canonical import sha256_bytes
from mtrp_hub.domain.enums import ResourceType, SnapshotKind
from mtrp_hub.domain.models import SnapshotRef, SourceRef
from mtrp_hub.domain.models_v2 import EvidenceV2, ResourceV2
from mtrp_hub.domain.requests import ReadStep
from mtrp_hub.domain.requests_v2 import OperationResult, StepExecutionV2, StepResultV2, StepStatusV2
from mtrp_hub.execution import identity
from mtrp_hub.execution.envelope import ExecutionReceipt
from mtrp_hub.execution.models import EntryCandidate, ManifestStep, Stage2Operation
from mtrp_hub.execution.profile import ResponseContractError


class LexiangMAdapter:
    def normalize(self, route_step: ReadStep, receipts: tuple[ExecutionReceipt, ...], context: Stage2ExecutionContext) -> StepExecutionV2:
        manifest_steps = [step for step in context.manifest.steps if step.route_step_id == route_step.step_id]
        by_id = {receipt.step_id: receipt for receipt in receipts}
        resources: list[ResourceV2] = []
        evidence_items: list[EvidenceV2] = []
        resources_by_operation: dict[str, int] = {step.step_id: 0 for step in manifest_steps}
        skipped_by_operation: dict[str, int] = {step.step_id: 0 for step in manifest_steps}
        failures: list[str] = []
        had_candidates = False
        for step in manifest_steps:
            receipt = by_id[step.step_id]
            if step.operation is Stage2Operation.LEXIANG_RESOLVE_SPACE:
                if receipt.status is not StepStatusV2.OK:
                    failures.append(receipt.reason_code or "UNAVAILABLE")
                continue
            if step.operation not in (Stage2Operation.LEXIANG_SEARCH_TEXT, Stage2Operation.LEXIANG_SEARCH_VECTOR):
                continue
            if receipt.status is StepStatusV2.DEGRADED:
                failures.append(receipt.reason_code or "UNAVAILABLE")
                continue
            read_step = next(
                item for item in manifest_steps
                if item.operation is Stage2Operation.LEXIANG_READ_ENTRIES
                and item.arguments["entry_reads"].get("from_step") == step.step_id
            )
            read_receipt = by_id[read_step.step_id]
            try:
                candidates, _ = context.profile.parse_search(
                    receipt.calls[0].response,
                    step.operation,
                    context.mode,
                    limit=step.arguments["limit"],
                )
            except (ResponseContractError, IndexError):
                failures.append("RESPONSE_CONTRACT_INVALID")
                continue
            if not candidates:
                continue
            had_candidates = True
            for index, candidate in enumerate(candidates):
                pair = read_receipt.calls[index * 2 : index * 2 + 2]
                if len(pair) != 2 or any(call.status is not StepStatusV2.OK for call in pair):
                    skipped_by_operation[read_step.step_id] += 1
                    continue
                try:
                    metadata = context.profile.parse_entry_meta(
                        pair[0].response,
                        entry_id=candidate.entry_id,
                        space_id=context.config.lexiang_m.space_id,
                        mode=context.mode,
                    )
                    content_format, content_text = context.profile.parse_entry_content(
                        pair[1].response,
                        entry_id=candidate.entry_id,
                        mode=context.mode,
                    )
                    content_bytes = content_text.encode("utf-8")
                    if len(content_bytes) > context.config.lexiang_m.max_entry_bytes:
                        raise ResponseContractError()
                    content_sha256 = sha256_bytes(content_bytes)
                    provider_hash = metadata.get("provider_content_sha256") or candidate.provider_content_sha256
                    if provider_hash is not None and provider_hash != content_sha256:
                        raise ResponseContractError()
                    resource, evidence = self._canonical(
                        route_step,
                        read_step,
                        context,
                        candidate,
                        metadata,
                        content_format,
                        content_sha256,
                    )
                except (ResponseContractError, ValueError):
                    skipped_by_operation[read_step.step_id] += 1
                    continue
                resources.append(resource)
                evidence_items.append(evidence)
                resources_by_operation[read_step.step_id] += 1
            if read_receipt.status is StepStatusV2.DEGRADED or skipped_by_operation[read_step.step_id]:
                failures.append("M_ENTRY_SKIPPED")
        operations = tuple(
            OperationResult(
                step_id=step.step_id,
                status=by_id[step.step_id].status,
                reason_code=by_id[step.step_id].reason_code,
                resource_count=resources_by_operation[step.step_id],
                skipped_count=skipped_by_operation[step.step_id],
                tool_call_count=len(by_id[step.step_id].calls),
                receipt_hash=by_id[step.step_id].receipt_hash,
                score_available_count=int(by_id[step.step_id].binding_data.get("score_available_count", 0)),
            )
            for step in manifest_steps
        )
        meaningful_failures = [reason for reason in failures if reason not in ("M_TEXT_EMPTY", "M_VECTOR_EMPTY", "M_EMPTY")]
        if resources:
            status = StepStatusV2.DEGRADED if meaningful_failures else StepStatusV2.OK
            reason = (
                "M_ENTRY_SKIPPED"
                if meaningful_failures and sum(skipped_by_operation.values())
                else meaningful_failures[0] if meaningful_failures else None
            )
        elif meaningful_failures:
            status = StepStatusV2.DEGRADED
            reason = "M_ENTRIES_INVALID" if had_candidates and all(reason == "M_ENTRY_SKIPPED" for reason in meaningful_failures) else meaningful_failures[0]
        else:
            status = StepStatusV2.EMPTY
            reason = "M_EMPTY"
        result = StepResultV2(
            step_id=route_step.step_id,
            store=route_step.store,
            status=status,
            reason_code=reason,
            resource_ids=tuple(item.resource_id for item in resources),
            evidence_ids=tuple(item.evidence_id for item in evidence_items),
            resource_count=len(resources),
            evidence_count=len(evidence_items),
            request_id=context.manifest.request_id,
            route_id=context.manifest.route_id,
            execution_id=context.manifest.execution_id,
            trace_id=context.trace_id,
            operations=operations,
            skipped_count=sum(skipped_by_operation.values()),
        )
        return StepExecutionV2(step_result=result, resources=tuple(resources), evidence=tuple(evidence_items))

    def _canonical(
        self,
        route_step: ReadStep,
        operation_step: ManifestStep,
        context: Stage2ExecutionContext,
        candidate: EntryCandidate,
        metadata: dict,
        content_format: str,
        content_sha256: str,
    ) -> tuple[ResourceV2, EvidenceV2]:
        version = metadata.get("version") or candidate.version
        revision = metadata.get("revision") or candidate.revision
        provider_hash = metadata.get("provider_content_sha256") or candidate.provider_content_sha256
        if provider_hash is not None:
            snapshot = SnapshotRef(kind=SnapshotKind.CONTENT_SHA256, value=provider_hash)
        elif version or revision:
            snapshot = SnapshotRef(kind=SnapshotKind.ENTRY_VERSION, value=str(version or revision))
        else:
            snapshot = None
        source = SourceRef(
            store=route_step.store,
            store_id=route_step.store_id,
            logical_key=f"{context.config.lexiang_m.team_id}/{context.config.lexiang_m.space_id}/{candidate.entry_id}",
        )
        evidence_id = identity.evidence_id(
            {
                "source": source,
                "snapshot": snapshot,
                "content_sha256": content_sha256,
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
            entry_id=candidate.entry_id,
            content_sha256=content_sha256,
            request_hash=context.manifest.request_hash,
            plan_hash=context.manifest.plan_hash,
            step_id=route_step.step_id,
            operation_id=operation_step.step_id,
            request_id=context.manifest.request_id,
            route_id=context.manifest.route_id,
            execution_id=context.manifest.execution_id,
            trace_id=context.trace_id,
            content_hash_scope="response_utf8",
            content_format=content_format,
        )
        resource_id = identity.resource_id(
            {
                "source": source,
                "snapshot": snapshot,
                "content_sha256": content_sha256,
                "step_id": route_step.step_id,
                "operation_id": operation_step.step_id,
                "trace_id": context.trace_id,
            }
        )
        title = metadata.get("name") or candidate.title
        resource = ResourceV2(
            resource_id=resource_id,
            resource_type=ResourceType.KNOWLEDGE_ENTRY,
            title=title,
            content_ref=identity.content_ref({"content_sha256": content_sha256, "source": source}),
            source_ref=source,
            snapshot_ref=snapshot,
            evidence_ids=(evidence_id,),
            score=candidate.score,
            score_source=candidate.score_source,
            trace_id=context.trace_id,
            step_id=route_step.step_id,
            operation_id=operation_step.step_id,
        )
        return resource, evidence


__all__ = ["LexiangMAdapter"]
