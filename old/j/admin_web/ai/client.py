"""LLM 客户端：用**用户自己的** API（OpenAI 兼容端点）。

设计前提
========
**插件不提供 LLM 服务，不代理请求。** key 是服主自己填的、存在本机
``tools_config.json`` 里、请求从本机直接发往服主填的地址。

为什么这样设计（而不是"插件统一接一个后端"）：
  · 服主可以选择任意厂商/自建模型，不会被绑死；
  · 插件方不接触用户的 key，也不产生费用关系；
  · 代价是回答质量由用户选的模型决定 —— 这一点已经和用户确认过，
    **不是本模块能解决的问题**，也不该假装能解决。

因此本模块的重点不是"让回答更好"，而是：
  1. 网络层要**可靠**（超时不炸、错误可读、不静默失败）；
  2. 引入的新攻击面（SSRF）要**堵死**；
  3. 对模型输出**零信任**（它只是文本，永远不是指令）。

只用标准库
==========
``urllib`` 而不是 ``requests``：插件要能在没有第三方库的环境里跑
（这正是之前 ``websockets`` 缺失导致 QQ 网关卡住的原因）。
``cloud.py`` 的 ``http_json`` 已经是同一套路，但那边耦合了云黑的
响应格式与 ``data_b64`` 包装，所以这里重写一份更薄的 —— 不硬套。
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .gate import check_ssrf, is_safe_resolved_ip

#: 默认超时。LLM 推理比普通 HTTP 慢，8 秒（云黑那套）明显不够。
_DEFAULT_TIMEOUT = 45.0
#: 上限。再长就不该在主流程里等了，应该走后台任务 + 通知。
_MAX_TIMEOUT = 180.0

#: 响应体读取上限（字节）。防止一个恶意/异常端点返回 500MB 撑爆内存。
_MAX_BODY = 2 * 1024 * 1024

#: 状态码 → 人话。与 cloud.py 同样的思路：服主看到 401 不该去猜。
_HTTP_HINT = {
    400: "请求格式被拒绝（可能是模型名写错，或该端点不支持 function calling）",
    401: "鉴权失败：API Key 无效或已过期",
    403: "服务器拒绝访问：Key 没有该模型的权限，或地区被限制",
    404: "端点不存在：检查 base_url 是否少写/多写了 /v1",
    408: "服务端超时",
    413: "请求体过大：可能是上下文太长",
    429: "触发限流：稍后重试，或检查账户额度",
    500: "模型服务端内部错误",
    502: "网关错误（模型服务暂时不可用）",
    503: "服务不可用（可能正在过载）",
    504: "网关超时",
}

#: 请求头里必带的 UA。有些网关会按 UA 拦。
_UA = "GreenMoon/0.11 (+admin_web ai)"


class AIError(Exception):
    """AI 调用失败。消息面向用户，应当可直接显示在面板上。"""


class AIClient:
    """OpenAI 兼容的 chat/completions 客户端。

    兼容面：``/chat/completions`` 是事实标准，OpenAI、DeepSeek、
    Moonshot、通义、本地 vLLM/Ollama（兼容模式）都实现了它。
    不实现各家的私有协议 —— 那会让维护成本爆炸且没必要。
    """

    def __init__(self, plugin: Any = None, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.p = plugin
        self.logger = getattr(plugin, "logger", None)
        self._cfg = dict(cfg or {})

    # ------------------------------------------------------------ 配置
    def configure(self, cfg: Dict[str, Any]) -> None:
        self._cfg = dict(cfg or {})

    @property
    def base_url(self) -> str:
        return str(self._cfg.get("base_url") or "").strip().rstrip("/")

    @property
    def api_key(self) -> str:
        return str(self._cfg.get("api_key") or "").strip()

    @property
    def model(self) -> str:
        return str(self._cfg.get("model") or "").strip()

    @property
    def timeout(self) -> float:
        try:
            t = float(self._cfg.get("timeout") or _DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            t = _DEFAULT_TIMEOUT
        return max(5.0, min(t, _MAX_TIMEOUT))

    def validate_config(self) -> "tuple[bool, str]":
        """配置自检。**在保存配置时就调用**，别等第一次请求才报错。

        三个必填项缺一不可，且地址要先过 SSRF 闸门 ——
        把内网地址挡在配置阶段，而不是等到发请求时才失败。
        """
        if not self.base_url:
            return False, "未填写 API 地址（base_url）"
        ok, why = check_ssrf(self.base_url)
        if not ok:
            return False, f"API 地址不安全：{why}"
        if not self.api_key:
            return False, "未填写 API Key"
        if not self.model:
            return False, "未填写模型名（model）"
        return True, ""

    # ------------------------------------------------------------ 网络
    def _endpoint(self) -> str:
        """拼出 chat/completions 的完整地址。

        服主可能填 ``https://api.deepseek.com``（不带 /v1），
        也可能填 ``https://api.openai.com/v1``（带）。两种都要能用 ——
        让用户去记"该不该带 /v1"是最常见的配置失败原因。
        """
        base = self.base_url
        if not base:
            raise AIError("未配置 API 地址")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def _guard_dns(self) -> None:
        """建立连接前复查解析出来的 IP。

        ★ 这是 SSRF 的第二层防御。只在保存配置时查一次是不够的：
          域名可以在"保存时指向公网、请求时指向 127.0.0.1"（DNS rebinding）。
          这里显式解析一次，命中内网就拒绝。
        """
        host = urllib.parse.urlparse(self.base_url).hostname or ""
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception as e:
            raise AIError(f"无法解析 API 地址的主机名 {host}：{e}")
        for info in infos:
            ip = info[4][0]
            ok, why = is_safe_resolved_ip(ip)
            if not ok:
                raise AIError(f"API 地址解析结果不安全：{why}")

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> Dict[str, Any]:
        """发一次对话请求，返回原始响应 dict。

        ``tools`` 传 OpenAI function-calling 格式（见
        ``tools.tools_for_prompt()``）。模型可能返回 tool_calls，
        本方法**只负责返回**，不执行 —— 执行是 gate + 上层的事。

        所有异常统一收敛成 ``AIError``（消息可直接给用户看）。
        """
        ok, why = self.validate_config()
        if not ok:
            raise AIError(why)
        self._guard_dns()

        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": max(0.0, min(float(temperature), 2.0)),
            "max_tokens": max(1, min(int(max_tokens), 8192)),
        }
        if tools:
            body["tools"] = tools
            # 只允许"要么回答、要么调工具"，不允许两者混着来 ——
            # 混着来的响应各厂商实现不一致，容易踩坑。
            body["tool_choice"] = "auto"

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint(),
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": _UA,
                "Accept": "application/json",
            },
        )

        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(_MAX_BODY)
        except urllib.error.HTTPError as e:
            raise AIError(self._http_error_message(e))
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            raise AIError(f"连接失败：{reason or e}（检查地址是否可达、是否需要代理）")
        except socket.timeout:
            raise AIError(f"请求超时（{self.timeout:.0f} 秒）。模型可能过慢，或该端点不可达")
        except Exception as e:
            raise AIError(f"请求失败：{e}")

        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            preview = raw[:200].decode("utf-8", errors="replace")
            raise AIError(f"响应不是有效 JSON：{preview!r}")

        elapsed = time.time() - t0
        if callable(getattr(self.logger, "debug", None)):
            try:
                self.logger.debug(f"[AI] 请求完成，用时 {elapsed:.1f}s")
            except Exception:
                pass
        return parsed

    def _http_error_message(self, e: urllib.error.HTTPError) -> str:
        """把 HTTP 错误翻成人话，并且尽量带上服务端说的原因。

        服务端的 body 往往比状态码有用得多（比如"model not found"、
        "insufficient balance"），丢掉它用户就只能干猜。
        """
        code = int(getattr(e, "code", 0) or 0)
        hint = _HTTP_HINT.get(code, f"HTTP {code}")
        detail = ""
        try:
            raw = e.read(_MAX_BODY)
            if raw:
                txt = raw.decode("utf-8", errors="replace").strip()
                try:
                    obj = json.loads(txt)
                    if isinstance(obj, dict):
                        err = obj.get("error")
                        if isinstance(err, dict):
                            detail = str(err.get("message") or err.get("type") or "")
                        elif isinstance(err, str):
                            detail = err
                        detail = detail or str(obj.get("message") or "")
                except Exception:
                    detail = txt
                detail = detail.strip()[:300]
        except Exception:
            pass
        return f"HTTP {code}：{hint}" + (f"（服务端说明：{detail}）" if detail else "")

    # ------------------------------------------------------------ 响应解析
    @staticmethod
    def extract_text(resp: Dict[str, Any]) -> str:
        """从响应里取正文。取不到返回空串（**不抛异常**）。

        取不到是正常情况：模型这次只返回了 tool_calls。
        """
        try:
            choices = resp.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                # 有些实现把 content 返回成分段数组
                return "".join(
                    str(seg.get("text") or "")
                    for seg in content
                    if isinstance(seg, dict)
                )
            return str(content or "")
        except Exception:
            return ""

    @staticmethod
    def extract_tool_calls(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
        """取出模型请求的工具调用列表。

        返回 ``[{"id": str, "name": str, "arguments": dict}, ...]``。
        ``id`` 是模型为这次调用生成的标识，**必须原样回传** ——
        严格的 OpenAI 兼容端点在回喂 ``role=tool`` 消息时会校验
        ``tool_call_id`` 与 assistant 消息里 tool_calls 的 id 一致，
        合成一个假 id 会被直接判为 400。缺失时留空串，交由调用方兜底。
        ``arguments`` 是**模型生成的 JSON 字符串**，可能不是合法 JSON
        —— 这是模型的常见失败模式，所以解析失败时给出空 dict 并在
        结果里标记 ``parse_error``，交给 gate 去拒绝（未知参数校验
        会拦住它），而不是在这里抛异常中断整个流程。
        """
        out: List[Dict[str, Any]] = []
        try:
            choices = resp.get("choices") or []
            if not choices:
                return out
            msg = choices[0].get("message") or {}
            for tc in (msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                if not name:
                    continue
                raw_args = fn.get("arguments")
                args: Dict[str, Any] = {}
                parse_error = ""
                if isinstance(raw_args, dict):
                    args = raw_args
                elif isinstance(raw_args, str) and raw_args.strip():
                    try:
                        loaded = json.loads(raw_args)
                        if isinstance(loaded, dict):
                            args = loaded
                        else:
                            parse_error = "参数不是对象"
                    except Exception as e:
                        parse_error = f"参数不是合法 JSON: {e}"
                rec = {"id": str(tc.get("id") or ""),
                       "name": name, "arguments": args}
                if parse_error:
                    rec["parse_error"] = parse_error
                out.append(rec)
        except Exception:
            pass
        return out

    # ------------------------------------------------------------ 连通性测试
    def test(self) -> "tuple[bool, str]":
        """给设置页用的「测试连接」。返回 ``(是否可用, 提示)``。

        发一条最小请求即可 —— 目的是验证地址/Key/模型名三件套，
        不是验证回答质量。
        """
        try:
            resp = self.chat(
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=8,
            )
        except AIError as e:
            return False, str(e)
        except Exception as e:
            return False, f"未知错误：{e}"

        text = self.extract_text(resp)
        model = ""
        try:
            model = str(resp.get("model") or "")
        except Exception:
            pass
        who = f"（模型：{model}）" if model else ""
        if text:
            return True, f"连接正常{who}"
        # 没正文但也没报错：说明通了，只是这次没说话
        return True, f"连接正常{who}，但模型未返回文本内容"
