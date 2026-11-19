"""Synthetic tool fixtures, exact GitHub plans, and strict controlled JSON loading."""

from __future__ import annotations

import base64

import pytest

from mtrp_hub.errors import MtrpError
from mtrp_hub.execution.envelope import RawResponse
from mtrp_hub.execution.models import ExecutionMode, ManifestStep, Stage2Operation
from mtrp_hub.execution.profile import STAGE2_PROFILE
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader
from mtrp_hub.fixtures.stage2_schemas import Stage2Fixture


def github_step(operation: Stage2Operation, arguments: dict) -> ManifestStep:
    return ManifestStep(
        step_id="s01-p-01-test",
        route_step_id="s01-p",
        executor="workbuddy",
        adapter="github-p",
        operation=operation,
        arguments=arguments,
        on_empty="continue",
        on_unavailable="continue",
    )


def test_root_and_file_calls_both_bind_same_full_commit() -> None:
    binding = {"from_step": "s01-p-02-resolve-ref", "field": "commit"}
    root = github_step(Stage2Operation.GITHUB_READ_REPOSITORY_ROOT, {"owner": "owner", "repo": "repo", "sha": binding})
    file = github_step(Stage2Operation.GITHUB_READ_FILE, {"owner": "owner", "repo": "repo", "path": "README.md", "sha": binding})
    root_call = STAGE2_PROFILE.materialize_calls(root, {"owner": "owner", "repo": "repo", "sha": "a" * 40})[0]
    file_call = STAGE2_PROFILE.materialize_calls(file, {"owner": "owner", "repo": "repo", "path": "README.md", "sha": "a" * 40})[0]
    assert root_call.arguments["sha"] == file_call.arguments["sha"] == "a" * 40
    assert "path" not in root_call.arguments and "ref" not in root_call.arguments
    assert root_call.arguments["fields"] == ["type", "name", "path"]
    assert file_call.arguments["path"] == "README.md" and "fields" not in file_call and "ref" not in file_call


def test_explicit_synthetic_base64_has_raw_bytes_scope() -> None:
    content = b"synthetic bytes\x00\n"
    response = RawResponse(
        kind="json",
        data={"path": "README.md", "encoding": "base64", "content_base64": base64.b64encode(content).decode(), "size": len(content)},
    )
    parsed = STAGE2_PROFILE.parse_file(response, ExecutionMode.FIXTURE)
    assert parsed.content == content and parsed.content_hash_scope == "raw_bytes"


def test_space_parser_only_maps_three_logical_ids() -> None:
    response = RawResponse(
        kind="json",
        data={
            "code": 0,
            "message": "success",
            "data": {"space": {"id": "0" * 32, "team_id": "0" * 32, "root_entry_id": "0" * 32, "name": "controlled synthetic"}, "config": {}},
            "request_id": "external-looking-but-synthetic",
        },
    )
    result = STAGE2_PROFILE.parse_space(response, {"space_id": "0" * 32, "team_id": "0" * 32, "root_entry_id": "0" * 32})
    assert set(result) == {"space_id", "team_id", "root_entry_id"}
    assert "name" not in result and "request_id" not in result


def test_controlled_json_rejects_duplicates_nan_and_extra_fixture_fields() -> None:
    loader = Stage2FixtureLoader()
    with pytest.raises(MtrpError):
        loader.loads('{"fixture_id":"x","fixture_id":"y"}', model=Stage2Fixture, field="fixture")
    with pytest.raises(MtrpError):
        loader.loads('{"fixture_id":"x","responses":{},"value":NaN}', model=Stage2Fixture, field="fixture")
    with pytest.raises(MtrpError):
        loader.loads('{"fixture_id":"x","responses":{},"tool_id":"write"}', model=Stage2Fixture, field="fixture")
