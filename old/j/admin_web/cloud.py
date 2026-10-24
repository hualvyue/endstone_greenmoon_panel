"""``cloud`` —— 云黑名单子系统（从 ``core.py`` 分离出来的独立模块）。

    from .cloud import CloudManager

    self.cloud_mgr = CloudManager(self)
    self.cloud_mgr.maybe_check(player)          # 进服查黑
    self.cloud_mgr.sync(payload)                # 异步上报封禁

设计要点
--------
* **注入依赖，不 import 插件类**。本模块只通过构造参数接收一个 plugin
  对象（鸭子类型），用它提供的 ``logger`` / ``_executor`` / ``server`` /
  ``_load_tools_config`` / ``_run_async_on_server_thread``。
  这样既不加剧 ``core.py`` 与本模块之间已有的双向依赖
  （``core`` 在顶部 import 本模块，``trust`` / ``cross`` 又 import ``core``），
  也让本模块可以被独立测试。

* **``http_json()`` 是本模块沉淀出的通用 HTTP 客户端**（纯 ``urllib``、
  零第三方依赖），供 AI 模块等复用。它保留了原 ``_cloud_request`` 的三条
  关键设计：超时必填、HTTPError 时读 body 提取 message、空/非 JSON 响应
  分别报明确错误。

* **端点自动协商**：云黑服务有新版 REST 与旧版 PHP 两套路径，用户不该为此
  操心。``_endpoint()`` 首次调用某逻辑名时按候选顺序探测，命中即缓存。

⚠ 迁出时保持行为完全不变 —— ``test_cloud_api.py``（77 条）是本次重构的
  主要安全网，任何行为差异都会被它抓到。
"""

from __future__ import annotations

import base64
import json
import urllib.error as _urllib_error
import urllib.parse
import urllib.request as _urllib_request
from typing import Any, Dict, List


def http_json(url: str, payload=None, method: str = "GET", timeout: float = 8.0,
              headers: Dict[str, str] | None = None, token: str = "",
              user_agent: str = "GreenMoon/0.11") -> Any:
    """通用 HTTP JSON 客户端（纯标准库，零第三方依赖）。

    与云黑协议**无关** —— 不涉及 base64 包装、不涉及逻辑名端点协商。
    云黑自己的 ``_request()`` 通过它发请求，AI 模块也复用它调 LLM。

    参数
    ----
    url     完整 URL
    payload 有值时：GET 会编码成 query，其余方法作为 JSON body 发出
    timeout 秒；**必填且有上限**（默认 8s，上限 60s）
    headers 额外请求头
    token   非空时自动加 ``Authorization: Bearer <token>``

    抛 ``RuntimeError``，错误信息尽量具体（含 HTTP 状态码的人话解释与
    服务端返回的 message）。
    """
    if not url:
        raise RuntimeError("请求地址为空")
    try:
        timeout = float(timeout)
    except Exception:
        timeout = 8.0
    # 上限：避免某个调用点写了个巨大的超时把线程占死
    timeout = max(1.0, min(timeout, 60.0))

    hdr = {"User-Agent": user_agent}
    if headers:
        for k, v in headers.items():
            hdr[str(k)] = str(v)
    if token:
        hdr["Authorization"] = "Bearer " + str(token)

    body = None
    if payload is not None:
        raw = json.dumps(payload, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        if method == "GET":
            sep = "&" if "?" in url else "?"
            url = url + sep + "data=" + urllib.parse.quote(
                base64.b64encode(raw).decode("ascii"), safe="")
        else:
            hdr["Content-Type"] = "application/json"
            body = raw

    req = _urllib_request.Request(url, data=body, headers=hdr, method=method)
    try:
        with _urllib_request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", "replace")
    except _urllib_error.HTTPError as e:
        # ★ 不能只报「HTTP 404」了事：服务端错误响应**常带 JSON body**
        #   （实测 401 {"ok":false,"message":"invalid token"}），
        #   把 message 提出来才能告诉用户到底哪儿错了。
        detail = ""
        try:
            raw = e.read().decode("utf-8", "replace")
            obj = json.loads(raw) if raw.strip() else None
            if isinstance(obj, dict):
                detail = str(obj.get("message") or obj.get("error") or "").strip()
        except Exception:
            detail = ""
        msg = f"HTTP {e.code}（{HTTP_HINT.get(int(e.code), str(e.reason))}）"
        if detail:
            msg += f"：{detail}"
        raise RuntimeError(msg)
    except _urllib_error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason}（连接失败）")
    except OSError as e:
        raise RuntimeError(f"连接失败: {e}")

    if not content.strip():
        raise RuntimeError("响应为空，请检查 API 地址是否正确（连接失败）")
    try:
        return json.loads(content)
    except Exception:
        raise RuntimeError(f"响应不是有效 JSON: {content[:120]!r}（连接失败）")


#: 错误码 → 人话。云黑文档明确区分 401/403/404/400，
#: 把这些翻译出来，用户才知道该去改 token 还是改地址还是改参数。
#: 同时也是 ``http_json`` 的公开常量，AI 模块可复用同一套解释。
HTTP_HINT = {
    400: "参数缺失或非法",
    401: "鉴权失败：Token / API Key 无效",
    403: "权限不足：该操作需要管理员 Token（只读 Key 不能执行）",
    404: "接口不存在：请检查 API 地址，或该服务不支持此功能",
    405: "请求方法不允许",
    429: "请求过于频繁，已被限流",
    500: "云端内部错误",
    502: "云端网关错误",
    503: "云端服务不可用",
}


class CloudManager:
    """云黑名单管理器。由 ``GreenMoonPlugin`` 在 ``on_enable`` 时实例化。"""

    def __init__(self, plugin):
        # ★ 构造时就捕获依赖，不要每处都 getattr(self.p, ...)：
        #   self.logger 是只读 property，构造后不会变；而 _executor 等
        #   在插件启用后才存在，所以由 host 保证构造顺序（on_enable 里建）。
        self.p = plugin
        self.logger = plugin.logger
        self._executor = getattr(plugin, "_executor", None)
        self.server = getattr(plugin, "server", None)
        #: 端点探测缓存：逻辑名 → 实际 URL
        self._ep_cache: Dict[str, str] = {}
        #: 缓存对应的（基址, 风格）快照；变化即作废
        self._ep_stamp = None

    # ------------------------------------------------------------ 依赖转发
    # 这些是 host 提供的能力，包一层便于统一与测试替换。
    def _load_tools_config(self) -> Dict[str, Any]:
        return self.p._load_tools_config()

    def _run_async_on_server_thread(self, fn, *a, **kw):
        return self.p._run_async_on_server_thread(fn, *a, **kw)

    _CLOUD_ENDPOINTS: Dict[str, List[str]] = {
        "check":    ["api/check_ban", "api/check_ban.php"],
        "create":   ["api/server_create_ban", "api/server_create_ban.php"],
        "revoke":   ["api/server_revoke_ban", "api/server_revoke_ban.php"],
        "validate": ["api/validate_token", "api/validate_token.php"],
        "list":     ["api/admin/list"],
        "stats":    ["api/admin/stats"],
        "whoami":   ["api/admin/whoami"],
    }

    _CLOUD_BASE_PLACEHOLDERS = (
        "https://your-domain.com", "https://your-domain.com/api",
        "https://example.com", "https://example.com/api",
    )

    #: 错误码→人话的表已提升为模块级 ``HTTP_HINT``（http_json 也用同一份），
    #: 保留这个别名只是为了让类内旧引用不必改，值恒等，不会漂移。
    _CLOUD_HTTP_HINT = HTTP_HINT

    CLOUD_EDITION = "bedrock"
    def _cfg(self) -> Dict[str, Any]:
        return self._load_tools_config().get("cloud_blacklist", {})

    @staticmethod
    def _b64(payload) -> str:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _base(self) -> str:
        cfg = self._cfg()
        base = str(cfg.get("api_base") or "").strip()
        if not base or base.rstrip("/") in [p.rstrip("/") for p in self._CLOUD_BASE_PLACEHOLDERS]:
            raise RuntimeError("未配置有效的云黑 API 地址（api_base），请先在「云黑名单」填写你的接口域名")
        return base.rstrip("/")

    def _url_candidates(self, kind: str) -> List[str]:
        """给出某个逻辑名的**全部**候选 URL，按优先级排序。

        基址写法要宽容：用户可能填
            https://host          （文档里 BASE 的写法，不带 /api）
            https://host/api      （旧版配置习惯，带 /api）
        两种都得能用。做法是把基址拆成「根」+「是否已带 /api」，
        再和路径组合：已带 /api 就直接拼；没带就补一个 /api。
        另外额外补一个「裸拼」候选兜底 —— 万一将来服务把端点挪到
        /api 之外，也不至于全盘失效。
        """
        base = self._base()
        paths = self._CLOUD_ENDPOINTS.get(str(kind))
        if not paths:
            raise RuntimeError(f"未知的云黑端点类型: {kind}")
        out: List[str] = []
        has_api = base.endswith("/api")
        root = base[:-len("/api")] if has_api else base
        for p in paths:
            if has_api:
                out.append(f"{root}/{p}")
            else:
                out.append(f"{root}/{p}")
                # 旧版习惯写法（base 已含 /api）也要能命中，避免用户改配置
                out.append(f"{root}/api/{p}")
        # 去重保序
        seen, uniq = set(), []
        for u in out:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        return uniq

    def _style(self) -> str:
        """取协议偏好：auto（默认）/ rest / legacy。"""
        st = str(self._cfg().get("api_style") or "auto").strip().lower()
        return st if st in ("auto", "rest", "legacy") else "auto"

    @staticmethod
    def _path_of(url: str) -> str:
        try:
            return urllib.parse.urlparse(str(url)).path or ""
        except Exception:
            return str(url)

    def _endpoint(self, kind: str) -> str:
        """解析某个逻辑名对应的**实际 URL**，带缓存。

        缓存键 = (kind)。探测一次成功后一直复用，不再重复试探 ——
        进服查黑是高频路径，每次都试错端点是纯浪费（而且失败那几次
        会让日志一片红）。缓存由 ``_reset_endpoints()`` 清空
        （用户在设置页改了地址/协议偏好时调用）。
        """
        cache = getattr(self, "_ep_cache", None)
        if cache is None:
            cache = {}
            self._ep_cache = cache
        style = self._style()
        # 换过地址或协议偏好 → 旧缓存作废
        stamp = (self._base(), style)
        if getattr(self, "_ep_stamp", None) != stamp:
            cache.clear()
            self._ep_stamp = stamp
        hit = cache.get(kind)
        if hit:
            return hit

        cands = self._url_candidates(kind)
        if style == "rest":
            cands = [u for u in cands if not self._path_of(u).endswith(".php")] or cands
        elif style == "legacy":
            cands = [u for u in cands if self._path_of(u).endswith(".php")] or cands

        # 探测：用**只读**请求试路径存在性，避免误触发写操作。
        # 对写类端点（create/revoke）不能真 POST 去试 —— 那会真的封个人上去。
        # 用 GET 试路径：新版/旧版在缺参数时都返回 400/401 而不是 404，
        # 足以区分「路径存在」与「路径不存在」。
        for url in cands:
            if self._probe(url):
                cache[kind] = url
                self.logger.info(f"[云黑] 端点协商：{kind} → {url}")
                return url
        # 全都探不通：退回首选候选，让真正的请求去报错（错误信息更具体）
        fallback = cands[0] if cands else ""
        cache[kind] = fallback
        return fallback

    def _probe(self, url: str) -> bool:
        """判断一个候选 URL 是否「存在」。

        只看 HTTP 状态：404 → 不存在；401/403/400/200 → 路径存在
        （只是参数或权限不对）。网络不通则一律返回 False，让调用方
        继续试下一个候选或最终报错。
        """
        cfg = self._cfg()
        timeout = float(cfg.get("timeout", 8) or 8)
        timeout = max(2.0, min(timeout, 6.0))       # 探测要快，别拖着
        req = _urllib_request.Request(url, headers={"User-Agent": "GreenMoon/0.11"}, method="GET")
        try:
            with _urllib_request.urlopen(req, timeout=timeout) as resp:
                return int(getattr(resp, "status", 200)) < 500
        except _urllib_error.HTTPError as e:
            return int(e.code) != 404
        except Exception:
            return False

    def _reset_endpoints(self) -> None:
        """清空端点探测缓存（改配置后调用）。"""
        self._ep_cache = {}
        self._ep_stamp = None

    def _request(self, kind, payload=None, method="GET", timeout=None,
                       token=None, require_token=False, url=None):
        """向云黑服务发一个请求，返回解析后的 JSON。

        ``kind`` 是**逻辑名**（check / create / revoke / validate / list /
        stats / whoami），实际 URL 由 ``_endpoint()`` 协商得出。

        ★ 兼容老调用点：凡是**不在逻辑名表里**的字符串，都当作路径片段
        直接拼到基址后面（老代码传的是 ``"check_ban.php"`` 这类端点名）。
        判定依据是「是否为已知逻辑名」而不是「有没有斜杠」——
        后者会漏掉 ``check_ban`` 这种既没斜杠也不带 .php 的写法。
        """
        cfg = self._cfg()
        if url is None:
            k = str(kind)
            if k in self._CLOUD_ENDPOINTS:
                url = self._endpoint(k)
            else:
                url = self._base() + "/" + k.lstrip("/")
        timeout = float(timeout if timeout is not None else cfg.get("timeout", 8))
        tk = token if token is not None else cfg.get("token")
        if require_token and (not tk or str(tk) in ("", "__SET__")):
            raise RuntimeError("未配置云黑 Token，请在「云黑名单」填入 API Token")
        if str(tk) in ("", "__SET__"):
            tk = None
        headers = {"User-Agent": "GreenMoon/0.11"}
        body = None
        data_b64 = None
        if payload is not None:
            data_b64 = self._b64(payload)
        if method == "GET":
            if data_b64:
                sep = "&" if "?" in url else "?"
                url = url + sep + "data=" + urllib.parse.quote(data_b64, safe="")
        else:
            headers["Content-Type"] = "application/json"
            body = json.dumps({"data": data_b64}).encode("utf-8") if data_b64 is not None else b"{}"
        if tk:
            headers["Authorization"] = "Bearer " + str(tk)
        req = _urllib_request.Request(url, data=body, headers=headers, method=method)
        try:
            with _urllib_request.urlopen(req, timeout=timeout) as resp:
                content = resp.read().decode("utf-8", "replace")
        except _urllib_error.HTTPError as e:
            # ★ 不能只报「HTTP 404」了事：新版所有错误响应**都带 JSON body**
            #   （实测 401 {"ok":false,"message":"invalid token"}），
            #   把 message 提出来才能告诉用户到底哪儿错了。
            detail = ""
            try:
                raw = e.read().decode("utf-8", "replace")
                obj = json.loads(raw) if raw.strip() else None
                if isinstance(obj, dict):
                    detail = str(obj.get("message") or obj.get("error") or "").strip()
            except Exception:
                detail = ""
            hint = self._CLOUD_HTTP_HINT.get(int(e.code), str(e.reason))
            msg = f"云端返回 HTTP {e.code}（{hint}）"
            if detail:
                msg += f"：{detail}"
            raise RuntimeError(msg)
        except _urllib_error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}（连接失败）")
        except OSError as e:
            raise RuntimeError(f"连接失败: {e}")
        if not content.strip():
            raise RuntimeError("云端响应为空，请检查 API 地址与端点是否正确（连接失败）")
        try:
            return json.loads(content)
        except Exception:
            raise RuntimeError(f"云端响应不是有效 JSON: {content[:120]!r}（连接失败）")

    def _fill_edition(self, p: Dict[str, Any], edition: str = None) -> Dict[str, Any]:
        """给上行 payload 补上 edition（不覆盖调用方显式给的值）。"""
        if not isinstance(p, dict):
            return p
        if not str(p.get("edition") or "").strip():
            p["edition"] = str(edition or self.CLOUD_EDITION)
        return p

    def _sync(self, payload: Dict[str, Any]):
        try:
            cfg = self._cfg()
            if not cfg.get("enabled"):
                return
            self._executor.submit(self._sync_worker, payload)
        except Exception as e:
            self.logger.warning(f"[云黑] 无法提交同步封禁任务: {e}")

    def _sync_worker(self, payload: Dict[str, Any]):
        try:
            cfg = self._cfg()
            tk = str(cfg.get("token") or "").strip()
            if not cfg.get("enabled") or not tk:
                return
            p = dict(payload or {})
            sn = str(cfg.get("server_name") or "").strip()
            if sn and not p.get("server_name"):
                p["server_name"] = sn
            if p.get("server_name") is None:
                p.pop("server_name", None)
            self._fill_edition(p, cfg.get("edition"))
            ident = p.get("player_name") or p.get("xuid") or p.get("ip_address") or "?"
            res = self._request("create", p, method="POST")
            ok = bool(res.get("ok"))
            msg = res.get("message") or ""
            self.logger.info(f"[云黑] 同步封禁 {ident}: {'成功' if ok else '失败 ' + str(msg)}")
        except Exception as e:
            self.logger.warning(f"[云黑] 同步封禁失败: {e}")

    def _unban_worker(self, payload: Dict[str, Any]):
        try:
            cfg = self._cfg()
            tk = str(cfg.get("token") or "").strip()
            if not cfg.get("enabled") or not tk:
                return
            p = dict(payload or {})
            sn = str(cfg.get("server_name") or "").strip()
            if sn and not p.get("server_name"):
                p["server_name"] = sn
            # ★ 撤销也要带 edition：新版两个分组的数据是隔离的，不指定分组
            #   时若记录落在 bedrock 组，按 xuid 能撤掉；但同一 IP 在 java 组
            #   也有一条时，不指定分组存在误撤对方的风险。显式点名只撤本组。
            self._fill_edition(p, cfg.get("edition"))
            ident = p.get("player_name") or p.get("xuid") or p.get("ip_address") or "?"
            res = self._request("revoke", p, method="POST")
            ok = bool(res.get("ok"))
            msg = res.get("message") or ""
            self.logger.info(f"[云黑] 同步解封 {ident}: {'成功' if ok else '失败 ' + str(msg)}")
        except Exception as e:
            self.logger.warning(f"[云黑] 同步解封失败: {e}")

    def _sync_unban(self, payload: Dict[str, Any]):
        try:
            cfg = self._cfg()
            if not cfg.get("enabled"):
                return
            self._executor.submit(self._unban_worker, payload)
        except Exception as e:
            self.logger.warning(f"[云黑] 无法提交同步解封任务: {e}")

    def _check_login(self, name: str, xuid, ip):
        try:
            cfg = self._cfg()
            if not cfg.get("enabled") or not cfg.get("check_on_login", True):
                return
            payload = {}
            if xuid:
                payload["xuid"] = str(xuid)
            if ip:
                payload["ip_address"] = str(ip)
            if not payload:
                return
            # ★ 显式声明分组。新版服务按「先标识、未命中再 IP」匹配：
            #   带 edition=bedrock 才能保证查的是基岩组里那条，不会撞到
            #   同一 IP 在 Java 组的记录；旧版忽略该字段，行为不变。
            self._fill_edition(payload, cfg.get("edition"))
            res = self._request("check", payload)
            if not (res.get("ok") and (res.get("data") or {}).get("banned")):
                return
            ban = (res.get("data") or {}).get("ban") or {}
            reason = str(ban.get("reason") or "你已被云黑名单封禁")
            src_srv = str(ban.get("server_name") or "").strip()
            km = str(cfg.get("kick_message") or "§c你在云黑名单中\n§7联合封禁系统 (UniteBan)")
            try:
                km = km.replace("{reason}", reason)
                # 新版 check_ban 会在 ban 里带 server_name（封禁来源服），
                # 一并支持 {server} 占位符，方便服主在踢出语里点明是哪个服封的。
                km = km.replace("{server}", src_srv or "未知")
            except Exception:
                pass

            def kick():
                try:
                    p = self.server.get_player(str(name))
                    if p is not None:
                        p.kick(km)
                except Exception:
                    pass

            self._run_async_on_server_thread(kick)
            origin = f"（来源：{src_srv}）" if src_srv else ""
            self.logger.info(f"[云黑] 玩家 {name} 命中云黑，已踢出：{reason}{origin}")
        except Exception as e:
            self.logger.warning(f"[云黑] 进服检查失败（放行）: {e}")

    def _test(self):
        """测试连接。返回 ``(ok, message, raw)``。

        新版与旧版的 ``validate_token`` 响应同构（``data.valid``），
        所以主流程不用分支。额外好处：新版有 ``whoami`` 能拿到 ``role``，
        顺手查一下告诉用户「你填的是管理员令牌还是只读 key」——
        只读 key 只能查黑、不能封禁，这是最常见的配置错误。
        """
        try:
            res = self._request("validate", method="GET", timeout=6, require_token=True)
            if not isinstance(res, dict):
                raise RuntimeError("云端响应格式异常（连接失败）")
            valid = bool((res.get("data") or {}).get("valid"))
            msg = str(res.get("message") or ("接口可达，Token 有效" if valid else "Token 无效"))
            role = ""
            if valid:
                try:
                    w = self._request("whoami", method="GET", timeout=6)
                    if isinstance(w, dict) and w.get("ok"):
                        role = str((w.get("data") or {}).get("role") or "").strip()
                except Exception as e:
                    # whoami 失败有三种正常/半正常情形，都不该当成连接错误：
                    #   1) 旧版服务没有这个端点            → 404 not found
                    #   2) 填的是**只读 Key**              → 403 或 401，body "forbidden"
                    #   3) 填的 key 本身是错的             → 401，body "invalid token"
                    # ★ 实测线上服务对只读 key 返回的是 **401 + {"message":"forbidden"}**，
                    #   而错误的 key 同样 401 但 body 是 "invalid token" —— 也就是
                    #   **状态码完全不足以区分**，必须看 body 里的 message。
                    #   （文档写的是 403，实际实现是 401；只按状态码判断会把
                    #   「只读 Key 配置正确」误报成「Token 无效」，正好把人引到
                    #   错误的方向，所以这里以 message 为准，状态码只作兜底。）
                    m = str(e)
                    low = m.lower()
                    if "forbidden" in low or "权限不足" in m:
                        role = "readonly"
                    self.logger.info(f"[云黑] whoami 未取到身份（{m}）")
            if role == "admin":
                msg += "；身份：管理员（可封禁/解封）"
            elif role == "readonly":
                msg += "；身份：只读 Key（仅可查黑，不能封禁/解封）"
            elif role:
                msg += f"；身份：{role}"
            else:
                # 旧版服务拿不到角色，但至少把填写方式说清楚，避免用户
                # 拿只读 key 去封禁、被拒后又找不到原因。
                msg += "；未能识别身份（旧版接口无此能力），封禁请确认填的是管理员 Token"
            raw = dict(res) if isinstance(res, dict) else {}
            if role:
                raw["_role"] = role
            return bool(valid and res.get("ok")), msg, raw
        except Exception as e:
            return False, str(e), None

    def _api_style_name(self) -> str:
        """把探测结果说成人话，供设置页展示。"""
        try:
            url = self._endpoint("check")
            p = self._path_of(url)
            if p.endswith(".php"):
                return "旧版 PHP 接口"
            if "/api/" in p:
                return "新版 REST 接口"
            return "自定义接口"
        except Exception:
            return "未配置"

    def _list_bans(self, edition: str = None):
        """拉取云黑明细（新版提供，旧版没有）。

        返回 ``(ok, message, bans)``。旧版服务上会 404 —— 这时返回
        ``ok=False`` + 一句「该云黑服务不支持列表查询」，而不是抛异常，
        前端好据此优雅降级（把说明文字换成一行提示，不弹错误）。
        """
        ed = str(edition or self.CLOUD_EDITION).strip().lower()
        if ed not in ("bedrock", "java"):
            ed = self.CLOUD_EDITION
        try:
            cfg = self._cfg()
            base = self._endpoint("list")
            sep = "&" if "?" in base else "?"
            # 标准只读 key 可作为默认：文档里 readonly 能访问 /api/admin/list
            tk = str(cfg.get("token") or "").strip()
            res = self._request(None, method="GET", url=f"{base}{sep}edition={ed}",
                                      token=(tk or "public"))
            if not isinstance(res, dict) or not res.get("ok"):
                return False, str((res or {}).get("message") or "查询失败"), []
            bans = ((res.get("data") or {}).get("bans")) or []
            return True, f"共 {len(bans)} 条", bans
        except RuntimeError as e:
            m = str(e)
            if "404" in m:
                return False, "当前云黑服务不支持列表查询（旧版接口）", []
            return False, m, []
        except Exception as e:
            return False, f"查询失败: {e}", []

    def _stats(self):
        """云黑统计（新版提供）。返回 ``(ok, message, data)``。"""
        try:
            cfg = self._cfg()
            tk = str(cfg.get("token") or "").strip()
            res = self._request("stats", method="GET", token=(tk or "public"))
            if not isinstance(res, dict) or not res.get("ok"):
                return False, str((res or {}).get("message") or "查询失败"), {}
            return True, "ok", (res.get("data") or {})
        except RuntimeError as e:
            m = str(e)
            if "404" in m:
                return False, "当前云黑服务不支持统计查询（旧版接口）", {}
            return False, m, {}
        except Exception as e:
            return False, f"查询失败: {e}", {}

    def _lookup(self, data: Dict[str, Any]):
        """手动查黑（排障用）：支持按 xuid 或按 IP 查。

        返回 ``(ok, message, ban)``，``ban`` 命中时为云端返回的 ban 对象。
        """
        d = data or {}
        xuid = str(d.get("xuid") or "").strip()
        ip = str(d.get("ip_address") or "").strip()
        if not xuid and not ip:
            return False, "请至少填写 XUID 或 IP", None
        payload: Dict[str, Any] = {}
        if xuid:
            payload["xuid"] = xuid
        if ip:
            payload["ip_address"] = ip
        self._fill_edition(payload, self._cfg().get("edition"))
        try:
            res = self._request("check", payload)
            if not isinstance(res, dict) or not res.get("ok"):
                return False, str((res or {}).get("message") or "查询失败"), None
            data_ = res.get("data") or {}
            if data_.get("banned"):
                return True, "命中云黑", (data_.get("ban") or {})
            return True, "未命中云黑", None
        except Exception as e:
            return False, str(e), None

    def maybe_check(self, player):
        """进服查黑。

        ⚠ 职责边界：**解析玩家标识（xuid / IP）不在这里做**。
        取 IP 需要 ``server.get_player`` 与 ``SocketAddress``，那是插件宿主
        的能力；而且这个插件里已经有唯一一份解析实现
        （``GreenMoonPlugin._resolve_player_ident``，``web.py`` 也在用）。
        真逻辑留在 core.py，本方法只做**转发 + 容错**，避免第二份实现
        各自漂移（比如一个读了 ``host``、一个读了 ``hostname``）。

        之所以保留这个方法而不直接删掉：``CloudManager`` 是对外的云黑
        门面，调用方不该被迫知道"标识解析在哪一层"。这里把它接回宿主。
        """
        resolver = getattr(self.p, "_resolve_player_ident", None)
        if resolver is None:
            return
        name = getattr(player, "name", None)
        if not name:
            return
        xuid, ip = resolver(str(name), _player=player)
        if not (xuid or ip):
            return

        cfg = self._cfg()
        if not cfg.get("enabled") or not cfg.get("check_on_login", True):
            return
        try:
            self._executor.submit(self._check_login, str(name), xuid, ip)
        except Exception as e:
            self.logger.warning(f"[云黑] 提交进服检查失败: {e}")

    def _cfg_raw(self) -> Dict[str, Any]:
        try:
            c = self._load_tools_config().get("cloud_blacklist", {}) or {}
            out = {
                "enabled": c.get("enabled"),
                "check_on_join": c.get("check_on_join"),
            }
            tk = c.get("token")
            out["token_set"] = bool(tk)
            api = str(c.get("api_base") or "")
            out["api_base"] = api
            return out
        except Exception:
            return {}
