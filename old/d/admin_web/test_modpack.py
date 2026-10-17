"""模组管理器三项调整（v6.16.40）验证。

覆盖三件事：
  1. ``_discover_packs_in_zip`` 宽容解析 mcaddon 的三种真实结构
     （子文件夹 / 嵌套 .mcpack / 根 manifest），并正确剪枝。
  2. ``_is_education_pack`` 识别教育模式（化学）包，
     且 ``_scan_installed_packs`` 只隐藏不删除。
  3. ``_mods_inbox_dir`` / ``_scan_mods_inbox`` / ``_find_mod_source``
     —— 手动投放目录里的文件要能被发现、定位、安装、删除。

跑法： python3 test_modpack.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile
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
        # 归档发现与安装
        "_discover_packs_in_zip", "_install_mod_pack", "_install_one_pack_folder",
        "_install_one_pack_file", "_pack_type", "_resolve_pack_target",
        "_locate_world", "_locate_world_for_target", "_enable_pack_in_world",
        "_read_properties_raw", "_get_property_value",
        # 教育包识别
        "_is_education_pack", "_read_pack_manifest",
        # inbox 与上传库
        "_mods_dir", "_mods_inbox_dir", "_scan_mods_inbox", "_is_mod_pack_name",
        "_list_mod_uploaded", "_find_mod_source", "_delete_mod_file",
        "_load_mod_registry", "_save_mod_registry", "_register_mod_file",
        # 扫描
        "_scan_installed_packs", "_read_world_enabled",
    }
    methods = [n for n in plugin.body
               if isinstance(n, ast.FunctionDef) and n.name in wanted]
    # 类属性：模块级常量在方法里会读写
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
          "Thread": __import__("threading").Thread,
          "RLock": __import__("threading").RLock}
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


def fresh(root_name="srv"):
    """建一个假的 BDS 根目录 + 插件数据目录。"""
    base = Path(tempfile.mkdtemp())
    root = base / root_name
    root.mkdir()
    (root / "worlds" / "Bedrock level").mkdir(parents=True)
    (root / "worlds" / "Bedrock level" / "level.dat").write_bytes(b"x")
    (root / "resource_packs").mkdir()
    (root / "behavior_packs").mkdir()
    (root / "server.properties").write_text("level-name=Bedrock level\n", encoding="utf-8")
    data = base / "data"
    data.mkdir()
    return Host2(root, data), base


def make_zip(path, entries, comment=""):
    """entries: {归档内路径: 内容}；值为 dict 时按 JSON 写。"""
    with zipfile.ZipFile(str(path), "w") as z:
        for name, content in entries.items():
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            z.writestr(name, content)
    return path


def mf(uuid_, name, mtype, version=(1, 0, 0), desc="", extra=None):
    m = {
        "format_version": 2,
        "header": {"uuid": uuid_, "name": name, "version": list(version),
                   "description": desc},
        "modules": [{"type": mtype, "uuid": uuid_ + "-mod", "version": list(version)}],
    }
    if extra:
        m.update(extra)
    return m


# ============================================================== 1. 归档结构解析
print("=" * 74)
print("1. ★ _discover_packs_in_zip：三种真实结构都要认")
print("=" * 74)

# --- 形态 A：子文件夹（中文社区最常见）
h, base = fresh()
f = make_zip(base / "A.mcaddon", {
    "我的行为包/manifest.json": mf("bp-a", "我的行为包", "data"),
    "我的行为包/entities/a.json": "{}",
    "我的资源包/manifest.json": mf("rp-a", "我的资源包", "resources"),
    "我的资源包/textures/a.png": "x",
})
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(str(f)) as z:
    z.extractall(str(tmp))
packs, notes = h._discover_packs_in_zip(f, tmp)
check("A 形态：子文件夹被发现（2 个）", len(packs) == 2, [p.name for p in packs])
check("A 形态：两个都是目录", all(p.is_dir() for p in packs))
check("A 形态：中文名保留", any(p.name == "我的行为包" for p in packs))
check("A 形态：不深入包内部（textures 不算包）",
      not any(p.name == "textures" for p in packs))

# --- 形态 B：嵌套 .mcpack（官方规范）
h, base = fresh()
inner = base / "i.mcpack"
make_zip(inner, {"manifest.json": mf("nest-b", "嵌套包", "resources")})
f = make_zip(base / "B.mcaddon", {"sub/inner.mcpack": inner.read_bytes()})
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(str(f)) as z:
    z.extractall(str(tmp))
packs, notes = h._discover_packs_in_zip(f, tmp)
check("B 形态：嵌套 .mcpack 被发现（1 个）", len(packs) == 1, [p.name for p in packs])
check("B 形态：返回的是文件而非目录", packs[0].is_file())
check("B 形态：后缀是 .mcpack", packs[0].suffix.lower() == ".mcpack")

# --- 形态 C：归档根直接 manifest
h, base = fresh()
f = make_zip(base / "C.mcaddon", {"manifest.json": mf("flat-c", "扁平包", "data")})
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(str(f)) as z:
    z.extractall(str(tmp))
packs, notes = h._discover_packs_in_zip(f, tmp)
check("C 形态：根 manifest 被发现（1 个）", len(packs) == 1)
check("C 形态：返回解压根目录本身", packs[0] == tmp)

# --- 形态 D：单个 .mcpack
h, base = fresh()
f = make_zip(base / "D.mcpack", {"manifest.json": mf("single-d", "单包", "resources")})
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(str(f)) as z:
    z.extractall(str(tmp))
packs, notes = h._discover_packs_in_zip(f, tmp)
check("D 形态：单 .mcpack 被发现（1 个）", len(packs) == 1)

# --- 形态 E：A+B 混装（同层既有文件夹包又有 .mcpack）
h, base = fresh()
inner = base / "dup.mcpack"
make_zip(inner, {"manifest.json": mf("dup-e", "重复", "data")})
f = make_zip(base / "E.mcaddon", {
    "真实包/manifest.json": mf("real-e", "真实", "data"),
    "dup.mcpack": inner.read_bytes(),
})
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(str(f)) as z:
    z.extractall(str(tmp))
packs, notes = h._discover_packs_in_zip(f, tmp)
check("E 混装：只认文件夹包（避免重复安装）", len(packs) == 1 and packs[0].name == "真实包",
      [p.name for p in packs])
check("E 混装：有备注说明跳过了 mcpack", any("mcpack" in n for n in notes), notes)

# --- 形态 F：垃圾目录干扰
h, base = fresh()
f = make_zip(base / "F.mcaddon", {
    "__MACOSX/._x": "junk",
    "__MACOSX/包/manifest.json": mf("junk-f", "垃圾", "data"),
    ".git/config": "junk",
    "真包/manifest.json": mf("clean-f", "干净", "resources"),
})
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(str(f)) as z:
    z.extractall(str(tmp))
packs, notes = h._discover_packs_in_zip(f, tmp)
check("F 干扰：__MACOSX 被跳过", len(packs) == 1 and packs[0].name == "真包",
      [p.name for p in packs])

# --- 形态 G：包内部还有 manifest（必须剪枝，不能当第二个包）
h, base = fresh()
f = make_zip(base / "G.mcaddon", {
    "外层/manifest.json": mf("outer-g", "外层", "data"),
    "外层/textures/manifest.json": mf("fake-g", "假的", "resources"),
})
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(str(f)) as z:
    z.extractall(str(tmp))
packs, notes = h._discover_packs_in_zip(f, tmp)
check("G 剪枝：包内部的 manifest 不算独立包", len(packs) == 1, [str(p) for p in packs])

# --- 空归档
h, base = fresh()
f = make_zip(base / "empty.mcaddon", {"readme.txt": "nothing here"})
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(str(f)) as z:
    z.extractall(str(tmp))
packs, notes = h._discover_packs_in_zip(f, tmp)
check("空归档：返回空列表（交由上层报错）", packs == [], packs)

# --- 自己套自己（同名同大小的 .mcpack）
h, base = fresh()
f = make_zip(base / "self.mcpack", {"manifest.json": mf("s", "自套", "resources")})
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(str(f)) as z:
    z.extractall(str(tmp))
shutil.copyfile(str(f), str(tmp / "self.mcpack"))
packs, notes = h._discover_packs_in_zip(f, tmp)
check("自套自身：同名同大小被跳过", packs == [tmp], [str(p) for p in packs])


# ============================================================== 2. 端到端安装
print()
print("=" * 74)
print("2. ★ 端到端安装：三种结构都要真的装进 resource_packs / behavior_packs")
print("=" * 74)

h, base = fresh()
f = make_zip(base / "A.mcaddon", {
    "我的行为包/manifest.json": mf("bp-1", "我的行为包", "data"),
    "我的资源包/manifest.json": mf("rp-1", "我的资源包", "resources"),
})
ok, msg = h._install_mod_pack(f, "global", None)
check("A 形态安装成功", ok, msg)
check("A 形态：行为包装进 behavior_packs/bp-1",
      (h._root / "behavior_packs" / "bp-1" / "manifest.json").is_file())
check("A 形态：资源包装进 resource_packs/rp-1",
      (h._root / "resource_packs" / "rp-1" / "manifest.json").is_file())

h, base = fresh()
inner = base / "i.mcpack"
make_zip(inner, {"manifest.json": mf("nest-1", "嵌套", "resources")})
f = make_zip(base / "B.mcaddon", {"sub/i.mcpack": inner.read_bytes()})
ok, msg = h._install_mod_pack(f, "global", None)
check("B 形态安装成功（老代码报 File is not a zip file）", ok, msg)
check("B 形态：包落到 resource_packs/nest-1",
      (h._root / "resource_packs" / "nest-1" / "manifest.json").is_file())

h, base = fresh()
f = make_zip(base / "C.mcaddon", {"manifest.json": mf("flat-1", "扁平", "data")})
ok, msg = h._install_mod_pack(f, "global", None)
check("C 形态安装成功", ok, msg)

h, base = fresh()
f = make_zip(base / "D.mcpack", {"manifest.json": mf("single-1", "单包", "resources")})
ok, msg = h._install_mod_pack(f, "global", None)
check("D 形态安装成功", ok, msg)

# 装到世界
h, base = fresh()
f = make_zip(base / "W.mcaddon", {
    "p1/manifest.json": mf("w-bp", "世界行为包", "data"),
})
ok, msg = h._install_mod_pack(f, "world", "Bedrock level")
wp = h._root / "worlds" / "Bedrock level"
check("世界模式安装成功", ok, msg)
check("世界模式：包进 worlds/<世界>/behavior_packs/w-bp",
      (wp / "behavior_packs" / "w-bp" / "manifest.json").is_file())
check("世界模式：world_behavior_packs.json 已写入启用项",
      (wp / "world_behavior_packs.json").is_file()
      and "w-bp" in (wp / "world_behavior_packs.json").read_text(encoding="utf-8"))

# 找不到世界要明确报错
h, base = fresh()
f = make_zip(base / "X.mcaddon", {"p/manifest.json": mf("x-1", "X", "data")})
ok, msg = h._install_mod_pack(f, "world", "不存在的世界")
check("世界不存在：返回失败而不是静默成功", not ok, msg)

# 空包
h, base = fresh()
f = make_zip(base / "N.mcaddon", {"readme.txt": "x"})
ok, msg = h._install_mod_pack(f, "global", None)
check("空归档：明确报错", not ok and "未找到有效的包" in msg, msg)

# 非 zip
h, base = fresh()
bad = base / "bad.mcaddon"
bad.write_bytes(b"this is not a zip")
ok, msg = h._install_mod_pack(bad, "global", None)
check("损坏文件：报安装失败而不是抛异常", not ok and "安装失败" in msg, msg)

# 扩展名拦截
h, base = fresh()
txt = base / "a.txt"
txt.write_text("x", encoding="utf-8")
ok, msg = h._install_mod_pack(txt, "global", None)
check("扩展名拦截：只支持 .mcaddon / .mcpack", not ok and "只支持" in msg, msg)


# ============================================================== 3. 教育包识别
print()
print("=" * 74)
print("3. ★ _is_education_pack：识别教育模式（化学）模组")
print("=" * 74)

h, _ = fresh()
check("模块 type=chemistry → 教育包",
      h._is_education_pack(mf("e1", "某模组", "chemistry")))
check("name 含 'Education Edition' → 教育包",
      h._is_education_pack(mf("e2", "Minecraft Education Edition Pack", "data")))
check("name 含 '教育版' → 教育包",
      h._is_education_pack(mf("e3", "教育版化学元素", "data")))
check("name 含 '化学' → 教育包",
      h._is_education_pack(mf("e4", "元素化学实验室", "resources")))
check("description 含 chemistry → 教育包",
      h._is_education_pack(mf("e5", "My Pack", "data", desc="A chemistry addon")))
check("显式 education 字段 → 教育包",
      h._is_education_pack(mf("e6", "P", "data", extra={"education": True})))
check("header.is_education → 教育包",
      h._is_education_pack({
          "header": {"uuid": "e7", "name": "P", "version": [1, 0, 0],
                     "is_education": True},
          "modules": [{"type": "data"}],
      }))
check("模块大小写不敏感（Chemistry）",
      h._is_education_pack(mf("e8", "P", "Chemistry")))

print("  --- 反向：不能误杀 ---")
check("普通资源包不判为教育包",
      not h._is_education_pack(mf("n1", "高清材质包", "resources")))
check("普通行为包不判为教育包",
      not h._is_education_pack(mf("n2", "更多工具", "data")))
check("name 含 'education' 之外相近词不误判（如 educational 除外）",
      not h._is_education_pack(mf("n3", "Redstone Tools", "data")))
check("畸形 manifest（None）不判为教育包",
      not h._is_education_pack(None))
check("畸形 manifest（字符串）不判为教育包",
      not h._is_education_pack("not a dict"))
check("空 dict 不判为教育包",
      not h._is_education_pack({}))
check("modules 是垃圾类型不崩",
      not h._is_education_pack({"modules": [None, "x", 1]}))
check("header 是字符串不崩",
      not h._is_education_pack({"header": "bad", "modules": []}))


# ============================================================== 4. 扫描过滤
print()
print("=" * 74)
print("4. ★ _scan_installed_packs：教育包只隐藏不删除")
print("=" * 74)

h, base = fresh()
rp = h._root / "resource_packs"
bp = h._root / "behavior_packs"

# 正常包
(rp / "normal-1").mkdir()
(rp / "normal-1" / "manifest.json").write_text(
    json.dumps(mf("n-rp", "普通材质", "resources")), encoding="utf-8")
# 教育包（化学）
(bp / "edu-1").mkdir()
(bp / "edu-1" / "manifest.json").write_text(
    json.dumps(mf("e-bp", "化学元素方块", "chemistry")), encoding="utf-8")
(bp / "edu-2").mkdir()
(bp / "edu-2" / "manifest.json").write_text(
    json.dumps(mf("e-bp2", "Minecraft Education Edition", "data")), encoding="utf-8")

installed, meta = h._scan_installed_packs("Bedrock level")
names = [p["name"] for p in installed]
check("普通包仍在列表里", "普通材质" in names, names)
check("教育包已从列表剔除", "化学元素方块" not in names and "Minecraft Education Edition" not in names, names)
check("meta.education_hidden = 2", meta.get("education_hidden") == 2, meta.get("education_hidden"))
check("meta.education_names 给出被隐藏的名字",
      set(meta.get("education_names") or []) == {"化学元素方块", "Minecraft Education Edition"},
      meta.get("education_names"))

print("  --- 只隐藏不删除：磁盘必须还在 ---")
check("教育包目录仍在磁盘上（bp/edu-1）", (bp / "edu-1" / "manifest.json").is_file())
check("教育包目录仍在磁盘上（bp/edu-2）", (bp / "edu-2" / "manifest.json").is_file())

print("  --- 没有教育包时不产生噪音 ---")
h2, base2 = fresh()
(rp2 := h2._root / "resource_packs" / "p1").mkdir()
(rp2 / "manifest.json").write_text(json.dumps(mf("p1", "普通", "resources")), encoding="utf-8")
installed2, meta2 = h2._scan_installed_packs("Bedrock level")
check("无教育包时 education_hidden = 0", meta2.get("education_hidden") == 0)
check("无教育包时 education_names 为空", meta2.get("education_names") == [])
check("无教育包时正常包照常显示", len(installed2) == 1)

print("  --- 教育包不能因为被隐藏就丢掉启用状态记账 ---")
h3, base3 = fresh()
wp3 = h3._root / "worlds" / "Bedrock level"
(wp3 / "behavior_packs" / "edu-w").mkdir(parents=True)
(wp3 / "behavior_packs" / "edu-w" / "manifest.json").write_text(
    json.dumps(mf("edu-w", "教育化学", "chemistry")), encoding="utf-8")
(wp3 / "world_behavior_packs.json").write_text(
    json.dumps([{"pack_id": "edu-w", "version": [1, 0, 0]}]), encoding="utf-8")
installed3, meta3 = h3._scan_installed_packs("Bedrock level")
check("被隐藏的教育包不出现在 installed", all(p["uuid"] != "edu-w" for p in installed3))
check("但 world_behavior_packs.json 未被改写（没被我们动过）",
      "edu-w" in (wp3 / "world_behavior_packs.json").read_text(encoding="utf-8"))


# ============================================================== 5. inbox 目录
print()
print("=" * 74)
print("5. ★ inbox 手动投放目录：发现 / 定位 / 安装 / 删除")
print("=" * 74)

h, base = fresh()
inbox = h._mods_inbox_dir()
check("inbox 目录可以创建", inbox.is_dir(), inbox)
check("inbox 在 数据目录/mods/inbox", inbox.parent == h._mods_dir() and inbox.name == "inbox",
      str(inbox))

# 放两个包 + 一个干扰文件
make_zip(inbox / "手动包.mcaddon", {"p/manifest.json": mf("inbox-1", "手动包", "data")})
make_zip(inbox / "materials.mcpack", {"manifest.json": mf("inbox-2", "材质", "resources")})
(inbox / "readme.txt").write_text("这不是模组", encoding="utf-8")
(inbox / ".hidden.mcpack").write_text("隐藏文件", encoding="utf-8")

ups = h._list_mod_uploaded()
files = [u["file"] for u in ups]
check("inbox 里的 .mcaddon 出现在待安装列表", "手动包.mcaddon" in files, files)
check("inbox 里的 .mcpack 出现在待安装列表", "materials.mcpack" in files, files)
check("非模组文件（readme.txt）不出现", "readme.txt" not in files, files)
check("隐藏文件（.hidden.mcpack）不出现", ".hidden.mcpack" not in files, files)
check("inbox 条目带 source=inbox",
      all(u.get("source") == "inbox" for u in ups if u["file"] in ("手动包.mcaddon", "materials.mcpack")))
check("inbox 条目 exists=True", all(u.get("exists") for u in ups))

print("  --- _find_mod_source 定位 ---")
p1, s1 = h._find_mod_source("手动包.mcaddon")
check("inbox 里的文件能被定位到", p1 is not None and s1 == "inbox", (str(p1), s1))
check("定位到的是真实 inbox 路径", p1 == inbox / "手动包.mcaddon", str(p1))
p2, s2 = h._find_mod_source("不存在.mcaddon")
check("不存在的文件返回 (None, '')", p2 is None and s2 == "")

print("  --- 回归：上传库优先 ---")
# 同名文件同时存在于上传库与 inbox → 应优先上传库
make_zip(h._mods_dir() / "both.mcpack", {"manifest.json": mf("both-1", "两边都有", "data")})
make_zip(inbox / "both.mcpack", {"manifest.json": mf("both-2", "inbox 版本", "data")})
h._register_mod_file("both.mcpack", (h._mods_dir() / "both.mcpack").stat().st_size, "两边都有")
p3, s3 = h._find_mod_source("both.mcpack")
check("重名时优先上传库", s3 == "upload", s3)
ups3 = h._list_mod_uploaded()
check("重名时不重复列出",
      len([u for u in ups3 if u["file"] == "both.mcpack"]) == 1,
      [u["file"] for u in ups3])

print("  --- 安装 inbox 文件 ---")
h4, base4 = fresh()
inbox4 = h4._mods_inbox_dir()
make_zip(inbox4 / "t.mcaddon", {"p/manifest.json": mf("t-1", "待装包", "data")})
src, source = h4._find_mod_source("t.mcaddon")
check("inbox 文件可定位", src is not None and source == "inbox")
ok, msg = h4._install_mod_pack(src, "global", None)
check("inbox 文件能成功安装", ok, msg)
check("安装后落到 behavior_packs/t-1",
      (h4._root / "behavior_packs" / "t-1" / "manifest.json").is_file())

print("  --- 删除 ---")
h5, base5 = fresh()
inbox5 = h5._mods_inbox_dir()
make_zip(inbox5 / "d1.mcpack", {"manifest.json": mf("d1", "删我", "resources")})
check("删除 inbox 文件返回 True", h5._delete_mod_file("d1.mcpack") is True)
check("文件真的没了", not (inbox5 / "d1.mcpack").exists())

make_zip(h5._mods_dir() / "d2.mcpack", {"manifest.json": mf("d2", "删我2", "resources")})
h5._register_mod_file("d2.mcpack", 10, "删我2")
check("删除上传库文件返回 True", h5._delete_mod_file("d2.mcpack") is True)
check("上传库文件没了", not (h5._mods_dir() / "d2.mcpack").exists())
reg = json.loads(Path(h5.data_dir / "mods" / "mod_registry.json").read_text(encoding="utf-8"))
check("注册表里的条目也清了", all(it.get("file") != "d2.mcpack" for it in reg.get("mods", [])),
      reg)

check("删除不存在的文件返回 False", h5._delete_mod_file("nope.mcpack") is False)
check("删除空名返回 False", h5._delete_mod_file("") is False)

print("  --- 路径穿越防护 ---")
h6, base6 = fresh()
victim = base6 / "victim.txt"
victim.write_text("不能删我", encoding="utf-8")
h6._delete_mod_file("../victim.txt")
check("delete 不接受 ../ 穿越", victim.is_file())
h6._delete_mod_file("..\\victim.txt")
check("delete 不接受 ..\\ 穿越", victim.is_file())

print("  --- inbox 与注册表同名时的清理（幽灵条目） ---")
h7, base7 = fresh()
inbox7 = h7._mods_inbox_dir()
make_zip(h7._mods_dir() / "same.mcpack", {"manifest.json": mf("s1", "上传的", "data")})
h7._register_mod_file("same.mcpack", 10, "上传的")
make_zip(inbox7 / "same.mcpack", {"manifest.json": mf("s2", "手动的", "data")})
h7._delete_mod_file("same.mcpack")
check("两边同名的文件都删掉", not (h7._mods_dir() / "same.mcpack").exists()
      and not (inbox7 / "same.mcpack").exists())
reg7 = json.loads(Path(h7.data_dir / "mods" / "mod_registry.json").read_text(encoding="utf-8"))
check("注册表幽灵条目已清理",
      all(it.get("file") != "same.mcpack" for it in reg7.get("mods", [])), reg7)


# ============================================================== 6. 中文与健壮性
print()
print("=" * 74)
print("6. 中文文件名 / 健壮性")
print("=" * 74)

h, base = fresh()
inbox = h._mods_inbox_dir()
make_zip(inbox / "我的材质包 v2.0.mcpack", {"manifest.json": mf("cn-1", "我的材质包", "resources")})
ups = h._list_mod_uploaded()
check("中文 + 空格 + 点的文件名能被列出",
      any(u["file"] == "我的材质包 v2.0.mcpack" for u in ups), [u["file"] for u in ups])
src, source = h._find_mod_source("我的材质包 v2.0.mcpack")
ok, msg = h._install_mod_pack(src, "global", None)
check("中文名包能安装", ok, msg)

h, base = fresh()
f = make_zip(base / "中文存档.mcaddon", {"中文包/manifest.json": mf("cn-2", "中文包", "data")})
ok, msg = h._install_mod_pack(f, "world", "Bedrock level")
check("归档内中文路径能安装", ok, msg)

print("  --- 畸形 manifest 的包不影响其它包 ---")
h, base = fresh()
f = make_zip(base / "partly.mcaddon", {
    "good/manifest.json": mf("good-1", "好包", "data"),
    "bad/manifest.json": "{ 这不是合法 JSON",
})
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(str(f)) as z:
    z.extractall(str(tmp))
packs, notes = h._discover_packs_in_zip(f, tmp)
check("畸形的包也会被识别为候选（交给安装阶段报错）", len(packs) == 2, [p.name for p in packs])

print("  --- _is_mod_pack_name ---")
check("_is_mod_pack_name 认 .mcaddon", h._is_mod_pack_name("a.mcaddon"))
check("_is_mod_pack_name 认 .MCPACK（大小写）", h._is_mod_pack_name("a.MCPACK"))
check("_is_mod_pack_name 不认 .zip", not h._is_mod_pack_name("a.zip"))
check("_is_mod_pack_name 不认 None", not h._is_mod_pack_name(None))
check("_is_mod_pack_name 不认空串", not h._is_mod_pack_name(""))

print("  --- 深度防御 ---")
h, base = fresh()
deep = {}
cur = ""
for i in range(12):
    cur += f"d{i}/"
deep[cur + "manifest.json"] = mf("deep-1", "超深", "data")
f = make_zip(base / "deep.mcaddon", deep)
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(str(f)) as z:
    z.extractall(str(tmp))
packs, notes = h._discover_packs_in_zip(f, tmp)
check("超深目录不会无限递归到崩（有深度上限或兜底）", isinstance(packs, list), packs)

print()
print("=" * 74)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
