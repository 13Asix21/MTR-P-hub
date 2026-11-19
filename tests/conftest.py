"""Shared explicit configuration, Fixture, and request helpers."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mtrp_hub.domain.enums import Intent, OutputFormat
from mtrp_hub.domain.requests import RouteQueryRequest
from mtrp_hub.fixtures.loader import FixtureLoader
from mtrp_hub.fixtures.schemas import FixtureBundle

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Copy only the three examples into an isolated explicit config directory."""
    target = tmp_path / "config"
    target.mkdir()
    shutil.copyfile(ROOT / "config" / "stores.example.yaml", target / "stores.yaml")
    shutil.copyfile(ROOT / "config" / "projects.example.yaml", target / "projects.yaml")
    shutil.copyfile(ROOT / "config" / "routing.yaml", target / "routing.yaml")
    return target


@pytest.fixture
def request_factory(config_dir: Path):
    """Build requests with safe defaults and explicit per-test overrides."""

    def build(**overrides: Any) -> RouteQueryRequest:
        values: dict[str, Any] = {
            "query": "How is deterministic routing implemented?",
            "intent": Intent.PROJECT_CONTEXT,
            "project_id": "example-project",
            "repo_id": "example-repo",
            "workspace_id": None,
            "config_dir": str(config_dir),
            "fixture": None,
            "include_r": False,
            "format": OutputFormat.JSON,
        }
        values.update(overrides)
        return RouteQueryRequest(**values)

    return build


@pytest.fixture
def bundle_loader():
    """Parse one explicitly named test Fixture without scanning its directory."""

    def load(name: str) -> FixtureBundle:
        path = FIXTURES / name
        return FixtureLoader.loads(path.read_text(encoding="utf-8"))

    return load


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()
