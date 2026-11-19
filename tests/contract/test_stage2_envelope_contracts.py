"""Missing, duplicate, extra, reordered, and tampered receipt rejection before Core I/O."""

from __future__ import annotations

import pytest

from mtrp_hub.application_stage2 import ManifestService, QueryFinalizationService
from mtrp_hub.errors import MtrpError
from mtrp_hub.execution.envelope import seal_envelope
from mtrp_hub.execution.executor import WorkBuddyManifestExecutor
from mtrp_hub.execution.models import ExecutionMode
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader
from mtrp_hub.fixtures.stage2_transport import Stage2FixtureTransport

from tests.conftest import ROOT


def setup_execution(request_factory, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(ROOT)
    request = request_factory()
    compiled = ManifestService().compile_request(request, ExecutionMode.FIXTURE)
    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-project-context.json")
    transport = Stage2FixtureTransport(compiled.config, fixture)
    executor = WorkBuddyManifestExecutor(transport, binding=compiled.binding, verifier=compiled.verifier)
    envelope = executor.execute(compiled.manifest)
    return request, compiled, transport, executor, envelope


def finalize_with_forbidden_vault(request, envelope):  # type: ignore[no-untyped-def]
    def forbidden():  # type: ignore[no-untyped-def]
        raise AssertionError("Vault must not be touched after an invalid envelope")

    return QueryFinalizationService(obsidian_factory=forbidden).finalize(
        request,
        envelope.manifest_hash,
        envelope,
        ExecutionMode.FIXTURE,
    )


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra", "reordered"])
def test_receipt_prefix_attacks_are_rejected_before_vault(request_factory, monkeypatch, mutation: str) -> None:
    request, _, _, _, envelope = setup_execution(request_factory, monkeypatch)
    data = envelope.model_dump(mode="json")
    if mutation == "missing":
        data["receipts"].pop(0)
    elif mutation == "duplicate":
        data["receipts"].insert(0, data["receipts"][0])
    elif mutation == "extra":
        extra = dict(data["receipts"][0])
        extra["step_id"] = "future-extra-step"
        data["receipts"].append(extra)
    else:
        data["receipts"][0], data["receipts"][1] = data["receipts"][1], data["receipts"][0]
    tampered = seal_envelope(data)
    with pytest.raises(MtrpError) as caught:
        finalize_with_forbidden_vault(request, tampered)
    assert caught.value.error_code.startswith("RECEIPT_")


def test_tool_argument_tamper_is_rejected_before_vault(request_factory, monkeypatch) -> None:
    request, _, _, _, envelope = setup_execution(request_factory, monkeypatch)
    data = envelope.model_dump(mode="json")
    data["receipts"][0]["calls"][0]["arguments"]["owner"] = "attacker"
    tampered = seal_envelope(data)
    with pytest.raises(MtrpError) as caught:
        finalize_with_forbidden_vault(request, tampered)
    assert caught.value.error_code == "RECEIPT_TOOL_MISMATCH"


def test_response_and_hash_tamper_are_rejected_before_vault(request_factory, monkeypatch) -> None:
    request, _, _, _, envelope = setup_execution(request_factory, monkeypatch)
    data = envelope.model_dump(mode="json")
    data["receipts"][1]["calls"][0]["response"]["data"]["sha"] = "b" * 40
    tampered = seal_envelope(data)
    with pytest.raises(MtrpError) as caught:
        finalize_with_forbidden_vault(request, tampered)
    assert caught.value.error_code in ("RECEIPT_RESPONSE_INVALID", "RECEIPT_INTEGRITY_FAILED")


def test_non_whitelist_reason_cannot_inject_secret_into_audit(request_factory, monkeypatch) -> None:
    request, _, _, _, envelope = setup_execution(request_factory, monkeypatch)
    data = envelope.model_dump(mode="json")
    data["receipts"][0]["reason_code"] = "token=secret-canary"
    tampered = seal_envelope(data)
    with pytest.raises(MtrpError) as caught:
        finalize_with_forbidden_vault(request, tampered)
    assert caught.value.error_code == "RECEIPT_RESPONSE_INVALID"
    assert "secret-canary" not in caught.value.safe_message


def test_executor_rechecks_continuation_before_any_new_remote_call(request_factory, monkeypatch) -> None:
    _, compiled, transport, executor, envelope = setup_execution(request_factory, monkeypatch)
    before = len(transport.calls)
    tampered = envelope.model_copy(update={"continuation_hash": "0" * 64})
    with pytest.raises(MtrpError) as caught:
        executor.execute(compiled.manifest, tampered)
    assert caught.value.error_code == "RECEIPT_INTEGRITY_FAILED"
    assert len(transport.calls) == before
