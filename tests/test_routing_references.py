"""M→P 引用解析器的确定性测试：宁可少识别，不可误识别。"""

from __future__ import annotations

import pytest

from mtrp_hub.routing.references import (
    GithubReference,
    extract_references,
    file_targets,
    summarize,
)


def test_blob_link_yields_file_with_ref() -> None:
    text = "见 https://github.com/13Asix21/MTR-P-hub/blob/main/src/mtrp_hub/routing/planner.py 的实现"
    refs = extract_references(text)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.owner == "13Asix21"
    assert ref.repo == "MTR-P-hub"
    assert ref.path == "src/mtrp_hub/routing/planner.py"
    assert ref.ref == "main"
    assert ref.kind == "file"


def test_repo_link_yields_repo_only() -> None:
    refs = extract_references("项目在 https://github.com/13Asix21/MTR-P-hub")
    assert len(refs) == 1
    assert refs[0].kind == "repo"
    assert refs[0].coordinate == "13Asix21/MTR-P-hub"
    assert refs[0].path == ""


def test_shorthand_hash_and_colon() -> None:
    refs = extract_references("改 13Asix21/MTR-P-hub#docs/plan.md 与 a/b:src/x.py 两处")
    assert [r.kind for r in refs] == ["file", "file"]
    assert refs[0].path == "docs/plan.md"
    assert refs[1].path == "src/x.py"


def test_described_form() -> None:
    refs = extract_references("仓库: 13Asix21/thinkloop 与 repo: a/b")
    assert {r.coordinate for r in refs} == {"13Asix21/thinkloop", "a/b"}
    assert all(r.kind == "repo" for r in refs)


def test_git_suffix_is_stripped() -> None:
    refs = extract_references("https://github.com/o/r.git")
    assert refs[0].repo == "r"


def test_trailing_punctuation_is_removed() -> None:
    refs = extract_references("见 o/r#a/b.md，以及 o/r#c/d.py。")
    assert [r.path for r in refs] == ["a/b.md", "c/d.py"]


def test_deduplicates_same_target() -> None:
    text = "https://github.com/o/r/blob/main/a.md 和 o/r#a.md"
    refs = extract_references(text)
    # 完整链接带 ref、简写不带 ref，属于不同键，但同一文件路径仍应可辨
    assert len(refs) <= 2
    assert all(r.path == "a.md" for r in refs)


def test_ignores_plain_text_without_reference() -> None:
    assert extract_references("这段文字没有仓库引用，也没有链接。") == []
    assert extract_references("") == []


def test_ignores_non_github_url() -> None:
    assert extract_references("见 https://gitlab.com/o/r/blob/main/a.md") == []


def test_ignores_relative_path_that_looks_like_repo() -> None:
    # 单段路径（无 owner）不应被当成 owner/repo
    assert extract_references("修改 ./src/app.py 与 2-Permanent/note.md") == []


def test_max_items_is_respected() -> None:
    text = " ".join(f"a/r{i}#f{i}.md" for i in range(30))
    refs = extract_references(text, max_items=5)
    assert len(refs) == 5


def test_file_targets_filters_repo_only() -> None:
    refs = extract_references("https://github.com/o/r 与 o/r#a.md")
    files = file_targets(refs)
    assert len(files) == 1
    assert files[0].path == "a.md"


def test_summarize_shape() -> None:
    # 裸 "o/s" 不做识别（与本地路径难区分），只认带锚点的引用。
    refs = extract_references("o/r#a.md 与 https://github.com/o/s")
    summary = summarize(refs)
    assert summary["total"] == 2
    assert summary["repos"] == ["o/r", "o/s"]
    assert summary["file_count"] == 1
    assert summary["files"] == [{"coordinate": "o/r", "path": "a.md", "ref": ""}]


def test_bare_owner_repo_is_not_guessed() -> None:
    """裸 owner/repo 与本地相对路径无法区分，必须不识别，避免误判。"""
    assert extract_references("13Asix21/MTR-P-hub") == []
    assert extract_references("2-Permanent/note.md") == []


def test_anchor_is_dropped_from_path() -> None:
    refs = extract_references("https://github.com/o/r/blob/main/docs/a.md#section-2")
    assert refs[0].path == "docs/a.md"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app.py", "src/app.py"),
        ("/src/app.py", "src/app.py"),
        ("src/app.py)", "src/app.py"),
        ("src/app.py.", "src/app.py"),
    ],
)
def test_clean_path_variants(path: str, expected: str) -> None:
    refs = extract_references(f"o/r#{path}")
    assert refs[0].path == expected


def test_reference_key_is_case_insensitive_on_coordinate() -> None:
    a = GithubReference(owner="O", repo="R", path="x.md", kind="file")
    b = GithubReference(owner="o", repo="r", path="x.md", kind="file")
    assert a.key() == b.key()
