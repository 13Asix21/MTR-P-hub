"""本地多用户档案层；唯一允许写入的位置是面板自管 data/users.json。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .github_check import validate_github_coordinates
from .settings import PanelError, normalize_local_absolute_path


_USER_ID_PATTERN = re.compile(r"^[a-z0-9-]{1,32}$")
_ROLES = ("owner", "editor", "viewer")
_MAX_PROFILE_BYTES = 1_000_000
_MAX_USERS = 100


@dataclass(frozen=True)
class GithubRepoPreference:
    """用户关注的一个公开 GitHub 仓库坐标。"""

    owner: str = ""
    repo: str = ""


@dataclass(frozen=True)
class UserPreferences:
    """按用户隔离的路径、仓库与刷新偏好。"""

    vault_root: str = ""
    git_local_path: str = ""
    github_repos: tuple[GithubRepoPreference, ...] = ()
    auto_refresh_minutes: int | None = 0


@dataclass(frozen=True)
class UserProfile:
    """不含任何认证信息的本地用户档案。"""

    user_id: str
    display_name: str
    role: str
    preferences: UserPreferences


def preferences_to_dict(preferences: UserPreferences) -> dict[str, Any]:
    return {
        "vault_root": preferences.vault_root,
        "git_local_path": preferences.git_local_path,
        "github_repos": [
            {"owner": repository.owner, "repo": repository.repo}
            for repository in preferences.github_repos
        ],
        "auto_refresh_minutes": preferences.auto_refresh_minutes,
    }


def profile_to_dict(profile: UserProfile, *, include_preferences: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "user_id": profile.user_id,
        "display_name": profile.display_name,
        "role": profile.role,
    }
    if include_preferences:
        data["preferences"] = preferences_to_dict(profile.preferences)
    return data


class ProfileStore:
    """线程安全的单文件档案仓库，采用 UTF-8 原子写入和全量结构校验。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()

    @staticmethod
    def _validate_user_id(user_id: Any) -> str:
        if (
            not isinstance(user_id, str)
            or user_id in (".", "..")
            or "/" in user_id
            or "\\" in user_id
            or not _USER_ID_PATTERN.fullmatch(user_id)
        ):
            raise PanelError("USER_ID_INVALID", "user_id 只能包含 1–32 位小写字母、数字和连字符，且不能包含路径字符。")
        return user_id

    @staticmethod
    def _validate_display_name(display_name: Any) -> str:
        if not isinstance(display_name, str):
            raise PanelError("DISPLAY_NAME_INVALID", "display_name 必须是字符串。")
        value = display_name.strip()
        if not 1 <= len(value) <= 64 or any(ord(character) < 32 for character in value):
            raise PanelError("DISPLAY_NAME_INVALID", "display_name 长度须为 1–64 且不能包含控制字符。")
        return value

    @staticmethod
    def _validate_role(role: Any) -> str:
        if role not in _ROLES:
            raise PanelError("ROLE_INVALID", "role 只能是 owner、editor 或 viewer。")
        return str(role)

    @staticmethod
    def _parse_repository(raw: Any) -> GithubRepoPreference:
        if not isinstance(raw, dict) or set(raw) != {"owner", "repo"}:
            raise PanelError("GITHUB_PREFERENCE_INVALID", "每个 GitHub 偏好只能包含 owner 和 repo。")
        owner, repo = validate_github_coordinates(raw.get("owner", ""), raw.get("repo", ""))
        return GithubRepoPreference(owner=owner, repo=repo)

    @classmethod
    def _parse_preferences(cls, raw: Any) -> UserPreferences:
        if not isinstance(raw, dict):
            raise PanelError("PREFERENCES_INVALID", "preferences 必须是对象。")
        expected = {"vault_root", "git_local_path", "github_repos", "auto_refresh_minutes"}
        if set(raw) != expected:
            raise PanelError("PREFERENCES_INVALID", "preferences 字段不完整或包含未知字段。")
        vault_root = str(normalize_local_absolute_path(raw.get("vault_root")))
        git_local_path = str(normalize_local_absolute_path(raw.get("git_local_path")))
        raw_repositories = raw.get("github_repos")
        if not isinstance(raw_repositories, list) or len(raw_repositories) > 32:
            raise PanelError("GITHUB_PREFERENCE_INVALID", "github_repos 必须是至多 32 项的数组。")
        repositories = tuple(cls._parse_repository(item) for item in raw_repositories)
        auto_refresh = raw.get("auto_refresh_minutes")
        if auto_refresh is not None and (
            isinstance(auto_refresh, bool)
            or not isinstance(auto_refresh, int)
            or not (auto_refresh == 0 or 5 <= auto_refresh <= 60)
        ):
            raise PanelError("AUTO_REFRESH_INVALID", "auto_refresh_minutes 可空，或为 0（关闭）及 5–60 分钟。")
        return UserPreferences(
            vault_root=vault_root,
            git_local_path=git_local_path,
            github_repos=repositories,
            auto_refresh_minutes=auto_refresh,
        )

    @classmethod
    def _parse_user(cls, raw: Any) -> UserProfile:
        if not isinstance(raw, dict):
            raise PanelError("PROFILE_STORE_INVALID", "用户档案结构无效。", 503)
        expected = {"user_id", "display_name", "role", "preferences"}
        if set(raw) != expected:
            raise PanelError("PROFILE_STORE_INVALID", "用户档案字段不完整或包含未知字段。", 503)
        return UserProfile(
            user_id=cls._validate_user_id(raw.get("user_id")),
            display_name=cls._validate_display_name(raw.get("display_name")),
            role=cls._validate_role(raw.get("role")),
            preferences=cls._parse_preferences(raw.get("preferences")),
        )

    def _read_unlocked(self) -> tuple[dict[str, Any], list[UserProfile]]:
        try:
            raw_bytes = self._path.read_bytes()
        except FileNotFoundError as exc:
            raise PanelError("PROFILE_STORE_NOT_INITIALIZED", "用户档案尚未初始化。", 503) from exc
        except OSError as exc:
            raise PanelError("PROFILE_READ_FAILED", "用户档案无法读取。", 503) from exc
        if len(raw_bytes) > _MAX_PROFILE_BYTES:
            raise PanelError("PROFILE_STORE_INVALID", "用户档案超过安全读取上限。", 503)
        try:
            raw = json.loads(raw_bytes.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PanelError("PROFILE_STORE_INVALID", "用户档案不是有效的 UTF-8 JSON。", 503) from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != "1" or set(raw) != {"schema_version", "users"}:
            raise PanelError("PROFILE_STORE_INVALID", "用户档案 schema_version 或顶层结构无效。", 503)
        raw_users = raw.get("users")
        if not isinstance(raw_users, list) or len(raw_users) > _MAX_USERS:
            raise PanelError("PROFILE_STORE_INVALID", "users 必须是至多 100 项的数组。", 503)
        users = [self._parse_user(item) for item in raw_users]
        identifiers = [user.user_id for user in users]
        if len(set(identifiers)) != len(identifiers):
            raise PanelError("PROFILE_STORE_INVALID", "用户档案存在重复 user_id。", 503)
        return raw, users

    def _write_unlocked(self, raw: dict[str, Any]) -> None:
        temporary_name: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=".users-",
                suffix=".tmp",
                dir=self._path.parent,
                delete=False,
            ) as stream:
                temporary_name = stream.name
                json.dump(raw, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self._path)
        except OSError as exc:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    temporary_name = None
            raise PanelError("PROFILE_WRITE_FAILED", "用户档案写入失败。", 500) from exc

    def ensure_default(self, preferences: UserPreferences) -> UserProfile:
        """文件不存在或缺 default 时补建，不覆盖已有 default 偏好。"""

        with self._lock:
            validated = self._parse_preferences(preferences_to_dict(preferences))
            default = UserProfile(
                user_id="default",
                display_name="本机默认用户",
                role="owner",
                preferences=validated,
            )
            if self._path.exists():
                raw, users = self._read_unlocked()
                existing = next((user for user in users if user.user_id == "default"), None)
                if existing is not None:
                    return existing
                users.append(default)
                raw["users"] = [profile_to_dict(item) for item in users]
            else:
                raw = {"schema_version": "1", "users": [profile_to_dict(default)]}
            self._write_unlocked(raw)
            return default

    def list_users(self) -> list[UserProfile]:
        with self._lock:
            _raw, users = self._read_unlocked()
            return users

    def get_user(self, user_id: str) -> UserProfile:
        selected_id = self._validate_user_id(user_id)
        with self._lock:
            _raw, users = self._read_unlocked()
            user = next((item for item in users if item.user_id == selected_id), None)
            if user is None:
                raise PanelError("USER_NOT_FOUND", "指定用户不存在。", 404)
            return user

    def create_user(self, display_name: str, role: str = "viewer") -> UserProfile:
        clean_name = self._validate_display_name(display_name)
        clean_role = self._validate_role(role)
        with self._lock:
            raw, users = self._read_unlocked()
            if len(users) >= _MAX_USERS:
                raise PanelError("USER_LIMIT_REACHED", "本地用户档案最多允许 100 个用户。", 409)
            default = next((user for user in users if user.user_id == "default"), None)
            if default is None:
                raise PanelError("PROFILE_DEFAULT_MISSING", "用户档案缺少 default 用户。", 503)
            existing = {user.user_id for user in users}
            for _attempt in range(10):
                user_id = f"u-{uuid.uuid4().hex[:12]}"
                if user_id not in existing:
                    break
            else:
                raise PanelError("USER_ID_GENERATION_FAILED", "无法生成唯一用户 ID，请重试。", 500)
            user = UserProfile(
                user_id=user_id,
                display_name=clean_name,
                role=clean_role,
                preferences=default.preferences,
            )
            users.append(user)
            raw["users"] = [profile_to_dict(item) for item in users]
            self._write_unlocked(raw)
            return user

    def update_preferences(self, user_id: str, patch: dict[str, Any]) -> UserProfile:
        selected_id = self._validate_user_id(user_id)
        allowed = {"vault_root", "git_local_path", "github_repos", "auto_refresh_minutes"}
        if not isinstance(patch, dict) or any(field not in allowed for field in patch):
            raise PanelError("PREFERENCES_PATCH_INVALID", "偏好更新包含未知字段。")
        with self._lock:
            raw, users = self._read_unlocked()
            index = next((position for position, user in enumerate(users) if user.user_id == selected_id), None)
            if index is None:
                raise PanelError("USER_NOT_FOUND", "指定用户不存在。", 404)
            merged = preferences_to_dict(users[index].preferences)
            merged.update(patch)
            preferences = self._parse_preferences(merged)
            updated = UserProfile(
                user_id=users[index].user_id,
                display_name=users[index].display_name,
                role=users[index].role,
                preferences=preferences,
            )
            users[index] = updated
            raw["users"] = [profile_to_dict(item) for item in users]
            self._write_unlocked(raw)
            return updated


__all__ = [
    "GithubRepoPreference",
    "ProfileStore",
    "UserPreferences",
    "UserProfile",
    "preferences_to_dict",
    "profile_to_dict",
]
