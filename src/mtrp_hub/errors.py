"""Safe, classified errors that never echo untrusted sensitive values."""

from __future__ import annotations

import re
from typing import Any


_SAFE_FIELD_COMPONENT = re.compile(r"^(?:[a-z_][a-z0-9_]*|[0-9]+)$")

# Stage 2 codes are constants so callers never propagate external error text.
STAGE2_CONFIG_NOT_FOUND = "STAGE2_CONFIG_NOT_FOUND"
STAGE2_CONFIG_INVALID = "STAGE2_CONFIG_INVALID"
MANIFEST_INVALID = "MANIFEST_INVALID"
MANIFEST_HASH_MISMATCH = "MANIFEST_HASH_MISMATCH"
MANIFEST_IDENTITY_MISMATCH = "MANIFEST_IDENTITY_MISMATCH"
MANIFEST_CONTEXT_CHANGED = "MANIFEST_CONTEXT_CHANGED"
MANIFEST_INTEGRITY_FAILED = "MANIFEST_INTEGRITY_FAILED"
TOOL_FORBIDDEN = "TOOL_FORBIDDEN"
ARGUMENT_FORBIDDEN = "ARGUMENT_FORBIDDEN"
ARGUMENT_SCHEMA_INVALID = "ARGUMENT_SCHEMA_INVALID"
BINDING_INVALID = "BINDING_INVALID"
BINDING_NOT_EARLIER = "BINDING_NOT_EARLIER"
BINDING_FIELD_FORBIDDEN = "BINDING_FIELD_FORBIDDEN"
BINDING_TYPE_INVALID = "BINDING_TYPE_INVALID"
BINDING_EMPTY = "BINDING_EMPTY"
UPSTREAM_BINDING_FAILED = "UPSTREAM_BINDING_FAILED"
RESPONSE_CONTRACT_INVALID = "RESPONSE_CONTRACT_INVALID"
ENVELOPE_INVALID = "ENVELOPE_INVALID"
RECEIPT_INTEGRITY_FAILED = "RECEIPT_INTEGRITY_FAILED"
PATH_ESCAPE = "PATH_ESCAPE"
VAULT_PARTIAL_FAILURE = "VAULT_PARTIAL_FAILURE"
DECISION_LOG_WRITE_FAILED = "DECISION_LOG_WRITE_FAILED"

# Only these non-sensitive values may cross receipt/Canonical/audit boundaries.
SAFE_REASON_CODES = frozenset(
    {
        "M_EMPTY", "M_TEXT_EMPTY", "M_VECTOR_EMPTY", "M_ENTRIES_INVALID", "M_ENTRY_SKIPPED",
        "P_EMPTY", "ADR_MISSING", "R_EMPTY", "T_EMPTY", "EMPTY_REPOSITORY",
        "SPACE_MISMATCH", "SCORE_UNAVAILABLE", "BRANCH_NOT_FOUND", "REF_UNRESOLVED",
        "REF_SNAPSHOT_MISMATCH", "FILE_READ_FAILED", "P_PARTIAL_FAILURE",
        "PERMISSION_DENIED", "RATE_LIMITED", "TIMEOUT", "NOT_FOUND", "UNAVAILABLE",
        "EMPTY_RESPONSE", "CONNECTOR_UNAVAILABLE", "RESPONSE_CONTRACT_INVALID",
        "TOOL_CONTRACT_UNCONFIRMED", "M_NONEMPTY_CONTRACT_UNCONFIRMED", "UPSTREAM_BINDING_FAILED", "BINDING_TYPE_INVALID",
        "BINDING_EMPTY", "ENTRY_READ_LIMIT_EXCEEDED", "VAULT_PARTIAL_FAILURE",
        "VAULT_SCAN_LIMIT_EXCEEDED", "VAULT_FILE_CHANGED", "VAULT_FILE_TOO_LARGE",
        "VAULT_READ_FAILED", "INVALID_ENCODING", "INVALID_YAML", "MTR_TAG_CONFLICT",
        "PATH_ESCAPE", "SYMLINK_ESCAPE", "VAULT_PATH_COLLISION", "CORE_STATE_CHANGED",
    }
)


def safe_validation_field(prefix: str, errors: list[dict[str, Any]]) -> str:
    """Keep known schema locations while discarding untrusted extra-key text."""
    locations: set[str] = set()
    for error in errors:
        parts = tuple(str(part) for part in error.get("loc", ()))
        if parts and all(_SAFE_FIELD_COMPONENT.fullmatch(part) for part in parts):
            locations.add(".".join(parts))
    if not locations:
        return prefix
    return f"{prefix}.{sorted(locations)[0]}"


class MtrpError(Exception):
    """An expected application error with a stable code and process exit code."""

    def __init__(
        self,
        error_code: str,
        safe_message: str,
        *,
        field: str | None = None,
        exit_code: int = 2,
    ) -> None:
        if exit_code not in (1, 2):
            raise ValueError("MtrpError exit_code must be 1 or 2")
        self.error_code = error_code
        self.safe_message = safe_message
        self.field = field
        self.exit_code = exit_code
        super().__init__(safe_message)

    def as_safe_dict(self) -> dict[str, str | int | None]:
        """Return only fields approved for stderr presentation."""
        return {
            "error_code": self.error_code,
            "message": self.safe_message,
            "field": self.field,
            "exit_code": self.exit_code,
        }


def configuration_error(error_code: str, field: str, safe_message: str) -> MtrpError:
    """Build a configuration/registration error without including source values."""
    return MtrpError(error_code, safe_message, field=field, exit_code=2)


def internal_error(error_code: str = "INTERNAL_ERROR") -> MtrpError:
    """Build an internal error with no underlying exception text attached."""
    return MtrpError(
        error_code,
        "The operation could not be completed; inspect the local decision log.",
        exit_code=1,
    )
