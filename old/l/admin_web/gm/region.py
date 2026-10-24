"""``gm.region`` —— 区块日志与区域回档（子插件用的接口）。

    from gm import region

    # 1) 查最近的方块变更（只读）
    rows = region.recent(limit=30, actor="Steve")

    # 2) 先看影响面（只读，务必先做）
    info = region.preview(p1=(100, 60, 200), p2=(130, 80, 230),
                          dimension="overworld", minutes=60)

    # 3) 执行回档（异步！返回只代表"已受理"）
    res = region.rollback(p1=(100, 60, 200), p2=(130, 80, 230),
                          dimension="overworld", minutes=60,
                          actor="my_plugin", reason="清理炸服建筑")

    # 4) 想拿到最终结果就订阅事件（回档是分批跑的，可能持续数秒）
    def _on(ev, payload):
        if ev == "done":
            print("回档完成:", payload.get("applied"))
    off = region.subscribe(_on)
    ...
    off()          # 取消订阅

    # 5) 撤销
    region.undo(session_id=res["session_id"], actor="my_plugin")

权限
----
需在 ``manifest.json`` 声明 ``"region"``：

    { "type": "py", "permissions": ["region"] }

⚠ **这是高危权限。** 它能：

* 拉出完整方块变更流水 —— 谁在哪个坐标放了什么、挖了什么；
* **直接改写世界方块**（回档 / 撤销都是真改）；
* 撤销别人做过的回档。

回档是**不可逆**的（虽然支持撤销，但撤销窗口有限）。请务必先调
``preview()`` 把影响面看清楚，再决定要不要动手。

时间窗
------
``preview()`` / ``rollback()`` 的时间窗支持四种单位，**四选一**
（同时传多个时按 minutes → seconds → hours → days 取第一个，其余忽略）：

    minutes=60        # 恢复到 60 分钟之前
    seconds=600       # 等价写法
    hours=1           # 等价写法
    days=1            # 恢复到 1 天之前

一个都不给时用配置里的 ``default_delta_seconds``（默认 1 小时）。
单位写错（例如 ``minutes="半小时"``）会直接抛 ``GmError`` ——
宁可让你立刻知道写错了，也不要悄悄按默认值给你回档一小时的伤害面。

维度
----
``preview()`` / ``rollback()`` / ``at()`` 的 ``dimension`` **留空 =
服务端默认维度**（通常 overworld），而**不是**"不限维度"：方块记录按维度
精确索引，跨维度混查只会得出错误结论。要查别的维度就显式传名字。

唯一例外是 ``recent()`` —— 它的 ``dimension`` 是**过滤器**，留空表示不限。
同名不同义，调用前看清是哪个函数。

线程
----
本模块所有函数都会把工作**派发到服务器主线程**执行（回档必须在那里
跑，理由见 regionlog/engine.py 的说明），调用方不需要自己切线程。
但正因为如此，**不要在主线程里 await 它的完成** —— 它是异步的，
返回时任务才刚刚开始。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ._context import require_grant, require_plugin
from ._errors import GmError

__all__ = [
    "enabled", "status",
    "recent", "at", "sessions", "preview",
    "rollback", "undo", "subscribe",
]

_SCOPE = "region"


def _api():
    """取主插件的区块 API；未装配抛 GmError。"""
    plugin = require_plugin()
    require_grant(_SCOPE)
    api = getattr(plugin, "region_api", None)
    if api is None:
        raise GmError(
            "区块日志系统未初始化（主插件启动时装配失败，"
            "或该版本不支持）。请查看服务器日志中的「[区块日志]」条目。")
    return api


def _region_dict(p1, p2, center, radius, **kw) -> Dict[str, Any]:
    """把调用方给的位置参数整理成 RegionAPI 认识的形状。

    ★ 为什么允许三种写法：子插件作者手里的坐标来源五花八门 ——
      可能是一对 Block、可能是一个中心点加半径。强制统一成一种
      只会让每个作者都写一遍转换。
    """
    d: Dict[str, Any] = dict(kw)
    if p1 is not None and p2 is not None:
        a = tuple(p1)
        b = tuple(p2)
        if len(a) != 3 or len(b) != 3:
            raise GmError("p1 / p2 必须是长度为 3 的坐标元组")
        d["p1"], d["p2"] = list(a), list(b)
    elif center is not None and radius is not None:
        c = tuple(center)
        if len(c) != 3:
            raise GmError("center 必须是长度为 3 的坐标元组")
        d["center"], d["radius"] = list(c), int(radius)
    else:
        raise GmError("需要 p1+p2（对角两点）或 center+radius（中心+半径）")
    return d


def _timekw(minutes=None, seconds=None, hours=None, days=None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in (("minutes", minutes), ("seconds", seconds),
                 ("hours", hours), ("days", days)):
        if v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                raise GmError(f"{k} 必须是数字")
    return out


# ---------------------------------------------------------------------------
# 只读
# ---------------------------------------------------------------------------
def enabled() -> bool:
    """区块记录是否开启。**任何调用前都应该先问一句。**"""
    try:
        api = _api()
    except GmError:
        return False
    return bool(api.enabled)


def status() -> Dict[str, Any]:
    """运行状态与库统计（只读）。"""
    api = _api()
    return api.status(caller="plugin").to_dict()


def recent(limit: int = 50, actor: str = "", action: str = "",
           dimension: str = "") -> List[Dict[str, Any]]:
    """最近的方块变更流水。

    ``actor`` / ``action`` / ``dimension`` 留空表示不限。
    ``action`` 取值：break / place / explode / liquid_flow 等。
    """
    api = _api()
    r = api.query_recent(limit=limit, actor=actor, action=action,
                         dimension=dimension, caller="plugin")
    if not r.ok:
        raise GmError(r.message)
    return r.data.get("rows") or []


def at(x: int, y: int, z: int, dimension: str = "",
       limit: int = 20) -> List[Dict[str, Any]]:
    """某个坐标的变更历史（这一格以前是什么）。

    ``dimension`` **留空 = 用服务端默认维度**（通常是 overworld），
    而**不是**"不限维度" —— 方块记录是 ``WHERE x=? AND y=? AND z=?
    AND dimension=?`` 精确索引的，把其它维度的记录混进来没有意义，
    反而会让你得出"这一格被人动过 / 一直没人动过"的错误结论。
    要查别的维度请**显式传维度名**。

    ⚠ 注意与 ``recent()`` 的区别：``recent()`` 的 ``dimension`` 是
    **过滤器**（留空 = 不限），``at()`` 的是**查询条件**（留空 = 默认
    维度）。同名不同义，别搞混。
    """
    api = _api()
    r = api.query_position(x=int(x), y=int(y), z=int(z),
                           dimension=dimension, limit=limit, caller="plugin")
    if not r.ok:
        raise GmError(r.message)
    return r.data.get("rows") or []


def sessions(limit: int = 20, actor: str = "") -> List[Dict[str, Any]]:
    """历史回档会话。**AI 与其它插件发起的回档也会出现在这里。**"""
    api = _api()
    r = api.list_sessions(limit=limit, actor=actor, caller="plugin")
    if not r.ok:
        raise GmError(r.message)
    return r.data.get("rows") or []


def preview(*, p1=None, p2=None, center=None, radius=None,
            dimension: str = "", minutes=None, seconds=None,
            hours=None, days=None) -> Dict[str, Any]:
    """影响面预览（只读，不改世界）。

    **执行回档之前必须先调这个。** 它告诉你会动多少条记录、
    涉及哪些玩家、主要改哪些方块类型。

    返回字段：``records_hit`` / ``by_action`` / ``by_actor`` /
    ``top_blocks`` / ``truncated``（被上限截断时为 True，说明实际
    影响可能比看到的大）。
    """
    api = _api()
    region = _region_dict(p1, p2, center, radius)
    r = api.preview(region=region, dimension=dimension, caller="plugin",
                    **_timekw(minutes, seconds, hours, days))
    if not r.ok:
        raise GmError(r.message)
    return r.data


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def rollback(*, p1=None, p2=None, center=None, radius=None,
             dimension: str = "", actor: str = "", reason: str = "",
             minutes=None, seconds=None, hours=None, days=None,
             notify: str = "", dry_run: bool = False,
             exclude_blocks: Optional[Iterable[str]] = None,
             only_blocks: Optional[Iterable[str]] = None,
             rollback_water: bool = True,
             rollback_lava: bool = True) -> Dict[str, Any]:
    """执行区域回档。**异步**：返回即代表"已受理"，不代表完成。

    Returns
    -------
    dict
        ``{"started": True, "session_id": N, "selected": N, "truncated": bool}``
        没有命中任何记录时 ``started=False, session_id=None``。

    Notes
    -----
    ``actor`` 必填 —— 回档是破坏性操作，必须留下"谁干的"。
    用你自己插件的名字，不要填玩家名（除非确实是代某个玩家执行）。

    ``dry_run=True`` 只做查询，不写世界。

    回档结果通过 ``subscribe()`` 的事件拿到（事件名 ``done``）。
    也可以事后调 ``sessions()`` 查最新会话的 ``block_count``。
    """
    api = _api()
    if not api.enabled:
        raise GmError("区块记录未开启，无法回档（服主可在面板开启）。")
    who = str(actor or "").strip()
    if not who:
        raise GmError("rollback() 必须提供 actor（谁发起的），用于留痕。")
    region = _region_dict(p1, p2, center, radius)
    r = api.rollback(
        region=region, dimension=dimension, actor=who,
        notify=notify, dry_run=dry_run,
        exclude_blocks=set(exclude_blocks) if exclude_blocks else None,
        only_blocks=set(only_blocks) if only_blocks else None,
        rollback_water=bool(rollback_water), rollback_lava=bool(rollback_lava),
        caller="plugin", **_timekw(minutes, seconds, hours, days))
    if not r.ok:
        raise GmError(r.message)
    return r.data


def undo(*, session_id: Optional[int] = None, actor: str = "",
         reason: str = "", notify: str = "",
         dry_run: bool = False) -> Dict[str, Any]:
    """撤销一次回档。``session_id`` 留空 = 撤销最近一次。**异步。**"""
    api = _api()
    if not api.enabled:
        raise GmError("区块记录未开启，无法撤销。")
    who = str(actor or "").strip()
    if not who:
        raise GmError("undo() 必须提供 actor（谁发起的），用于留痕。")
    sid = None
    if session_id not in (None, "", 0):
        try:
            sid = int(session_id)
        except (TypeError, ValueError):
            raise GmError("session_id 必须是整数")
    r = api.undo(session_id=sid, actor=who, notify=notify,
                 dry_run=dry_run, caller="plugin")
    if not r.ok:
        raise GmError(r.message)
    return r.data


def subscribe(fn: Callable[[str, Dict[str, Any]], None]) -> Callable[[], None]:
    """订阅回档事件，返回取消订阅的函数。

    事件名与 payload 请见 ``regionlog/api.py`` 的 ``publish`` 调用处：
    ``started`` / ``progress`` / ``done`` / ``no_change`` / ``error``，
    payload 至少含 ``session_id`` / ``actor`` / ``caller``。

    ★ 回调在**发起事件的那条线程**上执行（回档时是服务器主线程）。
      所以回调里**绝不能**做慢操作（读大文件、发网络请求）——
      那会直接卡 tick。要干活请自己再丢出去一个线程。
    """
    api = _api()
    return api.subscribe(fn)
