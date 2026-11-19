"""Canonical JSON and deterministic hash golden tests."""

from __future__ import annotations

import hashlib

import pytest

from mtrp_hub.canonical import canonical_json, request_fingerprint, sha256_json, stable_id


def test_canonical_json_is_sorted_compact_unicode_and_keeps_null() -> None:
    value = {"z": None, "a": "路由", "nested": {"b": 2, "a": 1}}
    assert canonical_json(value) == '{"a":"路由","nested":{"a":1,"b":2},"z":null}'
    assert "\n" not in canonical_json(value)


def test_sha256_golden_and_stable_id() -> None:
    expected = hashlib.sha256(b'{"a":1}').hexdigest()
    assert sha256_json({"a": 1}) == expected
    assert stable_id("id-", {"a": 1}) == f"id-{expected}"


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(ValueError):
        canonical_json({"invalid": float("nan")})


def test_request_hash_uses_query_but_never_exposes_it() -> None:
    first = request_fingerprint(
        query="secret https://example.invalid/private",
        intent="general_knowledge",
        project_id=None,
        repo_id=None,
        workspace_id=None,
        include_r=False,
        registered_ids=True,
    )
    second = request_fingerprint(
        query="different",
        intent="general_knowledge",
        project_id=None,
        repo_id=None,
        workspace_id=None,
        include_r=False,
        registered_ids=True,
    )
    assert first != second
    assert len(first) == 64


def test_rejected_request_hash_does_not_embed_malicious_coordinate() -> None:
    kwargs = {
        "query": "same",
        "intent": "project_context",
        "project_id": "https://evil.invalid/repo",
        "repo_id": "example-repo",
        "workspace_id": "E:/private/repo",
        "include_r": False,
        "registered_ids": False,
    }
    first = request_fingerprint(**kwargs)
    kwargs["project_id"] = "completely-different-untrusted-value"
    kwargs["workspace_id"] = "/another/private/path"
    assert request_fingerprint(**kwargs) == first
