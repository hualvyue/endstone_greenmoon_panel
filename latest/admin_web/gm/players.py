"""``gm.players`` —— 在线玩家快照（纯读）。

数据来自面板后台每隔一段时间刷新的缓存，**不触碰服务器线程**，
因此在任何线程里调用都安全，也不会卡住调用方。

    from gm import players

    print(players.count())
    for p in players.snapshot():
        print(p["name"], p["x"], p["y"], p["z"], p["dimension"])
    me = players.get("Steve")
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._context import require_grant, require_plugin

__all__ = ["snapshot", "names", "get", "count", "exists", "field"]

#: 与 web.py 的 scope 名保持一致
_SCOPE = "players"


def snapshot() -> List[Dict[str, Any]]:
    """返回全部在线玩家的信息快照（列表，每项是一个 dict）。

    字段：``name`` / ``uuid`` / ``xuid`` / ``x`` / ``y`` / ``z`` / ``dimension`` /
    ``health`` / ``max_health`` / ``ping`` / ``game_mode`` / ``is_op`` / ``tags``
    / ``scores`` / ``permissions`` / ``permission_level``
    """
    require_grant(_SCOPE)
    plugin = require_plugin()
    try:
        cache = plugin._get_cache()
    except Exception:
        return []
    items = cache.get("players") or []
    return [dict(x) for x in items if isinstance(x, dict)]


def names() -> List[str]:
    """返回在线玩家名列表。"""
    return [str(p.get("name")) for p in snapshot() if p.get("name")]


def get(name: str) -> Optional[Dict[str, Any]]:
    """按名字取单个玩家的快照；不在线返回 None。"""
    require_grant(_SCOPE)
    plugin = require_plugin()
    want = str(name or "").strip()
    if not want:
        return None
    try:
        got = plugin._get_cached_player(want)
    except Exception:
        return None
    return dict(got) if isinstance(got, dict) else None


def count() -> int:
    """在线玩家数量。"""
    return len(snapshot())


def exists(name: str) -> bool:
    """玩家是否在线。"""
    return get(name) is not None


def field(name: str, key: str, default: Any = None) -> Any:
    """取某个玩家的某个字段。"""
    rec = get(name)
    if not rec:
        return default
    return rec.get(key, default)
