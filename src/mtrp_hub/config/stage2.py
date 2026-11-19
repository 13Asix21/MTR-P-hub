"""Strict stage 2 configuration and trusted Registry coordinate bindings."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from mtrp_hub.canonical import sha256_json
from mtrp_hub.config.loader import ConfigLoader, LoadedConfig
from mtrp_hub.domain.enums import LocationKind, StoreKind
from mtrp_hub.domain.models import FrozenModel, ProjectCoordinate, RelativePath, SafeId, Sha256, validate_relative_path
from mtrp_hub.errors import MtrpError, configuration_error, safe_validation_field

Stage2Mode = Literal["fixture", "obsidian-local", "workbuddy-manifest"]
_LEXIANG_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_G1_ROOT = "2-Permanent"
_G1_EXCLUSIONS = frozenset(
    {
        "2-Permanent/Resources/MTR-P-System",
        "2-Permanent/Areas/AI-Assets/_templates",
    }
)


def _contains_path(parent: str, child: str) -> bool:
    """Compare normalized absolute paths without filesystem access."""
    parent_normalized = os.path.normcase(os.path.normpath(parent))
    child_normalized = os.path.normcase(os.path.normpath(child))
    try:
        return os.path.commonpath((parent_normalized, child_normalized)) == parent_normalized
    except ValueError:
        return False


def validate_posix_relative_path(value: str, *, allow_root: bool = False) -> str:
    """Validate one NFC POSIX relative path without touching the filesystem."""
    normalized = validate_relative_path(value)
    if not allow_root and normalized in ("", "."):
        raise ValueError("a Vault root cannot be scanned as a managed root")
    if PureWindowsPath(normalized).is_absolute() or PurePosixPath(normalized).is_absolute():
        raise ValueError("absolute paths are forbidden")
    return normalized


class ManagedRootConfig(FrozenModel):
    root_id: SafeId
    path: RelativePath


class ObsidianStage2Config(FrozenModel):
    mode: Stage2Mode = "fixture"
    vault_id: SafeId
    vault_root: str = Field(min_length=1, max_length=4096)
    managed_roots: tuple[ManagedRootConfig, ...] = Field(min_length=1, max_length=16)
    excluded_roots: tuple[RelativePath, ...] = Field(max_length=64, default=())
    trial_tag: Literal["mtr/trial"] = "mtr/trial"
    rarely_tag: Literal["mtr/rarely"] = "mtr/rarely"
    read_only: Literal[True] = True
    follow_symlinks: Literal[False] = False
    g1_confirmed: bool = False
    max_file_bytes: int = Field(gt=0, le=16 * 1024 * 1024, default=1_048_576)
    max_scan_files: int = Field(gt=0, le=5000, default=5000)
    max_results: int = Field(gt=0, le=100, default=100)

    @model_validator(mode="after")
    def validate_scope(self) -> "ObsidianStage2Config":
        root_ids = [item.root_id for item in self.managed_roots]
        roots = [item.path for item in self.managed_roots]
        if len(set(root_ids)) != len(root_ids) or len(set(roots)) != len(roots):
            raise ValueError("managed root IDs and paths must be unique")
        for index, left in enumerate(roots):
            for right in roots[index + 1 :]:
                if left.startswith(f"{right}/") or right.startswith(f"{left}/"):
                    raise ValueError("nested managed roots are forbidden")
        excluded = set(self.excluded_roots)
        if len(excluded) != len(self.excluded_roots):
            raise ValueError("excluded roots must be unique")
        for managed in roots:
            if any(managed == root or managed.startswith(f"{root}/") for root in excluded):
                raise ValueError("an excluded root cannot cover an entire managed root")
        if self.mode == "fixture":
            validate_posix_relative_path(self.vault_root)
            if not self.vault_root.startswith("tests/fixtures/"):
                raise ValueError("fixture Vault roots must remain under tests/fixtures")
        else:
            if (
                not self.g1_confirmed
                or not os.path.isabs(self.vault_root)
                or PureWindowsPath(self.vault_root).is_absolute() is False and not self.vault_root.startswith("/")
            ):
                raise ValueError("real Vault access requires G1 and an explicit absolute root")
            if roots != [_G1_ROOT] or excluded != _G1_EXCLUSIONS:
                raise ValueError("real Vault scope must exactly match the confirmed G1 scope")
        if "\x00" in self.vault_root or any(ord(char) < 32 for char in self.vault_root):
            raise ValueError("Vault root contains control characters")
        return self

    def scope_payload(self) -> dict[str, object]:
        """Return the relative, non-secret scope used by Core step arguments."""
        return {
            "vault_id": self.vault_id,
            "managed_roots": [item.model_dump(mode="json") for item in self.managed_roots],
            "excluded_roots": list(self.excluded_roots),
            "trial_tag": self.trial_tag,
            "rarely_tag": self.rarely_tag,
            "follow_symlinks": self.follow_symlinks,
            "max_file_bytes": self.max_file_bytes,
            "max_scan_files": self.max_scan_files,
            "max_results": self.max_results,
        }

    @property
    def scope_sha256(self) -> Sha256:
        return sha256_json(self.scope_payload())


class LexiangStage2Config(FrozenModel):
    mode: Stage2Mode = "fixture"
    team_id: str
    space_id: str
    root_entry_id: str
    read_only: Literal[True] = True
    allow_empty: Literal[True] = True
    search_modes: tuple[Literal["text", "vector"], ...] = Field(min_length=1, max_length=2, default=("text", "vector"))
    max_results: int = Field(gt=0, le=5, default=5)
    vector_threshold: float = Field(ge=0.0, le=1.0, default=0.3)
    max_entry_bytes: int = Field(gt=0, le=4 * 1024 * 1024, default=262_144)
    timeout_seconds: int = Field(gt=0, le=60, default=10)

    @field_validator("team_id", "space_id", "root_entry_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        if not _LEXIANG_ID_RE.fullmatch(value):
            raise ValueError("Lexiang IDs must be full 32-character lowercase hexadecimal values")
        return value

    @field_validator("vector_threshold")
    @classmethod
    def validate_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("threshold must be finite")
        return value

    @model_validator(mode="after")
    def validate_search_modes(self) -> "LexiangStage2Config":
        if len(set(self.search_modes)) != len(self.search_modes):
            raise ValueError("search modes must be unique")
        return self


class GitHubRepositoryBinding(FrozenModel):
    project_id: SafeId
    repo_id: SafeId
    location_id: SafeId
    owner: str = Field(min_length=1, max_length=39)
    repo: str = Field(min_length=1, max_length=100)
    read_paths: tuple[RelativePath, ...] = Field(max_length=16, default=())
    adr_files: tuple[RelativePath, ...] = Field(max_length=16, default=())

    @field_validator("owner")
    @classmethod
    def validate_owner(cls, value: str) -> str:
        if not _GITHUB_OWNER_RE.fullmatch(value) or "--" in value:
            raise ValueError("invalid GitHub owner")
        return value

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, value: str) -> str:
        if not _GITHUB_REPO_RE.fullmatch(value) or value in (".", "..") or ".." in value:
            raise ValueError("invalid GitHub repository name")
        return value

    @model_validator(mode="after")
    def validate_paths(self) -> "GitHubRepositoryBinding":
        if len(set(self.read_paths)) != len(self.read_paths) or len(set(self.adr_files)) != len(self.adr_files):
            raise ValueError("repository read paths must be unique")
        return self


class GitHubStage2Config(FrozenModel):
    mode: Stage2Mode = "fixture"
    read_only: Literal[True] = True
    timeout_seconds: int = Field(gt=0, le=60, default=10)
    max_file_bytes: int = Field(gt=0, le=16 * 1024 * 1024, default=1_048_576)
    repositories: tuple[GitHubRepositoryBinding, ...] = Field(max_length=64, default=())

    @model_validator(mode="after")
    def validate_unique_bindings(self) -> "GitHubStage2Config":
        keys = [(item.project_id, item.repo_id, item.location_id) for item in self.repositories]
        if len(set(keys)) != len(keys):
            raise ValueError("GitHub Registry bindings must be unique")
        return self

    def binding_for(self, coordinate: ProjectCoordinate) -> GitHubRepositoryBinding:
        key = (coordinate.project_id, coordinate.repo_id, coordinate.location_id)
        for binding in self.repositories:
            if (binding.project_id, binding.repo_id, binding.location_id) == key:
                return binding
        raise configuration_error(
            "GIT_REGISTRY_INVALID",
            "github_p.repositories",
            "The resolved Registry coordinate has no trusted GitHub binding.",
        )


class WorkBuddyManifestConfig(FrozenModel):
    schema_version: Literal["1"] = "1"
    tool_profile: Literal["stage2-readonly-v1"] = "stage2-readonly-v1"
    max_steps: int = Field(gt=0, le=32, default=32)
    max_entry_reads: int = Field(gt=0, le=10, default=10)
    max_envelope_bytes: int = Field(gt=0, le=16 * 1024 * 1024, default=16 * 1024 * 1024)
    persist_raw_responses: Literal[False] = False


class Stage2Config(FrozenModel):
    schema_version: Literal["1"] = "1"
    obsidian: ObsidianStage2Config
    lexiang_m: LexiangStage2Config
    github_p: GitHubStage2Config
    workbuddy_manifest: WorkBuddyManifestConfig

    @model_validator(mode="after")
    def validate_execution_limits(self) -> "Stage2Config":
        enabled_searches = len(self.lexiang_m.search_modes)
        required_entry_reads = enabled_searches * self.lexiang_m.max_results
        if self.workbuddy_manifest.max_entry_reads < required_entry_reads:
            raise ValueError("max_entry_reads must cover all enabled search candidates")
        modes = {self.obsidian.mode, self.lexiang_m.mode, self.github_p.mode}
        if modes == {"fixture"}:
            return self
        if self.obsidian.mode != "obsidian-local" or self.lexiang_m.mode != "workbuddy-manifest" or self.github_p.mode != "workbuddy-manifest":
            raise ValueError("real stage 2 requires obsidian-local plus workbuddy-manifest M/P modes")
        return self

    def execution_config_payload(self) -> dict[str, object]:
        """Include every execution semantic; the absolute root only enters this hash."""
        return self.model_dump(mode="json")

    @property
    def execution_config_hash(self) -> Sha256:
        return sha256_json(self.execution_config_payload())

    def validate_requested_mode(self, mode: Stage2Mode, *, has_remote: bool, has_core: bool) -> None:
        if mode == "fixture":
            if {self.obsidian.mode, self.lexiang_m.mode, self.github_p.mode} != {"fixture"}:
                raise configuration_error("STAGE2_CONFIG_INVALID", "mode", "Fixture execution requires an all-fixture configuration.")
        elif mode == "workbuddy-manifest":
            if has_remote and (self.lexiang_m.mode != "workbuddy-manifest" or self.github_p.mode != "workbuddy-manifest"):
                raise configuration_error("STAGE2_CONFIG_INVALID", "mode", "Remote execution is not enabled by trusted configuration.")
            if has_core and self.obsidian.mode != "obsidian-local":
                raise configuration_error("STAGE2_CONFIG_INVALID", "obsidian.mode", "Core Vault execution is not enabled.")
        elif mode == "obsidian-local":
            if has_remote or not has_core or self.obsidian.mode != "obsidian-local":
                raise configuration_error("STAGE2_CONFIG_INVALID", "mode", "obsidian-local only supports Core-only T/R routes.")


@dataclass(frozen=True)
class LoadedStage2Config:
    """The three legacy YAML files plus the explicit fourth stage 2 file."""

    base: LoadedConfig
    config: Stage2Config
    config_dir: str
    audit_path: Path

    @property
    def stage2(self) -> Stage2Config:
        return self.config

    @property
    def registry_hash(self) -> str:
        return self.base.registry_hash

    @property
    def policy_hash(self) -> str:
        return self.base.policy_hash

    @property
    def stores(self):  # type: ignore[no-untyped-def]
        return self.base.stores

    @property
    def projects(self):  # type: ignore[no-untyped-def]
        return self.base.projects

    @property
    def policy(self):  # type: ignore[no-untyped-def]
        return self.base.policy

    @property
    def obsidian(self) -> ObsidianStage2Config:
        return self.config.obsidian

    @property
    def lexiang_m(self) -> LexiangStage2Config:
        return self.config.lexiang_m

    @property
    def github_p(self) -> GitHubStage2Config:
        return self.config.github_p

    @property
    def workbuddy_manifest(self) -> WorkBuddyManifestConfig:
        return self.config.workbuddy_manifest


class Stage2ConfigLoader:
    """Load trusted configuration without probing a Vault or invoking a tool."""

    def __init__(self, base_loader: ConfigLoader | None = None) -> None:
        self._base_loader = base_loader or ConfigLoader()

    def load(self, config_dir: str, *, audit_path: str | Path | None = None) -> LoadedStage2Config:
        base = self._base_loader.load(config_dir)
        path = Path(config_dir) / "stage2.yaml"
        try:
            raw = ConfigLoader._read_yaml(path)
        except MtrpError as exc:
            code = "STAGE2_CONFIG_NOT_FOUND" if exc.error_code == "CONFIG_NOT_FOUND" else "STAGE2_CONFIG_INVALID"
            raise MtrpError(code, "Stage 2 configuration is missing or unreadable.", field="stage2.yaml") from exc
        try:
            config = Stage2Config.model_validate(raw)
        except ValidationError as exc:
            field = safe_validation_field("stage2", exc.errors(include_url=False, include_input=False))
            raise configuration_error("STAGE2_CONFIG_INVALID", field, "Stage 2 configuration schema validation failed.") from exc
        self._validate_registry_bindings(base, config)
        resolved_audit = Path(audit_path) if audit_path is not None else Path(config_dir) / ".mtrp" / "decision-log.sqlite3"
        self.ensure_audit_outside_vault(config_dir, resolved_audit, config.obsidian)
        return LoadedStage2Config(base=base, config=config, config_dir=config_dir, audit_path=resolved_audit)

    @staticmethod
    def _validate_registry_bindings(base: LoadedConfig, config: Stage2Config) -> None:
        t_store = base.stores.get(StoreKind.T)
        r_store = base.stores.get(StoreKind.R)
        if t_store.vault_id != config.obsidian.vault_id or r_store.vault_id != config.obsidian.vault_id:
            raise configuration_error("STAGE2_CONFIG_INVALID", "obsidian.vault_id", "Stage 2 Vault ID differs from the Store Registry.")
        for binding in config.github_p.repositories:
            coordinate = base.projects.resolve_default(binding.project_id, binding.repo_id)
            if coordinate.location_id != binding.location_id or coordinate.location_kind is not LocationKind.GITHUB:
                raise configuration_error("GIT_REGISTRY_INVALID", "github_p.repositories", "A GitHub binding does not match the authoritative Registry Location.")

    @staticmethod
    def ensure_audit_outside_vault(
        config_dir: str | Path,
        audit_path: str | Path,
        obsidian: ObsidianStage2Config,
    ) -> None:
        """Perform lexical absolute containment checks before SQLite creation."""
        vault_absolute = os.path.abspath(obsidian.vault_root)
        config_absolute = os.path.abspath(os.fspath(config_dir))
        audit_absolute = os.path.abspath(os.fspath(audit_path))
        if _contains_path(vault_absolute, config_absolute):
            raise configuration_error("CONFIG_INSIDE_VAULT", "config_dir", "Configuration must be outside the Vault.")
        if _contains_path(vault_absolute, audit_absolute):
            raise configuration_error("AUDIT_INSIDE_VAULT", "config_dir", "The Decision Log must be outside the Vault.")
