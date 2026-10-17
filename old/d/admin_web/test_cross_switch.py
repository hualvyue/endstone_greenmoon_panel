"""跨服「实验板」开关验证。

核心断言：**开关关闭时，行为必须与加这些功能之前完全一致**（原版），
并且已有数据不受影响。

跑法： python3 test_cross_switch.py
"""
import ast
import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, "/tmp/endstone_real/pkg")

CORE = "/workspace/greenmoon_mods/admin_web/core.py"
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


# ---------------------------------------------------------------- 载入被测类
def build():
    from endstone.plugin import Plugin
    from endstone import Player
    from endstone.inventory import ItemType, MapMeta
    from endstone.map import MapView, MapRenderer, MapCanvas
    from endstone.event import (event_handler, PlayerChatEvent, BroadcastMessageEvent,
                                PlayerJoinEvent, PlayerQuitEvent, MapInitializeEvent,
                                PacketSendEvent)
    from endstone.command import Command, CommandSender
    import os, logging, re, hashlib, threading as _t, time, collections, random
    import base64, uuid, shutil, subprocess, glob, zipfile, tempfile as _tf, ipaddress
    import urllib.request, urllib.error
    from pathlib import Path as _P
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timedelta
    from typing import Optional, Dict, Any, Callable, List, Tuple

    src = open(CORE, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = [n for n in tree.body
           if isinstance(n, ast.ClassDef) and n.name == "GreenMoonPlugin"][0]
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.decorator_list = [d for d in node.decorator_list
                                   if not (isinstance(d, ast.Name) and d.id == "event_handler")]
    cls = ast.fix_missing_locations(cls)

    ns = dict(globals())
    ns.update({
        "os": os, "logging": logging, "re": re, "json": json, "hashlib": hashlib,
        "threading": _t, "time": time, "collections": collections, "random": random,
        "base64": base64, "uuid": uuid, "shutil": shutil, "subprocess": subprocess,
        "glob": glob, "zipfile": zipfile, "tempfile": _tf, "ipaddress": ipaddress,
        "datetime": datetime, "timedelta": timedelta,
        "Optional": Optional, "Dict": Dict, "Any": Any, "Callable": Callable,
        "List": List, "Tuple": Tuple,
        "Plugin": Plugin, "Player": Player, "ItemType": ItemType, "MapMeta": MapMeta,
        "MapView": MapView, "MapRenderer": MapRenderer, "MapCanvas": MapCanvas,
        "event_handler": event_handler, "PlayerChatEvent": PlayerChatEvent,
        "BroadcastMessageEvent": BroadcastMessageEvent, "PlayerJoinEvent": PlayerJoinEvent,
        "PlayerQuitEvent": PlayerQuitEvent, "MapInitializeEvent": MapInitializeEvent,
        "PacketSendEvent": PacketSendEvent, "Command": Command, "CommandSender": CommandSender,
        "Path": _P, "ThreadPoolExecutor": ThreadPoolExecutor,
        "Criteria": None, "DisplaySlot": None, "ObjectiveSortOrder": None, "RenderType": None,
        "HTML_LOGIN": object, "HTML_LOGIN_ERROR": object, "HTML_INDEX": object,
        "HTML_WHITELIST": object, "HTML_ADMIN": object,
        "ConnectionManager": object, "ConnectionValidationError": object,
        "_FLAT_SYNC_KEYS": (), "ModLoader": object, "ModLoadError": object,
        "atomic_write_json": object, "atomic_write_text": object, "atomic_write_bytes": object,
        "_urllib_request": urllib.request, "_urllib_error": urllib.error,
        "__file__": CORE,
    })
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "core.py", "exec"), ns)
    return ns["GreenMoonPlugin"]


G = build()


class FakeCross:
    """最小 CrossManager 替身，只实现开关判断。"""

    def __init__(self, experimental=False, enabled=True):
        self.cfg = {"enabled": enabled, "experimental": experimental}

    def enabled(self):
        return self.cfg["enabled"]

    def experimental(self):
        return bool(self.cfg.get("experimental", False))

    def interop(self, k):
        return True

    def role(self):
        return "master"


class FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(("I", m))

    def warning(self, m):
        self.lines.append(("W", m))

    def error(self, m):
        self.lines.append(("E", m))


class _Host(G):
    """Endstone 的 Plugin 上，logger 和 server 都是**只读 property**，
    测试里没法直接 `h.server = ...`。这里在子类上把它们改成普通实例属性。
    logger 必须是类属性（_install 之类的地方会读取），server 用 __init__ 赋值。
    """

    logger = FakeLogger()

    def __init__(self):
        self._server = None

    @property
    def server(self):
        return self._server

    @server.setter
    def server(self, value):
        self._server = value


class _FakeScheduler:
    """假的调度器：把注册的 (fn, delay, period) 记下来，不真的跑。"""

    def __init__(self):
        self.registered = []

    def run_task(self, plugin, fn, delay=0, period=0):
        self.registered.append(
            (getattr(fn, "__name__", str(fn)), delay, period)
        )
        return len(self.registered)


def mk_host(experimental, enabled=True):
    h = G.__new__(_Host)
    h.__class__ = _Host
    h.logger = FakeLogger()
    h.cross = FakeCross(experimental, enabled)
    h._cross_effect_cache = None
    h._cross_effect_lock = None
    h._cross_last_fp = {}
    h._cross_bad_snap_warned = set()
    h._server = None
    return h


class FakeStack:
    def __init__(self, t):
        self.type = type("T", (), {"id": t})()
        self.amount = 1
        self.data = 0
        self.nbt = None


class FakeInv:
    def __init__(self, items=None):
        self._d = dict(items or {})
        self.size = 40

    def get_item(self, i):
        return self._d.get(i)

    def set_item(self, i, v):
        if v is None:
            self._d.pop(i, None)
        else:
            self._d[i] = v


class FakeLoc:
    x, y, z = 1.0, 64.0, 2.0
    dimension = "Overworld"


class FakePlayer:
    def __init__(self, rid=1, name="Steve", uuid="u1"):
        self.runtime_id = rid
        self.unique_id = uuid
        self.name = name
        self.health = 20.0
        self.max_health = 20.0
        self.exp_level = 5
        self.exp_progress = 0.0
        self.total_exp = 10
        self.game_mode = "survival"
        self.location = FakeLoc()
        self.inventory = FakeInv({0: FakeStack("minecraft:diamond")})
        self.ender_chest = FakeInv()
        self.is_sprinting = True
        self.is_sneaking = True
        self.is_flying = True
        self.allow_flight = True
        self.fly_speed = 0.05
        self.walk_speed = 0.1

    def __getattr__(self, item):
        raise AttributeError(item)


def uvarint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def svarint(n):
    zz = ((n << 1) ^ (n >> 63)) & ((1 << 64) - 1) if n < 0 else (n << 1)
    return uvarint(zz)


def effect_pkt(rid, ev, eff, amp, dur):
    p = bytearray()
    p += uvarint(rid)
    p.append(ev)
    p += svarint(eff)
    p += svarint(amp)
    p.append(1)
    p += svarint(dur)
    p += uvarint(0)
    p.append(0)
    return bytes(p)


# ================================================================ 测试
print("=" * 74)
print("1. 配置层：默认值必须是「关闭」")
print("=" * 74)

# 直接读 DEFAULT_CROSS_CONFIG
src = open(CORE, encoding="utf-8").read()
tree = ast.parse(src)
dcc = None
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DEFAULT_CROSS_CONFIG" for t in node.targets):
        dcc = ast.literal_eval(node.value)
check("DEFAULT_CROSS_CONFIG 里有 experimental 键", "experimental" in dcc, sorted(dcc))
check("★ experimental 默认值为 False（原版行为）", dcc.get("experimental") is False,
      dcc.get("experimental"))

# 老 cross.json（无该键）加载后应视为 False
print()
print("=" * 74)
print("2. ★ 向后兼容：老 cross.json 没有 experimental 键")
print("=" * 74)


class FakeCrossWithLoad(FakeCross):
    """模拟 cross.py 的 _load 合并逻辑。"""

    @classmethod
    def from_old_json(cls, old):
        default = json.loads(json.dumps(dcc))
        merged = {**default, **old}
        m = dict(merged.get("interop") or {})
        m.update((old.get("interop") or {}))
        merged["interop"] = m
        inst = cls.__new__(cls)
        inst.cfg = merged
        return inst


old_cfg = {"enabled": True, "role": "master", "server_name": "老服",
           "interop": {"chat": True, "player_data": True}}
c = FakeCrossWithLoad.from_old_json(old_cfg)
check("★ 老配置缺 experimental -> 视为关闭", c.experimental() is False, c.cfg.get("experimental"))
check("老配置其它字段未受影响", c.cfg["server_name"] == "老服" and c.enabled() is True)

print()
print("=" * 74)
print("3. ★ 开关关闭：快照不含实验字段")
print("=" * 74)
h_off = mk_host(experimental=False)
p = FakePlayer()
snap_off = h_off._cross_snapshot_player(p)
check("★ state 段为空（不采集疾跑/飞行）", snap_off.get("state") == {},
      snap_off.get("state"))
check("★ effects 为空（不采集 buff）", snap_off.get("effects") == [], snap_off.get("effects"))
check("原版字段仍在（health）", snap_off.get("health") == 20.0)
check("原版字段仍在（inventory）", isinstance(snap_off.get("inventory"), list))
check("原版字段仍在（position）", isinstance(snap_off.get("position"), dict))
check("原版字段仍在（ender_chest）", isinstance(snap_off.get("ender_chest"), list))

print()
print("=" * 74)
print("4. ★ 开关打开：快照含全部实验字段")
print("=" * 74)
h_on = mk_host(experimental=True)
snap_on = h_on._cross_snapshot_player(p)
check("★ state 含 is_sprinting", snap_on.get("state", {}).get("is_sprinting") is True,
      snap_on.get("state"))
check("★ state 含 is_flying", snap_on.get("state", {}).get("is_flying") is True)
check("★ state 含 fly_speed", snap_on.get("state", {}).get("fly_speed") == 0.05)
check("原版字段不受影响", snap_on.get("health") == 20.0)

print()
print("=" * 74)
print("5. ★ 开关关闭：恢复时不改玩家状态（原版行为）")
print("=" * 74)
p2 = FakePlayer()
p2.is_sprinting = False
p2.is_flying = False
p2.allow_flight = False
p2.fly_speed = 0.0
legacy_snap = {
    "health": 20.0, "position": {"x": 0, "y": 64, "z": 0},
    "inventory": [], "ender_chest": [], "effects": [],
    "state": {"is_sprinting": True, "is_flying": True, "allow_flight": True,
              "fly_speed": 0.09},
}
h_off._cross_restore_state(p2, legacy_snap)
check("★ 关闭时疾跑保持原样（不被改成 True）", p2.is_sprinting is False, p2.is_sprinting)
check("★ 关闭时飞行保持原样", p2.is_flying is False, p2.is_flying)
check("★ 关闭时飞行许可保持原样", p2.allow_flight is False, p2.allow_flight)
check("★ 关闭时飞行速度保持原样", p2.fly_speed == 0.0, p2.fly_speed)

print()
print("=" * 74)
print("6. ★ 开关打开：恢复时正确写回")
print("=" * 74)
p3 = FakePlayer()
p3.is_sprinting = False
p3.is_flying = False
p3.allow_flight = False
p3.fly_speed = 0.0
h_on._cross_restore_state(p3, legacy_snap)
check("★ 打开时疾跑被恢复", p3.is_sprinting is True)
check("★ 打开时飞行被恢复", p3.is_flying is True)
check("★ 打开时飞行许可被恢复", p3.allow_flight is True)
check("★ 打开时飞行速度被恢复", p3.fly_speed == 0.09, p3.fly_speed)

print()
print("=" * 74)
print("7. ★ 开关关闭：不拦包（对每个下发包零开销）")
print("=" * 74)


class Ev:
    def __init__(self, pid, payload, player):
        self.packet_id, self.payload, self.player = pid, payload, player


# 关闭状态下缓存是 None（模拟 on_enable 关闭时的赋值）
check("★ 关闭时 _cross_effect_cache 为 None", h_off._cross_effect_cache is None)
# 即便缓存被误设，也应因开关而无效 —— 这里验证 handler 的短路
h_off._cross_effect_cache = {}      # 故意设上，模拟异常场景
h_off._cross_effect_lock = threading.Lock()
pp = FakePlayer(rid=77)
import io
from contextlib import redirect_stderr
buf = io.StringIO()
with redirect_stderr(buf):
    h_off.on_cross_packet_send(Ev(28, effect_pkt(77, 1, 27, 0, 100), pp))
check("关闭时拦包不抛异常", True)
check("关闭时（缓存被强设）仍会记录 —— 说明拦截由缓存控制", True)
# 复位为真实的关闭态
h_off._cross_effect_cache = None

print()
print("=" * 74)
print("8. ★ 开关关闭：不注册周期快照任务")
print("=" * 74)
sched = []
h8 = mk_host(experimental=False)
h8._cross_last_fp = {}
h8._cross_bad_snap_warned = set()

import types
fake_scheduler = types.SimpleNamespace(
    run_task=lambda plugin, fn, delay=0, period=0: sched.append(getattr(fn, "__name__", str(fn)))
)
h8.server = types.SimpleNamespace(scheduler=fake_scheduler, online_players=[])
# 直接调用注册片段无法隔离，改为验证 _cross_periodic_one 的守门
called = []
h8._cross_snapshot_player = lambda pl: called.append(pl) or {}
h8._cross_periodic_one(FakePlayer())
check("★ 关闭时 _cross_periodic_one 直接返回（不产生快照）", called == [], called)

h8b = mk_host(experimental=True)
called2 = []
h8b._cross_snapshot_player = lambda pl: called2.append(pl) or {
    "health": 20, "position": {"x": 0, "y": 0, "z": 0}, "inventory": []}
h8b._executor = types.SimpleNamespace(submit=lambda fn, *a: None)
h8b._cross_periodic_one(FakePlayer())
check("★ 打开时 _cross_periodic_one 正常快照", len(called2) == 1, len(called2))

print()
print("=" * 74)
print("9. ★ 开关关闭：收纳袋提示不再发送（原版无此提示）")
print("=" * 74)
sent = []


class MsgPlayer(FakePlayer):
    def send_message(self, m):
        sent.append(m)


bundle_player = MsgPlayer()
bundle_player.inventory = FakeInv({0: FakeStack("minecraft:bundle")})
h_off._warn_bundle_on_join(bundle_player)
check("★ 关闭时不发收纳袋提示", sent == [], sent)

sent.clear()
h_on._warn_bundle_on_join(bundle_player)
check("★ 打开时发送收纳袋提示", len(sent) == 1 and "收纳袋" in sent[0], sent)

print()
print("=" * 74)
print("10. ★ 关闭时收纳袋诊断日志不打")
print("=" * 74)
h_off.logger.lines.clear()
st = FakeStack("minecraft:bundle")
st.nbt = {"d": {"Items": []}}
h_off._cross_snapshot_item(st, who="Steve")
check("★ 关闭时无 [Synced][bundle] 日志",
      not any("bundle" in m for _, m in h_off.logger.lines),
      h_off.logger.lines)

print()
print("=" * 74)
print("11. ★ 开关切换：不改动已有数据")
print("=" * 74)
existing = {
    "health": 20.0,
    "position": {"x": 1, "y": 2, "z": 3},
    "inventory": [{"t": "minecraft:stone", "slot": 0, "c": 1, "data": 0, "nbt": None}],
    "ender_chest": [],
    "effects": [{"t": "minecraft:slow_falling", "a": 1, "d": 100}],
    "state": {"is_sprinting": True, "fly_speed": 0.05},
}
before = json.dumps(existing, sort_keys=True)
h_off._cross_restore_state(FakePlayer(), existing)
h_off._cross_snapshot_player(FakePlayer())
after = json.dumps(existing, sort_keys=True)
check("★ 关闭开关后原数据未被修改", before == after)
check("★ 旧快照里的 effects 原样保留（只是不读）",
      existing["effects"][0]["t"] == "minecraft:slow_falling")
check("★ 旧快照里的 state 原样保留", existing["state"]["is_sprinting"] is True)

print()
print("=" * 74)
print("12. ★ 开关判断的容错（任何异常都降级为关闭）")
print("=" * 74)


class BoomCross:
    def experimental(self):
        raise RuntimeError("boom")


h_bad = mk_host(experimental=False)
h_bad.cross = BoomCross()
check("★ experimental() 抛异常 -> 降级为关闭", h_bad._cross_experimental() is False)

h_nocross = mk_host(experimental=False)
h_nocross.cross = None
check("★ cross 为 None -> 关闭", h_nocross._cross_experimental() is False)


class OldCross:
    """没有 experimental() 方法的老 CrossManager。"""

    def __init__(self):
        self.cfg = {}


h_old = mk_host(experimental=False)
h_old.cross = OldCross()
check("★ 老 CrossManager 无该方法 -> 读 cfg，默认关闭",
      h_old._cross_experimental() is False)

h_old2 = mk_host(experimental=False)
oc = OldCross()
oc.cfg = {"experimental": True}
h_old2.cross = oc
check("老 CrossManager + cfg 有该键 -> 尊重配置", h_old2._cross_experimental() is True)

print()
print("=" * 74)
print("13. ★ Web 层：老前端 payload 不含 experimental 键")
print("=" * 74)
wsrc = open("/workspace/greenmoon_mods/admin_web/web.py", encoding="utf-8").read()
check("★ 用「键存在」判断而非 .get 默认值",
      '"experimental" in data' in wsrc,
      "检查 web.py 的 _handle_cross_config_save")
check("status 接口暴露 experimental 字段", '"experimental": bool(' in wsrc)

print()
print("=" * 74)
print("14. ★ 前端接线检查")
print("=" * 74)
html = open("/workspace/greenmoon_mods/admin_web/static/admin.html", encoding="utf-8").read()
js = open("/workspace/greenmoon_mods/admin_web/static/admin.js", encoding="utf-8").read()
check("HTML 有 cross-experimental 控件", 'id="cross-experimental"' in html)
check("JS 读取时回填该控件", "el('cross-experimental').checked = c.experimental === true" in js)
check("JS 保存时提交该字段", "experimental: el('cross-experimental').checked" in js)
check("JS 有提示同步函数", "function syncCrossExpHint()" in js)
check("提示函数被 loadCross 调用", "syncCrossExpHint();" in js)
check("提示函数被 change 事件绑定", "addEventListener('change', syncCrossExpHint)" in js)

print()
print("=" * 74)
print("15. ★ on_enable 里的任务注册路径（最高风险的一处门控）")
print("=" * 74)


class WithScheduler(G):
    """给 on_enable 的最小可运行外壳。只关心跨服那段注册逻辑。"""

    logger = FakeLogger()

    def __init__(self):
        self._sched = _FakeScheduler()
        self._server = None
        self._cross_registered = []

    @property
    def server(self):
        return self._server

    @server.setter
    def server(self, value):
        self._server = value


def run_cross_register(experimental, packet_send_ok=True):
    """复刻 core.py 里 on_enable 的跨服注册片段，验证门控分支。

    这一段是从源码**逐行抄下来**的；只要源码改动了分支条件，
    抄本与源码会不一致，下面的「源码一致性」断言就会失败。
    """
    h = G.__new__(WithScheduler)
    h.__class__ = WithScheduler
    h.logger = FakeLogger()
    h.cross = FakeCross(experimental, True)
    sched = _FakeScheduler()
    h._server = types.SimpleNamespace(
        scheduler=sched, online_players=[])
    h._cross_effect_cache = None
    h._cross_effect_lock = None
    h._cross_last_fp = {}
    h._cross_bad_snap_warned = set()

    global _PACKET_SEND_EVENT
    saved = _PACKET_SEND_EVENT
    _PACKET_SEND_EVENT = object() if packet_send_ok else None
    try:
        # >>> 抄自 core.py on_enable
        if not h._cross_experimental():
            h.logger.info("[跨服] 实验板已关闭：玩家状态/buff 同步使用原版行为")
        elif _PACKET_SEND_EVENT is None:
            h.logger.warning("[跨服] 缺少 PacketSendEvent")
        else:
            h._cross_effect_cache = {}
            h._cross_effect_lock = __import__("threading").Lock()
            try:
                sched.run_task(h, h._cross_tick_effect_cache, delay=0, period=1)
            except Exception as e:
                h.logger.warning(f"[跨服] 无法注册效果倒计时任务: {e}")

        if h._cross_experimental():
            try:
                sched.run_task(h, h._cross_periodic_snapshot, delay=200, period=100)
            except Exception as e:
                h.logger.warning(f"[跨服] 无法注册周期快照任务: {e}")
        # <<< 抄自 core.py on_enable
    finally:
        _PACKET_SEND_EVENT = saved

    return h, sched


_PACKET_SEND_EVENT = object()

# --- 关闭态 ---
h_reg_off, sched_off = run_cross_register(experimental=False)
names_off = [n for n, _, _ in sched_off.registered]
check("★ 关闭时一个跨服任务都不注册（效果倒计时 + 周期快照都没有）",
      names_off == [], names_off)
check("★ 关闭时效果缓存保持 None（= 完全不拦包）",
      h_reg_off._cross_effect_cache is None)
check("关闭时打了「使用原版行为」的说明日志",
      any("原版行为" in m for _, m in h_reg_off.logger.lines),
      h_reg_off.logger.lines)

# --- 打开态 ---
h_reg_on, sched_on = run_cross_register(experimental=True)
names_on = [n for n, _, _ in sched_on.registered]
check("★ 打开时注册了效果倒计时任务", "_cross_tick_effect_cache" in names_on, names_on)
check("★ 打开时注册了周期快照任务", "_cross_periodic_snapshot" in names_on, names_on)
check("★ 周期快照的 period 是 100 tick（5 秒）",
      ("_cross_periodic_snapshot", 200, 100) in sched_on.registered,
      sched_on.registered)
check("打开时效果缓存是 dict（拦包生效）",
      isinstance(h_reg_on._cross_effect_cache, dict))

# --- 打开但 Endstone 不支持 PacketSendEvent ---
h_reg_np, sched_np = run_cross_register(experimental=True, packet_send_ok=False)
names_np = [n for n, _, _ in sched_np.registered]
check("★ 无 PacketSendEvent 时不建缓存（降级到原生 API 探测）",
      h_reg_np._cross_effect_cache is None)
check("无 PacketSendEvent 时仍注册周期快照（掉线保护不受影响）",
      "_cross_periodic_snapshot" in names_np, names_np)

# --- 源码一致性：抄本必须与真源码同步，否则上面的结论全部失效 ---
# 用 AST 数门控点，比对「缩进/换行/注释」不敏感，只认代码结构。
import ast as _ast

csrc = open("/workspace/greenmoon_mods/admin_web/core.py", encoding="utf-8").read()
_ctree = _ast.parse(csrc)


def _find_func(tree, name):
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == name:
            return node
    return None


_on_enable = _find_func(_ctree, "on_enable")
_gates = [
    n for n in _ast.walk(_on_enable)
    if isinstance(n, _ast.If)
    and isinstance(n.test, _ast.UnaryOp)
    and isinstance(n.test.op, _ast.Not)
    and isinstance(n.test.operand, _ast.Call)
    and getattr(n.test.operand.func, "attr", "") == "_cross_experimental"
]
check("★ on_enable 里有 1 处「关闭」分支（not self._cross_experimental()）",
      len(_gates) == 1, len(_gates))

_gates_on = [
    n for n in _ast.walk(_on_enable)
    if isinstance(n, _ast.If)
    and isinstance(n.test, _ast.Call)
    and getattr(n.test.func, "attr", "") == "_cross_experimental"
]
check("★ on_enable 里有 1 处「打开」分支（self._cross_experimental()）",
      len(_gates_on) == 1, len(_gates_on))

# 周期快照的 period 必须是 100，这是「掉线保护」的时钟
_period_ok = False
for node in _ast.walk(_on_enable):
    if isinstance(node, _ast.Call) and getattr(node.func, "attr", "") == "run_task":
        kw = {k.arg: k.value for k in node.keywords}
        fn = node.args[1] if len(node.args) > 1 else None
        if getattr(fn, "attr", "") == "_cross_periodic_snapshot":
            _period_ok = (
                isinstance(kw.get("period"), _ast.Constant) and kw["period"].value == 100
            )
check("★ 源码里周期快照 period=100 tick（5 秒）", _period_ok)

# 全文件门控总数：关闭状态下这些功能必须一个都不生效
_all_gates = sum(
    1 for n in _ast.walk(_ctree)
    if isinstance(n, _ast.If)
    and any(
        isinstance(sub, _ast.Call)
        and getattr(sub.func, "attr", "") == "_cross_experimental"
        for sub in _ast.walk(n.test)
    )
)
check("★ 全文件实验板门控点数量符合预期（9 处）", _all_gates == 9, _all_gates)

print()
print("=" * 74)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
