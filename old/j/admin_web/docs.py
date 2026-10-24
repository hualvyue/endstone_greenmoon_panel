"""面板内置文档 —— 供「🔑 账户与安全 → 📖 文档与条款」展示。

为什么要内置，而不是丢一个外链
-------------------------------
服主在服务器上遇到问题时，往往**没有外网、也没有耐心去翻仓库**。把文档
装进面板里，他点开就能查：怎么接 WebUI、子插件能调什么、以及这份功能的
风险和使用条款（尤其"回档不可逆""AI 完全放开意味着什么"这类必须讲清的事）。

文档以 **Markdown 源文件**存放在 ``docs/`` 目录里（随包分发），本模块只负
责读出来转成 HTML：

* 有 ``markdown`` 库 → 转成正式 HTML；
* 没有 → 降级成 ``<pre>`` 原文（**仍然可读**，只是没有排版）。

刻意不做"在线拉取"：服务器可能没有外网，而且文档必须和当前安装的版本对上。
在线版一旦滞后于本地版本，讲的就和实际行为不一致了 —— 那比没有文档更糟。
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["list_docs", "render_doc", "DOCS"]

_DOCS_DIR = Path(__file__).resolve().parent / "docs"

#: 文档清单。``id`` 是前端请求用的键，``file`` 是 docs/ 下的文件名。
#: 顺序即展示顺序：先开发者文档，再使用条款。
DOCS: List[Dict[str, str]] = [
    {
        "id": "webui",
        "title": "外部插件接入 WebUI 指南",
        "desc": "第三方插件如何把自己的设置面板挂进本面板：两种接入方式、"
                "字段类型、回调契约与安全边界。",
        "file": "webui.md",
    },
    {
        "id": "modapi",
        "title": "子插件 API（gm 模块）",
        "desc": "子插件能用的全部能力：模块清单、权限申请、常用示例与常见报错。",
        "file": "modapi.md",
    },
    {
        "id": "terms",
        "title": "使用须知与使用条款",
        "desc": "功能风险说明（区块回档 / AI 接管级别 / 第三方插件）与免责条款。",
        "file": "terms.md",
    },
]

_BY_ID = {d["id"]: d for d in DOCS}


def list_docs() -> List[Dict[str, str]]:
    """返回文档清单（不含正文，供前端列按钮）。"""
    return [{"id": d["id"], "title": d["title"], "desc": d["desc"]} for d in DOCS]


def _read(name: str) -> Optional[str]:
    """读一篇文档原文：**先读文件，读不到就用内嵌副本**。

    ★ 为什么要两级（这是踩过的坑）：
      最初只有 ``docs/*.md`` 这一份。结果部署时只要那个目录漏传
      （增量更新只覆盖 .py 是最常见的操作），点开文档就是
      「找不到文档文件」—— 功能明明做了却用不了，比不做还让人恼火。

      现在：文件在就用文件（服主可以自己改文档，改完立刻生效），
      文件不在就退到 ``docs_content.py`` 里的内嵌副本。**保证一定能看。**
      两份不一致时以文件为准。

    ★ 只认白名单里的文件名（来自 ``DOCS``），绝不拼接外部传入的路径。
    """
    try:
        path = _DOCS_DIR / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if text.strip():
                return text
    except Exception:
        pass
    try:
        from .docs_content import EMBEDDED

        text = EMBEDDED.get(name)
        if text and str(text).strip():
            return str(text)
    except Exception:
        pass
    return None


def _to_html(md: str) -> str:
    """Markdown → HTML。没有 markdown 库时降级为原文（仍然可读）。"""
    try:
        import markdown  # type: ignore

        return markdown.markdown(md, extensions=["tables", "fenced_code"])
    except Exception:
        # ★ 降级路径也要保证安全：整段转义后放进 <pre>。
        return "<pre class='doc-plain'>" + html.escape(md) + "</pre>"


def render_doc(doc_id: str) -> Dict[str, Any]:
    """渲染一篇文档。

    返回 ``{"ok": bool, "id", "title", "html"}`` 或 ``{"ok": False, "error"}``。
    **不抛异常** —— 面板上任何一个小角落炸掉都不该影响整页。
    """
    d = _BY_ID.get(str(doc_id or "").strip())
    if d is None:
        return {"ok": False, "error": f"没有 id 为 {doc_id!r} 的文档"}
    md = _read(d["file"])
    if md is None:
        return {
            "ok": False,
            "error": f"文档不可用：文件 docs/{d['file']} 与内嵌副本都读不到。"
                     f"这通常意味着插件包不完整，请用完整包重新覆盖一次。",
        }
    try:
        body = _to_html(md)
    except Exception as e:
        body = "<pre class='doc-plain'>" + html.escape(md) + "</pre>"
        body += "<p class='hint'>（渲染降级：" + html.escape(str(e)) + "）</p>"
    return {"ok": True, "id": d["id"], "title": d["title"], "html": body}
