"""``gm.server`` —— Endstone 服务器对象。

原生对象全量透传，同时提供主线程包装的常用操作。

    from gm import server

    server.broadcast("§e服务器即将重启")
    srv = server.get()                 # 原生 endstone Server 对象
    print(srv.version)
    server.dispatch("say hello")       # 派发服务器指令（自动切主线程）
    for p in server.online_players():  # 原生 Player 对象列表
        print(p.name)
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from ._context import require_plugin
from ._sched import call

__all__ = [
    "get", "raw", "run", "broadcast", "dispatch",
    "online_players", "player_list", "player_count",
]


def get() -> Any:
    """返回原生 Endstone ``Server`` 对象（全量透传）。

    纯读操作，只取属性引用，不触碰服务器内部状态。
    拿到对象后自行调用其方法时，线程安全由调用方负责；
    需要跨线程安全操作请改用 ``run()`` 或本模块的包装函数。
    """
    return require_plugin().server


#: ``get`` 的别名，语义上强调「原样透传」
raw = get


def run(fn: Callable[..., Any], *args: Any, timeout: float = 5.0,
        default: Any = None, **kwargs: Any) -> Any:
    """在主线程执行 ``fn(*args, **kwargs)`` 并返回结果。

    凡是会读写服务器状态的操作都应走这里。失败返回 ``default``。
    """
    return call(fn, *args, timeout=timeout, default=default, **kwargs)


def broadcast(message: str) -> bool:
    """向全服广播一条系统消息。返回是否发送成功。"""
    plugin = require_plugin()
    text = str(message or "")
    if not text:
        return False
    return bool(call(plugin._cross_broadcast, text, timeout=3.0, default=False))


def dispatch(command: str, sender: Any = None) -> bool:
    """以控制台身份派发一条服务器指令。

    例：``server.dispatch("give Steve diamond 1")``
    """
    plugin = require_plugin()
    cmd = str(command or "").strip()
    if not cmd:
        return False

    def _do() -> bool:
        srv = plugin.server
        operator = sender if sender is not None else srv.command_sender
        srv.dispatch_command(operator, cmd)
        return True

    return bool(call(_do, timeout=3.0, default=False))


def online_players() -> List[Any]:
    """返回在线玩家的**原生 Player 对象**列表。

    需要在主线程取，失败返回空列表。
    """
    plugin = require_plugin()
    return list(call(lambda: list(plugin.server.online_players),
                     timeout=3.0, default=[]) or [])


def player_list() -> List[str]:
    """返回在线玩家名列表。"""
    out: List[str] = []
    for p in online_players():
        name = getattr(p, "name", None)
        if name:
            out.append(str(name))
    return out


def player_count() -> int:
    """返回在线玩家数量。"""
    return len(online_players())
