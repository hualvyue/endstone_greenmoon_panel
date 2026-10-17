"""Windows 日志写失败（write failed）修复验证。

复现 Windows 上的三类失效场景，确认新实现能自愈且不污染 stderr。

跑法： python3 test_win_logging.py
"""
import ast
import logging
import logging.handlers          # ★ core.py 里的 _ResilientRotatingHandler 继承自它
import os
import shutil
import sys
import tempfile
import threading
from io import StringIO
from pathlib import Path

CORE = "/workspace/greenmoon_mods/admin_web/core.py"
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


# ---------------------------------------------------------------- 载入被测代码
def load():
    src = open(CORE, encoding="utf-8").read()
    tree = ast.parse(src)

    # 取模块级的两个 handler 类。
    # ★ 两个都要：_install_plugin_logging 现在装配的是
    #   _ResilientRotatingHandler（见下面的 ★ 说明），
    #   _ResilientFileHandler 是保留给极端环境的另一条路径，这里一并带上，
    #   免得以后有人切回去又忘了改测试。
    handler_classes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name in ("_ResilientFileHandler", "_ResilientRotatingHandler")
    ]
    assert len(handler_classes) == 2, \
        f"应抽出 2 个 handler 类，实际 {[c.name for c in handler_classes]}"

    # 取 GreenMoonPlugin 里的日志方法。
    # ★ 不要再把 _rotate_log_files 加回来：v6.16.41 起轮转交给
    #   RotatingFileHandler 按大小做，那个手写的 rename 滚动已删除
    #   （它正是 Windows 上 PermissionError 的来源）。
    plugin = [n for n in tree.body
              if isinstance(n, ast.ClassDef) and n.name == "GreenMoonPlugin"][0]
    wanted = {"_install_plugin_logging", "_close_plugin_logging",
              "_report_log_error"}
    methods = [n for n in plugin.body
               if isinstance(n, ast.FunctionDef) and n.name in wanted]

    # 组装一个最小的宿主类
    #
    # ★ 类属性也要带上：新代码用 self._log_error / self._data_dir_error 记账，
    #   真实插件类里有默认值，假宿主没有就会 AttributeError。
    #   （被测方法里已尽量用 getattr，但类属性补齐更贴近真实运行环境。）
    cls_attrs = []
    for n in plugin.body:
        if isinstance(n, ast.Assign):
            tgt = n.targets[0] if n.targets else None
            if isinstance(tgt, ast.Name) and tgt.id in (
                    "_log_error", "_data_dir_error", "_mapart_migrated"):
                cls_attrs.append(n)

    host = ast.ClassDef(
        name="_Host",
        bases=[],
        keywords=[],
        body=[ast.Expr(value=ast.Constant(value="..."))] + cls_attrs + methods,
        decorator_list=[],
    )
    module = ast.Module(body=handler_classes + [host], type_ignores=[])
    ast.fix_missing_locations(module)

    ns = {
        "os": os, "logging": logging, "threading": threading,
        "tempfile": tempfile, "Path": Path, "Any": object,
        "Optional": object, "Dict": dict, "List": list,
        # 被测方法会用 __file__ 定位插件目录，exec 环境里补上
        "__file__": CORE,
    }
    exec(compile(module, CORE, "exec"), ns)
    return ns["_Host"], ns["_ResilientRotatingHandler"]


Host, Resilient = load()


def mk_host(data_dir, logs=None):
    h = Host()
    h.data_dir = Path(data_dir)
    h._plugin_log_handler = None
    h._log_error = None
    h._data_dir_error = None

    class L:
        def __init__(self):
            self.lines = logs if logs is not None else []
            self.handlers = []

        def info(self, m):
            self.lines.append(("I", m))
            self._fan(m)

        def warning(self, m):
            self.lines.append(("W", m))
            self._fan(m)

        def error(self, m):
            self.lines.append(("E", m))
            self._fan(m)

        def _fan(self, m):
            for hh in list(self.handlers):
                try:
                    rec = logging.LogRecord("gm", logging.INFO, "", 0, m, None, None)
                    hh.emit(rec)
                except Exception:
                    pass

        def addHandler(self, hh):
            self.handlers.append(hh)

        def removeHandler(self, hh):
            if hh in self.handlers:
                self.handlers.remove(hh)

    h.logger = L()
    return h


# ================================================================ 测试
print("=" * 74)
print("1. 基础：能正常写日志")
print("=" * 74)
tmp = tempfile.mkdtemp()
h = mk_host(tmp)
h._install_plugin_logging(h.logger)
logfile = Path(tmp) / "greenmoon.log"
check("日志文件已创建", logfile.is_file(), logfile)
check("安装记录已打印", any("插件日志已保存到" in m for _, m in h.logger.lines))
check("handler 已注册", len(h.logger.handlers) == 1)
h.logger.info("测试消息一")
content = logfile.read_text(encoding="utf-8")
check("消息已落盘", "测试消息一" in content, repr(content[-80:]))

print()
print("=" * 74)
print("2. ★ Windows 场景 A：日志文件被外部删除后继续写")
print("=" * 74, "  （Linux 上句柄还在能写孤儿 inode；Windows 会直接失败）")
logfile.unlink()
err = StringIO()
old = sys.stderr
sys.stderr = err
try:
    h.logger.info("文件被删后的消息")
finally:
    sys.stderr = old
check("★ 不往 stderr 打 '--- Logging error ---'",
      "Logging error" not in err.getvalue(), repr(err.getvalue()[:150]))
check("★ 文件被自动重建", logfile.is_file(), logfile)
check("★ 消息被恢复写入", "文件被删后的消息" in logfile.read_text(encoding="utf-8"))

print()
print("=" * 74)
print("3. ★ Windows 场景 B：数据目录被整体删除后自愈")
print("=" * 74, "  （模拟 Endstone 重建 plugins/ 下的插件目录）")
tmp2 = tempfile.mkdtemp()
# ★ 用真实插件目录名（中文），顺带覆盖中文路径
sub = Path(tmp2) / "GreenMoon面板"
h2 = mk_host(sub)
h2._install_plugin_logging(h2.logger)
check("初始创建成功", (sub / "greenmoon.log").is_file())

# 模拟整个数据目录被删掉重建
shutil.rmtree(sub)
err = StringIO()
old = sys.stderr
sys.stderr = err
try:
    h2.logger.info("目录被删后")
finally:
    sys.stderr = old
check("★ 父目录不存在时静默不报错",
      "Logging error" not in err.getvalue(), repr(err.getvalue()[:150]))
check("★ 目录被自动重建", (sub / "greenmoon.log").is_file())
check("★ 消息成功写入", "目录被删后" in (sub / "greenmoon.log").read_text(encoding="utf-8"))
check("★ 日志确实落在 data_dir 内（不是代码目录）",
      (sub / "greenmoon.log").is_file()
      and not (Path(CORE).resolve().parent / "greenmoon.log").exists())

print()
print("=" * 74)
print("4. ★ Windows 场景 C：插件重载（不 rename 打开中的文件）")
print("=" * 74)
print("   旧实现每次 on_enable 都 os.rename(base, .1)，Windows 上旧句柄还没")
print("   释放 → PermissionError。新实现改用 RotatingFileHandler（按大小轮转，")
print("   重启时**追加**），因此重载不再碰 rename。")
tmp3 = tempfile.mkdtemp()
h3 = mk_host(tmp3)
h3._install_plugin_logging(h3.logger)
h3.logger.info("第一次运行的日志")
check("首次写入 OK", "第一次运行的日志" in (Path(tmp3) / "greenmoon.log").read_text(encoding="utf-8"))

# 模拟插件重载：再次 on_enable
h3._install_plugin_logging(h3.logger)
h3.logger.info("第二次运行的日志")
base3 = Path(tmp3) / "greenmoon.log"
check("★ 重载后只有一个 handler（旧句柄已释放）",
      len(h3.logger.handlers) == 1, len(h3.logger.handlers))
check("★ 重载不产生 .1（不再 rename 打开中的文件）",
      not (Path(tmp3) / "greenmoon.log.1").exists(),
      sorted(p.name for p in Path(tmp3).iterdir()))
content3 = base3.read_text(encoding="utf-8")
check("★ 两次运行的日志都在同一个文件里（追加而非覆盖）",
      "第一次运行的日志" in content3 and "第二次运行的日志" in content3,
      repr(content3[-120:]))

# 再重载两次，handler 不该泄漏
h3._install_plugin_logging(h3.logger)
h3.logger.info("第三次")
h3._install_plugin_logging(h3.logger)
h3.logger.info("第四次")
check("★ 多次重载后仍是 1 个 handler（不泄漏）",
      len(h3.logger.handlers) == 1, len(h3.logger.handlers))
final3 = base3.read_text(encoding="utf-8")
check("★ 四次运行的日志累积完整",
      all(f"第{n}次" in final3 for n in ("一", "二", "三", "四")),
      repr(final3[-160:]))

# ★ 真正的轮转是**按大小**触发的（8MB）。这里把小阈值手搭一个 handler
#   验证「写满就滚 .1」，而不是等 8MB。
print()
print("   ── 按大小轮转（新机制）──")
rot_dir = Path(tempfile.mkdtemp())
rot_handler = Resilient(
    str(rot_dir / "greenmoon.log"), maxBytes=2048, backupCount=2,
    encoding="utf-8", errors="replace", delay=True)
rot_logger = logging.getLogger("gm_win_rot")
rot_logger.handlers.clear()
rot_logger.propagate = False
rot_logger.setLevel(logging.DEBUG)
rot_logger.addHandler(rot_handler)
rot_logger.info("轮转前的内容")
for i in range(100):
    rot_logger.info("填充 %d %s", i, "y" * 40)
check("★ 写满 maxBytes 后滚出 .1", (rot_dir / "greenmoon.log.1").is_file(),
      sorted(p.name for p in rot_dir.iterdir()))
check("★ 回滚份数不超过 backupCount（不无限增长）",
      not (rot_dir / "greenmoon.log.3").exists(),
      sorted(p.name for p in rot_dir.iterdir()))
rot_handler.close()

print()
print("=" * 74)
print("5. ★ 文件被独占占用时（Windows 典型）不崩")
print("=" * 74)
tmp4 = tempfile.mkdtemp()
h4 = mk_host(tmp4)
h4._install_plugin_logging(h4.logger)
# 另开一个句柄模拟占用
holder = open(Path(tmp4) / "greenmoon.log", "a", encoding="utf-8")
err = StringIO()
old = sys.stderr
sys.stderr = err
try:
    for i in range(20):
        h4.logger.info(f"占用期间的日志 {i}")
finally:
    sys.stderr = old
    holder.close()
check("★ 文件被占用时写 20 条不报错",
      "Logging error" not in err.getvalue(), repr(err.getvalue()[:200]))
check("★ 占用期间的日志被写入（自愈成功）",
      "占用期间的日志 0" in (Path(tmp4) / "greenmoon.log").read_text(encoding="utf-8"))

print()
print("=" * 74)
print("6. ★ data_dir 不可写时：明确报错，不回退")
print("=" * 74, "  （v6.16.39：删除了回退到代码目录的分支）")
h5 = mk_host("/proc/nonexistent/forbidden/path")
h5._install_plugin_logging(h5.logger)
check("★ data_dir 不可写时不抛异常", True)
check("★ 已明确报错（不再静默回退）", h5._log_error is not None, h5._log_error)
check("★ 未装 handler（宁可不写也不换地方）", h5._plugin_log_handler is None)
check("★ 没有回退到代码目录（无 greenmoon.log）",
      not (Path(CORE).resolve().parent / "greenmoon.log").exists())
check("★ 未回退到代码目录（无 .1/.2 残留）",
      not (Path(CORE).resolve().parent / "greenmoon.log.1").exists())

print()
print("=" * 74)
print("6b. ★ data_dir 为 None 时（data_folder 解析失败）明确报错")
print("=" * 74)
h5b = mk_host(tempfile.mkdtemp())
h5b.data_dir = None
h5b._data_dir_error = "无法获取插件数据目录（AttributeError: ...）"
h5b._install_plugin_logging(h5b.logger)
check("★ 不抛异常", True)
check("★ _log_error 已设置", h5b._log_error is not None, h5b._log_error)
check("★ 复用了 data_folder 的原始诊断（上游根因不丢）",
      "无法获取插件数据目录" in str(h5b._log_error))
check("★ 未装 handler", h5b._plugin_log_handler is None)

print()
print("=" * 74)
print("6c. ★ 中文目录名 GreenMoon面板 冒烟")
print("=" * 74)
tmp_cn = tempfile.mkdtemp()
cn_dir = Path(tmp_cn) / "GreenMoon面板"
h_cn = mk_host(cn_dir)
h_cn._install_plugin_logging(h_cn.logger)
check("★ 中文目录能创建", cn_dir.is_dir(), cn_dir)
check("★ 中文目录下日志文件已创建", (cn_dir / "greenmoon.log").is_file())
h_cn.logger.info("中文路径消息")
check("★ 中文目录下能写入",
      "中文路径消息" in (cn_dir / "greenmoon.log").read_text(encoding="utf-8"))
check("★ 中文目录下无报错", h_cn._log_error is None, h_cn._log_error)

print()
print("=" * 74)
print("7. ★ 并发写不丢日志、不报错")
print("=" * 74)
tmp6 = tempfile.mkdtemp()
h6 = mk_host(tmp6)
h6._install_plugin_logging(h6.logger)
err = StringIO()
old = sys.stderr
sys.stderr = err


def worker(n):
    for i in range(50):
        h6.logger.info(f"线程{n}-消息{i}")


ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
for t in ts:
    t.start()
for t in ts:
    t.join()
sys.stderr = old
txt = (Path(tmp6) / "greenmoon.log").read_text(encoding="utf-8")
got = sum(1 for line in txt.splitlines() if "线程" in line)
check("★ 8 线程 × 50 条全部写入", got == 400, f"实际 {got}")
check("★ 并发下无 logging error", "Logging error" not in err.getvalue())

print()
print("=" * 74)
print("8. 关闭后不再写（避免句柄泄漏）")
print("=" * 74)
tmp7 = tempfile.mkdtemp()
h7 = mk_host(tmp7)
h7._install_plugin_logging(h7.logger)
h7._close_plugin_logging()
check("★ close 后 handler 引用已清空", h7._plugin_log_handler is None)
check("★ close 后 logger 里已移除 handler", len(h7.logger.handlers) == 0)
h7.logger.info("关闭后的消息")
check("★ 关闭后不再写入文件",
      "关闭后的消息" not in (Path(tmp7) / "greenmoon.log").read_text(encoding="utf-8"))

print()
print("=" * 74)
print("9. ★★ 反向断言：绝不往代码目录写任何东西（锁死回归）")
print("=" * 74, "  （这是本轮改造的核心目标）")
code_dir = Path(CORE).resolve().parent
stray_logs = sorted(p.name for p in code_dir.glob("greenmoon.log*"))
stray_mapart = (code_dir / "mapart_data").exists()
check("★ 代码目录下无 greenmoon.log*（一个都不能有）",
      not stray_logs, stray_logs)
check("★ 代码目录下无 mapart_data/（地图画不再放插件包内）",
      not stray_mapart)
check("★ 代码目录下无 .gmlog_ 探针残留",
      not list(code_dir.glob(".gmlog_*")))

print()
print("=" * 74)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
