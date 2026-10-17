"""``gm.bindings`` —— 玩家 QQ 账户绑定数据（纯读）。

    from gm import bindings

    bindings.by_qq("QQ_OPENID")     # -> 游戏名，如 "Steve"
    bindings.by_mc("Steve")         # -> {"qq": "...", "mc": "Steve", "bound_at": "..."}
    bindings.all()                  # 全量

权限：需在 manifest 声明 ``"bindings"``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._context import require_grant, require_plugin

__all__ = ["all", "by_qq", "by_mc", "get", "exists", "count", "pairs"]

_SCOPE = "bindings"


def all() -> Dict[str, Any]:
    """返回全量绑定数据。

    结构::

        {
          "by_qq": {"QQ_OPENID": "Steve", ...},
          "by_mc": {"steve": {"qq": "...", "mc": "Steve", "bound_at": "..."}, ...}
        }
    """
    require_grant(_SCOPE)
    plugin = require_plugin()
    try:
        data = plugin._load_bindings()
    except Exception:
        return {"by_qq": {}, "by_mc": {}}
    if not isinstance(data, dict):
        return {"by_qq": {}, "by_mc": {}}
    return data


def by_qq(qq_openid: str) -> Optional[str]:
    """用 QQ 的 openid 查绑定的游戏名；未绑定返回 None。"""
    require_grant(_SCOPE)
    plugin = require_plugin()
    want = str(qq_openid or "").strip()
    if not want:
        return None
    try:
        got = plugin._mc_for_qq(want)
    except Exception:
        return None
    return str(got) if got else None


def by_mc(mc_name: str) -> Optional[Dict[str, Any]]:
    """用游戏名查绑定记录；未绑定返回 None。

    返回 ``{"qq": "...", "mc": "...", "bound_at": "..."}``
    """
    require_grant(_SCOPE)
    plugin = require_plugin()
    want = str(mc_name or "").strip()
    if not want:
        return None
    try:
        got = plugin._qq_for_mc(want)
    except Exception:
        return None
    return dict(got) if isinstance(got, dict) else None


def get(mc_name: str) -> Optional[Dict[str, Any]]:
    """``by_mc`` 的别名。"""
    return by_mc(mc_name)


def exists(mc_name: str) -> bool:
    """该游戏名是否已绑定 QQ。"""
    return by_mc(mc_name) is not None


def count() -> int:
    """已绑定的玩家数量。"""
    return len(all().get("by_mc") or {})


def pairs() -> List[Dict[str, Any]]:
    """返回 `[{"mc": ..., "qq": ..., "bound_at": ...}, ...]` 形式的列表。"""
    out: List[Dict[str, Any]] = []
    for rec in (all().get("by_mc") or {}).values():
        if isinstance(rec, dict):
            out.append({
                "mc": str(rec.get("mc") or ""),
                "qq": str(rec.get("qq") or ""),
                "bound_at": str(rec.get("bound_at") or ""),
            })
    return out
