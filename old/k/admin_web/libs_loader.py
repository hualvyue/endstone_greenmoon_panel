# -*- coding: utf-8 -*-
"""离线依赖目录（``data/libs``）的加载器。

这个模块解决一个很具体的问题
============================

面板从 QQ 群收图片、再转成地图画，需要 **Pillow** 做解码和缩放。
但 BDS 自带的 Python 环境往往**不接受 pip 装包**，用户也没法随手
``pip install Pillow``。所以需要一个「把 whl 丢进目录就能用」的出路。

**为什么不能直接把 .whl 路径插进 sys.path**
-------------------------------------------

先澄清一个常见误解 —— CPython 的 ``zipimport.zipimporter``
**接受任意 ZIP 文件，包括 .whl**：:

    sys.path.insert(0, "six-1.16.0-py2.py3-none-any.whl")
    import six          # 成功

所以对**纯 Python 包**（six、requests、websockets 这类），
直接插 whl 路径确实有效。

但 Pillow **不行**，原因是 ``zipimport`` **加载不了 .so**。
Pillow 的 whl 里有 8 个二进制扩展::

    PIL/_imaging.cpython-311-x86_64-linux-gnu.so     3.0 MB
    PIL/_webp.cpython-311-x86_64-linux-gnu.so
    PIL/_imagingft.cpython-311-x86_64-linux-gnu.so
    pillow.libs/libopenjp2-05423b53.so               581 KB
    ...

而 Pillow 的**每一个实际功能**（``Image.new`` / ``Image.open`` /
``resize`` / 取像素）都要经过 ``_imaging``。于是从 whl 直接导入的表现是：

1. ``import PIL`` → 成功（纯 Python 部分）
2. ``from PIL import _imaging`` → ``ImportError``
3. 一调用就炸

第 1 步的成功**极有迷惑性** —— 检测代码若只看 ``import PIL`` 能不能过，
就会判断「依赖正常」，然后每张图都静默失败。

**所以本模块的做法是：解压到目录，再把目录插进 sys.path。**

解压后 ``.so`` 就是普通文件，能被 ``ExtensionFileLoader`` 正常加载。

三条设计原则
============

1. **幂等 + 增量**：用 ``.unpacked.json`` 记录每个 whl 的大小与 mtime，
   没变就跳过解压。否则每次启动都重解压几 MB 的 Pillow，白等好几秒。
2. **绝不抛异常**：这是插件启动路径上的代码。目录只读、whl 损坏、
   磁盘满 —— 任何一种都不该让面板起不来，最多打日志。
3. **追加而非插队**：解压目录加到 ``sys.path`` **末尾**。运行环境自带的
   库优先级更高 —— 内置的 Pillow 很可能本来就是好的，不该被一个
   版本不匹配的旧 whl 顶掉。

ABI 匹配的坑（必须让用户知道）
==============================

whl 文件名里带 ABI 标签，例如::

    pillow-10.4.0-cp311-cp311-manylinux_2_28_x86_64.whl
                    ^^^^^ 必须与服务器 Python 的 major.minor 一致
                          ^^^^^^^^^^^^^^^^^^^^^^^ 架构/平台也要一致

**装错版本不会在导入时报错** —— 解压正常、``import PIL`` 正常，
只有真正调用时才炸（因为加载的是 .so）。所以 ``gm.mapart.diagnose()``
会主动检查 C 扩展是否真的可用，见那里的说明。
"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any, List, Optional

__all__ = ["install", "register", "unpacked_dir_name", "state_path_for"]

#: 解压产物放在 libs 目录下的这个子目录里（下划线开头，便于一眼看出是派生的）
_UNPACKED_DIRNAME = "_unpacked"

#: 记录「哪个 whl 已解压到哪、什么时候解的」的清单文件名
_STATE_FILENAME = ".unpacked.json"

#: 单个 whl 解压后的体积上限保护（防止畸形压缩包撑爆磁盘）
_MAX_TOTAL_MB = 512


def unpacked_dir_name(whl_name: str) -> str:
    """由 whl 文件名推出解压目录名。

    直接沿用 whl 的 stem（``pillow-10.4.0-cp311-...``）—— 保留版本与
    ABI 标签，用户对着目录名就能看出装的是哪个版本、对不对得上。
    只做最小清理：去掉路径分隔符之类的危险字符。
    """
    stem = Path(str(whl_name)).stem
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in stem)
    return safe[:120] or "unknown"


def state_path_for(libs_dir: Path) -> Path:
    return Path(libs_dir) / _UNPACKED_DIRNAME / _STATE_FILENAME


# ------------------------------------------------------------------ 状态读写

def _load_state(libs_dir: Path) -> dict:
    """读解压清单。任何异常都当成「没有清单」——重新解压一遍即可，
    不值得为它报错。"""
    try:
        with open(state_path_for(libs_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(libs_dir: Path, state: dict) -> None:
    """写解压清单。写不进去只影响下次要重解压，不是致命问题。"""
    path = state_path_for(libs_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


def _fingerprint(whl: Path) -> str:
    """whl 的指纹 = 大小 + mtime。

    不读内容做哈希：Pillow 的 whl 有 4.5 MB，每次启动都全量读一遍
    只为判断"变没变"太浪费。大小 + mtime 对「用户换了 whl」这个
    实际场景足够灵敏。
    """
    try:
        st = whl.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except Exception:
        return ""


# ------------------------------------------------------------------ 解压

def _safe_extract(whl: Path, dest: Path) -> int:
    """把 whl 解压到 dest，返回解压出的文件数。

    ⚠ 必须防 ZIP-Slip：归档里的路径可能写成 ``../../etc/passwd``。
    逐个成员校验解析后的真实路径必须在 dest 之内，不在就跳过。
    这类 whl 来路不明（用户自己下载的），不能假定它是良性的。
    """
    count = 0
    dest = dest.resolve()
    total = 0
    with zipfile.ZipFile(whl) as z:
        for info in z.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            # 归一化并校验落点
            target = (dest / name).resolve()
            try:
                target.relative_to(dest)
            except ValueError:
                continue                    # 越界，丢弃
            total += info.file_size
            if total > _MAX_TOTAL_MB * 1024 * 1024:
                raise ValueError(f"解压体积超过 {_MAX_TOTAL_MB}MB 上限，放弃")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            count += 1
    return count


def _unpack_one(whl: Path, root: Path, logger: Any = None) -> Optional[Path]:
    """解压单个 whl，返回解压目录；失败返回 None（不抛）。"""
    dest = root / unpacked_dir_name(whl.name)
    try:
        # 先解压到临时目录再改名 —— 中途失败不会留下"半个包"被误当成完整包。
        # 这一点对 C 扩展尤其重要：半个包能 import 成功但调用时崩，
        # 比干脆失败更难查。
        tmp = dest.with_name(dest.name + ".tmp")
        if tmp.is_dir():
            _rmtree(tmp)
        tmp.mkdir(parents=True, exist_ok=True)
        n = _safe_extract(whl, tmp)
        if dest.is_dir():
            _rmtree(dest)
        os.replace(str(tmp), str(dest))
        if logger is not None:
            logger.info(f"[依赖] 已解压 {whl.name}（{n} 个文件）")
        return dest
    except Exception as e:
        if logger is not None:
            logger.warning(f"[依赖] 解压 {whl.name} 失败：{type(e).__name__}: {e}")
        try:
            tmp = dest.with_name(dest.name + ".tmp")
            if tmp.is_dir():
                _rmtree(tmp)
        except Exception:
            pass
        return None


def _rmtree(path: Path) -> None:
    """删目录。``shutil.rmtree`` 在 Windows 上遇到只读文件会失败，
    所以顺手清一下只读位。"""
    import shutil
    import stat
    for p in path.rglob("*"):
        try:
            p.chmod(p.stat().st_mode | stat.S_IWRITE)
        except Exception:
            pass
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


# ------------------------------------------------------------------ sys.path

def _add_to_path(p: str, logger: Any = None, label: str = "") -> bool:
    """把路径**追加**到 sys.path（没有才加），返回是否新增。

    为什么是追加而不是 ``insert(0, ...)``：
    运行环境自带的库应当优先。以 Pillow 为例，BDS 环境里很可能本来就有
    一个**版本正确、ABI 匹配**的 Pillow，而用户放进 libs 的那个可能是
    随手下载的、版本对不上的。插队会让"本来能跑的"变成"以前能跑现在炸"。
    """
    try:
        # ⚠ 去重要按**归一化路径**比，不能直接 ``p in sys.path``。
        # sys.path 里的条目形式五花八门：
        #   * ``''``（空串）= 当前工作目录 —— 这是 Python 启动时 sys.path[0]
        #     的标准写法
        #   * ``'.'`` / ``'./libs'`` = 同一个位置的相对写法
        #   * 绝对路径
        # 字符串直接比会把「同一个目录的不同写法」当成两个条目，重复注入。
        # 虽然无害（导入一次就进 sys.modules 了），但会让 sys.path 越跑越长，
        # 也会干扰那些「检查 sys.path 有没有被污染」的逻辑。
        key = _path_key(p)
        for existing in sys.path:
            if _path_key(existing) == key:
                return False
        sys.path.append(p)
        return True
    except Exception as e:
        if logger is not None:
            logger.warning(f"[依赖] 注入路径失败 {label or p}：{e}")
        return False


def _path_key(p: str) -> str:
    """把 sys.path 条目归一化成可比较的键。

    - 空串 / ``'.'`` → 当前工作目录
    - 其余 → ``normpath`` 后的绝对路径
    失败时退回原字符串（宁可比较不准，也不要因此抛异常）。
    """
    try:
        s = str(p or "")
        if s in ("", "."):
            return os.path.normcase(os.path.abspath(os.getcwd()))
        return os.path.normcase(os.path.normpath(os.path.abspath(s)))
    except Exception:
        return str(p or "")


def _coerce_dir(libs_dir) -> Optional[Path]:
    """把入参规整成目录 Path；不合法返回 None。

    ⚠ 空串要**明确拒绝**：``Path("")`` 会变成 ``Path(".")``，
    也就是当前工作目录。于是 ``install("")`` 会把 cwd 当成依赖目录
    扫描并注册进 sys.path —— 等于把工作目录里的任意 .py 都变成可导入
    模块。这不是我们想要的语义（调用方本意是"没配目录"），
    所以在这里挡掉，而不是交给 Path 去"猜"。

    同样地，``None`` / 空列表 / 纯空白都视为「没有依赖目录」。
    """
    if libs_dir is None:
        return None
    try:
        if isinstance(libs_dir, (list, tuple, set)):
            return None
        s = str(libs_dir).strip()
        if not s:
            return None
        return Path(s)
    except Exception:
        return None


def register(libs_dir, logger: Any = None) -> List[str]:
    """把 libs 目录**及其下已解压的包目录**注册进 sys.path。

    不做解压，只挂路径。适合在解压之后再调一次，或单独用来
    「补挂」某些目录。返回新增的路径列表。
    """
    added: List[str] = []
    root = _coerce_dir(libs_dir)
    if root is None:
        return added

    # 1) libs 目录本身：让纯 Python 的 .py / 包目录可直接导入
    try:
        if root.is_dir() and _add_to_path(str(root), logger, "libs"):
            added.append(str(root))
    except Exception:
        pass

    # 2) 解压出来的目录 —— 含 C 扩展的包（Pillow）靠这个才能工作
    #
    # ⚠ 这一步必须**早于**挂 .whl 路径（下面第 3 步）。
    # 两者都进 sys.path 时，导入按 sys.path 顺序解析：
    #   * .whl 在前 → ``PIL`` 解析到 wheel 内部（只有纯 Python 部分），
    #                 ``_imaging`` 加载失败 → Pillow 变空壳
    #   * 解压目录在前 → 解析到解压出来的完整包，C 扩展正常加载
    # 所以顺序反了会让整套解压机制**完全失效**，而且因为
    # ``import PIL`` 仍然成功，光看"能不能导入"根本发现不了。
    unpacked_root = root / _UNPACKED_DIRNAME
    try:
        if unpacked_root.is_dir():
            for d in sorted(unpacked_root.iterdir()):
                # 跳过临时目录和清单文件
                if not d.is_dir() or d.name.endswith(".tmp"):
                    continue
                if _add_to_path(str(d), logger, d.name):
                    added.append(str(d))
    except Exception:
        pass

    # 3) libs/*.whl —— 对**没被解压的**纯 Python 包有效
    #    （zipimport 接受 .whl，挂上就用，省一次解压）。
    #    已经解压过的 whl **不再挂** —— 否则又会把 PIL 解析回 wheel 内部，
    #    把第 2 步的成果顶掉。
    unpacked_names = set()
    try:
        if unpacked_root.is_dir():
            unpacked_names = {
                d.name for d in unpacked_root.iterdir()
                if d.is_dir() and not d.name.endswith(".tmp")
            }
    except Exception:
        pass

    try:
        for whl in sorted(root.glob("*.whl")):
            if unpacked_dir_name(whl.name) in unpacked_names:
                continue
            if _add_to_path(str(whl), logger, whl.name):
                added.append(str(whl))
    except Exception:
        pass

    return added


# ------------------------------------------------------------------ 主入口

def install(libs_dir, logger: Any = None) -> List[str]:
    """扫描 libs 目录，解压其中的 .whl，并注册进 sys.path。

    返回新增到 sys.path 的路径列表（便于日志与测试断言）。

    这是插件启动时该调用的入口。**它不会抛异常** —— 任何失败都降级成
    日志警告，因为插件启动路径不能因为一个坏 whl 就崩。
    """
    added: List[str] = []
    root = _coerce_dir(libs_dir)
    if root is None:
        return added

    if not root.is_dir():
        # 目录不存在不是错误：没放任何依赖是正常状态
        return added

    root_unpacked = root / _UNPACKED_DIRNAME
    state = _load_state(root)
    new_state: dict = {}
    changed = False

    try:
        wheels = sorted(p for p in root.glob("*.whl") if p.is_file())
    except Exception as e:
        if logger is not None:
            logger.warning(f"[依赖] 扫描 {root} 失败：{e}")
        wheels = []

    for whl in wheels:
        fp = _fingerprint(whl)
        rec = state.get(whl.name) if isinstance(state.get(whl.name), dict) else {}
        dest_name = unpacked_dir_name(whl.name)
        dest = root_unpacked / dest_name

        # 命中缓存：指纹一致 + 目录还在 → 跳过解压
        if rec.get("fp") == fp and dest.is_dir():
            new_state[whl.name] = rec
            continue

        got = _unpack_one(whl, root_unpacked, logger)
        if got is None:
            # 解压失败：不记录，下次启动会重试（也许用户会换个好文件）
            continue
        new_state[whl.name] = {"fp": fp, "dir": dest_name}
        changed = True

    # 清理：whl 已被删除的，把它的解压目录一并删掉，避免旧版本残留
    for old_name, old_rec in state.items():
        if old_name in new_state or not isinstance(old_rec, dict):
            continue
        old_dir = old_rec.get("dir")
        if old_dir:
            try:
                stale = root_unpacked / str(old_dir)
                if stale.is_dir():
                    _rmtree(stale)
                    if logger is not None:
                        logger.info(f"[依赖] 已清理过期的解压目录 {old_dir}")
            except Exception:
                pass
        changed = True

    if changed:
        _save_state(root, new_state)

    added = register(root, logger)
    if added and logger is not None:
        logger.info(f"[依赖] 已从 {root} 注册 {len(added)} 个依赖路径")
    return added
