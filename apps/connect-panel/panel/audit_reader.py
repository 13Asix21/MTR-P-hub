"""Decision Log 的统一只读入口；绝不读取原始请求或响应载荷。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .settings import PanelError


_EVENT_COLUMNS = """
    event_seq, run_seq, recorded_at, event_type, step_id, route_step_id,
    store, operation, status, reason_code, resource_count, evidence_count,
    skipped_count, tool_call_count, duration_ms, receipt_hash, response_sha256
"""
_RUN_COLUMNS = """
    run_seq, started_at, finished_at, request_id, route_id, execution_id,
    trace_id, manifest_hash, mode, status, reason_code, next_step_id,
    resource_count, evidence_count, step_count, duration_ms
"""


class AuditReader:
    """以 mode=ro、immutable 和 query_only 保证不写入数据库或 WAL 共享内存。"""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def _ensure_wal_quiescent(self) -> None:
        """活动 WAL 需要 SHM 写入才能安全读取，此时选择诚实降级。"""

        try:
            wal_path = Path(str(self._database_path) + "-wal")
            if wal_path.is_file() and wal_path.stat().st_size > 0:
                raise PanelError(
                    "AUDIT_WAL_ACTIVE",
                    "审计数据库存在活动 WAL；为避免写入共享内存或展示陈旧证据，本次未读取。",
                    503,
                )
        except PanelError:
            raise
        except OSError as exc:
            raise PanelError("AUDIT_OPEN_FAILED", "审计数据库状态无法安全确认。", 503) from exc

    def _connect(self) -> sqlite3.Connection:
        if not self._database_path.is_file():
            raise PanelError("AUDIT_NOT_FOUND", "审计数据库不存在，暂无执行证据。", 503)
        self._ensure_wal_quiescent()
        try:
            uri = self._database_path.resolve().as_uri() + "?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True, timeout=2.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            return connection
        except (OSError, sqlite3.Error) as exc:
            raise PanelError("AUDIT_OPEN_FAILED", "审计数据库无法以不可变只读方式打开。", 503) from exc

    @staticmethod
    def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
        return [dict(row) for row in cursor.fetchall()]

    def recent_events(self, limit: int = 10) -> list[dict[str, Any]]:
        """返回最近的审计事件，按事件序号倒序。"""

        try:
            with closing(self._connect()) as connection:
                cursor = connection.execute(
                    f"SELECT {_EVENT_COLUMNS} FROM stage2_execution_event "
                    "ORDER BY event_seq DESC LIMIT ?",
                    (limit,),
                )
                rows = self._rows(cursor)
                self._ensure_wal_quiescent()
                return rows
        except PanelError:
            raise
        except sqlite3.Error as exc:
            raise PanelError("AUDIT_READ_FAILED", "审计事件读取失败或表结构不受支持。", 503) from exc

    def latest_by_store(self) -> dict[str, dict[str, Any]]:
        """返回 M/T/R/P 各自序号最大的一条事件。"""

        try:
            with closing(self._connect()) as connection:
                cursor = connection.execute(
                    f"SELECT {_EVENT_COLUMNS} FROM stage2_execution_event AS event "
                    "WHERE event.store IN ('M', 'T', 'R', 'P') "
                    "AND event.event_seq = ("
                    "SELECT MAX(peer.event_seq) FROM stage2_execution_event AS peer "
                    "WHERE peer.store = event.store) "
                    "ORDER BY event.store"
                )
                rows = self._rows(cursor)
                self._ensure_wal_quiescent()
        except PanelError:
            raise
        except sqlite3.Error as exc:
            raise PanelError("AUDIT_READ_FAILED", "分库审计证据读取失败或表结构不受支持。", 503) from exc
        return {str(row["store"]): row for row in rows if row.get("store")}

    def latest_run(self) -> dict[str, Any] | None:
        """返回最近一次执行 run 的白名单字段。"""

        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    f"SELECT {_RUN_COLUMNS} FROM stage2_execution_run "
                    "ORDER BY run_seq DESC LIMIT 1"
                ).fetchone()
                result = dict(row) if row is not None else None
                self._ensure_wal_quiescent()
                return result
        except PanelError:
            raise
        except sqlite3.Error as exc:
            raise PanelError("AUDIT_READ_FAILED", "最近执行记录读取失败或表结构不受支持。", 503) from exc

    def summary(self) -> dict[str, Any]:
        """聚合 M 卡片所需的分库证据和最近 run。"""

        latest_events = self.latest_by_store()
        return {
            "available": True,
            "snapshot_mode": "immutable-main-db-no-active-wal",
            "latest_by_store": latest_events,
            "latest_run": self.latest_run(),
            "m_event_count": 1 if "M" in latest_events else 0,
        }


__all__ = ["AuditReader"]
