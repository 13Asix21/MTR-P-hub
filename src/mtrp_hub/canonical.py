"""The sole canonical JSON, SHA-256, and deterministic identifier implementation."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from enum import Enum
from typing import Any

from pydantic import BaseModel


def canonical_data(value: Any) -> Any:
    """Recursively convert models and ordered containers to JSON-safe data."""
    if isinstance(value, BaseModel):
        return canonical_data(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): canonical_data(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_data(child) for child in value]
    if isinstance(value, (set, frozenset)):
        raise TypeError("Unordered sets are not valid canonical JSON input")
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return canonical_data(value.model_dump(mode="json"))
    return value


def canonical_json(value: Any) -> str:
    """Serialize a value as sorted, compact UTF-8 JSON without a trailing newline."""
    return json.dumps(
        canonical_data(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    """Hash exact bytes, used for immutable fixture text contracts."""
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    """Hash a string as UTF-8 after NFC normalization."""
    normalized = unicodedata.normalize("NFC", value)
    return sha256_bytes(normalized.encode("utf-8"))


def sha256_json(value: Any) -> str:
    """Hash canonical JSON bytes."""
    return sha256_text(canonical_json(value))


def stable_id(prefix: str, value: Any) -> str:
    """Derive a stable, non-random identifier from canonical content."""
    return f"{prefix}{sha256_json(value)}"


def request_fingerprint(
    *,
    query: str,
    intent: str,
    project_id: str | None,
    repo_id: str | None,
    workspace_id: str | None,
    include_r: bool,
    registered_ids: bool,
) -> str:
    """Hash query text and coordinates without persisting the original query.

    Rejected requests contribute only supplied/not-supplied flags for coordinate
    fields, so malicious paths and URLs never enter the hash input or audit log.
    """
    coordinate_payload: dict[str, str | bool | None]
    if registered_ids:
        coordinate_payload = {
            "project_id": project_id,
            "repo_id": repo_id,
            "workspace_id": workspace_id,
        }
    else:
        coordinate_payload = {
            "project_supplied": project_id is not None,
            "repo_supplied": repo_id is not None,
            "workspace_supplied": workspace_id is not None,
        }
    return sha256_json(
        {
            "query": unicodedata.normalize("NFC", query),
            "intent": intent,
            "include_r": include_r,
            **coordinate_payload,
        }
    )
