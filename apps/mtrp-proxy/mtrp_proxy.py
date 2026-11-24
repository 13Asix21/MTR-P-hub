"""MTR-P 代理：把审核通过的文件传送到乐享知识库。

浏览器无法直连乐享 MCP（CORS 被拦），所以由本服务在服务端持有凭据并转发。
只监听回环地址；凭据仅从环境变量读取，不写入代码或日志。

启动：
    MTRP_LEXIANG_TOKEN=... MTRP_LEXIANG_SPACE_ID=... python mtrp_proxy.py --port 8012

前端调用：POST /mtrp/transfer
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


# 仓库根 src 加入路径，使 mtrp_hub 的纯路由逻辑可被复用（同一套规则，不另写一份）。
_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def load_dotenv(path: Path | None = None) -> None:
    """读取同目录 .env 填入环境变量；已存在的环境变量优先，不覆盖。

    只做最小的 KEY=VALUE 解析，避免为一个配置文件引入额外依赖。
    """
    target = path or Path(__file__).resolve().with_name(".env")
    if not target.is_file():
        return
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_dotenv()

LEXIANG_ENDPOINT = os.environ.get("MTRP_LEXIANG_ENDPOINT", "https://mcp.lexiang-app.com/mcp")
GITHUB_MCP_ENDPOINT = os.environ.get("MTRP_GITHUB_MCP_ENDPOINT", "https://api.githubcopilot.com/mcp/")
MAX_BODY_BYTES = 2 * 1024 * 1024
ALLOWED_ORIGINS = os.environ.get(
    "MTRP_ALLOWED_ORIGINS",
    "https://mtrp-review-workbench.app.workbuddy.host,http://127.0.0.1:8766,http://localhost:8766",
).split(",")


@dataclass(frozen=True)
class TransferRequest:
    """一次传送请求；字段与前端 transfer.js 保持一致。"""

    space_id: str
    path: str
    title: str
    source_path: str
    content_sha256: str
    markdown: str


class TransferError(Exception):
    """可映射为 HTTP 错误响应的传送失败。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def parse_request(payload: dict[str, Any], default_space: str) -> TransferRequest:
    """校验入参；缺字段或含可疑内容直接拒绝，不做静默降级。"""
    space_id = str(payload.get("space_id") or default_space).strip()
    if not space_id:
        raise TransferError(400, "SPACE_REQUIRED", "缺少 space_id。")
    markdown = payload.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise TransferError(400, "EMPTY_CONTENT", "markdown 正文为空。")
    sha = str(payload.get("content_sha256") or "")
    if len(sha) != 64:
        raise TransferError(400, "SHA_INVALID", "content_sha256 必须是 64 位十六进制。")
    path = str(payload.get("path") or "").strip().strip("/")
    if ".." in path.split("/"):
        raise TransferError(400, "PATH_INVALID", "目标路径不能包含 ..。")
    return TransferRequest(
        space_id=space_id,
        path=path,
        title=str(payload.get("title") or "未命名笔记"),
        source_path=str(payload.get("source_path") or ""),
        content_sha256=sha,
        markdown=markdown,
    )


def _require_token() -> str:
    token = os.environ.get("MTRP_LEXIANG_TOKEN", "").strip()
    if not token:
        raise TransferError(503, "TOKEN_MISSING", "服务端未配置 MTRP_LEXIANG_TOKEN。")
    return token


def _text(result: Any) -> str:
    parts = []
    for item in getattr(result, "content", []) or []:
        value = getattr(item, "text", None)
        if value:
            parts.append(value)
    return "\n".join(parts)


def _json_payload(result: Any) -> dict[str, Any]:
    """乐享网关返回 {"code":0,"data":{...}}；非零 code 视为失败。"""
    text = _text(result)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise TransferError(502, "UPSTREAM_INVALID", f"乐享返回非 JSON：{text[:120]}") from None
    if isinstance(data, dict) and data.get("code") not in (0, None):
        raise TransferError(502, "UPSTREAM_ERROR", f"乐享返回 code={data.get('code')} {data.get('message', '')}")
    return data


async def _call(session: Any, tool: str, arguments: dict[str, Any]) -> Any:
    result = await session.call_tool(tool, arguments=arguments)
    if getattr(result, "isError", False):
        raise TransferError(502, "UPSTREAM_TOOL_ERROR", _text(result)[:200] or f"{tool} 调用失败")
    return result


# 乐享整篇导入上限：file_import_markdown 支持 2 MiB，原样保存、不经 block 转换。
MAX_IMPORT_BYTES = 2 * 1024 * 1024


async def import_markdown(session: Any, parent_id: str, content: str, title: str) -> dict[str, Any]:
    """整篇导入 Markdown 文件（不转 block），随后重命名。

    这是乐享官方给“整文件入库”的通道：服务端完成上传与提交，内容原样保存，
    实测 16KB 文件导入后逐字节一致，不受 block 解析的结构稳定性限制。
    """
    size = len(content.encode("utf-8"))
    if size > MAX_IMPORT_BYTES:
        raise TransferError(
            413, "FILE_TOO_LARGE",
            f"文件 {size // 1024} KB 超过乐享单文件 {MAX_IMPORT_BYTES // 1024 // 1024} MiB 上限。",
        )
    result = await session.call_tool("call_tool", {
        "tool_name": "file_import_markdown",
        "arguments": {"parent_entry_id": parent_id, "content": content},
    })
    if getattr(result, "isError", False):
        raise TransferError(502, "IMPORT_FAILED", f"导入失败：{_text(result)[:200]}")
    entry = ((_json_payload(result).get("data") or {}).get("entry") or {})
    entry_id = entry.get("id") or ""
    if not entry_id:
        raise TransferError(502, "ENTRY_NOT_CREATED", "乐享未返回条目 ID。")

    renamed = await session.call_tool("call_tool", {
        "tool_name": "entry_rename_entry",
        "arguments": {"entry_id": entry_id, "name": title},
    })
    if getattr(renamed, "isError", False):
        # 改名失败不影响内容，但要如实告知。
        return {"entry_id": entry_id, "bytes": size, "renamed": False, "warning": _text(renamed)[:120]}
    return {"entry_id": entry_id, "bytes": size, "renamed": True}


async def _resolve_parent(session: Any, space_id: str, path: str) -> str:
    """按目标路径逐级查找/创建文件夹，返回最终的 parent_entry_id。"""
    space = _json_payload(await _call(session, "lexiang_fetch", {"resource": "space", "id": space_id}))
    root_id = ((space.get("data") or {}).get("space") or {}).get("root_entry_id") or ""
    if not root_id:
        raise TransferError(502, "ROOT_MISSING", "未取到知识库 root_entry_id。")
    parent = root_id
    for segment in [part for part in path.split("/") if part]:
        created = _json_payload(await _call(session, "call_tool", {
            "tool_name": "entry_create_entry",
            "arguments": {"entry_type": "folder", "parent_entry_id": parent, "name": segment},
        }))
        parent = ((created.get("data") or {}).get("entry") or {}).get("id") or parent
    return parent


async def transfer(req: TransferRequest, token: str) -> dict[str, Any]:
    """整篇导入：按目标路径定位父节点，导入原始 Markdown 文件并重命名。"""
    from mcp import ClientSession

    auth = {"Authorization": f"Bearer {token}"}
    opener = _select_opener(auth)

    async with opener() as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            parent = await _resolve_parent(session, req.space_id, req.path)
            imported = await import_markdown(session, parent, req.markdown, req.title)
            return {
                "ok": True,
                "entry_id": imported["entry_id"],
                "bytes": imported["bytes"],
                "renamed": imported["renamed"],
                "warning": imported.get("warning"),
                "space_id": req.space_id,
                "path": req.path,
                "source_path": req.source_path,
                "content_sha256": req.content_sha256,
                "url": f"https://mcp.lexiang-app.com/pages/{imported['entry_id']}",
            }


def _select_opener(auth: dict[str, str]):
    """乐享连接入口（保留原调用点）。"""
    return _select_opener_at(LEXIANG_ENDPOINT, auth)


def _select_opener_at(endpoint: str, auth: dict[str, str]):
    """按部署环境的 mcp 版本选择连接方式，并注入 Authorization 头。

    已探测到的形态：
      * 旧版：streamablehttp_client(url, headers=...)
      * 部分版本：streamable_http_client(url, httpx_client_factory=...)
      * 线上实际：streamable_http_client(url, http_client=httpx2.AsyncClient(...))
    """
    import inspect

    try:
        from mcp.client.streamable_http import streamablehttp_client
        params = inspect.signature(streamablehttp_client).parameters
        if "headers" in params:
            return lambda: streamablehttp_client(endpoint, headers=auth)
    except ImportError:
        pass

    from mcp.client.streamable_http import streamable_http_client

    params = inspect.signature(streamable_http_client).parameters
    if "http_client" in params:
        return lambda: streamable_http_client(endpoint, http_client=_async_client(auth))
    if "httpx_client_factory" in params:
        return lambda: streamable_http_client(endpoint, httpx_client_factory=lambda **kw: _async_client(auth, kw))
    return lambda: streamable_http_client(endpoint)


def _async_client(auth: dict[str, str], options: dict[str, Any] | None = None):
    """优先 httpx2，其次 httpx；两者 API 一致。"""
    options = options or {}
    kwargs = {"headers": auth, "follow_redirects": True}
    if options.get("timeout") is not None:
        kwargs["timeout"] = options["timeout"]
    try:
        import httpx2
        return httpx2.AsyncClient(**kwargs)
    except ImportError:
        import httpx
        return httpx.AsyncClient(**kwargs)

async def verify(token: str, space_id: str, target_path: str) -> dict[str, Any]:
    """只读自检：确认 token 有效、知识库可见、目标路径层级可解析。

    不创建任何条目；路径只做“是否已存在”的探测，不存在也视为通过（建的时候会自动建）。
    """
    from mcp import ClientSession

    if not space_id:
        raise TransferError(400, "SPACE_REQUIRED", "缺少 space_id。")
    auth = {"Authorization": f"Bearer {token}"}
    async with _select_opener(auth)() as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            me = _json_payload(await _call(session, "whoami", {}))
            staff = ((me.get("data") or {}).get("staff") or {})
            space = _json_payload(await _call(session, "lexiang_fetch", {"resource": "space", "id": space_id}))
            node = ((space.get("data") or {}).get("space") or {})
            if not node:
                raise TransferError(404, "SPACE_NOT_FOUND", "知识库不存在或无权访问。")
            return {
                "ok": True,
                "account": staff.get("display_name") or "",
                "organization": staff.get("organization") or "",
                "space_name": node.get("name") or "",
                "space_id": space_id,
                "target_path": target_path,
                "message": f"连接正常：{staff.get('display_name') or '已认证'} · 知识库「{node.get('name') or space_id}」",
            }


def agent_check() -> dict[str, Any]:
    """真实调用一次 Agent 网关，确认 key 与端点可用（只发最短请求）。"""
    import json as _json
    import urllib.error
    import urllib.request

    endpoint = os.environ.get("MTRP_AGENT_ENDPOINT", "https://token.moi.matrixorigin.cn/v1").rstrip("/")
    key = os.environ.get("MTRP_AGENT_KEY", "").strip()
    model = os.environ.get("MTRP_AGENT_MODEL", "deepseek-v4.1-flash")
    if not key:
        raise TransferError(503, "AGENT_KEY_MISSING", "服务端未配置 Agent key。")
    payload = _json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "回复 ok"}],
        "max_tokens": 8,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = _json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise TransferError(502, "AGENT_REJECTED", f"Agent 网关返回 {error.code}。") from None
    except Exception as error:
        raise TransferError(502, "AGENT_UNREACHABLE", f"Agent 网关不可达：{str(error)[:80]}") from None
    model_used = body.get("model") or model
    return {"ok": True, "model": model_used, "message": f"Agent 可用：{model_used}"}


# ---------------------------------------------------------------- GitHub MCP
# GitHub 改为全部经官方 MCP 服务（api.githubcopilot.com/mcp），不再直连 REST。
# 这样与乐享保持同一套调用形态，也为后续 M→P 路由复用同一客户端打底。


def _github_opener(token: str):
    """构造 GitHub MCP 连接；复用 _select_opener，仅换端点与令牌。"""
    auth = {"Authorization": f"Bearer {token}"}
    opener = _select_opener_at(GITHUB_MCP_ENDPOINT, auth)
    return opener


async def github_mcp_call(tool: str, arguments: dict[str, Any], token: str) -> dict[str, Any]:
    """调用 GitHub MCP 工具，返回解析后的结果文本。"""
    from mcp import ClientSession

    async with _github_opener(token)() as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments=arguments)
            text = _text(result)
            if getattr(result, "isError", False):
                raise TransferError(502, "GITHUB_MCP_ERROR", text[:200] or f"{tool} 调用失败")
            return {"tool": tool, "text": text}


def github_token(explicit: str = "") -> str:
    """令牌来源：请求头 > 代理 .env。缺失就明确报错，不降级为匿名。"""
    token = (explicit or os.environ.get("MTRP_GITHUB_TOKEN", "")).strip()
    if not token:
        raise TransferError(
            503, "GITHUB_TOKEN_MISSING",
            "GitHub 走 MCP 需要 token，请在设置页「高级设置」填写，或在代理 .env 配置 MTRP_GITHUB_TOKEN。",
        )
    return token


def github_check(owner: str, repo: str, explicit_token: str = "") -> dict[str, Any]:
    """经 MCP 探查仓库：存在性、可见性、默认分支。"""
    if not owner or not repo:
        raise TransferError(400, "REPO_REQUIRED", "缺少 owner 或 repo。")
    token = github_token(explicit_token)
    try:
        payload = asyncio.run(github_mcp_call(
            "search_repositories", {"query": f"repo:{owner}/{repo}"}, token))
    except TransferError:
        raise
    except Exception as error:
        raise TransferError(
            502, "GITHUB_MCP_UNREACHABLE",
            "GitHub MCP 连接失败：请确认 Token 有效（形如 ghp_ 或 github_pat_ 开头，长度通常 40 位以上）。"
            f"（{type(error).__name__}）",
        ) from None
    import json as _json
    try:
        data = _json.loads(payload["text"])
    except _json.JSONDecodeError:
        raise TransferError(502, "GITHUB_MCP_INVALID", "GitHub MCP 返回非 JSON。") from None
    items = data.get("items") or []
    match = next((item for item in items if str(item.get("full_name", "")).lower() == f"{owner}/{repo}".lower()), None)
    if not match:
        raise TransferError(404, "GITHUB_NOT_FOUND", "仓库不存在，或当前 token 无访问权限。")
    return {
        "ok": True,
        "transport": "mcp",
        "full_name": match.get("full_name"),
        "private": bool(match.get("private")),
        "default_branch": match.get("default_branch"),
        "html_url": match.get("html_url"),
        "pushed_at": match.get("updated_at"),
        "message": (
            f"连接正常（MCP）：{match.get('full_name')}（{'私有' if match.get('private') else '公开'}）"
        ),
    }


def github_scope(explicit_token: str = "") -> dict[str, Any]:
    """经 MCP 查当前 token 对应的账号。"""
    token = github_token(explicit_token)
    payload = asyncio.run(github_mcp_call("get_me", {}, token))
    import json as _json
    try:
        data = _json.loads(payload["text"])
    except _json.JSONDecodeError:
        data = {}
    login = data.get("login") or ""
    return {
        "ok": True,
        "transport": "mcp",
        "login": login,
        "message": f"MCP token 属于 {login or '（未知账号）'}",
    }


def github_list_repos(explicit_token: str = "", limit: int = 100) -> dict[str, Any]:
    """经 MCP 列出当前 token 可访问的全部仓库（分页合并）。"""
    token = github_token(explicit_token)
    repos: list[dict[str, Any]] = []
    page = 1
    while page <= 10 and len(repos) < limit:
        try:
            payload = asyncio.run(github_mcp_call(
                "search_repositories",
                {"query": "user:@me", "perPage": min(100, limit - len(repos)), "page": page},
                token,
            ))
        except TransferError:
            raise
        except Exception as error:
            raise TransferError(
                502, "GITHUB_MCP_UNREACHABLE",
                "GitHub MCP 连接失败：请确认 Token 有效（形如 ghp_ 或 github_pat_ 开头，长度通常 40 位以上）。"
                f"（{type(error).__name__}）",
            ) from None
        import json as _json
        try:
            data = _json.loads(payload["text"])
        except _json.JSONDecodeError:
            break
        items = data.get("items") or []
        if not items:
            break
        for item in items:
            repos.append({
                "full_name": item.get("full_name"),
                "private": bool(item.get("private")),
                "default_branch": item.get("default_branch"),
                "html_url": item.get("html_url"),
                "description": item.get("description"),
                "updated_at": item.get("updated_at"),
            })
        if len(items) < 100:
            break
        page += 1
    return {
        "ok": True,
        "transport": "mcp",
        "count": len(repos),
        "repos": repos,
        "message": f"经 MCP 读到 {len(repos)} 个仓库",
    }


def github_branches(owner: str, repo: str, explicit_token: str = "") -> dict[str, Any]:
    """经 MCP 列出仓库分支。"""
    if not owner or not repo:
        raise TransferError(400, "REPO_REQUIRED", "缺少 owner 或 repo。")
    token = github_token(explicit_token)
    payload = asyncio.run(github_mcp_call("list_branches", {"owner": owner, "repo": repo}, token))
    return {"ok": True, "transport": "mcp", "owner": owner, "repo": repo, "content": payload["text"]}


def github_read_file(owner: str, repo: str, path: str, ref: str = "", explicit_token: str = "") -> dict[str, Any]:
    """经 MCP 读取文件内容；为后续 M→P 引用补全预留的只读入口。"""
    if not owner or not repo or not path:
        raise TransferError(400, "PARAM_REQUIRED", "需要 owner、repo 与 path。")
    token = github_token(explicit_token)
    args: dict[str, Any] = {"owner": owner, "repo": repo, "path": path}
    if ref:
        args["sha"] = ref
    payload = asyncio.run(github_mcp_call("get_file_contents", args, token))
    return {"ok": True, "transport": "mcp", "path": path, "content": payload["text"]}


def _mcp_github_call(token: str):
    """把 GitHub MCP 调用包装成 route 宿主需要的 call(tool, arguments) 形态。

    需要保留 resource 项的原文：GitHub 把文件正文放在 resource.text 里，
    而 text 项只是 "successfully downloaded ..." 回执。这里直接返回 MCP 的
    content 数组，交给 McpFileReader 按类型解析。
    """
    from mcp import ClientSession

    def call(tool: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        async def run() -> list[dict[str, Any]]:
            auth = {"Authorization": f"Bearer {token}"}
            async with _select_opener_at(GITHUB_MCP_ENDPOINT, auth)() as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments=arguments)
                    if getattr(result, "isError", False):
                        raise TransferError(502, "GITHUB_MCP_ERROR", _text(result)[:200] or f"{tool} 调用失败")
                    return [
                        item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                        for item in result.content
                    ]

        return asyncio.run(run())

    return call


def route_m_to_p(markdown: str, explicit_token: str = "", max_items: int = 20) -> dict[str, Any]:
    """M→P 引用补全：解析正文里的 GitHub 引用并取回对应文件。

    没有引用时返回 ok=true、fetched 为空，不视为错误；
    有引用但取不到时返回 degraded=true，已取到的照常返回。
    """
    if not isinstance(markdown, str) or not markdown.strip():
        raise TransferError(400, "MARKDOWN_REQUIRED", "缺少 markdown 正文。")
    token = github_token(explicit_token)

    from mtrp_hub.routing.host import execute_reference_route
    from mtrp_hub.routing.mcp_reader import McpFileReader

    outcome = execute_reference_route(
        markdown,
        McpFileReader(_mcp_github_call(token)),
        max_items=max_items,
    )
    summary = outcome.summary()
    return {
        "ok": True,
        "transport": "mcp",
        "degraded": summary["degraded"],
        "fetched_count": summary["fetched_count"],
        "missing_count": summary["missing_count"],
        "repos": summary["repos"],
        "files": summary["fetched"],
        "missing": summary["missing"],
        "message": (
            f"补全 {summary['fetched_count']} 个文件"
            + (f"，{summary['missing_count']} 个未取到" if summary["missing_count"] else "")
            if summary["total"]
            else "正文中没有可定位的 GitHub 引用"
        ),
    }


class Handler(BaseHTTPRequestHandler):
    """最小 HTTP 处理器：健康检查、CORS 预检、传送。"""

    server_version = "MTRPProxy/1.0"

    def _route(self) -> str:
        """去掉查询串再路由：平台网关会用 sharecode 做访问控制并在转发前剥离它。"""
        return self.path.split("?", 1)[0]

    def _origin(self) -> str | None:
        origin = self.headers.get("Origin")
        if origin and origin in [item.strip() for item in ALLOWED_ORIGINS]:
            return origin
        return None

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = self._origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        """CORS 预检：只放行配置允许的来源。"""
        self.send_response(204)
        origin = self._origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "content-type, x-mtrp-sharecode, x-mtrp-github-token")
            self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self._route() == "/mtrp/verify":
            import urllib.parse
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            space = (query.get("space_id") or [os.environ.get("MTRP_LEXIANG_SPACE_ID", "")])[0]
            target = (query.get("path") or [""])[0]
            try:
                self._send(200, asyncio.run(verify(_require_token(), space, target)))
            except TransferError as error:
                self._send(error.status, {"ok": False, "code": error.code, "message": error.message})
            except Exception as error:  # pragma: no cover
                self._send(502, {"ok": False, "code": "UPSTREAM_FAILED", "message": str(error)[:200]})
            return
        if self._route() == "/mtrp/github-check":
            import urllib.parse
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            owner = (query.get("owner") or [""])[0].strip()
            repo = (query.get("repo") or [""])[0].strip()
            pat = self.headers.get("X-MTRP-GitHub-Token", "").strip() or os.environ.get("MTRP_GITHUB_TOKEN", "").strip()
            try:
                self._send(200, github_check(owner, repo, pat))
            except TransferError as error:
                self._send(error.status, {"ok": False, "code": error.code, "message": error.message})
            except Exception as error:  # pragma: no cover
                self._send(502, {"ok": False, "code": "GITHUB_FAILED", "message": str(error)[:200]})
            return
        if self._route() == "/mtrp/github-repos":
            token = self.headers.get("X-MTRP-GitHub-Token", "").strip()
            try:
                self._send(200, github_list_repos(token))
            except TransferError as error:
                self._send(error.status, {"ok": False, "code": error.code, "message": error.message})
            except Exception as error:  # pragma: no cover
                self._send(502, {"ok": False, "code": "GITHUB_FAILED", "message": str(error)[:200]})
            return
        if self._route() == "/mtrp/github-branches":
            import urllib.parse
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            owner = (query.get("owner") or [""])[0].strip()
            repo = (query.get("repo") or [""])[0].strip()
            token = self.headers.get("X-MTRP-GitHub-Token", "").strip()
            try:
                self._send(200, github_branches(owner, repo, token))
            except TransferError as error:
                self._send(error.status, {"ok": False, "code": error.code, "message": error.message})
            except Exception as error:  # pragma: no cover
                self._send(502, {"ok": False, "code": "GITHUB_FAILED", "message": str(error)[:200]})
            return
        if self._route() == "/mtrp/github-file":
            import urllib.parse
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            owner = (query.get("owner") or [""])[0].strip()
            repo = (query.get("repo") or [""])[0].strip()
            path = (query.get("path") or [""])[0].strip()
            ref = (query.get("ref") or [""])[0].strip()
            token = self.headers.get("X-MTRP-GitHub-Token", "").strip()
            try:
                self._send(200, github_read_file(owner, repo, path, ref, token))
            except TransferError as error:
                self._send(error.status, {"ok": False, "code": error.code, "message": error.message})
            except Exception as error:  # pragma: no cover
                self._send(502, {"ok": False, "code": "GITHUB_FAILED", "message": str(error)[:200]})
            return
        if self._route() == "/mtrp/github-scope":
            pat = self.headers.get("X-MTRP-GitHub-Token", "").strip()
            try:
                self._send(200, github_scope(pat))
            except TransferError as error:
                self._send(error.status, {"ok": False, "code": error.code, "message": error.message})
            return
        if self._route() == "/mtrp/agent-check":
            try:
                self._send(200, agent_check())
            except TransferError as error:
                self._send(error.status, {"ok": False, "code": error.code, "message": error.message})
            except Exception as error:  # pragma: no cover
                self._send(502, {"ok": False, "code": "AGENT_FAILED", "message": str(error)[:200]})
            return
        if self._route() == "/mtrp/health":
            self._send(200, {"ok": True, "token_configured": bool(os.environ.get("MTRP_LEXIANG_TOKEN"))})
            return
        self._send(404, {"ok": False, "message": "未知路径。"})

    def do_POST(self) -> None:  # noqa: N802
        route = self._route()
        if route not in ("/mtrp/transfer", "/mtrp/route-m-to-p"):
            self._send(404, {"ok": False, "message": "未知路径。"})
            return
        if self._origin() is None:
            self._send(403, {"ok": False, "code": "ORIGIN_NOT_ALLOWED", "message": "来源不在允许列表。"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(413, {"ok": False, "code": "BODY_TOO_LARGE", "message": "请求体为空或过大。"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if route == "/mtrp/route-m-to-p":
                result = route_m_to_p(
                    payload.get("markdown", ""),
                    self.headers.get("X-MTRP-GitHub-Token", "").strip(),
                    int(payload.get("max_items") or 20),
                )
                self._send(200, result)
                return
            req = parse_request(payload, os.environ.get("MTRP_LEXIANG_SPACE_ID", ""))
            outcome = asyncio.run(transfer(req, _require_token()))
        except TransferError as error:
            self._send(error.status, {"ok": False, "code": error.code, "message": error.message})
            return
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "code": "BAD_JSON", "message": "请求体不是合法 JSON。"})
            return
        except Exception as error:  # pragma: no cover - 上游异常统一收敛
            self._send(502, {"ok": False, "code": "UPSTREAM_FAILED", "message": str(error)[:200]})
            return
        self._send(200, outcome)

    def log_message(self, fmt: str, *args: Any) -> None:
        """不记录请求体与凭据，只记录状态行。"""
        print(f"[mtrp-proxy] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="MTR-P 乐享传送代理")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8012")))
    parser.add_argument(
        "--host",
        default=os.environ.get("MTRP_PROXY_HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"),
        help="监听地址；部署平台要求 0.0.0.0，本机调试默认 127.0.0.1",
    )
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"MTR-P 代理已就绪：http://{args.host}:{server.server_port}/mtrp/health", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
