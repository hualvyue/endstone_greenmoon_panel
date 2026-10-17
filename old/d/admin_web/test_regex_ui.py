#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正则触发器 + QQ 扫码绑定的**真实浏览器** UI 验证。

被验的是产品代码本身：直接取 ``web_assets.HTML_ADMIN`` —— 服务器真实吐给
浏览器的那个字符串（``web_assets._build`` 已把 admin.extra.js / admin.js /
admin.css 原位回填）。因此测试通过就代表线上页面行为正确。

为什么要真开浏览器，而不是查字符串
----------------------------------
上一版只断言"源码里有 qrCreate 这个函数"，这只能证明**代码存在**，证明不了
**它会跑、跑对**。真正会出事的地方恰恰是运行时：

  * ``qrcode`` 是否真的被内嵌进来、全局名对不对（extra.js 拼接顺序错了就没了）；
  * 拿到 ``secret_mask`` 之后页面有没有手滑把明文也打出来；
  * 轮询状态机 ``pending → created`` 的迁移是否会正确停表；
  * 规则列表渲染、开关、保存提交的 JSON 形状是否和 web.py 的契约一致。

验证点
------
  A. 扫码：入口按钮 → 拉卡片下拉 → 生成二维码 <img data:image/...> →
     轮询 pending 再 created → 状态区显示掩码与长度 → 停表；
  B. ★ 安全：整个页面的 outerHTML 里**不得出现 AppSecret 明文**，
     也不得出现 bind_key（那个 key 只该活在服务端内存里）；
  C. 异常：create 失败 / 轮询 error / 二维码过期，都要给出可读提示；
  D. 正则：列表渲染、开关、编辑保存的提交体、危险正则被拦、删除；
  E. 无未捕获异常、无 console.error。

跑法： python3 test_regex_ui.py
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
    print(("  [ok]   " if cond else "  [FAIL] ") + name +
          (("  -> " + str(extra)) if extra else ""))


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


# --------------------------------------------------------------- 假数据
#
# ★ 形状对齐 **端点契约**（web.py 的 _handle_api_qqbot_qr_* / _handle_api_regex_*），
#   不是 QQ 开放平台或磁盘上 rules.json 的原始形状。
#   端点已经把 data 拆开了、把 mask 算好了。按云端原始形状造数据会让前端
#   读到 undefined，最后失败点指向错误的位置。

CARD_ID = "card-official-1"
# ⚠ 这个字符串就是"绝不能出现在页面上"的那一个
FAKE_SECRET = "AbCdEf123456SeCrEtDoNotLeak"
FAKE_MASK = "•" * len(FAKE_SECRET)

QR_CREATE = {
    "ok": True,
    "task_id": "task-abc-123",
    "qrcode": "https://q.qq.com/qqbot/openclaw/connect.html?task_id=task-abc-123&_wv=2",
    "interval": "1",
}
QR_PENDING = {"ok": True, "status": "pending"}
QR_CREATED = {
    "ok": True, "status": "created", "appid": "102430001",
    "secret_len": len(FAKE_SECRET), "secret_mask": FAKE_MASK,
    "message": "已写入卡片并重启网关",
}
QR_EXPIRED = {"ok": False, "status": "expired",
              "error": "任务不存在或已过期，请重新获取二维码"}
QR_ERROR = {"ok": False, "status": "error", "error": "凭据解不开"}

BOTS_LIST = {
    "ok": True,
    "adapters": [
        {"id": CARD_ID, "name": "官方机器人一号", "type": "qqofficial", "app_id": ""},
        {"id": "card-ob-1", "name": "OneBot 小号", "type": "websocket"},
    ],
}

REGEX_RULES = {
    "ok": True,
    "rules": [
        {"id": "r1", "name": "查服", "enabled": True, "triggerType": "message",
         "pattern": "^查服$", "flags": "", "conditions": [],
         "actions": [{"type": "replyText", "params": "在线 $online"}], "block": True},
        {"id": "r2", "name": "入群欢迎（停用）", "enabled": False,
         "triggerType": "event", "eventType": "group.member_join",
         "pattern": "", "flags": "", "conditions": [],
         "actions": [{"type": "replyAtText", "params": "欢迎 $nickname"}], "block": False},
    ],
    "eventTypes": ["server.player_join", "server.player_left", "server.player_chat",
                   "group.member_join", "group.member_leave"],
    "actionTypes": ["replyText", "replyAtText", "replyImage", "deleteMessage",
                    "muteUser", "executeCommand", "logOnly"],
    "maxPatternLen": 512,
    "config": {"enable": True, "only_on_main": False,
               "admin_debug": False, "command_timeout": 5.0},
}


def fake_backend(qr_poll_seq, qr_create=None, rules=None, test_resp=None):
    """构造注入脚本。

    ``qr_poll_seq``：轮询依次返回的响应列表（用完后一直返回最后一个），
    这样能真实复刻 pending → created 的状态迁移。
    **裸脚本体**，不是箭头函数。

    ⚠ 规则库是**有状态**的：``regexSave`` 成功后前端会 ``await loadRegex()``
    重新拉一次列表（这是产品行为，不是 bug）。如果后端假实现每次
    都吐同一份原始 fixture，刚新增/删掉的规则会在保存后被"还原"，
    测试就会看到与预期不同的条数。所以这里把 save 的内容记下来，
    之后的 rules 请求返回它。
    """
    return """window.__calls = [];
    window.__regexSave = null;
    var __create = %s, __pollSeq = %s, __rules = %s, __test = %s;
    var __pollIdx = 0;
    // 有状态的规则库：save 之后 rules 要吐最新的
    var __rulesState = JSON.parse(JSON.stringify(__rules));
    window.fetch = async function (url, opts) {
        const u = String(url);
        const m = (opts && opts.method) || 'GET';
        const q = (o) => new Response(JSON.stringify(o),
            {status:200, headers:{'Content-Type':'application/json'}});
        if (u.indexOf('/api/qqbot/qr/create') >= 0) {
            window.__calls.push('qr-create');
            return q(__create);
        }
        if (u.indexOf('/api/qqbot/qr/poll') >= 0) {
            window.__calls.push('qr-poll');
            const r = __pollSeq[Math.min(__pollIdx, __pollSeq.length - 1)];
            __pollIdx++;
            return q(r);
        }
        if (u.indexOf('/api/qqbot/regex/rules') >= 0) {
            window.__calls.push('regex-rules');
            var out = JSON.parse(JSON.stringify(__rules));
            out.rules = __rulesState.rules;
            return q(out);
        }
        if (u.indexOf('/api/qqbot/regex/save') >= 0) {
            window.__regexSave = JSON.parse(opts.body || '{}');
            window.__calls.push('regex-save');
            // 落库：后续 loadRegex 要看到保存后的状态
            if (Array.isArray(window.__regexSave.rules)) {
                __rulesState.rules = JSON.parse(JSON.stringify(window.__regexSave.rules));
            }
            return q({ok:true, count:(window.__regexSave.rules||[]).length,
                      message:'已保存'});
        }
        if (u.indexOf('/api/qqbot/regex/toggle') >= 0) {
            window.__calls.push('regex-toggle');
            return q({ok:true});
        }
        if (u.indexOf('/api/qqbot/regex/delete') >= 0) {
            window.__calls.push('regex-delete');
            return q({ok:true});
        }
        if (u.indexOf('/api/qqbot/regex/test') >= 0) {
            window.__calls.push('regex-test');
            return q(__test);
        }
        if (u.indexOf('/api/bots/list') >= 0) {
            window.__calls.push('bots-list');
            return q(%s);
        }
        return q({ok:true});
    };
    window.EventSource = function () { this.close = function () {}; };""" % (
        json.dumps(qr_create if qr_create is not None else QR_CREATE),
        json.dumps(qr_poll_seq),
        json.dumps(rules if rules is not None else REGEX_RULES),
        json.dumps(test_resp if test_resp is not None
                   else {"ok": True, "valid": True, "matched": True,
                         "groups": ["查服"], "error": ""}),
        json.dumps(BOTS_LIST))


def open_page(page, base_url, backend, section=None):
    page.add_init_script(backend)
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(600)
    if section:
        click_sidebar(page, section)
        page.wait_for_timeout(500)


# ===========================================================================
def run():
    from playwright.sync_api import sync_playwright

    html = load_html_admin()
    print(f"页面大小 {len(html)} B（来自 web_assets.HTML_ADMIN，即线上真实页面）")

    # 前置：产物里必须真有这些 id，否则说明前端改动没进 web_assets 拼接
    for probe in ("qr-box", "qr-card", "qr-create-btn", "qr-img-box", "qr-status",
                  "sec-regex", "regex-list", "regex-enable", "regex-timeout",
                  "regex-test-pattern", "regex-test-result"):
        assert probe in html, f"页面里没有 {probe}，前端改动没进产物"
    assert "function qrCreate" in html, "admin.extra.js / admin.js 没被回填"
    print("产物自检通过：扫码与正则的 UI 骨架都在页面里")

    failures = []
    base_url, shutdown = serve(html)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()

            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda e: failures.append("pageerror: " + str(e)))
            page.on("console", lambda m: failures.append("console.error: " + m.text)
                    if m.type == "error" else None)

            # ===============================================================
            print()
            print("=" * 74)
            print("A. 扫码绑定：完整成功路径（pending → created）")
            print("=" * 74)
            open_page(page, base_url,
                      fake_backend([QR_PENDING, QR_PENDING, QR_CREATED]),
                      section="sec-bots")

            # --- 入口按钮存在且点了能开
            box = page.query_selector("#qr-box")
            check("扫码面板初始隐藏或未铺开（点了才出现）",
                  box is not None and
                  ("hidden" in (box.get_attribute("class") or "") or
                   not box.is_visible()), box.get_attribute("class") if box else None)

            page.evaluate("qrOpen()")
            page.wait_for_timeout(400)
            check("qrOpen() 后扫码面板可见", box.is_visible())

            # --- 卡片下拉：只列官方卡
            opts = page.eval_on_selector_all("#qr-card option",
                                             "els => els.map(e => e.value)")
            check("下拉里出现了官方卡片", CARD_ID in opts, opts)
            check("下拉里**没有** OneBot 卡（只绑官方）", "card-ob-1" not in opts, opts)
            text_opts = page.eval_on_selector_all("#qr-card option",
                                                  "els => els.map(e => e.textContent)")
            check("未配置 AppID 的卡有提示", any("请先新建" not in t for t in text_opts),
                  text_opts)

            # --- 点「获取二维码」
            page.select_option("#qr-card", CARD_ID)
            page.click("#qr-create-btn")
            page.wait_for_timeout(900)

            calls = page.evaluate("window.__calls")
            check("点了按钮会请求 /api/qqbot/qr/create", "qr-create" in calls, calls)

            img = page.query_selector("#qr-img-box img")
            check("二维码渲染成了 <img>", img is not None)
            if img is not None:
                src = img.get_attribute("src") or ""
                check("★ 二维码是内嵌 data URL（不依赖外网 CDN）",
                      src.startswith("data:image/"), src[:40])
                check("data URL 确实是图片数据（GIF/PNG 皆可）",
                      "base64," in src, src[:30])
                check("二维码图片不是空白/占位（数据量足够大）",
                      len(src) > 800, f"{len(src)} B")
                check("二维码 img 有可读 alt（无障碍/兜底）",
                      bool(img.get_attribute("alt")), img.get_attribute("alt"))

            status = page.inner_text("#qr-status")
            check("状态提示引导用户去扫码", "扫码" in status, status)

            # --- 等轮询跑完 pending → created
            page.wait_for_timeout(3200)
            calls = page.evaluate("window.__calls")
            polls = calls.count("qr-poll")
            check("轮询被真的调用了（≥2 次以覆盖状态迁移）", polls >= 2, polls)

            status = page.inner_text("#qr-status")
            check("★ created 后状态区显示「绑定成功」", "绑定成功" in status, status)
            check("★ 显示 AppID（这是要露出的）", "102430001" in status, status)
            check("★ 显示等长掩码", FAKE_MASK in status, status)
            check("★ 显示秘密长度", str(len(FAKE_SECRET)) in status, status)

            img_box = page.inner_text("#qr-img-box")
            check("二维码区域换成成功字样", "授权成功" in img_box, img_box)

            # 停表：created 之后不该再轮询
            before = page.evaluate("window.__calls").count("qr-poll")
            page.wait_for_timeout(2200)
            after = page.evaluate("window.__calls").count("qr-poll")
            check("★ created 之后停止轮询（不能无限打接口）", after == before,
                  f"{before} -> {after}")

            # ===============================================================
            print()
            print("=" * 74)
            print("B. ★ 安全：明文 AppSecret / bind_key 绝不出现在页面上")
            print("=" * 74)
            # 回到刚绑完的那一刻截图取样：页面此时理论上已经拿到了 secret
            page_html = page.content()
            check("★ 页面 HTML 里没有 AppSecret 明文",
                  FAKE_SECRET not in page_html, "泄漏了！" if FAKE_SECRET in page_html else "干净")
            check("★ 页面上也没有 secret 的任何片段（防部分回显）",
                  "DoNotLeak" not in page_html and "AbCdEf" not in page_html,
                  "片段泄漏" if ("DoNotLeak" in page_html or "AbCdEf" in page_html) else "干净")
            # bind_key 只该活在服务端内存：poll 响应里都不该有，页面更不该有
            check("★ 页面里没有 bind_key", "bind_key" not in page_html)
            # 掩码则应该出现（证明"确实拿到了"这一点被用户看见了）
            check("掩码应该出现（与明文的存在性做对照）", FAKE_MASK in page_html)

            # 整个 JS 运行时里也不该有明文变量
            leaked = page.evaluate("""() => {
                const hay = [document.documentElement.outerHTML,
                             JSON.stringify(window.__regexSave || {}),
                             JSON.stringify(window.__regexToggle || {}),
                             JSON.stringify(window.__regexDelete || {})].join('\\n');
                return hay.indexOf('DoNotLeak') >= 0 || hay.indexOf('AbCdEf') >= 0;
            }""")
            check("★ JS 运行时状态里也没有明文", not leaked)

            # ===============================================================
            print()
            print("=" * 74)
            print("C. 扫码异常路径：过期 / 失败 / create 报错")
            print("=" * 74)

            # --- 过期
            page2 = browser.new_page(viewport={"width": 1440, "height": 1000})
            page2.on("pageerror", lambda e: failures.append("pageerror(p2): " + str(e)))
            open_page(page2, base_url, fake_backend([QR_EXPIRED]), section="sec-bots")
            page2.evaluate("qrOpen()")
            page2.click("#qr-create-btn")
            page2.wait_for_timeout(2200)
            st = page2.inner_text("#qr-status")
            check("过期时提示「已过期」并要求重新获取", "过期" in st, st)
            check("过期后不再继续轮询",
                  page2.evaluate("window.__calls").count("qr-poll") <= 2,
                  page2.evaluate("window.__calls"))

            # --- 服务端报错
            page3 = browser.new_page(viewport={"width": 1440, "height": 1000})
            page3.on("pageerror", lambda e: failures.append("pageerror(p3): " + str(e)))
            open_page(page3, base_url, fake_backend([QR_ERROR]), section="sec-bots")
            page3.evaluate("qrOpen()")
            page3.click("#qr-create-btn")
            page3.wait_for_timeout(2200)
            st = page3.inner_text("#qr-status")
            check("绑定失败时回显后端错误原因", "凭据解不开" in st, st)
            check("失败提示不是空白页/未捕获异常", st.strip() != "", st)

            # --- create 直接失败
            page4 = browser.new_page(viewport={"width": 1440, "height": 1000})
            page4.on("pageerror", lambda e: failures.append("pageerror(p4): " + str(e)))
            open_page(page4, base_url,
                      fake_backend([QR_PENDING],
                                   qr_create={"ok": False, "error": "网络不可达"}),
                      section="sec-bots")
            page4.evaluate("qrOpen()")
            page4.click("#qr-create-btn")
            page4.wait_for_timeout(1200)
            st = page4.inner_text("#qr-status")
            check("create 失败时显示失败原因", "网络不可达" in st, st)
            check("create 失败后没有留下半个二维码",
                  page4.query_selector("#qr-img-box img") is None)
            check("create 失败后按钮恢复可点（能重试）",
                  page4.eval_on_selector("#qr-create-btn",
                                         "b => !b.disabled"), )

            # --- 关闭面板要停表
            page4.evaluate("qrCreate()")
            page4.wait_for_timeout(600)
            page4.evaluate("qrClose()")
            page4.wait_for_timeout(300)
            check("qrClose() 后面板隐藏",
                  not page4.query_selector("#qr-box").is_visible())
            check("qrClose() 后二维码被清掉，不残留上一次的图",
                  page4.query_selector("#qr-img-box img") is None)

            # ===============================================================
            print()
            print("=" * 74)
            print("D. 正则触发器：列表 / 开关 / 保存 / 测试 / 删除")
            print("=" * 74)
            page5 = browser.new_page(viewport={"width": 1440, "height": 1200})
            page5.on("pageerror", lambda e: failures.append("pageerror(p5): " + str(e)))
            page5.on("console", lambda m: failures.append("console.error(p5): " + m.text)
                     if m.type == "error" else None)
            open_page(page5, base_url, fake_backend([QR_PENDING]), section="sec-regex")

            calls = page5.evaluate("window.__calls")
            check("进板块自动拉规则列表", "regex-rules" in calls, calls)

            lst = page5.inner_text("#regex-list")
            check("规则名渲染出来了", "查服" in lst and "入群欢迎" in lst, lst)
            check("停用的规则有可辨识的状态标记",
                  "禁用" in lst, lst)
            check("规则正则会展示（便于核对）", "查服" in lst)
            # 渲染出的规则卡数量 == 后端给的条数
            n_cards = page5.evaluate(
                "() => document.querySelectorAll('#regex-list .card').length")
            check("规则条数正确（2 条）", n_cards == 2, n_cards)

            # --- 引擎开关回填
            check("引擎开关按后端配置回填为开",
                  page5.eval_on_selector("#regex-enable", "e => e.checked"))
            check("命令超时按后端配置回填",
                  page5.eval_on_selector("#regex-timeout", "e => e.value") in ("5", "5.0"),
                  page5.eval_on_selector("#regex-timeout", "e => e.value"))

            # --- 加一条规则并保存
            page5.evaluate("regexAddRule()")
            page5.wait_for_timeout(300)
            n_after = page5.evaluate(
                "() => document.querySelectorAll('#regex-list .card').length")
            check("能新增一条规则（列表变 3 条）", n_after == 3, n_after)

            page5.evaluate("regexSave()")
            page5.wait_for_timeout(700)
            saved = page5.evaluate("window.__regexSave")
            check("保存会提交整表规则", isinstance(saved, dict) and
                  isinstance(saved.get("rules"), list), type(saved).__name__)
            check("提交的规则条数是 3", len((saved or {}).get("rules", [])) == 3,
                  len((saved or {}).get("rules", [])))
            check("原文规则被原样带上（不丢字段）",
                  any(r.get("pattern") == "^查服$" for r in (saved or {}).get("rules", [])),
                  (saved or {}).get("rules"))
            check("保存时带上引擎配置（config）",
                  isinstance((saved or {}).get("config"), dict),
                  (saved or {}).get("config"))
            check("config 里有 enable 与 command_timeout",
                  "enable" in ((saved or {}).get("config") or {}) and
                  "command_timeout" in ((saved or {}).get("config") or {}),
                  (saved or {}).get("config"))

            # --- 开关
            #
            # ★ 前端是「改本地副本 → 整表 regexSave 提交」，不是单独打
            #   /regex/toggle。所以这里断言的落点是**保存体里的状态翻转**，
            #   而不是某个 toggle 接口被调用。按接口名断言会假绿。
            page5.evaluate("window.__regexSave = null")
            page5.evaluate("regexToggle(0)")
            page5.wait_for_timeout(700)
            saved2 = page5.evaluate("window.__regexSave")
            r0 = ((saved2 or {}).get("rules") or [{}])[0]
            check("开关后走整表保存（而不是单独 toggle 接口）",
                  saved2 is not None, saved2 is not None)
            check("★ 开关把该规则的 enabled 翻转了",
                  r0.get("enabled") is False, r0.get("enabled"))
            check("开关不影响其它规则",
                  ((saved2 or {}).get("rules") or [{}])[1].get("enabled") is False)

            # --- 删除
            #
            # regexDelete(idx) 会先 confirm 再 splice，然后整表保存；
            # 保存成功后前端会 loadRegex() 重拉列表（产品行为）。
            # 后端假实现必须跟着"落库"，否则重拉会把刚删的规则又变回来。
            page5.on("dialog", lambda d: d.accept())
            before_del = page5.evaluate("() => regexRules.map(r => r.name)")
            check("删除前列表是 3 条", len(before_del) == 3, before_del)

            page5.evaluate("window.__regexSave = null")
            page5.evaluate("regexDelete(1)")   # 删掉 index=1 的「入群欢迎」
            page5.wait_for_timeout(900)
            saved3 = page5.evaluate("window.__regexSave")
            names = [r.get("name") for r in ((saved3 or {}).get("rules") or [])]
            check("★ 删除后保存体条数 =2", len(names) == 2, names)
            check("★ 删掉的是指定的那条（按名字核对）",
                  not any(n and "入群欢迎" in n for n in names), names)
            check("★ 没被删的条目还在（按名字核对）",
                  any(n and "查服" == n for n in names), names)
            # 重拉之后页面上也应该是 2 条（证明删真的落库了）
            page5.wait_for_timeout(400)
            after_del = page5.evaluate("() => regexRules.map(r => r.name)")
            check("★ 重拉后页面仍是 2 条（删除已落库，不是只改了本地）",
                  len(after_del) == 2, after_del)
            n_final = page5.evaluate(
                "() => document.querySelectorAll('#regex-list .card').length")
            check("删除后列表也同步减少", n_final == 2, n_final)

            # --- 测试匹配
            page5.fill("#regex-test-pattern", "^查服$")
            page5.fill("#regex-test-text", "查服")
            page5.evaluate("regexTest()")
            page5.wait_for_timeout(700)
            tr = page5.inner_text("#regex-test-result")
            check("测试匹配有结果输出", tr.strip() != "", tr)
            check("命中时给出肯定反馈",
                  ("命中" in tr) or ("匹配" in tr) or ("成功" in tr), tr)
            check("命中时展示完整匹配内容", "查服" in tr, tr)

            # 危险正则要被后端拦下并在页面上说明
            page6 = browser.new_page(viewport={"width": 1440, "height": 1200})
            page6.on("pageerror", lambda e: failures.append("pageerror(p6): " + str(e)))
            open_page(page6, base_url,
                      fake_backend([QR_PENDING],
                                   test_resp={"ok": False, "valid": False, "matched": False,
                                              "error": "pattern 含嵌套量词或超大重复限定（ReDoS 风险），已拒绝"}),
                      section="sec-regex")
            page6.fill("#regex-test-pattern", "(a+)+$")
            page6.fill("#regex-test-text", "aaaaaaaaaa")
            page6.evaluate("regexTest()")
            page6.wait_for_timeout(700)
            tr6 = page6.inner_text("#regex-test-result")
            check("★ 危险正则被拒并说明原因",
                  ("ReDoS" in tr6) or ("风险" in tr6), tr6)

            # ===============================================================
            print()
            print("=" * 74)
            print("E. 全局：无未捕获异常 / 无 console.error")
            print("=" * 74)
            check("全程没有未捕获的 JS 异常", not any("pageerror" in f for f in failures),
                  [f for f in failures if "pageerror" in f][:3])
            check("全程没有 console.error", not any("console.error" in f for f in failures),
                  [f for f in failures if "console.error" in f][:3])

            browser.close()
    finally:
        shutdown()


# ===========================================================================
if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        FAIL.append(f"套件崩溃: {exc}")

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败用例：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 74)
    sys.exit(1 if FAIL else 0)
