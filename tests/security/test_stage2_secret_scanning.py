"""Canary scanning across SQLite, traces, Canonical output, and repeated runs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mtrp_hub.application_stage2 import QueryFinalizationService
from mtrp_hub.audit.sqlite import SQLiteDecisionLog
from mtrp_hub.audit.sqlite_stage2 import Stage2DecisionLog
from mtrp_hub.canonical import canonical_json
from mtrp_hub.domain.requests_v2 import response_payload_v2
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader
from mtrp_hub.fixtures.stage2_schemas import Stage2Fixture

from tests.conftest import ROOT


def test_query_body_title_remote_url_and_auth_canaries_never_enter_audit(request_factory, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    original = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-project-context.json")
    data = original.model_dump(mode="json")
    data["responses"]["github_resolve_ref"][0]["response"]["data"]["html_url"] = "https://remote-url-canary.invalid/private"
    data["responses"]["github_resolve_ref"][0]["response"]["data"]["authorization"] = "Bearer auth-canary"
    fixture = Stage2Fixture.model_validate(data)
    SQLiteDecisionLog(config_dir / ".mtrp" / "decision-log.sqlite3")
    query = "raw-query-canary token=query-secret-canary"
    request = request_factory(query=query)
    response = QueryFinalizationService().run_fixture(request, fixture)
    canonical = canonical_json(response_payload_v2(response))
    assert query not in canonical and "query-secret-canary" not in canonical
    assert "remote-url-canary" not in canonical and "auth-canary" not in canonical
    database = config_dir / ".mtrp" / "decision-log.sqlite3"
    with sqlite3.connect(database) as connection:
        rows: list[str] = []
        for table in ("stage2_plan_log", "stage2_execution_run", "stage2_execution_event"):
            rows.extend(str(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall())
        old_columns = [row[1] for row in connection.execute("PRAGMA table_info(decision_log)").fetchall()]
    assert old_columns == ["seq", "recorded_at", "event_type", "request_hash", "plan_hash", "status", "reason_code", "payload_json", "payload_sha256"]
    audit_text = "\n".join(rows)
    for canary in ("raw-query-canary", "query-secret-canary", "remote-url-canary", "auth-canary", "README fixture"):
        assert canary not in audit_text
    traces = Stage2DecisionLog(database, create=False).find(trace_id=response.trace_id)
    assert len(traces) == 1
    assert all(canary not in canonical_json(traces) for canary in ("raw-query-canary", "remote-url-canary", "auth-canary"))


def test_repeated_execution_uses_run_seq_without_changing_canonical_bytes(request_factory, config_dir: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    fixture = Stage2FixtureLoader().load("tests/fixtures/stage2/stage2-decision-history.json")
    request = request_factory(intent="decision_history")
    service = QueryFinalizationService()
    first = service.run_fixture(request, fixture)
    second = service.run_fixture(request, fixture)
    assert canonical_json(response_payload_v2(first)) == canonical_json(response_payload_v2(second))
    records = Stage2DecisionLog(config_dir / ".mtrp" / "decision-log.sqlite3", create=False).find(trace_id=first.trace_id)
    assert len(records) == 2
    assert [item.run_seq for item in records] == sorted(item.run_seq for item in records)
    assert records[0].run_seq != records[1].run_seq
