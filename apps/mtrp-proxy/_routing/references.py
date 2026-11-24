"""M→P 引用补全：从 M 库笔记正文中识别 GitHub 引用。

设计原则（对应 `docs/m-to-p-routing-plan.md` 的 R1.4）：
- **只接受正则可证的形态**，不做语义猜测。解析不出来就跳过该跳，不猜。
- 纯函数，不发网络请求，不读磁盘；便于逐条写单测并做确定性对拍。
- 引用必须显式指向某个仓库或文件，避免把普通文本误当引用。

支持的形态（按优先级）：

1. 完整文件链接：`https://github.com/<owner>/<repo>/blob/<ref>/<path>`
2. 仓库链接：     `https://github.com/<owner>/<repo>`（可带 `.git`）
3. 简写：         `<owner>/<repo>#<path>`、`<owner>/<repo>:<path>`
4. 描述式：       `repo: <owner>/<repo>`、`仓库: <owner>/<repo>`
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# owner / repo 的合法字符与 GitHub 规则一致（保守取值）。
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
_REPO = r"[A-Za-z0-9._-]{1,100}"
_PATH = r"[^\s)\]\"'`<>，。；：！？、（）【】《》]+"

# 1) 完整 blob 链接（含 ref 与文件路径）
_BLOB = re.compile(
    rf"https?://(?:www\.)?github\.com/(?P<owner>{_OWNER})/(?P<repo>{_REPO})(?:\.git)?"
    rf"/blob/(?P<ref>[^/\s]+)/(?P<path>{_PATH})",
    re.IGNORECASE,
)
# 2) 仓库链接（后面不能再跟路径段）
_REPO_URL = re.compile(
    rf"https?://(?:www\.)?github\.com/(?P<owner>{_OWNER})/(?P<repo>{_REPO})(?:\.git)?/?(?![\w/-])",
    re.IGNORECASE,
)
# 3) 简写 owner/repo#path 或 owner/repo:path
_SHORTHAND = re.compile(
    rf"(?<![\w/])(?P<owner>{_OWNER})/(?P<repo>{_REPO})(?:\.git)?(?P<sep>[#:])(?P<path>{_PATH})",
)
# 4) 描述式 repo: owner/repo
_DESCRIBED = re.compile(
    rf"(?:repo|repository|仓库)\s*[:：]\s*(?P<owner>{_OWNER})/(?P<repo>{_REPO})(?:\.git)?\b",
    re.IGNORECASE,
)

# 出现在路径里就说明不是文件路径的常见结尾标点。
_TRAILING = ".,;:!?，。；：！？、）)】]"


@dataclass(frozen=True)
class GithubReference:
    """一条可定位的 GitHub 引用。"""

    owner: str
    repo: str
    path: str = ""
    ref: str = ""
    kind: str = "repo"  # repo | file
    matched_text: str = ""

    @property
    def coordinate(self) -> str:
        return f"{self.owner}/{self.repo}"

    def key(self) -> tuple[str, str, str, str]:
        """去重键：同一仓库同一路径同一 ref 只保留一次。"""
        return (self.owner.lower(), self.repo.lower(), self.path, self.ref)


def _clean_path(raw: str) -> str:
    """去掉路径尾部标点与锚点，保留合法相对路径。"""
    path = (raw or "").strip()
    path = path.split("#", 1)[0]
    while path and path[-1] in _TRAILING:
        path = path[:-1]
    return path.lstrip("/")


def _within_span(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    """判断区间是否已被更高优先级的匹配覆盖，避免重复识别。"""
    return any(s <= start and end <= e for s, e in spans)


def extract_references(text: str, *, max_items: int = 20) -> list[GithubReference]:
    """从一段文本里提取 GitHub 引用，按出现顺序去重返回。

    超过 ``max_items`` 时截断，避免笔记里贴了几百条链接把结果撑爆。
    """
    if not text:
        return []
    results: list[GithubReference] = []
    seen: set[tuple[str, str, str, str]] = set()
    consumed: list[tuple[int, int]] = []

    def push(ref: GithubReference) -> None:
        key = ref.key()
        if key in seen:
            return
        seen.add(key)
        results.append(ref)

    # 优先级 1：完整 blob 链接，能拿到文件路径与 ref
    for match in _BLOB.finditer(text):
        path = _clean_path(match.group("path"))
        if not path:
            continue
        push(
            GithubReference(
                owner=match.group("owner"),
                repo=match.group("repo").removesuffix(".git"),
                path=path,
                ref=match.group("ref"),
                kind="file",
                matched_text=match.group(0),
            )
        )
        consumed.append((match.start(), match.end()))

    # 优先级 2：仓库链接（不与 blob 重叠）
    for match in _REPO_URL.finditer(text):
        if _within_span(consumed, match.start(), match.end()):
            continue
        push(
            GithubReference(
                owner=match.group("owner"),
                repo=match.group("repo").removesuffix(".git"),
                kind="repo",
                matched_text=match.group(0),
            )
        )
        consumed.append((match.start(), match.end()))

    # 优先级 3：简写 owner/repo#path
    for match in _SHORTHAND.finditer(text):
        if _within_span(consumed, match.start(), match.end()):
            continue
        path = _clean_path(match.group("path"))
        if not path:
            continue
        push(
            GithubReference(
                owner=match.group("owner"),
                repo=match.group("repo").removesuffix(".git"),
                path=path,
                kind="file",
                matched_text=match.group(0),
            )
        )

    # 优先级 4：描述式 repo: owner/repo
    for match in _DESCRIBED.finditer(text):
        if _within_span(consumed, match.start(), match.end()):
            continue
        push(
            GithubReference(
                owner=match.group("owner"),
                repo=match.group("repo").removesuffix(".git"),
                kind="repo",
                matched_text=match.group(0),
            )
        )

    return results[:max_items]


def file_targets(references: Iterable[GithubReference]) -> list[GithubReference]:
    """只挑出带具体路径的引用——路由里能真正取回内容的那些。"""
    return [ref for ref in references if ref.kind == "file" and ref.path]


def summarize(references: list[GithubReference]) -> dict[str, object]:
    """给调用方一个稳定的摘要，便于日志与前端展示。"""
    repos = {ref.coordinate.lower() for ref in references}
    files = file_targets(references)
    return {
        "total": len(references),
        "repos": sorted(repos),
        "file_count": len(files),
        "files": [
            {"coordinate": ref.coordinate, "path": ref.path, "ref": ref.ref}
            for ref in files
        ],
    }
