"""Stage 2 CLI Manifest, Fixture, finalize, and trace contracts."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mtrp_hub.canonical import canonical_json
from mtrp_hub.cli import app

from tests.conftest import ROOT


def create_arguments(config_dir: Path, intent: str = "project_context") -> list[str]:
    arguments = [
        "--mode", "fixture", "--config-dir", str(config_dir), "--query", "synthetic cli query",
        "--intent", intent,
    ]
    if intent in ("project_context", "decision_history"):
        arguments.extend(["--project", "example-project", "--repo", "example-repo"])
    return arguments


def test_manifest_create_verify_and_fixture_query(cli_runner: CliRunner, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    created = cli_runner.invoke(app, ["manifest", "create", *create_arguments(config_dir)])
    assert created.exit_code == 0, created.output
    manifest = json.loads(created.stdout)
    assert manifest["execution_id"] == "exe_" + manifest["manifest_hash"]
    assert "recorded_at" not in created.stdout and "run_seq" not in created.stdout
    verified = cli_runner.invoke(
        app,
        ["manifest", "verify", *create_arguments(config_dir)],
        input=canonical_json(manifest),
    )
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.stdout)["external_connectivity"] == "not-tested"
    query_args = [item for item in create_arguments(config_dir) if item not in ("--mode", "fixture")]
    result = cli_runner.invoke(
        app,
        ["query", "fixture", *query_args, "--fixture", "tests/fixtures/stage2/stage2-project-context.json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "2" and payload["data_mode"] == "fixture"
    assert [step["step_id"] for step in payload["step_results"]] == ["s01-p", "s02-m", "s03-t"]
    assert "result" not in payload and "duration_ms" not in result.stdout and "run_seq" not in result.stdout


def test_trace_query_returns_run_seq_only_in_audit_view(cli_runner: CliRunner, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    args = [item for item in create_arguments(config_dir, "cold_detail") if item not in ("--mode", "fixture")]
    result = cli_runner.invoke(
        app,
        ["query", "fixture", *args, "--fixture", "tests/fixtures/stage2/stage2-empty.json"],
    )
    assert result.exit_code == 0, result.output
    response = json.loads(result.stdout)
    traces = cli_runner.invoke(
        app,
        ["route", "traces", "--config-dir", str(config_dir), "--trace-id", response["trace_id"]],
    )
    assert traces.exit_code == 0, traces.output
    records = json.loads(traces.stdout)
    assert len(records) == 1 and records[0]["run_seq"] >= 1
    assert records[0]["trace_id"] == response["trace_id"]


def test_manifest_diff_outputs_only_field_paths_and_hashes(cli_runner: CliRunner, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    result = cli_runner.invoke(
        app,
        [
            "manifest", "diff",
            "--left", "tests/fixtures/stage2/manifest-project-context.json",
            "--right", "tests/fixtures/stage2/manifest-decision-history.json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["executed"] is False and payload["changes"]
    assert "synthetic query" not in result.stdout


def test_manifest_verify_rejects_overall_replacement(cli_runner: CliRunner, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    first = cli_runner.invoke(app, ["manifest", "create", *create_arguments(config_dir)])
    other_args = create_arguments(config_dir)
    other_args[other_args.index("synthetic cli query")] = "a different query"
    second = cli_runner.invoke(app, ["manifest", "create", *other_args])
    assert first.exit_code == second.exit_code == 0
    verified = cli_runner.invoke(
        app,
        ["manifest", "verify", *create_arguments(config_dir)],
        input=second.stdout,
    )
    assert verified.exit_code == 2
    assert verified.stdout == ""
    assert "MANIFEST_" in verified.stderr
