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


class AdapterHub(object):
    """多机器人网关调度器。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.logger = plugin.logger
        self.manager = ConnectionManager(plugin.data_dir, plugin.logger)
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
                gw._card_id = str(card.get("id"))
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

    # -------------------------------------------------------------- queries
    def status(self) -> list[dict[str, Any]]:
        result = []
        for card in self.manager.adapters_view():
            gid = str(card.get("id"))
            gw = self._gateways.get(gid)
            connected = False
            if gw is not None:
                try:
                    connected = bool(gw._connected.is_set())
                except Exception:
                    connected = False
            result.append({
                "id": gid,
                "type": card.get("type"),
                "name": card.get("name"),
                "enabled": card.get("enabled"),
                "configured": self.manager.is_configured(card),
                "running": gw is not None,
                "connected": connected if gw is not None else False,
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
                        do = syn.get("mc_to_qq_format", "[游戏] %s：%s") % (
                            kw.get("player", ""), kw.get("message", ""))
                elif kind == "join":
                    if syn.get("join_to_qq", True):
                        do = syn.get("join_format", "[游戏] %s 加入了服务器") % kw.get("name", "")
                elif kind == "leave":
                    if syn.get("leave_to_qq", True):
                        do = syn.get("leave_format", "[游戏] %s 离开了服务器") % kw.get("name", "")
                if not do:
                    continue
                for g in self.manager.parse_groups(card.get("main_group")):
                    gw.send_group_msg(int(g), str(do))
            except Exception as e:
                self.logger.warning(f"[机器人] WebSocket 出站转发失败: {e}")
