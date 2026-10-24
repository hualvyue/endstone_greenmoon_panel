"""GreenMoon 子插件 API 异常类型。"""

from __future__ import annotations

__all__ = ["GmError", "GmPermissionError", "GmNotReady"]


class GmError(RuntimeError):
    """gm API 调用失败的通用异常。"""


class GmNotReady(GmError):
    """面板尚未就绪（gm 未挂载），通常在插件 on_enable 之前调用触发。"""


class GmPermissionError(GmError):
    """子插件未在 manifest 里声明对应权限。"""
