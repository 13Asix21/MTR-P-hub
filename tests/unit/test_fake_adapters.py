"""FakeAdapter status, Fixture rejection, and snapshot isolation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtrp_hub.adapters.base import FakeExecutionContext
from mtrp_hub.adapters.fake import FakeAdapter
from mtrp_hub.canonical import sha256_json
from mtrp_hub.config.loader import ConfigLoader
from mtrp_hub.domain.enums import Intent, ReasonCode, StepStatus, StoreKind
from mtrp_hub.domain.requests import RouteQueryRequest
from mtrp_hub.errors import MtrpError
from mtrp_hub.fixtures.loader import FixtureLoader
from mtrp_hub.fixtures.schemas import FixtureBundle
from mtrp_hub.routing.planner import ReadPolicy
from mtrp_hub.security import AccessDecision


def execution_for(bundle: FixtureBundle, config_dir: Path, intent: Intent = Intent.PROJECT_CONTEXT):
    loaded = ConfigLoader().load(str(config_dir))
    needs_p = intent in (Intent.PROJECT_CONTEXT, Intent.DECISION_HISTORY)
    request = RouteQueryRequest(
        query="fake unit",
        intent=intent,
        project_id="example-project" if needs_p else None,
        repo_id="example-repo" if needs_p else None,
        config_dir=str(config_dir),
    )
    coordinate = loaded.projects.resolve_default("example-project", "example-repo") if needs_p else None
    plan = ReadPolicy(loaded.policy).create_plan(request, coordinate, loaded.stores, loaded.projects)
    fixture_hash = sha256_json(bundle)
    context = FakeExecutionContext(
        request_hash=plan.request_hash,
        plan_hash=plan.plan_hash,
        fixture_hash=fixture_hash,
        access_decision=AccessDecision(request_hash=plan.request_hash, coordinate=coordinate),
    )
    return [FakeAdapter(step.store).execute(step, bundle, context) for step in plan.steps]


def test_m_and_p_empty_are_honest_and_continue(bundle_loader, config_dir: Path) -> None:
    executions = execution_for(bundle_loader("m-empty-p-empty.json"), config_dir)
    assert executions[0].step_result.reason_code is ReasonCode.P_EMPTY
    assert executions[1].step_result.reason_code is ReasonCode.M_EMPTY
    assert executions[2].step_result.status is StepStatus.OK
    assert not executions[0].resources and not executions[1].resources


def test_empty_repository_has_no_commit_or_resource(bundle_loader, config_dir: Path) -> None:
    execution = execution_for(bundle_loader("p-empty-repository.json"), config_dir)[0]
    assert execution.step_result.reason_code is ReasonCode.EMPTY_REPOSITORY
    assert execution.step_result.coordinate.commit is None
    assert execution.resources == ()


def test_same_main_different_sha_has_distinct_snapshots_and_evidence(bundle_loader, config_dir: Path) -> None:
    first = execution_for(bundle_loader("p-main-sha-a.json"), config_dir)[0]
    second = execution_for(bundle_loader("p-main-sha-b.json"), config_dir)[0]
    assert first.resources[0].snapshot_ref.value == "a" * 40
    assert second.resources[0].snapshot_ref.value == "b" * 40
    assert first.resources[0].resource_id != second.resources[0].resource_id
    assert first.evidence[0].evidence_id != second.evidence[0].evidence_id


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("branch-missing.json", ReasonCode.BRANCH_NOT_FOUND),
        ("permission-denied.json", ReasonCode.PERMISSION_DENIED),
    ],
)
def test_p_faults_degrade_without_resources(name: str, reason: ReasonCode, bundle_loader, config_dir: Path) -> None:
    execution = execution_for(bundle_loader(name), config_dir)[0]
    assert execution.step_result.status is StepStatus.DEGRADED
    assert execution.step_result.reason_code is reason
    assert execution.resources == ()


def test_m_empty_response_is_distinct_from_empty_library(bundle_loader, config_dir: Path) -> None:
    execution = execution_for(bundle_loader("empty-response.json"), config_dir)[1]
    assert execution.step_result.status is StepStatus.DEGRADED
    assert execution.step_result.reason_code is ReasonCode.EMPTY_RESPONSE


def test_fixture_extra_route_adapter_or_write_field_is_rejected() -> None:
    text = FixtureBundle.empty().model_dump_json()
    text = text[:-1] + ',"adapter":"github","operation":"write"}'
    with pytest.raises(MtrpError) as caught:
        FixtureLoader.loads(text)
    assert caught.value.error_code in ("FIXTURE_INVALID", "FIXTURE_PARSE_ERROR")


def test_fixture_cannot_inject_commit_into_empty_repository() -> None:
    data = FixtureBundle.empty().model_dump(mode="json")
    data["p"]["status"] = "empty_repository"
    data["p"]["repository"] = {
        "project_id": "example-project",
        "repo_id": "example-repo",
        "location_id": "remote-main",
        "ref": "main",
        "commit": "a" * 40,
    }
    import json

    with pytest.raises(MtrpError):
        FixtureLoader.loads(json.dumps(data))


def test_invalid_vault_yaml_is_step_degradation(bundle_loader, config_dir: Path) -> None:
    data = bundle_loader("m-empty-p-empty.json").model_dump(mode="json")
    data["t"]["documents"][0]["raw_markdown"] = "---\ntags: [mtr/trial, broken\n---\nbody"
    executions = execution_for(FixtureBundle.model_validate(data), config_dir)
    assert executions[2].step_result.status is StepStatus.DEGRADED
    assert executions[2].step_result.reason_code is ReasonCode.INVALID_YAML
    assert executions[2].resources == ()


def test_no_frontmatter_document_does_not_enter_t(bundle_loader, config_dir: Path) -> None:
    data = bundle_loader("m-empty-p-empty.json").model_dump(mode="json")
    data["t"]["documents"][0]["raw_markdown"] = "# Unmanaged\ninline #mtr/trial is not frontmatter\n"
    executions = execution_for(FixtureBundle.model_validate(data), config_dir)
    assert executions[2].step_result.status is StepStatus.EMPTY
    assert executions[2].step_result.reason_code is ReasonCode.T_EMPTY


def test_adr_missing_continues_with_honest_reason(bundle_loader, config_dir: Path) -> None:
    executions = execution_for(bundle_loader("p-main-sha-a.json"), config_dir, Intent.DECISION_HISTORY)
    assert executions[0].step_result.status is StepStatus.EMPTY
    assert executions[0].step_result.reason_code is ReasonCode.ADR_MISSING
    assert executions[0].resources == ()


def test_unresolved_m_space_is_unavailable_not_empty_success(bundle_loader, config_dir: Path) -> None:
    data = bundle_loader("m-empty-p-empty.json").model_dump(mode="json")
    data["m"]["space_resolved"] = False
    executions = execution_for(FixtureBundle.model_validate(data), config_dir)
    assert executions[1].step_result.status is StepStatus.DEGRADED
    assert executions[1].step_result.reason_code is ReasonCode.UNAVAILABLE


def test_malicious_extra_key_does_not_leak_into_safe_error_field() -> None:
    import json

    data = FixtureBundle.empty().model_dump(mode="json")
    data["E:/private/secret.json"] = True
    with pytest.raises(MtrpError) as caught:
        FixtureLoader.loads(json.dumps(data))
    assert caught.value.error_code == "FIXTURE_INVALID"
    assert caught.value.field == "fixture"
    assert "E:/private" not in caught.value.safe_message
    assert "E:/private" not in (caught.value.field or "")
