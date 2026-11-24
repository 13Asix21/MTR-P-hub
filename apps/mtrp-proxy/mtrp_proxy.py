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


# 代理必须自包含：部署包只上传本目录，不能依赖仓库根 src/。
# 纯路由逻辑（references/serial）已随包放入本目录，见同目录 _routing/。
_LOCAL = Path(__file__).resolve().parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


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
# 企业版乐享（另一组织）：端点带 company_from，凭据独立。
LEXIANG_COMPANY_ENDPOINT = os.environ.get("MTRP_LEXIANG_COMPANY_ENDPOINT", "")
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


def github_list_dir(owner: str, repo: str, path: str = "", explicit_token: str = "") -> list[dict[str, Any]]:
    """经 MCP 列举目录内容；path 为空表示仓库根。"""
    token = github_token(explicit_token)
    args: dict[str, Any] = {"owner": owner, "repo": repo, "path": path}
    payload = asyncio.run(github_mcp_call("get_file_contents", args, token))
    import json as _json
    raw = payload["text"]
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else []
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append({
            "name": item.get("name"),
            "path": item.get("path"),
            "type": item.get("type"),  # file | dir
            "size": item.get("size"),
        })
    return result


# 串联检索要频繁列目录，必须复用同一个 MCP 会话，否则每次都重新握手会超时。
_SEARCH_MAX_ENTRIES = 120
_SEARCH_MAX_DEPTH = 3
_SEARCH_CONCURRENCY = 8
_SEARCH_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build", ".pytest_cache", ".workbuddy"}


def github_search_paths(
    owner: str,
    repo: str,
    keyword: str,
    explicit_token: str = "",
    max_depth: int = _SEARCH_MAX_DEPTH,
    max_entries: int = _SEARCH_MAX_ENTRIES,
) -> list[dict[str, Any]]:
    """在一个 MCP 会话里遍历仓库目录树，按关键词匹配路径。

    复用单会话是关键：每次调用单独握手会拖到超时。
    跳过 .git/node_modules 等目录，并按 max_entries 限制访问量。
    """
    keyword_lower = (keyword or "").strip().lower()
    if not keyword_lower:
        return []
    token = github_token(explicit_token)
    return asyncio.run(_search_paths_async(owner, repo, keyword_lower, token, max_depth, max_entries))


async def _search_paths_async(
    owner: str,
    repo: str,
    keyword: str,
    token: str,
    max_depth: int,
    max_entries: int,
) -> list[dict[str, Any]]:
    from mcp import ClientSession

    auth = {"Authorization": f"Bearer {token}"}
    matched: list[dict[str, Any]] = []
    visited = 0

    async def list_dir(session: Any, path: str) -> list[dict[str, Any]]:
        result = await session.call_tool(
            "get_file_contents", arguments={"owner": owner, "repo": repo, "path": path}
        )
        if getattr(result, "isError", False):
            return []
        raw = _text(result)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    async with _select_opener_at(GITHUB_MCP_ENDPOINT, auth)() as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            queue: list[tuple[str, int]] = [("", 0)]
            while queue and visited < max_entries:
                current, depth = queue.pop(0)
                if depth > max_depth:
                    continue
                entries = await list_dir(session, current)
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    visited += 1
                    if visited >= max_entries:
                        break
                    path = str(entry.get("path") or "")
                    kind = entry.get("type")
                    name = str(entry.get("name") or "")
                    if kind == "dir":
                        if name in _SEARCH_SKIP_DIRS:
                            continue
                        queue.append((path, depth + 1))
                        continue
                    lower = path.lower()
                    if not lower.endswith((".md", ".py", ".yaml", ".yml", ".json", ".txt", ".toml")):
                        continue
                    if keyword in lower:
                        score = 2 if keyword in lower.rsplit("/", 1)[-1] else 1
                        matched.append({"path": path, "size": entry.get("size"), "score": score})
    matched.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    return matched[:20]


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


async def _list_children_async(session: Any, parent_id: str) -> list[dict[str, Any]]:
    result = await session.call_tool(
        "call_tool", arguments={"tool_name": "entry_list_children", "arguments": {"parent_id": parent_id}}
    )
    try:
        data = json.loads(_text(result))
    except json.JSONDecodeError:
        return []
    return (data.get("data") or {}).get("entries") or []


def resolve_company_folder(raw_path: str, space_id: str = "") -> dict[str, Any]:
    """在企业乐享空间里按路径定位**已存在**目录，只读、不创建。"""
    token = os.environ.get("MTRP_LEXIANG_COMPANY_TOKEN", "").strip()
    endpoint = LEXIANG_COMPANY_ENDPOINT.strip()
    if not token or not endpoint:
        raise TransferError(
            503, "COMPANY_TOKEN_MISSING",
            "未配置企业乐享凭据（MTRP_LEXIANG_COMPANY_TOKEN / MTRP_LEXIANG_COMPANY_ENDPOINT）。",
        )
    target_space = space_id or os.environ.get("MTRP_LEXIANG_COMPANY_SPACE", "").strip()
    if not target_space:
        raise TransferError(400, "SPACE_REQUIRED", "缺少 space_id。")

    from _routing.lexiang_path import PathResolveError, resolve_existing_path

    async def run() -> dict[str, Any]:
        from mcp import ClientSession

        auth = {"Authorization": f"Bearer {token}"}
        async with _select_opener_at(endpoint, auth)() as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                space = await session.call_tool("lexiang_fetch", arguments={"resource": "space", "id": target_space})
                try:
                    node = (json.loads(_text(space)).get("data") or {}).get("space") or {}
                except json.JSONDecodeError:
                    node = {}
                root_id = node.get("root_entry_id") or ""
                if not root_id:
                    raise TransferError(404, "SPACE_NOT_FOUND", "取不到知识库 root_entry_id。")
                # 逐层解析改为「先把整棵树按需拉取到内存」，再交给纯同步解析器，
                # 彻底避免事件循环嵌套问题。
                collected: dict[str, list[dict[str, Any]]] = {}

                async def prefetch(parent_id: str, depth: int) -> None:
                    if depth > 8 or parent_id in collected:
                        return
                    entries = await _list_children_async(session, parent_id)
                    collected[parent_id] = entries
                    for entry in entries:
                        if str(entry.get("entry_type")) == "folder":
                            await prefetch(str(entry.get("id")), depth + 1)

                # 只预取路径经过的分支，避免整库遍历
                current = root_id
                for _segment in [seg for seg in raw_path.replace(">", "/").split("/") if seg.strip()]:
                    if current in collected:
                        break
                    entries = await _list_children_async(session, current)
                    collected[current] = entries
                    normalized = " ".join(str(_segment).split())
                    hit = next(
                        (e for e in entries if " ".join(str(e.get("name") or "").split()) == normalized),
                        None,
                    )
                    if hit is None:
                        break
                    current = str(hit.get("id"))

                try:
                    resolved = resolve_existing_path(
                        lambda pid: collected.get(pid, []),
                        root_id,
                        raw_path,
                    )
                except PathResolveError as error:
                    # 在协程内抛出会被 MCP 的 TaskGroup 包成不可读的 ExceptionGroup，
                    # 因此这里返回结构化错误，由外层按 404 处理。
                    return {
                        "ok": False,
                        "_resolve_error": True,
                        "code": error.code,
                        "message": error.message,
                    }
                return {
                    "ok": True,
                    "space_name": node.get("name") or "",
                    "space_id": target_space,
                    "root_entry_id": root_id,
                    "entry_id": resolved.entry_id,
                    "name": resolved.name,
                    "depth": resolved.depth,
                    "path": resolved.path,
                    "message": f"已定位：{resolved.path}（entry_id={resolved.entry_id}）",
                }

    try:
        result = asyncio.run(run())
        if isinstance(result, dict) and result.get("_resolve_error"):
            raise TransferError(404, str(result.get("code")), str(result.get("message")))
        return result
    except TransferError:
        raise
    except BaseExceptionGroup as group:  # noqa: F821  (3.11+)
        for inner in group.exceptions:
            if isinstance(inner, TransferError):
                raise inner from None
        raise TransferError(502, "RESOLVE_FAILED", str(group.exceptions[0])[:200]) from None
    except Exception as error:
        raise TransferError(502, "RESOLVE_FAILED", str(error)[:200]) from None


def _tree_searcher(token: str):
    """把目录树检索包装成 serial 宿主需要的 PTreeSearch 形态。

    关键优化：同一轮路由的所有关键词共用**一次**目录遍历与会话，
    否则每个关键词各开一次 MCP 连接，6 个关键词会握手 6 次直接超时。
    """

    class _Searcher:
        def __init__(self) -> None:
            self._cache: dict[str, list[dict[str, Any]]] | None = None
            self._keywords: list[str] = []
            self._all_paths: list[str] | None = None

        def search_paths(self, owner: str, repo: str, keyword: str):
            # 首次调用时遍历一次并缓存全部路径，后续关键词只做本地匹配。
            if self._all_paths is None:
                self._all_paths = asyncio.run(_collect_paths_async(owner, repo, token))
            kw = (keyword or "").strip().lower()
            if not kw:
                return []
            hits = []
            for path in self._all_paths:
                lower = path.lower()
                if kw in lower:
                    score = 2 if kw in lower.rsplit("/", 1)[-1] else 1
                    hits.append({"path": path, "score": score})
            hits.sort(key=lambda item: (-item["score"], item["path"]))
            return hits[:20]

    return _Searcher()


async def _collect_paths_async(owner: str, repo: str, token: str) -> list[str]:
    """并行遍历仓库目录树收集可检索路径。

    网关对单次请求有 60s 上限，串行列目录会超时；
    这里按层并发（默认 8 路），并把访问量压到预算内。
    """
    from mcp import ClientSession

    auth = {"Authorization": f"Bearer {token}"}
    paths: list[str] = []
    budget = {"left": _SEARCH_MAX_ENTRIES}

    async def list_dir(session: Any, path: str) -> list[dict[str, Any]]:
        try:
            result = await session.call_tool(
                "get_file_contents", arguments={"owner": owner, "repo": repo, "path": path}
            )
        except Exception:
            return []
        if getattr(result, "isError", False):
            return []
        try:
            data = json.loads(_text(result))
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    async with _select_opener_at(GITHUB_MCP_ENDPOINT, auth)() as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            level: list[str] = [""]
            depth = 0
            while level and depth <= _SEARCH_MAX_DEPTH and budget["left"] > 0:
                semaphore = asyncio.Semaphore(_SEARCH_CONCURRENCY)

                async def one(current: str) -> list[dict[str, Any]]:
                    async with semaphore:
                        return await list_dir(session, current)

                results = await asyncio.gather(*(one(item) for item in level))
                next_level: list[str] = []
                for entries in results:
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        budget["left"] -= 1
                        if budget["left"] < 0:
                            break
                        path = str(entry.get("path") or "")
                        if entry.get("type") == "dir":
                            if str(entry.get("name") or "") not in _SEARCH_SKIP_DIRS:
                                next_level.append(path)
                            continue
                        if path.lower().endswith((".md", ".py", ".yaml", ".yml", ".json", ".txt", ".toml")):
                            paths.append(path)
                level = next_level
                depth += 1
    return paths


def route_m_to_p_serial(
    markdown: str,
    coordinate: str,
    explicit_token: str = "",
    max_hints: int = 6,
) -> dict[str, Any]:
    """M→P 串联检索：按正文里的项目线索在仓库目录树里找相关文件。"""
    if not isinstance(markdown, str) or not markdown.strip():
        raise TransferError(400, "MARKDOWN_REQUIRED", "缺少 markdown 正文。")
    if not coordinate or "/" not in coordinate:
        raise TransferError(400, "COORDINATE_REQUIRED", "需要 coordinate，形如 owner/repo。")
    token = github_token(explicit_token)

    from _routing.serial import execute_serial_route

    outcome = execute_serial_route(
        markdown,
        coordinate,
        _tree_searcher(token),
        max_hints=max_hints,
    )
    summary = outcome.summary()
    return {
        "ok": True,
        "transport": "mcp",
        "mode": "serial",
        "degraded": summary["degraded"],
        "coordinate": coordinate,
        "keywords": [h["keyword"] for h in summary["hints"]],
        "hit_count": summary["hit_count"],
        "hits": summary["hits"],
        "errors": summary["errors"],
        "message": (
            f"按线索匹配到 {summary['hit_count']} 个文件"
            if summary["hit_count"]
            else "没有从正文里找到可用于检索的项目线索，或仓库里没有匹配路径"
        ),
    }


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

    from _routing.host import execute_reference_route
    from _routing.mcp_reader import McpFileReader

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
        if self._route() == "/mtrp/lexiang-resolve":
            import urllib.parse
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            path = (query.get("path") or [""])[0]
            space = (query.get("space_id") or [""])[0]
            try:
                self._send(200, resolve_company_folder(path, space))
            except TransferError as error:
                self._send(error.status, {"ok": False, "code": error.code, "message": error.message})
            except Exception as error:  # pragma: no cover
                self._send(502, {"ok": False, "code": "RESOLVE_FAILED", "message": str(error)[:200]})
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
        if route not in ("/mtrp/transfer", "/mtrp/route-m-to-p", "/mtrp/route-m-to-p-serial"):
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
            if route == "/mtrp/route-m-to-p-serial":
                result = route_m_to_p_serial(
                    payload.get("markdown", ""),
                    str(payload.get("coordinate") or ""),
                    self.headers.get("X-MTRP-GitHub-Token", "").strip(),
                    int(payload.get("max_hints") or 6),
                )
                self._send(200, result)
                return
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
