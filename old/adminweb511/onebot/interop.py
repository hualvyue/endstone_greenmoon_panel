"""OneBot v11 适配器入站互通路由。

WebSocket 适配器（正向 / 反向）把上报的入站包统一广播到共享事件总线
``onebot.pack``。本模块消费该事件，把其中的群消息按「来源适配器」还原出
对应卡片配置，再接入 GreenMoon 的群服互通 / 跨服 / 管理指令链路，从而
让 OneBot(个人号) 机器人与 QQ 官方机器人具备相同的互通能力。

多适配器并存时各自独立路由：处理器通过 ``data["_lumen_adapter_id"]``
反查 Hub 中对应网关，并以该网关回复，来源群只由收到消息的适配器处理。

注意：游戏侧调用（broadcast / dispatch_command）必须调度到主线程，
这里统一经 ``plugin.server.scheduler.run_task`` 回投。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

# 状态 / 关键词扩展命今前缀
_QQ_TO_MC_HINTS = ("查状态", "服务器状态", "状态", "在线", "tps")
_CMD_PREFIX = "/"


def _extract_text(data: dict[str, Any]) -> str:
    """从 OneBot message 段或 raw_message 抽取纯文本。"""
    segments = data.get("message")
    if isinstance(segments, list):
        chunks: list[str] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            sdata = seg.get("data") or {}
            if stype == "text":
                chunks.append(str(sdata.get("text", "")))
            elif stype == "at" and sdata.get("qq") not in (None, ""):
                chunks.append(f"@{sdata.get('qq')}")
        text = "".join(chunks).strip()
        if text:
            return text
    raw = data.get("raw_message")
    if isinstance(raw, str):
        return raw.strip()
    return ""


class OneBotInterop:
    """把 OneBot 群/私聊消息路由到互通链路，并按来源适配器回复。"""

    def __init__(self, plugin, event_bus, hub, *, register: bool = True) -> None:
        self.plugin = plugin
        self.bus = event_bus
        self.hub = hub
        self._log = plugin.logger
        self._registered = False
        if register:
            self.register()

    # ----------------------------------------------------------------- 注册
    def register(self) -> None:
        if self._registered:
            return
        self.bus.on("onebot.pack", self._on_pack)
        self._registered = True

    def unregister(self) -> None:
        if not self._registered:
            return
        try:
            self.bus.off("onebot.pack", self._on_pack)
        except Exception:
            pass
        self._registered = False

    # --------------------------------------------------------------- 主入口
    def _on_pack(self, data: dict[str, Any]) -> None:
        try:
            if not isinstance(data, dict):
                return
            if data.get("post_type") != "message":
                return
            adapter = self._resolve_adapter(data)
            if adapter is None:
                return
            card = self._card_of(adapter)
            if card is None:
                return
            mtype = data.get("message_type")
            if mtype == "group":
                self._handle_group(card, adapter, data)
            elif mtype == "private":
                self._handle_private(card, adapter, data)
        except Exception as e:
            self._log.warning(f"[OneBot互通] 处理入站消息失败: {e}")

    def _resolve_adapter(self, data: dict[str, Any]):
        adapter_id = data.get("_lumen_adapter_id")
        if not adapter_id:
            return None
        try:
            return self.hub.gateway_by_id(str(adapter_id))
        except Exception:
            return None

    def _card_of(self, adapter: dict[str, Any] | object):
        adapter_id = getattr(adapter, "adapter_id", None)
        if not adapter_id:
            return None
        try:
            return self.plugin.conn_mgr.get_view(str(adapter_id))
        except Exception:
            return None

    # ------------------------------------------------------- 主线程调度工具
    def _run_on_main(self, fn: Callable[[], None]) -> None:
        try:
            server = getattr(self.plugin, "server", None)
            if server is None:
                return
            scheduler = getattr(server, "scheduler", None)
            if scheduler is None:
                return
            scheduler.run_task(self.plugin, self._safe(fn))
        except Exception as e:
            self._log.error(f"[OneBot互通] 调度主线程任务失败: {e}")

    def _safe(self, fn: Callable[[], None]) -> Callable[[], None]:
        log = self._log

        def _run() -> None:
            try:
                fn()
            except Exception as e:
                # 与 QQ 官方网关 _safe_task 同策略：捕获并记录，避免任务中断
                log.error(f"[OneBot互通] 主线程任务异常: {e}")
        return _run

    def _broadcast_game(self, msg: str) -> None:
        msg = str(msg)

        def _do() -> None:
            try:
                self.plugin.server.broadcast_message(msg)
            except Exception:
                try:
                    self.plugin.server.broadcast(msg)
                except Exception:
                    pass
        self._run_on_main(_do)

    def _dispatch_cmd(self, cmd: str) -> None:
        def _do() -> None:
            try:
                self.plugin.server.dispatch_command(self.plugin.server.command_sender, cmd)
            except Exception as e:
                self._log.error(f"[OneBot互通] 执行指令失败: {e}")
        self._run_on_main(_do)

    # ------------------------------------------------------------- 群消息
    def _handle_group(self, card: dict[str, Any], adapter, data: dict[str, Any]) -> None:
        content = _extract_text(data)
        if not content:
            return
        group_id = data.get("group_id")
        user_id = data.get("user_id")
        sender = data.get("sender") or {}
        sender_name = str(sender.get("card") or sender.get("nickname") or "未知")
        msg_id = data.get("message_id")

        bound = self._is_bound_group(card, group_id)
        if not bound:
            # 未绑定群：仅提示一次，不做互通
            self._reply_group(adapter, group_id, "本群尚未绑定服务器，无法使用互通功能。")
            return

        # 管理指令：/cmd
        if content.startswith(_CMD_PREFIX):
            if not self._is_admin(card, user_id):
                self._reply_group(adapter, group_id, "⚠️ 你没有权限执行此指令")
                return
            cmd = content[1:].strip()
            if cmd:
                self._log.info(f"[OneBot互通] 管理员 {sender_name} 执行指令：{cmd}")
                self._dispatch_cmd(cmd)
                self._reply_group(adapter, group_id, f"✅ 已执行指令：{cmd}")
            return

        # 自定义关键词
        keyword_reply = self._keyword_reply(card, content)
        if keyword_reply:
            self._reply_group(adapter, group_id, keyword_reply)
            return

        # 状态查询
        if any(k in content for k in _QQ_TO_MC_HINTS):
            self._reply_group(adapter, group_id, self._status_text(card))
            return

        # 群服互通：QQ -> MC（广播到本服 + 跨服）
        banned = self._filter_banned(card, content)
        if banned is None:
            return
        if card.get("sync", {}).get("qq_to_mc", True):
            fmt = card.get("sync", {}).get("qq_to_mc_format", "[QQ] %s：%s")
            gname = str(group_id)
            self._broadcast_game(fmt % (gname, banned))
            self._cross_relay(gname, banned)

    def _handle_private(self, card: dict[str, Any], adapter, data: dict[str, Any]) -> None:
        content = _extract_text(data)
        if not content:
            return
        user_id = data.get("user_id")
        if content.startswith(_CMD_PREFIX):
            if not self._is_admin(card, user_id):
                self._reply_private(adapter, user_id, "⚠️ 你没有权限执行此指令")
                return
            cmd = content[1:].strip()
            if cmd:
                self._dispatch_cmd(cmd)
                self._reply_private(adapter, user_id, f"✅ 已执行指令：{cmd}")

    # ------------------------------------------------------------- 权限/群
    def _is_bound_group(self, card: dict[str, Any], group_id) -> bool:
        if group_id in (None, ""):
            return False
        groups = self.hub.manager.parse_groups(card.get("main_group"))
        return str(group_id) in {str(g) for g in groups}

    def _is_admin(self, card: dict[str, Any], user_id) -> bool:
        if user_id in (None, ""):
            return False
        admins = self.hub.manager.parse_groups_loose(card.get("admin_qq"))
        return str(user_id) in set(admins)

    def _filter_banned(self, card: dict[str, Any], content: str) -> str | None:
        text = content
        syn = card.get("sync") or {}
        banned = syn.get("banned_words") or []
        for word in banned:
            if word and str(word) in text:
                return None
        max_len = int(syn.get("max_message_length", 256) or 256)
        if max_len > 0 and len(text) > max_len:
            text = text[:max_len]
        return text

    @staticmethod
    def _expand(template: str, players: list[str]) -> str:
        try:
            return template.replace("{count}", str(len(players))) \
                .replace("{players}", ", ".join(players) if players else "无")
        except Exception:
            return template

    def _keyword_reply(self, card: dict[str, Any], content: str) -> str | None:
        syn = card.get("sync") or {}
        ck = syn.get("custom_keywords") or {}
        if not isinstance(ck, dict):
            return None
        players = self._online_players()
        for kw in ck:
            if kw and str(kw) in content:
                return self._expand(str(ck[kw]), players)
        return None

    def _status_text(self, card: dict[str, Any]) -> str:
        players = self._online_players()
        ip = ""
        try:
            wc = self.plugin.web_config or {}
            ip = str(wc.get("server_ip", "") or "") or ""
        except Exception:
            pass
        version = ""
        try:
            version = str(self.plugin.server.get_minecraft_version()) or ""
        except Exception:
            pass
        lines = [
            f"在线 {len(players)} 人：" + (", ".join(players) if players else "无"),
        ]
        if ip:
            lines.append(f"地址：{ip}")
        if version:
            lines.append(f"版本：{version}")
        return "\n".join(lines)

    def _online_players(self) -> list[str]:
        try:
            players = self.plugin._cache.get("players") or []
            names = []
            for p in players:
                if isinstance(p, dict):
                    name = str(p.get("name") or "")
                else:
                    name = str(p)
                if name:
                    names.append(name)
            return names
        except Exception:
            return []

    def _cross_relay(self, group_name: str, text: str) -> None:
        try:
            cross = getattr(self.plugin, "cross", None)
            if cross is not None and cross.enabled() and text:
                cross.send_qq_relay(group_name, text)
        except Exception as e:
            self._log.warning(f"[OneBot互通] 跨服推送失败: {e}")

    # ------------------------------------------------------------- 回复
    def _reply_group(self, adapter, group_id, text: str) -> None:
        if group_id in (None, ""):
            return
        try:
            adapter.send_group_msg(int(group_id), str(text))
        except Exception as e:
            self._log.warning(f"[OneBot互通] 群回复失败: {e}")

    def _reply_private(self, adapter, user_id, text: str) -> None:
        if user_id in (None, ""):
            return
        try:
            adapter.send_private_msg(int(user_id), str(text))
        except Exception as e:
            self._log.warning(f"[OneBot互通] 私聊回复失败: {e}")