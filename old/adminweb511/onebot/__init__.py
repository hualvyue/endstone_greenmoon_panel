"""GreenMoon OneBot v11 适配器子系统（多机器人网关）。

由 LumenBridge 的 onebot 模块移植而来，仅保留 WebSocket(OneBot v11)
双模式适配器与报文 / 消息段构建器，移除 i18n / vendor 依赖。
"""

from __future__ import annotations

from .adapter import OneBotAdapter
from . import packets
from . import message

__all__ = ["OneBotAdapter", "packets", "message"]