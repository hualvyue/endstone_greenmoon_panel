"""修复验证（浏览器侧）：安装按钮 → 轮询 → 装完从「待安装」消失。

这里只测**前端行为**：后端清理逻辑由 test_mod_install_cleanup.py 覆盖
（那里跑的是 core.py 的真实方法）。本测试关心的是：

  1. 点「🛠 安装」后按钮被禁用并变成「⏳ 安装中」，防止重复点击；
  2. 前端会**轮询** /api/mods 直到该文件从待安装列表消失，
     而不是老代码那种固定 setTimeout(…, 2500)；
  3. 安装失败（文件一直在列表里）时，超时后给出「已保留以便重试」的提示，
     而不是一直显示「安装中…」；
  4. 装完后列表里确实不再有该文件；
  5. 列表是**由服务端数据渲染出来的**，且安装只移除该文件本身 ——
     别的待安装包必须原样留着。

★ 两个容易踩的坑，都在这里踩过了：

  (a) **不能用 page.route 数请求次数。**
      fetch 在 HTTP 层命中缓存时浏览器根本不发请求，route 数不到 /api/mods。
      改成用 add_init_script 把 window.fetch 包一层计数，才是真实调用次数。

  (b) **不能用 page.click() 点安装按钮。**
      Playwright 的 click 会先等元素「稳定」（停止移动/重排），实测要 ~700ms
      才真正派发 click。而 400ms 后 waitModInstall 的第 1 次 sleep 就结束了，
      于是「刚提交」这个瞬间永远观察不到，状态栏已经走到了下一条文案。
      这类时序断言必须用 **evaluate 里同步 click()**，把观察点精确到毫秒。

跑法： python3 test_mod_install_ui.py
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


def click_install(page, filename):
    """同步点击某行的「🛠 安装」按钮。

    不走 page.click()：见文件头 (b)，它会等元素稳定，白白吃掉 700ms。
    """
    page.evaluate("""(f) => {
        const row = Array.from(document.querySelectorAll('.mod-row'))
            .find(x => x.getAttribute('data-file') === f);
        if (!row) throw new Error('找不到行: ' + f);
        row.querySelector('button.ok').click();
    }""", filename)


def row_payload(f, size=4096, source="upload"):
    return {"file": f, "name": f, "size": size, "exists": True,
            "uploaded": "2026-09-19 10:00:00", "source": source}


def mods_payload(files, installed_names=()):
    """构造 /api/mods 响应。

    files 可以是文件名列表，也可以是 row_payload() 出来的完整行
    （后者用来验证 尺寸/来源 这些字段有没有被前端透传）。
    """
    rows = [row_payload(f) if isinstance(f, str) else f for f in files]
    return {
        "ok": True,
        "uploaded": rows,
        "installed": [],
        "resource_packs": [
            {"name": n, "uuid": n, "version": "1.0.0", "path": "/srv/resource_packs/" + n,
             "parent": "resource_packs", "kind": "resource", "scope": "world",
             "enabled": True, "mod_types": ["resources"]}
            for n in installed_names
        ],
        "behavior_packs": [],
        "unknown_packs": [],
        "worlds": [{"name": "Bedrock level", "valid": True}],
        "current_world": "Bedrock level",
        "meta": {"world": "Bedrock level", "world_path": "/srv/worlds/Bedrock level",
                 "world_source": "level-name", "enabled_resource": list(installed_names),
                 "enabled_behavior": [], "resource_json": "", "behavior_json": "",
                 "education_hidden": 0, "education_names": []},
        "inbox_dir": "/data/mods/inbox",
    }


def backend(initial_files, install_ok=True, after_n_installs=None, keep_after=False):
    """假后端（add_init_script 的**脚本体**，不要写成箭头函数）。

    · /api/mod/install 记一次调用，立刻返回；从请求体里取出被装的文件名；
    · /api/mods 每次记一次调用。安装完成后**只把被装的那个**从 uploaded
      里去掉 —— 这就是后端 `_cleanup_installed_source` 的真实粒度，
      用「整个列表换掉」的不真实语义会漏掉「A 装了 B 却跟着没了」的 bug。

    · after_n_installs=N：装完后再拖 N 次 /api/mods 才生效，
      用来验证轮询循环本体（而不是它第一次就撞上结果）。
    · keep_after=True：装完但源文件没被清掉 → 模拟超时分支。
    """
    resp = ({"ok": True, "message": "正在后台安装，完成后自动更新清单（一般数秒）"}
            if install_ok else {"ok": False, "error": "提交安装失败"})

    return """window.__calls = {install:0, mods:0};
    // 后端语义：安装成功后只删掉被安装的那个源文件，其余原样保留。
    window.__afterFor = function (target) {
        const base = JSON.parse(JSON.stringify(window.__initial));
        if (target) {
            base.uploaded = (base.uploaded || []).filter(function (u) {
                return String(u.file) !== String(target);
            });
        }
        return base;
    };
    window.__initial = %s;
    window.__installResp = %s;
    window.__afterN = %s;        // null = 立刻生效；数字 = 再等 N 次 /api/mods
    window.__keepAfter = %s;     // true = 模拟「清理失败」：装完但文件仍在列表
    window.__installed = false;
    window.__realFetch = window.fetch.bind(window);
    window.fetch = async function (url, opts) {
        const u = String(url);
        if (u.indexOf('/api/mod/install') >= 0) {
            window.__calls.install++;
            window.__installed = true;
            window.__pending = window.__afterN;
            window.__target = null;
            try {
                window.__target = (JSON.parse(String((opts && opts.body) || '{}'))).file || null;
            } catch (e) { window.__target = null; }
            return new Response(JSON.stringify(window.__installResp),
                {status: 200, headers: {'Content-Type': 'application/json'}});
        }
        if (u.indexOf('/api/mods') >= 0) {
            window.__calls.mods++;
            let payload = window.__initial;
            if (window.__installed) {
                if (window.__afterN !== null && window.__pending > 0) {
                    window.__pending--;
                } else {
                    payload = window.__keepAfter
                        ? window.__initial
                        : window.__afterFor(window.__target);
                }
            }
            return new Response(JSON.stringify(payload),
                {status: 200, headers: {'Content-Type': 'application/json'}});
        }
        return new Response(JSON.stringify({ok: true}),
            {status: 200, headers: {'Content-Type': 'application/json'}});
    };
    // 页面 init() 会连 SSE；不 stub 的话浏览器会一直重连，污染 console 错误。
    window.EventSource = function () {
        this.close = function () {};
        this.onopen = null; this.onerror = null; this.onmessage = null;
        this.addEventListener = function () {};
    };""" % (
        json.dumps(mods_payload(initial_files)),
        json.dumps(resp),
        "null" if after_n_installs is None else str(int(after_n_installs)),
        "true" if keep_after else "false",
    )


def open_mods_section(page):
    """进模组页并等列表渲染出来。

    ★ 模组页**故意不做自动扫描**（showSection 里只有 renderMods()），
      因为要遍历磁盘上的 resource_packs/behavior_packs 和两个
      world_*_packs.json，切进来就扫太浪费。所以这里必须**显式点刷新**，
      这也是真实用户看到列表的唯一路径 —— 测试照着真实路径走。
    """
    click_sidebar(page, "sec-mods")
    page.wait_for_timeout(200)
    page.click("h2:has-text('模组管理器') button")      # 🔄 刷新 → scanMods()
    page.wait_for_timeout(800)


def run():
    from playwright.sync_api import sync_playwright

    html = load_html_admin()
    print(f"页面大小 {len(html)} B（来自 web_assets.HTML_ADMIN，即线上真实页面）")
    assert "async function installMod" in html
    assert "waitModInstall" in html, "页面里没有 waitModInstall，说明前端改动没进产物"
    assert "setTimeout(loadMods, 2500)" not in html, "老的固定延时刷新还在，改动没生效"

    failures = []
    base_url, shutdown = serve(html)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()

            def new_page(tag):
                p = browser.new_page(viewport={"width": 1440, "height": 900})
                p.on("pageerror", lambda e: failures.append(f"{tag} pageerror: {e}"))
                p.on("console", lambda m: failures.append(f"{tag} console.{m.type}: {m.text}")
                     if m.type == "error" else None)
                return p

            # ============ 场景 1：安装成功 → 轮询 → 文件消失 ============
            page = new_page("s1")
            page.add_init_script(backend(["mypack.mcaddon"]))
            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_timeout(400)

            print("=" * 74)
            print("1. ★ 安装成功：按钮禁用 → 轮询 → 从待安装消失")
            print("=" * 74)

            open_mods_section(page)
            lib = page.inner_text("#mod-library-box")
            check("装之前列表里有 mypack.mcaddon", "mypack.mcaddon" in lib,
                  lib.replace("\n", " | ")[:100])

            page.evaluate("() => { window.__calls = {install:0, mods:0}; }")
            click_install(page, "mypack.mcaddon")
            page.wait_for_timeout(300)

            check("★ 按钮变成「⏳ 安装中」",
                  "安装中" in page.eval_on_selector(".mod-row button.ok", "e => e.textContent"),
                  page.eval_on_selector(".mod-row button.ok", "e => e.textContent"))
            check("★ 按钮被禁用（防重复点击）",
                  page.eval_on_selector(".mod-row button.ok", "e => e.disabled") is True)

            page.wait_for_timeout(2200)
            calls = page.evaluate("() => window.__calls")
            check("★ 安装接口被调了 1 次", calls["install"] == 1, calls)
            check("★ 安装后前端主动重扫 /api/mods", calls["mods"] >= 1, calls)

            lib2 = page.inner_text("#mod-library-box")
            check("★ 装完后列表里不再有 mypack.mcaddon",
                  "mypack.mcaddon" not in lib2, lib2.replace("\n", " | ")[:100])
            st = page.inner_text("#mod-upload-status")
            check("★ 状态栏提示已安装并移除", "已安装" in st, st)

            still = page.query_selector(".mod-row button.ok")
            check("列表已重绘（原按钮随之消失或恢复可用）", True,
                  "无残留行" if still is None
                  else f"disabled={still.evaluate('e => e.disabled')}")
            page.close()

            # ============ 场景 1b：服务端清理慢 → 轮询真的会转多轮 ============
            pageB = new_page("s1b")
            # 安装成功后再拖 3 次轮询才把源文件移走 —— 模拟大包解压慢
            pageB.add_init_script(backend(["slow.mcaddon"], after_n_installs=3))
            pageB.goto(base_url, wait_until="domcontentloaded")
            pageB.wait_for_timeout(400)

            print()
            print("=" * 74)
            print("1b. ★ 服务端清理慢：轮询要真的转起来，且不早退")
            print("=" * 74)
            open_mods_section(pageB)
            pageB.evaluate("() => { window.__calls = {install:0, mods:0}; }")
            click_install(pageB, "slow.mcaddon")

            # 轮询第 1 次 sleep 是 800ms，200ms 时还没轮到第 1 次 /api/mods：
            # 这个瞬间状态栏应该还是「🛠 开始安装」，按钮仍禁用。
            pageB.wait_for_timeout(200)
            stB = pageB.inner_text("#mod-upload-status")
            check("★ 刚提交时状态栏显示「正在后台安装」",
                  "后台安装" in stB or "开始安装" in stB, stB)
            check("★ 安装中按钮保持禁用（防重复点击）",
                  pageB.eval_on_selector(".mod-row button.ok", "e => e.disabled") is True)

            # 800ms 后第 1 次轮询发生，文件还在（after_n=3）：此时状态栏应被
            # 轮询改写为「⏳ 安装中…（已等待 0.8s）」——这是「用户知道还在跑」
            # 的关键反馈，否则大包解压十几秒里界面纹丝不动，用户会重复点击。
            pageB.wait_for_timeout(800)
            mid = pageB.evaluate("() => window.__calls")
            check("★ 慢安装时已轮询 ≥1 次", mid["mods"] >= 1, mid)
            stB2 = pageB.inner_text("#mod-upload-status")
            check("★ 轮询期间状态栏进入「安装中」并显示已等待时长",
                  "安装中" in stB2 and "已等待" in stB2, repr(stB2))
            check("★ 未完成时不提前报成功", "已安装" not in stB2, stB2)
            check("★ 轮询期间按钮保持禁用（防重复点击）",
                  pageB.eval_on_selector(".mod-row button.ok", "e => e.disabled") is True)

            pageB.wait_for_timeout(3200)          # 累计 ≥5 轮 × 800ms，够越过 3 次
            callsB = pageB.evaluate("() => window.__calls")
            check("★ 前端在轮询 /api/mods（>1 次）", callsB["mods"] > 1, callsB)
            check("★ 慢安装最终也被识别为完成",
                  "已安装" in pageB.inner_text("#mod-upload-status"),
                  pageB.inner_text("#mod-upload-status"))
            pageB.close()

            # ============ 场景 1c：列表完全由服务端数据渲染 ============
            pageC = new_page("s1c")
            pageC.add_init_script(backend([
                row_payload("a.mcaddon", size=2048, source="upload"),
                row_payload("b.mcpack", size=1048576, source="inbox"),
                row_payload("c.mcaddon", size=536870912, source="upload"),
            ]))
            pageC.goto(base_url, wait_until="domcontentloaded")
            pageC.wait_for_timeout(400)

            print()
            print("=" * 74)
            print("1c. ★ 待安装列表来自服务端数据（不是前端硬塞的行）")
            print("=" * 74)
            open_mods_section(pageC)
            rows = pageC.query_selector_all(".mod-row")
            files = [r.get_attribute("data-file") for r in rows]
            check("3 个上传文件 → 渲染 3 行", len(rows) == 3, files)
            check("行的顺序与接口返回一致",
                  files == ["a.mcaddon", "b.mcpack", "c.mcaddon"], files)

            box = pageC.inner_text("#mod-library-box")
            check("1 MB 被格式化成 1.00 MB", "1.00 MB" in box,
                  box.replace("\n", " | ")[:160])
            check("512 MB 被格式化（大包不崩）", "512.00 MB" in box,
                  box.replace("\n", " | ")[:160])
            check("inbox 来源被标注", "📁 手动投放" in box,
                  box.replace("\n", " | ")[:200])
            check("inbox 目录提示被渲染",
                  "/data/mods/inbox" in pageC.inner_text("#mod-inbox-hint"),
                  pageC.inner_text("#mod-inbox-hint")[:120])

            # 装 c.mcaddon：只有它该消失，另两个必须还在
            pageC.evaluate("() => { window.__calls = {install:0, mods:0}; }")
            click_install(pageC, "c.mcaddon")
            pageC.wait_for_timeout(2200)
            left = [r.get_attribute("data-file")
                    for r in pageC.query_selector_all(".mod-row")]
            check("★ 只移除被安装的那个（a/b 仍在）",
                  left == ["a.mcaddon", "b.mcpack"], left)
            check("★ 被装的是 c.mcaddon（请求体传对了文件）",
                  pageC.evaluate("() => window.__target") == "c.mcaddon",
                  pageC.evaluate("() => window.__target"))
            pageC.close()

            # ============ 场景 2：安装失败 → 明确报错 ============
            page2 = new_page("s2")
            page2.add_init_script(backend(["bad.mcaddon"], install_ok=False))
            page2.goto(base_url, wait_until="domcontentloaded")
            page2.wait_for_timeout(400)

            print()
            print("=" * 74)
            print("2. ★ 安装提交失败：明确报错，不进入轮询")
            print("=" * 74)
            open_mods_section(page2)
            page2.evaluate("() => { window.__calls = {install:0, mods:0}; }")
            click_install(page2, "bad.mcaddon")
            page2.wait_for_timeout(1200)
            st2 = page2.inner_text("#mod-upload-status")
            check("★ 状态栏显示失败原因", "✗" in st2 or "失败" in st2, st2)
            lib3 = page2.inner_text("#mod-library-box")
            check("★ 失败后列表里仍有该文件（可重试）", "bad.mcaddon" in lib3,
                  lib3.replace("\n", " | ")[:100])
            check("★ 失败后按钮恢复可用",
                  page2.eval_on_selector(".mod-row button.ok", "e => e.disabled") is False)
            c2 = page2.evaluate("() => window.__calls")
            check("失败时不空转轮询", c2["mods"] == 0, c2)
            page2.close()

            # ============ 场景 3：后端说成功但源文件没被清掉（超时）============
            page3 = new_page("s3")
            page3.add_init_script(backend(["stuck.mcaddon"], keep_after=True))
            page3.goto(base_url, wait_until="domcontentloaded")
            page3.wait_for_timeout(400)

            print()
            print("=" * 74)
            print("3. ★ 文件一直不消失（超时）：给提示而不是永远「安装中」")
            print("=" * 74)
            open_mods_section(page3)
            click_install(page3, "stuck.mcaddon")
            page3.wait_for_timeout(18500)          # 轮询上限 20 × 800ms = 16s
            st3 = page3.inner_text("#mod-upload-status")
            check("★ 超时后有明确提示（不是卡在安装中）",
                  "安装中" != st3.strip() and ("保留" in st3 or "日志" in st3), st3)
            lib4 = page3.inner_text("#mod-library-box")
            check("超时后文件仍在列表里（符合「保留以便重试」）",
                  "stuck.mcaddon" in lib4, lib4.replace("\n", " | ")[:100])
            check("★ 超时后按钮恢复可用",
                  page3.eval_on_selector(".mod-row button.ok", "e => e.disabled") is False)
            c3 = page3.evaluate("() => window.__calls")
            check("超时前确实轮询满了（≥15 次）", c3["mods"] >= 15, c3)
            check("超时场景只装了 1 次（没有重复提交）", c3["install"] == 1, c3)
            page3.close()

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
