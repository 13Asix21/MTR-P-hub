"""P→T→M Core barrier and continuation state-machine integration."""

from __future__ import annotations

from mtrp_hub.application_stage2 import ManifestService, QueryFinalizationService
from mtrp_hub.domain.enums import Intent
from mtrp_hub.domain.requests_v2 import QueryResponseV2
from mtrp_hub.execution.envelope import ContinuationOutput
from mtrp_hub.execution.executor import WorkBuddyManifestExecutor
from mtrp_hub.execution.models import ExecutionMode
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader
from mtrp_hub.fixtures.stage2_transport import Stage2FixtureTransport

from tests.conftest import ROOT


def test_decision_history_stops_at_core_then_resumes_m(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    request = request_factory(intent=Intent.DECISION_HISTORY)
    manifests = ManifestService()
    compiled = manifests.compile_request(request, ExecutionMode.FIXTURE)
    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-decision-history.json")
    transport = Stage2FixtureTransport(compiled.config, fixture)
    executor = WorkBuddyManifestExecutor(transport, binding=compiled.binding, verifier=compiled.verifier)
    first = executor.execute(compiled.manifest)
    assert first.next_step_id == "s02-t-01-vault-view-search"
    assert [receipt.route_step_id for receipt in first.receipts] == ["s01-p"] * 3
    assert all("lexiang" not in call.tool_id for call in transport.calls)
    finalizer = QueryFinalizationService()
    continued = finalizer.finalize(request, compiled.manifest.manifest_hash, first, ExecutionMode.FIXTURE)
    assert isinstance(continued, ContinuationOutput)
    assert continued.envelope.next_step_id == "s03-m-01-resolve-space"
    assert [receipt.route_step_id for receipt in continued.envelope.core_receipts] == ["s02-t"]
    assert all("lexiang" not in call.tool_id for call in transport.calls)
    completed_envelope = executor.execute(compiled.manifest, continued.envelope)
    assert completed_envelope.next_step_id is None
    assert [receipt.route_step_id for receipt in completed_envelope.receipts] == ["s01-p"] * 3 + ["s03-m"] * 5
    output = finalizer.finalize(request, compiled.manifest.manifest_hash, completed_envelope, ExecutionMode.FIXTURE)
    assert isinstance(output, QueryResponseV2)
    assert [step.step_id for step in output.step_results] == ["s01-p", "s02-t", "s03-m"]


def test_cold_detail_needs_zero_workbuddy_calls(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    request = request_factory(intent=Intent.COLD_DETAIL, project_id=None, repo_id=None)
    compiled = ManifestService().compile_request(request, ExecutionMode.FIXTURE)
    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-empty.json")
    transport = Stage2FixtureTransport(compiled.config, fixture)
    executor = WorkBuddyManifestExecutor(transport, binding=compiled.binding, verifier=compiled.verifier)
    initial = executor.execute(compiled.manifest)
    assert initial.receipts == () and transport.calls == ()
    output = QueryFinalizationService().finalize(request, compiled.manifest.manifest_hash, initial, ExecutionMode.FIXTURE)
    assert isinstance(output, QueryResponseV2)
    assert [step.step_id for step in output.step_results] == ["s01-r", "s02-t"]
    assert transport.calls == ()
