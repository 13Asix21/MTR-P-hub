"""M→P 引用补全的路由执行宿主（R1.3/R1.5）。

职责边界：
- **只做编排**：拿一段 M 库正文 → 解析 GitHub 引用 → 逐个取回 P 库文件。
- **不做网络实现**：远端调用由注入的 `reader` 完成（生产用 MCP 客户端，测试用假实现）。
  这样路由逻辑可以完全离网单测，也避免把凭据/传输细节混进领域代码。
- **失败不冒泡**：任何一跳失败只标记该跳，已有结果照常返回，并明确说明缺了什么。

与 `docs/m-to-p-routing-plan.md` 的对应：
- R1.1 规则：由 `M_P_REFERENCE_INTENT` 表达
- R1.3 宿主：本文件 `execute_reference_route`
- R1.5 合并与降级：`RouteOutcome` 的 `degraded` / `missing`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from .references import (
    GithubReference,
    extract_references,
    file_targets,
    summarize,
)

M_P_REFERENCE_INTENT = "m_to_p_reference"


class PReader(Protocol):
    """P 库读取能力的抽象；生产实现经 MCP，测试用内存实现。"""

    def read_file(self, owner: str, repo: str, path: str, ref: str = "") -> str:
        """返回文件文本内容；失败应抛异常，由宿主编排收敛为降级。"""
        ...


@dataclass(frozen=True)
class FetchedFile:
    """一条成功取回的 P 库文件。"""

    coordinate: str
    path: str
    ref: str
    content: str
    chars: int


@dataclass(frozen=True)
class MissingFile:
    """一条未能取回的引用，附带可读原因。"""

    coordinate: str
    path: str
    ref: str
    reason: str


@dataclass
class RouteOutcome:
    """一次 M→P 路由的结果。

    `degraded` 为 True 表示有引用没取到，但已取到的不受影响；
    调用方不应把 degraded 当成整体失败。
    """

    intent: str = M_P_REFERENCE_INTENT
    source_chars: int = 0
    references: list[GithubReference] = field(default_factory=list)
    fetched: list[FetchedFile] = field(default_factory=list)
    missing: list[MissingFile] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.missing)

    @property
    def ok(self) -> bool:
        return bool(self.fetched)

    def summary(self) -> dict[str, object]:
        base = summarize(self.references)
        base.update(
            {
                "intent": self.intent,
                "source_chars": self.source_chars,
                "fetched_count": len(self.fetched),
                "missing_count": len(self.missing),
                "degraded": self.degraded,
                "fetched": [
                    {"coordinate": f.coordinate, "path": f.path, "ref": f.ref, "chars": f.chars}
                    for f in self.fetched
                ],
                "missing": [
                    {"coordinate": m.coordinate, "path": m.path, "ref": m.ref, "reason": m.reason}
                    for m in self.missing
                ],
            }
        )
        return base


def execute_reference_route(
    source_text: str,
    reader: PReader,
    *,
    max_items: int = 20,
    max_bytes_per_file: int = 200_000,
    on_step: Callable[[str, dict[str, object]], None] | None = None,
) -> RouteOutcome:
    """执行一次 M→P 引用补全。

    参数：
        source_text: M 库笔记正文（已解码的文本）。
        reader:      P 库读取器，注入以便离线测试。
        max_items:   最多处理多少条引用，防止单篇笔记引用过多拖垮响应。
        max_bytes_per_file: 单文件字符上限，超出视为过大而跳过。
        on_step:     可选回调，逐跳上报进度（供前端或日志使用）。
    """
    outcome = RouteOutcome(source_chars=len(source_text or ""))

    def emit(stage: str, payload: dict[str, object]) -> None:
        if on_step is not None:
            on_step(stage, payload)

    references = extract_references(source_text or "", max_items=max_items)
    outcome.references = references
    emit("parsed", {"total": len(references), "files": len(file_targets(references))})

    targets = file_targets(references)
    if not targets:
        # 没有可定位的文件引用：不是错误，只是这跳无事可做。
        emit("no_target", {})
        return outcome

    for ref in targets:
        try:
            content = reader.read_file(ref.owner, ref.repo, ref.path, ref.ref)
        except Exception as error:  # 单跳失败不影响其余
            reason = getattr(error, "message", None) or str(error) or type(error).__name__
            outcome.missing.append(
                MissingFile(
                    coordinate=ref.coordinate,
                    path=ref.path,
                    ref=ref.ref,
                    reason=str(reason)[:200],
                )
            )
            emit("missing", {"coordinate": ref.coordinate, "path": ref.path, "reason": str(reason)[:120]})
            continue

        text = content or ""
        if len(text) > max_bytes_per_file:
            outcome.missing.append(
                MissingFile(
                    coordinate=ref.coordinate,
                    path=ref.path,
                    ref=ref.ref,
                    reason=f"文件过大（{len(text)} 字符，上限 {max_bytes_per_file}）",
                )
            )
            emit("skipped_too_large", {"coordinate": ref.coordinate, "path": ref.path, "chars": len(text)})
            continue

        outcome.fetched.append(
            FetchedFile(
                coordinate=ref.coordinate,
                path=ref.path,
                ref=ref.ref,
                content=text,
                chars=len(text),
            )
        )
        emit("fetched", {"coordinate": ref.coordinate, "path": ref.path, "chars": len(text)})

    emit("done", {"fetched": len(outcome.fetched), "missing": len(outcome.missing)})
    return outcome
