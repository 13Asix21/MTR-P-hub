"""在企业乐享空间里按路径精准定位**已存在**的目录（A2）。

与 `_resolve_parent` 的区别：
- `_resolve_parent` 是"找不到就新建"，适合首次落地。
- 本模块是"**只找不建**"，用于往既有项目文件夹里写内容——找不到必须报错，
  否则会在根下重复造一个同名目录。

匹配规则（保守）：
- 名称比较前做 Unicode NFC 归一与去首尾空白，**不做模糊包含匹配**。
- 同一层出现多个同名节点 → 报歧义，不猜。
- 任何一层缺失 → 明确指出是哪一层，便于排查。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Callable


class PathResolveError(Exception):
    """路径解析失败；message 必须能定位到具体层级。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedFolder:
    """解析结果。"""

    entry_id: str
    name: str
    depth: int
    path: str


def normalize_name(name: Any) -> str:
    """统一名称比较口径：NFC + 去首尾空白 + 折叠内部连续空白。"""
    text = unicodedata.normalize("NFC", str(name or ""))
    return " ".join(text.split())


def split_path(raw_path: str) -> list[str]:
    """把路径字符串切成层级；同时接受 `/` 与 `>` 两种分隔。"""
    text = str(raw_path or "").replace(">", "/").strip().strip("/")
    return [segment.strip() for segment in text.split("/") if segment.strip()]


def resolve_existing_path(
    list_children: Callable[[str], list[dict[str, Any]]],
    root_entry_id: str,
    raw_path: str,
    *,
    max_depth: int = 8,
) -> ResolvedFolder:
    """逐层在已存在节点中查找，返回最末一级目录。

    参数：
        list_children: 给定 parent_entry_id，返回子节点列表（含 name/entry_type/id）。
        root_entry_id: 知识库根节点。
        raw_path:      形如 ``01 - 研发项目/06 PED烧靶位置视觉检测引导【负责人：李元伯】``。
    """
    segments = split_path(raw_path)
    if not segments:
        raise PathResolveError("PATH_EMPTY", "目标路径为空。")
    if len(segments) > max_depth:
        raise PathResolveError("PATH_TOO_DEEP", f"路径层级超过上限 {max_depth}：{raw_path}")

    current_id = root_entry_id
    current_name = "（知识库根）"
    walked: list[str] = []

    for index, segment in enumerate(segments, start=1):
        wanted = normalize_name(segment)
        try:
            children = list_children(current_id) or []
        except Exception as error:  # 上游失败要转成可读错误
            raise PathResolveError(
                "LIST_FAILED",
                f"读取层级 {index}（{current_name}）的子节点失败：{str(error)[:120]}",
            ) from None

        matches = [
            item
            for item in children
            if normalize_name(item.get("name")) == wanted
            and str(item.get("entry_type") or "folder") in ("folder", "page")
        ]

        if not matches:
            available = [normalize_name(item.get("name")) for item in children][:8]
            raise PathResolveError(
                "SEGMENT_NOT_FOUND",
                f"第 {index} 层找不到「{segment}」（在「{current_name}」下）。"
                f"该层现有：{('、'.join(available) if available else '空')}",
            )
        if len(matches) > 1:
            ids = [str(item.get("id")) for item in matches]
            raise PathResolveError(
                "SEGMENT_AMBIGUOUS",
                f"第 {index} 层「{segment}」存在 {len(matches)} 个同名节点，无法确定目标：{ids}。"
                "请改用 entry_id 直接指定。",
            )

        node = matches[0]
        current_id = str(node.get("id"))
        current_name = normalize_name(node.get("name"))
        walked.append(current_name)

        if index == len(segments) and str(node.get("entry_type")) != "folder":
            raise PathResolveError(
                "TARGET_NOT_FOLDER",
                f"路径末级「{segment}」不是文件夹（type={node.get('entry_type')}），不能作为写入目标。",
            )

    return ResolvedFolder(entry_id=current_id, name=current_name, depth=len(segments), path="/".join(walked))
