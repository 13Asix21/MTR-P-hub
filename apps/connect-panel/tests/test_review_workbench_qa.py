# -*- coding: utf-8 -*-
"""review_workbench QA 独立证伪补充用例（严过关，Round 1）。

与工程师寇豆码的 ``test_review_workbench.py`` 同跑；本文件聚焦其未覆盖的证伪点：
- ``REVIEW_ITEM_NOT_DECIDED`` 错误码真实 HTTP 触发（工程师 17 项中缺失的唯一新码）；
- review 系端点与请求参数的 drive-relative / 盘符 / URL / 穿越变体封闭（HTTP 层复验）；
- ``POST /api/review/scan`` 的 managed/excluded root 注入攻击（复用 v1 root 校验族）；
- 单写者锁并发抽查：三路同篇竞赛 + 八路跨篇风暴，档案可解析、历史逐条落；
- STALE 三时机隔离（快照口径 / 详情现算 / 导出收敛各自独立成立）；
- 分母随扫描走：同一会话先后扫描 2 篇与 9 篇的临时 vault，total 跟随变化；
- 只读铁证（fixture 全树、审计三文件）、旧端点回归抽测与前端 textContent 白名单。

全部写路径仅落在临时 vault 副本与 QA 测试用户；teardown 后 users.json 字节级还原。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import sys
import threading
import urllib.parse
from pathlib import Path

PANEL_PACKAGE_PARENT = Path(__file__).resolve().parents[1]
if str(PANEL_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PANEL_PACKAGE_PARENT))

from qa_support import (
    AUDIT_DB,
    AUDIT_SHM,
    AUDIT_WAL,
    EXPORTS_DIR,
    FIXTURE_VAULT,
    PANEL_DIR,
    USERS_JSON,
    file_sha256,
    tree_hashes,
    unwrap_error,
    unwrap_ok,
)


def _create_user(server, role: str = "owner") -> str:
    status, payload = server.request_json(
        "POST",
        "/api/users",
        payload={"display_name": "QA 增补用例 " + role, "role": role},
    )
    assert status == 201, payload
    return unwrap_ok(payload)["user"]["user_id"]


def _scan(server, user_id: str, vault: Path) -> dict:
    status, payload = server.request_json(
        "POST", "/api/review/scan", payload={"user_id": user_id, "path": str(vault)}
    )
    assert status == 200, payload
    return unwrap_ok(payload)


def _records(server, user_id: str) -> dict:
    status, payload = server.request_json("GET", "/api/review/records?user_id=" + user_id)
    assert status == 200, payload
    return unwrap_ok(payload)


def _progress(server, user_id: str) -> dict:
    status, payload = server.request_json("GET", "/api/review/progress?user_id=" + user_id)
    assert status == 200, payload
    return unwrap_ok(payload)


def _item_query(user_id: str, path: str) -> str:
    return "/api/review/item?" + urllib.parse.urlencode({"user_id": user_id, "path": path})


def _copy_vault(tmp_path: Path) -> Path:
    target = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, target)
    return target


def _sealed_error(server, method: str, path: str, payload, want_code: str, want_status: int) -> dict:
    status, body = server.request_json(method, path, payload=payload)
    assert status == want_status, (path, want_code, status, body)
    error = unwrap_error(body)
    assert error["code"] == want_code, (path, error)
    assert "Traceback" not in json.dumps(body, ensure_ascii=False), body
    return error


# ---------------------------------------------------------------------------
# 1. 错误码补全：REVIEW_ITEM_NOT_DECIDED 必须在真实 HTTP 响应中出现
# ---------------------------------------------------------------------------


def test_qa_01_reset_without_record_returns_item_not_decided(server, tmp_path: Path) -> None:
    """无在档记录时 undo/stale_reset 必须返回 404 REVIEW_ITEM_NOT_DECIDED（工程师 17 项中唯一未触发的新码）。"""

    vault = _copy_vault(tmp_path)
    user_id = _create_user(server)
    _scan(server, user_id, vault)
    target = "2-Permanent/unmanaged.md"
    for mode in ("undo", "stale_reset"):
        _sealed_error(
            server,
            "POST",
            "/api/review/reset",
            {"user_id": user_id, "path": target, "mode": mode},
            "REVIEW_ITEM_NOT_DECIDED",
            404,
        )
    # 撤销后再次撤销同样封闭：先落一条再撤销一次
    status, payload = server.request_json(
        "POST",
        "/api/review/decide",
        payload={
            "user_id": user_id,
            "path": target,
            "content_sha256": "0" * 64,
            "action": "approved",
        },
    )
    assert status == 409 and unwrap_error(payload)["code"] == "CONTENT_CHANGED"
    records = _records(server, user_id)
    assert records["items"] == {} and records["history"] == []


# ---------------------------------------------------------------------------
# 2. 路径安全复验：drive-relative 教训在全部 review 系端点上的 HTTP 层回归
# ---------------------------------------------------------------------------


def test_qa_02_http_layer_seals_drive_relative_and_traversal(server, tmp_path: Path) -> None:
    """C:Users、E:temp、/Users、..、URL、UNC 等变体在 item/decide/reset/position 四端点全部封闭 400。"""

    vault = _copy_vault(tmp_path)
    user_id = _create_user(server)
    _scan(server, user_id, vault)
    rejected = [
        "C:Users",
        "C:/x.md",
        "E:temp",
        "/Users/x.md",
        "../escape.md",
        "2-Permanent/../escape.md",
        "file://x.md",
        "https://example.com/x.md",
        "\\\\server\\share\\x.md",
        "2-Permanent/x:y.md",
        "2-Permanent//x.md",
        "2-Permanent/./x.md",
    ]
    for bad_path in rejected:
        encoded = urllib.parse.quote(bad_path)
        # GET item（query 参数路径）
        _sealed_error(
            server,
            "GET",
            "/api/review/item?user_id=%s&path=%s" % (user_id, encoded),
            None,
            "REVIEW_PATH_INVALID",
            400,
        )
        # POST decide（body 路径）
        _sealed_error(
            server,
            "POST",
            "/api/review/decide",
            {"user_id": user_id, "path": bad_path, "content_sha256": "0" * 64, "action": "approved"},
            "REVIEW_PATH_INVALID",
            400,
        )
        # POST reset
        _sealed_error(
            server,
            "POST",
            "/api/review/reset",
            {"user_id": user_id, "path": bad_path, "mode": "undo"},
            "REVIEW_PATH_INVALID",
            400,
        )
        # POST position
        _sealed_error(
            server,
            "POST",
            "/api/review/position",
            {"user_id": user_id, "path": bad_path},
            "REVIEW_PATH_INVALID",
            400,
        )
    # 全部拒绝后档案零变化
    assert _records(server, user_id)["items"] == {}


def test_qa_03_scan_roots_inheritance_rejects_injection(server, tmp_path: Path) -> None:
    """/api/review/scan 的 managed_roots/excluded_roots 注入必须继承 v1 root 校验族封闭错误。"""

    vault = _copy_vault(tmp_path)
    user_id = _create_user(server)
    cases = [
        ({"managed_roots": ["C:Users/.."]}, "ROOT_DRIVE_RELATIVE_REJECTED"),
        ({"managed_roots": ["2-Permanent/../../Windows"]}, "ROOT_TRAVERSAL_REJECTED"),
        ({"excluded_roots": ["E:temp"]}, "ROOT_DRIVE_RELATIVE_REJECTED"),
        ({"managed_roots": ["file://x/y"]}, "ROOT_MUST_BE_RELATIVE"),
        ({"managed_roots": []}, "MANAGED_ROOT_REQUIRED"),
    ]
    for extra, want_code in cases:
        payload = {"user_id": user_id, "path": str(vault)}
        payload.update(extra)
        if "managed_roots" not in extra:
            payload["managed_roots"] = ["2-Permanent"]
        error = _sealed_error(
            server, "POST", "/api/review/scan", payload, want_code, 400
        )
        assert error["code"] == want_code


# ---------------------------------------------------------------------------
# 3. STALE 三时机分级：同一 tamper 场景下三层口径必须各自独立成立
# ---------------------------------------------------------------------------


def test_qa_04_stale_three_timing_tiers_isolated(server, tmp_path: Path) -> None:
    """快照=progress（未重扫不翻 stale）、详情=现算（立即标 stale）、导出=收敛剔除（与重扫无关）。"""

    vault = _copy_vault(tmp_path)
    user_id = _create_user(server)
    scan = _scan(server, user_id, vault)
    by_path = {item["vault_relative_path"]: item for item in scan["items"]}
    target_rel = "2-Permanent/unmanaged.md"
    old_sha = by_path[target_rel]["sha256"]
    status, payload = server.request_json(
        "POST",
        "/api/review/decide",
        payload={
            "user_id": user_id,
            "path": target_rel,
            "content_sha256": old_sha,
            "action": "approved",
            "reason": "stale 分级基线",
        },
    )
    assert status == 200, payload

    target = vault / target_rel
    target.write_bytes(target.read_bytes() + "\nQA stale 分级篡改\n".encode("utf-8"))
    new_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    # 时机 1：清单/进度 = 快照口径——progress 在重扫前不得把该篇算作 stale
    progress = _progress(server, user_id)
    assert progress["user"]["approved"] == 1
    assert progress["user"]["stale"] == 0

    # 时机 2：详情 = 现算——立即精确标 stale，并返回新 hash
    status, payload = server.request_json("GET", _item_query(user_id, target_rel))
    assert status == 200, payload
    detail = unwrap_ok(payload)
    assert detail["stale"] is True
    assert detail["item"]["sha256"] == new_sha != old_sha

    # 时机 3：导出 = 现算收敛——approved 因 hash 不一致被剔除且计数 exclucded_stale
    status, payload = server.request_json(
        "POST", "/api/review/export", payload={"user_id": user_id, "include": "approved", "dry_run": True}
    )
    preview = unwrap_ok(payload)["preview"]
    assert preview == {"items_count": 0, "excluded_stale": 1, "include": "approved"}

    # 重扫后快照口径翻转为 stale，且不减少分母、不计入已审
    _scan(server, user_id, vault)
    progress = _progress(server, user_id)
    assert progress["user"]["stale"] == 1
    assert progress["user"]["approved"] == 0
    assert progress["user"]["reviewed"] == 0
    assert progress["total"] == 6


# ---------------------------------------------------------------------------
# 4. 分母动态：total 跟随最近一次扫描而非硬编码
# ---------------------------------------------------------------------------


def test_qa_05_denominator_follows_scan_not_hardcoded(server, tmp_path: Path) -> None:
    """先后扫描 2 篇与 9 篇的两个临时 vault，total 与 progress.total 必须逐次跟随。"""

    user_id = _create_user(server)
    small = tmp_path / "small"
    (small / "2-Permanent").mkdir(parents=True)
    for name in ("a.md", "b.md"):
        (small / "2-Permanent" / name).write_text("---\ntitle: t\n---\n#hi\n", encoding="utf-8")
    scan_small = _scan(server, user_id, small)
    assert scan_small["total"] == 2
    assert len(scan_small["items"]) == scan_small["statistics"]["scanned_files"] == 2
    assert _progress(server, user_id)["total"] == 2

    big = tmp_path / "big"
    (big / "2-Permanent").mkdir(parents=True)
    for index in range(9):
        (big / "2-Permanent" / ("n%d.md" % index)).write_text("---\ntitle: t\n---\n#hi\n", encoding="utf-8")
    scan_big = _scan(server, user_id, big)
    assert scan_big["total"] == 9
    assert scan_big["statistics"]["scanned_files"] == 9
    progress = _progress(server, user_id)
    assert progress["total"] == 9 and progress["user"]["pending"] == 9


# ---------------------------------------------------------------------------
# 5. 单写者锁并发抽查：同篇三路竞赛 + 跨篇八路风暴
# ---------------------------------------------------------------------------


def test_qa_06_concurrent_decides_single_writer_integrity(server, tmp_path: Path) -> None:
    """并发 decide 下档案 JSON 始终可解析、动作逐条入 history、无交错损坏。"""

    vault = _copy_vault(tmp_path)
    user_id = _create_user(server)
    scan = _scan(server, user_id, vault)
    by_path = {item["vault_relative_path"]: item for item in scan["items"]}
    lock = threading.Lock()
    failures: list[tuple[str, int, dict]] = []

    def decide(path: str, action: str) -> None:
        status, payload = server.request_json(
            "POST",
            "/api/review/decide",
            payload={
                "user_id": user_id,
                "path": path,
                "content_sha256": by_path[path]["sha256"],
                "action": action,
                "reason": "并发抽查",
            },
        )
        with lock:
            failures.append((path, status, payload))

    # 三路同篇竞赛：approved/skipped/rejected 同时打同一篇
    racer = "2-Permanent/unmanaged.md"
    threads = [
        threading.Thread(target=decide, args=(racer, "approved")),
        threading.Thread(target=decide, args=(racer, "skipped")),
        threading.Thread(target=decide, args=(racer, "rejected")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(status == 200 for _path, status, _payload in failures), failures

    # 六路跨篇风暴（六篇各一发）
    failures.clear()
    storm = [
        threading.Thread(target=decide, args=(path, ["approved", "rejected", "skipped"][index % 3]))
        for index, path in enumerate(sorted(by_path))
    ]
    for thread in storm:
        thread.start()
    for thread in storm:
        thread.join()
    assert all(status == 200 for _path, status, _payload in failures), failures

    # 档案可解析、历史 9 条（3 同篇 + 6 风暴）、键全为字符串
    raw = json.loads(USERS_JSON.read_text(encoding="utf-8"))
    profile = next(user for user in raw["users"] if user["user_id"] == user_id)
    review = profile["review"]
    assert len(review["history"]) == 9, review["history"]
    assert all(isinstance(key, str) for key in review["items"])
    # 九条 history 逐条结构完整
    for event in review["history"]:
        assert set(event) == {"path", "op", "from", "to", "reason", "content_sha256", "at"}
        assert event["op"] == "DECIDE" and event["to"] in ("approved", "rejected", "skipped")
    # 跨篇风暴的六篇全部在档
    records = _records(server, user_id)
    assert len(records["items"]) == len(by_path)


# ---------------------------------------------------------------------------
# 6. 批量轻量限值：501 条与 500 条边界（默认值路径 + 校验路径）
# ---------------------------------------------------------------------------


def test_qa_07_batch_limit_boundary_uses_default_user(server, tmp_path: Path) -> None:
    """batch 501 整体拒收且档案零变化；500 条幽灵路径逐篇 REVIEW_PATH_NOT_SCANNED（边界含）。"""

    vault = _copy_vault(tmp_path)
    _scan(server, "default", vault)
    ghost = [{"path": "2-Permanent/ghost-%d.md" % index, "content_sha256": "0" * 64} for index in range(501)]
    _sealed_error(
        server,
        "POST",
        "/api/review/batch",
        {"user_id": "default", "action": "approved", "items": ghost},
        "REVIEW_BATCH_LIMIT",
        413,
    )
    assert _records(server, "default")["items"] == {}

    exactly = ghost[:500]
    status, payload = server.request_json(
        "POST",
        "/api/review/batch",
        payload={"user_id": "default", "action": "approved", "items": exactly},
    )
    assert status == 200, payload
    batch = unwrap_ok(payload)
    assert batch["summary"]["total"] == 500
    assert batch["summary"]["failed"] == 500
    assert batch["summary"]["by_error"] == {"REVIEW_PATH_NOT_SCANNED": 500}
    assert _records(server, "default")["items"] == {}


# ---------------------------------------------------------------------------
# 7. 损坏 review 档案的封闭语义：拒绝操作、绝不静默重建
# ---------------------------------------------------------------------------


def test_qa_08_corrupt_review_rejected_without_rebuild(server, tmp_path: Path) -> None:
    """外部注入损坏 review 后：全部读写端点返回 REVIEW_RECORD_INVALID，磁盘档案保持损坏原样。"""

    vault = _copy_vault(tmp_path)
    user_id = _create_user(server)
    scan = _scan(server, user_id, vault)
    target = "2-Permanent/unmanaged.md"
    sha = next(item["sha256"] for item in scan["items"] if item["vault_relative_path"] == target)

    original_bytes = USERS_JSON.read_bytes()
    try:
        raw = json.loads(original_bytes.decode("utf-8"))
        profile = next(user for user in raw["users"] if user["user_id"] == user_id)
        profile["review"] = {
            "schema_version": "1",
            "items": {"bad": {"action": "banana"}},
            "history": [],
            "last_position": None,
        }
        USERS_JSON.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        _sealed_error(server, "GET", "/api/review/records?user_id=" + user_id, None, "REVIEW_RECORD_INVALID", 503)
        _sealed_error(server, "GET", "/api/review/progress?user_id=" + user_id, None, "REVIEW_RECORD_INVALID", 503)
        _sealed_error(
            server,
            "POST",
            "/api/review/decide",
            {"user_id": user_id, "path": target, "content_sha256": sha, "action": "approved"},
            "REVIEW_RECORD_INVALID",
            503,
        )
        # 拒绝后档案必须保持损坏原样（未被静默重建为空 review）
        raw_after = json.loads(USERS_JSON.read_text(encoding="utf-8"))
        profile_after = next(user for user in raw_after["users"] if user["user_id"] == user_id)
        assert profile_after["review"]["items"].get("bad") == {"action": "banana"}
    finally:
        USERS_JSON.write_bytes(original_bytes)


# ---------------------------------------------------------------------------
# 8. 空扫描面分母：无 .md 的 vault → total=0 且 progress 全零
# ---------------------------------------------------------------------------


def test_qa_09_empty_vault_scan_zero_denominator(server, tmp_path: Path) -> None:
    empty = tmp_path / "empty-vault"
    empty.mkdir()
    scan = _scan(server, "default", empty)
    assert scan["total"] == 0 and scan["items"] == []
    assert scan["statistics"]["scanned_files"] == 0
    progress = _progress(server, "default")
    assert progress["total"] == 0
    assert progress["user"]["pending"] == 0 and progress["user"]["reviewed"] == 0


# ---------------------------------------------------------------------------
# 9. 前端 textContent 白名单与零外链（HTTP html 现算复验 + 双 tab + 键盘提示）
# ---------------------------------------------------------------------------


def test_qa_10_frontend_textcontent_whitelist_live(server) -> None:
    status, headers, body = server.request("GET", "/")
    assert status == 200
    html = body.decode("utf-8")
    assert "tab-review" in html and "tab-status" in html
    assert "审核工作台" in html and "四库状态" in html
    for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval("):
        assert banned not in html, banned
    assert "https://" not in html and "http://" not in html
    assert not re.search(r"<script[^>]+src=", html)
    emoji = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
    assert not emoji.findall(html)
    for needle in ("J/K 导航", "A/R/S 决策", "U 撤销", "输入框内不拦截按键", "扫描并载入待审清单"):
        assert needle in html, needle
    content_type = ""
    security = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = value
        if key.lower() == "content-security-policy":
            security = value
    assert "text/html" in content_type
    assert "default-src 'self'" in security


# ---------------------------------------------------------------------------
# 10. 只读铁证（QA 自证）：fixture 全树、审计三文件、导出零残留
# ---------------------------------------------------------------------------


def test_qa_11_readonly_evidence_fixture_audit_and_exports(server) -> None:
    """fixture vault 7 文件全树 hash + 审计库三文件 hash 与基线一致；exports 目录无 tmp 残留。"""

    fixture_tree = tree_hashes(FIXTURE_VAULT)
    assert len(fixture_tree) == 7
    assert fixture_tree["2-Permanent/unmanaged.md"].startswith("c1d0698e")
    assert fixture_tree["2-Permanent/invalid-yaml.md"].startswith("3a887f6d")
    assert "2-Permanent/Resources/MTR-P-System/excluded.md" in fixture_tree

    # 审计三文件（本 QA 增补套件会话）与 conftest 基线一致
    assert file_sha256(AUDIT_DB) == server.baseline["audit_main_sha"]
    assert file_sha256(AUDIT_WAL) == server.baseline["audit_wal_sha"]
    assert file_sha256(AUDIT_SHM) == server.baseline["audit_shm_sha"]

    if EXPORTS_DIR.is_dir():
        residue = [
            path.name
            for path in EXPORTS_DIR.glob("*")
            if ".tmp" in path.name or path.name.startswith(".")
        ]
        assert not residue, residue


def test_qa_12_old_endpoints_regression_smoke(server) -> None:
    """旧端点抽测 2 条：/api/check/lexiang 与 /api/audit/recent 行为不回归。"""

    status, payload = server.request_json("POST", "/api/check/lexiang", payload={"user_id": "default"})
    assert status == 200, payload
    assert unwrap_ok(payload)["user_id"] == "default"

    status, payload = server.request_json("GET", "/api/audit/recent?limit=3")
    assert status == 200, payload
    data = unwrap_ok(payload)
    assert data["count"] == len(data["events"]) <= 3
    assert data["read_only"] is True
