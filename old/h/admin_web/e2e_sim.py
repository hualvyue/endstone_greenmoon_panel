"""端到端仿真：模拟「玩家在 A 服被上满 buff → 跨服到 B 服」的完整链路。

验证三件事：
  1. 快照带上了疾跑/潜行/飞行状态；
  2. buff 从拦包缓存进入了快照；
  3. 掉线（Player 对象已析构）时坏快照被拒绝，不会覆盖旧数据。

跑法： python3 e2e_sim.py
"""
import ast
import sys

sys.path.insert(0, "/tmp/endstone_real/pkg")

CORE = "/workspace/greenmoon_mods/admin_web/core.py"
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


# ------------------------------------------------------------------ 构造被测类
def build_class():
    from endstone.plugin import Plugin
    from endstone import Player
    from endstone.inventory import ItemType, MapMeta
    from endstone.map import MapView, MapRenderer, MapCanvas
    from endstone.event import (event_handler, PlayerChatEvent, BroadcastMessageEvent,
                                PlayerJoinEvent, PlayerQuitEvent, MapInitializeEvent,
                                PacketSendEvent)
    from endstone.command import Command, CommandSender
    import threading, json, hashlib, re, os as _os, time, collections, random
    import base64, uuid, shutil, subprocess, glob, zipfile, tempfile, ipaddress
    import urllib.request, urllib.error
    from pathlib import Path
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
        "re": re, "json": json, "hashlib": hashlib, "threading": threading,
        "time": time, "os": _os, "collections": collections, "random": random,
        "base64": base64, "uuid": uuid, "shutil": shutil, "subprocess": subprocess,
        "glob": glob, "zipfile": zipfile, "tempfile": tempfile, "ipaddress": ipaddress,
        "datetime": datetime, "timedelta": timedelta,
        "Optional": Optional, "Dict": Dict, "Any": Any, "Callable": Callable,
        "List": List, "Tuple": Tuple,
        "Plugin": Plugin, "Player": Player, "ItemType": ItemType, "MapMeta": MapMeta,
        "MapView": MapView, "MapRenderer": MapRenderer, "MapCanvas": MapCanvas,
        "event_handler": event_handler, "PlayerChatEvent": PlayerChatEvent,
        "BroadcastMessageEvent": BroadcastMessageEvent, "PlayerJoinEvent": PlayerJoinEvent,
        "PlayerQuitEvent": PlayerQuitEvent, "MapInitializeEvent": MapInitializeEvent,
        "PacketSendEvent": PacketSendEvent, "Command": Command, "CommandSender": CommandSender,
        "Path": Path, "ThreadPoolExecutor": ThreadPoolExecutor,
        "Criteria": None, "DisplaySlot": None, "ObjectiveSortOrder": None, "RenderType": None,
        "HTML_LOGIN": object, "HTML_LOGIN_ERROR": object, "HTML_INDEX": object,
        "HTML_WHITELIST": object, "HTML_ADMIN": object,
        "ConnectionManager": object, "ConnectionValidationError": object,
        "_FLAT_SYNC_KEYS": (), "ModLoader": object, "ModLoadError": object,
        "atomic_write_json": object, "atomic_write_text": object, "atomic_write_bytes": object,
        "_urllib_request": urllib.request, "_urllib_error": urllib.error,
    })
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "core.py", "exec"), ns)
    return ns["GreenMoonPlugin"]


G = build_class()


# ------------------------------------------------------------------ 测试替身
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
    def __init__(self):
        self.x, self.y, self.z = 12.5, 64.0, -30.25
        self.dimension = "Overworld"


class FakePlayer:
    """模拟 Endstone 的 Player：有运动状态属性，但**没有**任何效果 API。"""

    def __init__(self, rid=1234, name="Steve", uuid="uuid-steve"):
        self.runtime_id = rid
        self.unique_id = uuid
        self.name = name
        self.health = 20.0
        self.max_health = 20.0
        self.exp_level = 30
        self.exp_progress = 0.5
        self.total_exp = 1000
        self.game_mode = "survival"
        self.location = FakeLoc()
        self.inventory = FakeInv({0: FakeStack("minecraft:diamond_sword")})
        self.ender_chest = FakeInv()
        # 运动/姿态状态（Endstone 真有这些）
        self.is_sprinting = True
        self.is_sneaking = False
        self.is_flying = False
        self.allow_flight = True
        self.fly_speed = 0.05
        self.walk_speed = 0.1

    def __getattr__(self, item):
        # 模拟 Endstone 0.11.10：active_effects / get_active_effects 不存在
        raise AttributeError(f"Player 没有 {item}")


class DeadPlayer:
    """模拟掉线瞬间：对象还在，但所有字段都读不到了。"""

    def __init__(self, name="Steve", uuid="uuid-steve"):
        self.unique_id = uuid
        self.name = name

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


def effect_pkt(rid, eid_ev, eff, amp, dur):
    p = bytearray()
    p += uvarint(rid)
    p.append(eid_ev)
    p += svarint(eff)
    p += svarint(amp)
    p.append(1)
    p += svarint(dur)
    p += uvarint(0)
    p.append(0)
    return bytes(p)


class Ev:
    def __init__(self, pid, payload, player):
        self.packet_id, self.payload, self.player = pid, payload, player


_LOGS = []


class _FakeLogger:
    def warning(self, m):
        _LOGS.append(("W", m))

    def error(self, m):
        _LOGS.append(("E", m))

    def info(self, m):
        _LOGS.append(("I", m))


class _FakeCross:
    """e2e 仿真跑的是实验板打开后的完整链路，所以这里 experimental 固定为 True。
    开关关闭 → 原版行为的对照测试在 test_cross_switch.py 里。
    """

    def __init__(self):
        self.cfg = {"experimental": True}

    def experimental(self):
        return True


def mk_host():
    h = G.__new__(G)
    import threading
    h._cross_effect_cache = {}
    h._cross_effect_lock = threading.Lock()
    h._cross_last_fp = {}
    # Plugin.logger 是只读 property（数据描述符在类上，实例字典盖不住它）。
    # 直接往实例 __dict__ 里塞没用，得临时在**子类**上盖掉这个 property。
    h.__class__ = _HostWithLogger
    h.logger = _FakeLogger()
    h._logs = _LOGS
    # 实验板必须打开，这些场景才成立
    h.cross = _FakeCross()
    return h


class _HostWithLogger(G):
    logger = _FakeLogger()


# ================================================================ 场景
print("=" * 74)
print("场景 1：A 服 → 玩家身上有 缓降 + 疾跑加速 + 迅捷")
print("=" * 74)
h = mk_host()
p = FakePlayer()

# 服务端下发三个效果包
h.on_cross_packet_send(Ev(28, effect_pkt(1234, 1, 27, 1, 2400), p))   # 缓降 II, 120s
h.on_cross_packet_send(Ev(28, effect_pkt(1234, 1, 1, 2, 600), p))     # 迅捷 III, 30s
h.on_cross_packet_send(Ev(28, effect_pkt(1234, 1, 8, 0, 200), p))     # 跳跃提升 I, 10s

snap = h._cross_snapshot_player(p)
print(f"  快照 effects = {snap['effects']}")
print(f"  快照 state   = {snap['state']}")
print()

names = sorted(e["t"] for e in snap["effects"])
check("★ 缓降进入快照", "minecraft:slow_falling" in names, names)
check("★ 迅捷进入快照", "minecraft:speed" in names, names)
check("★ 跳跃提升进入快照", "minecraft:jump_boost" in names, names)
check("缓降等级正确 (amp=1)", [e for e in snap["effects"]
                          if e["t"] == "minecraft:slow_falling"][0]["a"] == 1)
check("缓降时长正确 (2400 tick)", [e for e in snap["effects"]
                             if e["t"] == "minecraft:slow_falling"][0]["d"] == 2400)

check("★ 疾跑状态进入快照", snap["state"].get("is_sprinting") is True, snap["state"])
check("★ 飞行许可进入快照", snap["state"].get("allow_flight") is True)
check("★ 飞行速度进入快照", snap["state"].get("fly_speed") == 0.05)
check("潜行=false 也被记录（区分「没读到」与「没潜行」）",
      "is_sneaking" in snap["state"] and snap["state"]["is_sneaking"] is False)

print()
print("=" * 74)
print("场景 2：快照校验通过，可以安全上传")
print("=" * 74)
ok, why = h._cross_snapshot_sane(snap)
check("正常快照通过校验", ok, why)

print()
print("=" * 74)
print("场景 3：B 服恢复（把快照写回一个新玩家）")
print("=" * 74)
h2 = mk_host()
p2 = FakePlayer(rid=7777, name="Steve", uuid="uuid-steve")
p2.is_sprinting = False
p2.allow_flight = False
p2.fly_speed = 0.0
p2.walk_speed = 0.0

# 绕过需要真服务器的部分，直接测 _cross_restore_state
h2._cross_restore_state(p2, snap)
check("★ 疾跑被恢复", p2.is_sprinting is True, p2.is_sprinting)
check("★ 飞行许可被恢复", p2.allow_flight is True, p2.allow_flight)
check("★ 飞行速度被恢复", p2.fly_speed == 0.05, p2.fly_speed)
check("★ 行走速度被恢复（顺带修的老漏项）", p2.walk_speed == 0.1, p2.walk_speed)
check("潜行被恢复为 False", p2.is_sneaking is False)

print()
print("=" * 74)
print("场景 4：★ 掉线保护 —— 玩家对象已析构")
print("=" * 74)
dead = DeadPlayer()
bad_snap = h._cross_snapshot_player(dead)
print(f"  析构后快照 = {bad_snap}")
ok, why = h._cross_snapshot_sane(bad_snap)
check("★ 坏快照被拒绝（不会覆盖好数据）", ok is False, f"ok={ok} why={why}")

print()
print("=" * 74)
print("场景 5：★ 掉线保护 —— 对象还在但内容已清空")
print("=" * 74)


class HalfDead(FakePlayer):
    """对象还在，但 health / location / 容器都读不到了（掉线中途的典型状态）。"""

    def __init__(self, name="Steve", uuid="uuid-steve"):
        # 跳过父类 __init__ 的赋值，避免撞上只读 property
        self.unique_id = uuid
        self.name = name
        self.runtime_id = 555

    @property
    def health(self):
        raise RuntimeError("already despawned")

    def __getattr__(self, item):
        if item in ("location", "inventory", "ender_chest"):
            raise RuntimeError("already despawned")
        raise AttributeError(item)


hd = HalfDead()
snap_hd = h._cross_snapshot_player(hd)
print(f"  半失效快照 = {snap_hd}")
ok, why = h._cross_snapshot_sane(snap_hd)
check("★ 位置读不到 -> 快照被拒", ok is False, f"ok={ok} why={why}")

print()
print("=" * 74)
print("场景 6：旧版快照的向后兼容（不含 state 字段）")
print("=" * 74)
legacy = {
    "health": 20.0,
    "position": {"x": 0, "y": 64, "z": 0},
    "inventory": [], "ender_chest": [], "effects": [],
    # 注意：没有 state
}
p3 = FakePlayer(rid=1)
p3.is_sprinting = True
p3.allow_flight = True
h3 = mk_host()
h3._cross_restore_state(p3, legacy)
check("★ 旧快照不会把疾跑清成 False（键不存在就跳过）",
      p3.is_sprinting is True, p3.is_sprinting)
check("★ 旧快照不会把飞行许可清成 False",
      p3.allow_flight is True, p3.allow_flight)
ok, why = h3._cross_snapshot_sane(legacy)
check("旧快照仍能通过校验", ok is True, why)

print()
print("=" * 74)
print("场景 7：周期快照不去重会打爆磁盘 —— 验证指纹生效")
print("=" * 74)
h4 = mk_host()
puts = []
h4._executor = type("E", (), {"submit": staticmethod(lambda fn, *a: puts.append(a))})()
p4 = FakePlayer(rid=42)

import types as _t
fake_cross = _t.SimpleNamespace(
    enabled=lambda: True,
    interop=lambda k: True,
    # ★ 周期快照整体属于实验板功能，必须显式打开，否则 _cross_periodic_one 直接返回
    experimental=lambda: True,
)
h4.cross = fake_cross

for _ in range(5):
    h4._cross_periodic_one(p4)
check("★ 内容不变时 5 次周期快照只写 1 次盘", len(puts) == 1, f"实际 {len(puts)} 次")

p4.health = 10.0
h4._cross_periodic_one(p4)
check("★ 内容变化后立即再写一次", len(puts) == 2, f"实际 {len(puts)} 次")

print()
print("=" * 74)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
