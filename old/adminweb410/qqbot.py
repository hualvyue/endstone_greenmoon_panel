# QQ 机器人子系统实现（网关 / API / 限流器）。
import asyncio
import collections
import json
import re
import secrets
import threading
import time
import traceback

from .core import DEFAULT_QQBOT_CONFIG, _to_slot


class _TokenBucket(object):

    def __init__(self, rate_per_sec, burst):
        self.rate = max(0.0, float(rate_per_sec))
        self.burst = max(0.0, float(burst))
        self.capacity = self.burst if self.burst > 0 else self.rate
        self.tokens = self.capacity
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def acquire(self, cost=1.0):
        if cost <= 0:
            return True
        with self._lock:
            if self.rate <= 0.0:
                return False
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
            self._last = now
            if self.tokens >= cost:
                self.tokens -= cost
                return True
            return False

class _GatewayLimiter(object):

    def __init__(self, send_rate_per_min, send_burst, recv_rate_per_sec, recv_burst, max_bytes_per_sec):
        self.sender_messages = _TokenBucket(float(send_rate_per_min) / 60.0, float(send_burst))
        self.receiver_messages = _TokenBucket(float(recv_rate_per_sec), float(recv_burst))
        self.bytes_bucket = _TokenBucket(float(max_bytes_per_sec), float(max_bytes_per_sec))

    def allow_send(self, cost_bytes=1):
        return self.sender_messages.acquire(1.0) and self.bytes_bucket.acquire(max(1, int(cost_bytes)))

    def allow_recv(self, cost_bytes=1):
        return self.receiver_messages.acquire(1.0) and self.bytes_bucket.acquire(max(1, int(cost_bytes)))

class QQBotAPI(object):

    def __init__(self, config, requests_module):
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")
        self.group_openid = config.get("group_openid", "")
        self.qq_api, self.token_api, self.msg_api = self._resolve_urls(config)
        self.access_token = ""
        self.token_expire = 0
        self.logger = None
        self.http = requests_module.Session()

    def _resolve_urls(self, config):
        env = str(config.get("env", "formal")).strip().lower()
        if env == "sandbox":
            qq = "https://sandbox.api.sgroup.qq.com"
            tok = "https://sandbox.bots.qq.com"
            msg = "https://sandbox.api.sgroup.qq.com"
        else:
            qq = "https://api.bot.qq.com"
            tok = "https://bots.qq.com"
            msg = "https://api.sgroup.qq.com"
        qq = str(config.get("qq_api", "") or "").strip() or qq
        tok = str(config.get("qq_token_api", "") or "").strip() or tok
        msg = str(config.get("qq_msg_api", "") or "").strip() or msg
        return qq, tok, msg

    def _log(self, level, message):
        if self.logger is None:
            return
        try:
            if str(message).startswith("[调试-"):
                p = getattr(self, "plugin", None)
                if p is not None:
                    try:
                        if not p._diag_verbose():
                            return
                    except Exception:
                        pass
            getattr(self.logger, level)(message)
        except Exception:
            pass

    def get_access_token(self, force=False):
        if not force and self.access_token and time.time() < self.token_expire - 60:
            return self.access_token
        url = f"{self.token_api}/app/getAppAccessToken"
        self._log("info", f"[调试-TOKEN] 请求 URL: {url}")
        self._log("info", f"[调试-TOKEN] AppID 前缀: {str(self.app_id)[:12]}...")
        try:
            resp = self.http.post(
                url,
                json={"appId": self.app_id, "clientSecret": self.app_secret},
                timeout=15,
            )
        except Exception as e:
            self._log("error", f"[调试-TOKEN] 网络异常: {e}")
            raise
        self._log("info", f"[调试-TOKEN] 响应码: {resp.status_code}")
        body = resp.text[:300]
        if self.app_secret:
            body = body.replace(self.app_secret, "****")
        self._log("info", f"[调试-TOKEN] 响应内容: {body}")
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data.get("access_token", "")
        self.token_expire = time.time() + int(data.get("expires_in", 7200))
        return self.access_token

    def get_gateway_url(self):
        url = f"{self.qq_api}/gateway"
        headers = {"Authorization": f"QQBot {self.get_access_token()}"}
        auth_short = headers["Authorization"][:30] if len(headers["Authorization"]) > 30 else headers["Authorization"]
        self._log("info", f"[调试-GATEWAY] 请求 URL: {url}")
        self._log("info", f"[调试-GATEWAY] Authorization: {auth_short}...")
        try:
            resp = self.http.get(url, headers=headers, timeout=10)
        except Exception as e:
            self._log("error", f"[调试-GATEWAY] 网络异常: {e}")
            raise
        self._log("info", f"[调试-GATEWAY] 响应码: {resp.status_code}")
        self._log("info", f"[调试-GATEWAY] 响应内容: {resp.text[:300]}")
        resp.raise_for_status()
        data = resp.json()
        if "url" not in data:
            self._log("error", f"[调试-GATEWAY] 响应缺少 url 字段: {data}")
            raise RuntimeError(f"网关响应缺少 url 字段: {data}")
        return data["url"]

    def test_token_validity(self):
        try:
            token = self.get_access_token(force=True)
            if not token:
                return False, "Token 为空，请检查 AppID/AppSecret"
            url = f"{self.qq_api}/gateway"
            headers = {"Authorization": f"QQBot {token}"}
            resp = self.http.get(url, headers=headers, timeout=10)
            if resp.status_code == 401:
                return False, "Token 无效（HTTP 401），请检查 AppID/AppSecret"
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    wss = data.get("url", "")
                    return True, f"Token 有效，网关: {wss or '(无 url 字段)'}"
                except Exception:
                    return True, "Token 有效（HTTP 200）"
            return False, f"未知状态码 {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            return False, f"网络异常: {e}"

    def send_message(self, message, msg_id=""):
        return self.send_message_to(self.group_openid, message, msg_id)

    def send_message_to(self, group_openid, message, msg_id=""):
        target = str(group_openid or "").strip()
        if not target:
            raise RuntimeError("未绑定任何群，无法发送消息")

        url = f"{self.msg_api}/v2/groups/{target}/messages".replace(
            "api.bot.qq.com", "api.sgroup.qq.com"
        )
        content = str(message.get("content", "") or "")
        if not content.strip():
            raise RuntimeError("不允许发送空消息")

        payload = {"msg_type": 0, "content": content}
        if msg_id:
            payload["msg_id"] = msg_id
        try:
            resp = self.http.post(
                url,
                json=payload,
                headers={"Authorization": f"QQBot {self.get_access_token()}", "Content-Type": "application/json"},
                timeout=20,
            )
            resp.raise_for_status()
        except Exception as e:

            body = getattr(e, "response", None)
            if body is not None:
                try:

                    _log = getattr(self, "logger", None)
                    if _log is not None:
                        _log.error(f"[QQ] 发送响应详情: {body.text}")
                except Exception:
                    pass
            raise
        return resp.json()

class QQBotGateway(object):
                                                           

                                           
                                                                                                                     
                                                                                                                    
                                                                                                                     
                                                                                                   
       

    def __init__(self, plugin, config, requests_mod, websockets_mod):
        self.plugin = plugin
        self.logger = plugin.logger
        self.server = plugin.server
        self.config = dict(DEFAULT_QQBOT_CONFIG)
        if isinstance(config, dict):
            self.config.update(config)
        self.requests = requests_mod
        self.websockets = websockets_mod

        self._stop = threading.Event()
        self._supervisor_stop = threading.Event()
        self._loop = None
        self._worker_thread = None
        self._supervisor_thread = None
        self._restart_count = 0
        self._connected = threading.Event()
        self._api = None
        self.binding_code = None

        self.limiter = _GatewayLimiter(
            self.config.get("send_rate_per_min", 30),
            self.config.get("send_burst", 10),
            self.config.get("recv_rate_per_sec", 5),
            self.config.get("recv_burst", 8),
            self.config.get("max_bytes_per_sec", 256 * 1024),
        )
        self._send_lock = threading.Lock()
        self._send_queue = collections.deque(maxlen=200)
        self._reply_targets = {}
        self._auth_fail_streak = 0

    _AUTH_CLOSE_CODES = (4004, 4013, 4014)

    def _is_auth_failure(self, exc):

        try:
            rcvd = getattr(exc, "rcvd", None)
            if rcvd is not None:
                code = getattr(rcvd, "code", None)
                if isinstance(code, int):
                    return code in self._AUTH_CLOSE_CODES
        except Exception:
            pass

        s = str(exc) or ""
        return "4004" in s or "authentication" in s.lower()

    def _auth_fail_threshold(self):
        try:
            return max(1, int(self.config.get("auth_fail_refresh_threshold", 3)))
        except Exception:
            return 3

    def start(self):
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            return
        self._supervisor_stop.clear()
        self._stop.clear()
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop, daemon=True, name="qqbot-supervisor"
        )
        self._supervisor_thread.start()

    def stop(self):
        self._supervisor_stop.set()
        self._stop.set()

        for th in (self._worker_thread, self._supervisor_thread):
            if th and th.is_alive() and th is not threading.current_thread():
                try:
                    th.join(timeout=3)
                except Exception:
                    pass
        self.logger.info("[QQ] 网关已停止")

    def _next_backoff(self):
        base = float(self.config.get("reconnect_seconds", 5))
        cap = float(self.config.get("reconnect_seconds_max", 120))
        self._restart_count += 1
        return min(cap, base * (2 ** min(self._restart_count - 1, 5)))

    def _supervisor_loop(self):
        self.logger.info("[QQ] 网关监督线程已启动")
        while not self._supervisor_stop.is_set():
            if self._worker_thread and self._worker_thread.is_alive():
                self._supervisor_stop.wait(1.0)
                continue
            if self._supervisor_stop.is_set():
                break
            delay = self._next_backoff()
            self.logger.warning(f"[QQ] 网关工作线程已退出，{delay:.1f} 秒后自动重启")
            self._supervisor_stop.wait(delay)
            if self._supervisor_stop.is_set():
                break
            try:
                self._launch_worker()
            except Exception as e:
                self.logger.error(f"[QQ] 启动网关工作线程失败: {e}")

    def _launch_worker(self):
        self._stop.clear()
        self._worker_thread = threading.Thread(target=self._worker_run, daemon=True, name="qqbot-worker")
        self._worker_thread.start()

    def _worker_run(self):
        self._restart_count = 0
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._run())
        except Exception as e:
            self.logger.error(f"[QQ] 网关 asyncio 循环异常: {e}")
            traceback.print_exc()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            self._connected.clear()

    async def _run(self):
        self._api = QQBotAPI(self.config, self.requests)
        try:
            self._api.logger = self.logger
            self._api.plugin = self.plugin
        except Exception:
            pass
        try:
            await asyncio.to_thread(self._api.get_access_token)
            self.logger.info("[QQ] access_token 获取成功")
        except Exception as e:
            self.logger.error(f"[QQ] 获取 access_token 失败: {e}")
            return
        try:
            gateway_url = await asyncio.to_thread(self._api.get_gateway_url)
        except Exception as e:
            self.logger.error(f"[QQ] 获取 Gateway URL 失败: {e}")
            return

        self._auth_fail_streak = 0

        while not self._stop.is_set():
            try:
                async with self.websockets.connect(gateway_url, ping_interval=None) as ws:
                    self._connected.set()
                    self.logger.info("[QQ] WebSocket 已连接")
                    if not str(self.config.get("group_openid", "")).strip():
                        self.binding_code = self._generate_binding_code()
                        self.logger.warning(
                            f"[QQ] 尚未绑定工作群。请在任意邀请本机器人的群里发送绑定码 {self.binding_code} 完成自动绑定"
                        )

                    try:
                        first_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        first_pkt = json.loads(first_raw)
                        await self._handle_packet(first_pkt, ws)
                    except asyncio.TimeoutError:
                        self.logger.warning("[QQ] 等待网关 Hello 超时，将直接发送 Identify")
                    except Exception as e:
                        self.logger.warning(f"[QQ] 读取网关 Hello 失败: {e}")

                    await ws.send(
                        json.dumps(
                            {
                                "op": 2,
                                "d": {
                                    "token": f"QQBot {self._api.access_token}",
                                    "intents": (1 << 25) | (1 << 24),
                                },
                            }
                        )
                    )

                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        if self._stop.is_set():
                            break
                        if not self.limiter.allow_recv(len(raw or b"")):
                            self.logger.warning("[QQ] 入站达到限流阈值，丢弃一条消息")
                            continue
                        try:
                            packet = json.loads(raw)
                        except Exception as e:
                            self.logger.warning(f"[QQ] 解析消息失败: {e}")
                            continue
                        await self._handle_packet(packet, ws)
            except self.websockets.ConnectionClosed as e:
                self._connected.clear()
                self.logger.warning(f"[QQ] 连接断开，稍后重连: {e}")
                if self._is_auth_failure(e):

                    self.logger.warning("[QQ] 检测到鉴权失败，强制刷新 access_token 并重取 Gateway URL")
                    try:
                        await asyncio.to_thread(self._api.get_access_token, True)
                        gateway_url = await asyncio.to_thread(self._api.get_gateway_url)
                        self.logger.info("[QQ] 已刷新 access_token 与 Gateway URL")
                    except Exception as e2:
                        self.logger.error(f"[QQ] 刷新 access_token 失败: {e2}")
                else:

                    self._auth_fail_streak = 0
            except Exception as e:
                self._connected.clear()
                self._auth_fail_streak = 0
                self.logger.error(f"[QQ] 网关异常: {e}")
            if self._stop.is_set():
                break
            try:
                await asyncio.sleep(float(self.config.get("reconnect_seconds", 5)))
            except Exception:
                await asyncio.sleep(5)

    async def _handle_packet(self, packet, ws):
        try:
            op = packet.get("op")
            if op == 10:
                try:
                    interval = float(packet["d"]["heartbeat_interval"]) / 1000.0
                except Exception:
                    interval = 40.0
                asyncio.ensure_future(self._heartbeat(ws, interval))
            elif op == 0:
                t = packet.get("t")
                if t == "READY":
                    self._auth_fail_streak = 0
                    self.logger.info("[QQ] 机器人已就绪")
                    try:
                        self.plugin._notify_server_start_once()
                    except Exception as exc:
                        self.logger.warning(f"[QQ] 触发上线播报失败: {exc}")
                elif t in ("GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
                    await self._handle_group_message(packet.get("d") or {})
        except Exception as e:
            self.logger.warning(f"[QQ] 处理消息包失败: {e}")

    async def _heartbeat(self, ws, interval):
        try:
            while not self._stop.is_set():
                await asyncio.sleep(max(1.0, interval))
                try:
                    await ws.send(json.dumps({"op": 1, "d": None}))
                except Exception:
                    break
        except Exception:
            pass

    async def _handle_group_message(self, data):
        try:
            content = str(data.get("content", "") or "").strip()
            if not content:
                return
            sender = data.get("author") or {}
            sender_name = str(sender.get("member_name") or sender.get("username") or "未知")
            sender_openid = str(sender.get("member_openid") or "")
            group_openid = str(data.get("group_openid") or data.get("group_id") or "")
            msg_id = str(data.get("id") or "")
            if group_openid and msg_id:
                self._reply_targets[msg_id] = group_openid
                if len(self._reply_targets) > 200:
                    try:
                        keys = list(self._reply_targets.keys())
                        for k in keys[:-100]:
                            self._reply_targets.pop(k, None)
                    except Exception:
                        pass

            bound_ids = set(g["openid"] for g in self._bound_groups())
            is_bound = bool(group_openid) and group_openid in bound_ids

            if not is_bound:
                pending = self.config.get("pending_group_binds") or []
                code_is_pending = group_openid and bool(pending) and content == str(pending[0].get("code", ""))
                if code_is_pending:
                    if not str(self.config.get("group_openid", "") or "").strip():
                        await asyncio.to_thread(self._complete_binding, group_openid)
                        await self._send_qq_text("✅ 主群绑定成功！可发送「设置群名 名字」为本群命名", msg_id)
                    else:
                        if self._add_extra_group(group_openid, group_openid):
                            await self._send_qq_text("✅ 已加入互联群！可发送「设置群名 名字」为本群命名", msg_id)
                        else:
                            await self._send_qq_text("⚠️ 该群已在互联列表中", msg_id)
                    self._consume_pending_code()
                    return
                if group_openid and content == (self.binding_code or ""):
                    await asyncio.to_thread(self._complete_binding, group_openid)
                    await self._send_qq_text("✅ 绑定成功！已将本群设为机器人的主群。", msg_id)
                    return
                code = None
                try:
                    code = (pending[0].get("code") if pending else self.binding_code) or None
                except Exception:
                    code = self.binding_code
                hint = "本群尚未绑定服务器。请在服务器 Web 后台「QQ机器人」的「群管理」点击「加入新群」，"
                hint += f"获取 4 位验证码后在本群发送即可绑定（示例：{code or '0000'}）。"
                await self._send_qq_text(hint, msg_id)
                return

            try:
                self._ensure_extra_group_registered(group_openid)
            except Exception:
                pass

            try:
                pl = self.plugin
                bind_cfg = pl._load_tools_config().get("binding", {}) or {}
                digits = int(bind_cfg.get("code_digits", 5) or 5)
                if (bind_cfg.get("enabled", True) and getattr(pl, "qq_bot", None) is not None
                        and sender_openid and content.isdigit() and len(content) == digits):
                    ok, msg = await asyncio.to_thread(pl._try_bind, sender_openid, content)
                    await self._send_qq_text(msg, msg_id)
                    return
            except Exception as e:
                self.logger.warning(f"[QQ] 处理账号绑定码失败: {e}")

            try:
                pl = self.plugin
                signin_cfg = pl._signin_config()
                cmd = str(signin_cfg.get("command", "签到") or "签到").strip()
                if signin_cfg.get("enabled", True) and sender_openid and content == cmd:
                    ok, msg = await asyncio.to_thread(pl._do_signin, sender_openid, sender_name)
                    await self._send_qq_text(msg, msg_id)
                    return
            except Exception as e:
                self.logger.warning(f"[QQ] 处理签到失败: {e}")

            if content.startswith("设置群名"):
                part = content[len("设置群名"):].strip()
                if part:
                    self._rename_group(group_openid, part)
                    await self._send_qq_text(f"✅ 本群已命名为：{part}", msg_id)
                else:
                    await self._send_qq_text("用法：设置群名 你的群名字", msg_id)
                return

            if content.startswith("/"):
                if sender_openid not in self.config.get("group_admins", []):
                    await self._send_qq_text("⚠️ 你没有权限执行此指令", msg_id)
                    return
                cmd = content[1:].strip()
                if cmd:
                    self.logger.info(f"[QQ] 管理员 {sender_name} 执行指令：{cmd}")
                    self._run_on_server(lambda c=cmd: self.server.dispatch_command(self.server.command_sender, c))
                    await self._send_qq_text(f"✅ 已执行指令：{cmd}", msg_id)
                return

            ck = self.config.get("custom_keywords") or {}
            if isinstance(ck, dict):
                for kw in ck:
                    if kw and str(kw) in content:
                        await self._send_qq_text(await self._expand_keyword_reply(ck[kw]), msg_id)
                        return

            if any(k in content for k in ("查状态", "服务器状态", "状态", "在线", "tps")):
                await self._send_qq_text(await self._get_status_text(), msg_id)
                return

            content = self._filter_banned(content)
            if self.config.get("qq_to_mc", True):
                fmt = self.config.get("qq_to_mc_format", "[QQ] %s：%s")
                gname = self._group_display_name(group_openid) or sender_name
                self._broadcast_game(fmt % (gname, content))
                cross = getattr(self.plugin, "cross", None)
                if cross is not None and cross.enabled():
                    try:
                        cross.send_qq_relay(gname, content)
                    except Exception:
                        pass
        except Exception as e:
            self.logger.warning(f"[QQ] 处理群消息失败: {e}")

    @staticmethod
    def _generate_binding_code():
        return f"{secrets.randbelow(10000):04d}"

    def _complete_binding(self, group_openid):
        if not group_openid or str(group_openid) == str(self.config.get("group_openid", "")):
            return
        self.config["group_openid"] = str(group_openid)
        if getattr(self, "_api", None) is not None:
            try:
                self._api.group_openid = str(group_openid)
            except Exception:
                pass
        try:
            self.plugin._qqbot_persist_group_openid(group_openid)
        except Exception as e:
            self.logger.warning(f"[QQ] 持久化绑定群出错: {e}")
        self.logger.info(f"[QQ] 已绑定群 openid: {group_openid}")

        try:
            self.plugin._qq_start_notified = False
        except Exception:
            pass

        threading.Thread(
            target=lambda: (time.sleep(1.5), self.plugin._restart_qq_gateway()), daemon=True
        ).start()

    def _add_extra_group(self, group_openid, name):
        oid = str(group_openid or "").strip()
        if not oid:
            return False
        groups = self.config.get("groups") or []
        if not isinstance(groups, list):
            groups = []
        bound_ids = set()
        for g in groups:
            if isinstance(g, dict) and str(g.get("openid", "") or "").strip():
                bound_ids.add(str(g["openid"]).strip())
        if oid in bound_ids:
            return False
        groups.append({"openid": oid, "name": str(name or "").strip() or oid})
        self.config["groups"] = groups
        try:
            self.plugin._qqbot_save_group_binds(self.config, keep_secret=True)
        except Exception as e:
            self.logger.warning(f"[QQ] 持久化扩展群失败: {e}")
        return True

    def _ensure_extra_group_registered(self, group_openid):
        oid = str(group_openid or "").strip()
        if not oid:
            return
        if oid == str(self.config.get("group_openid", "") or "").strip():
            return
        groups = self.config.get("groups") or []
        if not isinstance(groups, list):
            groups = []
        for g in groups:
            if isinstance(g, dict) and str(g.get("openid", "") or "").strip() == oid:
                return
        self._add_extra_group(oid, "")

    def _rename_group(self, group_openid, name):
        oid = str(group_openid or "").strip()
        name = str(name or "").strip()
        if not oid:
            return
        groups = self.config.get("groups") or []
        if not isinstance(groups, list):
            groups = []
        renamed = False
        is_primary = oid == str(self.config.get("group_openid", "") or "").strip()
        matches = False
        for g in groups:
            if isinstance(g, dict) and str(g.get("openid", "") or "").strip() == oid:
                g["name"] = name or oid
                renamed = True
                matches = True

        if is_primary and not matches:
            groups.append({"openid": oid, "name": name or oid})
            renamed = True
        self.config["groups"] = groups
        if renamed or is_primary:
            try:
                self.plugin._qqbot_save_group_binds(self.config, keep_secret=True)
            except Exception as e:
                self.logger.warning(f"[QQ] 持久化群名失败: {e}")

    def _consume_pending_code(self):
        pending = self.config.get("pending_group_binds") or []
        if not isinstance(pending, list) or not pending:
            return
        pending.pop(0)
        self.config["pending_group_binds"] = pending
        try:
            self.plugin._qqbot_save_group_binds(self.config, keep_secret=True)
        except Exception as e:
            self.logger.warning(f"[QQ] 消耗验证码失败: {e}")

    def send_qq_text(self, content, msg_id=""):
        if self._supervisor_stop.is_set():
            return
        try:
            with self._send_lock:
                self._send_queue.append({"content": str(content), "msg_id": str(msg_id or "")})
        except Exception:
            pass
        self._wake_sender()

    def _wake_sender(self):
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._drain_send_queue)
            except Exception:
                pass

    def _drain_send_queue(self):
        try:
            if self._loop is not None and self._loop.is_running():
                asyncio.ensure_future(self._drain_send_queue_async())
        except Exception:
            pass

    async def _drain_send_queue_async(self):
        while True:
            with self._send_lock:
                if not self._send_queue:
                    return
                item = self._send_queue.popleft()
            content = str(item.get("content", ""))
            if not self.limiter.allow_send(len(content.encode("utf-8")) + 64):
                self.logger.warning("[QQ] 出站达到限流阈值，丢弃一条消息")
                continue
            await self._send_qq_text(content, str(item.get("msg_id", "")))

    async def _send_qq_text(self, content, msg_id=""):
        if self._api is None:
            return
        targets = self._bound_group_openids()
        if msg_id and str(msg_id) in self._reply_targets:
            origin = self._reply_targets.get(str(msg_id))
            if origin:
                targets = [origin]
        if not targets:
            targets = [str(self.config.get("group_openid", "") or "").strip()]
        sent = []
        for tid in targets:
            if not tid:
                continue
            try:
                await asyncio.to_thread(
                    self._api.send_message_to, tid, {"type": "text", "content": content}, msg_id
                )
                sent.append(tid)
            except Exception as e:
                self.logger.error(f"[QQ] 发送到群 {tid} 失败: {e}")
        return sent

    def _bound_groups(self):

        out = []
        seen = set()
        primary = str(self.config.get("group_openid", "") or "").strip()
        groups = self.config.get("groups") or []
        if not isinstance(groups, list):
            groups = []

        name_map = {}
        for g in groups:
            if isinstance(g, dict):
                oid = str(g.get("openid", "") or "").strip()
                if oid:
                    name_map[oid] = str(g.get("name", "") or "").strip() or oid
        if primary:
            out.append({"openid": primary, "name": name_map.get(primary, primary)})
            seen.add(primary)
        for g in groups:
            if not isinstance(g, dict):
                continue
            oid = str(g.get("openid", "") or "").strip()
            if not oid:
                continue
            key = oid
            if key in seen:
                continue
            seen.add(key)
            nm = str(g.get("name", "") or "").strip() or oid
            out.append({"openid": oid, "name": nm})
        return out

    def _bound_group_openids(self):
        return [g["openid"] for g in self._bound_groups() if g.get("openid")]

    def _group_display_name(self, openid):
        oid = str(openid or "").strip()
        if not oid:
            return ""
        primary = str(self.config.get("group_openid", "") or "").strip()
        for g in self._bound_groups():
            if g["openid"] == oid:
                return g["name"]
        return oid if oid == primary else ""

    def _filter_banned(self, text):

        if not text:
            return text
        words = self.config.get("banned_words") or []
        if not words:
            return text
        masked = text
        try:
            for w in words:
                w = str(w).strip()
                if not w:
                    continue
                repl = "*" * len(w)
                try:
                    masked = re.sub(re.escape(w), lambda m: "*" * len(m.group(0)), masked)
                except Exception:
                    masked = masked.replace(w, repl)
        except Exception:
            pass
        return masked

    def _safe_task(self, fn):
        def _run():
            try:
                fn()
            except Exception as e:
                self.logger.error(f"[QQ] 主线程任务异常: {e}")
        return _run

    def _run_on_server(self, fn):
        try:
            self.server.scheduler.run_task(self.plugin, self._safe_task(fn))
        except Exception as e:
            self.logger.error(f"[QQ] 调度主线程任务失败: {e}")

    def _broadcast_game(self, msg):
        self._run_on_server(lambda: self.server.broadcast_message(str(msg)))

    async def _get_status_text(self):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def fetch():
            try:
                names = [p.name for p in self.server.online_players]
                level = getattr(self.server, "level", None)
                world = level.name if level else "未知世界"
                fut.set_result((names, world))
            except Exception as e:
                try:
                    fut.set_exception(e)
                except Exception:
                    pass

        try:
            self.server.scheduler.run_task(self.plugin, fetch)
        except Exception as e:
            return f"获取服务器状态失败：{e}"

        try:
            names, world = await asyncio.wait_for(fut, timeout=3.0)
        except Exception as e:
            return f"获取服务器状态失败：{e}"

        tps, mspt = await self._measure_tps()

        lines = [
            "服务器状态：",
            f"TPS: {tps:.1f}（平均 {mspt:.1f} ms/tick）",
            f"在线玩家 ({len(names)}人)：{'、'.join(names) if names else '无人在线'}",
            f"当前世界：{world}",
        ]
        return "\n".join(lines)

    async def _measure_tps(self, duration=3.0):
                                                                                               

                                                                                                                    
                                                                                                                       
                                                                                                  
           
        return await asyncio.to_thread(self._measure_tps_bg, duration)

    def _measure_tps_bg(self, duration=3.0):
        pl = self.plugin
        seq0 = 0
        try:
            with pl._tps_lock:
                seq0 = pl._tps_state["seq"]
        except Exception:
            return 0.0, 0.0
        try:
            pl.server.scheduler.run_task(pl, pl._tps_start_job)
        except Exception:
            return 0.0, 0.0

        time.sleep(max(0.1, duration))
        try:
            pl.server.scheduler.run_task(pl, pl._tps_finish_job)
        except Exception:
            pass

        count = 0.0
        first = 0.0
        last = 0.0
        deadline = time.monotonic() + 1.0
        got = False
        while time.monotonic() < deadline:
            try:
                with pl._tps_lock:
                    st = pl._tps_state
                    if st["done"] and st["seq"] != seq0 and not st["running"]:
                        count = st["count"]
                        first = st["first"]
                        last = st["last"]
                        got = True
                        break
            except Exception:
                pass
            time.sleep(0.05)
        if not got:
            return 0.0, 0.0
        elapsed = last - first
        if count >= 2 and elapsed > 0:
            return float(count / elapsed), float(elapsed * 1000.0 / count)
        return 0.0, 0.0

    async def _expand_keyword_reply(self, template):
                                                                                                

                                                                                                      
                                                                                                                   
                                                          
           
        tpl = str(template or "")
        if "{" not in tpl:
            return tpl
        need_tps = ("{tps}" in tpl) or ("{mspt}" in tpl) or ("{mtps}" in tpl)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def fetch():
            try:
                names = [p.name for p in self.server.online_players]
                level = getattr(self.server, "level", None)
                world = level.name if level else "未知世界"
                fut.set_result((names, world))
            except Exception as e:
                try:
                    fut.set_exception(e)
                except Exception:
                    pass

        try:
            self.server.scheduler.run_task(self.plugin, fetch)
        except Exception:
            pass
        try:
            names, world = await asyncio.wait_for(fut, timeout=3.0)
        except Exception:
            names, world = [], "未知世界"

        tps = 0.0
        mspt = 0.0
        if need_tps:
            tps, mspt = await self._measure_tps()

        stats = {}
        try:
            stats = await asyncio.to_thread(self.plugin.get_system_stats)
        except Exception:
            stats = {}
        wl_names = []
        try:
            with self.plugin._whitelist_lock:
                wl_names = [
                    str(a.get("name", "")).strip()
                    for a in self.plugin._whitelist_apps.values()
                    if str(a.get("name", "")).strip()
                    and str(a.get("status", "")).strip() == "approved"
                ]
        except Exception:
            wl_names = []

        players = "、".join(str(n) for n in names) if names else "无人在线"
        online = f"{len(names)} 人（{players}）" if names else "无人"

        def fmt_uptime(secs):
            secs = max(0, int(secs or 0))
            d, rem = divmod(secs, 86400)
            h, rem = divmod(rem, 3600)
            m, s = divmod(rem, 60)
            parts = []
            if d:
                parts.append(f"{d}天")
            if h:
                parts.append(f"{h}小时")
            if m:
                parts.append(f"{m}分")
            if s or not parts:
                parts.append(f"{s}秒")
            return "".join(parts)

        upt = fmt_uptime(stats.get("uptime", 0))
        wl_s = "、".join(wl_names) if wl_names else "暂无白名单"
        repl = {
            "{count}": str(len(names)),
            "{players}": players,
            "{online}": online,
            "{tps}": f"{tps:.1f}",
            "{world}": world,
            "{cpu}": f"{stats.get('cpu', 0):.1f}%",
            "{mem}": f"{stats.get('mem', 0):.1f}%",
            "{disk}": f"{stats.get('disk', 0):.1f}%",
            "{mspt}": f"{mspt:.1f}",
            "{mtps}": f"{mspt:.1f}",
            "{uptime}": upt,
            "{wl}": wl_s,
        }
        for k, v in repl.items():
            tpl = tpl.replace(k, v)
        return tpl
