"""Five-intent v2 routing, empty fallback, and Canonical reference integration."""

from __future__ import annotations

import pytest

from mtrp_hub.application_stage2 import QueryFinalizationService
from mtrp_hub.domain.enums import Intent
from mtrp_hub.domain.requests_v2 import StepStatusV2
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader

from tests.conftest import ROOT


def execute(request_factory, monkeypatch, intent: Intent, fixture_name: str):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(ROOT)
    needs_p = intent in (Intent.PROJECT_CONTEXT, Intent.DECISION_HISTORY)
    request = request_factory(
        intent=intent,
        project_id="example-project" if needs_p else None,
        repo_id="example-repo" if needs_p else None,
    )
    fixture = Stage2FixtureLoader().load(f"tests/fixtures/stage2/{fixture_name}")
    return QueryFinalizationService().run_fixture(request, fixture)


def test_project_context_preserves_p_m_t_and_m_empty_continues(request_factory, monkeypatch) -> None:
    response = execute(request_factory, monkeypatch, Intent.PROJECT_CONTEXT, "stage2-project-context.json")
    assert [step.step_id for step in response.step_results] == ["s01-p", "s02-m", "s03-t"]
    assert response.step_results[0].status is StepStatusV2.OK
    assert response.step_results[1].status is StepStatusV2.EMPTY
    assert response.step_results[1].reason_code == "M_EMPTY"
    assert response.step_results[1].next_step_id == "s03-t"
    assert response.step_results[2].resource_count == 1
    assert {resource.source_ref.store.value for resource in response.result.resources} == {"P", "T"}


def test_general_knowledge_has_m_hit_and_never_calls_p(request_factory, monkeypatch) -> None:
    response = execute(request_factory, monkeypatch, Intent.GENERAL_KNOWLEDGE, "stage2-m-nonempty.json")
    assert [step.step_id for step in response.step_results] == ["s01-m", "s02-t"]
    assert response.step_results[0].status is StepStatusV2.OK
    m_resource = next(item for item in response.result.resources if item.source_ref.store.value == "M")
    assert m_resource.score == pytest.approx(0.75)
    assert m_resource.score_source == "text_search"
    assert all(step.store.value != "P" for step in response.step_results)


def test_method_skill_uses_m_then_t(request_factory, monkeypatch) -> None:
    response = execute(request_factory, monkeypatch, Intent.METHOD_SKILL, "stage2-empty.json")
    assert [step.step_id for step in response.step_results] == ["s01-m", "s02-t"]
    assert response.step_results[0].reason_code == "M_EMPTY"


def test_decision_history_keeps_p_t_m_not_prefetched_order(request_factory, monkeypatch) -> None:
    response = execute(request_factory, monkeypatch, Intent.DECISION_HISTORY, "stage2-decision-history.json")
    assert [step.step_id for step in response.step_results] == ["s01-p", "s02-t", "s03-m"]
    assert response.step_results[0].next_step_id == "s02-t"
    assert response.step_results[1].next_step_id == "s03-m"
    assert response.step_results[2].reason_code == "M_EMPTY"


def test_cold_detail_uses_only_r_then_t_and_r_hit_does_not_stop(request_factory, monkeypatch) -> None:
    response = execute(request_factory, monkeypatch, Intent.COLD_DETAIL, "stage2-empty.json")
    assert [step.step_id for step in response.step_results] == ["s01-r", "s02-t"]
    assert response.step_results[0].resource_count == 1
    assert response.step_results[0].next_step_id == "s02-t"
    assert {resource.source_ref.store.value for resource in response.result.resources} == {"R", "T"}
    assert all(step.store.value not in ("P", "M") for step in response.step_results)
