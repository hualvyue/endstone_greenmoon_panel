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
)

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
        #: installed() 的缓存：uuid -> 记录，以及缓存生成时的 mods_dir mtime。
        #: 见 installed() 的注释 —— 权限校验路径上会高频调用它。
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
            tmp = self.state_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.state_path)

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
        """扫描 mod 目录，返回全部已安装子插件（不执行任何代码）。"""
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
            if m.get("type") in PAGE_TYPES:
                m["enabled"] = self._state.get(m.get("type")) == uid
            else:
                m["enabled"] = bool((self._state.get("code") or {}).get(uid))
            m["missing_libs"] = self._missing_libs(m.get("libs") or [])
            m["running"] = uid in self._loaded
            m.setdefault("permissions", [])
            out.append(m)
        return out

    def installed(self, uuid: str) -> Optional[Dict[str, Any]]:
        """按 uuid 取子插件记录。

        注意调用频率：``gm`` 层的**每次权限校验**都会走到这里
        （``_context._permissions_of`` → ``installed``）。而
        ``list_mods()`` 要遍历全部 mod 目录并逐个读 manifest.json ——
        子插件高频调 gm API 时会反复扫盘，若发生在主线程 tick 中就卡服。
        因此这里做一层带失效标记的缓存。

        任何会改变已安装集合的操作（装/卸/启/停）都应调用
        ``invalidate_cache()``；为稳妥起见这里再叠一个 mtime 校验：
        mods_dir 的修改时间变了就自动失效，即使某处漏调也不会读到陈旧数据。
        """
        table = self._installed_cache()
        if table is not None:
            return table.get(str(uuid))

    def _installed_cache(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """返回 uuid → 记录 的缓存表；需要重建时重建。"""
        try:
            stamp = self.mods_dir.stat().st_mtime if self.mods_dir.is_dir() else -1
        except Exception:
            stamp = -1
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
        tmp = self.liblist_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(set(liblist)), f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.liblist_path)

    def installed_packages(self) -> Dict[str, str]:
        return {_whl_pkg(n): n for n in self.load_liblist()}

    def sync_libs_path(self) -> int:
        added = 0
        try:
            wheels = sorted(p for p in self.libs_dir.glob("*.whl") if p.is_file())
        except Exception as e:
            self._log("warning", f"扫描依赖目录失败 {self.libs_dir}: {e}")
            return 0
        for whl in wheels:
            p = str(whl)
            if p not in sys.path:
                sys.path.insert(0, p)
                added += 1
        if added:
            self._log("info", f"已向 sys.path 注入 {added} 个依赖包")
        return added

    def unsync_libs_path(self) -> None:
        root = str(self.libs_dir)
        sys.path[:] = [p for p in sys.path if not (p.endswith(".whl") and p.startswith(root))]

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
        with self._state_lock:
            already = bool((self._state.get("code") or {}).get(uuid))

        # 已在跑：先卸干净再重新加载，等于一次热重载
        if uuid in self._loaded:
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
        act = "已重新加载" if already or uuid in self._loaded else "已启用"
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

        if mod_name is None and uuid not in self._owned_modules:
            self._log("warning", f"子插件 {uuid} 未处于加载状态")
            return False

        if mod_name is not None:
            sys.modules.pop(mod_name, None)
            while mod_name in self._module_names:
                self._module_names.remove(mod_name)
        self._purge_owned(uuid)
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
