"""第三方插件面板（Extension Panels）。

这是什么
--------
一个**隐形的**对外开放口子：别的 Endstone 插件可以把它自己的设置项
挂进 GreenMoon 的 Web 面板，用户于是能在一个后台里配置所有插件，
而不是为每个插件各开一个端口、各记一套账号。

为什么叫"隐形"
--------------
它不在主导航里占一个常驻入口。只有当**确实有插件注册了面板**时，
左侧才会冒出「🧩 插件面板」这一项；一个插件都没接进来时，整个板块
连同导航项一起不存在。这是刻意的设计 —— 一个占着位置却常年空白的
板块，比没有这个板块更让人困惑。

两条接入方式
------------
1. **Python 直连**（同一进程内的其它插件 :: 推荐）

       from admin_web.panel import register_panel
       register_panel(
           plugin_id="my_plugin",
           title="我的插件",
           fields=[{"key": "prefix", "type": "text", "label": "前缀"}],
           loader=lambda: {"prefix": "..."},
           saver=lambda values: (True, ""),
       )

2. **HTTP 远程注册**（别的进程 / 别的语言写的插件）

       POST /api/ext/register   （带 API token）

   远程方式每 ``ttl`` 秒要续约一次，超时自动摘除 —— 避免"插件明明
   已经卸载了，面板还挂着它的入口"。

为什么两种都留
--------------
Python 直连没有序列化开销、能直接回调；但它要求对方插件**先能拿到
admin_web 这个模块**，这在别的进程 / 别的语言里不成立。HTTP 方式
把这层限制去掉，代价是需要 token 与心跳。

安全边界（重要）
----------------
* 注册面板**不等于**获得权限。面板本身是否可见，仍受当前登录者的
  scope 约束（见 ``web.py`` 的 ``_SCOPE_EXACT``）。
* 插件提供的 ``saver`` 由**主插件主线程**调用（通过
  ``_run_in_server_thread``），不允许插件自己在后台线程里改世界状态。
* 字段值一律按插件声明的 ``type`` 归一化；``password`` 类型回传时
  打码，且面罩值不会被写回去。
* 插件声明的字段数、字段名、字符串长度都有硬上限 —— 一个插件不能
  用超大 payload 把配置文件撑爆。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "PanelRegistry", "Panel", "get_registry", "attach", "detach",
    "register_panel", "unregister_panel", "FieldSpec",
]

#: 单个插件最多声明多少个字段
MAX_FIELDS = 64
#: key 的合法形状：标识符，避免前端拿它拼 DOM id 时出问题
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
#: 单个字符串值的最大长度
_MAX_TEXT = 4096
#: 允许的字段类型（与前端渲染器一一对应）
FIELD_TYPES = ("text", "password", "number", "bool", "select", "textarea",
               "group", "note")
#: 远程注册的默认存活时间（秒）
DEFAULT_TTL = 900.0

MASK = "******"


# ---------------------------------------------------------------------------
# 字段声明
# ---------------------------------------------------------------------------
class FieldSpec:
    """一个设置字段的声明。

    插件作者写 ``{"key": ..., "type": ..., "label": ...}`` 这样的裸 dict
    就行；本类负责把它**清洗成安全形状**：坏字段直接丢弃而不是让它
    把整个面板带崩。
    """

    __slots__ = ("key", "type", "label", "hint", "placeholder", "options",
                 "min", "max", "default", "advanced", "readonly", "group")

    def __init__(self, raw: Dict[str, Any]):
        self.key = str(raw.get("key") or "").strip()
        ftype = str(raw.get("type") or "text").strip().lower()
        self.type = ftype if ftype in FIELD_TYPES else "text"
        self.label = str(raw.get("label") or self.key)[:120]
        self.hint = str(raw.get("hint") or "")[:400]
        self.placeholder = str(raw.get("placeholder") or "")[:200]
        self.options = self._clean_options(raw.get("options"))
        self.min = self._num(raw.get("min"))
        self.max = self._num(raw.get("max"))
        self.default = raw.get("default")
        self.advanced = bool(raw.get("advanced"))
        self.readonly = bool(raw.get("readonly"))
        self.group = str(raw.get("group") or "")[:64]

    @staticmethod
    def _clean_options(raw) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        if isinstance(raw, list):
            for o in raw:
                if isinstance(o, dict):
                    out.append({"value": str(o.get("value", ""))[:200],
                                "label": str(o.get("label", o.get("value", "")))[:200]})
                else:
                    out.append({"value": str(o)[:200], "label": str(o)[:200]})
        return out[:50]

    @staticmethod
    def _num(v) -> Optional[float]:
        try:
            if v is None or v == "":
                return None
            return float(v)
        except Exception:
            return None

    def valid(self) -> bool:
        return bool(_KEY_RE.match(self.key))

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}

    def normalize(self, value: Any) -> Tuple[bool, Any]:
        """按类型归一化一个值。返回 ``(是否接受, 归一化后的值)``。"""
        if self.readonly:
            return False, None
        if self.type == "bool":
            if isinstance(value, bool):
                return True, value
            s = str(value).strip().lower()
            return True, s in ("1", "true", "yes", "on", "是", "开")
        if self.type == "number":
            try:
                n = float(value)
            except (TypeError, ValueError):
                return False, None
            if self.min is not None and n < self.min:
                n = self.min
            if self.max is not None and n > self.max:
                n = self.max
            return True, n
        if self.type == "select":
            ok = any(o["value"] == str(value) for o in self.options) if self.options else True
            if not ok:
                return False, None
            return True, str(value)[:200]
        if self.type == "password":
            # 面罩值表示"管理员没动它"，直接丢弃由调用方保留旧值
            if str(value) == MASK:
                return False, None
            return True, str(value)[:_MAX_TEXT]
        # text / textarea
        return True, str(value)[:_MAX_TEXT]


# ---------------------------------------------------------------------------
# 面板
# ---------------------------------------------------------------------------
class Panel:
    """一个第三方插件注册进来的设置面板。"""

    __slots__ = ("pid", "title", "description", "icon", "scope", "version",
                 "fields", "loader", "saver", "remote", "registered_at",
                 "expires_at", "source", "homepage", "author")

    def __init__(self, *, pid: str, title: str, description: str = "",
                 icon: str = "🧩", scope: str = "", version: str = "",
                 fields: Optional[List[Dict[str, Any]]] = None,
                 loader: Optional[Callable[[], Dict[str, Any]]] = None,
                 saver: Optional[Callable[[Dict[str, Any]], Tuple[bool, str]]] = None,
                 remote: bool = False, ttl: float = DEFAULT_TTL,
                 source: str = "", homepage: str = "", author: str = ""):
        self.pid = str(pid or "").strip()
        self.title = str(title or pid or "未命名插件")[:120]
        self.description = str(description or "")[:400]
        self.icon = str(icon or "🧩")[:8]
        self.scope = str(scope or "")[:40]
        self.version = str(version or "")[:40]
        self.source = str(source or "")[:60]
        self.homepage = str(homepage or "")[:300]
        self.author = str(author or "")[:80]
        self.loader = loader
        self.saver = saver
        self.remote = bool(remote)
        self.registered_at = time.time()
        self.expires_at = (self.registered_at + float(ttl)) if remote else 0.0

        specs: List[FieldSpec] = []
        for raw in (fields or [])[:MAX_FIELDS]:
            if not isinstance(raw, dict):
                continue
            fs = FieldSpec(raw)
            if fs.valid():
                specs.append(fs)
        self.fields = specs

    def expired(self) -> bool:
        return bool(self.remote and self.expires_at and time.time() > self.expires_at)

    def renew(self, ttl: float = DEFAULT_TTL) -> None:
        if self.remote:
            self.expires_at = time.time() + float(ttl)

    def field_dicts(self) -> List[Dict[str, Any]]:
        return [f.to_dict() for f in self.fields]

    def public(self) -> Dict[str, Any]:
        """给前端的面板元信息（不含值）。"""
        return {
            "pid": self.pid, "title": self.title,
            "description": self.description, "icon": self.icon,
            "version": self.version, "source": self.source,
            "homepage": self.homepage, "author": self.author,
            "field_count": len(self.fields),
            "has_fields": bool(self.fields),
            "remote": self.remote,
            "registered_at": self.registered_at,
            "expires_at": self.expires_at,
        }

    def read_values(self) -> Dict[str, Any]:
        """读取当前值：按声明补默认、password 打码。"""
        stored: Dict[str, Any] = {}
        if self.loader is not None:
            try:
                got = self.loader()
                if isinstance(got, dict):
                    stored = {str(k): v for k, v in got.items()}
            except Exception:
                stored = {}
        out: Dict[str, Any] = {}
        for f in self.fields:
            if f.key in stored and stored[f.key] is not None:
                v = stored[f.key]
                out[f.key] = MASK if f.type == "password" else v
            elif f.default is not None:
                out[f.key] = f.default
            else:
                out[f.key] = (False if f.type == "bool"
                              else (0 if f.type == "number" else ""))
        return out

    def save_values(self, values: Dict[str, Any]) -> Tuple[bool, str, List[str]]:
        """保存：只接受声明过的键，逐字段归一化。

        ★ 与 mod_loader.write_mod_config 同一套策略：未声明的键直接丢，
          而不是"先收下再说"。多收一个未知键就等于给插件开了个
          任意写入口子。
        """
        if self.saver is None:
            return False, "该插件未提供保存接口", []
        by_key = {f.key: f for f in self.fields}
        payload: Dict[str, Any] = {}
        rejected: List[str] = []
        for k, v in (values or {}).items():
            f = by_key.get(str(k))
            if f is None:
                rejected.append(str(k))
                continue
            ok, nv = f.normalize(v)
            if not ok:
                continue
            payload[f.key] = nv
        try:
            res = self.saver(payload)
        except Exception as e:
            return False, f"插件保存失败：{e}", rejected
        if isinstance(res, tuple) and len(res) >= 2:
            return bool(res[0]), str(res[1] or ""), rejected
        if isinstance(res, bool):
            return res, "" if res else "插件拒绝了本次保存", rejected
        return True, "", rejected


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
class PanelRegistry:
    """所有第三方面板的持有者。线程安全。"""

    def __init__(self, data_dir: Optional[Path] = None, logger=None):
        self._lock = threading.RLock()
        self._panels: Dict[str, Panel] = {}
        self._logger = logger
        #: 保留备用：供将来做"面板布局持久化"（记住用户上次停留在哪个
        #: 面板、折叠了哪些高级项）。**当前不落盘任何面板内容** ——
        #: Python 直连的面板由插件自己在 setup() 里重新注册；
        #: 远程面板靠心跳续约，重启后由插件重新注册，都不需要持久化。
        #: 硬要存一份"上次有哪些面板"反而会引入陈旧数据的问题。
        self._data_dir = Path(data_dir) if data_dir else None
        self._listeners: List[Callable[[str, Dict[str, Any]], None]] = []
        self._seq = 0

    # ---- 订阅（供前端做实时刷新 / 供 AI 感知）----
    def subscribe(self, fn) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(fn)

        def _off():
            with self._lock:
                try:
                    self._listeners.remove(fn)
                except ValueError:
                    pass
        return _off

    def _notify(self, kind: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            fns = list(self._listeners)
        for fn in fns:
            try:
                fn(kind, payload)
            except Exception:
                pass

    # ---- 增删查 ----
    def register(self, panel: Panel) -> Tuple[bool, str]:
        if not panel.pid:
            return False, "缺少 plugin_id"
        with self._lock:
            old = self._panels.get(panel.pid)
            self._panels[panel.pid] = panel
        self._notify("registered", panel.public())
        if self._logger is not None:
            try:
                self._logger.info(
                    f"[插件面板] 已注册：{panel.title}"
                    f"（{len(panel.fields)} 个设置项"
                    f"{'，远程' if panel.remote else ''}）")
            except Exception:
                pass
        return True, ("已更新" if old else "已注册")

    def unregister(self, pid: str) -> bool:
        with self._lock:
            p = self._panels.pop(str(pid or ""), None)
        if p is None:
            return False
        self._notify("unregistered", {"pid": p.pid})
        return True

    def get(self, pid: str) -> Optional[Panel]:
        with self._lock:
            return self._panels.get(str(pid or ""))

    def list_panels(self, *, include_expired: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._panels.values())
        out = []
        for p in items:
            if p.expired() and not include_expired:
                continue
            out.append(p.public())
        out.sort(key=lambda d: (d["title"] or "").lower())
        return out

    def prune_expired(self) -> List[str]:
        """摘掉超期的远程面板。返回被摘除的 pid 列表。"""
        dead = []
        with self._lock:
            for pid, p in list(self._panels.items()):
                if p.expired():
                    dead.append(pid)
                    self._panels.pop(pid, None)
        for pid in dead:
            self._notify("unregistered", {"pid": pid})
        return dead

    def count(self) -> int:
        with self._lock:
            return len([p for p in self._panels.values() if not p.expired()])

    def purge_by_source(self, source: str) -> int:
        """摘掉某个来源（通常是某个插件实例）注册的全部面板。

        ★ 为什么需要这个：插件被卸载时，如果它只注销了自己记得的那
          几个 pid，而中途改过 id 或崩溃过，就会留下孤儿面板。按来源
          整体清理能保证"插件走了，它的面板也一定走"。
        """
        n = 0
        with self._lock:
            for pid, p in list(self._panels.items()):
                if p.source and p.source == source:
                    self._panels.pop(pid, None)
                    n += 1
        if n:
            self._notify("purged", {"source": source, "count": n})
        return n


# ---------------------------------------------------------------------------
# 进程级单例 + 便捷函数
# ---------------------------------------------------------------------------
_REGISTRY: Optional[PanelRegistry] = None
_REGISTRY_LOCK = threading.Lock()

#: 待挂载队列：主面板**尚未启用**（或正在重载）时，外部插件登记的面板
#: 先存在这里，等 ``attach()`` 建好注册表后自动补挂。
#:
#: 为什么需要它
#: ------------
#: Endstone 不保证插件加载顺序，第三方面板插件（如 satmap）常常先于
#: GreenMoon 的 ``on_enable`` 执行。此刻 ``require_plugin()`` 会抛
#: ``GmNotReady``；而如果 ``import gm.panel`` 拿到的是「重跑文件」生成
#: 的副本（``__package__='gm'``），副本里的 ``from .. import panel``
#: 还会直接越界，报 ``attempted relative import beyond top-level package``。
#: 后者已由 ``gm/_context._AliasFinder`` 从根上堵住（子模块导入一律返回
#: 内存里那一份真模块）；这里负责前者：把「注册意图」落在进程级、且
#: **不随 detach 清空**的队列里，每次 ``attach()`` 重放一遍 —— 顺序、
#: 重载、热更都不再影响结果。
_PENDING: Dict[str, Dict[str, Any]] = {}
_PENDING_LOCK = threading.Lock()


def queue_pending(**spec: Any) -> None:
    """登记一个「还挂不上」的面板，等主面板启用后自动补挂。

    以 ``plugin_id`` 为键，重复登记覆盖旧值（与 ``register_panel`` 语义一致）。
    """
    pid = str(spec.get("plugin_id") or "").strip()
    if not pid:
        return
    with _PENDING_LOCK:
        _PENDING[pid] = dict(spec)


def drop_pending(plugin_id: str) -> None:
    """摘掉待挂载队列里的一项（插件主动注销时用）。"""
    with _PENDING_LOCK:
        _PENDING.pop(str(plugin_id or "").strip(), None)


def pending_count() -> int:
    with _PENDING_LOCK:
        return len(_PENDING)


def _flush_pending(logger: Any = None) -> int:
    """把待挂载队列重放进当前注册表；返回成功条数。"""
    with _PENDING_LOCK:
        specs = list(_PENDING.values())
    n = 0
    for spec in specs:
        try:
            ok, _msg = register_panel(**spec)
            if ok:
                n += 1
        except Exception as e:  # 单条失败不影响其它面板
            if logger is not None:
                try:
                    logger.warning(f"补挂第三方面板失败（{spec.get('plugin_id')}）：{e}")
                except Exception:
                    pass
    return n


def get_registry() -> PanelRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = PanelRegistry()
    return _REGISTRY


def attach(data_dir=None, logger=None) -> PanelRegistry:
    """由主插件在 on_enable 里调用，给注册表接上持久化与日志。"""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = PanelRegistry(data_dir=data_dir, logger=logger)
        reg = _REGISTRY
    # 主面板启用前，外部插件可能已经登记（见 queue_pending）。现在补挂上：
    # 这样「插件先于 GreenMoon 启用」也不丢面板，重载后也会自动重新出现。
    try:
        n = _flush_pending(logger)
        if n and logger is not None:
            try:
                logger.info(f"已补挂 {n} 个第三方面板")
            except Exception:
                pass
    except Exception:
        pass
    return reg


def detach() -> None:
    """插件卸载时释放单例，避免下一个运行期串到旧实例。"""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


def register_panel(*, plugin_id: str, title: str, description: str = "",
                   icon: str = "🧩", version: str = "", fields=None,
                   loader=None, saver=None, source: str = "",
                   homepage: str = "", author: str = "") -> Tuple[bool, str]:
    """第三方插件注册自己的设置面板（最常用入口）。

    典型用法::

        from admin_web.panel import register_panel

        def setup(plugin):
            register_panel(
                plugin_id="my_prefix",
                title="我的前缀插件",
                fields=[{"key": "prefix", "type": "text",
                         "label": "聊天前缀", "default": "[玩家]"}],
                loader=_load,      # 返回当前值 dict
                saver=_save,       # (values) -> (ok, msg)
            )
    """
    p = Panel(pid=plugin_id, title=title, description=description, icon=icon,
              version=version, fields=fields, loader=loader, saver=saver,
              remote=False, source=source, homepage=homepage, author=author)
    return get_registry().register(p)


def unregister_panel(plugin_id: str) -> bool:
    drop_pending(plugin_id)
    return get_registry().unregister(plugin_id)
