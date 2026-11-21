# -*- coding: utf-8 -*-
"""L01 本地数据隔离、显式迁移与回环启动的独立回归测试。"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import re
import subprocess
import sys
from dataclasses import replace
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

PANEL_MODULE_DIR = Path(__file__).resolve().parents[1]
if str(PANEL_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(PANEL_MODULE_DIR))

from panel import runtime, review_api
from panel.runtime import RuntimePaths
from panel.settings import PanelError
from panel.user_profiles import ProfileStore, UserPreferences
from qa_support import PANEL_DIR, PANEL_VENV_PYTHON, USERS_JSON

import run_panel


def _paths(tmp_path: Path) -> RuntimePaths:
    data_dir = tmp_path / "isolated"
    return RuntimePaths(
        data_dir=data_dir,
        profile_path=data_dir / "users.json",
        legacy_profile_path=tmp_path / "legacy" / "users.json",
        explicit_data_dir=True,
        development=False,
    )


def _create_legacy(path: Path, tmp_path: Path) -> None:
    store = ProfileStore(path)
    store.ensure_default(UserPreferences(vault_root=str(tmp_path), git_local_path=str(tmp_path)))
    store.update_user_review(
        "default",
        lambda state: __import__("panel.review_store", fromlist=["decide"]).decide(
            state, "2-Permanent/旧笔记.md", "approved", "保留原文", "a" * 64,
            "2026-11-21T09:00:00+00:00",
        ),
    )


def test_parse_launcher_accepts_dynamic_port_data_dir_and_no_browser(tmp_path: Path) -> None:
    arguments = run_panel.parse_args(["--port", "0", "--data-dir", str(tmp_path), "--no-browser"])
    assert arguments.port == 0
    assert arguments.data_dir == str(tmp_path)
    assert arguments.no_browser is True


@pytest.mark.parametrize("port", ["-1", "65536"])
def test_parse_launcher_rejects_invalid_port(port: str) -> None:
    with pytest.raises(SystemExit) as caught:
        run_panel.parse_args(["--port", port])
    assert caught.value.code == 2


def test_parse_launcher_rejects_relative_data_dir() -> None:
    with pytest.raises(SystemExit) as caught:
        run_panel.parse_args(["--data-dir", "relative/private-data"])
    assert caught.value.code == 2


def test_source_development_keeps_legacy_profile_without_copy(tmp_path: Path) -> None:
    paths = runtime.resolve_runtime_paths(environ={}, platform_name="win32", packaged=False)
    assert paths.data_dir == runtime.application_directory() / "data"
    assert paths.profile_path == runtime.legacy_profile_path()
    assert paths.explicit_data_dir is False
    assert paths.development is True
    assert runtime.migration_status(paths)["migration_required"] is False


def test_explicit_data_dir_does_not_copy_or_read_legacy(tmp_path: Path) -> None:
    paths = runtime.resolve_runtime_paths(str(tmp_path / "new"), environ={}, packaged=True)
    assert paths.data_dir == (tmp_path / "new").resolve()
    assert paths.profile_path == paths.data_dir / "users.json"
    assert paths.explicit_data_dir is True
    assert paths.development is False
    assert not paths.profile_path.exists()
    assert runtime.resolve_config_dir(paths, environ={}) == runtime.application_directory() / "panel" / "default_config"


def test_packaged_per_user_data_directory(tmp_path: Path) -> None:
    windows = runtime.resolve_runtime_paths(
        environ={"LOCALAPPDATA": str(tmp_path)}, platform_name="win32", packaged=True,
    )
    linux = runtime.resolve_runtime_paths(
        environ={"XDG_DATA_HOME": str(tmp_path)}, platform_name="linux", packaged=True,
    )
    assert windows.data_dir == tmp_path / "MTRP-ReviewWorkbench"
    assert linux.data_dir == tmp_path / "MTRP-ReviewWorkbench"


def test_user_config_dir_precedes_safe_default(tmp_path: Path) -> None:
    paths = runtime.resolve_runtime_paths(str(tmp_path), environ={}, packaged=True)
    user_config = paths.data_dir / "config"
    user_config.mkdir(parents=True)
    (user_config / runtime.CONFIG_FILENAMES[0]).write_text("schema_version: '1'\n", encoding="utf-8")
    assert runtime.resolve_config_dir(paths, environ={}) == user_config


def _http_json(
    url: str, method: str = "GET", payload: object | None = None, headers: dict[str, str] | None = None,
) -> tuple[int, dict, dict[str, str]]:
    request_headers = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8")), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8")), dict(exc.headers)


def _launch_panel(
    data_dir: Path, legacy_profile: Path | None = None,
) -> tuple[subprocess.Popen[bytes], queue.Queue[str], threading.Thread]:
    environment = os.environ.copy()
    for key in (
        "MTRP_PANEL_DATA_DIR", "MTRP_PANEL_CONFIG_DIR", "MTRP_PANEL_PACKAGED",
        "MTRP_PANEL_LEGACY_PROFILE", "PYTHONPATH",
    ):
        environment.pop(key, None)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["MTRP_PANEL_LEGACY_PROFILE"] = str(
        legacy_profile if legacy_profile is not None else data_dir / "no-legacy" / "users.json"
    )
    process = subprocess.Popen(
        [str(PANEL_VENV_PYTHON), str(PANEL_DIR / "run_panel.py"), "--port", "0", "--data-dir", str(data_dir), "--no-browser"],
        cwd=str(PANEL_DIR), env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    lines: queue.Queue[str] = queue.Queue()

    def collect_output() -> None:
        assert process.stdout is not None
        for raw_line in process.stdout:
            lines.put(raw_line.decode("utf-8", "replace"))

    reader = threading.Thread(target=collect_output, daemon=True)
    reader.start()
    return process, lines, reader


def _real_panel(data_dir: Path, legacy_profile: Path | None = None):
    process, lines, reader = _launch_panel(data_dir, legacy_profile)
    output: list[str] = []
    port: int | None = None
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and port is None and process.poll() is None:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            continue
        output.append(line)
        match = re.search(r"本地面板已就绪：http://127\.0\.0\.1:(\d+)/", line)
        if match:
            port = int(match.group(1))
    assert port is not None, "启动入口未回报真实端口：" + "".join(output)

    class RunningPanel:
        base_url = f"http://127.0.0.1:{port}"

        def stop(self) -> None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            reader.join(timeout=2)

    return RunningPanel()


def test_safe_default_config_and_empty_vault_initialize(tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime-data"
    panel = _real_panel(data_dir)
    try:
        status, response, _headers = _http_json(panel.base_url + "/api/overview")
        assert status == 200
        data = response["data"]
        assert data["stores"]["T"]["vault_path"] == ""
        assert data["stores"]["T"]["status"] == "pending"
        assert data["configuration"]["store_count"] == 4
        _status, health, _headers = _http_json(panel.base_url + "/api/health")
        assert health["data"]["ready"] is True and health["data"]["local_only"] is True
        assert (data_dir / "users.json").is_file()
        assert not (data_dir / "config").exists()
    finally:
        panel.stop()


def test_migration_is_explicit_preserves_source_and_never_overwrites(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _create_legacy(paths.legacy_profile_path, tmp_path)
    source_before = paths.legacy_profile_path.read_bytes()
    assert runtime.migration_status(paths)["migration_required"] is True
    result = runtime.migrate_legacy_profile(paths)
    assert result["migrated"] is True
    assert paths.profile_path.read_bytes() == source_before
    assert paths.legacy_profile_path.read_bytes() == source_before
    migrated = ProfileStore(paths.profile_path)
    assert migrated.get_user_review("default").items["2-Permanent/旧笔记.md"].reason == "保留原文"
    assert runtime.migration_status(paths)["migration_required"] is False
    assert list(paths.data_dir.glob(".users-migration-*.tmp")) == []
    with pytest.raises(PanelError) as caught:
        runtime.migrate_legacy_profile(paths)
    assert caught.value.code == "MIGRATION_NOT_AVAILABLE"
    assert paths.profile_path.read_bytes() == source_before


def test_concurrent_migration_has_one_atomic_winner_and_preserves_source(tmp_path: Path) -> None:
    """两个线程发布同一完整临时档：只能一方成功，另一方明确收到 409。"""

    paths = _paths(tmp_path)
    _create_legacy(paths.legacy_profile_path, tmp_path)
    source_before = paths.legacy_profile_path.read_bytes()
    barrier = threading.Barrier(2)
    results: queue.Queue[dict[str, str | bool] | PanelError] = queue.Queue()

    def migrate() -> None:
        barrier.wait()
        try:
            results.put(runtime.migrate_legacy_profile(paths))
        except PanelError as exc:
            results.put(exc)

    workers = [threading.Thread(target=migrate) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()
    outcomes = [results.get(timeout=1), results.get(timeout=1)]
    assert sum(isinstance(item, dict) and item["migrated"] for item in outcomes) == 1
    conflicts = [item for item in outcomes if isinstance(item, PanelError)]
    assert len(conflicts) == 1
    assert conflicts[0].code == "MIGRATION_NOT_AVAILABLE" and conflicts[0].http_status == 409
    assert paths.profile_path.read_bytes() == source_before
    assert paths.legacy_profile_path.read_bytes() == source_before
    assert list(paths.data_dir.glob(".users-migration-*.tmp")) == []


def _migration_process(path_values: tuple[str, str, str], ready, start) -> None:
    """子进程执行真实 link 发布；屏障只协调起点，不 mock 系统调用。"""

    from panel.runtime import RuntimePaths, migrate_legacy_profile

    data_dir, profile, legacy = map(Path, path_values)
    paths = RuntimePaths(data_dir, profile, legacy, True, False)
    ready.put(True)
    start.get(timeout=10)
    try:
        migrate_legacy_profile(paths)
        ready.put("success")
    except PanelError as exc:
        ready.put(exc.code)


def test_concurrent_process_migration_has_one_winner(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    paths = _paths(tmp_path)
    _create_legacy(paths.legacy_profile_path, tmp_path)
    source_before = paths.legacy_profile_path.read_bytes()
    ready = context.Queue()
    start = context.Queue()
    values = (str(paths.data_dir), str(paths.profile_path), str(paths.legacy_profile_path))
    workers = [context.Process(target=_migration_process, args=(values, ready, start)) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        assert ready.get(timeout=15) is True
        assert ready.get(timeout=15) is True
        start.put(True)
        start.put(True)
        for worker in workers:
            worker.join(timeout=15)
            assert not worker.is_alive() and worker.exitcode == 0
        outcomes = sorted([ready.get(timeout=5), ready.get(timeout=5)])
        assert outcomes == ["MIGRATION_NOT_AVAILABLE", "success"]
        assert paths.profile_path.read_bytes() == source_before
        assert paths.legacy_profile_path.read_bytes() == source_before
        assert list(paths.data_dir.glob(".users-migration-*.tmp")) == []
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        ready.close()
        start.close()


@pytest.mark.parametrize("competitor", [b"", b'{"schema_version":"1","users":[]}\n'])
def test_link_publish_failure_never_removes_competitor_or_source(tmp_path: Path, competitor: bytes) -> None:
    paths = _paths(tmp_path)
    _create_legacy(paths.legacy_profile_path, tmp_path)
    source_before = paths.legacy_profile_path.read_bytes()
    original_link = runtime.os.link
    reached = threading.Event()

    def fail_link(_source: str, target: str) -> None:
        if Path(target) == paths.profile_path:
            paths.profile_path.write_bytes(competitor)
            reached.set()
            raise OSError("injected link failure")
        original_link(_source, target)

    runtime.os.link = fail_link
    try:
        with pytest.raises(PanelError) as caught:
            runtime.migrate_legacy_profile(paths)
    finally:
        runtime.os.link = original_link
    assert reached.is_set()
    assert caught.value.code == "MIGRATION_WRITE_FAILED"
    assert paths.profile_path.read_bytes() == competitor
    assert paths.legacy_profile_path.read_bytes() == source_before
    assert list(paths.data_dir.glob(".users-migration-*.tmp")) == []


def test_corrupt_legacy_is_rejected_without_target_or_source_change(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.legacy_profile_path.parent.mkdir(parents=True)
    paths.legacy_profile_path.write_text('{"schema_version":"1","users":[]}', encoding="utf-8")
    source_before = paths.legacy_profile_path.read_bytes()
    with pytest.raises(PanelError) as caught:
        runtime.migrate_legacy_profile(paths)
    assert caught.value.code == "PROFILE_STORE_INVALID"
    assert paths.legacy_profile_path.read_bytes() == source_before
    assert not paths.profile_path.exists()
    assert list(paths.data_dir.glob(".users-migration-*.tmp")) == []


def test_migration_endpoint_requires_confirmation(tmp_path: Path, monkeypatch) -> None:
    from panel import main

    paths = _paths(tmp_path)
    _create_legacy(paths.legacy_profile_path, tmp_path)
    monkeypatch.setattr(main, "resolve_runtime_paths", lambda: paths)
    monkeypatch.setattr(main, "migrate_legacy_profile", lambda selected: runtime.migrate_legacy_profile(selected))
    with pytest.raises(PanelError) as caught:
        main.migrate_runtime(main.MigrationRequest(confirm=False))
    assert caught.value.code == "MIGRATION_CONFIRMATION_REQUIRED"
    assert not paths.profile_path.exists()
    result = main.migrate_runtime(main.MigrationRequest(confirm=True))
    assert result["data"]["migrated"] is True


@pytest.mark.parametrize(
    "host",
    ["testserver", "example.com", "127.0.0.1.evil.example", "localhost.evil.example", "127.0.0.1@evil.example", "127.0.0.1:bad"],
)
def test_host_allowlist_rejects_rebinding(host: str) -> None:
    assert runtime.loopback_host_valid(host) is False
    with pytest.raises(PanelError) as caught:
        runtime.validate_web_request("GET", {"host": host})
    assert caught.value.code == "LOOPBACK_HOST_REQUIRED"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
def test_loopback_host_allowlist_accepts_explicit_local_addresses(host: str) -> None:
    assert runtime.loopback_host_valid(host) is True


@pytest.mark.parametrize(
    "origin",
    ["https://127.0.0.1:8765", "http://evil.example", "http://127.0.0.1:9999", "http://127.0.0.1:8765/path", "null"],
)
def test_cross_origin_is_rejected(origin: str) -> None:
    with pytest.raises(PanelError) as caught:
        runtime.validate_web_request("POST", {
            "host": "127.0.0.1:8765", "origin": origin, "content-type": "application/json", "content-length": "2",
        })
    assert caught.value.code == "CROSS_ORIGIN_REJECTED"


def test_empty_body_query_post_and_delete_remain_compatible() -> None:
    for method in ("POST", "DELETE"):
        runtime.validate_web_request(method, {"host": "127.0.0.1:8765", "content-length": "0"})


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_body_write_requires_json(content_type: str) -> None:
    with pytest.raises(PanelError) as caught:
        runtime.validate_web_request("POST", {
            "host": "127.0.0.1:8765", "content-type": content_type, "content-length": "3",
        })
    assert caught.value.code == "JSON_CONTENT_TYPE_REQUIRED"


def test_security_headers_present_on_rejected_request(tmp_path: Path) -> None:
    panel = _real_panel(tmp_path / "headers")
    try:
        status, response, headers = _http_json(panel.base_url + "/api/health", headers={"Host": "evil.example"})
        assert status == 403
        assert response["error"]["code"] == "LOOPBACK_HOST_REQUIRED"
        lowered = {key.lower(): value for key, value in headers.items()}
        assert lowered["x-frame-options"] == "DENY"
        assert lowered["cache-control"] == "no-store"
        assert lowered["x-content-type-options"] == "nosniff"
        status, response, _headers = _http_json(panel.base_url + "/api/check/lexiang?user_id=default", method="POST")
        assert status == 200, response
        status, response, _headers = _http_json(panel.base_url + "/", method="DELETE")
        assert status == 405 and response["error"]["code"] == "HTTP_REQUEST_REJECTED"
    finally:
        panel.stop()


def test_exports_follow_explicit_data_directory(tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(review_api, "resolve_runtime_paths", lambda: paths)
    from datetime import datetime, timezone

    # 调用同一参数传递路径，断言固定仓库导出目录没有被重新硬编码。
    output = paths.data_dir / "exports"
    json_path, csv_path = review_api._write_export_pair(
        {"schema_version": "1", "items": []}, [], "default", datetime.now(timezone.utc), output,
    )
    assert json_path.parent == output and csv_path.parent == output
    assert json_path.is_file() and csv_path.is_file()


def test_real_http_export_reports_absolute_paths_only_for_declared_files(tmp_path: Path) -> None:
    """显式 data-dir 下两个导出路径真实可读，但不能借导出接口读取任意文件。"""

    data_dir = tmp_path / "独立导出数据"
    vault = tmp_path / "合成 Vault"
    managed = vault / "2-Permanent"
    managed.mkdir(parents=True)
    note = managed / "导出路径.md"
    note.write_text("---\ntags: [mtr/trial]\n---\n正文\n", encoding="utf-8")
    private_file = tmp_path / "不得读取.txt"
    private_file.write_text("private fixture", encoding="utf-8")
    panel = _real_panel(data_dir)
    try:
        status, listing, _headers = _http_json(
            panel.base_url + "/api/review/scan", method="POST", payload={"path": str(vault)},
        )
        assert status == 200, listing
        item = listing["data"]["items"][0]
        status, decided, _headers = _http_json(
            panel.base_url + "/api/review/decide",
            method="POST",
            payload={
                "path": item["vault_relative_path"],
                "content_sha256": item["sha256"],
                "action": "approved",
                "reason": "独立数据根真实路径回归",
            },
        )
        assert status == 200, decided
        status, response, _headers = _http_json(
            panel.base_url + "/api/review/export", method="POST", payload={"include": "approved"},
        )
        assert status == 200, response
        exported = response["data"]
        assert set(exported) == {"exported_at", "files", "files_absolute", "export_location", "counts"}
        assert set(exported["files"]) == set(exported["files_absolute"]) == {"json", "csv"}
        assert exported["export_location"] == "当前数据目录/exports"
        for extension in ("json", "csv"):
            assert exported["files"][extension] == f"data/exports/{Path(exported['files'][extension]).name}"
            absolute = Path(exported["files_absolute"][extension])
            assert absolute.is_absolute() and absolute.is_file()
            assert absolute.parent == (data_dir / "exports").resolve()
            assert absolute.name == Path(exported["files"][extension]).name
        document = json.loads(Path(exported["files_absolute"]["json"]).read_text(encoding="utf-8"))
        assert document["items"][0]["vault_relative_path"] == "2-Permanent/导出路径.md"
        assert Path(exported["files_absolute"]["csv"]).read_bytes().startswith(b"\xef\xbb\xbf")
        for path, expected in (
            ("/api/review/export/" + urllib.parse.quote(str(private_file), safe=""), (404, "ROUTE_NOT_FOUND")),
            ("/api/review/export?path=" + urllib.parse.quote(str(private_file)), (405, "HTTP_REQUEST_REJECTED")),
            ("/data/users.json", (404, "ROUTE_NOT_FOUND")),
            ("/../data/users.json", (404, "ROUTE_NOT_FOUND")),
        ):
            status, rejected, _headers = _http_json(panel.base_url + path)
            assert (status, rejected["error"]["code"]) == expected, (path, rejected)
            assert "private fixture" not in json.dumps(rejected, ensure_ascii=False)
    finally:
        panel.stop()


def test_first_setup_frontend_and_migration_controls_are_text_safe() -> None:
    html = (PANEL_DIR / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="setup-notice"' in html
    assert "首次使用" in html
    assert "确认复制旧版档案" in html
    assert "/api/runtime/migrate" in html
    assert "innerHTML" not in html


def test_ready_server_uses_bound_port_and_browser_opt_out(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(run_panel, "_open_browser_when_ready", opened.append)
    for open_browser in (True, False):
        config = run_panel.uvicorn.Config("panel.main:app", host="127.0.0.1", port=0, log_level="error")
        server = run_panel.ReadyServer(config, open_browser=open_browser)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        address = server.servers[0].sockets[0].getsockname()
        status, health, _headers = _http_json(f"http://127.0.0.1:{address[1]}/api/health")
        assert status == 200 and health["data"]["ready"] is True
        server.should_exit = True
        thread.join(timeout=8)
        assert not thread.is_alive()
    assert len(opened) == 1
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", opened[0])


def test_real_http_migration_confirmation_and_source_preservation(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "http-isolated"
    paths = replace(_paths(tmp_path), data_dir=data_dir, profile_path=data_dir / "users.json")
    _create_legacy(paths.legacy_profile_path, tmp_path)
    source_before = paths.legacy_profile_path.read_bytes()
    monkeypatch.setattr(runtime, "legacy_profile_path", lambda: paths.legacy_profile_path)
    panel = _real_panel(paths.data_dir, paths.legacy_profile_path)
    try:
        status, response, _headers = _http_json(
            panel.base_url + "/api/runtime/migrate", method="POST", payload={"confirm": False},
        )
        assert status == 400 and response["error"]["code"] == "MIGRATION_CONFIRMATION_REQUIRED"
        for endpoint in ("/api/health", "/api/users", "/api/overview", "/api/review/records"):
            for _attempt in range(2):
                status, response, _headers = _http_json(panel.base_url + endpoint)
                assert status == 200, (endpoint, response)
                if endpoint == "/api/health":
                    assert response["data"]["migration_required"] is True
                if endpoint == "/api/users":
                    assert response["data"] == []
                assert not paths.profile_path.exists(), endpoint
        status, response, _headers = _http_json(
            panel.base_url + "/api/runtime/migrate", method="POST", payload={"confirm": True},
        )
        assert status == 200 and response["data"]["migrated"] is True
        assert paths.profile_path.read_bytes() == source_before
        assert paths.legacy_profile_path.read_bytes() == source_before
        status, response, _headers = _http_json(panel.base_url + "/api/health")
        assert status == 200 and response["data"]["migration_required"] is False
    finally:
        panel.stop()


def test_pending_review_read_only_requests_and_export_do_not_initialize(tmp_path: Path) -> None:
    data_dir = tmp_path / "review-pending"
    paths = replace(_paths(tmp_path), data_dir=data_dir, profile_path=data_dir / "users.json")
    _create_legacy(paths.legacy_profile_path, tmp_path)
    vault = tmp_path / "vault"
    (vault / "2-Permanent").mkdir(parents=True)
    (vault / "2-Permanent" / "待审.md").write_text("---\ntitle: 待审\n---\n正文\n", encoding="utf-8")
    panel = _real_panel(paths.data_dir, paths.legacy_profile_path)
    try:
        status, listing, _headers = _http_json(
            panel.base_url + "/api/review/scan", method="POST", payload={"path": str(vault)},
        )
        assert status == 200 and listing["data"]["total"] == 1, listing
        for endpoint in (
            "/api/review/records",
            "/api/review/progress",
            "/api/review/item?path=" + urllib.parse.quote("2-Permanent/待审.md"),
        ):
            status, response, _headers = _http_json(panel.base_url + endpoint)
            assert status == 200, (endpoint, response)
            assert not paths.profile_path.exists(), endpoint
        status, response, _headers = _http_json(
            panel.base_url + "/api/review/export", method="POST", payload={"dry_run": True},
        )
        assert status == 200 and response["data"]["preview"]["items_count"] == 0
        status, response, _headers = _http_json(
            panel.base_url + "/api/review/export", method="POST", payload={"dry_run": False},
        )
        assert status == 409 and response["error"]["code"] == "MIGRATION_DECISION_REQUIRED"
        assert not paths.profile_path.exists()
        assert not (paths.data_dir / "exports").exists()
    finally:
        panel.stop()


def test_real_http_skip_initializes_once_and_closes_migration(tmp_path: Path) -> None:
    data_dir = tmp_path / "http-skip"
    paths = replace(_paths(tmp_path), data_dir=data_dir, profile_path=data_dir / "users.json")
    _create_legacy(paths.legacy_profile_path, tmp_path)
    source_before = paths.legacy_profile_path.read_bytes()
    panel = _real_panel(paths.data_dir, paths.legacy_profile_path)
    try:
        status, response, _headers = _http_json(
            panel.base_url + "/api/runtime/migrate", method="POST", payload={"use_independent": True},
        )
        assert status == 200 and response["data"]["initialized"] is True
        assert paths.profile_path.is_file()
        assert ProfileStore(paths.profile_path).get_user("default").preferences.vault_root == ""
        status, response, _headers = _http_json(panel.base_url + "/api/health")
        assert response["data"]["migration_required"] is False
        status, response, _headers = _http_json(
            panel.base_url + "/api/runtime/migrate", method="POST", payload={"confirm": True},
        )
        assert status == 409 and response["error"]["code"] == "MIGRATION_NOT_AVAILABLE"
        assert paths.legacy_profile_path.read_bytes() == source_before
        assert paths.profile_path.read_bytes() != source_before
    finally:
        panel.stop()


def test_real_launcher_dynamic_port_isolated_data_and_loopback(tmp_path: Path) -> None:
    """不经 mock 启动 CLI，验证端口 0、安全模板、真实档案隔离和 HTTP。"""

    data_dir = tmp_path / "真实独立数据"
    source_before = hashlib.sha256(USERS_JSON.read_bytes()).hexdigest()
    panel = _real_panel(data_dir)
    try:
        status, health, _headers = _http_json(panel.base_url + "/api/health")
        assert status == 200 and health["data"]["ready"] is True
        status, overview, _headers = _http_json(panel.base_url + "/api/overview")
        assert status == 200
        assert overview["data"]["stores"]["T"]["vault_path"] == ""
        status, rejected, _headers = _http_json(panel.base_url + "/api/health", headers={"Host": "rebind.invalid"})
        assert status == 403 and rejected["error"]["code"] == "LOOPBACK_HOST_REQUIRED"
        assert (data_dir / "users.json").is_file()
        assert hashlib.sha256(USERS_JSON.read_bytes()).hexdigest() == source_before
    finally:
        panel.stop()
