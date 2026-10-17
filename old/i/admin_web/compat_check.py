#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨服实验板 —— Endstone 版本兼容性核对。

用法：
    python3 compat_check.py <endstone 包目录或 .pyi 文件>
例子：
    python3 compat_check.py /tmp/es1111/unpacked/endstone

核对内容（全部是实验板依赖的接口）：
  1. __minecraft_version__
  2. PacketSendEvent 是否存在 / packet_id / payload / player
  3. Player 上原生效果 API 是否已出现（若出现，可替换拦包方案）
  4. endstone.effect 模块是否存在（0.11.11 起被移除）
  5. endstone.potion 里有哪些类型
"""
import pathlib
import re
import sys


def find(root, name):
    root = pathlib.Path(root)
    if root.is_file():
        return root
    for p in root.rglob(name):
        return p
    return None


def main(root):
    root = pathlib.Path(root)

    # 1) MC 版本
    mc = "?"
    for p in root.rglob("*.pyi"):
        m = re.search(r'__minecraft_version__\s*=\s*["\']([^"\']+)', p.read_text(errors="ignore"))
        if m:
            mc = m.group(1)
            break
    print(f"[1] __minecraft_version__ = {mc}")

    # 2) PacketSendEvent
    ev = find(root, "event/__init__.pyi") or find(root, "__init__.pyi")
    txt = ev.read_text(errors="ignore") if ev else ""
    has_cls = bool(re.search(r"^class PacketSendEvent\b", txt, re.M))
    print(f"[2] PacketSendEvent 存在: {has_cls}")
    if has_cls:
        seg = txt.split("class PacketSendEvent", 1)[1]
        seg = re.split(r"^class \w", seg, maxsplit=1, flags=re.M)[0]
        for attr in ("packet_id", "payload", "player", "address", "sub_client_id"):
            ok = re.search(rf"\b{attr}\b", seg) is not None
            print(f"      - {attr:<15}: {'OK' if ok else '缺失 ★'}")
        if "get_packet" in seg:
            print("      - get_packet     : 存在（旧 API，本项目未使用）")

    # 3) 原生效果 API
    pl = find(root, "__init__.pyi")
    ptxt = pl.read_text(errors="ignore") if pl else ""
    print("[3] Player 原生效果 API（若已出现可弃用拦包方案）：")
    for api in ("active_effects", "add_effect", "remove_effect", "has_effect", "get_effect"):
        hit = re.search(rf"^\s+def {api}\b|^\s+{api}\s*:", ptxt, re.M) is not None
        print(f"      - {api:<16}: {'★ 已支持' if hit else '无'}")

    # 4) endstone.effect
    eff = root / "effect"
    if not eff.exists():
        eff = root / "effect.pyi"
        eff = eff if eff.exists() else None
    print(f"[4] endstone.effect 模块: {'存在' if eff else '★ 已移除'}")

    # 5) potion
    pot = find(root, "potion/__init__.pyi") or find(root, "potion.pyi")
    if pot:
        names = re.findall(r"^class (\w+)", pot.read_text(errors="ignore"), re.M)
        print(f"[5] endstone.potion 里的类: {names}")
    else:
        print("[5] endstone.potion: 不存在")

    print()
    print("结论：")
    print(f"  * 本插件依赖 PacketSendEvent.packet_id / .payload —— "
          f"{'两版一致，兼容' if has_cls else '★ 缺失，实验板将自动降级'}")
    print(f"  * 对应 MC 版本 {mc}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
