"""模组页「手动刷新」行为的真实浏览器验证。

被验的是**产品代码本身**：直接取 ``web_assets.HTML_ADMIN`` —— 也就是
服务器真实吐给浏览器的那个字符串（web_assets._build 已把 admin.css /
admin.js 原位回填好）。这里不做任何手工拼接，因此测试通过就代表真实页面
行为正确，不存在「测试用的页面和线上不是一个东西」的假阳性。

页面用**真实的本地 HTTP 服务**加载，而不是 ``file://``。原因是 admin.js 里
所有请求都走 ``apiPath()`` 拼相对路径（``api/mods``），file:// 下相对 URL
没有 base 可解析，``fetch`` 直接抛 ``Failed to parse URL``。用 http:// 起服务
既绕开了这个测试环境限制，又更贴近线上（线上本来就是 http）。

验证点：
  1. 打开页面（切到模组板块）**不发** /api/mods 请求；
  2. 两栏显示「尚未扫描」占位，不是空白；
  3. 点「🔄 刷新」→ 发 1 次请求，两栏渲染出数据，状态栏带「上次扫描」时间；
  4. 再切走再切回来 → **不**再发请求（仍用缓存）；
  5. 再点刷新 → 第 2 次请求；
  6. 切换存档 → 触发带 world= 参数的一次扫描。

跑法： python3 test_mods_ui.py
"""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


def load_html_admin():
    """按文件路径加载 web_assets，绕开 admin_web/__init__.py 的 endstone 依赖。"""
    spec = importlib.util.spec_from_file_location("_gm_web_assets", HERE / "web_assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HTML_ADMIN


# ---------------------------------------------------------------------------
# 本地 HTTP 服务：只干两件事 —— 吐 admin 页面、挡掉所有其它请求。
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    page_html = ""

    def log_message(self, *a):          # 静音，别把测试输出淹掉
        pass

    def do_GET(self):
        if self.path.startswith("/api/"):
            # admin.js 初始化时会打一批接口（状态/玩家等），一律给空成功，
            # 避免页面卡在「加载中…」。真正的 /api/mods 由浏览器里的
            # fetch 替身接管，根本不会走到这里。
            body = json.dumps({"ok": True}).encode()
        else:
            body = self.__class__.page_html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET


def serve(html):
    """起一个本地 HTTP 服务，返回 (base_url, 关闭函数)。"""
    _Handler.page_html = html
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/", srv.shutdown


# 构造一个「能骗过页面」的完整 /api/mods 响应。
# 字段名全部对齐 web.py 里 _handle_api_mods 的真实出参，多一个少一个都会
# 让前端渲染成 undefined，测试就失去意义。
def fake_mods_response():
    return {
        "ok": True,
        "uploaded": [
            {"file": "inbox_pack.mcpack", "name": "手动投放的包", "size": 4096,
             "exists": True, "uploaded": "2026-09-19 10:00:00", "source": "inbox"},
            {"file": "abc123_up.mcaddon", "name": "网页上传的包", "size": 8192,
             "exists": True, "uploaded": "2026-09-19 09:00:00", "source": "upload"},
        ],
        "installed": [],
        "resource_packs": [
            {"name": "普通材质包", "uuid": "rp-1", "version": "1.0.0",
             "path": "/srv/resource_packs/rp-1", "parent": "resource_packs",
             "kind": "resource", "scope": "global", "enabled": None,
             "mod_types": ["resources"]},
        ],
        "behavior_packs": [
            {"name": "普通行为包", "uuid": "bp-1", "version": "1.0.0",
             "path": "/srv/behavior_packs/bp-1", "parent": "behavior_packs",
             "kind": "behavior", "scope": "global", "enabled": None,
             "mod_types": ["data"]},
        ],
        "unknown_packs": [],
        "worlds": [{"name": "Bedrock level", "valid": True}],
        "current_world": "Bedrock level",
        "meta": {
            "world": "Bedrock level",
            "world_path": "/srv/worlds/Bedrock level",
            "world_source": "level-name",
            "enabled_resource": ["rp-1"],
            "enabled_behavior": [],
            "resource_json": "/srv/worlds/Bedrock level/world_resource_packs.json",
            "behavior_json": "/srv/worlds/Bedrock level/behavior_world_packs.json",
            "education_hidden": 2,
            "education_names": ["化学元素方块", "Minecraft Education Edition"],
        },
        "inbox_dir": "D:\\BDS\\plugins\\GreenMoon面板\\mods\\inbox",
    }


# 「假后端」注入脚本。在**所有页面脚本之前**执行，因此 admin.js 一跑起来
# 打到的就是我们这个替身。
#
# ★ 注意这里是**裸脚本体**，不是箭头函数。
#   Playwright 的 add_init_script 把传入的字符串当「脚本正文」直接求值：
#   写成 `() => { ... }` 只会**创建**一个函数然后丢掉，正文一行都不会跑，
#   且不会报错 —— 症状是「补丁看起来装了但完全没生效」，极易误判成产品 bug。
FAKE_BACKEND = """window.__modsCalls = [];
    window.__fakeMods = %s;
    window.fetch = async function (url, opts) {
        const u = String(url);
        if (u.indexOf('/api/mods') >= 0) {
            window.__modsCalls.push(u);
            await new Promise(r => setTimeout(r, 30));   // 模拟真实磁盘遍历耗时
            return new Response(JSON.stringify(window.__fakeMods),
                { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        // 其它接口一律返回空成功，避免页面初始化时卡在「加载中…」
        return new Response(JSON.stringify({ ok: true }), {
            status: 200, headers: { 'Content-Type': 'application/json' } });
    };
    // 面板会轮询这些接口，一并挡掉
    window.EventSource = function () { this.close = function () {}; };"""


def click_sidebar(page, section_id):
    """点侧边栏里指向某个板块的链接（真实用户路径）。

    admin.js 整个包在 IIFE 里，`showSection` 外部拿不到；但侧边栏
    `<a>` 用的是内联 onclick，点击它就等于用户真的切了板块 ——
    比直接调函数更接近用户行为。

    侧边栏很长，靠下的板块会落在视口之外；`dispatchEvent('click')` 直接在
    DOM 上派发事件，不要求元素可见 —— 语义与真实点击一致（同样会触发内联
    onclick），只是跳过了命中测试。
    """
    link = page.query_selector(f'.sidebar a[onclick*="{section_id}"]')
    if link is None:
        link = page.query_selector(f'a[onclick*="{section_id}"]')
    if link is None:
        raise RuntimeError(f"找不到指向 {section_id} 的侧边栏链接")
    link.dispatch_event("click")


def run():
    from playwright.sync_api import sync_playwright

    html = load_html_admin()
    print(f"页面大小 {len(html)} B（来自 web_assets.HTML_ADMIN，即线上真实页面）")
    assert "function renderMods" in html, "页面里没有 renderMods，说明前端改动没进产物"
    assert "async function scanMods" in html, "页面里没有 scanMods，说明前端改动没进产物"

    failures = []
    base_url, shutdown = serve(html)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})

            # 未捕获异常要看得见，否则「函数没被调用」这种失败会伪装成断言失败
            page.on("pageerror", lambda e: failures.append(str(e)))
            page.on("console", lambda m: failures.append("console." + m.type + ": " + m.text)
                    if m.type == "error" else None)

            page.add_init_script(FAKE_BACKEND % json.dumps(fake_mods_response()))
            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_timeout(800)

            print("=" * 74)
            print("1. 切到模组板块：不该发 /api/mods 请求")
            print("=" * 74)

            page.evaluate("() => { window.__modsCalls = []; }")
            click_sidebar(page, "sec-mods")
            page.wait_for_timeout(400)

            calls = page.evaluate("() => window.__modsCalls.length")
            check("★ 切到模组板块不发 /api/mods 请求", calls == 0, f"实际 {calls} 次")

            lib_box = page.inner_text("#mod-library-box")
            check("上传库显示「尚未扫描」而不是空白",
                  "尚未扫描" in lib_box, lib_box.strip()[:60])

            pack_box = page.inner_text("#mod-pack-groups")
            check("包列表显示「尚未扫描」", "尚未扫描" in pack_box, pack_box.strip()[:60])

            meta_txt = page.inner_text("#mod-world-meta")
            check("存档元信息提示点刷新", "刷新" in meta_txt, meta_txt.strip()[:60])

            status = page.inner_text("#mods-status")
            check("状态栏显示「未扫描」", "未扫描" in status, status)

            print()
            print("=" * 74)
            print("2. 点「🔄 刷新」：应发 1 次请求并渲染")
            print("=" * 74)

            page.evaluate("() => { window.__modsCalls = []; }")
            page.click("h2:has-text('模组管理器') button")
            page.wait_for_timeout(600)

            calls = page.evaluate("() => window.__modsCalls.length")
            check("★ 点刷新发出 1 次 /api/mods 请求", calls == 1, f"实际 {calls} 次")

            lib_box = page.inner_text("#mod-library-box")
            check("上传库渲染出 2 个包",
                  "手动投放的包" in lib_box and "网页上传的包" in lib_box,
                  lib_box.replace("\n", " | ")[:140])
            check("inbox 条目打了「手动投放」标签", "手动投放" in lib_box)

            hint = page.inner_text("#mod-inbox-hint")
            check("inbox 路径已显示", "mods" in hint and "inbox" in hint, hint.strip()[:80])

            pack_box = page.inner_text("#mod-pack-groups")
            check("资源包渲染出来", "普通材质包" in pack_box, pack_box[:80])
            check("行为包渲染出来", "普通行为包" in pack_box, pack_box[:80])

            meta_txt = page.inner_text("#mod-world-meta")
            check("★ 教育包隐藏数量已显示", "已自动隐藏 2 个" in meta_txt, meta_txt[:120])

            status = page.inner_text("#mods-status")
            check("★ 状态栏带「上次扫描」时间", "上次扫描" in status, status)
            check("状态栏统计正确", "2 个待安装" in status, status)

            print()
            print("=" * 74)
            print("3. 切走再切回：不该重复扫描")
            print("=" * 74)

            page.evaluate("() => { window.__modsCalls = []; }")
            click_sidebar(page, "sec-players")
            page.wait_for_timeout(200)
            click_sidebar(page, "sec-mods")
            page.wait_for_timeout(400)

            calls = page.evaluate("() => window.__modsCalls.length")
            check("★ 切走再切回不重新扫描", calls == 0, f"实际 {calls} 次")

            lib_box = page.inner_text("#mod-library-box")
            check("★ 但仍显示上次的数据（缓存生效）",
                  "手动投放的包" in lib_box and "尚未扫描" not in lib_box,
                  lib_box.replace("\n", " | ")[:100])

            print()
            print("=" * 74)
            print("4. 再点一次刷新：应发第 2 次请求")
            print("=" * 74)

            page.evaluate("() => { window.__modsCalls = []; }")
            page.click("h2:has-text('模组管理器') button")
            page.wait_for_timeout(600)
            calls = page.evaluate("() => window.__modsCalls.length")
            check("第二次点刷新再发 1 次", calls == 1, f"实际 {calls} 次")

            print()
            print("=" * 74)
            print("5. 切换存档：应触发一次扫描（含 world 参数）")
            print("=" * 74)
            page.evaluate("() => { window.__modsCalls = []; }")
            page.select_option("#mod-world-select", "Bedrock level")
            page.wait_for_timeout(700)
            calls = page.evaluate("() => window.__modsCalls")
            check("切换存档触发扫描", len(calls) >= 1, calls)
            if calls:
                check("存档参数已带上", "world=" in calls[0], calls[0])

            browser.close()
    finally:
        shutdown()

    if failures:
        print()
        print("浏览器报错（前 10 条）：")
        for f in failures[:10]:
            print("  !", f)

    check("页面无未捕获异常", not failures, f"{len(failures)} 条")


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
