"""「控制台标签切换」修复与「运行环境」新面板的浏览器验证。

被验的是**产品代码本身**：直接取 ``web_assets.HTML_ADMIN`` —— 服务器真实
吐给浏览器的那个字符串（已把 admin.css / admin.js 原位回填）。不做手工拼接，
因此测试通过就代表真实页面行为正确。

页面用**真实的本地 HTTP 服务**加载：admin.js 所有请求都走 ``apiPath()``
拼相对路径，``file://`` 下相对 URL 没有 base 可解析，``fetch`` 会直接抛
``Failed to parse URL``。

两组验证点
----------
A. 控制台标签切换（修复回归）
   历史 bug：``#console-pane`` 上同时挂了 ``.hidden`` 类和 ``style="display:none"``，
   而 ``switchLogTab`` 只改内联样式 → 清空后 ``.hidden`` 的
   ``display:none`` 立刻接管，表现成「点一下控制台标签，日志框就消失」。
   所以这里**不只看 JS 是否被调用，而是真的读 computedStyle** ——
   只有布局层面的可见性才算数，``style.display`` 是骗人的。

B. 运行环境面板
   平台/架构/Python 版本要有值，「下载标识」要能复制。

跑法： python3 test_ui_console_env.py
"""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


def section(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


def load_html_admin():
    spec = importlib.util.spec_from_file_location("_gm_web_assets", HERE / "web_assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HTML_ADMIN


# ---------------------------------------------------------------------------
# 本地 HTTP 服务
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    page_html = ""
    stats_payload = {}

    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/system/stats"):
            self._send(json.dumps(self.stats_payload).encode(),
                       "application/json; charset=utf-8")
        elif self.path.startswith("/api/console"):
            # 真控制台接口：给两条日志，验证切过去能渲染出来
            self._send(json.dumps({
                "ok": True, "logs": [
                    {"time": "20:00:01", "level": "INFO", "message": "控制台测试日志一"},
                    {"time": "20:00:02", "level": "WARN", "message": "控制台测试日志二"},
                ], "last_id": 2,
            }).encode(), "application/json; charset=utf-8")
        elif self.path.startswith("/api/"):
            self._send(json.dumps({"ok": True}).encode(),
                       "application/json; charset=utf-8")
        else:
            self._send(self.__class__.page_html.encode("utf-8"))

    do_POST = do_GET


def serve(html, stats):
    _Handler.page_html = html
    _Handler.stats_payload = stats
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/", srv.shutdown


# 假的 /api/system/stats 响应，字段对齐 core.py 的 get_system_stats
def fake_stats():
    return {
        "ok": True,
        "stats": {
            "cpu": 12.5, "mem": 43.2, "disk": 61.0,
            "mem_total_mb": 32768, "mem_used_mb": 14156,
            "disk_total_gb": 512.0, "disk_used_gb": 312.3,
            "uptime": 93784,
            "env": {
                "os_name": "Windows",
                "os_detail": "Windows 11（build 22631）",
                "arch": "x86_64",
                "arch_raw": "amd64",
                "bits": 64,
                "python": "3.11.9",
                "python_tag": "cp311",
                "platform_tag": "win_amd64",
                "pip_target": "cp311-win_amd64",
                "libext": ".dll",
                "lib_bundle": "win-x64",
            },
        },
    }


def click_sidebar(page, section_id):
    """点侧边栏里指向某个板块的链接（真实用户路径）。

    侧边栏很长，靠下的板块会落在视口外；``dispatch_event('click')`` 直接
    在 DOM 上派发，不要求元素可见，语义与真实点击一致（同样触发内联 onclick）。
    """
    link = page.query_selector(f'.sidebar a[onclick*="{section_id}"]')
    if link is None:
        link = page.query_selector(f'a[onclick*="{section_id}"]')
    if link is None:
        raise RuntimeError(f"找不到指向 {section_id} 的侧边栏链接")
    link.dispatch_event("click")


# 读「用户实际看到什么」。★ 必须走 computedStyle + 包围盒，
# 不能只看 style.display —— 历史上那个 bug 正是「内联样式对了但元素仍被隐藏」。
VIS_PROBE = """(sel) => {
    const n = document.querySelector(sel);
    if (!n) return { exists: false };
    const cs = getComputedStyle(n);
    const r = n.getBoundingClientRect();
    return {
        exists: true,
        display: cs.display,
        visibility: cs.visibility,
        width: Math.round(r.width),
        height: Math.round(r.height),
        hasHiddenClass: n.classList.contains('hidden'),
        inlineDisplay: n.style.display,
        text: (n.innerText || '').trim().slice(0, 60),
    };
}"""


def visible(info):
    """从探针结果判断「用户能不能看见」。"""
    if not info.get("exists"):
        return False
    if info.get("display") == "none" or info.get("visibility") == "hidden":
        return False
    return info.get("height", 0) > 0 and info.get("width", 0) > 0


def run():
    from playwright.sync_api import sync_playwright

    html = load_html_admin()
    print(f"页面大小 {len(html)} B（来自 web_assets.HTML_ADMIN，即线上真实页面）")

    errors = []
    base_url, shutdown = serve(html, fake_stats())
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1400})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append("console.error: " + m.text)
                    if m.type == "error" else None)

            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_timeout(700)

            # ================= A. 控制台标签切换 =================
            section("A. 控制台标签切换（历史 bug 回归）")
            click_sidebar(page, "sec-players")
            page.wait_for_timeout(400)

            # A1. 初始态：聊天可见、控制台不可见
            chat = page.evaluate(VIS_PROBE, "#chat-pane")
            con = page.evaluate(VIS_PROBE, "#console-pane")
            check("初始：聊天面板可见", visible(chat), chat)
            check("初始：控制台面板不可见", not visible(con), con)

            # A2. ★ 点「控制台日志」标签
            page.click("#tab-console")
            page.wait_for_timeout(500)

            con = page.evaluate(VIS_PROBE, "#console-pane")
            check("★ 点控制台标签后，控制台面板**真的可见**（computedStyle 判定）",
                  visible(con), con)
            check("★ 控制台面板不再挂 .hidden 类（两套机制不再打架）",
                  not con.get("hasHiddenClass"), con)
            chat = page.evaluate(VIS_PROBE, "#chat-pane")
            check("切到控制台后聊天面板隐藏", not visible(chat), chat)
            check("控制台标签高亮", page.evaluate(
                "() => document.querySelector('#tab-console').classList.contains('active')"))
            check("聊天标签取消高亮", page.evaluate(
                "() => !document.querySelector('#tab-chat').classList.contains('active')"))

            # A3. 内容真的渲染出来了（不只是空框）
            box = page.inner_text("#console-panel")
            check("★ 控制台日志内容已渲染",
                  "控制台测试日志一" in box and "控制台测试日志二" in box,
                  box.replace("\n", " | ")[:100])

            # A4. 来回切几次都稳定（旧 bug 是「切过去就没了」）
            for i in range(3):
                page.click("#tab-chat")
                page.wait_for_timeout(150)
                page.click("#tab-console")
                page.wait_for_timeout(150)
            con = page.evaluate(VIS_PROBE, "#console-pane")
            check("★ 反复切换 3 轮后控制台仍可见（不是一次性侥幸）",
                  visible(con), con)

            # A5. 切回聊天也要正常
            page.click("#tab-chat")
            page.wait_for_timeout(300)
            chat = page.evaluate(VIS_PROBE, "#chat-pane")
            con = page.evaluate(VIS_PROBE, "#console-pane")
            check("切回聊天：聊天可见", visible(chat), chat)
            check("切回聊天：控制台隐藏", not visible(con), con)
            check("★ 聊天面板也没被误加 .hidden",
                  not chat.get("hasHiddenClass"), chat)

            # A6. 源码层：两个 pane 的初始标记方式应保持一致
            check("★ #console-pane 初始不再同时挂 .hidden 类",
                  'id="console-pane" style="display:none"' in html.replace(
                      'class="log-pane" ', ''),
                  "HTML 里 console-pane 应只有 class=\"log-pane\" + 内联 style")

            # ================= B. 运行环境面板 =================
            section("B. 运行环境面板")
            env_box = page.evaluate(VIS_PROBE, "#pane-env")
            check("环境面板可见", visible(env_box), env_box)

            check("操作系统已显示", "Windows" in page.inner_text("#env-os"),
                  page.inner_text("#env-os"))
            check("CPU 架构已显示", "x86_64" in page.inner_text("#env-arch"),
                  page.inner_text("#env-arch"))
            check("Python 版本已显示", "3.11.9" in page.inner_text("#env-py"),
                  page.inner_text("#env-py"))
            check("系统位数已显示", "64" in page.inner_text("#env-bits"),
                  page.inner_text("#env-bits"))

            tag = page.inner_text("#env-pip-target")
            check("★ 下载标识正确（cp311-win_amd64）", tag.strip() == "cp311-win_amd64", tag)

            hint = page.inner_text("#env-hint")
            check("提示里带离线库包平台名", "win-x64" in hint, hint)
            check("提示里带动态库后缀", ".dll" in hint, hint)

            # 复制按钮
            page.click(".env-copy")
            page.wait_for_timeout(400)
            st = page.inner_text("#env-copy-status")
            check("★ 点复制有反馈", ("已复制" in st) or ("失败" in st), st)

            # 环境面板不该多发请求（复用 stats 轮询）
            page.evaluate("() => { window.__statsCalls = 0; }")
            page.evaluate("""() => {
                const rf = window.fetch;
                window.fetch = function (u, o) {
                    if (String(u).indexOf('/api/system/stats') >= 0) {
                        window.__statsCalls = (window.__statsCalls || 0) + 1;
                    }
                    return rf.apply(this, arguments);
                };
            }""")
            page.wait_for_timeout(3600)      # 跨一个 3s 轮询周期
            calls = page.evaluate("() => window.__statsCalls")
            check("★ 环境信息复用 stats 轮询（没有额外请求接口）",
                  calls is not None and calls <= 3, f"3.6s 内 {calls} 次")

            browser.close()
    finally:
        shutdown()

    if errors:
        print()
        print("浏览器报错（前 10 条）：")
        for e in errors[:10]:
            print("  !", e)
    check("页面无未捕获异常", not errors, f"{len(errors)} 条")


try:
    run()
except Exception as e:
    import traceback
    traceback.print_exc()
    check("浏览器测试执行", False, e)

print()
print("=" * 74)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
