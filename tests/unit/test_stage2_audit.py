"""Additive SQLite migration, four-ID trace lookup, and audit failure contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtrp_hub.application_stage2 import QueryFinalizationService
from mtrp_hub.audit.sqlite_stage2 import Stage2DecisionLog
from mtrp_hub.errors import MtrpError
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader

from tests.conftest import ROOT


def test_four_identity_queries_find_same_safe_run(request_factory, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-project-context.json")
    response = QueryFinalizationService().run_fixture(request_factory(), fixture)
    path = config_dir / ".mtrp" / "decision-log.sqlite3"
    log = Stage2DecisionLog(path, create=False)
    by_request = log.find(request_id=response.request_id)
    by_route = log.find(route_id=response.route_id)
    by_execution = log.find(execution_id=response.execution_id)
    by_trace = log.find(trace_id=response.trace_id)
    assert by_request == by_route == by_execution == by_trace
    assert by_trace[0].step_count == 3
    assert by_trace[0].events
    assert "github_list_branches" in {event.operation for event in by_trace[0].events}
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"stage2_plan_log", "stage2_execution_run", "stage2_execution_event"} <= tables


def test_trace_filter_requires_exactly_one_valid_id(config_dir: Path) -> None:
    path = config_dir / ".mtrp" / "decision-log.sqlite3"
    Stage2DecisionLog(path)
    log = Stage2DecisionLog(path, create=False)
    with pytest.raises(MtrpError):
        log.find()
    with pytest.raises(MtrpError):
        log.find(request_id="invalid")


def test_audit_write_failure_is_exit_one_before_fixture_execution(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)

    def fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise MtrpError("DECISION_LOG_WRITE_FAILED", "Audit write failed.", exit_code=1)

    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-project-context.json")
    with pytest.raises(MtrpError) as caught:
        QueryFinalizationService(decision_log_factory=fail).run_fixture(request_factory(), fixture)
    assert caught.value.exit_code == 1
    assert caught.value.error_code == "DECISION_LOG_WRITE_FAILED"
