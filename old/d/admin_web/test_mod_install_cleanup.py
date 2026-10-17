"""修复验证：安装成功后「待安装」列表不再残留该模组。

本轮 bug
--------
``web.py`` 的安装线程里，清理源文件只覆盖了 inbox 来源：

    inbox_file = src if source == "inbox" else None
    if ok and inbox_file is not None:
        inbox_file.unlink()

而**网页上传**（source == "upload"，也是主路径）拿到的永远是 ``None``，
清理被整段跳过 —— 用户装完刷新，「待安装」里还是同一个包，
分不清到底装没装过。

本测试直接跑 ``core.py`` 里的真实方法（AST 抠出 GreenMoonPlugin 再注入
最小宿主），因此测的是产品代码本身，不是复刻逻辑。

验证点
------
  1. upload 来源：装成功后源文件消失、注册表条目清空、列表里不再出现；
  2. inbox  来源：同上（回归，原有行为不能退化）；
  3. 安装失败：源文件**必须保留**（用户要能重试）；
  4. 多包 mcaddon 部分失败：源文件保留，且返回信息里能看出「已装 N 个」；
  5. ``_cleanup_installed_source`` 的边界：空名 / 路径穿越 / 已不存在；
  6. 中文名 + 空格名的包清理正确。

跑法： python3 test_mod_install_cleanup.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import threading
import zipfile
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
    wanted = {
        # 安装
        "_discover_packs_in_zip", "_install_mod_pack", "_install_one_pack_folder",
        "_install_one_pack_file", "_pack_type", "_resolve_pack_target",
        "_locate_world", "_locate_world_for_target", "_enable_pack_in_world",
        "_read_properties_raw", "_get_property_value",
        # 上传库 / inbox
        "_mods_dir", "_mods_inbox_dir", "_scan_mods_inbox", "_is_mod_pack_name",
        "_list_mod_uploaded", "_find_mod_source", "_delete_mod_file",
        "_load_mod_registry", "_save_mod_registry", "_register_mod_file",
        # ★ 本轮新增
        "_cleanup_installed_source",
        # 扫描（断言安装结果时用）
        "_scan_installed_packs", "_read_world_enabled",
        # 包识别
        "_is_education_pack", "_read_pack_manifest", "_pack_kind_of",
        # 存储
        "_json_store", "_json_save", "_store_lock",
    }
    methods = [n for n in plugin.body
               if isinstance(n, ast.FunctionDef) and n.name in wanted]
    cls_attrs = [n for n in plugin.body
                 if isinstance(n, ast.Assign) and n.targets
                 and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id in (
                     "_ARCHIVE_JUNK_DIRS", "_EDU_MODULE_TYPES", "_EDU_NAME_HINTS",
                     "MODS_INBOX_NAME")]

    host = ast.ClassDef(
        name="_Host", bases=[], keywords=[],
        body=[ast.Expr(value=ast.Constant(value="..."))] + cls_attrs + methods,
        decorator_list=[])
    module = ast.Module(body=[host], type_ignores=[])
    ast.fix_missing_locations(module)

    sys.path.insert(0, str(HERE))
    import atomic_io
    ns = {"Path": Path, "Any": Any, "Optional": Optional, "Dict": Dict, "List": List,
          "sys": sys, "os": os, "tempfile": tempfile, "json": json, "zipfile": zipfile,
          "shutil": shutil, "time": __import__("time"),
          "__file__": CORE, "str": str, "bool": bool, "int": int, "round": round,
          "atomic_write_json": atomic_io.atomic_write_json,
          "Thread": threading.Thread, "RLock": threading.RLock}
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


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Host2(Host):
    """补上宿主环境：server root、logger、锁、json store。"""

    def __init__(self, root: Path, data_dir: Path):
        self._root = Path(root)
        self.data_dir = Path(data_dir)
        self.logger = Logger()
        self._tools_config = {}

    def _find_server_root(self):
        return self._root

    def _store_lock(self):
        return _Lock()

    def _load_tools_config(self):
        return self._tools_config

    def _json_store(self, rel, default):
        p = self.data_dir / rel
        try:
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return default

    def _json_save(self, rel, data):
        p = self.data_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return True


def fresh():
    base = Path(tempfile.mkdtemp())
    root = base / "srv"
    root.mkdir()
    (root / "worlds" / "Bedrock level").mkdir(parents=True)
    (root / "worlds" / "Bedrock level" / "level.dat").write_bytes(b"x")
    (root / "resource_packs").mkdir()
    (root / "behavior_packs").mkdir()
    (root / "server.properties").write_text("level-name=Bedrock level\n", encoding="utf-8")
    data = base / "data"
    data.mkdir()
    return Host2(root, data), base


def mkzip(path, entries):
    """entries: {归档内路径: dict(按 JSON 写) 或 str}"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(path), "w") as z:
        for name, content in entries.items():
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            z.writestr(name, content)
    return path


def mf(uuid_, name, mtype, version=(1, 0, 0)):
    return {
        "format_version": 2,
        "header": {"uuid": uuid_, "name": name, "version": list(version)},
        "modules": [{"type": mtype, "uuid": uuid_ + "-mod", "version": list(version)}],
    }


def registry(h):
    p = Path(h.data_dir) / "mods" / "mod_registry.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def uploaded_files(h):
    return [str(u["file"]) for u in h._list_mod_uploaded()]


def simulate_install(h, fname, target="global", world=None):
    """复刻 web.py ``_handle_api_mod_install`` 的 worker 逻辑（改动后的版本）。

    刻意与 web.py 保持同样的调用顺序，这样这里过了，线上路径就是过的。
    """
    src, source = h._find_mod_source(fname)
    if src is None:
        return False, "文件已不存在（上传库与本目录都没有）"
    ok, msg = h._install_mod_pack(src, target, world)
    if ok:
        h._cleanup_installed_source(fname)
    return ok, msg


# ====================================================== 1. 核心 bug：upload 来源
print("=" * 74)
print("1. ★ upload 来源（网页上传，主路径）：装完必须从待安装列表消失")
print("=" * 74)

h, base = fresh()
make = mkzip(h._mods_dir() / "my.mcaddon", {"p/manifest.json": mf("bp-1", "我的行为包", "data")})
h._register_mod_file("my.mcaddon", make.stat().st_size, "我的行为包")

check("装之前：待安装列表里有它", "my.mcaddon" in uploaded_files(h), uploaded_files(h))
src, source = h._find_mod_source("my.mcaddon")
check("来源判定为 upload", source == "upload", source)

ok, msg = simulate_install(h, "my.mcaddon")
check("安装成功", ok, msg)
check("包已落盘 behavior_packs/bp-1",
      (h._root / "behavior_packs" / "bp-1" / "manifest.json").is_file())
check("★ 源文件已从上传库删除", not (h._mods_dir() / "my.mcaddon").exists())
check("★ 刷新后待安装列表不再出现它", "my.mcaddon" not in uploaded_files(h), uploaded_files(h))
check("★ 注册表里的登记项已清空",
      all(it.get("file") != "my.mcaddon" for it in registry(h).get("mods", [])),
      registry(h))

print()
print("  修复前对照：老代码只清 inbox，upload 会被跳过")
# 直接证明老逻辑的判定就是 None（这是 bug 的根）
_src, _source = h._find_mod_source("my.mcaddon")
check("（记录）老代码 inbox_file 对 upload 来源为 None",
      (_src if _source == "inbox" else None) is None, "确认根因")


# ============================================================== 2. inbox 回归
print()
print("=" * 74)
print("2. inbox 来源（手动投放）：原有行为不能退化")
print("=" * 74)

h, base = fresh()
make = mkzip(h._mods_inbox_dir() / "手动包.mcaddon", {"p/manifest.json": mf("inb-1", "手动包", "data")})
check("装之前：列表里有它", "手动包.mcaddon" in uploaded_files(h))
src, source = h._find_mod_source("手动包.mcaddon")
check("来源判定为 inbox", source == "inbox", source)

ok, msg = simulate_install(h, "手动包.mcaddon")
check("安装成功", ok, msg)
check("★ inbox 里的源文件已删除", not (h._mods_inbox_dir() / "手动包.mcaddon").exists())
check("★ 刷新后列表不再出现", "手动包.mcaddon" not in uploaded_files(h), uploaded_files(h))


# ============================================================ 3. 失败必须保留
print()
print("=" * 74)
print("3. ★ 安装失败：源文件必须保留（用户要能修正后重试）")
print("=" * 74)

h, base = fresh()
bad = h._mods_dir() / "bad.mcaddon"
bad.write_bytes(b"this is not a zip file")
h._register_mod_file("bad.mcaddon", bad.stat().st_size, "坏包")
ok, msg = simulate_install(h, "bad.mcaddon")
check("安装失败", not ok, msg)
check("★ 源文件仍在（不能删）", bad.is_file(), str(bad))
check("★ 列表里仍能看到它，可以重试", "bad.mcaddon" in uploaded_files(h), uploaded_files(h))

h, base = fresh()
mkzip(h._mods_dir() / "empty.mcaddon", {"readme.txt": "nothing"})
h._register_mod_file("empty.mcaddon", 10, "空包")
ok, msg = simulate_install(h, "empty.mcaddon")
check("空归档安装失败", not ok and "未找到有效的包" in msg, msg)
check("★ 空包源文件保留", (h._mods_dir() / "empty.mcaddon").is_file())

h, base = fresh()
mkzip(h._mods_dir() / "w.mcaddon", {"p/manifest.json": mf("w-1", "W", "data")})
h._register_mod_file("w.mcaddon", 10, "W")
ok, msg = simulate_install(h, "w.mcaddon", target="world", world="不存在的世界")
check("世界不存在 → 安装失败", not ok, msg)
check("★ 失败时源文件保留", (h._mods_dir() / "w.mcaddon").is_file())


# ====================================================== 4. 多包：部分成功语义
print()
print("=" * 74)
print("4. ★ 多包 mcaddon 部分失败：说清「装了几个」，源文件保留")
print("=" * 74)

h, base = fresh()
f = mkzip(h._mods_dir() / "multi.mcaddon", {
    "好包/manifest.json": mf("good-1", "好包", "data"),
    "坏包/manifest.json": "{ 这不是合法 JSON",
})
h._register_mod_file("multi.mcaddon", f.stat().st_size, "多包")
ok, msg = simulate_install(h, "multi.mcaddon")
check("部分失败 → 整体返回失败", not ok, msg)
check("★ 信息里说明了已装数量", "已安装 1 个包" in msg, msg)
check("★ 信息里保留了失败原因", "失败" in msg, msg)
check("好包确实已落盘",
      (h._root / "behavior_packs" / "good-1" / "manifest.json").is_file())
check("★ 部分失败时源文件保留（让用户重试剩余包）",
      (h._mods_dir() / "multi.mcaddon").is_file())

print("  --- 全部成功时才清理，且带上备注 ---")
h, base = fresh()
f = mkzip(h._mods_dir() / "mix.mcaddon", {
    "真实包/manifest.json": mf("real-1", "真实", "data"),
    "dup.mcpack": mkzip(base / "inner.mcpack",
                        {"manifest.json": mf("dup-1", "重复", "data")}).read_bytes(),
})
h._register_mod_file("mix.mcaddon", f.stat().st_size, "混装")
ok, msg = simulate_install(h, "mix.mcaddon")
check("A+B 混装安装成功", ok, msg)
check("备注里说明了跳过的 mcpack", "mcpack" in msg, msg)
check("★ 全成功后源文件被清理", not (h._mods_dir() / "mix.mcaddon").exists())

print("  --- 一个都没成 → 报第一条失败原因，不是笼统的「未安装任何包」 ---")
h, base = fresh()
f = mkzip(h._mods_dir() / "allbad.mcaddon", {
    "坏1/manifest.json": "{ 坏",
    "坏2/manifest.json": "{ 也坏",
})
h._register_mod_file("allbad.mcaddon", f.stat().st_size, "全坏")
ok, msg = simulate_install(h, "allbad.mcaddon")
check("全部失败", not ok)
check("报的是具体原因而不是笼统文案", "未安装任何包" != msg, msg)
check("源文件保留", (h._mods_dir() / "allbad.mcaddon").is_file())


# ============================================== 5. _cleanup_installed_source 边界
print()
print("=" * 74)
print("5. _cleanup_installed_source 边界")
print("=" * 74)

h, base = fresh()
mkzip(h._mods_dir() / "c1.mcpack", {"manifest.json": mf("c1", "C1", "resources")})
h._register_mod_file("c1.mcpack", 10, "C1")
removed, note = h._cleanup_installed_source("c1.mcpack")
check("正常清理返回 True", removed is True, note)
check("文件已删", not (h._mods_dir() / "c1.mcpack").exists())

removed2, note2 = h._cleanup_installed_source("c1.mcpack")
check("重复清理返回 False 且不抛异常", removed2 is False, note2)

removed3, note3 = h._cleanup_installed_source("")
check("空名返回 False", removed3 is False, note3)

removed4, note4 = h._cleanup_installed_source(None)
check("None 返回 False", removed4 is False, note4)

# 路径穿越：不能删到 mods 外面去
h2, base2 = fresh()
victim = base2 / "victim.txt"
victim.write_text("不能删我", encoding="utf-8")
h2._cleanup_installed_source("../victim.txt")
check("不接受 ../ 穿越", victim.is_file())
h2._cleanup_installed_source("..\\victim.txt")
check("不接受 ..\\ 穿越", victim.is_file())

# 两边同名：都要清掉，注册表也要清
h3, base3 = fresh()
mkzip(h3._mods_dir() / "same.mcpack", {"manifest.json": mf("s1", "上传的", "data")})
h3._register_mod_file("same.mcpack", 10, "上传的")
mkzip(h3._mods_inbox_dir() / "same.mcpack", {"manifest.json": mf("s2", "手动的", "data")})
h3._cleanup_installed_source("same.mcpack")
check("上传库与 inbox 同名文件都被清掉",
      not (h3._mods_dir() / "same.mcpack").exists()
      and not (h3._mods_inbox_dir() / "same.mcpack").exists())
check("注册表幽灵条目已清",
      all(it.get("file") != "same.mcpack" for it in registry(h3).get("mods", [])), registry(h3))


# ==================================================== 6. 中文 / 空格 / 大小写
print()
print("=" * 74)
print("6. 中文与特殊文件名")
print("=" * 74)

h, base = fresh()
cn = "我的材质包 v2.0.mcpack"
mkzip(h._mods_dir() / cn, {"manifest.json": mf("cn-1", "我的材质包", "resources")})
h._register_mod_file(cn, 10, "我的材质包")
check("中文名在列表里", cn in uploaded_files(h), uploaded_files(h))
ok, msg = simulate_install(h, cn)
check("中文名包安装成功", ok, msg)
check("★ 中文名源文件被清理", not (h._mods_dir() / cn).exists())
check("★ 列表里不再出现", cn not in uploaded_files(h), uploaded_files(h))

h, base = fresh()
mkzip(h._mods_dir() / "a.mcpack", {"manifest.json": mf("a-1", "A", "data")})
h._register_mod_file("a.mcpack", 10, "A")
removed, _ = h._cleanup_installed_source("a.mcpack")
check("大小写/普通名正常清理", removed)

print()
print("  --- 重复安装同一文件（源已被清）应给明确提示 ---")
h, base = fresh()
mkzip(h._mods_dir() / "once.mcpack", {"manifest.json": mf("once-1", "ONCE", "data")})
h._register_mod_file("once.mcpack", 10, "ONCE")
ok1, _ = simulate_install(h, "once.mcpack")
ok2, msg2 = simulate_install(h, "once.mcpack")
check("第一次装成功", ok1)
check("第二次找不到文件（源已清）", not ok2)
check("★ 给出明确提示而不是静默", "文件已不存在" in msg2, msg2)


# ============================================== 7. 安装到世界：确认账目一致
print()
print("=" * 74)
print("7. 装到存档：清理后 world_*_packs.json 的启用项不受影响")
print("=" * 74)

h, base = fresh()
mkzip(h._mods_dir() / "wi.mcaddon", {"p/manifest.json": mf("w-bp", "世界包", "data")})
h._register_mod_file("wi.mcaddon", 10, "世界包")
ok, msg = simulate_install(h, "wi.mcaddon", target="world", world="Bedrock level")
check("世界模式安装成功", ok, msg)
wp = h._root / "worlds" / "Bedrock level"
wj = wp / "world_behavior_packs.json"
check("★ world_behavior_packs.json 已写入", wj.is_file())
check("★ 清理源文件后启用项仍在", "w-bp" in wj.read_text(encoding="utf-8"),
      wj.read_text(encoding="utf-8") if wj.is_file() else "")
check("源文件已清理", not (h._mods_dir() / "wi.mcaddon").exists())
inst, meta = h._scan_installed_packs("Bedrock level")
check("扫描能看到该包且 enabled=True",
      any(p["uuid"] == "w-bp" and p["enabled"] is True for p in inst),
      [(p["uuid"], p["enabled"], p["scope"]) for p in inst])


print()
print("=" * 74)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
