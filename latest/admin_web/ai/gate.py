"""闸门层：AI 提议 → 真正执行之间必须过的四道关。

分工
====
``tools.py``  定义「有哪些工具、各自多危险」
``gate.py``   定义「一次提议在什么条件下才被放行」
``client.py`` 只管把话说给模型听、把话带回来说

为什么闸门必须在代码里、不能写进提示词
======================================
提示词是**建议**，闸门是**约束**。把"不要执行危险操作"写进 system
prompt 只是让模型倾向于不做 —— 但模型会被注入、会犯错。真正拦得住
的是"代码里没有那条路径"。

四道关
======
1. **SSRF 校验**（``check_ssrf``）
   用户填的 ``base_url`` 是本机发请求的目标。如果允许指向
   ``127.0.0.1`` / ``169.254.169.254``（云元数据）等内网地址，
   这个功能就变成了一台"代理任意内网请求"的机器。
   ★ 注意：这条**不由模型控制**，只在保存配置时校验 —— 但仍是本模块
     责任的一部分，因为它是这个功能引入的新攻击面。

2. **工具白名单**
   只能调用 ``TOOL_REGISTRY`` 里的名字。模型编造的名字直接拒。

3. **参数校验**
   玩家名必须真实存在、时长必须是合法数字、枚举值必须在允许集合内。
   ★ 光靠"名字存在"不够：模型可能传 ``name="../server.properties"``
     这种想穿越路径的字符串，所以还要做字符集与长度校验。

4. **频率熔断**
   同一工具在窗口内被调用太多次就停掉。作用有两层：
   - 防模型陷入循环疯狂提议（会烧 API 额度，也是常见的模型故障模式）
   - 防"慢速暴力"：比如反复 kick 全部玩家来搞破坏

审计必须区分 AI 与人工
======================
AI 触发的每一步都要单独标注（``actor="ai"``）。理由：出事时第一件事
就是分清"是模型干的还是人干的"，事后从日志里猜是猜不准的。
"""
from __future__ import annotations

import ipaddress
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .tools import RiskLevel, get_tool, is_forbidden

#: 玩家名允许的字符集。BDS 玩家名基本是字母数字下划线，中文名也有但少见。
#: 用**白名单**而不是黑名单：黑名单永远漏（``..``、``%2e``、``\0`` 各种变体）。
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff]{1,32}$")

#: 通用字符串参数长度上限。防止模型往 reason 里塞几十 KB 导致
#: 日志/UI 被撑爆。
_MAX_STR_LEN = 500

#: 频率熔断参数。窗口内同一工具超过次数就拒绝。
_BURST_WINDOW = 60.0
_BURST_MAX = 10


# ---------------------------------------------------------------------------
# SSRF 防护
# ---------------------------------------------------------------------------

#: 明确禁止的网段。用 ipaddress 模块判断，而不是字符串前缀匹配 ——
#: 字符串匹配会被 ``http://127.0.0.1@evil.com``、十进制 IP
#: （``http://2130706433``）、IPv6 映射（``::ffff:127.0.0.1``）等方法绕过。
_BLOCKED_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),        # 本机
    ipaddress.ip_network("169.254.0.0/16"),     # link-local / 云元数据
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),           # 唯一本地地址
    ipaddress.ip_network("fe80::/10"),          # link-local
]


def check_ssrf(url: str) -> Tuple[bool, str]:
    """校验 URL 是否是安全的对外地址。返回 ``(是否放过, 原因)``。

    ★ 这个函数**不做 DNS 解析**。原因：
      · DNS 解析需要网络 IO，会让配置保存卡住；
      · 而且解析结果可能在请求时改变（DNS rebinding），
        校验过了也不代表请求时一定安全。
      真正的纵深防御是在 ``client.py`` 发请求时**再查一次**解析结果。
      这里做的是"挡住明显错误配置"，那是最常见的情况。

    ★ 也不允许非 http(s) 协议：``file://`` 能读本机文件，
      ``gopher://``/``dict://`` 能打内网服务，都是经典 SSRF 跳板。
    """
    raw = str(url or "").strip()
    if not raw:
        return False, "地址为空"

    try:
        p = urllib.parse.urlparse(raw)
    except Exception as e:
        return False, f"地址无法解析: {e}"

    scheme = (p.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"只允许 http/https，收到的是 {scheme or '(空)'}"

    host = (p.hostname or "").strip()
    if not host:
        return False, "地址里没有主机名"

    # 主机名是 IP 字面量才做网段判断；域名留给 client.py 在请求时解析后再查。
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 是域名。挡住明显指向本机/内网的写法。
        low = host.lower().rstrip(".")
        if low in ("localhost", "localhost.localdomain") or low.endswith(".localhost"):
            return False, "不允许指向本机（localhost）"
        if low.endswith(".local") or low.endswith(".internal"):
            return False, "不允许指向内网域名（.local / .internal）"
        return True, ""

    for net in _BLOCKED_NETS:
        # version 必须一致，否则 ip_network.__contains__ 会抛 TypeError
        if ip.version == net.version and ip in net:
            return False, f"不允许指向内网/保留地址：{ip}"
    return True, ""


def is_safe_resolved_ip(ip_str: str) -> Tuple[bool, str]:
    """请求前对 DNS 解析结果复查（纵深防御第二层）。

    ``client.py`` 在真正建立连接前调用，挡住 DNS rebinding。
    """
    try:
        ip = ipaddress.ip_address(str(ip_str).strip())
    except ValueError:
        return False, f"无法识别的 IP: {ip_str}"
    for net in _BLOCKED_NETS:
        if ip.version == net.version and ip in net:
            return False, f"解析到内网/保留地址：{ip}"
    return True, ""


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------

@dataclass
class GateDecision:
    """一次提议的判定结果。"""

    allowed: bool
    reason: str = ""
    #: True 表示"允许，但必须人工确认后才能执行"
    needs_confirm: bool = False
    #: 摘要，进审计日志
    summary: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class Gate:
    """校验 AI 的工具调用提议。

    无状态的部分（SSRF / 白名单 / 参数）是纯函数式的；
    频率计数是唯一的可变状态，用锁保护（提议可能来自多个线程）。
    """

    def __init__(self, plugin: Any = None) -> None:
        self.p = plugin
        self.logger = getattr(plugin, "logger", None)
        self._lock = threading.Lock()
        #: {(tool_name): [时间戳, ...]}
        self._calls: Dict[str, List[float]] = {}
        #: 审计记录，供面板展示。**区分 actor**。
        self.audit: List[Dict[str, Any]] = []
        self._audit_max = 500

    # ------------------------------------------------------------ 主入口
    def check(self, tool_name: str, args: Optional[Dict[str, Any]] = None) -> GateDecision:
        """校验一次工具调用提议。

        判定顺序有意为之：**先查禁区，再查注册表**。
        因为"模型试图调用禁区能力"和"模型编了个不存在的名字"是两种
        性质完全不同的事件，前者要在审计里明确标出来。
        """
        name = str(tool_name or "").strip()

        # ---- 第 0 关：禁区识别（区分攻击信号 vs 普通幻觉）----
        forbidden, why = is_forbidden(name)
        if forbidden:
            self._record("blocked_forbidden", name, args, why)
            return GateDecision(False, f"拒绝：命中禁区能力 —— {why}")

        # ---- 第 1 关：必须在注册表内 ----
        spec = get_tool(name)
        if spec is None:
            self._record("blocked_unknown", name, args, "工具不存在")
            return GateDecision(False, f"拒绝：没有名为 {name} 的工具")

        # ---- 第 2 关：参数校验 ----
        ok, msg = self.validate_args(spec, args or {})
        if not ok:
            self._record("blocked_args", name, args, msg)
            return GateDecision(False, f"拒绝：{msg}")

        # ---- 第 3 关：频率熔断 ----
        ok, msg = self._check_rate(name)
        if not ok:
            self._record("blocked_rate", name, args, msg)
            return GateDecision(False, f"拒绝：{msg}")

        # 通过。不可逆操作仍需人工确认。
        needs = bool(spec.requires_confirm)
        self._record(
            "allowed" if not needs else "pending_confirm",
            name, args,
            "需要人工确认" if needs else "已放行",
        )
        return GateDecision(
            True,
            "需要人工确认后才执行" if needs else "已放行",
            needs_confirm=needs,
            summary=self._summary(name, args),
        )

    # ------------------------------------------------------------ 参数校验
    def validate_args(self, spec: Any, args: Dict[str, Any]) -> Tuple[bool, str]:
        """按工具定义校验参数。

        只做**类型与形式**校验，不做业务校验（"这个玩家存不存在"）
        —— 前者是纯函数、好测；后者依赖运行时状态，放在执行层，
        因为它随时会变（提议 → 确认之间玩家可能已经退了）。
        """
        if not isinstance(args, dict):
            return False, "参数必须是对象"

        allowed = set(spec.params.keys())
        extra = set(args.keys()) - allowed
        if extra:
            # 未知参数直接拒绝，而不是忽略。
            # 忽略会让"模型把危险值塞进一个被忽略的字段"不留痕迹。
            return False, f"出现未定义参数: {', '.join(sorted(extra))}"

        # ★ 必填参数不能缺。
        #   门槛本身不高（参数说明里写了哪些要传），但"缺参数"如果放行，
        #   执行层就会拿 None 去操作 —— 比如 kick_player 少了 name 变成
        #   "踢一个叫 None 的玩家"，或者更糟：某个实现把空值当成"全部"。
        #   空字符串也算缺失：模型经常用 "" 表达"我没想好"。
        #
        #   ⚠ 例外：``optional`` 里列的参数允许缺失 —— 查询类工具的
        #   "不加过滤条件"是**正常且必要**的用法（"把最近的报错都给我"）。
        #   把可选项也当必填，模型会被迫编造值来凑参数，
        #   反而制造出"keyword=随便填一个"这种噪音。
        missing = [
            k for k in spec.params
            if k not in spec.optional
            and (k not in args or args.get(k) is None or str(args.get(k)).strip() == "")
        ]
        if missing:
            return False, f"缺少必填参数: {', '.join(sorted(missing))}"

        for key, val in args.items():
            if val is None:
                continue
            if isinstance(val, (dict, list)):
                return False, f"参数 {key} 不接受复合类型"
            s = str(val)

            if len(s) > _MAX_STR_LEN:
                return False, f"参数 {key} 过长（>{_MAX_STR_LEN}）"

            # 控制字符（含 \0、\r\n）一律拒绝：\r\n 能用来伪造日志行，
            # \0 在某些原生边界上会截断字符串 —— 都是注入类风险。
            if any(ord(c) < 32 and c not in "\t" for c in s):
                return False, f"参数 {key} 含控制字符"

            if key == "name":
                if not _NAME_RE.match(s):
                    return False, f"玩家名不合法: {s!r}"

            # ---- 区块回档的坐标 / 时间窗 ----
            #
            # ★ 为什么坐标必须显式校验：
            #   没有这条规则时，任何字符串都能通过闸门直达实现层。
            #   目前实现层用 int(float(...)) 兜住了，但**闸门是唯一
            #   一处集中校验点** —— 把"参数长什么样"依赖在实现层
            #   各自兜底上，等于每加一个消费方就要重新想一遍。
            #   这里限定成合法整数且落在 Minecraft 世界高度/距离的
            #   实际量级内，越界值直接拒绝而不是悄悄夹取：
            #   夹取会让模型以为"回档了 100 万格外的地方"，实际没做。
            # 复数 x1..z2 = 区域回档的框选；单数 x/y/z = 单点历史查询
            # （inspect_block_history）。两者都要拦，否则单点查询会把
            # "abc" 之类的值直接放进实现层，而闸门本该是唯一校验点。
            if key in ("x", "y", "z", "x1", "y1", "z1", "x2", "y2", "z2"):
                try:
                    n = int(float(s))
                except (TypeError, ValueError):
                    return False, f"坐标 {key} 必须是整数：{s!r}"
                # 基岩版世界横向约 ±30,000,000，纵向 -64~320。
                # 这里放宽到 ±1e9 只做"显然离谱"的拦截，
                # 真正的区域体积由 RegionAPI 的上限把关。
                if abs(n) > 1_000_000_000:
                    return False, f"坐标 {key} 超出合理范围：{s!r}"

            if key in ("minutes", "seconds", "hours", "days"):
                try:
                    f = float(s)
                except (TypeError, ValueError):
                    return False, f"{key} 必须是数字：{s!r}"
                if f <= 0 or f > 3650 * 86400:
                    return False, f"{key} 必须在合理范围内：{s!r}"

            if key == "session_id":
                if s and not s.lstrip("+").isdigit():
                    return False, f"session_id 必须是整数：{s!r}"

            if key in ("duration", "limit"):
                if not str(s).lstrip("-").isdigit():
                    return False, f"参数 {key} 必须是整数"

            if key in ("level",):
                if s.upper() not in ("", "TRACE", "DEBUG", "INFO",
                                     "WARN", "WARNING", "ERROR", "FATAL"):
                    return False, f"日志级别不合法: {s!r}"

            if key in ("scope",):
                if s.lower() not in ("", "local", "cloud"):
                    return False, f"scope 只能是 local 或 cloud: {s!r}"

            if key in ("status",):
                if s.lower() not in ("", "pending", "approved", "rejected"):
                    return False, f"status 不合法: {s!r}"

        return True, ""

    # ------------------------------------------------------------ 频率熔断
    def _check_rate(self, name: str) -> Tuple[bool, str]:
        now = time.time()
        with self._lock:
            times = self._calls.setdefault(name, [])
            # 清掉窗口外的旧记录
            cutoff = now - _BURST_WINDOW
            times[:] = [t for t in times if t >= cutoff]
            if len(times) >= _BURST_MAX:
                return False, (
                    f"{name} 在 {int(_BURST_WINDOW)} 秒内已被调用 "
                    f"{len(times)} 次（上限 {_BURST_MAX}），已暂停"
                )
            times.append(now)
        return True, ""

    def reset_rate(self, name: Optional[str] = None) -> None:
        """清空频率计数。服主在面板上手动放行时调用。"""
        with self._lock:
            if name is None:
                self._calls.clear()
            else:
                self._calls.pop(str(name), None)

    # ------------------------------------------------------------ 审计
    def _summary(self, name: str, args: Optional[Dict[str, Any]]) -> str:
        if not args:
            return name
        bits = ", ".join(f"{k}={v!r}" for k, v in sorted(args.items()))
        return f"{name}({bits})"

    def _record(self, kind: str, name: str, args: Any, note: str) -> None:
        """记一条审计。

        ``actor`` 固定为 ``"ai"`` —— 本模块只处理 AI 发起的动作，
        人工操作走 ``_log_admin_action``。两边分开记，事后才能一眼
        分清责任。合在一起记会让"到底是谁干的"变成考古题。
        """
        rec = {
            "ts": time.time(),
            "actor": "ai",
            "kind": kind,
            "tool": name,
            "args": dict(args) if isinstance(args, dict) else {},
            "note": note,
        }
        with self._lock:
            self.audit.append(rec)
            if len(self.audit) > self._audit_max:
                del self.audit[:len(self.audit) - self._audit_max]

        # 被拦下的尝试要进日志：正常的放行不必刷屏，
        # 但"模型试图提权"这种事服主必须能看见。
        if kind.startswith("blocked") and callable(getattr(self.logger, "warning", None)):
            try:
                self.logger.warning(f"[AI] 拦下工具调用 {name}：{note}")
            except Exception:
                pass

    def recent_audit(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.audit[-limit:])
