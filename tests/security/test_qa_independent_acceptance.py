"""Independent stage 0+1 acceptance probes written from the PRD contracts."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from mtrp_hub.adapters.fake import FakeAdapter
from mtrp_hub.application import RouteQueryService
from mtrp_hub.audit.sqlite import SQLiteDecisionLog
from mtrp_hub.canonical import canonical_json, sha256_bytes
from mtrp_hub.cli import response_payload
from mtrp_hub.domain.enums import ReasonCode, StepStatus
from mtrp_hub.domain.models import CanonicalResult
from mtrp_hub.errors import MtrpError
from mtrp_hub.fixtures.loader import FixtureLoader
from mtrp_hub.fixtures.schemas import FixtureBundle
from mtrp_hub.security import RequestAccessGuard

from tests.conftest import ROOT


class LoaderSpy:
    def __init__(self) -> None:
        self.calls = 0

    def load(self, path: str | None) -> FixtureBundle:
        self.calls += 1
        raise AssertionError("FixtureLoader was reached before access validation")


def forbid_target_io(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    original_open = Path.open
    original_resolve = Path.resolve
    original_stat = Path.stat

    def checked_open(self: Path, *args, **kwargs):
        if self == target:
            raise AssertionError(f"Protected target was opened: {target}")
        return original_open(self, *args, **kwargs)

    def checked_resolve(self: Path, *args, **kwargs):
        if self == target:
            raise AssertionError(f"Protected target was resolved: {target}")
        return original_resolve(self, *args, **kwargs)

    def checked_stat(self: Path, *args, **kwargs):
        if self == target:
            raise AssertionError(f"Protected target was stated: {target}")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    monkeypatch.setattr(Path, "resolve", checked_resolve)
    monkeypatch.setattr(Path, "stat", checked_stat)


def read_database_rows(path: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql).fetchall()


@pytest.mark.parametrize(
    ("overrides", "attack", "expected_code"),
    [
        ({"project_id": "https://evil.invalid/p"}, "https://evil.invalid/p", "COORDINATE_URL_FORBIDDEN"),
        ({"repo_id": "https://evil.invalid/r"}, "https://evil.invalid/r", "COORDINATE_URL_FORBIDDEN"),
        ({"workspace_id": "https://evil.invalid/w"}, "https://evil.invalid/w", "COORDINATE_URL_FORBIDDEN"),
        ({"project_id": "C:/private/project"}, "C:/private/project", "ABSOLUTE_PATH_FORBIDDEN"),
        ({"repo_id": "C:\\private\\repo"}, "C:\\private\\repo", "ABSOLUTE_PATH_FORBIDDEN"),
        ({"project_id": "/private/project"}, "/private/project", "ABSOLUTE_PATH_FORBIDDEN"),
        ({"workspace_id": "E:/private/workspace"}, "E:/private/workspace", "UNREGISTERED_WORKSPACE"),
        ({"workspace_id": "/private/workspace"}, "/private/workspace", "UNREGISTERED_WORKSPACE"),
        ({"project_id": "../escape"}, "../escape", "PATH_ESCAPE"),
        ({"repo_id": ".."}, "..", "PATH_ESCAPE"),
        ({"workspace_id": "../escape"}, "../escape", "PATH_ESCAPE"),
        ({"workspace_id": "not-registered"}, "not-registered", "UNREGISTERED_WORKSPACE"),
    ],
)
def test_attack_coordinates_stop_before_loader_adapter_and_target(
    request_factory,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    attack: str,
    expected_code: str,
) -> None:
    loader = LoaderSpy()
    adapter_calls = []
    forbid_target_io(monkeypatch, Path(attack))
    monkeypatch.setattr(socket, "socket", lambda *args: (_ for _ in ()).throw(AssertionError("network")))
    service = RouteQueryService(
        fixture_loader=loader,
        adapter_factory=lambda kind: adapter_calls.append(kind) or FakeAdapter(kind),
    )

    with pytest.raises(MtrpError) as caught:
        service.query(request_factory(**overrides, fixture="never-load.json"))

    assert caught.value.error_code == expected_code
    assert caught.value.exit_code == 2
    assert loader.calls == 0
    assert adapter_calls == []
    database = config_dir / ".mtrp" / "decision-log.sqlite3"
    assert attack.encode("utf-8") not in database.read_bytes()


def test_registered_workspace_is_disabled_before_target_or_adapter(
    request_factory,
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "registered-but-disabled"
    projects_path = config_dir / "projects.yaml"
    yaml = YAML(typ="safe")
    projects = yaml.load(projects_path.read_text(encoding="utf-8"))
    repository = projects["projects"][0]["repositories"][0]
    repository["locations"].append(
        {
            "location_id": "local-disabled",
            "kind": "local_git",
            "role": "workspace",
            "state": "unknown",
            "snapshots": [],
        }
    )
    repository["workspaces"] = [
        {
            "workspace_id": "registered-disabled",
            "location_id": "local-disabled",
            "root_path": target.as_posix(),
        }
    ]
    with projects_path.open("w", encoding="utf-8") as stream:
        yaml.dump(projects, stream)

    loader = LoaderSpy()
    adapter_calls = []
    forbid_target_io(monkeypatch, target)
    service = RouteQueryService(
        fixture_loader=loader,
        adapter_factory=lambda kind: adapter_calls.append(kind) or FakeAdapter(kind),
    )
    with pytest.raises(MtrpError) as caught:
        service.query(
            request_factory(workspace_id="registered-disabled", fixture="never-load.json")
        )

    assert caught.value.error_code == "WORKSPACE_QUERY_DISABLED"
    assert loader.calls == 0
    assert adapter_calls == []
    database = config_dir / ".mtrp" / "decision-log.sqlite3"
    assert target.as_posix().encode("utf-8") not in database.read_bytes()


def test_query_url_and_credentials_are_hashed_without_network_or_output_leak(
    request_factory,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(socket, "socket", lambda *args: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args: (_ for _ in ()).throw(AssertionError("network")),
    )
    secret_query = (
        "Read https://evil.invalid/private token=QA_TOKEN_71 "
        "Cookie=QA_COOKIE_72 Authorization: Bearer QA_BEARER_73"
    )
    response = RouteQueryService().query(
        request_factory(query=secret_query, fixture="tests/fixtures/project-context.json")
    )
    rendered = canonical_json(response_payload(response))
    database = config_dir / ".mtrp" / "decision-log.sqlite3"
    raw_database = database.read_bytes()
    for sensitive in (
        secret_query,
        "QA_TOKEN_71",
        "QA_COOKIE_72",
        "QA_BEARER_73",
        "evil.invalid",
        "Fixture body is never emitted.",
        "example.invalid/owner/repository",
    ):
        assert sensitive not in rendered
        assert sensitive.encode("utf-8") not in raw_database


@pytest.mark.parametrize("field", ["adapter", "operation", "network", "allow_network", "write", "route", "steps"])
@pytest.mark.parametrize("nested", [False, True])
def test_fixture_injection_fields_fail_closed(field: str, nested: bool) -> None:
    data = FixtureBundle.empty().model_dump(mode="json")
    target = data["p"] if nested else data
    target[field] = {"enabled": True, "command": "push"}

    with pytest.raises(MtrpError) as caught:
        FixtureLoader.loads(json.dumps(data))

    assert caught.value.error_code == "FIXTURE_INVALID"
    assert "push" not in caught.value.safe_message
    assert "push" not in (caught.value.field or "")


def test_malformed_fixture_never_constructs_an_adapter(
    request_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    data = FixtureBundle.empty().model_dump(mode="json")
    data["p"]["write"] = {"operation": "push"}
    Path("malicious.json").write_text(json.dumps(data), encoding="utf-8")
    adapter_calls = []
    service = RouteQueryService(
        adapter_factory=lambda kind: adapter_calls.append(kind) or FakeAdapter(kind)
    )

    with pytest.raises(MtrpError) as caught:
        service.query(request_factory(fixture="malicious.json"))

    assert caught.value.error_code == "FIXTURE_INVALID"
    assert adapter_calls == []


def test_empty_and_branch_faults_continue_without_fake_hits(
    request_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    empty = RouteQueryService().query(
        request_factory(fixture="tests/fixtures/m-empty-p-empty.json")
    )
    assert [item.reason_code for item in empty.step_results[:2]] == [
        ReasonCode.P_EMPTY,
        ReasonCode.M_EMPTY,
    ]
    assert empty.step_results[2].status is StepStatus.OK
    assert all(item.source_ref.store.value == "T" for item in empty.result.resources)

    empty_repository = RouteQueryService().query(
        request_factory(fixture="tests/fixtures/p-empty-repository.json")
    )
    assert empty_repository.step_results[0].reason_code is ReasonCode.EMPTY_REPOSITORY
    assert empty_repository.step_results[0].coordinate.commit is None
    assert all(item.source_ref.store.value != "P" for item in empty_repository.result.resources)

    branch_missing = RouteQueryService().query(
        request_factory(fixture="tests/fixtures/branch-missing.json")
    )
    assert branch_missing.step_results[0].status is StepStatus.DEGRADED
    assert branch_missing.step_results[0].reason_code is ReasonCode.BRANCH_NOT_FOUND
    assert len(branch_missing.step_results) == 3
    assert branch_missing.result.resources == ()


def test_same_main_distinct_sha_can_coexist_without_merging(
    request_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    service = RouteQueryService()
    first = service.query(request_factory(fixture="tests/fixtures/p-main-sha-a.json"))
    second = service.query(request_factory(fixture="tests/fixtures/p-main-sha-b.json"))

    assert first.step_results[0].coordinate.ref == second.step_results[0].coordinate.ref == "main"
    assert first.step_results[0].coordinate.commit == "a" * 40
    assert second.step_results[0].coordinate.commit == "b" * 40
    assert first.result.resources[0].resource_id != second.result.resources[0].resource_id
    assert first.result.evidence[0].evidence_id != second.result.evidence[0].evidence_id
    combined = CanonicalResult.create(
        first.result.resources + second.result.resources,
        first.result.relations + second.result.relations,
        first.result.evidence + second.result.evidence,
    )
    assert len(combined.resources) == 2
    assert len(combined.evidence) == 2
    assert {item.snapshot_ref.value for item in combined.resources} == {"a" * 40, "b" * 40}


@pytest.mark.parametrize(
    ("markdown", "expected_status", "expected_reason"),
    [
        ("---\ntags: [mtr/trial\n---\nbody", StepStatus.DEGRADED, ReasonCode.INVALID_YAML),
        ("# Unmanaged\ninline #mtr/trial is not frontmatter\n", StepStatus.EMPTY, ReasonCode.T_EMPTY),
    ],
)
def test_invalid_yaml_and_missing_frontmatter_are_honest_t_results(
    request_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    markdown: str,
    expected_status: StepStatus,
    expected_reason: ReasonCode,
) -> None:
    monkeypatch.chdir(tmp_path)
    data = json.loads((ROOT / "tests/fixtures/m-empty-p-empty.json").read_text(encoding="utf-8"))
    data["t"]["documents"][0]["raw_markdown"] = markdown
    Path("vault-case.json").write_text(json.dumps(data), encoding="utf-8")

    response = RouteQueryService().query(request_factory(fixture="vault-case.json"))

    assert response.step_results[2].status is expected_status
    assert response.step_results[2].reason_code is expected_reason
    assert all(item.source_ref.store.value != "T" for item in response.result.resources)


def test_invalid_configuration_yaml_is_safe_input_error(
    request_factory,
    config_dir: Path,
) -> None:
    (config_dir / "stores.yaml").write_text("stores: [unterminated\n", encoding="utf-8")
    loader = LoaderSpy()
    adapter_calls = []
    service = RouteQueryService(
        fixture_loader=loader,
        adapter_factory=lambda kind: adapter_calls.append(kind) or FakeAdapter(kind),
    )

    with pytest.raises(MtrpError) as caught:
        service.query(request_factory())

    assert caught.value.error_code == "CONFIG_INVALID"
    assert caught.value.field == "stores.yaml"
    assert caught.value.exit_code == 2
    assert loader.calls == 0
    assert adapter_calls == []


def test_decisions_find_is_read_only_and_does_not_touch_wal(
    request_factory,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    response = RouteQueryService().query(
        request_factory(fixture="tests/fixtures/project-context.json")
    )
    log_dir = config_dir / ".mtrp"
    database = log_dir / "decision-log.sqlite3"
    before = {
        path.name: sha256_bytes(path.read_bytes())
        for path in log_dir.iterdir()
        if path.is_file()
    }

    original_connect = sqlite3.connect
    read_uris: list[str] = []

    def connect_spy(target, *args, **kwargs):
        if kwargs.get("uri"):
            read_uris.append(str(target))
        return original_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_spy)
    log = SQLiteDecisionLog(database, create=False)
    by_request = log.find(request_hash=response.plan.request_hash)
    by_plan = log.find(plan_hash=response.plan.plan_hash)

    assert by_request == by_plan
    assert by_request[-1].event_type.value == "QUERY_FINISHED"
    assert len(read_uris) == 2
    assert all("mode=ro" in uri for uri in read_uris)
    after = {
        path.name: sha256_bytes(path.read_bytes())
        for path in log_dir.iterdir()
        if path.is_file()
    }
    # SQLite may create/update shared-memory bookkeeping and an empty WAL sidecar
    # for a mode=ro reader. Existing DB/WAL content must remain byte-identical.
    sidecars = {"decision-log.sqlite3-shm", "decision-log.sqlite3-wal"}
    assert set(after) <= set(before) | sidecars
    wal = log_dir / "decision-log.sqlite3-wal"
    if "decision-log.sqlite3-wal" not in before and wal.exists():
        assert wal.stat().st_size == 0
    for name, digest in before.items():
        if not name.endswith("-shm"):
            assert after[name] == digest


def test_failed_fixture_query_has_one_request_hash_across_its_audit(
    request_factory,
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    data = FixtureBundle.empty().model_dump(mode="json")
    data["operation"] = "write"
    Path("invalid.json").write_text(json.dumps(data), encoding="utf-8")
    request = request_factory(fixture="invalid.json")

    with pytest.raises(MtrpError) as caught:
        RouteQueryService().query(request)

    assert caught.value.error_code == "FIXTURE_INVALID"
    database = config_dir / ".mtrp" / "decision-log.sqlite3"
    rows = read_database_rows(
        database,
        "SELECT request_hash, plan_hash, event_type FROM decision_log ORDER BY seq",
    )
    assert [row[2] for row in rows] == ["PLAN_CREATED", "REQUEST_REJECTED"]
    assert len({row[0] for row in rows}) == 1
    request_hash = RequestAccessGuard.successful_request_hash(request)
    events = SQLiteDecisionLog(database, create=False).find(request_hash=request_hash)
    assert [event.event_type.value for event in events] == ["PLAN_CREATED", "REQUEST_REJECTED"]


def test_write_rejection_is_recorded_in_decision_log(config_dir: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "mtrp_hub", "commit", "--config-dir", str(config_dir)],
        cwd=ROOT,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"WRITE_DISABLED" in completed.stderr
    database = config_dir / ".mtrp" / "decision-log.sqlite3"
    assert database.is_file(), "WRITE_DISABLED was not audited"
    rows = read_database_rows(
        database,
        "SELECT event_type, reason_code, payload_json FROM decision_log",
    )
    assert [(row[0], row[1]) for row in rows] == [("REQUEST_REJECTED", "WRITE_DISABLED")]
    assert str(config_dir) not in rows[0][2]


@pytest.mark.parametrize(
    "query",
    [pytest.param("", id="empty"), pytest.param("x" * 16385, id="too-long")],
)
def test_invalid_query_length_is_safe_exit_two_not_traceback(
    config_dir: Path,
    query: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mtrp_hub",
            "route",
            "query",
            "--config-dir",
            str(config_dir),
            "--query",
            query,
            "--intent",
            "general_knowledge",
        ],
        cwd=ROOT,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 2, completed.stderr.decode("utf-8", errors="replace")[:500]
    assert completed.stdout == b""
    error = json.loads(completed.stderr)
    assert error["error_code"] == "INVALID_INPUT"
    assert error["field"] == "query"
    assert b"Traceback" not in completed.stderr


def test_secret_like_fixture_title_cannot_leak_into_resource_output(
    request_factory,
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    secret = "QA_TITLE_BEARER_SECRET_819"
    data = json.loads((ROOT / "tests/fixtures/project-context.json").read_text(encoding="utf-8"))
    data["p"]["resources"][0]["title"] = f"Authorization: Bearer {secret}"
    Path("secret-title.json").write_text(json.dumps(data), encoding="utf-8")

    try:
        response = RouteQueryService().query(request_factory(fixture="secret-title.json"))
    except MtrpError as error:
        assert error.exit_code == 2
        assert secret not in error.safe_message
        assert secret not in (error.field or "")
    else:
        rendered = canonical_json(response_payload(response))
        assert secret not in rendered
    database = config_dir / ".mtrp" / "decision-log.sqlite3"
    assert secret.encode("utf-8") not in database.read_bytes()
