"""MCP 版 P 读取器的形态兼容测试。"""

from __future__ import annotations

import base64
import json

import pytest

from mtrp_hub.routing.mcp_reader import McpFileReader
from mtrp_hub.routing.host import execute_reference_route


def _reader(payload, **kwargs):
    calls: list[tuple[str, dict]] = []

    def call(tool, arguments):
        calls.append((tool, arguments))
        return payload

    return McpFileReader(call, **kwargs), calls


def test_reads_plain_text_field() -> None:
    payload = {"content": "# 标题\n正文"}
    reader, calls = _reader(payload)
    assert reader.read_file("o", "r", "a.md") == "# 标题\n正文"
    assert calls == [("get_file_contents", {"owner": "o", "repo": "r", "path": "a.md"})]


def test_passes_ref_as_sha() -> None:
    reader, calls = _reader({"content": "x"})
    reader.read_file("o", "r", "a.md", "main")
    assert calls[0][1]["sha"] == "main"


def test_decodes_base64_content() -> None:
    raw = "# 你好\n内容"
    payload = {"content": base64.b64encode(raw.encode()).decode()}
    reader, _ = _reader(payload)
    assert reader.read_file("o", "r", "a.md") == raw


def test_parses_json_string_payload() -> None:
    payload = json.dumps({"content": "从字符串解析"})
    reader, _ = _reader(payload)
    assert reader.read_file("o", "r", "a.md") == "从字符串解析"


def test_parses_mcp_content_array() -> None:
    payload = [{"type": "text", "text": json.dumps({"content": "数组里的内容"})}]
    reader, _ = _reader(payload)
    assert reader.read_file("o", "r", "a.md") == "数组里的内容"


def test_missing_field_raises() -> None:
    reader, _ = _reader({"unexpected": "x"})
    with pytest.raises(ValueError):
        reader.read_file("o", "r", "a.md")


def test_empty_payload_raises() -> None:
    reader, _ = _reader([])
    with pytest.raises(ValueError):
        reader.read_file("o", "r", "a.md")


def test_truncates_to_max_chars() -> None:
    reader, _ = _reader({"content": "x" * 500}, max_chars=100)
    assert len(reader.read_file("o", "r", "a.md")) == 100


def test_reader_drives_the_host_end_to_end() -> None:
    def call(_tool, arguments):
        return {"content": f"# {arguments['path']}"}

    outcome = execute_reference_route("o/r#a.md 与 o/r#b.md", McpFileReader(call))
    assert [f.content for f in outcome.fetched] == ["# a.md", "# b.md"]
    assert outcome.degraded is False


def test_host_degrades_when_reader_raises() -> None:
    def call(_tool, arguments):
        if arguments["path"] == "bad.md":
            raise RuntimeError("403")
        return {"content": "ok"}

    outcome = execute_reference_route("o/r#ok.md 与 o/r#bad.md", McpFileReader(call))
    assert [f.path for f in outcome.fetched] == ["ok.md"]
    assert [m.path for m in outcome.missing] == ["bad.md"]


def test_reads_text_from_resource_item() -> None:
    """GitHub MCP 真实形态：正文在 resource 项，text 项只是下载回执。"""
    payload = [
        {"type": "text", "text": "successfully downloaded text file (SHA: abc123)"},
        {"type": "resource", "resource": {"uri": "repo://o/r/sha/abc/contents/a.md",
                                          "mimeType": "text/plain; charset=utf-8",
                                          "text": "# 真实正文\n第二行"}},
    ]
    reader, _ = _reader(payload)
    assert reader.read_file("o", "r", "a.md") == "# 真实正文\n第二行"


def test_download_receipt_is_never_returned_as_content() -> None:
    payload = [{"type": "text", "text": "successfully downloaded text file (SHA: abc)"}]
    reader, _ = _reader(payload)
    with pytest.raises(ValueError):
        reader.read_file("o", "r", "a.md")


def test_resource_blob_alternative() -> None:
    raw = "# blob 正文"
    payload = [{"type": "resource", "resource": {"blob": base64.b64encode(raw.encode()).decode()}}]
    reader, _ = _reader(payload)
    assert reader.read_file("o", "r", "a.md") == raw
