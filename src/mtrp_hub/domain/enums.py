"""Closed enumerations for routing, canonical state, and read-only operations."""

from enum import Enum


class StrEnum(str, Enum):
    """Python 3.11-compatible string enum with predictable JSON values."""

    def __str__(self) -> str:
        return str(self.value)


class Intent(StrEnum):
    PROJECT_CONTEXT = "project_context"
    GENERAL_KNOWLEDGE = "general_knowledge"
    METHOD_SKILL = "method_skill"
    DECISION_HISTORY = "decision_history"
    COLD_DETAIL = "cold_detail"


class StoreKind(StrEnum):
    P = "P"
    M = "M"
    T = "T"
    R = "R"


class LocationKind(StrEnum):
    GITHUB = "github"
    LOCAL_GIT = "local_git"


class LocationRole(StrEnum):
    AUTHORITATIVE = "authoritative"
    MIRROR = "mirror"
    WORKSPACE = "workspace"


class RepositoryState(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    EMPTY = "empty"


class SnapshotKind(StrEnum):
    GIT_COMMIT = "git_commit"
    CONTENT_SHA256 = "content_sha256"
    FIXTURE_SNAPSHOT = "fixture_snapshot"


class ResourceType(StrEnum):
    PROJECT_FILE = "project_file"
    ADR = "adr"
    KNOWLEDGE_ENTRY = "knowledge_entry"
    VAULT_NOTE = "vault_note"


class EmptyAction(StrEnum):
    CONTINUE = "continue"
    RETURN_EMPTY = "return_empty"


class ReadOperation(StrEnum):
    PROJECT_READ = "project_read"
    KNOWLEDGE_SEARCH = "knowledge_search"
    VAULT_VIEW_SEARCH = "vault_view_search"
    ADR_SEARCH = "adr_search"


class StepStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    DEGRADED = "degraded"


class ReasonCode(StrEnum):
    M_EMPTY = "M_EMPTY"
    P_EMPTY = "P_EMPTY"
    EMPTY_REPOSITORY = "EMPTY_REPOSITORY"
    R_EMPTY = "R_EMPTY"
    T_EMPTY = "T_EMPTY"
    ADR_MISSING = "ADR_MISSING"
    BRANCH_NOT_FOUND = "BRANCH_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNAVAILABLE = "UNAVAILABLE"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    INVALID_YAML = "INVALID_YAML"


class ProposalOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class ProposalStatus(StrEnum):
    DRAFT = "draft"


class OutputFormat(StrEnum):
    JSON = "json"
    TEXT = "text"
