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

# 常见 CQ 码。拼接成一条正则一次扫完，比逐个 replace 快且不会误伤。
# 形如 [CQ:at,qq=123]、[CQ:image,file=xxx.jpg,url=http://...]
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")
# CQ 码里各字段的 key=value（value 允许含逗号，所以按 key 名切分）
_CQ_KV_RE = re.compile(r"(?:^|,)([a-zA-Z_]+)=([^,]*)")


def _parse_cq(raw: str) -> list[dict[str, Any]]:
    """把 raw_message 里的 CQ 码解析成 OneBot 的 message 段数组。

    为什么需要它：很多实现（含老版本 go-cqhttp / 部分 NapCat 配置）
    只下发 ``raw_message`` 而**不**下发结构化的 ``message`` 数组。
    原实现遇到这种情况直接把整串 raw 文本返回，于是
    ``[CQ:at,qq=123] 你好`` 会原样进游戏、进关键词匹配 ——
    用户看到的是「聊天里混着 [CQ:at,qq=...] 这种奇怪东西」，
    而真正有用的文本被 CQ 码污染，关键词匹配自然全部失效。
    """
    out: list[dict[str, Any]] = []
    pos = 0
    for m in _CQ_RE.finditer(raw):
        if m.start() > pos:
            plain = raw[pos:m.start()]
            if plain:
                out.append({"type": "text", "data": {"text": plain}})
        ctype = m.group(1)
        data: dict[str, Any] = {}
        for k, v in _CQ_KV_RE.findall(m.group(2) or ""):
            # CQ 码里实体是 HTML 转义的，这里做一次反转义
            data[k] = (v.replace("&#44;", ",").replace("&#91;", "[")
                        .replace("&#93;", "]").replace("&amp;", "&"))
        out.append({"type": ctype, "data": data})
        pos = m.end()
    if pos < len(raw):
        tail = raw[pos:]
        if tail:
            out.append({"type": "text", "data": {"text": tail}})
    return out


def _extract_text(data: dict[str, Any]) -> str:
    """从 OneBot message 段或 raw_message 抽取纯文本。"""
    segments = data.get("message")
    if not isinstance(segments, list):
        # ★ 没有结构化 message 时，别直接把 raw_message 当纯文本用，
        # 先尝试解析出 CQ 码再走同一套抽取逻辑。
        raw = data.get("raw_message")
        if isinstance(raw, str) and "[CQ:" in raw:
            segments = _parse_cq(raw)
            if segments:
                data = dict(data)
                data["message"] = segments
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
            elif stype == "image":
                # 图片要有占位符，否则「发图配字」的消息在游戏里
                # 会变成一段看不出有图的纯文字，用户以为图丢了
                chunks.append("[图片]")
        text = "".join(chunks).strip()
        if text:
            return text
    raw = data.get("raw_message")
    if isinstance(raw, str):
        # 兜底路径也要把 CQ 码清掉，绝不把 [CQ:xxx] 原样送进游戏
        return _CQ_RE.sub("", raw).strip()
    return ""


class OneBotInterop:
    """把 OneBot 群/私聊消息路由到互通链路，并按来源适配器回复。"""

    # 同一个未绑定群多久之内只提示一次（秒）
    _UNBOUND_HINT_COOLDOWN = 600.0

    def __init__(self, plugin, event_bus, hub, *, register: bool = True) -> None:
        self.plugin = plugin
        self.bus = event_bus
        self.hub = hub
        self._log = plugin.logger
        self._registered = False
        # 未绑定群提示冷却表：key = "card_id:group_id" → 上次提示时间
        self._unbound_hint: dict[str, float] = {}
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
            post_type = str(data.get("post_type") or "")
            # ★ 不再把非 message 的包直接扔掉。
            # 原实现开头就是 ``if post_type != "message": return``，
            # 于是 notice（群成员增减、撤回、戳一戳）和 request（加群/加好友申请）
            # 这两类事件**永远到不了任何订阅者**，子插件里注册的
            # ``gm.bot.on_notice`` / ``on_request`` 形同虚设。
            # 现在把它们派发到总线，由订阅者按需处理；message 仍走原路径。
            if post_type in ("notice", "request", "meta_event"):
                self._dispatch_event(post_type, data)
                return
            if post_type != "message":
                # 未知 post_type：也派发出去，别静默吞掉
                self._dispatch_event(post_type or "unknown", data)
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

    def _dispatch_event(self, kind: str, data: dict[str, Any]) -> None:
        """把 notice / request / meta_event 派发给总线订阅者。"""
        try:
            adapter = self._resolve_adapter(data)
            card = self._card_of(adapter) if adapter is not None else None
            try:
                self.bus.emit(f"onebot.{kind}", {"event": data, "adapter": adapter, "card": card})
            except Exception:
                pass
            # 再走一次统一事件名，方便只订阅一处就能收全部
            try:
                self.bus.emit("onebot.event", {"kind": kind, "event": data,
                                               "adapter": adapter, "card": card})
            except Exception:
                pass
        except Exception as e:
            self._log.warning(f"[OneBot互通] 派发 {kind} 事件失败: {e}")

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
                return
            except Exception:
                pass
            # broadcast(message, permission) 需要**两个**参数。
            # 原来只传 msg，必然抛 TypeError —— 兜底等于没有兜底。
            try:
                self.plugin.server.broadcast(msg, "endstone.broadcast.user")
                return
            except Exception:
                pass
            # 最后退回逐玩家发送
            try:
                for p in list(self.plugin.server.online_players):
                    try:
                        p.send_message(msg)
                    except Exception:
                        pass
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
            # 未绑定群：不互通。
            #
            # ⚠ 注释里一直写着「仅提示一次」，但代码里**没有任何去重**，
            # 于是群里每来一条消息就回一遍「本群尚未绑定服务器」——
            # 一个活跃群几分钟内就能把机器人刷成复读机，
            # 而且这些回复是**主动消息**，直接消耗官方「单群 1000 条/天」的额度。
            # 现在按 (卡片, 群) 做一个冷却窗口，窗口内只提示一次。
            gkey = f"{card.get('id') or ''}:{group_id}"
            now = time.time()
            if now - self._unbound_hint.get(gkey, 0.0) >= self._UNBOUND_HINT_COOLDOWN:
                self._unbound_hint[gkey] = now
                # 顺手清理过期项，避免长期运行下无界增长
                if len(self._unbound_hint) > 512:
                    for k, ts in list(self._unbound_hint.items()):
                        if now - ts > self._UNBOUND_HINT_COOLDOWN:
                            self._unbound_hint.pop(k, None)
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

        # ---- 正则触发引擎（普通消息）----
        #
        # ⚠ 与 qqbot.py 同一个道理：位置必须晚于「已绑定群判定」和
        #    `/cmd` 管理员校验，否则规则里的 executeCommand 会变成
        #    任何群成员都能用的提权入口，未绑定群也能白嫖指令执行。
        #    放在这里则只能匹配「过完权限校验、还没当普通聊天转发」的内容。
        #
        #    block=True 的规则返回 True → 这条消息到此为止。
        #
        # ★ 用途闸门（与 qqbot.py 严格对齐）。
        #   ``/cmd`` 及以上属于"管理机器人本身"，任何用途的卡都该能用
        #   （否则一张「空着」的卡连管理员指令都不通，第三方插件没法调试）。
        #   而正则 / 关键词 / 状态查询 / QQ→MC 属于**群服互通业务**，
        #   只有 purpose == bridge 的卡才做。
        #   ⚠ 这个判断必须两处一致：官方卡和 OneBot 卡各走一条代码路径，
        #     只改一处的典型症状是"官方机器人听话了，个人号还在乱转发"。
        _relay_ok = True
        try:
            # ``_card_of`` 用的是 ``conn_mgr.get_view()``，返回的就是配置真源，
            # 所以直接读 card["purpose"] 即可 —— 不必再经 conn_mgr 拿一次锁。
            # 只有字段缺失（老配置刚升级、还没走 _normalize）时才回查一次。
            _raw = card.get("purpose")
            if isinstance(_raw, str) and _raw:
                _relay_ok = _raw == "bridge"
            else:
                _cid = str(card.get("id") or "")
                _mgr = getattr(self.plugin, "conn_mgr", None)
                if _cid and _mgr is not None:
                    _relay_ok = str(_mgr.purpose_of(_cid) or "bridge") == "bridge"
        except Exception:
            # 取不到用途时按"可以互通"处理 —— 与 hub._gateway_purpose 的
            # 兜底策略一致：静默停止转发比多发一条消息严重得多。
            _relay_ok = True

        if _relay_ok:
          try:
            engine = getattr(self.plugin, "regex_engine", None)
            if engine is not None:
                pack = {
                    "card_id": str(card.get("id") or ""),
                    "group_id": group_id,
                    "user_id": user_id,
                    "message_id": msg_id,
                    "message": data.get("message"),
                    "raw_message": content,
                    "time": data.get("time") or int(time.time()),
                    "self_id": str(data.get("self_id", "") or ""),
                    "domain": "qq",
                    "sender": {
                        "nickname": sender_name,
                        "role": "admin" if self._is_admin(card, user_id) else "member",
                    },
                }
                if engine.handle_message(content, pack):
                    return
          except Exception as e:
            self._log.warning(f"[OneBot互通] 正则引擎处理失败: {e}")

        if not _relay_ok:
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
            # 群名：OneBot 侧能从 card 的群列表里查到名字，查不到才用 id。
            # 旧实现直接 str(group_id)，于是游戏里永远是一串数字。
            gname = self._group_display_name(card, group_id)
            from .. import qqrelay
            text = qqrelay.format_relay(
                card.get("sync", {}).get("qq_to_mc_format") or qqrelay.DEFAULT_FORMAT,
                sender_name, gname, banned,
            )
            self._broadcast_game(text)
            self._cross_relay(sender_name, gname, banned)

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

    def _group_display_name(self, card: dict[str, Any], group_id) -> str:
        """取群名用于显示。取不到返回空串，交给上层兜底。

        OneBot 协议确实有 ``get_group_list`` 能拿群名，但它是**异步回调**
        接口，而这里处在同步的消息处理路径上 —— 为了填一个显示名去发
        请求并等回调，只会把消息延迟拖长、还要处理超时与失败。
        所以走本地配置：卡片里存过 ``groups[].name``，直接查。

        ⚠ 为什么不再回退群号
        --------------------
        旧实现查不到就 ``return gid``（群号）。看起来"至少能看出是哪个
        群"，但 OneBot 的 ``group_id`` 常常也是一串数字/长标识 ——
        用户看到的还是「昵称@123456789：内容」这种，观感上跟没修一样。
        而且 ``qqrelay.format_relay`` 已经会把空缺的群名填成「群」，
        这里再塞一个群号进去，等于绕过了那层兜底。所以统一返回空串。

        ⚠ 数据源必须是 ``conn_mgr.adapters``
        -----------------------------------
        卡片列表存在 ``ConnectionManager.adapters``，**不是**
        ``plugin.web_config["qq_bots"]`` —— 后者这个键根本不存在
        （以前这里读的就是它，``get`` 一路返回 None，整段查表代码
        从未生效过）。这类"读了不存在的键"的 bug 不会报错，
        只是**静默不工作**，所以宁可写死真实位置。
        """
        gid = str(group_id if group_id is not None else "").strip()
        if not gid:
            return ""

        def _look(groups):
            for g in (groups or []):
                if not isinstance(g, dict):
                    continue
                # ★ 群标识的字段名在不同卡片类型里不一样：
                #   * 官方 QQ 卡 → ``openid``（群 openid）
                #   * OneBot / websocket 卡 → ``group_id``（数字群号）
                # 原实现只看 ``openid``，于是 websocket 卡片永远查不到群名，
                # 游戏里一直显示兜底占位。这里两个都认，并且和 gid 做
                # 字符串比较（一方可能是 int）。
                ids = [str(g.get("openid", "") or ""), str(g.get("group_id", "") or "")]
                if gid not in ids:
                    continue
                nm = str(g.get("name") or "").strip()
                # 名字等于标识本身 → 当初就没填名字，不算有效群名。
                if nm and nm not in ids:
                    return nm
            return ""

        try:
            # 1) 全局卡片表（权威来源）
            mgr = getattr(self.plugin, "conn_mgr", None)
            adapters = getattr(mgr, "adapters", None)
            if isinstance(adapters, list):
                for c in adapters:
                    if isinstance(c, dict):
                        nm = _look(c.get("groups"))
                        if nm:
                            return nm
            # 2) 兜底：调用方自己传进来的 card
            nm = _look(card.get("groups") if isinstance(card, dict) else None)
            if nm:
                return nm
        except Exception:
            pass
        return ""

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
            # 正确属性是 server.minecraft_version（是**属性**不是方法）。
            # 原写法 server.get_minecraft_version() 在 Endstone 里不存在，
            # AttributeError 被吞掉后「版本：」一行永远不显示。
            srv = self.plugin.server
            version = str(getattr(srv, "minecraft_version", "") or "") or ""
            if not version:
                version = str(getattr(srv, "version", "") or "") or ""
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

    def _cross_relay(self, sender_name: str, group_name: str, text: str) -> None:
        """把一条群消息推到跨服聊天。

        ``sender_name`` 是发言人昵称（跨服那边要显示「谁说的」），
        ``group_name`` 是群名（附加信息）。旧签名只有 ``(group_name, text)``，
        把群名填进了玩家的位置 —— 那会显示成「群名 说：内容」。
        """
        try:
            cross = getattr(self.plugin, "cross", None)
            if cross is not None and cross.enabled() and text:
                # send_qq_relay 自身兼容新旧两种参数形态，这里直接按新形态调
                cross.send_qq_relay(sender_name, group_name, text)
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