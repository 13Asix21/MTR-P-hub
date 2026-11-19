"""Offline stage 2 execution under network, subprocess, Git, and discovery denial."""

from __future__ import annotations

import ast
import os
import socket
import subprocess
from pathlib import Path

from mtrp_hub.application_stage2 import QueryFinalizationService
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader

from tests.conftest import ROOT


def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
    raise AssertionError("forbidden external capability invoked")


def test_full_v2_fixture_survives_external_capability_denial(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(os, "walk", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    monkeypatch.setattr(Path, "rglob", forbidden)
    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-project-context.json")
    response = QueryFinalizationService().run_fixture(request_factory(), fixture)
    assert response.status.value == "DEGRADED"  # isolated invalid-yaml fixture
    assert response.step_results[0].status.value == "OK"
    assert response.step_results[1].status.value == "EMPTY"


def test_new_adapter_executor_and_fixture_modules_have_no_external_transport_imports() -> None:
    roots = (
        ROOT / "src/mtrp_hub/adapters/obsidian.py",
        ROOT / "src/mtrp_hub/adapters/lexiang.py",
        ROOT / "src/mtrp_hub/adapters/github.py",
        ROOT / "src/mtrp_hub/execution/executor.py",
        ROOT / "src/mtrp_hub/fixtures/stage2_transport.py",
    )
    forbidden_imports = {"requests", "httpx", "urllib", "socket", "subprocess", "git", "gitpython", "pygit2"}
    for path in roots:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update((node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
        assert imports.isdisjoint(forbidden_imports), path.name


def test_fixture_vault_bytes_are_unchanged_by_query(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    import hashlib

    vault = ROOT / "tests/fixtures/stage2/vault"
    before = {path.relative_to(vault): hashlib.sha256(path.read_bytes()).hexdigest() for path in vault.glob("**/*.md")}
    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-empty.json")
    QueryFinalizationService().run_fixture(request_factory(), fixture)
    after = {path.relative_to(vault): hashlib.sha256(path.read_bytes()).hexdigest() for path in vault.glob("**/*.md")}
    assert after == before
