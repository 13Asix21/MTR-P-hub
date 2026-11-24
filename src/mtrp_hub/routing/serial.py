"""M→P 串联检索（R2）：从 M 命中正文里提取项目线索，再到 P 库找相关文件。

与 R1 的区别：
- R1 依赖正文里的**显式链接**，能精确定位到文件。
- R2 依赖正文里的**项目/关键词线索**，先猜仓库再按路径匹配，属于"扩大召回"。

因此 R2 的定位是"补充"，不是"替代"：结果必须带匹配依据，找不到就明确说没找到，
不能拿一堆无关文件充数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

# 常见的中文/英文停用词，避免拿它们去搜仓库路径。
_STOPWORDS = frozenset(
    """
    我们 这个 那个 一个 可以 已经 因为 所以 但是 如果 需要 进行 通过 相关 对应 下面 上面 其中
    以及 并且 或者 同时 目前 现在 这样 那样 之后 之前 这些 那些 没有 就是 不是 还是 只是
    什么 怎么 如何 为什么 为了 按照 根据 包括 比如 例如 方法 方式 内容 问题 情况 时候
    the and for with that this from are was were will can not you your all any but has have
    """.split()
)

# 项目线索：反引号内标识符、驼峰/蛇形路径、以及"项目/仓库/模块"后跟的名称。
_CODE_SPAN = re.compile(r"`([A-Za-z0-9_./-]{3,60})`")
_PATH_LIKE = re.compile(r"(?<![\w/])([A-Za-z0-9_.-]{2,40}/[A-Za-z0-9_./-]{2,80})")
_NAMED = re.compile(r"(?:项目|仓库|模块|服务|组件)\s*[:：]?\s*[《\"']?([\w\u4e00-\u9fa5.-]{2,30})[》\"']?")
_CAMEL = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+){1,5})\b")
_SNAKE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+){1,5})\b")


class PTreeSearch(Protocol):
    """P 库按关键词查路径的抽象；生产实现经 MCP 目录列举。"""

    def search_paths(self, owner: str, repo: str, keyword: str) -> list[dict[str, object]]:
        ...


@dataclass(frozen=True)
class ProjectHint:
    """从正文里提到的一条项目线索。"""

    keyword: str
    kind: str  # path | code | named | camel | snake
    matched_text: str = ""


@dataclass
class SerialHit:
    """一个仓库下命中到的一条路径。"""

    coordinate: str
    path: str
    keyword: str
    score: int


@dataclass
class SerialOutcome:
    keyword_repo: str = ""
    hints: list[ProjectHint] = field(default_factory=list)
    hits: list[SerialHit] = field(default_factory=list)
    searched: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.hits)

    def summary(self) -> dict[str, object]:
        return {
            "intent": "m_to_p_serial",
            "keyword_repo": self.keyword_repo,
            "hint_count": len(self.hints),
            "hints": [{"keyword": h.keyword, "kind": h.kind} for h in self.hints],
            "searched": self.searched,
            "hit_count": len(self.hits),
            "hits": [
                {"coordinate": h.coordinate, "path": h.path, "keyword": h.keyword, "score": h.score}
                for h in self.hits
            ],
            "errors": self.errors,
            "degraded": bool(self.errors),
        }


def _is_meaningful(token: str) -> bool:
    value = token.strip()
    if len(value) < 3 or len(value) > 60:
        return False
    if value.lower() in _STOPWORDS:
        return False
    # 纯数字或纯符号没有检索价值
    return any(ch.isalnum() for ch in value)


def extract_project_hints(text: str, *, max_items: int = 8) -> list[ProjectHint]:
    """从正文里提取可用于检索的项目线索，按可靠性排序去重。

    顺序：显式路径 > 反引号代码 > "项目：X" 命名 > 驼峰 > 蛇形。
    越靠前的越可能是真项目名，后面的只是补充召回。
    """
    if not text:
        return []
    hints: list[ProjectHint] = []
    seen: set[str] = set()

    def push(keyword: str, kind: str, matched: str = "") -> None:
        key = keyword.strip().lower()
        if not _is_meaningful(keyword) or key in seen:
            return
        seen.add(key)
        hints.append(ProjectHint(keyword=keyword.strip(), kind=kind, matched_text=matched))

    for pattern, kind in (
        (_PATH_LIKE, "path"),
        (_CODE_SPAN, "code"),
        (_NAMED, "named"),
        (_CAMEL, "camel"),
        (_SNAKE, "snake"),
    ):
        for match in pattern.finditer(text):
            token = match.group(1)
            if kind == "path":
                # 路径线索只取最后一段作为关键词（整段太长且易误配）
                token = token.rsplit("/", 1)[-1]
            push(token, kind, match.group(0))
        if len(hints) >= max_items:
            break
    return hints[:max_items]


def execute_serial_route(
    source_text: str,
    coordinate: str,
    searcher: PTreeSearch,
    *,
    max_hints: int = 6,
    on_step: Callable[[str, dict[str, object]], None] | None = None,
) -> SerialOutcome:
    """在指定仓库里按线索检索路径。

    参数：
        source_text: M 库正文。
        coordinate:  目标仓库，形如 ``owner/repo``。
        searcher:    P 库路径检索器（注入以便离线测试）。
    """
    outcome = SerialOutcome(keyword_repo=coordinate)

    def emit(stage: str, payload: dict[str, object]) -> None:
        if on_step is not None:
            on_step(stage, payload)

    if "/" not in (coordinate or ""):
        outcome.errors.append(f"仓库坐标不合法：{coordinate!r}")
        return outcome

    owner, repo = coordinate.split("/", 1)
    hints = extract_project_hints(source_text, max_items=max_hints)
    outcome.hints = hints
    emit("hinted", {"count": len(hints), "keywords": [h.keyword for h in hints]})

    if not hints:
        emit("no_hint", {})
        return outcome

    for hint in hints:
        outcome.searched += 1
        try:
            paths = searcher.search_paths(owner, repo, hint.keyword)
        except Exception as error:  # 单个关键词失败不影响其余
            outcome.errors.append(f"{hint.keyword}: {getattr(error, 'message', str(error))[:120]}")
            emit("search_failed", {"keyword": hint.keyword})
            continue
        for item in paths or []:
            path = str(item.get("path") or "")
            if not path:
                continue
            outcome.hits.append(
                SerialHit(
                    coordinate=coordinate,
                    path=path,
                    keyword=hint.keyword,
                    score=int(item.get("score") or 1),
                )
            )
        emit("searched", {"keyword": hint.keyword, "hits": len(paths or [])})

    # 同一路径被多个关键词命中时只保留最高分的一条
    best: dict[str, SerialHit] = {}
    for hit in outcome.hits:
        current = best.get(hit.path)
        if current is None or hit.score > current.score:
            best[hit.path] = hit
    outcome.hits = sorted(best.values(), key=lambda h: (-h.score, h.path))[:20]
    emit("done", {"hits": len(outcome.hits), "errors": len(outcome.errors)})
    return outcome
