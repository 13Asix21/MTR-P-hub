"""本地运行目录、配置来源和回环 Web 安全校验。

本机网页防护不是身份认证；本阶段不引入伪登录或用户凭据。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .settings import PanelError


CONFIG_FILENAMES = ("stage2.yaml", "stores.yaml", "projects.yaml", "routing.yaml")


@dataclass(frozen=True)
class RuntimePaths:
    """已规范化的本机目录；日志与 API 不回显这些绝对路径。"""

    data_dir: Path
    profile_path: Path
    legacy_profile_path: Path
    explicit_data_dir: bool
    development: bool


def application_directory() -> Path:
    """返回面板应用目录，不依赖进程当前工作目录。"""

    return Path(__file__).resolve().parent.parent


def legacy_profile_path() -> Path:
    return application_directory() / "data" / "users.json"


def _absolute_directory(raw: str | os.PathLike[str], code: str, *, label: str = "数据目录") -> Path:
    candidate = str(raw).strip()
    if not candidate or "\x00" in candidate:
        raise PanelError(code, f"{label}不能为空或包含无效字符。")
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        raise PanelError(code, f"{label}必须使用本地绝对路径。")
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PanelError(code, f"{label}无法规范化，请选择有效的本地路径。") from exc


def resolve_runtime_paths(
    explicit_data_dir: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    packaged: bool | None = None,
) -> RuntimePaths:
    """显式参数 > 环境变量 > OS 每用户目录；源码开发默认保留旧档案位置。"""

    environment = os.environ if environ is None else environ
    system = sys.platform if platform_name is None else platform_name
    frozen = bool(getattr(sys, "frozen", False)) if packaged is None else packaged
    development = not frozen and environment.get("MTRP_PANEL_PACKAGED") != "1"
    explicit = explicit_data_dir is not None or bool(environment.get("MTRP_PANEL_DATA_DIR"))
    if explicit_data_dir is not None:
        data_dir = _absolute_directory(explicit_data_dir, "DATA_DIR_INVALID")
    elif environment.get("MTRP_PANEL_DATA_DIR"):
        data_dir = _absolute_directory(environment["MTRP_PANEL_DATA_DIR"], "DATA_DIR_INVALID")
    elif development:
        # 仅为旧源码开发版提供无迁移兼容；显式新目录绝不复制此档案。
        data_dir = application_directory() / "data"
    elif system.startswith("win"):
        base = environment.get("LOCALAPPDATA")
        root = _absolute_directory(base, "DATA_DIR_INVALID") if base else Path.home() / "AppData" / "Local"
        data_dir = root / "MTRP-ReviewWorkbench"
    elif system == "darwin":
        data_dir = Path.home() / "Library" / "Application Support" / "MTRP-ReviewWorkbench"
    else:
        base = environment.get("XDG_DATA_HOME")
        root = _absolute_directory(base, "DATA_DIR_INVALID") if base else Path.home() / ".local" / "share"
        data_dir = root / "MTRP-ReviewWorkbench"
    data_dir = data_dir.resolve(strict=False)
    return RuntimePaths(
        data_dir=data_dir,
        profile_path=data_dir / "users.json",
        legacy_profile_path=(
            _absolute_directory(
                environment["MTRP_PANEL_LEGACY_PROFILE"], "DATA_DIR_INVALID", label="旧版档案路径",
            )
            if environment.get("MTRP_PANEL_LEGACY_PROFILE")
            else legacy_profile_path()
        ),
        explicit_data_dir=explicit,
        development=development,
    )


def migration_status(paths: RuntimePaths) -> dict[str, str | bool]:
    """只报告旧档案是否存在，不复制、不迁移、不返回任何绝对路径。"""

    legacy_exists = (
        paths.explicit_data_dir
        and paths.profile_path != paths.legacy_profile_path
        and paths.legacy_profile_path.is_file()
        and not paths.profile_path.exists()
    )
    return {
        "legacy_data_detected": legacy_exists,
        "migration_required": legacy_exists,
        "status": "migration_available" if legacy_exists else "current",
        "message": "检测到旧版本地数据，需显式迁移。" if legacy_exists else "当前数据目录已独立使用。",
    }


def migrate_legacy_profile(paths: RuntimePaths) -> dict[str, str | bool]:
    """仅在用户显式确认后复制旧档案；源文件保持原字节，不移动、不合并。"""

    if not migration_status(paths)["migration_required"]:
        raise PanelError("MIGRATION_NOT_AVAILABLE", "当前没有可迁移的旧版档案，或目标档案已经存在。", 409)
    from .user_profiles import ProfileStore

    # 空档案不是有效旧台账，避免生命周期初始化抢先建 default 后误导迁移。
    try:
        legacy_store = ProfileStore(paths.legacy_profile_path)
        legacy_users = legacy_store.list_users()
        if not legacy_users:
            raise PanelError("PROFILE_STORE_INVALID", "旧版档案没有可迁移的用户。", 503)
        for user in legacy_users:
            legacy_store.get_user_review(user.user_id)
    except OSError as exc:
        raise PanelError("MIGRATION_READ_FAILED", "旧版档案无法读取，未进行迁移。", 503) from exc
    temporary_path: Path | None = None
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".users-migration-", suffix=".tmp", dir=paths.data_dir, delete=False
        ) as target, paths.legacy_profile_path.open("rb") as source:
            temporary_path = Path(target.name)
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        migrated_store = ProfileStore(temporary_path)
        for user in migrated_store.list_users():
            migrated_store.get_user_review(user.user_id)
        # 同文件系统硬链接只在目标不存在时原子发布；绝不按目标大小或 stat 推断所有权。
        # Windows 与 POSIX 均不调用会覆盖竞争者的 os.replace。硬链接不可用时明确失败，不降级为覆盖写。
        os.link(temporary_path, paths.profile_path)
    except FileExistsError as exc:
        raise PanelError("MIGRATION_NOT_AVAILABLE", "目标档案已存在，未覆盖任何数据。", 409) from exc
    except PanelError:
        raise
    except OSError as exc:
        raise PanelError("MIGRATION_WRITE_FAILED", "旧版档案迁移未完成，原档案保持不变。", 500) from exc
    finally:
        # 唯一可清理的是 NamedTemporaryFile 返回的自有路径；目标路径可能已属于竞争者。
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return {
        "migrated": True,
        "status": "current",
        "message": "旧版档案已复制到独立数据目录；原档案保持不变。",
    }


def resolve_config_dir(
    paths: RuntimePaths,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """配置优先级：显式环境变量 > 数据根配置 > 开发配置 > 安全模板。"""

    environment = os.environ if environ is None else environ
    if environment.get("MTRP_PANEL_CONFIG_DIR"):
        return _absolute_directory(environment["MTRP_PANEL_CONFIG_DIR"], "CONFIG_DIR_INVALID")
    user_config = paths.data_dir / "config"
    if (user_config / CONFIG_FILENAMES[0]).is_file():
        return user_config
    if paths.development and not paths.explicit_data_dir:
        repository_config = application_directory().parents[1] / "config"
        if (repository_config / CONFIG_FILENAMES[0]).is_file():
            return repository_config
    return application_directory() / "panel" / "default_config"


def loopback_host_valid(host: str | None) -> bool:
    """Host 必须是明确的回环主机，域名解析或后缀欺骗一律拒绝。"""

    if not isinstance(host, str) or not host or any(character.isspace() for character in host):
        return False
    try:
        parsed = urlsplit(f"http://{host}")
        _port = parsed.port
        return parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.username is None
    except ValueError:
        return False


def same_loopback_origin(origin: str, host: str) -> bool:
    """Origin 仅接受 HTTP 回环源，并要求其有效端口与 Host 完全一致。"""

    if not loopback_host_valid(host):
        return False
    try:
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.path or parsed.query or parsed.fragment:
            return False
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return False
        host_parts = urlsplit(f"http://{host}")
        return parsed.port == host_parts.port
    except ValueError:
        return False


def validate_web_request(method: str, headers: Mapping[str, str]) -> None:
    """在请求进入路由前拒绝 DNS rebinding、跨源写和非 JSON 状态变更。"""

    normalized = {key.lower(): value for key, value in headers.items()}
    if not loopback_host_valid(normalized.get("host")):
        raise PanelError("LOOPBACK_HOST_REQUIRED", "本地面板只接受回环地址请求。", 403)
    origin = normalized.get("origin")
    if origin and not same_loopback_origin(origin, normalized["host"]):
        raise PanelError("CROSS_ORIGIN_REJECTED", "本地面板拒绝跨源网页请求。", 403)
    if normalized.get("sec-fetch-site", "").lower() == "cross-site":
        raise PanelError("CROSS_SITE_REQUEST_REJECTED", "本地面板拒绝跨站请求。", 403)
    if method.upper() in {"POST", "PATCH", "PUT", "DELETE"}:
        content_length = normalized.get("content-length", "0").strip()
        try:
            has_body = int(content_length) > 0
        except ValueError:
            has_body = True
        if "transfer-encoding" in normalized:
            has_body = True
        content_type = normalized.get("content-type", "").split(";", 1)[0].strip().lower()
        if has_body and content_type != "application/json":
            raise PanelError("JSON_CONTENT_TYPE_REQUIRED", "带请求体的状态变更必须使用 application/json。", 415)


__all__ = [
    "CONFIG_FILENAMES",
    "RuntimePaths",
    "application_directory",
    "legacy_profile_path",
    "loopback_host_valid",
    "migration_status",
    "migrate_legacy_profile",
    "resolve_config_dir",
    "resolve_runtime_paths",
    "same_loopback_origin",
    "validate_web_request",
]
