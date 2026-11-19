# -*- coding: utf-8 -*-
"""connect-panel QA 独立验收测试（Round 1/2 通用回归集）。

执行方式（主仓根目录）::

    ./.venv/Scripts/python.exe -m pytest apps/connect-panel/tests -q

约定：
- fixture server 由 ``conftest.server`` 以子进程 uvicorn 启动，端口 9678–9681 探测选定。
- users.json 在套件期间被真实写入（多用户流必须走真实 API），teardown 恢复原始字节。
- 模块内测试依赖定义顺序（default 快照在 QA PATCH 之前捕获；不变性断言放最后）。
- GitHub 在线探测整个套件只发起真实调用一次（内部两次 GET 属于该端点的一次校验）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from qa_support import (
    AUDIT_DB,
    AUDIT_SHM,
    AUDIT_WAL,
    FIXTURE_VAULT,
    PANEL_DIR,
    REPO_ROOT,
    USERS_DIR,
    aggregation_digest,
    aggregation_paths,
    file_sha256,
    tree_hashes,
    unwrap_error,
    unwrap_ok,
    unwrap_ok_list,
)

# ---------------------------------------------------------------------------
# 预期常量（先于实现写死，不许"跑出来是啥就断言啥"）
# ---------------------------------------------------------------------------

STORE_TITLES = (
    "M 库 · 乐享知识库",
    "T 库 · Obsidian 临时视图",
    "R 库 · 低频沉淀视图",
    "P 库 · 原则与决策仓库",
)
ROLE_LABELS = ("所有者", "编辑者", "观察者")

# fixture vault 手工盘点（md 共 7 个；excluded.md 位于排除目录，不进入任何统计）。
FIXTURE_STATS = {
    "scanned_files": 6,
    "parsed_files": 5,
    "trial_notes": 1,
    "rarely_notes": 1,
    "unmanaged_notes": 2,
    "conflict_notes": 1,
    "skipped_files": 0,
    "skipped_directories": 0,
    "excluded_directories": 1,
    "error_count": 1,
    "limit_reached": False,
}
FIXTURE_VIEW_STATUS = "limited"  # invalid-yaml.md 产生 1 个读取级错误 → limited

AUDIT_EVENT_KEYS = {
    "event_seq", "run_seq", "recorded_at", "event_type", "step_id",
    "route_step_id", "store", "operation", "status", "reason_code",
    "resource_count", "evidence_count", "skipped_count", "tool_call_count",
    "duration_ms", "receipt_hash", "response_sha256",
}

_SEALED_REJECT_STATUSES = (400, 404, 422)

# 模块级共享状态：default 用户偏好快照（在 QA 任何 PATCH 之前捕获）。
_DEFAULT_BEFORE: dict[str, Any] = {}
# QA 创建的用户（定义顺序依赖：先执行创建用例）。
_QA_STATE: dict[str, Any] = {}
# 环境事实：验收窗口内 users.json 由工程师冒烟维护（v2 契约），
# 只断言default 存在，不断言具体冒烟用户 ID。
SMOKE_MARKER_USER = "u-c1bab11b3116"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _assert_vault_fixture_scan(data: dict[str, Any]) -> None:
    """对 fixture vault 的单次扫描结果做全量断言（多端点复用）。"""

    assert data["status"] == FIXTURE_VIEW_STATUS, data["status"]
    assert data["read_only"] is True
    assert data["follow_symlinks"] is False
    assert Path(data["path"]) == FIXTURE_VAULT, data["path"]
    assert data["parser_available"] is True
    statistics = data["statistics"]
    for key, expected in FIXTURE_STATS.items():
        assert statistics.get(key) == expected, (
            f"statistics[{key}] 预期 {expected}，实际 {statistics.get(key)}"
        )
    assert data["views"]["T"]["note_count"] == 1
    assert data["views"]["T"]["tag"] == "mtr/trial"
    assert data["views"]["R"]["note_count"] == 1
    assert data["views"]["R"]["tag"] == "mtr/rarely"
    assert data["views"]["R"]["reserved"] is True
    assert data["obsidian"]["kind"] == "probable", "fixture 无 .obsidian，应为 probable"
    issues = data["issues"]
    invalid = [item for item in issues if item.get("code") == "INVALID_YAML"]
    assert invalid, f"issues 应包含 INVALID_YAML：{issues}"
    assert invalid[0]["path"] == "2-Permanent/invalid-yaml.md"
    # 排除目录中的 excluded.md 不得出现在任何 issue/统计路径中。
    assert all("excluded.md" not in item.get("path", "") for item in issues)
    # 管理根与排除根来自 stage2 配置的缺省值。
    assert data["managed_roots"] == ["2-Permanent"]
    assert "2-Permanent/Resources/MTR-P-System" in data["excluded_roots"]


def _assert_no_server_internals(payload: Any) -> None:
    """封闭性：响应不得含堆栈与框架内部信息。"""

    import json

    raw = json.dumps(payload, ensure_ascii=False)
    assert "Traceback" not in raw
    assert "pydantic" not in raw.lower()
    assert "FastAPI" not in raw


# ---------------------------------------------------------------------------
# 1. 契约与统一包裹
# ---------------------------------------------------------------------------


class TestContractWrapper:
    def test_index_serves_html_with_security_headers(self, server) -> None:
        status, headers, body = server.request("GET", "/")
        assert status == 200
        lowered = {key.lower(): value for key, value in headers.items()}
        assert lowered.get("content-type", "").startswith("text/html")
        assert lowered.get("x-frame-options") == "DENY"
        assert lowered.get("x-content-type-options") == "nosniff"
        assert lowered.get("referrer-policy") == "no-referrer"
        assert "default-src 'self'" in lowered.get("content-security-policy", "")

    def test_index_contains_store_names_switcher_role_labels(self, server) -> None:
        _status, _headers, body = server.request("GET", "/")
        html = body.decode("utf-8")
        for title in STORE_TITLES:
            assert title in html, f"页面缺少四库标题：{title}"
        assert 'id="user-select"' in html, "页面缺少用户切换器"
        for label in ROLE_LABELS:
            assert label in html, f"页面缺少角色徽标词：{label}"

    def test_index_no_external_refs_and_no_emoji(self, server) -> None:
        _status, _headers, body = server.request("GET", "/")
        html = body.decode("utf-8")
        assert not re.search(r"<script[^>]+src=", html), "存在外链 script"
        assert "<link" not in html, "存在 link 外链"
        assert not re.search(r"""(?:src|href)\s*=\s*["']https?:""", html), "存在外链资源"
        emojis = [
            character
            for character in html
            if ord(character) >= 0x1F000 or 0x2600 <= ord(character) <= 0x27BF
        ]
        assert not emojis, f"页面存在 emoji：{emojis[:5]}"

    def test_unknown_route_returns_sealed_404(self, server) -> None:
        status, payload = server.request_json("GET", "/api/no-such-route")
        assert status == 404
        assert unwrap_error(payload)["code"] == "ROUTE_NOT_FOUND"

    def test_method_not_allowed_sealed(self, server) -> None:
        status, payload = server.request_json("DELETE", "/")
        assert status == 405
        assert unwrap_error(payload)["code"] == "HTTP_REQUEST_REJECTED"

    def test_unknown_body_field_sealed_422(self, server) -> None:
        status, payload = server.request_json(
            "POST", "/api/check/vault", payload={"user_id": "default", "foo": 1}
        )
        assert status == 422
        assert unwrap_error(payload)["code"] == "REQUEST_VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# 2. 多用户档案流（在定义顺序上先捕获 default 快照，再做 QA 写入）
# ---------------------------------------------------------------------------


class TestUserProfileLayer:
    def test_list_users_baseline_and_default_snapshot(self, server) -> None:
        _status, payload = server.request_json("GET", "/api/users")
        data = unwrap_ok_list(payload)
        identities = {user["user_id"]: user for user in data}
        assert "default" in identities, "default 用户必须存在"
        assert identities["default"]["role"] == "owner"
        if SMOKE_MARKER_USER in identities:
            assert identities[SMOKE_MARKER_USER]["role"] == "viewer", "冒烟用户必须是 viewer"
        for user in data:
            # v2 契约：列表项含完整 preferences，但绝不含任何认证字段。
            assert set(user) == {"user_id", "display_name", "role", "preferences"}, set(user)
        # 捕获 default 偏好总览（QA 任何写操作之前的基线）。
        _s2, payload2 = server.request_json("GET", "/api/overview")
        overview = unwrap_ok(payload2)
        assert overview["current_user"]["user_id"] == "default"
        _DEFAULT_BEFORE["vault_root"] = overview["stores"]["T"]["vault_path"]
        _DEFAULT_BEFORE["git_local_path"] = overview["stores"]["P"]["local"]["path"]
        _DEFAULT_BEFORE["auto_refresh"] = overview["current_user"]["preferences"]["auto_refresh_minutes"]
        _DEFAULT_BEFORE["repos"] = (
            overview["stores"]["P"]["online"]["owner"]
            + "/"
            + overview["stores"]["P"]["online"]["repo"]
        )
        assert "Obsidian Vault" in str(_DEFAULT_BEFORE.get("vault_root")), "default 真实 Vault 路径"

    def test_create_qa_editor_user(self, server) -> None:
        status, payload = server.request_json(
            "POST", "/api/users", payload={"display_name": "QA 验收用户", "role": "editor"}
        )
        assert status == 201
        data = unwrap_ok(payload)
        user = data["user"]
        assert user["role"] == "editor"
        assert re.fullmatch(r"u-[a-z0-9]{12}", user["user_id"]), user["user_id"]
        # 新用户复制 default 当前偏好。
        assert user["preferences"]["auto_refresh_minutes"] == _DEFAULT_BEFORE["auto_refresh"]
        _QA_STATE["user_id"] = user["user_id"]

    def test_create_user_invalid_role_sealed(self, server) -> None:
        status, payload = server.request_json(
            "POST", "/api/users", payload={"display_name": "QA 坏角色", "role": "admin"}
        )
        assert status == 400
        assert unwrap_error(payload)["code"] == "ROLE_INVALID"

    @pytest.mark.parametrize(
        ("minutes", "expect_status", "expect_code"),
        [
            (0, 200, None),
            (3, 400, "AUTO_REFRESH_INVALID"),
            (4, 400, "AUTO_REFRESH_INVALID"),
            (5, 200, None),
            (60, 200, None),
            (61, 422, "REQUEST_VALIDATION_FAILED"),
        ],
    )
    def test_auto_refresh_boundaries(self, server, minutes, expect_status, expect_code) -> None:
        endpoint = f"/api/users/{_QA_STATE['user_id']}/preferences"
        status, payload = server.request_json(
            "PATCH", endpoint, payload={"auto_refresh_minutes": minutes}
        )
        assert status == expect_status, f"auto_refresh={minutes} HTTP {status}：{payload}"
        if expect_code is None:
            data = unwrap_ok(payload)
            value = data["user"]["preferences"]["auto_refresh_minutes"]
            assert value == minutes, f"回读 auto_refresh 预期 {minutes}，实际 {value}"
        else:
            assert unwrap_error(payload)["code"] == expect_code

    def test_patch_vault_root_and_overview_effect(self, server) -> None:
        endpoint = f"/api/users/{_QA_STATE['user_id']}/preferences"
        status, payload = server.request_json(
            "PATCH", endpoint, payload={"vault_root": str(FIXTURE_VAULT)}
        )
        assert status == 200
        unwrap_ok(payload)
        status, payload = server.request_json(
            "GET", f"/api/overview?user_id={_QA_STATE['user_id']}"
        )
        overview = unwrap_ok(payload)["stores"]
        assert Path(overview["T"]["vault_path"]) == FIXTURE_VAULT

    def test_default_preferences_not_polluted(self, server) -> None:
        """双向验证：QA 用户的 PATCH 不得影响 default 的偏好。"""

        _status, payload = server.request_json("GET", "/api/overview")
        overview = unwrap_ok(payload)
        prefs = overview["current_user"]["preferences"]
        assert str(prefs["vault_root"]) == str(_DEFAULT_BEFORE["vault_root"]), (
            f"default vault_root 被污染：{prefs['vault_root']}"
        )
        assert prefs["git_local_path"] == _DEFAULT_BEFORE["git_local_path"]
        assert prefs["auto_refresh_minutes"] == _DEFAULT_BEFORE["auto_refresh"]
        assert (
            overview["stores"]["P"]["online"]["owner"]
            + "/"
            + overview["stores"]["P"]["online"]["repo"]
        ) == _DEFAULT_BEFORE["repos"]

    def test_patch_ghost_user_404(self, server) -> None:
        status, payload = server.request_json(
            "PATCH", "/api/users/u-ghost-404/preferences", payload={"auto_refresh_minutes": 5}
        )
        assert status == 404
        assert unwrap_error(payload)["code"] == "USER_NOT_FOUND"

    def test_user_id_uppercase_sealed(self, server) -> None:
        status, payload = server.request_json("GET", "/api/overview?user_id=Nope")
        assert status == 422, payload
        assert unwrap_error(payload)["code"] == "REQUEST_VALIDATION_FAILED"
        status, payload = server.request_json(
            "PATCH", "/api/users/Nope/preferences", payload={"auto_refresh_minutes": 5}
        )
        assert status in _SEALED_REJECT_STATUSES
        error = unwrap_error(payload)
        assert error["code"] in {"USER_ID_INVALID", "REQUEST_VALIDATION_FAILED", "ROUTE_NOT_FOUND"}

    def test_user_id_traversal_and_slash_sealed(self, server) -> None:
        status, payload = server.request_json("GET", "/api/overview?user_id=..%2Fetc")
        assert status == 422
        assert unwrap_error(payload)["code"] == "REQUEST_VALIDATION_FAILED"
        status, payload = server.request_json(
            "PATCH", "/api/users/bad/id/preferences", payload={"auto_refresh_minutes": 5}
        )
        assert status in _SEALED_REJECT_STATUSES
        unwrap_error(payload)


# ---------------------------------------------------------------------------
# 3. Vault 只读铁证与安全负路径
# ---------------------------------------------------------------------------


class TestVaultChecks:
    def test_vault_scan_readonly_proof_with_explicit_path(self, server) -> None:
        """显式 path 优先于 default 的真实偏好：fixture 扫描前中后三态 hash 铁证。"""

        before = tree_hashes(FIXTURE_VAULT)
        assert len(before) == 7, f"fixture 树应是 7 个文件，实际 {len(before)}"
        status, payload = server.request_json(
            "POST",
            "/api/check/vault",
            payload={"user_id": "default", "path": FIXTURE_VAULT.as_posix()},
        )
        assert status == 200
        data = unwrap_ok(payload)
        assert data["user_id"] == "default"
        _assert_vault_fixture_scan(data)
        after = tree_hashes(FIXTURE_VAULT)
        assert after == before, "扫描后 fixture 树字节发生变化（只读铁证失败）"

    def test_vault_scan_uses_qa_preferences(self, server) -> None:
        """不传 path 时取 QA 用户偏好（此前已 PATCH 到 fixture vault）。"""

        status, payload = server.request_json(
            "POST", "/api/check/vault", payload={"user_id": _QA_STATE["user_id"]}
        )
        assert status == 200
        data = unwrap_ok(payload)
        assert data["user_id"] == _QA_STATE["user_id"]
        _assert_vault_fixture_scan(data)

    def test_vault_path_backslash_forward_slash_equivalent(self, server) -> None:
        windows_form = str(FIXTURE_VAULT).replace("/", "\\")
        status, payload = server.request_json(
            "POST",
            "/api/check/vault",
            payload={"user_id": _QA_STATE["user_id"], "path": windows_form},
        )
        assert status == 200
        data = unwrap_ok(payload)
        assert Path(data["path"]) == FIXTURE_VAULT
        assert data["statistics"]["trial_notes"] == 1

    def test_vault_rejects_url(self, server) -> None:
        status, payload = server.request_json(
            "POST",
            "/api/check/vault",
            payload={"user_id": "default", "path": "http://127.0.0.1/secret"},
        )
        assert status == 400
        assert unwrap_error(payload)["code"] == "URL_PATH_REJECTED"

    def test_vault_rejects_relative_path(self, server) -> None:
        status, payload = server.request_json(
            "POST",
            "/api/check/vault",
            payload={"user_id": "default", "path": "apps/connect-panel"},
        )
        assert status == 400
        assert unwrap_error(payload)["code"] == "RELATIVE_PATH_REJECTED"

    def test_vault_rejects_unc(self, server) -> None:
        status, payload = server.request_json(
            "POST",
            "/api/check/vault",
            payload={"user_id": "default", "path": "\\\\127.0.0.1\\c$"},
        )
        assert status == 400
        assert unwrap_error(payload)["code"] == "NETWORK_PATH_REJECTED"

    def test_vault_traversal_probe_stays_closed(self, server) -> None:
        """C:/x/../x 式穿越试探：必须封闭响应且不泄漏服务器内部路径。"""

        status, payload = server.request_json(
            "POST",
            "/api/check/vault",
            payload={"user_id": "default", "path": "C:/x/../x"},
        )
        assert status in (200, *_SEALED_REJECT_STATUSES)
        if status == 200:
            data = unwrap_ok(payload)
            assert data["status"] == "error", "穿越目标不存在时应为封闭 error"
            assert data["error"]["code"] == "DIRECTORY_NOT_FOUND"
        else:
            unwrap_error(payload)
        assert "Traceback" not in str(payload)

    def test_vault_managed_roots_absolute_rejected(self, server) -> None:
        status, payload = server.request_json(
            "POST",
            "/api/check/vault",
            payload={
                "user_id": "default",
                "path": FIXTURE_VAULT.as_posix(),
                "managed_roots": ["E:/some/absolute"],
                "excluded_roots": [],
            },
        )
        assert status == 400
        assert unwrap_error(payload)["code"] == "ROOT_MUST_BE_RELATIVE"

    def test_vault_managed_roots_drive_relative_escape_rejected(self, server) -> None:
        """驱动器相对路径（C:Users 形态）必须按 400 ROOT_DRIVE_RELATIVE_REJECTED 封闭拒绝。

        Round 1 已证实缺陷形态：Path("C:Users") 是"驱动器相对"路径，
        旧版 _normalise_root 的 is_absolute() 拦不住，joinpath 后整棵扫描落到
        C:\\Users（实测 scanned_files=5000、issues 泄漏 C:Users/postgres 等）。
        Round 2 工程师修复：":" 一律拒绝，精确错误码 + 错误消息不回显攻击输入路径。
        """

        status, payload = server.request_json(
            "POST",
            "/api/check/vault",
            payload={
                "user_id": "default",
                "path": FIXTURE_VAULT.as_posix(),
                "managed_roots": ["C:Users"],
                "excluded_roots": [],
            },
        )
        statistics = (
            payload.get("data", {}).get("statistics") if isinstance(payload, dict) else None
        ) or {}
        leaked = statistics.get("scanned_files", 0)
        assert status == 400, (
            f"驱动器相对 managed root 拒绝应为 HTTP 400（实际 {status}），"
            f"若响应携带扫描统计则视为越权扫描残留：{statistics}"
        )
        assert leaked == 0, f"拒绝路径竟产生扫描统计——越权读取：{statistics}"
        error = unwrap_error(payload)
        assert error["code"] == "ROOT_DRIVE_RELATIVE_REJECTED", error["code"]
        # 错误消息只含字段名语义，不得回显攻击者输入的路径内容。
        assert "managed_roots" in error["message"], error["message"]
        assert "C:Users" not in error["message"], f"错误消息回显了攻击输入：{error['message']}"

    def test_vault_excluded_roots_drive_relative_rejected(self, server) -> None:
        """纵深复核同型攻击打 excluded_roots 字段也必须拦截（Round 2 新增）。"""

        status, payload = server.request_json(
            "POST",
            "/api/check/vault",
            payload={
                "user_id": "default",
                "path": FIXTURE_VAULT.as_posix(),
                "managed_roots": ["2-Permanent"],
                "excluded_roots": ["C:Users"],
            },
        )
        assert status == 400, f"excluded_roots 驱动器相对变体应 400（实际 {status}）"
        error = unwrap_error(payload)
        assert error["code"] == "ROOT_DRIVE_RELATIVE_REJECTED", error["code"]
        assert "excluded_roots" in error["message"], error["message"]
        assert "C:Users" not in error["message"], f"错误消息回显了攻击输入：{error['message']}"

    def test_vault_managed_roots_slash_lead_outside_vault(self, server) -> None:
        """第二层 containment 纵深："/Users" 过第一层不是绝对路径但解析后越出 Vault。

        joinpath("/Users") 在 Windows concrete Path 上生成盘根路径 E:\\Users，
        与请求的 Vault 根（...\\tests\\fixtures\\stage2\\vault）无子树关系，
        必须 400 ROOT_OUTSIDE_VAULT。
        """

        status, payload = server.request_json(
            "POST",
            "/api/check/vault",
            payload={
                "user_id": "default",
                "path": FIXTURE_VAULT.as_posix(),
                "managed_roots": ["/Users"],
                "excluded_roots": [],
            },
        )
        assert status == 400, f"越出 Vault 的 managed root 应 400（实际 {status}）：{payload}"
        error = unwrap_error(payload)
        assert error["code"] == "ROOT_OUTSIDE_VAULT", error["code"]
        assert "managed_roots" in error["message"], error["message"]


# ---------------------------------------------------------------------------
# 4. git-local 只读
# ---------------------------------------------------------------------------


class TestGitLocal:
    def test_repo_root_status_ok_no_lock(self, server) -> None:
        lock = REPO_ROOT / ".git" / "index.lock"
        status, payload = server.request_json(
            "POST", "/api/check/git-local", payload={"path": REPO_ROOT.as_posix()}
        )
        assert status == 200
        data = unwrap_ok(payload)
        assert data["status"] == "ok"
        assert data["branch"] == "main", f"实际分支：{data['branch']}"
        assert re.fullmatch(r"[0-9a-f]{40}", data["head_sha"]), data["head_sha"]
        assert isinstance(data["dirty_file_count"], int) and data["dirty_file_count"] >= 1
        assert data["optional_locks_disabled"] is True
        assert data["read_only"] is True
        # SSH scp 风格 origin 脱敏：git@github.com: 前缀被去除。
        assert data["remote_url"] == "github.com:13Asix21/MTR-P-hub.git", data["remote_url"]
        assert data["remote_url_redacted"] is True
        assert not lock.exists(), "git-local 调用后出现 .git/index.lock"

    def test_non_git_directory_sealed_error(self, server) -> None:
        status, payload = server.request_json(
            "POST",
            "/api/check/git-local",
            payload={"path": (REPO_ROOT / "tests" / "fixtures").as_posix()},
        )
        assert status == 200
        data = unwrap_ok(payload)
        assert data["status"] == "error"
        assert data["error"]["code"] == "GIT_METADATA_NOT_FOUND"


# ---------------------------------------------------------------------------
# 5. 审计库只读
# ---------------------------------------------------------------------------


class TestAuditStore:
    def test_recent_limit_5_whitelist_columns(self, server) -> None:
        status, payload = server.request_json("GET", "/api/audit/recent?limit=5")
        assert status == 200
        data = unwrap_ok(payload)
        events = data["events"]
        assert data["read_only"] is True
        assert data["limit"] == 5
        assert data["count"] == len(events) == 5, f"库内当前事件数不足或超限：{len(events)}"
        sequences = [event["event_seq"] for event in events]
        assert sequences == sorted(sequences, reverse=True), "event_seq 未按倒序返回"
        for event in events:
            assert set(event) == AUDIT_EVENT_KEYS, f"审计字段白名单不匹配：{set(event) ^ AUDIT_EVENT_KEYS}"
        assert all("payload_json" not in event for event in events), "不得返回原始载荷"

    @pytest.mark.parametrize("limit", [0, -1, 101])
    def test_recent_invalid_limit_sealed(self, server, limit) -> None:
        status, payload = server.request_json("GET", f"/api/audit/recent?limit={limit}")
        assert status == 422, f"limit={limit} 应 422，实际 {status}"
        assert unwrap_error(payload)["code"] == "REQUEST_VALIDATION_FAILED"

    def test_audit_files_invariant_across_suite(self, server) -> None:
        """套件级只读铁证：主库/旁路文件字节与基线完全一致。"""

        baseline = server.baseline
        assert file_sha256(AUDIT_DB) == baseline["audit_main_sha"], "审计主库被写入"
        assert file_sha256(AUDIT_WAL) == baseline["audit_wal_sha"], "WAL 内容发生变化"
        assert AUDIT_WAL.stat().st_size == baseline["audit_wal_size"], "WAL 大小增长"
        assert file_sha256(AUDIT_SHM) == baseline["audit_shm_sha"], "SHM 被写入"


# ---------------------------------------------------------------------------
# 6. M 库 lexiang
# ---------------------------------------------------------------------------


class TestLexiang:
    def test_lexiang_config_valid_but_limited(self, server) -> None:
        # v2 契约：body 可传 user_id（query 同名参数也兼容，二选一实测 body 形式）。
        status, payload = server.request_json(
            "POST", "/api/check/lexiang", payload={"user_id": _QA_STATE["user_id"]}
        )
        assert status == 200
        data = unwrap_ok(payload)
        assert data["user_id"] == _QA_STATE["user_id"]
        assert data["status"] == "limited", "配置有效也为机器级 limited"
        assert data["realtime_mcp_available"] is False
        assert data["config_valid"] is True
        assert data["issues"] == []
        assert data["mode"] == "workbuddy-manifest"
        assert data["read_only"] is True
        for field in ("team_id", "space_id", "root_entry_id"):
            entry = data["identifiers"][field]
            assert entry["configured"] is True, field
            assert entry["valid"] is True, field
            assert entry["display"].endswith("…" + entry["display"][-4:]), field
        assert data["audit"]["available"] is True
        assert "M" not in data["audit"]["latest_by_store"], "当前库内无 M 事件"
        assert data["latest_m_evidence"] is None

    def test_lexiang_query_form_still_accepted(self, server) -> None:
        status, payload = server.request_json(
            "POST", f"/api/check/lexiang?user_id={_QA_STATE['user_id']}"
        )
        assert status == 200
        data = unwrap_ok(payload)
        assert data["user_id"] == _QA_STATE["user_id"]
        assert data["status"] == "limited", data["status"]


# ---------------------------------------------------------------------------
# 7. P 在线（整套 QA 仅此一次真实在线校验）
# ---------------------------------------------------------------------------


class TestGithubOnline:
    def test_single_real_probe(self, server) -> None:
        status, payload = server.request_json(
            "POST", "/api/check/github", payload={"user_id": "default", "owner": "13Asix21", "repo": "MTR-P"}
        )
        assert status == 200, f"在线探测 HTTP 层失败：{payload}"
        data = unwrap_ok(payload)
        assert data["credential_used"] is False
        assert data["user_id"] == "default"
        requests_meta = data["requests"]
        if data["status"] == "ok":
            assert data["default_branch"] == "main", data["default_branch"]
            assert re.fullmatch(r"[0-9a-fA-F]{40}", data["latest_commit_sha"])
            assert isinstance(data["total_latency_ms"], (int, float))
            assert data["total_latency_ms"] > 0
            assert len(requests_meta) == 2
            for meta in requests_meta:
                assert isinstance(meta["latency_ms"], (int, float)) and meta["latency_ms"] >= 0
        else:
            # 网络故障属环境限制，不算面板失败；但必须有封闭错误证据。
            error = data["error"]
            assert error["code"] in {
                "GITHUB_NETWORK_UNAVAILABLE", "GITHUB_TIMEOUT", "GITHUB_RATE_LIMITED",
                "GITHUB_REPOSITORY_NOT_FOUND", "GITHUB_HTTP_ERROR", "GITHUB_SERVICE_UNAVAILABLE",
            }
            assert data["connectivity"] == "在线探测失败"
            pytest.skip(f"GitHub 在线探测受环境限制（{error['code']}），已记录封闭证据")


# ---------------------------------------------------------------------------
# 8. 套件级不变性（放在最后执行）
# ---------------------------------------------------------------------------


class TestSuiteInvariance:
    def test_main_repo_aggregation_unchanged(self, server) -> None:
        paths = aggregation_paths()
        digest = aggregation_digest(paths)
        assert digest == server.baseline["aggregation_digest"], (
            f"主仓聚合内容在验收过程中发生变化：文件数 {len(paths)}"
        )

    def test_users_json_restored_by_teardown_documented(self, server) -> None:
        # 套件运行期 users.json 合法地被面板写入（原始字节的恢复交给 conftest teardown）。
        # 这里确认 default 与 QA 用户在列表中（不删除原则）。
        status, payload = server.request_json("GET", "/api/users")
        data = unwrap_ok_list(payload)
        identities = {user["user_id"] for user in data}
        assert "default" in identities
        assert _QA_STATE["user_id"] in identities
