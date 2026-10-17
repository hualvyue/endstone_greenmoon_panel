"""运行环境探测（``_env_info``）的单元测试。

这个功能的用途：用户要给面板装第三方库，但 wheel 在**不同系统 / CPU / Python
版本之间不通用** —— Windows 的包装不到 Linux、3.11 的包装不到 3.12。下载页
上那堆标签（cp311 / win_amd64 / manylinux）对普通用户是天书，所以后端直接算出
「你该用哪个」给前端显示。

本测试重点验的是**映射表**：把 ``os.name`` / ``platform`` 的各种返回值喂进去，
看算出来的 wheel tag 对不对。这些分支在 CI 上只会跑到当前平台那一条，
其余全靠这里覆盖 —— 否则 Linux 上跑的测试永远测不到 Windows 分支。

跑法： python3 test_env_info.py
"""
import ast
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE / "core.py"

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


def section(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


def load_host():
    """从 core.py 抽出环境相关方法，组装成最小宿主类（绕开 endstone 依赖）。"""
    src = CORE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    plugin = [n for n in tree.body
              if isinstance(n, ast.ClassDef) and n.name == "GreenMoonPlugin"][0]
    wanted = {"_env_info", "_win_build", "_win_friendly",
              "_linux_pretty_name", "_wheel_platform_tag", "_lib_bundle_name"}
    methods = [n for n in plugin.body
               if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert len(methods) == len(wanted), \
        f"少抽到方法：{[m.name for m in methods]}"

    host = ast.ClassDef(
        name="_Host", bases=[], keywords=[],
        body=[ast.Expr(value=ast.Constant(value="..."))] + methods,
        decorator_list=[],
    )
    mod = ast.Module(body=[host], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {"os": os, "sys": sys, "Path": Path, "Any": object, "Dict": dict, "List": list}
    exec(compile(mod, str(CORE), "exec"), ns)
    return ns["_Host"]


H = load_host()


# ---------------------------------------------------------------------------
section("1. 真实环境：当前机器要能算出完整结果")
# ---------------------------------------------------------------------------
info = H()._env_info()

for k in ("os_name", "os_detail", "arch", "bits", "python",
          "python_tag", "platform_tag", "pip_target", "libext", "lib_bundle"):
    check(f"返回字段 {k} 存在", k in info, info.get(k))

check("平台名非空", bool(info.get("os_name")), info.get("os_name"))
check("架构非空", bool(info.get("arch")), info.get("arch"))
check("位数是 32 或 64", info.get("bits") in (32, 64), info.get("bits"))

# Python 版本要与解释器一致
import platform
v = sys.version_info
check("Python 版本与实际一致",
      info.get("python") == f"{v.major}.{v.minor}.{v.micro}",
      f"{info.get('python')} vs {v.major}.{v.minor}.{v.micro}")
check("python_tag 形如 cp311", info.get("python_tag") == f"cp{v.major}{v.minor}",
      info.get("python_tag"))

check("pip_target 由 python_tag 和 platform_tag 拼成",
      info.get("pip_target") == f"{info.get('python_tag')}-{info.get('platform_tag')}",
      info.get("pip_target"))
check("libext 是已知动态库后缀",
      info.get("libext") in (".dll", ".so", ".dylib"), info.get("libext"))


# ---------------------------------------------------------------------------
section("2. ★ wheel 平台标签映射（各平台分支，CI 只跑到本机那一条）")
# ---------------------------------------------------------------------------
wp = H._wheel_platform_tag

CASES = [
    # (os_name, arch, bits, 期望)
    ("Windows", "x86_64",  64, "win_amd64"),
    ("Windows", "i686",    32, "win32"),
    ("Windows", "arm64",   64, "win_arm64"),
    ("Windows", "aarch64", 64, "win_arm64"),
    ("Linux",   "x86_64",  64, "manylinux2014_x86_64"),
    ("Linux",   "aarch64", 64, "manylinux2014_aarch64"),
    ("Linux",   "arm64",   64, "manylinux2014_aarch64"),
    ("Linux",   "i686",    32, "manylinux2014_i686"),
    ("Linux",   "ppc64le", 64, "manylinux2014_ppc64le"),
    ("Linux",   "s390x",   64, "manylinux2014_s390x"),
    ("macOS",   "arm64",   64, "macosx_11_0_arm64"),
    ("macOS",   "aarch64", 64, "macosx_11_0_arm64"),
    ("macOS",   "x86_64",  64, "macosx_10_9_x86_64"),
]
for osm, arch, bits, want in CASES:
    got = wp(osm, arch, bits)
    check(f"{osm} / {arch} → {want}", got == want, got)

check("未知平台返回空串（不瞎猜）", wp("Haiku", "x86_64", 64) == "",
      wp("Haiku", "x86_64", 64))
check("Linux 未知架构降级为 linux_<arch>",
      wp("Linux", "riscv64", 64) == "linux_riscv64", wp("Linux", "riscv64", 64))


# ---------------------------------------------------------------------------
section("3. ★ 离线库包平台名映射")
# ---------------------------------------------------------------------------
lb = H._lib_bundle_name
BUNDLE = [
    ("Windows", "x86_64",  "win-x64"),
    ("Windows", "arm64",   "win-arm64"),
    ("Linux",   "x86_64",  "linux-x64"),
    ("Linux",   "aarch64", "linux-arm64"),
    ("Linux",   "arm64",   "linux-arm64"),
    ("macOS",   "arm64",   "macos-arm64"),
    ("macOS",   "x86_64",  "macos-x64"),
]
for osm, arch, want in BUNDLE:
    got = lb(osm, arch)
    check(f"{osm} / {arch} → {want}", got == want, got)
check("未知平台返回空串", lb("Haiku", "x86_64") == "", lb("Haiku", "x86_64"))


# ---------------------------------------------------------------------------
section("4. ★ Windows 版本判定（不能只看 platform.release）")
# ---------------------------------------------------------------------------
wf = H._win_friendly
check("build 22631 → Windows 11", wf(22631) == "Windows 11", wf(22631))
check("build 22000 → Windows 11（边界）", wf(22000) == "Windows 11", wf(22000))
check("build 21999 → Windows 10（边界外一格）", wf(21999) == "Windows 10", wf(21999))
check("build 19045 → Windows 10", wf(19045) == "Windows 10", wf(19045))
check("build 10240 → Windows 10（边界）", wf(10240) == "Windows 10", wf(10240))
check("build 9200 → Windows 8", wf(9200) == "Windows 8", wf(9200))
check("build 7600 → Windows 7", wf(7600) == "Windows 7", wf(7600))
check("build 0 → 退回 Windows（不崩）", wf(0) == "Windows", wf(0))

# ★ 这条是本组要防的坑：Windows 11 的 platform.release() 返回的仍是 "10"
check("★ 注释里说明了 release() 区分不出 Win11",
      "release()" in CORE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
section("5. 健壮性：任何一环失败都不能让整个面板挂掉")
# ---------------------------------------------------------------------------
# _env_info 里每个 try 都独立，单项失败要退化成「未知」而不是抛异常
src = CORE.read_text(encoding="utf-8")
tree = ast.parse(src)
plugin = [n for n in tree.body
          if isinstance(n, ast.ClassDef) and n.name == "GreenMoonPlugin"][0]
env_fn = [n for n in plugin.body
          if isinstance(n, ast.FunctionDef) and n.name == "_env_info"][0]
has_try = [n for n in ast.walk(env_fn) if isinstance(n, ast.Try)]
check("★ _env_info 有多个 try 分支（单项失败不连累整体）", len(has_try) >= 4,
      f"{len(has_try)} 个 try")
check("★ 平台名探测有兜底（except 里也赋值）",
      len([n for n in ast.walk(env_fn)
           if isinstance(n, ast.ExceptHandler)]) >= 4,
      len([n for n in ast.walk(env_fn) if isinstance(n, ast.ExceptHandler)]))

# 真的跑一遍：把 platform 换成会炸的假模块
fake = H()
try:
    got = fake._wheel_platform_tag(None, None, None)
    check("参数全 None 时不抛异常", True, got)
except Exception as e:
    check("参数全 None 时不抛异常", False, e)

try:
    got = H._lib_bundle_name(None, None)
    check("库包名参数全 None 时不抛异常", True, got)
except Exception as e:
    check("库包名参数全 None 时不抛异常", False, e)

# /etc/os-release 不存在时 _linux_pretty_name 要返回空串而不是抛
try:
    r = H._linux_pretty_name()
    check("_linux_pretty_name 不抛异常", isinstance(r, str), repr(r))
except Exception as e:
    check("_linux_pretty_name 不抛异常", False, e)


# ---------------------------------------------------------------------------
section("6. 与 get_system_stats 的接合")
# ---------------------------------------------------------------------------
stats_fn = [n for n in plugin.body
            if isinstance(n, ast.FunctionDef) and n.name == "get_system_stats"][0]

# ★ 别用 ast.unparse 之后做字符串匹配 —— 它会把 "env" 规范化成 'env'，
#   带双引号去找必然假失败（这个坑在 test_logfix.py 里也踩过）。
#   直接用 AST 找「下标为 env 的赋值」。
def _sub_keys(fn, var):
    """找出 ``<var>['xxx'] = ...`` 形式的赋值，返回 {key: 行号}。"""
    out = {}
    for n in ast.walk(fn):
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            if (isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Name) and t.value.id == var
                    and isinstance(t.slice, ast.Constant)):
                out[t.slice.value] = n.lineno
    return out


keys = _sub_keys(stats_fn, "stats")
check("★ get_system_stats 输出 env 字段", "env" in keys, sorted(keys))

has_try_env = any(
    "env" in _sub_keys(n, "stats")
    for n in ast.walk(stats_fn) if isinstance(n, ast.Try)
)
check("env 的计算包在 try 里（失败不影响其它指标）", has_try_env)

# env 必须放在最后，别把前面指标的异常处理带偏
check("env 在 uptime 之后计算（附加信息不抢占主指标）",
      keys.get("uptime", 0) < keys.get("env", 0),
      f"uptime@{keys.get('uptime')} env@{keys.get('env')}")


# ---------------------------------------------------------------------------
section("7. 前端契约：字段名要对得上")
# ---------------------------------------------------------------------------
JS = (HERE / "static" / "admin.js").read_text(encoding="utf-8")
HTML = (HERE / "static" / "admin.html").read_text(encoding="utf-8")

check("前端有 renderEnv", "function renderEnv" in JS)
check("前端有 copyEnvTag", "function copyEnvTag" in JS)
for fid in ("env-os", "env-arch", "env-py", "env-bits",
            "env-pip-target", "env-hint", "env-copy-status"):
    check(f"HTML 有 #{fid}", f'id="{fid}"' in HTML)
    if fid.startswith("env-") and fid not in ("env-copy-status",):
        check(f"  JS 里也用到了 #{fid}", f"'{fid}'" in JS or f'"{fid}"' in JS)

check("环境面板容器存在", 'id="pane-env"' in HTML)
check("★ renderEnv 由 updateGauges 调用（复用同一个 stats 响应，不另发请求）",
      "renderEnv(s.env)" in JS)

# navigator.clipboard 在 http:// 下不可用 —— 面板常是内网 http 访问
check("★ 复制有 execCommand 兜底（http:// 下 clipboard API 不可用）",
      "execCommand" in JS)
check("复制按钮绑定了 copyEnvTag", 'onclick="copyEnvTag(' in HTML)


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
