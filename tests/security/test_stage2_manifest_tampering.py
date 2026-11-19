"""Write-tool, URL, command, absolute-path, and profile tampering with zero calls."""

from __future__ import annotations

import pytest

from mtrp_hub.application_stage2 import ManifestService
from mtrp_hub.canonical import sha256_json
from mtrp_hub.errors import MtrpError
from mtrp_hub.execution.envelope import seal_envelope
from mtrp_hub.execution.executor import WorkBuddyManifestExecutor
from mtrp_hub.execution.models import ExecutionManifest, ExecutionMode
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader
from mtrp_hub.fixtures.stage2_schemas import Stage2Fixture
from mtrp_hub.fixtures.stage2_transport import Stage2FixtureTransport

from tests.conftest import ROOT


def signed(data: dict) -> dict:
    data = dict(data)
    data.pop("execution_id", None)
    data.pop("manifest_hash", None)
    digest = sha256_json(data)
    return {**data, "execution_id": "exe_" + digest, "manifest_hash": digest}


@pytest.mark.parametrize("path", ["/etc/passwd", "C:/private/file.md", "../outside.md", "https://evil.invalid/file"])
def test_forbidden_path_or_url_argument_never_reaches_transport(request_factory, monkeypatch, path: str) -> None:
    monkeypatch.chdir(ROOT)
    compiled = ManifestService().compile_request(request_factory(), ExecutionMode.FIXTURE)
    data = compiled.manifest.model_dump(mode="json")
    data["steps"][3]["arguments"]["path"] = path
    manifest = ExecutionManifest.model_validate(signed(data))
    transport = Stage2FixtureTransport(compiled.config, Stage2Fixture(fixture_id="attack-spy"))
    with pytest.raises(MtrpError) as caught:
        WorkBuddyManifestExecutor(transport, binding=compiled.binding, verifier=compiled.verifier).execute(manifest)
    assert caught.value.error_code in ("ARGUMENT_SCHEMA_INVALID", "PATH_ESCAPE", "ABSOLUTE_PATH_FORBIDDEN")
    assert transport.calls == ()


def test_extra_command_argument_is_rejected_before_calls(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    compiled = ManifestService().compile_request(request_factory(), ExecutionMode.FIXTURE)
    data = compiled.manifest.model_dump(mode="json")
    data["steps"][0]["arguments"]["command"] = "git push"
    manifest = ExecutionManifest.model_validate(signed(data))
    transport = Stage2FixtureTransport(compiled.config, Stage2Fixture(fixture_id="attack-spy"))
    with pytest.raises(MtrpError) as caught:
        WorkBuddyManifestExecutor(transport, binding=compiled.binding, verifier=compiled.verifier).execute(manifest)
    assert caught.value.error_code == "ARGUMENT_SCHEMA_INVALID"
    assert transport.calls == ()


def test_write_tool_in_receipt_cannot_be_executed_or_finalized(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    request = request_factory()
    compiled = ManifestService().compile_request(request, ExecutionMode.FIXTURE)
    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-project-context.json")
    transport = Stage2FixtureTransport(compiled.config, fixture)
    executor = WorkBuddyManifestExecutor(transport, binding=compiled.binding, verifier=compiled.verifier)
    envelope = executor.execute(compiled.manifest)
    data = envelope.model_dump(mode="json")
    data["receipts"][0]["calls"][0]["tool_id"] = "mcp__github__push_files"
    tampered = seal_envelope(data)
    before = len(transport.calls)
    with pytest.raises(MtrpError) as caught:
        executor.execute(compiled.manifest, tampered)
    assert caught.value.error_code == "RECEIPT_TOOL_MISMATCH"
    assert len(transport.calls) == before


def test_query_url_is_text_keyword_not_a_fetch_capability(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    from mtrp_hub.domain.enums import Intent

    compiled = ManifestService().compile_request(
        request_factory(intent=Intent.GENERAL_KNOWLEDGE, project_id=None, repo_id=None, query="https://example.invalid/text"),
        ExecutionMode.FIXTURE,
    )
    search = next(step for step in compiled.manifest.steps if step.operation.value == "lexiang_search_text")
    assert search.arguments["keyword"] == "https://example.invalid/text"
    call = compiled.binding.resolve(search, ()).calls[0]
    assert call.tool_id == "mcp__lexiang__call_tool"
    assert set(call.arguments) == {"tool_name", "arguments"}
