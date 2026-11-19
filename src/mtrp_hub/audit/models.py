"""Decision DTOs with an explicit allowlist and no raw request/content fields."""

from __future__ import annotations

from mtrp_hub.domain.enums import StoreKind, StrEnum
from mtrp_hub.domain.models import FrozenModel, ProjectCoordinate, Sha256


class DecisionEventType(StrEnum):
    REQUEST_REJECTED = "REQUEST_REJECTED"
    PLAN_CREATED = "PLAN_CREATED"
    STEP_FINISHED = "STEP_FINISHED"
    QUERY_FINISHED = "QUERY_FINISHED"


class DecisionStep(FrozenModel):
    step_id: str
    store: StoreKind
    operation: str
    view_tag: str | None = None
    on_empty: str
    on_unavailable: str


class DecisionPayload(FrozenModel):
    """Only fields explicitly approved for local Decision Log persistence."""

    intent: str | None = None
    error_code: str | None = None
    field: str | None = None
    workspace_supplied: bool | None = None
    rule_version: str | None = None
    policy_hash: Sha256 | None = None
    registry_hash: Sha256 | None = None
    plan_hash: Sha256 | None = None
    include_r: bool | None = None
    steps: tuple[DecisionStep, ...] | None = None
    coordinate: ProjectCoordinate | None = None
    step_id: str | None = None
    store: StoreKind | None = None
    next_step_id: str | None = None
    resource_count: int | None = None
    evidence_count: int | None = None
    relation_count: int | None = None
    fixture_id: str | None = None
    fixture_hash: Sha256 | None = None
    final_status: str | None = None


class DecisionRecord(FrozenModel):
    """One safe decision event; timestamps and SQLite sequence stay outside it."""

    event_type: DecisionEventType
    request_hash: Sha256
    plan_hash: Sha256 | None = None
    status: str
    reason_code: str | None = None
    payload: DecisionPayload = DecisionPayload()
