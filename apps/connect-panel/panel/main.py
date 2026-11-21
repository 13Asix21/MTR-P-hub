"""FastAPI 路由与统一、无堆栈泄漏的 JSON 错误边界。"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api_common import (
    USER_ID_PATTERN as _USER_ID_PATTERN,
    StrictRequest,
    error as _error,
    ok as _ok,
)
from .audit_reader import AuditReader
from .git_local_check import check_git_local
from .github_check import check_github
from .lexiang_check import check_lexiang
from .review_api import create_review_router
from .runtime import migrate_legacy_profile, migration_status, resolve_runtime_paths, validate_web_request
from .user_profiles import (
    GithubRepoPreference,
    ProfileStore,
    UserPreferences,
    UserProfile,
    profile_to_dict,
)
from .settings import PanelError, PanelSettings, load_settings
from .vault_check import check_vault


logger = logging.getLogger("connect-panel")
_STATIC_INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"
_PROFILE_COMMIT_LOCK = threading.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """未迁移的数据根保持不落盘；普通新根按原有默认档案行为启动。"""

    _profile_for_read()
    yield


app = FastAPI(
    title="MTR-P 四库连接面板",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


class VaultCheckRequest(StrictRequest):
    user_id: str = Field(default="default", pattern=_USER_ID_PATTERN)
    path: str | None = Field(default=None, min_length=1, max_length=2048)
    managed_roots: list[str] | None = Field(default=None, max_length=64)
    excluded_roots: list[str] | None = Field(default=None, max_length=64)
    trial_tag: str | None = Field(default=None, max_length=128)
    rarely_tag: str | None = Field(default=None, max_length=128)


class GithubCheckRequest(StrictRequest):
    user_id: str = Field(default="default", pattern=_USER_ID_PATTERN)
    owner: str | None = Field(default=None, min_length=1, max_length=39)
    repo: str | None = Field(default=None, min_length=1, max_length=104)


class GitLocalCheckRequest(StrictRequest):
    user_id: str = Field(default="default", pattern=_USER_ID_PATTERN)
    path: str | None = Field(default=None, min_length=1, max_length=2048)


class LexiangCheckRequest(StrictRequest):
    user_id: str = Field(default="default", pattern=_USER_ID_PATTERN)


class CreateUserRequest(StrictRequest):
    display_name: str = Field(min_length=1, max_length=64)
    role: str = "viewer"


class GithubRepoInput(StrictRequest):
    owner: str = Field(min_length=1, max_length=39)
    repo: str = Field(min_length=1, max_length=104)


class MigrationRequest(StrictRequest):
    confirm: bool = False
    use_independent: bool = False


class PreferencesPatchRequest(StrictRequest):
    vault_root: str | None = Field(default=None, min_length=1, max_length=2048)
    git_local_path: str | None = Field(default=None, min_length=1, max_length=2048)
    github_repos: list[GithubRepoInput] | None = Field(default=None, max_length=32)
    auto_refresh_minutes: int | None = Field(default=None, ge=0, le=60)


@lru_cache(maxsize=4)
def _cached_settings(config_dir: str | None = None) -> PanelSettings:
    return load_settings(config_dir)


def get_settings() -> PanelSettings:
    """读取当前进程配置；不监听也不写入配置文件。"""

    return _cached_settings(None)


@lru_cache(maxsize=4)
def get_profile_store(profile_path: str | None = None) -> ProfileStore:
    """按规范化路径缓存档案；显式数据目录不会读取或复制旧版档案。"""

    selected_path = Path(profile_path) if profile_path is not None else resolve_runtime_paths().profile_path
    return ProfileStore(selected_path)


def _default_preferences(settings: PanelSettings) -> UserPreferences:
    repository = settings.github.repositories[0] if settings.github.repositories else None
    repositories = (
        (GithubRepoPreference(owner=repository.owner, repo=repository.repo),)
        if repository is not None
        else ()
    )
    return UserPreferences(
        vault_root=settings.obsidian.vault_root,
        git_local_path=str(settings.repository_root),
        github_repos=repositories,
        auto_refresh_minutes=0,
    )


def _migration_pending() -> bool:
    return bool(migration_status(resolve_runtime_paths())["migration_required"])


def _default_profile() -> UserProfile:
    """仅在内存中生成待决根的快照，不读取或写入目标路径。"""

    return get_profile_store().ephemeral_default(_default_preferences(get_settings()))


def _profile_for_read() -> UserProfile | None:
    """迁移待决时不创建文件；没有旧档的新目录仍按原行为初始化。"""

    if _migration_pending():
        return None
    return _profile_for_commit().get_user("default")


def _profile_for_commit() -> ProfileStore:
    """将待决根转成持久化新档，且不与同进程的迁移发布交错。"""

    with _PROFILE_COMMIT_LOCK:
        return _ready_profile_store()


def _ready_profile_store() -> ProfileStore:
    store = get_profile_store()
    store.ensure_default(_default_preferences(get_settings()))
    return store


def _get_profile(user_id: str) -> UserProfile:
    """普通只读检查在迁移待决时仅支持默认用户的内存档案。"""

    if _migration_pending():
        if user_id != "default":
            raise PanelError("USER_NOT_FOUND", "迁移待决时只有默认用户的临时视图。", 404)
        return _default_profile()
    return _profile_for_commit().get_user(user_id)


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Any:
    """为本地页面和 API 添加最小浏览器安全头。"""

    try:
        validate_web_request(request.method, request.headers)
    except PanelError as exc:
        response = JSONResponse(status_code=exc.http_status, content=_error(exc.code, exc.message))
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path == "/":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
    return response


@app.exception_handler(PanelError)
async def panel_error_handler(_request: Request, exc: PanelError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=_error(exc.code, exc.message))


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "ROUTE_NOT_FOUND" if exc.status_code == 404 else "HTTP_REQUEST_REJECTED"
    message = "请求的路由不存在。" if exc.status_code == 404 else "请求方法或地址不受支持。"
    return JSONResponse(status_code=exc.status_code, content=_error(code, message))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = sorted(
        {
            ".".join(str(part) for part in error.get("loc", ()) if part not in ("body", "query"))
            for error in exc.errors()
            if error.get("loc")
        }
    )
    message = "请求参数格式无效。"
    if fields:
        message += "请检查字段：" + "、".join(fields)
    return JSONResponse(status_code=422, content=_error("REQUEST_VALIDATION_FAILED", message))


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    # 仅记录异常类型，避免把用户路径、底层错误或 traceback 写入响应。
    logger.error("面板请求发生未处理异常：%s", type(exc).__name__)
    return JSONResponse(status_code=500, content=_error("PANEL_INTERNAL_ERROR", "面板处理失败，请检查服务日志后重试。"))


@app.get("/")
def index() -> FileResponse:
    """返回零外部依赖的单文件前端。"""

    if not _STATIC_INDEX.is_file():
        raise PanelError("FRONTEND_NOT_FOUND", "面板前端文件缺失。", 503)
    return FileResponse(_STATIC_INDEX, media_type="text/html; charset=utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    """仅暴露本机服务状态，不扫描用户文件，也不泄漏数据目录。"""

    return _ok(
        {
            "service": "mtrp-review-workbench",
            "ready": True,
            "local_only": True,
            "host": "127.0.0.1",
            "version": app.version,
            **migration_status(resolve_runtime_paths()),
        }
    )


@app.post("/api/runtime/migrate")
def migrate_runtime(payload: MigrationRequest) -> dict[str, Any]:
    """只有明确确认才复制旧档案，不自动移动、合并或删除源文件。"""

    if payload.use_independent:
        with _PROFILE_COMMIT_LOCK:
            if not _migration_pending():
                raise PanelError("MIGRATION_NOT_AVAILABLE", "当前没有待决的旧版档案迁移。", 409)
            store = _ready_profile_store()
        return _ok({
            "initialized": True,
            "migration_required": False,
            "user": profile_to_dict(store.get_user("default")),
            "message": "已使用独立目录建立新档案；旧版档案保持不变。",
        })
    if not payload.confirm:
        raise PanelError("MIGRATION_CONFIRMATION_REQUIRED", "迁移旧档案前必须明确确认。")
    with _PROFILE_COMMIT_LOCK:
        return _ok(migrate_legacy_profile(resolve_runtime_paths()))


@app.get("/api/overview")
def overview(user_id: str = Query(default="default", pattern=_USER_ID_PATTERN)) -> dict[str, Any]:
    """返回指定用户的预填偏好；不做文件扫描或网络探测。"""

    settings = get_settings()
    if _migration_pending():
        profile = _default_profile() if user_id == "default" else None
    else:
        profile = _profile_for_commit().get_user(user_id)
    if profile is None:
        raise PanelError("USER_NOT_FOUND", "迁移待决时只有默认用户的临时视图。", 404)
    preferences = profile.preferences
    repository = (
        preferences.github_repos[0]
        if preferences.github_repos
        else settings.github.repositories[0]
        if settings.github.repositories
        else None
    )
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "bind_host": "127.0.0.1",
        "current_user": profile_to_dict(profile),
        "authentication_enabled": False,
        "configuration": {
            "store_count": settings.store_count,
            "all_stores_read_only": settings.all_stores_read_only,
            "routing_rule_version": settings.routing_rule_version,
            "audit_database_present": settings.audit_path.is_file(),
        },
        "stores": {
            "M": {
                "status": "pending",
                "mode": settings.lexiang.mode or "未配置",
                "realtime_mcp_available": False,
                "summary": "配置与历史执行证据；实时验证需 WorkBuddy 会话。",
            },
            "T": {
                "status": "pending",
                "mode": settings.obsidian.mode,
                "vault_path": preferences.vault_root,
                "managed_roots": list(settings.obsidian.managed_roots),
                "excluded_roots": list(settings.obsidian.excluded_roots),
                "trial_tag": settings.obsidian.trial_tag,
                "scan_limit": settings.obsidian.max_scan_files,
                "summary": "等待手动执行本地文件只读扫描。",
            },
            "R": {
                "status": "pending",
                "mode": settings.obsidian.mode,
                "vault_path": preferences.vault_root,
                "rarely_tag": settings.obsidian.rarely_tag,
                "reserved": True,
                "summary": "与 T 共用扫描；生命周期管理属于阶段 3。",
            },
            "P": {
                "online": {
                    "status": "pending" if repository else "unconfigured",
                    "mode": settings.github.mode or "未配置",
                    "owner": repository.owner if repository else "",
                    "repo": repository.repo if repository else "",
                    "summary": "等待手动执行 GitHub API 无凭据实测。",
                },
                "local": {
                    "status": "pending",
                    "path": preferences.git_local_path,
                    "summary": "等待手动执行本地 Git 只读校验。",
                },
            },
        },
    }
    return _ok(data)


@app.get("/api/users")
def list_users() -> dict[str, Any]:
    """迁移待决时返回空清单；不将临时默认视图伪装成已保存用户。"""

    if _migration_pending():
        return _ok([])
    users = _profile_for_commit().list_users()
    return _ok([profile_to_dict(user) for user in users])


@app.post("/api/users", status_code=201)
def create_user(payload: CreateUserRequest) -> dict[str, Any]:
    user = _profile_for_commit().create_user(payload.display_name, payload.role)
    return _ok({"user": profile_to_dict(user), "created": True})


@app.patch("/api/users/{user_id}/preferences")
def update_preferences(user_id: str, payload: PreferencesPatchRequest) -> dict[str, Any]:
    patch = payload.model_dump(exclude_unset=True)
    user = _profile_for_commit().update_preferences(user_id, patch)
    return _ok({"user": profile_to_dict(user), "updated": True})


@app.post("/api/check/vault")
def vault_check_endpoint(payload: VaultCheckRequest) -> dict[str, Any]:
    settings = get_settings()
    profile = _get_profile(payload.user_id)
    result = check_vault(
        payload.path if payload.path is not None else profile.preferences.vault_root or settings.obsidian.vault_root,
        settings.obsidian,
        managed_roots=payload.managed_roots,
        excluded_roots=payload.excluded_roots,
        trial_tag=payload.trial_tag,
        rarely_tag=payload.rarely_tag,
    )
    result["user_id"] = profile.user_id
    return _ok(result)


@app.post("/api/check/github")
def github_check_endpoint(payload: GithubCheckRequest) -> dict[str, Any]:
    settings = get_settings()
    profile = _get_profile(payload.user_id)
    repository = (
        profile.preferences.github_repos[0]
        if profile.preferences.github_repos
        else settings.github.repositories[0]
        if settings.github.repositories
        else None
    )
    if repository is None and (payload.owner is None or payload.repo is None):
        raise PanelError("GITHUB_REPOSITORY_UNCONFIGURED", "该用户尚未配置 GitHub 仓库，请显式提供 owner 和 repo。")
    owner = payload.owner if payload.owner is not None else repository.owner
    repo = payload.repo if payload.repo is not None else repository.repo
    result = check_github(owner, repo, settings.github.timeout_seconds)
    result["user_id"] = profile.user_id
    return _ok(result)


@app.post("/api/check/git-local")
def git_local_check_endpoint(payload: GitLocalCheckRequest) -> dict[str, Any]:
    profile = _get_profile(payload.user_id)
    path = (
        payload.path
        if payload.path is not None
        else profile.preferences.git_local_path or str(get_settings().repository_root)
    )
    result = check_git_local(path)
    result["user_id"] = profile.user_id
    return _ok(result)


@app.post("/api/check/lexiang")
def lexiang_check_endpoint(
    payload: LexiangCheckRequest | None = Body(default=None),
    user_id: str = Query(default="default", pattern=_USER_ID_PATTERN),
) -> dict[str, Any]:
    settings = get_settings()
    selected_user_id = payload.user_id if payload is not None else user_id
    profile = _get_profile(selected_user_id)
    result = check_lexiang(settings.lexiang, AuditReader(settings.audit_path))
    result["user_id"] = profile.user_id
    return _ok(result)


@app.get("/api/audit/recent")
def recent_audit(limit: int = Query(default=10, ge=1, le=100)) -> dict[str, Any]:
    settings = get_settings()
    events = AuditReader(settings.audit_path).recent_events(limit)
    return _ok({"events": events, "count": len(events), "limit": limit, "read_only": True})


app.include_router(create_review_router(get_settings, get_profile_store, _profile_for_commit, _migration_pending))


__all__ = ["app", "get_profile_store", "get_settings"]
