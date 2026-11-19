"""Frozen canonical entities, registry entities, and reference validation."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Iterable, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from mtrp_hub.canonical import stable_id
from mtrp_hub.domain.enums import (
    LocationKind,
    LocationRole,
    ProposalOperation,
    ProposalStatus,
    RepositoryState,
    ResourceType,
    SnapshotKind,
    StoreKind,
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def validate_safe_id(value: str) -> str:
    """Validate a registry identifier without touching the filesystem."""
    if not _ID_RE.fullmatch(value) or ".." in value:
        raise ValueError("must be a lowercase ASCII registry ID without '..'")
    return value


def validate_relative_path(value: str) -> str:
    """Return an NFC, POSIX-style repository-relative safe path."""
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or normalized != value.strip():
        raise ValueError("path must be non-empty and have no surrounding whitespace")
    if "\\" in normalized or _URL_RE.match(normalized) or _DRIVE_RE.match(normalized):
        raise ValueError("path must be relative and cannot contain URL/drive syntax")
    if normalized.startswith("/") or any(ord(char) < 32 for char in normalized):
        raise ValueError("path must be relative and contain no control characters")
    parts = normalized.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("path contains an empty, current, or parent segment")
    if "//" in normalized or normalized.endswith("/"):
        raise ValueError("path contains an empty segment")
    return normalized


def validate_ref(value: str) -> str:
    """Validate a logical Git ref while permitting hierarchical branch names."""
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized != value.strip()
        or normalized.startswith(("/", "-"))
        or normalized.endswith(("/", ".", ".lock"))
        or ".." in normalized
        or "\\" in normalized
        or ":" in normalized
        or "@{" in normalized
        or any(ord(char) < 32 or char == "\x7f" for char in normalized)
        or any(part in ("", ".", "..") for part in normalized.split("/"))
    ):
        raise ValueError("invalid Git ref")
    return normalized


def validate_commit(value: str) -> str:
    """Require a full lowercase SHA-1 commit identifier."""
    if not _COMMIT_RE.fullmatch(value):
        raise ValueError("commit must be a full 40-character lowercase SHA-1")
    return value


def validate_sha256(value: str) -> str:
    """Require a full lowercase SHA-256 digest."""
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("expected a full lowercase SHA-256 digest")
    return value


SafeId = Annotated[str, AfterValidator(validate_safe_id)]
RelativePath = Annotated[str, AfterValidator(validate_relative_path)]
CommitSha = Annotated[str, AfterValidator(validate_commit)]
Sha256 = Annotated[str, AfterValidator(validate_sha256)]
GitRef = Annotated[str, AfterValidator(validate_ref)]


class FrozenModel(BaseModel):
    """Base DTO configuration shared by all canonical models."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=False)


class SourceRef(FrozenModel):
    """Logical source identity; never a fetchable URL or local path."""

    store: StoreKind
    store_id: SafeId
    logical_key: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_logical_key(self) -> "SourceRef":
        value = self.logical_key
        if (
            _URL_RE.match(value)
            or value.lower().startswith("file:")
            or _DRIVE_RE.match(value)
            or value.startswith(("/", "\\"))
            or "\\" in value
            or ".." in value
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("logical_key must be opaque and non-fetchable")
        return self


class SnapshotRef(FrozenModel):
    """A precise content or Git snapshot."""

    kind: SnapshotKind
    value: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_snapshot_value(self) -> "SnapshotRef":
        if self.kind is SnapshotKind.GIT_COMMIT:
            validate_commit(self.value)
        elif self.kind in (SnapshotKind.CONTENT_SHA256, SnapshotKind.FIXTURE_SNAPSHOT):
            validate_sha256(self.value)
        return self


class ProjectCoordinate(FrozenModel):
    """Resolved repository identity with explicit remote/local separation."""

    project_id: SafeId
    repo_id: SafeId
    location_id: SafeId
    location_kind: LocationKind
    ref: GitRef
    commit: CommitSha | None = None
    path: RelativePath | None = None
    workspace_id: SafeId | None = None
    repository_state: RepositoryState

    @model_validator(mode="after")
    def validate_identity(self) -> "ProjectCoordinate":
        if self.repository_state in (RepositoryState.EMPTY, RepositoryState.UNKNOWN) and self.commit is not None:
            raise ValueError("empty/unknown repositories cannot have a commit")
        if self.location_kind is LocationKind.GITHUB and self.workspace_id is not None:
            raise ValueError("GitHub coordinates cannot reference a Workspace")
        if self.location_kind is LocationKind.LOCAL_GIT and self.workspace_id is None:
            raise ValueError("LocalGit coordinates require a registered Workspace")
        return self


class Resource(FrozenModel):
    """Canonical resource metadata; deliberately excludes body/content text."""

    resource_id: str = Field(min_length=1, max_length=128)
    resource_type: ResourceType
    title: str = Field(min_length=1, max_length=512)
    content_ref: str = Field(min_length=1, max_length=512)
    source_ref: SourceRef
    snapshot_ref: SnapshotRef | None = None
    project_coordinate: ProjectCoordinate | None = None
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_resource_source(self) -> "Resource":
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids must be unique")
        if self.source_ref.store is StoreKind.P and self.project_coordinate is None:
            raise ValueError("P resources require a project coordinate")
        if self.source_ref.store is not StoreKind.P and self.project_coordinate is not None:
            raise ValueError("non-P resources cannot carry a project coordinate")
        return self


class Relation(FrozenModel):
    """A typed relationship between two resources, supported by evidence."""

    relation_id: str = Field(min_length=1, max_length=128)
    relation_type: str = Field(min_length=1, max_length=128)
    from_resource_id: str = Field(min_length=1, max_length=128)
    to_resource_id: str = Field(min_length=1, max_length=128)
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class Evidence(FrozenModel):
    """Decision-traceable source evidence without content bodies or secrets."""

    evidence_id: str = Field(min_length=1, max_length=128)
    source_ref: SourceRef
    snapshot_ref: SnapshotRef | None = None
    project_coordinate: ProjectCoordinate | None = None
    path: RelativePath | None = None
    entry_id: SafeId | None = None
    content_sha256: Sha256 | None = None
    request_hash: Sha256
    plan_hash: Sha256
    step_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_evidence_locator(self) -> "Evidence":
        if self.path is None and self.entry_id is None:
            raise ValueError("Evidence requires path or entry_id")
        if self.source_ref.store is StoreKind.P:
            coordinate = self.project_coordinate
            if coordinate is None or self.path is None:
                raise ValueError("P Evidence requires a project coordinate and path")
            if coordinate.path != self.path:
                raise ValueError("P Evidence path must match its coordinate path")
            if coordinate.commit is None or self.snapshot_ref is None:
                raise ValueError("P Evidence requires a known commit snapshot")
            if self.snapshot_ref.kind is not SnapshotKind.GIT_COMMIT:
                raise ValueError("P Evidence snapshot must be a Git commit")
            if self.snapshot_ref.value != coordinate.commit:
                raise ValueError("P Evidence snapshot and coordinate commit differ")
        elif self.project_coordinate is not None:
            raise ValueError("non-P Evidence cannot carry a project coordinate")
        return self


class CanonicalResult(FrozenModel):
    """An aggregate with unique IDs and no dangling or orphaned references."""

    resources: tuple[Resource, ...] = ()
    relations: tuple[Relation, ...] = ()
    evidence: tuple[Evidence, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> "CanonicalResult":
        resource_ids = [item.resource_id for item in self.resources]
        relation_ids = [item.relation_id for item in self.relations]
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("duplicate Resource ID")
        if len(set(relation_ids)) != len(relation_ids):
            raise ValueError("duplicate Relation ID")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("duplicate Evidence ID")
        resource_set = set(resource_ids)
        evidence_set = set(evidence_ids)
        referenced_evidence: set[str] = set()
        for resource in self.resources:
            if not set(resource.evidence_ids).issubset(evidence_set):
                raise ValueError("Resource references missing Evidence")
            referenced_evidence.update(resource.evidence_ids)
        for relation in self.relations:
            if relation.from_resource_id not in resource_set or relation.to_resource_id not in resource_set:
                raise ValueError("Relation has a dangling Resource endpoint")
            if not set(relation.evidence_ids).issubset(evidence_set):
                raise ValueError("Relation references missing Evidence")
            referenced_evidence.update(relation.evidence_ids)
        if evidence_set != referenced_evidence:
            raise ValueError("orphaned Evidence is not allowed")
        return self

    @classmethod
    def create(
        cls,
        resources: Iterable[Resource] = (),
        relations: Iterable[Relation] = (),
        evidence: Iterable[Evidence] = (),
    ) -> "CanonicalResult":
        """Sort collections by their canonical shared keys before validation."""
        ordered_resources = tuple(
            sorted(
                resources,
                key=lambda item: (
                    item.source_ref.store.value,
                    item.resource_id,
                    item.snapshot_ref.kind.value if item.snapshot_ref else "",
                    item.snapshot_ref.value if item.snapshot_ref else "",
                    item.content_ref,
                ),
            )
        )
        return cls(
            resources=ordered_resources,
            relations=tuple(sorted(relations, key=lambda item: item.relation_id)),
            evidence=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
        )


class WriteProposal(FrozenModel):
    """A non-executable proposal data contract; no execution methods exist."""

    proposal_id: str = Field(min_length=1, max_length=128)
    target: ProjectCoordinate
    operation: ProposalOperation
    reason: str = Field(min_length=1, max_length=1024)
    base_snapshot: SnapshotRef | None = None
    status: ProposalStatus
    approval_required: bool
    executable: bool

    @model_validator(mode="after")
    def validate_non_executable(self) -> "WriteProposal":
        if self.status is not ProposalStatus.DRAFT or not self.approval_required or self.executable:
            raise ValueError("WriteProposal must remain draft, approval-required, and non-executable")
        if self.operation is not ProposalOperation.CREATE and self.base_snapshot is None:
            raise ValueError("update/delete proposals require a base snapshot")
        return self


class SnapshotPin(FrozenModel):
    """An optional trusted Registry pin for a known Git snapshot."""

    ref: GitRef
    commit: CommitSha


class Location(FrozenModel):
    """A repository location; remote metadata is never passed to FakeAdapter."""

    location_id: SafeId
    kind: LocationKind
    role: LocationRole
    remote: str | None = None
    state: RepositoryState = RepositoryState.UNKNOWN
    snapshots: tuple[SnapshotPin, ...] = ()

    @model_validator(mode="after")
    def validate_location_state(self) -> "Location":
        if self.state in (RepositoryState.EMPTY, RepositoryState.UNKNOWN) and self.snapshots:
            raise ValueError("empty/unknown locations cannot contain snapshot pins")
        refs = [pin.ref for pin in self.snapshots]
        if len(set(refs)) != len(refs):
            raise ValueError("snapshot refs must be unique per Location")
        if self.kind is LocationKind.LOCAL_GIT and self.remote is not None:
            raise ValueError("LocalGit Location cannot have a remote URL")
        return self


class Workspace(FrozenModel):
    """Explicit local registration metadata; loading performs no path probing."""

    workspace_id: SafeId
    location_id: SafeId
    root_path: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_root_metadata(self) -> "Workspace":
        if "\x00" in self.root_path or any(ord(char) < 32 for char in self.root_path):
            raise ValueError("Workspace root_path contains a control character")
        return self


class Repository(FrozenModel):
    """Repository registry entity with exactly one authoritative Location."""

    repo_id: SafeId
    default_branch: GitRef
    authoritative_location_id: SafeId
    adr_paths: tuple[RelativePath, ...] = ()
    locations: tuple[Location, ...]
    workspaces: tuple[Workspace, ...] = ()


class Project(FrozenModel):
    """A project containing uniquely identified repositories."""

    project_id: SafeId
    repositories: tuple[Repository, ...]


class StoreDefinition(FrozenModel):
    """A logical store whose only stage 0+1 adapter is read-only Fake."""

    store_id: SafeId
    kind: StoreKind
    adapter: str
    read_only: bool
    vault_id: SafeId | None = None
    view_tag: Literal["mtr/trial", "mtr/rarely"] | None = None


def resource_identifier(
    source: SourceRef,
    snapshot: SnapshotRef | None,
    content_sha256: str,
) -> str:
    """Derive a Resource ID that cannot merge distinct snapshots."""
    return stable_id("res-", {"source": source, "snapshot": snapshot, "content_sha256": content_sha256})
