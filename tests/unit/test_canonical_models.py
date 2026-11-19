"""Canonical model validation, snapshots, and aggregate reference tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mtrp_hub.canonical import stable_id
from mtrp_hub.domain.enums import (
    LocationKind,
    ProposalOperation,
    ProposalStatus,
    RepositoryState,
    ResourceType,
    SnapshotKind,
    StoreKind,
)
from mtrp_hub.domain.models import (
    CanonicalResult,
    Evidence,
    ProjectCoordinate,
    Relation,
    Resource,
    SnapshotRef,
    SourceRef,
    WriteProposal,
    resource_identifier,
    validate_relative_path,
)

COMMIT = "a" * 40
DIGEST = "1" * 64
REQUEST_HASH = "2" * 64
PLAN_HASH = "3" * 64


def coordinate(path: str | None = "README.md", commit: str | None = COMMIT) -> ProjectCoordinate:
    return ProjectCoordinate(
        project_id="example-project",
        repo_id="example-repo",
        location_id="remote-main",
        location_kind=LocationKind.GITHUB,
        ref="main",
        commit=commit,
        path=path,
        repository_state=RepositoryState.READY if commit else RepositoryState.UNKNOWN,
    )


def evidence(evidence_id: str = "evidence-1") -> Evidence:
    source = SourceRef(store=StoreKind.P, store_id="p", logical_key="example-repo/README.md")
    return Evidence(
        evidence_id=evidence_id,
        source_ref=source,
        snapshot_ref=SnapshotRef(kind=SnapshotKind.GIT_COMMIT, value=COMMIT),
        project_coordinate=coordinate(),
        path="README.md",
        content_sha256=DIGEST,
        request_hash=REQUEST_HASH,
        plan_hash=PLAN_HASH,
        step_id="s01-p",
    )


def resource(resource_id: str = "resource-1", evidence_id: str = "evidence-1") -> Resource:
    return Resource(
        resource_id=resource_id,
        resource_type=ResourceType.PROJECT_FILE,
        title="README",
        content_ref="fixture:opaque:0",
        source_ref=SourceRef(store=StoreKind.P, store_id="p", logical_key="example-repo/README.md"),
        snapshot_ref=SnapshotRef(kind=SnapshotKind.GIT_COMMIT, value=COMMIT),
        project_coordinate=coordinate(),
        evidence_ids=(evidence_id,),
    )


def test_valid_canonical_aggregate_and_relation() -> None:
    first = resource()
    second = resource("resource-2")
    relation = Relation(
        relation_id="relation-1",
        relation_type="documents",
        from_resource_id=first.resource_id,
        to_resource_id=second.resource_id,
        evidence_ids=("evidence-1",),
    )
    result = CanonicalResult.create((second, first), (relation,), (evidence(),))
    assert [item.resource_id for item in result.resources] == ["resource-1", "resource-2"]
    assert result.relations == (relation,)


@pytest.mark.parametrize("path", ["../secret", "/absolute", "C:/private", "a//b", "a/./b", "https://x.invalid/a"])
def test_relative_path_rejects_escape_absolute_and_url(path: str) -> None:
    with pytest.raises(ValidationError):
        ProjectCoordinate(
            project_id="example-project",
            repo_id="example-repo",
            location_id="remote-main",
            location_kind=LocationKind.GITHUB,
            ref="main",
            commit=COMMIT,
            path=path,
            repository_state=RepositoryState.READY,
        )


def test_empty_coordinate_cannot_have_fake_commit() -> None:
    with pytest.raises(ValidationError):
        ProjectCoordinate(
            project_id="example-project",
            repo_id="example-repo",
            location_id="remote-main",
            location_kind=LocationKind.GITHUB,
            ref="main",
            commit=COMMIT,
            repository_state=RepositoryState.EMPTY,
        )


def test_extra_fields_and_mutation_are_forbidden() -> None:
    item = resource()
    with pytest.raises(ValidationError):
        Resource(**{**item.model_dump(), "body": "forbidden"})
    with pytest.raises(ValidationError):
        item.title = "changed"


def test_dangling_relation_and_orphan_evidence_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CanonicalResult(
            resources=(resource(),),
            relations=(
                Relation(
                    relation_id="bad",
                    relation_type="broken",
                    from_resource_id="resource-1",
                    to_resource_id="missing",
                    evidence_ids=("evidence-1",),
                ),
            ),
            evidence=(evidence(),),
        )
    with pytest.raises(ValidationError):
        CanonicalResult(resources=(resource(),), evidence=(evidence(), evidence("orphan")))


def test_evidence_requires_locator_and_p_snapshot() -> None:
    data = evidence().model_dump()
    data["path"] = None
    with pytest.raises(ValidationError):
        Evidence(**data)
    data = evidence().model_dump()
    data["snapshot_ref"] = None
    with pytest.raises(ValidationError):
        Evidence(**data)


def test_same_branch_different_sha_never_merges_resource_identity() -> None:
    source = SourceRef(store=StoreKind.P, store_id="p", logical_key="example-repo/README.md")
    first = resource_identifier(source, SnapshotRef(kind=SnapshotKind.GIT_COMMIT, value="a" * 40), DIGEST)
    second = resource_identifier(source, SnapshotRef(kind=SnapshotKind.GIT_COMMIT, value="b" * 40), DIGEST)
    assert first != second


def test_write_proposal_is_always_draft_non_executable() -> None:
    proposal = WriteProposal(
        proposal_id="proposal-1",
        target=coordinate(path=None),
        operation=ProposalOperation.UPDATE,
        reason="Fixture-only contract",
        base_snapshot=SnapshotRef(kind=SnapshotKind.GIT_COMMIT, value=COMMIT),
        status=ProposalStatus.DRAFT,
        approval_required=True,
        executable=False,
    )
    assert proposal.executable is False
    with pytest.raises(ValidationError):
        WriteProposal(**{**proposal.model_dump(), "executable": True})
    with pytest.raises(ValidationError):
        WriteProposal(**{**proposal.model_dump(), "base_snapshot": None})
