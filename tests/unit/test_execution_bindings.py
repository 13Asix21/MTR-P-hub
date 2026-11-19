"""Binding direction, full SHA, entry ID/type/count, and no-latest-fallback tests."""

from __future__ import annotations

import pytest

from mtrp_hub.application_stage2 import ManifestService
from mtrp_hub.canonical import sha256_json
from mtrp_hub.errors import MtrpError
from mtrp_hub.execution.binding import BindingResolver
from mtrp_hub.execution.envelope import RawResponse, RawToolOutcome
from mtrp_hub.execution.executor import WorkBuddyManifestExecutor
from mtrp_hub.execution.models import ExecutionManifest, ExecutionMode, Stage2Operation
from mtrp_hub.fixtures.stage2_schemas import Stage2Fixture
from mtrp_hub.fixtures.stage2_transport import Stage2FixtureTransport

from tests.conftest import ROOT


def resign(data: dict) -> ExecutionManifest:
    hashed = dict(data)
    hashed.pop("execution_id", None)
    hashed.pop("manifest_hash", None)
    digest = sha256_json(hashed)
    data["manifest_hash"] = digest
    data["execution_id"] = "exe_" + digest
    return ExecutionManifest.model_validate(data)


@pytest.mark.parametrize("value", [None, "", "a" * 7, "a" * 41, 42, ["a" * 40], "A" * 40])
def test_invalid_commit_binding_never_falls_back_to_latest(value) -> None:
    with pytest.raises(MtrpError) as caught:
        BindingResolver.scalar_commit(value)
    assert caught.value.error_code in ("BINDING_EMPTY", "BINDING_TYPE_INVALID")


def test_forward_binding_is_rejected_before_any_transport(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    compiled = ManifestService().compile_request(request_factory(), ExecutionMode.FIXTURE)
    data = compiled.manifest.model_dump(mode="json")
    data["steps"][2]["arguments"]["sha"]["from_step"] = data["steps"][3]["step_id"]
    manifest = resign(data)
    transport = Stage2FixtureTransport(compiled.config, Stage2Fixture(fixture_id="spy"))
    executor = WorkBuddyManifestExecutor(transport, binding=compiled.binding, verifier=compiled.verifier)
    with pytest.raises(MtrpError) as caught:
        executor.execute(manifest)
    assert caught.value.error_code == "BINDING_NOT_EARLIER"
    assert transport.calls == ()


def test_self_binding_is_rejected_before_any_transport(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    compiled = ManifestService().compile_request(request_factory(), ExecutionMode.FIXTURE)
    data = compiled.manifest.model_dump(mode="json")
    data["steps"][2]["arguments"]["sha"]["from_step"] = data["steps"][2]["step_id"]
    manifest = resign(data)
    transport = Stage2FixtureTransport(compiled.config, Stage2Fixture(fixture_id="spy"))
    with pytest.raises(MtrpError):
        WorkBuddyManifestExecutor(transport, binding=compiled.binding, verifier=compiled.verifier).execute(manifest)
    assert transport.calls == ()


def test_short_remote_sha_stops_root_and_file_calls(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    compiled = ManifestService().compile_request(request_factory(), ExecutionMode.FIXTURE)
    fixture = Stage2Fixture(
        fixture_id="short-sha",
        responses={
            Stage2Operation.GITHUB_LIST_BRANCHES: (
                RawToolOutcome(response=RawResponse(kind="json", data=[{"name": "main", "sha": "a" * 40, "protected": False}])),
            ),
            Stage2Operation.GITHUB_RESOLVE_REF: (
                RawToolOutcome(response=RawResponse(kind="json", data={"sha": "aaaaaaa"})),
            ),
        },
    )
    transport = Stage2FixtureTransport(compiled.config, fixture)
    envelope = WorkBuddyManifestExecutor(transport, binding=compiled.binding, verifier=compiled.verifier).execute(compiled.manifest)
    file_receipt = next(receipt for receipt in envelope.receipts if receipt.step_id.endswith("read-file-01"))
    assert file_receipt.status.value == "DEGRADED" and file_receipt.calls == ()
    assert sum(call.tool_id == "mcp__github__get_file_contents" for call in transport.calls) == 0


def test_entry_candidate_type_id_and_limit_are_closed() -> None:
    resolver = BindingResolver(max_entry_reads=1)
    with pytest.raises(MtrpError):
        resolver.entry_candidates("not-a-list")
    with pytest.raises(MtrpError):
        resolver.entry_candidates([{"entry_id": "short", "title": "Synthetic"}])
    with pytest.raises(MtrpError) as caught:
        resolver.entry_candidates(
            [
                {"entry_id": "1" * 32, "title": "One"},
                {"entry_id": "2" * 32, "title": "Two"},
            ]
        )
    assert caught.value.error_code == "ENTRY_READ_LIMIT_EXCEEDED"
    assert resolver.entry_candidates([]) == []
