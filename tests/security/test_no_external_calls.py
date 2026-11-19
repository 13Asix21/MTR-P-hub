"""Network/subprocess/discovery denial and secret/log leakage regressions."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest

from mtrp_hub.application import RouteQueryService
from mtrp_hub.canonical import canonical_json
from mtrp_hub.cli import response_payload

from tests.conftest import ROOT


def forbidden(*args, **kwargs):
    raise AssertionError("External/network/discovery capability was invoked")


def test_full_fake_query_survives_external_capability_denial(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "walk", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    monkeypatch.setattr(Path, "rglob", forbidden)
    response = RouteQueryService().query(request_factory(fixture="tests/fixtures/project-context.json"))
    assert response.plan.data_mode == "fake"
    assert response.plan.read_only is True
    assert response.step_results[0].status.value == "ok"


def test_query_url_secret_and_note_body_never_enter_output_or_database(request_factory, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    secret_query = "token=supersecret-cookie Authorization=BearerSecret https://evil.invalid/private"
    response = RouteQueryService().query(
        request_factory(query=secret_query, fixture="tests/fixtures/project-context.json")
    )
    stdout = canonical_json(response_payload(response))
    assert secret_query not in stdout
    assert "supersecret-cookie" not in stdout
    assert "BearerSecret" not in stdout
    assert "evil.invalid" not in stdout
    assert "Fixture body is never emitted." not in stdout
    database = config_dir / ".mtrp" / "decision-log.sqlite3"
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT payload_json, reason_code FROM decision_log").fetchall()
    raw_log = "\n".join(str(row) for row in rows)
    assert "supersecret-cookie" not in raw_log
    assert "BearerSecret" not in raw_log
    assert "evil.invalid" not in raw_log
    assert "Fixture body" not in raw_log


def test_stage2_modules_have_no_native_external_sdk_implementation() -> None:
    import ast

    source_root = ROOT / "src" / "mtrp_hub"
    adapters = source_root / "adapters"
    for required_name in ("obsidian.py", "lexiang.py", "github.py"):
        assert (adapters / required_name).is_file()
    assert (source_root / "execution").is_dir()
    assert not (adapters / "local_git.py").exists()

    forbidden_imports = {
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "socket",
        "subprocess",
        "git",
        "gitpython",
        "pygit2",
        "mcp",
    }
    for source in source_root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        imported_modules = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        violations = forbidden_imports.intersection(imported_modules)
        assert not violations, f"{source}: {sorted(violations)}"


def test_fixture_with_injected_network_or_write_fields_fails_closed(request_factory, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    source = ROOT / "tests" / "fixtures" / "project-context.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    data["allow_network"] = True
    data["write"] = {"operation": "push"}
    injected = ROOT / "tests" / "fixtures" / "injected-temporary.json"
    injected.write_text(json.dumps(data), encoding="utf-8")
    try:
        with pytest.raises(Exception) as caught:
            RouteQueryService().query(request_factory(fixture="tests/fixtures/injected-temporary.json"))
        assert getattr(caught.value, "error_code", None) in ("FIXTURE_INVALID", "FIXTURE_PARSE_ERROR")
    finally:
        injected.unlink()
