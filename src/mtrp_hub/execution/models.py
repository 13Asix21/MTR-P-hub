"""Frozen stage 2 route, Manifest, binding, and execution limit models."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from mtrp_hub.canonical import sha256_json
from mtrp_hub.domain.enums import EmptyAction, StoreKind, StrEnum
from mtrp_hub.domain.models import CommitSha, FrozenModel, Sha256
from mtrp_hub.domain.requests import ReadPlan, ReadStep
from mtrp_hub.execution import identity


class ExecutionMode(StrEnum):
    FIXTURE = "fixture"
    OBSIDIAN_LOCAL = "obsidian-local"
    WORKBUDDY_MANIFEST = "workbuddy-manifest"


class ExecutorKind(StrEnum):
    WORKBUDDY = "workbuddy"
    CORE = "core"


class AdapterKind(StrEnum):
    GITHUB_P = "github-p"
    LEXIANG_M = "lexiang-m"
    OBSIDIAN_LOCAL = "obsidian-local"


class Stage2Operation(StrEnum):
    GITHUB_LIST_BRANCHES = "github_list_branches"
    GITHUB_RESOLVE_REF = "github_resolve_ref"
    GITHUB_READ_REPOSITORY_ROOT = "github_read_repository_root"
    GITHUB_READ_FILE = "github_read_file"
    LEXIANG_RESOLVE_SPACE = "lexiang_resolve_space"
    LEXIANG_SEARCH_TEXT = "lexiang_search_text"
    LEXIANG_SEARCH_VECTOR = "lexiang_search_vector"
    LEXIANG_READ_ENTRIES = "lexiang_read_entries"
    VAULT_VIEW_SEARCH = "vault_view_search"


class StepBinding(FrozenModel):
    """The only permitted dynamic argument expression."""

    from_step: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    field: Literal["commit", "entry_reads"]


class EntryCandidate(FrozenModel):
    entry_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    title: str = Field(min_length=1, max_length=512)
    score: float | None = None
    score_source: Literal["text_search", "vector_search", "not_provided"] = "not_provided"
    version: str | None = Field(default=None, max_length=128)
    revision: str | None = Field(default=None, max_length=128)
    provider_content_sha256: Sha256 | None = None

    @field_validator("score")
    @classmethod
    def validate_score(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
            raise ValueError("score must be a finite number between zero and one")
        return value

    @model_validator(mode="after")
    def validate_score_source(self) -> "EntryCandidate":
        if self.score is None and self.score_source != "not_provided":
            return self.model_copy(update={"score_source": "not_provided"})
        if self.score is not None and self.score_source == "not_provided":
            raise ValueError("a supplied score requires its search source")
        return self


class ExecutionLimits(FrozenModel):
    max_steps: int = Field(gt=0, le=32)
    max_entry_reads: int = Field(gt=0, le=10)
    max_tool_calls: int = Field(gt=0, le=64)
    max_file_bytes: int = Field(gt=0, le=16 * 1024 * 1024)
    max_entry_bytes: int = Field(gt=0, le=4 * 1024 * 1024)
    max_envelope_bytes: int = Field(gt=0, le=16 * 1024 * 1024)
    max_scan_files: int = Field(gt=0, le=5000)
    max_results: int = Field(gt=0, le=100)


class Stage2RoutePlan(FrozenModel):
    """A v1 ReadPlan plus all stage 2 execution semantics."""

    schema_version: Literal["1"] = "1"
    mode: ExecutionMode
    v1_plan: ReadPlan
    execution_config_hash: Sha256
    profile_hash: Sha256
    plan_hash: Sha256

    @model_validator(mode="after")
    def validate_plan_hash(self) -> "Stage2RoutePlan":
        if self.plan_hash != sha256_json(self.hashed_payload()):
            raise ValueError("stage 2 plan hash mismatch")
        return self

    def hashed_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"plan_hash"})

    @property
    def route_id(self) -> str:
        return identity.route_id(self.plan_hash)

    @property
    def request_id(self) -> str:
        return identity.request_id(self.v1_plan.request_hash)

    @property
    def steps(self) -> tuple[ReadStep, ...]:
        return self.v1_plan.steps


class ManifestStep(FrozenModel):
    step_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    route_step_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    executor: ExecutorKind
    adapter: AdapterKind
    operation: Stage2Operation
    arguments: dict[str, Any] = Field(default_factory=dict)
    on_empty: EmptyAction
    on_unavailable: EmptyAction
    read_only: Literal[True] = True


class ExecutionManifest(FrozenModel):
    """Complete ordered schedule; only two derived identity fields leave its hash."""

    schema_version: Literal["1"] = "1"
    mode: ExecutionMode
    read_only: Literal[True] = True
    request_id: str = Field(pattern=r"^req_[0-9a-f]{64}$")
    route_id: str = Field(pattern=r"^route_[0-9a-f]{64}$")
    execution_id: str = Field(pattern=r"^exe_[0-9a-f]{64}$")
    request_hash: Sha256
    plan_hash: Sha256
    manifest_hash: Sha256
    tool_profile: Literal["stage2-readonly-v1"] = "stage2-readonly-v1"
    profile_hash: Sha256
    execution_config_hash: Sha256
    limits: ExecutionLimits
    steps: tuple[ManifestStep, ...] = Field(min_length=1, max_length=32)

    def hashed_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"execution_id", "manifest_hash"})

    def trace_id(self) -> str:
        return identity.trace_id(
            request_id_value=self.request_id,
            route_id_value=self.route_id,
            execution_id_value=self.execution_id,
        )

    @model_validator(mode="after")
    def validate_step_ids(self) -> "ExecutionManifest":
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("Manifest step IDs must be unique")
        if len(self.steps) > self.limits.max_steps:
            raise ValueError("Manifest exceeds max_steps")
        return self


class PlannedToolCall(FrozenModel):
    """One fully materialized call generated solely by the closed profile."""

    call_id: str = Field(min_length=1, max_length=128)
    tool_id: str = Field(min_length=1, max_length=128)
    inner_tool_name: str | None = Field(default=None, max_length=128)
    arguments: dict[str, Any]
    timeout_seconds: int = Field(gt=0, le=60)


class ManifestVerification(FrozenModel):
    valid: Literal[True] = True
    request_id: str
    route_id: str
    execution_id: str
    manifest_hash: Sha256


__all__ = [
    "AdapterKind",
    "CommitSha",
    "EntryCandidate",
    "ExecutionLimits",
    "ExecutionManifest",
    "ExecutionMode",
    "ExecutorKind",
    "ManifestStep",
    "ManifestVerification",
    "PlannedToolCall",
    "Stage2Operation",
    "Stage2RoutePlan",
    "StepBinding",
    "StoreKind",
]
