"""Canonical v2 entities with scores, execution identity, and honest hash scope."""

from __future__ import annotations

import math
from typing import Iterable, Literal

from pydantic import Field, field_validator, model_validator

from mtrp_hub.domain.enums import StoreKind
from mtrp_hub.domain.models import (
    CanonicalResult,
    Evidence,
    FrozenModel,
    Relation,
    Resource,
    Sha256,
)


class ResourceV2(Resource):
    score: float | None = None
    score_source: Literal["text_search", "vector_search", "not_provided"] = "not_provided"
    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{64}$")
    step_id: str = Field(min_length=1, max_length=64)
    operation_id: str = Field(min_length=1, max_length=64)

    @field_validator("score")
    @classmethod
    def validate_score(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
            raise ValueError("score must be finite and between zero and one")
        return value

    @model_validator(mode="after")
    def validate_score_consistency(self) -> "ResourceV2":
        if self.score is None and self.score_source != "not_provided":
            raise ValueError("missing scores must use not_provided")
        if self.score is not None and self.score_source == "not_provided":
            raise ValueError("a score requires its source")
        return self

    def to_legacy(self) -> Resource:
        """Explicitly discard only v2 fields; legacy CLI never calls this."""
        return Resource(**{name: getattr(self, name) for name in Resource.model_fields})


class EvidenceV2(Evidence):
    operation_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(pattern=r"^req_[0-9a-f]{64}$")
    route_id: str = Field(pattern=r"^route_[0-9a-f]{64}$")
    execution_id: str = Field(pattern=r"^exe_[0-9a-f]{64}$")
    trace_id: str = Field(pattern=r"^trace_[0-9a-f]{64}$")
    content_hash_scope: Literal["raw_bytes", "response_utf8"]
    content_format: Literal["markdown", "html", "ocr_text", "utf8_text"] | None = None

    def to_legacy(self) -> Evidence:
        return Evidence(**{name: getattr(self, name) for name in Evidence.model_fields})


class CanonicalResultV2(FrozenModel):
    resources: tuple[ResourceV2, ...] = ()
    relations: tuple[Relation, ...] = ()
    evidence: tuple[EvidenceV2, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> "CanonicalResultV2":
        resource_ids = [item.resource_id for item in self.resources]
        relation_ids = [item.relation_id for item in self.relations]
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(set(resource_ids)) != len(resource_ids) or len(set(relation_ids)) != len(relation_ids) or len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("Canonical v2 IDs must be unique")
        resource_set = set(resource_ids)
        evidence_set = set(evidence_ids)
        referenced: set[str] = set()
        snapshots: dict[tuple[str, str, str], str | None] = {}
        for resource in self.resources:
            if not set(resource.evidence_ids).issubset(evidence_set):
                raise ValueError("Resource references missing Evidence")
            referenced.update(resource.evidence_ids)
            resource_snapshot = resource.snapshot_ref.value if resource.snapshot_ref else None
            if resource.source_ref.store is StoreKind.P:
                key = (resource.step_id, resource.source_ref.logical_key, resource.source_ref.store.value)
                snapshot = resource_snapshot
                if key in snapshots and snapshots[key] != snapshot:
                    raise ValueError("SNAPSHOT_CONFLICT")
                snapshots[key] = snapshot
        for relation in self.relations:
            if relation.from_resource_id not in resource_set or relation.to_resource_id not in resource_set:
                raise ValueError("Relation has a dangling Resource endpoint")
            if not set(relation.evidence_ids).issubset(evidence_set):
                raise ValueError("Relation references missing Evidence")
            referenced.update(relation.evidence_ids)
        if evidence_set != referenced:
            raise ValueError("orphaned Evidence is not allowed")
        traces = {item.trace_id for item in self.resources} | {item.trace_id for item in self.evidence}
        executions = {item.execution_id for item in self.evidence}
        if len(traces) > 1 or len(executions) > 1:
            raise ValueError("Canonical v2 execution/trace identity must be consistent")
        return self

    @classmethod
    def create(
        cls,
        resources: Iterable[ResourceV2] = (),
        relations: Iterable[Relation] = (),
        evidence: Iterable[EvidenceV2] = (),
    ) -> "CanonicalResultV2":
        ordered_resources = tuple(
            sorted(
                resources,
                key=lambda item: (
                    item.source_ref.store.value,
                    item.source_ref.logical_key,
                    item.snapshot_ref.kind.value if item.snapshot_ref else "",
                    item.snapshot_ref.value if item.snapshot_ref else "",
                    item.operation_id,
                    item.resource_id,
                ),
            )
        )
        return cls(
            resources=ordered_resources,
            relations=tuple(sorted(relations, key=lambda item: item.relation_id)),
            evidence=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
        )

    def to_legacy(self) -> CanonicalResult:
        return CanonicalResult.create(
            (item.to_legacy() for item in self.resources),
            self.relations,
            (item.to_legacy() for item in self.evidence),
        )


__all__ = ["CanonicalResultV2", "EvidenceV2", "Relation", "ResourceV2", "Sha256"]
