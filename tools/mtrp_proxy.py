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
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LEXIANG_ENDPOINT = os.environ.get("MTRP_LEXIANG_ENDPOINT", "https://mcp.lexiang-app.com/mcp")
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
    """创建页面条目并写入正文，返回 entry_id。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(LEXIANG_ENDPOINT, headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            parent = await _resolve_parent(session, req.space_id, req.path)
            entry = _json_payload(await _call(session, "call_tool", {
                "tool_name": "entry_create_entry",
                "arguments": {"entry_type": "page", "parent_entry_id": parent, "name": req.title},
            }))
            entry_id = ((entry.get("data") or {}).get("entry") or {}).get("id") or ""
            if not entry_id:
                raise TransferError(502, "ENTRY_NOT_CREATED", "乐享未返回新条目 ID。")
            await _call(session, "call_tool", {
                "tool_name": "block_update_page",
                "arguments": {"entry_id": entry_id, "command": "replace_content", "new_str": req.markdown},
            })
            return {
                "ok": True,
                "entry_id": entry_id,
                "space_id": req.space_id,
                "path": req.path,
                "source_path": req.source_path,
                "content_sha256": req.content_sha256,
                "url": f"https://mcp.lexiang-app.com/pages/{entry_id}",
            }


class Handler(BaseHTTPRequestHandler):
    """最小 HTTP 处理器：健康检查、CORS 预检、传送。"""

    server_version = "MTRPProxy/1.0"

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
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/mtrp/health":
            self._send(200, {"ok": True, "token_configured": bool(os.environ.get("MTRP_LEXIANG_TOKEN"))})
            return
        self._send(404, {"ok": False, "message": "未知路径。"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mtrp/transfer":
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
    parser = argparse.ArgumentParser(description="MTR-P 乐享传送代理（仅回环）")
    parser.add_argument("--port", type=int, default=8012)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"MTR-P 代理已就绪：http://127.0.0.1:{server.server_port}/mtrp/health", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
