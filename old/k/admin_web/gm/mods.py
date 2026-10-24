"""``gm.mods`` —— 子插件（模组）管理：清点 / 启停 / 安装 / 卸载。

    from gm import mods

    mods.list()                       # 已装子插件清单
    mods.info('a1b2c3d4')             # 单个子插件的详情
    mods.enable('a1b2c3d4')           # 启用（py/code 型会真正加载代码）
    mods.disable('a1b2c3d4')          # 停用
    mods.install('/path/to/x.gmmod')  # 安装（路径必须在允许目录内）
    mods.uninstall('a1b2c3d4')        # 卸载（删目录，不可逆）

权限
----
需在 ``manifest.json`` 声明 ``"mods"``::

    { "type": "py", "permissions": ["mods"] }

⚠⚠ **这是最高危的权限，没有之一**
----------------------------------
* ``install()`` 会执行归档里的代码；
* ``uninstall()`` 会**真删目录**；
* ``enable()`` 会让一个 py 型子插件的 ``setup()`` 当场跑起来。

换句话说，拿到 ``mods`` 权限就等于拿到了**在这台机器上执行任意代码**
的能力（装一个恶意子插件即可）。请只授予你自己写的、可信的管理类子插件。

⚠ 另一个必须说清楚的自举风险：本模块允许子插件**卸载/停用其它子插件**，
  包括调用者自己。停用自己时，当前正在执行的栈还在跑，但代码已被移出
  内存 —— 后续调用会拿到 ``GmNotReady``。这不是本模块的 bug，是
  「热卸载正在跑的东西」的固有性质；调用方若要停用自己，请把动作放到
  ``gm.sched`` 之类的延后队列里（见文档「热重载与自停用」章节）。

实现说明
--------
本模块**只做权限 + 参数校验 + 主线程派发**，真正的逻辑全部委托给主插件
的 ``mod_loader.ModLoader``。刻意不在这里重写任何一步：

* 路径校验复用 ``mod_loader.install()`` 内部既有的校验（含 ZIP 校验、
  manifest 校验、``_check_env`` 环境检查）。**绝不能**在这里新造一套更宽松
  的 —— 两套校验必然漂移，而漂移的那套就是漏洞。
* 这里额外加的**只有**一条：限制 ``install(path)`` 的路径必须落在
  面板自己的 ``mods`` / 子插件目录下（见 ``_safe_install_path``）。这是
  给"子插件被攻陷"的场景兜一层底，防止它去装 ``/etc/passwd`` 之类的东西。

⚠ 所有操作都必须**在主线程**执行：``enable()`` 会 import 模块并执行
  ``setup()``，``disable()``/``uninstall()`` 会改 ``self._state`` 与
  Endstone 的事件注册表 —— 在后台线程碰这些会踩到未同步状态，轻则状态
  错乱，重则崩服。所以下面每个写操作都走 ``_run_in_server_thread``。
"""

from __future__ import annotations

import builtins                    # ★ 见下方「list 遮蔽」说明
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._context import require_grant, require_plugin
from ._errors import GmError

__all__ = ["list", "info", "enabled", "enable", "disable",
           "install", "uninstall", "reload"]

_SCOPE = "mods"

# ⚠⚠ 为什么到处写 ``builtins.list`` 而不是 ``list``
# ------------------------------------------------
# 本模块导出了一个**公开函数就叫 ``list``**（``mods.list()`` 是对外的
# API 名）。于是模块全局命名空间里的 ``list`` 指向的是**我们的函数**，
# 不再指向内置类型 —— 任何在模块内直接写 ``list(x)`` 的地方都会去调
# ``mods.list(x)`` 并立刻 ``TypeError``（出错信息还很容易误导人：
# 「list() takes 0 positional arguments」，看着像内置 list 坏了）。
# 所以本模块内一律用 ``builtins.list``，并有测试盯着。


def _loader():
    """取主插件的 ModLoader。"""
    plugin = require_plugin()
    mgr = getattr(plugin, "mod_loader", None)
    if mgr is None:
        raise GmError("子插件管理器未初始化。")
    return mgr


def _uuid(uuid: str) -> str:
    """uuid 的基础校验。

    真正"这个 uuid 存不存在"的判断交给 ``mod_loader.installed()`` ——
    它会做格式校验 + 查表，返回 ``None`` 表示不存在。这里只挡明显为空
    的输入，避免把一个空串传下去拿到一个语焉不详的报错。
    """
    u = str(uuid or "").strip()
    if not u:
        raise GmError("uuid 不能为空。")
    return u


# ------------------------------------------------------------------ 只读

def list() -> List[Dict[str, Any]]:
    """返回全部已安装子插件。

    每条记录含 ``uuid`` / ``name`` / ``type`` / ``version`` /
    ``permissions`` / ``enabled`` / ``running`` / ``missing_libs`` 等字段
    （``enabled``/``running`` 由主插件即时计算）。

    ⚠ 读操作**不需要**主线程：``mod_loader.list_mods()`` 只扫盘 + 读
      json，不碰服务端状态。但 ``enabled`` 字段读的是内存状态表，故仍是
      "某一瞬间的近似值"，不要拿它做跨调用的一致性判断。
    """
    require_grant(_SCOPE)
    try:
        return builtins.list(_loader().list_mods() or [])
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"读取子插件列表失败: {e}") from e

def info(uuid: str) -> Optional[Dict[str, Any]]:
    """按 uuid 取单个子插件记录；不存在返回 ``None``。"""
    require_grant(_SCOPE)
    u = _uuid(uuid)
    try:
        rec = _loader().installed(u)
    except Exception as e:
        raise GmError(f"读取子插件信息失败: {e}") from e
    return dict(rec) if rec else None


def enabled(ptype: str = "") -> Dict[str, Any]:
    """当前**启用中**的子插件。

    * 不传 ``ptype`` → ``{"page": ..., "admin": ..., "code": [...]}`` 全貌；
    * 传 ``"page"``/``"admin"`` → 返回该槽位启用的 uuid 或 ``None``；
    * 传 ``"code"``/``"py"`` → 返回启用中的 uuid 列表。
    """
    require_grant(_SCOPE)
    p = str(ptype or "").strip().lower()
    loader = _loader()
    try:
        state = dict(getattr(loader, "_state", None) or {})
    except Exception as e:
        raise GmError(f"读取启用状态失败: {e}") from e
    code = dict(state.get("code") or {})
    code_on = [u for u, on in code.items() if on]
    allview = {"page": state.get("page") or None,
               "admin": state.get("admin") or None,
               "code": code_on}
    if not p:
        return allview
    if p in ("page", "admin"):
        return {p: allview[p]}
    if p in ("code", "py"):
        return {"code": code_on}
    raise GmError(f"未知类型 {ptype!r}（可用: page / admin / code）。")


# ------------------------------------------------------------------ 写

def enable(uuid: str) -> Dict[str, Any]:
    """启用一个子插件。

    * page/admin 型 → 切换面板页面（会顶掉同槽位原占用的那个）；
    * py/code 型 → 真正 import 代码并调 ``setup()``；已在跑则等于热重载。

    失败（入口不存在、代码 import 报错、``setup()`` 抛异常）时主插件会
    **回滚状态**，不会留下"以为启用了其实没跑"的假象；此时本函数抛
    ``GmError``。
    """
    require_grant(_SCOPE)
    u = _uuid(uuid)
    if info(u) is None:
        raise GmError(f"未找到子插件 {u!r}。")
    loader = _loader()
    plugin = require_plugin()

    def _do():
        try:
            return loader.enable(u)
        except Exception as e:
            raise GmError(f"启用子插件失败: {e}") from e

    try:
        res = plugin._run_in_server_thread(_do, timeout=15.0)
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"启用子插件失败: {e}") from e
    return dict(res or {"ok": True})


def disable(uuid: str) -> Dict[str, Any]:
    """停用一个子插件（py/code 型会把入口模块移出内存）。"""
    require_grant(_SCOPE)
    u = _uuid(uuid)
    if info(u) is None:
        raise GmError(f"未找到子插件 {u!r}。")
    loader = _loader()
    plugin = require_plugin()

    def _do():
        try:
            return loader.disable(u)
        except Exception as e:
            raise GmError(f"停用子插件失败: {e}") from e

    try:
        res = plugin._run_in_server_thread(_do, timeout=15.0)
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"停用子插件失败: {e}") from e
    return dict(res or {"ok": True})


def reload(uuid: str) -> Dict[str, Any]:
    """热重载 = 停用后立刻启用（仅对 py/code 型有意义）。

    对 page/admin 型调用它就是"重新启用一次"，无副作用。
    """
    require_grant(_SCOPE)
    u = _uuid(uuid)
    disable(u)
    return enable(u)


def _safe_install_path(path: str) -> str:
    """校验安装包路径落在允许目录内，返回规范化后的绝对路径字符串。

    ★ 这是本模块**唯一**新加的校验，且刻意做得保守：
      允许的根目录只有主插件工作目录（``plugin.data_folder``）与
      面板的 ``mods`` 目录。子插件想装包，就把 .gmmod 放到这些目录下
      再传路径进来 —— 而不是让它有能力去读整台机器的任意路径。

    ⚠ 这里用的是 ``Path.resolve()`` 做**真实路径**比对，而不是字符串
      前缀。原因是符号链接：``/allowed/evil -> /etc`` 这种，字符串比对
      会放行，resolve 之后就能看出它其实指向外面。因此判断必须放在
      resolve 之后。
    """
    raw = str(path or "").strip()
    if not raw:
        raise GmError("安装包路径不能为空。")
    try:
        src = Path(raw).expanduser().resolve()
    except Exception as e:
        raise GmError(f"安装包路径无法解析: {e}") from e
    if not src.is_file():
        raise GmError(f"文件不存在: {src}")

    plugin = require_plugin()
    roots: List[Path] = []
    for attr in ("data_folder", "data_dir", "work_dir"):
        try:
            v = getattr(plugin, attr, None)
            if v:
                roots.append(Path(v).resolve())
        except Exception:
            continue
    try:
        loader = _loader()
        md = getattr(loader, "mods_dir", None)
        if md:
            roots.append(Path(md).resolve())
    except Exception:
        pass

    if not roots:
        # 一个允许根都拿不到 → 宁可拒绝，也不要在"不知道边界"的情况下放行
        raise GmError("无法确定允许的安装目录，已拒绝安装。")

    for root in roots:
        try:
            src.relative_to(root)
            return str(src)
        except ValueError:
            continue
    raise GmError(
        f"安装包必须位于插件数据目录内，已拒绝: {src}\n"
        f"允许的目录: {', '.join(str(r) for r in roots)}"
    )


def install(path: str, force: bool = False) -> Dict[str, Any]:
    """安装一个 ``.gmmod`` / ``.gmlib``。

    ``force=True`` 时跳过环境（Endstone 版本 / 依赖）检查。

    返回主插件给出的 ``{ok, kind, ...}``；失败时返回 ``{"ok": False,
    "error": "..."}``（**不抛异常** —— 安装失败是常见业务结果，不是
    程序错误，调用方应当检查 ``ok`` 字段）。

    路径必须位于插件数据目录内，详见 ``_safe_install_path``。
    """
    require_grant(_SCOPE)
    src = _safe_install_path(path)
    loader = _loader()
    plugin = require_plugin()

    def _do():
        try:
            return loader.install(src, force=bool(force))
        except Exception as e:
            raise GmError(f"安装子插件失败: {e}") from e

    try:
        res = plugin._run_in_server_thread(_do, timeout=30.0)
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"安装子插件失败: {e}") from e
    if not isinstance(res, dict):
        return {"ok": False, "error": f"安装返回了意外的结果: {res!r}"}
    return dict(res)


def uninstall(uuid: str) -> Dict[str, Any]:
    """卸载一个子插件（**删目录，不可逆**）。

    返回 ``{"ok": True, "uuid": ..., "removed": bool}``。
    ``removed=False`` 表示记录里没这东西（已被卸载/目录已消失），
    不算错误。

    ⚠ 不能卸载**正在运行的自己**并期待后续代码正常执行：当前栈还在跑，
      但模块已被移出内存，之后的 gm 调用会抛 ``GmNotReady``。
    """
    require_grant(_SCOPE)
    u = _uuid(uuid)
    loader = _loader()
    plugin = require_plugin()

    def _do():
        try:
            return loader.uninstall(u)
        except Exception as e:
            raise GmError(f"卸载子插件失败: {e}") from e

    try:
        res = plugin._run_in_server_thread(_do, timeout=30.0)
    except GmError:
        raise
    except Exception as e:
        raise GmError(f"卸载子插件失败: {e}") from e
    return {"ok": True, "uuid": u, "removed": bool(res)}
