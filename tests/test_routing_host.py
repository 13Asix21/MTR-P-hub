"""M→P 路由宿主的行为测试：重点是失败隔离与降级语义。"""

from __future__ import annotations

import pytest

from mtrp_hub.routing.host import (
    M_P_REFERENCE_INTENT,
    FetchedFile,
    execute_reference_route,
)


class MemoryReader:
    """内存 P 读取器；可按路径注入失败。"""

    def __init__(self, files: dict[tuple[str, str, str], str], fail: set[str] | None = None) -> None:
        self.files = files
        self.fail = fail or set()
        self.calls: list[tuple[str, str, str, str]] = []

    def read_file(self, owner: str, repo: str, path: str, ref: str = "") -> str:
        self.calls.append((owner, repo, path, ref))
        if path in self.fail:
            raise RuntimeError(f"读取失败：{path}")
        key = (owner, repo, path)
        if key not in self.files:
            raise RuntimeError(f"文件不存在：{path}")
        return self.files[key]


def test_fetches_referenced_files() -> None:
    text = "实现见 https://github.com/o/r/blob/main/src/a.py 与 o/r#docs/b.md"
    reader = MemoryReader({("o", "r", "src/a.py"): "print(1)", ("o", "r", "docs/b.md"): "# b"})
    outcome = execute_reference_route(text, reader)

    assert outcome.intent == M_P_REFERENCE_INTENT
    assert len(outcome.fetched) == 2
    assert [f.path for f in outcome.fetched] == ["src/a.py", "docs/b.md"]
    assert outcome.degraded is False
    assert outcome.ok is True


def test_ref_is_passed_through() -> None:
    text = "https://github.com/o/r/blob/v1.2.3/src/a.py"
    reader = MemoryReader({("o", "r", "src/a.py"): "x"})
    execute_reference_route(text, reader)
    assert reader.calls == [("o", "r", "src/a.py", "v1.2.3")]


def test_missing_file_does_not_break_others() -> None:
    text = "o/r#ok.md 与 o/r#bad.md 与 o/r#ok2.md"
    reader = MemoryReader({("o", "r", "ok.md"): "1", ("o", "r", "ok2.md"): "2"}, fail={"bad.md"})
    outcome = execute_reference_route(text, reader)

    assert [f.path for f in outcome.fetched] == ["ok.md", "ok2.md"]
    assert [m.path for m in outcome.missing] == ["bad.md"]
    assert outcome.degraded is True
    assert outcome.ok is True
    assert "读取失败" in outcome.missing[0].reason


def test_no_reference_is_not_an_error() -> None:
    outcome = execute_reference_route("没有任何仓库引用的一段文字", MemoryReader({}))
    assert outcome.references == []
    assert outcome.fetched == []
    assert outcome.degraded is False
    assert outcome.ok is False


def test_repo_only_reference_is_not_fetched() -> None:
    text = "项目在 https://github.com/o/r"
    reader = MemoryReader({})
    outcome = execute_reference_route(text, reader)
    assert outcome.references
    assert outcome.fetched == []
    assert reader.calls == []


def test_oversize_file_is_skipped_with_reason() -> None:
    text = "o/r#big.md"
    reader = MemoryReader({("o", "r", "big.md"): "x" * 500})
    outcome = execute_reference_route(text, reader, max_bytes_per_file=100)
    assert outcome.fetched == []
    assert "文件过大" in outcome.missing[0].reason


def test_max_items_caps_work() -> None:
    text = " ".join(f"o/r#f{i}.md" for i in range(10))
    reader = MemoryReader({("o", "r", f"f{i}.md"): "x" for i in range(10)})
    outcome = execute_reference_route(text, reader, max_items=3)
    assert len(reader.calls) == 3
    assert len(outcome.fetched) == 3


def test_on_step_reports_progress() -> None:
    stages: list[str] = []
    reader = MemoryReader({("o", "r", "a.md"): "x"})
    execute_reference_route("o/r#a.md", reader, on_step=lambda stage, _p: stages.append(stage))
    assert stages == ["parsed", "fetched", "done"]


def test_summary_shape() -> None:
    reader = MemoryReader({("o", "r", "a.md"): "abc"}, fail={"b.md"})
    outcome = execute_reference_route("o/r#a.md 与 o/r#b.md", reader)
    summary = outcome.summary()
    assert summary["intent"] == M_P_REFERENCE_INTENT
    assert summary["fetched_count"] == 1
    assert summary["missing_count"] == 1
    assert summary["degraded"] is True
    assert summary["fetched"] == [{"coordinate": "o/r", "path": "a.md", "ref": "", "chars": 3}]


def test_empty_source_is_safe() -> None:
    outcome = execute_reference_route("", MemoryReader({}))
    assert outcome.source_chars == 0
    assert outcome.fetched == []


def test_duplicate_references_fetch_once() -> None:
    text = "o/r#a.md 又一次 o/r#a.md"
    reader = MemoryReader({("o", "r", "a.md"): "x"})
    outcome = execute_reference_route(text, reader)
    assert len(reader.calls) == 1
    assert len(outcome.fetched) == 1
