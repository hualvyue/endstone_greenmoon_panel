"""GreenMoon 子插件加载器（页面接管型）。

包类型（manifest.json 的 ``type`` 字段）
--------------------------------------
``index``
    官网首页子插件。启用后 ``/`` 返回该包内的 ``index.html``，
    包内 **全部** 文件（js / css / 图片 / 子页面 / 子目录）通过 ``/gmpage/index/<路径>`` 可达。
``admin``
    管理页子插件。启用后 ``/admin`` 返回该包内的 ``index.html``，
    包内文件通过 ``/gmpage/admin/<路径>`` 可达。
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
        <uuid>/              代码型子插件

设计要点
--------
* 纯标准库，不引入第三方依赖。
* 静态资源由 web 层 **流式** 发送并按扩展名给 MIME，支持 Range（音视频可拖进度）。
* 路径一律以插件数据目录为基准，解压与静态请求都做 Zip-Slip / 目录穿越校验。
* 同一 ``type`` 同时只允许一个子插件处于启用状态，重复启用自动停用前一个。
"""

from __future__ import annotations

import importlib.util
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "ModLoader",
    "ModLoadError",
    "GMMOD_REQUIRED",
    "GMLIB_REQUIRED",
    "GMMOD_SUFFIX",
    "GMLIB_SUFFIX",
    "PAGE_TYPES",
    "mime_for",
]

GMMOD_SUFFIX = ".gmmod"
GMLIB_SUFFIX = ".gmlib"

#: 可接管页面的 type 取值
PAGE_TYPES = ("index", "admin")

#: 未声明 type 时的默认值
DEFAULT_TYPE = "code"

GMMOD_REQUIRED = ["name", "uuid", "version", "min_support_version", "description"]
GMLIB_REQUIRED = ["name", "python_version", "platform", "liblist"]

GMMOD_OPTIONAL = {
    "type": DEFAULT_TYPE,
    "author": "",
    "entry": "main.py",
    "index": "index.html",
    "libs": [],
    "homepage": "",
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
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def enabled_uuid(self, ptype: str) -> Optional[str]:
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
        """子插件落盘目录：页面型按 type 分目录，代码型保持旧结构。"""
        if ptype in PAGE_TYPES:
            return self.mods_dir / ptype / uuid
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

        for ptype in PAGE_TYPES:
            pdir = self.mods_dir / ptype
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
                    rec["type"] = ptype
                    mods.append(rec)

        try:
            entries = sorted(p for p in self.mods_dir.iterdir() if p.is_dir())
        except Exception:
            entries = []
        for d in entries:
            if d.name.startswith(".") or d.name in PAGE_TYPES:
                continue
            rec = self._read_mod(d)
            if rec:
                rec["type"] = str(rec.get("type") or DEFAULT_TYPE)
                mods.append(rec)

        for m in mods:
            uid = str(m.get("uuid"))
            if m.get("type") in PAGE_TYPES:
                m["enabled"] = self._state.get(m.get("type")) == uid
            else:
                m["enabled"] = bool((self._state.get("code") or {}).get(uid))
            m["missing_libs"] = self._missing_libs(m.get("libs") or [])
        return mods

    def installed(self, uuid: str) -> Optional[Dict[str, Any]]:
        for m in self.list_mods():
            if str(m.get("uuid")) == uuid:
                return m
        return None

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
        if ptype not in PAGE_TYPES and ptype != DEFAULT_TYPE:
            raise ModLoadError(f"type 非法: {ptype!r}（可选 {DEFAULT_TYPE} / {' / '.join(PAGE_TYPES)}）")

        libs = manifest.get("libs", [])
        if not isinstance(libs, list):
            raise ModLoadError("libs 字段必须为列表（如果提供）")
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

        record = {
            "uuid": uuid,
            "name": manifest.get("name"),
            "version": manifest.get("version"),
            "description": manifest.get("description"),
            "author": manifest.get("author", ""),
            "type": ptype,
            "entry": str(manifest.get("entry") or GMMOD_OPTIONAL["entry"]),
            "index": str(manifest.get("index") or GMMOD_OPTIONAL["index"]),
            "libs": libs,
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
            with open(target / ".gm_installed.json", "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)
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
            prev = self.enabled_uuid(ptype)
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

        if self.import_mod(uuid) is None:
            raise ModLoadError("入口模块加载失败，详见服务端日志")
        code = dict(self._state.get("code") or {})
        code[uuid] = True
        self._state["code"] = code
        self._save_state()
        return {"ok": True, "message": f"已启用 {rec.get('name')}（入口模块已加载）"}

    def disable(self, uuid: str) -> Dict[str, Any]:
        rec = self.installed(uuid)
        if not rec:
            raise ModLoadError(f"未找到子插件 {uuid}")
        ptype = str(rec.get("type") or DEFAULT_TYPE)

        if ptype in PAGE_TYPES:
            if self.enabled_uuid(ptype) != uuid:
                return {"ok": True, "message": "该子插件本就未启用"}
            self._state[ptype] = None
            self._save_state()
            where = "/admin" if ptype == "admin" else "官网首页"
            self._log("info", f"已停用 {rec.get('name')}，{where} 已还原")
            return {"ok": True, "message": "已停用，页面已还原为面板默认"}

        self.unload_mod(uuid)
        code = dict(self._state.get("code") or {})
        code.pop(uuid, None)
        self._state["code"] = code
        self._save_state()
        return {"ok": True, "message": "已停用（入口模块已移出内存）"}

    def uninstall(self, uuid: str) -> bool:
        if not _UUID_RE.match(str(uuid or "")):
            raise ModLoadError(f"uuid 非法: {uuid!r}")
        rec = self.installed(uuid)
        if not rec:
            return False
        ptype = str(rec.get("type") or DEFAULT_TYPE)

        if ptype in PAGE_TYPES:
            if self.enabled_uuid(ptype) == uuid:
                self._state[ptype] = None
                self._save_state()
        elif uuid in self._loaded:
            self.unload_mod(uuid)
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
        """把子插件的入口模块加载为模块对象（不注册事件、不调用生命周期）。"""
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

        mod_name = f"greenmoon_mods.{re.sub(r'[^0-9A-Za-z_]', '_', uuid)}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, str(entry_file))
            if spec is None or spec.loader is None:
                raise ModLoadError("无法构建模块 spec")
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            if mod_name not in self._module_names:
                self._module_names.append(mod_name)
            spec.loader.exec_module(module)
        except Exception as e:
            sys.modules.pop(mod_name, None)
            self._log("error", f"子插件 {uuid} 入口执行出错: {e}")
            return None

        self._loaded[uuid] = mod_name
        self._log("info", f"子插件 {uuid} 入口模块已加载: {mod_name}")
        return module, getattr(module, "setup", None)

    def unload_mod(self, uuid: str) -> bool:
        mod_name = self._loaded.pop(uuid, None)
        if mod_name is None:
            self._log("warning", f"子插件 {uuid} 未处于加载状态")
            return False
        sys.modules.pop(mod_name, None)
        if mod_name in self._module_names:
            self._module_names.remove(mod_name)
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
            target.write_text(str(content), encoding="utf-8")
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
        """启动时：注入依赖路径 + 清点子插件 + 校验启用状态是否仍有效。"""
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
        if changed:
            self._save_state()
        return {"libs_added": added, "mods": mods}

    def shutdown(self) -> None:
        for name in self._module_names:
            sys.modules.pop(name, None)
        self._module_names = []
        self._loaded.clear()
        self._installed.clear()
        self.unsync_libs_path()
