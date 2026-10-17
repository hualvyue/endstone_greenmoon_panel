"""数据目录统一改造（v6.16.39）验证。

覆盖三件事：
  1. ``_resolve_data_dir`` 只认 Endstone ``data_folder``，失败返回 None
     —— **绝不**退回相对路径（这是线上日志乱写位置的根因之一）。
  2. ``_diag_summary`` 把 data_dir / log_error / packaging_error 透出，
     让面板能显示"为什么没写"。
  3. ``check_dist_name`` 能识别不合规发行名（data_folder 拿不到的上游原因）。

跑法： python3 test_datadir.py
"""
import ast
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
CORE = str(HERE / "core.py")
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


# ---------------------------------------------------------------- 载入被测代码
def load_plugin():
    """把 GreenMoonPlugin 抠出来，注入最小依赖后实例化，用于跑真实方法。"""
    src = open(CORE, encoding="utf-8").read()
    tree = ast.parse(src)
    plugin = [n for n in tree.body
              if isinstance(n, ast.ClassDef) and n.name == "GreenMoonPlugin"][0]
    wanted = {"_resolve_data_dir", "_report_log_error", "_diag_summary",
              "_mapart_root", "_mapart_migrate_old", "_mapart_index_path"}
    methods = [n for n in plugin.body
               if isinstance(n, ast.FunctionDef) and n.name in wanted]
    # 类属性：这些是缺陷记账字段，方法里会读写
    cls_attrs = [n for n in plugin.body
                 if isinstance(n, ast.Assign) and n.targets
                 and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id in ("_log_error", "_data_dir_error",
                                         "_mapart_migrated", "_MAPART_SUBDIR")]

    host = ast.ClassDef(
        name="_Host", bases=[], keywords=[],
        body=[ast.Expr(value=ast.Constant(value="..."))] + cls_attrs + methods,
        decorator_list=[])
    module = ast.Module(body=[host], type_ignores=[])
    ast.fix_missing_locations(module)

    ns = {"Path": Path, "Any": object, "Optional": Optional, "Dict": dict, "List": list,
          "sys": sys, "os": __import__("os"), "tempfile": tempfile,
          "__file__": CORE, "str": str, "bool": bool, "int": int, "round": round,
          "Thread": __import__("threading").Thread, "RLock": __import__("threading").RLock}
    exec(compile(module, CORE, "exec"), ns)
    return ns["_Host"]


Host = load_plugin()


class Logger:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(("I", m))

    def warning(self, m):
        self.lines.append(("W", m))

    def error(self, m):
        self.lines.append(("E", m))

    def addHandler(self, h):
        pass

    def removeHandler(self, h):
        pass


def mk(**kw):
    h = Host()
    h.logger = Logger()
    for k, v in kw.items():
        setattr(h, k, v)
    return h


# ================================================================ 1. data_folder
print("=" * 74)
print("1. ★ _resolve_data_dir：只认 data_folder，失败不返回相对路径")
print("=" * 74)

# 1a. 正常：data_folder 有值
class GoodFolder:
    data_folder = "/srv/bedrock/plugins/GreenMoon面板"

h = mk()
h.__class__.data_folder = property(lambda self: "/srv/bedrock/plugins/GreenMoon面板")
got = h._resolve_data_dir()
check("★ data_folder 正常时返回其路径", got == Path("/srv/bedrock/plugins/GreenMoon面板"), got)
check("★ 正常时不设置 _data_dir_error", h._data_dir_error is None)

# 1b. 失败：data_folder 抛异常（线上真实场景）
class Boom:
    @property
    def data_folder(self):
        raise AttributeError("data_folder not set")

h2 = mk()
h2.__class__ = type("H2", (Boom, Host), {})
h2.logger = Logger()
got2 = h2._resolve_data_dir()
check("★ data_folder 抛异常时返回 None（不再返回相对路径）", got2 is None, got2)
check("★ 不再返回 'plugins/endstone-greenmoon-panel/data'",
      got2 != Path("plugins/endstone-greenmoon-panel/data") and got2 is None)
check("★ _data_dir_error 已记录原因", h2._data_dir_error is not None)
check("★ 原因里带上了原始异常类型",
      "AttributeError" in str(h2._data_dir_error), h2._data_dir_error)
check("★ 控制台打了 error（用户能看到）",
      any(lv == "E" and "数据目录" in m for lv, m in h2.logger.lines))
check("★ 给出了命名排查指引（指向 dist 名）",
      any(lv == "E" and "endstone-" in m for lv, m in h2.logger.lines))

# 1c. data_folder 为空值
h3 = mk()
h3.__class__.data_folder = property(lambda self: None)
got3 = h3._resolve_data_dir()
check("★ data_folder 为空时也返回 None", got3 is None, got3)
check("★ 空值也有报错记录", h3._data_dir_error is not None, h3._data_dir_error)


# ================================================================ 2. 日志失败上报
print()
print("=" * 74)
print("2. ★ _report_log_error：失败显式化，且不刷屏")
print("=" * 74)
h4 = mk()
h4._report_log_error("磁盘满")
check("★ _log_error 已设置", h4._log_error == "磁盘满", h4._log_error)
check("★ 控制台打了 warning", any(lv == "W" for lv, _ in h4.logger.lines))
n_before = len(h4.logger.lines)
h4._report_log_error("又一条")
check("★ 第二次不覆盖（保留首个原因）", h4._log_error == "磁盘满", h4._log_error)
check("★ 第二次不再刷屏", len(h4.logger.lines) == n_before)
h5 = mk()
h5._report_log_error("")
check("★ 空消息忽略", h5._log_error is None)


# ================================================================ 3. 诊断字段
print()
print("=" * 74)
print("3. ★ _diag_summary：透出数据目录与日志故障")
print("=" * 74)
h6 = mk()
h6.data_dir = Path("/srv/plugins/GreenMoon面板")
h6._data_dir_error = None
h6._log_error = None
h6._plugin_log_handler = None
# _diag_summary 需要的运行期字段
h6._tick_lock = __import__("threading").RLock()
h6._tick_gap_ms = 50.0
h6._tick_slow_count = 0
h6._pt_pending = 0
h6._diag_events = []
h6._backup_state = {"running": False, "next_run": None}
h6._main_thread_id = 123
h6._pt_current_name = lambda: "(无)"
h6._diag_on = lambda: True
h6._diag_verbose = lambda: False
h6._cloud_cfg_raw = lambda: {}
d = h6._diag_summary()
check("★ 含 data_dir 字段", "data_dir" in d, list(d.keys()))
check("★ data_dir 值正确", d["data_dir"] == "/srv/plugins/GreenMoon面板", d.get("data_dir"))
check("★ 含 log_error 字段", "log_error" in d)
check("★ 含 log_path 字段", "log_path" in d)
check("★ 含 packaging_error 字段", "packaging_error" in d)
check("★ 健康时 log_error 为 None", d["log_error"] is None)

h7 = mk()
h7.data_dir = None
h7._data_dir_error = "无法获取插件数据目录"
h7._log_error = "日志目录不可写"
h7._plugin_log_handler = None
h7._tick_lock = __import__("threading").RLock()
h7._tick_gap_ms = 50.0
h7._tick_slow_count = 0
h7._pt_pending = 0
h7._diag_events = []
h7._backup_state = {"running": False, "next_run": None}
h7._pt_current_name = lambda: "(无)"
h7._diag_on = lambda: True
h7._diag_verbose = lambda: False
h7._cloud_cfg_raw = lambda: {}
d7 = h7._diag_summary()
check("★ 故障时 log_error 被透出", d7["log_error"] == "日志目录不可写")
check("★ 故障时 data_dir_error 被透出", d7["data_dir_error"] == "无法获取插件数据目录")
check("★ log_path 为 None（日志未启用）", d7["log_path"] is None)


# ================================================================ 4. 地图画单一来源
print()
print("=" * 74)
print("4. ★ _mapart_root：只认 data_dir，不再优先插件包目录")
print("=" * 74)
tmp = Path(tempfile.mkdtemp())
h8 = mk()
h8.data_dir = tmp / "GreenMoon面板"
h8.data_dir.mkdir(parents=True, exist_ok=True)
d8 = h8._mapart_root()
check("★ 地图画目录落在 data_dir 下",
      d8 == h8.data_dir / "mapart_data", d8)
check("★ 目录已创建", d8.is_dir())
check("★ 不在代码目录创建 mapart_data", not (HERE / "mapart_data").exists())

h9 = mk()
h9.data_dir = None
d9 = h9._mapart_root()
check("★ data_dir 为 None 时返回 None（功能降级）", d9 is None, d9)
check("★ 降级时有 warning", any(lv == "W" for lv, _ in h9.logger.lines))

# override 仍然生效（测试用）
tmp2 = Path(tempfile.mkdtemp())
h10 = mk()
h10.data_dir = tmp2 / "ignored"
h10._mapart_root_override = str(tmp2 / "over")
do = h10._mapart_root()
check("★ override 生效", do == tmp2 / "over" / "mapart_data", do)

# 迁移方法已是空实现（按交付口径不自动迁移）
h11 = mk()
h11.data_dir = tmp2
r = h11._mapart_migrate_old(tmp2 / "target")
check("★ _mapart_migrate_old 为空实现（不迁移）", r is None)
check("★ 空实现不创建任何目录", not (tmp2 / "target").exists())
check("★ 空实现不抛异常（new_dir=None 也安全）",
      h11._mapart_migrate_old(None) is None)


# ================================================================ 5. 发行名自查
print()
print("=" * 74)
print("5. ★ check_dist_name：识别不合规发行名")
print("=" * 74)
sys.path.insert(0, str(HERE))
import _packaging_check as pc  # noqa: E402

check("★ 期望名常量正确",
      pc.EXPECTED_DIST_NAME == "endstone-greenmoon-panel", pc.EXPECTED_DIST_NAME)
check("★ entry point 名常量正确",
      pc.EXPECTED_ENTRY_NAME == "greenmoon-panel", pc.EXPECTED_ENTRY_NAME)
check("★ 名字配套（dist == endstone- + entry）",
      pc.EXPECTED_DIST_NAME == "endstone-" + pc.EXPECTED_ENTRY_NAME)

check("★ 下划线归一化：endstone_greenmoon_panel 视为合规",
      pc._normalize("endstone_greenmoon_panel") == pc._normalize("endstone-greenmoon-panel"))
check("★ 归一化大小写不敏感",
      pc._normalize("Endstone-GreenMoon-Panel") == pc._normalize("endstone-greenmoon-panel"))

# 本环境（沙箱）没装该发行版 → 应返回 None，不报错
check("★ 查不到发行版时返回 None（不误报）", pc.check_dist_name(None) is None)


# 用一个假的 distribution 验证「不合规」分支
class _FakeMeta(dict):
    pass


class _FakeDist:
    def __init__(self, name):
        self.metadata = {"Name": name}
        self.name = name


_orig_find = pc._find_distribution
try:
    pc._find_distribution = lambda: _FakeDist("greenmoon-panel")
    lg = Logger()
    msg = pc.check_dist_name(lg)
    check("★ 缺 endstone- 前缀被判为不合规", msg is not None, msg)
    check("★ 提示里包含正确的名字", "endstone-greenmoon-panel" in str(msg))
    check("★ 提示解释了后果（data_folder / .local）",
          "data_folder" in str(msg) and ".local" in str(msg))
    check("★ 通过 logger 报了出来", any(lv == "E" for lv, _ in lg.lines))

    pc._find_distribution = lambda: _FakeDist("endstone-greenmoon-panel")
    check("★ 合规名字返回 None", pc.check_dist_name(None) is None)

    pc._find_distribution = lambda: _FakeDist("endstone-wrong-name")
    msg2 = pc.check_dist_name(None)
    check("★ 前缀对但名字不匹配也算不合规", msg2 is not None, msg2)
finally:
    pc._find_distribution = _orig_find


# ================================================================ 6. on_enable 守卫
print()
print("=" * 74)
print("6. ★ on_enable：data_dir 解析失败时有降级保护，不直接崩")
print("=" * 74)
src = open(CORE, encoding="utf-8").read()
tree = ast.parse(src)
plugin = [n for n in tree.body
          if isinstance(n, ast.ClassDef) and n.name == "GreenMoonPlugin"][0]
on_enable = [n for n in plugin.body
             if isinstance(n, ast.FunctionDef) and n.name == "on_enable"][0]
seg = ast.get_source_segment(src, on_enable) or ""

check("★ on_enable 里有 data_dir 为 None 的守卫",
      "if self.data_dir is None" in seg)
check("★ 降级时用临时目录（插件仍能启动）",
      "mkdtemp" in seg)
check("★ 守卫在 web_dir 赋值之前",
      seg.index("if self.data_dir is None") < seg.index('self.data_dir / "web"'))
check("★ 日志安装发生在 data_dir 确定之后",
      seg.index("self._install_plugin_logging") > seg.index("self.data_dir = self._resolve_data_dir()"))
check("★ 启动时打印数据目录（便于用户自查）",
      "插件数据目录" in seg)
check("★ 调用了发行名自查",
      "check_dist_name" in seg)

print()
print("=" * 74)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
