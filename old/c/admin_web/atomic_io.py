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
import tempfile
from pathlib import Path
from typing import Any, Optional

__all__ = ["atomic_write_text", "atomic_write_json", "atomic_write_bytes"]


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
    except Exception:
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
