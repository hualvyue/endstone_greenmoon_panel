"""老版本 ``write failed`` 相关修复的回归测试。

背景（这份测试存在的理由）
--------------------------------------------------------------
Windows 上服务端日志里会周期性出现：

    [*** LOG ERROR #0001 ***] [GreenmoonPanel] [xxx] write failed
    Unable to write file 'logs/latest.log': ...

根因链条（逐环已核实）：

  1. 插件旧实现在 on_enable 里起了 ``_ConsoleStreamCapture``，
     它用 ``os.dup2(w, 1)`` 把进程的 **fd 1/2** 换成一根匿名管道；
  2. Endstone 的 C++ 侧 ``ConsoleLogSink`` 在启动时**已经持有 fd 1 的
     ``FILE*``**，它以为自己还在写控制台，实际写进了这根**没人读**的管道；
  3. 管道缓冲区（Windows 上仅 4KB）一满，写就失败 →
     报 ``write failed`` / ``Unable to write file``；
  4. Windows 上还叠加了一层：BDS 的日志文件句柄在轮转时没释放，
     ``rotate()`` 里的 ``rename`` 抛 ``PermissionError``。

修复原则：**不碰 fd，不碰 C++ sink，只在 Python logging 层做加法。**

因此本测试分两层：
  * **源码契约层**（AST / 文本扫描）：确保那些「不许回来」的写法真的不在，
    且该有的组件真的在。这层挡的是「以后有人手滑改回去」。
  * **行为层**：把日志组件单独拎出来跑，验它在各种异常下都不炸、不写 stderr。

跑法： python3 test_logfix.py
"""
import ast
import importlib.util
import io
import logging
import logging.handlers
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


CORE_SRC = (HERE / "core.py").read_text(encoding="utf-8")
WEB_SRC = (HERE / "web.py").read_text(encoding="utf-8")
ATOMIC_SRC = (HERE / "atomic_io.py").read_text(encoding="utf-8")

# 把注释剥掉再做文本扫描 —— 否则「修复说明注释里提到了 dup2」会误判成「还在用 dup2」。
# ★ 只丢 COMMENT，docstring 与换行**原样保留**：否则 ``def foo`` / ``class Bar``
#   这些跨 token 的字符串会被拼断，导致「组件明明在却扫不到」的假失败。
def strip_comments(src):
    import tokenize
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        out.append(tok)
    # 用 tokenize.untokenize 原样还原（含换行/缩进）
    return tokenize.untokenize(out)


def strip_comments_and_docstrings(src):
    """撕掉注释和 docstring，只留**可执行的代码**。

    用来做「某个危险写法是不是真的不在了」这类断言：修复说明的注释里
    往往会引用 ``os.dup2(...)`` 这样的原文（比如「旧实现用 dup2 抢 fd」），
    若不清掉就会把「解释为什么不能这么做」误判成「又这么做了」。

    做法：把每个 docstring 节点换成 ``pass``，再用 AST 重新 unparse。
    """
    tree = ast.parse(src)

    def _is_docstring(node):
        return (isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str))

    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and _is_docstring(body[0]):
            body[0] = ast.Pass()
    return ast.unparse(tree)


CORE_CODE = strip_comments_and_docstrings(CORE_SRC)
WEB_CODE = strip_comments_and_docstrings(WEB_SRC)


# ---------------------------------------------------------------------------
# 1. 源码契约：fd 抢占必须彻底消失
# ---------------------------------------------------------------------------
section("1. 源码契约：fd 抢占代码必须彻底消失")

check("web.py 里没有 os.dup2（真凶）", "dup2" not in WEB_CODE)
check("web.py 里没有 _ConsoleStreamCapture", "_ConsoleStreamCapture" not in WEB_CODE)
check("web.py 里没有 _strip_ansi", "_strip_ansi" not in WEB_CODE)
check("web.py 里没有 _ANSI_RE", "_ANSI_RE" not in WEB_CODE)
check("core.py 里没有 os.dup2", "dup2" not in CORE_CODE)
check("core.py 里没有 _ConsoleStreamCapture",
      "_ConsoleStreamCapture" not in CORE_CODE)
check("core.py 不再从 web 导入 _ConsoleStreamCapture",
      "_ConsoleStreamCapture" not in CORE_CODE,
      "模块末尾的 from .web import (...) 必须已清理")

# core.py 里也不该有任何形式的 fd 重定向
for bad in ("os.dup(", "sys.stdout =", "sys.stderr ="):
    check(f"core.py 里没有 {bad}", bad not in CORE_CODE)


# ---------------------------------------------------------------------------
# 2. 源码契约：该有的组件必须在
# ---------------------------------------------------------------------------
section("2. 源码契约：替代实现必须到位")

check("core.py 有 _PanelLogHandler", "class _PanelLogHandler" in CORE_CODE)
check("core.py 有 _ResilientRotatingHandler",
      "class _ResilientRotatingHandler" in CORE_CODE)
check("core.py 有 _install_plugin_logging",
      "def _install_plugin_logging" in CORE_CODE)
check("core.py 有 _close_plugin_logging",
      "def _close_plugin_logging" in CORE_CODE)
check("导入 logging.handlers", "import logging.handlers" in CORE_CODE)

# _ResilientRotatingHandler 必须真是 RotatingFileHandler 的子类
tree = ast.parse(CORE_SRC)
rot_cls = next((n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "_ResilientRotatingHandler"), None)
check("_ResilientRotatingHandler 存在且是类", rot_cls is not None)
if rot_cls is not None:
    bases = [ast.unparse(b) for b in rot_cls.bases]
    check("它继承 logging.handlers.RotatingFileHandler",
          any("RotatingFileHandler" in b for b in bases), bases)
    methods = {n.name for n in rot_cls.body if isinstance(n, ast.FunctionDef)}
    for m in ("emit", "_reopen", "handleError", "doRollover", "_open", "_ensure_dir"):
        check(f"_ResilientRotatingHandler 覆写了 {m}", m in methods, sorted(methods))
    # ★ _open 必须被覆写：RotatingFileHandler.shouldRollover 会绕过 emit 直接调它
    check("★ 覆写了 _open（shouldRollover 绕过 emit 直调它）", "_open" in methods)

# handleError 必须是空实现（吞掉异常），否则会落到 stderr → 又像「写失败」
if rot_cls is not None:
    he = next((n for n in rot_cls.body
               if isinstance(n, ast.FunctionDef) and n.name == "handleError"), None)
    if he is not None:
        body_stmts = [s for s in he.body
                      if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        check("handleError 是空实现（不往 stderr 冒）",
              len(body_stmts) == 1 and isinstance(body_stmts[0], ast.Return), 
              ast.unparse(he))


# ---------------------------------------------------------------------------
# 3. 源码契约：_install_plugin_logging 的关键参数
# ---------------------------------------------------------------------------
section("3. 源码契约：轮转参数与 delay/errors")

check("_install_plugin_logging 用了 _ResilientRotatingHandler",
      "_ResilientRotatingHandler" in CORE_CODE)
# ★ 这几个参数直接查**原始源码**，不走 ast.unparse ——
#   unparse 会把 'utf-8' 规范化成 "utf-8"，用带引号的字面量去比对必然失败。
check("maxBytes 为 8MB（不是默认的 0=不轮转）",
      "maxBytes=8 * 1024 * 1024" in CORE_SRC)
check("backupCount=5（保留 5 个历史文件）", "backupCount=5" in CORE_SRC)
check("encoding='utf-8'", 'encoding="utf-8"' in CORE_SRC)
check("errors='replace'（乱码字节不抛异常）", 'errors="replace"' in CORE_SRC)
check("delay=True（启动不占句柄，Windows 轮转才不失败）",
      "delay=True" in CORE_SRC)
check("★ 参数写在同一个构造调用里（不是散落各处）",
      "maxBytes" in CORE_SRC and "delay=True" in CORE_SRC)

check("日志名是 greenmoon.log", "greenmoon.log" in CORE_CODE)


# ---------------------------------------------------------------------------
# 4. 源码契约：_close_plugin_logging 要卸两个 handler
# ---------------------------------------------------------------------------
section("4. 源码契约：关闭时卸干净")

close_fn = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "_close_plugin_logging":
        close_fn = node
check("找到 _close_plugin_logging", close_fn is not None)
if close_fn is not None:
    src = ast.unparse(close_fn)
    check("卸载 _plugin_log_handler", "_plugin_log_handler" in src)
    check("卸载 _panel_log_handler", "_panel_log_handler" in src)
    check("用 removeHandler 先摘除", "removeHandler" in src)
    # 顺序：removeHandler 必须在 close 之前
    i_rm = src.find("removeHandler")
    i_cl = src.find(".close()")
    check("★ 先摘除再关闭（顺序不能反）", 0 <= i_rm < i_cl, f"remove@{i_rm} close@{i_cl}")


# ---------------------------------------------------------------------------
# 5. 源码契约：跨平台系统指标
# ---------------------------------------------------------------------------
section("5. 源码契约：跨平台系统指标")

for m in ("_read_cpu_ticks_windows", "_read_mem", "_read_mem_windows", "_disk_usage_path"):
    check(f"core.py 有 {m}", f"def {m}" in CORE_CODE)

check("磁盘走 _disk_usage_path()", "_disk_usage_path()" in CORE_CODE)

# ★ 判据用 **AST**，不是字符串比对。
#   docstring 里为了解释修复原因会引用 ``shutil.disk_usage("/")``，
#   用字符串 in 会把「解释旧写法错在哪」误判成「还在用旧写法」。
HARDCODED_ROOT = [
    n for n in ast.walk(ast.parse(CORE_SRC))
    if isinstance(n, ast.Call)
    and isinstance(n.func, ast.Attribute) and n.func.attr == "disk_usage"
    and n.args
    and isinstance(n.args[0], ast.Constant) and n.args[0].value == "/"
]
check("★ 磁盘不再写死 /（改为 data_dir 所在卷）",
      not HARDCODED_ROOT, HARDCODED_ROOT)

# os.name 的 Windows 分支：同样别用字符串 in ——
# ast.unparse 会把 "nt" 规范化成 'nt'，带双引号去找必然假失败。
NT_NODES = [n for n in ast.walk(ast.parse(CORE_SRC))
            if isinstance(n, ast.Compare)
            and isinstance(n.left, ast.Attribute) and n.left.attr == "name"
            and isinstance(n.left.value, ast.Name) and n.left.value.id == "os"
            and any(isinstance(c, ast.Constant) and c.value == "nt" for c in n.comparators)]
check("有 Windows 分支（os.name == 'nt'）", len(NT_NODES) >= 2,
      f"找到 {len(NT_NODES)} 处")
check("Windows CPU 走 GetSystemTimes", "GetSystemTimes" in CORE_SRC)
check("Windows 内存走 GlobalMemoryStatusEx", "GlobalMemoryStatusEx" in CORE_SRC)
# 内存单位：/proc/meminfo 是 kB，转字节要 *1024；转 MB 要 /1048576
check("★ 内存 kB→字节 已换算（* 1024）", "* 1024" in CORE_SRC or "*1024" in CORE_SRC)
check("★ 内存转 MB 用 1048576（不再错 1024 倍）", "1048576" in CORE_SRC)


# ---------------------------------------------------------------------------
# 6. 行为层：_ResilientRotatingHandler 真的能自愈
# ---------------------------------------------------------------------------
section("6. 行为：_ResilientRotatingHandler 自愈")

# 按文件路径加载 core.py 里的两个 handler 类，绕开 endstone 依赖。
# 只取需要的类定义，其余靠 AST 抽取 + exec，避免 import 整个 core。
def load_logging_classes():
    """只抽取要用到的 handler 类，绕开 core.py 顶部的 endstone 依赖。

    关键：**别把 core.py 的 import 原样搬过来** —— 那会把
    ``from endstone.plugin import Plugin`` 也带进来，测试环境没这个包。
    这里只放这几个类真正需要的模块，其余一律丢掉。
    """
    st = ast.parse(CORE_SRC)
    keep = [n for n in st.body
            if isinstance(n, ast.ClassDef)
            and n.name in ("_ResilientFileHandler", "_ResilientRotatingHandler",
                           "_PanelLogHandler")]
    check("抽到 3 个 handler 类", len(keep) == 3, [n.name for n in keep])

    mod = ast.Module(body=keep, type_ignores=[])
    ast.fix_missing_locations(mod)
    code = compile(mod, "<core-handlers>", "exec")
    ns = {
        "logging": logging, "os": os, "sys": sys,
        "time": __import__("time"), "io": io,
        "threading": __import__("threading"),
        "collections": __import__("collections"),
        "Path": Path,
        "Any": __import__("typing").Any,
        "Callable": __import__("typing").Callable,
        "Optional": __import__("typing").Optional,
    }
    exec(code, ns)
    return ns


ns = load_logging_classes()
Rot = ns["_ResilientRotatingHandler"]
Panel = ns["_PanelLogHandler"]

tmp = Path(tempfile.mkdtemp(prefix="gm_logfix_"))
try:
    logp = tmp / "sub" / "greenmoon.log"

    h = Rot(str(logp), maxBytes=1024, backupCount=2,
            encoding="utf-8", errors="replace", delay=True)
    check("构造成功（delay=True 时还没建文件）", not logp.exists(), logp)

    logger = logging.getLogger("gm_logfix_t1")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(h)

    logger.info("第一条")
    check("首次 emit 后才创建文件（delay 生效）", logp.exists(), logp)

    # ★ 父目录不存在 / 被删掉时，必须自动补建，且**不许**往 stderr 冒
    err = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, err
    try:
        logger.info("这里会触发 shouldRollover → _open → 需要补建目录")
    finally:
        sys.stderr = real_stderr
    check("文件已落盘", logp.exists())
    check("内容写进去了", "第一条" in logp.read_text(encoding="utf-8"))
    check("★ 首次写日志没有往 stderr 冒 FileNotFoundError",
          err.getvalue() == "", err.getvalue()[:300])

    # 触发轮转：写超 1024 字节
    err2 = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, err2
    try:
        for i in range(200):
            logger.info("填充行 %d %s", i, "x" * 40)
    finally:
        sys.stderr = real_stderr
    check("轮转产生了备份文件", (tmp / "sub" / "greenmoon.log.1").exists(),
          sorted(p.name for p in (tmp / "sub").iterdir()))
    check("轮转期间没漏到 stderr", err2.getvalue() == "", err2.getvalue()[:300])

    # ★ 核心自愈：把底层文件句柄搞坏，看它是否还敢继续写且不抛
    before_stderr = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, before_stderr
    try:
        h.stream.close()
        h.stream = None
        ok = True
        for i in range(20):
            logger.info("句柄已坏后第 %d 行", i)
    except Exception as e:
        ok = False
        check("自愈后继续写不抛异常", False, e)
    finally:
        sys.stderr = real_stderr
    if ok:
        check("自愈后继续写不抛异常", True)
        check("★ 期间没有东西漏到 stderr", before_stderr.getvalue() == "",
              before_stderr.getvalue()[:200])

    h.close()

    # 目录整个不存在 → 也能自己建起来
    h2 = Rot(str(tmp / "gone" / "a.log"), maxBytes=1024, backupCount=1,
             encoding="utf-8", errors="replace", delay=True)
    logger2 = logging.getLogger("gm_logfix_t2")
    logger2.handlers.clear(); logger2.propagate = False; logger2.setLevel(logging.DEBUG)
    logger2.addHandler(h2)
    check("构造时仍不建文件", not (tmp / "gone").exists())

    err3 = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, err3
    try:
        logger2.info("建目录")
    finally:
        sys.stderr = real_stderr
    check("★ 父目录不存在时自动创建", (tmp / "gone" / "a.log").exists())
    check("★ 期间没漏到 stderr", err3.getvalue() == "", err3.getvalue()[:300])

    # 运行中被整个删掉 → 下次写要能恢复
    shutil.rmtree(tmp / "gone")
    err4 = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, err4
    try:
        logger2.info("目录被删后仍能恢复")
    finally:
        sys.stderr = real_stderr
    check("目录被删后重建成功", (tmp / "gone" / "a.log").exists())
    check("★ 目录被删期间也没漏到 stderr", err4.getvalue() == "", err4.getvalue()[:300])
    h2.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. 行为层：_PanelLogHandler 不炸、不进面板自己
# ---------------------------------------------------------------------------
section("7. 行为：_PanelLogHandler 收集日志")


class _FakePlugin:
    def __init__(self):
        self.records = []

    def _append_console_log(self, level, msg):
        # 模拟真实插件里的环形缓冲追加
        if not msg.strip():
            return
        self.records.append((level, msg))


fake = _FakePlugin()
ph = Panel(fake)
plog = logging.getLogger("gm_logfix_t3")
plog.handlers.clear(); plog.propagate = False; plog.setLevel(logging.DEBUG)
plog.addHandler(ph)
plog.info("面板该收到这条")
plog.warning("还有这条")

check("收到了 info 记录", any(r[0] == "INFO" for r in fake.records), fake.records[:3])
check("收到了 warning 记录", any(r[0] == "WARNING" for r in fake.records), fake.records[:3])
check("消息内容正确", any("面板该收到这条" in r[1] for r in fake.records))

# 插件侧抛异常时，handler 必须吞掉，不能让 logging 崩
class _Broken:
    def _append_console_log(self, level, msg):
        raise RuntimeError("故意炸")

bh = Panel(_Broken())
plog2 = logging.getLogger("gm_logfix_t4")
plog2.handlers.clear(); plog2.propagate = False; plog2.setLevel(logging.DEBUG)
plog2.addHandler(bh)
err = io.StringIO()
real_stderr, sys.stderr = sys.stderr, err
try:
    plog2.info("这条会触发异常")
    raised = False
except Exception:
    raised = True
finally:
    sys.stderr = real_stderr
check("★ 插件侧抛异常被吞掉，不冒到 logging", not raised)
check("★ 也没有漏到 stderr", err.getvalue() == "", err.getvalue()[:200])

# 空消息不该进缓冲（否则面板会被空行刷屏）
fake2 = _FakePlugin()
fh = Panel(fake2)
plog3 = logging.getLogger("gm_logfix_t5")
plog3.handlers.clear(); plog3.propagate = False; plog3.setLevel(logging.DEBUG)
plog3.addHandler(fh)
plog3.info("   ")
check("空白消息被过滤", fake2.records == [], fake2.records)


# ---------------------------------------------------------------------------
# 8. 行为层：atomic_io 的重试与原子替换
# ---------------------------------------------------------------------------
section("8. 行为：atomic_io 退避重试 / 原子替换")

spec = importlib.util.spec_from_file_location("_gm_atomic", HERE / "atomic_io.py")
atomic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(atomic)

check("导出 retry_on_lock", hasattr(atomic, "retry_on_lock"))
check("导出 atomic_replace_dir", hasattr(atomic, "atomic_replace_dir"))
check("导出 atomic_write_bytes", hasattr(atomic, "atomic_write_bytes"))
check("导出 atomic_write_text", hasattr(atomic, "atomic_write_text"))

# _is_lock_error 的判据
ile = getattr(atomic, "_is_lock_error", None)
check("有 _is_lock_error", ile is not None)
if ile is not None:
    check("PermissionError 算锁冲突", ile(PermissionError("denied")))
    e32 = OSError("sharing violation"); e32.winerror = 32
    check("winerror=32 算锁冲突", ile(e32))
    e5 = OSError("access denied"); e5.winerror = 5
    check("winerror=5 算锁冲突", ile(e5))
    check("FileNotFoundError 不算锁冲突（不该白等）", not ile(FileNotFoundError("x")))
    check("ValueError 不算锁冲突", not ile(ValueError("x")))

# retry_on_lock：非锁类异常应立即返回 False，不做无谓等待
import time as _time
t0 = _time.monotonic()
r = atomic.retry_on_lock(lambda: (_ for _ in ()).throw(FileNotFoundError("nope")))
dt = _time.monotonic() - t0
check("非锁类失败返回 False", r is False)
check("★ 非锁类失败立即返回（不白等）", dt < 0.05, f"{dt*1000:.1f} ms")

# 锁类异常应重试，最终成功
state = {"n": 0}
def flaky():
    state["n"] += 1
    if state["n"] < 3:
        raise PermissionError("busy")
    return "done"
r = atomic.retry_on_lock(flaky)
check("锁类异常会重试直到成功", r is True and state["n"] == 3, state)

# 一直锁着 → 耗尽重试后 False，且总耗时约 0.45s（短，别让用户干等）
state2 = {"n": 0}
def always_locked():
    state2["n"] += 1
    raise PermissionError("always")
t0 = _time.monotonic()
r = atomic.retry_on_lock(always_locked)
dt = _time.monotonic() - t0
check("一直锁着最终返回 False", r is False)
check("重试次数 = 1 + 4 个退避点", state2["n"] == 5, state2)
check("★ 总耗时约 0.45s（交互式面板不该让用户久等）", 0.35 < dt < 0.9, f"{dt:.3f}s")

# atomic_replace_dir：跨卷替换不留空档、不留垃圾
tmp2 = Path(tempfile.mkdtemp(prefix="gm_atomic_"))
try:
    src = tmp2 / "src"; dst = tmp2 / "dst"
    src.mkdir(); dst.mkdir()
    for i in range(200):
        (src / f"f{i}.txt").write_text(str(i))
    (dst / "old.txt").write_text("old")

    ok = atomic.atomic_replace_dir(str(src), str(dst))
    check("atomic_replace_dir 返回 True", ok is True)
    check("新内容已就位", (dst / "f199.txt").exists())
    check("旧内容已清掉", not (dst / "old.txt").exists())
    check("src 已被移走", not src.exists())
    leftovers = [p.name for p in tmp2.iterdir() if "staging" in p.name or ".old" in p.name]
    check("★ 没有 staging / .old 残留", leftovers == [], leftovers)

    # 目标不存在时也要能成功（首次安装场景）
    src2 = tmp2 / "src2"; src2.mkdir()
    (src2 / "a.txt").write_text("a")
    ok2 = atomic.atomic_replace_dir(str(src2), str(tmp2 / "brandnew"))
    check("目标不存在时也能替换", ok2 is True and (tmp2 / "brandnew" / "a.txt").exists())

    # atomic_write_bytes 落盘正确
    p = tmp2 / "blob.bin"
    check("atomic_write_bytes 成功", atomic.atomic_write_bytes(p, b"\x00\x01\xff") is True)
    check("字节内容无误", p.read_bytes() == b"\x00\x01\xff")
    # 同目录不应残留临时文件
    tmps = [q.name for q in tmp2.iterdir() if q.name.startswith(".tmp") or ".tmp" in q.name]
    check("★ 原子写不留临时文件", tmps == [], tmps)
finally:
    shutil.rmtree(tmp2, ignore_errors=True)


# ---------------------------------------------------------------------------
section("9. 源码契约：文件管理接口走原子写")
# ---------------------------------------------------------------------------
bad_write = []
for name in ("_handle_api_files_write", "_handle_api_mod_upload", "_handle_api_gmods_upload"):
    fn = next((n for n in ast.walk(ast.parse(WEB_SRC))
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if fn is None:
        check(f"找到 {name}", False)
        continue
    src = ast.unparse(fn)
    uses = ("atomic_write_bytes" in src or "atomic_write_text" in src)
    check(f"{name} 使用原子写", uses)

check("web.py 导入了 atomic_io",
      "from .atomic_io import" in WEB_SRC)

for name in ("_handle_api_files_delete", "_handle_api_files_rename",
             "_handle_api_files_move", "_handle_api_files_copy"):
    fn = next((n for n in ast.walk(ast.parse(WEB_SRC))
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if fn is None:
        check(f"找到 {name}", False)
        continue
    src = ast.unparse(fn)
    check(f"{name} 走 retry_on_lock / atomic_replace_dir",
          "retry_on_lock" in src or "atomic_replace_dir" in src)


# ---------------------------------------------------------------------------
print()
print("=" * 74)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
