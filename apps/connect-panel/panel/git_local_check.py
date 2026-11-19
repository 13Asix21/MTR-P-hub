"""P 库本地工作区的最小、无锁、只读 Git 校验。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .settings import PanelError, normalize_local_absolute_path


def _safe_remote_url(remote: str) -> tuple[str | None, bool]:
    """去除远端 URL 中可能存在的用户信息和查询参数。"""

    value = remote.strip()
    if not value:
        return None, False
    if "://" in value:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if not hostname:
            return None, True
        host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        try:
            port = parsed.port
        except ValueError:
            return None, True
        if port is not None:
            host = f"{host}:{port}"
        redacted = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        return redacted, redacted != value
    if "@" in value.split(":", 1)[0]:
        return value.split("@", 1)[1], True
    return value, False


def _run_git(path: Path, arguments: list[str], timeout: int = 8) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", *arguments],
            cwd=path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise PanelError("GIT_COMMAND_TIMEOUT", "本地 Git 只读命令执行超时。", 503) from exc
    except OSError as exc:
        raise PanelError("GIT_UNAVAILABLE", "本机 Git 无法启动，请确认已安装并加入 PATH。", 503) from exc


def check_git_local(raw_path: str) -> dict[str, Any]:
    """验证 .git、分支、HEAD、dirty 数量和经过脱敏的 origin URL。"""

    path = normalize_local_absolute_path(raw_path)
    result: dict[str, Any] = {
        "status": "pending",
        "path": str(path),
        "mode": "local-git",
        "read_only": True,
    }
    try:
        path.lstat()
    except OSError:
        result.update(
            status="error",
            error={"code": "DIRECTORY_NOT_FOUND", "message": "目录不存在或当前无法访问。"},
        )
        return result
    if not path.is_dir():
        result.update(
            status="error",
            error={"code": "NOT_A_DIRECTORY", "message": "输入路径不是目录。"},
        )
        return result
    if not (path / ".git").exists():
        result.update(
            status="error",
            error={"code": "GIT_METADATA_NOT_FOUND", "message": "该目录下未发现 .git，未执行任何 Git 命令。"},
        )
        return result
    if shutil.which("git") is None:
        result.update(
            status="error",
            error={"code": "GIT_UNAVAILABLE", "message": "未在 PATH 中找到 Git。"},
        )
        return result

    try:
        status_result = _run_git(
            path,
            ["status", "--porcelain=v2", "--branch", "--untracked-files=normal"],
            timeout=15,
        )
        if status_result.returncode != 0:
            raise PanelError("GIT_STATUS_FAILED", "无法只读获取分支、HEAD 和工作区状态。")
        branch: str | None = None
        head_sha: str | None = None
        dirty_count = 0
        for line in status_result.stdout.splitlines():
            if line.startswith("# branch.oid "):
                head_sha = line.removeprefix("# branch.oid ").strip()
            elif line.startswith("# branch.head "):
                branch = line.removeprefix("# branch.head ").strip()
            elif line.strip() and not line.startswith("# "):
                dirty_count += 1
        if branch is None or head_sha is None:
            raise PanelError("GIT_STATUS_INVALID", "Git 状态输出缺少分支或 HEAD 信息。")
        if len(head_sha) not in (40, 64) or any(character not in "0123456789abcdefABCDEF" for character in head_sha):
            raise PanelError("GIT_HEAD_INVALID", "工作区可能尚无提交，或 Git 返回的 HEAD 格式无效。")

        remote_result = _run_git(path, ["remote", "get-url", "origin"])
        remote_url: str | None = None
        remote_redacted = False
        remote_warning: str | None = None
        if remote_result.returncode == 0:
            remote_url, remote_redacted = _safe_remote_url(remote_result.stdout)
        else:
            remote_warning = "未配置 origin 或无法读取 origin URL。"
    except PanelError as exc:
        result.update(status="error", error={"code": exc.code, "message": exc.message})
        return result

    result.update(
        status="ok",
        git_metadata_present=True,
        branch=None if branch == "(detached)" else branch,
        detached=branch == "(detached)",
        head_sha=head_sha.lower(),
        dirty_file_count=dirty_count,
        remote_name="origin",
        remote_url=remote_url,
        remote_url_redacted=remote_redacted,
        remote_warning=remote_warning,
        commands=("status --porcelain=v2 --branch", "remote get-url origin"),
        optional_locks_disabled=True,
    )
    return result


__all__ = ["check_git_local"]
