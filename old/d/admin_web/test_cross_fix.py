"""跨服同步修复的离线验证。

不依赖 Endstone 运行时：把 core.py 里的纯逻辑方法摘出来，
用真实的 MobEffectPacket 字节流和构造出的快照 dict 直接测。

跑法： python3 test_cross_fix.py
"""
import sys
import types
import importlib.util

CORE = "/workspace/greenmoon_mods/admin_web/core.py"

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


# ---------------------------------------------------------------- 载入被测代码
# core.py 是包内模块，直接 import 会拉起一堆 Endstone 依赖。
# 这里只把它的**类体**取出来，剥掉继承和装饰器，单独构造一个最小宿主类。
def load_methods():
    src = open(CORE, encoding="utf-8").read()
    start = src.index("class GreenMoonPlugin")
    body = src[start:]

    # 用 AST 精确截出这个类，避免把类后面的顶层代码一起吃进来
    import ast
    tree = ast.parse(body)
    cls = tree.body[0]
    assert isinstance(cls, ast.ClassDef), "没找到 GreenMoonPlugin 类定义"
    # 剥掉基类（Plugin）与所有装饰器
    cls.bases = []
    cls.decorator_list = []
    _ENDSTONE_TYPES = {
        "PlayerChatEvent", "BroadcastMessageEvent", "PlayerJoinEvent",
        "PlayerQuitEvent", "MapInitializeEvent", "Player", "Command",
        "CommandSender", "PacketSendEvent",
    }
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        node.decorator_list = []
        # 注解在 exec 时会被求值，而这些类型来自 Endstone，测试环境没有
        args = node.args
        for a in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
            if a.annotation is not None and _references(a.annotation, _ENDSTONE_TYPES):
                a.annotation = None
        if node.returns is not None and _references(node.returns, _ENDSTONE_TYPES):
            node.returns = None
    cls = ast.fix_missing_locations(cls)
    module = ast.Module(body=[cls], type_ignores=[])
    code = compile(module, CORE, "exec")

    import threading as _t
    import json as _json
    import hashlib as _hashlib
    import re as _re
    import os as _os
    import sys as _sys
    import ast as _ast
    import time as _time
    import collections as _collections
    import random as _random
    import base64 as _base64
    import uuid as _uuid
    import shutil as _shutil
    import subprocess as _subprocess
    import glob as _glob
    from pathlib import Path as _Path
    from datetime import datetime as _dt, timedelta as _td
    from typing import Optional as _Opt, Dict as _Dict, Any as _Any, Callable as _Callable, List as _List, Tuple as _Tuple
    from concurrent.futures import ThreadPoolExecutor as _TPE

    ns = {
        "threading": _t, "json": _json, "hashlib": _hashlib, "re": _re,
        "os": _os, "sys": _sys, "ast": _ast, "time": _time,
        "collections": _collections, "random": _random, "base64": _base64,
        "uuid": _uuid, "shutil": _shutil, "subprocess": _subprocess,
        "glob": _glob, "zipfile": __import__("zipfile"), "tempfile": __import__("tempfile"),
        "ipaddress": __import__("ipaddress"),
        "urllib": __import__("urllib"),
        "Path": _Path, "datetime": _dt, "timedelta": _td,
        "Optional": _Opt, "Dict": _Dict, "Any": _Any, "Callable": _Callable,
        "List": _List, "Tuple": _Tuple, "ThreadPoolExecutor": _TPE,
        "Player": object, "ItemType": object, "MapMeta": object,
        "MapView": object, "MapRenderer": object, "MapCanvas": object,
        "Command": object, "CommandSender": object,
        "Criteria": None, "DisplaySlot": None, "ObjectiveSortOrder": None,
        "RenderType": None,
        "event_handler": (lambda f: f),
        "PlayerChatEvent": object, "BroadcastMessageEvent": object,
        "PlayerJoinEvent": object, "PlayerQuitEvent": object,
        "MapInitializeEvent": object,
    }
    exec(code, ns)
    return ns["GreenMoonPlugin"]


def _references(annotation, names):
    """注解里是否引用了测试环境不存在的类型。"""
    import ast as _ast
    for n in _ast.walk(annotation):
        if isinstance(n, _ast.Name) and n.id in names:
            return True
        if isinstance(n, _ast.Attribute):
            return True
    return False


def _unused_threading_mod():
    import threading as _t
    return _t


def make_host():
    H = load_methods()
    h = H.__new__(H)
    h._cross_effect_cache = {}
    import threading
    h._cross_effect_lock = threading.Lock()
    return h


# ---------------------------------------------------------------- 字节编码器
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
    """基岩版 zigzag 编码（PocketMine VarInt::writeSignedInt 用的是这个）。"""
    zz = (n << 1) ^ (n >> 63) if n < 0 else (n << 1)
    zz &= (1 << 64) - 1
    return uvarint(zz)


def mob_effect_packet(runtime_id, event_id, effect_id, amplifier, particles, duration,
                      tick=0, ambient=False):
    """按 PocketMine decodePayload 的顺序拼一个完整的 MobEffectPacket payload。"""
    p = bytearray()
    p += uvarint(runtime_id)          # actor_runtime_id : uvarint64
    p.append(event_id & 0xFF)         # event_id         : u8
    p += svarint(effect_id)           # effect_id        : svarint
    p += svarint(amplifier)           # amplifier        : svarint
    p.append(1 if particles else 0)   # particles        : bool
    p += svarint(duration)            # duration         : svarint
    p += uvarint(tick)                # tick             : uvarint64
    p.append(1 if ambient else 0)     # ambient          : bool
    return bytes(p)


class FakePlayer:
    def __init__(self, rid, uuid="uuid-1", name="Steve"):
        self.runtime_id = rid
        self.unique_id = uuid
        self.name = name

    # 模拟 Endstone 0.11.10：Player 上**没有**效果 API
    def __getattr__(self, item):
        raise AttributeError(item)


class FakeEvent:
    def __init__(self, packet_id, payload, player):
        self.packet_id = packet_id
        self.payload = payload
        self.player = player


# ================================================================ 开始测试
print("=" * 72)
print("1. varint / svarint 解码")
print("=" * 72)
h = make_host()

cases = [
    (uvarint(0), 0, False),
    (uvarint(127), 127, False),
    (uvarint(128), 128, False),
    (uvarint(300), 300, False),
    (uvarint(1 << 40), 1 << 40, False),
    (svarint(0), 0, True),
    (svarint(1), 1, True),
    (svarint(-1), -1, True),
    (svarint(27), 27, True),
    (svarint(-1400), -1400, True),
]
for buf, want, signed in cases:
    got, off = h._read_varint(buf, 0, signed)
    check(f"varint {buf.hex()} signed={signed} -> {want}", got == want and off == len(buf),
          f"got={got} off={off} len={len(buf)}")

# 越界必须抛，而不是返回垃圾值
for bad in (b"", b"\x80", b"\x80\x80\x80\x80\x80\x80"):
    try:
        h._read_varint(bad, 0)
        check(f"越界 {bad.hex()} 应抛异常", False, "没抛")
    except ValueError:
        check(f"越界 {bad.hex()} 抛 ValueError", True)

# 偏移量非 0 的情况
buf = b"\xff" + uvarint(300)
got, off = h._read_varint(buf, 1)
check("非零偏移解码", got == 300 and off == len(buf), f"got={got} off={off}")

print()
print("=" * 72)
print("2. 基岩版效果 ID 映射表")
print("=" * 72)
check("1 -> speed", h._BE_EFFECT_ID[1] == "minecraft:speed", h._BE_EFFECT_ID.get(1))
check("2 -> slowness", h._BE_EFFECT_ID[2] == "minecraft:slowness")
check("8 -> jump_boost（跳跃提升）", h._BE_EFFECT_ID[8] == "minecraft:jump_boost")
check("22 -> absorption（伤害吸收）", h._BE_EFFECT_ID[22] == "minecraft:absorption")
check("★ 27 -> slow_falling（缓降）", h._BE_EFFECT_ID[27] == "minecraft:slow_falling",
      h._BE_EFFECT_ID.get(27))
check("28 -> bad_omen（不是 slow_falling，基岩版与 Java 错位）",
      h._BE_EFFECT_ID[28] == "minecraft:bad_omen")
check("30 -> darkness", h._BE_EFFECT_ID[30] == "minecraft:darkness")
check("表中无 0 / 无 31+", 0 not in h._BE_EFFECT_ID and 31 not in h._BE_EFFECT_ID)

print()
print("=" * 72)
print("3. 拦包解码（MobEffectPacket id=28）")
print("=" * 72)
h = make_host()
p = FakePlayer(rid=4242)

# Add 缓降，等级 1，剩 1400 tick
ev = FakeEvent(28, mob_effect_packet(4242, 1, 27, 0, True, 1400), p)
h.on_cross_packet_send(ev)
lst = h._cross_effect_list(p)
check("ADD 缓降 被缓存", len(lst) == 1 and lst[0]["t"] == "minecraft:slow_falling",
      lst)
check("ADD 缓降 amplifier 正确", lst and lst[0]["a"] == 0, lst)
check("ADD 缓降 duration 正确", lst and lst[0]["d"] == 1400, lst)

# Add 疾跑加速，等级 3（amplifier=2）
h.on_cross_packet_send(FakeEvent(28, mob_effect_packet(4242, 1, 1, 2, True, 600), p))
lst = h._cross_effect_list(p)
speeds = [e for e in lst if e["t"] == "minecraft:speed"]
check("ADD 疾跑加速 被缓存", len(speeds) == 1, lst)
check("ADD 疾跑加速 amplifier=2", speeds and speeds[0]["a"] == 2, speeds)

# Modify 缓降：等级不变，时间刷新
h.on_cross_packet_send(FakeEvent(28, mob_effect_packet(4242, 2, 27, 1, True, 3000), p))
lst = h._cross_effect_list(p)
sf = [e for e in lst if e["t"] == "minecraft:slow_falling"]
check("MODIFY 覆盖旧值（amplifier 0->1）", sf and sf[0]["a"] == 1, sf)
check("MODIFY 刷新 duration -> 3000", sf and sf[0]["d"] == 3000, sf)
check("MODIFY 不影响其它效果", len(lst) == 2, lst)

# Remove 缓降
h.on_cross_packet_send(FakeEvent(28, mob_effect_packet(4242, 3, 27, 0, False, 0), p))
lst = h._cross_effect_list(p)
check("REMOVE 缓降后只剩 1 个", len(lst) == 1 and lst[0]["t"] == "minecraft:speed", lst)

print()
print("=" * 72)
print("4. 拦包的边界情况")
print("=" * 72)
h = make_host()
p = FakePlayer(rid=100)

# 4.1 别人的 buff 不能记到我头上
other = FakePlayer(rid=999, uuid="uuid-other", name="Alex")
h.on_cross_packet_send(FakeEvent(28, mob_effect_packet(999, 1, 27, 0, True, 1400), p))
check("★ 发给别人的效果包不被误记", h._cross_effect_list(p) == [],
      h._cross_effect_list(p))

# 4.2 非 28 号包直接短路
h.on_cross_packet_send(FakeEvent(9, mob_effect_packet(100, 1, 27, 0, True, 1400), p))
check("非 28 号包被忽略", h._cross_effect_list(p) == [])

# 4.3 截断的 payload 不能崩
for trunc in range(1, 8):
    full = mob_effect_packet(100, 1, 27, 0, True, 1400)
    try:
        h.on_cross_packet_send(FakeEvent(28, full[:trunc], p))
        ok = True
    except Exception as e:
        ok = False
        print("      异常:", e)
    check(f"截断 payload len={trunc} 不抛异常", ok)

# 4.4 垃圾字节不能崩
import os
for i in range(200):
    try:
        h.on_cross_packet_send(FakeEvent(28, os.urandom(i % 20), p))
    except Exception as e:
        check(f"随机字节不抛异常 (len={i%20})", False, e)
        break
else:
    check("随机字节 200 次均不抛异常", True)

# 4.5 packet_id 不是 int
try:
    h.on_cross_packet_send(FakeEvent(None, b"xx", p))
    check("packet_id=None 不抛异常", True)
except Exception as e:
    check("packet_id=None 不抛异常", False, e)

# 4.6 player 为 None（官方文档说登录前可能是 None）
try:
    h.on_cross_packet_send(FakeEvent(28, mob_effect_packet(100, 1, 27, 0, True, 10), None))
    check("player=None 不抛异常", True)
except Exception as e:
    check("player=None 不抛异常", False, e)

# 4.7 未初始化缓存的实例（on_enable 之前收到包）
h2 = make_host()
h2._cross_effect_cache = None
try:
    h2.on_cross_packet_send(FakeEvent(28, mob_effect_packet(1, 1, 1, 0, True, 10), p))
    check("缓存未初始化时安全返回", True)
except Exception as e:
    check("缓存未初始化时安全返回", False, e)

print()
print("=" * 72)
print("5. 效果倒计时")
print("=" * 72)
h = make_host()
p = FakePlayer(rid=7)
h.on_cross_packet_send(FakeEvent(28, mob_effect_packet(7, 1, 27, 0, True, 3), p))
check("倒计时前 duration=3", h._cross_effect_list(p)[0]["d"] == 3)
h._cross_tick_effect_cache()
check("扣 1 次 -> 2", h._cross_effect_list(p)[0]["d"] == 2)
h._cross_tick_effect_cache()
h._cross_tick_effect_cache()
check("扣到 0 后效果被移除", h._cross_effect_list(p) == [], h._cross_effect_list(p))

# 无限时长不衰减
h.on_cross_packet_send(FakeEvent(28, mob_effect_packet(7, 1, 1, 0, True, -1), p))
for _ in range(50):
    h._cross_tick_effect_cache()
lst = h._cross_effect_list(p)
check("duration<0（无限）不衰减", len(lst) == 1 and lst[0]["d"] == -1, lst)

# 空缓存不炸
h3 = make_host()
h3._cross_tick_effect_cache()
check("空缓存 tick 安全", True)

print()
print("=" * 72)
print("6. 快照校验（掉线保护的核心）")
print("=" * 72)
h = make_host()

good = {
    "health": 20.0, "position": {"x": 1, "y": 64, "z": 2},
    "inventory": [], "ender_chest": [], "effects": [],
    "state": {"is_sprinting": True},
}
check("正常快照通过", h._cross_snapshot_sane(good)[0] is True, h._cross_snapshot_sane(good))

# 玩家对象析构后的典型特征
check("★ 空 dict 被拒", h._cross_snapshot_sane({})[0] is False)
check("★ None 被拒", h._cross_snapshot_sane(None)[0] is False)
check("★ 只有 health 没有容器被拒",
      h._cross_snapshot_sane({"health": 20})[0] is False)
check("★ 有容器但没 position 被拒",
      h._cross_snapshot_sane({"inventory": [], "effects": []})[0] is False)
check("★ 有容器有 position 但 health=NaN 被拒",
      h._cross_snapshot_sane(
          {"health": float("nan"), "position": {"x": 0, "y": 0, "z": 0}, "inventory": []}
      )[0] is False)
check("★ health 为负被拒",
      h._cross_snapshot_sane(
          {"health": -1, "position": {"x": 0, "y": 0, "z": 0}, "inventory": []}
      )[0] is False)

# 合法但「看起来空」的快照必须放行，否则误伤真空背包的玩家
empty_but_real = {
    "health": 20.0, "position": {"x": 0, "y": 0, "z": 0},
    "inventory": [], "ender_chest": [], "effects": [],
}
check("★ 真空背包的正常快照必须放行（不误伤）",
      h._cross_snapshot_sane(empty_but_real)[0] is True)

# 只有 ender_chest 也算容器齐全
check("仅 ender_chest 也算容器齐全",
      h._cross_snapshot_sane(
          {"health": 20, "position": {"x": 0, "y": 0, "z": 0}, "ender_chest": []}
      )[0] is True)

print()
print("=" * 72)
print("7. 快照指纹去重")
print("=" * 72)
h = make_host()
a = {"health": 20, "position": {"x": 0, "y": 0, "z": 0}, "inventory": [{"t": "stone"}]}
b = {"inventory": [{"t": "stone"}], "position": {"x": 0, "y": 0, "z": 0}, "health": 20}
c = {"health": 20, "position": {"x": 1, "y": 0, "z": 0}, "inventory": [{"t": "stone"}]}
fa, fb, fc = (h._cross_snapshot_fingerprint(x) for x in (a, b, c))
check("指纹可用", fa is not None)
check("★ 键序不同但内容相同 -> 指纹相同（不会白写盘）", fa == fb, f"{fa} vs {fb}")
check("内容变化 -> 指纹变化", fa != fc)
# default=str 是刻意设计的：快照里可能混入 ItemType 之类的非原生对象，
# 有 default 兜底就能算指纹，否则整个快照都进不了去重逻辑。
# 只有真正无法 str 的对象（这里用会抛异常的 __str__）才返回 None。
class _Bad:
    def __str__(self):
        raise RuntimeError("boom")
    __repr__ = __str__
check("★ 极端对象不抛异常（能 str 就出指纹，不能就返回 None）",
      h._cross_snapshot_fingerprint({"x": object()}) is not None)
try:
    h._cross_snapshot_fingerprint({"x": _Bad()})
    check("不可 str 的对象不抛异常", True)
except Exception as e:
    check("不可 str 的对象不抛异常", False, e)

print()
print("=" * 72)
print("8. 纳袋 NBT 探针")
print("=" * 72)
h = make_host()
nbt = {"d": {"Name": "minecraft:bundle", "Items": [{"Slot": 0}]}}
hints = h._probe_bundle_nbt(nbt)
check("能列出 d 与 d.Items", "d" in hints and "d.Items" in hints, hints)

deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
hints = h._probe_bundle_nbt(deep)
check("深度被限制在 4 层内", all(len(x.split(".")) <= 5 for x in hints), hints)

check("None 安全", h._probe_bundle_nbt(None) == [])
check("标量安全", h._probe_bundle_nbt({"a": 1, "b": "x"}) == [], h._probe_bundle_nbt({"a": 1}))
# 自引用不能死循环
cyc = {}
cyc["self"] = cyc
hints = h._probe_bundle_nbt(cyc)
check("自引用结构不死循环", True)

print()
print("=" * 72)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 72)
sys.exit(1 if FAIL else 0)
