"""QA regressions for nonempty real M and deterministic receipt projections."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from ruamel.yaml import YAML

from mtrp_hub.adapters.lexiang import LexiangMAdapter
from mtrp_hub.adapters.stage2_base import Stage2ExecutionContext
from mtrp_hub.application_stage2 import ManifestService, QueryFinalizationService
from mtrp_hub.audit.sqlite_stage2 import Stage2DecisionLog
from mtrp_hub.canonical import canonical_json, sha256_json
from mtrp_hub.cli import app
from mtrp_hub.domain.enums import Intent, StoreKind
from mtrp_hub.execution.envelope import EnvelopeVerifier, RawResponse, RawToolOutcome
from mtrp_hub.execution.executor import WorkBuddyManifestExecutor
from mtrp_hub.execution.models import ExecutionManifest, ExecutionMode
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader
from mtrp_hub.fixtures.stage2_schemas import Stage2Fixture
from mtrp_hub.fixtures.stage2_transport import Stage2FixtureTransport

from tests.conftest import ROOT


def signed_mode(manifest: ExecutionManifest, mode: ExecutionMode) -> ExecutionManifest:
    data = manifest.model_dump(mode="json")
    data["mode"] = mode.value
    data.pop("execution_id")
    data.pop("manifest_hash")
    digest = sha256_json(data)
    return ExecutionManifest.model_validate({**data, "execution_id": "exe_" + digest, "manifest_hash": digest})


def execute(compiled, manifest: ExecutionManifest, fixture: Stage2Fixture):  # type: ignore[no-untyped-def]
    transport = Stage2FixtureTransport(compiled.config, fixture)
    executor = WorkBuddyManifestExecutor(transport, binding=compiled.binding, verifier=compiled.verifier)
    envelope = executor.execute(manifest)
    EnvelopeVerifier(binding=compiled.binding).verify_prefix(manifest, envelope)
    return transport, envelope


def customized_project_fixture(*, suffix: str, protected: bool, blob_sha: str, duration: int) -> Stage2Fixture:
    base = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-project-context.json")
    data = base.model_dump(mode="json")
    data["fixture_id"] = f"projection-{suffix}"
    data["responses"]["github_list_branches"][0]["response"]["data"][0]["protected"] = protected
    commit = data["responses"]["github_resolve_ref"][0]["response"]["data"]
    commit["html_url"] = f"https://example.invalid/{suffix}"
    commit["commit"] = {"message": f"synthetic message {suffix}"}
    data["responses"]["github_read_file"][0]["response"] = {
        "kind": "text",
        "text": "successfully downloaded text file (SHA: " + blob_sha + ")\nREADME fixture\n",
    }
    data["responses"]["lexiang_resolve_space"] = [
        {
            "response": {
                "kind": "json",
                "data": {
                    "code": 0,
                    "message": "success",
                    "data": {
                        "space": {"id": "0" * 32, "team_id": "0" * 32, "root_entry_id": "0" * 32},
                        "config": {"noise": suffix},
                    },
                    "request_id": f"random-{suffix}",
                },
            },
            "duration_ms": duration,
        }
    ]
    for operation in ("lexiang_search_text", "lexiang_search_vector"):
        data["responses"][operation] = [
            {
                "response": {
                    "kind": "json",
                    "data": {"code": 0, "message": "success", "data": {}, "request_id": f"random-{suffix}"},
                },
                "duration_ms": duration,
            }
        ]
    for outcomes in data["responses"].values():
        for outcome in outcomes:
            outcome["duration_ms"] = duration
    return Stage2Fixture.model_validate(data)


def test_real_nonempty_m_has_specific_reason_and_zero_entry_calls(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    request = request_factory(intent=Intent.GENERAL_KNOWLEDGE, project_id=None, repo_id=None)
    compiled = ManifestService().compile_request(request, ExecutionMode.FIXTURE)
    manifest = signed_mode(compiled.manifest, ExecutionMode.WORKBUDDY_MANIFEST)
    fixture = Stage2Fixture(
        fixture_id="real-nonempty-unconfirmed",
        responses={
            "lexiang_search_text": (
                RawToolOutcome(
                    response=RawResponse(
                        kind="json",
                        data={"code": 0, "message": "success", "data": {"entries": []}, "request_id": "synthetic"},
                    )
                ),
            )
        },
    )
    transport, envelope = execute(compiled, manifest, fixture)
    text_receipt = next(item for item in envelope.receipts if item.step_id.endswith("search-text"))
    dependent = next(item for item in envelope.receipts if item.step_id.endswith("read-text-entries"))
    assert text_receipt.status.value == "DEGRADED"
    assert text_receipt.reason_code == "M_NONEMPTY_CONTRACT_UNCONFIRMED"
    assert dependent.reason_code == "UPSTREAM_BINDING_FAILED" and dependent.calls == ()
    assert all(call.inner_tool_name not in ("entry_describe_entry", "entry_describe_ai_parse_content") for call in transport.calls)
    route_step = next(step for step in compiled.route.steps if step.store is StoreKind.M)
    context = Stage2ExecutionContext(compiled.config, manifest, route_step)
    normalized = LexiangMAdapter().normalize(route_step, envelope.receipts, context)
    assert normalized.step_result.status.value == "DEGRADED"
    assert normalized.step_result.reason_code == "M_NONEMPTY_CONTRACT_UNCONFIRMED"
    assert normalized.step_result.resource_count == 0


def test_real_nonempty_m_reason_reaches_cli_and_sqlite(
    request_factory,
    config_dir: Path,
    tmp_path: Path,
    cli_runner,
    monkeypatch,
) -> None:
    monkeypatch.chdir(ROOT)
    real_config = tmp_path / "real-config"
    shutil.copytree(config_dir, real_config)
    vault = tmp_path / "vault"
    shutil.copytree(ROOT / "tests/fixtures/stage2/vault", vault)
    stage2_path = real_config / "stage2.yaml"
    yaml = YAML(typ="safe")
    stage2 = yaml.load(stage2_path.read_text(encoding="utf-8"))
    stage2["obsidian"]["mode"] = "obsidian-local"
    stage2["obsidian"]["vault_root"] = str(vault)
    stage2["obsidian"]["g1_confirmed"] = True
    stage2["lexiang_m"]["mode"] = "workbuddy-manifest"
    stage2["github_p"]["mode"] = "workbuddy-manifest"
    with stage2_path.open("w", encoding="utf-8") as stream:
        yaml.dump(stage2, stream)
    request = request_factory(
        query="synthetic real nonempty query",
        intent=Intent.GENERAL_KNOWLEDGE,
        project_id=None,
        repo_id=None,
        config_dir=str(real_config),
    )
    compiled = ManifestService().compile_request(request, ExecutionMode.WORKBUDDY_MANIFEST)
    fixture = Stage2Fixture(
        fixture_id="real-nonempty-cli",
        responses={
            "lexiang_search_text": (
                RawToolOutcome(
                    response=RawResponse(
                        kind="json",
                        data={"code": 0, "message": "success", "data": {"entries": []}, "request_id": "synthetic"},
                    )
                ),
            )
        },
    )
    transport, envelope = execute(compiled, compiled.manifest, fixture)
    assert all(call.inner_tool_name not in ("entry_describe_entry", "entry_describe_ai_parse_content") for call in transport.calls)
    result = cli_runner.invoke(
        app,
        [
            "query", "finalize",
            "--mode", "workbuddy-manifest",
            "--config-dir", str(real_config),
            "--query", "synthetic real nonempty query",
            "--intent", "general_knowledge",
            "--expected-manifest-hash", compiled.manifest.manifest_hash,
        ],
        input=canonical_json(envelope),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data_mode"] == "workbuddy-manifest"
    assert payload["local_data_mode"] == "obsidian-local"
    assert payload["step_results"][0]["reason_code"] == "M_NONEMPTY_CONTRACT_UNCONFIRMED"
    assert payload["step_results"][0]["resource_count"] == 0
    assert payload["step_results"][1]["resource_count"] == 1
    assert "synthetic real nonempty query" not in result.stdout
    runs = Stage2DecisionLog(real_config / ".mtrp" / "decision-log.sqlite3", create=False).find(
        trace_id=payload["trace_id"]
    )
    assert len(runs) == 1 and runs[0].reason_code == "M_NONEMPTY_CONTRACT_UNCONFIRMED"
    search_events = [event for event in runs[0].events if event.operation == "lexiang_search_text"]
    assert len(search_events) == 1 and search_events[0].reason_code == "M_NONEMPTY_CONTRACT_UNCONFIRMED"
    assert "synthetic real nonempty query" not in canonical_json(runs)


def test_ignored_fields_and_duration_do_not_change_receipts_or_v2(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    request = request_factory()
    compiled = ManifestService().compile_request(request, ExecutionMode.FIXTURE)
    left = customized_project_fixture(suffix="left", protected=False, blob_sha="a" * 40, duration=0)
    right = customized_project_fixture(suffix="right", protected=True, blob_sha="f" * 40, duration=17)
    _, left_envelope = execute(compiled, compiled.manifest, left)
    _, right_envelope = execute(compiled, compiled.manifest, right)
    left_hashes = {item.step_id: item.receipt_hash for item in left_envelope.receipts}
    right_hashes = {item.step_id: item.receipt_hash for item in right_envelope.receipts}
    assert left_hashes == right_hashes
    left_raw_hashes = {call.call_id: call.response_sha256 for receipt in left_envelope.receipts for call in receipt.calls}
    right_raw_hashes = {call.call_id: call.response_sha256 for receipt in right_envelope.receipts for call in receipt.calls}
    assert left_raw_hashes != right_raw_hashes
    branches = left_envelope.receipts[0].binding_data["branches"]
    assert all(set(item) == {"name", "sha"} for item in branches)
    first_receipt = left_envelope.receipts[0]
    first_step = next(step for step in compiled.manifest.steps if step.step_id == first_receipt.step_id)
    projection = first_receipt.hashed_payload(compiled.manifest, first_step)
    assert "binding_data" not in projection
    assert all("response" not in call and "response_sha256" not in call and "duration_ms" not in call for call in projection["calls"])
    assert all(len(call["projection_sha256"]) == 64 for call in projection["calls"])
    service = QueryFinalizationService()
    left_response = service.run_fixture(request, left)
    right_response = service.run_fixture(request, right)
    assert [step.model_dump(mode="json") for step in left_response.step_results] == [step.model_dump(mode="json") for step in right_response.step_results]


def test_approved_file_content_change_still_changes_receipt_hash(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    compiled = ManifestService().compile_request(request_factory(), ExecutionMode.FIXTURE)
    left = customized_project_fixture(suffix="left", protected=False, blob_sha="a" * 40, duration=0)
    right_data = left.model_dump(mode="json")
    right_data["responses"]["github_read_file"][0]["response"]["text"] = (
        "successfully downloaded text file (SHA: " + "a" * 40 + ")\nchanged approved content\n"
    )
    right = Stage2Fixture.model_validate(right_data)
    _, left_envelope = execute(compiled, compiled.manifest, left)
    _, right_envelope = execute(compiled, compiled.manifest, right)
    left_file = next(item for item in left_envelope.receipts if item.step_id.endswith("read-file-01"))
    right_file = next(item for item in right_envelope.receipts if item.step_id.endswith("read-file-01"))
    assert left_file.receipt_hash != right_file.receipt_hash


def test_root_entry_change_with_same_count_changes_projection(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    compiled = ManifestService().compile_request(request_factory(), ExecutionMode.FIXTURE)
    left = customized_project_fixture(suffix="same", protected=False, blob_sha="a" * 40, duration=0)
    right_data = left.model_dump(mode="json")
    right_data["responses"]["github_read_repository_root"][0]["response"]["data"][0]["name"] = "OTHER.md"
    right_data["responses"]["github_read_repository_root"][0]["response"]["data"][0]["path"] = "OTHER.md"
    right = Stage2Fixture.model_validate(right_data)
    _, left_envelope = execute(compiled, compiled.manifest, left)
    _, right_envelope = execute(compiled, compiled.manifest, right)
    left_root = next(item for item in left_envelope.receipts if item.step_id.endswith("read-repository-root"))
    right_root = next(item for item in right_envelope.receipts if item.step_id.endswith("read-repository-root"))
    assert left_root.binding_data["root_entry_count"] == right_root.binding_data["root_entry_count"] == 1
    assert left_root.receipt_hash != right_root.receipt_hash


def test_nonempty_m_random_request_ids_do_not_change_v2(request_factory, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    request = request_factory(intent=Intent.GENERAL_KNOWLEDGE, project_id=None, repo_id=None)
    base = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-m-nonempty.json")

    def customized(suffix: str, duration: int) -> Stage2Fixture:
        data = copy.deepcopy(base.model_dump(mode="json"))

        def visit(value) -> None:  # type: ignore[no-untyped-def]
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "request_id":
                        value[key] = f"random-{suffix}"
                    elif key == "duration_ms":
                        value[key] = duration
                    else:
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(data)
        return Stage2Fixture.model_validate(data)

    service = QueryFinalizationService()
    left = service.run_fixture(request, customized("left", 0))
    right = service.run_fixture(request, customized("right", 23))
    assert [step.model_dump(mode="json") for step in left.step_results] == [step.model_dump(mode="json") for step in right.step_results]
