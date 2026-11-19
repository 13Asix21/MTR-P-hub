"""Strict serial WorkBuddy executor using only a host-supplied transport port."""

from __future__ import annotations

from typing import Protocol

from mtrp_hub.canonical import sha256_json
from mtrp_hub.errors import MtrpError
from mtrp_hub.domain.requests_v2 import StepStatusV2
from mtrp_hub.execution.binding import BindingResolver, upstream_blocks
from mtrp_hub.execution.envelope import (
    EnvelopeVerifier,
    ExecutionEnvelope,
    ExecutionReceipt,
    RawToolOutcome,
    ToolCallReceipt,
    ToolErrorKind,
    build_receipt,
    envelope_with_receipts,
    initial_envelope,
    upstream_receipt,
)
from mtrp_hub.execution.models import (
    ExecutionManifest,
    ExecutorKind,
    ManifestStep,
    PlannedToolCall,
)
from mtrp_hub.execution.profile import STAGE2_PROFILE, Stage2ReadonlyProfile
from mtrp_hub.execution.verifier import ManifestVerifier


class WorkBuddyToolTransport(Protocol):
    """The sole external port; the repository intentionally provides no real implementation."""

    def invoke(
        self,
        tool_id: str,
        inner_tool_name: str | None,
        arguments: dict,
        timeout_seconds: int,
    ) -> RawToolOutcome:
        raise TypeError("WorkBuddyToolTransport must be supplied by the host")


class WorkBuddyManifestExecutor:
    """Execute only the remote prefix up to the next Core barrier."""

    def __init__(
        self,
        transport: WorkBuddyToolTransport,
        *,
        profile: Stage2ReadonlyProfile | None = None,
        binding: BindingResolver | None = None,
        verifier: ManifestVerifier | None = None,
    ) -> None:
        self._transport = transport
        self._profile = profile or STAGE2_PROFILE
        self._binding = binding or BindingResolver(self._profile)
        self._verifier = verifier or ManifestVerifier(self._profile, self._binding)
        self._envelope_verifier = EnvelopeVerifier(self._profile, self._binding)

    def execute(
        self,
        manifest: ExecutionManifest,
        envelope: ExecutionEnvelope | None = None,
        *,
        expected_manifest_hash: str | None = None,
    ) -> ExecutionEnvelope:
        self._verifier.verify_before_call(manifest, expected_manifest_hash or manifest.manifest_hash)
        current = envelope or initial_envelope(manifest)
        self._envelope_verifier.verify_prefix(manifest, current)
        receipts = list(current.receipts)
        core_receipts = current.core_receipts
        step_ids = [step.step_id for step in manifest.steps]
        while current.next_step_id is not None:
            step_index = step_ids.index(current.next_step_id)
            step = manifest.steps[step_index]
            if step.executor is ExecutorKind.CORE:
                break
            receipt = self.execute_remote_step(manifest, step, tuple(receipts))
            receipts.append(receipt)
            next_step_id = step_ids[step_index + 1] if step_index + 1 < len(step_ids) else None
            current = envelope_with_receipts(
                current,
                receipts=tuple(receipts),
                core_receipts=core_receipts,
                next_step_id=next_step_id,
            )
            if next_step_id is not None and manifest.steps[step_index + 1].executor is ExecutorKind.CORE:
                break
        return current

    def execute_remote_step(
        self,
        manifest: ExecutionManifest,
        step: ManifestStep,
        prior_receipts: tuple[ExecutionReceipt, ...],
    ) -> ExecutionReceipt:
        if upstream_blocks(step, prior_receipts):
            return upstream_receipt(manifest, step)
        try:
            resolved = self._binding.resolve(step, prior_receipts)
        except MtrpError:
            return upstream_receipt(manifest, step)
        existing_calls = sum(len(receipt.calls) for receipt in prior_receipts)
        if existing_calls + len(resolved.calls) > manifest.limits.max_tool_calls:
            raise MtrpError("ENTRY_READ_LIMIT_EXCEEDED", "The planned execution exceeds its tool call budget.", field=step.step_id)
        calls = tuple(self._invoke(planned) for planned in resolved.calls)
        return build_receipt(manifest, step, resolved, calls, self._profile)

    # BindingResolver/upstream_blocks is shared verbatim with EnvelopeVerifier.

    def _invoke(self, planned: PlannedToolCall) -> ToolCallReceipt:
        arguments_hash = sha256_json(planned.arguments)
        try:
            outcome = self._transport.invoke(
                planned.tool_id,
                planned.inner_tool_name,
                planned.arguments,
                planned.timeout_seconds,
            )
            if not isinstance(outcome, RawToolOutcome):
                outcome = RawToolOutcome(error=ToolErrorKind.UNAVAILABLE)
        except Exception:
            outcome = RawToolOutcome(error=ToolErrorKind.UNAVAILABLE)
        if outcome.response is not None and outcome.error is None:
            return ToolCallReceipt(
                call_id=planned.call_id,
                tool_id=planned.tool_id,
                inner_tool_name=planned.inner_tool_name,
                arguments=planned.arguments,
                arguments_sha256=arguments_hash,
                response=outcome.response,
                response_sha256=outcome.response.sha256(),
                status=StepStatusV2.OK,
                reason_code=None,
                duration_ms=outcome.duration_ms,
            )
        reason = outcome.error.value if outcome.error is not None else ToolErrorKind.EMPTY_RESPONSE.value
        if reason == ToolErrorKind.NOT_FOUND.value:
            reason = "REF_UNRESOLVED" if planned.tool_id.endswith("get_commit") else "FILE_READ_FAILED"
        return ToolCallReceipt(
            call_id=planned.call_id,
            tool_id=planned.tool_id,
            inner_tool_name=planned.inner_tool_name,
            arguments=planned.arguments,
            arguments_sha256=arguments_hash,
            response=None,
            response_sha256=None,
            status=StepStatusV2.DEGRADED,
            reason_code=reason,
            duration_ms=outcome.duration_ms,
        )


__all__ = ["WorkBuddyManifestExecutor", "WorkBuddyToolTransport"]
