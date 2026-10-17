"""``gm.ai`` —— 把面板的 AI 能力暴露给子插件。

子插件里这样用：

    from gm import ai

    if ai.available():
        r = ai.ask("用一句话总结当前服务器状态")
        if r["ok"]:
            print(r["text"])

    # 注册自己的工具给 AI 用
    ai.register_tool(
        "query_shop_stock",
        description="查询商店某件商品的库存",
        params={"item": "商品名"},
        handler=lambda item: shop.stock_of(item),
        risk="read",
    )

    # 探测 / 调整接管级别
    print(ai.autonomy())            # 0 / 1 / 2
    ai.set_autonomy(1)              # 需要 "ai:control" 权限

权限
----
全部接口需要 ``manifest.json`` 声明 ``"ai"``：

    { "type": "py", "permissions": ["ai"] }

两个**额外**的高危 scope：

  · ``"ai:control"``      —— 允许 ``set_autonomy()`` 改接管级别。
    改级别等于改"AI 能不能自己动手删档"，比调一次问答重得多。
    单靠 ``"ai"`` 只能**读**级别，不能写。

  · ``"ai:irreversible"`` —— 允许注册 ``risk="irreversible"`` 的工具。
    这类工具在接管级别 2 下会被 AI 直接执行，影响面与内置的
    ``delete_player_data`` 同级。必须单独声明，给服主一个二次确认的机会。

设计约束（为什么这么严）
========================
**1. 子插件注册的工具也走同一套风险模型。**
   不是"子插件的工具随便注册"——它照样进 ``TOOL_REGISTRY``，
   照样受 ``gate`` 校验、照样按 ``RiskLevel`` 决定执行策略。
   唯一的不同是名字带前缀 ``mod.<uuid>.<name>``，便于区分来源。

**2. ``FORBIDDEN`` 依然永不注册。**
   即使子插件声明了 ``ai:irreversible``，``exec_command`` / ``grant_op``
   这类名字一样会被拒 —— 那道检查在 ``ToolSpec.__post_init__`` 里，
   是结构性的，不受任何权限影响。

**3. 子插件卸载时自动注销。**
   ``unregister_all()`` 由主插件在卸载子插件时调用。
   残留一个指向已卸载模块的 handler，调用时会抛
   ``ModuleNotFoundError``，而那会被 AI 当成"工具执行失败"，
   变成一堆莫名其妙的报错。
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from ._context import current_owner, require_grant, require_plugin

__all__ = [
    "available", "status", "ask", "register_tool", "unregister_tool",
    "registered_tools", "autonomy", "set_autonomy", "SCOPE",
    "SCOPE_CONTROL", "SCOPE_IRREVERSIBLE",
]

#: 基础权限：可用 available / status / ask / register_tool / 读 autonomy
SCOPE = "ai"

#: 高危：允许 set_autonomy（改接管级别）
SCOPE_CONTROL = "ai:control"

#: 高危：允许注册 risk="irreversible" 的工具
SCOPE_IRREVERSIBLE = "ai:irreversible"

#: 子插件注册的工具名前缀。用它把"谁注册的"编码进名字里 ——
#: 这样即使子插件被卸载、注销逻辑出 bug，审计日志里也能一眼看出
#: 这条调用属于哪个子插件。名字即溯源。
MOD_TOOL_PREFIX = "mod."

#: 注册表的写锁。子插件可能在自建线程里注册工具，与主线程的
#: 卸载/注销并发 —— 没有锁会出现"注册表在迭代时被改"的 RuntimeError。
_lock = threading.RLock()

#: ``{uuid: {tool_name: {"handler":..., "risk":..., "spec":...}}}``
_registry: Dict[str, Dict[str, Dict[str, Any]]] = {}

#: 工具名 → 子插件的原始 handler（经 _wrap_handler 包装后）
_HANDLERS: Dict[str, Any] = {}


# --------------------------------------------------------------------- 内部

def _manager():
    """取 AIManager；未就绪抛 ``GmNotReady``。"""
    plugin = require_plugin()
    mgr = getattr(plugin, "ai_manager", None)
    if mgr is None:
        from ._errors import GmNotReady
        raise GmNotReady("AI 模块尚未就绪，请稍后重试")
    return mgr


def _owner() -> str:
    """当前调用者 uuid。识别不出来时返回空串（由各接口自行决定是否放行）。"""
    try:
        return str(current_owner() or "")
    except Exception:
        return ""


def _safe_name(name: str) -> str:
    """把工具名规整成小写下划线形式。

    ★ 为什么要过滤：工具名会进到提示词和 ``TOOL_REGISTRY`` 的键里。
      一个带空格/换行的名字会破坏 JSON schema；一个带点的名字会与
      我们的分隔符 ``mod.<uuid>.<name>`` 混淆，导致注销时按错误的
      uuid 去查。所以统一成 ``[a-z0-9_]``，其余全部替换掉。
    """
    import re
    s = re.sub(r"[^0-9A-Za-z_]", "_", str(name or "")).strip("_").lower()
    # 连续下划线压成一个，避免 mod__a__b 这种难读的名字
    s = re.sub(r"_{2,}", "_", s)
    return s[:60]


# --------------------------------------------------------------------- 查询

def available() -> bool:
    """AI 是否已启用且配置有效（**不发请求、不消耗额度**）。

    典型用法：子插件启动时探测一次，决定要不要注册自己的 AI 工具 ——
    在没开 AI 的服务器上白注册一堆工具只是噪音。
    """
    require_grant(SCOPE)
    try:
        return bool(_manager().available())
    except Exception:
        return False


def status() -> Dict[str, Any]:
    """AI 的完整状态（含接管级别、额度、聊天配置）。

    直接返回 ``AIManager.status()`` 的副本。**不含 api_key**
    （那边本来就不回传），子插件拿不到凭据。

    ★ 返回的是 ``dict`` 副本，子插件改它不会影响内部状态 ——
      直接给引用会让一个手滑的 ``status()["enabled"] = True``
      看起来像"改了配置"，实际什么都不影响，最难查的一类 bug。
    """
    require_grant(SCOPE)
    st = _manager().status()
    return dict(st) if isinstance(st, dict) else {}


def autonomy() -> int:
    """当前接管级别：0 纯建议 / 1 白名单自动 / 2 完全放开。

    子插件可以据此调整自己的行为 —— 比如一个"提案审核"类插件
    在级别 0 时提示用户"AI 现在是纯建议模式，我的提案不会被自动执行"。
    """
    require_grant(SCOPE)
    try:
        return int(_manager().autonomy())
    except Exception:
        return 0


def set_autonomy(level: int) -> bool:
    """设置接管级别。**需要 ``ai:control`` 权限。**

    ★ 为什么这个要单独一个 scope：改级别等于改"AI 能不能自己动手"
      的全局策略，影响所有子插件、所有内置工具。一个只需要
      "问 AI 几个问题"的子插件不该顺手能把服务器推进
      "AI 可自主删档"的状态。

    ★ 只改**内存中的运行配置**并由主插件落盘 —— 本函数不直接写文件，
      避免子插件绕过主插件的写锁与字段白名单。
    """
    require_grant(SCOPE_CONTROL)
    mgr = _manager()
    try:
        ok, _msg = mgr.set_autonomy(level)
    except Exception:
        return False
    if not ok:
        return False
    # 落盘：走主插件的统一保存入口（它持写锁、做字段白名单）
    try:
        plugin = require_plugin()
        saver = getattr(plugin, "_save_ai_config", None)
        if callable(saver):
            ok2, _ = saver({"autonomy_level": int(level)})
            return bool(ok2)
    except Exception:
        pass
    # 存不下也返回 True：内存里已经生效了，重启后回原值 ≠ 操作失败。
    return True


# --------------------------------------------------------------------- 问答

def ask(prompt: str, *, system: str = "", max_tokens: int = 0,
        temperature: float = 0.2, allow_tools: bool = False
        ) -> Dict[str, Any]:
    """让 AI 回答一个问题。

    返回 ``{"ok": bool, "text": str, "error": str, ...}``。

    ``system``
        为空时用内置默认提示词 + 当前接管级别说明。给了就**完全替换** ——
        子插件的业务它自己最清楚，它要的措辞不该被我们的默认值干扰。

    ``allow_tools``
        是否把工具清单也给模型。默认 **False** —— 子插件问一句答一句
        是最常见的用法，让它能自己调工具（可能改动服务器）应该显式开启。

        ★ 即使开了，真正能调还是受接管级别与工具权限约束，
          这一层只是"要不要把清单给它看"。

    ★ **消耗服主的每日额度**，并受配额闸门约束。子插件不能成为
      绕过"防烧钱"的后门。额度用尽时返回 ``ok=False`` 并说明原因。
    """
    require_grant(SCOPE)
    try:
        return _manager().ask(str(prompt or ""), system=str(system or ""),
                              max_tokens=int(max_tokens or 0),
                              temperature=float(temperature),
                              allow_tools=bool(allow_tools))
    except Exception as e:
        return {"ok": False, "text": "", "error": f"调用失败：{e}"}


# ----------------------------------------------------------------- 工具注册

def register_tool(name: str, *, description: str,
                  handler: Callable[..., Any],
                  params: Optional[Dict[str, str]] = None,
                  optional: Optional[Any] = None,
                  risk: str = "read",
                  background: bool = True) -> bool:
    """注册一个让 AI 可以调用的工具。成功返回 ``True``。

    参数
    ----
    name
        工具名（不含前缀，本函数自动加 ``mod.<你的uuid>.``）。
        会被规整为小写下划线；与已有工具重名会失败。
    handler
        ``handler(**args) -> (ok, payload, note)`` 或任意可 JSON 化的值。
        两种签名都接受（见 ``_wrap_handler``）。
    risk
        ``"read"`` / ``"reversible"`` / ``"irreversible"``。
        **``"irreversible"`` 需要 ``ai:irreversible`` 权限。**
    background
        工具是否在后台线程执行。**涉及服务器 API 调用时保持 True**
        （默认），否则会阻塞主线程。

    ★ 风险等级是**由你声明**的，但主插件会做一致性检查：
      名字里带 ``delete`` / ``clear`` / ``set`` 这类动作词却声明
      ``read``，会被降级为 ``reversible`` 并写一条警告 ——
      名字与等级不符是 review 时最容易漏的东西。
    """
    require_grant(SCOPE)
    uuid = _owner()
    if not uuid:
        # 识别不出调用者 = 不是子插件在调（比如面板自身）。
        # ★ 这里**拒绝**而不是放行：注册工具必须能追溯到主人，
        #   否则注销时不知道找谁，会留下永久残留。
        raise PermissionError(
            "无法识别调用者，register_tool 只能由子插件调用")

    clean = _safe_name(name)
    if not clean:
        raise ValueError("工具名不合法（规范后为空）")

    # ---- 禁区名：结构与变体一律拒绝 ----
    # ★★ 这道检查必须在这里，不能依赖 ``ToolSpec.__post_init__``：
    #    那个只在 ``risk == FORBIDDEN`` 时抛异常，而本函数只会传
    #    read / reversible / irreversible —— 也就是说，如果没有这层
    #    检查，子插件可以注册一个名叫 ``exec_command`` 的“可逆”工具，
    #    它会安然进入 TOOL_REGISTRY 并被 AI 调用。这正是禁区清单
    #    要防的“自提权通路”，只是换了个入口。
    #    变体（exec_cmd / run_shell_command ...）同样拦，用 is_forbidden。
    from ..ai.tools import is_forbidden
    bad, why = is_forbidden(clean)
    if bad:
        raise ValueError(
            f"工具名 {clean!r} 命中禁区能力，不允许注册：{why}。"
            f"见 ai/tools.py 的 FORBIDDEN_CAPABILITIES —— "
            f"禁区是结构性不存在的，任何来源（含子插件）都不能注册。")

    # ---- 风险等级 ----
    from ..ai.tools import RiskLevel
    want = str(risk or "read").strip().lower()
    rmap = {"read": RiskLevel.READ,
            "reversible": RiskLevel.REVERSIBLE,
            "irreversible": RiskLevel.IRREVERSIBLE}
    if want not in rmap:
        raise ValueError(
            f"risk 必须是 read / reversible / irreversible（收到 {risk!r}）")
    level = rmap[want]

    if level == RiskLevel.IRREVERSIBLE:
        require_grant(SCOPE_IRREVERSIBLE)

    full = f"{MOD_TOOL_PREFIX}{uuid}.{clean}"

    # ---- 名字与风险等级的一致性检查 ----
    level = _reconcile_risk(clean, level, RiskLevel)

    # ---- 构造 ToolSpec ----
    # ★ 走和内置工具**完全相同**的构造函数。这意味着：
    #   · FORBIDDEN 的名字会在 __post_init__ 里被拒；
    #   · IRREVERSIBLE 会被自动加上 requires_confirm。
    #   子插件注册的工具不是"另一个世界的东西"。
    from ..ai.tools import ToolSpec
    # 把 params 的 value 规整成字符串说明（ToolSpec 要求 Dict[str,str]）
    p = {}
    if isinstance(params, dict):
        for k, v in params.items():
            kk = _safe_name(k)
            if kk:
                p[kk] = str(v)
    try:
        opt = frozenset(_safe_name(x) for x in (optional or [])
                        if _safe_name(x))
    except Exception:
        opt = frozenset()

    spec = ToolSpec(
        name=full,
        risk=level,
        description=f"[子插件 {uuid}] {str(description or '').strip()}",
        params=p,
        optional=opt,
        background=bool(background),
    )

    handler_fn = _wrap_handler(full, handler)

    with _lock:
        # 名字全局唯一（含内置工具）
        from ..ai.tools import TOOL_REGISTRY
        if full in TOOL_REGISTRY:
            raise ValueError(f"工具名已存在：{full}")

        # ---- 写进主注册表 ----
        # 直接赋值而不是调 tools._register()：后者在重复时抛异常，
        # 而我们已经在上一步检查过了。这里要的是"原子地同时写两张表"。
        TOOL_REGISTRY[full] = spec
        try:
            from ..ai.runner import ToolRunner
            ToolRunner._DISPATCH[full] = _make_dispatcher(full)
            _handlers()[full] = handler_fn
        except Exception:
            # 回滚：注册表写了一半比没写更糟（工具在清单里但执行不了）
            TOOL_REGISTRY.pop(full, None)
            raise

        _registry.setdefault(uuid, {})
        _registry[uuid][full] = {"handler": handler_fn, "risk": str(level),
                                 "spec": spec, "clean": clean}
    return True


def _risk_name(level, RiskLevel) -> str:
    return str(getattr(level, "value", level))


def _reconcile_risk(clean_name: str, level, RiskLevel):
    """名字与风险等级不符时，**往严的方向**修正。

    ★ 为什么是"往严"：一个声明成 ``read`` 但名字叫 ``delete_guild``
      的工具，如果不修正，它会绕过所有只读保护被 AI 自由调用。
      反过来（声明 irreversible 但其实是查询）只是多要一次确认，
      代价小得多。所以不一致时一律升到更严的那一档。
    """
    lname = str(clean_name or "").lower()
    if level == RiskLevel.IRREVERSIBLE:
        return level
    irreversible_stems = ("delete", "drop", "purge", "clear", "wipe",
                          "reset", "remove", "destroy")
    reversible_stems = ("kick", "ban", "mute", "set", "add", "grant",
                        "enable", "disable", "modify", "edit", "update")
    if any(s in lname for s in irreversible_stems):
        return RiskLevel.IRREVERSIBLE
    if level == RiskLevel.READ and any(s in lname for s in reversible_stems):
        return RiskLevel.REVERSIBLE
    return level


def _handlers() -> Dict[str, Any]:
    """工具名 → 包装后的 handler。与 ``ToolRunner._DISPATCH`` 一一对应。

    ★ 单独一张表的原因：``ToolRunner._DISPATCH`` 里放的是
      ``fn(runner_self, **kwargs)`` 形态的适配层，而这里要存的是
      **子插件自己的 handler**（用于注销时一并清理、以及自检时枚举）。
      两张表同步增删，``_drop`` 里保证原子性。
    """
    return _HANDLERS


def _wrap_handler(full: str, handler: Callable[..., Any]) -> Callable[..., Any]:
    """把子插件的 handler 包成 runner 期望的 ``(ok, payload, note)`` 形状。

    ★ 为什么要适配两种签名：子插件作者最自然的写法是
      ``lambda item: shop.stock(item)``（返回一个值），
      而 runner 要求的是三元组。逼所有人写三元组是没必要的摩擦 ——
      适配一层就好。返回值也能是 ``(ok, payload, note)``，
      那是需要精细控制失败原因时的用法。
    """
    def _call(**kwargs):
        r = handler(**kwargs)
        if isinstance(r, tuple) and len(r) == 3:
            ok, payload, note = r
            return bool(ok), payload, str(note or "")
        return True, r, f"{full} 执行完成"
    return _call


def _make_dispatcher(full: str):
    """runner 的 dispatch 表里放的是 ``fn(self, **kwargs)`` 形态，
    这里生成一个适配层，把 runner 实例忽略掉、转发给子插件的 handler。"""
    def _dispatch(_runner_self, **kwargs):
        fn = _HANDLERS.get(full)
        if fn is None:
            return False, None, f"工具 {full} 已注销"
        try:
            return fn(**kwargs)
        except Exception as e:
            # ★ 子插件抛的异常必须在这里被拦住并转成"执行失败"。
            #   让它冒到 runner 的 except 里也行 —— 但那样日志里
            #   看不到是哪个子插件出的问题，只有一句泛泛的
            #   "工具执行异常"。这里是唯一知道归属的地方。
            return False, None, f"子插件工具 {full} 执行异常：{e}"
    return _dispatch


def unregister_tool(name: str) -> bool:
    """注销本子插件注册的一个工具。"""
    require_grant(SCOPE)
    uuid = _owner()
    full = str(name or "")
    if not full.startswith(f"{MOD_TOOL_PREFIX}{uuid}."):
        # 允许传短名
        cand = f"{MOD_TOOL_PREFIX}{uuid}.{_safe_name(name)}"
        full = cand
    return _drop(uuid, full)


def registered_tools() -> List[Dict[str, Any]]:
    """本子插件已注册的工具列表（给子插件自己做自检用）。"""
    require_grant(SCOPE)
    uuid = _owner()
    with _lock:
        items = list((_registry.get(uuid) or {}).items())
    return [{"name": n, "risk": v["risk"],
             "description": getattr(v["spec"], "description", "")}
            for n, v in sorted(items)]


def unregister_all(uuid: str) -> int:
    """注销某个子插件的**全部**工具。由主插件在卸载子插件时调用。

    ★ 为什么需要这个：子插件卸载后，它注册的 handler 还指向
      已经不在 ``sys.modules`` 里的模块方法。保留着的话，AI 一调用
      就抛 ``ModuleNotFoundError``，日志里一堆"工具执行失败"，
      而真正的原因是"那个子插件早没了"。主动清掉更干净。

    返回注销的数量。
    """
    uuid = str(uuid or "").strip()
    if not uuid:
        return 0
    with _lock:
        tools = dict(_registry.pop(uuid, {}) or {})
    n = 0
    for full in tools:
        if _drop(uuid, full):
            n += 1
    return n


def _drop(uuid: str, full: str) -> bool:
    """从三张表里移除一个工具。返回是否真的删掉了东西。"""
    removed = False
    with _lock:
        owner_tools = _registry.get(str(uuid) or "")
        if owner_tools and full in owner_tools:
            owner_tools.pop(full, None)
            if not owner_tools:
                _registry.pop(str(uuid), None)
            removed = True
    try:
        from ..ai.tools import TOOL_REGISTRY
        if full in TOOL_REGISTRY:
            TOOL_REGISTRY.pop(full, None)
            removed = True
    except Exception:
        pass
    try:
        from ..ai.runner import ToolRunner
        if full in ToolRunner._DISPATCH:
            ToolRunner._DISPATCH.pop(full, None)
    except Exception:
        pass
    _HANDLERS.pop(full, None)
    return removed


def owners() -> List[str]:
    """当前注册过工具的子插件 uuid 列表（供主插件做自检）。"""
    with _lock:
        return sorted(_registry.keys())
