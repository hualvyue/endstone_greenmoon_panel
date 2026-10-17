# QQ 机器人子系统实现（网关 / API / 限流器）。
import asyncio
import collections
import json
import re
import secrets
import threading
import time
import traceback

from . import qqrelay
from .core import DEFAULT_QQBOT_CONFIG, _to_slot


class QQImageEvent(object):
    """一条来自 QQ 群的图片消息（入站）。

    主插件与子插件（``gm.bot.on_image``）共用这个对象。

    字段
    ----
    ``adapter_id``   收到消息的机器人卡片 id（多卡片环境下区分是哪张卡）
    ``adapter_name`` 该卡片的显示名
    ``group_id``     群 openid
    ``sender_id``    发送者的 member_openid（用来查账号绑定）
    ``sender_name``  发送者群昵称
    ``images``       图片列表，每项 ``{url, content_type, filename, size, width, height}``
    ``text``         同一条消息里的文字（可能为空串，纯图消息就是空）
    ``message_id``   消息 id，可用于被动回复
    ``raw``          原始消息体（需要协议里其它字段时用）

    ⚠ ``images[i]["url"]`` 是**带签名、会过期**的临时下载地址。
    拿到后请尽快下载，不要持久化保存这个 URL。
    """

    __slots__ = (
        "adapter_id", "adapter_name", "group_id", "sender_id", "sender_name",
        "images", "text", "message_id", "raw",
    )

    def __init__(self, adapter_id="", adapter_name="", group_id="", sender_id="",
                 sender_name="", images=None, text="", message_id="", raw=None):
        self.adapter_id = str(adapter_id or "")
        self.adapter_name = str(adapter_name or "")
        self.group_id = str(group_id or "")
        self.sender_id = str(sender_id or "")
        self.sender_name = str(sender_name or "")
        self.images = list(images or [])
        self.text = str(text or "")
        self.message_id = str(message_id or "")
        self.raw = raw if isinstance(raw, dict) else {}

    def __repr__(self):
        return (f"QQImageEvent(card={self.adapter_id!r}, group={self.group_id!r}, "
                f"sender={self.sender_name!r}, images={len(self.images)})")


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

    # ------------------------------------------------------------ 富媒体
    #
    # QQ 官方协议**没有**「列出群文件」这种接口（那是客户端能力，
    # 开放平台不暴露），也拿不到群号。官方能做的富媒体是：
    #   ① 上传一份媒体文件，换回 file_info / file_uuid（有有效期）
    #   ② 用 msg_type=7 发一条引用该 file_info 的消息
    # 文字用 msg_type=0。
    #
    # ⚠ 上传接口的完整路径在不同环境/版本上略有差异，因此这里把
    #   「先试 v2、失败回落旧路径」做成显式逻辑，并在失败时把响应体
    #   写进日志 —— 否则线上只能看到一个光秃秃的 404，无从下手。

    def upload_media(self, group_openid, file_url, file_type=1, srv_send_msg=False):
        """上传媒体，返回 ``file_info`` 字符串（失败抛异常）。

        ``file_type``：1=图片 2=视频 3=语音 4=文件
        ``file_url``：公网可访问的媒体地址（官方要求 URL，不接受本地路径）
        ``srv_send_msg``：True 时由服务端直接发出，不返回 file_info
        """
        target = str(group_openid or "").strip()
        if not target:
            raise RuntimeError("未指定群，无法上传媒体")
        src = str(file_url or "").strip()
        if not src:
            raise RuntimeError("媒体地址为空")

        payload = {
            "file_type": int(file_type),
            "url": src,
            "srv_send_msg": bool(srv_send_msg),
        }
        headers = {
            "Authorization": f"QQBot {self.get_access_token()}",
            "Content-Type": "application/json",
        }

        last_err = None
        for path in (f"/v2/groups/{target}/files",
                     f"/v2/groups/{target}/files/upload"):
            url = f"{self.msg_api}{path}".replace("api.bot.qq.com", "api.sgroup.qq.com")
            try:
                resp = self.http.post(url, json=payload, headers=headers, timeout=60)
                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    self._log("warning", f"[QQ] 上传媒体失败 {path}: {last_err}")
                    continue
                data = resp.json() or {}
                info = str(data.get("file_info") or "").strip()
                if info:
                    return info
                if data.get("file_uuid"):
                    return str(data["file_uuid"])
                last_err = f"响应缺少 file_info: {str(data)[:200]}"
                self._log("warning", f"[QQ] 上传媒体响应异常 {path}: {last_err}")
            except Exception as e:
                last_err = str(e)
                self._log("warning", f"[QQ] 上传媒体异常 {path}: {e}")
        raise RuntimeError(f"上传媒体失败: {last_err}")

    def send_media_message(self, group_openid, file_info, msg_id=""):
        """发送一条富媒体消息（msg_type=7，需先拿到 ``file_info``）。"""
        target = str(group_openid or "").strip()
        info = str(file_info or "").strip()
        if not target or not info:
            raise RuntimeError("群或 file_info 为空，无法发送媒体消息")
        url = f"{self.msg_api}/v2/groups/{target}/messages".replace(
            "api.bot.qq.com", "api.sgroup.qq.com"
        )
        payload = {"msg_type": 7, "media": {"file_info": info}}
        if msg_id:
            payload["msg_id"] = msg_id
        resp = self.http.post(
            url, json=payload,
            headers={"Authorization": f"QQBot {self.get_access_token()}",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        try:
            resp.raise_for_status()
        except Exception as e:
            # 把响应体打出来 —— 官方接口的报错信息只在体里，光看状态码没用
            body = getattr(e, "response", None)
            if body is not None:
                self._log("error", f"[QQ] 发送媒体响应详情: {body.text[:300]}")
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

    def is_connected(self) -> bool:
        """网关当前是否已连上 WebSocket（只读、不抛异常）。

        ★ 为什么要有这个公开方法
        ----------------------
        ``Hub.status()`` 原先直接读 ``gw._connected.is_set()`` —— 一个
        **私有属性**。QQ 官方网关和 OneBot 网关是两套完全独立实现，
        这个私有名没有任何约定保证会一直存在。哪天 OneBot 网关换成
        别的连接管理方式，``hub.status()`` 就会静默退化成「永远未连接」，
        面板上机器人在线但显示离线，而且不报错。

        现在 Hub 统一调 ``is_connected()``，两套网关各自实现即可。
        """
        try:
            return bool(self._connected.is_set())
        except Exception:
            return False

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
        # worker 线程已退出（或超时），确保连接池被释放
        self._close_api()
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
            # ★ 关掉本轮的 HTTP 连接池。
            #
            # _run() 每次都会 new 一个 QQBotAPI，而 QQBotAPI.__init__ 里
            # 建了 requests.Session()（内含连接池，持有 socket）。
            # 监督线程在鉴权失败/断线时会反复重启 worker，
            # 如果这里不关，每轮就泄漏一份连接池 —— 长时间抖动重连后
            # 会累积到 "Too many open files"，最终整个网关再也起不来。
            self._close_api()

    def _close_api(self):
        """关闭当前 API 的连接池（幂等，不抛异常）。"""
        api = getattr(self, "_api", None)
        if api is None:
            return
        try:
            sess = getattr(api, "http", None)
            if sess is not None:
                sess.close()
        except Exception:
            pass
        self._api = None

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
                        # 传入 self：播报要发到「我这张卡」的群，
                        # 不能靠 plugin.qq_bot（那只指向主卡）
                        self.plugin._notify_server_start_once(self)
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

    # ------------------------------------------------------ 入站图片
    #
    # QQ 群消息里的图片不在 ``content``（那里只有文字），而是挂在
    # ``attachments`` 数组里，形如：
    #
    #     {"content_type": "image/png",
    #      "url": "https://multimedia.nt.qq.com.cn/download?...",
    #      "filename": "xxx.png", "size": 12345, "width": 100, "height": 100}
    #
    # 注意 ``url`` 是**带签名、会过期**的临时地址（通常几小时后失效），
    # 所以拿到后要尽快下载，不能存起来慢慢用。

    @staticmethod
    def _extract_attachments(data):
        """从消息体里挑出图片附件，返回 ``[{url, content_type, filename, size, width, height}]``。

        兼容两种字段名：官方 WS 协议用 ``attachments``，部分
        代理/网关会写成 ``attachment``（单数）；两者都认，
        避免换个网关就静默收不到图。
        """
        raw = None
        for key in ("attachments", "attachment", "images", "image"):
            got = data.get(key)
            if got:
                raw = got
                break
        if raw is None:
            return []
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return []

        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("image_url")
                      or item.get("download_url") or "").strip()
            if not url:
                continue
            ctype = str(item.get("content_type") or item.get("contentType")
                        or item.get("type") or "").strip().lower()
            # content_type 可能是 image/png、image/jpeg……
            # 也可能只给了个 "image"。非图片类型（语音、文件）跳过。
            if ctype and "image" not in ctype:
                continue
            out.append({
                "url": url,
                "content_type": ctype or "image/*",
                "filename": str(item.get("filename") or "").strip(),
                "size": item.get("size") or 0,
                "width": item.get("width") or 0,
                "height": item.get("height") or 0,
            })
        return out

    def _dispatch_image(self, group_openid, sender_openid, sender_name,
                        images, content, msg_id, data):
        """把入站图片派发给已注册的钩子（主插件与子插件共用）。

        钩子签名：``fn(ev)``，``ev`` 是 ``QQImageEvent``。
        任何一个钩子抛异常都不影响其它钩子 —— 图片类钩子往往要做
        网络下载，不能让它把整条消息处理链路带崩。
        """
        # 两个列表里**任意一个**非空都要继续派发。
        # 早先这里只看 `_qq_image_hooks`（主插件内部钩子），空就直接返回 ——
        # 但主插件自己本来就没有内置的图片处理，这个列表常态为空，
        # 于是**子插件注册的钩子永远收不到任何图片**（功能整条静默失效）。
        if not getattr(self.plugin, "_qq_image_hooks", None) \
                and not getattr(self.plugin, "_gm_image_hooks", None):
            return

        # ⚠ 一律用 getattr 兜底取卡片信息。
        # ``_card_id`` 是 Hub 在 ``_spawn`` 里**动态挂上来**的，而
        # ``_card_name`` 以前根本不存在 —— 直接写 ``self._card_name``
        # 会让每一次图片派发都抛 AttributeError（被外层 except 记成
        # 「派发图片事件失败」），图片功能整条失效。
        # 这类「用了不存在的属性」的问题不会在启动时暴露，只有真收到
        # 图片才炸，所以取值处必须防御。
        ev = QQImageEvent(
            adapter_id=getattr(self, "_card_id", "") or "",
            adapter_name=getattr(self, "_card_name", "") or "",
            group_id=str(group_openid or ""),
            sender_id=str(sender_openid or ""),
            sender_name=str(sender_name or ""),
            images=list(images or []),
            text=str(content or ""),
            message_id=str(msg_id or ""),
            raw=data,
        )

        # 先跑全局（主插件内部）钩子，再跑子插件钩子；
        # 复制一份再遍历，防止回调里增删导致 RuntimeError。
        for group_name, lst in (("plugin", getattr(self.plugin, "_qq_image_hooks", None)),
                                ("gm", getattr(self.plugin, "_gm_image_hooks", None))):
            if not lst:
                continue
            for fn in list(lst):
                try:
                    fn(ev)
                except Exception as e:
                    self.logger.warning(
                        f"[QQ] 图片钩子 {getattr(fn, '__name__', fn)} 执行失败: {e}")

    async def _handle_group_message(self, data):
        try:
            content = str(data.get("content", "") or "").strip()
            attachments = self._extract_attachments(data)
            # 注意：不能因为 content 为空就 return。
            # QQ 群里「只发一张图、不带文字」是极常见的用法，那种消息
            # content 就是空串，图片只在 attachments 里。原来这里直接
            # return，导致纯图片消息整条被丢弃（子插件的图片钩子也就
            # 永远收不到东西）。
            if not content and not attachments:
                return
            sender = data.get("author") or {}
            # ⚠ 官方消息体里**没有 ``member_name`` 这个字段**。
            # 昵称在 ``username``（见 QQ 官方文档 GROUP_AT_MESSAGE_CREATE
            # 的 User 结构：id / username / bot / union_openid /
            # union_user_account / user_openid / member_openid / member_role）。
            # 原先优先读 ``member_name`` 永远取不到，一路 or 下去落到
            # ``"未知"``，游戏内于是显示不出是谁在说话。
            #
            # 这里不再用「未知」兜底，而是留空 —— 由 qqrelay.format_relay
            # 统一决定缺昵称时显示什么（它会给一个可读的占位词），
            # 免得同一个概念在两个模块里各有一套兜底文案。
            sender_name = str(sender.get("username") or "").strip()
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

            # ---- 已绑定的群里发了「当前验证码」→ 明确回一句，别装死 ----
            #
            # 场景：A 群已绑定，用户又申请了一个码（准备加 B 群），
            # 结果在 A 群里把码发了出来。原实现会走下面的通用分支，
            # 码被当成普通聊天内容转发到游戏里，机器人**一个字都不回**，
            # 用户以为消息发丢了、或者以为验证码失效了。
            #
            # ⚠ 触发条件必须**同时**满足两点，缺一不可：
            #   1) 本群已绑定（is_bound）；
            #   2) 内容**精确等于**当前有效的验证码。
            # 绝不能放宽成「已绑定群发的任何数字」之类 —— 下面紧跟着
            # 图片派发、`!` 指令、自定义关键词、查状态、账号绑定码绑定、
            # QQ→MC 转发等一长串逻辑（尤其账号绑定码也是 5 位纯数字），
            # 一旦误判就会把用户在已绑定群里的正常消息整条吃掉。
            if is_bound and group_openid:
                _cur_pending = self.config.get("pending_group_binds") or []
                _cur_code = ""
                try:
                    if _cur_pending and isinstance(_cur_pending[0], dict):
                        _cur_code = str(_cur_pending[0].get("code", "") or "")
                except Exception:
                    _cur_code = ""
                if content and content in (c for c in (_cur_code, self.binding_code or "") if c):
                    await self._send_qq_text(
                        "ℹ️ 本群已经绑定过了，无需重复绑定。\n"
                        "该验证码是给「还没加入的新群」用的 —— 请把机器人拉进目标群，"
                        "在那个群里发送这个验证码。\n"
                        "如需更换主群或解绑，请在服务器 Web 后台「QQ机器人 → 群管理」里操作。",
                        msg_id,
                    )
                    # 注意：这里**不**消耗验证码。它属于另一个还没绑的群，
                    # 在这里吃掉会导致用户去目标群发时反而绑定失败。
                    return

            if not is_bound:
                pending = self.config.get("pending_group_binds") or []
                code_is_pending = group_openid and bool(pending) and content == str(pending[0].get("code", ""))
                if code_is_pending:
                    # ``_complete_binding`` 在本群**已经是主群**时会直接 return
                    # （它开头就挡了这一条）。如果这里不问青红皂白地报
                    # 「主群绑定成功」，就是在为一个什么都没发生的操作发成功回执。
                    # 所以先问一句「主群位是不是本来就是本群」。
                    _cur_primary = str(self.config.get("group_openid", "") or "").strip()
                    if not _cur_primary:
                        await asyncio.to_thread(self._complete_binding, group_openid)
                        await self._send_qq_text("✅ 主群绑定成功！可发送「设置群名 名字」为本群命名", msg_id)
                    elif _cur_primary == str(group_openid):
                        await self._send_qq_text("ℹ️ 本群已经是主群了，无需重复绑定。", msg_id)
                    else:
                        if self._add_extra_group(group_openid, group_openid):
                            await self._send_qq_text("✅ 已加入互联群！可发送「设置群名 名字」为本群命名", msg_id)
                        else:
                            await self._send_qq_text("⚠️ 该群已在互联列表中", msg_id)
                    # 成功加入一个群 → 注销该验证码（一码一用）。
                    self._consume_pending_code()
                    return
                if group_openid and content == (self.binding_code or ""):
                    # 同上：已经是主群就别报「成功」。``binding_code`` 是主卡
                    # 的固定绑定码，在同一群里重复发会一直命中这一条。
                    if str(self.config.get("group_openid", "") or "").strip() == str(group_openid):
                        await self._send_qq_text("ℹ️ 本群已经是主群了，无需重复绑定。", msg_id)
                        return
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

            # 已绑定的群里，把图片派发给钩子（子插件靠这个收图）。
            # 放在这里而不是开头，是为了不处理未绑定群的消息 ——
            # 否则随便谁拉个群发图都能触发地图画生成，等于开了个
            # 免费的白嫖入口（生成地图是有开销的）。
            if attachments:
                try:
                    self._dispatch_image(
                        group_openid, sender_openid, sender_name,
                        attachments, content, msg_id, data,
                    )
                except Exception as e:
                    self.logger.warning(f"[QQ] 派发图片事件失败: {e}")

            try:
                pl = self.plugin
                bind_cfg = pl._load_tools_config().get("binding", {}) or {}
                digits = int(bind_cfg.get("code_digits", 5) or 5)
                # 这里判断「我这张卡能不能用」，而不是「全局有没有官方机器人在跑」。
                # 原来读的是 plugin.qq_bot（全局主卡引用）：当用户禁用主卡、
                # 只留第二张卡运行时，这个条件会随主卡一起失效，导致能正常
                # 收发消息的第二张卡反而处理不了账号绑定码。
                if (bind_cfg.get("enabled", True) and self._supervisor_stop.is_set() is False
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
                    # 日志里昵称可能为空（部分消息不带 username），
                    # 这里补一个可读兜底，免得打出「管理员  执行指令」。
                    _who = sender_name or "未知"
                    self.logger.info(f"[QQ] 管理员 {_who} 执行指令：{cmd}")
                    self._run_on_server(lambda c=cmd: self.server.dispatch_command(self.server.command_sender, c))
                    await self._send_qq_text(f"✅ 已执行指令：{cmd}", msg_id)
                return

            # ---- 正则触发引擎（普通消息）----
            #
            # ⚠ 位置不可前移。必须晚于：已绑定群判定、图片派发、
            #   账号绑定码（5 位纯数字）、签到、`设置群名`、`/` 管理员指令。
            #
            #   前移会踩两个坑：
            #     ① 绑定码 / 签到词被正则先一步匹配走，功能整条失效；
            #     ② 规则里的 executeCommand 会绕过上面 group_admins 的
            #        权限校验 —— 任何群成员都能靠一条规则在服务器上执行命令。
            #
            #   放在这里则相反：正则天然只能匹配「已经过完所有权限校验、
            #   又还没被当作普通聊天转发」的那部分内容，语义最干净。
            #
            #   block=True 的规则返回 True，表示这条消息到此为止，
            #   不再走下面的 custom_keywords / 状态查询 / QQ→MC 转发。
            try:
                engine = getattr(self.plugin, "regex_engine", None)
                if engine is not None:
                    _pack = {
                        "card_id": getattr(self, "_card_id", "") or "",
                        "group_id": group_openid,
                        "user_id": sender_openid,
                        "message_id": msg_id,
                        "message": [{"type": "text", "data": {"text": content}}],
                        "raw_message": content,
                        "time": int(time.time()),
                        "self_id": str(getattr(self._api, "app_id", "") or ""),
                        "domain": "official",
                        "sender": {
                            "nickname": sender_name,
                            "role": "admin" if sender_openid in self.config.get("group_admins", []) else "member",
                        },
                    }
                    if await asyncio.to_thread(engine.handle_message, content, _pack):
                        return
            except Exception as e:
                self.logger.warning(f"[QQ] 正则引擎处理失败: {e}")

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
                # 群名用于显示；取不到就交给 format_relay 兜底
                gname = self._group_display_name(group_openid)
                text = qqrelay.format_relay(
                    self.config.get("qq_to_mc_format") or qqrelay.DEFAULT_FORMAT,
                    sender_name, gname, content,
                )
                self._broadcast_game(text)
                cross = getattr(self.plugin, "cross", None)
                if cross is not None and cross.enabled():
                    try:
                        # send_qq_relay 自身兼容新旧参数形态，
                        # 这里按新形态传：发言人 + 群名 + 内容
                        cross.send_qq_relay(sender_name, gname, content)
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
            self.plugin._qqbot_persist_group_openid(
                group_openid, card_id=getattr(self, "_card_id", None)
            )
        except Exception as e:
            self.logger.warning(f"[QQ] 持久化绑定群出错: {e}")
        self.logger.info(f"[QQ] 已绑定群 openid: {group_openid}")

        try:
            # 只清掉「我自己这张卡」的播报标志，别动其他卡的。
            # 绑定群之后要允许重新播报一次上线消息。
            _n = getattr(self.plugin, "_qq_start_notified", None)
            if not isinstance(_n, set):
                _n = set()
            _cid = str(getattr(self, "_card_id", "") or "")
            if _cid:
                _n.discard(_cid)
            self.plugin._qq_start_notified = _n
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
            self.plugin._qqbot_save_group_binds(
                self.config, keep_secret=True, card_id=getattr(self, "_card_id", None)
            )
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
                self.plugin._qqbot_save_group_binds(
                    self.config, keep_secret=True, card_id=getattr(self, "_card_id", None)
                )
            except Exception as e:
                self.logger.warning(f"[QQ] 持久化群名失败: {e}")

    def _consume_pending_code(self):
        pending = self.config.get("pending_group_binds") or []
        if not isinstance(pending, list) or not pending:
            return
        pending.pop(0)
        self.config["pending_group_binds"] = pending
        try:
            self.plugin._qqbot_save_group_binds(
                self.config, keep_secret=True, card_id=getattr(self, "_card_id", None)
            )
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
        """取群名用于显示。取不到就返回空串，**绝不返回 openid**。

        为什么要这样：``groups[].name`` 在 core.py 规范化时，缺名字会
        回退成 openid 本身（见 ``_qqbot_public_state`` / ``group_edit``）。
        所以查表拿到的 ``name`` **有可能就是一串 openid**。

        而 openid 正是用户报的那个问题——游戏里显示成
        「[QQ] 7FCD011F67733A4572DCDBF2D525F201：？」。一旦它被当成
        群名填进格式串，就等于什么都没修。所以这里要做一次识别：
        名字长得跟群标识一样就丢掉，交给 ``qqrelay.format_relay``
        填一个可读的占位词。

        主群（``group_openid``）同理：以前这里 ``return oid if oid ==
        primary else ""`` —— 主群没填名字时直接把 openid 当群名返回，
        正是线上那个现象的直接来源。

        ⚠ 配置里**没有** ``group_name`` 这个键（群名只存在
        ``groups[].name`` 里）。别凭印象去读一个不存在的字段 ——
        那正是本轮 ``_card_name`` 那个 bug 的同类错误。
        主群若没登记在 groups 里，就拿不到名字，返回空串让调用方兜底。
        """
        oid = str(openid or "").strip()
        if not oid:
            return ""
        for g in self._bound_groups():
            if str(g.get("openid") or "") == oid:
                nm = str(g.get("name") or "").strip()
                # 名字等于标识本身 → 说明当初就没填名字，不算有效群名。
                if nm and nm != oid:
                    return nm
                break
        return ""

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
