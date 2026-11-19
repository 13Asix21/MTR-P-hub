"""FastAPI 路由与统一、无堆栈泄漏的 JSON 错误边界。"""

from __future__ import annotations

import logging
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
_PROFILE_STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "users.json"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """首次启动仅初始化面板自管档案，不触碰主仓配置。"""

    get_profile_store().ensure_default(_default_preferences(get_settings()))
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


@lru_cache(maxsize=1)
def get_profile_store() -> ProfileStore:
    return ProfileStore(_PROFILE_STORE_PATH)


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


def _ready_profile_store() -> ProfileStore:
    store = get_profile_store()
    store.ensure_default(_default_preferences(get_settings()))
    return store


def _get_profile(user_id: str) -> UserProfile:
    return _ready_profile_store().get_user(user_id)


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Any:
    """为本地页面和 API 添加最小浏览器安全头。"""

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


@app.get("/api/overview")
def overview(user_id: str = Query(default="default", pattern=_USER_ID_PATTERN)) -> dict[str, Any]:
    """返回指定用户的预填偏好；不做文件扫描或网络探测。"""

    settings = get_settings()
    profile = _get_profile(user_id)
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
    users = _ready_profile_store().list_users()
    return _ok([profile_to_dict(user) for user in users])


@app.post("/api/users", status_code=201)
def create_user(payload: CreateUserRequest) -> dict[str, Any]:
    user = _ready_profile_store().create_user(payload.display_name, payload.role)
    return _ok({"user": profile_to_dict(user), "created": True})


@app.patch("/api/users/{user_id}/preferences")
def update_preferences(user_id: str, payload: PreferencesPatchRequest) -> dict[str, Any]:
    patch = payload.model_dump(exclude_unset=True)
    user = _ready_profile_store().update_preferences(user_id, patch)
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


app.include_router(create_review_router(get_settings, get_profile_store))


__all__ = ["app", "get_settings"]
