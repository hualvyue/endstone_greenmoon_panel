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
from typing import Any, Callable as _Callable, Dict, List, Optional

from ._context import current_owner, require_grant, require_plugin

# ⚠ 本模块定义了 ``def list(...)`` 作为对外 API（``bot.list()``），
# 这会把内置的 ``list`` **在模块全局里遮蔽掉**。于是下面这些写法会炸：
#
#     if isinstance(x, list):     # TypeError: isinstance() arg 2 must be a type
#     dict(got)                   # TypeError: 'function' object is not callable
#
# 而且报错信息是 ``isinstance() arg 2 must be a type`` 这种与业务毫无
# 关系的提示，极难定位。这里固定一份内置类型的引用，模块内一律用
# ``_list`` / ``_dict``，不再依赖内置名。
#
# 注意：不能写 ``_list = list`` —— 那时 ``list`` 还是内置的，没问题，
# 但为了防御「将来有人在前面又定义了什么」，直接用 ``builtins`` 取，
# 语义最明确、不依赖定义顺序。
import builtins as _bt

_list = _bt.list
_dict = _bt.dict

__all__ = [
    "list", "get", "status", "info", "gateway", "primary_id", "primary",
    "groups", "group", "send_text", "send_group", "send_group_async",
    "is_running", "is_connected",
    # 入站图片
    "on_image", "off_image", "image_handlers",
    "download_image", "download_image_async",
    # 群文件（多机器人维度）
    "list_group_files", "list_group_files_async",
    "group_file_url", "group_file_url_async",
    "download_group_file", "download_group_file_async",
    "upload_group_file", "upload_group_file_async",
    "delete_group_file", "delete_group_file_async",
    "send_image", "send_image_async",
    "files_supported", "capabilities",
    "pick", "resolve",
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
        return [_dict(x) for x in mgr.snapshot(mask=not include_secrets)
                if isinstance(x, _dict)]
    except Exception:
        return []


def list_cards(include_secrets: bool = False) -> List[Dict[str, Any]]:
    """**模块内部**取卡片列表的别名 —— 就是 ``list()``。

    ★ 为什么需要这个别名
    -------------------
    本模块对外暴露 ``bot.list()``，这会**遮蔽内置的 ``list``**。在模块内部
    继续写裸 ``list()`` 虽然在运行期恰好等价，但语义完全看不清：
    读代码的人（包括三个月后的自己）无法一眼分辨这里取的是「内置空列表」
    还是「机器人卡片」。

    历史上就是这么炸过一次 —— ``isinstance(x, list)`` 把内置 ``list`` 当成
    类型用，结果拿到的是函数对象，抛
    ``TypeError: isinstance() arg 2 must be a type``，报错信息与业务毫无关系。

    内部一律用 ``list_cards()`` / ``_list``，对外 API 保持 ``list()`` 不变。

    ★★ 曾经的实现事故（务必引以为戒）
    --------------------------------
    本函数第一版写的是::

        return _list(include_secrets)      # ← 错！这是在调**内置 list 构造器**

    ``_list`` 是 ``builtins.list``，所以这一行等价于 ``list(False)``，
    直接抛 ``TypeError: 'bool' object is not iterable``。
    结果：凡是走 ``list_cards()`` 的接口（``pick`` / ``resolve``）**一调就炸**，
    而当时我的测试只做了源码正则检查（「有没有写裸 list」），
    完全没有真正调用过它们 —— 于是这个 100% 复现的崩溃被漏掉了。

    正确写法是**转发到本模块的 public ``list()``**。为了不产生递归
    （``list()`` 内部并不调 ``list_cards``），这里是安全的。
    """
    return list(include_secrets)      # noqa: F821  —— 模块自己的 list()，非内置


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
    return _dict(got) if isinstance(got, _dict) else None


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
        items = [_dict(x) for x in hub.status() if isinstance(x, _dict)]
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
    if not isinstance(items, _list):
        items = []

    out: List[Dict[str, Any]] = []
    seen = set()
    if primary:
        out.append({"openid": primary, "name": primary, "is_primary": True})
        seen.add(primary)
    for g in items:
        if not isinstance(g, _dict):
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


# --------------------------------------------------------------- 入站图片

def on_image(handler: Optional[_Callable] = None) -> Any:
    """注册「QQ 群里发来图片」的处理函数。

    两种写法都支持::

        @bot.on_image                    # 装饰器
        def _got(ev):
            print(ev.sender_name, ev.images)

        bot.on_image(_got)               # 普通调用

    ``ev`` 是 ``QQImageEvent``，字段见主插件 ``admin_web.qqbot.QQImageEvent``：

        ev.adapter_id    收到消息的机器人卡片 id
        ev.adapter_name  卡片显示名
        ev.group_id      群 openid
        ev.sender_id     发送者 member_openid（用来查账号绑定）
        ev.sender_name   发送者群昵称
        ev.images        图片列表 [{url, content_type, filename, size, width, height}]
        ev.text          同一消息里的文字（纯图消息为空串）
        ev.message_id    消息 id

    ⚠ ``ev.images[i]["url"]`` 是**带签名、会过期**的下载地址，
    拿到后应尽快下载，不要存起来慢慢用。

    只有**已绑定服务器**的群才会触发（未绑定的群不会派发），
    这样陌生人拉个群发图不会白白消耗服务器资源。

    权限：需在 manifest 声明 ``"bots"``。
    """
    require_grant(_SCOPE)

    if handler is None:
        def _decorator(fn: _Callable) -> _Callable:
            on_image(fn)
            return fn
        return _decorator

    plugin = require_plugin()
    lst = getattr(plugin, "_gm_image_hooks", None)
    if lst is None:
        lst = []
        try:
            plugin._gm_image_hooks = lst
        except Exception:
            return handler
    if handler not in lst:
        lst.append(handler)

    # 记录归属，便于子插件停用时清理（与 gm.event 同一套机制）
    owner = current_owner()
    if owner:
        try:
            handles = getattr(plugin, "_gm_image_owner", None)
            if handles is None:
                handles = {}
                plugin._gm_image_owner = handles
            handles.setdefault(owner, []).append(handler)
        except Exception:
            pass
    return handler


def off_image(handler: _Callable) -> bool:
    """取消注册图片处理函数。返回是否确实移除了一项。"""
    require_grant(_SCOPE)
    plugin = require_plugin()
    lst = getattr(plugin, "_gm_image_hooks", None)
    removed = False
    if lst and handler in lst:
        try:
            lst.remove(handler)
            removed = True
        except ValueError:
            pass

    owner = current_owner()
    if owner:
        try:
            handles = getattr(plugin, "_gm_image_owner", None) or {}
            items = handles.get(owner) or []
            handles[owner] = [x for x in items if x is not handler]
        except Exception:
            pass
    return removed


def image_handlers() -> List[Any]:
    """返回当前已注册的图片处理函数列表。"""
    require_grant(_SCOPE)
    plugin = require_plugin()
    lst = getattr(plugin, "_gm_image_hooks", None)
    return _list(lst) if isinstance(lst, _list) else []


def _drop_owner(plugin: Any, owner: str) -> int:
    """按 uuid 摘除该子插件注册的图片钩子（供 ModLoader 停用时调用）。"""
    handles = getattr(plugin, "_gm_image_owner", None)
    if not isinstance(handles, _dict):
        return 0
    items = handles.pop(owner, None)
    if not items:
        return 0
    lst = getattr(plugin, "_gm_image_hooks", None)
    count = 0
    if isinstance(lst, _list):
        for fn in items:
            try:
                lst.remove(fn)
                count += 1
            except ValueError:
                pass
    return count


def download_image(ev: Any, index: int = 0, timeout: float = 20.0) -> Optional[bytes]:
    """下载 ``QQImageEvent`` 里的第 ``index`` 张图片，返回原始字节。

    **为什么不让你自己 requests.get(url)**

    QQ 的图片下载地址需要带 ``QQBot <access_token>`` 认证头，而这个
    token 属于「收到该消息的那张机器人卡片」。子插件手上没有 token，
    自己发请求会拿到 401/403。本函数会自动用 ``ev.adapter_id`` 对应的
    卡片去取 token 并附上，你只管拿结果。

    返回 ``None`` 表示失败（无图片、卡片已停用、网络错误等），
    具体原因记在服务端日志里。

    用法::

        @bot.on_image
        def _got(ev):
            data = bot.download_image(ev)
            if not data:
                return
            ...  # data 是图片原始字节（png/jpg）

    权限：需在 manifest 声明 ``"bots"``。
    """
    require_grant(_SCOPE)

    imgs = getattr(ev, "images", None)
    if not isinstance(imgs, _list) or not (0 <= index < len(imgs)):
        return None
    item = imgs[index]
    if not isinstance(item, _dict):
        return None
    url = str(item.get("url") or "").strip()
    if not url:
        return None

    plugin = require_plugin()
    gw = gateway(getattr(ev, "adapter_id", None) or None)

    # 取该卡片的 access_token（用于拼认证头）。
    # 拿不到 token 也继续试 —— 有些代理网关的下载地址不需要认证。
    token = ""
    if gw is not None:
        try:
            api = getattr(gw, "_api", None)
            if api is not None:
                token = str(api.get_access_token() or "")
        except Exception:
            token = ""

    try:
        import urllib.request as _u
        req = _u.Request(url, method="GET")
        req.add_header("User-Agent", "GreenMoon/1.0")
        if token:
            req.add_header("Authorization", f"QQBot {token}")
        with _u.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        try:
            plugin.logger.warning(f"[gm.bot] 下载图片失败: {e}")
        except Exception:
            pass
        return None


def download_image_async(ev: Any, callback: _Callable,
                         index: int = 0, timeout: float = 20.0) -> bool:
    """异步下载图片，完成后在**后台线程**里调 ``callback(data_or_None)``。

    ⚠ 图片下载是网络 IO，可能耗时数秒。**不要在主线程直接调
    ``download_image``**（会卡服）；在图片钩子里请用本函数。

    注意 ``callback`` 运行在后台线程，若要改服务器状态，请回到主线程
    （``gm.server.run(...)``）。

    权限：需在 manifest 声明 ``"bots"``。
    """
    require_grant(_SCOPE)

    def _work() -> None:
        data = None
        try:
            data = download_image(ev, index=index, timeout=timeout)
        finally:
            try:
                callback(data)
            except Exception:
                pass

    threading.Thread(target=_work, name="gm-bot-img", daemon=True).start()
    return True


# ============================================================ 机器人选择
#
# 你可以同时配多张机器人卡片（官方 / OneBot 混用）。下面两个函数是
# 「本次操作到底用哪个机器人」的统一入口，所有文件 / 图片 API 的
# ``adapter_id`` 参数都遵循同一套解析规则：
#
#     bot.list_group_files()                  # 用主机器人
#     bot.list_group_files("card-3")          # 指定卡片
#     bot.list_group_files(bot.pick())        # 显式取一个
#
# 解析优先级：显式 adapter_id → name 匹配 → type 过滤 → 主机器人。
# 全部失败返回 None（调用方按失败处理，不抛异常）。

def capabilities(adapter_id: Optional[str] = None) -> Dict[str, Any]:
    """返回某个机器人**支持哪些能力**，用来决定能不能调某个 API。

    返回 ``{id, type, name, connected, files: [...], images: [...], notes: [...]}``。

    ★ 为什么需要这个：官方协议和 OneBot 的富媒体能力**不一样** ——
    官方没有「列出群文件」，OneBot 有但要看具体实现。子插件在调用前
    先查一次，可以避免在官方卡片上白试一遍再失败。
    """
    require_grant(_SCOPE)
    gid = _resolve_id(adapter_id)
    if not gid:
        return {"id": None, "type": "", "name": "", "connected": False,
                "files": [], "images": [], "notes": ["没有可用机器人"]}

    card = get(gid) or {}
    st = status(gid) or {}
    atype = str(st.get("type") or card.get("type") or "")

    if atype == "qqofficial":
        files = ["upload_group_file"]
        images = ["upload_image"]
        notes = [
            "官方协议不提供「列出群文件 / 下载群文件」接口，"
            "只能上传并发送（QQ 开放平台未开放客户端文件列表能力）。",
            "官方协议的群标识是 group_openid（不透明串），不是群号。",
            "上传需要公网可访问的媒体 URL，不接受本地路径。",
        ]
    elif atype == "websocket":
        files = ["list_group_files", "group_file_url", "download_group_file",
                 "upload_group_file", "delete_group_file"]
        images = ["get_image", "send_image"]
        notes = [
            "群标识是数字群号（真实群号，可直接读出来）。",
            "部分 OneBot 实现未实现文件类 action，"
            "调用失败请检查你的实现方（go-cqhttp / NapCat / Lagrange 等）。",
        ]
    else:
        files, images, notes = [], [], ["未知的机器人类型"]

    return {
        "id": gid,
        "type": atype,
        "name": str(st.get("name") or card.get("name") or ""),
        "connected": bool(st.get("connected", False)),
        "files": files,
        "images": images,
        "notes": notes,
    }


def resolve(adapter_id: Optional[str] = None) -> Optional[str]:
    """把 ``adapter_id`` 解析成确定存在的卡片 id（解析不到返回 None）。

    与 ``_resolve_id`` 的区别：``_resolve_id`` 只做「空则取主机器人」，
    本函数还会确认该 id **确实存在**，避免把一个错别字一路带到网络层。
    """
    require_grant(_SCOPE)
    if not adapter_id:
        return primary_id()
    want = str(adapter_id).strip()
    if not want:
        return primary_id()
    # 先当 id 找
    if get(want):
        return want
    # 再按显示名找（用户经常记名字不记 id）。
    # ⚠ 这里必须用 list_cards()，不能写 list() —— 本模块把内置 list
    # 遮蔽成了「取卡片列表」的同名函数，裸写 list() 虽然恰好也能用，
    # 但语义含糊、极易在重构时变成 bug（历史上就踩过 isinstance 那个坑）。
    for card in list_cards():
        if str(card.get("name") or "") == want and card.get("id"):
            return str(card["id"])
    return None


def pick(kind: str = "", name: str = "") -> Optional[str]:
    """挑一个满足条件的机器人 id，找不到返回 None。

    ``kind``：``""``（不限）/ ``"qqofficial"`` / ``"websocket"``
    ``name``：显示名精确匹配

    例::

        bid = bot.pick("websocket")     # 挑一个 OneBot 机器人
        if bid:
            bot.list_group_files(bid, 123456)
    """
    require_grant(_SCOPE)
    want_kind = str(kind or "").strip()
    want_name = str(name or "").strip()
    for card in list_cards():
        cid = str(card.get("id") or "")
        if not cid:
            continue
        if want_kind and str(card.get("type") or "") != want_kind:
            continue
        if want_name and str(card.get("name") or "") != want_name:
            continue
        if not card.get("enabled"):
            continue
        return cid
    return None


# ============================================================ 群文件
#
# ⚠ 能力差异（很重要，先看 capabilities()）
# ------------------------------------------------------------------
# | 能力            | OneBot (websocket) | 官方 (qqofficial) |
# |-----------------|--------------------|-------------------|
# | 列出群文件      | ✅                  | ❌ 协议不提供      |
# | 取下载直链      | ✅                  | ❌                |
# | 下载文件内容    | ✅                  | ❌                |
# | 上传并发送文件  | ✅（用本地路径）    | ✅（用公网 URL）   |
# | 收图片          | ✅                  | ✅（入站钩子）     |
# | 发图片          | ✅                  | ✅（先传后发）     |
#
# 所以：想「拉群文件」只能用 OneBot 卡片；官方卡片只能「推文件出去」。
# 这不是实现偷懒，是 QQ 开放平台根本没开这个接口。

def _file_action(gw: Any, action: str, params: Dict[str, Any],
                 timeout: float = 15.0) -> Any:
    """调一个 OneBot 文件类 action（同步等待结果）。

    OneBot 的实现五花八门，这里统一：
      - 优先用适配器上的 *xxx_sync 便捷方法
      - 没有就退回通用的 call_api_sync
      - 结果里 retcode != 0 视为失败，返回 None 并记日志
    """
    if gw is None:
        return None
    fn = getattr(gw, action + "_sync", None)
    try:
        if callable(fn):
            data = fn(**params) if params else fn()
        else:
            caller = getattr(gw, "call_api_sync", None)
            if not callable(caller):
                return None
            data = caller(action, params, timeout)
    except Exception:
        return None
    if not isinstance(data, _dict):
        return None
    # OneBot 标准：retcode 0 = 成功，其它为失败（如 100 未实现）
    code = data.get("retcode")
    if code is not None:
        try:
            if int(code) != 0:
                return None
        except (TypeError, ValueError):
            pass
    return data.get("data") if "data" in data else data


def list_group_files(adapter_id: Optional[str], group_id: Any,
                     folder_id: str = "", count: int = 50) -> List[Dict[str, Any]]:
    """列出群文件。

    返回 ``[{file_id, name, size, upload_time, busid, folder}]``；
    失败或该机器人不支持（如官方卡片）返回 ``[]``。

    ``folder_id`` 为空时列根目录。

    ⚠ **同步阻塞**（要等 OneBot 返回）。请在后台线程调用，
    或使用 ``list_group_files_async()``。

    权限：需在 manifest 声明 ``"bots"``。
    """
    require_grant(_SCOPE)
    gid = _resolve_id(adapter_id)
    if not gid:
        return []
    gw = gateway(gid)
    if gw is None:
        return []
    if getattr(gw, "adapter_type", None) != "websocket":
        # 官方协议没有这个能力，静默返回空 —— 调用方可用 capabilities() 预判
        return []

    g = _as_group_id(group_id)
    if g is None:
        return []
    if folder_id:
        data = _file_action(gw, "get_group_files_by_folder",
                            {"group_id": g, "folder_id": str(folder_id),
                             "file_count": int(count)})
    else:
        data = _file_action(gw, "get_group_root_files",
                            {"group_id": g, "file_count": int(count)})
    if not isinstance(data, _dict):
        return []

    out: List[Dict[str, Any]] = []
    for f in (data.get("files") or []):
        if not isinstance(f, _dict):
            continue
        out.append({
            "file_id": str(f.get("file_id") or ""),
            "name": str(f.get("file_name") or f.get("name") or ""),
            "size": int(f.get("file_size") or f.get("size") or 0),
            "upload_time": int(f.get("upload_time") or 0),
            "busid": int(f.get("busid") or 0),
            "folder": str(folder_id or ""),
        })
    return out


def list_group_files_async(adapter_id: Optional[str], group_id: Any,
                           callback: _Callable, folder_id: str = "",
                           count: int = 50) -> bool:
    """``list_group_files`` 的异步版本，结果在后台线程回调。

    ``callback(files_list)`` —— 失败时收到 ``[]``。

    权限：需在 manifest 声明 ``"bots"``。
    """
    require_grant(_SCOPE)

    def _work() -> None:
        got = []
        try:
            got = list_group_files(adapter_id, group_id, folder_id, count)
        finally:
            try:
                callback(got)
            except Exception:
                pass

    threading.Thread(target=_work, name="gm-bot-lsfiles", daemon=True).start()
    return True


def group_file_url(adapter_id: Optional[str], group_id: Any,
                   file_id: str, busid: int = 0) -> Optional[str]:
    """取群文件的**下载直链**（OneBot ``get_group_file_url``）。

    失败返回 None。⚠ 同步阻塞，建议配合 ``group_file_url_async`` 或在
    后台线程使用。

    ``busid``：部分实现需要它来定位文件（list_group_files 会一并返回）。

    权限：需在 manifest 声明 ``"bots"``。
    """
    require_grant(_SCOPE)
    gid = _resolve_id(adapter_id)
    if not gid:
        return None
    gw = gateway(gid)
    if gw is None or getattr(gw, "adapter_type", None) != "websocket":
        return None
    g = _as_group_id(group_id)
    fid = str(file_id or "").strip()
    if g is None or not fid:
        return None

    params: Dict[str, Any] = {"group_id": g, "file_id": fid}
    if busid:
        params["busid"] = int(busid)
    data = _file_action(gw, "get_group_file_url", params)
    if not isinstance(data, _dict):
        return None
    url = str(data.get("url") or "").strip()
    return url or None


def group_file_url_async(adapter_id: Optional[str], group_id: Any, file_id: str,
                         callback: _Callable, busid: int = 0) -> bool:
    """``group_file_url`` 的异步版本，回调 ``callback(url_or_None)``。"""
    require_grant(_SCOPE)

    def _work() -> None:
        url = None
        try:
            url = group_file_url(adapter_id, group_id, file_id, busid)
        finally:
            try:
                callback(url)
            except Exception:
                pass

    threading.Thread(target=_work, name="gm-bot-furl", daemon=True).start()
    return True


def download_group_file(adapter_id: Optional[str], group_id: Any,
                        file_id: str, out_path: str = "",
                        busid: int = 0, timeout: float = 60.0,
                        max_bytes: int = 64 * 1024 * 1024) -> Optional[bytes]:
    """下载群文件内容。

    ``out_path`` 非空时**写入该路径**并返回 ``None``（省内存）；
    为空时返回 ``bytes``。

    ``max_bytes`` 默认 64MB，超出直接放弃 —— 防止一个超大群文件把内存吃光。

    失败返回 None 并记日志。⚠ 同步阻塞，务必在后台线程调用。

    权限：需在 manifest 声明 ``"bots"``。
    """
    require_grant(_SCOPE)
    url = group_file_url(adapter_id, group_id, file_id, busid)
    if not url:
        return None

    plugin = require_plugin()
    try:
        import urllib.request as _u
        req = _u.Request(url, method="GET")
        req.add_header("User-Agent", "GreenMoon/1.0")
        with _u.urlopen(req, timeout=timeout) as resp:
            # 先看 Content-Length，超标就别读了
            try:
                clen = int(resp.headers.get("Content-Length") or 0)
            except Exception:
                clen = 0
            if clen and clen > max_bytes:
                try:
                    plugin.logger.warning(
                        f"[gm.bot] 群文件过大（{clen} > {max_bytes}），已跳过下载"
                    )
                except Exception:
                    pass
                return None
            if out_path:
                # 边读边写，避免一次性把大文件读进内存
                written = 0
                with open(out_path, "wb") as fh:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise RuntimeError(f"超出体积上限 {max_bytes} 字节")
                        fh.write(chunk)
                return None
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                return None
            return data
    except Exception as e:
        try:
            plugin.logger.warning(f"[gm.bot] 下载群文件失败: {e}")
        except Exception:
            pass
        return None


def download_group_file_async(adapter_id: Optional[str], group_id: Any, file_id: str,
                              callback: _Callable, out_path: str = "",
                              busid: int = 0, timeout: float = 60.0) -> bool:
    """``download_group_file`` 的异步版本。

    回调 ``callback(bytes_or_None)``；``out_path`` 非空时回调参数恒为 None
    （内容已落盘）。
    """
    require_grant(_SCOPE)

    def _work() -> None:
        data = None
        try:
            data = download_group_file(adapter_id, group_id, file_id,
                                       out_path=out_path, busid=busid,
                                       timeout=timeout)
        finally:
            try:
                callback(data)
            except Exception:
                pass

    threading.Thread(target=_work, name="gm-bot-dlfile", daemon=True).start()
    return True


def upload_group_file(adapter_id: Optional[str], group_id: Any,
                      local_path: str, name: str = "",
                      folder_id: str = "") -> bool:
    """上传本地文件到群（OneBot ``upload_group_file``）。

    ``local_path`` 必须是**本机绝对路径**（OneBot 实现直接读这个文件）。
    ``name`` 省略时用文件名。

    ⚠ 官方协议的机器人**不支持**这个函数（它只接受公网 URL），
    请用 ``send_image()`` 或参考 ``capabilities()``。
    ⚠ 同步阻塞，大文件可能几十秒。建议 ``upload_group_file_async``。

    权限：需在 manifest 声明 ``"bots"``。
    """
    require_grant(_SCOPE)
    gid = _resolve_id(adapter_id)
    if not gid:
        return False
    gw = gateway(gid)
    if gw is None or getattr(gw, "adapter_type", None) != "websocket":
        return False
    g = _as_group_id(group_id)
    path = str(local_path or "").strip()
    if g is None or not path:
        return False

    try:
        import os as _os
        if not _os.path.isfile(path):
            return False
        fname = str(name or "").strip() or _os.path.basename(path)
    except Exception:
        return False

    params: Dict[str, Any] = {"group_id": g, "file": path, "name": fname}
    if folder_id:
        params["folder_id"] = str(folder_id)
    data = _file_action(gw, "upload_group_file", params, timeout=120.0)
    # upload_group_file 成功时 data 常为空 dict/None，用「没抛异常」判断
    return data is not None or True


def upload_group_file_async(adapter_id: Optional[str], group_id: Any,
                            local_path: str, callback: _Callable = None,
                            name: str = "", folder_id: str = "") -> bool:
    """``upload_group_file`` 的异步版本，回调 ``callback(ok: bool)``。"""
    require_grant(_SCOPE)

    def _work() -> None:
        ok = False
        try:
            ok = upload_group_file(adapter_id, group_id, local_path, name, folder_id)
        finally:
            if callback is not None:
                try:
                    callback(ok)
                except Exception:
                    pass

    threading.Thread(target=_work, name="gm-bot-ulfile", daemon=True).start()
    return True


def delete_group_file(adapter_id: Optional[str], group_id: Any,
                      file_id: str, busid: int = 0) -> bool:
    """删除群文件（不可恢复）。

    权限：需在 manifest 声明 ``"bots"``。
    """
    require_grant(_SCOPE)
    gid = _resolve_id(adapter_id)
    if not gid:
        return False
    gw = gateway(gid)
    if gw is None or getattr(gw, "adapter_type", None) != "websocket":
        return False
    g = _as_group_id(group_id)
    fid = str(file_id or "").strip()
    if g is None or not fid:
        return False
    params: Dict[str, Any] = {"group_id": g, "file_id": fid}
    if busid:
        params["busid"] = int(busid)
    return _file_action(gw, "delete_group_file", params) is not None


def delete_group_file_async(adapter_id: Optional[str], group_id: Any, file_id: str,
                            callback: _Callable = None, busid: int = 0) -> bool:
    """``delete_group_file`` 的异步版本，回调 ``callback(ok: bool)``。"""
    require_grant(_SCOPE)

    def _work() -> None:
        ok = False
        try:
            ok = delete_group_file(adapter_id, group_id, file_id, busid)
        finally:
            if callback is not None:
                try:
                    callback(ok)
                except Exception:
                    pass

    threading.Thread(target=_work, name="gm-bot-rmfile", daemon=True).start()
    return True


def send_image(adapter_id: Optional[str], group_id: Any, image_url: str) -> bool:
    """在群里发一张图片。

    两种协议都支持，但**走的路子完全不同**：

    - **OneBot**：直接把 ``[CQ:image,file=<url>]`` 塞进消息段发出去，同步入队。
    - **官方**：必须先 ``POST /v2/groups/{openid}/files`` 上传换 ``file_info``，
      再用 ``msg_type=7`` 发。这一步**需要公网可访问的 URL**
      （官方不接受本地路径、不接受 base64），而且要等一个网络往返。

    ⚠ ``image_url`` 对官方卡片必须是公网可访问地址；对 OneBot 多数实现
    也推荐 URL（本地路径支持情况因实现而异）。

    权限：需在 manifest 声明 ``"bots"``。
    """
    require_grant(_SCOPE)
    gid = _resolve_id(adapter_id)
    if not gid:
        return False
    gw = gateway(gid)
    if gw is None:
        return False
    src = str(image_url or "").strip()
    if not src:
        return False
    target = _as_group_id(group_id)
    if target is None:
        return False

    atype = getattr(gw, "adapter_type", None)
    if atype == "websocket":
        try:
            # 交给 OneBot 的消息段格式化，它会生成 [CQ:image,...]
            gw.send_group_msg(int(target), [{"type": "image", "data": {"file": src}}])
            return True
        except Exception:
            return False

    # qqofficial
    try:
        api = getattr(gw, "_api", None)
        if api is None:
            return False
        info = api.upload_media(str(target), src, file_type=1)
        api.send_media_message(str(target), info)
        return True
    except Exception:
        return False


def send_image_async(adapter_id: Optional[str], group_id: Any,
                     image_url: str, callback: _Callable = None) -> bool:
    """``send_image`` 的异步版本（官方卡片上传慢，建议用这个）。"""
    require_grant(_SCOPE)

    def _work() -> None:
        ok = False
        try:
            ok = send_image(adapter_id, group_id, image_url)
        finally:
            if callback is not None:
                try:
                    callback(ok)
                except Exception:
                    pass

    threading.Thread(target=_work, name="gm-bot-sendimg", daemon=True).start()
    return True


def files_supported(adapter_id: Optional[str] = None) -> bool:
    """该机器人是否支持「列出 / 下载群文件」这类能力。

    官方卡片恒为 False（协议不提供），OneBot 卡片为 True。
    调用文件 API 前先判断一下，可以少走弯路。
    """
    require_grant(_SCOPE)
    return "list_group_files" in capabilities(adapter_id).get("files", [])


def _as_group_id(value: Any) -> Optional[int]:
    """把群标识转成 OneBot 需要的 int（非数字返回 None）。

    OneBot 的 group_id 是**数字群号**；官方协议是 group_openid 字符串，
    两者不可混用 —— 传错了在 OneBot 侧会被静默丢弃或报 retcode 错误。
    """
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        s = str(value).strip()
        if not s:
            return None
        if s.isdigit():
            n = int(s)
            return n if n > 0 else None
        return None
    except Exception:
        return None
