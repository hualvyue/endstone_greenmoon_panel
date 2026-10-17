# QQ 机器人子系统实现（网关 / API / 限流器）。
import asyncio
import collections
import json
import random
import re
import secrets
import threading
import time
import traceback

from . import qqrelay
from .core import DEFAULT_QQBOT_CONFIG, _to_slot


class _QQRateLimited(RuntimeError):
    """QQ 开放平台限流（HTTP 400 / code 100017 / err_code 40023001）。

    ★ 为什么必须有自己的异常类型
    --------------------------
    ``requests`` 的 ``HTTPError`` 在 ``raise_for_status()`` 时**只保留
    状态码和 URL**，响应体被丢掉。而限流的全部判据（100017、
    40023001、"接口调用超过频率限制"）都只在响应体里 —— 上层拿到的
    异常字符串是 ``400 Client Error: Bad Request for url: ...``，
    不含任何限流特征。

    所以必须在 ``raise_for_status()`` **之前**判一次，并抛出这个类型；
    上层 ``_run`` / 监督线程只需要 ``isinstance`` 就能走限流退避，
    不必再解析文本。
    """


# ===========================================================================
# 限流识别：**模块级常量 + 函数**，不是某个类的方法。
#
# ⚠⚠ 这里是本项目踩过的一个坑，务必别改回去。
#
# 最初这些常量和方法定义在 ``QQBotGateway`` 里（``_RATE_LIMIT_CODES`` /
# ``_looks_rate_limited``），然后我在 ``QQBotAPI.get_access_token`` 里写了
# ``self._looks_rate_limited(body)`` —— **两个类没有继承关系**，
# 于是每次取 token 都抛：
#
#     AttributeError: 'QQBotAPI' object has no attribute '_looks_rate_limited'
#
# 表现是：机器人 100% 连不上，日志每 6 秒刷一次「获取 access_token 失败」。
#
# 教训有两条：
#   1. 跨类复用的**纯函数**（不依赖 self 状态）就该放模块级，
#      「放在哪个类里」本身就是个错误决策；
#   2. 我的测试只做了**静态断言**（检查源码里有没有某些字符串），
#      从没真正执行过 ``get_access_token``。静态断言查不出
#      「调用了不存在的属性」—— 必须补一个真跑的测试。
#
# 因此 v4.5.3 起：常量与判定函数都在模块级，两个类共用；
# 类上保留同名 ``@classmethod`` 转发，兼容既有调用与测试。
# ===========================================================================

# 实测返回形态：HTTP 400，body 为
#   {"message":"接口调用超过频率限制","code":100017,"err_code":40023001}
# 注意它**不是** WebSocket 关闭码，而是 HTTP 错误。
RATE_LIMIT_CODES = (100017, 40023001)
RATE_LIMIT_KEYWORDS = ("超过频率限制", "频率限制", "ratelimit", "rate limit",
                       "too many requests", "too frequent")

# 限流退避阶梯：60 → 120 → 240，封顶 600 秒。
# 走「长退避 + 明确日志」，因为限流是**服务端**施加的，
# 快速重试只会把封禁窗口越拉越长。
RATE_LIMIT_BACKOFF_STEPS = (60.0, 120.0, 240.0)
RATE_LIMIT_BACKOFF_CAP = 600.0


def looks_rate_limited(text) -> bool:
    """从任意文本里判定是否命中 QQ 开放平台限流。

    ★ 为什么是模块级函数而不是某个类的方法
    -------------------------------------
    ``QQBotAPI``（HTTP 层，取 token / 取 gateway）和 ``QQBotGateway``
    （连接层，跑 WebSocket 主循环）**都要用**这个判定，但两者**没有
    继承关系**。把它挂在 `QQBotGateway` 上、再在 `QQBotAPI` 里
    `self.` 调用，就是 AttributeError。纯函数放模块级是唯一正确的收口。

    ★★ 为什么错误码必须做**结构化**匹配，不能用裸子串
    ------------------------------------------------
    最早的实现是 ``if str(code) in s``。这会把**任何含这串数字的文本**
    都判成限流，例如::

        msg_id=100017000          → 误判
        file_info=100017abc       → 误判
        https://cdn.x/100017/y    → 误判

    而 ``QQBotGateway._is_rate_limited`` 是对**任意异常**的字符串调用
    这个函数 —— 于是一条普通报错只要碰巧带这几个数字，就会被当成限流，
    触发一次 60 秒起步的退避，把「一次偶发失败」放大成「一分钟不干活」。

    所以错误码只认**结构化形态**：``"code": 100017``、
    ``"err_code":40023001``、``code=100017``、``100017`` 独立成词
    这类**带字段名或分隔符**的写法。判据来自实测响应体::

        {"message":"接口调用超过频率限制","code":100017,"err_code":40023001}

    并且关键词匹配优先级更高、本身已足够可靠（真限流响应体一定含中文
    "接口调用超过频率限制"），错误码只是兜底。
    """
    s = str(text or "")
    if not s:
        return False
    low = s.lower()
    for kw in RATE_LIMIT_KEYWORDS:
        if kw.lower() in low:
            return True
    for code in RATE_LIMIT_CODES:
        if _has_structured_code(s, code):
            return True
    return False


# 错误码前面允许出现的「字段名 + 分隔符」形态。
# ⚠ 见 ``_has_structured_code`` 的说明：不能用裸子串，
#   否则 msg_id / file_info / URL 里碰巧含同一串数字就会被误判成限流。
_CODE_STRICT_PATTERNS = (
    r'["\']?code["\']?\s*[:=]\s*["\']?%d(?![0-9])',
    r'["\']?err_?code["\']?\s*[:=]\s*["\']?%d(?![0-9])',
)


def _has_structured_code(text: str, code: int) -> bool:
    """``code`` 是否以**结构化**形态出现（带字段名，或独立成词）。

    只接受两类：
      1. 带字段名：``code=100017`` / ``"code": 100017`` / ``err_code:40023001``
      2. 严格独立成词且**两侧都不是字母/数字**：``(100017)`` / ``[100017]``

    明确**不**接受 ``msg_id=100017000``、``file_info=100017abc`` 这类，
    它们只是碰巧包含同一串数字。
    """
    import re as _re
    for pat in _CODE_STRICT_PATTERNS:
        if _re.search(pat % code, text, _re.IGNORECASE):
            return True
    # 独立成词：两侧必须是非字母数字（允许引号/括号/空格/逗号等）
    pat = r'(?<![0-9A-Za-z])%d(?![0-9A-Za-z])' % code
    return bool(_re.search(pat, text))


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

    def refund(self, cost=1.0):
        """归还已扣的令牌（组合扣费里后一个桶失败时回滚前一个桶）。"""
        if cost <= 0:
            return
        with self._lock:
            self.tokens = min(self.capacity, self.tokens + float(cost))

class _GatewayLimiter(object):

    def __init__(self, send_rate_per_min, send_burst, recv_rate_per_sec, recv_burst, max_bytes_per_sec):
        self.sender_messages = _TokenBucket(float(send_rate_per_min) / 60.0, float(send_burst))
        self.receiver_messages = _TokenBucket(float(recv_rate_per_sec), float(recv_burst))
        self.bytes_bucket = _TokenBucket(float(max_bytes_per_sec), float(max_bytes_per_sec))

    def allow_send(self, cost_bytes=1):
        # ★ 不能写 ``a and b`` —— Python 的 and 是短路的，
        # 前一个桶有令牌时后一个桶**根本不会被扣**，
        # 于是字节桶永远满着，整体速率上限实际上比配置值大得多。
        # 必须把两次 acquire 都算出来（即使第一个已经失败）。
        ok_bytes = self.bytes_bucket.acquire(max(1, int(cost_bytes)))
        ok_msg = self.sender_messages.acquire(1.0)
        # 字节桶通过但消息桶不过：把字节退回去，避免「字节被扣了、
        # 消息数没扣成功」的双重错账（会白吃掉带宽额度）。
        if ok_bytes and not ok_msg:
            self.bytes_bucket.refund(max(1, int(cost_bytes)))
        return ok_msg and ok_bytes

    def allow_recv(self, cost_bytes=1):
        # 同上：两次 acquire 都要执行，不能短路。
        ok_bytes = self.bytes_bucket.acquire(max(1, int(cost_bytes)))
        ok_msg = self.receiver_messages.acquire(1.0)
        if ok_bytes and not ok_msg:
            self.bytes_bucket.refund(max(1, int(cost_bytes)))
        return ok_msg and ok_bytes

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
            # ★ 三个地址统一到 sgroup 域。
            # 原实现把 ``qq`` 写成 api.bot.qq.com，而真正可用的 API 域是
            # api.sgroup.qq.com —— 于是所有走 ``qq_api`` 的调用（网关地址、
            # 在线状态等）都打在一个**平台没有开放 API** 的域名上，
            # 只能靠各处零散的 ``.replace()`` 打补丁救回来；一旦哪处漏了
            # replace 就是一个难查的 404。这里从源头上把域名写对，
            # 各处不再需要 replace。
            qq = "https://api.sgroup.qq.com"
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
        self._log("debug", f"[调试-TOKEN] 请求 URL: {url}")
        self._log("debug", f"[调试-TOKEN] AppID 前缀: {str(self.app_id)[:12]}...")
        try:
            resp = self.http.post(
                url,
                json={"appId": self.app_id, "clientSecret": self.app_secret},
                timeout=15,
            )
        except Exception as e:
            self._log("error", f"[调试-TOKEN] 网络异常: {e}")
            raise
        self._log("debug", f"[调试-TOKEN] 响应码: {resp.status_code}")
        body = resp.text[:300]
        if self.app_secret:
            body = body.replace(self.app_secret, "****")
        self._log("debug", f"[调试-TOKEN] 响应内容: {body}")
        # ★ 限流要在 raise_for_status() 之前识别 —— HTTPException 的 str()
        # 只带状态码和 URL，**丢掉响应体**，而 100017/40023001 恰好只在
        # 响应体里。不提前抛自定义异常的话，上层永远看不到限流特征。
        #
        # ⚠ 这里必须调**模块级** looks_rate_limited，不能写
        #   self._looks_rate_limited —— 那是 QQBotGateway 的方法，
        #   本类（QQBotAPI）没有，会抛 AttributeError。
        if looks_rate_limited(body):
            raise _QQRateLimited(f"取 access_token 被限流: HTTP {resp.status_code} {body}")
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data.get("access_token", "")
        self.token_expire = time.time() + int(data.get("expires_in", 7200))
        return self.access_token

    def get_gateway_url(self):
        url = f"{self.qq_api}/gateway"
        headers = {"Authorization": f"QQBot {self.get_access_token()}"}
        auth_short = headers["Authorization"][:30] if len(headers["Authorization"]) > 30 else headers["Authorization"]
        self._log("debug", f"[调试-GATEWAY] 请求 URL: {url}")
        self._log("debug", f"[调试-GATEWAY] Authorization: {auth_short}...")
        try:
            resp = self.http.get(url, headers=headers, timeout=10)
        except Exception as e:
            self._log("error", f"[调试-GATEWAY] 网络异常: {e}")
            raise
        self._log("debug", f"[调试-GATEWAY] 响应码: {resp.status_code}")
        self._log("debug", f"[调试-GATEWAY] 响应内容: {resp.text[:300]}")
        if looks_rate_limited(resp.text[:300]):
            raise _QQRateLimited(
                f"取 Gateway URL 被限流: HTTP {resp.status_code} {resp.text[:200]}"
            )
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

    def send_message(self, message, msg_id="", msg_seq=0):
        return self.send_message_to(self.group_openid, message, msg_id, msg_seq)

    def send_message_to(self, group_openid, message, msg_id="", msg_seq=0):
        target = str(group_openid or "").strip()
        if not target:
            raise RuntimeError("未绑定任何群，无法发送消息")

        url = f"{self.msg_api}/v2/groups/{target}/messages"
        content = str(message.get("content", "") or "")
        if not content.strip():
            raise RuntimeError("不允许发送空消息")

        payload = {"msg_type": 0, "content": content}
        if msg_id:
            payload["msg_id"] = msg_id
            # ★ msg_seq 必须带。官方规定同一条被动消息最多回 5 条，
            # 靠 msg_seq(从 1 开始) 区分；不传时默认 1 ——
            # 第 2 条回复就会和第 1 条判定为「同一条消息」，被服务端拒掉。
            payload["msg_seq"] = int(msg_seq or 1)
        resp = self._post_with_retry(url, payload)
        return resp.json()

    def _post_with_retry(self, url, payload, timeout=20, attempts=3):
        """带鉴权重试的 POST。

        ★ 原实现里 access_token 是「拿到就用到过期」。但官方有两种情况会
        让一个**看起来没过期**的 token 立刻失效：① 后台重置了 AppSecret；
        ② token 被风控提前吊销。此时每个请求都 401，而代码只会把异常抛给
        上层记日志 —— 表现是「机器人突然完全不发消息，日志里一片 401，
        但要重启插件才恢复」。这里对 401/403 强制刷 token 重试，
        对 429 按 Retry-After 退避重试。
        """
        last_exc = None
        for i in range(max(1, attempts)):
            headers = {"Authorization": f"QQBot {self.get_access_token()}",
                       "Content-Type": "application/json"}
            try:
                resp = self.http.post(url, json=payload, headers=headers, timeout=timeout)
            except Exception as e:
                last_exc = e
                break
            code = getattr(resp, "status_code", 0)
            if code in (401, 403):
                self._log("warning", f"[QQ] {code} 鉴权失败，强制刷新 token 后重试({i + 1}/{attempts})")
                try:
                    self.get_access_token(force=True)
                except Exception as e:
                    self._log("error", f"[QQ] 刷新 token 失败: {e}")
                    last_exc = e
                    break
                continue
            if code == 429:
                wait = 1.0
                try:
                    wait = float(resp.headers.get("Retry-After", "") or 1.0)
                except Exception:
                    wait = 1.0
                wait = min(max(wait, 0.5), 30.0)
                self._log("warning", f"[QQ] 429 限流，{wait:.1f}s 后重试({i + 1}/{attempts})")
                time.sleep(wait)
                continue
            # ★★ 官方限流**不是** 429，而是 HTTP 400 + code 100017 /
            #    err_code 40023001（"接口调用超过频率限制"）。取 token 和取
            #    gateway 两处都已识别这个形态，唯独**发消息**这条路没有 ——
            #    于是「限流发生在发消息时」会被当作普通错误抛出去，网关的
            #    限流退避（_note_rate_limited）永远不触发，表现为一秒内
            #    反复撞限流。这里补齐，并统一抛 _QQRateLimited 让上层识别。
            #
            #    必须在 raise_for_status() **之前**判 —— HTTPError 的 str()
            #    只留状态码和 URL，响应体（含 100017）会被丢掉。
            if code == 400 and looks_rate_limited(getattr(resp, "text", "") or ""):
                detail = str(getattr(resp, "text", "") or "")[:200]
                self._log("warning", f"[QQ] 发送消息被限流: HTTP 400 {detail}")
                raise _QQRateLimited(f"发送消息被限流: HTTP {code} {detail}")
            try:
                resp.raise_for_status()
            except Exception as e:
                # ★ 原实现从 ``e.response`` 取响应体 —— 但本项目的 HTTPError
                #   是 ``resp.raise_for_status()`` 抛的，response 属性在本项目的
                #   封装下并不保证存在，取不到就静默丢掉详情，排障时最需要的
                #   恰恰是这段 body。改成**直接从 resp 取**，稳定拿到详情。
                detail = getattr(resp, "text", "") or ""
                _log = getattr(self, "logger", None)
                if _log is not None and detail:
                    try:
                        _log.error(f"[QQ] 发送响应详情: HTTP {code} {detail[:300]}")
                    except Exception:
                        pass
                raise
            return resp
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("发送失败：重试次数用尽")

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
            url = f"{self.msg_api}{path}"
            try:
                resp = self.http.post(url, json=payload, headers=headers, timeout=60)
                # ★ 限流要在「试下一个路径」之前拦截并上抛。
                #   否则 400 + code 100017 会被当成「这个路径不对」，
                #   默默换下一条路径重试 —— 不仅多打一次限流接口，
                #   还把真正的限流原因吞掉，上层退避永远不触发。
                if resp.status_code == 400 and looks_rate_limited(getattr(resp, "text", "") or ""):
                    detail = str(getattr(resp, "text", "") or "")[:200]
                    self._log("warning", f"[QQ] 上传媒体被限流: HTTP 400 {detail}")
                    raise _QQRateLimited(f"上传媒体被限流: HTTP 400 {detail}")
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
            except _QQRateLimited:
                # ★ 限流必须**原样上抛**，不能被下面那个 except Exception
                #   吞掉再包成 RuntimeError —— _QQRateLimited 本身是
                #   RuntimeError 的子类，顺序写错就会丢掉限流语义，
                #   上层退避照样不触发。这个分支必须放在 except Exception 之前。
                raise
            except Exception as e:
                last_err = str(e)
                self._log("warning", f"[QQ] 上传媒体异常 {path}: {e}")
        raise RuntimeError(f"上传媒体失败: {last_err}")

    def send_media_message(self, group_openid, file_info, msg_id="", msg_seq=0):
        """发送一条富媒体消息（msg_type=7，需先拿到 ``file_info``）。"""
        target = str(group_openid or "").strip()
        info = str(file_info or "").strip()
        if not target or not info:
            raise RuntimeError("群或 file_info 为空，无法发送媒体消息")
        url = f"{self.msg_api}/v2/groups/{target}/messages"
        payload = {"msg_type": 7, "media": {"file_info": info}}
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = int(msg_seq or 1)
        resp = self.http.post(
            url, json=payload,
            headers={"Authorization": f"QQBot {self.get_access_token()}",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        # ★ 与 _post_with_retry 保持一致：限流要在 raise_for_status 之前判，
        #   否则 HTTPError 会把含 100017 的响应体丢掉，上层看不到限流特征。
        if getattr(resp, "status_code", 0) == 400 and \
                looks_rate_limited(getattr(resp, "text", "") or ""):
            detail = str(getattr(resp, "text", "") or "")[:200]
            self._log("warning", f"[QQ] 发送媒体被限流: HTTP 400 {detail}")
            raise _QQRateLimited(f"发送媒体被限流: HTTP 400 {detail}")
        try:
            resp.raise_for_status()
        except Exception:
            # 把响应体打出来 —— 官方接口的报错信息只在体里，光看状态码没用。
            # ⚠ 必须直接读 resp.text，不能读 e.response —— 本项目的
            #   HTTPError 不保证带 response 属性，取不到就静默丢详情。
            detail = getattr(resp, "text", "") or ""
            if detail:
                self._log("error", f"[QQ] 发送媒体响应详情: HTTP "
                                  f"{getattr(resp, 'status_code', '?')} {detail[:300]}")
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
        # msg_id → 已用 msg_seq。被动回复上限 5 条，超出后转主动消息。
        self._msg_seq = collections.OrderedDict()
        self._msg_seq_lock = threading.Lock()
        # 入站消息去重表（msg_id → 首次见到的时间戳）。Resume 补齐时会重推。
        self._seen_msgs = collections.OrderedDict()
        self._seen_msg_lock = threading.Lock()
        # 心跳 ACK 标记：发过心跳但没收到 op 11 就判定连接已死
        self._hb_ack = True
        self._hb_task = None
        self._auth_fail_streak = 0
        # intents 类关闭码的连续计数。4013/4914/4915 都不是「token 坏了」，
        # 刷 token 重连一万次也没用；连续踩到阈值就进入熔断（长间隔慢重试）。
        self._intent_fail_streak = 0
        self._intent_circuit_open = False
        # 限流（100017 / 40023001）的连续计数，驱动指数退避。
        self._rate_limit_streak = 0
        # ★ 网关地址缓存（详见 _get_gateway_url_cached 的注释）。
        #   解决的是「每轮重连都打一次 /gateway → 重连风暴把官方配额撞穿」。
        self._gateway_url = ""
        self._gateway_fetched_at = 0.0
        self._gateway_force_refresh = True

    # token 无效类关闭码：必须重新鉴权（刷 access_token）才可能恢复
    _AUTH_CLOSE_CODES = (4004, 4013, 4014)

    # ⚠ 4013 虽然官方文档写作 "invalid intents"，但它**不是**鉴权问题，
    # 刷 token 无效。历史上它被错放进 _AUTH_CLOSE_CODES，导致「刷新 token
    # → 重连 → 再次 4013」的死循环（实测 38 秒内刷了 7 次 token，日志被
    # 「已刷新 access_token」刷屏，而问题一点没解决）。
    #
    # ★★ 4013 的头号成因是**本地发出的 intents 值不对**（代码问题）
    # --------------------------------------------------------------
    # v7.8.2 曾把 Identify 的 intents 改成 ``config.get("intents", 0)``，
    # 而配置里根本没有这个键 → 实际发 0 → 等于"不订阅任何事件" →
    # 服务端每次都立刻回 4013。**这才是"旧的能用、新的不能用"的真正原因**，
    # 详见 ``_resolve_intents`` 的说明。
    #
    # 次要成因才在用户的 QQ 开放平台后台：
    #   ① 机器人未在后台订阅所需事件（群 @ 消息等）；
    #   ② 用的是测试环境 AppID，而测试环境的事件权限集受限。
    # 所以策略是：识别出来 → 连续若干次 → 熔断（长间隔慢重试）→ 给出明确指引，
    # 且指引里要**先让用户核对本地 intents 值**，而不是先去后台折腾。
    _INTENT_CLOSE_CODES = (4013, 4914, 4915)

    # ★★ 熔断态下的重试间隔（秒）。
    #
    # 早期实现让熔断直接 `break` 出 worker 循环、**不再重连**，但监督线程
    # 看到 worker 死了就照常 `_launch_worker()` 重启它，而 `start()` 里有
    # 一句无条件复位 —— 结果是「熔断 → 5 秒后被复活 → 复位 → 再熔断」，
    # 实测 38 秒内重连 7 次，和没有熔断完全一样，还把官方限流撞了出来
    # （响应码 400 / code 100017「接口调用超过频率限制」）。
    #
    # 现在的语义是「熔断 = 慢重试」而不是「熔断 = 停死」：
    #   * 监督线程**必须**尊重熔断态，把间隔拉长到本值（默认 300 秒）；
    #   * 不彻底停死，因为用户可能就在这几分钟内改好了后台事件订阅，
    #     停死会让「改好了也不恢复」变成新的坑。
    _CIRCUIT_RETRY_SECONDS = 300.0

    # ★ 限流识别特征：**转发到模块级常量**，不再在本类重新定义。
    #
    # ⚠ 改这里之前请先读模块顶部那段注释：这些值原先定义在 QQBotGateway 上，
    # 结果 QQBotAPI 里 `self._looks_rate_limited(...)` 直接 AttributeError。
    # 现在真身只有一个（模块级），本类只是别名，避免两处漂移。
    _RATE_LIMIT_CODES = RATE_LIMIT_CODES
    _RATE_LIMIT_KEYWORDS = RATE_LIMIT_KEYWORDS
    _RATE_LIMIT_BACKOFF_STEPS = RATE_LIMIT_BACKOFF_STEPS
    _RATE_LIMIT_BACKOFF_CAP = RATE_LIMIT_BACKOFF_CAP

    def _intent_circuit_threshold(self) -> int:
        """熔断阈值：连续踩到多少次 intents 关闭码后进入熔断（慢重试）。"""
        try:
            return max(1, int(self.config.get("intent_fail_circuit_threshold", 3)))
        except Exception:
            return 3

    def _circuit_retry_seconds(self) -> float:
        """熔断态的慢重试间隔（秒），下限 30 秒，防止配成 0 又把限流撞出来。"""
        try:
            v = float(self.config.get("circuit_retry_seconds", self._CIRCUIT_RETRY_SECONDS))
        except Exception:
            v = self._CIRCUIT_RETRY_SECONDS
        return max(30.0, v)

    def _rate_limit_backoff(self) -> float:
        """按连续限流次数取退避秒数：60 → 120 → 240，封顶 600。

        ★ 关于「要不要被 reconnect_seconds_max 压小」
        ------------------------------------------
        早期写法是 ``max(step, min(cfg_cap, CAP))``，本意是「限流退避必须
        长于普通重连上限」。但这句 ``min(cfg_cap, CAP)`` 在用户把
        ``reconnect_seconds_max`` 配成 120（默认值）时，会变成一个
        **下限 120 秒的地板** —— 把第一档的 60 秒直接顶成 120 秒，
        退避阶梯的「渐进」意义就没了。

        正确做法：退避阶梯本身**不受 reconnect_seconds_max 影响**
        （它是限流专用曲线，与普通重连是两个概念），只受 CAP 封顶。
        """
        try:
            n = max(0, int(self._rate_limit_streak) - 1)
        except Exception:
            n = 0
        if n >= len(self._RATE_LIMIT_BACKOFF_STEPS):
            return self._RATE_LIMIT_BACKOFF_CAP
        return self._RATE_LIMIT_BACKOFF_STEPS[n]

    # =====================================================================
    # /gateway 取用节流（纵深防御，不是主因修复）
    # =====================================================================
    #
    # ⚠⚠ 先纠正一个曾经的误判（v7.8.3 前我写错在这里）
    # ------------------------------------------------
    # 曾经把「新版报频率太高、旧版能跑」归因成"新版每轮重取 /gateway
    # 导致把配额打穿"。**这个归因是错的**。真实因果链是：
    #
    #   本地 intents 发了 0（见 _resolve_intents）
    #     → 服务端每次 Identify 都以 4013 拒掉
    #     → 连接秒级断开 → 秒级重连
    #     → 每秒打 2~3 次 /gateway
    #     → 才撞出 code 100017「接口调用超过频率限制」
    #
    # 也就是说：**限流是重连风暴的果，不是因**。日志里 `响应码: 200`
    # 紧跟着 `响应码: 400` 就是证据 —— 同一秒内前几次是好的，
    # 是因为风暴把配额瞬间打爆了。
    #
    # 所以主因修复是 intents；下面的节流**保留**，但定位是纵深防御：
    #
    #   即使将来出现别的重连风暴（网络抖动、服务端主动踢、intents 又配错），
    #   也不至于把 /gateway 的配额打穿，避免"一个故障放大成两个"。
    #
    # 修法
    # ----
    #   1. **缓存**：正常掉线复用上次拿到的地址（消掉绝大多数重复调用）；
    #   2. **失效标记**：鉴权失败、或握手在建立阶段就失败（地址很可能
    #      已作废）时，置 ``_gateway_force_refresh``，下一轮强制重取 ——
    #      这保住了「地址坏了能自愈」，没有退回旧版"一旦掉线就再也连不上"的坑；
    #   3. **最小间隔**：两次真实调用之间强制冷却 ``gateway_min_interval`` 秒
    #      （默认 30）。这是兜底闸门。
    # =====================================================================

    # 两次真实 /gateway 调用之间的最小间隔（秒）。
    # 30 秒是保守值：官方没公开该接口配额，但按「正常机器人一天重连
    # 几次」的量级，30 秒冷却足以正常运作，同时把风暴压死在配额之下。
    _GATEWAY_MIN_INTERVAL = 30.0

    def _gateway_min_interval(self) -> float:
        """读取最小调用间隔，下限 0（允许用户关掉冷却，但默认开着）。"""
        try:
            v = float(self.config.get("gateway_min_interval",
                                      self._GATEWAY_MIN_INTERVAL))
        except Exception:
            v = self._GATEWAY_MIN_INTERVAL
        return max(0.0, v)

    async def _get_gateway_url_cached(self, *, force: bool = False) -> str:
        """按需取 gateway 地址：缓存优先 → 失效强制重取 → 冷却兜底。

        返回值语义与 ``get_gateway_url()`` 一致（直接给可连接的 URL）。
        被限流时**原样抛出** ``_QQRateLimited``，交给上层走长退避 ——
        这里不吞异常，否则限流信号就断了。

        ⚠ 日志用 ``self.logger``，**不要**用 ``self._log`` ——
        ``_log`` 是 ``QQBotAPI``（HTTP 层）的方法，``QQBotGateway``
        没有它。写了 ``self._log`` 会在运行时 AttributeError，
        而这个异常恰好发生在异常处理路径上，表现是「连不上且日志空白」，
        极难定位。（这个坑本文件顶部已经踩过一次，见模块头注释。）
        """
        now = time.time()
        interval = self._gateway_min_interval()

        # ① 冷却检查放在最前：即使 force，也不能突破冷却。
        #    这是兜底闸门 —— force 表达的是"我想重取"，不代表"我可以
        #    无视配额"。地址通常缓存 2 小时以上才自然作废，30 秒冷却
        #    在实践中不会挡住任何真实需求。
        fresh_enough = (now - float(self._gateway_fetched_at or 0.0)) < interval

        if self._gateway_url and not force:
            # 有缓存且未要求强刷 → 直接用，零调用。
            return self._gateway_url

        if fresh_enough and self._gateway_url:
            # 想重取，但冷却没过 → 先用旧地址顶着。
            # 之所以敢这么干：地址作废的表现是"连上就断"，外层会立刻
            # 再次进入本函数，等冷却过去自然就换了。用旧地址尝试一次
            # 的成本，远小于把配额打穿。
            self._debug_gw("冷却未过，复用缓存地址")
            return self._gateway_url

        url = await asyncio.to_thread(self._api.get_gateway_url)
        self._gateway_url = url
        self._gateway_fetched_at = time.time()
        self._gateway_force_refresh = False
        return url

    def _debug_gw(self, message: str) -> None:
        """网关层调试日志（安全版）：logger 可能为 None 或非标准对象。"""
        logger = getattr(self, "logger", None)
        if logger is None:
            return
        try:
            logger.debug(f"[调试-GATEWAY] {message}")
        except Exception:
            pass

    # ★ 群聊机器人必需的事件位。见 _resolve_intents 的说明。
    #   GROUP_AND_C2C_EVENT (1<<25) 覆盖：
    #     GROUP_AT_MESSAGE_CREATE（群 @ 消息）、C2C_MESSAGE_CREATE（单聊）等
    _DEFAULT_INTENTS = 1 << 25

    def _resolve_intents(self) -> int:
        """解析 Identify 要发的 intents 值。

        ★★ 为什么默认值是 (1<<25) 而不是 0
        ---------------------------------
        这是 v7.8.2 引入的一个**会让机器人彻底连不上**的 bug，
        根因是把官方文档读岔了：

          * 官方位表：``GROUP_AND_C2C_EVENT (1 << 25)`` 定义了一组事件，
            其中就包含群聊机器人最需要的 ``GROUP_AT_MESSAGE_CREATE``。
          * 误读成"``(1<<25)|(1<<24)`` 里 1<<24 是保留位，所以置 0 更干净"，
            于是改成 ``int(self.config.get("intents", 0) or 0)``。
          * 但 core 的默认配置里**从来没有** ``intents`` 这个键 →
            ``.get(..., 0)`` 永远返回 **0** → Identify 发 intents=0。
          * 发 0 的语义是"**不订阅任何事件**"，不是"默认事件集"。
            服务端收到后直接以关闭码 **4013 invalid intents** 拒绝连接。

        症状对比（用户实测）：
          * 旧版（发 ``(1<<25)|(1<<24)``）：1<<25 正确 → 连接稳定、群消息正常，
            只在网络超时后重连一次。
          * 新版（发 0）：每次连接都被 4013 当场掐断 → 每秒重连 2~3 次 →
            重连风暴顺带把 ``/gateway`` 打到限流（code 100017）→
            表现成"一直连不上 + 报频率太高"。

        所以：默认必须是 ``1 << 25``。配置里若显式给了 ``intents``，
        以配置为准（允许用户按官方位表精细化）。
        """
        raw = self.config.get("intents", None)
        if raw is None:
            return self._DEFAULT_INTENTS
        try:
            val = int(raw)
        except Exception:
            return self._DEFAULT_INTENTS
        # 0 是已知会导致 4013 的值 —— 视为"未配置"，回落到默认位。
        # （发 0 等于不订阅，服务端必拒；没有场景需要它。）
        if val == 0:
            return self._DEFAULT_INTENTS
        return val

    def _invalidate_gateway(self, reason: str = "") -> None:
        """标记网关地址疑似失效，下轮强制重取（仍受冷却约束）。"""
        self._gateway_force_refresh = True
        if reason:
            self._debug_gw(f"标记地址失效（{reason}）")

    @classmethod
    def _looks_rate_limited(cls, text: str) -> bool:
        """转发到模块级 ``looks_rate_limited``（保留旧调用口径）。

        ⚠ 本方法**只是别名**，真正的实现和常量都在模块级。
        当初把它当成 QQBotGateway 的独有方法、然后在 QQBotAPI 里
        `self.` 调用，直接导致机器人 100% 连不上（AttributeError）。
        跨类复用的纯函数必须放模块级。
        """
        return looks_rate_limited(text)

    def _is_rate_limited(self, exc) -> bool:
        """异常是否是限流类的（HTTP 400 / code 100017 / err_code 40023001）。"""
        try:
            # 只在限流特征**同时**出现时才判限流，避免把普通 400 误判。
            return self._looks_rate_limited(str(exc))
        except Exception:
            return False

    def reset_circuit(self, reason: str = "") -> None:
        """人工复位熔断/限流状态。

        ★ 为什么要单独开一个方法
        -----------------------
        原先的复位写在 ``start()`` 里，而 ``start()`` 会被监督线程间接
        反复调用 —— 复位和熔断互相抵消。现在复位只允许由**用户动作**
        触发（面板重新保存机器人配置 / 手动重置），并且不经过 start()。
        """
        self._intent_fail_streak = 0
        self._intent_circuit_open = False
        self._rate_limit_streak = 0
        if reason:
            self.logger.info(f"[QQ] 熔断/限流状态已复位（{reason}）")

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

        # ★ 文本兜底。
        #
        # 原实现写的是 ``"4004" in s``。实测关闭异常的字符串是
        #   ``sent 1000 (OK); then received 4013 (private use) invalid intents``
        # —— 4004 根本不会出现，所以这个兜底是**死代码**，4013/4014
        # 也只靠 class 常量命中。现在改成按**关键字**匹配，覆盖真实文本。
        s = (str(exc) or "").lower()
        if "authentication" in s or "invalid token" in s or "token invalid" in s:
            return True
        # 显式排除 intents 类，避免 4013 的文本描述里恰好含关键词时误判
        if self._is_intent_failure(exc):
            return False
        # 裸关闭码文本形态：形如 "then received 4004 (authentication failed)"
        for code in self._AUTH_CLOSE_CODES:
            if code == 4013:
                # 4013 由 _INTENT_CLOSE_CODES 优先接管，这里不再认它
                continue
            if f"received {code}" in s or f"code={code}" in s:
                return True
        return False

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
        # ★★ 这里**绝不能**复位熔断状态。
        #
        # 教训：早期版本在此写下 ``self._intent_circuit_open = False``，
        # 想让「重新保存配置」能恢复。但 ``start()`` 会被监督线程间接
        # 反复走到（worker 退出 → 监督线程重启 → 新一轮），于是熔断
        # 每次都被清掉，实测 38 秒熔断 7 次却一直在重连。
        #
        # 恢复路径改为显式调用 ``reset_circuit()``：
        #   * 面板重新保存机器人配置 → core._restart_qq_gateway → 重建
        #     新的网关实例（新实例天然是干净的初始状态）；
        #   * 或人工调用 ``reset_circuit("手动重置")``。
        # 同一个实例内反复 start() 属于「监督线程自动重启」，保留熔断态。
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
        """监督线程：worker 退出后按策略重启它。

        ★★ 必须尊重熔断态（这是早期版本最严重的缺陷）
        ---------------------------------------------
        worker 里的熔断只是 ``break`` 出自己的循环，**管不到监督线程**。
        早期实现里监督线程看到 worker 死了就无条件 ``_launch_worker()``，
        配上 ``start()`` 里的无条件复位，熔断完全形同虚设。

        现在的取值优先级：
          1. 熔断态 → 用 ``_circuit_retry_seconds()``（默认 300 秒）慢重试；
          2. 限流态（``_rate_limit_streak > 0``）→ 用 ``_rate_limit_backoff()``
             阶梯退避（60 → 120 → 240 → 600 封顶）；
          3. 常态 → 原有指数退避（``_next_backoff()``）。
        """
        self.logger.info("[QQ] 网关监督线程已启动")
        while not self._supervisor_stop.is_set():
            if self._worker_thread and self._worker_thread.is_alive():
                self._supervisor_stop.wait(1.0)
                continue
            if self._supervisor_stop.is_set():
                break

            if self._intent_circuit_open:
                delay = self._circuit_retry_seconds()
                self.logger.warning(
                    f"[QQ] 处于熔断态（intents 配置未被服务端接受），"
                    f"改为每 {delay:.0f} 秒慢重试一次。"
                    f"修复后建议在面板重新保存机器人配置以立即恢复。"
                )
            elif self._rate_limit_streak > 0:
                delay = self._rate_limit_backoff()
                self.logger.warning(
                    f"[QQ] 连续第 {self._rate_limit_streak} 次被服务端限流，"
                    f"退避 {delay:.0f} 秒后重试"
                )
            else:
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
        except _QQRateLimited as e:
            # ★ 别急着 return 走监督线程的普通指数退避 —— 限流要长退避，
            # 且必须让 _rate_limit_streak 递增，监督线程下一轮才会照着来。
            self._note_rate_limited(str(e))
            return
        except Exception as e:
            if self._is_rate_limited(e):
                self._note_rate_limited(str(e))
                return
            self.logger.error(f"[QQ] 获取 access_token 失败: {e}")
            return

        self._auth_fail_streak = 0
        # 会话恢复状态。官方协议：客户端要在内存里记最后一次收到的 s，
        # 重连时带 ``resume_gateway_url`` + ``session_id`` + 最后一条 ``seq``
        # 发 op 6 尝试续传；服务端回 op 0 的 RESUMED 表示续传成功。
        # 原来每次重连都硬发 op 2 Identify，等于每次都开新会话 ——
        # 断线期间的消息全部丢失；而且频繁 Identify 在官方那边有每日上限，
        # 反复掉线会把当天额度烧光，之后再也连不上。
        self._seq = None
        self._session_id = ""
        self._resume_url = ""
        self._resume_ok = False

        while not self._stop.is_set():
            # 每次循环开头都复位：供 except 里判断「是否真的进过连接体」。
            _entered = False
            try:
                # ★ 取 Gateway URL —— **按需 + 冷却**，不再每轮无脑重取。
                #
                # 历史教训（两代都踩过，这里要同时躲开两个坑）：
                #
                #   旧版：只在最外层取一次，掉线后一直用过期的带票据地址
                #         重连 → 「连上一次之后掉线，就再也连不上了」。
                #   新版：改成每轮都取 —— 修好了上面的坑，但在「事件订阅
                #         没配好、5 秒掉一次线」的场景下，把 /gateway 的
                #         调用频率放大成了 5 秒 1 次，直接撞穿官方配额
                #         （code 100017），表现为用户看到的「频率太高」。
                #
                # 现在两头都要：**正常掉线复用缓存**（消掉重复调用），
                # **地址疑似失效时强制重取**（保住自愈能力），外加
                # **最小间隔冷却**做兜底闸门。详见 _get_gateway_url_cached。
                force = bool(self._gateway_force_refresh)
                try:
                    gateway_url = await self._get_gateway_url_cached(force=force)
                except _QQRateLimited as e:
                    # ★ 限流：走长退避 + 明确日志，不做「睡 5 秒再来」。
                    delay = self._note_rate_limited(str(e))
                    await asyncio.sleep(delay)
                    continue
                except Exception as e:
                    if self._is_rate_limited(e):
                        delay = self._note_rate_limited(str(e))
                        await asyncio.sleep(delay)
                        continue
                    self.logger.error(f"[QQ] 获取 Gateway URL 失败: {e}")
                    # 取不到地址通常意味着票据/鉴权有问题，标记失效，
                    # 让下轮（在冷却允许时）重取，而不是一直用空值重试。
                    self._invalidate_gateway("获取失败")
                    await asyncio.sleep(float(self.config.get("reconnect_seconds", 5) or 5))
                    continue
                # 优先用服务端下发的 resume 地址（op 0 READY 里有），
                # 它专门用于会话续传；没有就退回普通 gateway 地址。
                connect_url = self._resume_url or gateway_url
                # ★ 用「是否成功进入连接体」区分两类异常：
                #   建连阶段就抛（握手失败）→ 地址大概率作废，标记失效；
                #   进入连接体之后抛 → 连接层的正常异常，不动地址缓存。
                #   不区分的话，任何一次网络抖动都会被当成"地址坏了"，
                #   于是又退化成"每轮都重取"。
                async with self.websockets.connect(connect_url, ping_interval=None) as ws:
                    _entered = True
                    self._connected.set()
                    self.logger.info("[QQ] WebSocket 已连接")
                    if not str(self.config.get("group_openid", "")).strip():
                        self.binding_code = self._generate_binding_code()
                        self.logger.warning(
                            f"[QQ] 尚未绑定工作群。请在任意邀请本机器人的群里发送绑定码 {self.binding_code} 完成自动绑定"
                        )

                    hello_interval = None
                    try:
                        first_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        first_pkt = json.loads(first_raw)
                        # Hello 既带来心跳间隔，也可能顺带带来一个 seq
                        self._track_seq(first_pkt)
                        if first_pkt.get("op") == 10:
                            try:
                                hello_interval = float(first_pkt["d"]["heartbeat_interval"]) / 1000.0
                            except Exception:
                                hello_interval = 40.0
                            self._start_heartbeat(ws, hello_interval)
                        else:
                            await self._handle_packet(first_pkt, ws)
                    except asyncio.TimeoutError:
                        self.logger.warning("[QQ] 等待网关 Hello 超时，将直接发送 Identify")
                    except Exception as e:
                        self.logger.warning(f"[QQ] 读取网关 Hello 失败: {e}")
                    if hello_interval is None:
                        # 没等到 Hello 也要起心跳，否则服务端会以为我们死了
                        self._start_heartbeat(ws, 40.0)

                    # 有可续传的会话就发 op 6 Resume，否则发 op 2 Identify
                    if self._can_resume():
                        self._resume_ok = False
                        await ws.send(json.dumps({
                            "op": 6,
                            "d": {
                                "token": f"QQBot {self._api.access_token}",
                                "session_id": self._session_id,
                                "seq": self._seq,
                            },
                        }))
                        self.logger.info(f"[QQ] 已发出 Resume（seq={self._seq}）")
                    else:
                        await ws.send(
                            json.dumps(
                                {
                                    "op": 2,
                                    "d": {
                                        "token": f"QQBot {self._api.access_token}",
                                        # ★★ intents —— 这里必须发 (1<<25)，不是 0！
                                        #
                                        # 官方位表（bot.q.qq.com/wiki/.../payload.html）：
                                        #   GROUP_AND_C2C_EVENT (1 << 25)
                                        #     ├─ GROUP_AT_MESSAGE_CREATE  群 @ 机器人消息
                                        #     ├─ C2C_MESSAGE_CREATE       单聊消息
                                        #     ├─ GROUP_ADD_ROBOT / GROUP_DEL_ROBOT
                                        #     └─ ...
                                        #
                                        # 群聊机器人要收的就是 GROUP_AT_MESSAGE_CREATE，
                                        # 它**就在 1<<25 这一位里**，没有别的位。
                                        #
                                        # ⚠ 血泪教训（v7.8.2 引入、v7.8.3 修复）
                                        # ----------------------------------
                                        # 曾把这里改成 `int(self.config.get("intents", 0) or 0)`，
                                        # 理由是"官方文档说 (1<<25)|(1<<24) 里 1<<24 是保留位，
                                        # 置 0 即默认事件集"。**这个理解是错的**：
                                        #   ① 配置里根本没有 "intents" 这一项
                                        #      （core 的默认配置从未定义它）→ 永远取到 0；
                                        #   ② 发 0 等于"不订阅任何事件"，不是"默认事件集"。
                                        #       服务端直接以关闭码 **4013 invalid intents** 拒绝。
                                        #   ③ 而旧版发的 (1<<25)|(1<<24) 里 1<<25 是对的，
                                        #       所以**旧版能正常收群消息** —— 这才是
                                        #       「旧版能用、新版不能用」的真正原因。
                                        #
                                        # 结论：1<<25 是必需位，绝不能省。(1<<24) 多余但
                                        # 官方并未拒绝，为稳妥起见只发干净的正确位。
                                        "intents": self._resolve_intents(),
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
                        self._track_seq(packet)
                        await self._handle_packet(packet, ws)
                    # 正常退出 while（self._stop）时清掉 resume 状态，下次全新 Identify
                    self._clear_session()
            except self.websockets.ConnectionClosed as e:
                self._connected.clear()
                code = getattr(e, "code", None) or getattr(getattr(e, "rcvd", None), "code", None)
                self.logger.warning(f"[QQ] 连接断开(code={code})，稍后重连: {e}")

                # ★ intents 类关闭码优先判定，且**必须短路**。
                #
                # 4013 同时出现在 _AUTH_CLOSE_CODES 里（历史遗留），
                # 如果让下面的鉴权分支先跑，就会去刷 token —— 而 intents
                # 问题刷 token 无效。实测表现是 38 秒刷 7 次 token，
                # 日志全是「已刷新 access_token」，真正的病因被淹没。
                if code in self._INTENT_CLOSE_CODES or self._is_intent_failure(e):
                    await self._handle_intent_failure(code)
                    if self._stop.is_set():
                        break
                    # ★ 熔断已开 → 退出 worker 循环。
                    #
                    # ⚠ 注意这里的语义已从「彻底停死」改成「交给监督线程慢重试」。
                    # 早期版本在此 `break` 并声称「停止自动重连」，但监督线程
                    # 转头就把 worker 复活了，而且 start() 还会复位熔断 ——
                    # 实际效果是 38 秒重连 7 次，比不熔断还糟（把官方限流撞了出来）。
                    # 现在真正的节流点是 _supervisor_loop 对 _intent_circuit_open
                    # 的判定，这里 break 只是让出本轮。
                    if self._intent_circuit_open:
                        self.logger.warning(
                            "[QQ] 熔断中，本轮不再立即重连（由监督线程按慢重试间隔拉起）"
                        )
                        break
                    continue

                # 4004 = token 无效，4006/4007/4009 = 会话/身份异常，必须重新鉴权
                if code in (4004, 4006, 4007, 4009) or self._is_auth_failure(e):
                    self.logger.warning("[QQ] 检测到鉴权失败，强制刷新 access_token 并重取 Gateway URL")
                    # 鉴权失败说明 resume 一定不成了，清掉会话状态
                    self._clear_session()
                    # ★ 鉴权换了新 token，旧地址的票据随之失效 → 强制重取。
                    #   这是「必须重取」的唯一硬场景（与旧版行为一致）。
                    self._invalidate_gateway(f"鉴权失败(code={code})")
                    try:
                        await asyncio.to_thread(self._api.get_access_token, True)
                        self.logger.info("[QQ] 已刷新 access_token，下轮重连将重取 Gateway URL")
                    except _QQRateLimited as e2:
                        # 刷新 token 本身被限流 —— 绝不能再马上重连
                        delay = self._note_rate_limited(str(e2))
                        await asyncio.sleep(delay)
                    except Exception as e2:
                        if self._is_rate_limited(e2):
                            delay = self._note_rate_limited(str(e2))
                            await asyncio.sleep(delay)
                        else:
                            self.logger.error(f"[QQ] 刷新 access_token 失败: {e2}")
                elif code in (4008,):
                    # 4008 = 发送过快被限流，退避久一点
                    delay = self._note_rate_limited(f"关闭码 4008（发送过快被限流）")
                    await asyncio.sleep(delay)
                else:
                    self._auth_fail_streak = 0
            except Exception as e:
                self._connected.clear()
                self._auth_fail_streak = 0
                # ★ 建连阶段就失败（没进过连接体）→ 地址可疑，标记失效。
                #   这是新版"每轮重取"本要解决的问题的正解：只在**真的
                #   连不上**时才换地址，而不是每轮都换。
                if not _entered and not self._stop.is_set():
                    self._invalidate_gateway(f"建连失败: {type(e).__name__}")
                if self._is_rate_limited(e):
                    delay = self._note_rate_limited(str(e))
                    await asyncio.sleep(delay)
                else:
                    self.logger.error(f"[QQ] 网关异常: {e}")
            if self._stop.is_set():
                break
            # ★ 常态退避。限流/熔断时**不再叠加**这里的 5 秒 ——
            # 上面各分支已经各自睡过（限流 60~600 秒），这里再睡是无害的
            # 小叠加，但会让「退避 60 秒」变成「65 秒」；熔断态下更是
            # 完全不该短睡，否则 worker 会以 5 秒节奏空转。
            if self._intent_circuit_open or self._rate_limit_streak > 0:
                continue
            try:
                await asyncio.sleep(float(self.config.get("reconnect_seconds", 5)))
            except Exception:
                await asyncio.sleep(5)

    def _note_rate_limited(self, detail: str = "") -> float:
        """记录一次限流并返回应退避的秒数（同时打明确日志）。

        ★ 为什么要单独一个方法
        ----------------------
        限流可能发生在三个完全不同的位置（取 token、取 gateway、刷新 token），
        三处的处理必须是**同一套**计数与日志，否则会出现「有的地方退避 60 秒、
        有的地方 5 秒」，把退避阶梯打得七零八落。收口在这里。
        """
        try:
            self._rate_limit_streak += 1
        except Exception:
            self._rate_limit_streak = 1
        delay = self._rate_limit_backoff()
        # 限流是「被服务端掐」的明确信号，级别用 error（而不是 warning）：
        # 它意味着继续高频请求会把封禁窗口越拉越长，需要用户看到。
        self.logger.error(
            f"[QQ] 被 QQ 开放平台限流（连续第 {self._rate_limit_streak} 次）：{detail} "
            f"→ 退避 {delay:.0f} 秒后重试。"
            f"常见诱因是短时间内反复重连/换 token；若频繁出现，请检查机器人"
            f"事件订阅配置，避免无意义重连。"
        )
        return delay

    def _is_intent_failure(self, exc) -> bool:
        """从异常文本里识别 intents 类错误（兜底，防止 code 拿不到）。"""
        s = (str(exc) or "").lower()
        return "invalid intents" in s or "invalid_intents" in s

    async def _handle_intent_failure(self, code):
        """intents 类关闭码的统一处理：计数 → 熔断（慢重试）→ 明确指引。

        ★ 4013 最可能的原因（按发生概率排序）
        ------------------------------------
        ① **本地发的 intents 值本身不对**（本次线上事故就是这一类：
           v7.8.2 发了 0，等于不订阅任何事件）。这是**代码问题**，
           用户去开放平台怎么配都没用 —— 提示必须点明这一点，
           否则用户会在后台白折腾。
        ② 开放平台后台未开通对应事件权限 / 误用测试环境 AppID。
        ③ 事件订阅方式选成了 HTTP 回调而非长连接。

        ★ 为什么不能继续无脑重连
        ------------------------
        重连只是把同一个错误原样再撞一次，而且每轮都会：
            * 发一次 HTTP 取 token（必要时）+ 一次取 Gateway URL；
            * 4013 是**秒级**关闭，于是几秒内就能打十几次 /gateway ——
              这本身就会把 ``code 100017`` 限流撞出来，把真正的病因
              （intents 值错）伪装成"频率太高"。
        日志也会被刷满，真正该被看到的提示被埋掉。

        ★ 熔断的语义：「慢重试」而不是「停死」
        ------------------------------------
        早期实现让熔断**彻底停止重连**，但监督线程会把 worker 复活，
        而 ``start()`` 里又有无条件复位 —— 结果是熔断完全失效。
        现在熔断 = 置 ``_intent_circuit_open``，由 ``_supervisor_loop``
        把重试间隔拉长到 ``_circuit_retry_seconds()``（默认 300 秒）。
        """
        self._intent_fail_streak += 1
        threshold = self._intent_circuit_threshold()

        if code in self._INTENT_CLOSE_CODES:
            hint = (
                f"[QQ] 关闭码 {code}：机器人事件订阅（intents）不被服务端接受。"
                "这**不是** token 问题，刷新凭据无效。请依次检查："
                "① 【最常见】本地发出的 intents 值是否正确 —— 群聊机器人"
                f"必须是 GROUP_AND_C2C_EVENT(1<<25)，当前发送值 = {self._resolve_intents()}"
                f"（{bin(self._resolve_intents())}）。若为 0 则服务端必定拒绝；"
                "② 开放平台后台是否已为该机器人开通所需事件（群 @ 消息等）；"
                "③ 是否误用了测试环境 AppID（测试环境事件权限受限）。"
            )
        else:
            hint = f"[QQ] 关闭码 {code}：疑似 intents 不被接受。"

        retry = self._circuit_retry_seconds()
        if self._intent_fail_streak < threshold:
            self.logger.error(
                f"{hint}（连续第 {self._intent_fail_streak}/{threshold} 次，"
                f"再失败 {threshold - self._intent_fail_streak} 次将进入熔断，"
                f"熔断后改为每 {retry:.0f} 秒慢重试）"
            )
            return

        self._intent_circuit_open = True
        self.logger.error(
            f"{hint} 已连续 {self._intent_fail_streak} 次失败，**熔断：改为每 "
            f"{retry:.0f} 秒慢重试**（不再高频重连，避免触发官方限流）。"
            "请在开放平台修好事件订阅后，回到面板重新保存机器人配置以立即恢复连接。"
        )
        # 熔断状态下不在出站队列里堆积消息 —— 反正发不出去，
        # 堆到 maxlen 上限只会把更早的真实待发消息挤掉。
        try:
            with self._send_lock:
                self._send_queue.clear()
        except Exception:
            pass

    def _track_seq(self, packet):
        """记录服务端下发的序列号，供 Resume 使用。"""
        try:
            s = packet.get("s")
            if s is not None:
                self._seq = s
        except Exception:
            pass

    def _clear_session(self):
        self._session_id = ""
        self._resume_url = ""
        self._seq = None
        self._resume_ok = False

    def _can_resume(self) -> bool:
        return bool(self._session_id) and self._seq is not None and not self._resume_ok

    def _start_heartbeat(self, ws, interval):
        """起一个心跳任务；同一连接内只允许一个。"""
        try:
            prev = getattr(self, "_hb_task", None)
            if prev is not None and not prev.done():
                prev.cancel()
        except Exception:
            pass
        try:
            self._hb_task = asyncio.ensure_future(self._heartbeat(ws, interval))
        except Exception:
            pass

    async def _handle_packet(self, packet, ws):
        try:
            op = packet.get("op")
            if op == 10:
                try:
                    interval = float(packet["d"]["heartbeat_interval"]) / 1000.0
                except Exception:
                    interval = 40.0
                self._start_heartbeat(ws, interval)
            elif op == 11:
                # Heartbeat ACK。收到说明连接还活着，把「没回 ACK」的计数清零。
                self._hb_ack = True
            elif op == 7:
                # Reconnect：服务端主动要求重连（不是错误）。直接关闭当前连接，
                # 外层 while 会带上 session_id/seq 走 Resume。
                self.logger.info("[QQ] 服务端要求重连(op 7)")
                try:
                    await ws.close()
                except Exception:
                    pass
            elif op == 9:
                # Invalid Session。d=True 表示可恢复 → 等待 1~5s 再 Resume；
                # d=False 表示会话彻底失效 → 清状态走全新 Identify。
                resumable = bool((packet.get("d") if isinstance(packet.get("d"), bool)
                                  else (packet.get("d") or {}).get("resumable", False)))
                if resumable:
                    self.logger.warning("[QQ] 会话无效但可恢复，稍后 Resume")
                    self._resume_ok = False
                    await asyncio.sleep(random.uniform(1.0, 5.0))
                else:
                    self.logger.warning("[QQ] 会话已失效，需重新 Identify")
                    self._clear_session()
                try:
                    await ws.close()
                except Exception:
                    pass
            elif op == 6:
                # 服务端确认 Resume 成功（部分实现用 op 6 回执）
                self._resume_ok = True
                self._auth_fail_streak = 0
                self.logger.info("[QQ] 会话已恢复(RESUMED)")
            elif op == 0:
                t = packet.get("t")
                if t == "READY":
                    self._auth_fail_streak = 0
                    self._resume_ok = True
                    d = packet.get("d") or {}
                    # 记下 session_id / resume 地址，供断线续传
                    self._session_id = str(d.get("session_id", "") or self._session_id)
                    ru = str(d.get("resume_gateway_url", "") or "")
                    if ru:
                        self._resume_url = ru
                    self.logger.info("[QQ] 机器人已就绪")
                    # ★ 连上且完成鉴权 = intents 配置这次是被接受的，
                    # 清掉连续失败计数，避免「偶发一两次 → 后来修好了 →
                    # 计数还留着 → 某次抖动直接熔断」。
                    #
                    # ⚠ 这里是**唯一**允许自动清熔断的地方（自然恢复路径）。
                    # ``start()`` 里**绝不能**清 —— 那是监督线程会间接
                    # 反复走到的地方，清掉等于熔断永远不生效。
                    self._intent_fail_streak = 0
                    self._intent_circuit_open = False
                    # 连上了说明限流窗口已过，退避阶梯也随之归零。
                    self._rate_limit_streak = 0
                    try:
                        # 传入 self：播报要发到「我这张卡」的群，
                        # 不能靠 plugin.qq_bot（那只指向主卡）
                        self.plugin._notify_server_start_once(self)
                    except Exception as exc:
                        self.logger.warning(f"[QQ] 触发上线播报失败: {exc}")
                elif t == "RESUMED":
                    self._resume_ok = True
                    self._auth_fail_streak = 0
                    self.logger.info("[QQ] 会话恢复成功(RESUMED)，断线期间消息已补齐")
                elif t in ("GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
                    await self._handle_group_message(packet.get("d") or {})
        except Exception as e:
            self.logger.warning(f"[QQ] 处理消息包失败: {e}")

    async def _heartbeat(self, ws, interval):
        """心跳。

        ★ 两点修正：
        1. 心跳里要带上**最后收到的 seq**（``d`` 为 seq，不是 null）。
           不带 seq 的心跳在部分网关实现上会被判定为会话不同步。
        2. 官方要求：发心跳后没收到 op 11 ACK 就应断开重连。
           原实现发完就睡，连接已经半死（TCP 还在、对端已不处理）时
           会一直「假在线」——消息收不到也不重连。
        """
        try:
            while not self._stop.is_set():
                await asyncio.sleep(max(1.0, interval))
                # 上一轮没收到 ACK → 连接不可信，主动断开交给外层重连
                if getattr(self, "_hb_ack", True) is False:
                    self.logger.warning("[QQ] 心跳未收到 ACK，主动断开并重连")
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    break
                self._hb_ack = False
                try:
                    await ws.send(json.dumps({"op": 1, "d": self._seq}))
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
            msg_id = str(data.get("id") or "")
            # ★ 入站去重。
            # 断线重连 / Resume 补齐时，服务端可能把我们已经处理过的消息
            # **再推一次**。没有去重表就会出现「同一条群消息在游戏里刷两遍、
            # 关键词回复发两遍、连签到都被记两次」。按消息 id 去重最稳。
            if msg_id:
                now = time.time()
                with self._seen_msg_lock:
                    # 顺手清掉过期项，避免无界增长
                    if len(self._seen_msgs) > self._SEEN_MSG_MAX:
                        for k, ts in list(self._seen_msgs.items()):
                            if now - ts > self._SEEN_MSG_TTL:
                                self._seen_msgs.pop(k, None)
                        while len(self._seen_msgs) > self._SEEN_MSG_MAX:
                            try:
                                self._seen_msgs.popitem(last=False)
                            except Exception:
                                break
                    if msg_id in self._seen_msgs:
                        self.logger.warning(f"[QQ] 收到重复消息 id={msg_id[:16]}…，已忽略")
                        return
                    self._seen_msgs[msg_id] = now
            attachments = self._extract_attachments(data)
            # ★ 官方消息体里 content 对**某些消息类型**是空的：
            #   msg_type / message_type = 1 是「图文混排（含卡片/Ark）」，
            #   文本在这类消息里可能整段缺失，只有 attachments 或原始
            #   raw_content。原实现完全不看这个字段，于是带卡片的消息
            #   在面板日志里显示为空、关键词匹配也全部失效。
            # 这里做一次兜底：从已知的备用字段里把文本捡回来。
            if not content:
                _mt = data.get("message_type")
                try:
                    _mt = int(_mt) if _mt is not None else 0
                except Exception:
                    _mt = 0
                for _alt in ("raw_content", "text", "message", "plain_text"):
                    _v = data.get(_alt)
                    if isinstance(_v, str) and _v.strip():
                        content = _v.strip()
                        self.logger.info(f"[QQ] message_type={_mt} 的正文为空，已从 {_alt} 兜底取回文本")
                        break
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

            # ---- 用途闸门 ----
            #
            # ★ 位置极其关键：**必须晚于**上面所有"运维类"处理。
            #
            #   这个位置之前已经处理完：群绑定/验证码、图片派发、
            #   账号绑定码、签到、设置群名、/ 管理员指令 —— 这些是
            #   **管理机器人本身**要做的事，跟"这张卡拿来干嘛"无关。
            #   一张用途填「空着」的卡，如果连绑定码都发不出去，
            #   用户就没法把它接进自己的群，第三方插件也接管不了。
            #
            #   而下面的部分才是真正的"群服互通业务"：
            #   正则规则、自定义关键词、状态查询、QQ→MC 转发。
            #   用途不是 bridge 的卡，这些一律不该做：
            #     · broadcast：收玩家聊天进游戏毫无意义，而且会把
            #       一个"只发公告"的群变成双向聊天通道；
            #     · reserved：第三方插件明确要求独占，我们多嘴转发
            #       一条就是往别人的管线里插数据。
            #
            #   注意这里**不 return**，而是继续往下但不做互通 ——
            #   下面还有图片钩子之后的收尾逻辑，直接 return 会漏掉。
            #   所以用一个显式布尔量，让下面每个互通分支自己判断。
            _purpose = "bridge"
            try:
                _cid = str(getattr(self, "_card_id", "") or "")
                _mgr = getattr(self.plugin, "conn_mgr", None)
                if _mgr is not None and _cid:
                    _purpose = str(_mgr.purpose_of(_cid) or "bridge")
                else:
                    _inst = getattr(self, "_card_purpose", None)
                    if isinstance(_inst, str) and _inst:
                        _purpose = _inst
            except Exception:
                _purpose = "bridge"
            _relay_ok = (_purpose == "bridge")

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
            #
            #   ★ 用途闸门：规则属于"互通业务"，非 bridge 卡直接跳过整块。
            #     见上面 _relay_ok 的注释。
            if _relay_ok:
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
            if _relay_ok and isinstance(ck, dict):
                for kw in ck:
                    if kw and str(kw) in content:
                        await self._send_qq_text(await self._expand_keyword_reply(ck[kw]), msg_id)
                        return

            if _relay_ok and any(k in content for k in ("查状态", "服务器状态", "状态", "在线", "tps")):
                await self._send_qq_text(await self._get_status_text(), msg_id)
                return

            content = self._filter_banned(content)
            if _relay_ok and self.config.get("qq_to_mc", True):
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

    # 出站队列限流时的重试策略（见 _drain_send_queue_async）
    _SEND_RETRY_MAX = 5
    _SEND_RETRY_WAIT = 2.0

    def send_qq_text(self, content, msg_id="", broadcast=False):
        """把一条文本塞进出站队列。

        ``broadcast=True`` 才会扇出到全部互联群；默认只发主群/原群。
        这个参数是**关键字**，老的 ``send_qq_text(text)`` / ``(text, msg_id)``
        调用行为不变（默认不广播），但语义比原来更保守 —— 原来是无条件广播。
        """
        if self._supervisor_stop.is_set():
            return
        try:
            with self._send_lock:
                self._send_queue.append({"content": str(content),
                                         "msg_id": str(msg_id or ""),
                                         "broadcast": bool(broadcast)})
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
            tries = int(item.get("_tries", 0) or 0)
            if not self.limiter.allow_send(len(content.encode("utf-8")) + 64):
                # ★ 原来是 ``continue`` —— 直接丢弃这条消息。
                # 限流是**暂时**的，消息本身没毛病；丢掉等于用户看到
                # 「机器人偶尔不回话」，且没有任何日志线索。
                # 现在改成退避重排：等一个令牌周期再试，最多重试 N 次。
                if tries < self._SEND_RETRY_MAX:
                    item["_tries"] = tries + 1
                    with self._send_lock:
                        self._send_queue.appendleft(item)
                    self.logger.warning(
                        f"[QQ] 出站限流，{self._SEND_RETRY_WAIT:.1f}s 后重试"
                        f"（第 {tries + 1}/{self._SEND_RETRY_MAX} 次）"
                    )
                    await asyncio.sleep(self._SEND_RETRY_WAIT)
                    continue
                self.logger.error(
                    f"[QQ] 出站限流重试 {self._SEND_RETRY_MAX} 次仍失败，丢弃一条消息"
                )
                continue
            await self._send_qq_text(content, str(item.get("msg_id", "")),
                                     broadcast=bool(item.get("broadcast", False)))

    async def _send_qq_text(self, content, msg_id="", broadcast=False):
        if self._api is None:
            return
        # ★ 默认「只发一个群」。
        #
        # 原实现无条件 ``targets = self._bound_group_openids()``，
        # 意味着**任何**一次发送都会扇出到所有已绑定群。被动回复更糟：
        # 玩家在 A 群 @ 了机器人，机器人会把回复同时发到 A、B、C 三个群 ——
        # ① 玩家看到「我没问它，它在我群里刷屏」；
        # ② 每条被动回复在不同群里各自消耗一次 msg_seq 额度，
        #    官方「同一 msg_id 最多回 5 条」的策略按 msg_id 算，
        #    多群扇出会瞬间把额度打光，导致真正需要回复的那个群反而收到 40054。
        #
        # 现在的语义：
        #   * 带 msg_id 的被动回复 → 只回原群（查 _reply_targets）；
        #   * broadcast=True（主动公告/上线播报）→ 才扇出到所有绑定群，
        #     且每个群各自限速，避免触发「单群 1000 条/天」的硬上限。
        targets = None
        if msg_id and str(msg_id) in self._reply_targets:
            origin = self._reply_targets.get(str(msg_id))
            if origin:
                targets = [origin]
        elif msg_id:
            # msg_id 不是我们登记过的被动回复目标 —— 说明它已经过期
            # （被动回复窗口只有 5 分钟）。继续带 msg_id 发只会拿到
            # 一条 40054「msg_id 无效」；这里显式转成主动消息更可控。
            self.logger.warning(f"[QQ] msg_id={str(msg_id)[:16]}… 不在被动回复窗口内，按主动消息发送")
            msg_id = ""
        if targets is None:
            if broadcast:
                targets = self._bound_group_openids()
            else:
                primary = str(self.config.get("group_openid", "") or "").strip()
                targets = [primary] if primary else self._bound_group_openids()
        if not targets:
            targets = [str(self.config.get("group_openid", "") or "").strip()]
        sent = []
        first = True
        for tid in targets:
            if not tid:
                continue
            # 广播时群与群之间留呼吸间隙：官方对**主动**消息有
            # 「单群 1000 条/天 + 每分钟 N 条」的限制，连发很容易被风控。
            if broadcast and not first:
                try:
                    await asyncio.sleep(0.6)
                except Exception:
                    pass
            first = False
            ms = self._next_msg_seq(msg_id) if msg_id else 0
            try:
                await asyncio.to_thread(
                    self._api.send_message_to, tid, {"type": "text", "content": content}, msg_id, ms
                )
                sent.append(tid)
            except Exception as e:
                self.logger.error(f"[QQ] 发送到群 {tid} 失败: {e}")
        return sent

    # ------------------------------------------------ 被动回复序号（msg_seq）
    #
    # 官方协议：同一条被动消息最多回 5 条，靠 ``msg_seq`` 区分。
    # 不传时服务端按 1 处理 —— 于是第 2 次回复会和第 1 次**撞车**：
    # 要么被拒（40054 / 重复消息），要么覆盖，表现为
    # 「机器人只回第一条，后面的回复全没了」。
    #
    # 这里维护 msg_id → 已用序号 的短表：
    #   * 序号从 1 递增；
    #   * 超过 5 条时不再带 msg_id（退化成主动消息），避免整条被丢弃；
    #   * 表满自动淘汰最旧的，防止内存无界增长。
    _MSG_SEQ_MAX = 5
    _MSG_SEQ_TABLE_MAX = 512
    # 入站消息去重窗口（秒）与容量
    _SEEN_MSG_TTL = 300.0
    _SEEN_MSG_MAX = 1024

    def _next_msg_seq(self, msg_id) -> int:
        key = str(msg_id or "")
        if not key:
            return 0
        with self._msg_seq_lock:
            n = int(self._msg_seq.get(key, 0)) + 1
            if n > self._MSG_SEQ_MAX:
                # 超出被动回复上限，返回 0 让调用方走主动消息
                return 0
            self._msg_seq[key] = n
            while len(self._msg_seq) > self._MSG_SEQ_TABLE_MAX:
                try:
                    self._msg_seq.popitem(last=False)
                except Exception:
                    break
            return n

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
