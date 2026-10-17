"""``gm.trust`` —— 玩家可信度（加权安全系统）。

    from gm import trust

    trust.get('Steve')                       # {'key':..., 'score':72.0, ...} 或 None
    trust.score('Steve')                     # 72.0（不存在则 0.0）
    trust.add('Steve', 5, '协助管理')          # 手动加分
    trust.add('Steve', -10, '破坏建筑')        # 手动扣分
    trust.set('Steve', 80.0)                 # 直接设分（受上下限钳制）

    trust.is_trusted_person('Steve')         # 是否「信任的人」
    trust.set_trusted_person('Steve', True)  # 设为「信任的人」→ 永久满分

权限
----
需在 ``manifest.json`` 声明 ``"trust"``::

    { "type": "py", "permissions": ["trust"] }

⚠⚠ 「信任的人」的语义（很容易误解，务必看清）
------------------------------------------------
``set_trusted_person(name, True)`` 之后，该玩家**每次登录自动保持满分**
（配置项 ``hard_ceiling``，默认 100），**绕过全部扣分逻辑** ——
包括断签惩罚、陌生设备扣分等等。

它不是"给个高分"，而是"永久免疫扣分"。授予前请确认你真的信任这个人；
``False`` 取消后会立即回落到其真实历史分。

⚠ 本模块是 ``trust`` scope 的**唯一官方入口**。
  旧文档里示范的 ``plugin.trust`` 直连写法虽然能跑（子插件拿得到主插件
  实例），但它**绕过了权限校验** —— 任何子插件都能改任何人的分数。
  现在请统一改用 ``gm.trust``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._context import require_grant, require_plugin
from ._errors import GmError

__all__ = [
    "get", "score", "all_scores", "set", "add",
    "is_trusted_person", "set_trusted_person", "trusted_people",
    "config", "note_whitelist",
]

_SCOPE = "trust"


def _mgr():
    """取主插件的 TrustManager。"""
    plugin = require_plugin()
    mgr = getattr(plugin, "trust", None)
    if mgr is None:
        raise GmError("可信度模块未初始化（可能未启用）。")
    return mgr


def get(name: str) -> Optional[Dict[str, Any]]:
    """返回该玩家的完整可信度档案（dict），没有档案返回 ``None``。

    返回的是**深拷贝**，改它不会影响主插件内部状态或磁盘。
    """
    require_grant(_SCOPE)
    n = str(name or "").strip()
    if not n:
        return None
    mgr = _mgr()
    try:
        key = mgr.find_key_by_name(n)
        if not key:
            return None
        return mgr.detail(key)
    except Exception as e:
        raise GmError(f"读取可信度档案失败: {e}") from e


def score(name: str) -> float:
    """只取分数；没有档案返回 ``0.0``（而不是 None，方便直接比较）。

    ⚠ 本函数**没有**独立的 ``require_grant`` —— 它只是 ``get()`` 的
      便利包装，而 ``get()`` 第一行就校验了。刻意不重复校验：如果哪天
      有人把 ``score`` 改成不经过 ``get`` 的实现，下面的测试（A 段会把
      ``score`` 标为"豁免项"，同时 B 段真的用它去撞权限墙）会立刻变红。
    """
    rec = get(name)
    if not rec:
        return 0.0
    try:
        return float(rec.get("score") or 0.0)
    except Exception:
        return 0.0


def all_scores(keyword: str = "", limit: int = 500) -> Dict[str, Any]:
    """批量取分数（等同面板列表）。返回 ``public_list()`` 的结构。"""
    require_grant(_SCOPE)
    try:
        return _mgr().public_list(keyword=keyword, limit=limit)
    except Exception as e:
        raise GmError(f"读取可信度列表失败: {e}") from e


def set(name: str, value: float) -> Dict[str, Any]:
    """把分数**直接设**为 ``value``（会被 ``hard_floor``/``hard_ceiling`` 钳制）。

    ⚠ 对「信任的人」无效 —— 他们登录时会被短路回满分。要真正降低其分数，
      请先 ``set_trusted_person(name, False)``。
    """
    require_grant(_SCOPE)
    n = str(name or "").strip()
    if not n:
        raise GmError("玩家名不能为空。")
    mgr = _mgr()
    try:
        key = mgr.find_key_by_name(n)
        if not key:
            raise GmError(f"未找到玩家「{n}」的可信度档案（需先进入过服务器）。")
        return mgr.set_score(key, value, by="gm", note="gm.set")
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"设置分数失败: {e}") from e


def add(name: str, delta: float, reason: str = "") -> Dict[str, Any]:
    """手动加/减分。``delta`` 为负即扣分。

    实际生效值可能被上下限截断（返回值里的 ``delta`` 是**真实生效**的量）。
    """
    require_grant(_SCOPE)
    n = str(name or "").strip()
    if not n:
        raise GmError("玩家名不能为空。")
    try:
        d = float(delta)
    except Exception:
        raise GmError(f"delta 必须是数字，收到 {delta!r}。")
    mgr = _mgr()
    try:
        key = mgr.find_key_by_name(n)
        if not key:
            raise GmError(f"未找到玩家「{n}」的可信度档案（需先进入过服务器）。")
        return mgr.adjust(key, d, reason or "gm.add", by="gm")
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"调整分数失败: {e}") from e


# ------------------------------------------------------------------ 信任的人

def is_trusted_person(name: str) -> bool:
    """该玩家是否是「信任的人」（永久满分）。"""
    require_grant(_SCOPE)
    try:
        return bool(_mgr().is_trusted_person(str(name or "").strip()))
    except Exception as e:
        raise GmError(f"查询「信任的人」状态失败: {e}") from e


def set_trusted_person(name: str, on: bool = True) -> Dict[str, Any]:
    """添加 / 移除「信任的人」。

    * ``on=True`` —— 该玩家**永久保持满分**，免疫全部扣分；
    * ``on=False`` —— 立即回落到真实历史分。

    返回 ``{"ok": True, "name": ..., "trusted": bool}``。
    玩家没有档案时抛 ``GmError``（本接口**不会**自动建档 —— 给一个
    从没进过服的玩家标记"信任"没有意义，还会污染档案目录）。
    """
    require_grant(_SCOPE)
    n = str(name or "").strip()
    if not n:
        raise GmError("玩家名不能为空。")
    mgr = _mgr()
    try:
        ok = bool(mgr.set_trusted_person(n, bool(on)))
    except Exception as e:
        raise GmError(f"设置「信任的人」失败: {e}") from e
    if not ok:
        raise GmError(
            f"未找到玩家「{n}」的可信度档案；该玩家需先进入过服务器。"
        )
    return {"ok": True, "name": n, "trusted": bool(on)}


def trusted_people() -> List[Dict[str, Any]]:
    """列出所有「信任的人」（``[{"name":..., "key":..., "score":...}, ...]``）。"""
    require_grant(_SCOPE)
    out: List[Dict[str, Any]] = []
    try:
        lst = _mgr().public_list(limit=100000)
    except Exception as e:
        raise GmError(f"读取「信任的人」列表失败: {e}") from e
    for r in (lst.get("players") or []):
        if isinstance(r, dict) and r.get("trusted_person"):
            out.append({"name": r.get("name"), "key": r.get("key"),
                        "score": r.get("score")})
    return out


# ------------------------------------------------------------------ 只读辅助

def config() -> Dict[str, Any]:
    """返回可信度模块的当前配置（只读副本）。

    ⚠ **只读**：改它不会生效。要改可信度设置请引导管理员去面板
      「安全」页，而不是让子插件偷偷写 —— 那会绕过管理员的知情权。
    """
    require_grant(_SCOPE)
    try:
        return dict(_mgr().cfg())
    except Exception as e:
        raise GmError(f"读取可信度配置失败: {e}") from e


def note_whitelist(name: str, device_id: str = "") -> bool:
    """通知主插件"该玩家通过了白名单审核"（打标记 + 可选信任其设备）。

    这是给白名单审批类子插件用的钩子：审核通过后调一下，该玩家会拿到
    白名单设备加分，且其设备不再被判为"陌生设备"。
    """
    require_grant(_SCOPE)
    n = str(name or "").strip()
    if not n:
        return False
    try:
        return bool(_mgr().note_whitelist(n, str(device_id or "")))
    except Exception as e:
        raise GmError(f"标记白名单失败: {e}") from e
