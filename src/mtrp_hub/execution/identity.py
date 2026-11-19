"""Deterministic stage 2 identity derivation; no clock, randomness, or SQLite state."""

from __future__ import annotations

from mtrp_hub.canonical import sha256_json, stable_id


def request_id(request_hash: str) -> str:
    return f"req_{request_hash}"


def route_id(plan_hash: str) -> str:
    return f"route_{plan_hash}"


def execution_id(manifest_hash: str) -> str:
    return f"exe_{manifest_hash}"


def trace_id(*, request_id_value: str, route_id_value: str, execution_id_value: str) -> str:
    return stable_id(
        "trace_",
        {
            "request_id": request_id_value,
            "route_id": route_id_value,
            "execution_id": execution_id_value,
        },
    )


def manifest_digest(hashed_payload: object) -> str:
    """Hash the exact canonical payload supplied by ExecutionManifest."""
    return sha256_json(hashed_payload)


def resource_id(payload: object) -> str:
    return stable_id("res_", payload)


def evidence_id(payload: object) -> str:
    return stable_id("ev_", payload)


def content_ref(payload: object) -> str:
    return stable_id("cnt_", payload)
