"""审核领域的数据校验与纯函数变换；本模块不执行任何文件 IO。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Literal

from .settings import PanelError


REVIEW_HISTORY_LIMIT = 200
REVIEW_REASON_MAX_CHARS = 500
REVIEW_BATCH_MAX_ITEMS = 500
_ACTIONS = ("approved", "rejected", "skipped")
_HISTORY_OPS = ("DECIDE", "UNDO", "STALE_RESET")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONFLICT_SAMPLE_LIMIT = 100


@dataclass(frozen=True)
class ReviewRecord:
    """一篇笔记在某个内容版本上的最后审核结论。"""

    action: str
    reason: str
    content_sha256: str
    decided_at: str
    updated_at: str


@dataclass(frozen=True)
class HistoryEvent:
    """追加式审核历史；JSON 中的 from/to 对应两个动作字段。"""

    path: str
    op: str
    from_action: str | None
    to_action: str | None
    reason: str | None
    content_sha256: str | None
    at: str


@dataclass(frozen=True)
class ReviewState:
    """单用户完整审核台账；未审篇目在 items 中无键。"""

    schema_version: str = "1"
    items: dict[str, ReviewRecord] | None = None
    history: list[HistoryEvent] | None = None
    last_position: str | None = None

    def __post_init__(self) -> None:
        if self.items is None:
            object.__setattr__(self, "items", {})
        if self.history is None:
            object.__setattr__(self, "history", [])


def _invalid(message: str = "审核记录结构损坏，已拒绝操作。") -> None:
    raise PanelError("REVIEW_RECORD_INVALID", message, 503)


def _timestamp(value: Any, *, stored: bool = True) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            if stored:
                _invalid("审核记录时间必须带 UTC 时区。")
            raise PanelError("REQUEST_VALIDATION_FAILED", "时间必须带 UTC 时区。", 422)
        return value.astimezone(timezone.utc).isoformat()
    if not isinstance(value, str) or not value.endswith("+00:00"):
        if stored:
            _invalid("审核记录时间必须是带 +00:00 的 ISO8601 字符串。")
        raise PanelError("REQUEST_VALIDATION_FAILED", "时间必须是带 +00:00 的 ISO8601 字符串。", 422)
    try:
        datetime.fromisoformat(value)
    except ValueError:
        if stored:
            _invalid("审核记录包含无效时间。")
        raise PanelError("REQUEST_VALIDATION_FAILED", "时间格式无效。", 422) from None
    return value


def _path(value: Any, *, stored: bool = True) -> str:
    valid = isinstance(value, str) and 1 <= len(value) <= 2048 and "\x00" not in value
    normalized = unicodedata.normalize("NFC", value) if isinstance(value, str) else ""
    if valid:
        valid = (
            normalized == value
            and not value.startswith(("/", "\\"))
            and "\\" not in value
            and ":" not in value
            and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value)
            and all(part not in ("", ".", "..") for part in value.split("/"))
        )
    if not valid:
        if stored:
            _invalid("审核记录包含无效或非 NFC 的相对路径。")
        raise PanelError("REVIEW_PATH_INVALID", "审核路径必须是安全的 NFC 相对 POSIX 路径。", 400)
    return normalized


def _sha256(value: Any, *, stored: bool = True) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        if stored:
            _invalid("审核记录包含无效 content_sha256。")
        raise PanelError("REQUEST_VALIDATION_FAILED", "content_sha256 必须是 64 位小写十六进制。", 422)
    return value


def _reason(value: Any, *, stored: bool = True) -> str:
    if value is None and not stored:
        return ""
    if not isinstance(value, str) or len(value) > REVIEW_REASON_MAX_CHARS:
        if stored:
            _invalid("审核理由必须是至多 500 字符的字符串。")
        raise PanelError("REQUEST_VALIDATION_FAILED", "审核理由不能超过 500 字符。", 422)
    return value


def record_to_dict(record: ReviewRecord) -> dict[str, Any]:
    return {
        "action": record.action,
        "reason": record.reason,
        "content_sha256": record.content_sha256,
        "decided_at": record.decided_at,
        "updated_at": record.updated_at,
    }


def history_event_to_dict(event: HistoryEvent) -> dict[str, Any]:
    return {
        "path": event.path,
        "op": event.op,
        "from": event.from_action,
        "to": event.to_action,
        "reason": event.reason,
        "content_sha256": event.content_sha256,
        "at": event.at,
    }


def state_to_dict(state: ReviewState) -> dict[str, Any]:
    """序列化完整审核状态，键名与 users.json 契约一致。"""

    return {
        "schema_version": state.schema_version,
        "items": {path: record_to_dict(record) for path, record in (state.items or {}).items()},
        "history": [history_event_to_dict(event) for event in (state.history or [])],
        "last_position": state.last_position,
    }


def _parse_record(raw: Any) -> ReviewRecord:
    expected = {"action", "reason", "content_sha256", "decided_at", "updated_at"}
    if not isinstance(raw, dict) or set(raw) != expected:
        _invalid("ReviewRecord 字段不完整或包含未知字段。")
    action = raw.get("action")
    if action not in _ACTIONS:
        _invalid("ReviewRecord.action 无效。")
    return ReviewRecord(
        action=str(action),
        reason=_reason(raw.get("reason")),
        content_sha256=_sha256(raw.get("content_sha256")),
        decided_at=_timestamp(raw.get("decided_at")),
        updated_at=_timestamp(raw.get("updated_at")),
    )


def _parse_history(raw: Any) -> HistoryEvent:
    expected = {"path", "op", "from", "to", "reason", "content_sha256", "at"}
    if not isinstance(raw, dict) or set(raw) != expected:
        _invalid("HistoryEvent 字段不完整或包含未知字段。")
    op = raw.get("op")
    from_action = raw.get("from")
    to_action = raw.get("to")
    if op not in _HISTORY_OPS:
        _invalid("HistoryEvent.op 无效。")
    if from_action is not None and from_action not in _ACTIONS:
        _invalid("HistoryEvent.from 无效。")
    if to_action is not None and to_action not in _ACTIONS:
        _invalid("HistoryEvent.to 无效。")
    if op == "DECIDE" and to_action not in _ACTIONS:
        _invalid("DECIDE 历史必须有目标动作。")
    if op in {"UNDO", "STALE_RESET"} and (from_action not in _ACTIONS or to_action is not None):
        _invalid("撤销历史必须保留前动作且目标为空。")
    reason = raw.get("reason")
    if reason is not None:
        reason = _reason(reason)
    sha256 = raw.get("content_sha256")
    if sha256 is not None:
        sha256 = _sha256(sha256)
    return HistoryEvent(
        path=_path(raw.get("path")),
        op=str(op),
        from_action=from_action,
        to_action=to_action,
        reason=reason,
        content_sha256=sha256,
        at=_timestamp(raw.get("at")),
    )


def validate_state(raw: ReviewState | dict[str, Any] | None) -> ReviewState:
    """深度校验档案中的 review；旧档缺失时仅返回内存空态，不改写档案。"""

    if raw is None:
        return ReviewState()
    if isinstance(raw, ReviewState):
        raw = state_to_dict(raw)
    expected = {"schema_version", "items", "history", "last_position"}
    if not isinstance(raw, dict) or set(raw) != expected or raw.get("schema_version") != "1":
        _invalid("review 顶层结构或 schema_version 无效。")
    raw_items = raw.get("items")
    raw_history = raw.get("history")
    if not isinstance(raw_items, dict) or not isinstance(raw_history, list):
        _invalid("review.items 必须是对象，review.history 必须是数组。")
    if len(raw_history) > REVIEW_HISTORY_LIMIT:
        _invalid("review.history 超过 200 条上限。")
    items: dict[str, ReviewRecord] = {}
    for path, record in raw_items.items():
        items[_path(path)] = _parse_record(record)
    history = [_parse_history(event) for event in raw_history]
    last_position = raw.get("last_position")
    if last_position is not None:
        last_position = _path(last_position)
    return ReviewState(
        schema_version="1",
        items=items,
        history=history,
        last_position=last_position,
    )


def _now(now: str | datetime) -> str:
    return _timestamp(now, stored=False)


def _with_history(state: ReviewState, event: HistoryEvent) -> list[HistoryEvent]:
    return [*(state.history or []), event][-REVIEW_HISTORY_LIMIT:]


def decide(
    state: ReviewState,
    path: str,
    action: str,
    reason: str | None,
    sha256: str,
    now: str | datetime,
) -> ReviewState:
    """以最后一次动作为准改判，并追加一条 DECIDE 历史。"""

    current = validate_state(state)
    normalized_path = _path(path, stored=False)
    if action not in _ACTIONS:
        raise PanelError("REQUEST_VALIDATION_FAILED", "审核动作无效。", 422)
    clean_reason = _reason(reason, stored=False)
    clean_sha = _sha256(sha256, stored=False)
    timestamp = _now(now)
    previous = (current.items or {}).get(normalized_path)
    record = ReviewRecord(
        action=action,
        reason=clean_reason,
        content_sha256=clean_sha,
        decided_at=timestamp,
        updated_at=timestamp,
    )
    items = dict(current.items or {})
    items[normalized_path] = record
    event = HistoryEvent(
        path=normalized_path,
        op="DECIDE",
        from_action=previous.action if previous else None,
        to_action=action,
        reason=clean_reason,
        content_sha256=clean_sha,
        at=timestamp,
    )
    return ReviewState(items=items, history=_with_history(current, event), last_position=current.last_position)


def reset(
    state: ReviewState,
    path: str,
    mode: Literal["undo", "stale_reset"] | str,
    now: str | datetime,
) -> ReviewState:
    """删除当前结论、保留前值历史，使该篇重新进入 pending。"""

    current = validate_state(state)
    normalized_path = _path(path, stored=False)
    if mode not in {"undo", "stale_reset"}:
        raise PanelError("REQUEST_VALIDATION_FAILED", "重置模式无效。", 422)
    items = dict(current.items or {})
    previous = items.get(normalized_path)
    if previous is None:
        raise PanelError("REVIEW_ITEM_NOT_DECIDED", "该篇没有可撤销的审核记录。", 404)
    del items[normalized_path]
    timestamp = _now(now)
    event = HistoryEvent(
        path=normalized_path,
        op="UNDO" if mode == "undo" else "STALE_RESET",
        from_action=previous.action,
        to_action=None,
        reason=None,
        content_sha256=previous.content_sha256,
        at=timestamp,
    )
    return ReviewState(items=items, history=_with_history(current, event), last_position=current.last_position)


def review_status_for(record: ReviewRecord | None, current_sha: str) -> str:
    """由持久记录和当前 hash 派生 pending/stale/三种已决状态。"""

    if record is None:
        return "pending"
    if record.content_sha256 != current_sha:
        return "stale"
    return record.action


def compute_progress(listing: Mapping[str, Any], state: ReviewState) -> dict[str, Any]:
    """以当前扫描清单为唯一分母计算单用户进度。"""

    current = validate_state(state)
    counts = {
        "reviewed": 0,
        "approved": 0,
        "rejected": 0,
        "skipped": 0,
        "pending": 0,
        "stale": 0,
        "invalid": 0,
        "pass_rate": None,
    }
    items = listing.get("items", [])
    for item in items:
        if item.get("parse_status") != "ok":
            counts["invalid"] += 1
        record = (current.items or {}).get(item.get("vault_relative_path"))
        status = review_status_for(record, str(item.get("sha256", "")))
        if status in _ACTIONS:
            counts[status] += 1
        elif status == "stale":
            counts["stale"] += 1
    counts["reviewed"] = sum(counts[action] for action in _ACTIONS)
    total = int(listing.get("total", len(items)))
    counts["pending"] = total - counts["reviewed"]
    counts["pass_rate"] = counts["approved"] / counts["reviewed"] if counts["reviewed"] else None
    return counts


def compute_global(
    listing: Mapping[str, Any],
    all_states: Mapping[str, ReviewState],
) -> dict[str, Any]:
    """按篇合并只读全局视图；分歧独立计数，不自动裁决。"""

    states = {user_id: validate_state(state) for user_id, state in all_states.items()}
    global_counts = {"approved": 0, "rejected": 0, "skipped": 0, "diverged": 0, "pending": 0}
    conflicts: list[dict[str, Any]] = []
    conflict_total = 0
    for item in listing.get("items", []):
        path = item.get("vault_relative_path")
        current_sha = str(item.get("sha256", ""))
        decisions: dict[str, str] = {}
        for user_id, state in states.items():
            record = (state.items or {}).get(path)
            if record is not None and record.content_sha256 == current_sha:
                decisions[user_id] = record.action
        actions = sorted(set(decisions.values()))
        if len(actions) > 1:
            global_counts["diverged"] += 1
            conflict_total += 1
            if len(conflicts) < _CONFLICT_SAMPLE_LIMIT:
                conflicts.append({"path": path, "actions": actions, "decisions": decisions})
        elif len(actions) == 1:
            global_counts[actions[0]] += 1
        else:
            global_counts["pending"] += 1
    per_user = [
        {"user_id": user_id, "counts": compute_progress(listing, state)}
        for user_id, state in sorted(states.items())
    ]
    return {
        **global_counts,
        "per_user": per_user,
        "conflicts": conflicts,
        "conflicts_truncated": conflict_total > len(conflicts),
    }


def coverage_by_directory(listing: Mapping[str, Any], state: ReviewState) -> dict[str, dict[str, Any]]:
    """按文件直接父目录计算互不重叠的覆盖率，目录 total 之和等于清单 total。"""

    current = validate_state(state)
    directories: dict[str, dict[str, Any]] = {}
    for item in listing.get("items", []):
        path = str(item.get("vault_relative_path", ""))
        parent = PurePosixPath(path).parent.as_posix()
        directory = "" if parent == "." else parent
        counts = directories.setdefault(
            directory,
            {
                "total": 0,
                "reviewed": 0,
                "approved": 0,
                "rejected": 0,
                "skipped": 0,
                "pending": 0,
                "stale": 0,
                "invalid": 0,
            },
        )
        counts["total"] += 1
        if item.get("parse_status") != "ok":
            counts["invalid"] += 1
        record = (current.items or {}).get(path)
        status = review_status_for(record, str(item.get("sha256", "")))
        if status in _ACTIONS:
            counts[status] += 1
            counts["reviewed"] += 1
        elif status == "stale":
            counts["stale"] += 1
    for counts in directories.values():
        counts["pending"] = counts["total"] - counts["reviewed"]
    return dict(sorted(directories.items(), key=lambda pair: pair[0].casefold()))


def build_export_items(
    listing: Mapping[str, Any],
    state: ReviewState,
    include: Literal["approved", "all"] | str,
    fresh_sha: Callable[[str], str | None],
    *,
    user_id: str = "default",
) -> dict[str, Any]:
    """选择导出项并现算复核；approved 剔除 stale，all 仅标注 stale。"""

    if include not in {"approved", "all"}:
        raise PanelError("REQUEST_VALIDATION_FAILED", "导出范围无效。", 422)
    current = validate_state(state)
    exported: list[dict[str, Any]] = []
    excluded_stale = 0
    approved_total = 0
    for listing_item in listing.get("items", []):
        path = str(listing_item.get("vault_relative_path", ""))
        record = (current.items or {}).get(path)
        if record is None:
            continue
        if record.action == "approved":
            approved_total += 1
        if include == "approved" and record.action != "approved":
            continue
        current_sha = fresh_sha(path)
        stale = current_sha != record.content_sha256
        if include == "approved" and stale:
            excluded_stale += 1
            continue
        exported_item = {
            "vault_relative_path": path,
            "action": record.action,
            "reason": record.reason,
            "content_sha256": record.content_sha256,
            "decided_at": record.decided_at,
            "decided_by": user_id,
            "mtr_state": listing_item.get("mtr_state"),
            "tags": list(listing_item.get("tags", [])),
            "parse_status": listing_item.get("parse_status"),
            "size_bytes": listing_item.get("size_bytes"),
            "mtime": listing_item.get("mtime"),
        }
        if include == "all":
            exported_item["stale"] = stale
        exported.append(exported_item)
    exported.sort(key=lambda item: item["vault_relative_path"].casefold())
    return {
        "items": exported,
        "excluded_stale": excluded_stale,
        "approved_total": approved_total,
    }


__all__ = [
    "HistoryEvent",
    "REVIEW_BATCH_MAX_ITEMS",
    "REVIEW_HISTORY_LIMIT",
    "REVIEW_REASON_MAX_CHARS",
    "ReviewRecord",
    "ReviewState",
    "build_export_items",
    "compute_global",
    "compute_progress",
    "coverage_by_directory",
    "decide",
    "history_event_to_dict",
    "record_to_dict",
    "reset",
    "review_status_for",
    "state_to_dict",
    "validate_state",
]
