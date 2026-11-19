"""Lexical and Registry-backed access guards executed before target access."""

from __future__ import annotations

import re
from pathlib import PurePath, PureWindowsPath

from pydantic import ConfigDict

from mtrp_hub.canonical import request_fingerprint
from mtrp_hub.domain.enums import Intent
from mtrp_hub.domain.models import FrozenModel, ProjectCoordinate, validate_safe_id
from mtrp_hub.domain.requests import RouteQueryRequest
from mtrp_hub.errors import MtrpError

_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WRITE_COMMANDS = frozenset({"write", "approve", "apply", "publish", "commit", "push", "merge"})


class AccessDecision(FrozenModel):
    """Proof that only in-memory Fake access was authorized."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=False)

    request_hash: str
    coordinate: ProjectCoordinate | None
    data_mode: str = "fake"
    network: str = "disabled"
    target_filesystem: str = "disabled"


class RequestAccessGuard:
    """Two-phase guard: pure lexical rejection, then in-memory registration checks."""

    def validate_bootstrap(self, request: RouteQueryRequest) -> None:
        """Validate explicit bootstrap strings without resolving or opening them."""
        config_dir = request.config_dir
        if "\x00" in config_dir or any(ord(char) < 32 for char in config_dir):
            raise MtrpError("INVALID_INPUT", "Configuration directory contains invalid characters.", field="config_dir")
        if _URL_RE.match(config_dir):
            raise MtrpError("COORDINATE_URL_FORBIDDEN", "Configuration directory cannot be a URL.", field="config_dir")
        if request.fixture is None:
            return
        fixture = request.fixture
        if "\x00" in fixture or any(ord(char) < 32 for char in fixture):
            raise MtrpError("INVALID_INPUT", "Fixture name contains invalid characters.", field="fixture")
        if _URL_RE.match(fixture):
            raise MtrpError("COORDINATE_URL_FORBIDDEN", "Fixture cannot be a URL.", field="fixture")
        if self._is_absolute(fixture):
            raise MtrpError("ABSOLUTE_PATH_FORBIDDEN", "Fixture must be a relative JSON path.", field="fixture")
        if ".." in PurePath(fixture).parts or ".." in PureWindowsPath(fixture).parts or ".." in fixture.split("/"):
            raise MtrpError("PATH_ESCAPE", "Fixture cannot contain parent traversal.", field="fixture")
        if "\\" in fixture or not fixture.lower().endswith(".json") or fixture in ("", ".json"):
            raise MtrpError("INVALID_INPUT", "Fixture must be a relative .json path using '/' separators.", field="fixture")

    def validate_lexical_coordinates(self, request: RouteQueryRequest) -> None:
        """Reject URL/path/traversal coordinates before config, fixture, or adapters."""
        if not request.query.strip():
            raise MtrpError("INVALID_INPUT", "Query cannot be blank.", field="query")
        needs_p = request.intent in (Intent.PROJECT_CONTEXT, Intent.DECISION_HISTORY)
        if needs_p and (request.project_id is None or request.repo_id is None):
            raise MtrpError("INVALID_INPUT", "This intent requires project and repo IDs.", field="project")
        if (request.project_id is None) != (request.repo_id is None):
            raise MtrpError("INVALID_INPUT", "Project and repo must be supplied together.", field="repo")
        if request.workspace_id is not None and request.project_id is None:
            raise MtrpError("INVALID_INPUT", "Workspace requires project and repo IDs.", field="workspace")
        for field, value in (
            ("project", request.project_id),
            ("repo", request.repo_id),
            ("workspace", request.workspace_id),
        ):
            if value is None:
                continue
            if _URL_RE.match(value) or value.lower().startswith("file:"):
                raise MtrpError("COORDINATE_URL_FORBIDDEN", "Coordinates must be registry IDs, not URLs.", field=field)
            if self._is_absolute(value):
                code = "UNREGISTERED_WORKSPACE" if field == "workspace" else "ABSOLUTE_PATH_FORBIDDEN"
                raise MtrpError(code, "Local paths are not accepted as coordinates.", field=field)
            if ".." in value or "/" in value or "\\" in value:
                code = "PATH_ESCAPE" if ".." in value else "INVALID_INPUT"
                raise MtrpError(code, "Coordinates must be simple registered IDs.", field=field)
            try:
                validate_safe_id(value)
            except ValueError as exc:
                raise MtrpError("INVALID_INPUT", "Coordinate is not a valid registry ID.", field=field) from exc

    def validate_registry_coordinate(
        self,
        request: RouteQueryRequest,
        coordinate: ProjectCoordinate | None,
    ) -> AccessDecision:
        """Return an in-memory-only decision after Registry resolution succeeds."""
        return AccessDecision(
            request_hash=self.successful_request_hash(request),
            coordinate=coordinate,
        )

    @staticmethod
    def successful_request_hash(request: RouteQueryRequest) -> str:
        """Hash successful requests with registered IDs only."""
        return request_fingerprint(
            query=request.query,
            intent=request.intent.value,
            project_id=request.project_id,
            repo_id=request.repo_id,
            workspace_id=request.workspace_id,
            include_r=request.include_r,
            registered_ids=True,
        )

    @staticmethod
    def rejected_request_hash(request: RouteQueryRequest) -> str:
        """Hash rejected requests without embedding untrusted coordinate strings."""
        return request_fingerprint(
            query=request.query,
            intent=request.intent.value,
            project_id=request.project_id,
            repo_id=request.repo_id,
            workspace_id=request.workspace_id,
            include_r=request.include_r,
            registered_ids=False,
        )

    @staticmethod
    def _is_absolute(value: str) -> bool:
        return bool(
            value.startswith(("/", "\\"))
            or _DRIVE_RE.match(value)
            or PurePath(value).is_absolute()
            or PureWindowsPath(value).is_absolute()
        )


def rejected_write_command(arguments: list[str]) -> str | None:
    """Return a write verb only when it occupies a CLI command position."""
    if not arguments:
        return None
    if arguments[0] in _WRITE_COMMANDS:
        return arguments[0]
    if len(arguments) > 1 and arguments[0] == "route" and arguments[1] in _WRITE_COMMANDS:
        return arguments[1]
    return None
