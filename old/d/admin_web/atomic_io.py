"""原子文件写入工具。

背景
----
项目里原先大量使用 ``path.write_text(json.dumps(...))`` 直接覆盖目标文件。
这种写法在**写入过程中崩溃/断电/被杀进程**时，会留下一个被截断的
半成品文件 —— 对 JSON 来说就是语法错误，下次读取直接抛异常：

* ``world_behavior_packs.json`` 写坏 → 该存档所有模组失效，玩家进不去世界
* ``bindings.json`` / 签到库写坏 → 玩家绑定与签到记录全部丢失
* ``config.json`` 写坏 → 面板配置回退默认值

``os.replace`` 在同一文件系统内是**原子操作**：要么看到旧文件，
要么看到完整的新文件，不存在「看到一半」的中间态。

用法
----
    from .atomic_io import atomic_write_text, atomic_write_json

    atomic_write_json(path, data, indent=2)
    atomic_write_text(path, "raw content")

两个函数都返回 ``bool``（成功与否），且**不会抛异常** —— 调用方
如果原来在 ``except`` 里静默处理，可以无缝替换。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = ["atomic_write_text", "atomic_write_json", "atomic_write_bytes",
           "retry_on_lock", "atomic_replace_dir"]


# Windows 上杀软实时扫描、索引器（SearchIndexer）、以及我们自己刚关掉的
# 日志句柄，都会造成「文件短暂被占用」。这类失败**重试几次就好了**，
# 但如果不重试，用户会看到莫名其妙的「拒绝访问」。
#
# 退避序列刻意做得短（总耗时 ~0.45s）：面板是交互式的，让用户等太久
# 比直接报错更糟。真正长期的占用（比如另一个程序一直开着这个文件）
# 重试也救不回来，早点失败让用户看到原因反而更好。
_RETRY_DELAYS = (0.02, 0.05, 0.10, 0.28)


def _is_lock_error(exc: BaseException) -> bool:
    """判断异常是不是「文件被占用」这一类可重试的错误。

    Windows 上是 ``PermissionError`` (WinError 32/5)，Linux 上对应
    ``ETXTBSY`` / ``EACCES``。不做平台判断，直接看异常类型 ——
    ``PermissionError`` 是 ``OSError`` 的子类，两个平台都会抛。
    """
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        # WinError 32 = ERROR_SHARING_VIOLATION，33 = ERROR_LOCK_VIOLATION
        code = getattr(exc, "winerror", None)
        if code in (5, 32, 33):
            return True
        if getattr(exc, "errno", None) in (11, 13, 16, 26):   # EAGAIN/EACCES/EBUSY/ETXTBSY
            return True
    return False


def retry_on_lock(func: Callable, *args, **kwargs):
    """执行 ``func``，遇到「文件被占用」类错误自动退避重试。

    返回值语义与其它函数一致：成功返回 ``True``，耗尽重试返回 ``False``。
    非锁类异常（比如路径不存在）**不重试**，立即返回 False —— 那类错误
    重试也不会变好，只会平白让用户多等半秒。
    """
    last: Optional[BaseException] = None
    for i in range(len(_RETRY_DELAYS) + 1):
        try:
            func(*args, **kwargs)
            return True
        except Exception as e:                      # noqa: BLE001
            last = e
            if not _is_lock_error(e) or i >= len(_RETRY_DELAYS):
                break
            try:
                time.sleep(_RETRY_DELAYS[i])
            except Exception:
                break
    return False


def atomic_replace_dir(src, dst) -> bool:
    """用 ``src`` 的内容**原子地**替换目录 ``dst``。

    ★ 为什么不能直接 ``shutil.move(src, dst)``：

      * 目标已存在时，``shutil.move`` 内部的行为是「尽可能 rename，
        失败则 copytree + rmtree」。跨卷时就走后者 —— 于是 ``dst`` 会先被
        递归删掉、再从 ``src`` 拷回来，中间有一个**窗口期 dst 完全不存在**。
        这个窗口里如果服务器在读这个目录（比如正在加载资源包），就会失败；
        如果进程在窗口里被杀掉，用户的数据就**永久丢失**；
      * Windows 上 ``shutil.move`` 遇到目标是已存在目录时，还可能
        直接抛 ``PermissionError``。

    本实现分三步，把「破坏性」的那一步压缩到一次 ``os.replace``：

      1. 先把 ``src`` 挪成 ``dst`` 的同级 staging 兄弟目录（同卷则瞬间完成）；
      2. 把旧 ``dst`` 让位成 ``<dst>.old-<ts>``（同样是单次 rename）；
      3. ``os.replace(staging, dst)`` —— **同卷下这一步是原子的**。

    任一步失败都会尽力回滚（把 .old 挪回来），保证不会出现
    「旧的没了、新的也没到」的空档。
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        return False
    if not dst.exists():
        # 目标不存在：直接 rename 即可，本身就是原子的
        try:
            os.replace(str(src), str(dst))
            return True
        except Exception:
            return retry_on_lock(shutil.move, str(src), str(dst))

    staging = dst.parent / ("." + dst.name + ".staging")
    old = dst.parent / (dst.name + ".old")
    # 清掉上一次失败留下的残骸
    for junk in (staging, old):
        try:
            if junk.exists():
                if junk.is_dir():
                    shutil.rmtree(str(junk), ignore_errors=True)
                else:
                    junk.unlink()
        except Exception:
            pass

    def _move_src_to_staging():
        shutil.move(str(src), str(staging))

    if not retry_on_lock(_move_src_to_staging):
        return False

    # 旧目录让位（单次 rename）。失败则把 staging 挪回 src，不破坏现状。
    try:
        os.replace(str(dst), str(old))
    except Exception:
        try:
            shutil.move(str(staging), str(src))
        except Exception:
            pass
        return retry_on_lock(os.replace, str(dst), str(old))

    # 关键一步：把 staging 变成 dst
    try:
        os.replace(str(staging), str(dst))
    except Exception:
        # 回滚：旧目录挪回来
        try:
            os.replace(str(old), str(dst))
        except Exception:
            pass
        try:
            shutil.move(str(staging), str(src))
        except Exception:
            pass
        return False

    # 成功后清理旧目录（清不掉也不影响正确性，只是占点空间）
    try:
        if old.exists():
            shutil.rmtree(str(old), ignore_errors=True)
    except Exception:
        pass
    return True



def atomic_write_bytes(path, data: bytes, backup: bool = False) -> bool:
    """把 ``data`` 原子地写入 ``path``。

    实现：先写同目录下的临时文件 → ``fsync`` 刷盘 → ``os.replace`` 覆盖。
    临时文件必须和目标**同目录**，否则 ``os.replace`` 会退化成跨设备拷贝，
    失去原子性（这是常见的实现错误）。

    ``backup=True`` 时先把原文件复制成 ``<name>.bak``，用于世界配置这类
    「写坏了后果很严重、且用户希望能手工回滚」的场景。
    """
    p = Path(path)
    tmp_name: Optional[str] = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)

        if backup and p.is_file():
            try:
                bak = p.with_suffix(p.suffix + ".bak")
                # 用 copyfile 而不是 copy2：不需要保留元数据，且更快
                with open(p, "rb") as src, open(bak, "wb") as dst:
                    dst.write(src.read())
            except Exception:
                # 备份失败不应阻断写入（否则用户反而写不进去了）
                pass

        fd, tmp_name = tempfile.mkstemp(
            prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent)
        )
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())     # 确保数据真的落盘再替换
            except Exception:
                pass
        os.replace(tmp_name, str(p))
        tmp_name = None                  # 已被 replace 消费掉
        return True
    except Exception as e:
        # os.replace 在 Windows 上可能因为临时时间内目标被占用而失败
        # （杀软扫描刚写好的临时文件是最常见的）。退避重试一次。
        if tmp_name and _is_lock_error(e):
            if retry_on_lock(os.replace, tmp_name, str(p)):
                tmp_name = None
                return True
        return False
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except Exception:
                pass


def atomic_write_text(path, text: str, encoding: str = "utf-8",
                      backup: bool = False) -> bool:
    """把文本原子地写入 ``path``。"""
    try:
        payload = str(text).encode(encoding)
    except Exception:
        return False
    return atomic_write_bytes(path, payload, backup=backup)


def atomic_write_json(path, data: Any, encoding: str = "utf-8",
                      indent: int = 2, ensure_ascii: bool = False,
                      backup: bool = False, **dump_kwargs) -> bool:
    """把 ``data`` 序列化成 JSON 并原子写入 ``path``。

    序列化失败（如含不可序列化对象）返回 False，不会写坏原文件。
    """
    try:
        text = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii,
                          **dump_kwargs)
    except Exception:
        return False
    if not text.endswith("\n"):
        text += "\n"
    return atomic_write_text(path, text, encoding=encoding, backup=backup)
