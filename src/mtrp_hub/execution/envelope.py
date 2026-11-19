"""Controlled execution envelopes, receipts, hashes, and prefix verification."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from mtrp_hub.canonical import sha256_bytes, sha256_json
from mtrp_hub.domain.enums import StrEnum
from mtrp_hub.domain.models import FrozenModel, Sha256
from mtrp_hub.domain.requests_v2 import StepExecutionV2, StepStatusV2
from mtrp_hub.errors import MtrpError, SAFE_REASON_CODES
from mtrp_hub.execution.binding import BindingResolver, ResolvedCalls, upstream_blocks
from mtrp_hub.execution.models import (
    EntryCandidate,
    ExecutionManifest,
    ExecutionMode,
    ExecutorKind,
    ManifestStep,
    PlannedToolCall,
    Stage2Operation,
)
from mtrp_hub.execution.profile import ResponseContractError, STAGE2_PROFILE, Stage2ReadonlyProfile


class RawResponse(FrozenModel):
    """Business payload only; transport headers and credentials have no field here."""

    kind: Literal["json", "text"]
    data: Any = None
    text: str | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "RawResponse":
        if self.kind == "json" and self.text is not None:
            raise ValueError("JSON responses cannot carry text")
        if self.kind == "text" and (self.text is None or self.data is not None):
            raise ValueError("text responses require exact text and no JSON data")
        if self.kind == "json":
            sha256_json(self.data)
        return self

    def sha256(self) -> str:
        return sha256_json(self.data) if self.kind == "json" else sha256_bytes((self.text or "").encode("utf-8"))


class ToolErrorKind(StrEnum):
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    CONNECTOR_UNAVAILABLE = "CONNECTOR_UNAVAILABLE"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"


class RawToolOutcome(FrozenModel):
    response: RawResponse | None = None
    error: ToolErrorKind | None = None
    duration_ms: int = Field(ge=0, le=3_600_000, default=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> "RawToolOutcome":
        if self.response is not None and self.error is not None:
            raise ValueError("an outcome cannot contain both a response and an error")
        return self


class ToolCallReceipt(FrozenModel):
    call_id: str = Field(min_length=1, max_length=128)
    tool_id: str = Field(min_length=1, max_length=128)
    inner_tool_name: str | None = Field(default=None, max_length=128)
    arguments: dict[str, Any]
    arguments_sha256: Sha256
    response: RawResponse | None = None
    response_sha256: Sha256 | None = None
    status: StepStatusV2
    reason_code: str | None = Field(default=None, max_length=64)
    duration_ms: int = Field(ge=0, le=3_600_000, default=0)


class ExecutionReceipt(FrozenModel):
    step_id: str = Field(min_length=1, max_length=64)
    route_step_id: str = Field(min_length=1, max_length=64)
    manifest_hash: Sha256
    execution_id: str = Field(pattern=r"^exe_[0-9a-f]{64}$")
    status: StepStatusV2
    reason_code: str | None = Field(default=None, max_length=64)
    calls: tuple[ToolCallReceipt, ...] = ()
    binding_data: dict[str, Any] = Field(default_factory=dict)
    receipt_hash: Sha256

    def hashed_payload(
        self,
        manifest: ExecutionManifest,
        step: ManifestStep,
        profile: Stage2ReadonlyProfile = STAGE2_PROFILE,
    ) -> dict[str, Any]:
        """Bind identity/calls and deterministic projections, never raw payloads."""
        payload = self.model_dump(mode="json", exclude={"receipt_hash", "binding_data", "calls"})
        payload["calls"] = []
        for call in self.calls:
            projected_call = call.model_dump(
                mode="json",
                exclude={"response", "response_sha256", "duration_ms"},
            )
            projected_call["projection_sha256"] = profile.projection_sha256(manifest, step, call)
            payload["calls"].append(projected_call)
        return payload


class CoreStepReceipt(FrozenModel):
    step_id: str = Field(min_length=1, max_length=64)
    route_step_id: str = Field(min_length=1, max_length=64)
    execution_id: str = Field(pattern=r"^exe_[0-9a-f]{64}$")
    scope_sha256: Sha256
    execution: StepExecutionV2
    receipt_hash: Sha256

    def hashed_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"receipt_hash"})


class ExecutionEnvelope(FrozenModel):
    schema_version: Literal["1"] = "1"
    mode: ExecutionMode
    request_id: str = Field(pattern=r"^req_[0-9a-f]{64}$")
    route_id: str = Field(pattern=r"^route_[0-9a-f]{64}$")
    execution_id: str = Field(pattern=r"^exe_[0-9a-f]{64}$")
    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{64}$")
    manifest_hash: Sha256
    profile_hash: Sha256
    receipts: tuple[ExecutionReceipt, ...] = ()
    core_receipts: tuple[CoreStepReceipt, ...] = ()
    next_step_id: str | None = Field(default=None, max_length=64)
    continuation_hash: Sha256

    def hashed_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"continuation_hash"})


class ContinuationOutput(FrozenModel):
    output_kind: Literal["continuation"] = "continuation"
    envelope: ExecutionEnvelope


def response_sha256(response: RawResponse | None) -> str | None:
    return response.sha256() if response is not None else None


def seal_envelope(values: dict[str, Any]) -> ExecutionEnvelope:
    values = dict(values)
    values["continuation_hash"] = "0" * 64
    provisional = ExecutionEnvelope.model_validate(values)
    digest = sha256_json(provisional.hashed_payload())
    values["continuation_hash"] = digest
    return ExecutionEnvelope.model_validate(values)


def initial_envelope(manifest: ExecutionManifest) -> ExecutionEnvelope:
    return seal_envelope(
        {
            "schema_version": "1",
            "mode": manifest.mode,
            "request_id": manifest.request_id,
            "route_id": manifest.route_id,
            "execution_id": manifest.execution_id,
            "trace_id": manifest.trace_id(),
            "manifest_hash": manifest.manifest_hash,
            "profile_hash": manifest.profile_hash,
            "receipts": (),
            "core_receipts": (),
            "next_step_id": manifest.steps[0].step_id,
        }
    )


def envelope_with_receipts(
    envelope: ExecutionEnvelope,
    *,
    receipts: tuple[ExecutionReceipt, ...],
    core_receipts: tuple[CoreStepReceipt, ...],
    next_step_id: str | None,
) -> ExecutionEnvelope:
    values = envelope.model_dump(mode="json")
    values.update({"receipts": receipts, "core_receipts": core_receipts, "next_step_id": next_step_id})
    return seal_envelope(values)


def seal_receipt(
    manifest: ExecutionManifest,
    step: ManifestStep,
    *,
    status: StepStatusV2,
    reason_code: str | None,
    calls: tuple[ToolCallReceipt, ...],
    binding_data: dict[str, Any],
    profile: Stage2ReadonlyProfile = STAGE2_PROFILE,
) -> ExecutionReceipt:
    values = {
        "step_id": step.step_id,
        "route_step_id": step.route_step_id,
        "manifest_hash": manifest.manifest_hash,
        "execution_id": manifest.execution_id,
        "status": status,
        "reason_code": reason_code,
        "calls": calls,
        "binding_data": binding_data,
        "receipt_hash": "0" * 64,
    }
    provisional = ExecutionReceipt.model_validate(values)
    values["receipt_hash"] = sha256_json(provisional.hashed_payload(manifest, step, profile))
    return ExecutionReceipt.model_validate(values)


def upstream_receipt(
    manifest: ExecutionManifest,
    step: ManifestStep,
    reason: str = "UPSTREAM_BINDING_FAILED",
    profile: Stage2ReadonlyProfile = STAGE2_PROFILE,
) -> ExecutionReceipt:
    return seal_receipt(
        manifest,
        step,
        status=StepStatusV2.DEGRADED,
        reason_code=reason,
        calls=(),
        binding_data={},
        profile=profile,
    )


def build_receipt(
    manifest: ExecutionManifest,
    step: ManifestStep,
    resolved: ResolvedCalls,
    calls: tuple[ToolCallReceipt, ...],
    profile: Stage2ReadonlyProfile = STAGE2_PROFILE,
) -> ExecutionReceipt:
    """Derive status and downstream binding data solely from planned calls/responses."""
    failed = next((call for call in calls if call.status is not StepStatusV2.OK), None)
    if step.operation is Stage2Operation.LEXIANG_READ_ENTRIES:
        return _build_entry_receipt(manifest, step, resolved, calls, profile)
    if failed is not None:
        return seal_receipt(
            manifest,
            step,
            status=StepStatusV2.DEGRADED,
            reason_code=failed.reason_code or "UNAVAILABLE",
            calls=calls,
            binding_data={},
            profile=profile,
        )
    try:
        binding_data = profile.extract_binding_data(step, calls, manifest.mode)
    except ResponseContractError as exc:
        return seal_receipt(
            manifest,
            step,
            status=StepStatusV2.DEGRADED,
            reason_code=exc.error_code,
            calls=calls,
            binding_data={},
            profile=profile,
        )
    status = StepStatusV2.OK
    reason: str | None = None
    if step.operation in (Stage2Operation.LEXIANG_SEARCH_TEXT, Stage2Operation.LEXIANG_SEARCH_VECTOR) and binding_data.get("entry_reads") == []:
        status = StepStatusV2.EMPTY
        reason = "M_TEXT_EMPTY" if step.operation is Stage2Operation.LEXIANG_SEARCH_TEXT else "M_VECTOR_EMPTY"
    elif step.operation is Stage2Operation.GITHUB_LIST_BRANCHES and binding_data.get("repository_state") == "empty":
        status = StepStatusV2.EMPTY
        reason = "EMPTY_REPOSITORY"
    return seal_receipt(manifest, step, status=status, reason_code=reason, calls=calls, binding_data=binding_data, profile=profile)


def _build_entry_receipt(
    manifest: ExecutionManifest,
    step: ManifestStep,
    resolved: ResolvedCalls,
    calls: tuple[ToolCallReceipt, ...],
    profile: Stage2ReadonlyProfile,
) -> ExecutionReceipt:
    raw_candidates = resolved.arguments.get("entry_reads", [])
    candidates = [item if isinstance(item, EntryCandidate) else EntryCandidate.model_validate(item) for item in raw_candidates]
    space_step = next(
        item for item in manifest.steps
        if item.route_step_id == step.route_step_id and item.operation is Stage2Operation.LEXIANG_RESOLVE_SPACE
    )
    space_id = space_step.arguments["space_id"]
    errors = 0
    for index, candidate in enumerate(candidates):
        pair = calls[index * 2 : index * 2 + 2]
        if len(pair) != 2 or any(call.status is not StepStatusV2.OK for call in pair):
            errors += 1
            continue
        try:
            profile.parse_entry_meta(pair[0].response, entry_id=candidate.entry_id, space_id=space_id, mode=manifest.mode)
            profile.parse_entry_content(pair[1].response, entry_id=candidate.entry_id, mode=manifest.mode)
        except ResponseContractError:
            errors += 1
    binding_data = {"entry_read_count": len(candidates), "entry_error_count": errors}
    if not candidates:
        return seal_receipt(manifest, step, status=StepStatusV2.EMPTY, reason_code="M_EMPTY", calls=calls, binding_data=binding_data, profile=profile)
    if errors:
        return seal_receipt(manifest, step, status=StepStatusV2.DEGRADED, reason_code="M_ENTRY_SKIPPED", calls=calls, binding_data=binding_data, profile=profile)
    return seal_receipt(manifest, step, status=StepStatusV2.OK, reason_code=None, calls=calls, binding_data=binding_data, profile=profile)


class EnvelopeVerifier:
    """Recompute the unique legal prefix and every planned call before new I/O."""

    def __init__(self, profile: Stage2ReadonlyProfile | None = None, binding: BindingResolver | None = None) -> None:
        self._profile = profile or STAGE2_PROFILE
        self._binding = binding or BindingResolver(self._profile)

    def verify_prefix(self, manifest: ExecutionManifest, envelope: ExecutionEnvelope) -> None:
        if envelope.continuation_hash != sha256_json(envelope.hashed_payload()):
            raise MtrpError("RECEIPT_INTEGRITY_FAILED", "Envelope continuation hash verification failed.", field="continuation_hash")
        expected_identity = (
            manifest.mode,
            manifest.request_id,
            manifest.route_id,
            manifest.execution_id,
            manifest.trace_id(),
            manifest.manifest_hash,
            manifest.profile_hash,
        )
        actual_identity = (
            envelope.mode,
            envelope.request_id,
            envelope.route_id,
            envelope.execution_id,
            envelope.trace_id,
            envelope.manifest_hash,
            envelope.profile_hash,
        )
        if actual_identity != expected_identity:
            raise MtrpError("EXECUTION_RUN_MISMATCH", "Envelope identity differs from the expected Manifest.", field="execution_id")
        step_ids = [step.step_id for step in manifest.steps]
        if envelope.next_step_id is None:
            next_index = len(step_ids)
        else:
            try:
                next_index = step_ids.index(envelope.next_step_id)
            except ValueError as exc:
                raise MtrpError("RECEIPT_STEP_EXTRA", "The envelope points to an unknown step.", field="next_step_id") from exc
        prefix = manifest.steps[:next_index]
        expected_remote = [step.step_id for step in prefix if step.executor is ExecutorKind.WORKBUDDY]
        expected_core = [step.step_id for step in prefix if step.executor is ExecutorKind.CORE]
        self._compare_step_ids([item.step_id for item in envelope.receipts], expected_remote, "remote")
        self._compare_step_ids([item.step_id for item in envelope.core_receipts], expected_core, "core")
        by_id = {step.step_id: step for step in manifest.steps}
        prior_remote: list[ExecutionReceipt] = []
        for receipt in envelope.receipts:
            step = by_id[receipt.step_id]
            if receipt.reason_code is not None and receipt.reason_code not in SAFE_REASON_CODES:
                raise MtrpError("RECEIPT_RESPONSE_INVALID", "A receipt contains a non-whitelist reason code.", field=step.step_id)
            for call in receipt.calls:
                if call.reason_code is not None and call.reason_code not in SAFE_REASON_CODES:
                    raise MtrpError("RECEIPT_RESPONSE_INVALID", "A call contains a non-whitelist reason code.", field=step.step_id)
                if (call.status is StepStatusV2.OK) != (call.reason_code is None):
                    raise MtrpError("RECEIPT_RESPONSE_INVALID", "Call status and reason code are inconsistent.", field=step.step_id)
            if upstream_blocks(step, prior_remote):
                expected = upstream_receipt(manifest, step, profile=self._profile)
                self._compare_receipt(receipt, expected, manifest, step)
                prior_remote.append(receipt)
                continue
            try:
                resolved = self._binding.resolve(step, prior_remote)
                expected = self._verify_calls(manifest, step, resolved, receipt.calls)
            except MtrpError as exc:
                if exc.error_code in {"UPSTREAM_BINDING_FAILED", "BINDING_EMPTY", "BINDING_TYPE_INVALID", "ENTRY_READ_LIMIT_EXCEEDED"}:
                    if receipt.calls:
                        raise MtrpError("RECEIPT_STEP_EXTRA", "A failed binding produced forbidden tool calls.", field=step.step_id) from exc
                    expected = upstream_receipt(manifest, step, profile=self._profile)
                else:
                    raise
            self._compare_receipt(receipt, expected, manifest, step)
            prior_remote.append(receipt)
        for receipt in envelope.core_receipts:
            step = by_id[receipt.step_id]
            core_reason = receipt.execution.step_result.reason_code
            if core_reason is not None and core_reason not in SAFE_REASON_CODES:
                raise MtrpError("CORE_STATE_CHANGED", "A Core receipt contains a non-whitelist reason code.", field=step.step_id)
            if receipt.route_step_id != step.route_step_id or receipt.execution_id != manifest.execution_id or receipt.scope_sha256 != step.arguments.get("scope_sha256"):
                raise MtrpError("CORE_STATE_CHANGED", "A Core receipt differs from its authorized Vault scope.", field=step.step_id)
            if receipt.receipt_hash != sha256_json(receipt.hashed_payload()):
                raise MtrpError("RECEIPT_INTEGRITY_FAILED", "A Core receipt hash is invalid.", field=step.step_id)
            if receipt.execution.step_result.step_id != step.route_step_id:
                raise MtrpError("CORE_STATE_CHANGED", "A Core receipt route step is invalid.", field=step.step_id)

    @staticmethod
    def _compare_step_ids(actual: list[str], expected: list[str], kind: str) -> None:
        if len(set(actual)) != len(actual):
            raise MtrpError("RECEIPT_STEP_DUPLICATE", f"Duplicate {kind} receipts are forbidden.", field="receipts")
        actual_set, expected_set = set(actual), set(expected)
        if actual_set - expected_set:
            raise MtrpError("RECEIPT_STEP_EXTRA", f"Extra {kind} receipts are forbidden.", field="receipts")
        if expected_set - actual_set:
            raise MtrpError("RECEIPT_STEP_MISSING", f"Missing {kind} receipts are forbidden.", field="receipts")
        if actual != expected:
            raise MtrpError("RECEIPT_ORDER_INVALID", f"The {kind} receipt order is invalid.", field="receipts")

    def _verify_calls(
        self,
        manifest: ExecutionManifest,
        step: ManifestStep,
        resolved: ResolvedCalls,
        actual_calls: tuple[ToolCallReceipt, ...],
    ) -> ExecutionReceipt:
        planned = resolved.calls
        if len(actual_calls) != len(planned):
            raise MtrpError("RECEIPT_TOOL_MISMATCH", "The receipt call count differs from the planned calls.", field=step.step_id)
        for planned_call, actual in zip(planned, actual_calls, strict=True):
            if (actual.call_id, actual.tool_id, actual.inner_tool_name, actual.arguments) != (
                planned_call.call_id,
                planned_call.tool_id,
                planned_call.inner_tool_name,
                planned_call.arguments,
            ):
                raise MtrpError("RECEIPT_TOOL_MISMATCH", "A receipt tool or inner tool differs from the plan.", field=step.step_id)
            if actual.arguments_sha256 != sha256_json(actual.arguments):
                raise MtrpError("RECEIPT_ARGUMENT_MISMATCH", "Tool argument hash verification failed.", field=step.step_id)
            if actual.response_sha256 != response_sha256(actual.response):
                raise MtrpError("RECEIPT_RESPONSE_INVALID", "Tool response hash verification failed.", field=step.step_id)
            if actual.status is StepStatusV2.OK and actual.response is None:
                raise MtrpError("RECEIPT_RESPONSE_MISSING", "An OK tool call requires a business response.", field=step.step_id)
            if actual.status is not StepStatusV2.OK and actual.response is not None:
                raise MtrpError("RECEIPT_RESPONSE_INVALID", "A failed tool call cannot carry an OK response.", field=step.step_id)
        return build_receipt(manifest, step, resolved, actual_calls, self._profile)

    def _compare_receipt(
        self,
        actual: ExecutionReceipt,
        expected: ExecutionReceipt,
        manifest: ExecutionManifest,
        step: ManifestStep,
    ) -> None:
        if actual.receipt_hash != sha256_json(actual.hashed_payload(manifest, step, self._profile)):
            raise MtrpError("RECEIPT_INTEGRITY_FAILED", "Execution receipt hash verification failed.", field=actual.step_id)
        ignored = actual.model_dump(mode="json", exclude={"receipt_hash", "calls"})
        expected_ignored = expected.model_dump(mode="json", exclude={"receipt_hash", "calls"})
        if ignored != expected_ignored:
            raise MtrpError("RECEIPT_PREFIX_MISMATCH", "Receipt status or binding data differs from the verified plan.", field=actual.step_id)
        # The approved response projection participates in receipt_hash; full
        # raw payloads are independently checked through response_sha256 above.
        if actual.receipt_hash != expected.receipt_hash:
            raise MtrpError("RECEIPT_INTEGRITY_FAILED", "Receipt content differs from the verified call plan.", field=actual.step_id)


__all__ = [
    "ContinuationOutput",
    "CoreStepReceipt",
    "EnvelopeVerifier",
    "ExecutionEnvelope",
    "ExecutionReceipt",
    "RawResponse",
    "RawToolOutcome",
    "ToolCallReceipt",
    "ToolErrorKind",
    "build_receipt",
    "envelope_with_receipts",
    "initial_envelope",
    "response_sha256",
    "seal_envelope",
    "seal_receipt",
    "upstream_receipt",
]
