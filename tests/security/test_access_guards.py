"""Prove coordinate rejection happens before Fixture or Adapter access."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mtrp_hub.adapters.fake import FakeAdapter
from mtrp_hub.application import RouteQueryService
from mtrp_hub.errors import MtrpError

from tests.conftest import ROOT


class LoaderSpy:
    def __init__(self) -> None:
        self.calls = 0

    def load(self, path: str | None):
        self.calls += 1
        raise AssertionError("FixtureLoader must not be reached")


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"project_id": "https://evil.invalid/repository"}, "COORDINATE_URL_FORBIDDEN"),
        ({"repo_id": "/absolute/repository"}, "ABSOLUTE_PATH_FORBIDDEN"),
        ({"project_id": "../escape"}, "PATH_ESCAPE"),
        ({"workspace_id": "E:/not-registered/repo"}, "UNREGISTERED_WORKSPACE"),
        ({"workspace_id": "not-registered"}, "UNREGISTERED_WORKSPACE"),
    ],
)
def test_invalid_coordinates_have_zero_fixture_and_adapter_calls(request_factory, overrides, error_code: str) -> None:
    loader = LoaderSpy()
    adapter_calls = []

    def adapter_factory(kind):
        adapter_calls.append(kind)
        return FakeAdapter(kind)

    service = RouteQueryService(fixture_loader=loader, adapter_factory=adapter_factory)
    with pytest.raises(MtrpError) as caught:
        service.query(request_factory(**overrides, fixture="tests/fixtures/unregistered-workspace.json"))
    assert caught.value.error_code == error_code
    assert loader.calls == 0
    assert adapter_calls == []


def test_absolute_or_escaping_fixture_rejected_before_load(request_factory) -> None:
    loader = LoaderSpy()
    service = RouteQueryService(fixture_loader=loader)
    for fixture in ("/tmp/private.json", "../private.json", "https://evil.invalid/data.json"):
        with pytest.raises(MtrpError):
            service.query(request_factory(fixture=fixture))
    assert loader.calls == 0


def test_registered_workspace_is_still_disabled_before_target_or_fixture(
    request_factory,
    config_dir: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    projects = config_dir / "projects.yaml"
    target = tmp_path / "protected-workspace"
    text = projects.read_text(encoding="utf-8")
    text = text.replace(
        "        workspaces: []",
        "          - location_id: local\n"
        "            kind: local_git\n"
        "            role: workspace\n"
        "            state: unknown\n"
        "            snapshots: []\n"
        "        workspaces:\n"
        "          - workspace_id: registered-local\n"
        "            location_id: local\n"
        f"            root_path: {target.as_posix()}",
    )
    projects.write_text(text, encoding="utf-8")
    loader = LoaderSpy()
    adapter_calls = []
    service = RouteQueryService(
        fixture_loader=loader,
        adapter_factory=lambda kind: adapter_calls.append(kind) or FakeAdapter(kind),
    )
    original_stat = Path.stat
    original_open = Path.open
    original_resolve = Path.resolve

    def guarded_stat(self, *args, **kwargs):
        if self == target:
            raise AssertionError("Workspace stat is forbidden")
        return original_stat(self, *args, **kwargs)

    def guarded_open(self, *args, **kwargs):
        if self == target:
            raise AssertionError("Workspace open is forbidden")
        return original_open(self, *args, **kwargs)

    def guarded_resolve(self, *args, **kwargs):
        if self == target:
            raise AssertionError("Workspace resolve is forbidden")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    with pytest.raises(MtrpError) as caught:
        service.query(request_factory(workspace_id="registered-local", fixture="tests/fixtures/project-context.json"))
    assert caught.value.error_code == "WORKSPACE_QUERY_DISABLED"
    assert loader.calls == 0
    assert adapter_calls == []


def test_write_verb_guard_has_no_implementation_command(config_dir: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "mtrp_hub", "commit", "--config-dir", str(config_dir)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"WRITE_DISABLED" in completed.stderr
