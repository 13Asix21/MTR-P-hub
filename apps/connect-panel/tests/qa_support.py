# -*- coding: utf-8 -*-
"""connect-panel QA 验收测试支撑代码。

只用 Python 标准库；测试客户端通过子进程 uvicorn 打真实 HTTP。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
PANEL_DIR = REPO_ROOT / "apps" / "connect-panel"
PANEL_VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures" / "stage2" / "vault"
AUDIT_DB = REPO_ROOT / "config" / ".mtrp" / "decision-log.sqlite3"
AUDIT_WAL = Path(str(AUDIT_DB) + "-wal")
AUDIT_SHM = Path(str(AUDIT_DB) + "-shm")
USERS_JSON = PANEL_DIR / "data" / "users.json"
USERS_DIR = USERS_JSON.parent
EXPORTS_DIR = USERS_DIR / "exports"
PORT_CANDIDATES = (9678, 9679, 9680, 9681)


def pick_free_port() -> int:
    """纯探测挑选一个无监听者的端口，不预 bind。"""

    for port in PORT_CANDIDATES:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("QA fixture server 没有可用候选端口。")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    """目录全树逐文件 sha256，键为相对 POSIX 路径。"""

    if not root.is_dir():
        raise RuntimeError(f"tree_hashes 根目录不存在：{root}")
    result: dict[str, str] = {}
    for current, _dirs, files in os.walk(root):
        for name in files:
            full = Path(current) / name
            result[full.relative_to(root).as_posix()] = file_sha256(full)
    return dict(sorted(result.items()))


def aggregation_paths() -> list[Path]:
    """主仓不变性聚合范围：src/mtrp_hub + config + pyproject.toml。

    与工程师口径一致地排除 sqlite 旁路文件（-wal、-shm），保留主库文件。
    """

    if not PANEL_VENV_PYTHON.is_file():
        raise RuntimeError("QA 找不到 .venv 解释器，请在主仓根目录执行。")
    paths: list[Path] = [REPO_ROOT / "pyproject.toml"]
    for base in (REPO_ROOT / "src" / "mtrp_hub", REPO_ROOT / "config"):
        for path in base.rglob("*"):
            if path.is_file() and not path.name.endswith(("-wal", "-shm")):
                paths.append(path)
    return sorted(paths, key=lambda p: p.relative_to(REPO_ROOT).as_posix())


def aggregation_digest(paths: list[Path]) -> str:
    combined = "".join(
        f"{path.relative_to(REPO_ROOT).as_posix()}\0{file_sha256(path)}\n"
        for path in paths
    )
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


class PanelServer:
    """以子进程方式启动真实 uvicorn 面板并提供 HTTP 客户端。"""

    def __init__(self, port: int) -> None:
        if not PANEL_VENV_PYTHON.is_file():
            raise RuntimeError("QA 找不到 .venv/Scripts/python.exe。")
        self.python = PANEL_VENV_PYTHON
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.process: subprocess.Popen[bytes] | None = None
        # 套件级基线快照，由 conftest 在启动前填充。
        self.baseline: dict[str, Any] = {}

    def start(self, timeout: float = 40.0) -> None:
        environment = os.environ.copy()
        for key in (("PYTHONPATH", "MTRP_PANEL_CONFIG_DIR", "MTRP_TESTING")):
            environment.pop(key, None)
        environment["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            [
                str(self.python),
                "-m",
                "uvicorn",
                "panel.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=str(PANEL_DIR),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                terminal = self.process.stdout.read() if self.process.stdout else b""
                raise RuntimeError(
                    "fixture server 子进程提前退出：\n"
                    + terminal.decode("utf-8", "replace")
                )
            try:
                self.request_json("GET", "/api/users", timeout=5.0)
                return
            except (RuntimeError, OSError):
                time.sleep(0.25)
        raise RuntimeError("fixture server 未在超时时间内就绪。")

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout:
            try:
                process.stdout.read()
            except OSError:
                pass
            process.stdout.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        timeout: float = 120.0,
    ) -> tuple[int, dict[str, str], bytes]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"请求 {method} {path} 失败：{exc.reason}") from exc

    def request_json(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        status, _headers, body = self.request(method, path, **kwargs)
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"{method} {path} 返回非 JSON（HTTP {status}）：{body[:200]!r}"
            ) from exc
        return status, parsed


def unwrap_ok(payload: Any) -> dict[str, Any]:
    assert isinstance(payload, dict), f"响应顶层不是对象：{payload!r}"
    assert payload.get("status") == "ok", f"外层包裹预期 ok，实际：{payload!r}"
    data = payload.get("data")
    assert isinstance(data, dict), f"data 不是对象：{payload!r}"
    return data


def unwrap_ok_list(payload: Any) -> list[Any]:
    """v2 契约：GET /api/users 的 data 直接是用户数组。

    v2 实现把 authentication_enabled 放在 overview，不再放在用户列表响应里，
    这里只做通用安全断言：数组结构 + 无认证字段 + 无堆栈。
    """

    import json as _json

    assert isinstance(payload, dict), f"响应顶层不是对象：{payload!r}"
    assert payload.get("status") == "ok", f"外层包裹预期 ok，实际：{payload!r}"
    raw = _json.dumps(payload, ensure_ascii=False)
    assert "token" not in raw.lower() and "password" not in raw.lower(), "用户列表泄漏认证字段"
    assert "Traceback" not in raw
    data = payload.get("data")
    assert isinstance(data, list), f"data 不是数组：{payload!r}"
    return data


def unwrap_error(payload: Any) -> dict[str, Any]:
    assert isinstance(payload, dict), f"响应顶层不是对象：{payload!r}"
    assert payload.get("status") == "error", f"外层包裹预期 error，实际：{payload!r}"
    error = payload.get("error")
    assert isinstance(error, dict), f"error 不是对象：{payload!r}"
    assert isinstance(error.get("code"), str) and error["code"], f"错误码缺失：{payload!r}"
    assert isinstance(error.get("message"), str) and error["message"], f"错误消息缺失：{payload!r}"
    assert "Traceback" not in json.dumps(payload, ensure_ascii=False), "错误响应泄漏堆栈"
    return error


def export_artifacts() -> list[Path]:
    """列出当前导出文件；测试只据此核对本套件新增产物。"""

    if not EXPORTS_DIR.is_dir():
        return []
    return sorted(path for path in EXPORTS_DIR.glob("*") if path.is_file())


__all__ = [
    "AUDIT_DB",
    "AUDIT_SHM",
    "AUDIT_WAL",
    "EXPORTS_DIR",
    "FIXTURE_VAULT",
    "PANEL_DIR",
    "PANEL_VENV_PYTHON",
    "REPO_ROOT",
    "USERS_DIR",
    "USERS_JSON",
    "PanelServer",
    "aggregation_digest",
    "aggregation_paths",
    "export_artifacts",
    "file_sha256",
    "pick_free_port",
    "tree_hashes",
    "unwrap_error",
    "unwrap_ok",
    "unwrap_ok_list",
]
