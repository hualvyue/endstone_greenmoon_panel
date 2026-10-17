"""适配器 Hub：统一管理多张机器人卡片对应的网关实例。

设计要点：
  - ``ConnectionManager`` 只负责配置存取；``AdapterHub`` 负责把「已启用且已配置」
    的卡片实例化成运行中的网关，并对事件做统一上报。
  - 兼容旧版：主官方卡片对应的网关实例会回填到 ``plugin.qq_bot``，
    使现有 web / 互通 / 跨服对 ``self.qq_bot`` 的调用继续有效。
  - 多机器人均启用时，各自独立收发、互不干扰；群消息按「来源网关」路由，
    即由收到该消息的网关实例负责回复与广播。

当前网关实现：
  - ``qqofficial`` 复用 ``QQBotGateway``（官方机器人网关鉴权）。
  - ``websocket`` 使用 ``OneBotAdapter``（OneBot v11 正向/反向 WebSocket，
    移植自 LumenBridge），并把入站群消息经 ``OneBotInterop`` 路由到互通链路。
"""

from __future__ import annotations

import threading
from typing import Any

from .connections import ConnectionManager


def _render(template, **values):
    """套用用户自定义的播报模板。

    模板可能写成 ``%s``（按位置）也可能写成 ``%(name)s``（按名字），
    还可能有占位符个数对不上的情况。之前这里直接 ``tpl % (a, b)``，
    用户一旦把模板改成 ``%(player)s`` 这种命名式，转发就会静默失败
    （异常被下面的 except 吞掉，群里什么都收不到）。

    依次尝试命名式、位置式、宽松替换；全失败返回 None（放弃这条）。
    """
    text = str(template or "")
    if not text:
        return None
    vals = list(values.values())
    if "%(" in text:
        try:
            return text % values
        except Exception:
            pass
    try:
        return text % tuple(vals)
    except Exception:
        pass
    try:
        out = text
        for v in vals:
            if "%s" not in out:
                break
            out = out.replace("%s", str(v), 1)
        return out
    except Exception:
        return None


class AdapterHub(object):
    """多机器人网关调度器。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.logger = plugin.logger
        # 全局只应存在一个 ConnectionManager 实例。
        # 它内部持有 adapters 列表的内存缓存，两个实例会各自缓存一份：
        # Web 侧（plugin.conn_mgr）改了 A 卡配置，Hub 侧（self.manager）
        # 仍读旧值，表现为「改了没用 / 状态对不上 / 卡片串台」。
        # 因此优先复用 plugin.conn_mgr，只有它确实不存在时才自建并回填。
        mgr = getattr(plugin, "conn_mgr", None)
        if mgr is None:
            mgr = ConnectionManager(plugin.data_dir, plugin.logger)
            plugin.conn_mgr = mgr
        elif not isinstance(mgr, ConnectionManager):
            # 防御：外部给了一个签名不兼容的对象时宁可自建，也不要让
            # 后续 self.manager.xxx() 在运行时抛 AttributeError。
            self.logger.warning("[机器人] plugin.conn_mgr 类型异常，Hub 自建配置管理器")
            mgr = ConnectionManager(plugin.data_dir, plugin.logger)
        self.manager = mgr
        # 共享事件总线：WebSocket 适配器与互通回调都经它通信
        from .eventbus import EventBus
        self.event_bus = getattr(plugin, "event_bus", None)
        if self.event_bus is None:
            self.event_bus = EventBus(plugin.logger)
            plugin.event_bus = self.event_bus
        # 入站互通：把 onebot.pack 群消息路由到游戏互通 / 跨服 / 指令
        from .onebot.interop import OneBotInterop
        self._interop = OneBotInterop(plugin, self.event_bus, self)
        self._gateways: dict[str, Any] = {}   # card_id -> gateway 实例
        self._lock = threading.RLock()

    # ------------------------------------------------------------- lifecycle
    def start_all(self, libs=None) -> None:
        """启动所有已启用且已配置的卡片；主官方卡回填 plugin.qq_bot。"""
        with self._lock:
            self._boot(libs)

    def _boot(self, libs=None) -> None:
        primary = None
        for card in self.manager.adapters_view():
            gid = str(card.get("id"))
            if not card.get("enabled"):
                continue
            if not self.manager.is_configured(card):
                self.logger.warning(
                    f"[机器人] 卡片「{card.get('name')}」未配置完整，未启动"
                )
                continue
            if gid in self._gateways:
                continue
            atype = str(card.get("type"))
            gw = self._spawn(card, atype, libs)
            if gw is None:
                continue
            self._gateways[gid] = gw
            if atype == "qqofficial" and primary is None:
                primary = gw
        # 兼容回填：现有调用点读 self.qq_bot
        if primary is not None:
            self.plugin.qq_bot = primary
        elif hasattr(self.plugin, "qq_bot"):
            pass  # 若无官方网关，保留既有（可能为 None）

    def _spawn(self, card, atype, libs):
        try:
            if atype == "qqofficial":
                if libs is None:
                    self.logger.error("[机器人] 第三方库不可用，网关未启动")
                    return None
                flat = self.manager.to_flat_config(card)
                from .qqbot import QQBotGateway
                gw = QQBotGateway(self.plugin, flat, libs[0], libs[1])
                # 卡片身份统一挂载。
                # 这两个属性是**运行期动态加上去**的，不在 QQBotGateway.__init__
                # 里定义，所以任何读取方都必须用 getattr 兜底 —— 曾经
                # _dispatch_image 直接写 self._card_name，而这里又只设了
                # _card_id，导致每次收到图片都抛 AttributeError。
                # 现在两个都设，并且读取侧也做了防御（双保险）。
                gw._card_id = str(card.get("id") or "")
                gw._card_name = str(card.get("name") or "")
                gw.start()
                self.logger.info(f"[机器人] 官方网关已启动：{card.get('name')}")
                return gw
            if atype == "websocket":
                if libs is None:
                    self.logger.error("[机器人] 第三方库不可用，WebSocket 网关未启动")
                    return None
                websockets_mod = libs[1]
                if websockets_mod is None:
                    self.logger.error("[机器人] 未找到 websockets 库，WebSocket 网关未启动")
                    return None
                from .onebot import OneBotAdapter
                conn_event = threading.Event()

                def _on_state(adapter, connected):
                    try:
                        if connected:
                            conn_event.set()
                        else:
                            conn_event.clear()
                    except Exception:
                        pass

                groups = self.manager.parse_groups(card.get("main_group"))
                gw = OneBotAdapter(
                    websockets_mod,
                    self.plugin.logger,
                    self.event_bus,
                    ws_type=int(card.get("ws_type", 0) or 0),
                    target=str(card.get("target", "") or "ws://127.0.0.1:3001"),
                    listen_host=str(card.get("listen_host", "0.0.0.0") or "0.0.0.0"),
                    listen_port=int(card.get("listen_port", 3002) or 3002),
                    access_token=str(card.get("access_token", "") or ""),
                    bot_qq=int(card.get("bot_qq", 0) or 0),
                    adapter_id=str(card.get("id") or ""),
                    adapter_name=str(card.get("name") or ""),
                    adapter_type="websocket",
                    groups=groups,
                    on_state_change=_on_state,
                )
                gw._connected = conn_event
                gw.start()
                self.logger.info(f"[机器人] WebSocket 网关已启动：{card.get('name')}（{'正向' if int(card.get('ws_type', 0) or 0) == 0 else '反向'}）")
                return gw
        except Exception as e:
            self.logger.error(f"[机器人] 启动卡片「{card.get('name')}」失败: {e}")
        return None

    def stop_all(self) -> None:
        with self._lock:
            for gid, gw in list(self._gateways.items()):
                try:
                    gw.stop()
                except Exception as e:
                    self.logger.warning(f"[机器人] 停止网关失败: {e}")
            self._gateways.clear()
        if hasattr(self.plugin, "qq_bot"):
            self.plugin.qq_bot = None

    def restart(self, libs=None) -> None:
        self.stop_all()
        self._boot(libs)

    def reset_circuits(self, reason: str = "配置重新加载") -> int:
        """复位所有网关的熔断/限流状态，返回被复位的网关数量。

        ★ 为什么需要这个入口
        -------------------
        熔断态的**唯一**自动复位点是 QQ 网关收到 READY（即真的连上了）。
        除此之外只能人工复位。原先这条路径靠在 ``QQBotGateway.start()``
        里无条件清标志实现 —— 那是个致命设计：``start()`` 会被监督线程
        间接反复走到，于是熔断标志每 5 秒被清一次，熔断从未生效过
        （实测 38 秒重连 7 次，还撞出官方限流 code 100017）。

        现在复位改为显式动作：面板「重新保存机器人配置」时经
        ``_restart_qq_gateway`` 调用本方法。注意 ``restart()`` 本身走的是
        ``stop_all()`` + ``_boot()`` 重建实例，新实例天然是干净状态，
        所以这里主要覆盖的是「同一实例、只按了保存」的场景。
        """
        n = 0
        for gw in list(self._gateways.values()):
            fn = getattr(gw, "reset_circuit", None)
            if not callable(fn):
                continue
            try:
                fn(reason)
                n += 1
            except Exception as e:
                self.logger.warning(f"[机器人] 复位熔断状态失败: {e}")
        return n

    # -------------------------------------------------------------- queries
    @staticmethod
    def _gw_connected(gw) -> bool:
        """统一探询网关的「已连接」状态，不碰任何私有属性。

        ★ 为什么需要这个适配层
        --------------------
        两套网关暴露连接状态的形式**长得不一样**：

        * ``QQBotGateway``（官方）—— 普通方法 ``is_connected()``
        * ``OneBotAdapter``（OneBot）—— ``@property is_connected``

        而 ``Hub.status()`` 里原先直接写 ``gw._connected.is_set()``，
        读的是**私有属性**。这在 OneBot 侧纯属巧合能跑通
        （``_spawn`` 里手工塞了个 ``namedtuple([server, websockets])``
        以外的 ``threading.Event``），一旦哪天不塞了就会**静默**变成
        「永远显示未连接」——不报错、不抛异常，面板上机器人明明在线
        却挂着灰点，没有任何线索可查。

        现在按优先级依次尝试：
        ``is_connected()`` 方法 → ``is_connected`` 属性 →
        ``_connected.is_set()`` 私有兜底 → False。
        前三种任意一种成功即返回，全部失败才落到 False。
        """
        try:
            probe = getattr(gw, "is_connected", None)
            if probe is not None:
                # 先当**方法**调；property 已经在 getattr 时求值成 bool 了
                val = probe() if callable(probe) else probe
                return bool(val)
        except Exception:
            pass
        try:
            ev = getattr(gw, "_connected", None)
            if ev is not None and hasattr(ev, "is_set"):
                return bool(ev.is_set())
        except Exception:
            pass
        return False

    @staticmethod
    def _gw_circuit_tripped(gw) -> bool:
        """网关是否处于「已熔断」状态（不再自动重连）。

        ★ 为什么面板要区分「熔断」和「离线」
        ----------------------------------
        intents 配置错误时（4013/4914/4915），网关会主动停止重连。
        如果面板只显示「离线」，服主的第一反应是「网络又抖了，等等就好」，
        然后一直等下去 —— 而实际上必须去 QQ 开放平台改后台配置，
        干等永远不会恢复。把熔断状态单独暴露出来，前端才能给出
        「去检查事件订阅」这种可执行的指引。
        """
        try:
            return bool(getattr(gw, "_intent_circuit_open", False))
        except Exception:
            return False

    def status(self) -> list[dict[str, Any]]:
        result = []
        for card in self.manager.adapters_view():
            gid = str(card.get("id"))
            gw = self._gateways.get(gid)
            connected = False
            if gw is not None:
                connected = self._gw_connected(gw)
            result.append({
                "id": gid,
                "type": card.get("type"),
                "name": card.get("name"),
                "enabled": card.get("enabled"),
                "configured": self.manager.is_configured(card),
                "running": gw is not None,
                "connected": connected if gw is not None else False,
                "circuit_tripped": self._gw_circuit_tripped(gw) if gw is not None else False,
            })
        return result

    def adapter_ids(self) -> list[str]:
        return [str(a.get("id")) for a in self.manager.adapters_view()]

    def gateway_by_id(self, gid: str):
        return self._gateways.get(gid)

    def primary_gateway(self):
        """主网关：第一个官方网关实例，否则 None。"""
        for card in self.manager.adapters_view():
            if str(card.get("type")) == "qqofficial":
                return self._gateways.get(str(card.get("id")))
        return None

    # ------------------------------------------------------------ 出站转发
    def websocket_relay(self, kind: str, **kw) -> None:
        """把游戏事件（聊天 / 加入 / 离开）按各自卡片配置转发给 WebSocket 机器人。

        kind: ``chat`` / ``join`` / ``leave``
        """
        for gid, gw in list(self._gateways.items()):
            if getattr(gw, "adapter_type", None) != "websocket":
                continue
            try:
                card = self.manager.get_view(str(gid)) or {}
                syn = card.get("sync") or {}
                do = None
                if kind == "chat":
                    if syn.get("mc_to_qq", True):
                        do = _render(syn.get("mc_to_qq_format", "[游戏] %s：%s"),
                                     player=kw.get("player", ""),
                                     message=kw.get("message", ""))
                elif kind == "join":
                    if syn.get("join_to_qq", True):
                        do = _render(syn.get("join_format", "[游戏] %s 加入了服务器"),
                                     name=kw.get("name", ""))
                elif kind == "leave":
                    if syn.get("leave_to_qq", True):
                        do = _render(syn.get("leave_format", "[游戏] %s 离开了服务器"),
                                     name=kw.get("name", ""))
                if not do:
                    continue
                for g in self.manager.parse_groups(card.get("main_group")):
                    gw.send_group_msg(int(g), str(do))
            except Exception as e:
                self.logger.warning(f"[机器人] WebSocket 出站转发失败: {e}")
