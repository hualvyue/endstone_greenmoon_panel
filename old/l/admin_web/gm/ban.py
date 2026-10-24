"""``gm.ban`` —— 服务器封禁列表（ban_list）读写。

    from gm import ban

    ban.list()                        # [{'name': 'Griefer', 'reason': '...'}, ...]
    ban.has('Griefer')                # True
    ban.add('Griefer', '刷屏')         # 封禁
    ban.remove('Griefer')             # 解封

权限
----
需在 ``manifest.json`` 声明 ``"bans"``::

    { "type": "py", "permissions": ["bans"] }

⚠ **这是高危权限**：封禁决定谁能进服。申请前请想清楚。

⚠⚠ 实现说明：为什么读用 ``server.ban_list``、写用控制台指令
--------------------------------------------------------
Endstone 0.11.11 的 ``Server`` 提供 ``ban_list``（``PlayerBanList``），
它有 ``.entries`` 可读（每项带 ``.name``），但**没有稳定的 Python 侧
增删方法**。所以：

* **读** → 直接遍历 ``ban_list.entries``（比解析控制台输出可靠得多）；
* **写** → 走 ``ban`` / ``pardon`` 控制台指令，并复用主插件的
  ``_safe_player_name`` 做注入防护。

⚠ ``ban_list`` 只能在**主线程**访问（它连着服务端状态）。所以读操作也
  必须调度到主线程 —— 这和 ``gm.whitelist`` 的读不同，那边主插件内部
  已经处理了调度。

历史坑（已在本模块规避）：老代码里读的是 ``getattr(server, "bans", None)``，
那个属性**不存在**，于是恒为空集 —— 表现是"面板上封禁名单永远是空的"，
但没有任何报错。这里只用确认存在的 ``ban_list``。
"""

from __future__ import annotations

import builtins                    # ★ 见下方「list 遮蔽」说明
from typing import Dict, List

from ._context import require_grant, require_plugin
from ._errors import GmError

__all__ = ["list", "add", "remove", "has", "snapshot"]

_SCOPE = "bans"

# ⚠⚠ 为什么到处写 ``builtins.list`` 而不是 ``list``
# ------------------------------------------------
# 本模块导出了一个**公开函数就叫 ``list``**（``ban.list()`` 是它对外的
# API 名）。于是模块全局命名空间里的 ``list`` 指向的是**我们的函数**，
# 而不是内置类型 —— 任何在模块内直接写 ``list(x)`` 的地方都会去调
# ``ban.list(x)``，那个函数不接受位置参数，立刻 ``TypeError``。
#
# 这个坑很隐蔽：出错信息是「list() takes 0 positional arguments」，
# 看起来像内置 list 出了问题，实际是名字被自己遮蔽了。而且它只在
# **_entries_raw / snapshot / has 真正被调用时**才炸 —— 静态看代码
# 完全正常。所以本模块内一律用 ``builtins.list``，并有测试盯着。


def _plugin():
    return require_plugin()


def _entries_raw() -> List[object]:
    """在主线程里取出 ``ban_list.entries`` 的原始列表。

    ★ 必须调度到主线程：``ban_list`` 连着服务端状态，后台线程碰它会踩到
      未同步的数据。主插件的 ``_run_in_server_thread`` 自己判断"当前是否
      已在主线程"，已在主线程时是**直接调用**、不额外排队，所以这里
      放心使用，不会自我死锁。
    """
    plugin = _plugin()

    def _read():
        bl = getattr(plugin.server, "ban_list", None)
        if bl is None:
            return []
        try:
            return builtins.list(bl.entries)
        except Exception:
            return []

    try:
        res = plugin._run_in_server_thread(_read, timeout=3.0)
    except Exception as e:
        raise GmError(f"读取封禁列表失败: {e}") from e
    return builtins.list(res or [])


def _entries_snapshot() -> List[Dict[str, str]]:
    """取封禁列表（**不带**权限校验的内部读）。带名字/理由去重。

    ★ 为什么要有这个内部函数
    ------------------------
    本模块导出了公开函数 ``list``，于是模块内名字 ``list`` 被自己遮蔽。
    直接写 ``list()`` 想调"读封禁列表"时，一旦有人手滑传参就会炸；
    所以把读逻辑收敛到这个显式命名的函数里，``list()`` / ``snapshot()`` /
    ``has()`` 都调它，语义不再双关。

    ⚠ 它**不校验权限**，只给本模块内已校验过的调用方用。
    """
    out: List[Dict[str, str]] = []
    seen = set()
    for e in _entries_raw():
        try:
            name = str(getattr(e, "name", "") or "").strip()
        except Exception:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        reason = ""
        for attr in ("reason", "message", "display"):
            try:
                v = getattr(e, attr, None)
            except Exception:
                v = None
            if v:
                reason = str(v)
                break
        out.append({"name": name, "reason": reason})
    return out


def list() -> List[Dict[str, str]]:
    """返回封禁列表 ``[{"name": ..., "reason": ...}, ...]``。

    ``reason`` 在某些实现里拿不到，此时为空串（不省略字段，方便调用方
    直接拼表格，不必到处判 None）。
    """
    require_grant(_SCOPE)
    try:
        return _entries_snapshot()
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"读取封禁列表失败: {e}") from e


def snapshot() -> Dict[str, object]:
    """封禁快照：``{"count": int, "items": [...]}``。"""
    require_grant(_SCOPE)                      # ★ 别忘了：它读的是同一份数据
    items = _entries_snapshot()
    return {"count": len(items), "items": items}


def has(name: str) -> bool:
    """该玩家是否被封禁（大小写不敏感）。"""
    require_grant(_SCOPE)
    probe = str(name or "").strip().lower()
    if not probe:
        return False
    return any(str(it.get("name", "")).lower() == probe for it in _entries_snapshot())


def _validate(name: str) -> str:
    """玩家名校验 —— 复用主插件的 ``_safe_player_name``（防指令注入）。"""
    raw = str(name or "").strip()
    if not raw:
        raise GmError("玩家名不能为空。")
    plugin = _plugin()
    safe = None
    try:
        safe = plugin._safe_player_name(raw)
    except Exception as e:
        raise GmError(f"玩家名校验失败: {e}") from e
    if safe is None:
        raise GmError(f"玩家名 {raw!r} 不合规（含非法字符或疑似指令注入），已拒绝。")
    return str(safe)


def add(name: str, reason: str = "") -> Dict[str, object]:
    """封禁一名玩家。

    ``reason`` 会被拼进控制台指令，因此**也要做同样的净化** —— 只校验
    玩家名而放过理由，等于给注入留了后门（``reason`` 里带换行即可）。
    这里把 reason 里的换行/回车剥掉，并限制长度。
    """
    require_grant(_SCOPE)
    safe = _validate(name)
    plugin = _plugin()
    already = has(safe)

    # ★ reason 同样危险：它也会被拼进 `ban <name> <reason>` 指令。
    #   只去掉换行是不够的 —— 但控制台指令的参数是以空白分隔的，
    #   换行/回车是唯一能"另起一条指令"的字符，剥掉它们即可阻断注入。
    #   其余可打印字符留在理由里是正常的（中文、标点都合法）。
    clean_reason = str(reason or "").replace("\r", " ").replace("\n", " ").strip()[:120]

    def _do():
        cmd = f"ban {safe}" + (f" {clean_reason}" if clean_reason else "")
        try:
            plugin.server.dispatch_command(plugin.server.command_sender, cmd)
        except Exception as e:
            raise GmError(f"执行封禁指令失败: {e}") from e

    try:
        plugin._run_in_server_thread(_do, timeout=3.0)
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"封禁失败: {e}") from e
    return {"ok": True, "name": safe, "reason": clean_reason, "changed": not already}


def remove(name: str) -> Dict[str, object]:
    """解封一名玩家（``pardon``）。"""
    require_grant(_SCOPE)
    safe = _validate(name)
    plugin = _plugin()
    existed = has(safe)

    def _do():
        try:
            plugin.server.dispatch_command(plugin.server.command_sender, f"pardon {safe}")
        except Exception as e:
            raise GmError(f"执行解封指令失败: {e}") from e

    try:
        plugin._run_in_server_thread(_do, timeout=3.0)
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"解封失败: {e}") from e
    return {"ok": True, "name": safe, "changed": existed}
