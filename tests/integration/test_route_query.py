"""End-to-end determinism, snapshot isolation, and audit integration tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from mtrp_hub.application import RouteQueryService
from mtrp_hub.audit.sqlite import SQLiteDecisionLog

from tests.conftest import ROOT


def cli_arguments(config_dir: Path, fixture: str) -> list[str]:
    return [
        "-m", "mtrp_hub", "route", "query",
        "--config-dir", str(config_dir),
        "--query", "deterministic integration query",
        "--intent", "project_context",
        "--project", "example-project",
        "--repo", "example-repo",
        "--fixture", fixture,
        "--format", "json",
    ]


def test_three_independent_processes_are_byte_identical(config_dir: Path) -> None:
    outputs: list[bytes] = []
    for seed in ("1", "77", "random"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, *cli_arguments(config_dir, "tests/fixtures/project-context.json")],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    payload = json.loads(outputs[0])
    assert len(payload["request_hash"]) == 64
    assert len(payload["plan_hash"]) == 64
    assert len(payload["fixture"]["fixture_hash"]) == 64


def test_a_b_fixture_sha_never_merges(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    service = RouteQueryService()
    first = service.query(request_factory(fixture="tests/fixtures/p-main-sha-a.json"))
    second = service.query(request_factory(fixture="tests/fixtures/p-main-sha-b.json"))
    first_p = first.step_results[0]
    second_p = second.step_results[0]
    assert first_p.coordinate.ref == second_p.coordinate.ref == "main"
    assert first_p.coordinate.commit == "a" * 40
    assert second_p.coordinate.commit == "b" * 40
    assert first.result.resources[0].resource_id != second.result.resources[0].resource_id
    assert first.result.evidence[0].evidence_id != second.result.evidence[0].evidence_id
    assert first.plan.plan_hash == second.plan.plan_hash


def test_decision_log_explains_empty_and_degraded_steps(request_factory, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    response = RouteQueryService().query(request_factory(fixture="tests/fixtures/m-empty-p-empty.json"))
    log = SQLiteDecisionLog(config_dir / ".mtrp" / "decision-log.sqlite3", create=False)
    events = log.find(plan_hash=response.plan.plan_hash)
    step_reasons = [event.reason_code for event in events if event.event_type.value == "STEP_FINISHED"]
    assert step_reasons[:2] == ["P_EMPTY", "M_EMPTY"]
    assert events[-1].status == "ok"


def test_empty_repository_is_not_reported_as_readable_p_asset(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    response = RouteQueryService().query(request_factory(fixture="tests/fixtures/p-empty-repository.json"))
    assert response.step_results[0].reason_code.value == "EMPTY_REPOSITORY"
    assert response.step_results[0].coordinate.commit is None
    assert all(item.source_ref.store.value != "P" for item in response.result.resources)
