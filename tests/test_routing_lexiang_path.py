"""按名称定位企业乐享目录的测试：核心是"不猜、不重复建"。"""

from __future__ import annotations

import pytest

from mtrp_hub.routing.lexiang_path import (
    PathResolveError,
    normalize_name,
    resolve_existing_path,
    split_path,
)

ROOT = "root-id"


def tree(nodes: dict[str, list[dict[str, object]]]):
    """构造 list_children：nodes 的 key 是 parent_id。"""

    def list_children(parent_id: str):
        if parent_id not in nodes:
            raise RuntimeError(f"未知节点 {parent_id}")
        return nodes[parent_id]

    return list_children


def folder(node_id: str, name: str) -> dict[str, object]:
    return {"id": node_id, "name": name, "entry_type": "folder"}


REAL_TREE = tree(
    {
        ROOT: [
            folder("f00", "00-导航与规范"),
            folder("f01", "01 - 研发项目"),
            folder("f02", "02-产品与软件"),
        ],
        "f01": [
            folder("p01", "01-***项目【负责人：***】"),
            folder("p05", "05载板清洗线-定位【负责人 倪青云】"),
            folder("p06", "06 PED烧靶位置视觉检测引导【负责人：李元伯】"),
        ],
        "p06": [],
    }
)


def test_resolves_two_level_path_to_existing_folder() -> None:
    result = resolve_existing_path(REAL_TREE, ROOT, "01 - 研发项目/06 PED烧靶位置视觉检测引导【负责人：李元伯】")
    assert result.entry_id == "p06"
    assert result.depth == 2
    assert result.path == "01 - 研发项目/06 PED烧靶位置视觉检测引导【负责人：李元伯】"


def test_accepts_angle_bracket_separator() -> None:
    result = resolve_existing_path(REAL_TREE, ROOT, "01 - 研发项目 > 06 PED烧靶位置视觉检测引导【负责人：李元伯】")
    assert result.entry_id == "p06"


def test_does_not_match_partial_name() -> None:
    """只允许精确匹配，"06 PED" 不能命中完整长名。"""
    with pytest.raises(PathResolveError) as info:
        resolve_existing_path(REAL_TREE, ROOT, "01 - 研发项目/06 PED")
    assert info.value.code == "SEGMENT_NOT_FOUND"


def test_missing_segment_reports_the_level() -> None:
    with pytest.raises(PathResolveError) as info:
        resolve_existing_path(REAL_TREE, ROOT, "01 - 研发项目/07 不存在的项目")
    assert info.value.code == "SEGMENT_NOT_FOUND"
    assert "第 2 层" in info.value.message
    # 报错里应列出该层现有名称，便于排查
    assert "06 PED" in info.value.message


def test_missing_first_level_reports_first() -> None:
    with pytest.raises(PathResolveError) as info:
        resolve_existing_path(REAL_TREE, ROOT, "99-不存在")
    assert "第 1 层" in info.value.message


def test_ambiguous_duplicate_names_are_rejected() -> None:
    dup = tree(
        {
            ROOT: [folder("a1", "同名目录"), folder("a2", "同名目录")],
        }
    )
    with pytest.raises(PathResolveError) as info:
        resolve_existing_path(dup, ROOT, "同名目录")
    assert info.value.code == "SEGMENT_AMBIGUOUS"
    assert "a1" in info.value.message and "a2" in info.value.message


def test_last_segment_must_be_folder() -> None:
    with_page = tree({ROOT: [{"id": "pg", "name": "某文档", "entry_type": "page"}]})
    with pytest.raises(PathResolveError) as info:
        resolve_existing_path(with_page, ROOT, "某文档")
    assert info.value.code == "TARGET_NOT_FOLDER"


def test_empty_path_rejected() -> None:
    with pytest.raises(PathResolveError) as info:
        resolve_existing_path(REAL_TREE, ROOT, "   ")
    assert info.value.code == "PATH_EMPTY"


def test_list_failure_is_wrapped() -> None:
    def boom(_parent: str):
        raise RuntimeError("网络断开")

    with pytest.raises(PathResolveError) as info:
        resolve_existing_path(boom, ROOT, "任意")
    assert info.value.code == "LIST_FAILED"
    assert "网络断开" in info.value.message


def test_whitespace_is_normalized() -> None:
    spaced = tree({ROOT: [folder("x", "  01 -   研发项目  ")]})
    assert resolve_existing_path(spaced, ROOT, "01 - 研发项目").entry_id == "x"


def test_split_path_handles_separators() -> None:
    assert split_path("a/b") == ["a", "b"]
    assert split_path("/a/b/") == ["a", "b"]
    assert split_path("a > b") == ["a", "b"]
    assert split_path("") == []


def test_normalize_name_nfc() -> None:
    # 分解形式与合成形式应视为同名
    assert normalize_name("cafe\u0301") == normalize_name("café")


def test_never_creates_or_writes() -> None:
    """本模块必须只读：调用方不应看到任何写操作。"""
    calls: list[str] = []

    def spy(parent_id: str):
        calls.append(parent_id)
        return REAL_TREE(parent_id)

    resolve_existing_path(spy, ROOT, "01 - 研发项目/06 PED烧靶位置视觉检测引导【负责人：李元伯】")
    assert calls == [ROOT, "f01"], "只应发生两次只读列举"
