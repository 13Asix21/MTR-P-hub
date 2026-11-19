"""Safe, classified errors that never echo untrusted sensitive values."""

from __future__ import annotations

import re
from typing import Any


_SAFE_FIELD_COMPONENT = re.compile(r"^(?:[a-z_][a-z0-9_]*|[0-9]+)$")


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
