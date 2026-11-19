"""Whitelist-only stage 2 audit DTOs; raw requests/responses are not representable."""

from __future__ import annotations

from pydantic import Field

from mtrp_hub.domain.enums import StoreKind
from mtrp_hub.domain.models import FrozenModel, Sha256


class Stage2AuditPayload(FrozenModel):
    intent: str | None = None
    rule_version: str | None = None
    mode: str | None = None
    project_id: str | None = None
    repo_id: str | None = None
    location_id: str | None = None
    ref: str | None = None
    commit: str | None = None
    step_id: str | None = None
    route_step_id: str | None = None
    store: StoreKind | None = None
    operation: str | None = None
    next_step_id: str | None = None
    resource_count: int | None = Field(default=None, ge=0)
    evidence_count: int | None = Field(default=None, ge=0)
    skipped_count: int | None = Field(default=None, ge=0)
    tool_call_count: int | None = Field(default=None, ge=0)
    score_available_count: int | None = Field(default=None, ge=0)
    scanned_count: int | None = Field(default=None, ge=0)
    error_code: str | None = None


class Stage2ExecutionRun(FrozenModel):
    run_seq: int = Field(gt=0)
    request_id: str
    route_id: str
    execution_id: str
    trace_id: str
    status: str
    next_step_id: str | None = None
    last_continuation_hash: Sha256


class Stage2ExecutionEvent(FrozenModel):
    event_seq: int = Field(gt=0)
    run_seq: int = Field(gt=0)
    recorded_at: str
    event_type: str
    step_id: str | None = None
    route_step_id: str | None = None
    store: StoreKind | None = None
    operation: str | None = None
    status: str
    reason_code: str | None = None
    next_step_id: str | None = None
    resource_count: int = Field(ge=0, default=0)
    evidence_count: int = Field(ge=0, default=0)
    skipped_count: int = Field(ge=0, default=0)
    tool_call_count: int = Field(ge=0, default=0)
    duration_ms: int = Field(ge=0, default=0)
    receipt_hash: Sha256 | None = None
    response_sha256: Sha256 | None = None
    payload: Stage2AuditPayload = Stage2AuditPayload()


class TraceRunRecord(FrozenModel):
    run_seq: int = Field(gt=0)
    started_at: str
    finished_at: str | None = None
    request_id: str
    route_id: str
    execution_id: str
    trace_id: str
    manifest_hash: Sha256
    mode: str
    status: str
    reason_code: str | None = None
    next_step_id: str | None = None
    last_continuation_hash: Sha256
    resource_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    step_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    events: tuple[Stage2ExecutionEvent, ...] = ()


__all__ = ["Stage2AuditPayload", "Stage2ExecutionEvent", "Stage2ExecutionRun", "TraceRunRecord"]
