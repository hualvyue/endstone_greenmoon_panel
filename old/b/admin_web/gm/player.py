"""``gm.player`` —— 单个玩家（原生 Player 对象）。

    from gm import player

    p = player.find("Steve")
    if p:
        player.run_on(p, lambda pl: pl.send_message("§a你好"))
        print(p.location)

    # 或者直接用封装好的操作
    player.tell("Steve", "§e欢迎回来")
    player.teleport("Steve", 0, 64, 0)
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from ._context import require_plugin
from ._sched import call

__all__ = [
    "find", "raw", "all", "run_on", "names",
    "tell", "kick", "teleport", "health", "game_mode",
]


def all() -> List[Any]:
    """返回在线玩家的原生 Player 对象列表（主线程）。"""
    plugin = require_plugin()
    return list(call(lambda: list(plugin.server.online_players),
                     timeout=3.0, default=[]) or [])


def names() -> List[str]:
    """返回在线玩家名列表。"""
    return [str(getattr(p, "name", "")) for p in all() if getattr(p, "name", None)]


def find(name: str) -> Optional[Any]:
    """按名字查找在线玩家，返回**原生 Player 对象**；不在线返回 None。"""
    plugin = require_plugin()
    want = str(name or "").strip()
    if not want:
        return None

    def _do() -> Optional[Any]:
        for p in plugin.server.online_players:
            if str(getattr(p, "name", "")) == want:
                return p
        return None

    return call(_do, timeout=3.0, default=None)


#: ``find`` 的别名，语义上强调「原样透传」
raw = find


def run_on(target: Any, fn: Callable[..., Any], *args: Any,
           timeout: float = 5.0, default: Any = None) -> Any:
    """在指定玩家上执行 ``fn(player, *args)``（主线程）。

    ``target`` 可以是 Player 对象或玩家名。
    """
    plugin = require_plugin()

    def _do() -> Any:
        pl = target
        if isinstance(target, str):
            pl = None
            for p in plugin.server.online_players:
                if str(getattr(p, "name", "")) == target:
                    pl = p
                    break
        if pl is None:
            raise ValueError(f"玩家不在线: {target}")
        return fn(pl, *args)

    return call(_do, timeout=timeout, default=default)


def tell(name: str, message: str) -> bool:
    """给玩家发一条私聊消息。"""
    text = str(message or "")
    if not text:
        return False

    def _do(pl: Any) -> bool:
        pl.send_message(text)
        return True

    return bool(run_on(name, _do, default=False))


def kick(name: str, reason: str = "") -> bool:
    """踢出玩家。"""
    msg = str(reason or "")

    def _do(pl: Any) -> bool:
        pl.kick(msg)
        return True

    return bool(run_on(name, _do, default=False))


def teleport(name: str, x: float, y: float, z: float) -> bool:
    """把玩家传送到指定坐标。"""
    from endstone import Location  # 延迟导入：避免插件未加载时 import 失败

    def _do(pl: Any) -> bool:
        world = pl.location.dimension
        pl.teleport(Location(world, float(x), float(y), float(z)))
        return True

    return bool(run_on(name, _do, default=False))


def health(name: str, value: Optional[float] = None) -> Optional[float]:
    """读取（``value=None``）或设置玩家生命值。"""
    def _do(pl: Any) -> float:
        if value is not None:
            pl.health = float(value)
        return float(pl.health)

    return run_on(name, _do, default=None)


def game_mode(name: str) -> Optional[str]:
    """读取玩家的游戏模式。"""
    def _do(pl: Any) -> str:
        return str(pl.game_mode)

    return run_on(name, _do, default=None)
