"""GitHub empty repository, full SHA, text/base64 content, and A/B isolation."""

from __future__ import annotations

import hashlib

from mtrp_hub.application_stage2 import QueryFinalizationService
from mtrp_hub.execution.models import Stage2Operation
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader
from mtrp_hub.fixtures.stage2_schemas import Stage2Fixture

from tests.conftest import ROOT


def run(request_factory, monkeypatch, fixture: Stage2Fixture):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(ROOT)
    return QueryFinalizationService().run_fixture(request_factory(), fixture)


def test_empty_repository_has_null_commit_and_no_p_resource(request_factory, monkeypatch) -> None:
    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-empty.json")
    response = run(request_factory, monkeypatch, fixture)
    p_step = response.step_results[0]
    assert p_step.status.value == "EMPTY" and p_step.reason_code == "EMPTY_REPOSITORY"
    assert p_step.coordinate.commit is None
    assert all(item.source_ref.store.value != "P" for item in response.result.resources)
    assert response.step_results[1].reason_code == "M_EMPTY"


def test_a_b_commit_snapshots_never_merge(request_factory, monkeypatch) -> None:
    original = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-project-context.json")
    first = run(request_factory, monkeypatch, original)
    data = original.model_dump(mode="json")
    data["fixture_id"] = "stage2-project-context-b"
    data["responses"]["github_list_branches"][0]["response"]["data"][0]["sha"] = "b" * 40
    data["responses"]["github_resolve_ref"][0]["response"]["data"]["sha"] = "b" * 40
    second = run(request_factory, monkeypatch, Stage2Fixture.model_validate(data))
    first_p = next(item for item in first.result.resources if item.source_ref.store.value == "P")
    second_p = next(item for item in second.result.resources if item.source_ref.store.value == "P")
    assert first_p.snapshot_ref.value == "a" * 40 and second_p.snapshot_ref.value == "b" * 40
    assert first_p.resource_id != second_p.resource_id
    first_evidence = next(item for item in first.result.evidence if item.source_ref.store.value == "P")
    second_evidence = next(item for item in second.result.evidence if item.source_ref.store.value == "P")
    assert first_evidence.evidence_id != second_evidence.evidence_id


def test_controlled_text_hash_excludes_only_verified_header(request_factory, monkeypatch) -> None:
    original = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-project-context.json")
    data = original.model_dump(mode="json")
    data["responses"]["github_read_file"][0]["response"] = {
        "kind": "text",
        "text": "successfully downloaded text file (SHA: " + "a" * 40 + ")\nREADME synthetic text\n",
    }
    response = run(request_factory, monkeypatch, Stage2Fixture.model_validate(data))
    evidence = next(item for item in response.result.evidence if item.source_ref.store.value == "P")
    assert evidence.content_hash_scope == "response_utf8"
    assert evidence.content_sha256 == hashlib.sha256(b"README synthetic text\n").hexdigest()
    assert evidence.content_sha256 != hashlib.sha256(data["responses"]["github_read_file"][0]["response"]["text"].encode()).hexdigest()


def test_base64_path_mismatch_cannot_replace_planned_coordinate(request_factory, monkeypatch) -> None:
    original = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-project-context.json")
    data = original.model_dump(mode="json")
    data["responses"]["github_read_file"][0]["response"]["data"]["path"] = "other.md"
    response = run(request_factory, monkeypatch, Stage2Fixture.model_validate(data))
    assert response.step_results[0].status.value == "DEGRADED"
    assert all(item.source_ref.store.value != "P" for item in response.result.resources)
