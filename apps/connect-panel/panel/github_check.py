"""P 库 GitHub REST 无凭据、只读、真实网络探测。"""

from __future__ import annotations

import http.client
import json
import re
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from .settings import PanelError


_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_MAX_RESPONSE_BYTES = 2_000_000
_API_ROOT = "https://api.github.com"


def validate_github_coordinates(owner: str, repo: str) -> tuple[str, str]:
    clean_owner = owner.strip()
    clean_repo = repo.strip().removesuffix(".git")
    if not _OWNER_PATTERN.fullmatch(clean_owner) or clean_owner.endswith("-"):
        raise PanelError("GITHUB_OWNER_INVALID", "GitHub owner 格式无效。")
    if not _REPO_PATTERN.fullmatch(clean_repo) or clean_repo in (".", ".."):
        raise PanelError("GITHUB_REPO_INVALID", "GitHub repo 格式无效。")
    return clean_owner, clean_repo


def _http_error_code(status_code: int, rate_remaining: str | None) -> tuple[str, str]:
    if status_code == 404:
        return "GITHUB_REPOSITORY_NOT_FOUND", "仓库不存在，或无凭据访问时不可见。"
    if status_code == 429 or (status_code == 403 and rate_remaining == "0"):
        return "GITHUB_RATE_LIMITED", "GitHub 未认证请求已达限流，请稍后再试。"
    if status_code in (401, 403):
        return "GITHUB_FORBIDDEN", "GitHub 拒绝了此次无凭据只读请求。"
    if 500 <= status_code <= 599:
        return "GITHUB_SERVICE_UNAVAILABLE", "GitHub 服务暂时不可用。"
    return "GITHUB_HTTP_ERROR", f"GitHub 返回 HTTP {status_code}。"


def _get_json(path: str, timeout: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """执行单个直连 GET；禁止环境代理和凭据，限制响应大小。"""

    url = _API_ROOT + path
    request = Request(
        url,
        method="GET",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "MTR-P-Connect-Panel/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = build_opener(ProxyHandler({}))
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise PanelError("GITHUB_RESPONSE_TOO_LARGE", "GitHub 响应超过面板安全读取上限。", 502)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise PanelError("GITHUB_RESPONSE_INVALID", "GitHub 返回了无法识别的响应结构。", 502)
            metadata = {
                "http_status": response.status,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "rate_limit_remaining": response.headers.get("X-RateLimit-Remaining"),
                "rate_limit_reset": response.headers.get("X-RateLimit-Reset"),
            }
            return payload, metadata
    except HTTPError as exc:
        code, message = _http_error_code(exc.code, exc.headers.get("X-RateLimit-Remaining"))
        raise PanelError(code, message, 502) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise PanelError("GITHUB_TIMEOUT", "GitHub 探测超时（10 秒）。", 503) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise PanelError("GITHUB_TIMEOUT", "GitHub 探测超时（10 秒）。", 503) from exc
        raise PanelError("GITHUB_NETWORK_UNAVAILABLE", "本机当前无法直连 GitHub API。", 503) from exc
    except (OSError, UnicodeError, json.JSONDecodeError, http.client.HTTPException) as exc:
        raise PanelError("GITHUB_RESPONSE_INVALID", "GitHub 响应读取或解析失败。", 502) from exc


def check_github(owner: str, repo: str, timeout: int = 10) -> dict[str, Any]:
    """依次读取仓库元数据和默认分支最新 commit，并记录实测延迟。"""

    clean_owner, clean_repo = validate_github_coordinates(owner, repo)
    safe_timeout = min(max(timeout, 1), 30)
    result: dict[str, Any] = {
        "status": "pending",
        "mode": "github-online",
        "owner": clean_owner,
        "repo": clean_repo,
        "repository_url": f"https://github.com/{clean_owner}/{clean_repo}",
        "credential_used": False,
        "read_only": True,
        "timeout_seconds": safe_timeout,
    }
    started = time.perf_counter()
    requests: list[dict[str, Any]] = []
    try:
        repository_path = f"/repos/{quote(clean_owner, safe='')}/{quote(clean_repo, safe='')}"
        repository, repository_meta = _get_json(repository_path, safe_timeout)
        requests.append({"name": "repository", **repository_meta})
        default_branch = repository.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch.strip():
            raise PanelError("GITHUB_BRANCH_MISSING", "GitHub 响应未包含有效默认分支。", 502)

        commit_path = repository_path + "/commits/" + quote(default_branch, safe="")
        commit, commit_meta = _get_json(commit_path, safe_timeout)
        requests.append({"name": "latest_commit", **commit_meta})
        head_sha = commit.get("sha")
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
            raise PanelError("GITHUB_COMMIT_INVALID", "GitHub 响应未包含有效 commit SHA。", 502)
        commit_data = commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
        committer = commit_data.get("committer") if isinstance(commit_data.get("committer"), dict) else {}
        author = commit_data.get("author") if isinstance(commit_data.get("author"), dict) else {}
        committed_at = committer.get("date") or author.get("date")
        if committed_at is not None and not isinstance(committed_at, str):
            committed_at = None

        result.update(
            status="ok",
            connectivity="在线 · 实测可达",
            full_name=repository.get("full_name") if isinstance(repository.get("full_name"), str) else f"{clean_owner}/{clean_repo}",
            default_branch=default_branch,
            pushed_at=repository.get("pushed_at") if isinstance(repository.get("pushed_at"), str) else None,
            latest_commit_sha=head_sha.lower(),
            latest_commit_at=committed_at,
            total_latency_ms=round((time.perf_counter() - started) * 1000, 1),
            requests=requests,
            rate_limit_remaining=commit_meta.get("rate_limit_remaining"),
            rate_limit_reset=commit_meta.get("rate_limit_reset"),
        )
        return result
    except PanelError as exc:
        result.update(
            status="error",
            connectivity="在线探测失败",
            phase="latest_commit" if requests else "repository",
            total_latency_ms=round((time.perf_counter() - started) * 1000, 1),
            requests=requests,
            error={"code": exc.code, "message": exc.message},
        )
        return result


__all__ = ["check_github", "validate_github_coordinates"]
