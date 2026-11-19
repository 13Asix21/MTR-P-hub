"""Temporary Fixture Vault views, exclusion, raw hashes, and failure isolation."""

from __future__ import annotations

import builtins
import hashlib
import shutil
from dataclasses import replace
from pathlib import Path

from mtrp_hub.adapters.obsidian import ObsidianAdapter
from mtrp_hub.adapters.stage2_base import Stage2ExecutionContext
from mtrp_hub.application_stage2 import ManifestService
from mtrp_hub.domain.enums import Intent
from mtrp_hub.domain.requests_v2 import StepStatusV2
from mtrp_hub.execution.models import ExecutionMode

from tests.conftest import ROOT


def core_execution(request_factory, monkeypatch, intent: Intent = Intent.PROJECT_CONTEXT, index: int = -1):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(ROOT)
    needs_p = intent in (Intent.PROJECT_CONTEXT, Intent.DECISION_HISTORY)
    request = request_factory(
        intent=intent,
        project_id="example-project" if needs_p else None,
        repo_id="example-repo" if needs_p else None,
    )
    compiled = ManifestService().compile_request(request, ExecutionMode.FIXTURE)
    route_step = compiled.route.steps[index]
    step = next(item for item in compiled.manifest.steps if item.route_step_id == route_step.step_id)
    context = Stage2ExecutionContext(compiled.config, compiled.manifest, route_step)
    return ObsidianAdapter().execute(step, context)


def test_t_view_reads_only_trial_and_hashes_raw_bytes(request_factory, monkeypatch) -> None:
    execution = core_execution(request_factory, monkeypatch)
    assert execution.step_result.status is StepStatusV2.DEGRADED  # invalid-yaml.md is isolated
    assert execution.step_result.resource_count == 1
    resource = execution.resources[0]
    assert resource.source_ref.logical_key.startswith("fixture-vault:2-Permanent/中文目录/")
    expected = hashlib.sha256(
        (ROOT / "tests/fixtures/stage2/vault/2-Permanent/中文目录/trial-inline.md").read_bytes()
    ).hexdigest()
    assert resource.snapshot_ref.value == expected
    assert execution.evidence[0].content_hash_scope == "raw_bytes"
    assert execution.evidence[0].content_sha256 == expected


def test_excluded_conflict_unmanaged_and_code_only_notes_are_never_resources(request_factory, monkeypatch) -> None:
    execution = core_execution(request_factory, monkeypatch)
    keys = {item.source_ref.logical_key for item in execution.resources}
    assert len(keys) == 1
    assert all("MTR-P-System" not in key for key in keys)
    assert all("tag-conflict" not in key and "unmanaged" not in key and "code-fences" not in key for key in keys)
    assert execution.step_result.skipped_count >= 5


def test_r_view_uses_only_rarely_and_does_not_promote_unmanaged(request_factory, monkeypatch) -> None:
    execution = core_execution(request_factory, monkeypatch, Intent.COLD_DETAIL, 0)
    assert execution.step_result.resource_count == 1
    assert execution.resources[0].source_ref.store.value == "R"
    assert execution.resources[0].source_ref.logical_key.endswith("rarely.md")


def limited_execution(request_factory, monkeypatch, **limits):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(ROOT)
    compiled = ManifestService().compile_request(request_factory(), ExecutionMode.FIXTURE)
    obsidian = compiled.config.obsidian.model_copy(update=limits)
    stage2 = compiled.config.config.model_copy(update={"obsidian": obsidian})
    config = replace(compiled.config, config=stage2)
    route_step = compiled.route.steps[-1]
    original_step = next(item for item in compiled.manifest.steps if item.route_step_id == route_step.step_id)
    step = original_step.model_copy(update={"arguments": {**original_step.arguments, "scope_sha256": obsidian.scope_sha256}})
    context = Stage2ExecutionContext(config, compiled.manifest, route_step)
    return ObsidianAdapter().execute(step, context)


def test_scan_and_file_size_limits_fail_closed_without_partial_resources(request_factory, monkeypatch) -> None:
    scan_limited = limited_execution(request_factory, monkeypatch, max_scan_files=1)
    assert scan_limited.step_result.status is StepStatusV2.DEGRADED
    assert scan_limited.step_result.reason_code == "VAULT_SCAN_LIMIT_EXCEEDED"
    assert scan_limited.resources == ()
    size_limited = limited_execution(request_factory, monkeypatch, max_file_bytes=1)
    assert size_limited.step_result.status is StepStatusV2.DEGRADED
    assert size_limited.resources == ()


def test_result_limit_does_not_silently_truncate(request_factory, tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "tests/fixtures/stage2/vault"
    target.parent.mkdir(parents=True)
    shutil.copytree(ROOT / "tests/fixtures/stage2/vault", target)
    (target / "2-Permanent/second-trial.md").write_text("#mtr/trial\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    compiled = ManifestService().compile_request(request_factory(), ExecutionMode.FIXTURE)
    obsidian = compiled.config.obsidian.model_copy(update={"max_results": 1})
    stage2 = compiled.config.config.model_copy(update={"obsidian": obsidian})
    config = replace(compiled.config, config=stage2)
    route_step = compiled.route.steps[-1]
    original_step = next(item for item in compiled.manifest.steps if item.route_step_id == route_step.step_id)
    step = original_step.model_copy(update={"arguments": {**original_step.arguments, "scope_sha256": obsidian.scope_sha256}})
    execution = ObsidianAdapter().execute(step, Stage2ExecutionContext(config, compiled.manifest, route_step))
    assert execution.step_result.status is StepStatusV2.DEGRADED
    assert execution.step_result.reason_code == "VAULT_SCAN_LIMIT_EXCEEDED"
    assert execution.resources == ()


def test_excluded_markdown_is_never_opened(request_factory, monkeypatch) -> None:
    opened: list[str] = []
    original = builtins.open

    def spy(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(file).endswith(".md"):
            opened.append(str(file))
            assert "MTR-P-System" not in str(file)
        return original(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    core_execution(request_factory, monkeypatch)
    assert opened and all("MTR-P-System" not in path for path in opened)
