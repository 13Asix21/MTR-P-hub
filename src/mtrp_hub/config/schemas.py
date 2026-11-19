"""Raw YAML DTOs, decoupled from validated Registry and routing objects."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mtrp_hub.domain.enums import (
    EmptyAction,
    LocationKind,
    LocationRole,
    ReadOperation,
    RepositoryState,
    StoreKind,
)
from mtrp_hub.domain.models import (
    CommitSha,
    GitRef,
    RelativePath,
    SafeId,
    SnapshotPin,
)


class ConfigModel(BaseModel):
    """Strict common configuration for every YAML DTO."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=False)


class StoreConfig(ConfigModel):
    store_id: SafeId
    kind: StoreKind
    adapter: str
    read_only: bool
    vault_id: SafeId | None = None
    view_tag: Literal["mtr/trial", "mtr/rarely"] | None = None


class StoresConfig(ConfigModel):
    schema_version: Literal["1"]
    stores: tuple[StoreConfig, ...] = Field(min_length=1)


class LocationConfig(ConfigModel):
    location_id: SafeId
    kind: LocationKind
    role: LocationRole
    remote: str | None = None
    state: RepositoryState = RepositoryState.UNKNOWN
    snapshots: tuple[SnapshotPin, ...] = ()


class WorkspaceConfig(ConfigModel):
    workspace_id: SafeId
    location_id: SafeId
    root_path: str = Field(min_length=1, max_length=4096)


class RepositoryConfig(ConfigModel):
    repo_id: SafeId
    default_branch: GitRef
    authoritative_location_id: SafeId
    adr_paths: tuple[RelativePath, ...] = ()
    locations: tuple[LocationConfig, ...] = Field(min_length=1)
    workspaces: tuple[WorkspaceConfig, ...] = ()


class ProjectConfig(ConfigModel):
    project_id: SafeId
    repositories: tuple[RepositoryConfig, ...] = Field(min_length=1)


class ProjectsConfig(ConfigModel):
    schema_version: Literal["1"]
    projects: tuple[ProjectConfig, ...] = Field(min_length=1)


class RoutingStepConfig(ConfigModel):
    store: StoreKind
    operation: ReadOperation


class IntentRuleConfig(ConfigModel):
    implicit_r: bool = False
    steps: tuple[RoutingStepConfig, ...] = Field(min_length=1)


class TerminalConfig(ConfigModel):
    on_empty: EmptyAction
    on_unavailable: EmptyAction


class OptionalRConfig(ConfigModel):
    position: Literal["append"]
    operation: ReadOperation


class RoutingConfig(ConfigModel):
    schema_version: Literal["1"]
    rule_version: str
    terminal: TerminalConfig
    optional_r: OptionalRConfig
    intents: dict[str, IntentRuleConfig]
