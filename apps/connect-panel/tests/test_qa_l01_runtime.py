# -*- coding: utf-8 -*-
"""L01 独立 QA：仅合成临时目录，不导入仓库 users.json。"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import pytest

PANEL_DIR = Path(__file__).resolve().parents[1]
PYTHON = PANEL_DIR.parents[1] / ".venv" / "Scripts" / "python.exe"
sys.path.insert(0, str(PANEL_DIR))

from panel import runtime
from panel.runtime import RuntimePaths
from panel.settings import PanelError
from panel.user_profiles import ProfileStore, UserPreferences


def _paths(root: Path) -> RuntimePaths:
    data = root / "new"
    return RuntimePaths(data, data / "users.json", root / "legacy" / "users.json", True, False)


def _legacy(path: Path, root: Path) -> bytes:
    store = ProfileStore(path)
    store.ensure_default(UserPreferences(vault_root="", git_local_path=str(root)))
    return path.read_bytes()


def test_qa_migration_nonempty_replacement_is_not_deleted_or_overwritten(tmp_path: Path) -> None:
    """实际原子发布边界：竞争者先创建非空目标时不得覆盖。"""
    paths = _paths(tmp_path)
    source_before = _legacy(paths.legacy_profile_path, tmp_path)
    competitor = b'{"schema_version":"1","users":[]}\n'
    original_link = os.link
    competed = [False]

    def competing_link(source_path, target_path, *args, **kwargs):
        if Path(target_path) == paths.profile_path and not competed[0]:
            competed[0] = True
            paths.profile_path.write_bytes(competitor)
        return original_link(source_path, target_path, *args, **kwargs)

    import panel.runtime as production
    production.os.link = competing_link
    try:
        with pytest.raises(PanelError) as caught:
            runtime.migrate_legacy_profile(paths)
        assert caught.value.code == "MIGRATION_NOT_AVAILABLE"
    finally:
        production.os.link = original_link
    assert competed[0], "探针必须到达实际 link 发布边界"
    assert paths.legacy_profile_path.read_bytes() == source_before
    assert paths.profile_path.read_bytes() == competitor
    assert list(paths.data_dir.glob(".users-migration-*.tmp")) == []


def test_qa_migration_zero_byte_replacement_survives_failure_cleanup(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    source = _legacy(paths.legacy_profile_path, tmp_path)
    original_link = os.link
    injected = [False]

    def failing_link(source_path, target_path, *args, **kwargs) -> None:
        if Path(target_path) == paths.profile_path:
            injected[0] = True
            paths.profile_path.write_bytes(b"")
            raise OSError("QA injected link publish failure")
        original_link(source_path, target_path, *args, **kwargs)

    import panel.runtime as production
    production.os.link = failing_link
    try:
        with pytest.raises(PanelError) as caught:
            runtime.migrate_legacy_profile(paths)
    finally:
        production.os.link = original_link
    assert injected[0], "探针必须到达实际 link 发布边界"
    assert caught.value.code == "MIGRATION_WRITE_FAILED"
    assert paths.legacy_profile_path.read_bytes() == source
    assert paths.profile_path.exists(), "清理删除了竞争者接管的零字节文件"
    assert paths.profile_path.read_bytes() == b""
    assert list(paths.data_dir.glob(".users-migration-*.tmp")) == []


def test_qa_migration_preexisting_target_and_source_are_preserved(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    source = _legacy(paths.legacy_profile_path, tmp_path)
    paths.data_dir.mkdir()
    paths.profile_path.write_bytes(b"target already exists")
    with pytest.raises(PanelError) as caught:
        runtime.migrate_legacy_profile(paths)
    assert caught.value.code == "MIGRATION_NOT_AVAILABLE"
    assert paths.profile_path.read_bytes() == b"target already exists"
    assert paths.legacy_profile_path.read_bytes() == source


def test_qa_migration_replace_failure_preserves_competitor(tmp_path: Path) -> None:
    """link 发布失败时，非空竞争者目标与旧源必须原字节保持。"""
    paths = _paths(tmp_path)
    source = _legacy(paths.legacy_profile_path, tmp_path)
    competitor = b"another process owns this"
    original_link = os.link
    injected = [False]

    def failing_link(source_path, target_path, *args, **kwargs) -> None:
        if Path(target_path) == paths.profile_path:
            injected[0] = True
            paths.profile_path.write_bytes(competitor)
            raise OSError("QA injected link publish failure")
        original_link(source_path, target_path, *args, **kwargs)

    import panel.runtime as production
    production.os.link = failing_link
    try:
        with pytest.raises(PanelError) as caught:
            runtime.migrate_legacy_profile(paths)
    finally:
        production.os.link = original_link
    assert injected[0], "探针必须到达实际 link 发布边界"
    assert caught.value.code == "MIGRATION_WRITE_FAILED"
    assert paths.legacy_profile_path.read_bytes() == source
    assert paths.profile_path.read_bytes() == competitor
    assert list(paths.data_dir.glob(".users-migration-*.tmp")) == []


def test_qa_explicit_new_directory_does_not_copy_legacy(tmp_path: Path, monkeypatch) -> None:
    source = _legacy(tmp_path / "legacy" / "users.json", tmp_path)
    monkeypatch.setattr(runtime, "legacy_profile_path", lambda: tmp_path / "legacy" / "users.json")
    paths = runtime.resolve_runtime_paths(str(tmp_path / "new"), environ={}, packaged=True)
    assert not paths.profile_path.exists()
    assert paths.legacy_profile_path.read_bytes() == source
    assert runtime.resolve_config_dir(paths, environ={}) == PANEL_DIR / "panel" / "default_config"
    development = runtime.resolve_runtime_paths(environ={}, packaged=False)
    assert development.profile_path == runtime.application_directory() / "data" / "users.json"
    assert development.legacy_profile_path == tmp_path / "legacy" / "users.json"
    assert runtime.migration_status(development)["migration_required"] is False


@pytest.mark.parametrize("header,value,code,status", [
    ("Host", "evil.example", "LOOPBACK_HOST_REQUIRED", 403),
    ("Origin", "http://evil.example", "CROSS_ORIGIN_REJECTED", 403),
    ("Origin", "https://127.0.0.1", "CROSS_ORIGIN_REJECTED", 403),
    ("Sec-Fetch-Site", "cross-site", "CROSS_SITE_REQUEST_REJECTED", 403),
    ("Content-Type", "text/plain", "JSON_CONTENT_TYPE_REQUIRED", 415),
])
def test_qa_request_security_contracts(header: str, value: str, code: str, status: int) -> None:
    headers = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Content-Length": "2"}
    headers[header] = value
    with pytest.raises(PanelError) as caught:
        runtime.validate_web_request("POST", headers)
    assert caught.value.code == code and caught.value.http_status == status


class RunningPanel:
    def __init__(self, data: Path, cwd: Path, legacy: Path | None = None) -> None:
        environment = os.environ.copy()
        for key in (
            "MTRP_PANEL_DATA_DIR", "MTRP_PANEL_CONFIG_DIR", "MTRP_PANEL_PACKAGED",
            "MTRP_PANEL_LEGACY_PROFILE", "PYTHONPATH",
        ):
            environment.pop(key, None)
        # 显式隔离子进程的旧档来源，绝不继承父进程 monkeypatch 或真实开发档案。
        selected_legacy = data / "legacy" / "users.json" if legacy is None else legacy
        environment["MTRP_PANEL_LEGACY_PROFILE"] = str(selected_legacy)
        environment["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            [str(PYTHON), str(PANEL_DIR / "run_panel.py"), "--port", "0", "--data-dir", str(data), "--no-browser"],
            cwd=str(cwd), env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.output: list[str] = []
        self.port: int | None = None
        deadline = time.monotonic() + 20
        assert self.process.stdout is not None
        while time.monotonic() < deadline:
            line = self.process.stdout.readline().decode("utf-8", "replace")
            self.output.append(line)
            match = re.search(r"本地面板已就绪：http://127\.0\.0\.1:(\d+)/", line)
            if match:
                self.port = int(match.group(1))
                break
            if self.process.poll() is not None:
                break
        assert self.port is not None, "".join(self.output)

    def request(self, method: str, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.headers), payload
        connection.close()
        return result

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdout:
            self.process.stdout.close()


def test_qa_real_http_safe_template_honest_empty_vault_and_static_cwd(tmp_path: Path) -> None:
    data = tmp_path / "中文数据根"
    server = RunningPanel(data, tmp_path)
    try:
        status, _, health_bytes = server.request("GET", "/api/health")
        health = json.loads(health_bytes)
        assert status == 200 and health["data"]["ready"] is True and health["data"]["local_only"] is True
        status, _, overview_bytes = server.request("GET", "/api/overview")
        overview = json.loads(overview_bytes)["data"]
        assert status == 200 and overview["stores"]["T"]["vault_path"] == ""
        assert overview["stores"]["T"]["status"] == "pending"
        status, _, index = server.request("GET", "/")
        assert status == 200 and b"<!doctype html>" in index.lower()
        status, _, rejected = server.request("GET", "/api/health", headers={"Host": "evil.example"})
        assert status == 403 and json.loads(rejected)["error"]["code"] == "LOOPBACK_HOST_REQUIRED"
        for origin in ("http://evil.example", "http://127.0.0.1:1", "https://127.0.0.1"):
            status, _, cross_origin = server.request("GET", "/api/health", headers={"Origin": origin})
            assert status == 403 and json.loads(cross_origin)["error"]["code"] == "CROSS_ORIGIN_REJECTED"
        legal_headers = {"Origin": f"http://127.0.0.1:{server.port}", "Sec-Fetch-Site": "same-origin"}
        status, _, _ = server.request("GET", "/api/health", headers=legal_headers)
        assert status == 200
        status, _, _ = server.request(
            "POST", "/api/check/lexiang", body=b"{}",
            headers={**legal_headers, "Content-Type": "application/json"},
        )
        assert status == 200
        status, _, cross_site = server.request("GET", "/api/health", headers={"Sec-Fetch-Site": "cross-site"})
        assert status == 403 and json.loads(cross_site)["error"]["code"] == "CROSS_SITE_REQUEST_REJECTED"
        status, _, not_json = server.request("POST", "/api/runtime/migrate", body=b"{}", headers={"Content-Type": "text/plain"})
        assert status == 415 and json.loads(not_json)["error"]["code"] == "JSON_CONTENT_TYPE_REQUIRED"
        status, _, legal = server.request("POST", "/api/check/lexiang", headers={"Content-Length": "0"})
        assert status == 200
        assert (data / "users.json").is_file()
        assert not (data / "config").exists()
    finally:
        server.stop()


def test_qa_explicit_migration_remains_available_after_startup(tmp_path: Path) -> None:
    """存在有效 legacy 时，启动保留迁移决策，不提前创建目标。"""
    data = tmp_path / "migration-after-startup"
    legacy = tmp_path / "legacy" / "users.json"
    source = _legacy(legacy, tmp_path)
    server = RunningPanel(data, tmp_path, legacy=legacy)
    try:
        for endpoint in ("/api/health", "/api/users", "/api/overview") * 2:
            status, _, payload = server.request("GET", endpoint)
            assert status == 200, (endpoint, payload)
            assert not (data / "users.json").exists(), f"{endpoint} 提前提交了目标档案"
            status, _, health_bytes = server.request("GET", "/api/health")
            assert status == 200
            health = json.loads(health_bytes)["data"]
            assert health["migration_required"] is True
            assert not (data / "users.json").exists()
        status, _, migrated_bytes = server.request(
            "POST", "/api/runtime/migrate", body=b'{"confirm":true}',
            headers={"Content-Type": "application/json"},
        )
        migrated = json.loads(migrated_bytes)
        assert (tmp_path / "legacy" / "users.json").read_bytes() == source
        assert (status, migrated.get("error", {}).get("code")) == (200, None)
        assert migrated["data"]["migrated"] is True
        assert (data / "users.json").read_bytes() == source
    finally:
        server.stop()


def test_qa_unconfigured_vault_check_is_an_explicit_error(tmp_path: Path) -> None:
    server = RunningPanel(tmp_path / "empty-vault-data", tmp_path)
    try:
        status, _, payload = server.request(
            "POST", "/api/check/vault", body=b"{}", headers={"Content-Type": "application/json"},
        )
        assert status == 400
        assert json.loads(payload)["error"]["code"] == "PATH_REQUIRED"
    finally:
        server.stop()


def test_qa_ready_server_browser_called_after_real_bind(tmp_path: Path, monkeypatch) -> None:
    import run_panel
    from panel import main as panel_main

    data = tmp_path / "browser-test-data"
    monkeypatch.setenv("MTRP_PANEL_DATA_DIR", str(data))
    monkeypatch.setenv("MTRP_PANEL_LEGACY_PROFILE", str(data / "legacy" / "users.json"))
    monkeypatch.delenv("MTRP_PANEL_CONFIG_DIR", raising=False)
    panel_main.get_profile_store.cache_clear()
    panel_main._cached_settings.cache_clear()
    events: list[str] = []
    monkeypatch.setattr(run_panel, "_open_browser_when_ready", lambda url: events.append(url))
    config = run_panel.uvicorn.Config("panel.main:app", host="127.0.0.1", port=0, log_level="error")
    server = run_panel.ReadyServer(config, open_browser=True)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 12
        while not events and time.monotonic() < deadline:
            time.sleep(0.02)
        assert events and server.started
        address = server.servers[0].sockets[0].getsockname()
        assert events[0] == f"http://127.0.0.1:{address[1]}/"
        with urllib.request.urlopen(events[0] + "api/health", timeout=5) as response:
            assert response.status == 200
        assert (data / "users.json").is_file()
    finally:
        server.should_exit = True
        thread.join(timeout=8)
        panel_main.get_profile_store.cache_clear()
        panel_main._cached_settings.cache_clear()
    assert not thread.is_alive()


def _json_request(server: RunningPanel, method: str, endpoint: str, payload: object | None = None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    status, _, raw = server.request(method, endpoint, body=body, headers=headers)
    return status, json.loads(raw)


def _pending_review_fixture(tmp_path: Path):
    data = tmp_path / "review-pending-data"
    legacy = tmp_path / "legacy" / "users.json"
    source = _legacy(legacy, tmp_path)
    vault = tmp_path / "synthetic-vault"
    permanent = vault / "2-Permanent"
    permanent.mkdir(parents=True)
    for name in ("first", "second"):
        (permanent / f"{name}.md").write_text(
            f"---\ntitle: {name}\ntags: [mtr/trial]\n---\n正文\n", encoding="utf-8",
        )
    return data, legacy, source, vault


def test_qa_pending_all_review_reads_and_explicit_write_commits(tmp_path: Path) -> None:
    """待决扫描/读视图/dry-run零副作用；成功decide明确提交新档，不复制legacy。"""
    data, legacy, source, vault = _pending_review_fixture(tmp_path)
    server = RunningPanel(data, tmp_path, legacy=legacy)
    target = data / "users.json"
    try:
        status, listing = _json_request(server, "POST", "/api/review/scan", {"path": str(vault)})
        assert status == 200 and listing["data"]["total"] == 2
        first = listing["data"]["items"][0]
        relative = first["vault_relative_path"]
        endpoints = (
            "/api/review/listing",
            "/api/review/records",
            "/api/review/progress",
            "/api/review/progress?view=directory",
            "/api/review/item?" + urllib.parse.urlencode({"path": relative}),
        )
        for endpoint in endpoints * 2:
            status, payload = _json_request(server, "GET", endpoint)
            assert status == 200, (endpoint, payload)
            assert not target.exists(), endpoint
        status, preview = _json_request(server, "POST", "/api/review/export", {"dry_run": True})
        assert status == 200 and preview["data"]["preview"]["items_count"] == 0
        status, rejected = _json_request(server, "POST", "/api/review/export", {"dry_run": False})
        assert status == 409 and rejected["error"]["code"] == "MIGRATION_DECISION_REQUIRED"
        assert not target.exists() and not (data / "exports").exists()
        status, decided = _json_request(server, "POST", "/api/review/decide", {
            "path": relative, "content_sha256": first["sha256"], "action": "approved", "reason": "QA explicit commit",
        })
        assert status == 200 and decided["data"]["item"]["action"] == "approved"
        assert target.is_file()
        saved = ProfileStore(target)
        assert saved.get_user_review("default").items[relative].action == "approved"
        assert saved.get_user("default").preferences.vault_root == ""
        assert legacy.read_bytes() == source and target.read_bytes() != source
        status, health = _json_request(server, "GET", "/api/health")
        assert status == 200 and health["data"]["migration_required"] is False
    finally:
        server.stop()


@pytest.mark.parametrize("operation", ["position", "batch"])
def test_qa_pending_remaining_review_writes_commit_only_on_success(tmp_path: Path, operation: str) -> None:
    data, legacy, source, vault = _pending_review_fixture(tmp_path)
    server = RunningPanel(data, tmp_path, legacy=legacy)
    target = data / "users.json"
    try:
        status, listing = _json_request(server, "POST", "/api/review/scan", {"path": str(vault)})
        assert status == 200
        first, second = listing["data"]["items"]
        if operation == "position":
            payload = {"path": first["vault_relative_path"]}
        else:
            payload = {
                "action": "skipped", "items": [
                    {"path": item["vault_relative_path"], "content_sha256": item["sha256"]}
                    for item in (first, second)
                ],
            }
        status, response = _json_request(server, "POST", f"/api/review/{operation}", payload)
        assert status == 200, response
        assert target.is_file()
        state = ProfileStore(target).get_user_review("default")
        if operation == "position":
            assert state.last_position == first["vault_relative_path"]
        else:
            assert len(state.items) == 2 and response["data"]["summary"]["succeeded"] == 2
        assert legacy.read_bytes() == source and target.read_bytes() != source
    finally:
        server.stop()


def test_qa_pending_reset_undoes_explicit_decision_and_preserves_legacy(tmp_path: Path) -> None:
    data, legacy, source, vault = _pending_review_fixture(tmp_path)
    server = RunningPanel(data, tmp_path, legacy=legacy)
    target = data / "users.json"
    try:
        status, listing = _json_request(server, "POST", "/api/review/scan", {"path": str(vault)})
        assert status == 200
        item = listing["data"]["items"][0]
        relative = item["vault_relative_path"]
        status, response = _json_request(server, "POST", "/api/review/decide", {
            "path": relative, "content_sha256": item["sha256"], "action": "approved",
        })
        assert status == 200, response
        status, response = _json_request(server, "POST", "/api/review/reset", {"path": relative, "mode": "undo"})
        assert status == 200, response
        state = ProfileStore(target).get_user_review("default")
        assert not state.items and state.history[-1].op == "UNDO"
        assert legacy.read_bytes() == source and target.read_bytes() != source
    finally:
        server.stop()


def test_qa_pending_failed_review_write_does_not_commit(tmp_path: Path) -> None:
    data, legacy, source, vault = _pending_review_fixture(tmp_path)
    server = RunningPanel(data, tmp_path, legacy=legacy)
    target = data / "users.json"
    try:
        status, listing = _json_request(server, "POST", "/api/review/scan", {"path": str(vault)})
        assert status == 200
        item = listing["data"]["items"][0]
        status, response = _json_request(server, "POST", "/api/review/decide", {
            "path": item["vault_relative_path"], "content_sha256": "0" * 64, "action": "approved",
        })
        assert status == 409 and response["error"]["code"] == "CONTENT_CHANGED"
        assert not target.exists()
        status, response = _json_request(server, "POST", "/api/review/batch", {
            "action": "approved",
            "items": [{"path": item["vault_relative_path"], "content_sha256": "0" * 64}],
        })
        assert status == 200 and response["data"]["summary"]["failed"] == 1
        assert not target.exists()
        status, response = _json_request(server, "POST", "/api/review/reset", {
            "path": item["vault_relative_path"], "mode": "undo",
        })
        assert status == 404 and response["error"]["code"] == "REVIEW_ITEM_NOT_DECIDED"
        assert not target.exists() and legacy.read_bytes() == source
    finally:
        server.stop()


def test_qa_skip_and_concurrent_migration_never_overwrite_winner(tmp_path: Path) -> None:
    """真实HTTP共享提交锁：迁移/skip只有一方成功，响应不得伪造临时default。"""
    data = tmp_path / "skip-race-data"
    legacy = tmp_path / "legacy" / "users.json"
    source = _legacy(legacy, tmp_path)
    server = RunningPanel(data, tmp_path, legacy=legacy)
    target = data / "users.json"
    ready = threading.Barrier(3)
    outcomes: list[tuple[str, int, dict]] = []

    def commit(operation: str) -> None:
        payload = {"confirm": True} if operation == "migrate" else {"use_independent": True}
        ready.wait()
        status, response = _json_request(server, "POST", "/api/runtime/migrate", payload)
        outcomes.append((operation, status, response))

    try:
        workers = [threading.Thread(target=commit, args=(operation,)) for operation in ("migrate", "skip")]
        for worker in workers:
            worker.start()
        ready.wait()
        for worker in workers:
            worker.join(timeout=10)
            assert not worker.is_alive()
        assert len(outcomes) == 2
        successful = [item for item in outcomes if item[1] == 200]
        conflicts = [item for item in outcomes if item[1] == 409]
        assert len(successful) == len(conflicts) == 1, outcomes
        assert conflicts[0][2]["error"]["code"] == "MIGRATION_NOT_AVAILABLE"
        winner = successful[0][0]
        if winner == "migrate":
            assert successful[0][2]["data"]["migrated"] is True
            assert target.read_bytes() == source
        else:
            assert successful[0][2]["data"]["initialized"] is True
            assert target.read_bytes() != source
        assert legacy.read_bytes() == source
        assert ProfileStore(target).get_user("default").user_id == "default"
        assert not list(data.glob(".users-*.tmp"))
    finally:
        server.stop()


def test_qa_export_returns_actual_isolated_paths_and_preserves_legacy(tmp_path: Path) -> None:
    data, legacy, source, vault = _pending_review_fixture(tmp_path)
    server = RunningPanel(data, tmp_path, legacy=legacy)
    target = data / "users.json"
    try:
        status, listing = _json_request(server, "POST", "/api/review/scan", {"path": str(vault)})
        assert status == 200
        item = listing["data"]["items"][0]
        status, response = _json_request(server, "POST", "/api/review/decide", {
            "path": item["vault_relative_path"], "content_sha256": item["sha256"], "action": "approved",
        })
        assert status == 200, response
        status, response = _json_request(server, "POST", "/api/review/export", {"dry_run": False})
        assert status == 200, response
        exported = response["data"]
        assert exported["export_location"] == "当前数据目录/exports"
        for extension in ("json", "csv"):
            absolute = Path(exported["files_absolute"][extension])
            assert absolute.is_file() and absolute.parent == data / "exports"
            assert absolute.name == Path(exported["files"][extension]).name
        assert legacy.read_bytes() == source and target.read_bytes() != source
    finally:
        server.stop()
