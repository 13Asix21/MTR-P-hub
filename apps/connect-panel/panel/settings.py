"""面板配置加载与共享输入校验；所有配置文件均只读打开。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


class PanelError(Exception):
    """可安全返回给前端的封闭错误，不携带底层异常或堆栈。"""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


@dataclass(frozen=True)
class ObsidianConfig:
    """Obsidian T/R 视图的只读扫描配置。"""

    vault_root: str = ""
    managed_roots: tuple[str, ...] = ("2-Permanent",)
    excluded_roots: tuple[str, ...] = ()
    trial_tag: str = "mtr/trial"
    rarely_tag: str = "mtr/rarely"
    mode: str = "obsidian-local"
    read_only: bool = True
    follow_symlinks: bool = False
    max_file_bytes: int = 1_048_576
    max_scan_files: int = 5000


@dataclass(frozen=True)
class LexiangConfig:
    """乐享 M 库的 WorkBuddy manifest 配置。"""

    mode: str = ""
    team_id: str = ""
    space_id: str = ""
    root_entry_id: str = ""
    read_only: bool = False
    timeout_seconds: int = 10


@dataclass(frozen=True)
class GithubRepository:
    """一个已登记的 GitHub 仓库坐标。"""

    project_id: str = ""
    repo_id: str = ""
    owner: str = ""
    repo: str = ""
    default_branch: str = "main"


@dataclass(frozen=True)
class GithubConfig:
    """GitHub P 库配置。"""

    mode: str = ""
    read_only: bool = True
    timeout_seconds: int = 10
    repositories: tuple[GithubRepository, ...] = ()


@dataclass(frozen=True)
class PanelSettings:
    """面板完整配置及派生的安全默认值。"""

    repository_root: Path
    config_dir: Path
    audit_path: Path
    obsidian: ObsidianConfig
    lexiang: LexiangConfig
    github: GithubConfig
    store_count: int = 0
    all_stores_read_only: bool = True
    routing_rule_version: str = ""


def _read_yaml(path: Path) -> dict[str, Any]:
    """读取一个 UTF-8 YAML 映射，失败时只返回封闭错误。"""

    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            yaml = YAML(typ="safe")
            data = yaml.load(stream)
    except (OSError, UnicodeError, YAMLError, ValueError) as exc:
        raise PanelError("CONFIG_READ_FAILED", "配置文件无法读取或格式无效。", 503) from exc
    if not isinstance(data, dict):
        raise PanelError("CONFIG_SCHEMA_INVALID", "配置文件顶层必须是映射。", 503)
    return data


def _mapping(value: Any, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    return value if isinstance(value, dict) else (fallback or {})


def _string(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


def _boolean(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _integer(value: Any, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, minimum), maximum)


def _managed_root_paths(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ("2-Permanent",)
    roots: list[str] = []
    for item in value:
        if isinstance(item, dict):
            candidate = _string(item.get("path"))
        else:
            candidate = _string(item)
        if candidate:
            roots.append(candidate)
    return tuple(roots) or ("2-Permanent",)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _repository_from_projects(projects_data: dict[str, Any]) -> GithubRepository:
    """在 stage2 未登记仓库时，从 projects.yaml 的公开远端派生坐标。"""

    projects = projects_data.get("projects")
    if not isinstance(projects, list):
        return GithubRepository()
    for project in projects:
        project_map = _mapping(project)
        repositories = project_map.get("repositories")
        if not isinstance(repositories, list):
            continue
        for repository in repositories:
            repository_map = _mapping(repository)
            default_branch = _string(repository_map.get("default_branch"), "main")
            locations = repository_map.get("locations")
            if not isinstance(locations, list):
                continue
            for location in locations:
                location_map = _mapping(location)
                remote = _string(location_map.get("remote"))
                if not remote:
                    continue
                parsed = urlsplit(remote)
                parts = [part for part in parsed.path.split("/") if part]
                if parsed.scheme in ("http", "https") and len(parts) >= 2:
                    return GithubRepository(
                        project_id=_string(project_map.get("project_id")),
                        repo_id=_string(repository_map.get("repo_id")),
                        owner=parts[-2],
                        repo=parts[-1].removesuffix(".git"),
                        default_branch=default_branch,
                    )
    return GithubRepository()


def load_settings(config_dir: str | Path | None = None) -> PanelSettings:
    """从主仓 config 目录加载面板配置，不创建或修改任何文件。"""

    module_path = Path(__file__).resolve()
    repository_root = module_path.parents[3]
    if config_dir is not None or os.environ.get("MTRP_PANEL_CONFIG_DIR"):
        selected_config = config_dir or os.environ["MTRP_PANEL_CONFIG_DIR"]
    else:
        # 延迟导入避免与 runtime 的 PanelError 类型依赖形成循环。
        from .runtime import resolve_config_dir, resolve_runtime_paths

        selected_config = resolve_config_dir(resolve_runtime_paths())
    selected_dir = Path(selected_config).expanduser().absolute()

    stage2 = _read_yaml(selected_dir / "stage2.yaml")
    stores = _read_yaml(selected_dir / "stores.yaml")
    projects = _read_yaml(selected_dir / "projects.yaml")
    routing = _read_yaml(selected_dir / "routing.yaml")

    obsidian_data = _mapping(stage2.get("obsidian"))
    obsidian = ObsidianConfig(
        vault_root=_string(obsidian_data.get("vault_root")),
        managed_roots=_managed_root_paths(obsidian_data.get("managed_roots")),
        excluded_roots=_string_tuple(obsidian_data.get("excluded_roots")),
        trial_tag=_string(obsidian_data.get("trial_tag"), "mtr/trial"),
        rarely_tag=_string(obsidian_data.get("rarely_tag"), "mtr/rarely"),
        mode=_string(obsidian_data.get("mode"), "obsidian-local"),
        read_only=_boolean(obsidian_data.get("read_only"), True),
        follow_symlinks=_boolean(obsidian_data.get("follow_symlinks"), False),
        max_file_bytes=_integer(obsidian_data.get("max_file_bytes"), 1_048_576, 1, 16_777_216),
        max_scan_files=_integer(obsidian_data.get("max_scan_files"), 5000, 1, 100_000),
    )

    lexiang_data = _mapping(stage2.get("lexiang_m"))
    lexiang = LexiangConfig(
        mode=_string(lexiang_data.get("mode")),
        team_id=_string(lexiang_data.get("team_id")),
        space_id=_string(lexiang_data.get("space_id")),
        root_entry_id=_string(lexiang_data.get("root_entry_id")),
        read_only=_boolean(lexiang_data.get("read_only"), False),
        timeout_seconds=_integer(lexiang_data.get("timeout_seconds"), 10, 1, 30),
    )

    github_data = _mapping(stage2.get("github_p"))
    repositories: list[GithubRepository] = []
    raw_repositories = github_data.get("repositories")
    if isinstance(raw_repositories, list):
        for item in raw_repositories:
            repository = _mapping(item)
            owner = _string(repository.get("owner"))
            repo = _string(repository.get("repo"))
            if owner and repo:
                repositories.append(
                    GithubRepository(
                        project_id=_string(repository.get("project_id")),
                        repo_id=_string(repository.get("repo_id")),
                        owner=owner,
                        repo=repo,
                    )
                )
    if not repositories:
        fallback = _repository_from_projects(projects)
        if fallback.owner and fallback.repo:
            repositories.append(fallback)
    github = GithubConfig(
        mode=_string(github_data.get("mode")),
        read_only=_boolean(github_data.get("read_only"), True),
        timeout_seconds=_integer(github_data.get("timeout_seconds"), 10, 1, 30),
        repositories=tuple(repositories),
    )

    raw_stores = stores.get("stores")
    store_list = raw_stores if isinstance(raw_stores, list) else []
    all_read_only = all(
        isinstance(store, dict) and store.get("read_only") is True for store in store_list
    ) if store_list else False

    return PanelSettings(
        repository_root=repository_root,
        config_dir=selected_dir,
        audit_path=selected_dir / ".mtrp" / "decision-log.sqlite3",
        obsidian=obsidian,
        lexiang=lexiang,
        github=github,
        store_count=len(store_list),
        all_stores_read_only=all_read_only,
        routing_rule_version=_string(routing.get("rule_version")),
    )


def normalize_local_absolute_path(raw_path: str) -> Path:
    """接受本地绝对路径，拒绝 URL、UNC 网络路径与相对路径。"""

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise PanelError("PATH_REQUIRED", "请输入本地目录的绝对路径。")
    candidate = raw_path.strip()
    if "\x00" in candidate:
        raise PanelError("PATH_INVALID", "路径包含无效字符。")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", candidate):
        raise PanelError("URL_PATH_REJECTED", "出于安全原因，目录校验只接受本地绝对路径，不接受 URL。")
    if candidate.startswith(("\\\\", "//")):
        raise PanelError("NETWORK_PATH_REJECTED", "出于安全原因，不接受 UNC 网络路径。")
    path = Path(candidate)
    if not path.is_absolute():
        raise PanelError("RELATIVE_PATH_REJECTED", "路径必须是本地绝对路径。")
    return Path(os.path.abspath(os.path.normpath(candidate)))


__all__ = [
    "GithubConfig",
    "GithubRepository",
    "LexiangConfig",
    "ObsidianConfig",
    "PanelError",
    "PanelSettings",
    "load_settings",
    "normalize_local_absolute_path",
]
