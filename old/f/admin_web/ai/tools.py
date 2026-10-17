"""工具分级表：定义 AI **能做什么、绝不能做什么**。

这是整个 AI 模块的安全基石。请先读完这段再改任何东西。

为什么不给模型"一个万能执行器" + 提示词约束
==========================================
因为提示词不是安全边界。模型可以被玩家聊天内容注入、可以被日志里的
一段文字注入、也可以单纯地判断失误。把危险操作放在"模型不该调用"的
约定上，等于把服务器安全押在模型每次都听话。

所以这里用的是**能力枚举**：危险能力根本不存在对应的工具，
它们在 ``FORBIDDEN_CAPABILITIES`` 里有明确记录，且有测试断言
"这些名字不在注册表里"。想加回去必须先删掉那条测试 ——
删除一个刻意写下的断言，比"忘了加校验"更难被忽略。

四级风险
========
``READ``      只读      → 可直接执行。玩家列表、日志查询、统计。
``REVERSIBLE``可逆      → 执行但写审计。踢人、封禁（都能解）。
``IRREVERSIBLE`` 不可逆 → **只提议**，必须人工在面板上确认才执行。
                          删存档、清空白名单、改服务器关键属性。
``FORBIDDEN`` 禁区      → **不注册**。结构上不存在，模型调不到。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class RiskLevel(str, Enum):
    """风险等级。用 str 枚举，方便直接进 JSON 给前端显示。"""

    READ = "read"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"
    #: 禁区。**这个等级的工具不允许出现在 TOOL_REGISTRY 里** ——
    #: 它只作为 ``FORBIDDEN_CAPABILITIES`` 的标注值存在。
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的定义。

    ``params`` 是 ``{参数名: 说明}``。**只用于给模型生成提示词**，
    真正的校验在 ``gate.py`` 里按 ``param_schema`` 做 —— 不要指望
    模型看到说明就传对参数类型。

    ``requires_confirm`` 由 ``risk`` 自动推导（不可逆 → 需要确认），
    单独留成字段是为了将来给某个 tool 单独收紧，不用改风险等级。
    """

    name: str
    risk: RiskLevel
    description: str
    params: Dict[str, str] = field(default_factory=dict)
    #: 允许缺省的参数名。默认所有 ``params`` 都是必填 —— 让"漏填"失败，
    #: 比让它悄悄按 None 执行安全得多。查询类工具要显式声明哪些可空。
    optional: "frozenset[str]" = field(default_factory=frozenset)
    requires_confirm: bool = False
    #: 该工具是否会在服务器主线程外执行。True 表示走后台线程。
    background: bool = True

    def __post_init__(self):
        # 不可逆操作必须经过人工确认。写成 post_init 而不是在每个
        # ToolSpec 里手填，是因为"漏填一个"的代价太大 —— 一次无声的
        # 删档就够毁掉一个服务器。这里强制推导，无法绕过。
        if self.risk == RiskLevel.IRREVERSIBLE and not self.requires_confirm:
            object.__setattr__(self, "requires_confirm", True)
        if self.risk == RiskLevel.FORBIDDEN:
            raise ValueError(
                f"禁区能力不允许注册为工具: {self.name}。"
                f"见 tools.py 顶部注释：禁区是结构性不存在的，"
                f"不是靠运行时校验拦住的。"
            )


# ---------------------------------------------------------------------------
# 禁区：永不注册的能力
#
# 这些条目**只是文字记录**，没有对应的可调用实现。它们存在的意义是：
#   · 让后来者看到"为什么这里没有这个功能"，而不是误以为漏做了；
#   · 测试可以断言"注册表里确实没有这些名字"；
#   · code review 时改动这里必须解释清楚。
#
# ★ 如果你正在考虑"给 AI 加一个执行任意命令的工具"，请先想清楚：
#   那等于把服务器的最高权限交给一段可能被聊天内容影响的文本。
#   服主想要那个效果，正确的做法是自己 SSH 上去。
# ---------------------------------------------------------------------------
FORBIDDEN_CAPABILITIES: Dict[str, str] = {
    "exec_command": "执行任意控制台命令 —— 等价于交出整个服务器",
    "grant_op": "给玩家 OP —— 提权，且模型可能被玩家话语诱导",
    "deop": "撤销 OP —— 同样属于权限体系，必须人来决定",
    "write_file": "写任意文件 —— 能改插件自身代码，等于持久化后门",
    "read_file": "读任意文件 —— 能读走 server.properties 里的密钥",
    "install_mod": "安装模组 —— 任意代码执行",
    "delete_backup": "删除备份 —— 毁掉唯一的退路",
    "stop_server": "关服 —— 影响面太大，且无正当自动化理由",
    "whitelist_add": "自行放行白名单玩家 —— 审核权必须留给人",
    "modify_plugin": "修改插件/子插件代码 —— 自提权通路",
    "network_request": "对任意地址发起网络请求 —— SSRF 到内网服务",
    "manage_accounts": "创建/删除面板账号 —— 认证体系归人管",
}

#: 变体检测用的"敏感词根"。
#
# 为什么不从 FORBIDDEN_CAPABILITIES 的键里自动拆：
# 试过 —— ``exec_command`` 按 ``_`` 拆出 ``exec`` / ``command``，
# 但模型编的 ``exec_cmd`` 拆出来是 ``exec`` / ``cmd``，最后一段
# ``cmd`` 不在任何键里，于是漏判。**"取最后一段"这个做法本身就是错的**：
# 攻击者只要把词根放前面、"容器词"放后面就能绕过。
#
# 所以这里显式列出真正危险的动作词根，并对名字里的**任意一段**做匹配；
# 再叠加一层：整个名字里出现 "op" 这种独立权限词也要算（``op_player``）。
_SENSITIVE_STEMS = (
    "exec", "command", "cmd", "run", "shell", "spawn", "eval", "sudo",
    "file", "write", "read", "delete", "remove", "purge", "drop",
    "install", "upload", "download",
    "stop", "shutdown", "restart", "kill",
    "whitelist", "account", "plugin", "mod", "request", "network", "http",
    # 独立权限词：``op`` 本身是权限体系的核心动作，单独成段也要拦。
    # 漏掉它的话 ``op_player`` / ``give_op`` 这类名字会被放过。
    "op",
)


def is_forbidden(name: str) -> Tuple[bool, str]:
    """名字是否命中禁区清单。返回 ``(是否禁区, 原因)``。

    这个检查是**双保险**：禁区工具本来就不在注册表里，调不到。
    但模型可能编造一个叫 ``exec_command`` 的名字出来 —— 那时我们要
    能在审计里明确写出"它试图调用禁区能力"，而不是含糊的"工具不存在"。
    这两条记录的价值完全不同：后者像 bug，前者是攻击信号。
    """
    n = str(name or "").strip().lower()
    if not n:
        return False, ""
    if n in FORBIDDEN_CAPABILITIES:
        return True, FORBIDDEN_CAPABILITIES[n]

    # 变体匹配：拆成词根后逐段比对。
    # 用「任意一段命中」而不是「最后一段命中」，因为词序可以任意调换
    # （exec_cmd / cmd_exec / run_shell_command 都是同一件事）。
    parts = [p for p in re.split(r"[^a-z0-9]+", n) if p]
    hit = next((p for p in parts if p in _SENSITIVE_STEMS), "")
    if hit:
        # 单段命中的情况要收紧一点：像 "list_accounts" 里带了 account，
        # 但它其实是查账号列表这种无害工具 —— 这类**已注册**的名字
        # 不该被误报，否则真正的攻击信号会被日常噪音淹没。
        if n in TOOL_REGISTRY:
            return False, ""
        base = next(
            (bad for bad in FORBIDDEN_CAPABILITIES
             if hit in bad.split("_") or hit == bad),
            "",
        )
        why = FORBIDDEN_CAPABILITIES.get(base, "命中高危动作词根")
        return True, f"疑似禁区能力变体（{hit} → {base or '未知'}）：{why}"
    return False, ""


# ---------------------------------------------------------------------------
# 注册表：AI 实际可以调用的工具
#
# 命名约定：``<对象>_<动作>``。动作词本身就体现风险：
#   list_/get_/read_  → 只读
#   kick_/ban_/unban_ → 可逆
#   delete_/clear_/set_ → 不可逆（需确认）
# 有这条约定，review 时看名字就知道该不该 require confirm。
# ---------------------------------------------------------------------------

TOOL_REGISTRY: Dict[str, ToolSpec] = {}


def _register(spec: ToolSpec) -> None:
    if spec.name in TOOL_REGISTRY:
        raise ValueError(f"工具名重复: {spec.name}")
    TOOL_REGISTRY[spec.name] = spec


# ---- 只读 ----------------------------------------------------------------

_register(ToolSpec(
    name="list_players",
    risk=RiskLevel.READ,
    description="列出当前在线玩家（名字、xuid、延迟、所在维度）。",
    background=False,          # 纯内存读，不必开线程
))

_register(ToolSpec(
    name="get_player_info",
    risk=RiskLevel.READ,
    description="查询某个玩家的详细信息（可信度、设备、封禁状态）。",
    params={"name": "玩家名（必须是在线或历史存在的玩家）"},
    background=False,
))

_register(ToolSpec(
    name="query_logs",
    risk=RiskLevel.READ,
    description=(
        "检索服务器日志。可按级别（ERROR/WARN/INFO）与关键词过滤，"
        "返回最近若干行。用于定位报错与异常。"
    ),
    params={
        "level": "日志级别，如 ERROR / WARN，留空表示不限",
        "keyword": "关键词，留空表示不限",
        "limit": "返回行数上限（受服务端硬上限约束）",
    },
    # 三个都是"过滤条件"，全空 = 不过滤（看最近的日志）—— 这是正当用法
    optional=frozenset({"level", "keyword", "limit"}),
))

_register(ToolSpec(
    name="list_whitelist",
    risk=RiskLevel.READ,
    description="列出白名单申请（含申请理由、来源 IP、时间）。",
    params={"status": "pending / approved / rejected，留空表示全部"},
    optional=frozenset({"status"}),
))

_register(ToolSpec(
    name="list_bans",
    risk=RiskLevel.READ,
    description="列出封禁记录。",
    params={"scope": "local（本地封禁）或 cloud（云黑），留空表示本地"},
    optional=frozenset({"scope"}),
))

_register(ToolSpec(
    name="server_stats",
    risk=RiskLevel.READ,
    description="服务器概况：TPS、在线人数、内存、运行时长。",
    background=False,
))

_register(ToolSpec(
    name="list_mods",
    risk=RiskLevel.READ,
    description="列出已安装的模组与它们的启用状态。",
    background=False,
))


# ---- 可逆（执行 + 审计）---------------------------------------------------

_register(ToolSpec(
    name="kick_player",
    risk=RiskLevel.REVERSIBLE,
    description="踢出一个在线玩家。玩家可立即重新进入，属于可逆操作。",
    params={
        "name": "玩家名（必须在当前在线列表中，服务端会校验）",
        "reason": "踢出原因，会显示给玩家",
    },
))

_register(ToolSpec(
    name="ban_player",
    risk=RiskLevel.REVERSIBLE,
    description="封禁玩家（本地）。可通过 unban_player 撤销。",
    params={
        "name": "玩家名（必须真实存在于玩家记录中）",
        "reason": "封禁原因",
        "duration": "时长（分钟），留空表示永久",
    },
    # duration 留空 = 永久封禁，这是明确语义，不是"没填"
    optional=frozenset({"duration"}),
))

_register(ToolSpec(
    name="unban_player",
    risk=RiskLevel.REVERSIBLE,
    description="解除封禁。只能撤销已有封禁，不会新增记录。",
    params={
        "name": "玩家名",
        "scope": "local 或 cloud，默认 local",
    },
    optional=frozenset({"scope"}),
))

_register(ToolSpec(
    name="mute_player",
    risk=RiskLevel.REVERSIBLE,
    description="禁言玩家一段时间。到期自动解除。",
    params={
        "name": "玩家名（必须在线）",
        "duration": "禁言时长（分钟）",
        "reason": "禁言原因",
    },
))


# ---- 不可逆（只提议，人工确认）---------------------------------------------
# 注意：这类工具**不会**被自动执行。模型调用它们只产生一条"待确认提议"，
# 必须服主在面板上点确认才真正执行（执行路径由 web 层实现，本阶段不做）。

_register(ToolSpec(
    name="delete_player_data",
    risk=RiskLevel.IRREVERSIBLE,
    description=(
        "提议删除某个玩家的存档数据。**不可逆**，会生成待确认提议，"
        "由服主在面板上确认后才执行。"
    ),
    params={
        "name": "玩家名",
        "reason": "为什么需要删除（会显示给服主作为决策依据）",
    },
))

_register(ToolSpec(
    name="clear_whitelist_entry",
    risk=RiskLevel.IRREVERSIBLE,
    description=(
        "提议移除一条已通过的白名单记录。不可逆，需人工确认。"
    ),
    params={
        "name": "玩家名",
        "reason": "原因",
    },
))

_register(ToolSpec(
    name="set_server_property",
    risk=RiskLevel.IRREVERSIBLE,
    description=(
        "提议修改 server.properties 的某一项。改错会导致服务端行为异常，"
        "不可逆（虽可改回，但中途可能已造成影响），需人工确认。"
    ),
    params={
        "key": "属性名（必须在现有属性列表内）",
        "value": "目标值",
        "reason": "原因",
    },
))


# ---- 查询接口 --------------------------------------------------------------

def get_tool(name: str) -> Optional[ToolSpec]:
    """按名取工具；不存在返回 None。**不要抛异常** ——
    模型可能编造工具名，那是正常情况，不是错误。"""
    return TOOL_REGISTRY.get(str(name or ""))


def list_tools(risk: Optional[RiskLevel] = None,
               include_irreversible: bool = True,
               names: Optional["set[str]"] = None) -> List[ToolSpec]:
    """列出工具，可按风险等级 / 是否含不可逆 / 具体名字过滤。

    参数
    ----
    risk
        只返回该风险等级的工具。``None`` 表示不限。
    include_irreversible
        ``False`` 时排除不可逆工具 —— 影子模式下给面板展示"模型实际
        能看到哪些工具"要用这个。

    .. warning::
       **这个参数曾经引发过一个真实 bug**：``core.py::_ai_tools()`` 曾把
       ``include_irreversible=`` 传给 ``list_tools()``，而当时的
       ``list_tools`` 只接受 ``risk`` —— 面板点「加载工具清单」直接
       抛 ``TypeError: list_tools() got an unexpected keyword argument``。

       根因是两个函数语义相近但签名不同：
         · ``list_tools()``         → 返回 ToolSpec 对象（给面板看）
         · ``tools_for_prompt()``   → 返回 OpenAI dict（给模型看）

       所以这里显式接纳该参数，让两条路径的调用方式保持一致，
       避免以后又把两者搞混。

    names
        只返回名字在这个集合里的工具。``None`` 表示不限。
        用于"工具权限"功能 —— 面板按用户勾选的白名单取工具。
    """
    out = list(TOOL_REGISTRY.values())
    if risk is not None:
        out = [t for t in out if t.risk == risk]
    if not include_irreversible:
        out = [t for t in out if t.risk != RiskLevel.IRREVERSIBLE]
    if names is not None:
        want = {str(n) for n in names}
        out = [t for t in out if t.name in want]
    return out


# 注意：``is_forbidden`` 定义在上面的禁区区段（与 _SENSITIVE_STEMS 放一起，
# 因为它依赖那张词根表）。这里**不要**再定义一份 —— 重复定义会静默
# 让后面那个覆盖前面那个，是最难查的一类"改了没生效"。


def tools_for_prompt(include_irreversible: bool = True,
                     allowed: Optional["set[str]"] = None) -> List[Dict[str, Any]]:
    """生成给模型看的工具清单（OpenAI function-calling 风格）。

    ``include_irreversible=False`` 时排除不可逆工具 —— 用于"影子模式"：
    只想让模型做分析、还不想让它提议改动时。

    ``allowed`` 是**工具权限白名单**（见 ``resolve_allowed``）。
    ``None`` 表示不做权限过滤，只按风险等级走默认策略。
    """
    out = []
    for t in TOOL_REGISTRY.values():
        if t.risk == RiskLevel.FORBIDDEN:
            continue
        if allowed is not None and t.name not in allowed:
            continue
        if t.risk == RiskLevel.IRREVERSIBLE and not include_irreversible:
            continue
        props = {k: {"type": "string", "description": v} for k, v in t.params.items()}
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": sorted(t.params.keys()),
                },
            },
        })
    return out


# ---------------------------------------------------------------------------
# 工具权限（单工具级开关）
# ---------------------------------------------------------------------------
#
# 设计要点
# --------
# 1. **空列表 = 未配置 = 不做白名单过滤**，而不是"一个都不允许"。
#    理由：默认放行才符合直觉 —— 用户没动过这个设置，就不该因为新版本
#    引入了权限功能而让 AI 突然全哑。真要"全禁"，那是 ``enabled=False``
#    该干的事，不该让一个空列表背这个锅。
#
# 2. **工具权限与影子模式是 AND 关系，不是覆盖关系**（已与用户确认）。
#    影子模式是硬闸门，工具权限不能让它放行任何执行。
#    权限的作用是把"影子模式下也允许出现/提议"的范围再收窄一层。
#    例：影子开着 + 权限勾了 delete_player_data
#        → 该工具在清单里可见、可以提议，但不会执行。
#    用户什么时候关掉影子，它才什么时候真能动。
#
# 3. **不可逆工具的放行是允许的**，但只影响"能不能提议"。
#    真正执行仍然必须过人工确认 —— 那一步在 runner 内部，
#    是独立的、任何权限都绕不过的。


def all_tool_names() -> List[str]:
    """所有已注册工具的名字（不含禁区）。"""
    return sorted(t.name for t in TOOL_REGISTRY.values()
                  if t.risk != RiskLevel.FORBIDDEN)


def normalize_tool_names(raw: Any) -> List[str]:
    """把用户传来的工具名列表规范化。

    · 非列表/None → ``[]``
    · 去空白、去重、保序
    · **未知名字被丢弃** —— 工具集随版本变化，存进去一个已删除的工具名
      不该让整份配置失效（fail-open：丢掉的只是那一项）。
    · 禁区名字即使被伪造传入也一律丢弃（双重保险：注册表里本来就没有，
      但万一将来有人手滑注册了，这里还能拦一道）。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    known = set(all_tool_names())
    seen = set()
    out = []
    for item in raw:
        n = str(item or "").strip()
        if not n or n in seen or n not in known:
            continue
        seen.add(n)
        out.append(n)
    return out


def resolve_allowed(configured: Any, *, shadow: bool,
                    enabled: bool = True) -> "Optional[set[str]]":
    """算出一份**实际生效**的工具白名单（只用于过滤，不放大权限）。

    返回值语义：

    · ``None``    → 用户没配权限，不做白名单过滤。
    · ``set()``   → 一个工具都不给（功能关掉时）。
    · ``set{...}``→ 用这份白名单。

    规则：

    1. ``enabled=False`` → 空集合。功能都关了，谈不上工具权限。
    2. 用户配置非空 → 直接用配置。
    3. 用户配置为空 → ``None``（不做过滤，交给调用方的风险等级策略）。

    .. note::
       这个函数**只做收窄，不做放宽**。影子模式下不可逆工具是否可用，
       由调用方叠加 ``include_irreversible`` 决定 —— 两者是 AND。
       早先版本曾想让权限"覆盖"影子模式，那会让界面上的影子开关
       与实际保护强度不符，已废弃。

    ``shadow`` 参数保留在签名里是为了调用方语义完整（以及未来的
    分级策略），当前逻辑不依赖它。
    """
    if not enabled:
        return set()
    names = normalize_tool_names(configured)
    if names:
        return set(names)
    return None


def shadow_blocks(configured: Any, *, shadow: bool,
                  enabled: bool = True) -> List[Dict[str, str]]:
    """影子模式下**因为影子模式而无法执行**的已勾选工具。

    这个是给前端做提示用的。用户在权限列表里勾了某个可逆/不可逆工具，
    却仍然发现 AI 不动手 —— 不解释的话，第一反应是"权限没生效"。

    只读工具不算：它们任何模式下都只读，与影子模式无关。
    """
    if not shadow or not enabled:
        return []
    names = set(normalize_tool_names(configured))
    if not names:
        return []
    out = []
    for t in TOOL_REGISTRY.values():
        if t.name in names and t.risk in (RiskLevel.REVERSIBLE,
                                          RiskLevel.IRREVERSIBLE):
            out.append({"name": t.name, "risk": str(t.risk),
                        "desc": t.description})
    return sorted(out, key=lambda x: x["name"])
