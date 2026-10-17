"""主线程调度包装。

背景
----
``GreenMoonPlugin._run_in_server_thread(func, ...)`` 用 ``(False, msg)`` 二元组表示失败，
但 ``func`` 自己也可能合法地返回二元组（项目里就有 ``(xuid, ip)`` 这样的真实调用），
所以「看第一个元素是不是 False」这种判定方式**会误判**。

做法
----
让闭包自己吞掉异常、且**恒定返回 True**，于是：

* 调度层成功 → ``_run_in_server_thread`` 必然返回 ``True``
* 调度层失败（超时 / 繁忙 / 主线程无返回）→ 必然返回 ``(False, msg)``

调用侧只需 ``ret is not True`` 就能无歧义区分，业务返回值放在闭包里传递。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from ._context import require_plugin
from ._errors import GmError

__all__ = ["call", "run", "run_async"]


def call(fn: Callable[..., Any], *args: Any, timeout: float = 5.0,
         default: Any = None, raise_on_error: bool = False, **kwargs: Any) -> Any:
    """在主线程执行 ``fn`` 并返回其结果。

    参数
    ----
    timeout
        等待主线程执行的秒数。超时返回 ``default``。
    default
        失败时的返回值（``raise_on_error=False`` 时生效）。
    raise_on_error
        为 True 时，失败改为抛 ``GmError``。

    返回
    ----
    ``fn`` 的返回值；失败时返回 ``default``。
    """
    plugin = require_plugin()
    box: dict = {}

    def _wrapped() -> bool:
        try:
            box["ok"] = fn(*args, **kwargs)
        except Exception as e:            # 业务异常关在闭包里，不污染调度层返回值
            box["err"] = e
        return True                        # 恒定 True：调度层只看「跑没跑到」

    ret = plugin._run_in_server_thread(_wrapped, timeout=timeout)

    # 闭包根本没被执行到（超时 / 信号量繁忙 / 调度失败）
    if ret is not True and "ok" not in box and "err" not in box:
        msg = "主线程调度失败"
        if isinstance(ret, tuple) and len(ret) == 2:
            msg = str(ret[1])
        elif isinstance(ret, str):
            msg = ret
        if raise_on_error:
            raise GmError(msg)
        try:
            plugin.logger.warning(f"[gm] {msg}")
        except Exception:
            pass
        return default

    if "err" in box:
        err = box["err"]
        if raise_on_error:
            raise GmError(str(err)) from err
        try:
            plugin.logger.error(f"[gm] 主线程任务异常: {err}")
        except Exception:
            pass
        return default

    return box.get("ok", default)


def call_checked(fn: Callable[..., Any], *args: Any, timeout: float = 5.0,
                 **kwargs: Any) -> Any:
    """同 ``call``，但失败直接抛 ``GmError``。"""
    return call(fn, *args, timeout=timeout, raise_on_error=True, **kwargs)


def run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在当前线程直接执行 ``fn``（不做主线程调度）。

    适合纯读操作：读取的是主插件维护的缓存/快照，本身已加锁。
    """
    require_plugin()
    return fn(*args, **kwargs)


def run_async(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> threading.Thread:
    """把 ``fn`` 丢到后台线程执行，立即返回线程对象。

    适合子插件里那些会阻塞的操作（同步 HTTP、大文件 IO），
    避免卡住调用方自己的线程。
    """
    require_plugin()

    def _target() -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            pass

    t = threading.Thread(target=_target, name="gm-async", daemon=True)
    t.start()
    return t


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Optional[Any]:
    """尽力在主线程执行；失败静默返回 None（不抛异常）。

    适合事件回调、通知这类「跑不到就算了」的场景。
    """
    try:
        return call(fn, *args, timeout=3.0, default=None, **kwargs)
    except Exception:
        return None
