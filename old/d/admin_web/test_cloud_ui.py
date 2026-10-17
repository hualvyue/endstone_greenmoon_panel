"""云黑板块新增 UI 的真实浏览器验证。

被验的是**产品代码本身**：直接取 ``web_assets.HTML_ADMIN`` —— 也就是
服务器真实吐给浏览器的那个字符串（web_assets._build 已把 admin.css /
admin.js 原位回填好）。因此测试通过就代表真实页面行为正确。

验证点
------
  1. 切到云黑板块 → 自动拉 stats + list（各 1 次），不进板块不拉；
  2. 总览渲染出 active_bans / bedrock / java 三个数字；
  3. 明细表渲染出玩家名 / XUID / 原因 / 来源 / 到期（永久 / 具体时间）；
  4. 测试连接 → 状态卡显示协议（新版 REST）与身份（管理员）；
  5. 手动查黑：命中 / 未命中两种结果都能正确展示；
  6. 旧版服务（list/stats 返回 ok:false）→ 页面显示说明文字，
     **不弹错误、不出现未捕获异常**；
  7. 「接口协议」下拉框存在且默认 auto，保存时会被提交。

跑法： python3 test_cloud_ui.py
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


def load_html_admin():
    spec = importlib.util.spec_from_file_location("_gm_web_assets", HERE / "web_assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HTML_ADMIN


class _Handler(BaseHTTPRequestHandler):
    page_html = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/"):
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
    _Handler.page_html = html
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/", srv.shutdown


def click_sidebar(page, section_id):
    link = page.query_selector(f'.sidebar a[onclick*="{section_id}"]')
    if link is None:
        link = page.query_selector(f'a[onclick*="{section_id}"]')
    if link is None:
        raise RuntimeError(f"找不到指向 {section_id} 的侧边栏链接")
    link.dispatch_event("click")


# --------------------------------------------------------------- 假后端
# ★ 这里的形状必须对齐 **端点契约**（web.py 的 _handle_api_cloud_* 出参），
#   而不是云端 API 的原始 body。两者是不同的东西：
#     云端  →  {"ok":true,"data":{"bans":[...]}}
#     端点  →  {"ok":true,"message":"...","bans":[...]}
#   web.py 已经把 data 拆开、把 message 提出来了。测试里如果按云端形状
#   造数据，前端读 r.bans / r.stats 就会拿到 undefined，渲染成一片 "-"，
#   测试失败但原因指向错误的地方。
REST_STATS = {"ok": True, "message": "ok",
              "stats": {"active_bans": 2, "java": 0, "bedrock": 2}}
REST_LIST = {"ok": True, "message": "共 2 条", "bans": [
    {"id": 5, "player_name": "Dream", "edition": "bedrock", "xuid": None,
     "uuid": None, "ip_address": None, "server_name": None,
     "reason": "时长自检", "expires": 1789912846, "created_at": 1789308047},
    {"id": 4, "player_name": "Steve", "edition": "bedrock", "xuid": "2535414915229641",
     "uuid": None, "ip_address": None, "server_name": "生存服A",
     "reason": "开挂", "expires": 0, "created_at": 1789307218},
]}

# 旧版服务的降级响应（新版专属接口在旧版上不存在）
LEGACY_STATS = {"ok": False, "message": "当前云黑服务不支持统计查询（旧版接口）", "stats": {}}
LEGACY_LIST = {"ok": False, "message": "当前云黑服务不支持列表查询（旧版接口）", "bans": []}

TEST_OK = {"ok": True, "message": "ok；身份：管理员（可封禁/解封）",
           "data": {"ok": True, "data": {"valid": True}, "_role": "admin"},
           "api_style": "新版 REST 接口"}
LOOKUP_HIT = {"ok": True, "message": "命中云黑",
              "ban": {"reason": "开挂", "server_name": "生存服A", "edition": "bedrock"}}
LOOKUP_MISS = {"ok": True, "message": "未命中云黑", "ban": None}


def fake_backend(stats, lst, test_resp=TEST_OK):
    """构造注入脚本。**裸脚本体**，不是箭头函数（见 test_mods_ui 里的说明）。"""
    return """window.__cloudCalls = [];
    window.__cloudSave = null;
    var __stats = %s, __list = %s, __test = %s, __hit = %s, __miss = %s;
    window.fetch = async function (url, opts) {
        const u = String(url);
        const m = (opts && opts.method) || 'GET';
        if (u.indexOf('/api/cloud/stats') >= 0) {
            window.__cloudCalls.push('stats');
            return new Response(JSON.stringify(__stats), {status:200, headers:{'Content-Type':'application/json'}});
        }
        if (u.indexOf('/api/cloud/list') >= 0) {
            window.__cloudCalls.push('list');
            return new Response(JSON.stringify(__list), {status:200, headers:{'Content-Type':'application/json'}});
        }
        if (u.indexOf('/api/cloud/test') >= 0) {
            window.__cloudCalls.push('test');
            return new Response(JSON.stringify(__test), {status:200, headers:{'Content-Type':'application/json'}});
        }
        if (u.indexOf('/api/cloud/lookup') >= 0) {
            const body = JSON.parse(opts.body || '{}');
            const isHit = String(body.xuid||'').indexOf('2535') === 0;
            window.__cloudCalls.push('lookup');
            return new Response(JSON.stringify(isHit ? __hit : __miss), {status:200, headers:{'Content-Type':'application/json'}});
        }
        if (u.indexOf('/api/tools/config/save') >= 0) {
            window.__cloudSave = JSON.parse(opts.body || '{}');
            return new Response(JSON.stringify({ok:true, message:'已保存'}), {status:200, headers:{'Content-Type':'application/json'}});
        }
        if (u.indexOf('/api/tools/config') >= 0) {
            // 给一份「已启用云黑」的配置，让设置区正常回填
            return new Response(JSON.stringify({ok:true, config:{
                backup: {}, cloud_blacklist: {
                    enabled: true, check_on_login: true, sync_default: false,
                    api_base: 'https://cloudban.nexacode.dpdns.org', token: '__SET__',
                    server_name: '测试服', kick_message: '§c你在云黑名单中',
                    api_style: 'auto', edition: 'bedrock'}}}),
                {status:200, headers:{'Content-Type':'application/json'}});
        }
        return new Response(JSON.stringify({ok:true}),
            {status:200, headers:{'Content-Type':'application/json'}});
    };
    window.EventSource = function () { this.close = function () {}; };""" % (
        json.dumps(stats), json.dumps(lst), json.dumps(test_resp),
        json.dumps(LOOKUP_HIT), json.dumps(LOOKUP_MISS))


def run_case(name, page, base_url, backend):
    print()
    print("=" * 74)
    print(name)
    print("=" * 74)
    page.add_init_script(backend)
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(700)


def run():
    from playwright.sync_api import sync_playwright

    html = load_html_admin()
    print(f"页面大小 {len(html)} B（来自 web_assets.HTML_ADMIN，即线上真实页面）")
    for probe in ("loadCloudStats", "loadCloudBans", "cloudLookup", "cloud-api-style",
                  "cloud-status-card", "cloud-ban-list", "cloud-stats-box"):
        assert probe in html, f"页面里没有 {probe}，说明前端改动没进产物"

    failures = []
    base_url, shutdown = serve(html)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()

            # ================= 场景 1：新版 REST 服务 =================
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda e: failures.append(str(e)))
            page.on("console", lambda m: failures.append("console." + m.type + ": " + m.text)
                    if m.type == "error" else None)
            run_case("1. ★ 新版 REST 服务：总览 / 明细 / 状态卡 / 手动查黑",
                     page, base_url, fake_backend(REST_STATS, REST_LIST))

            print("  --- 不进板块不该发请求 ---")
            page.evaluate("() => { window.__cloudCalls = []; }")
            click_sidebar(page, "sec-players")
            page.wait_for_timeout(300)
            n = page.evaluate("() => window.__cloudCalls.filter(function(x){return x==='stats'||x==='list';}).length")
            check("未进云黑板块不拉 stats/list", n == 0, f"实际 {n} 次")

            print("  --- 切到云黑板块：自动拉 ---")
            page.evaluate("() => { window.__cloudCalls = []; }")
            click_sidebar(page, "sec-cloud")
            page.wait_for_timeout(700)
            calls = page.evaluate("() => window.__cloudCalls")
            check("★ 进板块自动拉 stats", "stats" in calls, calls)
            check("★ 进板块自动拉 list", "list" in calls, calls)

            stats_txt = page.inner_text("#cloud-stats-box")
            check("★ 总览显示当前有效云黑数", "2" in stats_txt, stats_txt.replace("\n", " | ")[:120])
            check("总览显示 bedrock 分组", "基岩版分组" in stats_txt, stats_txt[:80])
            check("总览显示 java 分组", "Java 版分组" in stats_txt, stats_txt[:80])

            bans_txt = page.inner_text("#cloud-ban-list")
            check("★ 明细渲染玩家名", "Steve" in bans_txt and "Dream" in bans_txt,
                  bans_txt.replace("\n", " | ")[:160])
            check("明细渲染 XUID", "2535414915229641" in bans_txt)
            check("明细渲染原因", "开挂" in bans_txt)
            check("明细渲染来源服务器", "生存服A" in bans_txt)
            check("★ expires=0 显示为「永久」", "永久" in bans_txt, bans_txt[-120:])
            bans_status = page.inner_text("#cloud-bans-status")
            check("明细状态栏报了条数", "2" in bans_status, bans_status)

            print("  --- 测试连接 → 状态卡 ---")
            # ★ 选择器必须限定在云黑板块内：「🧪 测试连接」在跨服板块
            #   还有一个同名的隐藏按钮（cross-test-btn），裸选择器会命中
            #   那个不可见的元素，Playwright 等可见性超时后报错。
            page.click("#sec-cloud button:has-text('🧪 测试连接')")
            page.wait_for_timeout(600)
            card = page.inner_text("#cloud-status-card")
            check("★ 状态卡显示连接正常", "连接正常" in card, card.replace("\n", " | ")[:140])
            check("★ 状态卡显示协议", "新版 REST" in card, card[:140])
            check("★ 状态卡显示管理员身份", "管理员" in card, card[:140])

            print("  --- 手动查黑：命中 ---")
            page.fill("#cloud-lookup-xuid", "2535414915229641")
            page.click("#sec-cloud button:has-text('🔎 查询')")
            page.wait_for_timeout(500)
            res = page.inner_text("#cloud-lookup-result")
            check("★ 命中时显示命中与原因", "命中云黑" in res and "开挂" in res,
                  res.replace("\n", " | ")[:140])

            print("  --- 手动查黑：未命中 ---")
            page.fill("#cloud-lookup-xuid", "9999999999999999")
            page.click("#sec-cloud button:has-text('🔎 查询')")
            page.wait_for_timeout(500)
            res2 = page.inner_text("#cloud-lookup-result")
            check("★ 未命中时显示未命中", "未命中" in res2, res2[:140])

            print("  --- 手动查黑：不填参数 ---")
            page.fill("#cloud-lookup-xuid", "")
            page.fill("#cloud-lookup-ip", "")
            page.click("#sec-cloud button:has-text('🔎 查询')")
            page.wait_for_timeout(300)
            res3 = page.inner_text("#cloud-lookup-result")
            check("空参数给出提示（不发请求）", "请至少填写" in res3, res3[:120])

            print("  --- 设置区：api_style 下拉 ---")
            val = page.eval_on_selector("#cloud-api-style", "e => e.value")
            check("★ 接口协议下拉默认 auto", val == "auto", val)
            opts = page.eval_on_selector("#cloud-api-style",
                                         "e => Array.from(e.options).map(o => o.value)")
            check("下拉包含 auto/rest/legacy 三项",
                  set(opts) == {"auto", "rest", "legacy"}, opts)

            print("  --- 保存配置要把 api_style 带上 ---")
            page.select_option("#cloud-api-style", "rest")
            page.click("#sec-cloud button:has-text('💾 保存云黑设置')")
            page.wait_for_timeout(700)
            saved = page.evaluate("() => window.__cloudSave")
            check("★ 保存请求已发出", bool(saved), saved)
            if saved:
                cb = (saved.get("cloud_blacklist") or {})
                check("★ payload 带上了 api_style=rest",
                      cb.get("api_style") == "rest", cb)

            page.close()

            # ============= 场景 2：旧版服务（优雅降级） =============
            page2 = browser.new_page(viewport={"width": 1440, "height": 900})
            page2.on("pageerror", lambda e: failures.append("legacy: " + str(e)))
            page2.on("console", lambda m: failures.append("legacy console." + m.type + ": " + m.text)
                     if m.type == "error" else None)
            run_case("2. ★ 旧版 PHP 服务：列表/统计优雅降级，不报错",
                     page2, base_url, fake_backend(LEGACY_STATS, LEGACY_LIST))

            click_sidebar(page2, "sec-cloud")
            page2.wait_for_timeout(700)

            stats_txt2 = page2.inner_text("#cloud-stats-box")
            check("★ 旧版：总览显示降级说明",
                  "旧版接口" in stats_txt2, stats_txt2.replace("\n", " | ")[:140])
            bans_hint = page2.inner_text("#cloud-bans-hint")
            check("★ 旧版：明细显示降级说明",
                  "旧版接口" in bans_hint, bans_hint[:140])
            bans_status2 = page2.inner_text("#cloud-bans-status")
            check("★ 旧版：状态标记为不可用而不是错误", "不可用" in bans_status2, bans_status2)
            check("旧版：表格不残留旧行", "Steve" not in page2.inner_text("#cloud-ban-list"))

            # 手动查黑在旧版上仍然可用（check_ban 两版都有）
            page2.fill("#cloud-lookup-xuid", "2535414915229641")
            page2.click("button:has-text('🔎 查询')")
            page2.wait_for_timeout(500)
            res4 = page2.inner_text("#cloud-lookup-result")
            check("★ 旧版：手动查黑仍可用", "命中云黑" in res4, res4[:140])
            page2.close()

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
