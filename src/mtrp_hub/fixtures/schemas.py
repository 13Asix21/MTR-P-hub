"""Strict Fixture DTOs; extra fields cannot inject routes, adapters, or writes."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from mtrp_hub.domain.enums import StrEnum
from mtrp_hub.domain.models import (
    CommitSha,
    FrozenModel,
    GitRef,
    RelativePath,
    SafeId,
    Sha256,
)


_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bauthorization\s*:\s*(?:bearer|basic|token)\b\s*[^\s,;]+",
        r"\b(?:bearer|basic)\s+[a-z0-9._~+/=-]{4,}",
        r"\b(?:token|secret|password|passwd|pwd|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|id[_-]?token|cookie|set-cookie|session(?:_?id)?|credentials?)\s*[:=]\s*[^\s,;]+",
        r"\beyj[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b",
        r"\b(?:gh[pousr]_[a-z0-9]{16,}|github_pat_[a-z0-9_]{16,}|akia[0-9a-z]{16}|sk-[a-z0-9]{16,})\b",
        r"-----begin(?: [a-z0-9]+)? private key-----",
        r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@",
    )
)


def _contains_secret_like_text(value: Any) -> bool:
    """Recursively inspect Fixture text without retaining or reporting matches."""
    if isinstance(value, BaseModel):
        return _contains_secret_like_text(value.model_dump(mode="json"))
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_PATTERNS)
    if isinstance(value, dict):
        return any(_contains_secret_like_text(key) or _contains_secret_like_text(child) for key, child in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_like_text(child) for child in value)
    return False


class PFixtureStatus(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    EMPTY_REPOSITORY = "empty_repository"
    BRANCH_MISSING = "branch_missing"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"
    EMPTY_RESPONSE = "empty_response"


class MFixtureStatus(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"
    EMPTY_RESPONSE = "empty_response"


class ViewFixtureStatus(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"
    EMPTY_RESPONSE = "empty_response"


class FixtureRelation(FrozenModel):
    """A logical relation whose endpoints must resolve within the same step."""

    relation_type: str = Field(min_length=1, max_length=128)
    from_key: str = Field(min_length=1, max_length=512)
    to_key: str = Field(min_length=1, max_length=512)


class PRepositoryFixture(FrozenModel):
    project_id: SafeId
    repo_id: SafeId
    location_id: SafeId
    ref: GitRef
    commit: CommitSha | None = None


class PResourceFixture(FrozenModel):
    """A complete P snapshot coordinate plus repository-relative content locator."""

    project_id: SafeId
    repo_id: SafeId
    location_id: SafeId
    ref: GitRef
    commit: CommitSha
    path: RelativePath
    content_sha256: Sha256
    title: str = Field(min_length=1, max_length=512)


class PFixture(FrozenModel):
    status: PFixtureStatus = PFixtureStatus.EMPTY
    repository: PRepositoryFixture | None = None
    resources: tuple[PResourceFixture, ...] = ()
    relations: tuple[FixtureRelation, ...] = ()

    @model_validator(mode="after")
    def validate_status_data(self) -> "PFixture":
        if self.status is not PFixtureStatus.READY and (self.resources or self.relations):
            raise ValueError("non-ready P fixtures cannot contain resources or relations")
        if self.status in (PFixtureStatus.EMPTY, PFixtureStatus.EMPTY_REPOSITORY):
            if self.repository is not None and self.repository.commit is not None:
                raise ValueError("empty P fixtures cannot declare a commit")
        if self.status is PFixtureStatus.READY:
            if self.repository is None or self.repository.commit is None:
                raise ValueError("ready P fixtures require a repository and commit")
            for resource in self.resources:
                identity = (resource.project_id, resource.repo_id, resource.location_id, resource.ref, resource.commit)
                expected = (
                    self.repository.project_id,
                    self.repository.repo_id,
                    self.repository.location_id,
                    self.repository.ref,
                    self.repository.commit,
                )
                if identity != expected:
                    raise ValueError("P resource snapshot must match its repository fixture")
        return self


class MResourceFixture(FrozenModel):
    entry_id: SafeId
    version: str = Field(min_length=1, max_length=128)
    content_sha256: Sha256
    title: str = Field(min_length=1, max_length=512)


class MFixture(FrozenModel):
    status: MFixtureStatus = MFixtureStatus.EMPTY
    space_resolved: bool = True
    resources: tuple[MResourceFixture, ...] = ()
    relations: tuple[FixtureRelation, ...] = ()

    @model_validator(mode="after")
    def validate_status_data(self) -> "MFixture":
        if self.status is not MFixtureStatus.READY and (self.resources or self.relations):
            raise ValueError("non-ready M fixtures cannot contain resources or relations")
        return self


class ViewDocumentFixture(FrozenModel):
    path: RelativePath
    raw_markdown: str


class ViewFixture(FrozenModel):
    status: ViewFixtureStatus = ViewFixtureStatus.EMPTY
    documents: tuple[ViewDocumentFixture, ...] = ()
    relations: tuple[FixtureRelation, ...] = ()

    @model_validator(mode="after")
    def validate_status_data(self) -> "ViewFixture":
        if self.status is not ViewFixtureStatus.READY and (self.documents or self.relations):
            raise ValueError("non-ready view fixtures cannot contain documents or relations")
        return self


class FixtureBundle(FrozenModel):
    """The complete Fake input; no operation, route, adapter, URL, or tool fields exist."""

    schema_version: Literal["1"] = "1"
    fixture_id: SafeId
    p: PFixture = PFixture()
    m: MFixture = MFixture()
    t: ViewFixture = ViewFixture()
    r: ViewFixture = ViewFixture()

    @model_validator(mode="before")
    @classmethod
    def reject_secret_like_text(cls, data: Any) -> Any:
        """Fail closed before any Fixture text can become a Canonical/CLI value."""
        if _contains_secret_like_text(data):
            raise ValueError("secret-like text is not allowed in Fixture data")
        return data

    @classmethod
    def empty(cls) -> "FixtureBundle":
        """Return the deterministic default all-empty in-memory Fixture."""
        return cls(
            schema_version="1",
            fixture_id="empty-default",
            p=PFixture(status=PFixtureStatus.EMPTY),
            m=MFixture(status=MFixtureStatus.EMPTY, space_resolved=True),
            t=ViewFixture(status=ViewFixtureStatus.EMPTY),
            r=ViewFixture(status=ViewFixtureStatus.EMPTY),
        )
