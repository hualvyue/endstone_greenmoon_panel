"""``gm.config`` —— 子插件读写**自己包内**的配置。

    from gm import config

    who = config.get("bot_id", "")          # 读单个键
    cfg = config.all()                      # 读全量（dict）
    config.save({"bot_id": "123456"})       # 覆盖写
    config.update(bot_id="123456")          # 增量合并

数据落在子插件包根目录的 ``config.json`` 里，和面板的「⚙ 设置」页面读写
的**是同一份文件** —— 管理员在面板上改的值，这里立刻读得到；反过来也一样。

分工
----
``settings.json``（作者写，随包分发）
    声明"有哪些设置项"：字段的 key、类型、默认值、下拉选项……
``config.json``（运行时可写）
    存"这些设置项现在是什么值"。

没有 ``settings.json`` 就没有设置页，``save()`` 也会被拒 —— 因为主插件
不知道该让管理员填什么，无法校验你写进来的内容。

权限
----
需在 ``manifest.json`` 声明 ``"config"``：

    { "type": "py", "permissions": ["config"] }

⚠ **只能碰自己的配置**
------------------------
本模块所有函数都**不接受** uuid 参数，uuid 一律由调用栈反查得出
（``_context.current_owner()``）。这是刻意的硬边界：一旦开了"传参指定
读谁的配置"这个口子，任何一个 py 子插件都能去改别人的配置 —— 包括改掉
另一个子插件的 API token。

如果你确实需要读别的子插件的配置，那是设计问题，不是能力缺失：公共数据
应该由主插件侧提供专门的 API，而不是让子插件互相翻对方的包。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._context import require_grant, require_plugin
from ._errors import GmError

__all__ = ["all", "get", "save", "update", "keys", "reset", "path"]

_SCOPE = "config"


def _loader():
    """取主插件的子插件加载器（ModLoader 实例）。"""
    plugin = require_plugin()
    loader = getattr(plugin, "mod_loader", None)
    if loader is None:
        raise GmError("子插件加载器未初始化，无法读写配置。")
    return loader


def _owner() -> str:
    """当前子插件的 uuid；识别不出调用者返回空串。

    ``require_grant`` 内部已经查过一次调用栈，这里再查一次是**故意**的：
    uuid 只在这一处产出，后面的读写全用它，避免"权限校验用的身份"和
    "实际读写用的身份"是两次独立查询、理论上可能不一致。

    （识别不到调用者时 ``granted()`` 会放行 —— 那是给面板自身调用留的口子，
    但面板不会走这条 API；真走到这里说明是异常调用路径，直接报错更诚实。）
    """
    from ._context import current_owner
    return str(current_owner() or "")


def _require_owner() -> str:
    uuid = _owner()
    if not uuid:
        raise GmError(
            "无法确定当前子插件身份，配置读写仅限子插件自身调用。"
            "（面板侧请直接调 ModLoader.read_mod_config / write_mod_config）"
        )
    return uuid


def path() -> str:
    """返回当前子插件配置文件的绝对路径（``config.json``）。

    文件可能还不存在 —— ``save()`` 之后才有。只读场景下拿到路径去
    ``open()`` 请自行判存在。
    """
    require_grant(_SCOPE)
    uuid = _require_owner()
    loader = _loader()
    rec = loader.installed(uuid)
    if not rec:
        raise GmError(f"子插件 {uuid} 未安装或已卸载，无法定位配置。")
    import os
    root = str(rec.get("path") or "")
    if not root:
        raise GmError(f"子插件 {uuid} 的包目录未知，无法定位配置。")
    return os.path.join(root, "config.json")


def all() -> Dict[str, Any]:
    """返回当前子插件的全量配置（dict）。

    * 文件不存在 → ``{}``
    * 文件写坏了 → ``{}``（并记一条服务端日志）
    * ``settings.json`` 里已经删掉的旧键会被过滤掉，不会回给你

    返回值始终是**新 dict**，改它不会影响磁盘，也不会影响下次调用。
    """
    require_grant(_SCOPE)
    uuid = _require_owner()
    try:
        data = _loader().read_mod_config(uuid)
    except Exception as e:
        raise GmError(f"读取配置失败: {e}") from e
    return dict(data) if isinstance(data, dict) else {}


def get(key: str, default: Any = None) -> Any:
    """读单个配置项；不存在返回 ``default``。

    ``default`` 给了就优先用它 —— 注意这**不是**"schema 默认值"。
    schema 里声明的 ``default`` 会由主插件在首次保存时落到磁盘上，
    所以正常情况下这里能直接读到值；``default`` 参数是给你在
    "管理员还没打开过设置页"时兜底用的。
    """
    name = str(key or "")
    if not name:
        return default
    data = all()
    if name in data:
        return data[name]
    return default


def keys() -> List[str]:
    """返回当前配置文件里实际存在的键（已经过 schema 过滤）。"""
    return sorted(all().keys())


def save(values: Dict[str, Any]) -> Dict[str, Any]:
    """**覆盖写**配置：磁盘上的内容变成 ``values``。

    * 只接受 ``settings.json`` 声明过的键，未声明的会被拒（见返回值里的
      ``rejected``），不会落盘；
    * 值会按字段类型归一化（``"true"`` → 真布尔、``"3"`` → 数字等）；
    * 类型不符或超出 ``min``/``max`` 的键同样被拒。

    返回::

        {"ok": True, "saved": ["bot_id", "groups"], "rejected": []}

    ⚠ 未出现在 ``values`` 里的已声明字段**保持磁盘原值**，不会被清空。
    想清空一个文本字段，请显式传空串 ``{"note": ""}``。

    包内没有 ``settings.json`` 时抛 ``GmError`` —— 主插件不知道你有哪些
    设置项，无从校验，这种情况下写进去的配置面板也显示不出来。
    """
    require_grant(_SCOPE)
    uuid = _require_owner()
    if not isinstance(values, dict):
        raise GmError("save() 的入参必须是 dict。")
    ok, rejected, msg = _loader().write_mod_config(uuid, values)
    if not ok:
        raise GmError(msg or "保存配置失败。")
    saved = [k for k in values.keys() if str(k) not in set(rejected)]
    if rejected:
        # 不抛异常：部分成功也是成功，但要让作者知道哪些键被挡了，
        # 否则他会对着"存了但读不出来"的现象debug很久。
        try:
            _loader()._log(
                "warning",
                f"子插件 {uuid} 调用 gm.config.save() 时以下键被拒（未在 settings.json 声明或类型不符）: "
                f"{', '.join(str(r) for r in rejected)}",
            )
        except Exception:
            pass
    return {"ok": True, "saved": [str(k) for k in saved],
            "rejected": [str(r) for r in rejected], "message": msg}


def update(**values: Any) -> Dict[str, Any]:
    """**增量合并**：只改动传进来的键，其余保持原样。

        config.update(bot_id="123")     # 只动 bot_id

    等价于 ``save({**all(), **values})``，但少一次读-改-写的窗口。
    返回结构与 ``save()`` 相同。
    """
    merged = all()
    merged.update({str(k): v for k, v in values.items()})
    return save(merged)


def reset() -> Dict[str, Any]:
    """把所有已声明字段恢复成 ``settings.json`` 里的默认值。

    没有声明默认值的字段会被清成"未设置"（写动作本身仍会保留它们的
    磁盘原值语义见 ``save``，这里显式给出空值以覆盖）。
    """
    require_grant(_SCOPE)
    uuid = _require_owner()
    schema = _loader().settings_schema(uuid)
    if not schema:
        raise GmError(
            "该子插件没有声明设置项（包内缺少或无法解析 settings.json），无法重置。"
        )
    payload: Dict[str, Any] = {}
    for fld in schema:
        key = fld.get("key")
        if not key:
            continue
        ftype = fld.get("type")
        if "default" in fld:
            payload[key] = fld["default"]
        elif ftype == "bool":
            payload[key] = False
        elif ftype == "number":
            payload[key] = 0
        elif ftype == "select":
            opts = fld.get("options") or []
            payload[key] = opts[0]["value"] if opts else ""
        else:
            payload[key] = ""
    return save(payload)


def render_hint() -> Optional[str]:
    """返回给子插件的提示：面板设置页的入口在哪。

    供子插件在日志里打印一句「请在后台 XXX 处配置」用，避免作者把
    配置项硬编码成环境变量或者自建一个配置文件。
    """
    return "面板 → 子插件管理 → 对应子插件的「⚙ 设置」按钮"
