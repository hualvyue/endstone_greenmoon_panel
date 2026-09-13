"""``gm.admin`` —— 面板状态直读。

子插件可以直接读取面板的各项状态数据，无需经过 HTTP。

    from gm import admin

    st = admin.system_stats()        # CPU / 内存 / 磁盘 / 运行时长
    print(st["cpu"], st["uptime"])

    for p in admin.players_snapshot():
        print(p["name"], p["dimension"])

    for m in admin.messages(limit=20):
        print(m["time"], m["player"], m["message"])

    admin.state()                    # 一次性拿综合状态

权限：玩家数据需 ``"players"``，消息需 ``"logs"``，控制台需 ``"console"``；
``state()`` 会按其中已授权的部分尽力拼装。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ._context import granted, require_grant, require_plugin

__all__ = [
    "system_stats", "players_snapshot", "online_names", "online_count",
    "messages", "state", "cache",
]


def system_stats() -> Dict[str, Any]:
    """系统指标：``cpu`` / ``mem`` / ``mem_total_mb`` / ``mem_used_mb`` /
    ``disk`` / ``disk_total_gb`` / ``disk_used_gb`` / ``uptime``。

    纯读，任何线程可调。需 ``"players"`` 权限（该权限即「读面板状态」的基础权限）。
    """
    require_grant("players")
    plugin = require_plugin()
    try:
        got = plugin.get_system_stats()
    except Exception:
        return {}
    return dict(got) if isinstance(got, dict) else {}


def cache() -> Dict[str, Any]:
    """面板缓存的原始快照（含 players / objectives / bans 等）。纯读。"""
    require_grant("players")
    plugin = require_plugin()
    try:
        got = plugin._get_cache()
    except Exception:
        return {}
    return dict(got) if isinstance(got, dict) else {}


def players_snapshot() -> List[Dict[str, Any]]:
    """在线玩家快照列表（字段同 ``gm.players.snapshot()``）。"""
    require_grant("players")
    items = cache().get("players") or []
    return [dict(x) for x in items if isinstance(x, dict)]


def online_names() -> List[str]:
    """在线玩家名列表。"""
    return [str(p.get("name")) for p in players_snapshot() if p.get("name")]


def online_count() -> int:
    """在线玩家数量。"""
    return len(players_snapshot())


def messages(limit: int = 50, since_id: int = 0) -> List[Dict[str, Any]]:
    """局内聊天 / 系统消息。

    每项 ``{"id": int, "time": "HH:MM:SS", "type": str, "player": str,
    "message": str}``。
    """
    require_grant("logs")
    plugin = require_plugin()
    buf = getattr(plugin, "_messages", None)
    if buf is None:
        return []
    lock = getattr(plugin, "_messages_lock", None)
    try:
        if lock is not None:
            with lock:
                items = list(buf)
        else:
            items = list(buf)
    except Exception:
        return []

    after = int(since_id or 0)
    if after > 0:
        items = [m for m in items if int(m.get("id", 0) or 0) > after]
    if limit and limit > 0:
        items = items[-int(limit):]
    return [dict(m) for m in items if isinstance(m, dict)]


def state() -> Dict[str, Any]:
    """面板综合状态。

    会尽力拼装已授权的部分：未授权的板块在结果里标 ``"__denied__"``，
    而不是直接抛异常，方便子插件做「有就显示」的容错处理。
    """
    out: Dict[str, Any] = {
        "system": None,
        "online_count": None,
        "players": None,
        "bots": None,
        "cross": None,
    }

    if granted("players"):
        out["system"] = system_stats()
        try:
            out["players"] = online_names()
            out["online_count"] = len(out["players"])
        except Exception:
            out["players"] = None
    else:
        out["system"] = "__denied__"
        out["players"] = "__denied__"

    try:
        plugin = require_plugin()
        hub = getattr(plugin, "qq_hub", None)
        if hub is not None:
            if granted("bots"):
                out["bots"] = [dict(x) for x in hub.status() if isinstance(x, dict)]
            else:
                out["bots"] = "__denied__"
    except Exception:
        out["bots"] = None

    if granted("cross"):
        try:
            from . import cross as _cross
            out["cross"] = _cross.status()
        except Exception:
            out["cross"] = None

    return out
