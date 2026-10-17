"""``gm.console`` —— 控制台日志与控制台指令。

    from gm import console

    for line in console.logs(limit=50):
        print(line["time"], line["level"], line["message"])

    console.run("say hello")        # 以控制台身份执行指令

权限：需在 manifest 声明 ``"console"``。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ._context import require_grant, require_plugin
from ._sched import call

__all__ = ["logs", "run", "command", "tail", "clear"]

_SCOPE = "console"


def logs(limit: int = 200, since_id: int = 0) -> List[Dict[str, Any]]:
    """返回控制台日志。

    每项 ``{"id": int, "time": "HH:MM:SS", "level": "INFO", "message": str}``。
    ``since_id`` 用于增量拉取（只返回 id 大于该值的条目）。
    """
    require_grant(_SCOPE)
    plugin = require_plugin()
    deque_ = getattr(plugin, "_console_logs", None)
    if deque_ is None:
        return []
    lock = getattr(plugin, "_console_lock", None)
    try:
        if lock is not None:
            with lock:
                items = list(deque_)
        else:
            items = list(deque_)
    except Exception:
        return []

    after = int(since_id or 0)
    if after > 0:
        items = [m for m in items if int(m.get("id", 0) or 0) > after]
    if limit and limit > 0:
        items = items[-int(limit):]
    return [dict(m) for m in items if isinstance(m, dict)]


def tail(count: int = 20) -> List[Dict[str, Any]]:
    """取最近 ``count`` 条控制台日志。"""
    return logs(limit=count)


def run(command: str) -> bool:
    """以控制台身份执行一条服务器指令（主线程）。"""
    require_grant(_SCOPE)
    plugin = require_plugin()
    cmd = str(command or "").strip()
    if not cmd:
        return False

    def _do() -> bool:
        srv = plugin.server
        srv.dispatch_command(srv.command_sender, cmd)
        return True

    return bool(call(_do, timeout=3.0, default=False))


#: ``run`` 的别名
command = run


def clear() -> None:
    """清空控制台日志缓冲。"""
    require_grant(_SCOPE)
    plugin = require_plugin()
    deque_ = getattr(plugin, "_console_logs", None)
    lock = getattr(plugin, "_console_lock", None)
    if deque_ is None:
        return
    try:
        if lock is not None:
            with lock:
                deque_.clear()
        else:
            deque_.clear()
    except Exception:
        pass
