"""Manifest identity, expected recompile, full schedule, and tamper rejection."""

from __future__ import annotations

import copy
import json

import pytest

from mtrp_hub.application_stage2 import ManifestService
from mtrp_hub.canonical import sha256_json
from mtrp_hub.domain.enums import Intent
from mtrp_hub.errors import MtrpError
from mtrp_hub.execution.models import ExecutionManifest, ExecutionMode

from tests.conftest import ROOT


def compiled(request_factory, **overrides):  # type: ignore[no-untyped-def]
    return ManifestService().compile_request(request_factory(**overrides), ExecutionMode.FIXTURE)


def resign(data: dict) -> ExecutionManifest:
    hashed = dict(data)
    hashed.pop("execution_id", None)
    hashed.pop("manifest_hash", None)
    digest = sha256_json(hashed)
    data["manifest_hash"] = digest
    data["execution_id"] = "exe_" + digest
    return ExecutionManifest.model_validate(data)


def test_manifest_hash_excludes_only_two_derived_fields(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    result = compiled(request_factory)
    manifest = result.manifest
    assert manifest.manifest_hash == sha256_json(manifest.hashed_payload())
    assert manifest.execution_id == "exe_" + manifest.manifest_hash
    assert manifest.request_id == "req_" + manifest.request_hash
    assert manifest.route_id == "route_" + manifest.plan_hash
    assert len(manifest.trace_id()) == 70
    result.verifier.verify_intrinsic(manifest)


def test_all_route_schedules_keep_v1_order_and_core_barriers(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    project = compiled(request_factory).manifest
    assert [step.route_step_id for step in project.steps] == ["s01-p"] * 4 + ["s02-m"] * 5 + ["s03-t"]
    decision = compiled(request_factory, intent=Intent.DECISION_HISTORY).manifest
    assert [step.route_step_id for step in decision.steps] == ["s01-p"] * 3 + ["s02-t", "s03-m"][:1] + ["s03-m"] * 5
    cold = compiled(
        request_factory,
        intent=Intent.COLD_DETAIL,
        project_id=None,
        repo_id=None,
    ).manifest
    assert [step.route_step_id for step in cold.steps] == ["s01-r", "s02-t"]
    assert all(step.executor.value == "core" for step in cold.steps)


@pytest.mark.parametrize(
    "field,value",
    [
        ("read_only", False),
        ("request_id", "req_" + "1" * 64),
        ("route_id", "route_" + "1" * 64),
        ("execution_config_hash", "1" * 64),
        ("profile_hash", "1" * 64),
    ],
)
def test_every_manifest_field_is_bound(request_factory, monkeypatch, field: str, value) -> None:
    monkeypatch.chdir(ROOT)
    result = compiled(request_factory)
    actual = result.manifest.model_copy(update={field: value})
    with pytest.raises(MtrpError):
        result.verifier.verify_expected(actual, result.manifest)


def test_step_reorder_and_argument_tamper_fail_even_after_resigning(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    result = compiled(request_factory)
    data = result.manifest.model_dump(mode="json")
    data["steps"] = list(reversed(data["steps"]))
    reordered = resign(data)
    with pytest.raises(MtrpError):
        result.verifier.verify_expected(reordered, result.manifest)
    data = result.manifest.model_dump(mode="json")
    data["steps"][1]["arguments"]["ref"] = "other-branch"
    changed = resign(data)
    with pytest.raises(MtrpError) as caught:
        result.verifier.verify_expected(changed, result.manifest)
    assert caught.value.error_code in ("MANIFEST_CONTEXT_CHANGED", "MANIFEST_IDENTITY_MISMATCH")


def test_self_consistent_overall_replacement_cannot_bypass_expected_recompile(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    expected = compiled(request_factory, query="original synthetic query")
    replacement = compiled(request_factory, query="replacement synthetic query").manifest
    expected.verifier.verify_intrinsic(replacement)
    with pytest.raises(MtrpError) as caught:
        expected.verifier.verify_expected(replacement, expected.manifest)
    assert caught.value.error_code.startswith("MANIFEST_")


def test_tracked_manifest_goldens_match_recompilation(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    project = compiled(request_factory, query="synthetic query").manifest
    project_golden = json.loads((ROOT / "tests/fixtures/stage2/manifest-project-context.json").read_text(encoding="utf-8"))
    assert project.model_dump(mode="json") == project_golden
    decision = compiled(request_factory, query="synthetic query", intent=Intent.DECISION_HISTORY).manifest
    decision_golden = json.loads((ROOT / "tests/fixtures/stage2/manifest-decision-history.json").read_text(encoding="utf-8"))
    assert decision.model_dump(mode="json") == decision_golden


def test_v2_explicit_downgrade_does_not_change_legacy_cli(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    from mtrp_hub.application_stage2 import QueryFinalizationService
    from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader

    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-project-context.json")
    response = QueryFinalizationService().run_fixture(request_factory(), fixture)
    legacy = response.result.to_legacy()
    assert len(legacy.resources) == len(response.result.resources)
    assert all("score" not in item.model_dump() for item in legacy.resources)
