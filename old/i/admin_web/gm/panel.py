"""``gm.panel`` —— 把自己的设置面板挂进主面板（子插件用的接口）。

    from gm import panel

    def setup(plugin, gm):
        panel.register(
            plugin_id="my_prefix",          # 全局唯一，建议用你的 uuid
            title="我的前缀插件",
            icon="✨",
            description="聊天与公告前缀",
            fields=[
                {"key": "prefix", "type": "text", "label": "聊天前缀",
                 "default": "[玩家]", "hint": "显示在玩家名前面"},
                {"key": "enabled", "type": "bool", "label": "启用"},
                {"key": "mode", "type": "select", "label": "模式",
                 "options": [{"value": "a", "label": "全部"},
                             {"value": "b", "label": "仅管理员"}]},
                {"key": "secret", "type": "password", "label": "接口密钥"},
            ],
            loader=lambda: config.all(),          # 返回当前值
            saver=lambda v: (_write(v), "已保存"), # (ok, msg)
        )

面板会出现在主面板的「🧩 插件面板」板块里，用户填完点保存，
值就通过 ``saver`` 交回给你。

★ 没有插件注册时，那个板块**连同导航项一起不显示** ——
  它不会在界面上占一个常年空白的入口。

字段类型
--------
``text`` / ``password`` / ``number`` / ``bool`` / ``select`` /
``textarea`` / ``group`` / ``note``

* ``password`` 的值回传时会被打码（``******``）；用户没改动时
  交回给你的 saver 里**不会包含这个键** —— 你自己保留旧值即可。
* ``number`` 支持 ``min`` / ``max``，越界会被夹取。
* ``select`` 的 ``options`` 是 ``[{value, label}, ...]``，非法值会被丢弃。
* ``group`` / ``note`` 只是分组标题与说明，不产生值。

权限
----
需在 ``manifest.json`` 声明 ``"panel"``：

    { "type": "py", "permissions": ["panel", "config"] }

线程
----
``loader`` 与 ``saver`` 都由**主插件主线程**调用（用户点保存时）。
所以它们**必须快速返回**，不能阻塞（读网络、等锁）。
慢操作请在 saver 里丢给后台线程，并立刻返回成功。

生命周期
--------
用 ``register()`` 注册，插件卸载时主插件会**自动帮你注销**（它按
模块名认身份）。但你也可以主动调 ``unregister()`` 提前摘掉。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ._context import current_owner, require_grant, require_plugin
from ._errors import GmError

__all__ = ["register", "unregister", "mine", "available"]

_SCOPE = "panel"


def _registry():
    """取面板注册表。"""
    plugin = require_plugin()
    require_grant(_SCOPE)
    reg = getattr(plugin, "panel_registry", None)
    if reg is None:
        # 退化路径：拿进程级单例。主插件若因为异常没能把它挂上，
        # 这里仍然能工作（注册表本身是模块级单例）。
        try:
            from .. import panel as _p
            reg = _p.get_registry()
        except Exception as e:
            raise GmError(f"插件面板注册表不可用：{e}")
    return reg


def available() -> bool:
    """面板功能是否可用（注册表是否就绪）。"""
    try:
        _registry()
        return True
    except GmError:
        return False


def register(*, plugin_id: str, title: str, fields,
             description: str = "", icon: str = "\U0001F9E9",
             version: str = "", homepage: str = "", author: str = "",
             loader: Optional[Callable[[], Dict[str, Any]]] = None,
             saver: Optional[Callable[[Dict[str, Any]], Any]] = None,
             ) -> Tuple[bool, str]:
    """注册（或更新）一个面板。返回 ``(ok, message)``。

    Parameters
    ----------
    plugin_id : str
        全局唯一的 id。重复注册会**覆盖**前一个（方便插件热重载）。
    fields : list
        字段声明，最多 64 个。见模块 docstring 的类型说明。
    loader : callable
        无参，返回"当前值"的 dict。用户打开面板时调用。
    saver : callable
        收一个 dict（只含用户改动过的、且是你声明过的键），
        返回 ``(ok: bool, msg: str)``，或直接返回 bool。
    """
    reg = _registry()
    if not str(plugin_id or "").strip():
        raise GmError("register() 必须提供 plugin_id")
    if not isinstance(fields, (list, tuple)):
        raise GmError("fields 必须是列表")

    # ★ 用当前子插件的 uuid 作为 source，便于插件卸载时整批清掉。
    #   如果作者把 plugin_id 改来改去，靠它 alone 会留孤儿面板。
    owner = current_owner() or ""
    try:
        from .. import panel as _p
        return _p.register_panel(
            plugin_id=plugin_id, title=title, description=description,
            icon=icon, version=version, fields=list(fields),
            loader=loader, saver=saver, source=owner,
            homepage=homepage, author=author,
        )
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"注册面板失败：{e}")


def unregister(plugin_id: str) -> bool:
    """摘掉自己的面板。"""
    reg = _registry()
    return bool(reg.unregister(str(plugin_id or "")))


def mine() -> List[Dict[str, Any]]:
    """列出**自己**注册的面板。

    ★ 按 ``source``（= 当前子插件的 uuid）过滤，而不是把全部面板
      返回给调用方。理由与 ``gm.config`` 一致：子插件没有理由看到
      "别的插件挂了什么面板" —— 那既是信息泄露，也会诱使作者去写
      "我帮你改一下隔壁插件的配置"这类越界代码。

      识别不出调用者时返回空列表（而不是全量）：宁可让一个异常
      调用路径拿到空结果，也不要让它拿到别人的信息。
    """
    reg = _registry()
    owner = current_owner() or ""
    if not owner:
        return []
    return [d for d in reg.list_panels() if str(d.get("source") or "") == owner]
