"""Stage 0 read-only smoke contracts implemented exclusively with Fixtures."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mtrp_hub.application import RouteQueryService
from mtrp_hub.domain.enums import ReasonCode
from mtrp_hub.fixtures.vault import FixtureMarkdownError, parse_fixture_markdown

from tests.conftest import FIXTURES


def test_vault_fixture_parse_is_read_only_and_tags_are_exact() -> None:
    for name, expected_tag in (("valid-t.md", "mtr/trial"), ("valid-r.md", "mtr/rarely")):
        path = FIXTURES / "vault" / name
        before = path.read_bytes()
        before_hash = hashlib.sha256(before).hexdigest()
        parsed = parse_fixture_markdown(before.decode("utf-8"), logical_path=f"vault/{name}")
        assert expected_tag in parsed.tags
        assert parsed.classification == "managed"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash


def test_no_frontmatter_is_unmanaged_not_t() -> None:
    text = (FIXTURES / "vault" / "no-frontmatter.md").read_text(encoding="utf-8")
    parsed = parse_fixture_markdown(text, logical_path="no-frontmatter.md")
    assert parsed.classification == "unmanaged"
    assert parsed.needs_classification is True
    assert "mtr/trial" not in parsed.tags


def test_invalid_vault_yaml_is_explicit_fixture_error() -> None:
    text = (FIXTURES / "vault" / "invalid-yaml.md").read_text(encoding="utf-8")
    with pytest.raises(FixtureMarkdownError):
        parse_fixture_markdown(text, logical_path="invalid-yaml.md")


def test_lexiang_space_empty_result_is_success(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(FIXTURES.parents[1])
    request = request_factory(fixture="tests/fixtures/m-empty-p-empty.json")
    response = RouteQueryService().query(request)
    m_result = response.step_results[1]
    assert m_result.reason_code is ReasonCode.M_EMPTY
    assert m_result.resource_ids == ()
    assert response.result.resources[0].source_ref.store.value == "T"


def test_github_readme_evidence_is_repo_sha_and_path(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(FIXTURES.parents[1])
    request = request_factory(fixture="tests/fixtures/project-context.json")
    response = RouteQueryService().query(request)
    readme = next(item for item in response.result.evidence if item.path == "README.md")
    assert readme.project_coordinate.repo_id == "example-repo"
    assert readme.project_coordinate.ref == "main"
    assert readme.project_coordinate.commit == "a" * 40
    assert readme.snapshot_ref.value == "a" * 40
    assert readme.path == "README.md"
