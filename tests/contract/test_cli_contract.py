"""CLI stdout/stderr, exit-code, text, and Decision Log inspection contracts."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mtrp_hub.cli import app

from tests.conftest import ROOT


def arguments(config_dir: Path, fixture: str = "tests/fixtures/project-context.json") -> list[str]:
    return [
        "route",
        "query",
        "--config-dir",
        str(config_dir),
        "--query",
        "How is routing implemented?",
        "--intent",
        "project_context",
        "--project",
        "example-project",
        "--repo",
        "example-repo",
        "--fixture",
        fixture,
    ]


def test_query_json_is_one_sorted_stable_line(cli_runner: CliRunner, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    result = cli_runner.invoke(app, arguments(config_dir))
    assert result.exit_code == 0, result.output
    assert result.stdout.endswith("\n") and not result.stdout.endswith("\n\n")
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["read_only"] is True
    assert payload["data_mode"] == "fake"
    assert payload["write_proposal"] is None
    assert "query" not in payload
    assert "recorded_at" not in payload
    assert list(payload) == sorted(payload)


def test_empty_p_and_m_still_exit_zero_and_degrade_to_t(cli_runner: CliRunner, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    result = cli_runner.invoke(app, arguments(config_dir, "tests/fixtures/m-empty-p-empty.json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["step_results"][0]["reason_code"] == "P_EMPTY"
    assert payload["step_results"][1]["reason_code"] == "M_EMPTY"
    assert payload["step_results"][2]["status"] == "ok"


def test_text_output_contains_route_and_disabled_writes(cli_runner: CliRunner, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    args = [
        "route", "query", "--config-dir", str(config_dir), "--query", "cold",
        "--intent", "cold_detail", "--fixture", "tests/fixtures/r-empty.json", "--format", "text",
    ]
    result = cli_runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "规则版本: v1" in result.stdout
    assert "R[mtr/rarely] -> T[mtr/trial]" in result.stdout
    assert "R_EMPTY" in result.stdout
    assert "外部写入: disabled" in result.stdout


def test_invalid_coordinate_has_safe_stderr_and_empty_stdout(cli_runner: CliRunner, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    args = arguments(config_dir)
    args[args.index("--project") + 1] = "https://evil.invalid/private"
    result = cli_runner.invoke(app, args)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "COORDINATE_URL_FORBIDDEN" in result.stderr
    assert "https://evil.invalid/private" not in result.stderr


def test_decisions_entry_reads_by_request_hash(cli_runner: CliRunner, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    query_result = cli_runner.invoke(app, arguments(config_dir))
    request_hash = json.loads(query_result.stdout)["request_hash"]
    result = cli_runner.invoke(
        app,
        ["route", "decisions", "--config-dir", str(config_dir), "--request-hash", request_hash],
    )
    assert result.exit_code == 0, result.output
    events = json.loads(result.stdout)
    assert [item["event_type"] for item in events][0] == "PLAN_CREATED"
    assert events[-1]["event_type"] == "QUERY_FINISHED"


def test_help_exposes_no_write_or_real_adapter_commands(cli_runner: CliRunner) -> None:
    for args in (["--help"], ["route", "--help"], ["route", "query", "--help"]):
        result = cli_runner.invoke(app, args)
        assert result.exit_code == 0
        lowered = result.stdout.lower()
        for command in ("approve", "apply", "publish", "commit", "push", "merge"):
            assert command not in lowered
        assert "obsidian" not in lowered
        assert "lexiang" not in lowered
