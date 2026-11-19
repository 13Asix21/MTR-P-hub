"""Golden v1 route ordering, R placement, terminal actions, and plan hashes."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtrp_hub.canonical import canonical_json
from mtrp_hub.config.loader import ConfigLoader
from mtrp_hub.domain.enums import EmptyAction, Intent, StoreKind
from mtrp_hub.domain.requests import RouteQueryRequest
from mtrp_hub.routing.planner import ReadPolicy


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        (Intent.PROJECT_CONTEXT, ("P", "M", "T")),
        (Intent.GENERAL_KNOWLEDGE, ("M", "T")),
        (Intent.METHOD_SKILL, ("M", "T")),
        (Intent.DECISION_HISTORY, ("P", "T", "M")),
        (Intent.COLD_DETAIL, ("R", "T")),
    ],
)
def test_v1_route_order(intent: Intent, expected: tuple[str, ...], config_dir: Path) -> None:
    loaded = ConfigLoader().load(str(config_dir))
    needs_p = intent in (Intent.PROJECT_CONTEXT, Intent.DECISION_HISTORY)
    request = RouteQueryRequest(
        query="route golden",
        intent=intent,
        project_id="example-project" if needs_p else None,
        repo_id="example-repo" if needs_p else None,
        config_dir=str(config_dir),
    )
    coordinate = loaded.projects.resolve_default("example-project", "example-repo") if needs_p else None
    plan = ReadPolicy(loaded.policy).create_plan(request, coordinate, loaded.stores, loaded.projects)
    assert tuple(step.store.value for step in plan.steps) == expected
    assert plan.include_r is (intent is Intent.COLD_DETAIL)
    assert plan.steps[-1].on_empty is EmptyAction.RETURN_EMPTY
    assert all(step.on_empty is EmptyAction.CONTINUE for step in plan.steps[:-1])


def test_optional_r_is_appended_and_terminal_moves(config_dir: Path) -> None:
    loaded = ConfigLoader().load(str(config_dir))
    request = RouteQueryRequest(
        query="include rare",
        intent=Intent.GENERAL_KNOWLEDGE,
        config_dir=str(config_dir),
        include_r=True,
    )
    plan = ReadPolicy(loaded.policy).create_plan(request, None, loaded.stores, loaded.projects)
    assert tuple(step.store.value for step in plan.steps) == ("M", "T", "R")
    assert [step.step_id for step in plan.steps] == ["s01-m", "s02-t", "s03-r"]
    assert plan.steps[-2].on_empty is EmptyAction.CONTINUE
    assert plan.steps[-1].view_tag == "mtr/rarely"


def test_cold_detail_never_duplicates_r(config_dir: Path) -> None:
    loaded = ConfigLoader().load(str(config_dir))
    request = RouteQueryRequest(
        query="cold",
        intent=Intent.COLD_DETAIL,
        config_dir=str(config_dir),
        include_r=True,
    )
    plan = ReadPolicy(loaded.policy).create_plan(request, None, loaded.stores, loaded.projects)
    assert [step.store for step in plan.steps] == [StoreKind.R, StoreKind.T]


def test_plan_is_repeatable_and_excludes_bootstrap_paths_and_query(config_dir: Path) -> None:
    loaded = ConfigLoader().load(str(config_dir))
    coordinate = loaded.projects.resolve_default("example-project", "example-repo")
    first_request = RouteQueryRequest(
        query="raw query must not appear",
        intent=Intent.PROJECT_CONTEXT,
        project_id="example-project",
        repo_id="example-repo",
        config_dir=str(config_dir),
        fixture="first.json",
    )
    second_request = first_request.model_copy(update={"config_dir": "other-bootstrap", "fixture": "other.json"})
    first = ReadPolicy(loaded.policy).create_plan(first_request, coordinate, loaded.stores, loaded.projects)
    second = ReadPolicy(loaded.policy).create_plan(second_request, coordinate, loaded.stores, loaded.projects)
    assert first == second
    assert first.plan_hash == second.plan_hash
    rendered = canonical_json(first)
    assert "raw query must not appear" not in rendered
    assert "first.json" not in rendered
    assert "config_dir" not in rendered
    assert "recorded_at" not in rendered
