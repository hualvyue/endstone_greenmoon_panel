"""``gm.cross`` —— 跨服玩家数据。

    from gm import cross

    if cross.enabled():
        print(cross.role())              # "master" / "slave" / ""
        data = cross.get_player(uuid)    # 玩家背包/末影箱/效果等快照
        cross.put_player(uuid, "Steve", data)

权限：需在 manifest 声明 ``"cross"``。

⚠ **阻塞提醒**：``get_player`` 在 slave（从服）模式下会走一次同步 HTTP 请求到主服，
可能阻塞数秒。不要在服务器主线程里调用它；子插件里请自建线程，
或改用 ``get_player_async()``。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from ._context import require_grant, require_plugin

__all__ = [
    "enabled", "role", "is_master", "is_slave", "interop", "config",
    "get_player", "get_player_async", "put_player", "server_name", "status",
]

_SCOPE = "cross"


def _mgr():
    plugin = require_plugin()
    mgr = getattr(plugin, "cross", None)
    if mgr is None:
        raise RuntimeError("跨服模块未初始化")
    return mgr


def enabled() -> bool:
    """跨服同步是否已启用。"""
    require_grant(_SCOPE)
    try:
        return bool(_mgr().enabled())
    except Exception:
        return False


def role() -> str:
    """返回 ``"master"`` / ``"slave"`` / ``""``。"""
    require_grant(_SCOPE)
    try:
        return str(_mgr().role() or "")
    except Exception:
        return ""


def is_master() -> bool:
    """本服是否为主服。"""
    return role() == "master"


def is_slave() -> bool:
    """本服是否为从服。"""
    return role() == "slave"


def interop(key: str) -> bool:
    """某项互通能力是否开启（如 ``player_data`` / ``whitelist`` / ``ban``）。"""
    require_grant(_SCOPE)
    try:
        return bool(_mgr().interop(str(key)))
    except Exception:
        return False


def config() -> Dict[str, Any]:
    """跨服配置快照（纯读）。"""
    require_grant(_SCOPE)
    try:
        cfg = getattr(_mgr(), "cfg", None)
        return dict(cfg) if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def server_name() -> str:
    """本服在跨服网络里的名字。"""
    return str(config().get("server_name") or "")


def status() -> Dict[str, Any]:
    """跨服综合状态：角色、启用与否、各项互通开关、服务器名。"""
    return {
        "enabled": enabled(),
        "role": role(),
        "server_name": server_name(),
        "player_data": interop("player_data"),
        "whitelist": interop("whitelist"),
        "ban": interop("ban"),
    }


def get_player(uuid: str) -> Optional[Dict[str, Any]]:
    """取某个玩家的跨服数据快照（背包 / 末影箱 / 效果）。

    ⚠ slave 模式下是同步 HTTP，可能阻塞。见模块文档。
    """
    require_grant(_SCOPE)
    want = str(uuid or "").strip()
    if not want:
        return None
    try:
        got = _mgr().get_player_data(want)
    except Exception:
        return None
    return got if isinstance(got, dict) else None


def get_player_async(uuid: str,
                     callback: Optional[Callable[[Optional[Dict[str, Any]]], None]] = None) -> threading.Thread:
    """``get_player`` 的异步版本，在后台线程拉取，不阻塞调用方。

    ``callback`` 收到结果或 None 后在同一条后台线程里被调用。
    """
    require_grant(_SCOPE)

    def _work() -> None:
        result: Optional[Dict[str, Any]] = None
        try:
            result = get_player(uuid)
        except Exception:
            result = None
        if callback is not None:
            try:
                callback(result)
            except Exception:
                pass

    t = threading.Thread(target=_work, name="gm-cross-get", daemon=True)
    t.start()
    return t


def put_player(uuid: str, name: str, data: Dict[str, Any]) -> bool:
    """写入某个玩家的跨服数据（异步上报，立即返回）。"""
    require_grant(_SCOPE)
    want = str(uuid or "").strip()
    if not want or not isinstance(data, dict):
        return False
    try:
        _mgr().put_player_data(want, str(name or ""), data)
        return True
    except Exception:
        return False
