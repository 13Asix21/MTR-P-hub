"""Stage 2 execution statuses, logical step results, and Canonical responses."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mtrp_hub.domain.enums import StoreKind, StrEnum
from mtrp_hub.domain.models import FrozenModel, ProjectCoordinate, Relation, Sha256
from mtrp_hub.domain.models_v2 import CanonicalResultV2, EvidenceV2, ResourceV2
from mtrp_hub.execution.models import ExecutionMode


class StepStatusV2(StrEnum):
    OK = "OK"
    EMPTY = "EMPTY"
    DEGRADED = "DEGRADED"
    REJECTED = "REJECTED"


class OperationResult(FrozenModel):
    step_id: str = Field(min_length=1, max_length=64)
    status: StepStatusV2
    reason_code: str | None = Field(default=None, max_length=64)
    resource_count: int = Field(ge=0, default=0)
    skipped_count: int = Field(ge=0, default=0)
    tool_call_count: int = Field(ge=0, default=0)
    receipt_hash: Sha256 | None = None
    score_available_count: int = Field(ge=0, default=0)

    @model_validator(mode="after")
    def validate_reason(self) -> "OperationResult":
        if self.status is StepStatusV2.OK and self.reason_code is not None:
            raise ValueError("OK operations cannot have a reason code")
        if self.status is not StepStatusV2.OK and self.reason_code is None:
            raise ValueError("non-OK operations require a safe reason code")
        if self.status in (StepStatusV2.EMPTY, StepStatusV2.REJECTED) and self.resource_count:
            raise ValueError("EMPTY/REJECTED operations cannot contain resources")
        return self


class StepResultV2(FrozenModel):
    step_id: str
    store: StoreKind
    status: StepStatusV2
    reason_code: str | None = None
    coordinate: ProjectCoordinate | None = None
    resource_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    resource_count: int = Field(ge=0, default=0)
    evidence_count: int = Field(ge=0, default=0)
    next_step_id: str | None = None
    request_id: str = Field(pattern=r"^req_[0-9a-f]{64}$")
    route_id: str = Field(pattern=r"^route_[0-9a-f]{64}$")
    execution_id: str = Field(pattern=r"^exe_[0-9a-f]{64}$")
    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{64}$")
    operations: tuple[OperationResult, ...] = ()
    skipped_count: int = Field(ge=0, default=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "StepResultV2":
        if self.resource_count != len(self.resource_ids) or self.evidence_count != len(self.evidence_ids):
            raise ValueError("StepResult counts must match references")
        if len(set(self.resource_ids)) != len(self.resource_ids) or len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("StepResult references must be unique")
        if self.status is StepStatusV2.OK and self.reason_code is not None:
            raise ValueError("OK steps cannot have a reason code")
        if self.status is not StepStatusV2.OK and self.reason_code is None:
            raise ValueError("non-OK steps require a reason code")
        if self.status in (StepStatusV2.EMPTY, StepStatusV2.REJECTED) and (self.resource_ids or self.evidence_ids):
            raise ValueError("EMPTY/REJECTED steps cannot contain resources")
        return self


class StepExecutionV2(FrozenModel):
    step_result: StepResultV2
    resources: tuple[ResourceV2, ...] = ()
    relations: tuple[Relation, ...] = ()
    evidence: tuple[EvidenceV2, ...] = ()

    @model_validator(mode="after")
    def validate_execution(self) -> "StepExecutionV2":
        if self.step_result.status in (StepStatusV2.EMPTY, StepStatusV2.REJECTED) and (self.resources or self.relations or self.evidence):
            raise ValueError("EMPTY/REJECTED executions must have empty Canonical collections")
        if tuple(sorted(item.resource_id for item in self.resources)) != tuple(sorted(self.step_result.resource_ids)):
            raise ValueError("Resource references do not match StepResult")
        if tuple(sorted(item.evidence_id for item in self.evidence)) != tuple(sorted(self.step_result.evidence_ids)):
            raise ValueError("Evidence references do not match StepResult")
        CanonicalResultV2.create(self.resources, self.relations, self.evidence)
        return self


class QueryResponseV2(FrozenModel):
    schema_version: Literal["2"] = "2"
    data_mode: ExecutionMode
    local_data_mode: Literal["obsidian-local"] | None = None
    request_id: str = Field(pattern=r"^req_[0-9a-f]{64}$")
    route_id: str = Field(pattern=r"^route_[0-9a-f]{64}$")
    execution_id: str = Field(pattern=r"^exe_[0-9a-f]{64}$")
    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{64}$")
    manifest_hash: Sha256
    status: StepStatusV2
    step_results: tuple[StepResultV2, ...] = Field(min_length=1)
    result: CanonicalResultV2

    @model_validator(mode="after")
    def validate_response_identity(self) -> "QueryResponseV2":
        for step in self.step_results:
            if (step.request_id, step.route_id, step.execution_id, step.trace_id) != (
                self.request_id,
                self.route_id,
                self.execution_id,
                self.trace_id,
            ):
                raise ValueError("StepResult identity differs from QueryResponse")
        expected = StepStatusV2.REJECTED if any(item.status is StepStatusV2.REJECTED for item in self.step_results) else None
        if expected is None:
            expected = StepStatusV2.DEGRADED if any(item.status is StepStatusV2.DEGRADED for item in self.step_results) else None
        if expected is None:
            expected = StepStatusV2.OK if any(item.status is StepStatusV2.OK for item in self.step_results) else StepStatusV2.EMPTY
        if self.status is not expected:
            raise ValueError("QueryResponse status does not aggregate its steps")
        return self


def response_payload_v2(response: QueryResponseV2) -> dict[str, object]:
    """Flatten the aggregate exactly once and exclude internal execution state."""
    return {
        "schema_version": response.schema_version,
        "data_mode": response.data_mode.value,
        "local_data_mode": response.local_data_mode,
        "request_id": response.request_id,
        "route_id": response.route_id,
        "execution_id": response.execution_id,
        "trace_id": response.trace_id,
        "manifest_hash": response.manifest_hash,
        "status": response.status.value,
        "step_results": [item.model_dump(mode="json") for item in response.step_results],
        "resources": [item.model_dump(mode="json") for item in response.result.resources],
        "relations": [item.model_dump(mode="json") for item in response.result.relations],
        "evidence": [item.model_dump(mode="json") for item in response.result.evidence],
    }


__all__ = [
    "OperationResult",
    "QueryResponseV2",
    "StepExecutionV2",
    "StepResultV2",
    "StepStatusV2",
    "response_payload_v2",
]
