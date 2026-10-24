#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 GreenMoon 的 gm-ico 图标全部换成 Tabler Icons Outline（MIT，24×24 描边）。

产出
----
1. 完整的 <style id="gm-icon-style"> 块内容（CSS mask 版，静态图标零 DOM 开销）
2. 供 morphicons 形变用到的 path 数据（只导出会动的那几个，省体积）
"""
import os
import re
import sys

TABLER = os.environ.get("GM_TABLER", "/tmp/mfetch/x/tabler-icons-3.49.0/icons/outline")
HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, os.pardir, "admin.html")
OUT_CSS = "/tmp/out_icon_style.txt"
OUT_JS = "/tmp/out_icon_data.js"

# 现有 data-ic 名 -> Tabler 图标名（"@" 开头是本地手绘的特例）
MAP = {
    "arrow-d": "arrow-down",
    "arrow-l": "arrow-left",
    "arrow-lr": "arrows-left-right",
    "arrow-r": "arrow-right",
    "arrow-u": "arrow-up",
    "ban": "ban",
    "book": "book",
    "box": "box",
    "bulb": "bulb",
    "chart": "chart-histogram",
    "chat": "message-circle",
    "check": "check",
    "clock": "clock",
    "cloud": "cloud",
    "code": "code",
    "cpu": "cpu",
    "cross": "x",
    "cube": "cube",
    "dice": "dice",
    "disk": "device-floppy",
    "doc": "file-text",
    "dot": "@DOT",
    "edit": "pencil",
    "enter": "corner-down-left",
    "eye": "eye",
    "flask": "flask-2",
    "folder": "folder",
    "gamepad": "device-gamepad-2",
    "gear": "settings",
    "gift": "gift",
    "globe": "world",
    "heart": "heart",
    "help": "help",
    "hourglass": "hourglass",
    "key": "key",
    "link": "link",
    "list": "list",
    "lock": "lock",
    "map": "map",
    "megaphone": "speakerphone",
    "menu": "menu-2",
    "microscope": "microscope",
    "monitor": "device-desktop",
    "moon": "moon",
    "note": "note",
    "palette": "palette",
    "pause": "player-pause",
    "phone": "device-mobile",
    "pin": "map-pin",
    "plug": "plug",
    "plus": "plus",
    "puzzle": "puzzle",
    "refresh": "refresh",
    "rewind": "history",
    "ring": "circle",
    "robot": "robot",
    "search": "search",
    "send": "send",
    "server": "server",
    "shield": "shield",
    "spark": "sparkles",
    "star": "star",
    "steth": "stethoscope",
    "stop": "player-stop",
    "sun": "sun",
    "tag": "tag",
    "target": "target",
    "toolbox": "terminal-2",
    "trash": "trash",
    "tri-d": "chevron-down",
    "tri-r": "player-play",
    "undo": "arrow-back-up",
    "upload": "upload",
    "user": "user",
    "users": "users",
    "warn": "alert-triangle",
    "wrench": "tool",
    "x": "x",
}

# Tabler 里没有 circle-filled（outline 全是描边），状态圆点自己画一个实心圆
SPECIAL = {"@DOT": "M12 7.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9z"}

# 会参与形变的图标（只有这些需要把 d 数据带进 JS）
MORPH_ICONS = [
    "refresh", "check", "tri-r", "pause", "stop", "menu", "x",
    "tri-d", "arrow-u", "arrow-d", "plus", "sun", "moon", "warn", "gear",
    "disk", "arrow-r", "arrow-l",
]

# data URI 里保留这些字符不转义，其余（含空格）一律 percent-encode
_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/='.:-_")


def enc(s):
    out = []
    for ch in s:
        if ch in _SAFE:
            out.append(ch)
        elif ch == " ":
            out.append("%20")
        elif ch == "+":
            out.append("%2B")
        else:
            out.append("%%%02X" % ord(ch))
    return "".join(out)


def trim_num(m):
    """小数截到 2 位：24 单位网格下 0.01 的位移肉眼不可见，能省不少字节。"""
    v = float(m.group(0))
    s = "%.2f" % v
    s = s.rstrip("0").rstrip(".")
    return s if s in ("0", "-0") else s


def clean_d(d):
    d = re.sub(r"-?\d+\.\d+", trim_num, d)
    d = re.sub(r"\s+", " ", d).strip()
    d = re.sub(r" ?(-)", r"\1", d)          # 负数前的空格可以省掉
    d = re.sub(r"(-?[\d.]+)\s+(-?[\d.]+)", r"\1 \2", d)
    return d


def load_paths(name):
    """取某个 Tabler 图标的全部 path d（丢掉那条 stroke=none 的背景路径）。"""
    if name.startswith("@"):
        return [SPECIAL[name]]
    path = os.path.join(TABLER, name + ".svg")
    if not os.path.isfile(path):
        raise SystemExit("Tabler 里没有这个图标: %s" % name)
    svg = open(path, encoding="utf-8").read()
    ds = re.findall(r'<path\s+([^>]*?)d="([^"]+)"', svg)
    out = []
    for attrs, d in ds:
        if "stroke=\"none\"" in attrs or "stroke='none'" in attrs:
            continue          # Tabler 每条都带的空背景路径
        out.append(clean_d(d))
    if not out:
        raise SystemExit("没有可用的 path: %s" % name)
    return out


def svg_of(ds):
    inner = "".join("<path d='%s'/>" % d for d in ds)
    return ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' "
            "fill='none' stroke='black' stroke-width='2' "
            "stroke-linecap='round' stroke-linejoin='round'>%s</svg>" % inner)


def main():
    html = open(HTML, encoding="utf-8").read()
    used = sorted(set(re.findall(r"gm-ico\[data-ic=([a-z0-9-]+)\]", html)))
    missing = [k for k in used if k not in MAP]
    if missing:
        raise SystemExit("这些图标还没映射: %s" % missing)

    # 按现有 data-ic 名排序输出，保证每次生成结果稳定（便于 diff）
    lines = []
    css = []
    data = {}
    for key in sorted(MAP):
        if key not in used:
            continue
        ds = load_paths(MAP[key])
        data[key] = ds
        uri = "data:image/svg+xml," + enc(svg_of(ds))
        lines.append(
            ".gm-ico[data-ic=%s]{-webkit-mask-image:url(\"%s\");mask-image:url(\"%s\");"
            "background-color:currentColor}" % (key, uri, uri)
        )
        css.append(".gm-ico[data-ic=%s]" % key)

    head = (".gm-ico{display:inline-block;width:1.1em;height:1.1em;vertical-align:-0.15em;"
            "background-color:transparent;-webkit-mask-repeat:no-repeat;mask-repeat:no-repeat;"
            "-webkit-mask-position:center center;mask-position:center center;"
            "-webkit-mask-size:contain;mask-size:contain;flex:0 0 auto}\n"
            ".gm-ico-emoji{display:inline-block;min-width:1.1em;font-size:1em;"
            "line-height:1.1;text-align:center;vertical-align:-0.12em}\n")
    note = ("/* 本面板的图标来自 Tabler Icons（Outline，24×24 描边）—— "
            "MIT © Paweł Kuna\n"
            "   https://github.com/tabler/tabler-icons\n"
            "   图标之间的形状渐变由 morphicons 驱动，见 admin.morph.js。 */\n")
    block = note + head + "\n".join(lines)
    open(OUT_CSS, "w", encoding="utf-8").write(block)

    # 形变用的 path 数据：多个子路径拼成一个 d 字符串（morphicons 接受 d 串）
    items = []
    for key in MORPH_ICONS:
        if key not in data:
            raise SystemExit("形变图标不存在: %s" % key)
        items.append('"%s":"%s"' % (key, " ".join(data[key])))
    js = "/* 供 morphicons 形变的图标路径（Tabler Icons Outline, MIT） */\n"
    js += "var GM_MORPH_D={%s};\n" % ",".join(items)
    open(OUT_JS, "w", encoding="utf-8").write(js)

    print("图标 %d 个，CSS %d 字节，JS %d 字节"
          % (len(lines), len(block.encode()), len(js.encode())))
    m = re.search(r'<style id="gm-icon-style">.*?</style>', html, re.S)
    print("旧 style 块：%s，%d 字节" % ("找到" if m else "没找到！",
                                       len(m.group(0).encode()) if m else 0))
    if "--apply" in sys.argv:
        if not m:
            raise SystemExit("HTML 里没有 gm-icon-style 块")
        new = '<style id="gm-icon-style">\n' + block + "\n</style>"
        open(HTML, "w", encoding="utf-8").write(html[:m.start()] + new + html[m.end():])
        print("已写回 %s（新块 %d 字节）" % (HTML, len(new.encode())))
    for key in sorted(MAP):
        if key in used:
            print("  %-10s <- %-18s %s" % (key, MAP[key],
                                           " ".join(x[:28] for x in data[key])[:60]))


if __name__ == "__main__":
    main()
