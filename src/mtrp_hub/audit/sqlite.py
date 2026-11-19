"""SQLite Decision Log with transactional append and hash-based read queries."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from mtrp_hub.audit.models import DecisionPayload, DecisionRecord
from mtrp_hub.canonical import canonical_json, sha256_text
from mtrp_hub.errors import MtrpError, internal_error

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class SQLiteDecisionLog:
    """Persist only DecisionRecord DTOs; never serialize requests or exceptions."""

    def __init__(self, path: str | Path, *, create: bool = True) -> None:
        self._path = Path(path)
        self._read_only = not create
        if not create and not self._path.is_file():
            raise MtrpError("DECISION_LOG_NOT_FOUND", "Decision Log does not exist.", field="config_dir")
        try:
            if create:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._initialize()
        except (OSError, sqlite3.Error) as exc:
            raise internal_error("DECISION_LOG_WRITE_FAILED") from exc

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            uri = self._path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
        else:
            connection = sqlite3.connect(self._path)
        connection.execute("PRAGMA foreign_keys=ON")
        if not self._read_only:
            connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS decision_log (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    plan_hash TEXT NULL,
                    status TEXT NOT NULL,
                    reason_code TEXT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decision_request ON decision_log(request_hash, seq);
                CREATE INDEX IF NOT EXISTS idx_decision_plan ON decision_log(plan_hash, seq);
                """
            )

    def append(self, record: DecisionRecord) -> None:
        """Append one event in a transaction."""
        self.append_many((record,))

    def append_many(self, records: Iterable[DecisionRecord]) -> None:
        """Append all events for one request in a single transaction."""
        materialized = tuple(records)
        if not materialized:
            return
        rows = []
        for record in materialized:
            payload_json = canonical_json(record.payload)
            rows.append(
                (
                    datetime.now(timezone.utc).isoformat(),
                    record.event_type.value,
                    record.request_hash,
                    record.plan_hash,
                    record.status,
                    record.reason_code,
                    payload_json,
                    sha256_text(payload_json),
                )
            )
        try:
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO decision_log (
                        recorded_at, event_type, request_hash, plan_hash,
                        status, reason_code, payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        except (OSError, sqlite3.Error) as exc:
            raise internal_error("DECISION_LOG_WRITE_FAILED") from exc

    def find(
        self,
        request_hash: str | None = None,
        plan_hash: str | None = None,
    ) -> tuple[DecisionRecord, ...]:
        """Read events by exactly one full request or plan hash, ordered by sequence."""
        if (request_hash is None) == (plan_hash is None):
            raise MtrpError("INVALID_INPUT", "Provide exactly one request or plan hash.", field="request_hash")
        selected = request_hash if request_hash is not None else plan_hash
        if selected is None or not _HASH_RE.fullmatch(selected):
            raise MtrpError("INVALID_INPUT", "Hash must be a full lowercase SHA-256 value.", field="request_hash")
        column = "request_hash" if request_hash is not None else "plan_hash"
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT event_type, request_hash, plan_hash, status, reason_code,
                           payload_json, payload_sha256
                    FROM decision_log WHERE {column} = ? ORDER BY seq
                    """,
                    (selected,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise internal_error("DECISION_LOG_READ_FAILED") from exc
        records: list[DecisionRecord] = []
        for event_type, req_hash, p_hash, status, reason, payload_json, payload_hash in rows:
            if sha256_text(payload_json) != payload_hash:
                raise internal_error("DECISION_LOG_READ_FAILED")
            payload = DecisionPayload.model_validate_json(payload_json)
            records.append(
                DecisionRecord(
                    event_type=event_type,
                    request_hash=req_hash,
                    plan_hash=p_hash,
                    status=status,
                    reason_code=reason,
                    payload=payload,
                )
            )
        return tuple(records)
