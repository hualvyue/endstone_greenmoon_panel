"""gm 的上下文：当前插件实例 + 调用者身份识别 + 权限校验。

设计要点
--------
**为什么不用 ContextVar / threading.local**
    子插件经常自建线程（定时任务、网络回调），``ContextVar`` 不会跨线程继承，
    ``threading.local`` 在新线程里也是空的 —— 两者都无法回答「现在是哪个子插件在调用」。

**改用调用帧反查**
    子插件的模块名固定为 ``greenmoon_mods.<uuid>``（由 ``ModLoader.import_mod`` 决定），
    沿调用栈往上找第一个属于该前缀的帧，即可无歧义地拿到 uuid。
    这个办法对子插件自建线程同样有效（新线程里栈底仍然是子插件的函数），
    且不需要任何全局可变状态，多个 py 子插件同时启用也不会串扰。
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
import threading
import types
from typing import Any, Dict, List, Optional, Tuple

from ._errors import GmNotReady, GmPermissionError

__all__ = [
    "attach",
    "detach",
    "get_plugin",
    "require_plugin",
    "current_owner",
    "require_grant",
    "granted",
    "api_version",
    "MOD_PREFIX",
    "GM_ALIASES",
]

#: 子插件模块名前缀，与 ModLoader.import_mod 保持一致
MOD_PREFIX = "greenmoon_mods."

#: gm API 版本，供子插件做兼容判断
__version__ = "1.0.0"

_lock = threading.RLock()
_plugin: Any = None

#: gm 包对外暴露的短名。子插件写 ``from gm import server``，
#: 但 gm 实际住在 ``admin_web.gm`` 里，必须补一个顶层别名才 import 得到。
GM_ALIASES = ("gm", "greenmoon_gm")

#: 进程级唯一的别名查找器（见 ``_AliasFinder``）
_ALIAS_FINDER: Optional["_AliasFinder"] = None


class _AliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """让 ``import gm.xxx`` 命中内存里那一份 ``admin_web.gm.xxx``。

    为什么光有 ``sys.modules['gm'] = pkg`` 还不够
    --------------------------------------------
    那只给**包对象**起了别名。第三方执行 ``import gm.panel`` 时，如果
    ``sys.modules['gm.panel']`` 是空的，import 机制会顺着 ``gm.__path__``
    把 ``panel.py`` **再执行一遍**，生成一个名为 ``gm.panel`` 的新模块。
    这个副本的 ``__package__`` 是 ``gm``，于是文件里的
    ``from .. import panel`` 就变成了「越过顶层包」，直接抛
    ``attempted relative import beyond top-level package`` —— 第三方插件
    正是在这里注册失败（日志里刷的就是这句）。

    在这里把这类导入截住：按真名取回**同一个模块对象**返回，既不重复
    执行模块代码，也不留下 ``__name__`` / ``__package__`` 被改写的副本。
    """

    def __init__(self, aliases: Tuple[str, ...], real_root: str) -> None:
        self.aliases = tuple(aliases)
        self.real_root = real_root
        self._pending: Dict[str, str] = {}       # 别名全名 -> 真实全名
        self._restore: Dict[str, Tuple[Any, Any]] = {}

    # -- MetaPathFinder --------------------------------------------------
    def find_spec(self, fullname, path=None, target=None):
        alias, _, rest = fullname.partition(".")
        # 顶层 ``gm`` 已由 _install_aliases 直接塞进 sys.modules，这里只管子模块
        if not rest or alias not in self.aliases or not self.real_root:
            return None
        real = self.real_root + "." + rest
        if real not in sys.modules:
            try:
                importlib.import_module(real)   # 走真名加载，相对导入合法
            except Exception:
                return None                     # 确实没有这个子模块
        self._pending[fullname] = real
        return importlib.util.spec_from_loader(fullname, self)

    # -- Loader ----------------------------------------------------------
    def is_package(self, fullname):
        real = self._pending.get(fullname)
        mod = sys.modules.get(real) if real else None
        return hasattr(mod, "__path__")

    def create_module(self, spec):
        real = self._pending.pop(spec.name, None)
        mod = sys.modules.get(real) if real else None
        if mod is None:
            return None
        self._restore[real] = (getattr(mod, "__spec__", None),
                              getattr(mod, "__loader__", None))
        return mod

    def exec_module(self, module):
        # import 机制会顺手改掉 __spec__ / __loader__，这里改回去，
        # 保证拿到的还是「原来那个模块」，热重载行为不受影响。
        real = getattr(module, "__name__", "")
        saved = self._restore.pop(real, None)
        if saved is None:
            return
        spec, loader = saved
        try:
            module.__spec__ = spec
            module.__loader__ = loader
        except Exception:
            pass


def _install_alias_finder(aliases: Tuple[str, ...], real_root: str) -> None:
    """装上（或更新）别名查找器，幂等。"""
    global _ALIAS_FINDER
    f = _ALIAS_FINDER
    if f is None:
        f = _AliasFinder(aliases, real_root)
        _ALIAS_FINDER = f
    else:
        f.aliases = tuple(aliases)
        f.real_root = real_root
    if f not in sys.meta_path:
        sys.meta_path.insert(0, f)


def _remove_alias_finder() -> None:
    global _ALIAS_FINDER
    f = _ALIAS_FINDER
    if f is None:
        return
    try:
        sys.meta_path.remove(f)
    except ValueError:
        pass
    _ALIAS_FINDER = None


def _repair_attrs(pkg: Any, real_root: str) -> None:
    """把真包上被「副本」顶掉的子模块属性改回来。

    ``import gm.panel`` 命中过一次「重跑 panel.py」的旧路径后，import
    机制会 ``setattr(gm, 'panel', 副本)`` —— 真包的属性就此被污染，
    之后连 ``from gm import panel`` 拿到的都是那个 ``__package__='gm'``
    的副本。这里按真名把内存里那份正确的模块挂回去。
    """
    if not real_root:
        return
    prefix = real_root + "."
    for name, obj in list(vars(pkg).items()):
        if not isinstance(obj, types.ModuleType):
            continue
        if str(getattr(obj, "__name__", "")).startswith(prefix):
            continue                        # 本来就是对的那份
        real = sys.modules.get(prefix + name)
        if real is not None and real is not obj:
            try:
                setattr(pkg, name, real)
            except Exception:
                pass


def _alias_submodules(pkg: Any, real_root: str, alias: str) -> None:
    """把 ``admin_web.gm.panel`` 这类已加载子模块同步登记成 ``gm.panel``。

    这是快路径：常见子模块全部预登记后，``import gm.panel`` 在
    ``sys.modules`` 就命中，连查找器都不用进。
    """
    if not real_root:
        return
    prefix = real_root + "."
    for obj in list(vars(pkg).values()):
        if not isinstance(obj, types.ModuleType):
            continue
        name = getattr(obj, "__name__", "")
        if not isinstance(name, str) or not name.startswith(prefix):
            continue
        short = name[len(prefix):]
        if not short or "." in short:
            continue
        key = alias + "." + short
        cur = sys.modules.get(key)
        cur_name = str(getattr(cur, "__name__", "")) if cur is not None else ""
        # 空槽 / 同一对象 / 自家重载前的旧副本（真名仍带前缀）→ 覆盖；
        # cur_name 与 key 不一致也说明是「重跑文件」留下的副本，同样覆盖。
        if cur is None or cur is obj or cur_name.startswith(prefix) or cur_name != key:
            sys.modules[key] = obj



def _install_aliases() -> None:
    """把 gm 包注册成顶层 ``gm``，让子插件的 ``import gm`` 能命中。

    只在槽位为空或已经被自己占用时写入，绝不顶掉别的东西 —— 万一服主
    真的装了个叫 gm 的包，我们退让（子插件仍可用 ``admin_web.gm``）。

    除了顶层包，还要把**已加载的子模块**一起登记（``gm.panel`` 等），
    并装上一个查找器兜住后续的 ``import gm.xxx``：否则 import 机制会
    按 ``gm.__path__`` 把子模块重新执行一遍，生成 ``__package__='gm'``
    的副本，副本里的 ``from .. import panel`` 直接越界报错。
    """
    pkg = _gm_package()
    if pkg is None:
        return
    real_root = getattr(pkg, "__name__", "") or ""
    _repair_attrs(pkg, real_root)
    bound: List[str] = []
    for alias in GM_ALIASES:
        cur = sys.modules.get(alias)
        # 空槽 / 就是自己 / 是**重载前的旧副本**（带 __gm_alias__ 标记）→ 覆盖。
        # 只对真·第三方 gm 包退让，避免顶掉别人的模块。
        if cur is None or cur is pkg or getattr(cur, "__gm_alias__", False):
            sys.modules[alias] = pkg
            _alias_submodules(pkg, real_root, alias)
            bound.append(alias)
    if bound:
        _install_alias_finder(tuple(bound), real_root)
    # 兜底：greenmoon.gm，别名被占时子插件还有路可走
    top = sys.modules.get("greenmoon")
    if top is None:
        top = types.ModuleType("greenmoon")
        top.__doc__ = "GreenMoon 面板的顶层命名空间（供子插件 `from greenmoon import gm` 使用）。"
        top.__path__ = []                   # 标记为包，避免被当成单文件模块
        sys.modules["greenmoon"] = top
    setattr(top, "gm", pkg)


def _gm_package() -> Any:
    """取 gm 包的模块对象（不是 _context 这个子模块）。"""
    mod = sys.modules.get(__name__)         # admin_web.gm._context
    pkg_name = (mod.__package__ if mod is not None else "") or ""
    if not pkg_name:
        pkg_name = __name__.rsplit(".", 1)[0]
    pkg = sys.modules.get(pkg_name)
    if pkg is None:
        try:
            import importlib

            pkg = importlib.import_module(pkg_name)
        except Exception:
            return None
    return pkg


def _remove_aliases() -> None:
    pkg = _gm_package()
    real_root = (getattr(pkg, "__name__", "") or "") if pkg is not None else ""
    prefix = (real_root + ".") if real_root else ""
    for alias in GM_ALIASES:
        cur = sys.modules.get(alias)
        if cur is not None and (cur is pkg or getattr(cur, "__gm_alias__", False)):
            sys.modules.pop(alias, None)
        if not prefix:
            continue
        # 子模块别名：只清**自家那份**（真名带 gm 包前缀），
        # 真·第三方 gm 包里的同名模块不动。
        for key in [k for k in list(sys.modules) if k.startswith(alias + ".")]:
            val = sys.modules.get(key)
            if val is not None and str(getattr(val, "__name__", "")).startswith(prefix):
                sys.modules.pop(key, None)
    _remove_alias_finder()
    top = sys.modules.get("greenmoon")
    if top is not None and getattr(top, "gm", None) is not None:
        try:
            delattr(top, "gm")
        except Exception:
            pass


def attach(plugin: Any) -> None:
    """挂载主插件实例（由 ``GreenMoonPlugin.on_enable`` 调用）。"""
    global _plugin
    with _lock:
        _plugin = plugin
    _install_aliases()


def detach() -> None:
    """卸载主插件实例（由 ``GreenMoonPlugin.on_disable`` 调用）。"""
    global _plugin
    with _lock:
        _plugin = None
    _remove_aliases()


def get_plugin() -> Any:
    """取当前插件实例；未挂载返回 None。"""
    return _plugin


def require_plugin() -> Any:
    """取当前插件实例；未挂载抛 ``GmNotReady``。"""
    p = _plugin
    if p is None:
        raise GmNotReady(
            "GreenMoon 面板尚未就绪（gm 未挂载）。"
            "请在插件启用后再调用 gm API，或检查面板是否已正常加载。"
        )
    return p


def api_version() -> str:
    """返回 gm API 版本号。"""
    return __version__


# --------------------------------------------------------------- 调用者识别

def current_owner() -> Optional[str]:
    """沿调用栈找属于子插件的帧，返回其 uuid；找不到返回 None。

    这是「哪个子插件在调用 gm」的唯一判定入口。子插件自建线程同样适用。

    ⚠ 必须把**主插件自己的模块**排除在外
    ------------------------------------
    ``MOD_PREFIX`` 是 ``greenmoon_mods.``，而主插件自己就住在这个前缀
    下面（``greenmoon_mods.admin_web.*``，含 gm 包本身）。如果只判断
    前缀，会出现两个方向的误判：

    1. **主插件被当成子插件。** ``gm/players.py`` 自己的模块名是
       ``greenmoon_mods.admin_web.gm.players``，会被解出一个假 uuid
       ``admin_web.gm.players``，拿去问 mod_loader 要权限必然拿不到，
       于是**主插件调用自己的 gm api 被自己拦下**。

    2. **子插件身份被主插件的帧盖住。** 栈是「子插件 → gm/players.py →
       gm/_context.py」时，从 ``_getframe(1)`` 开始第一个匹配前缀的帧
       就是 ``_context`` 自己（或 ``players``），于是**任何子插件调用
       都被判成 ``admin_web.gm._context``**，权限校验全面失效 ——
       有权限的被拒、没权限的也被拒，等于 gm 的权限系统完全没法用。

    区分办法：子插件模块名固定是 ``greenmoon_mods.<uuid>``，而
    **归一键只可能落在 ``greenmoon_mods.`` 之后的第一段**
    （见 ``ModLoader._module_name``：``re.sub(r"[^0-9A-Za-z_]", "_", uuid)``
    把 uuid 里的点和横线全换成了下划线），所以子插件的模块名
    **前缀之后只剩一段标识符、不含点**；而主插件是 ``admin_web.gm.xxx``，
    前缀之后**还有分隔点**。据此即可无歧义区分。
    """
    frame = sys._getframe(1)
    depth = 0
    while frame is not None and depth < 64:
        name = frame.f_globals.get("__name__", "")
        if isinstance(name, str) and name.startswith(MOD_PREFIX):
            rest = name[len(MOD_PREFIX):]
            # 只认「前缀之后为单个标识符」的模块 —— 含点说明是主插件
            # 自己的子模块（admin_web / gm / web ...），不是子插件。
            if rest and "." not in rest:
                return rest
        frame = frame.f_back
        depth += 1
    return None


def _permissions_of(uuid: str) -> List[str]:
    """取某个子插件在 manifest 里声明的权限列表。"""
    if not uuid:
        return []
    plugin = _plugin
    if plugin is None:
        return []
    loader = getattr(plugin, "mod_loader", None)
    if loader is None:
        return []
    try:
        rec = loader.installed(uuid)
    except Exception:
        rec = None
    if not rec:
        return []
    perms = rec.get("permissions") or []
    if not isinstance(perms, list):
        return []
    return [str(p).strip() for p in perms if str(p).strip()]


def granted(scope: str) -> bool:
    """当前调用者是否拥有某项权限。管理员之外的调用者按 manifest 声明判定。"""
    uuid = current_owner()
    if not uuid:
        # 无法识别调用者（例如面板自身调用）→ 放行，因为面板本身已受登录鉴权保护
        return True
    perms = _permissions_of(uuid)
    return "*" in perms or str(scope) in perms


def require_grant(scope: str) -> None:
    """校验当前调用者是否拥有 ``scope`` 权限；没有则抛 ``GmPermissionError``。"""
    if granted(scope):
        return
    uuid = current_owner() or "?"
    raise GmPermissionError(
        f"子插件 {uuid} 未获授权访问「{scope}」。"
        f"请在 manifest.json 的 permissions 字段里声明 \"{scope}\"（或 \"*\" 表示全部）。"
    )
