#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 docs/*.md 打包成 docs_content.py（内嵌副本）。

为什么要这个脚本
----------------
面板的文档有**两级来源**：
  1. ``docs/*.md``            —— 服主可以自己改，改了立刻生效；
  2. ``docs_content.py``      —— 内嵌副本，**文件缺失时兜底**。

第 2 级是为了防一种很常见的部署事故：增量更新时只覆盖了 .py，
``docs/`` 目录没传上去，于是点开文档报「找不到文档文件」——
功能明明做了却用不了。

用法（在 admin_web/ 目录下）::

    python3 _gen_docs_content.py

改完 docs/*.md 记得跑一次，否则内嵌副本会落后于文件版本
（两级不一致时以文件为准，所以只有"文件也丢了"才会露馅）。
"""
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ITEMS = [("webui", "webui.md"), ("modapi", "modapi.md"), ("terms", "terms.md")]


def main() -> int:
    docs_dir = HERE / "docs"
    missing = [f for _, f in ITEMS if not (docs_dir / f).is_file()]
    if missing:
        print("缺少源文件：%s" % ", ".join(missing), file=sys.stderr)
        return 1

    lines = [
        "# -*- coding: utf-8 -*-",
        '"""内置文档的**内嵌副本**（自动生成，请勿手改）。',
        "",
        "由 ``_gen_docs_content.py`` 从 ``docs/*.md`` 生成。",
        "",
        "为什么要有这个文件",
        "------------------",
        "文档正文原本只放在 ``docs/*.md``。但只要那个目录在部署时漏传（增量更新",
        "只覆盖 .py 是最常见的操作），点开文档就会得到「找不到文档文件」——",
        "功能明明做了却用不了，比没做还让人恼火。",
        "",
        "所以这里再存一份**内嵌副本**作为兜底：",
        "",
        "  * ``docs/*.md`` 存在  -> 用它（方便服主自己改文档，改完即刻生效）",
        "  * 文件缺失/读不出来    -> 用这里的副本（保证一定能看）",
        "",
        "两份内容不一致时以文件为准。改完 md 请重跑 ``_gen_docs_content.py``。",
        '"""',
        "",
        '__all__ = ["EMBEDDED"]',
        "",
    ]
    for key, fname in ITEMS:
        text = io.open(docs_dir / fname, encoding="utf-8").read()
        lines.append("# ---- %s (%s, %d 字符) ----" % (key, fname, len(text)))
        lines.append("%s_MD = %s" % (key.upper(), repr(text)))
        lines.append("")
    lines.append("")
    lines.append("EMBEDDED = {")
    for key, fname in ITEMS:
        lines.append('    "%s": %s_MD,' % (fname, key.upper()))
    lines.append("}")
    lines.append("")

    out = HERE / "docs_content.py"
    io.open(out, "w", encoding="utf-8").write("\n".join(lines))
    print("已生成 %s（%d 字节）" % (out.name, out.stat().st_size))
    for key, fname in ITEMS:
        print("  %-12s %d 字符" % (fname, len(io.open(docs_dir / fname, encoding="utf-8").read())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
