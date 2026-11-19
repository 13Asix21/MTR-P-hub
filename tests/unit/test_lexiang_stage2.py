"""Lexiang space/search/entry normalization, nullability, and partial failures."""

from __future__ import annotations

from mtrp_hub.application_stage2 import QueryFinalizationService
from mtrp_hub.domain.enums import Intent
from mtrp_hub.execution.envelope import RawResponse, RawToolOutcome
from mtrp_hub.execution.models import Stage2Operation
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader
from mtrp_hub.fixtures.stage2_schemas import Stage2Fixture

from tests.conftest import ROOT


def lexiang_response(data) -> RawToolOutcome:  # type: ignore[no-untyped-def]
    return RawToolOutcome(response=RawResponse(kind="json", data={"code": 0, "message": "success", "data": data, "request_id": "synthetic"}))


def space(space_id: str = "0" * 32) -> RawToolOutcome:
    return lexiang_response({"space": {"id": space_id, "team_id": "0" * 32, "root_entry_id": "0" * 32}, "config": {}})


def run(request_factory, monkeypatch, fixture: Stage2Fixture):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(ROOT)
    request = request_factory(intent=Intent.GENERAL_KNOWLEDGE, project_id=None, repo_id=None)
    return QueryFinalizationService().run_fixture(request, fixture)


def test_nonempty_score_snapshot_and_content_hash_scope(request_factory, monkeypatch) -> None:
    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-m-nonempty.json")
    response = run(request_factory, monkeypatch, fixture)
    resource = next(item for item in response.result.resources if item.source_ref.store.value == "M")
    evidence = next(item for item in response.result.evidence if item.source_ref.store.value == "M")
    assert resource.score == 0.75 and resource.score_source == "text_search"
    assert resource.snapshot_ref.kind.value == "content_sha256"
    assert evidence.content_hash_scope == "response_utf8"
    assert evidence.content_sha256 == "ecf357e47a306367ba946fdc506d966c42e00a61d662f2ba72f8a6d6abff5070"


def test_missing_score_and_version_remain_null(request_factory, monkeypatch) -> None:
    entry_id = "2" * 32
    body = "Synthetic unversioned body.\n"
    fixture = Stage2Fixture(
        fixture_id="unversioned",
        responses={
            Stage2Operation.LEXIANG_SEARCH_TEXT: (lexiang_response({"entries": [{"entry_id": entry_id, "title": "Synthetic", "score": "invalid", "version": None, "revision": None, "provider_content_sha256": None}]}),),
            Stage2Operation.LEXIANG_READ_ENTRIES: (
                lexiang_response({"entry": {"entry_id": entry_id, "space_id": "0" * 32, "entry_type": "doc", "name": "Synthetic", "version": None, "revision": None, "provider_content_sha256": None}}),
                lexiang_response({"entry_id": entry_id, "content_format": "markdown", "content_text": body}),
            ),
        },
    )
    response = run(request_factory, monkeypatch, fixture)
    resource = next(item for item in response.result.resources if item.source_ref.store.value == "M")
    assert resource.score is None and resource.score_source == "not_provided"
    assert resource.snapshot_ref is None
    operation = next(item for item in response.step_results[0].operations if item.step_id.endswith("search-text"))
    assert operation.score_available_count == 0


def test_space_mismatch_skips_all_entry_calls_and_continues_t(request_factory, monkeypatch) -> None:
    fixture = Stage2Fixture(
        fixture_id="space-mismatch",
        responses={Stage2Operation.LEXIANG_RESOLVE_SPACE: (space("f" * 32),)},
    )
    response = run(request_factory, monkeypatch, fixture)
    m_step = response.step_results[0]
    assert m_step.status.value == "DEGRADED" and m_step.reason_code == "SPACE_MISMATCH"
    assert m_step.resource_count == 0
    assert all(operation.tool_call_count == 0 for operation in m_step.operations[1:])
    assert response.step_results[1].resource_count == 1


def test_malformed_entry_is_not_empty_and_does_not_drag_down_t(request_factory, monkeypatch) -> None:
    entry_id = "1" * 32
    fixture = Stage2Fixture(
        fixture_id="bad-entry",
        responses={
            Stage2Operation.LEXIANG_SEARCH_TEXT: (lexiang_response({"entries": [{"entry_id": entry_id, "title": "Synthetic", "score": None}]}),),
            Stage2Operation.LEXIANG_READ_ENTRIES: (
                lexiang_response({"entry": {"entry_id": entry_id, "space_id": "0" * 32, "entry_type": "doc", "name": "Synthetic"}}),
                lexiang_response({"entry_id": entry_id, "content_format": "unknown", "content_text": "synthetic"}),
            ),
        },
    )
    response = run(request_factory, monkeypatch, fixture)
    assert response.step_results[0].status.value == "DEGRADED"
    assert response.step_results[0].reason_code in ("M_ENTRIES_INVALID", "M_ENTRY_SKIPPED")
    assert response.step_results[0].resource_count == 0
    assert response.step_results[1].resource_count == 1
