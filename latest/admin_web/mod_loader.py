"""GreenMoon 子插件加载器。

包类型（manifest.json 的 ``type`` 字段）
--------------------------------------
``index``
    官网首页子插件。启用后 ``/`` 返回该包内的 ``index.html``，
    包内 **全部** 文件（js / css / 图片 / 子页面 / 子目录）通过 ``/gmpage/index/<路径>`` 可达。
``admin``
    管理页子插件。启用后 ``/admin`` 返回该包内的 ``index.html``，
    包内文件通过 ``/gmpage/admin/<路径>`` 可达。
``py``
    可执行 Python 代码型子插件。启用后自动定位包内 ``main/main.py`` 并执行；
    入口里可以 ``from gm import ...`` 拿到面板提供的一整套 API
    （服务器 / 玩家 / 机器人 / 绑定 / 跨服 / 面板状态 / 事件注册）。
    支持 ``setup()`` / ``teardown()`` 生命周期钩子。
    可声明 ``permissions`` 申请面板数据权限。
``code``（或不写 type）
    无页面接管的代码型子插件，保持原有行为。

目录约定（均在插件数据目录下）::

    <data_dir>/
      libs/                  .gmlib 解出的 .whl（与 QQ 离线依赖共用）
      mod/
        liblist.json         已登记的依赖清单
        gmod.json            页面接管型子插件的启用状态
        index/<uuid>/        官网子插件（已解包）
        index/<uuid>.gmmod   原始包留档
        admin/<uuid>/        管理页子插件（已解包）
        admin/<uuid>.gmmod
        code/<uuid>/         py 型子插件（已解包，含 main/main.py）
        code/<uuid>.gmmod
        <uuid>/              代码型子插件（旧结构，保持不变）

设计要点
--------
* 纯标准库，不引入第三方依赖。
* 静态资源由 web 层 **流式** 发送并按扩展名给 MIME，支持 Range（音视频可拖进度）。
* 路径一律以插件数据目录为基准，解压与静态请求都做 Zip-Slip / 目录穿越校验。
* 同一 ``type`` 同时只允许一个子插件处于启用状态（``py`` 除外，可多个并存），
  重复启用自动停用前一个。
* 子插件抛出的异常一律隔离，只记日志，不影响主插件。
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .atomic_io import atomic_write_json, atomic_write_text

__all__ = [
    "ModLoader",
    "ModLoadError",
    "GMMOD_REQUIRED",
    "GMLIB_REQUIRED",
    "GMMOD_SUFFIX",
    "GMLIB_SUFFIX",
    "PAGE_TYPES",
    "PY_TYPE",
    "CODE_TYPES",
    "ALL_TYPES",
    "DEFAULT_PY_ENTRY",
    "KNOWN_PERMISSIONS",
    "MOD_PREFIX",
    "SETTINGS_SCHEMA_NAME",
    "MOD_CONFIG_NAME",
    "SETTINGS_FIELD_TYPES",
    "SETTINGS_MASK",
    "mime_for",
]

GMMOD_SUFFIX = ".gmmod"
GMLIB_SUFFIX = ".gmlib"

#: 子插件入口模块的包名前缀。``gm`` 包靠它识别"当前调用者是哪个子插件"，
#: 改动会导致权限判定与身份识别全部失效。
MOD_PREFIX = "greenmoon_mods."

#: 可接管页面的 type 取值
PAGE_TYPES = ("index", "admin")

#: 会自动执行 Python 代码的 type 取值
PY_TYPE = "py"
CODE_TYPES = ("code", "py")

#: 未声明 type 时的默认值
DEFAULT_TYPE = "code"

#: 全部合法的 type 取值
ALL_TYPES = PAGE_TYPES + CODE_TYPES

#: 根目录扫描时要跳过的目录名（这些是按 type 分的容器目录，不是 uuid）
ALL_TYPE_DIRS = frozenset(ALL_TYPES)

#: py 型子插件的默认入口（位于包内 main 子目录）
DEFAULT_PY_ENTRY = "main/main.py"

#: py 型子插件可声明的面板权限。``*`` 表示全部。
KNOWN_PERMISSIONS = (
    "players", "logs", "console", "detail", "bots", "bindings",
    "cross", "mods", "backup", "cloud", "gamerules", "worlds",
    "perm", "scoreboard", "gametools", "files", "diag", "account",
    # 读写**自己包内**的 config.json（gm.config）。注意它不含「读别人的」——
    # gm.config 的 uuid 只能由 current_owner() 反查，不接受调用方传入。
    "config",
)

#: 子插件包内**声明**设置字段的文件（随包分发，只读，定义"有哪些设置项"）。
SETTINGS_SCHEMA_NAME = "settings.json"

#: 子插件包内**存值**的文件（面板与子插件都可写，存"这些设置项现在是什么值"）。
MOD_CONFIG_NAME = "config.json"

#: settings.json 的字段数量上限 —— 防呆，避免某个包塞进几千个字段把面板卡死。
MAX_SETTINGS_FIELDS = 64

#: 设置字段支持的控件类型（``group`` 是纯展示分区标题，不存值）。
SETTINGS_FIELD_TYPES = (
    "text", "password", "number", "bool", "select", "textarea", "group",
)

#: 字段 key 的合法形态：它要同时当 JSON 键和 HTML id，限制得严一点。
#: 不允许点（会和 config 的层级语义打架）、不允许连字符（在 JS 里做 id 拼接易踩坑）。
_SETTINGS_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

#: 文本类字段的值长度上限（字符）
MAX_SETTINGS_TEXT_LEN = 4096

#: 密码字段在接口回传时的面罩串。前端看到它就**不回写**，保留原值。
SETTINGS_MASK = "******"

GMMOD_REQUIRED = ["name", "uuid", "version", "min_support_version", "description"]
GMLIB_REQUIRED = ["name", "python_version", "platform", "liblist"]

GMMOD_OPTIONAL = {
    "type": DEFAULT_TYPE,
    "author": "",
    "entry": "main.py",
    "index": "index.html",
    "libs": [],
    "homepage": "",
    "main": DEFAULT_PY_ENTRY,
    "permissions": [],
}

MAX_UNPACK_BYTES = 256 * 1024 * 1024
MAX_UNPACK_FILES = 4096

#: 允许在线读写（纯文本）的扩展名
TEXT_EXTS = {
    ".html", ".htm", ".js", ".mjs", ".css", ".json", ".txt", ".md",
    ".svg", ".xml", ".csv", ".yml", ".yaml", ".map",
}

_UUID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: 扩展名 -> Content-Type 覆盖表（mimetypes 在不同系统上对 js/css 的判断不一致）
_MIME_OVERRIDES = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".xml": "application/xml; charset=utf-8",
    ".wasm": "application/wasm",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".ico": "image/x-icon",
}


class ModLoadError(Exception):
    """子插件 / 依赖库处理失败。"""


def mime_for(path: Path) -> str:
    """按扩展名推断 Content-Type。"""
    ext = path.suffix.lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    guess, _ = mimetypes.guess_type(str(path))
    return guess or "application/octet-stream"


#: 已分配的模块名 → uuid。用于检测「两个不同 uuid 归一后重名」。
#: 归一化会把 ``-`` ``.`` ``/`` ``空格`` 全变成 ``_``，于是
#: ``ab-cd`` 与 ``ab.cd`` 会撞成同一个 ``greenmoon_mods.ab_cd``。
#: 撞名的后果不只是卸载 A 会连带毁掉 B —— ``gm`` 包靠这个模块名
#: 沿调用栈**反查调用者身份来判权限**，撞名会导致 A 用上 B 的权限。
_MODULE_NAME_OWNER: dict[str, str] = {}
_MODULE_NAME_LOCK = threading.Lock()


def _module_name(uuid: str) -> str:
    """把 uuid 归一成合法的 ``greenmoon_mods.<uuid>`` 模块名。

    ``greenmoon_mods.`` 前缀不是随便起的 —— ``gm`` 包就是靠这个前缀
    沿调用栈反查"当前是谁在调我"，从而判定子插件身份与权限。

    归一化后若与**别的 uuid** 重名，追加内容哈希后缀区分，保证
    一个 uuid 永远对应一个（且只对应一个）模块名。后缀只取决于
    uuid 本身，所以同一 uuid 每次调用结果一致，``sys.modules``
    的键是稳定的。
    """
    raw = str(uuid)
    safe = re.sub(r"[^0-9A-Za-z_]", "_", raw)
    name = f"greenmoon_mods.{safe}"
    with _MODULE_NAME_LOCK:
        owner = _MODULE_NAME_OWNER.get(name)
        if owner is None or owner == raw:
            _MODULE_NAME_OWNER[name] = raw
            return name
        # 撞名：加短哈希，并把映射指向当前 uuid
        suffix = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        uniq = f"{name}_{suffix}"
        _MODULE_NAME_OWNER.setdefault(uniq, raw)
        return uniq


def _invoke_hook(fn: Any, *candidates: Any) -> Any:
    """按签名自适应调用生命周期钩子。

    只传它声明需要的参数，因此 ``setup(plugin)``（旧写法）与
    ``setup(plugin, gm)``（新写法）都能正常工作。
    """
    try:
        n = len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        n = 0
    return fn(*candidates[:n])


def _gm_module() -> Any:
    """延迟取 gm 包，避免加载器与 gm 相互 import 形成环。"""
    try:
        from . import gm

        return gm
    except Exception:
        return None


def _norm_pkg(name: str) -> str:
    """PEP 503 包名归一化。"""
    return str(name or "").strip().lower().replace("_", "-")


def _whl_pkg(filename: str) -> str:
    """从 wheel 文件名取包名（wheel 规范里 name 段不含连字符）。"""
    stem = os.path.basename(str(filename or ""))
    if stem.lower().endswith(".whl"):
        stem = stem[:-4]
    return _norm_pkg(stem.split("-")[0])


def _version_tuple(v: Any) -> Tuple[int, ...]:
    """把 ``4.1.0`` / ``4.1.0-beta`` 转成可比较的整数元组。"""
    parts: List[int] = []
    for chunk in str(v or "").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    """安全解压，返回解出的条目数。"""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)

    members = zf.infolist()
    if len(members) > MAX_UNPACK_FILES:
        raise ModLoadError(f"归档条目过多（{len(members)} > {MAX_UNPACK_FILES}）")

    total = 0
    for info in members:
        total += int(info.file_size or 0)
        if total > MAX_UNPACK_BYTES:
            raise ModLoadError(f"解压后体积超限（> {MAX_UNPACK_BYTES // 1024 // 1024} MB）")

    prefix = str(dest)
    for info in members:
        name = (info.filename or "").replace("\\", "/")
        if not name or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
            raise ModLoadError(f"归档成员路径非法: {info.filename!r}")
        if name.startswith("../") or "/../" in name or name.endswith("/.."):
            raise ModLoadError(f"归档成员存在目录穿越: {info.filename!r}")
        target = (dest / name).resolve()
        tstr = str(target)
        if tstr != prefix and not tstr.startswith(prefix + os.sep):
            raise ModLoadError(f"归档成员越出目标目录: {info.filename!r}")

    zf.extractall(dest)
    return len(members)


class ModLoader(object):
    """子插件导入器 + 页面接管管理。"""

    def __init__(self, plugin: Any, data_dir: Optional[Path] = None) -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "logger", None)
        base = Path(data_dir) if data_dir else Path(getattr(plugin, "data_dir", "."))

        self.data_dir = base
        self.libs_dir = Path(getattr(plugin, "libs_dir", None) or (base / "libs"))
        self.mods_dir = base / "mod"
        self.liblist_path = self.mods_dir / "liblist.json"
        self.state_path = self.mods_dir / "gmod.json"

        self.libs_dir.mkdir(parents=True, exist_ok=True)
        self.mods_dir.mkdir(parents=True, exist_ok=True)

        self._installed: Dict[str, Dict[str, Any]] = {}
        self._loaded: Dict[str, str] = {}
        self._module_names: List[str] = []
        #: installed() 的缓存：uuid -> 记录（**只含磁盘派生的字段**），
        #: 以及缓存生成时的 mods_dir mtime。见 installed() 的注释 ——
        #: 权限校验路径上会高频调用它。
        #:
        #: ⚠ 缓存里**不能**存 enabled / running 这类运行时字段。
        #: 历史 bug（已修）：这两个字段原先在 list_mods() 里算完就一起被
        #: 缓存了，而启停只改内存里的 _state / _loaded，**不动 mtime**，
        #: 于是缓存永不失效 —— disable 之后 enabled 还是 True。
        #: 现在改为在 installed() 返回前**即时叠加**（见 _with_runtime）。
        self._installed_cache_data: Optional[Dict[str, Dict[str, Any]]] = None
        self._installed_cache_stamp: Any = None
        #: uuid -> 生命周期钩子 {"setup": fn|None, "teardown": fn|None}
        self._lifecycles: Dict[str, Dict[str, Any]] = {}
        #: uuid -> 该子插件 exec 期间新增的模块名（热重载时清理用）
        self._owned_modules: Dict[str, List[str]] = {}
        #: 保护 _state 的读改写 + 落盘。启用/停用既可能来自 Web 线程
        #: （/api/gmods/enable 等），也可能来自服务器主线程（/gmback 指令），
        #: 没有锁时两个并发写会互相覆盖，导致某个变更只在内存生效没写进
        #: gmod.json，重启后状态对不上。
        #: 用 RLock 而非 Lock：_save_state() 自己也会拿这把锁，而它经常在
        #: 「持锁改完状态后立刻落盘」的路径里被调用，普通 Lock 会自死锁。
        self._state_lock = threading.RLock()
        self._state: Dict[str, Any] = self._load_state()

    # ------------------------------------------------------------------ 日志

    def _log(self, level: str, msg: str) -> None:
        text = f"[子插件] {msg}"
        try:
            getattr(self.logger, level)(text)
        except Exception:
            if level in ("error", "warning"):
                print(text)

    # ----------------------------------------------------------- 启用状态

    def _load_state(self) -> Dict[str, Any]:
        st: Dict[str, Any] = {"index": None, "admin": None, "code": {}}
        try:
            if self.state_path.is_file():
                with open(self.state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k in PAGE_TYPES:
                        v = data.get(k)
                        st[k] = str(v) if v else None
                    code = data.get("code")
                    st["code"] = dict(code) if isinstance(code, dict) else {}
        except Exception as e:
            self._log("warning", f"读取子插件状态失败（{e}），按全部停用处理")
        return st

    def _save_state(self) -> None:
        with self._state_lock:
            # ★ 走 atomic_write_json，不再自己手搓 ``.json.tmp``。
            #
            # 旧写法把临时文件名**写死**成 ``<state>.json.tmp``：
            #     tmp = self.state_path.with_suffix(".json.tmp")
            #     open(tmp, "w") ... os.replace(tmp, self.state_path)
            #
            # 两个进程/线程同时对同一个状态文件写（例如面板保存 +
            # 子插件自启停），会**共用同一个临时文件**：
            #   A 打开 tmp 写一半 → B 打开同一个 tmp 截断重写 →
            #   A 执行 os.replace 把 B 的半成品搬成正式文件 →
            #   B 再 replace 时 tmp 已经不存在，抛 FileNotFoundError。
            # 结果轻则状态丢失，重则状态文件变成半截 JSON。
            #
            # atomic_write_bytes 用的是 tempfile.mkstemp()，每次随机名，
            # 从根本上没有「两个写者抢同一个临时文件」的可能。
            if not atomic_write_json(self.state_path, self._state, indent=2):
                self._log("warning", "保存子插件状态失败（写入未成功）")
        # 状态落盘后，installed() 缓存里被一起存下的 enabled/running
        # 立刻变成旧值，这里补一刀。
        #
        # ⚠ 为什么不能只靠 mtime 校验：gmod.json 就住在 mods_dir 里，
        # 写它会顺带把 mods_dir 自身的 mtime 顶掉。也就是说「状态变更」
        # 在磁盘层面**看起来像**目录内容变了 —— 缓存确实会被重建。
        # 但那只是因为文件恰好同处一个目录，纯属巧合：一旦 mods_dir
        # 被挪走、或状态存到别处，mtime 校验就彻底失灵，而
        # enabled/running 又已经被缓存进去，启停状态立刻变成僵尸值。
        # 所以这里显式失效，不依赖那个巧合。
        self.invalidate_cache()

    def enabled_uuid(self, ptype: str) -> Optional[str]:
        with self._state_lock:
            return self._state.get(ptype) or None

    def active_page(self, ptype: str) -> Optional[Tuple[Path, str]]:
        """返回当前启用的页面子插件的 ``(目录, 入口文件)``；未启用返回 None。"""
        if ptype not in PAGE_TYPES:
            return None
        uid = self.enabled_uuid(ptype)
        if not uid:
            return None
        rec = self._find(uid, ptype)
        if not rec:
            return None
        root = Path(rec.get("path") or self._dir_of(ptype, uid))
        return root, str(rec.get("index") or GMMOD_OPTIONAL["index"])

    # -------------------------------------------------------------- 目录

    def _dir_of(self, ptype: str, uuid: str) -> Path:
        """子插件落盘目录。

        页面型（index/admin）与 py 型各按 type 分目录；
        code 型保持旧的 ``mod/<uuid>`` 结构不变，以兼容既有子插件。
        """
        if ptype in PAGE_TYPES:
            return self.mods_dir / ptype / uuid
        if ptype == PY_TYPE:
            return self.mods_dir / "code" / uuid
        return self.mods_dir / uuid

    def _find(self, uuid: str, ptype: Optional[str] = None) -> Optional[Dict[str, Any]]:
        rec = self._installed.get(uuid)
        if rec is not None and (ptype is None or rec.get("type") == ptype):
            return rec
        for m in self.list_mods():
            if str(m.get("uuid")) == uuid and (ptype is None or m.get("type") == ptype):
                return m
        return None

    @staticmethod
    def _read_mod(d: Path) -> Optional[Dict[str, Any]]:
        for fname in (".gm_installed.json", "manifest.json"):
            f = d / fname
            if not f.is_file():
                continue
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
                if isinstance(rec, dict):
                    rec.setdefault("uuid", d.name)
                    rec.setdefault("type", DEFAULT_TYPE)
                    rec.setdefault("index", GMMOD_OPTIONAL["index"])
                    # py 型入口在 main/ 子目录，与 code 型的 main.py 不同
                    if str(rec.get("type")) == PY_TYPE:
                        rec.setdefault("entry", DEFAULT_PY_ENTRY)
                    else:
                        rec.setdefault("entry", GMMOD_OPTIONAL["entry"])
                    rec["path"] = str(d)
                    return rec
            except Exception:
                continue
        return None

    # ------------------------------------------------------------- 清点

    def list_mods(self) -> List[Dict[str, Any]]:
        """扫描 mod 目录，返回全部已安装子插件（不执行任何代码）。

        返回的每条记录都带**即时计算**的 ``enabled`` / ``running``。
        注意它们是由 ``_with_runtime()`` 叠加的：本函数的返回值会被
        塞进 ``_installed_cache()``，而 ``installed()`` 也会再叠加一次。
        两处都叠是为了让"直接调 list_mods()"和"走缓存"两条路径给出
        完全一致的字段 —— 叠加是**幂等且零成本**的（两次 dict 查找）。
        """
        mods: List[Dict[str, Any]] = []

        # 1) 按 type 分目录的：index/<uuid>/、admin/<uuid>/、code/<uuid>/
        #    注意 code/ 目录里同时住着 code 型与 py 型，以各自 record 里的
        #    type 字段为准，不能把目录名当成 type 覆盖掉。
        type_dirs = dict.fromkeys(PAGE_TYPES + CODE_TYPES)
        for dirname in type_dirs:
            pdir = self.mods_dir / dirname
            if not pdir.is_dir():
                continue
            try:
                entries = sorted(p for p in pdir.iterdir() if p.is_dir())
            except Exception:
                continue
            for d in entries:
                if d.name.startswith("."):
                    continue
                rec = self._read_mod(d)
                if rec:
                    mods.append(rec)

        # 2) 根目录下的：code 型的旧结构 mod/<uuid>/
        try:
            entries = sorted(p for p in self.mods_dir.iterdir() if p.is_dir())
        except Exception:
            entries = []
        for d in entries:
            if d.name.startswith(".") or d.name in ALL_TYPE_DIRS:
                continue
            rec = self._read_mod(d)
            if rec:
                mods.append(rec)

        seen = set()
        out: List[Dict[str, Any]] = []
        for m in mods:
            uid = str(m.get("uuid"))
            if uid in seen:
                continue
            seen.add(uid)
            m["missing_libs"] = self._missing_libs(m.get("libs") or [])
            m.setdefault("permissions", [])
            # ⚠ 必须叠加运行时字段：本函数的返回值会被塞进
            # _installed_cache()，而那个缓存的失效键是 mods_dir 的
            # mtime —— 启停不改 mtime。若这里返回裸记录，缓存里就
            # 没有 enabled，installed() 事后补算也补不出正确值。
            out.append(self._with_runtime(m))
        return out

    def _with_runtime(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """给磁盘记录**即时叠加**运行时字段（enabled / running）。

        为什么要单独一步：这两个字段取决于内存里的 ``self._state`` 与
        ``self._loaded``，而 ``installed()`` 的缓存键是 **mods_dir 的
        mtime**。启停一个子插件不会改 mtime，所以只要把运行时字段
        一起缓存，缓存就**永远不会因启停而失效**：disable 完再查还是
        ``enabled=True``；``enabled`` 为假的插件在权限上也会被摘掉。

        所以这里每次调用都重算，成本只是两次 dict 查找 —— 相对
        ``list_mods()`` 的扫盘 + 读 json，可以忽略。
        """
        m = dict(rec)
        uid = str(m.get("uuid") or "")
        if m.get("type") in PAGE_TYPES:
            m["enabled"] = self._state.get(m.get("type")) == uid
        else:
            m["enabled"] = bool((self._state.get("code") or {}).get(uid))
        m["running"] = uid in self._loaded
        return m

    def installed(self, uuid: str) -> Optional[Dict[str, Any]]:
        """按 uuid 取子插件记录（含即时计算的 ``enabled`` / ``running``）。

        注意调用频率：``gm`` 层的**每次权限校验**都会走到这里
        （``_context._permissions_of`` → ``installed``）。而
        ``list_mods()`` 要遍历全部 mod 目录并逐个读 manifest.json ——
        子插件高频调 gm API 时会反复扫盘，若发生在主线程 tick 中就卡服。
        因此这里做一层带失效标记的缓存。

        任何会改变**已安装集合**（也就是磁盘内容）的操作（装/卸）都应
        调用 ``invalidate_cache()``；为稳妥起见这里再叠一个 mtime 校验：
        mods_dir 的修改时间变了就自动失效，即使某处漏调也不会读到陈旧数据。

        ⚠ 但 **启用/停用不会改 mtime** —— 它们只动内存状态。所以缓存里
        只允许存磁盘派生的字段，``enabled`` / ``running`` 一律在返回前
        用 ``_with_runtime()`` 现算（历史 bug 见该函数 docstring）。
        """
        table = self._installed_cache()
        if table is None:
            return None
        rec = table.get(str(uuid))
        return None if rec is None else self._with_runtime(rec)

    def _installed_cache_stamp_now(self):
        """算出"磁盘上装了什么"的指纹，供缓存比对。

        ⚠⚠ **绝对不能用 mtime 当指纹。**
        -----------------------------------------------------------
        踩过两次坑，第二次是这个函数存在的直接原因：

        1) 第一版指纹只取 ``mods_dir`` 自己的 mtime。但子插件实际住在
           ``mods_dir/<type>/<uuid>/``（code / index / admin）里，往
           ``code/`` 里新建一个子插件目录，动的是 ``code/`` 的 mtime，
           ``mods_dir`` 纹丝不动 → 指纹不变 → 返回旧表。

        2) 改成"取 mods_dir + 各 type 子目录的 mtime"之后，**仍然不对**。
           实测（Linux/ext4）在 ``code/`` 里 ``mkdir`` 一个子目录，
           ``code/`` 自己的 mtime **压根不变**：

               初始            mtime_ns=1789478427853778927
               建 code/m1      mtime_ns=1789478427853778927   ← 没动
               建 code/m2      mtime_ns=1789478427853778927   ← 没动

           也就是说"目录里多了个子目录"这件事，在这个文件系统上从目录
           mtime 里**根本读不出来**。靠 mtime 做失效判断，无论取几层都是
           赌文件系统行为，迟早再翻一次车。

        所以指纹改成**真实的目录清单**：每层目录下有哪些条目（名字）。
        名字列表变了 → 指纹必然变；没变 → 内容结构确实没变。这不再依赖
        任何文件系统的时间戳语义，代价是多几次 ``iterdir()`` —— 相对
        ``list_mods()`` 要逐个读 manifest.json + 解析 JSON，可以忽略
        （浅目录列举远比逐个读文件快）。

        注意只需要看**目录**：子插件的增删必然表现为目录的增删。
        manifest.json 内容被改（比如换了名字）不影响这里 —— 那种情况
        需要显式 ``invalidate_cache()``，面板里改文件走的就是那条路。
        """
        parts = []
        for d in (self.mods_dir,) + tuple(self.mods_dir / t for t in ALL_TYPE_DIRS):
            try:
                if d.is_dir():
                    # 只取目录项的名字，排序后作为集合指纹
                    names = sorted(
                        p.name for p in d.iterdir()
                        if p.is_dir() and not p.name.startswith(".")
                    )
                    parts.append((str(d), tuple(names)))
                else:
                    parts.append((str(d), None))
            except Exception:
                parts.append((str(d), "err"))
        return tuple(parts)

    def _installed_cache(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """返回 uuid → 记录（磁盘派生字段）的缓存表；需要重建时重建。"""
        stamp = self._installed_cache_stamp_now()
        if self._installed_cache_data is not None and self._installed_cache_stamp == stamp:
            return self._installed_cache_data
        try:
            mods = self.list_mods()
        except Exception:
            return None
        table: Dict[str, Dict[str, Any]] = {}
        for m in mods:
            key = str(m.get("uuid") or "")
            if key:
                table[key] = m
        self._installed_cache_data = table
        self._installed_cache_stamp = stamp
        return table

    def invalidate_cache(self) -> None:
        """主动让 installed() 缓存失效（装载/卸载/启停后调用）。"""
        self._installed_cache_data = None
        self._installed_cache_stamp = None

    # ------------------------------------------------------ 依赖与 sys.path

    def load_liblist(self) -> List[str]:
        if not self.liblist_path.exists():
            return []
        try:
            with open(self.liblist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(x) for x in data if isinstance(x, str)]
            self._log("warning", "liblist.json 格式错误，重置为空列表")
        except Exception as e:
            self._log("warning", f"读取 liblist.json 失败（{e}），按空列表处理")
        return []

    def save_liblist(self, liblist: List[str]) -> None:
        # 同 _save_state：手搓固定名 ``.json.tmp`` 会被并发写者抢用，
        # 统一走 atomic_write_json（内部用 mkstemp 生成随机临时名）。
        atomic_write_json(self.liblist_path, sorted(set(liblist)), indent=2)

    def installed_packages(self) -> Dict[str, str]:
        return {_whl_pkg(n): n for n in self.load_liblist()}

    def sync_libs_path(self) -> int:
        """把 ``libs/`` 下的依赖注册进 ``sys.path``（供子插件导入）。

        ⚠ 委托给 ``libs_loader``，**不再自己挂 .whl 路径**。

        历史坑（已修）：这里原先直接对每个 ``*.whl`` 做
        ``sys.path.insert(0, whl)``。两个后果都很严重：

        1. **把解压出来的包顶掉。** ``libs_loader`` 的解压目录是
           ``append`` 到末尾的，而 ``insert(0, whl)`` 插到了最前。
           于是 ``import PIL`` 被 zipimport 抢先命中 wheel 内部那份
           （没有 ``.so``）→ ``_imaging`` 加载失败 → Pillow 变空壳，
           地图画整条功能静默失效。
        2. **把解压机制整个绕过去。** 这里只 ``glob("*.whl")``，
           根本不认 ``_unpacked/`` 目录 —— 等于每次调它都要把
           libs_loader 的成果往回退一步。

        而 ``bootstrap()`` 会调本函数，``on_enable`` 的顺序又是
        ``libs_loader.install()`` → ``ModLoader.bootstrap()``，
        所以这个 bug 必然在生产路径上被触发（不是理论风险）。

        返回新增到 sys.path 的路径数（保持原有返回值语义）。
        """
        added = 0
        try:
            from . import libs_loader

            got = libs_loader.install(self.libs_dir, self.logger)
            added = len(got or [])
        except Exception as e:
            self._log("warning", f"注册依赖目录失败 {self.libs_dir}: {e}")
            # 兜底：至少把 libs 目录本身挂上，让纯 Python 的 .py 可导入。
            # 仍然**不**挂 .whl —— 那正是上面说的坑。
            try:
                p = str(self.libs_dir)
                if p not in sys.path:
                    sys.path.append(p)
                    added = 1
            except Exception:
                pass
        return added

    def unsync_libs_path(self) -> None:
        """摘掉本加载器注册过的依赖路径（插件停用时调用）。

        覆盖两类条目：
          * ``libs/`` 下的 ``*.whl``（旧实现可能留下）
          * ``libs/_unpacked/<包名>`` 解压目录，以及 ``libs`` 目录本身
        """
        root = str(self.libs_dir)
        unpacked_marker = os.path.join(root, "_unpacked")

        def _keep(p: str) -> bool:
            if not isinstance(p, str):
                return True
            # 旧的 whl 直挂形式
            if p.endswith(".whl") and p.startswith(root):
                return False
            # libs 目录本身 / 解压目录
            if p == root or p == unpacked_marker:
                return False
            if p.startswith(unpacked_marker + os.sep):
                return False
            return True

        try:
            sys.path[:] = [p for p in sys.path if _keep(p)]
        except Exception:
            pass

    def _missing_libs(self, libs: List[Any]) -> List[str]:
        known = self.installed_packages()
        return [str(l) for l in (libs or []) if _norm_pkg(str(l)) not in known]

    # -------------------------------------------------------------- 安装

    def install(self, path: str, force: bool = False) -> Dict[str, Any]:
        """导入一个 .gmmod 或 .gmlib。返回 ``{ok, kind, ...}``。"""
        # 装完会新增目录，先让 installed() 缓存作废
        self.invalidate_cache()
        src = Path(str(path or "")).expanduser()
        if not src.is_file():
            return self._fail(f"文件不存在: {src}")
        ext = src.suffix.lower()
        if ext not in (GMMOD_SUFFIX, GMLIB_SUFFIX):
            return self._fail(f"不支持的文件类型，请使用 {GMMOD_SUFFIX} 或 {GMLIB_SUFFIX}")
        if not zipfile.is_zipfile(src):
            return self._fail(f"不是有效的 ZIP 归档: {src.name}")
        try:
            if ext == GMLIB_SUFFIX:
                return self._install_gmlib(src, force=force)
            return self._install_gmmod(src, force=force)
        except ModLoadError as e:
            self._log("error", str(e))
            return self._fail(str(e))
        except Exception as e:
            self._log("error", f"导入 {src.name} 失败: {e}")
            return self._fail(f"{type(e).__name__}: {e}")

    @staticmethod
    def _fail(msg: str) -> Dict[str, Any]:
        return {"ok": False, "error": msg}

    @staticmethod
    def _read_manifest(path: Path) -> Dict[str, Any]:
        if not path.is_file():
            raise ModLoadError("归档内缺少 manifest.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise ModLoadError(f"manifest.json 解析失败: {e}")
        if not isinstance(data, dict):
            raise ModLoadError("manifest.json 必须是对象")
        return data

    @staticmethod
    def _validate(manifest: Dict[str, Any], required: List[str], kind: str) -> None:
        missing = [f for f in required if f not in manifest]
        if missing:
            raise ModLoadError(f"{kind} manifest 缺少字段: {', '.join(missing)}")

    # ------------------------------------------------------------- .gmlib

    def _install_gmlib(self, src: Path, force: bool = False) -> Dict[str, Any]:
        temp_dir = Path(tempfile.mkdtemp(prefix="gmlib_"))
        try:
            with zipfile.ZipFile(src, "r") as zf:
                _safe_extract(zf, temp_dir)

            manifest = self._read_manifest(temp_dir / "manifest.json")
            self._validate(manifest, GMLIB_REQUIRED, "gmlib")

            pkgs = manifest.get("liblist") or []
            if not isinstance(pkgs, list):
                raise ModLoadError("liblist 字段必须为列表")
            self._check_env(manifest, force=force)

            known = self.installed_packages()
            copied, skipped, missing = [], [], []
            for pkg in pkgs:
                whl = self._find_whl(temp_dir, str(pkg))
                if whl is None:
                    missing.append(str(pkg))
                    self._log("error", f"包 '{pkg}' 在归档内找不到对应 .whl")
                    continue
                name = os.path.basename(whl)
                key = _whl_pkg(name)
                if key in known:
                    skipped.append(name)
                    if known[key] != name:
                        self._log("warning", f"{key} 已安装为 {known[key]}，跳过 {name}（同包多版本会冲突）")
                    else:
                        self._log("info", f"{name} 已登记，跳过")
                    continue
                dest = self.libs_dir / name
                if dest.exists() and not force:
                    known[key] = name
                    skipped.append(name)
                    self._log("info", f"{name} 已存在，登记并跳过复制")
                    continue
                shutil.copy2(whl, dest)
                known[key] = name
                copied.append(name)
                self._log("info", f"依赖入库 {name}")

            self.save_liblist(list(known.values()))
            self.sync_libs_path()

            ok = not missing
            result = {"ok": ok, "kind": "gmlib", "name": manifest.get("name"),
                      "copied": copied, "skipped": skipped, "missing": missing}
            if ok:
                self._log("info", f"依赖库 {manifest.get('name')} 导入完成")
            else:
                result["error"] = f"缺少依赖包: {', '.join(missing)}"
            return result
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _find_whl(self, search_dir: Path, pkg_name: str) -> Optional[str]:
        want = _norm_pkg(pkg_name)
        for root, _dirs, files in os.walk(str(search_dir)):
            for f in files:
                if f.lower().endswith(".whl") and _whl_pkg(f) == want:
                    return os.path.join(root, f)
        return None

    def _check_env(self, manifest: Dict[str, Any], force: bool = False) -> None:
        want_py = str(manifest.get("python_version") or "").strip()
        if want_py and want_py.lower() not in ("any", "all", "*"):
            if _version_tuple(want_py)[:2] != _version_tuple("%d.%d" % sys.version_info[:2])[:2]:
                msg = f"Python 版本不符：需要 {want_py}，当前 {sys.version.split()[0]}"
                if not force:
                    raise ModLoadError(msg + "（可用 force=True 强制导入）")
                self._log("warning", msg + "，已强制导入")

        want_plat = str(manifest.get("platform") or "").strip().lower()
        if want_plat and want_plat not in ("any", "all", "*"):
            cur = sys.platform.lower()
            tags = [t for t in re.split(r"[,\s|/]+", want_plat) if t]
            if tags and not any(cur.startswith(t) for t in tags):
                msg = f"平台不符：需要 {want_plat}，当前 {cur}"
                if not force:
                    raise ModLoadError(msg + "（可用 force=True 强制导入）")
                self._log("warning", msg + "，已强制导入")

    # ------------------------------------------------------------- .gmmod

    def _install_gmmod(self, src: Path, force: bool = False) -> Dict[str, Any]:
        with zipfile.ZipFile(src, "r") as zf:
            if "manifest.json" not in zf.namelist():
                raise ModLoadError("归档中缺少 manifest.json")
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

        if not isinstance(manifest, dict):
            raise ModLoadError("manifest.json 必须是对象")
        self._validate(manifest, GMMOD_REQUIRED, "gmmod")

        uuid = str(manifest.get("uuid") or "").strip()
        if not _UUID_RE.match(uuid):
            raise ModLoadError(f"uuid 非法（仅允许字母数字、下划线与连字符，长度 1-64）: {uuid!r}")

        ptype = str(manifest.get("type") or DEFAULT_TYPE).strip().lower()
        if ptype not in ALL_TYPES:
            raise ModLoadError(
                f"type 非法: {ptype!r}（可选 {' / '.join(ALL_TYPES)}）"
            )

        libs = manifest.get("libs", [])
        if not isinstance(libs, list):
            raise ModLoadError("libs 字段必须为列表（如果提供）")

        # py 型可申请面板数据权限；声明了就要是可识别的名字，避免拼错后静默失效
        perms = manifest.get("permissions", [])
        if perms is None:
            perms = []
        if not isinstance(perms, list):
            raise ModLoadError("permissions 字段必须为列表（如果提供）")
        perms = [str(p).strip() for p in perms if str(p).strip()]
        if perms and "*" not in perms:
            unknown = [p for p in perms if p not in KNOWN_PERMISSIONS]
            if unknown:
                raise ModLoadError(
                    f"permissions 含未知权限: {', '.join(unknown)}"
                    f"（可选 {' / '.join(KNOWN_PERMISSIONS)}，或 '*' 表示全部）"
                )
        self._check_support(manifest, force=force)

        target = self._dir_of(ptype, uuid)
        overwritten = target.exists()
        if overwritten and not force:
            raise ModLoadError(
                f"子插件 {manifest.get('name')}（{uuid}）已安装，请先卸载或使用 force=True 覆盖"
            )

        temp_dir = Path(tempfile.mkdtemp(prefix="gmmod_"))
        try:
            with zipfile.ZipFile(src, "r") as zf:
                _safe_extract(zf, temp_dir)

            if ptype in PAGE_TYPES:
                entry = str(manifest.get("index") or GMMOD_OPTIONAL["index"])
                if not (temp_dir / entry).is_file():
                    raise ModLoadError(
                        f"type={ptype} 的子插件必须包含入口页面 {entry}（可用 index 字段指定）"
                    )
            elif ptype == PY_TYPE:
                # py 型要真正执行代码，入口必须存在 —— 这里比 code 型严格
                entry = str(manifest.get("main") or DEFAULT_PY_ENTRY)
                if not (temp_dir / entry).is_file():
                    raise ModLoadError(
                        f"type=py 的子插件必须包含入口文件 {entry}"
                        f"（默认 main/main.py，可用 main 字段指定）"
                    )
            else:
                entry = str(manifest.get("entry") or GMMOD_OPTIONAL["entry"])
                if not (temp_dir / entry).is_file():
                    self._log("warning", f"入口文件 {entry} 不存在，将仅安装不加载")

            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(temp_dir), str(target))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        try:
            shutil.copy2(src, target.parent / (uuid + GMMOD_SUFFIX))
        except Exception as e:
            self._log("warning", f"保存原始包失败: {e}")

        missing = self._missing_libs(libs)
        if missing:
            self._log("warning", f"子插件 {manifest.get('name')} 依赖缺失: {', '.join(missing)}（可先导入对应 .gmlib）")

        if ptype == PY_TYPE:
            entry_value = str(manifest.get("main") or DEFAULT_PY_ENTRY)
        else:
            entry_value = str(manifest.get("entry") or GMMOD_OPTIONAL["entry"])

        record = {
            "uuid": uuid,
            "name": manifest.get("name"),
            "version": manifest.get("version"),
            "description": manifest.get("description"),
            "author": manifest.get("author", ""),
            "type": ptype,
            "entry": entry_value,
            "index": str(manifest.get("index") or GMMOD_OPTIONAL["index"]),
            "libs": libs,
            "permissions": perms,
            "path": str(target),
            "missing_libs": missing,
        }
        self._installed[uuid] = record
        self._write_meta(target, record)
        self._log("info", f"子插件 {record['name']} v{record['version']}（type={ptype}）导入成功 -> {target}")

        return {
            "ok": True, "kind": "gmmod", "uuid": uuid, "mod_type": ptype,
            "name": record["name"], "version": record["version"],
            "path": str(target), "missing_libs": missing, "overwritten": overwritten,
        }

    def _check_support(self, manifest: Dict[str, Any], force: bool = False) -> None:
        if _version_tuple(manifest.get("min_support_version")) > _version_tuple(self._panel_version()):
            msg = (f"子插件 {manifest.get('name')} 需要面板 >= "
                   f"{manifest.get('min_support_version')}，当前 {self._panel_version()}")
            if not force:
                raise ModLoadError(msg)
            self._log("warning", msg + "，已强制导入")

    def _panel_version(self) -> str:
        try:
            from . import __version__  # type: ignore

            return str(__version__)
        except Exception:
            return str(getattr(self.plugin, "version", "") or "0.0.0")

    def _write_meta(self, target: Path, record: Dict[str, Any]) -> None:
        try:
            if not atomic_write_json(target / ".gm_installed.json", record, indent=2):
                raise OSError("原子写入失败")
        except Exception as e:
            self._log("warning", f"写入安装记录失败: {e}")

    # ------------------------------------------------- 启用 / 停用 / 卸载

    def enable(self, uuid: str) -> Dict[str, Any]:
        rec = self.installed(uuid)
        if not rec:
            raise ModLoadError(f"未找到子插件 {uuid}")
        ptype = str(rec.get("type") or DEFAULT_TYPE)

        if ptype in PAGE_TYPES:
            if not self.page_file_ready(ptype, uuid):
                raise ModLoadError(
                    f"入口页面 {(Path(rec.get('path')) / str(rec.get('index') or 'index.html'))} 不存在，无法启用"
                )
            with self._state_lock:
                prev = self._state.get(ptype) or None
                if prev == uuid:
                    return {"ok": True, "message": f"已是启用状态"}
                self._state[ptype] = uuid
                self._save_state()
            where = "/admin" if ptype == "admin" else "官网首页"
            msg = f"已启用 {rec.get('name')}，{where} 已切换"
            if prev:
                msg += f"（原启用的 {prev} 已自动停用）"
            self._log("info", msg)
            return {"ok": True, "message": msg, "replaced": prev}

        # ---- py / code 型：需要真正加载并执行代码 ----
        #
        # ``already`` 必须在下面 unload/import 之前取，且只由「进本函数时
        # 的状态」决定 —— 它是用来区分「首次启用」与「热重载」的。
        with self._state_lock:
            already = bool((self._state.get("code") or {}).get(uuid))

        was_loaded = uuid in self._loaded

        # 已在跑：先卸干净再重新加载，等于一次热重载
        if was_loaded:
            self.unload_mod(uuid)

        got = self.import_mod(uuid)
        if got is None:
            # 加载失败时保持原状态，避免"以为启用了其实没跑"
            with self._state_lock:
                code = dict(self._state.get("code") or {})
                code.pop(uuid, None)
                self._state["code"] = code
                self._save_state()
            raise ModLoadError("入口模块加载失败，详见服务端日志")

        module, life = got
        setup = (life or {}).get("setup")
        if setup is not None:
            try:
                _invoke_hook(setup, self.plugin, _gm_module())
            except Exception as e:
                # setup 失败 → 回滚，不留半个加载状态
                self.unload_mod(uuid)
                with self._state_lock:
                    code = dict(self._state.get("code") or {})
                    code.pop(uuid, None)
                    self._state["code"] = code
                    self._save_state()
                raise ModLoadError(f"setup() 执行失败: {e}")

        with self._state_lock:
            code = dict(self._state.get("code") or {})
            code[uuid] = True
            self._state["code"] = code
            self._save_state()

        what = "py 子插件" if ptype == PY_TYPE else "子插件"
        # ⚠ 这里**只能**用进函数时抓的快照（already / was_loaded）。
        #
        # 历史 bug：原写法是 ``already or uuid in self._loaded``。
        # 而上面 import_mod() 成功时会把 uuid 写进 self._loaded，
        # 所以走到这行时 ``uuid in self._loaded`` **恒为 True** ——
        # 整个表达式成了恒真式，**首次启用也会报「已重新加载」**。
        # 实测：全新子插件第一次 enable 返回
        #   {'ok': True, 'message': '已重新加载 演示（py 子插件入口已执行）'}
        # 用户因此以为插件早就跑着、只是被重载了一次，排查时极易误判。
        act = "已重新加载" if (already or was_loaded) else "已启用"
        return {"ok": True, "message": f"{act} {rec.get('name')}（{what}入口已执行）"}

    def disable(self, uuid: str) -> Dict[str, Any]:
        rec = self.installed(uuid)
        if not rec:
            raise ModLoadError(f"未找到子插件 {uuid}")
        ptype = str(rec.get("type") or DEFAULT_TYPE)

        if ptype in PAGE_TYPES:
            with self._state_lock:
                if (self._state.get(ptype) or None) != uuid:
                    return {"ok": True, "message": "该子插件本就未启用"}
                self._state[ptype] = None
                self._save_state()
            where = "/admin" if ptype == "admin" else "官网首页"
            self._log("info", f"已停用 {rec.get('name')}，{where} 已还原")
            return {"ok": True, "message": "已停用，页面已还原为面板默认"}

        self.unload_mod(uuid)
        with self._state_lock:
            code = dict(self._state.get("code") or {})
            code.pop(uuid, None)
            self._state["code"] = code
            self._save_state()
        return {"ok": True, "message": "已停用（入口模块已移出内存）"}

    def uninstall(self, uuid: str) -> bool:
        if not _UUID_RE.match(str(uuid or "")):
            raise ModLoadError(f"uuid 非法: {uuid!r}")
        # 卸载会删目录，缓存必须作废
        self.invalidate_cache()
        rec = self.installed(uuid)
        if not rec:
            return False
        ptype = str(rec.get("type") or DEFAULT_TYPE)

        if ptype in PAGE_TYPES:
            with self._state_lock:
                if (self._state.get(ptype) or None) == uuid:
                    self._state[ptype] = None
                    self._save_state()
        elif uuid in self._loaded:
            self.unload_mod(uuid)
        with self._state_lock:
            code = dict(self._state.get("code") or {})
            if code.pop(uuid, None) is not None:
                self._state["code"] = code
                self._save_state()

        target = Path(rec.get("path") or self._dir_of(ptype, uuid))
        if not target.exists():
            self._installed.pop(uuid, None)
            return False

        # 先改名再删，避免 Windows 上文件被占用导致删一半
        pending = target.with_name("." + uuid + ".pending_delete")
        try:
            if pending.exists():
                shutil.rmtree(pending, ignore_errors=True)
            shutil.move(str(target), str(pending))
            shutil.rmtree(pending, ignore_errors=True)
        except Exception as e:
            raise ModLoadError(f"删除目录失败: {e}")

        pkg = target.parent / (uuid + GMMOD_SUFFIX)
        try:
            if pkg.is_file():
                pkg.unlink()
        except Exception:
            pass

        self._installed.pop(uuid, None)
        self._log("info", f"子插件 {uuid} 已卸载")
        return True

    # ---------------------------------------------------------- 入口模块

    def import_mod(self, uuid: str) -> Optional[Any]:
        """把子插件的入口模块加载并执行，返回 ``(module, life)``。

        ``life`` 是 ``{"setup": fn|None, "teardown": fn|None}``。
        加载失败返回 None（只记日志，不抛异常），调用方负责回滚启用状态。

        执行入口时会把入口所在目录临时插入 ``sys.path`` —— 这样入口写在
        ``main/main.py`` 时，同目录的 ``import helper`` 才能解析到
        ``main/helper.py``（实测不插入会 ModuleNotFoundError）。
        """
        rec = self.installed(uuid)
        if rec is None:
            self._log("error", f"未找到子插件 {uuid}")
            return None
        mod_dir = Path(rec.get("path"))
        entry = str(rec.get("entry") or GMMOD_OPTIONAL["entry"])
        entry_file = mod_dir / entry
        if not entry_file.is_file():
            self._log("error", f"子插件 {uuid} 入口文件不存在: {entry_file}")
            return None
        missing = self._missing_libs(rec.get("libs") or [])
        if missing:
            self._log("error", f"子插件 {uuid} 依赖未满足: {', '.join(missing)}")
            return None

        mod_name = _module_name(uuid)
        # 先记录 exec 之前已存在的模块，结束后做差分，得到"这个子插件带进来的模块"
        before = set(sys.modules)
        entry_dir = str(entry_file.parent)
        added_path = False
        try:
            if entry_dir not in sys.path:
                sys.path.insert(0, entry_dir)
                added_path = True
            importlib.invalidate_caches()

            spec = importlib.util.spec_from_file_location(mod_name, str(entry_file))
            if spec is None or spec.loader is None:
                raise ModLoadError("无法构建模块 spec")
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            self._module_names.append(mod_name)
            spec.loader.exec_module(module)
        except Exception as e:
            # 回滚：模块名、子模块残留、sys.modules 里的自己，全部清掉
            sys.modules.pop(mod_name, None)
            while mod_name in self._module_names:
                self._module_names.remove(mod_name)
            self._purge_owned(uuid, before)
            self._log("error", f"子插件 {uuid} 入口执行出错: {type(e).__name__}: {e}")
            return None
        finally:
            if added_path:
                try:
                    sys.path.remove(entry_dir)
                except ValueError:
                    pass

        # 子插件自己 import 进来的模块也记在它名下，热重载时一并清理
        self._owned_modules[uuid] = sorted(
            name for name in (set(sys.modules) - before)
            if isinstance(name, str) and name.startswith(MOD_PREFIX)
        )

        life = {
            "setup": getattr(module, "setup", None),
            "teardown": getattr(module, "teardown", None),
        }
        self._lifecycles[uuid] = life
        self._loaded[uuid] = mod_name
        # running 变了；缓存不会自动失效（mtime 未变），补一刀。
        self.invalidate_cache()
        self._log("info", f"子插件 {uuid} 入口模块已加载: {mod_name}")
        return module, life

    def _purge_owned(self, uuid: str, before: Optional[set] = None) -> None:
        """清掉子插件 exec 期间带进来的模块（只动 greenmoon_mods. 前缀）。"""
        names = self._owned_modules.pop(uuid, None)
        if names is None and before is not None:
            names = [
                name for name in (set(sys.modules) - before)
                if isinstance(name, str) and name.startswith(MOD_PREFIX)
            ]
        for name in names or []:
            sys.modules.pop(name, None)
        if names:
            importlib.invalidate_caches()

    def unload_mod(self, uuid: str) -> bool:
        """停用：跑 teardown → 摘事件 → 移出内存（磁盘文件保留）。"""
        mod_name = self._loaded.pop(uuid, None)
        life = self._lifecycles.pop(uuid, None) or {}

        # teardown 要在模块还活着的时候跑，且异常不能影响后续清理
        teardown = life.get("teardown")
        if callable(teardown):
            try:
                _invoke_hook(teardown, self.plugin)
            except Exception as e:
                self._log("error", f"子插件 {uuid} teardown() 执行异常: {e}")

        # 摘掉它注册过的事件处理器，否则重载后会收到重复事件
        gm = _gm_module()
        if gm is not None:
            try:
                gm.event._drop_owner(self.plugin, uuid)
            except Exception as e:
                self._log("warning", f"清理子插件 {uuid} 事件处理器失败: {e}")
            # 图片钩子同理：不摘的话，子插件停用后仍在收 QQ 图片，
            # 轻则重复生成地图画，重则调用到已被卸载的模块里的函数。
            try:
                gm.bot._drop_owner(self.plugin, uuid)
            except Exception as e:
                self._log("warning", f"清理子插件 {uuid} 图片钩子失败: {e}")

        if mod_name is None and uuid not in self._owned_modules:
            self._log("warning", f"子插件 {uuid} 未处于加载状态")
            return False

        if mod_name is not None:
            sys.modules.pop(mod_name, None)
            while mod_name in self._module_names:
                self._module_names.remove(mod_name)
        self._purge_owned(uuid)
        # running 字段来自 self._loaded，缓存不会自动失效（mtime 未变），
        # 所以这里和学生状态落盘一样补一刀，避免 installed() 读到旧的 running。
        self.invalidate_cache()
        self._log("info", f"子插件 {uuid} 已移出内存（文件保留）")
        return True

    def loaded_uuids(self) -> List[str]:
        return sorted(self._loaded.keys())

    # --------------------------------------------------------- 静态文件映射

    def _resolve_in(self, root: Path, rel: str) -> Optional[Path]:
        """把相对路径安全地解析到 root 内的真实文件。"""
        try:
            root_r = root.resolve()
        except Exception:
            return None
        raw = str(rel or "").replace("\\", "/").lstrip("/")
        parts = [s for s in raw.split("/") if s not in ("", ".")]
        for s in parts:
            if s == "..":
                return None
        target = root_r
        for s in parts:
            target = target / s
        try:
            target = target.resolve()
        except Exception:
            return None
        ts, rs = str(target), str(root_r)
        if ts != rs and not ts.startswith(rs + os.sep):
            return None
        return target

    def page_file(self, ptype: str, rel: str) -> Optional[Path]:
        """把 ``/gmpage/<ptype>/<rel>`` 映射到启用子插件目录下的真实文件。

        路径为空时返回入口页面；未启用、路径非法或文件不存在都返回 None。
        """
        got = self.active_page(ptype)
        if not got:
            return None
        root, entry = got
        raw = str(rel or "").replace("\\", "/").lstrip("/")
        target = self._resolve_in(root, raw or entry)
        if target is None or not target.is_file():
            return None
        return target

    def list_page_files(self, uuid: str) -> List[str]:
        """列出某个子插件内的全部文件（相对路径），供 Web UI 展示文件树。"""
        rec = self.installed(uuid)
        if not rec:
            return []
        root = Path(rec.get("path"))
        out: List[str] = []
        try:
            for p in sorted(root.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(root).as_posix()
                if rel.startswith(".gm_"):
                    continue
                out.append(rel)
        except Exception:
            pass
        return out

    def read_page_text(self, uuid: str, rel: str, limit: int = 512 * 1024) -> Optional[str]:
        """读取子插件内一个文本文件（仅限白名单扩展名 + 大小上限）。"""
        rec = self.installed(uuid)
        if not rec:
            return None
        root = Path(rec.get("path"))
        target = self._resolve_in(root, rel)
        if target is None or not target.is_file():
            return None
        if target.suffix.lower() not in TEXT_EXTS:
            return None
        try:
            if target.stat().st_size > limit:
                return None
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                return target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return None
        except Exception:
            return None

    def write_page_text(self, uuid: str, rel: str, content: str) -> bool:
        """写回子插件内的一个文本文件（仅限白名单扩展名）。"""
        rec = self.installed(uuid)
        if not rec:
            return False
        root = Path(rec.get("path"))
        parts = [s for s in str(rel or "").replace("\\", "/").lstrip("/").split("/") if s not in ("", ".")]
        if not parts or any(s == ".." for s in parts):
            return False
        target = self._resolve_in(root, str(rel))
        if target is None or target.suffix.lower() not in TEXT_EXTS:
            return False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not atomic_write_text(target, str(content)):
                raise OSError("原子写入失败")
            return True
        except Exception as e:
            self._log("error", f"写入 {rel} 失败: {e}")
            return False

    def page_file_ready(self, ptype: str, uuid: str) -> bool:
        rec = self._find(uuid, ptype)
        if not rec:
            return False
        root = Path(rec.get("path") or "")
        return (root / str(rec.get("index") or GMMOD_OPTIONAL["index"])).is_file()

    # ------------------------------------------------------------ 子插件配置
    #
    # 分工（两份文件都在子插件包根目录）：
    #
    #   settings.json   子插件作者写的**字段声明**（"有哪些设置项"）—— 只读
    #   config.json     实际**存的值**（"现在是什么值"）—— 面板与子插件都可写
    #
    # 为什么要拆成两份：schema 描述的是**这个子插件的形状**，随包分发、不该
    # 被运行时改写；值则相反，天天在变。混成一份的话，子插件一升级就会把
    # 用户设好的值连同旧 schema 一起冲掉。

    def _mod_root(self, uuid: str) -> Optional[Path]:
        """取子插件包根目录；不存在返回 None。"""
        rec = self.installed(uuid)
        if not rec:
            return None
        raw = str(rec.get("path") or "")
        if not raw:
            return None
        return Path(raw)

    def settings_schema(self, uuid: str, log_invalid: bool = False) -> List[Dict[str, Any]]:
        """读子插件包内的 ``settings.json``，返回**已校验**的字段列表。

        返回空列表表示「该子插件没有设置项」—— 这既包括文件不存在，
        也包括文件写坏了。调用方一律按"没有设置项"处理，不要区分。

        校验策略是**逐字段丢弃**而非整体拒绝：某个字段的 key 拼错、或者
        用了新版本才支持的 type，只让它自己消失，剩下的照常渲染。整个
        列表因为一行写错就全空、管理员打开设置页一片空白，那种体验更糟，
        而且看不出到底哪错了 —— 所以非法字段会记一条 warning 日志。

        ``log_invalid=True`` 时（面板主动打开设置页的场景）对解析失败也
        记日志；列表接口批量调用时传 False，避免刷屏。
        """
        root = self._mod_root(uuid)
        if root is None:
            return []
        target = self._resolve_in(root, SETTINGS_SCHEMA_NAME)
        if target is None or not target.is_file():
            return []
        try:
            raw = target.read_text(encoding="utf-8")
        except Exception as e:
            if log_invalid:
                self._log("warning", f"子插件 {uuid} 的 {SETTINGS_SCHEMA_NAME} 读取失败: {e}")
            return []
        try:
            data = json.loads(raw)
        except Exception as e:
            if log_invalid:
                self._log("error", f"子插件 {uuid} 的 {SETTINGS_SCHEMA_NAME} 不是合法 JSON: {e}")
            return []
        if not isinstance(data, dict):
            if log_invalid:
                self._log("error", f"子插件 {uuid} 的 {SETTINGS_SCHEMA_NAME} 顶层必须是对象")
            return []
        fields = data.get("fields")
        if not isinstance(fields, list):
            if log_invalid:
                self._log("error", f"子插件 {uuid} 的 {SETTINGS_SCHEMA_NAME} 缺少 fields 数组")
            return []
        if len(fields) > MAX_SETTINGS_FIELDS:
            if log_invalid:
                self._log(
                    "warning",
                    f"子插件 {uuid} 的 {SETTINGS_SCHEMA_NAME} 有 {len(fields)} 个字段，"
                    f"超出上限 {MAX_SETTINGS_FIELDS}，只取前 {MAX_SETTINGS_FIELDS} 个",
                )
            fields = fields[:MAX_SETTINGS_FIELDS]

        out: List[Dict[str, Any]] = []
        seen_keys = set()
        for idx, item in enumerate(fields):
            got = self._clean_field(item, uuid, idx)
            if got is None:
                continue
            key = got.get("key")
            # group 不存值，允许重名（两个分区标题可以都叫"高级"）；
            # 真正的字段 key 必须唯一，否则前端收集值时会互相覆盖。
            if key and got["type"] != "group":
                if key in seen_keys:
                    if log_invalid:
                        self._log("warning", f"子插件 {uuid} 的 {SETTINGS_SCHEMA_NAME} 字段 key 重复: {key}")
                    continue
                seen_keys.add(key)
            out.append(got)
        return out

    def _clean_field(self, item: Any, uuid: str, idx: int) -> Optional[Dict[str, Any]]:
        """校验单个字段声明；不合法返回 None。"""
        if not isinstance(item, dict):
            self._log("warning", f"子插件 {uuid} 的 settings.json 第 {idx} 项不是对象，已忽略")
            return None
        ftype = str(item.get("type") or "").strip().lower()
        if ftype not in SETTINGS_FIELD_TYPES:
            self._log(
                "warning",
                f"子插件 {uuid} 的 settings.json 第 {idx} 项 type 非法: {ftype!r}"
                f"（可选 {' / '.join(SETTINGS_FIELD_TYPES)}），已忽略",
            )
            return None

        # group 是纯展示的分区标题，不需要 key，也不存值
        if ftype == "group":
            label = str(item.get("label") or "").strip()
            if not label:
                self._log("warning", f"子插件 {uuid} 的 settings.json 第 {idx} 项 group 缺少 label，已忽略")
                return None
            return {"type": "group", "label": label}

        key = str(item.get("key") or "").strip()
        if not _SETTINGS_KEY_RE.match(key):
            self._log(
                "warning",
                f"子插件 {uuid} 的 settings.json 第 {idx} 项 key 非法: {key!r}"
                f"（须以字母或下划线开头，仅含字母数字下划线，长度 1-64），已忽略",
            )
            return None

        fld: Dict[str, Any] = {
            "key": key,
            "type": ftype,
            "label": str(item.get("label") or key),
        }
        for opt in ("hint", "placeholder"):
            val = item.get(opt)
            if isinstance(val, str) and val:
                fld[opt] = val

        if ftype == "select":
            opts = item.get("options")
            cleaned: List[Dict[str, str]] = []
            if isinstance(opts, list):
                for o in opts:
                    if not isinstance(o, dict):
                        continue
                    if "value" not in o:
                        continue
                    cleaned.append({
                        "value": str(o.get("value", "")),
                        "label": str(o.get("label", o.get("value", ""))),
                    })
            if not cleaned:
                self._log(
                    "warning",
                    f"子插件 {uuid} 的 settings.json 字段 {key} 是 select 但没有合法 options，已忽略",
                )
                return None
            fld["options"] = cleaned

        if ftype == "number":
            for opt in ("min", "max"):
                val = item.get(opt)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    fld[opt] = val

        if "default" in item:
            norm = self._normalize_value(fld, item.get("default"))
            if norm is not None:
                fld["default"] = norm
        if fld.get("type") == "bool" and "default" not in fld:
            fld["default"] = False

        # advanced 字段在面板里默认折叠起来，避免设置页一打开就是几十行
        if bool(item.get("advanced")):
            fld["advanced"] = True
        return fld

    def _normalize_value(self, fld: Dict[str, Any], value: Any) -> Optional[Any]:
        """按字段类型把前端/子插件给的值归一化；不合法返回 None（表示"拒收"）。

        ⚠ 返回 None 表示拒收，**不能**用 None 表示"合法地设成了空值"：
        文本类字段的空串要能正常存下来（用户清空一个输入框是合法操作），
        所以空串走的是返回 ``""`` 这条路，跟拒收区分得开。
        """
        ftype = fld.get("type")
        if ftype == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "1", "yes", "on"):
                    return True
                if low in ("false", "0", "no", "off", ""):
                    return False
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return bool(value)
            return None
        if ftype == "number":
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)):
                num: Any = value
            elif isinstance(value, str):
                raw = value.strip()
                if not raw:
                    return None
                try:
                    num = float(raw) if ("." in raw or "e" in raw.lower()) else int(raw)
                except Exception:
                    return None
            else:
                return None
            lo, hi = fld.get("min"), fld.get("max")
            if isinstance(num, (int, float)):
                if isinstance(lo, (int, float)) and num < lo:
                    return None
                if isinstance(hi, (int, float)) and num > hi:
                    return None
            return num
        if ftype == "select":
            want = "" if value is None else str(value)
            for o in (fld.get("options") or []):
                if o.get("value") == want:
                    return want
            return None
        # text / textarea / password
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return None
        text = str(value)
        if len(text) > MAX_SETTINGS_TEXT_LEN:
            return None
        return text

    def read_mod_config(self, uuid: str) -> Dict[str, Any]:
        """读子插件包内的 ``config.json``；缺文件或写坏都返回 ``{}``。"""
        root = self._mod_root(uuid)
        if root is None:
            return {}
        target = self._resolve_in(root, MOD_CONFIG_NAME)
        if target is None or not target.is_file():
            return {}
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except Exception as e:
            self._log("warning", f"子插件 {uuid} 的 {MOD_CONFIG_NAME} 读取失败: {e}")
            return {}
        if not isinstance(data, dict):
            self._log("warning", f"子插件 {uuid} 的 {MOD_CONFIG_NAME} 顶层不是对象，按空配置处理")
            return {}
        return self._clean_stored(uuid, data)

    def _clean_stored(self, uuid: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """把落盘的值按 schema 过一遍，丢掉不再声明/类型不对的残留键。

        为什么读的时候也要过滤：schema 改过（作者删了个字段、或改了它的
        type）之后，config.json 里会留下谁都不认的旧键。原样回给前端的话，
        它会渲染成"看不见但确实存在"的值，保存时又被原样写回去，永远清不掉。
        这里一次性剥干净。
        """
        schema = self.settings_schema(uuid)
        allowed = {f["key"]: f for f in schema if f.get("key")}
        out: Dict[str, Any] = {}
        for key, val in data.items():
            fld = allowed.get(str(key))
            if fld is None:
                continue
            norm = self._normalize_value(fld, val)
            if norm is not None:
                out[str(key)] = norm
        return out

    def write_mod_config(self, uuid: str, values: Dict[str, Any]) -> Tuple[bool, List[str], str]:
        """把配置写回子插件包内的 ``config.json``。

        返回 ``(是否写入成功, 被拒的键列表, 说明文字)``。

        **只接受 schema 声明过的键**，理由有两个：
        * 前端提交上来的东西不能全信 —— 不然任何能调这个接口的人都能往
          配置里塞任意键值；
        * 子插件作者改了 schema 之后，旧键不该继续被写回去"复活"。

        ``values`` 按**覆盖**语义处理（提交什么就是什么），不做增量合并 ——
        增量合并会让"取消勾选一个开关"无法表达（不提交 ≠ 要关掉）。
        需要增量的话，子插件侧有 ``gm.config.update()``。
        """
        if not isinstance(values, dict):
            return False, [], "values 必须是对象"
        root = self._mod_root(uuid)
        if root is None:
            return False, [], "子插件不存在"
        schema = self.settings_schema(uuid)
        if not schema:
            return False, list(values.keys()), (
                f"该子插件没有声明设置项（包内缺少或无法解析 {SETTINGS_SCHEMA_NAME}），拒绝写入"
            )
        allowed = {f["key"]: f for f in schema if f.get("key")}

        cleaned: Dict[str, Any] = {}
        rejected: List[str] = []
        for key, val in values.items():
            name = str(key)
            fld = allowed.get(name)
            if fld is None:
                rejected.append(name)
                continue
            # ── 面罩兜底 ──────────────────────────────────────────────
            # 密码字段的"面罩 = 别动它"语义**主要**由 web 层负责（前端把
            # 面罩原样提交回来，web 识别后跳过）。但那条规则只活在 web.py
            # 里：任何绕过 web 直接调本函数的代码（子插件、未来新增的接口、
            # 批量导入脚本）都会把真 token 冲成一串星号，而且**静默** ——
            # 用户只会发现"过几天机器人鉴权就失败了"，极难排查。
            #
            # 所以这里再挡一道：password 字段的值等于面罩串时，直接忽略
            # （保留磁盘原值）。想真的把密码设成 6 个星号？那是病态需求，
            # 不值得为它开一个能把所有人坑进去的口子。
            if fld.get("type") == "password" and str(val) == SETTINGS_MASK:
                continue
            norm = self._normalize_value(fld, val)
            if norm is None:
                rejected.append(name)
                continue
            cleaned[name] = norm

        # 未提交的已声明字段保留磁盘上的原值（前端可能只渲染了一部分）
        try:
            current = self.read_mod_config(uuid)
        except Exception:
            current = {}
        for key in allowed:
            if key not in cleaned and key in current:
                cleaned[key] = current[key]
        # schema 有默认值的、磁盘上也没有的，把默认值落下来 ——
        # 这样子插件代码第一次 gm.config.get() 就能拿到声明里的默认值，
        # 不用自己再写一遍 fallback。
        for key, fld in allowed.items():
            if key not in cleaned and "default" in fld:
                cleaned[key] = fld["default"]

        target = self._resolve_in(root, MOD_CONFIG_NAME)
        if target is None:
            return False, rejected, "目标路径非法"
        # config.json 写坏了子插件就读不到自己的配置，所以做一次 .bak 备份
        if not atomic_write_json(target, cleaned, backup=True):
            self._log("error", f"子插件 {uuid} 的 {MOD_CONFIG_NAME} 写入失败")
            return False, rejected, "写入失败（详见服务端日志）"
        return True, rejected, "配置已保存"

    def mod_config_summary(self, uuid: str) -> Dict[str, Any]:
        """给列表接口用的轻量摘要 —— 前端据此决定要不要显示「⚙ 设置」按钮。"""
        schema = self.settings_schema(uuid)
        return {
            "has_settings": bool(schema),
            "field_count": len([f for f in schema if f.get("type") != "group"]),
        }

    # ------------------------------------------------------------ 生命周期

    def bootstrap(self) -> Dict[str, Any]:
        """启动时：注入依赖路径 + 清点子插件 + 校验启用状态 + 重载 py 子插件。"""
        added = self.sync_libs_path()
        mods = self.list_mods()
        for m in mods:
            self._installed[str(m.get("uuid"))] = m

        uuids = {str(m.get("uuid")) for m in mods}
        changed = False
        for ptype in PAGE_TYPES:
            uid = self._state.get(ptype)
            if uid and (uid not in uuids or not self.page_file_ready(ptype, uid)):
                self._log("warning", f"{ptype} 页面子插件 {uid} 已失效，自动停用")
                self._state[ptype] = None
                changed = True
        # py / code 型：状态里标着启用的，重启后要重新执行入口
        started: List[str] = []
        failed: List[str] = []
        code = dict(self._state.get("code") or {})
        for uid in sorted(code.keys()):
            if not code.get(uid):
                continue
            if uid not in uuids:
                self._log("warning", f"代码型子插件 {uid} 已不存在，自动停用")
                code.pop(uid, None)
                changed = True
                continue
            got = self.import_mod(uid)
            if got is None:
                code.pop(uid, None)
                changed = True
                failed.append(uid)
                continue
            _module, life = got
            setup = (life or {}).get("setup")
            if setup is not None:
                try:
                    _invoke_hook(setup, self.plugin, _gm_module())
                except Exception as e:
                    self._log("error", f"子插件 {uid} setup() 执行失败: {e}")
                    self.unload_mod(uid)
                    code.pop(uid, None)
                    changed = True
                    failed.append(uid)
                    continue
            started.append(uid)

        # bootstrap 在 Web 服务器启动之前调用，本身是单线程的；这里统一走
        # 同一把锁只是为了让「改状态必持锁」这条不变量没有例外，便于日后维护。
        with self._state_lock:
            self._state["code"] = code
            if changed:
                self._save_state()
        if started:
            self._log("info", f"已恢复 {len(started)} 个子插件: {', '.join(started)}")
        return {"libs_added": added, "mods": mods, "started": started, "failed": failed}

    def shutdown(self) -> None:
        # 先按逆序跑 teardown，再统一清模块；顺序反了子插件会拿到半个模块
        for uuid in list(self._loaded.keys()):
            try:
                life = self._lifecycles.pop(uuid, None) or {}
                teardown = life.get("teardown")
                if callable(teardown):
                    _invoke_hook(teardown, self.plugin)
            except Exception as e:
                self._log("error", f"子插件 {uuid} teardown() 执行异常: {e}")
            try:
                gm = _gm_module()
                if gm is not None:
                    gm.event._drop_owner(self.plugin, uuid)
                    gm.bot._drop_owner(self.plugin, uuid)
            except Exception:
                pass

        for name in self._module_names:
            sys.modules.pop(name, None)
        for names in self._owned_modules.values():
            for name in names:
                sys.modules.pop(name, None)
        importlib.invalidate_caches()

        self._module_names = []
        self._owned_modules.clear()
        self._lifecycles.clear()
        self._loaded.clear()
        self._installed.clear()
        self.unsync_libs_path()
