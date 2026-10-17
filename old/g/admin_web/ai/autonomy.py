"""接管级别：AI 到底被允许「自己动手」到什么程度。

为什么单独一个文件
==================
在 v7.8 之前，这件事由一个布尔量 ``shadow_mode`` 表达：开 = 只分析、
关 = 可执行。两个档位在当时够用，但用户很快提出第三种需求：
「白名单审核这种天天要做的琐事，让 AI 自己批了吧，但删档还是要问我」。

一个布尔量塞不下三档，硬塞的方式是「shadow_mode + 一堆 if」——
那种代码三个月后没人敢动。所以这里把档位抽成显式常量 + 一张策略表，
所有「哪个级别能干什么」的判断都从 ``policy()`` 出，不再散落在各处。

三档的语义（用户原话）
======================
    第一级：纯建议，不做操作
    第二级：自动完成白名单审核，其他依旧以建议的方式
    第三级：完全放开，AI 想做什么就做什么，不需要等到服主的同意

映射到工具风险等级：

    ┌──────────┬────────┬────────┬──────────┬──────────┐
    │   级别   │  只读  │  可逆  │  不可逆  │ 白名单审核│
    ├──────────┼────────┼────────┼──────────┼──────────┤
    │ 0 纯建议 │ 不执行 │ 不执行 │  不执行  │  只给建议 │
    │ 1 白名单 │  执行  │  执行  │ 不执行(提议)│ 自动完成 │
    │ 2 完全放开│  执行  │  执行  │ 执行+可回滚│ 自动完成 │
    └──────────┴────────┴────────┴──────────┴──────────┘

禁区（``RiskLevel.FORBIDDEN``）在任何级别下都**不注册**，
结构上不可达 —— 这不是策略，是 ``ToolSpec.__post_init__`` 的硬约束。

不可逆在第三级「执行但可回滚」
==============================
用户选择了「放开但留撤销窗口」+「立即执行，窗口内可回滚」。
所以第三级不是「无限制」——它是「做，但给 30 秒后悔药」。
撤不回的操作（比如已经生效的服务器属性改动）会如实告知服主
「这条无法回滚」，而不是假装有退路。

旧字段的迁移
============
``shadow_mode`` 保留可读，映射规则：

    shadow_mode = True  → 级别 0（旧语义「只分析不执行」原样保持）
    shadow_mode = False → 级别 1（旧语义「关掉影子」—— 注意旧版关掉影子
                          也**只是**让只读/可逆工具能动，不可逆仍然只提议，
                          那正是级别 1 的定义）
    字段缺失            → 级别 1（v7.9 默认）

★ 为什么不把 ``False`` 映射到级别 2
    v7.9 引入级别 2（完全放开，不可逆直执行）是**一个全新的能力**。
    把老配置升级成"AI 可以不等确认就删档"，等于在一个用户从没同意过的
    维度上放开了他。用户明确要求"升级默认第二级"，而第二级就是
    「白名单自动、其余仍以建议的方式」—— 与旧的 shadow_mode=False
    行为**完全一致**。所以迁移的目标是**不改变任何既有行为**。

★ 这也意味着：从旧版本升级上来的用户，**不会被自动推进级别 2**。
   要开完全放开，必须在面板上显式点。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = [
    "AUTONOMY_ADVISE",
    "AUTONOMY_WHITELIST",
    "AUTONOMY_FULL",
    "AUTONOMY_LEVELS",
    "AUTONOMY_LABELS",
    "AUTONOMY_HINTS",
    "DEFAULT_AUTONOMY",
    "normalize_autonomy",
    "policy",
    "AutonomyPolicy",
]

#: 第一级：纯建议 —— 任何工具都不执行，连提议也不产生。
#: 等价于旧版 ``shadow_mode=True``：让服主先观察模型的判断力。
AUTONOMY_ADVISE = 0

#: 第二级：白名单审核自动完成 —— 只读与可逆工具照常执行，
#: 不可逆仍然只提议；白名单的批准/驳回由模型直接落袋。
#: ★ v7.9 的默认值。
AUTONOMY_WHITELIST = 1

#: 第三级：完全放开 —— 不可逆也直接执行，但**同时登记撤销记录**，
#: 在 ``undo_window_seconds`` 内服主可以一键回滚。
AUTONOMY_FULL = 2

#: 有效档位集合。用于校验外部传入的值。
AUTONOMY_LEVELS = (AUTONOMY_ADVISE, AUTONOMY_WHITELIST, AUTONOMY_FULL)

#: 面板展示用的短标签
AUTONOMY_LABELS: Dict[int, str] = {
    AUTONOMY_ADVISE: "第一级 · 纯建议",
    AUTONOMY_WHITELIST: "第二级 · 白名单自动",
    AUTONOMY_FULL: "第三级 · 完全放开",
}

#: 面板展示用的说明。写长一点，因为选错这个的后果都不小。
AUTONOMY_HINTS: Dict[int, str] = {
    AUTONOMY_ADVISE: (
        "AI 只输出分析与建议，任何工具都不会执行，也不会生成待确认提议。"
        "适合刚接入、想先观察几天模型判断力的时候。"
    ),
    AUTONOMY_WHITELIST: (
        "只读查询与可逆操作（踢人、封禁等）由 AI 直接执行并留审计；"
        "白名单的批准/驳回也由 AI 自动完成。"
        "危险操作（删档、改 server.properties）依然只生成待确认提议，需要你点确认。"
    ),
    AUTONOMY_FULL: (
        "包括删档、改服务器属性在内的所有工具都由 AI 直接执行，不再等你确认。"
        "★ 执行后会登记撤销记录，在「撤销窗口」内（默认 30 秒）可以在面板上"
        "或群里一键回滚。窗口一过就真的不可逆了。"
    ),
}

#: 新装默认。用户明确要求：升级到 v7.9 后默认落在第二级。
DEFAULT_AUTONOMY = AUTONOMY_WHITELIST

#: 撤销窗口的默认秒数（仅第三级用得上）
DEFAULT_UNDO_WINDOW = 30


def _is_irreversible(risk: Any) -> bool:
    """判断一个风险等级是不是"不可逆"。

    ★ 为什么要单独一个函数、而不是直接 ``str(risk) == "irreversible"``
    ---------------------------------------------------------------
    ``RiskLevel`` 是 Enum，``str(RiskLevel.IRREVERSIBLE)`` 得到的是
        'RiskLevel.IRREVERSIBLE'      ← 不是 'irreversible'！
    直接比字符串会**恒为假**。这个坑在 v9.0.1 已经真实咬过一次
    （见 manager.chat 里那道只读检查，所有工具调用都被误拒）。

    这里同时接受三种形态，任一种演进都不会失效：
      · ``RiskLevel.IRREVERSIBLE``  → 比枚举
      · ``"irreversible"``          → 比字符串值
      · 任何带 ``.value`` 的对象     → 取 value 再比（兼容未来的包装类型）
    """
    if risk is None:
        return False
    # 形态 1：枚举（或任何带 .value 的东西）
    val = getattr(risk, "value", None)
    if val is not None:
        return str(val) == "irreversible"
    # 形态 2：纯字符串
    return str(risk) == "irreversible"


def normalize_autonomy(raw: Any, *, default: int = DEFAULT_AUTONOMY) -> int:
    """把任意输入规整成合法档位 0/1/2。

    接受 int、数字字符串（前端表单常传字符串）、以及 ``None``。
    非法值一律回落到 ``default`` —— **不做报错**，因为这是配置读取路径，
    一个手改坏的配置文件不该让整个 AI 模块起不来。
    """
    if raw is None or raw == "":
        return int(default)
    try:
        # 布尔要特殊处理：Python 里 True == 1，但用户写 true 时
        # 想表达的绝不是「第二级」，而是旧 shadow 语义。这里拒绝布尔，
        # 交给 migrate_shadow() 去处理。
        if isinstance(raw, bool):
            return int(default)
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return int(default)
    return v if v in AUTONOMY_LEVELS else int(default)


class AutonomyPolicy:
    """某个档位下「允许做什么」的完整答案。

    做成不可变对象而不是一堆散落的 ``if level >= 1``：那些 if 会在
    增删档位时漏改，而漏改一处就等于开了个后门。集中在一张表里，
    加档位时编译器会逼你看完整个表。
    """

    __slots__ = ("level", "execute_read", "execute_reversible",
                 "execute_irreversible", "auto_whitelist", "undo_window")

    def __init__(self, level: int, *, undo_window: float = DEFAULT_UNDO_WINDOW):
        self.level = int(level)
        #: 只读：级别 0 也不执行。★ 这是刻意的 —— 第一级的定义是
        #: 「纯建议，不做操作」，查询也算「做」。而且这保证了级别 0
        #: 与旧 shadow_mode=True 的行为**逐字等价**，升级不改变既有语义。
        self.execute_read = self.level >= AUTONOMY_WHITELIST
        self.execute_reversible = self.level >= AUTONOMY_WHITELIST
        self.execute_irreversible = self.level >= AUTONOMY_FULL
        self.auto_whitelist = self.level >= AUTONOMY_WHITELIST
        self.undo_window = float(undo_window)

    # ---- 三个问题的统一回答 ----
    def include_irreversible(self) -> bool:
        """给模型的工具清单里要不要带上不可逆工具。

        ★ 注意这与「执不执行」是两件事，但在本设计里恰好一致：
          级别 0/1 时不可逆工具**不进清单也不执行**（级别 1 还能提议吗？
          见 ``can_propose``）——这里返回 False 表示不进清单。
        """
        return self.execute_irreversible

    def can_propose(self, risk) -> bool:
        """该风险等级的操作是否允许生成「待确认提议」。

        级别 1 仍允许提议不可逆操作：用户要的是「白名单自动、其他以建议
        的方式」——「建议」在这里就体现为「生成一条待确认提议」。
        级别 0 连提议都不产生（纯建议 = 只输出文本）。

        ``risk`` 接受 ``RiskLevel`` 枚举**或**它的字符串值。
        """
        if self.level >= AUTONOMY_FULL:
            return False        # 第三级直接执行，不需要提议
        if self.level <= AUTONOMY_ADVISE:
            return False        # 第一级连提议都不生成
        return _is_irreversible(risk)

    def needs_undo(self, risk) -> bool:
        """执行后是否需要登记撤销记录。只有第三级动不可逆才需要。

        ``risk`` 接受 ``RiskLevel`` 枚举**或**它的字符串值。
        """
        return self.level >= AUTONOMY_FULL and _is_irreversible(risk)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "label": AUTONOMY_LABELS.get(self.level, f"未知({self.level})"),
            "hint": AUTONOMY_HINTS.get(self.level, ""),
            "execute_read": self.execute_read,
            "execute_reversible": self.execute_reversible,
            "execute_irreversible": self.execute_irreversible,
            "auto_whitelist": self.auto_whitelist,
            "undo_window": self.undo_window,
        }


def policy(level: Any, *, undo_window: float = DEFAULT_UNDO_WINDOW) -> AutonomyPolicy:
    """按档位取策略表。非法档位回落到 ``DEFAULT_AUTONOMY``。"""
    return AutonomyPolicy(normalize_autonomy(level), undo_window=undo_window)


def migrate_shadow(cfg: Dict[str, Any]) -> Optional[int]:
    """从旧 ``shadow_mode`` 迁移出档位。无需迁移时返回 ``None``。

    规则（详见模块头注释）：

        shadow_mode 存在且为 True  → 0（保持旧的保守行为）
        shadow_mode 存在且为 False → 1（旧的"关掉影子"= 只读/可逆可执行，
                                        不可逆仍提议 —— 正是级别 1）
        字段缺失且无 autonomy_level → 1（v7.9 默认）
        已有 autonomy_level        → 不迁移，原样使用

    ★ 只在**用户没有显式设过** ``autonomy_level`` 时才迁移。
      已经存了新字段的配置，再被旧字段影响就是 bug。

    ★ 迁移的目标是「**行为不变**」，不是「语义上最接近的新档位」。
      所以旧 ``False`` 落 1 而不是 2 —— 级别 2 是旧版本根本不存在的能力，
      升级时把它打开等于替用户做了他没做过的决定。
    """
    if not isinstance(cfg, dict):
        return None
    if cfg.get("autonomy_level") is not None:
        return None                     # 已迁移过，不覆盖
    if "shadow_mode" not in cfg:
        return None                     # 什么都不做，由调用方给默认值
    return AUTONOMY_ADVISE if bool(cfg.get("shadow_mode")) else AUTONOMY_WHITELIST
