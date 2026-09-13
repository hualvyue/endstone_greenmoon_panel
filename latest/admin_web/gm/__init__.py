"""GreenMoon 子插件 API（``gm``）。

子插件里直接这样用：

    from gm import server, player, players, bot, bindings, cross, admin, event

    def setup(plugin=None, gm=None):
        server.broadcast("§a子插件已启动")
        for p in players.snapshot():
            print(p["name"])

    @event.on(PlayerChatEvent)
    def _on_chat(ev):
        ...

模块清单
--------
``server``     Endstone Server —— 原生透传 + 主线程包装
``player``     单个玩家（原生 Player 对象）
``players``    在线玩家快照（纯读，不碰服务器线程）
``bot``        机器人（多卡片维度）：自身信息、群聊信息、发消息
``bindings``   玩家 QQ 账户绑定数据
``cross``      跨服玩家数据
``admin``      面板状态直读（系统指标 / 玩家 / 消息）
``console``    控制台日志与控制台指令
``event``      游戏事件注册

权限
----
涉及面板数据的模块（``bot`` / ``bindings`` / ``cross`` / ``admin`` / ``console``）
需要在子插件的 ``manifest.json`` 里声明 ``permissions``，例如：

    { "type": "py", "permissions": ["players", "logs", "bots", "bindings", "cross"] }

写 ``"*"`` 表示全部。不声明则默认空集，只能使用游戏侧能力（server / player / event）。
"""

from __future__ import annotations

from ._context import (
    MOD_PREFIX,
    api_version,
    attach,
    current_owner,
    detach,
    get_plugin,
    granted,
    require_grant,
    require_plugin,
)
from ._errors import GmError, GmNotReady, GmPermissionError

# 子模块必须在 context 之后导入：它们的模块级代码会用 require_plugin 等
from . import admin, bindings, bot, console, cross, event, player, players, server

__all__ = [
    # 子模块
    "server", "player", "players", "bot", "bindings", "cross",
    "admin", "console", "event",
    # 上下文与异常
    "attach", "detach", "get_plugin", "require_plugin", "current_owner",
    "require_grant", "granted", "api_version",
    "GmError", "GmNotReady", "GmPermissionError",
]

__version__ = api_version()
