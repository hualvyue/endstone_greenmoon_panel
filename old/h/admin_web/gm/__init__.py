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
``mapart``     把图片画到地图上（缩放 / 编码 / 发放）
``config``     读写**自己包内**的 config.json（与面板「设置」页共用同一份）
``trust``      玩家可信度（加权安全系统）+「信任的人」
``whitelist``  服务器白名单读写
``ban``        服务器封禁列表读写
``mods``       子插件管理（清点 / 启停 / 安装 / 卸载）
``ai``         面板 AI 能力（探测 / 问答 / 注册工具 / 接管级别）

权限
----
涉及面板数据的模块需要在子插件的 ``manifest.json`` 里声明
``permissions``，例如：

    { "type": "py", "permissions": ["players", "logs", "bots", "bindings", "cross", "config"] }

写 ``"*"`` 表示全部。不声明则默认空集，只能使用游戏侧能力（server / player / event）。

⚠ 高危权限（申请前请想清楚）
    * ``trust``     —— 改任意玩家的可信度（含「信任的人」＝永久满分免疫扣分）
    * ``whitelist`` —— 决定谁能进服
    * ``bans``      —— 决定谁进不来
    * ``mods``      —— 启停/安装/卸载其它子插件 ⇒ 事实上的**任意代码执行**
    * ``ai``        —— 使用面板 AI（消耗服主额度）+ 注册工具给 AI 调用
    * ``ai:control``      —— 改 AI 接管级别（等于决定"AI 能不能自己动手删档"）
    * ``ai:irreversible`` —— 注册不可逆工具（影响面与内置的删档工具同级）
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
from . import (
    admin,
    ai,
    ban,
    bindings,
    bot,
    config,
    console,
    cross,
    event,
    mapart,
    mods,
    player,
    players,
    server,
    trust,
    whitelist,
)

__all__ = [
    # 子模块
    "server", "player", "players", "bot", "bindings", "cross",
    "admin", "console", "event", "mapart", "config",
    "trust", "whitelist", "ban", "mods", "ai",
    # 上下文与异常
    "attach", "detach", "get_plugin", "require_plugin", "current_owner",
    "require_grant", "granted", "api_version",
    "GmError", "GmNotReady", "GmPermissionError",
]

__version__ = api_version()
