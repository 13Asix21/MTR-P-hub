"""The sole closed stage2-readonly-v1 tool and raw response contract profile."""

from __future__ import annotations

import base64
import binascii
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from pydantic import ValidationError

from mtrp_hub.canonical import sha256_bytes, sha256_json
from mtrp_hub.domain.models import FrozenModel, validate_commit, validate_ref, validate_relative_path
from mtrp_hub.errors import MtrpError
from mtrp_hub.execution.models import (
    EntryCandidate,
    ExecutionManifest,
    ExecutionMode,
    ManifestStep,
    PlannedToolCall,
    Stage2Operation,
    StepBinding,
)

LEXIANG_OUTER_TOOL = "mcp__lexiang__call_tool"
GITHUB_LIST_BRANCHES_TOOL = "mcp__github__list_branches"
GITHUB_GET_COMMIT_TOOL = "mcp__github__get_commit"
GITHUB_GET_FILE_TOOL = "mcp__github__get_file_contents"
_FULL_ENTRY_ID = re.compile(r"^[0-9a-f]{32}$")
_FULL_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_SAFE_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_DOWNLOAD_HEADER = re.compile(r"\Asuccessfully downloaded text file \(SHA: [0-9a-f]{40}\)\r?\n")
_DOWNLOAD_HEADER_CANDIDATE = re.compile(r"^successfully downloaded text file \(SHA: [0-9a-f]{40}\)\r?$", re.MULTILINE)


class ResponseContractError(ValueError):
    """A safe raw response contract failure with no source text attached."""

    def __init__(self, code: str = "RESPONSE_CONTRACT_INVALID") -> None:
        self.error_code = code
        super().__init__(code)


class ToolSpec(FrozenModel):
    operation: Stage2Operation
    tool_id: str
    inner_tool_name: str | None = None
    response_contract: str
    real_response_verified: bool
    real_nonempty_verified: bool = True


@dataclass(frozen=True)
class ParsedGitHubFile:
    """Controlled file content used only in memory, never inserted into binding_data."""

    content: bytes
    content_sha256: str
    content_hash_scope: Literal["raw_bytes", "response_utf8"]
    content_format: Literal["utf8_text"]
    path: str | None = None


class Stage2ReadonlyProfile:
    """Map logical operations to fixed tools; no name is accepted from a Manifest."""

    name = "stage2-readonly-v1"
    response_contract_version = "2026-11-real-probe-v3"

    _SPECS = {
        Stage2Operation.LEXIANG_RESOLVE_SPACE: ToolSpec(
            operation=Stage2Operation.LEXIANG_RESOLVE_SPACE,
            tool_id=LEXIANG_OUTER_TOOL,
            inner_tool_name="space_describe_space",
            response_contract="lexiang-space-v1",
            real_response_verified=True,
            real_nonempty_verified=True,
        ),
        Stage2Operation.LEXIANG_SEARCH_TEXT: ToolSpec(
            operation=Stage2Operation.LEXIANG_SEARCH_TEXT,
            tool_id=LEXIANG_OUTER_TOOL,
            inner_tool_name="search_kb_search",
            response_contract="lexiang-search-fixture-nonempty-real-empty-v1",
            real_response_verified=True,
            real_nonempty_verified=False,
        ),
        Stage2Operation.LEXIANG_SEARCH_VECTOR: ToolSpec(
            operation=Stage2Operation.LEXIANG_SEARCH_VECTOR,
            tool_id=LEXIANG_OUTER_TOOL,
            inner_tool_name="search_kb_embedding_search",
            response_contract="lexiang-search-fixture-nonempty-real-empty-v1",
            real_response_verified=True,
            real_nonempty_verified=False,
        ),
        Stage2Operation.LEXIANG_READ_ENTRIES: ToolSpec(
            operation=Stage2Operation.LEXIANG_READ_ENTRIES,
            tool_id=LEXIANG_OUTER_TOOL,
            inner_tool_name="entry_describe_entry",
            response_contract="lexiang-entry-fixture-v1",
            real_response_verified=False,
            real_nonempty_verified=False,
        ),
        Stage2Operation.GITHUB_LIST_BRANCHES: ToolSpec(
            operation=Stage2Operation.GITHUB_LIST_BRANCHES,
            tool_id=GITHUB_LIST_BRANCHES_TOOL,
            response_contract="github-branches-v1",
            real_response_verified=True,
        ),
        Stage2Operation.GITHUB_RESOLVE_REF: ToolSpec(
            operation=Stage2Operation.GITHUB_RESOLVE_REF,
            tool_id=GITHUB_GET_COMMIT_TOOL,
            response_contract="github-commit-v1",
            real_response_verified=True,
        ),
        Stage2Operation.GITHUB_READ_REPOSITORY_ROOT: ToolSpec(
            operation=Stage2Operation.GITHUB_READ_REPOSITORY_ROOT,
            tool_id=GITHUB_GET_FILE_TOOL,
            response_contract="github-root-v1",
            real_response_verified=True,
        ),
        Stage2Operation.GITHUB_READ_FILE: ToolSpec(
            operation=Stage2Operation.GITHUB_READ_FILE,
            tool_id=GITHUB_GET_FILE_TOOL,
            response_contract="github-text-real-or-explicit-base64-fixture-v1",
            real_response_verified=True,
        ),
    }
    _ENTRY_PARSE_INNER = "entry_describe_ai_parse_content"

    def profile_hash(self) -> str:
        specs = [self._SPECS[key].model_dump(mode="json") for key in sorted(self._SPECS, key=lambda item: item.value)]
        return sha256_json(
            {
                "name": self.name,
                "response_contract_version": self.response_contract_version,
                "entry_parse_inner_tool": self._ENTRY_PARSE_INNER,
                "specs": specs,
                "binding_fields": {
                    "github_read_file": ["commit"],
                    "github_read_repository_root": ["commit"],
                    "lexiang_read_entries": ["entry_reads"],
                },
            }
        )

    def spec_for(self, step: ManifestStep | Stage2Operation | str) -> ToolSpec:
        try:
            operation = step.operation if isinstance(step, ManifestStep) else Stage2Operation(step)
            return self._SPECS[operation]
        except (KeyError, ValueError) as exc:
            raise MtrpError("TOOL_FORBIDDEN", "The logical operation is not in the read-only profile.", field="operation") from exc

    def validate_arguments(self, step: ManifestStep) -> None:
        """Validate exact logical argument keys and values before any target call."""
        arguments = step.arguments
        expected_keys = {
            Stage2Operation.GITHUB_LIST_BRANCHES: {"owner", "repo"},
            Stage2Operation.GITHUB_RESOLVE_REF: {"owner", "repo", "ref"},
            Stage2Operation.GITHUB_READ_REPOSITORY_ROOT: {"owner", "repo", "sha"},
            Stage2Operation.GITHUB_READ_FILE: {"owner", "repo", "path", "sha"},
            Stage2Operation.LEXIANG_RESOLVE_SPACE: {"space_id", "team_id", "root_entry_id"},
            Stage2Operation.LEXIANG_SEARCH_TEXT: {"space_id", "keyword", "limit"},
            Stage2Operation.LEXIANG_SEARCH_VECTOR: {"space_id", "filters", "limit", "threshold"},
            Stage2Operation.LEXIANG_READ_ENTRIES: {"entry_reads"},
            Stage2Operation.VAULT_VIEW_SEARCH: {"vault_id", "view_tag", "query", "scope_sha256"},
        }.get(step.operation)
        if expected_keys is None:
            raise MtrpError("TOOL_FORBIDDEN", "The operation is forbidden.", field="operation")
        if set(arguments) != expected_keys:
            raise MtrpError("ARGUMENT_SCHEMA_INVALID", "Manifest arguments do not match the fixed operation schema.", field=step.step_id)
        if step.operation in (
            Stage2Operation.GITHUB_LIST_BRANCHES,
            Stage2Operation.GITHUB_RESOLVE_REF,
            Stage2Operation.GITHUB_READ_REPOSITORY_ROOT,
            Stage2Operation.GITHUB_READ_FILE,
        ):
            self._validate_github_coordinates(arguments)
        if step.operation is Stage2Operation.GITHUB_RESOLVE_REF:
            self._validate(lambda: validate_ref(arguments["ref"]), step)
        if step.operation in (Stage2Operation.GITHUB_READ_REPOSITORY_ROOT, Stage2Operation.GITHUB_READ_FILE):
            self._validate_binding(arguments["sha"], "commit", step)
        if step.operation is Stage2Operation.GITHUB_READ_FILE:
            self._validate(lambda: validate_relative_path(arguments["path"]), step)
        if step.operation in (
            Stage2Operation.LEXIANG_RESOLVE_SPACE,
            Stage2Operation.LEXIANG_SEARCH_TEXT,
            Stage2Operation.LEXIANG_SEARCH_VECTOR,
        ):
            self._validate_id(arguments["space_id"], step)
        if step.operation is Stage2Operation.LEXIANG_RESOLVE_SPACE:
            self._validate_id(arguments["team_id"], step)
            self._validate_id(arguments["root_entry_id"], step)
        if step.operation is Stage2Operation.LEXIANG_SEARCH_TEXT:
            self._validate_keyword(arguments["keyword"], step)
            self._validate_limit(arguments["limit"], step)
        if step.operation is Stage2Operation.LEXIANG_SEARCH_VECTOR:
            if set(arguments["filters"]) != {"keyword"} if isinstance(arguments["filters"], dict) else True:
                raise MtrpError("ARGUMENT_SCHEMA_INVALID", "Vector filters have an invalid schema.", field=step.step_id)
            self._validate_keyword(arguments["filters"]["keyword"], step)
            self._validate_limit(arguments["limit"], step)
            threshold = arguments["threshold"]
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
                raise MtrpError("ARGUMENT_SCHEMA_INVALID", "Vector threshold is invalid.", field=step.step_id)
        if step.operation is Stage2Operation.LEXIANG_READ_ENTRIES:
            self._validate_binding(arguments["entry_reads"], "entry_reads", step)
        if step.operation is Stage2Operation.VAULT_VIEW_SEARCH:
            self._validate_keyword(arguments["query"], step)
            if arguments["view_tag"] not in ("mtr/trial", "mtr/rarely") or not _FULL_SHA256.fullmatch(arguments["scope_sha256"]):
                raise MtrpError("ARGUMENT_SCHEMA_INVALID", "Vault view arguments are invalid.", field=step.step_id)
            self._validate_id(arguments["vault_id"], step, vault=True)

    @staticmethod
    def _validate(action, step: ManifestStep) -> None:  # type: ignore[no-untyped-def]
        try:
            action()
        except (TypeError, ValueError) as exc:
            raise MtrpError("ARGUMENT_SCHEMA_INVALID", "Manifest argument validation failed.", field=step.step_id) from exc

    def _validate_github_coordinates(self, arguments: dict[str, Any]) -> None:
        owner, repo = arguments.get("owner"), arguments.get("repo")
        if not isinstance(owner, str) or not _SAFE_OWNER.fullmatch(owner) or "--" in owner:
            raise MtrpError("ARGUMENT_SCHEMA_INVALID", "GitHub owner is invalid.", field="owner")
        if not isinstance(repo, str) or not _SAFE_REPO.fullmatch(repo) or ".." in repo:
            raise MtrpError("ARGUMENT_SCHEMA_INVALID", "GitHub repository is invalid.", field="repo")

    @staticmethod
    def _validate_id(value: Any, step: ManifestStep, *, vault: bool = False) -> None:
        valid = isinstance(value, str) and (re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value) if vault else _FULL_ENTRY_ID.fullmatch(value))
        if not valid or (isinstance(value, str) and ".." in value):
            raise MtrpError("ARGUMENT_SCHEMA_INVALID", "A configured logical ID is invalid.", field=step.step_id)

    @staticmethod
    def _validate_keyword(value: Any, step: ManifestStep) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > 16384:
            raise MtrpError("ARGUMENT_SCHEMA_INVALID", "Query text is invalid.", field=step.step_id)

    @staticmethod
    def _validate_limit(value: Any, step: ManifestStep) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise MtrpError("ARGUMENT_SCHEMA_INVALID", "Search limit is invalid.", field=step.step_id)

    @staticmethod
    def _validate_binding(value: Any, field: str, step: ManifestStep) -> None:
        if not isinstance(value, dict) or set(value) != {"from_step", "field"}:
            raise MtrpError("BINDING_INVALID", "Dynamic arguments must use the declared binding shape.", field=step.step_id)
        try:
            binding = StepBinding.model_validate(value)
        except ValidationError as exc:
            raise MtrpError("BINDING_INVALID", "Dynamic argument binding is invalid.", field=step.step_id) from exc
        if binding.field != field:
            raise MtrpError("BINDING_FIELD_FORBIDDEN", "The binding field is forbidden for this operation.", field=step.step_id)

    def materialize_calls(self, step: ManifestStep, arguments: dict[str, Any]) -> tuple[PlannedToolCall, ...]:
        """Create all actual calls from fixed constants and already-resolved bindings."""
        if step.operation is Stage2Operation.VAULT_VIEW_SEARCH:
            return ()
        if step.operation is Stage2Operation.LEXIANG_READ_ENTRIES:
            candidates = arguments["entry_reads"]
            calls: list[PlannedToolCall] = []
            for candidate in candidates:
                entry_id = candidate.entry_id if isinstance(candidate, EntryCandidate) else candidate["entry_id"]
                for inner in (self._SPECS[step.operation].inner_tool_name, self._ENTRY_PARSE_INNER):
                    calls.append(self._planned(step, len(calls), inner, {"entry_id": entry_id}))
            return tuple(calls)
        operation = step.operation
        if operation is Stage2Operation.GITHUB_LIST_BRANCHES:
            return (self._planned(step, 0, None, {"owner": arguments["owner"], "repo": arguments["repo"], "page": 1, "perPage": 100}),)
        if operation is Stage2Operation.GITHUB_RESOLVE_REF:
            return (self._planned(step, 0, None, {"owner": arguments["owner"], "repo": arguments["repo"], "sha": arguments["ref"], "detail": "none", "page": 1, "perPage": 1}),)
        if operation is Stage2Operation.GITHUB_READ_REPOSITORY_ROOT:
            return (self._planned(step, 0, None, {"owner": arguments["owner"], "repo": arguments["repo"], "sha": arguments["sha"], "fields": ["type", "name", "path"]}),)
        if operation is Stage2Operation.GITHUB_READ_FILE:
            return (self._planned(step, 0, None, {"owner": arguments["owner"], "repo": arguments["repo"], "path": arguments["path"], "sha": arguments["sha"]}),)
        if operation is Stage2Operation.LEXIANG_RESOLVE_SPACE:
            return (self._planned(step, 0, "space_describe_space", {"space_id": arguments["space_id"]}),)
        if operation is Stage2Operation.LEXIANG_SEARCH_TEXT:
            actual = {
                "keyword": arguments["keyword"],
                "type": "doc",
                "space_id": arguments["space_id"],
                "limit": arguments["limit"],
                "include_references": False,
                "highlight": False,
                "title_only": False,
            }
            return (self._planned(step, 0, "search_kb_search", actual),)
        if operation is Stage2Operation.LEXIANG_SEARCH_VECTOR:
            actual = {
                "filters": {"keyword": arguments["filters"]["keyword"]},
                "space_id": arguments["space_id"],
                "limit": arguments["limit"],
                "threshold": arguments["threshold"],
            }
            return (self._planned(step, 0, "search_kb_embedding_search", actual),)
        raise MtrpError("TOOL_FORBIDDEN", "The operation cannot produce a tool call.", field=step.step_id)

    def _planned(self, step: ManifestStep, index: int, inner: str | None, actual_arguments: dict[str, Any]) -> PlannedToolCall:
        spec = self.spec_for(step)
        arguments = {"tool_name": inner, "arguments": actual_arguments} if spec.tool_id == LEXIANG_OUTER_TOOL else actual_arguments
        return PlannedToolCall(
            call_id=f"{step.step_id}-call-{index:02d}",
            tool_id=spec.tool_id,
            inner_tool_name=inner if spec.tool_id == LEXIANG_OUTER_TOOL else None,
            arguments=arguments,
            timeout_seconds=10,
        )

    def operation_for_call(self, tool_id: str, inner_tool_name: str | None, arguments: dict[str, Any]) -> Stage2Operation:
        """Resolve Fixture transport calls without accepting a Fixture-selected tool."""
        if tool_id == GITHUB_LIST_BRANCHES_TOOL:
            return Stage2Operation.GITHUB_LIST_BRANCHES
        if tool_id == GITHUB_GET_COMMIT_TOOL:
            return Stage2Operation.GITHUB_RESOLVE_REF
        if tool_id == GITHUB_GET_FILE_TOOL:
            return Stage2Operation.GITHUB_READ_FILE if "path" in arguments else Stage2Operation.GITHUB_READ_REPOSITORY_ROOT
        if tool_id == LEXIANG_OUTER_TOOL:
            mapping = {
                "space_describe_space": Stage2Operation.LEXIANG_RESOLVE_SPACE,
                "search_kb_search": Stage2Operation.LEXIANG_SEARCH_TEXT,
                "search_kb_embedding_search": Stage2Operation.LEXIANG_SEARCH_VECTOR,
                "entry_describe_entry": Stage2Operation.LEXIANG_READ_ENTRIES,
                "entry_describe_ai_parse_content": Stage2Operation.LEXIANG_READ_ENTRIES,
            }
            if inner_tool_name in mapping:
                return mapping[inner_tool_name]
        raise MtrpError("TOOL_FORBIDDEN", "The Fixture transport attempted an unknown tool.", field="tool_id")

    @staticmethod
    def _json_payload(response: Any) -> Any:
        if response is None or getattr(response, "kind", None) != "json":
            raise ResponseContractError("EMPTY_RESPONSE")
        return response.data

    @staticmethod
    def _lexiang_data(response: Any) -> Any:
        payload = Stage2ReadonlyProfile._json_payload(response)
        if not isinstance(payload, dict) or not {"code", "message", "data"}.issubset(payload):
            raise ResponseContractError()
        if payload.get("code") != 0 or payload.get("message") != "success":
            raise ResponseContractError("UNAVAILABLE")
        return payload["data"]

    def parse_space(self, response: Any, expected: dict[str, Any]) -> dict[str, str]:
        data = self._lexiang_data(response)
        if not isinstance(data, dict) or not isinstance(data.get("space"), dict):
            raise ResponseContractError()
        space = data["space"]
        values = {key: space.get(key) for key in ("id", "team_id", "root_entry_id")}
        if any(not isinstance(value, str) or not _FULL_ENTRY_ID.fullmatch(value) for value in values.values()):
            raise ResponseContractError()
        expected_values = {"id": expected["space_id"], "team_id": expected["team_id"], "root_entry_id": expected["root_entry_id"]}
        if values != expected_values:
            raise ResponseContractError("SPACE_MISMATCH")
        return {"space_id": values["id"], "team_id": values["team_id"], "root_entry_id": values["root_entry_id"]}

    def parse_search(
        self,
        response: Any,
        operation: Stage2Operation,
        mode: ExecutionMode,
        *,
        limit: int,
    ) -> tuple[tuple[EntryCandidate, ...], int]:
        data = self._lexiang_data(response)
        if data == {} and mode is not ExecutionMode.FIXTURE:
            return (), 0
        if mode is not ExecutionMode.FIXTURE and not self.spec_for(operation).real_nonempty_verified:
            raise ResponseContractError("M_NONEMPTY_CONTRACT_UNCONFIRMED")
        if data == {} or data == []:
            return (), 0
        if not isinstance(data, dict) or set(data) != {"entries"} or not isinstance(data["entries"], list):
            raise ResponseContractError()
        raw_entries = data["entries"]
        if len(raw_entries) > limit:
            raise ResponseContractError()
        source = "text_search" if operation is Stage2Operation.LEXIANG_SEARCH_TEXT else "vector_search"
        candidates: list[EntryCandidate] = []
        seen: set[str] = set()
        score_count = 0
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise ResponseContractError()
            allowed = {"entry_id", "title", "score", "version", "revision", "provider_content_sha256"}
            if set(raw) - allowed or not isinstance(raw.get("entry_id"), str) or not _FULL_ENTRY_ID.fullmatch(raw["entry_id"]):
                raise ResponseContractError()
            entry_id = raw["entry_id"]
            if entry_id in seen:
                continue
            title = raw.get("title")
            if not isinstance(title, str) or not title.strip() or len(title) > 512:
                raise ResponseContractError()
            raw_score = raw.get("score")
            valid_score = isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool) and math.isfinite(raw_score) and 0 <= raw_score <= 1
            score = float(raw_score) if valid_score else None
            if score is not None:
                score_count += 1
            version = raw.get("version")
            revision = raw.get("revision")
            provider_hash = raw.get("provider_content_sha256")
            if version is not None and (not isinstance(version, str) or not version or len(version) > 128):
                raise ResponseContractError()
            if revision is not None and (not isinstance(revision, str) or not revision or len(revision) > 128):
                raise ResponseContractError()
            if provider_hash is not None and (not isinstance(provider_hash, str) or not _FULL_SHA256.fullmatch(provider_hash)):
                raise ResponseContractError()
            candidate = EntryCandidate(
                entry_id=entry_id,
                title=title,
                score=score,
                score_source=source if score is not None else "not_provided",
                version=version,
                revision=revision,
                provider_content_sha256=provider_hash,
            )
            candidates.append(candidate)
            seen.add(entry_id)
        return tuple(candidates), score_count

    def parse_entry_meta(self, response: Any, *, entry_id: str, space_id: str, mode: ExecutionMode) -> dict[str, Any]:
        if mode is not ExecutionMode.FIXTURE:
            raise ResponseContractError("TOOL_CONTRACT_UNCONFIRMED")
        data = self._lexiang_data(response)
        entry = data.get("entry") if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            raise ResponseContractError()
        allowed = {"entry_id", "space_id", "entry_type", "name", "version", "revision", "provider_content_sha256"}
        if set(entry) - allowed or entry.get("entry_id") != entry_id or entry.get("space_id") != space_id:
            raise ResponseContractError("SPACE_MISMATCH" if entry.get("space_id") != space_id else "RESPONSE_CONTRACT_INVALID")
        if entry.get("entry_type") not in (None, "doc", "document") or not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise ResponseContractError()
        result = {key: entry.get(key) for key in allowed}
        if result["provider_content_sha256"] is not None and not _FULL_SHA256.fullmatch(result["provider_content_sha256"]):
            raise ResponseContractError()
        return result

    def parse_entry_content(self, response: Any, *, entry_id: str, mode: ExecutionMode) -> tuple[str, str]:
        if mode is not ExecutionMode.FIXTURE:
            raise ResponseContractError("TOOL_CONTRACT_UNCONFIRMED")
        data = self._lexiang_data(response)
        allowed = {"entry_id", "content_format", "content_text"}
        if not isinstance(data, dict) or set(data) - allowed or data.get("entry_id") != entry_id:
            raise ResponseContractError()
        content_format = data.get("content_format")
        text = data.get("content_text")
        if content_format not in ("markdown", "html", "ocr_text") or not isinstance(text, str):
            raise ResponseContractError()
        return content_format, text

    def parse_branches(self, response: Any) -> dict[str, Any]:
        payload = self._json_payload(response)
        if not isinstance(payload, list):
            raise ResponseContractError()
        branches: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict) or set(item) != {"name", "sha", "protected"} or not isinstance(item["name"], str) or not isinstance(item["protected"], bool):
                raise ResponseContractError()
            try:
                validate_ref(item["name"])
                validate_commit(item["sha"])
            except (TypeError, ValueError) as exc:
                raise ResponseContractError() from exc
            branches.append({"name": item["name"], "sha": item["sha"]})
        return {
            "repository_state": "empty" if not branches else "ready",
            "branches": branches,
            "truncated": len(branches) >= 100,
        }

    def parse_commit(self, response: Any) -> str:
        payload = self._json_payload(response)
        if not isinstance(payload, dict):
            raise ResponseContractError("REF_UNRESOLVED")
        try:
            validate_commit(payload.get("sha"))
        except (TypeError, ValueError) as exc:
            raise ResponseContractError("REF_UNRESOLVED") from exc
        return payload["sha"]

    def parse_root_entries(self, response: Any) -> tuple[dict[str, str], ...]:
        payload = self._json_payload(response)
        if not isinstance(payload, list):
            raise ResponseContractError()
        entries: list[dict[str, str]] = []
        for item in payload:
            if not isinstance(item, dict) or set(item) != {"name", "path", "type"}:
                raise ResponseContractError()
            if not isinstance(item["name"], str) or not isinstance(item["path"], str) or item["type"] not in ("file", "dir", "symlink", "submodule"):
                raise ResponseContractError()
            entries.append({"name": item["name"], "path": item["path"], "type": item["type"]})
        return tuple(entries)

    def parse_root(self, response: Any) -> int:
        return len(self.parse_root_entries(response))

    def parse_file(self, response: Any, mode: ExecutionMode) -> ParsedGitHubFile:
        if getattr(response, "kind", None) == "text":
            text = response.text
            match = _DOWNLOAD_HEADER.match(text)
            if match is None:
                raise ResponseContractError()
            content_text = text[match.end() :]
            if _DOWNLOAD_HEADER_CANDIDATE.search(content_text) is not None:
                raise ResponseContractError()
            content = content_text.encode("utf-8")
            return ParsedGitHubFile(content, sha256_bytes(content), "response_utf8", "utf8_text")
        if mode is not ExecutionMode.FIXTURE:
            raise ResponseContractError()
        payload = self._json_payload(response)
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise ResponseContractError()
        allowed = {"path", "encoding", "content", "content_base64", "size"}
        if set(payload) - allowed or not {"content", "content_base64"}.intersection(payload):
            raise ResponseContractError()
        encoded = payload.get("content_base64", payload.get("content"))
        path = payload.get("path")
        if not isinstance(encoded, str) or (path is not None and not isinstance(path, str)):
            raise ResponseContractError()
        try:
            content = base64.b64decode(encoded, validate=True)
            if path is not None:
                validate_relative_path(path)
        except (ValueError, binascii.Error) as exc:
            raise ResponseContractError() from exc
        if "size" in payload and payload["size"] != len(content):
            raise ResponseContractError()
        return ParsedGitHubFile(content, sha256_bytes(content), "raw_bytes", "utf8_text", path)

    def validate_response(
        self,
        step: ManifestStep,
        response: Any,
        *,
        mode: ExecutionMode = ExecutionMode.FIXTURE,
    ) -> None:
        """Validate one business response without retaining it or its text."""
        if step.operation is Stage2Operation.LEXIANG_RESOLVE_SPACE:
            self.parse_space(response, step.arguments)
            return
        if step.operation in (Stage2Operation.LEXIANG_SEARCH_TEXT, Stage2Operation.LEXIANG_SEARCH_VECTOR):
            self.parse_search(response, step.operation, mode, limit=step.arguments["limit"])
            return
        if step.operation is Stage2Operation.GITHUB_LIST_BRANCHES:
            self.parse_branches(response)
            return
        if step.operation is Stage2Operation.GITHUB_RESOLVE_REF:
            self.parse_commit(response)
            return
        if step.operation is Stage2Operation.GITHUB_READ_REPOSITORY_ROOT:
            self.parse_root(response)
            return
        if step.operation is Stage2Operation.GITHUB_READ_FILE:
            self.parse_file(response, mode)
            return
        if step.operation is Stage2Operation.LEXIANG_READ_ENTRIES:
            if mode is not ExecutionMode.FIXTURE:
                raise ResponseContractError("TOOL_CONTRACT_UNCONFIRMED")
            data = self._lexiang_data(response)
            if not isinstance(data, dict) or not (isinstance(data.get("entry"), dict) or "content_text" in data):
                raise ResponseContractError()
            return
        raise MtrpError("TOOL_FORBIDDEN", "This operation has no external response contract.", field=step.step_id)

    def extract_binding_data(self, step: ManifestStep, calls: Iterable[Any], mode: ExecutionMode) -> dict[str, Any]:
        """Extract only declared downstream fields from validated business responses."""
        materialized = tuple(calls)
        if not materialized:
            if step.operation is Stage2Operation.LEXIANG_READ_ENTRIES:
                return {"entry_reads": [], "result_count": 0, "score_available_count": 0}
            return {}
        response = materialized[0].response
        if step.operation is Stage2Operation.LEXIANG_RESOLVE_SPACE:
            return self.parse_space(response, step.arguments)
        if step.operation in (Stage2Operation.LEXIANG_SEARCH_TEXT, Stage2Operation.LEXIANG_SEARCH_VECTOR):
            candidates, score_count = self.parse_search(
                response,
                step.operation,
                mode,
                limit=step.arguments["limit"],
            )
            return {
                "entry_reads": [item.model_dump(mode="json") for item in candidates],
                "result_count": len(candidates),
                "score_available_count": score_count,
            }
        if step.operation is Stage2Operation.GITHUB_LIST_BRANCHES:
            return self.parse_branches(response)
        if step.operation is Stage2Operation.GITHUB_RESOLVE_REF:
            return {"commit": self.parse_commit(response)}
        if step.operation is Stage2Operation.GITHUB_READ_REPOSITORY_ROOT:
            entries = self.parse_root_entries(response)
            return {"root_entry_count": len(entries), "entries": list(entries)}
        if step.operation is Stage2Operation.GITHUB_READ_FILE:
            parsed = self.parse_file(response, mode)
            return {"content_sha256": parsed.content_sha256, "content_hash_scope": parsed.content_hash_scope}
        return {}

    def receipt_projection(self, manifest: ExecutionManifest, step: ManifestStep, call: Any) -> Any:
        """Project one raw response onto the approved deterministic fields only.

        Full payloads remain independently bound by response_sha256. This
        projection deliberately excludes transport noise and all content text.
        """
        response = call.response
        if response is None:
            return None
        try:
            if step.operation is Stage2Operation.LEXIANG_RESOLVE_SPACE:
                return self.parse_space(response, step.arguments)
            if step.operation in (Stage2Operation.LEXIANG_SEARCH_TEXT, Stage2Operation.LEXIANG_SEARCH_VECTOR):
                candidates, score_count = self.parse_search(
                    response,
                    step.operation,
                    manifest.mode,
                    limit=step.arguments["limit"],
                )
                return {
                    "entry_reads": [item.model_dump(mode="json") for item in candidates],
                    "result_count": len(candidates),
                    "score_available_count": score_count,
                }
            if step.operation is Stage2Operation.GITHUB_LIST_BRANCHES:
                return self.parse_branches(response)
            if step.operation is Stage2Operation.GITHUB_RESOLVE_REF:
                return {"commit": self.parse_commit(response)}
            if step.operation is Stage2Operation.GITHUB_READ_REPOSITORY_ROOT:
                return {"entries": list(self.parse_root_entries(response))}
            if step.operation is Stage2Operation.GITHUB_READ_FILE:
                parsed = self.parse_file(response, manifest.mode)
                return {
                    "path": step.arguments["path"],
                    "commit": call.arguments.get("sha"),
                    "content_sha256": parsed.content_sha256,
                    "content_hash_scope": parsed.content_hash_scope,
                }
            if step.operation is Stage2Operation.LEXIANG_READ_ENTRIES:
                actual = call.arguments["arguments"]
                entry_id = actual["entry_id"]
                if call.inner_tool_name == "entry_describe_entry":
                    space_step = next(
                        item for item in manifest.steps
                        if item.route_step_id == step.route_step_id and item.operation is Stage2Operation.LEXIANG_RESOLVE_SPACE
                    )
                    metadata = self.parse_entry_meta(
                        response,
                        entry_id=entry_id,
                        space_id=space_step.arguments["space_id"],
                        mode=manifest.mode,
                    )
                    return {"entry_meta": metadata}
                if call.inner_tool_name == self._ENTRY_PARSE_INNER:
                    content_format, content_text = self.parse_entry_content(
                        response,
                        entry_id=entry_id,
                        mode=manifest.mode,
                    )
                    return {
                        "entry_id": entry_id,
                        "content_format": content_format,
                        "content_sha256": sha256_bytes(content_text.encode("utf-8")),
                        "content_hash_scope": "response_utf8",
                    }
                raise ResponseContractError()
        except (ResponseContractError, TypeError, ValueError):
            return {"contract_valid": False}
        raise MtrpError("TOOL_FORBIDDEN", "This operation has no receipt projection.", field=step.step_id)

    def projection_sha256(self, manifest: ExecutionManifest, step: ManifestStep, call: Any) -> str:
        """Hash only the deterministic projection; callers cannot supply it."""
        return sha256_json(self.receipt_projection(manifest, step, call))


STAGE2_PROFILE = Stage2ReadonlyProfile()

__all__ = [
    "GITHUB_GET_COMMIT_TOOL",
    "GITHUB_GET_FILE_TOOL",
    "GITHUB_LIST_BRANCHES_TOOL",
    "LEXIANG_OUTER_TOOL",
    "ParsedGitHubFile",
    "ResponseContractError",
    "STAGE2_PROFILE",
    "Stage2ReadonlyProfile",
    "ToolSpec",
]
