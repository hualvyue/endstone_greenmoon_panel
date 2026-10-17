"""``gm.event`` —— 子插件注册游戏事件。

用法：

    from gm import event
    from endstone.event import PlayerChatEvent, PlayerJoinEvent

    @event.on(PlayerChatEvent)
    def on_chat(ev):
        if "spawn" in ev.message:
            ev.player.send_message("§e坐标：0 64 0")

    @event.on(PlayerJoinEvent)
    def on_join(ev):
        ev.player.send_message("§a欢迎回来！")

    event.off(PlayerChatEvent, on_chat)     # 取消注册

说明
----
Endstone 的事件注册是 **Plugin 级、面向插件自己方法**的，子插件模块无法直接注册。
因此这里由主插件预先挂好各事件类型的转发方法，再按类型分发给子插件注册的函数。

同一子插件停用/卸载时，它注册的所有 handler 会被自动清理，不会残留。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ._context import current_owner, require_plugin

__all__ = ["on", "off", "clear", "handlers", "supported"]


def _map(plugin: Any) -> Dict[Any, List[Callable[..., Any]]]:
    """取（必要时创建）主插件上的事件分发表。"""
    got = getattr(plugin, "_gm_event_map", None)
    if got is None:
        got = {}
        try:
            plugin._gm_event_map = got
        except Exception:
            pass
    return got


def on(event_cls: Any, handler: Optional[Callable[..., Any]] = None) -> Callable[..., Any]:
    """注册一个游戏事件处理函数。

    两种写法都支持：

        @event.on(PlayerChatEvent)          # 装饰器
        def on_chat(ev): ...

        event.on(PlayerChatEvent, on_chat)  # 普通调用

    返回该 handler 本身；装饰器形式下返回一个"补上 handler 再注册"的包装。
    """
    if handler is None:
        # 装饰器形式：先拿到事件类，等函数定义好再回调注册
        def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            on(event_cls, fn)
            return fn

        return _decorator

    plugin = require_plugin()
    table = _map(plugin)
    lst = table.setdefault(event_cls, [])
    if handler not in lst:
        lst.append(handler)

    # 记录归属，便于子插件停用时清理
    owner = current_owner()
    if owner:
        try:
            handles = getattr(plugin, "_gm_event_owner", None)
            if handles is None:
                handles = {}
                plugin._gm_event_owner = handles
            handles.setdefault(owner, []).append((event_cls, handler))
        except Exception:
            pass
    return handler


def off(event_cls: Any, handler: Callable[..., Any]) -> bool:
    """取消注册。返回是否确实移除了一项。"""
    plugin = require_plugin()
    table = _map(plugin)
    lst = table.get(event_cls)
    removed = False
    if lst and handler in lst:
        lst.remove(handler)
        removed = True
        if not lst:
            table.pop(event_cls, None)

    owner = current_owner()
    if owner:
        try:
            handles = getattr(plugin, "_gm_event_owner", None) or {}
            items = handles.get(owner) or []
            handles[owner] = [x for x in items if x != (event_cls, handler)]
        except Exception:
            pass
    return removed


def clear() -> int:
    """清空**当前子插件**注册的全部 handler，返回清理数量。"""
    plugin = require_plugin()
    owner = current_owner()
    if not owner:
        return 0
    return _drop_owner(plugin, owner)


def _drop_owner(plugin: Any, owner: str) -> int:
    """按 uuid 摘除该子插件注册的全部 handler（供 ModLoader 停用时调用）。"""
    handles = getattr(plugin, "_gm_event_owner", None)
    if not isinstance(handles, dict):
        return 0
    items = handles.pop(owner, None)
    if not items:
        return 0
    table = _map(plugin)
    count = 0
    for event_cls, handler in items:
        lst = table.get(event_cls)
        if lst and handler in lst:
            try:
                lst.remove(handler)
                count += 1
            except ValueError:
                pass
            if not lst:
                table.pop(event_cls, None)
    return count


def handlers(event_cls: Any = None) -> Any:
    """查看已注册的 handler。

    传 ``event_cls`` 返回该类型的函数列表；不传返回整张表。
    """
    plugin = require_plugin()
    table = _map(plugin)
    if event_cls is None:
        return {k: list(v) for k, v in table.items()}
    return list(table.get(event_cls) or [])


def supported() -> List[str]:
    """返回主插件已挂好转发的事件类型名列表。"""
    plugin = require_plugin()
    names = getattr(plugin, "_gm_event_supported", None)
    if isinstance(names, (list, tuple)):
        return [str(x) for x in names]
    return []
