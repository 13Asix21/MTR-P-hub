"""Compatible additive stage 2 SQLite audit tables and trace queries."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from mtrp_hub.audit.models_stage2 import Stage2AuditPayload, Stage2ExecutionEvent, Stage2ExecutionRun, TraceRunRecord
from mtrp_hub.canonical import canonical_json, sha256_text
from mtrp_hub.domain.requests_v2 import QueryResponseV2
from mtrp_hub.errors import MtrpError, internal_error
from mtrp_hub.execution.envelope import ExecutionEnvelope
from mtrp_hub.execution.models import ExecutionManifest, Stage2RoutePlan

_ID_PATTERNS = {
    "request_id": re.compile(r"^req_[0-9a-f]{64}$"),
    "route_id": re.compile(r"^route_[0-9a-f]{64}$"),
    "execution_id": re.compile(r"^exe_[0-9a-f]{64}$"),
    "trace_id": re.compile(r"^trace_[0-9a-f]{64}$"),
}


class Stage2DecisionLog:
    """Persist only safe DTOs; execution_id deliberately is not unique."""

    def __init__(self, path: str | Path, *, create: bool = True) -> None:
        self._path = Path(path)
        self._read_only = not create
        if not create and not self._path.is_file():
            raise MtrpError("DECISION_LOG_NOT_FOUND", "Decision Log does not exist.", field="config_dir")
        try:
            if create:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._initialize()
        except MtrpError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise internal_error("DECISION_LOG_WRITE_FAILED") from exc

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            connection = sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if not self._read_only:
            connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > 2:
                raise internal_error("DECISION_LOG_WRITE_FAILED")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS stage2_plan_log (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    profile_hash TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason_code TEXT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stage2_execution_run (
                    run_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NULL,
                    request_id TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason_code TEXT NULL,
                    next_step_id TEXT NULL,
                    last_continuation_hash TEXT NOT NULL,
                    resource_count INTEGER NOT NULL DEFAULT 0,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    step_count INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS stage2_execution_event (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_seq INTEGER NOT NULL REFERENCES stage2_execution_run(run_seq),
                    recorded_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    step_id TEXT NULL,
                    route_step_id TEXT NULL,
                    store TEXT NULL,
                    operation TEXT NULL,
                    status TEXT NOT NULL,
                    reason_code TEXT NULL,
                    next_step_id TEXT NULL,
                    resource_count INTEGER NOT NULL DEFAULT 0,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    tool_call_count INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    receipt_hash TEXT NULL,
                    response_sha256 TEXT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_stage2_request ON stage2_execution_run(request_id, run_seq);
                CREATE INDEX IF NOT EXISTS idx_stage2_route ON stage2_execution_run(route_id, run_seq);
                CREATE INDEX IF NOT EXISTS idx_stage2_execution ON stage2_execution_run(execution_id, run_seq);
                CREATE INDEX IF NOT EXISTS idx_stage2_trace ON stage2_execution_run(trace_id, run_seq);
                CREATE INDEX IF NOT EXISTS idx_stage2_continuation ON stage2_execution_run(execution_id, last_continuation_hash, status);
                PRAGMA user_version=2;
                """
            )

    def record_plan(self, manifest: ExecutionManifest, route: Stage2RoutePlan | None = None) -> None:
        coordinate = route.v1_plan.project_coordinate if route else None
        payload = Stage2AuditPayload(
            intent=route.v1_plan.intent.value if route else None,
            rule_version=route.v1_plan.rule_version if route else None,
            mode=manifest.mode.value,
            project_id=coordinate.project_id if coordinate else None,
            repo_id=coordinate.repo_id if coordinate else None,
            location_id=coordinate.location_id if coordinate else None,
            ref=coordinate.ref if coordinate else None,
        )
        payload_json = canonical_json(payload)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO stage2_plan_log (
                        recorded_at, request_id, route_id, execution_id, request_hash,
                        plan_hash, manifest_hash, profile_hash, mode, status,
                        reason_code, payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', NULL, ?, ?)
                    """,
                    (
                        self._now(), manifest.request_id, manifest.route_id, manifest.execution_id,
                        manifest.request_hash, manifest.plan_hash, manifest.manifest_hash,
                        manifest.profile_hash, manifest.mode.value, payload_json, sha256_text(payload_json),
                    ),
                )
        except sqlite3.Error as exc:
            raise internal_error("DECISION_LOG_WRITE_FAILED") from exc

    def begin_run(self, envelope: ExecutionEnvelope) -> Stage2ExecutionRun:
        try:
            with closing(self._connect()) as connection, connection:
                duplicates = connection.execute(
                    "SELECT COUNT(*) FROM stage2_execution_run WHERE execution_id=? AND last_continuation_hash=? AND status='IN_PROGRESS'",
                    (envelope.execution_id, envelope.continuation_hash),
                ).fetchone()[0]
                if duplicates:
                    raise internal_error("EXECUTION_RUN_AMBIGUOUS")
                cursor = connection.execute(
                    """
                    INSERT INTO stage2_execution_run (
                        started_at, request_id, route_id, execution_id, trace_id,
                        manifest_hash, mode, status, next_step_id, last_continuation_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'IN_PROGRESS', ?, ?)
                    """,
                    (
                        self._now(), envelope.request_id, envelope.route_id, envelope.execution_id,
                        envelope.trace_id, envelope.manifest_hash, envelope.mode.value,
                        envelope.next_step_id, envelope.continuation_hash,
                    ),
                )
                run_seq = int(cursor.lastrowid)
        except MtrpError:
            raise
        except sqlite3.Error as exc:
            raise internal_error("DECISION_LOG_WRITE_FAILED") from exc
        return Stage2ExecutionRun(
            run_seq=run_seq,
            request_id=envelope.request_id,
            route_id=envelope.route_id,
            execution_id=envelope.execution_id,
            trace_id=envelope.trace_id,
            status="IN_PROGRESS",
            next_step_id=envelope.next_step_id,
            last_continuation_hash=envelope.continuation_hash,
        )

    def continue_run(self, envelope: ExecutionEnvelope) -> Stage2ExecutionRun:
        try:
            with closing(self._connect()) as connection, connection:
                rows = connection.execute(
                    "SELECT run_seq FROM stage2_execution_run WHERE execution_id=? AND last_continuation_hash=? AND status='IN_PROGRESS'",
                    (envelope.execution_id, envelope.continuation_hash),
                ).fetchall()
                if not rows:
                    # WorkBuddy may already have advanced a previously returned
                    # continuation before this process receives it. Stage 2 permits
                    # only one IN_PROGRESS run for an execution, so identity plus
                    # the fully verified prefix safely selects that run; concurrency
                    # remains explicitly ambiguous and fails closed.
                    rows = connection.execute(
                        "SELECT run_seq FROM stage2_execution_run WHERE execution_id=? AND status='IN_PROGRESS'",
                        (envelope.execution_id,),
                    ).fetchall()
        except sqlite3.Error as exc:
            raise internal_error("DECISION_LOG_READ_FAILED") from exc
        if len(rows) > 1:
            raise internal_error("EXECUTION_RUN_AMBIGUOUS")
        if not rows:
            raise internal_error("EXECUTION_RUN_MISMATCH")
        return Stage2ExecutionRun(
            run_seq=int(rows[0][0]),
            request_id=envelope.request_id,
            route_id=envelope.route_id,
            execution_id=envelope.execution_id,
            trace_id=envelope.trace_id,
            status="IN_PROGRESS",
            next_step_id=envelope.next_step_id,
            last_continuation_hash=envelope.continuation_hash,
        )

    def update_continuation(self, run: Stage2ExecutionRun, envelope: ExecutionEnvelope) -> Stage2ExecutionRun:
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "UPDATE stage2_execution_run SET next_step_id=?, last_continuation_hash=? WHERE run_seq=? AND status='IN_PROGRESS'",
                    (envelope.next_step_id, envelope.continuation_hash, run.run_seq),
                )
                self._insert_event(connection, run.run_seq, "CONTINUATION", "IN_PROGRESS", None, envelope.next_step_id, Stage2AuditPayload(next_step_id=envelope.next_step_id))
        except sqlite3.Error as exc:
            raise internal_error("DECISION_LOG_WRITE_FAILED") from exc
        return run.model_copy(update={"next_step_id": envelope.next_step_id, "last_continuation_hash": envelope.continuation_hash})

    def finish_run(
        self,
        run: Stage2ExecutionRun,
        response: QueryResponseV2,
        envelope: ExecutionEnvelope | None = None,
        manifest: ExecutionManifest | None = None,
    ) -> None:
        duration = sum(call.duration_ms for receipt in envelope.receipts for call in receipt.calls) if envelope else 0
        receipts = {receipt.step_id: receipt for receipt in envelope.receipts} if envelope else {}
        manifest_steps = {step.step_id: step for step in manifest.steps} if manifest else {}
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    UPDATE stage2_execution_run SET finished_at=?, status=?, reason_code=?,
                        next_step_id=NULL, resource_count=?, evidence_count=?, step_count=?, duration_ms=?
                    WHERE run_seq=? AND status='IN_PROGRESS'
                    """,
                    (
                        self._now(), response.status.value,
                        next((item.reason_code for item in response.step_results if item.status.value in ("DEGRADED", "REJECTED")), None),
                        len(response.result.resources), len(response.result.evidence), len(response.step_results), duration, run.run_seq,
                    ),
                )
                for step in response.step_results:
                    payload = Stage2AuditPayload(
                        step_id=step.step_id,
                        store=step.store,
                        next_step_id=step.next_step_id,
                        resource_count=step.resource_count,
                        evidence_count=step.evidence_count,
                        skipped_count=step.skipped_count,
                    )
                    self._insert_event(connection, run.run_seq, "STEP_FINISHED", step.status.value, step.reason_code, step.next_step_id, payload, step=step)
                    for operation in step.operations:
                        receipt = receipts.get(operation.step_id)
                        response_hash = next((call.response_sha256 for call in receipt.calls if call.response_sha256), None) if receipt else None
                        operation_duration = sum(call.duration_ms for call in receipt.calls) if receipt else 0
                        operation_name = manifest_steps[operation.step_id].operation.value if operation.step_id in manifest_steps else operation.step_id
                        operation_payload = Stage2AuditPayload(
                            step_id=operation.step_id,
                            route_step_id=step.step_id,
                            store=step.store,
                            operation=operation_name,
                            resource_count=operation.resource_count,
                            skipped_count=operation.skipped_count,
                            tool_call_count=operation.tool_call_count,
                            score_available_count=operation.score_available_count,
                        )
                        self._insert_event(
                            connection,
                            run.run_seq,
                            "OPERATION_FINISHED",
                            operation.status.value,
                            operation.reason_code,
                            None,
                            operation_payload,
                            step_id=operation.step_id,
                            route_step_id=step.step_id,
                            store=step.store.value,
                            operation=operation_name,
                            resource_count=operation.resource_count,
                            skipped_count=operation.skipped_count,
                            tool_call_count=operation.tool_call_count,
                            duration_ms=operation_duration,
                            receipt_hash=operation.receipt_hash,
                            response_sha256=response_hash,
                        )
                self._insert_event(
                    connection,
                    run.run_seq,
                    "QUERY_FINISHED",
                    response.status.value,
                    None,
                    None,
                    Stage2AuditPayload(resource_count=len(response.result.resources), evidence_count=len(response.result.evidence)),
                    resource_count=len(response.result.resources),
                    evidence_count=len(response.result.evidence),
                    duration_ms=duration,
                )
        except sqlite3.Error as exc:
            raise internal_error("DECISION_LOG_WRITE_FAILED") from exc

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        run_seq: int,
        event_type: str,
        status: str,
        reason_code: str | None,
        next_step_id: str | None,
        payload: Stage2AuditPayload,
        **overrides,
    ) -> None:
        step = overrides.get("step")
        payload_json = canonical_json(payload)
        connection.execute(
            """
            INSERT INTO stage2_execution_event (
                run_seq, recorded_at, event_type, step_id, route_step_id, store,
                operation, status, reason_code, next_step_id, resource_count,
                evidence_count, skipped_count, tool_call_count, duration_ms,
                receipt_hash, response_sha256, payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_seq, self._now(), event_type,
                overrides.get("step_id", step.step_id if step else None),
                overrides.get("route_step_id", step.step_id if step else None),
                overrides.get("store", step.store.value if step else None),
                overrides.get("operation"), status, reason_code, next_step_id,
                overrides.get("resource_count", step.resource_count if step else 0),
                overrides.get("evidence_count", step.evidence_count if step else 0),
                overrides.get("skipped_count", step.skipped_count if step else 0),
                overrides.get("tool_call_count", 0), overrides.get("duration_ms", 0),
                overrides.get("receipt_hash"), overrides.get("response_sha256"),
                payload_json, sha256_text(payload_json),
            ),
        )

    def find(
        self,
        request_id: str | None = None,
        route_id: str | None = None,
        execution_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[TraceRunRecord, ...]:
        supplied = {"request_id": request_id, "route_id": route_id, "execution_id": execution_id, "trace_id": trace_id}
        selected = [(key, value) for key, value in supplied.items() if value is not None]
        if len(selected) != 1:
            raise MtrpError("INVALID_INPUT", "Provide exactly one stage 2 trace identity.", field="request_id")
        column, value = selected[0]
        if not _ID_PATTERNS[column].fullmatch(value):
            raise MtrpError("INVALID_INPUT", "Trace identity has an invalid format.", field=column)
        try:
            with closing(self._connect()) as connection, connection:
                runs = connection.execute(f"SELECT * FROM stage2_execution_run WHERE {column}=? ORDER BY run_seq", (value,)).fetchall()
                result = tuple(self._trace_record(connection, row) for row in runs)
        except sqlite3.Error as exc:
            raise internal_error("DECISION_LOG_READ_FAILED") from exc
        return result

    def _trace_record(self, connection: sqlite3.Connection, row: sqlite3.Row) -> TraceRunRecord:
        event_rows = connection.execute("SELECT * FROM stage2_execution_event WHERE run_seq=? ORDER BY event_seq", (row["run_seq"],)).fetchall()
        events: list[Stage2ExecutionEvent] = []
        for event in event_rows:
            if sha256_text(event["payload_json"]) != event["payload_sha256"]:
                raise internal_error("DECISION_LOG_READ_FAILED")
            events.append(
                Stage2ExecutionEvent(
                    event_seq=event["event_seq"], run_seq=event["run_seq"], recorded_at=event["recorded_at"],
                    event_type=event["event_type"], step_id=event["step_id"], route_step_id=event["route_step_id"],
                    store=event["store"], operation=event["operation"], status=event["status"], reason_code=event["reason_code"],
                    next_step_id=event["next_step_id"], resource_count=event["resource_count"], evidence_count=event["evidence_count"],
                    skipped_count=event["skipped_count"], tool_call_count=event["tool_call_count"], duration_ms=event["duration_ms"],
                    receipt_hash=event["receipt_hash"], response_sha256=event["response_sha256"],
                    payload=Stage2AuditPayload.model_validate_json(event["payload_json"]),
                )
            )
        return TraceRunRecord(
            run_seq=row["run_seq"], started_at=row["started_at"], finished_at=row["finished_at"],
            request_id=row["request_id"], route_id=row["route_id"], execution_id=row["execution_id"], trace_id=row["trace_id"],
            manifest_hash=row["manifest_hash"], mode=row["mode"], status=row["status"], reason_code=row["reason_code"],
            next_step_id=row["next_step_id"], last_continuation_hash=row["last_continuation_hash"],
            resource_count=row["resource_count"], evidence_count=row["evidence_count"], step_count=row["step_count"],
            duration_ms=row["duration_ms"], events=tuple(events),
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


__all__ = ["Stage2DecisionLog"]
