"""审核工作台 APIRouter 工厂、会话扫描缓存与封闭端点错误。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import Field

from .api_common import USER_ID_PATTERN, StrictRequest, ok
from .review_scan import load_file_detail, read_current_file, scan_review_listing
from .review_store import (
    REVIEW_BATCH_MAX_ITEMS,
    ReviewState,
    build_export_items,
    compute_global,
    compute_progress,
    coverage_by_directory,
    decide,
    history_event_to_dict,
    record_to_dict,
    reset,
    review_status_for,
    state_to_dict,
)
from .settings import ObsidianConfig, PanelError, PanelSettings
from .user_profiles import ProfileStore


_EXPORTS_DIR = Path(__file__).resolve().parent.parent / "data" / "exports"
_CSV_FIELDS = (
    "vault_relative_path",
    "action",
    "reason",
    "content_sha256",
    "decided_at",
    "decided_by",
    "mtr_state",
    "tags",
    "parse_status",
    "stale",
    "size_bytes",
    "mtime",
)


@dataclass(frozen=True)
class ReviewScanSnapshot:
    """最近一次完整扫描的只读快照及 NFC 索引。"""

    listing: dict[str, Any]
    root: Path
    config: ObsidianConfig
    index: dict[str, dict[str, Any]]


class ReviewScanCache:
    """进程内单槽缓存；替换与读取均由锁保护，绝不持久化正文。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: ReviewScanSnapshot | None = None

    def replace(self, listing: dict[str, Any], config: ObsidianConfig) -> None:
        try:
            root = Path(listing["path"]).resolve(strict=False)
        except (OSError, RuntimeError):
            root = Path(listing["path"]).absolute()
        source_paths = listing.pop("_source_paths", {})
        index = {
            str(item["vault_relative_path"]): {
                **item,
                "_source_relative_path": source_paths.get(
                    str(item["vault_relative_path"]),
                    str(item["vault_relative_path"]),
                ),
            }
            for item in listing.get("items", [])
        }
        snapshot = ReviewScanSnapshot(
            listing=listing,
            root=root,
            config=config,
            index=index,
        )
        with self._lock:
            self._snapshot = snapshot

    def get(self) -> ReviewScanSnapshot:
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            raise PanelError(
                "REVIEW_SCAN_REQUIRED",
                "请先在审核工作台点击“扫描并载入待审清单”。",
                409,
            )
        return snapshot


class ReviewScanRequest(StrictRequest):
    user_id: str = Field(default="default", pattern=USER_ID_PATTERN)
    path: str | None = Field(default=None, min_length=1, max_length=2048)
    managed_roots: list[str] | None = Field(default=None, max_length=64)
    excluded_roots: list[str] | None = Field(default=None, max_length=64)


class ReviewDecideRequest(StrictRequest):
    user_id: str = Field(default="default", pattern=USER_ID_PATTERN)
    path: str = Field(min_length=1, max_length=2048)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["approved", "rejected", "skipped"]
    reason: str | None = Field(default=None, max_length=500)


class ReviewResetRequest(StrictRequest):
    user_id: str = Field(default="default", pattern=USER_ID_PATTERN)
    path: str = Field(min_length=1, max_length=2048)
    mode: Literal["undo", "stale_reset"]


class ReviewPositionRequest(StrictRequest):
    user_id: str = Field(default="default", pattern=USER_ID_PATTERN)
    path: str | None = Field(default=None, min_length=1, max_length=2048)


class ReviewExportRequest(StrictRequest):
    user_id: str = Field(default="default", pattern=USER_ID_PATTERN)
    include: Literal["approved", "all"] = "approved"
    dry_run: bool = False


class ReviewBatchItem(StrictRequest):
    path: str = Field(min_length=1, max_length=2048)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewBatchRequest(StrictRequest):
    user_id: str = Field(default="default", pattern=USER_ID_PATTERN)
    action: Literal["approved", "rejected", "skipped"]
    reason: str | None = Field(default=None, max_length=500)
    items: list[ReviewBatchItem]


def _review_path(raw_path: str) -> str:
    """NFC 归一后校验相对 POSIX 路径，拒绝逃逸、URL、盘符与 ADS。"""

    if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 2048 or "\x00" in raw_path:
        raise PanelError("REVIEW_PATH_INVALID", "审核路径为空、过长或包含无效字符。", 400)
    value = unicodedata.normalize("NFC", raw_path)
    if (
        value.startswith(("/", "\\"))
        or "\\" in value
        or ":" in value
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value)
        or Path(value).is_absolute()
    ):
        raise PanelError("REVIEW_PATH_INVALID", "审核路径只能是 Vault 内相对 POSIX 路径。", 400)
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PanelError("REVIEW_PATH_INVALID", "审核路径不能包含空段、当前目录或父目录跳转。", 400)
    return value


def _scan_member(snapshot: ReviewScanSnapshot, path: str) -> dict[str, Any]:
    item = snapshot.index.get(path)
    if item is None:
        raise PanelError("REVIEW_PATH_NOT_SCANNED", "该路径不在当前扫描清单中，请重新扫描后再操作。", 404)
    return item


def _write_export_pair(
    document: dict[str, Any],
    items: list[dict[str, Any]],
    user_id: str,
    timestamp: datetime,
) -> tuple[Path, Path]:
    """成对写临时文件并替换；失败时清理本次产物，不触碰历史导出。"""

    temporary_paths: set[Path] = set()
    replaced_paths: list[Path] = []
    try:
        _EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stem_base = f"review-{user_id}-{timestamp.strftime('%Y%m%d-%H%M%S')}"
        suffix = 1
        while True:
            stem = stem_base if suffix == 1 else f"{stem_base}-{suffix}"
            json_path = _EXPORTS_DIR / f"{stem}.json"
            csv_path = _EXPORTS_DIR / f"{stem}.csv"
            if not json_path.exists() and not csv_path.exists():
                break
            suffix += 1

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{stem}-",
            suffix=".json.tmp",
            dir=_EXPORTS_DIR,
            delete=False,
        ) as stream:
            json_temporary = Path(stream.name)
            temporary_paths.add(json_temporary)
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            prefix=f".{stem}-",
            suffix=".csv.tmp",
            dir=_EXPORTS_DIR,
            delete=False,
        ) as stream:
            csv_temporary = Path(stream.name)
            temporary_paths.add(csv_temporary)
            writer = csv.DictWriter(stream, fieldnames=_CSV_FIELDS, lineterminator="\r\n")
            writer.writeheader()
            for item in items:
                writer.writerow(
                    {
                        "vault_relative_path": item["vault_relative_path"],
                        "action": item["action"],
                        "reason": item["reason"],
                        "content_sha256": item["content_sha256"],
                        "decided_at": item["decided_at"],
                        "decided_by": item["decided_by"],
                        "mtr_state": item.get("mtr_state") or "",
                        "tags": ";".join(str(tag) for tag in item.get("tags", [])),
                        "parse_status": item.get("parse_status") or "",
                        "stale": "true" if item.get("stale", False) else "false",
                        "size_bytes": item.get("size_bytes", ""),
                        "mtime": item.get("mtime") or "",
                    }
                )
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(json_temporary, json_path)
        temporary_paths.discard(json_temporary)
        replaced_paths.append(json_path)
        os.replace(csv_temporary, csv_path)
        temporary_paths.discard(csv_temporary)
        replaced_paths.append(csv_path)
        return json_path, csv_path
    except Exception as exc:
        for path in temporary_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        for path in replaced_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise PanelError("EXPORT_WRITE_FAILED", "导出文件写入失败，未保留不完整的导出产物。", 500) from exc


def create_review_router(
    settings_factory: Callable[[], PanelSettings],
    profile_store_factory: Callable[[], ProfileStore],
) -> APIRouter:
    """以 main 的配置与 ProfileStore 单例构造审核路由，不创建第二写者。"""

    router = APIRouter(prefix="/api/review")
    cache = ReviewScanCache()
    export_lock = threading.Lock()

    @router.post("/scan")
    def scan(payload: ReviewScanRequest) -> dict[str, Any]:
        settings = settings_factory()
        profile = profile_store_factory().get_user(payload.user_id)
        raw_path = (
            payload.path
            if payload.path is not None
            else profile.preferences.vault_root or settings.obsidian.vault_root
        )
        listing = scan_review_listing(
            raw_path,
            settings.obsidian,
            payload.managed_roots,
            payload.excluded_roots,
        )
        cache.replace(listing, settings.obsidian)
        return ok(listing)

    @router.get("/listing")
    def listing() -> dict[str, Any]:
        return ok(cache.get().listing)

    @router.get("/item")
    def item(
        user_id: str = Query(default="default", pattern=USER_ID_PATTERN),
        path: str = Query(),
    ) -> dict[str, Any]:
        snapshot = cache.get()
        normalized_path = _review_path(path)
        listing_item = _scan_member(snapshot, normalized_path)
        detail = load_file_detail(snapshot.root, listing_item, snapshot.config)
        state = profile_store_factory().get_user_review(user_id)
        record = (state.items or {}).get(normalized_path)
        decision = None
        if record is not None:
            decision = {
                "action": record.action,
                "reason": record.reason,
                "decided_at": record.decided_at,
            }
        status = review_status_for(record, str(detail["item"]["sha256"]))
        return ok(
            {
                **detail,
                "decision": decision,
                "stale": status == "stale",
                "read_only": True,
            }
        )

    @router.post("/decide")
    def decide_item(payload: ReviewDecideRequest) -> dict[str, Any]:
        snapshot = cache.get()
        normalized_path = _review_path(payload.path)
        listing_item = _scan_member(snapshot, normalized_path)
        current = read_current_file(snapshot.root, listing_item, snapshot.config)
        if current.content_sha256 != payload.content_sha256:
            raise PanelError(
                "CONTENT_CHANGED",
                "文件内容已变化，本次审核未写入；请重新打开详情后再决定。",
                409,
            )
        timestamp = datetime.now(timezone.utc).isoformat()
        store = profile_store_factory()
        updated = store.update_user_review(
            payload.user_id,
            lambda state: decide(
                state,
                normalized_path,
                payload.action,
                payload.reason,
                current.content_sha256,
                timestamp,
            ),
        )
        record = (updated.items or {})[normalized_path]
        return ok(
            {
                "item": record_to_dict(record),
                "progress": compute_progress(snapshot.listing, updated),
                "stale": False,
            }
        )

    @router.post("/reset")
    def reset_item(payload: ReviewResetRequest) -> dict[str, Any]:
        snapshot = cache.get()
        normalized_path = _review_path(payload.path)
        store = profile_store_factory()
        if normalized_path not in snapshot.index:
            existing = store.get_user_review(payload.user_id)
            if normalized_path not in (existing.items or {}):
                _scan_member(snapshot, normalized_path)
        captured: dict[str, Any] = {}

        def apply_reset(state: ReviewState) -> ReviewState:
            previous = (state.items or {}).get(normalized_path)
            updated = reset(state, normalized_path, payload.mode, datetime.now(timezone.utc).isoformat())
            if previous is not None:
                captured["removed"] = record_to_dict(previous)
            captured["history_tail"] = history_event_to_dict((updated.history or [])[-1])
            return updated

        updated = store.update_user_review(payload.user_id, apply_reset)
        return ok(
            {
                "reset": True,
                "removed": captured["removed"],
                "history_tail": captured["history_tail"],
                "progress": compute_progress(snapshot.listing, updated),
            }
        )

    @router.get("/records")
    def records(
        user_id: str = Query(default="default", pattern=USER_ID_PATTERN),
        history_limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        state = profile_store_factory().get_user_review(user_id)
        data = state_to_dict(state)
        data["history"] = list(reversed(data["history"]))[:history_limit]
        return ok(data)

    @router.post("/position")
    def position(payload: ReviewPositionRequest) -> dict[str, Any]:
        snapshot = cache.get()
        normalized_path: str | None = None
        if payload.path is not None:
            normalized_path = _review_path(payload.path)
            _scan_member(snapshot, normalized_path)
        updated = profile_store_factory().update_user_review(
            payload.user_id,
            lambda state: replace(state, last_position=normalized_path),
        )
        return ok({"last_position": updated.last_position})

    @router.get("/progress")
    def progress(
        user_id: str = Query(default="default", pattern=USER_ID_PATTERN),
        view: Literal["summary", "directory"] = Query(default="summary"),
    ) -> dict[str, Any]:
        snapshot = cache.get()
        store = profile_store_factory()
        profile = store.get_user(user_id)
        state = store.get_user_review(user_id)
        global_summary = None
        if profile.role == "owner":
            all_states = {
                user.user_id: store.get_user_review(user.user_id)
                for user in store.list_users()
            }
            global_summary = compute_global(snapshot.listing, all_states)
        data = {
            "total": snapshot.listing["total"],
            "scanned_at": snapshot.listing["scanned_at"],
            "user": compute_progress(snapshot.listing, state),
            "global": global_summary,
        }
        if view == "directory":
            data["directories"] = coverage_by_directory(snapshot.listing, state)
        return ok(data)

    @router.post("/batch")
    def batch(payload: ReviewBatchRequest) -> dict[str, Any]:
        if len(payload.items) > REVIEW_BATCH_MAX_ITEMS:
            raise PanelError(
                "REVIEW_BATCH_LIMIT",
                "单次批量最多 500 篇；请分批提交，本次未执行任何审核。",
                413,
            )
        snapshot = cache.get()
        store = profile_store_factory()
        store.get_user(payload.user_id)
        results: list[dict[str, Any]] = []
        prepared: list[dict[str, Any]] = []
        by_error: dict[str, int] = {}
        invalid_yaml_failed = 0

        for requested in payload.items:
            result: dict[str, Any] = {"path": requested.path, "status": "error"}
            listing_item: dict[str, Any] | None = None
            try:
                normalized_path = _review_path(requested.path)
                result["path"] = normalized_path
                listing_item = _scan_member(snapshot, normalized_path)
                current = read_current_file(snapshot.root, listing_item, snapshot.config)
                if current.content_sha256 != requested.content_sha256:
                    raise PanelError("CONTENT_CHANGED", "内容已变更。", 409)
            except PanelError as exc:
                result["code"] = exc.code
                by_error[exc.code] = by_error.get(exc.code, 0) + 1
                if listing_item is not None and listing_item.get("parse_status") == "INVALID_YAML":
                    invalid_yaml_failed += 1
                results.append(result)
                continue
            result["status"] = "ok"
            results.append(result)
            prepared.append(
                {
                    "path": normalized_path,
                    "sha256": current.content_sha256,
                    "listing_item": listing_item,
                    "result": result,
                }
            )

        if prepared:
            timestamp = datetime.now(timezone.utc).isoformat()

            def apply_batch(state: ReviewState) -> ReviewState:
                updated = state
                for accepted in prepared:
                    updated = decide(
                        updated,
                        accepted["path"],
                        payload.action,
                        payload.reason,
                        accepted["sha256"],
                        timestamp,
                    )
                return updated

            updated = store.update_user_review(payload.user_id, apply_batch)
        else:
            updated = store.get_user_review(payload.user_id)

        invalid_yaml_decided = 0
        for accepted in prepared:
            record = (updated.items or {})[accepted["path"]]
            accepted["result"]["item"] = record_to_dict(record)
            if accepted["listing_item"].get("parse_status") == "INVALID_YAML":
                invalid_yaml_decided += 1
        succeeded = len(prepared)
        summary = {
            "total": len(payload.items),
            "succeeded": succeeded,
            "failed": len(payload.items) - succeeded,
            "content_changed": by_error.get("CONTENT_CHANGED", 0),
            "invalid_yaml": {
                "decided": invalid_yaml_decided,
                "failed": invalid_yaml_failed,
            },
            "by_error": dict(sorted(by_error.items())),
        }
        return ok(
            {
                "results": results,
                "summary": summary,
                "progress": compute_progress(snapshot.listing, updated),
            }
        )

    @router.post("/export")
    def export(payload: ReviewExportRequest) -> dict[str, Any]:
        snapshot = cache.get()
        state = profile_store_factory().get_user_review(payload.user_id)

        def fresh_sha(path: str) -> str | None:
            listing_item = snapshot.index.get(path)
            if listing_item is None:
                return None
            try:
                return read_current_file(snapshot.root, listing_item, snapshot.config).content_sha256
            except PanelError as exc:
                if exc.code in {
                    "REVIEW_FILE_MISSING",
                    "REVIEW_PATH_INVALID",
                    "FILE_TOO_LARGE",
                }:
                    return None
                raise

        selection = build_export_items(
            snapshot.listing,
            state,
            payload.include,
            fresh_sha,
            user_id=payload.user_id,
        )
        items = selection["items"]
        if payload.dry_run:
            return ok(
                {
                    "preview": {
                        "items_count": len(items),
                        "excluded_stale": selection["excluded_stale"],
                        "include": payload.include,
                    }
                }
            )

        timestamp = datetime.now(timezone.utc)
        exported_at = timestamp.isoformat()
        scope_note = (
            "items 仅含 approved 且当前内容 hash 复核一致"
            if payload.include == "approved"
            else "items 含全部已决篇；stale 字段按导出时现算标注"
        )
        document = {
            "schema_version": "1",
            "exported_at": exported_at,
            "user_id": payload.user_id,
            "vault_id": hashlib.sha256(str(snapshot.root).encode("utf-8")).hexdigest()[:16],
            "scope": {
                "vault_root": snapshot.root.as_posix(),
                "managed_roots": snapshot.listing["managed_roots"],
                "excluded_roots": snapshot.listing["excluded_roots"],
                "scanned_at": snapshot.listing["scanned_at"],
                "total_scanned": snapshot.listing["total"],
                "include": payload.include,
            },
            "counts": {
                "exported_items": len(items),
                "approved_total": selection["approved_total"],
                "excluded_stale": selection["excluded_stale"],
                "scope_note": scope_note,
            },
            "items": items,
        }
        with export_lock:
            json_path, csv_path = _write_export_pair(
                document,
                items,
                payload.user_id,
                timestamp,
            )
        return ok(
            {
                "files": {
                    "json": f"data/exports/{json_path.name}",
                    "csv": f"data/exports/{csv_path.name}",
                },
                "counts": {
                    "exported_items": len(items),
                    "excluded_stale": selection["excluded_stale"],
                },
                "exported_at": exported_at,
            }
        )

    return router


__all__ = ["ReviewScanCache", "create_review_router"]
