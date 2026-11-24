"""按项目惯例为写入文件生成序号前缀（B1 命名规则）。

部门惯例（实测自「01 - 研发项目」下各文件夹）：

    AB-01-xxx.md        两段字母前缀 + 序号
    EPL-04-xxx.md
    01-xxx.md           纯序号
    xxx.md              无序号

生成规则：
- 目录里**已有**同前缀文件 → 沿用同前缀，序号取最大值 +1（两位补零）。
- 目录为空 → 用用户配置的前缀（如 ``PED``），序号从 ``01`` 起。
- 文件名本身已带 ``前缀-序号-`` 或 ``序号-`` 开头 → 视为已编号，原样保留，避免重复加。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# AB-01-xxx / EPL-04-xxx / 01-xxx
_PREFIXED = re.compile(r"^(?P<prefix>[A-Za-z]{2,8})-(?P<seq>\d{2,3})-(?P<rest>.+)$")
_BARE_SEQ = re.compile(r"^(?P<seq>\d{2,3})-(?P<rest>.+)$")


@dataclass(frozen=True)
class NamePlan:
    """一次命名决策的结果，带可解释的依据。"""

    name: str
    prefix: str
    seq: int
    reason: str

    def summary(self) -> dict[str, object]:
        return {"name": self.name, "prefix": self.prefix, "seq": self.seq, "reason": self.reason}


def parse_existing(name: str) -> tuple[str, int, str] | None:
    """解析已有文件名的前缀、序号与正文部分；不符合惯例返回 None。"""
    stem = re.sub(r"\.md$", "", str(name or "").strip(), flags=re.IGNORECASE)
    match = _PREFIXED.match(stem)
    if match:
        return match.group("prefix").upper(), int(match.group("seq")), match.group("rest")
    match = _BARE_SEQ.match(stem)
    if match:
        return "", int(match.group("seq")), match.group("rest")
    return None


def _strip_own_number(stem: str) -> str:
    """去掉标题里可能已存在的编号，避免生成 ``PED-01-01-xxx``。"""
    match = _PREFIXED.match(stem)
    if match and match.group("rest"):
        return match.group("rest").strip()
    match = _BARE_SEQ.match(stem)
    if match and match.group("rest"):
        return match.group("rest").strip()
    return stem.strip()


def plan_indexed_name(
    title: str,
    existing_names: list[str],
    *,
    default_prefix: str = "",
    width: int = 2,
) -> NamePlan:
    """根据同目录已有文件推导下一个序号文件名。

    参数：
        title:          期望的文件名（通常来自源笔记标题）。
        existing_names: 目标目录下现有条目名。
        default_prefix: 目录为空时使用的字母前缀（可空，表示只用序号）。
        width:          序号补零位数，默认 2。
    """
    stem = re.sub(r"\.md$", "", str(title or "").strip(), flags=re.IGNORECASE)
    if not stem:
        stem = "未命名笔记"

    already = parse_existing(stem)
    if already:
        prefix, seq, _rest = already
        # 带字母前缀的标题视为已按惯例编号，原样保留；
        # 纯序号标题只在目录本身也是纯序号体系时才保留，否则改跟目录惯例。
        bare_dir = not any(
            (parse_existing(n) or ("", 0, ""))[0]
            for n in (existing_names or [])
        )
        if prefix or bare_dir:
            return NamePlan(
                name=f"{stem}.md",
                prefix=prefix,
                seq=seq,
                reason="标题自带编号，保持原样",
            )

    body = _strip_own_number(stem)

    # 从同目录已有文件中推导前缀与最大序号。
    prefix_counts: dict[str, int] = {}
    max_seq_by_prefix: dict[str, int] = {}
    for name in existing_names or []:
        parsed = parse_existing(name)
        if not parsed:
            continue
        prefix, seq, _rest = parsed
        prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
        max_seq_by_prefix[prefix] = max(max_seq_by_prefix.get(prefix, 0), seq)

    if prefix_counts:
        # 取出现次数最多的前缀，其次取序号最大的前缀，保证稳定。
        prefix = max(prefix_counts.items(), key=lambda kv: (kv[1], max_seq_by_prefix.get(kv[0], 0)))[0]
        next_seq = max_seq_by_prefix.get(prefix, 0) + 1
        reason = f"沿用同目录前缀「{prefix or '（纯序号）'}」，序号续 {next_seq}"
    else:
        prefix = re.sub(r"[^A-Za-z]", "", str(default_prefix or "")).upper()[:8]
        next_seq = 1
        reason = f"目录为空，使用默认前缀「{prefix or '（纯序号）'}」，从 1 起编号"

    number = str(next_seq).zfill(width)
    name = f"{prefix}-{number}-{body}.md" if prefix else f"{number}-{body}.md"
    return NamePlan(name=name, prefix=prefix, seq=next_seq, reason=reason)
