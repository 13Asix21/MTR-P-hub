"""M→P 串联检索（R2）的行为测试。"""

from __future__ import annotations

import pytest

from mtrp_hub.routing.serial import (
    ProjectHint,
    execute_serial_route,
    extract_project_hints,
)


class FakeTree:
    """内存版路径检索：按关键词返回预设命中，可注入失败。"""

    def __init__(self, table: dict[str, list[dict[str, object]]], fail: set[str] | None = None) -> None:
        self.table = table
        self.fail = fail or set()
        self.calls: list[str] = []

    def search_paths(self, owner: str, repo: str, keyword: str):
        self.calls.append(keyword)
        if keyword in self.fail:
            raise RuntimeError(f"搜索失败：{keyword}")
        return self.table.get(keyword, [])


def test_extracts_path_hint_first() -> None:
    text = "参考 src/mtrp_hub/routing/host.py 的实现"
    hints = extract_project_hints(text)
    assert hints[0].kind == "path"
    assert hints[0].keyword == "host.py"


def test_extracts_code_span_and_named() -> None:
    text = "调用 `execute_reference_route`，项目：MTR-P-hub 里有说明"
    hints = extract_project_hints(text)
    kinds = {h.kind for h in hints}
    assert "code" in kinds
    assert "named" in kinds


def test_stopwords_are_filtered() -> None:
    hints = extract_project_hints("我们需要进行通过相关的内容说明")
    assert [h for h in hints if h.keyword in ("需要", "进行", "通过", "相关")] == []


def test_empty_text_gives_no_hint() -> None:
    assert extract_project_hints("") == []


def test_deduplicates_hints() -> None:
    text = "`host.py` 与 `host.py` 重复出现"
    hints = extract_project_hints(text)
    assert len([h for h in hints if h.keyword == "host.py"]) == 1


def test_serial_route_hits_paths() -> None:
    tree = FakeTree({"host.py": [{"path": "src/mtrp_hub/routing/host.py", "score": 2}]})
    outcome = execute_serial_route("见 `host.py` 的实现", "o/r", tree)
    assert len(outcome.hits) == 1
    assert outcome.hits[0].path == "src/mtrp_hub/routing/host.py"
    assert outcome.searched == 1


def test_serial_route_survives_search_failure() -> None:
    tree = FakeTree({"good": [{"path": "a/good.md", "score": 2}]}, fail={"bad"})
    outcome = execute_serial_route("`good` 与 `bad`", "o/r", tree)
    assert [h.path for h in outcome.hits] == ["a/good.md"]
    assert outcome.errors
    assert outcome.summary()["degraded"] is True


def test_serial_route_rejects_bad_coordinate() -> None:
    outcome = execute_serial_route("`x`", "not-a-coordinate", FakeTree({}))
    assert outcome.hits == []
    assert outcome.errors


def test_serial_route_deduplicates_same_path_by_best_score() -> None:
    # 关键词需 >= 3 字符才被视为有效线索（单字母会被过滤，属设计意图）
    tree = FakeTree(
        {
            "alpha": [{"path": "x/y.md", "score": 1}],
            "bravo": [{"path": "x/y.md", "score": 5}],
        }
    )
    outcome = execute_serial_route("`alpha` 与 `bravo`", "o/r", tree)
    assert len(outcome.hits) == 1
    assert outcome.hits[0].score == 5


def test_on_step_reports_progress() -> None:
    stages: list[str] = []
    tree = FakeTree({"host": [{"path": "a/host.py", "score": 2}]})
    execute_serial_route("`host`", "o/r", tree, on_step=lambda s, _p: stages.append(s))
    assert stages[0] == "hinted"
    assert stages[-1] == "done"


def test_no_hint_returns_cleanly() -> None:
    outcome = execute_serial_route("", "o/r", FakeTree({}))
    assert outcome.hits == []
    assert outcome.errors == []


def test_summary_shape() -> None:
    tree = FakeTree({"host": [{"path": "a/host.py", "score": 2}]})
    outcome = execute_serial_route("`host`", "o/r", tree)
    summary = outcome.summary()
    assert summary["intent"] == "m_to_p_serial"
    assert summary["hit_count"] == 1
    assert summary["hits"][0]["path"] == "a/host.py"


def test_hint_cap_is_respected() -> None:
    text = " ".join(f"`tok{i}abc`" for i in range(20))
    hints = extract_project_hints(text, max_items=3)
    assert len(hints) == 3
