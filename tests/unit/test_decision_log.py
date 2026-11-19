"""Decision Log whitelist, transactional persistence, and lookup tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from mtrp_hub.audit.models import DecisionEventType, DecisionPayload, DecisionRecord
from mtrp_hub.audit.sqlite import SQLiteDecisionLog
from mtrp_hub.errors import MtrpError


def record(request_hash: str, plan_hash: str | None = None) -> DecisionRecord:
    return DecisionRecord(
        event_type=DecisionEventType.PLAN_CREATED,
        request_hash=request_hash,
        plan_hash=plan_hash,
        status="planned",
        payload=DecisionPayload(rule_version="v1", resource_count=0),
    )


def test_append_and_find_by_request_and_plan_hash(tmp_path: Path) -> None:
    path = tmp_path / ".mtrp" / "decision-log.sqlite3"
    log = SQLiteDecisionLog(path)
    req = "1" * 64
    plan = "2" * 64
    log.append_many((record(req, plan), record(req, plan)))
    assert len(log.find(request_hash=req)) == 2
    assert len(log.find(plan_hash=plan)) == 2
    assert log.find(request_hash="3" * 64) == ()


def test_find_requires_exactly_one_full_hash(tmp_path: Path) -> None:
    log = SQLiteDecisionLog(tmp_path / "log.sqlite3")
    with pytest.raises(MtrpError):
        log.find()
    with pytest.raises(MtrpError):
        log.find(request_hash="short")


def test_payload_has_no_query_body_secret_or_url_field() -> None:
    with pytest.raises(ValidationError):
        DecisionPayload.model_validate({"query": "token=secret", "rule_version": "v1"})
    with pytest.raises(ValidationError):
        DecisionPayload.model_validate({"remote": "https://example.invalid/private"})


def test_sqlite_contains_only_canonical_whitelist_payload(tmp_path: Path) -> None:
    path = tmp_path / "log.sqlite3"
    log = SQLiteDecisionLog(path)
    log.append(record("1" * 64))
    with sqlite3.connect(path) as connection:
        payload, digest = connection.execute("SELECT payload_json, payload_sha256 FROM decision_log").fetchone()
    assert "query" not in payload
    assert "token" not in payload
    assert len(digest) == 64


def test_missing_read_only_log_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "decision-log.sqlite3"
    with pytest.raises(MtrpError) as caught:
        SQLiteDecisionLog(path, create=False)
    assert caught.value.error_code == "DECISION_LOG_NOT_FOUND"
    assert not path.exists()


def test_read_only_find_uses_actual_sqlite_read_only_mode(tmp_path: Path) -> None:
    path = tmp_path / "log.sqlite3"
    SQLiteDecisionLog(path).append(record("1" * 64))
    read_only = SQLiteDecisionLog(path, create=False)
    assert len(read_only.find(request_hash="1" * 64)) == 1
    with pytest.raises(MtrpError) as caught:
        read_only.append(record("2" * 64))
    assert caught.value.error_code == "DECISION_LOG_WRITE_FAILED"
