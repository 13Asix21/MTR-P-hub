"""M 库配置有效性与 WorkBuddy 历史执行证据校验。"""

from __future__ import annotations

import re
from typing import Any

from .audit_reader import AuditReader
from .settings import LexiangConfig, PanelError


_HEX_32 = re.compile(r"^[0-9a-fA-F]{32}$")


def _masked_identifier(value: str) -> str:
    if not value:
        return "未配置"
    if len(value) < 12:
        return "已配置（格式待校验）"
    return f"{value[:8]}…{value[-4:]}"


def check_lexiang(config: LexiangConfig, audit_reader: AuditReader) -> dict[str, Any]:
    """如实返回配置检查和间接证据；不尝试调用会话级 MCP。"""

    issues: list[dict[str, str]] = []
    identifiers = {
        "team_id": config.team_id,
        "space_id": config.space_id,
        "root_entry_id": config.root_entry_id,
    }
    configured_count = sum(bool(value) for value in identifiers.values())
    for field, value in identifiers.items():
        if not value:
            issues.append({"field": field, "code": "ID_MISSING", "message": f"{field} 未配置。"})
        elif not _HEX_32.fullmatch(value):
            issues.append({"field": field, "code": "ID_FORMAT_INVALID", "message": f"{field} 不是 32 位十六进制 ID。"})
    if config.mode != "workbuddy-manifest":
        issues.append({"field": "mode", "code": "MODE_INVALID", "message": "M 库模式必须为 workbuddy-manifest。"})
    if not config.read_only:
        issues.append({"field": "read_only", "code": "READ_ONLY_REQUIRED", "message": "M 库配置必须显式保持 read_only=true。"})

    if configured_count == 0 and not config.mode:
        status = "unconfigured"
    else:
        status = "limited" if not issues else "error"

    try:
        audit: dict[str, Any] = audit_reader.summary()
    except PanelError as exc:
        audit = {
            "available": False,
            "latest_by_store": {},
            "latest_run": None,
            "m_event_count": 0,
            "error": {"code": exc.code, "message": exc.message},
        }

    m_evidence = audit["latest_by_store"].get("M")
    return {
        "status": status,
        "mode": config.mode or "未配置",
        "read_only": config.read_only,
        "config_valid": not issues,
        "issues": issues,
        "identifiers": {
            field: {"configured": bool(value), "valid": bool(_HEX_32.fullmatch(value)), "display": _masked_identifier(value)}
            for field, value in identifiers.items()
        },
        "realtime_mcp_available": False,
        "evidence_scope": "WorkBuddy 会话历史审计证据",
        "latest_m_evidence": m_evidence,
        "audit": audit,
        "explanation": "乐享连接器属于 WorkBuddy 会话级 MCP，独立后端不能实时调用；此处仅校验配置并只读展示最近执行证据。实时在线验证需在 WorkBuddy 会话中完成。",
    }


__all__ = ["check_lexiang"]
