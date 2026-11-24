"""序号命名规则的测试：重点是不重复编号、前缀推导稳定。"""

from __future__ import annotations

import pytest

from mtrp_hub.routing.naming import NamePlan, parse_existing, plan_indexed_name


def test_empty_directory_uses_default_prefix_from_one() -> None:
    plan = plan_indexed_name("烧靶位置检测方案", [], default_prefix="PED")
    assert plan.name == "PED-01-烧靶位置检测方案.md"
    assert plan.seq == 1


def test_continues_existing_prefix_sequence() -> None:
    existing = ["AB-01-需求分析.md", "AB-02-接口设计.md", "AB-03-测试计划.md"]
    plan = plan_indexed_name("上线方案", existing)
    assert plan.name == "AB-04-上线方案.md"
    assert plan.prefix == "AB"


def test_bare_sequence_is_continued() -> None:
    plan = plan_indexed_name("第三份文档", ["01-启动.md", "02-执行.md"])
    assert plan.name == "03-第三份文档.md"


def test_title_with_existing_number_is_kept() -> None:
    plan = plan_indexed_name("AB-05-已有编号.md", ["AB-01-a.md"])
    assert plan.name == "AB-05-已有编号.md"
    assert "保持原样" in plan.reason


def test_bare_numbered_title_is_kept() -> None:
    plan = plan_indexed_name("07-已有序号.md", ["01-a.md"])
    assert plan.name == "07-已有序号.md"


def test_does_not_double_number() -> None:
    """标题若残留纯序号片段，不应生成 AB-04-03-xxx。"""
    plan = plan_indexed_name("03-新内容", ["AB-01-a.md", "AB-02-b.md", "AB-03-c.md"])
    assert plan.name == "AB-04-新内容.md"


def test_majority_prefix_wins_when_multiple() -> None:
    existing = ["AB-01-a.md", "AB-02-b.md", "CD-01-c.md"]
    plan = plan_indexed_name("新文档", existing)
    assert plan.prefix == "AB"
    assert plan.name == "AB-03-新文档.md"


def test_no_prefix_anywhere_gives_default_when_configured() -> None:
    plan = plan_indexed_name("文档", ["无编号文件.md"], default_prefix="PED")
    assert plan.name == "PED-01-文档.md"


def test_width_is_configurable() -> None:
    plan = plan_indexed_name("文档", ["AB-009-a.md"], width=3)
    assert plan.name == "AB-010-文档.md"


def test_parse_existing_variants() -> None:
    assert parse_existing("AB-01-x.md") == ("AB", 1, "x")
    assert parse_existing("01-x.md") == ("", 1, "x")
    assert parse_existing("无编号.md") is None


def test_empty_title_falls_back() -> None:
    plan = plan_indexed_name("", [], default_prefix="PED")
    assert plan.name == "PED-01-未命名笔记.md"


def test_summary_is_json_friendly() -> None:
    plan = plan_indexed_name("a", ["AB-01-b.md"])
    summary = plan.summary()
    assert summary["name"] == "AB-02-a.md"
    assert isinstance(summary["seq"], int)


def test_ignores_non_conforming_names() -> None:
    plan = plan_indexed_name("新内容", ["笔记一.md", "笔记二.md"], default_prefix="PED")
    assert plan.name == "PED-01-新内容.md"
    assert plan.seq == 1
