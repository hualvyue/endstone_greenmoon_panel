"""``gm.bot`` —— 机器人 API（多卡片维度）。

面板支持多张「机器人卡片」独立启停，本模块按 ``adapter_id`` 定位到具体卡片；
不传 id 时默认取主官方机器人。

    from gm import bot

    # 机器人自身信息
    bot.info()                      # 主机器人
    bot.info("card-3")              # 指定卡片

    # 群聊信息
    bot.groups()                    # 主机器人的群列表
    bot.group("card-3", "A1B2C3")   # 某个群的详情

    # 所有卡片
    for card in bot.list():
        print(card["id"], card["name"], bot.status(card["id"]))

    # 发消息
    bot.send_text("card-3", "§a来自子插件的消息")
    bot.send_group("card-3", "群openid", "§e大家好")

权限：需在 manifest 声明 ``"bots"``。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from ._context import require_grant, require_plugin

__all__ = [
    "list", "get", "status", "info", "gateway", "primary_id", "primary",
    "groups", "group", "send_text", "send_group", "send_group_async",
    "is_running", "is_connected",
]

_SCOPE = "bots"


# ------------------------------------------------------------- 卡片与状态

def list(include_secrets: bool = False) -> List[Dict[str, Any]]:
    """返回全部机器人卡片。

    ``include_secrets=False``（默认）时密钥字段会被掩码，适合展示。
    """
    require_grant(_SCOPE)
    plugin = require_plugin()
    mgr = getattr(plugin, "conn_mgr", None)
    if mgr is None:
        return []
    try:
        return [dict(x) for x in mgr.snapshot(mask=not include_secrets)
                if isinstance(x, dict)]
    except Exception:
        return []


def get(adapter_id: str) -> Optional[Dict[str, Any]]:
    """取单张卡片的完整配置视图。"""
    require_grant(_SCOPE)
    plugin = require_plugin()
    mgr = getattr(plugin, "conn_mgr", None)
    if mgr is None:
        return None
    try:
        got = mgr.get_view(str(adapter_id))
    except Exception:
        return None
    return dict(got) if isinstance(got, dict) else None


def status(adapter_id: Optional[str] = None) -> Any:
    """运行状态。

    ``adapter_id`` 为空时返回**全部卡片**的状态列表；
    指定时返回该卡片的 dict，找不到返回 None。

    每项字段：``id`` / ``type`` / ``name`` / ``enabled`` / ``configured``
    / ``running`` / ``connected``
    """
    require_grant(_SCOPE)
    plugin = require_plugin()
    hub = getattr(plugin, "qq_hub", None)
    if hub is None:
        return [] if adapter_id is None else None
    try:
        items = [dict(x) for x in hub.status() if isinstance(x, dict)]
    except Exception:
        items = []
    if adapter_id is None:
        return items
    want = str(adapter_id)
    for it in items:
        if str(it.get("id")) == want:
            return it
    return None


def primary_id() -> Optional[str]:
    """主官方机器人的卡片 id；没有则返回 None。"""
    require_grant(_SCOPE)
    plugin = require_plugin()
    mgr = getattr(plugin, "conn_mgr", None)
    if mgr is None:
        return None
    try:
        card = mgr.primary_qqofficial()
    except Exception:
        return None
    if not card:
        return None
    got = card.get("id")
    return str(got) if got else None


def primary() -> Optional[Dict[str, Any]]:
    """主官方机器人的卡片信息。"""
    gid = primary_id()
    return get(gid) if gid else None


def _resolve_id(adapter_id: Optional[str]) -> Optional[str]:
    """把可选的 adapter_id 解析成具体 id（空则用主机器人）。"""
    if adapter_id:
        return str(adapter_id)
    return primary_id()


def info(adapter_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """机器人自身信息。

    返回 ``{id, name, type, enabled, configured, running, connected,
    app_id, groups_count, is_primary}``；找不到卡片返回 None。
    """
    require_grant(_SCOPE)
    gid = _resolve_id(adapter_id)
    if not gid:
        return None

    card = get(gid) or {}
    st = status(gid) or {}
    plugin = require_plugin()

    app_id = ""
    groups_count = 0
    try:
        mgr = getattr(plugin, "conn_mgr", None)
        if mgr is not None:
            flat = mgr.to_flat_config(card) if card else {}
            app_id = str(flat.get("app_id", "") or "")
    except Exception:
        pass
    groups_count = len(groups(gid))

    return {
        "id": gid,
        "name": str(st.get("name") or card.get("name") or ""),
        "type": str(st.get("type") or card.get("type") or ""),
        "enabled": bool(st.get("enabled", card.get("enabled", False))),
        "configured": bool(st.get("configured", False)),
        "running": bool(st.get("running", False)),
        "connected": bool(st.get("connected", False)),
        "app_id": app_id,
        "groups_count": groups_count,
        "is_primary": gid == (primary_id() or ""),
    }


def gateway(adapter_id: Optional[str] = None) -> Optional[Any]:
    """返回**原生网关对象**（透传）。

    qqofficial 卡片是 ``QQBotGateway``，websocket 卡片是 ``OneBotAdapter``。
    用于访问本模块未封装的底层能力；自行调用时请留意线程安全。
    """
    require_grant(_SCOPE)
    plugin = require_plugin()
    hub = getattr(plugin, "qq_hub", None)
    if hub is None:
        return None
    try:
        if adapter_id:
            return hub.gateway_by_id(str(adapter_id))
        return hub.primary_gateway()
    except Exception:
        return None


def is_running(adapter_id: Optional[str] = None) -> bool:
    """机器人是否已启动（网关进程/线程在跑）。"""
    st = status(_resolve_id(adapter_id))
    return bool(st and st.get("running"))


def is_connected(adapter_id: Optional[str] = None) -> bool:
    """机器人是否已连上平台（WebSocket 已建立）。"""
    st = status(_resolve_id(adapter_id))
    return bool(st and st.get("connected"))


# ------------------------------------------------------------------- 群聊

def groups(adapter_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """返回某个机器人已绑定的群列表。

    每项 ``{openid, name, is_primary}``。数据来自卡片配置，纯读。
    """
    require_grant(_SCOPE)
    plugin = require_plugin()
    mgr = getattr(plugin, "conn_mgr", None)
    if mgr is None:
        return []

    if adapter_id:
        card = get(str(adapter_id))
    else:
        try:
            card = mgr.primary_qqofficial()
        except Exception:
            card = None
    if not card:
        return []

    try:
        flat = mgr.to_flat_config(card)
    except Exception:
        return []

    primary = str(flat.get("group_openid", "") or "").strip()
    items = flat.get("groups") or []
    if not isinstance(items, list):
        items = []

    out: List[Dict[str, Any]] = []
    seen = set()
    if primary:
        out.append({"openid": primary, "name": primary, "is_primary": True})
        seen.add(primary)
    for g in items:
        if not isinstance(g, dict):
            continue
        oid = str(g.get("openid", "") or "").strip()
        if not oid or oid in seen:
            continue
        seen.add(oid)
        out.append({
            "openid": oid,
            "name": str(g.get("name", "") or "").strip() or oid,
            "is_primary": False,
        })
    return out


def group(adapter_id: str, openid: str) -> Optional[Dict[str, Any]]:
    """返回某个群的信息。

    字段 ``{openid, name, is_primary, adapter_id, adapter_name, bound}``。
    """
    require_grant(_SCOPE)
    want = str(openid or "").strip()
    if not want:
        return None
    for g in groups(adapter_id):
        if str(g.get("openid")) == want:
            st = status(adapter_id) or {}
            return {
                "openid": want,
                "name": g.get("name") or want,
                "is_primary": bool(g.get("is_primary")),
                "adapter_id": str(adapter_id),
                "adapter_name": str(st.get("name") or ""),
                "bound": True,
            }
    return None


# ------------------------------------------------------------------- 发送

def send_text(adapter_id: Optional[str], text: str) -> bool:
    """向机器人的主群发送文本（异步入队，立即返回）。

    走网关内置发送队列，不会阻塞调用线程，也不会抛网络异常。
    """
    require_grant(_SCOPE)
    gw = gateway(adapter_id)
    if gw is None:
        return False
    body = str(text or "")
    if not body.strip():
        return False
    try:
        sender = getattr(gw, "send_qq_text", None)
        if callable(sender):
            sender(body)
            return True
    except Exception:
        pass
    return False


def send_group(adapter_id: Optional[str], group_id: str, text: str) -> bool:
    """向指定群发送文本。

    ⚠ 对 qqofficial 卡片是**同步网络调用**，可能阻塞数百毫秒到数秒。
    在服务器主线程或高频路径里调用会卡顿，建议改用 ``send_group_async()``。
    OneBot（websocket）卡片是异步入队，无此问题。
    """
    require_grant(_SCOPE)
    gw = gateway(adapter_id)
    if gw is None:
        return False
    body = str(text or "")
    target = str(group_id or "").strip()
    if not body.strip() or not target:
        return False

    # OneBot：直接入队，线程安全
    if getattr(gw, "adapter_type", None) == "websocket":
        try:
            gw.send_group_msg(int(target), body)
            return True
        except (TypeError, ValueError):
            # 群号不是数字时退回字符串，交给底层处理
            try:
                gw.send_group_msg(target, body)
                return True
            except Exception:
                return False
        except Exception:
            return False

    # qqofficial：同步 HTTP
    try:
        api = getattr(gw, "_api", None)
        if api is None:
            return False
        api.send_message_to(target, {"type": "text", "content": body}, "")
        return True
    except Exception:
        return False


def send_group_async(adapter_id: Optional[str], group_id: str, text: str) -> bool:
    """``send_group`` 的异步版本：丢到后台线程执行，立即返回。

    qqofficial 卡片的同步网络调用不会卡住调用方。
    """
    require_grant(_SCOPE)

    def _work() -> None:
        try:
            send_group(adapter_id, group_id, text)
        except Exception:
            pass

    threading.Thread(target=_work, name="gm-bot-send", daemon=True).start()
    return True
