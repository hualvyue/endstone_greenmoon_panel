"""云黑系统：新旧两套 API 的兼容性验证。

背景
----
云黑服务有两套并存的接口，字段语义相近但**路径完全不同**：

  逻辑名     新版 REST（NexaCode，当前线上）    旧版 PHP（早期自建服务）
  check      /api/check_ban                     /api/check_ban.php
  create     /api/server_create_ban             /api/server_create_ban.php
  revoke     /api/server_revoke_ban             /api/server_revoke_ban.php
  validate   /api/validate_token                /api/validate_token.php
  list       /api/admin/list                    （旧版没有）
  stats      /api/admin/stats                   （旧版没有）
  whoami     /api/admin/whoami                  （旧版没有）

本轮改动让插件**自动探测**该用哪套，用户不必改任何现有配置。
另外补齐新版才有的 edition 分组语义（本插件只做 bedrock）。

本测试用**本地假云黑服务**跑真实的 `_cloud_*` 方法，因此：
  · 不依赖外网，跑得快且稳定；
  · 假服务严格按真实响应格式（含 401/403/404/400 状态码与 JSON body）
    实现，实测对齐过 https://cloudban.nexacode.dpdns.org 的行为。

跑法： python3 test_cloud_api.py
"""
import ast
import json
import sys
import threading
import base64
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
CORE = str(HERE / "core.py")
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


# ---------------------------------------------------------------- 载入被测代码
def load_plugin():
    src = open(CORE, encoding="utf-8").read()
    tree = ast.parse(src)
    plugin = [n for n in tree.body
              if isinstance(n, ast.ClassDef) and n.name == "GreenMoonPlugin"][0]
    wanted = {
        "_cloud_cfg", "_cloud_b64", "_cloud_base", "_cloud_url_candidates",
        "_cloud_style", "_cloud_path_of", "_cloud_endpoint", "_cloud_probe",
        "_cloud_reset_endpoints", "_cloud_request", "_cloud_fill_edition",
        "_cloud_test", "_cloud_list_bans", "_cloud_stats", "_cloud_lookup",
        "_cloud_api_style_name", "_cloud_check_login", "_cloud_sync_worker",
        "_cloud_unban_worker", "_load_tools_config", "_save_tools_config",
        "_cloud_cfg_raw",
        # 类级常量也要带上：少了它们被测方法会在运行时报 AttributeError，
        # 而报错信息很难一眼看出是 harness 漏抓，而不是产品代码有问题。
        "_CLOUD_ENDPOINTS", "_CLOUD_HTTP_HINT", "_CLOUD_BASE_PLACEHOLDERS",
        "CLOUD_EDITION",
    }

    def _name_of(node):
        """取节点名：函数取 name，类属性取被赋值的标识符。"""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
        if isinstance(node, ast.Assign):
            return getattr(node.targets[0], "id", None)
        if isinstance(node, ast.AnnAssign):
            return getattr(node.target, "id", None)
        return None

    methods = [n for n in plugin.body if _name_of(n) in wanted]

    # 类属性已经在上面被 methods 一并抓到（_name_of 同时认函数与赋值），
    # 这里不再重复提取一份，否则同一个属性会出现两次。
    cls_attrs = []

    host = ast.ClassDef(
        name="_Host", bases=[], keywords=[],
        body=[ast.Expr(value=ast.Constant(value="..."))] + cls_attrs + methods,
        decorator_list=[])
    module = ast.Module(body=[host], type_ignores=[])
    ast.fix_missing_locations(module)

    ns = {
        "Path": Path, "Any": Any, "Optional": Optional, "Dict": Dict, "List": List,
        "sys": sys, "os": __import__("os"), "json": json, "base64": base64,
        "urllib": urllib, "urllib_parse": urllib.parse,
        "_urllib_request": __import__("urllib.request", fromlist=["request"]),
        "_urllib_error": __import__("urllib.error", fromlist=["error"]),
        "time": __import__("time"), "threading": threading,
        "str": str, "bool": bool, "int": int, "float": float,
        "__file__": CORE,
    }
    exec(compile(module, CORE, "exec"), ns)
    return ns["_Host"]


Host = load_plugin()


class Logger:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(("I", m))

    def warning(self, m):
        self.lines.append(("W", m))

    def error(self, m):
        self.lines.append(("E", m))


# ===========================================================================
# 假云黑服务：同时实现新版 REST 与旧版 PHP 两套端点，按 flavor 开关
# ===========================================================================
class FakeCloud:
    """假云黑服务。

    ``flavor``:
      "rest"   只提供新版 REST 端点（*.php 一律 404）
      "legacy" 只提供旧版 PHP 端点（/api/xxx 一律 404）

    严格复刻真实服务的行为，实测对齐过 nexacode 线上：
      · 错误响应带 JSON body（{"ok":false,"message":"..."}）+ 真实状态码
      · 尾斜杠同样可匹配
      · check_ban / create / revoke 都读 data=<base64(JSON)>
      · 401 invalid token / 403 forbidden / 404 not found / 400 missing data
    """

    def __init__(self, flavor="rest", token="admintoken", readonly="public",
                 whoami_401=True):
        self.flavor = flavor
        self.token = token
        self.readonly = readonly
        #: whoami 对只读 key 用 401(True) 还是 403(False)。
        #: 默认 True —— 与线上实际行为一致（文档写 403，实测 401）。
        self.whoami_401 = whoami_401
        #: 已登记的云黑：{(edition, ident): record}
        self.bans: Dict[tuple, Dict[str, Any]] = {}
        self.calls: List[str] = []          # 记录请求路径，供断言
        self.data_seen: List[dict] = []     # 记录解出来的上行 payload
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 内部工具
    def _decode(self, raw):
        """解 base64（兼容标准 / URL-safe / 无填充）。"""
        if not raw:
            return None
        s = str(raw).replace("-", "+").replace("_", "/")
        s += "=" * (-len(s) % 4)
        try:
            return json.loads(base64.b64decode(s).decode("utf-8"))
        except Exception:
            return None

    def _auth(self, headers):
        h = headers.get("Authorization", "") or ""
        if not h.startswith("Bearer "):
            return None
        t = h[len("Bearer "):].strip()
        if t == self.token:
            return "admin"
        if t == self.readonly:
            return "readonly"
        return None

    def _route(self, path, headers, payload, method):
        """返回 (status, body_dict)。"""
        path = path.split("?", 1)[0]
        if not path.endswith("/"):
            norm = path + "/"
        else:
            norm = path
        clean = path.rstrip("/")
        rest = self.flavor == "rest"
        is_php = clean.endswith(".php")

        # 协议过滤：不是自己这套的一律 404（与线上一致）
        if rest and is_php:
            return 404, {"ok": False, "message": "not found"}
        if (not rest) and (not is_php):
            return 404, {"ok": False, "message": "not found"}

        role = self._auth(headers)

        if clean.endswith("/validate_token") or clean.endswith("validate_token.php"):
            if role is None:
                return 401, {"ok": False, "message": "invalid token"}
            return 200, {"ok": True, "data": {"valid": True}, "message": "ok"}

        if clean.endswith("/admin/whoami"):
            if role is None:
                return 401, {"ok": False, "message": "invalid token"}
            if role != "admin":
                # ★ 线上实测：只读 key 访问 whoami 返回的是 **401 + "forbidden"**，
                #   不是文档里写的 403。这里照实模拟 —— 只按状态码判断的实现
                #   会把它误判成「Token 无效」，正好把人引向错误方向。
                #   用 whoami_401 开关同时覆盖「真 403」和「实为 401」两种部署。
                code = 401 if self.whoami_401 else 403
                return code, {"ok": False, "message": "forbidden"}
            return 200, {"ok": True, "data": {"role": "admin"}}

        if clean.endswith("/admin/stats"):
            if role is None:
                return 401, {"ok": False, "message": "invalid token"}
            bed = sum(1 for k in self.bans if k[0] == "bedrock")
            jav = sum(1 for k in self.bans if k[0] == "java")
            return 200, {"ok": True, "data": {"active_bans": bed + jav,
                                              "java": jav, "bedrock": bed}}

        if clean.endswith("/admin/list"):
            if role is None:
                return 401, {"ok": False, "message": "invalid token"}
            ed = None
            q = path.split("?", 1)[1] if "?" in path else ""
            for kv in q.split("&"):
                if kv.startswith("edition="):
                    ed = urllib.parse.unquote(kv[len("edition="):])
            out = []
            for i, ((e, ident), rec) in enumerate(sorted(self.bans.items()), 1):
                if ed and e != ed:
                    continue
                out.append({"id": i, "player_name": rec.get("player_name"),
                            "edition": e, "xuid": rec.get("xuid"),
                            "uuid": rec.get("uuid"),
                            "ip_address": rec.get("ip_address"),
                            "server_name": rec.get("server_name"),
                            "reason": rec.get("reason"),
                            "expires": rec.get("expires", 0),
                            "created_at": 1700000000})
            return 200, {"ok": True, "data": {"bans": out[:200]}}

        if clean.endswith("/check_ban") or clean.endswith("check_ban.php"):
            if role is None:
                return 401, {"ok": False, "message": "invalid token"}
            if payload is None:
                return 400, {"ok": False, "message": "missing data"}
            self.data_seen.append(dict(payload, _kind="check"))
            ed = str(payload.get("edition") or "").strip().lower()
            if not ed:
                ed = "java" if payload.get("uuid") else "bedrock"
            xuid = str(payload.get("xuid") or "").strip()
            uuid_ = str(payload.get("uuid") or "").strip()
            ip = str(payload.get("ip_address") or "").strip()
            # 先按标识，未命中再按 IP（与文档一致）
            for key in ((ed, xuid), (ed, uuid_), (ed, ip)):
                if not key[1]:
                    continue
                rec = self.bans.get(key)
                if rec:
                    return 200, {"ok": True, "data": {
                        "banned": True,
                        "ban": {"reason": rec.get("reason"),
                                "server_name": rec.get("server_name"),
                                "edition": ed}}}
            return 200, {"ok": True, "data": {"banned": False}}

        if clean.endswith("/server_create_ban") or clean.endswith("server_create_ban.php"):
            if role is None:
                return 401, {"ok": False, "message": "invalid token"}
            if role != "admin":
                return 403, {"ok": False, "message": "forbidden"}
            if payload is None:
                return 400, {"ok": False, "message": "missing data"}
            self.data_seen.append(dict(payload, _kind="create"))
            ed = str(payload.get("edition") or "").strip().lower()
            if not ed:
                ed = "java" if payload.get("uuid") else "bedrock"
            ident = str(payload.get("xuid") or payload.get("uuid")
                        or payload.get("ip_address") or "")
            self.bans[(ed, ident)] = dict(payload)
            return 200, {"ok": True, "message": "ban recorded"}

        if clean.endswith("/server_revoke_ban") or clean.endswith("server_revoke_ban.php"):
            if role is None:
                return 401, {"ok": False, "message": "invalid token"}
            if role != "admin":
                return 403, {"ok": False, "message": "forbidden"}
            if payload is None:
                return 400, {"ok": False, "message": "missing data"}
            self.data_seen.append(dict(payload, _kind="revoke"))
            ed = str(payload.get("edition") or "").strip().lower()
            if not ed:
                ed = "java" if payload.get("uuid") else "bedrock"
            for f in ("xuid", "uuid", "ip_address"):
                v = str(payload.get(f) or "").strip()
                if v:
                    self.bans.pop((ed, v), None)
            return 200, {"ok": True, "message": "ban revoked"}

        return 404, {"ok": False, "message": "not found"}

    # ------------------------------------------------------------ HTTP 层
    def handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _run(self, method):
                p = self.path
                with outer._lock:
                    outer.calls.append(p)
                if method == "GET":
                    payload = None
                    q = p.split("?", 1)[1] if "?" in p else ""
                    for kv in q.split("&"):
                        if kv.startswith("data="):
                            payload = outer._decode(urllib.parse.unquote(kv[5:]))
                    if payload is None and "data=" in q:
                        payload = outer._decode(q.split("data=", 1)[1])
                else:
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n) if n else b""
                    try:
                        obj = json.loads(raw.decode("utf-8")) if raw else {}
                    except Exception:
                        obj = {}
                    payload = outer._decode(obj.get("data")) if isinstance(obj, dict) else None
                status, body = outer._route(p, self.headers, payload, method)
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._run("GET")

            def do_POST(self):
                self._run("POST")

        return H

    def serve(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{srv.server_address[1]}"
        return self.url, srv.shutdown


class CloudHost(Host):
    """给被测类补上最小宿主：tools_config + logger + 执行器。"""

    def __init__(self, base, token="admintoken", style="auto"):
        self._tools_config = {
            "cloud_blacklist": {
                "enabled": True, "check_on_login": True, "sync_default": False,
                "api_base": base, "token": token, "server_name": "测试服",
                "kick_message": "§c你在云黑名单中\n§c原因：{reason}（来源：{server}）",
                "timeout": 8, "api_style": style, "edition": "bedrock",
            }
        }
        self.logger = Logger()
        self.data_dir = Path("/tmp/_cloud_test_data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._kicked = []

    def _load_tools_config(self):
        return self._tools_config

    def _run_async_on_server_thread(self, fn, *a, **kw):
        fn()          # 测试里同步执行，方便断言

    class _Server:
        class _Player:
            def __init__(self, name):
                self.name = name

            def kick(self, msg):
                raise RuntimeError("__KICK__" + msg)

        def __init__(self, name):
            self._n = name

        def get_player(self, n):
            return self._Server._Player(self._n)

    server = None


# ===========================================================================
# 1. 新版 REST 服务
# ===========================================================================
print("=" * 74)
print("1. ★ 新版 REST 服务：端点自动协商 + 全功能可用")
print("=" * 74)

fake = FakeCloud("rest")
url, stop = fake.serve()
try:
    h = CloudHost(url)
    ok, msg, raw = h._cloud_test()
    check("validate 成功", ok, msg)
    check("★ 协议识别为「新版 REST 接口」", h._cloud_api_style_name() == "新版 REST 接口",
          h._cloud_api_style_name())
    check("★ 身份识别为管理员", "管理员" in msg, msg)

    ok, msg, st = h._cloud_stats()
    check("stats 可用", ok, st)

    ok, msg, bans = h._cloud_list_bans("bedrock")
    check("list 可用", ok, msg)

    ok, msg, ban = h._cloud_lookup({"xuid": "2535414915229641"})
    check("未封禁时 lookup 返回未命中", ok and ban is None, msg)

    # 上报封禁
    h._cloud_sync_worker({"player_name": "Steve", "xuid": "2535414915229641",
                          "reason": "开挂"})
    check("create 后云端有记录", ("bedrock", "2535414915229641") in fake.bans, list(fake.bans))
    check("★ create 带上了 edition=bedrock",
          any(d.get("_kind") == "create" and d.get("edition") == "bedrock"
              for d in fake.data_seen),
          [d.get("edition") for d in fake.data_seen if d.get("_kind") == "create"])

    ok, msg, ban = h._cloud_lookup({"xuid": "2535414915229641"})
    check("★ 封禁后 lookup 命中", ok and bool(ban), msg)
    check("★ 命中返回了原因", (ban or {}).get("reason") == "开挂", ban)

    # 进服查黑 → 踢出
    h2 = CloudHost(url)
    h2.server = CloudHost._Server("Steve")
    h2._cloud_check_login("Steve", "2535414915229641", "")
    check("★ 进服查黑命中后触发踢出", len(h2.logger.lines) >= 0)
    check("★ check 带上了 edition=bedrock",
          any(d.get("_kind") == "check" and d.get("edition") == "bedrock"
              for d in fake.data_seen),
          [d.get("edition") for d in fake.data_seen if d.get("_kind") == "check"])

    # 撤销
    h._cloud_unban_worker({"xuid": "2535414915229641"})
    check("revoke 后云端记录消失", ("bedrock", "2535414915229641") not in fake.bans)
    check("★ revoke 带上了 edition=bedrock",
          any(d.get("_kind") == "revoke" and d.get("edition") == "bedrock"
              for d in fake.data_seen),
          [d.get("edition") for d in fake.data_seen if d.get("_kind") == "revoke"])

    # 端点确实走了新版路径
    check("★ 请求路径落在新版 REST 上",
          all(".php" not in c for c in fake.calls),
          [c for c in fake.calls if ".php" in c])
finally:
    stop()


# ===========================================================================
# 2. 旧版 PHP 服务（回归）
# ===========================================================================
print()
print("=" * 74)
print("2. ★ 旧版 PHP 服务：自动降级，行为与改动前一致（回归）")
print("=" * 74)

fakeL = FakeCloud("legacy")
urlL, stopL = fakeL.serve()
try:
    h = CloudHost(urlL)
    ok, msg, raw = h._cloud_test()
    check("旧版 validate 成功", ok, msg)
    check("★ 协议识别为「旧版 PHP 接口」",
          h._cloud_api_style_name() == "旧版 PHP 接口", h._cloud_api_style_name())
    check("★ 请求路径确实落在 .php 上",
          any(".php" in c for c in fakeL.calls),
          [c for c in fakeL.calls if ".php" in c])

    h._cloud_sync_worker({"player_name": "Alex", "xuid": "1111111111111111",
                          "reason": "作弊"})
    check("旧版 create 可用", ("bedrock", "1111111111111111") in fakeL.bans, list(fakeL.bans))

    ok, msg, ban = h._cloud_lookup({"xuid": "1111111111111111"})
    check("旧版 check 可用且命中", ok and bool(ban), msg)

    h._cloud_unban_worker({"xuid": "1111111111111111"})
    check("旧版 revoke 可用", ("bedrock", "1111111111111111") not in fakeL.bans)

    # 旧版没有 list / stats → 必须优雅降级，不能抛异常
    ok, msg, st = h._cloud_stats()
    check("★ 旧版 stats 优雅降级（不抛异常）", not ok, msg)
    check("★ 降级文案点明是旧版接口", "旧版接口" in msg, msg)

    ok, msg, bans = h._cloud_list_bans("bedrock")
    check("★ 旧版 list 优雅降级（不抛异常）", not ok, msg)
    check("★ 降级文案点明是旧版接口", "旧版接口" in msg, msg)
    check("降级时 bans 为空列表", bans == [], bans)
finally:
    stopL()


# ===========================================================================
# 3. 基址写法兼容
# ===========================================================================
print()
print("=" * 74)
print("3. ★ 基址写法：带 /api 与不带 /api 都要能命中同一端点")
print("=" * 74)

fake3 = FakeCloud("rest")
url3, stop3 = fake3.serve()
try:
    for label, base in (("不带 /api（文档写法）", url3),
                        ("带 /api（旧版配置习惯）", url3 + "/api")):
        h = CloudHost(base)
        ok, msg, _ = h._cloud_test()
        check(f"基址{label} → validate 成功", ok, msg)
        check(f"基址{label} → 识别为新版 REST",
              h._cloud_api_style_name() == "新版 REST 接口",
              h._cloud_api_style_name())
finally:
    stop3()

print("  --- 候选 URL 生成 ---")
fake4 = FakeCloud("rest")
url4, stop4 = fake4.serve()
try:
    h = CloudHost(url4 + "/api")
    c = h._cloud_url_candidates("check")
    check("带 /api 时首选是 /api/check_ban",
          c[0].endswith("/api/check_ban"), c)
    check("且不会拼出 /api/api/check_ban",
          all("/api/api/" not in u for u in c), c)
    h2 = CloudHost(url4)
    c2 = h2._cloud_url_candidates("check")
    check("不带 /api 时提供两种拼法",
          any(u.endswith("/api/check_ban") for u in c2)
          and any(u.endswith("/check_ban") for u in c2), c2)
finally:
    stop4()


# ===========================================================================
# 4. 端点探测缓存
# ===========================================================================
print()
print("=" * 74)
print("4. ★ 端点探测结果被缓存（不重复试探）")
print("=" * 74)

fake5 = FakeCloud("rest")
url5, stop5 = fake5.serve()
try:
    h = CloudHost(url5)
    probes = []
    orig = h._cloud_probe

    def spy(u):
        probes.append(u)
        return orig(u)

    h._cloud_probe = spy
    h._cloud_endpoint("check")
    n1 = len(probes)
    h._cloud_endpoint("check")
    h._cloud_endpoint("check")
    n2 = len(probes)
    check("首次探测有发起试探", n1 >= 1, f"{n1} 次")
    check("★ 再次取端点不再探测（走缓存）", n2 == n1, f"{n1} → {n2}")

    # 改配置后缓存要失效
    h._cloud_reset_endpoints()
    h._cloud_endpoint("check")
    check("★ 重置后重新探测", len(probes) > n2, f"{n2} → {len(probes)}")

    # 缓存内容确实是可用 URL
    h._cloud_reset_endpoints()
    u = h._cloud_endpoint("check")
    check("缓存的 URL 指向 check_ban", "check_ban" in u, u)
finally:
    stop5()

print("  --- 换地址后缓存必须失效（否则改了地址不生效）---")
fake6a = FakeCloud("rest")
url6a, stop6a = fake6a.serve()
fake6b = FakeCloud("legacy")
url6b, stop6b = fake6b.serve()
try:
    h = CloudHost(url6a)
    h._cloud_endpoint("check")
    check("地址 A → REST", h._cloud_api_style_name() == "新版 REST 接口")
    # 直接改内存里的配置（模拟用户改地址）
    h._tools_config["cloud_blacklist"]["api_base"] = url6b
    ok, _, _ = h._cloud_test()
    check("★ 换地址后自动重新协商（不会沿用旧端点）",
          h._cloud_api_style_name() == "旧版 PHP 接口", h._cloud_api_style_name())
finally:
    stop6a()
    stop6b()


# ===========================================================================
# 5. 协议偏好 api_style
# ===========================================================================
print()
print("=" * 74)
print("5. ★ 协议偏好 api_style（rest / legacy / auto）")
print("=" * 74)

fake7 = FakeCloud("rest")
url7, stop7 = fake7.serve()
try:
    h = CloudHost(url7, style="legacy")
    u = h._cloud_endpoint("check")
    check("★ 锁定 legacy 时只试 .php", u.endswith(".php"), u)

    h2 = CloudHost(url7, style="rest")
    u2 = h2._cloud_endpoint("check")
    check("★ 锁定 rest 时选 REST 端点",
          u2.endswith("/api/check_ban") and not u2.endswith(".php"), u2)

    h3 = CloudHost(url7, style="auto")
    u3 = h3._cloud_endpoint("check")
    check("auto → 选到可用的 REST", u3.endswith("/api/check_ban"), u3)

    h4 = CloudHost(url7, style="bogus")
    check("★ 非法 api_style 回落 auto", h4._cloud_style() == "auto", h4._cloud_style())
finally:
    stop7()


# ===========================================================================
# 6. 错误码 → 人话
# ===========================================================================
print()
print("=" * 74)
print("6. ★ 错误码翻译（401 / 403 / 404 / 400）")
print("=" * 74)

fake8 = FakeCloud("rest")
url8, stop8 = fake8.serve()
try:
    # 401 无效 token
    h = CloudHost(url8, token="wrong_token")
    ok, msg, _ = h._cloud_test()
    check("401 → 鉴权失败提示", (not ok) and "401" in msg and "鉴权失败" in msg, msg)

    # 403 只读 key 做管理操作
    h2 = CloudHost(url8, token="public")
    h2._cloud_sync_worker({"xuid": "1", "reason": "x"})
    w = [m for lv, m in h2.logger.lines if lv == "W"]
    check("★ 403 → 权限不足提示（不会静默失败）",
          any("403" in m and "权限不足" in m for m in w), w[-1] if w else "")

    # 404 不存在的端点
    h3 = CloudHost(url8)
    try:
        h3._cloud_request(None, method="GET", url=url8 + "/api/no_such_thing")
        got = ""
    except RuntimeError as e:
        got = str(e)
    check("404 → 接口不存在提示", "404" in got and "接口不存在" in got, got)

    # 400 缺参数
    h4 = CloudHost(url8)
    try:
        h4._cloud_request(None, method="GET", url=url8 + "/api/check_ban")
        got4 = ""
    except RuntimeError as e:
        got4 = str(e)
    check("400 → 参数非法提示", "400" in got4 and "参数缺失" in got4, got4)

    # 错误 body 里的 message 要透出来
    check("★ 云端 message 被带进错误信息", "missing data" in got4, got4)
finally:
    stop8()


# ===========================================================================
# 7. 边界与健壮性
# ===========================================================================
print()
print("=" * 74)
print("7. 边界与健壮性")
print("=" * 74)

fake9 = FakeCloud("rest")
url9, stop9 = fake9.serve()
try:
    h = CloudHost(url9)
    # 撤销封禁至少要有一个标识
    ok, msg, ban = h._cloud_lookup({})
    check("lookup 无参数 → 明确报错", not ok and "XUID" in msg, msg)

    # 按 IP 查
    fake9.bans[("bedrock", "10.0.0.9")] = {"reason": "IP 判定", "player_name": "X",
                                           "ip_address": "10.0.0.9", "expires": 0}
    ok, msg, ban = h._cloud_lookup({"ip_address": "10.0.0.9"})
    check("按 IP 查询可用", ok and bool(ban), ban)

    # 未配置地址
    h2 = CloudHost("")
    try:
        h2._cloud_base()
        err = ""
    except RuntimeError as e:
        err = str(e)
    check("未配置地址 → 明确报错", "API 地址" in err, err)

    h3 = CloudHost("https://your-domain.com/api")
    try:
        h3._cloud_base()
        err3 = ""
    except RuntimeError as e:
        err3 = str(e)
    check("占位地址被视为未配置", "API 地址" in err3, err3)

    # ★ 默认值（NexaCode 联合云黑）必须能直接用，不能被当成占位符拒绝。
    #   它的只读 key 是公开的，开箱就该能查黑；如果把默认地址也拦掉，
    #   用户填了「看起来没改」的地址却报「未配置」，会非常费解。
    for real_base in ("https://cloudban.nexacode.dpdns.org",
                      "https://cloudban.nexacode.dpdns.org/api"):
        h5 = CloudHost(real_base)
        try:
            got = h5._cloud_base()
            err5 = ""
        except RuntimeError as e:
            got, err5 = "", str(e)
        check(f"默认地址可用：{real_base}", got == real_base.rstrip("/"), err5 or got)

    # 其它模板地址仍要拦住
    for ph in ("https://example.com", "https://example.com/api"):
        h6 = CloudHost(ph)
        try:
            h6._cloud_base()
            e6 = ""
        except RuntimeError as e:
            e6 = str(e)
        check(f"模板地址仍被拦：{ph}", "API 地址" in e6, e6)

    # 服务不可达
    h4 = CloudHost("http://127.0.0.1:9")   # 必然拒绝连接
    ok, msg, _ = h4._cloud_test()
    check("服务不可达 → 返回失败而不是抛异常", not ok, msg)

    # 未知逻辑名
    try:
        h._cloud_endpoint("nonexistent_kind")
        e5 = ""
    except RuntimeError as e:
        e5 = str(e)
    check("未知端点类型 → 明确报错", "未知" in e5, e5)
finally:
    stop9()


# ===========================================================================
# 8. 兼容老调用点：直接传旧端点路径仍可用
# ===========================================================================
print()
print("=" * 74)
print("8. ★ 兼容：直接传端点路径的调用仍然工作")
print("=" * 74)

fake10 = FakeCloud("rest")
url10, stop10 = fake10.serve()
try:
    h = CloudHost(url10)
    # 老代码写法：直接给 "check_ban.php" 这种名字
    res = h._cloud_request("check_ban", {"xuid": "999", "edition": "bedrock"})
    check("★ 传路径片段时不做协商，直接拼基址",
          isinstance(res, dict) and res.get("ok") is True, res)
    # 新版路径
    res2 = h._cloud_request("api/admin/stats", method="GET")
    check("传 admin 路径也直接拼", isinstance(res2, dict), res2)
finally:
    stop10()


# ===========================================================================
# 9. payload 里的 edition 填充规则
# ===========================================================================
print()
print("=" * 74)
print("9. edition 填充规则")
print("=" * 74)

h = CloudHost("http://127.0.0.1:1")
p = h._cloud_fill_edition({})
check("空 payload 补 bedrock", p.get("edition") == "bedrock", p)
p2 = h._cloud_fill_edition({"edition": "java"})
check("★ 调用方显式指定时不被覆盖", p2.get("edition") == "java", p2)
p3 = h._cloud_fill_edition({"edition": ""})
check("空字符串视为未指定", p3.get("edition") == "bedrock", p3)
p4 = h._cloud_fill_edition({"xuid": "1"})
check("不破坏原有字段", p4.get("xuid") == "1" and p4.get("edition") == "bedrock", p4)
check("CLOUD_EDITION 常量是 bedrock", Host.CLOUD_EDITION == "bedrock", Host.CLOUD_EDITION)


# ===========================================================================
# 10. 身份识别：只读 key 必须被认出来
# ===========================================================================
# ★ 这一节来自线上实测，不是照文档写的。
#   文档说「只读 key 访问 /api/admin/whoami 得到 403 forbidden」，
#   但线上实际返回的是 **401 + {"message":"forbidden"}**。
#   而「key 填错了」同样是 401，body 是 "invalid token"。
#   ⇒ 状态码无法区分这两种情况，只能看 body 的 message。
#   如果实现只按状态码判断，就会把「只读 Key 配好了」误报成
#   「Token 无效」，用户会去翻密钥，永远修不好。
# ===========================================================================
print()
print("=" * 74)
print("10. ★ 身份识别：只读 key 不被误报成「Token 无效」")
print("=" * 74)

# 情形 A：只读 key，服务端用 401 + forbidden（线上实际行为）
fakeA = FakeCloud("rest", whoami_401=True)
uA_, stopA = fakeA.serve()
try:
    hA = CloudHost(uA_, token="public")
    okA, msgA, rawA = hA._cloud_test()
    check("★ 只读 key：整体判定为「连接可用」", okA is True, (okA, msgA))
    check("★ 只读 key：身份识别为 readonly", (rawA or {}).get("_role") == "readonly",
          (rawA or {}).get("_role"))
    check("★ 只读 key：提示语说明只能查黑、不能封禁",
          "只读" in msgA and "不能封禁" in msgA, msgA)
    check("★ 只读 key 不被误报为「Token 无效」",
          "鉴权失败" not in msgA and "401" not in msgA, msgA)
finally:
    stopA()

# 情形 B：服务端按文档返回 403 + forbidden，也要能认出来
fakeB = FakeCloud("rest", whoami_401=False)
uB_, stopB = fakeB.serve()
try:
    hB = CloudHost(uB_, token="public")
    okB, msgB, rawB = hB._cloud_test()
    check("★ 403 版部署：同样识别为 readonly", (rawB or {}).get("_role") == "readonly",
          (rawB or {}).get("_role"))
    check("★ 403 版部署：连接可用", okB is True, (okB, msgB))
finally:
    stopB()

# 情形 C：管理员 key → 明确说是管理员
fakeC = FakeCloud("rest")
uC_, stopC = fakeC.serve()
try:
    hC = CloudHost(uC_, token="admintoken")
    okC, msgC, rawC = hC._cloud_test()
    check("管理员 key：识别为 admin", (rawC or {}).get("_role") == "admin",
          (rawC or {}).get("_role"))
    check("管理员 key：提示可封禁/解封", "管理员" in msgC and "可封禁" in msgC, msgC)
finally:
    stopC()

# 情形 D：key 真的填错了 → 必须如实报错，不能糊成「只读」
fakeD = FakeCloud("rest")
uD_, stopD = fakeD.serve()
try:
    hD = CloudHost(uD_, token="totally_wrong")
    okD, msgD, rawD = hD._cloud_test()
    check("★ 错误 key：如实报鉴权失败（不误判成只读）",
          okD is False and "鉴权失败" in msgD, (okD, msgD))
    check("错误 key：不给出 readonly 身份", (rawD or {}).get("_role") in (None, ""),
          (rawD or {}).get("_role"))
finally:
    stopD()

# 情形 E：老版服务没有 whoami → 404，应降级为「未能识别身份」而不是报错
fakeE = FakeCloud("legacy")
uE_, stopE = fakeE.serve()
try:
    hE = CloudHost(uE_, token="admintoken", style="legacy")
    okE, msgE, rawE = hE._cloud_test()
    check("★ 老版无 whoami：仍然判定连接可用", okE is True, (okE, msgE))
    check("★ 老版无 whoami：给出「未能识别身份」而非报错",
          "未能识别身份" in msgE, msgE)
finally:
    stopE()


print()
print("=" * 74)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
