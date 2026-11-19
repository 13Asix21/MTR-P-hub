"""FastAPI 请求模型与统一响应包裹的共享基座。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


USER_ID_PATTERN = r"^[a-z0-9-]{1,32}$"


class StrictRequest(BaseModel):
    """所有请求默认拒绝未声明字段，且不自动修剪字符串。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


def ok(data: Any) -> dict[str, Any]:
    """返回面板统一成功响应。"""

    return {"status": "ok", "data": data}


def error(code: str, message: str) -> dict[str, Any]:
    """返回面板统一封闭错误响应。"""

    return {"status": "error", "error": {"code": code, "message": message}}


__all__ = ["StrictRequest", "USER_ID_PATTERN", "error", "ok"]
