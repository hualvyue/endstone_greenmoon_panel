"""网页前端资源（GreenMoon面板）。

前端源码以独立文件存放于本包 static/ 目录：
  - *.html  页面结构
  - *.css   样式（每页一份）
  - *.js    脚本（每页一份）

html 中用原位标记 <!--__GM_CSS__--> / <!--__GM_JS__--> 占位，
本模块读取后原位回填，得到与内嵌时代完全一致的 HTML 字符串。
"""
from pathlib import Path

_STATIC = Path(__file__).resolve().parent / "static"

_CSS_MARK = "<!--__GM_CSS__-->"
_JS_MARK = "<!--__GM_JS__-->"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def _build(key: str) -> str:
    html = _read(key + ".html")
    css_path = _STATIC / (key + ".css")
    js_path = _STATIC / (key + ".js")
    if _CSS_MARK in html and css_path.exists():
        html = html.replace(_CSS_MARK, css_path.read_text(encoding="utf-8"))
    if _JS_MARK in html and js_path.exists():
        html = html.replace(_JS_MARK, js_path.read_text(encoding="utf-8"))
    return html


HTML_LOGIN = _build("login")
HTML_LOGIN_ERROR = _build("login_error")
HTML_INDEX = _build("index")
HTML_WHITELIST = _build("whitelist")
HTML_ADMIN = _build("admin")
