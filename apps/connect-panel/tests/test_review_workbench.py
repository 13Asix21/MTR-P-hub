# -*- coding: utf-8 -*-
"""review_workbench 增量验收；与 v1 的 43 项用例共存。"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import sys
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

PANEL_PACKAGE_PARENT = Path(__file__).resolve().parents[1]
if str(PANEL_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PANEL_PACKAGE_PARENT))

import panel.review_api as review_api
import panel.user_profiles as user_profiles
from panel.review_scan import scan_review_listing
from panel.review_store import ReviewState, decide
from panel.settings import PanelError, load_settings
from panel.user_profiles import ProfileStore, UserPreferences
from qa_support import (
    EXPORTS_DIR,
    FIXTURE_VAULT,
    PANEL_DIR,
    USERS_JSON,
    aggregation_digest,
    aggregation_paths,
    tree_hashes,
    unwrap_error,
    unwrap_ok,
)


def _create_user(server, role: str = "editor") -> str:
    status, payload = server.request_json(
        "POST",
        "/api/users",
        payload={"display_name": "审核工作台 QA " + role, "role": role},
    )
    assert status == 201, payload
    return unwrap_ok(payload)["user"]["user_id"]


def _scan(server, user_id: str, vault: Path) -> dict:
    status, payload = server.request_json(
        "POST",
        "/api/review/scan",
        payload={"user_id": user_id, "path": str(vault)},
    )
    assert status == 200, payload
    return unwrap_ok(payload)


def _records(server, user_id: str) -> dict:
    status, payload = server.request_json("GET", "/api/review/records?user_id=" + user_id)
    assert status == 200, payload
    return unwrap_ok(payload)


def _decide(server, user_id: str, path: str, sha256: str, action: str = "approved") -> dict:
    status, payload = server.request_json(
        "POST",
        "/api/review/decide",
        payload={
            "user_id": user_id,
            "path": path,
            "content_sha256": sha256,
            "action": action,
            "reason": "QA 审核",
        },
    )
    assert status == 200, payload
    return unwrap_ok(payload)


def _copy_vault(tmp_path: Path) -> Path:
    target = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, target)
    return target


def _item_query(user_id: str, path: str) -> str:
    return "/api/review/item?" + urllib.parse.urlencode({"user_id": user_id, "path": path})


def test_00_empty_cache_requires_scan(server) -> None:
    status, payload = server.request_json("GET", "/api/review/listing")
    assert status == 409
    error = unwrap_error(payload)
    assert error["code"] == "REVIEW_SCAN_REQUIRED"
    assert "扫描" in error["message"]


def test_01_single_response_statistics_and_check_vault_equal(server) -> None:
    user_id = _create_user(server)
    review = _scan(server, user_id, FIXTURE_VAULT)
    stats = review["statistics"]
    items = review["items"]
    assert review["total"] == len(items) == stats["scanned_files"] == 6
    assert review["counts"]["parsed_ok"] == stats["parsed_files"] == 5
    assert review["counts"]["invalid"] == stats["error_count"] == 1
    assert review["counts"]["file_too_large"] == 0
    assert review["counts"]["mtr"] == {
        "trial": stats["trial_notes"],
        "rarely": stats["rarely_notes"],
        "unmanaged": stats["unmanaged_notes"],
        "conflict": stats["conflict_notes"],
    } == {"trial": 1, "rarely": 1, "unmanaged": 2, "conflict": 1}
    assert sum(item["parse_status"] == "ok" for item in items) == 5
    assert sum(item["mtr_state"] in {"trial", "rarely", "unmanaged", "conflict"} for item in items) == 5
    invalid = next(item for item in items if item["vault_relative_path"].endswith("invalid-yaml.md"))
    assert invalid["parse_status"] == "INVALID_YAML"
    assert invalid["mtr_state"] == "unknown"
    assert re.fullmatch(r"[0-9a-f]{64}", invalid["sha256"])
    assert not any("Resources/MTR-P-System/excluded.md" in item["vault_relative_path"] for item in items)

    status, payload = server.request_json(
        "POST",
        "/api/check/vault",
        payload={"user_id": user_id, "path": str(FIXTURE_VAULT)},
    )
    assert status == 200, payload
    check = unwrap_ok(payload)
    assert review["statistics"] == check["statistics"]


def test_02_single_pass_reads_each_file_once() -> None:
    config = load_settings().obsidian
    original = Path.read_bytes
    calls: list[str] = []

    def counted(path: Path) -> bytes:
        calls.append(str(path))
        return original(path)

    with patch.object(Path, "read_bytes", counted):
        listing = scan_review_listing(str(FIXTURE_VAULT), config)
    assert len(calls) == listing["total"] == 6
    assert len(listing["items"]) == listing["statistics"]["scanned_files"]


def test_03_listing_item_invalid_preview_and_sealed_paths(server) -> None:
    user_id = _create_user(server)
    scanned = _scan(server, user_id, FIXTURE_VAULT)
    status, payload = server.request_json("GET", "/api/review/listing")
    assert status == 200
    listing = unwrap_ok(payload)
    assert listing["scanned_at"] == scanned["scanned_at"]
    assert listing["total"] == 6

    path = "2-Permanent/invalid-yaml.md"
    status, payload = server.request_json("GET", _item_query(user_id, path))
    assert status == 200, payload
    detail = unwrap_ok(payload)
    assert detail["frontmatter"]["parse_status"] == "INVALID_YAML"
    assert detail["frontmatter"]["entries"] == []
    assert "title: [unterminated" in detail["frontmatter"]["raw"]
    assert "该文件只产生文件级安全降级" in detail["body"]["text"]
    assert detail["body"]["raw_only"] is True
    assert detail["read_only"] is True
    assert detail["decision"] is None
    assert detail["stale"] is False
    assert detail["item"]["sha256"] == hashlib.sha256((FIXTURE_VAULT / path).read_bytes()).hexdigest()

    for bad_path in ("../outside.md", "2-Permanent/../outside.md", "C:/outside.md", "file://outside.md"):
        status, payload = server.request_json("GET", _item_query(user_id, bad_path))
        assert status == 400, (bad_path, payload)
        assert unwrap_error(payload)["code"] == "REVIEW_PATH_INVALID"
    status, payload = server.request_json("GET", _item_query(user_id, "2-Permanent/not-scanned.md"))
    assert status == 404
    assert unwrap_error(payload)["code"] == "REVIEW_PATH_NOT_SCANNED"


def test_04_file_too_large_item_and_detail(server, tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    large_relative = "2-Permanent/oversize.md"
    (vault / large_relative).write_bytes(b"x" * (load_settings().obsidian.max_file_bytes + 1))
    user_id = _create_user(server)
    listing = _scan(server, user_id, vault)
    assert listing["total"] == listing["statistics"]["scanned_files"] == 7
    assert listing["statistics"]["skipped_files"] == 1
    large = next(item for item in listing["items"] if item["vault_relative_path"] == large_relative)
    assert large["parse_status"] == "FILE_TOO_LARGE"
    assert large["mtr_state"] == "unknown"
    assert large["sha256"] == ""
    status, payload = server.request_json("GET", _item_query(user_id, large_relative))
    assert status == 413
    assert unwrap_error(payload)["code"] == "FILE_TOO_LARGE"
    status, payload = server.request_json(
        "POST",
        "/api/review/decide",
        payload={"user_id": user_id, "path": large_relative, "content_sha256": "0" * 64, "action": "approved"},
    )
    assert status == 413
    assert unwrap_error(payload)["code"] == "FILE_TOO_LARGE"
    assert _records(server, user_id)["items"] == {}


def test_05_decide_content_changed_stale_reset_and_undo(server, tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    user_id = _create_user(server)
    listing = _scan(server, user_id, vault)
    path = "2-Permanent/unmanaged.md"
    old_sha = next(item for item in listing["items"] if item["vault_relative_path"] == path)["sha256"]
    _decide(server, user_id, path, old_sha, "approved")
    target = vault / path
    target.write_bytes(target.read_bytes() + b"\nQA stale\n")

    status, payload = server.request_json(
        "POST",
        "/api/review/decide",
        payload={"user_id": user_id, "path": path, "content_sha256": old_sha, "action": "rejected"},
    )
    assert status == 409
    assert unwrap_error(payload)["code"] == "CONTENT_CHANGED"
    records = _records(server, user_id)
    assert records["items"][path]["action"] == "approved"
    assert len(records["history"]) == 1

    status, payload = server.request_json("GET", _item_query(user_id, path))
    assert status == 200
    assert unwrap_ok(payload)["stale"] is True
    status, payload = server.request_json(
        "POST",
        "/api/review/export",
        payload={"user_id": user_id, "include": "approved", "dry_run": True},
    )
    preview = unwrap_ok(payload)["preview"]
    assert preview["items_count"] == 0
    assert preview["excluded_stale"] == 1

    status, payload = server.request_json(
        "POST", "/api/review/reset", payload={"user_id": user_id, "path": path, "mode": "stale_reset"}
    )
    reset_data = unwrap_ok(payload)
    assert reset_data["history_tail"]["op"] == "STALE_RESET"
    assert reset_data["history_tail"]["from"] == "approved"
    assert reset_data["progress"]["reviewed"] == 0
    current_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    _decide(server, user_id, path, current_sha, "approved")
    status, payload = server.request_json(
        "POST", "/api/review/reset", payload={"user_id": user_id, "path": path, "mode": "undo"}
    )
    undo = unwrap_ok(payload)
    assert undo["removed"]["action"] == "approved"
    assert undo["history_tail"]["op"] == "UNDO"
    assert undo["progress"]["pending"] == 6
    records = _records(server, user_id)
    assert records["items"] == {}
    assert [event["op"] for event in records["history"][:2]] == ["UNDO", "DECIDE"]


def test_06_multiuser_isolation_global_conflict_and_position(server, tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    path = "2-Permanent/global-unique.md"
    (vault / path).write_text("---\ntitle: 全局分歧\n---\n正文\n", encoding="utf-8")
    owner = _create_user(server, "owner")
    editor = _create_user(server, "editor")
    listing = _scan(server, owner, vault)
    sha256 = next(item for item in listing["items"] if item["vault_relative_path"] == path)["sha256"]
    _decide(server, owner, path, sha256, "approved")
    _decide(server, editor, path, sha256, "rejected")

    status, payload = server.request_json("GET", "/api/review/progress?user_id=" + owner)
    owner_progress = unwrap_ok(payload)
    assert owner_progress["user"]["approved"] == 1
    assert owner_progress["user"]["rejected"] == 0
    conflict = next(item for item in owner_progress["global"]["conflicts"] if item["path"] == path)
    assert conflict["decisions"] == {owner: "approved", editor: "rejected"}
    status, payload = server.request_json("GET", "/api/review/progress?user_id=" + editor)
    editor_progress = unwrap_ok(payload)
    assert editor_progress["user"]["rejected"] == 1
    assert editor_progress["global"] is None

    status, payload = server.request_json(
        "POST", "/api/review/position", payload={"user_id": editor, "path": path}
    )
    assert unwrap_ok(payload)["last_position"] == path
    assert _records(server, editor)["last_position"] == path
    status, payload = server.request_json(
        "POST", "/api/review/position", payload={"user_id": editor, "path": None}
    )
    assert unwrap_ok(payload)["last_position"] is None
    status, payload = server.request_json(
        "POST", "/api/review/reset", payload={"user_id": editor, "path": path, "mode": "undo"}
    )
    assert unwrap_ok(payload)["history_tail"]["from"] == "rejected"
    assert _records(server, editor)["items"] == {}
    assert _records(server, owner)["items"][path]["action"] == "approved"


def test_07_export_approved_all_and_csv(server, tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    user_id = _create_user(server)
    listing = _scan(server, user_id, vault)
    by_path = {item["vault_relative_path"]: item for item in listing["items"]}
    actions = {
        "2-Permanent/unmanaged.md": "approved",
        "2-Permanent/invalid-yaml.md": "approved",
        "2-Permanent/code-fences.md": "rejected",
    }
    for path, action in actions.items():
        _decide(server, user_id, path, by_path[path]["sha256"], action)
    target = vault / "2-Permanent/unmanaged.md"
    target.write_bytes(target.read_bytes() + b"\nQA export stale\n")

    status, payload = server.request_json(
        "POST", "/api/review/export", payload={"user_id": user_id, "include": "approved", "dry_run": True}
    )
    assert unwrap_ok(payload)["preview"] == {"items_count": 1, "excluded_stale": 1, "include": "approved"}
    status, payload = server.request_json(
        "POST", "/api/review/export", payload={"user_id": user_id, "include": "approved"}
    )
    exported = unwrap_ok(payload)
    # 默认源码开发模式保留旧相对字段，同时给出实际运行时绝对路径，不新增文件下载端点。
    assert exported["export_location"] == "当前数据目录/exports"
    assert set(exported["files"]) == set(exported["files_absolute"]) == {"json", "csv"}
    json_path = PANEL_DIR / exported["files"]["json"]
    csv_path = PANEL_DIR / exported["files"]["csv"]
    for extension, path in (("json", json_path), ("csv", csv_path)):
        assert exported["files"][extension] == f"data/exports/{path.name}"
        assert Path(exported["files_absolute"][extension]) == path.resolve()
        assert path.resolve().parent == EXPORTS_DIR.resolve()
        assert path.is_file()
    status, rejected = server.request_json(
        "GET", "/api/review/export/" + urllib.parse.quote(exported["files_absolute"]["json"], safe="")
    )
    assert status == 404 and rejected["error"]["code"] == "ROUTE_NOT_FOUND"
    assert "users.json" not in json.dumps(rejected, ensure_ascii=False)
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == "1"
    assert set(document) >= {"schema_version", "exported_at", "user_id", "vault_id", "scope", "counts", "items"}
    assert len(document["vault_id"]) == 16
    assert document["counts"]["approved_total"] == 2
    assert document["counts"]["excluded_stale"] == 1
    assert len(document["items"]) == 1
    assert document["items"][0]["vault_relative_path"] == "2-Permanent/invalid-yaml.md"
    assert document["items"][0]["mtr_state"] == "unknown"
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in csv_path.read_bytes()
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(document["items"]) == 1
    assert rows[0]["stale"] == "false"

    status, payload = server.request_json(
        "POST", "/api/review/export", payload={"user_id": user_id, "include": "all"}
    )
    all_export = unwrap_ok(payload)
    assert all_export["files"]["json"] != exported["files"]["json"]
    all_document = json.loads((PANEL_DIR / all_export["files"]["json"]).read_text(encoding="utf-8"))
    assert len(all_document["items"]) == 3
    assert all_document["counts"]["excluded_stale"] == 0
    stale = {item["vault_relative_path"]: item["stale"] for item in all_document["items"]}
    assert stale["2-Permanent/unmanaged.md"] is True
    assert stale["2-Permanent/invalid-yaml.md"] is False
    with (PANEL_DIR / all_export["files"]["csv"]).open(encoding="utf-8-sig", newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 3


def test_08_batch_limit_mixed_partial_and_directory(server, tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    user_id = _create_user(server)
    listing = _scan(server, user_id, vault)
    by_path = {item["vault_relative_path"]: item for item in listing["items"]}
    too_many = [{"path": "2-Permanent/unmanaged.md", "content_sha256": "0" * 64} for _ in range(501)]
    status, payload = server.request_json(
        "POST", "/api/review/batch", payload={"user_id": user_id, "action": "approved", "items": too_many}
    )
    assert status == 413
    assert unwrap_error(payload)["code"] == "REVIEW_BATCH_LIMIT"
    assert _records(server, user_id)["items"] == {}

    missing = "2-Permanent/tag-conflict.md"
    (vault / missing).unlink()
    batch_items = [
        {"path": "2-Permanent/invalid-yaml.md", "content_sha256": by_path["2-Permanent/invalid-yaml.md"]["sha256"]},
        {"path": "2-Permanent/unmanaged.md", "content_sha256": by_path["2-Permanent/unmanaged.md"]["sha256"]},
        {"path": "2-Permanent/code-fences.md", "content_sha256": "0" * 64},
        {"path": missing, "content_sha256": by_path[missing]["sha256"]},
        {"path": "2-Permanent/ghost.md", "content_sha256": "0" * 64},
    ]
    status, payload = server.request_json(
        "POST",
        "/api/review/batch",
        payload={"user_id": user_id, "action": "approved", "reason": "混合批", "items": batch_items},
    )
    assert status == 200, payload
    batch = unwrap_ok(payload)
    assert batch["summary"]["succeeded"] == 2
    assert batch["summary"]["failed"] == 3
    assert batch["summary"]["content_changed"] == 1
    assert batch["summary"]["invalid_yaml"] == {"decided": 1, "failed": 0}
    assert batch["summary"]["by_error"] == {
        "CONTENT_CHANGED": 1,
        "REVIEW_FILE_MISSING": 1,
        "REVIEW_PATH_NOT_SCANNED": 1,
    }
    assert [result.get("code", "ok") for result in batch["results"]] == [
        "ok",
        "ok",
        "CONTENT_CHANGED",
        "REVIEW_FILE_MISSING",
        "REVIEW_PATH_NOT_SCANNED",
    ]
    records = _records(server, user_id)
    assert len(records["items"]) == 2
    assert len(records["history"]) == 2

    status, payload = server.request_json("GET", "/api/review/progress?user_id=" + user_id + "&view=directory")
    progress = unwrap_ok(payload)
    directories = progress["directories"]
    assert sum(item["total"] for item in directories.values()) == 6
    assert sum(item["reviewed"] for item in directories.values()) == 2
    assert progress["user"]["approved"] == 2

    status, payload = server.request_json(
        "POST",
        "/api/review/batch",
        payload={
            "user_id": user_id,
            "action": "rejected",
            "items": [{"path": "2-Permanent/invalid-yaml.md", "content_sha256": "0" * 64}],
        },
    )
    assert unwrap_ok(payload)["summary"]["invalid_yaml"] == {"decided": 0, "failed": 1}
    assert len(_records(server, user_id)["history"]) == 2


def test_09_nfc_request_and_record_keys(server, tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    nfd_path = "2-Permanent/中文目录/e\u0301-审核.md"
    nfc_path = unicodedata.normalize("NFC", nfd_path)
    target = vault / nfd_path
    target.write_text("---\ntitle: NFC\n---\n正文\n", encoding="utf-8")
    user_id = _create_user(server)
    listing = _scan(server, user_id, vault)
    assert any(item["vault_relative_path"] == nfc_path for item in listing["items"])
    status, payload = server.request_json("GET", _item_query(user_id, nfd_path))
    assert status == 200, payload
    sha256 = unwrap_ok(payload)["item"]["sha256"]
    _decide(server, user_id, nfd_path, sha256, "approved")
    records = _records(server, user_id)
    assert nfc_path in records["items"]
    assert nfd_path not in records["items"]
    status, payload = server.request_json("POST", "/api/review/export", payload={"user_id": user_id})
    exported = unwrap_ok(payload)
    document = json.loads((PANEL_DIR / exported["files"]["json"]).read_text(encoding="utf-8"))
    assert document["items"][0]["vault_relative_path"] == nfc_path


def test_10_wikilinks_unique_ambiguous_and_invalid_yaml(server, tmp_path: Path) -> None:
    vault = _copy_vault(tmp_path)
    (vault / "2-Permanent/b.md").write_text("目标 B\n", encoding="utf-8")
    (vault / "2-Permanent/dir1").mkdir()
    (vault / "2-Permanent/dir2").mkdir()
    (vault / "2-Permanent/dir1/dup.md").write_text("甲\n", encoding="utf-8")
    (vault / "2-Permanent/dir2/dup.md").write_text("乙\n", encoding="utf-8")
    (vault / "2-Permanent/a.md").write_text("[[b]]\n[[dup]]\n```md\n[[b]]\n```\n", encoding="utf-8")
    invalid = vault / "2-Permanent/invalid-yaml.md"
    invalid.write_bytes(invalid.read_bytes() + b"[[b]]\n")
    user_id = _create_user(server)
    listing = _scan(server, user_id, vault)
    items = {item["vault_relative_path"]: item for item in listing["items"]}
    assert items["2-Permanent/a.md"]["links_out"] == 2
    assert items["2-Permanent/b.md"]["links_in"] == 2
    assert items["2-Permanent/dir1/dup.md"]["links_in"] == 0
    assert items["2-Permanent/dir2/dup.md"]["links_in"] == 0
    assert items["2-Permanent/invalid-yaml.md"]["links_out"] == 1


def test_11_full_flow_vault_hash_unchanged_and_body_not_stored(server) -> None:
    before = tree_hashes(FIXTURE_VAULT)
    user_id = _create_user(server)
    listing = _scan(server, user_id, FIXTURE_VAULT)
    path = "2-Permanent/invalid-yaml.md"
    sha256 = next(item for item in listing["items"] if item["vault_relative_path"] == path)["sha256"]
    status, payload = server.request_json("GET", _item_query(user_id, path))
    assert status == 200
    _decide(server, user_id, path, sha256, "approved")
    status, payload = server.request_json("POST", "/api/review/export", payload={"user_id": user_id})
    assert status == 200, payload
    after = tree_hashes(FIXTURE_VAULT)
    assert after == before
    users_text = USERS_JSON.read_text(encoding="utf-8")
    assert "该文件只产生文件级安全降级" not in users_text
    records = _records(server, user_id)
    assert records["items"][path]["content_sha256"] == sha256
    assert "body" not in records["items"][path]


def test_12_profile_store_old_review_serialization_and_atomic_failure(tmp_path: Path, monkeypatch) -> None:
    store = ProfileStore(tmp_path / "users.json")
    preferences = UserPreferences(vault_root=str(tmp_path), git_local_path=str(tmp_path))
    store.ensure_default(preferences)
    path = tmp_path / "users.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["users"][0]["review"]
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert store.get_user_review("default").items == {}
    store.update_preferences("default", {"auto_refresh_minutes": 5})
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["users"][0]["review"]["items"] == {}

    now = "2026-11-19T09:00:00+00:00"
    store.update_user_review(
        "default",
        lambda state: decide(state, "2-Permanent/旧档.md", "approved", "保留", "a" * 64, now),
    )
    store.create_user("第二审核人", "editor")
    store.update_preferences("default", {"auto_refresh_minutes": 10})
    store.ensure_default(preferences)
    raw = json.loads(path.read_text(encoding="utf-8"))
    default = next(user for user in raw["users"] if user["user_id"] == "default")
    assert default["review"]["items"]["2-Permanent/旧档.md"]["reason"] == "保留"

    original = path.read_bytes()

    def fail_replace(_source, _target) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(user_profiles.os, "replace", fail_replace)
    with pytest.raises(PanelError) as caught:
        store.update_user_review(
            "default",
            lambda state: decide(state, "2-Permanent/失败.md", "approved", "", "b" * 64, now),
        )
    assert caught.value.code == "PROFILE_WRITE_FAILED"
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".users-*.tmp")) == []


def test_13_review_state_limits_and_corrupt_store(tmp_path: Path) -> None:
    state = ReviewState()
    for _index in range(205):
        state = decide(state, "2-Permanent/历史.md", "approved", "x" * 500, "a" * 64, "2026-11-19T09:00:00+00:00")
    assert len(state.history) == 200
    with pytest.raises(PanelError) as caught:
        decide(state, "2-Permanent/历史.md", "approved", "x" * 501, "a" * 64, "2026-11-19T09:00:00+00:00")
    assert caught.value.code == "REQUEST_VALIDATION_FAILED"

    store = ProfileStore(tmp_path / "users.json")
    store.ensure_default(UserPreferences(vault_root=str(tmp_path), git_local_path=str(tmp_path)))
    path = tmp_path / "users.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["users"][0]["review"] = {"schema_version": "1", "items": {"bad": {}}, "history": [], "last_position": None}
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(PanelError) as corrupted:
        store.get_user_review("default")
    assert corrupted.value.code == "REVIEW_RECORD_INVALID"
    assert path.read_bytes() == original


def test_14_export_pair_failure_cleans_partial(tmp_path: Path, monkeypatch) -> None:
    exports = tmp_path / "exports"
    monkeypatch.setattr(review_api, "_EXPORTS_DIR", exports)
    original_replace = review_api.os.replace
    calls = 0

    def fail_second(source, target) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected export failure")
        original_replace(source, target)

    monkeypatch.setattr(review_api.os, "replace", fail_second)
    with pytest.raises(PanelError) as caught:
        review_api._write_export_pair(
            {"schema_version": "1", "items": []},
            [],
            "default",
            datetime.now(timezone.utc),
        )
    assert caught.value.code == "EXPORT_WRITE_FAILED"
    assert list(exports.glob("*")) == []


def test_15_frontend_single_file_security_contract() -> None:
    text = (PANEL_DIR / "static" / "index.html").read_text(encoding="utf-8")
    identifiers = re.findall(r"\bid=\"([^\"]+)\"", text)
    assert len(identifiers) == len(set(identifiers))
    for element_id in (
        "tab-review",
        "review-progress-title",
        "review-list",
        "review-search",
        "review-invalid-only",
        "review-batch",
        "review-export",
        "review-global",
        "review-coverage",
        "review-reason",
    ):
        assert element_id in identifiers
    assert "innerHTML" not in text
    assert "eval(" not in text
    assert "https://" not in text and "http://" not in text
    assert text.count("document.addEventListener(\"keydown\"") == 1
    assert "slice(offset, offset + 500)" in text
    assert "reviewState.records.history.slice(0, 20)" in text


def test_99_main_repository_aggregation_unchanged(server) -> None:
    paths = aggregation_paths()
    assert len(paths) == server.baseline["aggregation_paths_count"]
    assert aggregation_digest(paths) == server.baseline["aggregation_digest"]
