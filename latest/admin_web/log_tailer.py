"""控制台日志采集：把 BDS 的 latest.log 逐行喂给正则引擎。

为什么需要这个模块
------------------
插件的面板日志（``_append_console_log``）**拿不到 BDS 原生日志**。
全库只有 3 处 Python logging 站点会往里写：

  · ``core.py`` ``_PanelLoggerAdapter.emit``
  · ``core.py`` ``_PanelLogHandler.emit``
  · ``core.py`` ``_log_admin_action``

而真正的原生输出走的是 C++ 侧的 ``ConsoleLogSink`` / ``FileLogSink``，
**完全绕过 Python 解释器**。所以「钩一下缓冲区」这种方案从根上不成立。

曾经有过用 ``os.dup2`` 重定向文件描述符的做法，被明确禁用了 ——
在 Windows 上会让 BDS 打不出日志并抛 ``[*** LOG ERROR #0001 ***]``
（见 core.py 里那段警告注释）。**不要退回去。**

唯一安全且可靠的路子是**读文件**：``logs/latest.log`` 就在
``server.properties`` 旁边，纯 Python 文件 IO，不碰任何原生资源。

设计要点（每一条都对应一个真实会踩的坑）
----------------------------------------
1. ``errors="replace"`` —— BDS 会在日志里写**被截断的 UTF-8**
   （比如 0xe4 后面没有续字节）。不加这个参数直接 UnicodeDecodeError，
   整个采集线程死掉，而现象是「日志规则忽然不工作了」，极难定位。
2. **增量读**：从上次 offset 继续，绝不整文件重读。日志能到几十 MB。
3. **只读新行**：首次启动时从**文件末尾**开始（``start_at_end=True``），
   否则一开启就把历史几百行全灌进规则，瞬间刷屏。
4. **半行缓冲**：读到没有 ``\n`` 结尾的尾巴要留着，等下次补齐再发 ——
   否则一行被劈成两条规则事件。
5. **轮转检测**：日志按大小/日期轮转后是同名新文件。用
   ``(size, mtime)`` 识别「文件变小了」= 轮转，offset 归零重读。
6. **静默降级**：文件不在、目录找不到、读失败 —— 一律安静跳过，
   不抛异常、不刷错误日志。缺少日志文件是完全正常的情况
   （比如服主禁用了文件日志）。

线程模型
--------
本模块自己的采集线程是**唯一**读文件的人，读到的行通过 ``on_line``
回调交出去。回调**必须在后台线程执行**（网络发送、匹配都在里面），
所以这里不碰主线程任何状态，也不可能造成
``executeCommand`` 那种主线程死锁。
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

#: 一次最多读多少字节，防止日志暴涨时一次读进几百 MB 把内存顶爆。
#: 每次轮询读满这么多就交给下一轮继续，采集会稍微滞后但不会炸。
_MAX_READ_BYTES = 512 * 1024

#: 轮询间隔（秒）。日志不是实时性要求高的东西，1 秒足够。
_POLL_INTERVAL = 1.0

#: 单行长度上限。超长行（比如插件 dump 出来的巨型 JSON）截断，
#: 免得下游正则和时间戳解析在超长串上白烧 CPU。
_MAX_LINE_LEN = 8192

# ---------------------------------------------------------------------------
# 日志行解析
#
# 真实格式（取自线上冷启动日志）：
#     [02:12:24] [Server/ERROR]: xxx
#     [02:12:04] [Main/INFO]: Loading Endstone...
#
# 注意级别标记是**两段式** ``[source/LEVEL]``，不是一个 ``[ERROR]``。
# 所以不能简单地找 ``[`` 开头的 token —— 那样会把时间戳也当成级别。
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^\[(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)\]\s*")
#: 匹配 ``[来源/级别]:`` （来源可含空格，级别是纯字母）
_MARKER_RE = re.compile(r"^\[([^\[\]/]*)\s*/\s*([A-Za-z]+)\]\s*:?\s*")

#: BDS / 插件的级别别名 → 归一化级别。
#: 覆盖 ``WARN``/``WARNING``、``SEVERE``/``FATAL`` 这类不同写法，
#: 否则用户的 level == "ERROR" 条件会漏掉 ``SEVERE`` 的行。
_LEVEL_ALIAS = {
    "TRACE": "TRACE",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "INFORMATION": "INFO",
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "SEVERE": "ERROR",
    "FATAL": "FATAL",
    "CRITICAL": "FATAL",
}


def parse_log_line(line: str) -> Optional[Dict[str, Any]]:
    """把一行原始日志解析成 pack 字段；无法解析返回 ``None``。

    返回的 dict 含：
      ``time``          ``"02:12:24"`` 或 ``""``
      ``source``        ``"Server"`` / ``"Main"`` / ``""``
      ``level``         归一化大写级别，如 ``"ERROR"``；认不出则 ``"UNKNOWN"``
      ``raw_message``   去掉时间戳与级别标记之后的**正文**
      ``raw_line``      原始整行（排障时想看原貌）

    为什么把 level 拆成独立字段而不是让用户在 pattern 里写
    ``^\\[Server/ERROR\\]``：转义容易写错，而且一旦日志前缀格式变了
    （BDS 升级改过）所有规则同时失效。字段化之后，级别过滤是
    ``conditions`` 里的字符串相等，改格式只需改这一个解析函数。
    """
    s = str(line or "").rstrip("\r\n")
    if not s.strip():
        return None
    if len(s) > _MAX_LINE_LEN:
        s = s[:_MAX_LINE_LEN] + "…[已截断]"

    out: Dict[str, Any] = {
        "time": "", "source": "", "level": "UNKNOWN",
        "raw_message": s, "raw_line": s,
    }

    rest = s
    m = _TS_RE.match(rest)
    if m:
        out["time"] = m.group(1)
        rest = rest[m.end():]

    m = _MARKER_RE.match(rest)
    if m:
        out["source"] = m.group(1).strip()
        lvl = m.group(2).strip().upper()
        out["level"] = _LEVEL_ALIAS.get(lvl, "UNKNOWN")
        rest = rest[m.end():]
    else:
        # 没有标准前缀行 —— 常见于堆栈跟踪的续行（"    at xxx"）。
        # 这类行**保留**，因为报错的根因经常就写在堆栈里。
        # 级别继承交给调用方（见 LogTailer._pending_level）。
        pass

    out["raw_message"] = rest.strip()
    return out


def _looks_like_marker(line: str) -> bool:
    """这行是否带 ``[时间] [来源/级别]:`` 前缀（= 一条新日志的开始）。"""
    s = str(line or "").lstrip()
    m = _TS_RE.match(s)
    if not m:
        return False
    return _MARKER_RE.match(s[m.end():]) is not None


class LogTailer:
    """按增量方式跟随 latest.log，逐行回调。

    依赖注入而非 import 插件类：与 ``cloud.py`` 同样的约定，
    传入的 ``plugin`` 只被 **duck-typing** 使用（``logger`` 等）。
    这样本模块能脱离 Endstone 单独测试。
    """

    def __init__(self, plugin: Any, on_line: Callable[[Dict[str, Any]], None]) -> None:
        self.p = plugin
        self.logger = getattr(plugin, "logger", None)
        self._on_line = on_line

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._path: Optional[Path] = None
        self._offset = 0
        self._partial = ""            # 上次读剩的半行
        self._sig: Optional[tuple] = None   # (size, mtime) 用于轮转检测

        # 续行（无前缀的堆栈行）继承上一条的级别。
        # 这样「ERROR 后面跟着 5 行堆栈」的 5 行也带着 level=ERROR，
        # 用户的 level 过滤不会把根因那几行漏掉。
        self._pending_level = "UNKNOWN"
        self._pending_source = ""

        # 统计，供 WebUI 显示采集活力（可选消费）
        self.lines_seen = 0
        self.last_error = ""

    # ------------------------------------------------------------ 路径解析
    def resolve_path(self) -> Optional[Path]:
        """定位 ``logs/latest.log``。

        优先用宿主的 ``_find_server_root()``（它靠找 ``server.properties``
        判断服务器根目录，比瞎猜路径可靠）；拿不到就返回 None。
        """
        root = None
        finder = getattr(self.p, "_find_server_root", None)
        if callable(finder):
            try:
                root = finder()
            except Exception:
                root = None
        if root is None:
            # 退路：data_dir 往上找 logs/
            data_dir = getattr(self.p, "data_dir", None)
            if data_dir is not None:
                for cand in (Path(data_dir), Path(data_dir).parent, Path(data_dir).parent.parent):
                    p = cand / "logs" / "latest.log"
                    if p.exists():
                        return p
            return None
        p = Path(root) / "logs" / "latest.log"
        return p

    # ------------------------------------------------------------ 生命周期
    def start(self, start_at_end: bool = True) -> bool:
        """启动采集。``start_at_end=True`` 时从文件末尾开始（只跟新行）。

        返回是否成功启动。**失败不是异常** —— 没有日志文件是正常情况，
        调用方只需知道"这次没起来"。
        """
        if self._thread is not None and self._thread.is_alive():
            return True
        p = self.resolve_path()
        if p is None:
            self.last_error = "未找到服务器目录（server.properties）"
            return False
        if not p.exists():
            self.last_error = f"日志文件不存在: {p}"
            return False

        self._path = p
        try:
            size = p.stat().st_size
        except Exception as e:
            self.last_error = f"无法读取日志文件状态: {e}"
            return False
        # 只跟新行：把 offset 挪到末尾。否则一开启就把历史全灌进去。
        self._offset = size if start_at_end else 0
        self._partial = ""
        self._sig = self._stat_sig()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="gm-log-tailer", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            try:
                t.join(timeout)
            except Exception:
                pass
        self._thread = None

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    # ------------------------------------------------------------ 内部
    def _stat_sig(self) -> Optional[tuple]:
        try:
            st = self._path.stat()  # type: ignore[union-attr]
            return (st.st_size, int(st.st_mtime))
        except Exception:
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                # 采集线程绝不能因为一次异常就死掉 —— 死了以后
                # 「日志规则静默失效」，而主线程毫无察觉。
                self.last_error = str(e)
                self._log("warning", f"[日志采集] 读取异常，已跳过本轮: {e}")
            # 用 wait 而不是 sleep：停止信号能立刻打断等待
            self._stop.wait(_POLL_INTERVAL)

    def _log(self, level: str, msg: str) -> None:
        fn = getattr(self.logger, level, None)
        if callable(fn):
            try:
                fn(msg)
            except Exception:
                pass

    def _tick(self) -> None:
        if self._path is None:
            return

        # ---- 1. 轮转 / 截断检测 -------------------------------------
        sig = self._stat_sig()
        if sig is None:
            # 文件被删了（轮转中）。下一轮再试，不报错。
            return
        if self._sig is not None and sig != self._sig:
            old_size = self._sig[0]
            new_size = sig[0]
            if new_size < old_size or new_size < self._offset:
                # 变小 = 轮转/清空：从头重读
                self._offset = 0
                self._partial = ""
        self._sig = sig

        # ---- 2. 没有新内容就返回（省 IO）-----------------------------
        if sig[0] <= self._offset:
            return

        # ---- 3. 增量读 ----------------------------------------------
        try:
            with open(self._path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read(_MAX_READ_BYTES)
        except FileNotFoundError:
            return
        except Exception as e:
            self.last_error = str(e)
            return
        if not chunk:
            return
        self._offset += len(chunk)

        # errors="replace"：BDS 会写出被截断的 UTF-8 字节序列，
        # 严格解码会直接抛 UnicodeDecodeError 让采集线程死掉。
        text = self._partial + chunk.decode("utf-8", errors="replace")
        self._partial = ""

        # 末尾可能是半行，留到下一轮补齐
        if not text.endswith("\n"):
            idx = text.rfind("\n")
            if idx < 0:
                self._partial = text
                return
            self._partial = text[idx + 1:]
            text = text[:idx + 1]

        for raw in text.splitlines():
            if self._stop.is_set():
                break
            self._emit(raw)

    def _emit(self, raw: str) -> None:
        """解析并回调一行。"""
        if not raw.strip():
            return
        rec = parse_log_line(raw)
        if rec is None:
            return
        self.lines_seen += 1

        if _looks_like_marker(raw) or rec["level"] != "UNKNOWN":
            # 新日志行：更新续行继承用的级别
            self._pending_level = rec["level"]
            self._pending_source = rec["source"]
        else:
            # 续行（堆栈 / 缩进输出）：继承上一条的级别与来源，
            # 这样 ERROR 的堆栈不会被 level 过滤掉。
            rec["level"] = self._pending_level
            rec["source"] = rec["source"] or self._pending_source

        try:
            self._on_line(rec)
        except Exception as e:
            self.last_error = str(e)
            self._log("warning", f"[日志采集] 规则处理异常: {e}")


def snapshot_tail(path: Path, max_lines: int = 200) -> list:
    """读文件尾部若干行，用于 WebUI「日志预览」。

    与采集线程无关：直接打开读，用于用户点开页面时看最近发生了什么。
    编码同样用 ``errors="replace"``。
    """
    try:
        data = path.read_bytes()
    except Exception:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-max_lines:] if max_lines > 0 else lines
