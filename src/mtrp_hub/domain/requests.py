"""Request, deterministic plan, step execution, and response DTOs."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mtrp_hub.domain.enums import (
    EmptyAction,
    Intent,
    OutputFormat,
    ReadOperation,
    ReasonCode,
    StepStatus,
    StoreKind,
)
from mtrp_hub.domain.models import (
    CanonicalResult,
    Evidence,
    FrozenModel,
    ProjectCoordinate,
    Relation,
    RelativePath,
    Resource,
    Sha256,
)


class RouteQueryRequest(FrozenModel):
    """Untrusted CLI request; bootstrap paths are checked before any target I/O."""

    query: str = Field(min_length=1, max_length=16384)
    intent: Intent
    project_id: str | None = None
    repo_id: str | None = None
    workspace_id: str | None = None
    config_dir: str = Field(min_length=1, max_length=4096)
    fixture: str | None = None
    include_r: bool = False
    format: OutputFormat = OutputFormat.JSON


class ReadStep(FrozenModel):
    """One logical read-only step with no real tool name or tool arguments."""

    step_id: str = Field(min_length=1, max_length=64)
    store: StoreKind
    store_id: str
    operation: ReadOperation
    view_tag: Literal["mtr/trial", "mtr/rarely"] | None = None
    coordinate: ProjectCoordinate | None = None
    adr_roots: tuple[RelativePath, ...] = ()
    on_empty: EmptyAction
    on_unavailable: EmptyAction
    read_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_step_shape(self) -> "ReadStep":
        if self.store is StoreKind.P:
            if self.coordinate is None:
                raise ValueError("P steps require a resolved coordinate")
            if self.operation not in (ReadOperation.PROJECT_READ, ReadOperation.ADR_SEARCH):
                raise ValueError("P steps only support project/ADR reads")
        elif self.coordinate is not None or self.adr_roots:
            raise ValueError("only P steps may carry repository coordinates/ADR roots")
        if self.store in (StoreKind.T, StoreKind.R):
            expected = "mtr/trial" if self.store is StoreKind.T else "mtr/rarely"
            if self.view_tag != expected or self.operation is not ReadOperation.VAULT_VIEW_SEARCH:
                raise ValueError("T/R steps must use their fixed managed view")
        elif self.view_tag is not None:
            raise ValueError("only T/R steps have view tags")
        if self.store is StoreKind.M and self.operation is not ReadOperation.KNOWLEDGE_SEARCH:
            raise ValueError("M steps only support knowledge search")
        return self


class ReadPlan(FrozenModel):
    """Immutable deterministic plan; excludes query text, time, randomness, and I/O args."""

    schema_version: Literal["1"] = "1"
    intent: Intent
    rule_version: Literal["v1"]
    policy_hash: Sha256
    registry_hash: Sha256
    request_hash: Sha256
    plan_hash: Sha256
    include_r: bool
    read_only: Literal[True] = True
    data_mode: Literal["fake"] = "fake"
    project_coordinate: ProjectCoordinate | None = None
    steps: tuple[ReadStep, ...] = Field(min_length=1)
    write_proposal: None = None

    @model_validator(mode="after")
    def validate_plan_identity(self) -> "ReadPlan":
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step IDs must be unique")
        if self.intent is Intent.COLD_DETAIL and not self.include_r:
            raise ValueError("cold_detail always includes R")
        has_r = any(step.store is StoreKind.R for step in self.steps)
        if has_r != self.include_r:
            raise ValueError("include_r and materialized R steps disagree")
        needs_p = self.intent in (Intent.PROJECT_CONTEXT, Intent.DECISION_HISTORY)
        if needs_p and self.project_coordinate is None:
            raise ValueError("P intents require a project coordinate")
        return self


class StepResult(FrozenModel):
    """The status and references produced by one Fake read step."""

    step_id: str
    store: StoreKind
    status: StepStatus
    reason_code: ReasonCode | None = None
    coordinate: ProjectCoordinate | None = None
    resource_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    next_step_id: str | None = None

    @model_validator(mode="after")
    def validate_result_status(self) -> "StepResult":
        if self.status is StepStatus.OK and self.reason_code is not None:
            raise ValueError("OK results cannot have a reason code")
        if self.status is not StepStatus.OK and self.reason_code is None:
            raise ValueError("empty/degraded results require a reason code")
        if self.status is not StepStatus.OK and (self.resource_ids or self.evidence_ids):
            raise ValueError("empty/degraded results cannot contain resource/evidence IDs")
        if len(set(self.resource_ids)) != len(self.resource_ids):
            raise ValueError("resource IDs must be unique")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence IDs must be unique")
        return self


class StepExecution(FrozenModel):
    """Canonical objects and status returned together by a read-only adapter."""

    step_result: StepResult
    resources: tuple[Resource, ...] = ()
    relations: tuple[Relation, ...] = ()
    evidence: tuple[Evidence, ...] = ()

    @model_validator(mode="after")
    def validate_execution_consistency(self) -> "StepExecution":
        if self.step_result.status is not StepStatus.OK and (self.resources or self.relations or self.evidence):
            raise ValueError("empty/degraded executions must have empty canonical collections")
        if tuple(sorted(item.resource_id for item in self.resources)) != tuple(sorted(self.step_result.resource_ids)):
            raise ValueError("StepResult resource IDs do not match returned Resources")
        if tuple(sorted(item.evidence_id for item in self.evidence)) != tuple(sorted(self.step_result.evidence_ids)):
            raise ValueError("StepResult evidence IDs do not match returned Evidence")
        CanonicalResult.create(self.resources, self.relations, self.evidence)
        return self


class RouteQueryResponse(FrozenModel):
    """Internal response DTO; the JSON presenter flattens the plan fields."""

    plan: ReadPlan
    fixture_id: str
    fixture_hash: Sha256
    step_results: tuple[StepResult, ...]
    result: CanonicalResult
    write_proposal: None = None

    @model_validator(mode="after")
    def validate_response_steps(self) -> "RouteQueryResponse":
        if tuple(result.step_id for result in self.step_results) != tuple(step.step_id for step in self.plan.steps):
            raise ValueError("response step results must match plan order")
        return self
