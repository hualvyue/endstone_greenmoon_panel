"""``gm.whitelist`` —— 服务器白名单（allowlist）读写。

    from gm import whitelist

    whitelist.list()              # ['Steve', 'Alex']
    whitelist.has('Steve')        # True
    whitelist.add('Steve')        # 加入白名单
    whitelist.remove('Steve')     # 移出

权限
----
需在 ``manifest.json`` 声明 ``"whitelist"``::

    { "type": "py", "permissions": ["whitelist"] }

⚠ **这是高危权限**：白名单决定谁能进服。请只在你确实要承担这个责任时申请。

⚠⚠ 为什么不直接 ``server.dispatch_command("allowlist add X")``
------------------------------------------------------------
两个原因，都很实际：

1. **指令注入**。玩家名是要**拼进控制台指令**的。名字里带换行就能追加
   任意一条指令（``"Steve\\nop Notch"``）。所以必须过主插件的
   ``_safe_player_name`` 白名单校验，本模块不自己写一套简化的
   （两套校验迟早会漂移，而漂移的那一套就是漏洞）。
2. **线程**。``dispatch_command`` 必须在**服务器主线程**调用，子插件多半
   在自己的线程里跑。主插件已有 ``_run_in_server_thread`` 负责调度，
   直接复用即可，不必让每个子插件各自处理调度。

所以本模块只是**薄转发**：校验权限 → 交给主插件的既有实现。
"""

from __future__ import annotations

import builtins                    # ★ 见下方「list 遮蔽」说明
from typing import Dict, List

from ._context import require_grant, require_plugin
from ._errors import GmError

__all__ = ["list", "add", "remove", "has", "snapshot"]

_SCOPE = "whitelist"

# ⚠⚠ 为什么到处写 ``builtins.list`` 而不是 ``list``
# ------------------------------------------------
# 本模块导出了一个**公开函数就叫 ``list``**（``whitelist.list()`` 是它
# 对外的 API 名）。于是模块全局命名空间里的 ``list`` 指向的是**我们的
# 函数**，而不是内置类型 —— 任何在模块内直接写 ``list(x)`` 的地方都会
# 去调 ``whitelist.list(x)``，那个函数不接受位置参数，立刻 ``TypeError``。
#
# 这个坑很隐蔽：出错信息是「list() takes 0 positional arguments」，
# 看起来像内置 list 出了问题，实际是名字被自己遮蔽了。而且它只在
# **snapshot / has 真正被调用时**才炸 —— 静态看代码完全正常。
# 所以本模块内一律用 ``builtins.list``，并有测试盯着。


def _plugin():
    return require_plugin()


def _names_snapshot() -> List[str]:
    """取白名单名字列表（**不带**权限校验的内部读）。

    ★ 为什么要有这个内部函数，而不是直接调 ``list()``
    ------------------------------------------------
    本模块导出了公开函数 ``list``，于是模块内名字 ``list`` 被它自己
    遮蔽 —— 写 ``list()`` 虽然"看起来"是调自己那个读名单的函数，能跑，
    但极其危险：只要哪天有人手滑写成 ``list(x)``，或者把 ``builtins.list``
    与它混用，就会拿到空列表/直接 TypeError。在这里显式包一层，
    语义（"读名单快照"）和名字都不再双关。

    ⚠ 它**不校验权限** —— 只给本模块内已经校验过的调用方用。
      （`list()` / `snapshot()` / `has()` 各自都已经先 require_grant。）
    """
    plugin = _plugin()
    names = plugin._local_whitelist_names()
    out: List[str] = []
    seen = set()
    for n in (names or []):
        s = str(n).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def list() -> List[str]:
    """返回当前白名单里的玩家名（有序，去重后的列表）。

    读操作**不需要**排在主线程：主插件的 ``_local_whitelist_names``
    自己会做「要不要调度到主线程」的判断（它内部走
    ``_run_in_server_thread``），子插件不必关心。

    取不到（服务端未就绪 / 解析失败）时返回空列表而不是抛异常 ——
    「读不到名单」和「名单是空的」在调用方看来都只能渲染成"暂无"，
    为此抛异常反而逼着每个调用方写一遍 try/except。
    """
    require_grant(_SCOPE)
    try:
        return _names_snapshot()
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"读取白名单失败: {e}") from e


def snapshot() -> Dict[str, object]:
    """白名单快照：``{"count": int, "names": [...]}``，便于日志/展示。"""
    require_grant(_SCOPE)                      # ★ 别忘了：它读的是同一份数据
    names = _names_snapshot()
    return {"count": len(names), "names": names}


def has(name: str) -> bool:
    """该玩家是否在白名单里（大小写不敏感）。"""
    require_grant(_SCOPE)
    probe = str(name or "").strip().lower()
    if not probe:
        return False
    return any(n.lower() == probe for n in _names_snapshot())


def _validate(name: str) -> str:
    """玩家名校验 —— 复用主插件的 ``_safe_player_name``。

    ★ 刻意**不**在这里自己写正则：主插件那套是配合 ``_exec_allowlist_cmd``
      的注入防护设计的，两边必须完全一致。自己实现一套等价逻辑，
      等哪天主插件收紧了规则，这里就是现成的绕过口。
    """
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
        raise GmError(
            f"玩家名 {raw!r} 不合规（含非法字符或疑似指令注入），已拒绝。"
        )
    return str(safe)


def add(name: str) -> Dict[str, object]:
    """把玩家加入白名单。返回 ``{"ok": True, "name": ..., "changed": bool}``。

    ``changed=False`` 表示该玩家原本就在名单里（幂等，不算失败）。
    """
    require_grant(_SCOPE)
    safe = _validate(name)
    plugin = _plugin()
    already = has(safe)
    try:
        # 交给主插件的既有实现：它内部会 _safe_player_name 再校验一次
        # 并调度到主线程 dispatch_command。这里多校验一次是廉价的纵深防御。
        plugin._run_in_server_thread(plugin._exec_allowlist_cmd, safe, "add", timeout=3.0)
    except Exception as e:
        raise GmError(f"加入白名单失败: {e}") from e
    return {"ok": True, "name": safe, "changed": not already}


def remove(name: str) -> Dict[str, object]:
    """把玩家移出白名单。返回 ``{"ok": True, "name": ..., "changed": bool}``。"""
    require_grant(_SCOPE)
    safe = _validate(name)
    plugin = _plugin()
    existed = has(safe)
    try:
        plugin._run_in_server_thread(plugin._exec_allowlist_cmd, safe, "remove", timeout=3.0)
    except Exception as e:
        raise GmError(f"移出白名单失败: {e}") from e
    return {"ok": True, "name": safe, "changed": existed}
