"""P 库读取器的 MCP 实现：把宿主与 GitHub MCP 客户端接起来（R1.2）。

与 `host.PReader` 协议一致：`read_file(owner, repo, path, ref)` 返回文本。
传输细节与凭据都留在这里，路由宿主保持纯粹。
"""

from __future__ import annotations

import json
from typing import Any

# GitHub MCP 返回的文件内容是 JSON，正文在 content 字段（可能是 base64）。
_TEXT_FIELDS = ("content", "text", "file_content")
# GitHub MCP 的下载回执形如 "successfully downloaded text file (SHA: ...)"，不是正文。
_DOWNLOAD_RECEIPT = "successfully downloaded text file"


class McpFileReader:
    """经注入的调用函数读取 GitHub 文件。

    `call` 形如 ``call(tool_name, arguments) -> dict``，由外层提供（生产用 MCP 客户端，
    测试可直接传假实现），因此本类不发网络请求，便于替换与测试。
    """

    def __init__(self, call: Any, *, max_chars: int = 200_000) -> None:
        self._call = call
        self._max_chars = max_chars

    def read_file(self, owner: str, repo: str, path: str, ref: str = "") -> str:
        arguments: dict[str, Any] = {"owner": owner, "repo": repo, "path": path}
        if ref:
            arguments["sha"] = ref
        payload = self._call("get_file_contents", arguments)
        return self._extract_text(payload)

    def _extract_text(self, payload: Any) -> str:
        """兼容 MCP 的几种返回形态，取不到就明确报错而不是返回空串。"""
        if isinstance(payload, str):
            text = payload
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return text[: self._max_chars]
            return self._extract_text(parsed)

        if isinstance(payload, list):
            # GitHub MCP 把文件正文放在 resource 项里：
            #   [{'type':'text','text':'successfully downloaded ...'},
            #    {'type':'resource','resource':{'uri':..., 'text':'<正文>'}}]
            # text 项只是下载回执，不能当内容。
            for item in payload:
                if isinstance(item, dict) and item.get("type") == "resource":
                    resource = item.get("resource") or {}
                    text = resource.get("text")
                    if isinstance(text, str) and text.strip():
                        return text[: self._max_chars]
                    blob = resource.get("blob")
                    if isinstance(blob, str) and blob:
                        return self._maybe_base64(blob)[: self._max_chars]
            # 没有 resource 时才退回 text，但必须排除下载回执——
            # 回执说明内容没被读回来，此时应当报错而不是把回执当正文。
            parts = [
                item["text"]
                for item in payload
                if isinstance(item, dict)
                and isinstance(item.get("text"), str)
                and not self._is_receipt(item["text"])
            ]
            joined = "\n".join(parts)
            if joined.strip():
                return self._extract_text(joined)
            return self._fail("no-content")

        if isinstance(payload, dict) and isinstance(payload.get("resource"), dict):
            resource = payload["resource"]
            text = resource.get("text")
            if isinstance(text, str) and text.strip():
                return text[: self._max_chars]

        if isinstance(payload, dict):
            for key in _TEXT_FIELDS:
                value = payload.get(key)
                if isinstance(value, str) and value and not self._is_receipt(value):
                    # 若是 base64 编码（GitHub 常见），尝试解码
                    decoded = self._maybe_base64(value)
                    return decoded[: self._max_chars]
            raise ValueError("GitHub 返回中未找到文件内容字段")
        raise ValueError("GitHub 返回结构不可识别")

    def _is_receipt(self, text: str) -> bool:
        """下载回执不是文件内容，必须排除。"""
        return text.strip().lower().startswith(_DOWNLOAD_RECEIPT)

    def _maybe_base64(self, value: str) -> str:
        """GitHub 的 base64 内容通常带换行；不是 base64 就当纯文本。"""
        stripped = value.strip()
        if not stripped:
            return ""
        try:
            import base64

            raw = base64.b64decode(stripped, validate=False)
            text = raw.decode("utf-8")
            # 解出来像文本才采用，避免把普通字符串误当 base64。
            if text.isprintable() or "\n" in text:
                return text
        except Exception:
            pass
        return value

    @staticmethod
    def _fail(kind: str) -> str:
        raise ValueError(f"GitHub 返回内容为空（{kind}）")
