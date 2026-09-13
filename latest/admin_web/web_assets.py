"""网页前端资源（服务器管理面板）。

前端源码以独立文件存放于本包 static/ 目录：
  - *.html     页面结构
  - *.css      样式（每页一份）
  - *.js       脚本（每页一份）
  - brand/     品牌位图形（gm.svg），由 /__gm/brand.svg 对外提供

html 中用原位标记 <!--__GM_CSS__--> / <!--__GM_JS__--> 占位，
本模块读取后原位回填，得到与内嵌时代完全一致的 HTML 字符串。
"""
from pathlib import Path
from typing import Optional, Tuple

import hashlib

_STATIC = Path(__file__).resolve().parent / "static"

_CSS_MARK = "<!--__GM_CSS__-->"
_JS_MARK = "<!--__GM_JS__-->"

#: 前端资源版本戳标记。脚本是内联进 HTML 的，没有 URL 可以加 ``?v=``，
#: 只能把内容哈希写进页面里 —— 否则浏览器拿旧缓存跑新后端，会出现
#: 「改了没生效 / 群列表串台」这类只影响用户、影响不到开发者的鬼问题。
_JS_REV_MARK = "<!--__GM_JS_REV__-->"

#: 品牌位占位标记，形如 ``<!--__GM_BRAND:page-brand__-->``。
_BRAND_PREFIX = "<!--__GM_BRAND:"
_BRAND_SUFFIX = "__-->"

#: 品牌位 SVG 的对外路径。改了这里要同步改 web.py 的 BRAND_ASSET_PATH，
#: 两者必须一致。
BRAND_URL = "/__gm/brand.svg"


def brand_img(cls: str, alt: str = "服务器管理面板") -> str:
    """生成品牌位 ``<img>`` 标签，供 ``<!--__GM_BRAND:<class>__-->`` 标记回填。"""
    return f'<img class="{cls}" src="{BRAND_URL}" alt="{alt}">'


def _fill_brand(html: str) -> str:
    """把 ``<!--__GM_BRAND:<class>__-->`` 展开成真实的 img 标签。

    用简单字符串扫描而非正则：标记格式完全自控，扫描更直观也更快。
    """
    out = []
    idx = 0
    while True:
        start = html.find(_BRAND_PREFIX, idx)
        if start < 0:
            out.append(html[idx:])
            break
        end = html.find(_BRAND_SUFFIX, start)
        if end < 0:
            out.append(html[idx:])
            break
        cls = html[start + len(_BRAND_PREFIX):end].strip() or "brand-img"
        out.append(html[idx:start])
        out.append(brand_img(cls))
        idx = end + len(_BRAND_SUFFIX)
    return "".join(out)


_UPLOAD_EXT = (".mcaddon", ".mcpack", ".gmmod", ".gmlib")

#: 落盘文件名里允许出现的字符。其余一律换成下划线。
_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._-"
)


def upload_display_name(raw: object) -> str:
    """从原始文件名里取一个「人看的」名字：去掉目录部分，再摘掉编码残留。

    浏览器的 form 提交在部分实现里会把文件名按表单页面的字符集先编码一次，
    于是 ``中文.mcpack`` 会变成 ``%E4%B8%AD%E6%96%87.mcpack`` 或
    ``ä¸æ–‡.mcpack``（UTF-8 字节按 latin-1 解码）。这里做一次可逆尝试：
    解出来像样就用解出来的，解不出就原样返回。
    """
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name:
        return ""
    from urllib.parse import unquote
    for _ in range(2):
        if "%" not in name:
            break
        try:
            dec = unquote(name, encoding="utf-8", errors="strict")
        except Exception:
            break
        if dec and dec != name:
            name = dec
        else:
            break
    try:
        fixed = name.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        fixed = name
    if fixed and fixed != name:
        name = fixed
    return name.replace("\\", "/").rsplit("/", 1)[-1].strip()


def safe_upload_name(raw: object, prefix: str = "") -> Tuple[str, str]:
    """把任意（可能含中文的）文件名收敛成一个纯 ASCII 的落盘名。

    返回 ``(落盘名, 原始显示名)``。落盘名必然只含 ``[A-Za-z0-9._-]``，
    因此可以安全地出现在 HTTP 头、URL、JSON 以及任何 locale 受限的环境里
    （这正是 ``'ascii' codec can't encode ...`` 那类报错的来源）。

    中文等非 ASCII 字符不会丢：它们会被剔除出落盘名，但完整保留在
    显示名里，由调用方存进清单、只用于界面展示。
    """
    display = upload_display_name(raw)
    stem, dot, ext = display.rpartition(".")
    if not dot:
        stem, ext = display, ""
    ext = ext.lower()
    if ext not in {e.lstrip(".") for e in _UPLOAD_EXT}:
        ext = "bin"
    stem = "".join(ch if ch in _SAFE_CHARS else "_" for ch in stem)
    stem = stem.strip("._-")
    while "__" in stem or "--" in stem or ".." in stem:
        stem = stem.replace("__", "_").replace("--", "-").replace("..", ".")
    if not stem:
        stem = "pack"
    name = f"{prefix}{stem[:60]}.{ext}"
    return name, display


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def _build(key: str) -> str:
    html = _read(key + ".html")
    css_path = _STATIC / (key + ".css")
    js_path = _STATIC / (key + ".js")
    if _BRAND_PREFIX in html:
        html = _fill_brand(html)
    if _CSS_MARK in html and css_path.exists():
        html = html.replace(_CSS_MARK, css_path.read_text(encoding="utf-8"))
    if _JS_MARK in html and js_path.exists():
        js_text = js_path.read_text(encoding="utf-8")
        html = html.replace(_JS_MARK, js_text)
        if _JS_REV_MARK in html:
            digest = hashlib.sha1(js_text.encode("utf-8")).hexdigest()[:12]
            html = html.replace(
                _JS_REV_MARK,
                f'<meta name="gm-js-rev" content="{key}-{digest}">',
            )
    return html


HTML_LOGIN = _build("login")
HTML_LOGIN_ERROR = _build("login_error")
HTML_INDEX = _build("index")
HTML_WHITELIST = _build("whitelist")
HTML_ADMIN = _build("admin")
