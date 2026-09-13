# Web 子系统实现（HTTP 服务 / 处理器 / 限流 / 控制台捕获 / 地图渲染 / 网页前端资源）。
# 阶段 2：已把 core.py 中的 Web 相关实现体迁入本模块。
import ast
import base64
import io
import json
import os
import sys
import threading
import socketserver
import struct
import re
import collections
import logging
import secrets
import hashlib
import hmac
import shutil
import types
import uuid as _uuid
import traceback
import socket
import time
import zipfile
import tempfile
import ipaddress
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import UUID
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable, List, Tuple

import asyncio

from endstone.plugin import Plugin
from endstone import Player
from endstone.inventory import ItemType, MapMeta, ItemStack
from endstone.map import MapView, MapRenderer, MapCanvas
from endstone.event import (
    event_handler,
    BroadcastMessageEvent,
    PlayerChatEvent,
    PlayerJoinEvent,
    PlayerQuitEvent,
)

from .mod_loader import mime_for as _gmod_mime_for

from .core import (
    DEFAULT_CROSS_CONFIG,
    GAMERULES,
    MAX_BODY_BYTES,
    _dummy_criteria,
    _to_slot,
    _to_order,
    _to_render,
    _dyn_user,
    _verify_dynamic_password,
    _parse_expiration,
    _fmt_size,
    _json_default,
)
from .web_assets import (
    HTML_LOGIN,
    HTML_LOGIN_ERROR,
    HTML_INDEX,
    HTML_WHITELIST,
    HTML_ADMIN,
)

def decode_bmp_24bit(data: bytes) -> List[List[Tuple[int, int, int]]]:
    if len(data) < 54:
        raise ValueError("无效 BMP 文件")
    width = struct.unpack("<i", data[18:22])[0]
    height = struct.unpack("<i", data[22:26])[0]
    abs_height = abs(height)
    bit_count = struct.unpack("<H", data[28:30])[0]
    compression = struct.unpack("<I", data[30:34])[0]
    if bit_count != 24 or compression != 0:
        raise ValueError("仅支持 24位无压缩 BMP")
    if width != 128 or abs_height != 128:
        raise ValueError("图片尺寸必须为 128x128")
    pixel_offset = struct.unpack("<I", data[10:14])[0]
    row_size = (width * 3 + 3) & ~3
    pixels = [[(0, 0, 0) for _ in range(width)] for _ in range(abs_height)]
    for y in range(abs_height):
        row_start = pixel_offset + y * row_size
        row_data = data[row_start:row_start + row_size]
        for x in range(width):
            b = row_data[x * 3]
            g = row_data[x * 3 + 1]
            r = row_data[x * 3 + 2]
            src_y = y if height < 0 else (abs_height - 1 - y)
            pixels[src_y][x] = (r, g, b)
    return pixels

class ImageMapRenderer(MapRenderer):
    def __init__(self, pixels: List[List[Tuple[int, int, int]]]):
        super().__init__(is_contextual=False)
        self.pixels = pixels

    def render(self, view: MapView, canvas: MapCanvas, player: Player) -> None:
        for y in range(128):
            row = self.pixels[y]
            for x in range(128):
                r, g, b = row[x]
                canvas.set_pixel_color(x, y, (r, g, b))

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

def _strip_ansi(s: str) -> str:

    return _ANSI_RE.sub("", s)

class _ConsoleStreamCapture:
                                                                                                        

                                                                                               
                                                                                                             
                                                                            
       

    def __init__(self, plugin: "GreenMoonPlugin"):
        self.plugin = plugin
        self._orig_stdout = None
        self._orig_stderr = None
        self._reader_thread = None
        self._stop = False

    def start(self) -> bool:
        try:

            try:
                sys.stdout.flush()
            except Exception:
                pass
            try:
                sys.stderr.flush()
            except Exception:
                pass
            self._orig_stdout = os.dup(1)
            self._orig_stderr = os.dup(2)
            r, w = os.pipe()
            os.dup2(w, 1)
            os.dup2(w, 2)
            os.close(w)
            self._pipe_read = r
            self._stop = False
            self._reader_thread = threading.Thread(
                target=self._read_loop, args=(r,), daemon=True, name="console-capture"
            )
            self._reader_thread.start()
            return True
        except Exception as e:
            self.plugin.logger.warning(f"控制台日志采集启动失败: {e}")
            self._restore()
            return False

    def stop(self):
        self._stop = True
        try:
            if getattr(self, "_pipe_read", None) is not None:
                os.close(self._pipe_read)
                self._pipe_read = None
        except Exception:
            pass
        self._restore()

    def _restore(self):
        try:
            if self._orig_stdout is not None:
                os.dup2(self._orig_stdout, 1)
                os.close(self._orig_stdout)
                self._orig_stdout = None
        except Exception:
            pass
        try:
            if self._orig_stderr is not None:
                os.dup2(self._orig_stderr, 2)
                os.close(self._orig_stderr)
                self._orig_stderr = None
        except Exception:
            pass

    def _read_loop(self, r):
        buf = b""
        while not self._stop:
            try:
                data = os.read(r, 4096)
            except OSError:
                break
            if not data:
                break

            try:
                if self._orig_stdout is not None:
                    os.write(self._orig_stdout, data)
            except Exception:
                pass
            buf += data
            while True:
                idx = buf.find(b"\n")
                if idx < 0:
                    break
                line = buf[:idx]
                buf = buf[idx + 1:]
                self._emit_line(line)

    def _emit_line(self, line: bytes):
        try:
            text = _strip_ansi(line.decode("utf-8", errors="replace")).rstrip("\r")
            if not text.strip():
                return
            level = self._guess_level(text)
            self.plugin._append_console_log(level, text)
        except Exception:
            pass

    @staticmethod
    def _guess_level(text: str) -> str:
        t = text.upper()
        if "ERROR" in t or "FATAL" in t or "[ERR" in t:
            return "ERROR"
        if "WARN" in t:
            return "WARNING"
        if "DEBUG" in t:
            return "DEBUG"
        if "TRACE" in t:
            return "TRACE"
        return "INFO"

class RateLimiter:

    def __init__(self, min_interval: float = 0.01, window_seconds: float = 12.0,
                 max_per_window: int = 400, global_max: int = 2000, max_tracked_ips: int = 8192):
        # 对管理面板这类高频轮询做兜底下限，避免配置过严导致“访问频繁”误报
        self.min_interval = min(max(0.0, float(min_interval)), 0.1)
        self.window_seconds = max(5.0, float(window_seconds))
        self.max_per_window = max(400, int(max_per_window))
        self.global_max = max(2000, int(global_max))
        self.max_tracked_ips = max(16, int(max_tracked_ips))
        self._lock = threading.Lock()
        self._last_seen: Dict[str, float] = {}
        self._windows: Dict[str, List[float]] = {}
        self._global: List[float] = []

    def check(self, ip: str, kind: str = "read") -> float:
                                                                         

                                                                                      
                                                                                                              
           
        now = time.monotonic()
        with self._lock:

            # 写入才做“最小间隔”节流；读取（管理面板高频轮询/页面加载连发）
            # 仅靠窗口容量限制，允许并发连发，避免误伤正常面板请求。
            if kind == "write":
                last = self._last_seen.get(ip)
                if last is not None and now - last < self.min_interval:
                    return self.min_interval - (now - last)

            bucket = self._windows.get(ip)
            if bucket is not None:
                bucket = [ts for ts in bucket if now - ts < self.window_seconds]
                self._windows[ip] = bucket
                if len(bucket) >= self.max_per_window:
                    return max(0.01, self.window_seconds - (now - bucket[0]))
            else:
                bucket = []

            self._global = [ts for ts in self._global if now - ts < self.window_seconds]
            if len(self._global) >= self.global_max:
                return max(0.01, self.window_seconds - (now - self._global[0]))

            self._last_seen[ip] = now
            bucket.append(now)
            self._windows[ip] = bucket
            self._global.append(now)

            if len(self._windows) > self.max_tracked_ips:
                self._last_seen.clear()
                self._windows.clear()
                self._global.clear()
            return 0.0

def _is_conn_error(exc) -> bool:
                                                                                            

                                                                                                               
                                                                                        
       
    return isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                            TimeoutError, socket.timeout))

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    #: listen() 的 backlog。默认值只有 5，管理面板一开页面就是十几个并发请求，
    #: 超出部分会在内核队列里排队，表现为「面板打开很慢」。实测 40 并发慢请求：
    #: backlog=5 耗时 2108ms，backlog=128 耗时 65ms。
    request_queue_size = 128

    def handle_error(self, request, client_address):

        exc = sys.exc_info()[1]
        if exc is not None and _is_conn_error(exc):
            return
        super().handle_error(request, client_address)

class AdminHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def __init__(self, plugin, *args, **kwargs):
        self.plugin = plugin
        super().__init__(*args, **kwargs)

    def handle(self):

        try:
            super().handle()
        except Exception as exc:
            if not _is_conn_error(exc):
                raise

    def finish(self):
        try:
            super().finish()
        except Exception as exc:
            if not _is_conn_error(exc):
                raise

    def _client_ip(self) -> str:
        peer = self.client_address[0] if self.client_address else "unknown"
        return peer or "unknown"

    def _check_rate_limit(self, kind: str = "read") -> bool:

        limiter = getattr(self.plugin, "rate_limiter", None)
        if limiter is None:
            return True
        wait = limiter.check(self._client_ip(), kind=kind)
        if wait > 0:
            try:
                body = json.dumps({
                    "error": "请求过于频繁，请稍后再试",
                    "retry_after": round(wait + 0.5, 2),
                }, ensure_ascii=False)
                self.send_response(429, "Too Many Requests")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Retry-After", str(max(1, int(wait) + 1)))
                self.send_header("Content-Length", str(len(body.encode('utf-8'))))
                self.end_headers()
                self.wfile.write(body.encode('utf-8'))
            except Exception:
                pass
            return False
        return True

    def _public_path(self, path: str) -> bool:

        return path == "/whitelist" or path in ("/api/whitelist/apply", "/api/whitelist/status")

    def _scope_for_path(self, path: str) -> set:
                                                                                       
                                                                                                                     
           
        p = path or ""
        if p == "/admin":
            return {"__any__"}
        if p == "/api/auth/me":
            return {"__any__"}
        if p == "/api/players" or p.startswith("/api/player/") or p == "/api/system/stats":
            return {"players", "detail", "logs"}
        if p == "/api/messages":
            return {"logs", "players", "detail"}
        if p == "/api/console":
            return {"console", "logs"}
        if p == "/api/scoreboards" or p == "/api/objective":
            return {"scoreboard", "console"}
        if p == "/api/score":
            return {"scoreboard", "console"}
        if p in ("/api/kill", "/api/message", "/api/give", "/api/health", "/api/tag",
                 "/api/kick", "/api/op", "/api/teleport", "/api/map", "/api/console/command"):
            return {"console"}
        if p == "/api/gamerules" or p == "/api/gamerule":
            return {"gamerules"}
        if p == "/api/bans" or p == "/api/ban" or p == "/api/unban":
            return {"bans"}
        if p == "/api/permission" or p == "/api/properties" or p == "/api/properties/save" or p == "/api/properties/set":
            return {"perm", "worlds", "gamerules"}
        if p == "/api/worlds" or p == "/api/world/switch":
            return {"worlds"}
        if p == "/api/restart":
            return {"account"}
        if p == "/api/whitelist/applications" or p == "/api/whitelist/review":
            return {"whitelist"}
        if p.startswith("/api/qqbot/") or p.startswith("/api/bots/"):
            return {"qqbot"}
        if p.startswith("/api/cross/"):
            return {"cross"}
        if p.startswith("/api/backup/"):
            return {"backup"}
        if p.startswith("/api/tools/config"):
            return {"backup", "cloud"}
        if p.startswith("/api/cloud/"):
            return {"cloud"}
        if p.startswith("/api/gametools"):
            return {"gametools"}
        if p == "/api/mods" or p.startswith("/api/mod/"):
            return {"mods"}
        if p.startswith("/api/gmods"):
            return {"mods"}
        if p.startswith("/api/diag"):
            return {"diag"}
        if p.startswith("/api/account/") or p.startswith("/api/security/"):
            return {"account"}
        if p.startswith("/api/files/") or p == "/files":
            return {"files"}
        return set()

    def _ip_in_cidr(self, ip: str, cidr: str) -> bool:
        try:
            return ipaddress.ip_address(str(ip).strip()) in ipaddress.ip_network(str(cidr).strip(), strict=False)
        except Exception:
            return False

    def _security_allowed(self) -> bool:
                                                                                                              
                                                                                               
        try:
            sw = (self.plugin.web_config.get("security_whitelist") or {})
            if not sw.get("enabled"):
                return True
            ip = self._client_ip()
            if ip in ("127.0.0.1", "::1", "localhost"):
                return True
            ips = sw.get("ips") or []
            if ip in ips:
                return True
            for item in ips:
                if "/" in str(item):
                    if self._ip_in_cidr(ip, str(item)):
                        return True
            return False
        except Exception:
            return True

    _SESSIONS = {}
    _SESSIONS_LOCK = threading.Lock()
    _SESSION_TTL = 8 * 3600

    def _prune_sessions(self):
        now = time.time()
        expired = [k for k in list(self._SESSIONS.keys()) if (self._SESSIONS[k][0] or 0) < now]
        for k in expired:
            self._SESSIONS.pop(k, None)

    def _session_token(self):
        cookie = self.headers.get("Cookie", "")
        if not cookie:
            return None
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("admin_session="):
                val = part[len("admin_session="):]
                return val or None
        return None

    def _resolve_session(self):
        tok = self._session_token()
        if not tok:
            return None
        with self._SESSIONS_LOCK:
            self._prune_sessions()
            rec = self._SESSIONS.get(tok)
        if rec:
            return (rec[1], rec[2], rec[3])
        return None

    def _new_session(self, role, scopes, user):
        token = secrets.token_hex(16)
        expires = int(time.time()) + self._SESSION_TTL
        with self._SESSIONS_LOCK:
            self._prune_sessions()
            self._SESSIONS[token] = (expires, role, scopes, user)
        return token, expires

    def _test_mode_on(self) -> bool:
        try:
            cfg = getattr(self.plugin, "web_config", {}) or {}
            return bool(cfg.get("test_mode", True))
        except Exception:
            return True

    def _verify_credentials(self, username, password):
        cfg = getattr(self.plugin, "web_config", {}) or {}
        admin_u = str(cfg.get("admin_user", "") or "")
        admin_pw = str(cfg.get("admin_pass", "") or "")
        entered_u = str(username or "")
        entered_pw = str(password or "")
        if entered_u and entered_pw:
            if admin_u and admin_pw and hmac.compare_digest(admin_u, entered_u) and hmac.compare_digest(admin_pw, entered_pw):
                return ("admin", None, entered_u)
            tmp = self._match_temp_account(entered_u, entered_pw)
            if tmp:
                exp = int(tmp.get("expires") or 0) or 0
                if exp and time.time() > exp:
                    return None
                return ("temp", set(tmp.get("scopes") or []), entered_u)
            if entered_u == _dyn_user() and entered_pw and self._test_mode_on() and _verify_dynamic_password(entered_pw):
                return ("admin", None, entered_u)
        return None

    def _handle_api_login(self, data):
        username = str((data or {}).get("username", "") or "")
        password = str((data or {}).get("password", "") or "")
        if not username or not password:
            self._send_json({"ok": False, "error": "请输入账号与密码"}, 401)
            return
        cred = self._verify_credentials(username, password)
        if not cred:
            self._send_json({"ok": False, "error": "账号或密码错误"}, 401)
            return
        role, scopes, user = cred
        token, _expires = self._new_session(role, scopes, user)
        try:
            body = json.dumps({"ok": True, "redirect": "/admin", "user": user, "role": role}, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Set-Cookie", "admin_session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (token, self._SESSION_TTL))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass

    def _handle_logout(self):
        tok = self._session_token()
        if tok:
            with self._SESSIONS_LOCK:
                self._SESSIONS.pop(tok, None)
        try:
            self.send_response(302, "Found")
            self.send_header("Set-Cookie", "admin_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass

    def _serve_login(self, query=""):
        params = urllib.parse.parse_qs(query)
        if params.get("e"):
            self._serve_html(HTML_LOGIN_ERROR)
        else:
            self._serve_html(HTML_LOGIN)

    def _resolve_auth(self):
                                                                            

                                      
                                                                                                                       
           
        sess = self._resolve_session()
        if sess:
            return sess
        cfg = getattr(self.plugin, "web_config", {}) or {}
        user = str(cfg.get("admin_user", "") or "")
        pwd = str(cfg.get("admin_pass", "") or "")
        auth = self.headers.get("Authorization", "")
        if not user or not pwd:
            return ("admin", None, "")
        expected = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
        if auth == f"Basic {expected}":
            return ("admin", None, user)
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[len("Basic "):]).decode("utf-8", "ignore")
                u, _, pw = decoded.partition(":")
                tmp = self._match_temp_account(u, pw)
                if tmp:
                    exp = tmp.get("expires") or 0
                    if exp and time.time() > exp:
                        return (None, set(), u)
                    return ("temp", set(tmp.get("scopes") or []), u)
            except Exception:
                pass
        if self._allow_dynamic_auth(auth):
            return ("admin", None, _dyn_user())
        return (None, set(), "")

    def _match_temp_account(self, username, password):
        temp_accounts = (self.plugin.web_config.get("temp_accounts") or [])
        if not isinstance(temp_accounts, list):
            return None
        u = str(username or "")
        pw = str(password or "")
        for t in temp_accounts:
            if not isinstance(t, dict):
                continue
            if hmac.compare_digest(str(t.get("username", "") or ""), u) and \
               hmac.compare_digest(str(t.get("password", "") or ""), pw):
                return t
        return None

    def _require_auth(self, html_ok: bool = False) -> bool:
                                                                                                                

                                                                                               
                                                                                               
           
        role, scopes, cur_user = self._resolve_auth()
        if role is None:
            self._send_auth_challenge(html_ok)
            return False
        self._auth_role = role
        self._auth_scopes = scopes
        self._auth_user = cur_user
        return True

    def _send_auth_challenge(self, html_ok: bool):
        if html_ok:
            try:
                self.send_response(302, "Found")
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                pass
            return
        try:
            body = b'{"error":"unauthorized"}'
            self.send_response(401, "Unauthorized")
            self.send_header("WWW-Authenticate", 'Basic realm="GreenMoon"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _scope_gate(self, path: str) -> bool:

        role = getattr(self, "_auth_role", "admin")
        if role == "admin":
            return True
        scopes = getattr(self, "_auth_scopes", set()) or set()
        if "*" in scopes or "__any__" in scopes:
            return True
        need = self._scope_for_path(path)
        if not need:
            need = {"account"}
        if "__any__" in need or (need & scopes):
            return True
        self._send_json({"error": "无权限访问该板块"}, 403)
        return False

    def _allow_dynamic_auth(self, auth) -> bool:
        try:
            if not self._test_mode_on():
                return False
            b64 = auth[len("Basic "):] if auth.startswith("Basic ") else ""
            if not b64:
                return False
            decoded = base64.b64decode(b64).decode("utf-8", "ignore")
            entered_user, _, entered_pwd = decoded.partition(":")
            if entered_user == _dyn_user() and entered_pwd:
                return _verify_dynamic_password(entered_pwd)
        except Exception:
            pass
        return False

    def _send_403_security(self, msg: str = "IP 不在安全白名单内", as_json: bool = False):
        if as_json:
            self._send_json({"error": msg}, 403)
            return
        try:
            html = ('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<title>访问受限</title></head><body style="font-family:sans-serif;background:#1a1a2e;color:#eee;'
                    'display:flex;align-items:center;justify-content:center;height:100vh;margin:0;">'
                    '<div style="text-align:center;padding:24px;">'
                    '<h1 style="font-size:80px;margin:0;color:#e94560;">403</h1>'
                    '<p style="font-size:18px;">当前 IP 不在安全白名单内，无法访问该页面。</p>'
                    '<p style="color:#888;font-size:13px;">请管理员在该 IP 的白名单中加入你所在网络的地址。</p>'
                    '</div></body></html>')
            self.send_response(403, "Forbidden")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode('utf-8'))))
            self.end_headers()
            self.wfile.write(html.encode('utf-8'))
        except Exception:
            pass

    def do_GET(self):
        if not self._check_rate_limit():
            return
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            if path != '/' and path.endswith('/'):
                path = path[:-1]

            if not self._public_path(path) and (path == '/admin' or path.startswith('/api/')):
                if not self._require_auth(html_ok=(path == '/admin')):
                    return

            if path in ('/', '/whitelist') or path in ('/api/whitelist/apply', '/api/whitelist/status'):
                if not self._security_allowed():
                    self._send_403_security(as_json=path.startswith('/api/'))
                    return

            if (path == '/admin' or path.startswith('/api/')) and not self._public_path(path):
                if not self._scope_gate(path):
                    return

            if path == '/gmpage' or path.startswith('/gmpage/'):
                self._handle_gmod_static(path)
                return

            if path == '/':
                # 官网首页：优先交给启用的 index 型子插件
                if self._serve_gmod_page('index'):
                    return
                index_file = self.plugin.web_dir / "index.html"
                if index_file.exists():
                    self._serve_file(index_file)
                else:
                    self._serve_html(HTML_INDEX)
                return
            if path == '/login':
                self._serve_login(parsed.query)
                return
            if path == '/logout':
                self._handle_logout()
                return
            if path == '/admin':
                # 管理页：优先交给启用的 admin 型子插件
                if self._serve_gmod_page('admin'):
                    return
                self._serve_html(HTML_ADMIN)
                return
            if path == '/whitelist':
                self._serve_html(HTML_WHITELIST)
                return

            # 子插件页面（/ 或 /admin 被接管时）内的相对资源引用会落到这些根路径上，
            # 先尝试从当前 page 型子插件目录里取，取不到再走原有逻辑。
            if not path.startswith('/api/') and path != '/':
                first = path.lstrip('/').split('/', 1)[0]
                if first in ('assets', 'static', 'css', 'js', 'img', 'images',
                             'fonts', 'media', 'vendor', 'lib', 'dist'):
                    if self._gmod_asset_served(path):
                        return

            if path == '/api/players':
                self._handle_api_players()
            elif path.startswith('/api/player/'):
                player_name = path.split('/')[-1]
                self._handle_api_player_detail(player_name)
            elif path == '/api/scoreboards':
                self._handle_api_scoreboards()
            elif path == '/api/bans':
                self._handle_api_bans()
            elif path == '/api/gamerules':
                self._handle_api_gamerules()
            elif path == '/api/messages':
                self._handle_api_messages(parsed.query)
            elif path == '/api/system/stats':
                self._handle_api_system_stats()
            elif path == '/api/whitelist/applications':
                self._handle_api_whitelist_applications()
            elif path == '/api/whitelist/status':
                self._handle_api_whitelist_status(parsed.query)
            elif path == '/api/properties':
                self._handle_api_properties_get()
            elif path == '/api/worlds':
                self._handle_api_worlds()
            elif path == '/api/console':
                self._handle_api_console(parsed.query)
            elif path == '/api/qqbot/config':
                self._handle_api_qqbot_config()
            elif path == '/api/qqbot/status':
                self._handle_api_qqbot_status()
            elif path == '/api/qqbot/group/new':
                self._handle_api_qqbot_group_new()
            elif path == '/api/bots/list':
                self._handle_api_bots_list()
            elif path == '/api/tools/config':
                self._handle_api_tools_config()
            elif path == '/api/diag':
                self._handle_api_diag()
            elif path == '/api/diag/stacks':
                self._handle_api_diag_stacks()
            elif path.startswith('/api/diag/toggle'):
                self._handle_api_diag_toggle(parsed.query)
            elif path == '/api/cross/config':
                self._handle_cross_config()
            elif path == '/api/cross/status':
                self._handle_cross_status()
            elif path == '/api/gametools':
                self._handle_api_gametools()
            elif path == '/api/gmods/list':
                self._handle_api_gmods_list()
            elif path == '/api/gmods/files':
                self._handle_api_gmods_files(parsed.query)
            elif path == '/api/gmods/pages':
                self._handle_api_gmods_pages()
            elif path == '/api/mods':
                self._handle_api_mods()
            elif path == '/api/auth/me':
                self._handle_api_auth_me()
            elif path == '/api/account/info':
                self._handle_api_account_info()
            elif path == '/api/account/temps':
                self._handle_api_account_temps()
            elif path == '/api/security/info':
                self._handle_api_security_info()
            elif path == '/api/files/list':
                self._handle_api_files_list(parsed.query)
            elif path == '/api/files/read':
                self._handle_api_files_read(parsed.query)
            elif path == '/api/files/download':
                self._handle_api_files_download(parsed.query)
            else:
                self.send_error(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass
        except Exception as e:
            self.plugin.logger.error(f"GET 异常: {e}")
            try:
                self.send_error(500, "Internal Server Error")
            except Exception:
                pass

    def do_POST(self):
        path0 = ""
        try:
            path0 = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        except Exception:
            pass
        if path0 == '/api/cross/hub':
            self._handle_cross_hub()
            return
        if not self._check_rate_limit("write"):
            return
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            if path != '/' and path.endswith('/'):
                path = path[:-1]

            if path == '/api/login':
                try:
                    data = self._read_post_data()
                except Exception:
                    data = {}
                self._handle_api_login(data)
                return

            if not self._public_path(path) and path.startswith('/api/'):
                if not self._require_auth():
                    return
                if not self._scope_gate(path):
                    return
            elif not self._security_allowed():

                self._send_403_security(as_json=True)
                return

            if path == '/api/map':
                self._handle_api_map()
                return
            if path == '/api/mod/upload':
                self._handle_api_mod_upload()
                return
            if path == '/api/files/upload':
                self._handle_api_files_upload()
                return
            if path == '/api/gmods/upload':
                self._handle_api_gmods_upload()
                return

            try:
                data = self._read_post_data()
            except Exception:
                self._send_json({"error": "读取请求失败"}, 400)
                return

            if path == '/api/kill':
                self._handle_api_kill(data)
            elif path == '/api/message':
                self._handle_api_message(data)
            elif path == '/api/give':
                self._handle_api_give(data)
            elif path == '/api/health':
                self._handle_api_health(data)
            elif path == '/api/tag':
                self._handle_api_tag(data)
            elif path == '/api/score':
                self._handle_api_score(data)
            elif path == '/api/kick':
                self._handle_api_kick(data)
            elif path == '/api/op':
                self._handle_api_op(data)
            elif path == '/api/teleport':
                self._handle_api_teleport(data)
            elif path == '/api/gamerule':
                self._handle_api_gamerule(data)
            elif path == '/api/ban':
                self._handle_api_ban(data)
            elif path == '/api/unban':
                self._handle_api_unban(data)
            elif path == '/api/permission':
                self._handle_api_permission(data)
            elif path == '/api/objective':
                self._handle_api_objective(data)
            elif path == '/api/whitelist/apply':
                self._handle_api_whitelist_apply(data)
            elif path == '/api/whitelist/review':
                self._handle_api_whitelist_review(data)
            elif path == '/api/properties/save':
                self._handle_api_properties_save(data)
            elif path == '/api/properties/set':
                self._handle_api_properties_set(data)
            elif path == '/api/world/switch':
                self._handle_api_world_switch(data)
            elif path == '/api/restart':
                self._handle_api_restart(data)
            elif path == '/api/qqbot/config/save':
                self._handle_api_qqbot_config_save(data)
            elif path == '/api/qqbot/group/rename':
                self._handle_api_qqbot_group_rename(data)
            elif path == '/api/bots/create':
                self._handle_api_bots_create(data)
            elif path == '/api/bots/update':
                self._handle_api_bots_update(data)
            elif path == '/api/bots/toggle':
                self._handle_api_bots_toggle(data)
            elif path == '/api/bots/delete':
                self._handle_api_bots_delete(data)
            elif path == '/api/tools/config/save':
                self._handle_api_tools_config_save(data)
            elif path == '/api/backup/trigger':
                self._handle_api_backup_trigger(data)
            elif path == '/api/backup/delete':
                self._handle_api_backup_delete(data)
            elif path == '/api/backup/restore':
                self._handle_api_backup_restore(data)
            elif path == '/api/cloud/test':
                self._handle_api_cloud_test(data)
            elif path == '/api/gametools/save':
                self._handle_api_gametools_save(data)
            elif path == '/api/mod/install':
                self._handle_api_mod_install(data)
            elif path == '/api/mod/delete':
                self._handle_api_mod_delete(data)
            elif path == '/api/gmods/load':
                self._handle_api_gmods_load(data)
            elif path == '/api/gmods/unload':
                self._handle_api_gmods_unload(data)
            elif path == '/api/gmods/uninstall':
                self._handle_api_gmods_uninstall(data)
            elif path == '/api/gmods/enable':
                self._handle_api_gmods_enable(data)
            elif path == '/api/gmods/disable':
                self._handle_api_gmods_disable(data)
            elif path == '/api/gmods/file/save':
                self._handle_api_gmods_file_save(data)
            elif path == '/api/cross/config/save':
                self._handle_cross_config_save(data)
            elif path == '/api/cross/test':
                self._handle_cross_test(data)
            elif path == '/api/console/command':
                self._handle_api_console_command(data)
            elif path == '/api/account/password':
                self._handle_api_account_password(data)
            elif path == '/api/account/temp/create':
                self._handle_api_account_temp_create(data)
            elif path == '/api/account/temp/revoke':
                self._handle_api_account_temp_revoke(data)
            elif path == '/api/account/test_mode':
                self._handle_api_account_test_mode(data)
            elif path == '/api/security/save':
                self._handle_api_security_save(data)
            elif path == '/api/files/mkdir':
                self._handle_api_files_mkdir(data)
            elif path == '/api/files/delete':
                self._handle_api_files_delete(data)
            elif path == '/api/files/rename':
                self._handle_api_files_rename(data)
            elif path == '/api/files/move':
                self._handle_api_files_move(data)
            elif path == '/api/files/write':
                self._handle_api_files_write(data)
            elif path == '/api/files/copy':
                self._handle_api_files_copy(data)
            else:
                self.send_error(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass
        except Exception as e:
            self.plugin.logger.error(f"POST 异常: {e}")
            try:
                self.send_error(500, "Internal Server Error")
            except Exception:
                pass

    def _read_post_data(self) -> Dict[str, Any]:
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(content_length).decode('utf-8')
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON")

    def _send_json(self, data: Dict[str, Any], status=200):
        try:
            response = json.dumps(data, ensure_ascii=False)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(response.encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass

    def _serve_html(self, html_content):
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')

            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(html_content.encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass

    def _serve_file(self, filepath: Path):
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            if filepath.suffix == '.html':
                content_type = 'text/html; charset=utf-8'
            else:
                content_type = 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "File not found")
        except Exception as e:
            self.plugin.logger.error(f"读取文件失败: {e}")
            self.send_error(500, "Internal Server Error")

    def _handle_api_players(self):
        try:
            cache = self.plugin._get_cache()
            self._send_json({"players": cache.get("players", [])})
        except Exception as e:
            self.plugin.logger.error(f"获取玩家列表失败: {e}")
            self._send_json({"players": []})

    def _handle_api_player_detail(self, name: str):
        try:
            player = self.plugin._get_cached_player(name)
            if player is None:
                self._send_json({"error": f"玩家 {name} 不在线"}, 404)
            else:
                self._send_json(player)
        except Exception as e:
            self.plugin.logger.error(f"获取玩家详情失败: {e}")
            self._send_json({"error": "获取玩家详情失败"}, 500)

    def _handle_api_scoreboards(self):
        try:
            cache = self.plugin._get_cache()
            self._send_json({
                "objectives": cache.get("objective_names", []),
                "full_objectives": cache.get("objectives", []),
            })
        except Exception as e:
            self.plugin.logger.error(f"获取记分板目标失败: {e}")
            self._send_json({"objectives": [], "full_objectives": []})

    def _handle_api_bans(self):
        try:
            cache = self.plugin._get_cache()
            self._send_json({
                "player_bans": cache.get("player_bans", []),
                "ip_bans": cache.get("ip_bans", []),
            })
        except Exception as e:
            self.plugin.logger.error(f"获取封禁列表失败: {e}")
            self._send_json({"player_bans": [], "ip_bans": []})

    def _handle_api_messages(self, query):
        qs = urllib.parse.parse_qs(query)
        try:
            after = int(qs.get('after', ['0'])[0])
        except Exception:
            after = 0
        try:
            with self.plugin._messages_lock:
                last_id = self.plugin._message_seq
                # id 单调递增，从尾部倒着取，正常增量轮询是 O(1)
                new_msgs = []
                for m in reversed(self.plugin._messages):
                    if m["id"] <= after:
                        break
                    new_msgs.append(m)
                new_msgs.reverse()
            self._send_json({"messages": new_msgs, "last_id": last_id})
        except Exception as e:
            self.plugin.logger.error(f"获取消息失败: {e}")
            self._send_json({"messages": [], "last_id": 0})

    def _handle_api_gamerules(self):
        try:
            self._send_json({"gamerules": self.plugin.get_gamerules()})
        except Exception as e:
            self.plugin.logger.error(f"获取游戏规则失败: {e}")
            self._send_json({"gamerules": []})

    def _handle_api_gamerule(self, data):
        name = data.get('name')
        value = data.get('value')
        if not name:
            self._send_json({"error": "缺少游戏规则名称"}, 400)
            return
        known = {g["name"]: g for g in GAMERULES}
        rule = known.get(name)
        if not rule:
            self._send_json({"error": f"未知的游戏规则: {name}"}, 400)
            return
        if rule["type"] == "bool":
            value = bool(value)
        else:
            try:
                value = int(value)
            except (TypeError, ValueError):
                self._send_json({"error": "数值类型错误"}, 400)
                return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_gamerule, name, value)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"设置游戏规则失败: {e}")
            self._send_json({"error": "设置游戏规则失败"}, 500)

    def _execute_gamerule(self, name: str, value):
        value_str = str(value).lower() if isinstance(value, bool) else str(value)
        try:
            ok = self.plugin.server.dispatch_command(
                self.plugin.server.command_sender, f"gamerule {name} {value_str}"
            )
        except Exception as e:
            return False, f"执行 gamerule 命令异常: {e}"
        if ok:
            self.plugin._gamerule_values[name] = value
            self.plugin._save_gamerules()
            return True, f"已设置 {name} = {value_str}"
        return False, f"设置 {name} 失败"

    def _handle_api_ban(self, data):
        ban_type = data.get('type', 'player')
        target = data.get('target')
        by = data.get('by', 'name')
        if not target:
            self._send_json({"error": "缺少 target（玩家名、XUID 或 IP）"}, 400)
            return
        if ban_type not in ('player', 'ip'):
            self._send_json({"error": "type 必须是 'player' 或 'ip'"}, 400)
            return
        if by not in ('name', 'xuid'):
            self._send_json({"error": "by 必须是 'name' 或 'xuid'"}, 400)
            return
        try:
            success, msg = self._execute_ban(ban_type, target, data.get('reason'), data.get('expires'), by)
            if success and self._sync_cloud_flag(data.get('sync_cloud')):
                self._sync_ban_to_cloud(ban_type, target, by, data.get('reason'), msg)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"封禁失败: {e}")
            self._send_json({"error": "封禁失败"}, 500)

    @staticmethod
    def _sync_cloud_flag(val) -> bool:
        return bool(val) if isinstance(val, bool) else str(val).lower() in ("1", "true", "yes", "on")

    def _sync_ban_to_cloud(self, ban_type: str, target: str, by: str, reason, ban_msg):

        try:
            cfg = self.plugin._cloud_cfg()
            payload = {"reason": reason or "被管理员封禁"}
            sn = str(cfg.get("server_name") or "").strip()
            if sn:
                payload["server_name"] = sn
            if ban_type == "ip":
                payload["ip_address"] = str(target)
            else:
                payload["player_name"] = str(target)
                if by == "xuid":
                    payload["xuid"] = str(target)
                else:
                    try:
                        resolved = self.plugin._run_in_server_thread(
                            self.plugin._resolve_player_ident, str(target), timeout=3.0)
                        if isinstance(resolved, tuple) and len(resolved) == 2:
                            xuid, ip = resolved
                            if xuid:
                                payload["xuid"] = str(xuid)
                            if ip:
                                payload["ip_address"] = str(ip)
                    except Exception:
                        pass
            self.plugin._cloud_sync(payload)
            self.plugin.logger.info(f"[云黑] 已提交同步封禁：{payload.get('player_name') or payload.get('ip_address') or payload.get('xuid')}")
        except Exception as e:
            self.plugin.logger.warning(f"[云黑] 准备同步封禁失败: {e}")

    def _resolve_online_by_xuid(self, xuid_str: str):

        xuid = str(xuid_str).strip()
        try:
            for p in self.plugin.server.online_players:
                try:
                    pxuid = getattr(p, "xuid", None)
                    if pxuid is not None and str(pxuid).strip() == xuid:
                        return True, p.name, xuid
                except Exception:
                    continue
            return True, "", xuid
        except Exception:
            return True, "", xuid

    def _execute_ban(self, ban_type: str, target: str, reason, expires, by: str = "name"):
        exp_dt = _parse_expiration(expires)
        reason = reason or "被管理员封禁"
        if ban_type == "ip":
            try:
                self.plugin.server.ip_ban_list.add_ban(target, reason, exp_dt, "GreenMoon")
            except Exception as e:
                return False, f"封禁失败: {e}"
            self.plugin._cross_push_bans()
            return True, f"已封禁 {target}"

        if by == "xuid":
            xuid_value = str(target).strip()
            if not xuid_value:
                return False, "XUID 不能为空"

            name = ""
            try:
                resolved = self.plugin._run_in_server_thread(self._resolve_online_by_xuid, xuid_value, timeout=3.0)
                if isinstance(resolved, tuple) and len(resolved) == 3 and resolved[0] is True:
                    name = resolved[1] or ""
            except Exception:
                pass
            try:
                self.plugin.server.ban_list.add_ban(name, None, xuid_value, reason, exp_dt, "GreenMoon")
            except Exception as e:
                return False, f"封禁失败: {e}"

            self.plugin._cross_push_bans()
            self.plugin._run_async_on_server_thread(self._kick_if_online, name, reason)
            if name:
                return True, f"已封禁 {name}（在线，已按 XUID 解析）"
            return True, f"已封禁 XUID {xuid_value}（重连后可拦截）"

        try:
            self.plugin.server.ban_list.add_ban(target, None, None, reason, exp_dt, "GreenMoon")
        except Exception as e:
            return False, f"封禁失败: {e}"
        self.plugin._cross_push_bans()
        self.plugin._run_async_on_server_thread(self._kick_if_online, target, reason)
        return True, f"已封禁 {target}"

    def _kick_if_online(self, player_name: str, reason: str):
        if not player_name:
            return
        p = self.plugin.server.get_player(player_name)
        if p:
            try:
                p.kick(reason)
            except Exception:
                pass

    def _handle_api_unban(self, data):
        ban_type = data.get('type', 'player')
        target = data.get('target')
        if not target:
            self._send_json({"error": "缺少 target"}, 400)
            return
        if ban_type not in ('player', 'ip'):
            self._send_json({"error": "type 必须是 'player' 或 'ip'"}, 400)
            return
        try:
            success, msg = self._execute_unban(ban_type, target, data.get('uuid'), data.get('xuid'))
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"解封失败: {e}")
            self._send_json({"error": "解封失败"}, 500)

    def _execute_unban(self, ban_type: str, target: str, uuid_str=None, xuid=None):
        try:
            if ban_type == "ip":
                self.plugin.server.ip_ban_list.remove_ban(target)
            else:
                uuid_obj = UUID(uuid_str) if uuid_str else None
                self.plugin.server.ban_list.remove_ban(target or "", uuid_obj, xuid)
        except ValueError:
            return False, "UUID 格式无效"
        except Exception as e:
            return False, f"解封失败: {e}"
        self.plugin._cross_push_bans()
        return True, f"已解封 {target or uuid_str or xuid or ''}"

    def _handle_api_tools_config(self):
        try:
            cfg = self.plugin._tools_public_config()
            cfg["backup_state"] = self.plugin._get_backup_state()
            cfg["backups"] = self.plugin._list_backups()
            self._send_json({"ok": True, "config": cfg})
        except Exception as e:
            self.plugin.logger.error(f"读取备份/云黑配置失败: {e}")
            self._send_json({"error": f"读取失败: {e}"}, 500)

    def _handle_api_diag(self):
        try:
            self._send_json({"ok": True, "diag": self.plugin._diag_summary()})
        except Exception as e:
            self.plugin.logger.error(f"读取诊断信息失败: {e}")
            self._send_json({"error": f"读取诊断信息失败: {e}"}, 500)

    def _handle_api_diag_stacks(self):
        try:
            stacks = self.plugin._dump_all_stacks()
            self.plugin.logger.info("[诊断] 手动触发线程栈转储:\n%s", stacks)
            self._send_json({"ok": True, "message": "已打印到服务端控制台", "len": len(stacks)})
        except Exception as e:
            self.plugin.logger.error(f"线程栈转储失败: {e}")
            self._send_json({"error": f"线程栈转储失败: {e}"}, 500)

    def _handle_api_diag_toggle(self, query):
        try:
            params = urllib.parse.parse_qs(query or "")
            key = (params.get("key") or [""])[0]
            v = (params.get("v") or ["1"])[0]
            if key not in ("enabled", "detailed"):
                self._send_json({"error": "无效的开关项"}, 400)
                return
            self.plugin._diag_cfg[key] = str(v).lower() in ("1", "true", "on", "yes")
            self.plugin._diag_save_cfg()
            self._send_json({"ok": True, "enabled": self.plugin._diag_on(), "detailed": self.plugin._diag_verbose()})
        except Exception as e:
            self.plugin.logger.error(f"切换诊断设置失败: {e}")
            self._send_json({"error": f"切换诊断设置失败: {e}"}, 500)

    def _cross_auth_ok(self, cross):
        try:
            pwd_cfg = str(cross.cfg.get("password", "") or "")
            if not pwd_cfg:
                return False
            provided = self.headers.get("X-Cross-Pass", "") or ""
            try:
                provided = urllib.parse.unquote(provided)
            except Exception:
                pass
            return provided == pwd_cfg
        except Exception:
            return False

    def _handle_cross_hub(self):
        try:
            cross = getattr(self.plugin, "cross", None)
            if cross is None or not cross.enabled():
                self._send_json({"error": "跨服未启用"}, 403)
                return
            if not self._cross_auth_ok(cross):
                self._send_json({"error": "password mismatch"}, 401)
                return
            data = self._read_post_data()
            action = str(data.get("action", "") or "")
            sender = str(data.get("server_name", "") or "")
            if cross.is_master():
                self._cross_hub_master(cross, action, sender, data)
            elif cross.is_slave():
                self._cross_hub_slave(cross, action, data)
            else:
                self._send_json({"ok": False, "error": "cross disabled"})
        except Exception as e:
            self.plugin.logger.error(f"[跨服] hub 处理异常: {e}")
            self._send_json({"error": f"hub error: {e}"}, 500)

    def _cross_broadcast_wl_to_peers(self, cross, wl, peers):
        for purl in peers.values():
            if purl:
                try:
                    cross._http_json("POST", purl.rstrip("/") + "/api/cross/hub", {"action": "wl_update", "names": dict(wl)})
                except Exception:
                    pass

    def _cross_broadcast_ban_to_peers(self, cross, bans, peers):
        for purl in peers.values():
            if purl:
                try:
                    cross._http_json("POST", purl.rstrip("/") + "/api/cross/hub", {"action": "ban_update", "entries": dict(bans)})
                except Exception:
                    pass

    def _cross_hub_master(self, cross, action, sender, data):
        if action == "register":
            sname = sender or str(data.get("server_name", "") or "")
            self_url = str(data.get("self_url", "") or "").strip()
            with cross.db_lock:
                peers = cross.db.setdefault("peers", {})
                if sname and sname != str(cross.cfg.get("server_name", "") or ""):
                    if self_url:
                        peers[sname] = self_url
                    else:
                        peers.pop(sname, None)
                cross._save_db()
                wl = dict(cross.db.get("whitelist", {}))
                bans = dict(cross.db.get("bans", {}))
                peerlist = dict(peers)
            self._send_json({"ok": True, "role": "master", "server_name": cross.cfg.get("server_name"), "whitelist": wl, "bans": bans, "peers": peerlist})
            return
        if action == "chat":
            try:
                origin = str(data.get("origin", "") or "server")
                cross._master_on_chat(sender or str(data.get("server", "") or ""), str(data.get("player", "") or ""), str(data.get("text", "") or ""), origin)
            except Exception as e:
                self.plugin.logger.warning(f"[跨服] 主服处理聊天失败: {e}")
            self._send_json({"ok": True})
            return
        if action == "sys_notice":
            try:
                cross._master_on_sys(str(data.get("text", "") or ""))
            except Exception as e:
                self.plugin.logger.warning(f"[跨服] 主服处理系统通知失败: {e}")
            self._send_json({"ok": True})
            return
        if action == "player_get":
            uuid = str(data.get("uuid", "") or "")
            rec = (cross.db.get("players") or {}).get(uuid)
            self._send_json({"ok": True, "data": rec or None})
            return
        if action == "player_put":
            uuid = str(data.get("uuid", "") or "")
            nm = str(data.get("name", "") or "")
            pd = data.get("data") or {}
            with cross.db_lock:
                cross.db.setdefault("players", {})[uuid] = {"name": nm, "data": pd, "ts": time.time()}
                cross._save_db()
            self._send_json({"ok": True})
            return
        if action in ("wl_set", "wl_merge"):
            names = data.get("names")
            if isinstance(names, dict):
                with cross.db_lock:
                    cross.db.setdefault("whitelist", {}).update(names)
                    cross._save_db()
                    wl = dict(cross.db["whitelist"])
                    peers = dict(cross.db.get("peers", {}))
                self._cross_broadcast_wl_to_peers(cross, wl, peers)
            self._send_json({"ok": True})
            return
        if action in ("ban_set", "ban_merge"):
            entries = data.get("entries")
            if isinstance(entries, dict):
                with cross.db_lock:
                    cross.db.setdefault("bans", {}).update(entries)
                    cross._save_db()
                    bans = dict(cross.db["bans"])
                    peers = dict(cross.db.get("peers", {}))
                self._cross_broadcast_ban_to_peers(cross, bans, peers)
            self._send_json({"ok": True})
            return
        if action == "ping":
            self._send_json({"ok": True, "role": "master", "server_name": cross.cfg.get("server_name")})
            return
        self._send_json({"ok": False, "error": "unknown master action"})

    def _cross_hub_slave(self, cross, action, data):
        if action == "relay":
            sname = str(data.get("server", "") or "")
            player = str(data.get("player", "") or "")
            text = str(data.get("text", "") or "")
            try:
                self.plugin._cross_broadcast(f"§b[{sname}] §f{player}: §7{text}")
            except Exception:
                pass
            self._send_json({"ok": True})
            return
        if action == "sys_relay":
            text = str(data.get("text", "") or "")
            if text:
                try:
                    self.plugin._cross_broadcast(text)
                except Exception:
                    pass
            self._send_json({"ok": True})
            return
        if action == "wl_update":
            names = data.get("names")
            if isinstance(names, (list, dict)):
                keylist = list(names.keys()) if isinstance(names, dict) else names
                try:
                    self.plugin._apply_cross_whitelist(keylist)
                except Exception:
                    pass
                with cross.db_lock:
                    if isinstance(names, dict):
                        cross.db["whitelist"] = dict(names)
                        cross._save_db()
            self._send_json({"ok": True})
            return
        if action == "ban_update":
            entries = data.get("entries")
            if isinstance(entries, dict):
                try:
                    self.plugin._apply_cross_bans(entries)
                except Exception:
                    pass
                with cross.db_lock:
                    cross.db["bans"] = dict(entries)
                    cross._save_db()
            self._send_json({"ok": True})
            return
        if action == "player_restore":
            self._send_json({"ok": True})
            return
        if action == "ping":
            self._send_json({"ok": True, "role": "slave", "server_name": cross.cfg.get("server_name")})
            return
        self._send_json({"ok": False, "error": "unknown slave action"})

    def _handle_cross_config(self):
        try:
            cross = getattr(self.plugin, "cross", None)
            cfg = dict(cross.cfg) if cross is not None else dict(DEFAULT_CROSS_CONFIG)
            self._send_json({"config": cfg})
        except Exception as e:
            self._send_json({"error": f"读取跨服配置失败: {e}"}, 500)

    def _handle_cross_status(self):
        try:
            cross = getattr(self.plugin, "cross", None)
            if cross is None:
                self._send_json({"ok": True, "role": "none", "enabled": False, "started": False})
                return
            peers = dict(cross.db.get("peers", {})) if hasattr(cross, "db") else {}
            self._send_json({
                "ok": True,
                "role": cross.role(),
                "enabled": cross.enabled(),
                "started": cross.started,
                "server_name": cross.cfg.get("server_name", ""),
                "master_url": cross.cfg.get("master_url", ""),
                "last_ping": cross.last_ping,
                "last_error": cross.last_error,
                "peers": peers,
                "players": len((cross.db.get("players") or {})) if hasattr(cross, "db") else 0,
            })
        except Exception as e:
            self._send_json({"error": f"读取跨服状态失败: {e}"}, 500)

    def _handle_cross_config_save(self, data):
        try:
            cross = getattr(self.plugin, "cross", None)
            if cross is None:
                self._send_json({"error": "跨服模块不可用"}, 500)
                return
            with cross.lock:
                old_role = cross.role()
                cfg = cross.cfg
                cfg["enabled"] = bool(data.get("enabled", False))
                role = str(data.get("role", "none") or "none")
                if role not in ("none", "master", "slave"):
                    role = "none"
                cfg["role"] = role
                cfg["server_name"] = str(data.get("server_name", cfg.get("server_name", "本服")) or "").strip() or cfg.get("server_name", "本服")
                cfg["password"] = str(data.get("password", cfg.get("password", "")) or "")
                cfg["master_url"] = str(data.get("master_url", cfg.get("master_url", "")) or "").strip()
                cfg["self_url"] = str(data.get("self_url", cfg.get("self_url", "")) or "").strip()
                try:
                    cfg["sync_seconds"] = int(float(data.get("sync_seconds", cfg.get("sync_seconds", 8)) or 8))
                except Exception:
                    pass
                interop = dict(cfg.get("interop", {}))
                received = data.get("interop")
                if isinstance(received, dict):
                    for k in ("chat", "player_data", "whitelist", "ban"):
                        interop[k] = bool(received.get(k, interop.get(k, True)))
                cfg["interop"] = interop
                cross.save()
                cross.stop()
                cross.start()
            self._send_json({"ok": True})
        except Exception as e:
            self.plugin.logger.error(f"[跨服] 保存配置失败: {e}")
            self._send_json({"error": f"保存配置失败: {e}"}, 500)

    def _handle_cross_test(self, data):
        try:
            cross = getattr(self.plugin, "cross", None)
            if cross is None:
                self._send_json({"error": "跨服模块不可用"}, 500)
                return
            if cross.is_master():
                self._send_json({"ok": True, "message": "当前为主服，无需测试连接"})
                return
            base = cross.master_base()
            if not base:
                self._send_json({"error": "未填写主服地址"}, 400)
                return
            if not str(cross.cfg.get("password", "") or "").strip():
                self._send_json({"error": "未设置连接密码"}, 400)
                return
            r = cross._hub(base, {"action": "ping", "server_name": cross.cfg.get("server_name")})
            if r is None:
                self._send_json({"error": f"连接失败: {cross.last_error or '无响应'}"})
                return
            if not r.get("ok"):
                self._send_json({"error": r.get("error") or "连接失败"})
                return
            cross.last_ping = time.time()
            cross.last_error = ""
            self._send_json({"ok": True, "message": f"连接成功！已连接主服（{cross.cfg.get('server_name')}）"})
        except Exception as e:
            self._send_json({"error": f"测试失败: {e}"}, 500)

    _ALL_SCOPES = ["players", "logs", "gamerules", "bans", "whitelist", "worlds", "perm",
                   "scoreboard", "detail", "console", "qqbot", "cross", "backup", "cloud",
                   "gametools", "mods", "diag", "files"]

    def _scope_label(self, s: str) -> str:
        labels = {
            "players": "在线玩家", "logs": "日志中心", "gamerules": "游戏规则", "bans": "封禁管理",
            "whitelist": "白名单审核", "worlds": "多存档", "perm": "权限管理", "scoreboard": "记分板",
            "detail": "玩家详情", "console": "操作控制台", "qqbot": "QQ机器人", "cross": "跨服互联",
            "backup": "自动备份", "cloud": "云黑名单", "gametools": "签到绑定", "mods": "模组管理",
            "diag": "诊断", "files": "文件管理",
        }
        return labels.get(s, s)

    def _handle_api_auth_me(self):
        role = getattr(self, "_auth_role", "admin")
        scopes = getattr(self, "_auth_scopes", None)
        cur_user = getattr(self, "_auth_user", "")
        if role == "admin":
            scopes = ["*"]
        else:
            scopes = sorted(set(scopes or []) & set(self._ALL_SCOPES))
        self._send_json({"ok": True, "role": role, "user": cur_user, "scopes": scopes,
                         "all_scopes": self._ALL_SCOPES})

    def _handle_api_account_info(self):
        cfg = getattr(self.plugin, "web_config", {}) or {}
        self._send_json({"ok": True,
                         "admin_user": str(cfg.get("admin_user", "") or ""),
                         "admin_configured": bool(str(cfg.get("admin_user", "") or "") and
                                                  str(cfg.get("admin_pass", "") or "")),
                         "test_mode": bool(cfg.get("test_mode", True)),
                         "all_scopes": self._ALL_SCOPES})

    def _handle_api_account_test_mode(self, data):
        try:
            value = bool((data or {}).get("test_mode", True))
            cfg = dict(getattr(self.plugin, "web_config", {}) or {})
            cfg["test_mode"] = value
            self.plugin.web_config = cfg
            self.plugin._persist_config(cfg)
            self._send_json({"ok": True, "test_mode": value})
        except Exception as e:
            self._send_json({"ok": False, "error": f"保存测试模式失败: {e}"}, 500)

    def _handle_api_account_password(self, data):
        cfg = dict(getattr(self.plugin, "web_config", {}) or {})
        new_user = str((data or {}).get("admin_user", "") or "").strip()
        new_pass = str((data or {}).get("admin_pass", "") or "").strip()
        if not new_user or not new_pass:
            self._send_json({"error": "用户名与密码不能为空"}, 400)
            return
        if len(new_pass) < 4:
            self._send_json({"error": "密码长度至少 4 位"}, 400)
            return
        cfg["admin_user"] = new_user
        cfg["admin_pass"] = new_pass
        self.plugin._persist_config(cfg)
        self.plugin._log_admin_action(f"已修改面板登录账号")
        self._send_json({"ok": True, "message": "账号/密码已更新"})

    def _handle_api_account_temp_create(self, data):
        scope_keys = set((data or {}).get("scopes") or []) & set(self._ALL_SCOPES)
        try:
            expires = int(float((data or {}).get("expires", 0)) or 0)
        except Exception:
            expires = 0
        if not scope_keys:
            self._send_json({"error": "请至少选择一个可访问板块"}, 400)
            return
        cfg = dict(getattr(self.plugin, "web_config", {}) or {})
        temp_accounts = cfg.get("temp_accounts") or []
        if not isinstance(temp_accounts, list):
            temp_accounts = []
        username = "tmp" + secrets.token_hex(3)
        password = secrets.token_urlsafe(9)
        tid = secrets.token_hex(6)
        exp = int(time.time()) + expires if expires and expires > 0 else 0
        temp_accounts.append({
            "id": tid,
            "username": username,
            "password": password,
            "expires": exp,
            "scopes": sorted(scope_keys),
            "created_at": int(time.time()),
        })
        cfg["temp_accounts"] = temp_accounts
        self.plugin._persist_config(cfg)
        self.plugin._log_admin_action("已创建临时账户")
        self._send_json({"ok": True, "id": tid, "username": username, "password": password,
                         "expires": exp, "scopes": sorted(scope_keys)})

    def _handle_api_account_temp_revoke(self, data):
        tid = str((data or {}).get("id", "") or "").strip()
        if not tid:
            self._send_json({"error": "缺少账户 id"}, 400)
            return
        cfg = dict(getattr(self.plugin, "web_config", {}) or {})
        temp_accounts = cfg.get("temp_accounts") or []
        if not isinstance(temp_accounts, list):
            temp_accounts = []
        new_list = [t for t in temp_accounts if not (isinstance(t, dict) and str(t.get("id", "")) == tid)]
        cfg["temp_accounts"] = new_list
        self.plugin._persist_config(cfg)
        self.plugin._log_admin_action("已撤销临时账户")
        self._send_json({"ok": True, "message": "临时账户已撤销"})

    def _handle_api_account_temps(self):
        cfg = getattr(self.plugin, "web_config", {}) or {}
        temp_accounts = cfg.get("temp_accounts") or []
        out = []
        now = int(time.time())
        for t in temp_accounts:
            if not isinstance(t, dict):
                continue
            exp = int(t.get("expires") or 0) or 0
            out.append({
                "id": str(t.get("id", "")),
                "username": str(t.get("username", "")),
                "expires": exp,
                "expired": bool(exp and now > exp),
                "scopes": sorted(set(t.get("scopes") or []) & set(self._ALL_SCOPES)),
            })
        self._send_json({"ok": True, "accounts": out})

    def _handle_api_security_info(self):
        sw = dict((getattr(self.plugin, "web_config", {}) or {}).get("security_whitelist") or {})
        self._send_json({"ok": True, "enabled": bool(sw.get("enabled")), "ips": list(sw.get("ips") or [])})

    def _handle_api_security_save(self, data):
        enabled = bool((data or {}).get("enabled"))
        raw = str((data or {}).get("ips", "") or "")
        ips = []
        for item in raw.replace("，", ",").split(","):
            item = item.strip()
            if item:
                ips.append(item)
        cfg = dict(getattr(self.plugin, "web_config", {}) or {})
        dirty = dict(cfg.get("security_whitelist") or {})
        dirty["enabled"] = enabled
        dirty["ips"] = ips
        cfg["security_whitelist"] = dirty
        self.plugin._persist_config(cfg)
        self.plugin._log_admin_action("已更新安全系统 IP 白名单")
        self._send_json({"ok": True, "message": "安全白名单已保存"})

    def _execute_console_command(self, command: str):
        try:
            self.plugin.logger.info(f"[控制台] 执行指令: {command}")
            self.plugin.server.dispatch_command(self.plugin.server.command_sender, command)
        except Exception as e:
            self.plugin.logger.error(f"执行控制台指令失败: {e}")

    def _handle_api_console_command(self, data):
        command = str((data or {}).get("command", "") or "").strip()
        if not command:
            self._send_json({"error": "指令不能为空"}, 400)
            return
        if len(command) > 512:
            self._send_json({"error": "指令过长"}, 400)
            return
        self.plugin._run_async_on_server_thread(self._execute_console_command, command)
        self.plugin._log_admin_action(f"向控制台发送指令: {command}")
        self._send_json({"ok": True, "success": True, "message": f"已发送指令: {command}"})

    def _handle_api_tools_config_save(self, data):
        try:
            ok, msg = self.plugin._save_tools_config(data)
            self._send_json({"ok": ok, "message": msg}) if ok else self._send_json({"error": msg})
        except Exception as e:
            self._send_json({"error": f"保存失败: {e}"}, 500)

    def _handle_api_backup_trigger(self, data):
        try:
            ok = self.plugin._trigger_backup()
            self._send_json({"ok": ok, "message": "已在后台启动备份" if ok else "备份任务正在运行中"})
        except Exception as e:
            self._send_json({"error": f"启动备份失败: {e}"}, 500)

    def _handle_api_backup_delete(self, data):
        try:
            name = (data or {}).get("name")
            ok, msg = self.plugin._delete_backup(name)
            self._send_json({"ok": ok, "message": msg} if ok else {"error": msg})
        except Exception as e:
            self._send_json({"error": f"删除失败: {e}"}, 500)

    def _handle_api_backup_restore(self, data):
        name = (data or {}).get("name")
        if not name:
            self._send_json({"error": "缺少备份文件名"}, 400)
            return

        try:
            ok, msg = self.plugin._restore_backup(str(name))
            if ok:
                self.plugin.logger.info(f"[备份] 已复制备份 {name} 到服务器根目录")
                self._send_json({"ok": True, "message": msg, "restart_required": True})
            else:
                self._send_json({"ok": False, "error": msg})
        except Exception as e:
            self.plugin.logger.error(f"提交恢复任务失败: {e}")
            self._send_json({"error": f"提交恢复任务失败: {e}"}, 500)

    def _handle_api_cloud_test(self, data):
        try:
            ok, msg, res = self.plugin._cloud_test()
            self._send_json({"ok": ok, "message": msg, "data": res})
        except Exception as e:
            self._send_json({"error": f"测试失败: {e}"}, 500)

    def _handle_api_gametools(self):
        try:
            pl = self.plugin
            cfg = pl._load_tools_config()
            binding = cfg.get("binding", {}) or {}
            signin = cfg.get("signin", {}) or {}
            bind_list = []
            bindings = pl._load_bindings()
            for mc, entry in (bindings.get("by_mc", {}) or {}).items():
                if isinstance(entry, dict):
                    bind_list.append({
                        "mc": entry.get("mc", mc),
                        "qq": entry.get("qq", ""),
                        "bound_at": entry.get("bound_at", ""),
                    })
            self._send_json({
                "ok": True,
                "binding": binding,
                "signin": signin,
                "delayed": pl._delayed_stats(),
                "bindings": sorted(bind_list, key=lambda x: str(x.get("bound_at", "")), reverse=True),
                "signin_records": pl._load_signin().get("last", {}),
            })
        except Exception as e:
            self.plugin.logger.error(f"GET /api/gametools 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_gametools_save(self, data):
        try:
            ok, msg = self.plugin._save_tools_config(data or {})
            self._send_json({"ok": bool(ok), "message": msg})
        except Exception as e:
            self.plugin.logger.error(f"POST /api/gametools/save 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_mods(self):
        try:
            pl = self.plugin
            worlds, current_world, _root = pl._list_worlds()
            self._send_json({
                "ok": True,
                "uploaded": pl._list_mod_uploaded(),
                "installed": pl._scan_installed_packs(),
                "worlds": worlds,
                "current_world": current_world,
            })
        except Exception as e:
            self.plugin.logger.error(f"GET /api/mods 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_mod_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._send_json({"ok": False, "error": "必须使用 multipart/form-data"}, 400)
            return
        boundary_match = re.search(r'boundary=([^;\s]+)', content_type)
        if not boundary_match:
            self._send_json({"ok": False, "error": "无效的 multipart 格式"}, 400)
            return
        boundary = boundary_match.group(1).encode('ascii')
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json({"ok": False, "error": "请求体过大或非法"}, 400)
            return
        body = self.rfile.read(length)
        parts = body.split(b'--' + boundary)
        fname = None
        fdata = None
        for part in parts:
            if not part or part in (b'--\r\n', b'--'):
                continue
            h_end = part.find(b'\r\n\r\n')
            if h_end == -1:
                continue
            header = part[:h_end].decode('utf-8', errors='ignore')
            content = part[h_end + 4:]
            if content.endswith(b'\r\n'):
                content = content[:-2]
            if 'filename=' in header:
                fm = re.search(r'filename="([^"]*)"', header)
                fname = fm.group(1) if fm else 'upload'
                if 'name="file"' in header or 'filename=' in header:
                    fdata = content
        if not fname or fdata is None:
            self._send_json({"ok": False, "error": "未收到文件"}, 400)
            return
        base = os.path.basename(str(fname).replace('\\', '/'))
        if not (base.endswith('.mcaddon') or base.endswith('.mcpack')):
            self._send_json({"ok": False, "error": "只支持 .mcaddon / .mcpack 文件"}, 400)
            return
        try:
            uname = _uuid.uuid4().hex[:12] + "_" + base
            dest = self.plugin._mods_dir() / uname
            dest.write_bytes(fdata)
            self.plugin._register_mod_file(uname, len(fdata))
            self._send_json({"ok": True, "message": f"已上传 {base}（{_fmt_size(len(fdata))}）", "file": uname})
        except Exception as e:
            self.plugin.logger.error(f"模组上传失败: {e}")
            self._send_json({"ok": False, "error": f"上传失败: {e}"}, 500)

    def _handle_api_mod_install(self, data):
        fname = (data or {}).get("file", "")
        target_type = (data or {}).get("target", "global")
        world_name = (data or {}).get("world", "")
        if not fname:
            self._send_json({"ok": False, "error": "缺少文件名"}, 400)
            return
        pl = self.plugin
        src = pl._mods_dir() / os.path.basename(str(fname))
        if not src.is_file():
            self._send_json({"ok": False, "error": "上传的文件已不存在"}, 400)
            return
        def _worker():
            ok, msg = pl._install_mod_pack(src, target_type, world_name or None)
            pl.logger.info(f"[模组] 安装{'成功' if ok else '失败'}: {msg}")
        try:
            pl._executor.submit(_worker)
        except Exception as e:
            self._send_json({"ok": False, "error": f"提交安装失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "正在后台安装，完成后自动更新清单（一般数秒）"})

    def _handle_api_mod_delete(self, data):
        fname = (data or {}).get("file", "")
        if not fname:
            self._send_json({"ok": False, "error": "缺少文件名"}, 400)
            return
        if self.plugin._delete_mod_file(fname):
            self._send_json({"ok": True, "message": "已删除"})
        else:
            self._send_json({"ok": False, "error": "删除失败"}, 400)

    _FILE_READ_LIMIT = 1024 * 1024
    _FILES_UPLOAD_LIMIT = 100 * 1024 * 1024

    _GMOD_UPLOAD_LIMIT = 64 * 1024 * 1024
    _GMOD_EXTS = (".gmmod", ".gmlib")

    # ---------------------------------------------------------------- 子插件

    def _gmod_loader(self):
        ld = getattr(self.plugin, "mod_loader", None)
        if ld is None:
            self._send_json({"ok": False, "error": "子插件加载器未初始化"}, 500)
            return None
        return ld

    def _gmod_admin_only(self) -> bool:
        """子插件可携带任意代码，只允许管理员操作，临时账号一律拒绝。"""
        if getattr(self, "_auth_role", "admin") != "admin":
            self._send_json({"ok": False, "error": "仅管理员可管理子插件"}, 403)
            return False
        return True

    def _gmod_upload_dir(self) -> Path:
        d = self.plugin.mods_dir / ".upload"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------ 页面接管

    def _serve_gmod_page(self, ptype: str) -> bool:
        """把 ``/`` 或 ``/admin`` 交给启用的子插件。接管成功返回 True。

        注意：这里刻意直接读文件、不走 ``_serve_html`，因为子插件页面可能引用
        同目录下的相对资源（如 ``assets/app.js``），而浏览器会以当前路径为基准
        解析这些相对地址（``/assets/app.js``），需要由静态路由兜住。
        """
        ld = getattr(self.plugin, "mod_loader", None)
        if ld is None:
            return False
        try:
            got = ld.active_page(ptype)
        except Exception as e:
            self.plugin.logger.warning(f"[子插件] 读取 {ptype} 页面接管状态失败: {e}")
            return False
        if not got:
            return False
        root, entry = got
        top = str(entry or "index.html").replace("\\", "/").split("/")[0]
        if str(entry).replace("\\", "/").strip("/") not in ("index.html", "index.htm"):
            self.plugin.logger.warning(
                f"[子插件] {ptype} 子插件 {root.name} 的入口为 {entry}，"
                f"页面内相对资源请改用 /gmpage/{ptype}/ 前缀引用"
            )
        return self._gmod_send_file(ld.page_file(ptype, entry))

    def _handle_gmod_static(self, path: str) -> None:
        """``/gmpage/<type>/<相对路径>``：按类型找启用子插件，或回退到同名目录的包。"""
        ld = getattr(self.plugin, "mod_loader", None)
        if ld is None:
            self.send_error(404, "Not Found")
            return
        rest = path[len('/gmpage/'):] if path.startswith('/gmpage/') else ''
        parts = rest.split('/', 1)
        ptype = parts[0] if parts and parts[0] else ''
        rel = parts[1] if len(parts) > 1 else ''

        if ptype not in ('index', 'admin'):
            self.send_error(404, "Not Found")
            return

        try:
            if rel:
                target = ld.page_file(ptype, rel)
            else:
                got = ld.active_page(ptype)
                target = ld.page_file(ptype, got[1]) if got else None
            if target is None:
                # 回退：按包名定位（未启用时也能预览该包自己的资源）
                segs = [s for s in rel.split('/') if s not in ('', '.')]
                if segs and '..' not in segs:
                    cand = ld._resolve_in(ld.mods_dir / ptype / segs[0], '/'.join(segs[1:]) or 'index.html')
                    if cand is not None and cand.is_file():
                        target = cand
        except Exception as e:
            self.plugin.logger.warning(f"[子插件] 静态资源解析失败 {path}: {e}")
            target = None

        if target is None:
            self._gmod_asset_fallback(ptype, rel)
            return
        self._gmod_send_file(target)

    def _gmod_asset_served(self, path: str) -> bool:
        """把根路径上的资源请求，尝试映射到当前启用的 page 型子插件目录。

        子插件页面里写 ``<script src="assets/app.js">`` 时，浏览器会按当前路径
        （``/`` → ``/assets/app.js``，``/admin`` → ``/assets/app.js``）去取，
        与子插件目录对不上。这里按 ``index`` 优先、``admin`` 次之的顺序兜住。
        找到并发出返回 True。
        """
        ld = getattr(self.plugin, "mod_loader", None)
        if ld is None:
            return False
        rel = path.lstrip('/')
        if not rel:
            return False
        for ptype in ('index', 'admin'):
            got = ld.active_page(ptype)
            if not got:
                continue
            root = got[0]
            try:
                cand = ld._resolve_in(root, rel)
            except Exception:
                cand = None
            if cand is not None and cand.is_file():
                self._gmod_send_file(cand)
                return True
        return False

    def _gmod_asset_fallback(self, ptype: str, rel: str) -> None:
        """子插件页面里 ``assets/app.js`` 这类相对引用会被浏览器解析成 ``/assets/app.js``。

        这里做一次兜底：若当前有启用的子插件，就把它目录下同名文件送出去。
        这保证了「子插件作者不需要改任何写法，附件资源全都能用」。
        """
        ld = getattr(self.plugin, "mod_loader", None)
        if ld is None or not rel:
            return
        segs = [s for s in str(rel).replace("\\", "/").split("/") if s not in ("", ".")]
        if not segs or ".." in segs:
            self.send_error(404, "Not Found")
            return

        roots = []
        got = ld.active_page(ptype)
        if got:
            roots.append(got[0])
        roots.append(ld.mods_dir / ptype / segs[0])

        for root in roots:
            try:
                cand = ld._resolve_in(root, "/".join(segs[1:]) if root.name == segs[0] else rel)
            except Exception:
                cand = None
            if cand is not None and cand.is_file():
                self._gmod_send_file(cand)
                return
        self.send_error(404, "Not Found")

    def _gmod_send_file(self, filepath, prefix: str = "") -> bool:
        """流式发送子插件内的文件，按扩展名给 MIME，支持 Range。"""
        if filepath is None:
            return False
        try:
            path = Path(filepath)
            size = path.stat().st_size
        except Exception:
            return False

        ctype = _gmod_mime_for(path)
        # HTML 里的相对引用以请求目录为基准，把规范前缀注入 <base> 只能改内容，
        # 这里保持原样送出，由 /gmpage/<type>/ 静态路由兜住。
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range", "") if hasattr(self, "headers") else ""
        if range_header.startswith("bytes="):
            try:
                spec = range_header[len("bytes="):].split(",")[0].strip()
                s_raw, _, e_raw = spec.partition("-")
                if s_raw:
                    start = int(s_raw)
                    end = int(e_raw) if e_raw else size - 1
                else:
                    start = max(0, size - int(e_raw or 0))
                if start > end or start >= size:
                    raise ValueError("bad range")
                end = min(end, size - 1)
                status = 206
            except Exception:
                start, end, status = 0, size - 1, 200

        length = max(0, end - start + 1)
        try:
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(length))
            self.send_header('Accept-Ranges', 'bytes')
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            if ctype.startswith('text/html'):
                self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            else:
                self.send_header('Cache-Control', 'no-cache')
            self.end_headers()

            with open(path, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            return True
        except Exception as e:
            self.plugin.logger.error(f"[子插件] 发送文件失败 {filepath}: {e}")
            return False

    def _handle_api_gmods_list(self):
        ld = self._gmod_loader()
        if ld is None:
            return
        try:
            loaded = set(ld.loaded_uuids())
            mods = []
            for m in ld.list_mods():
                item = dict(m)
                item["loaded"] = str(m.get("uuid")) in loaded
                mods.append(item)
            self._send_json({
                "ok": True,
                "mods": mods,
                "libs": ld.load_liblist(),
                "mod_dir": str(ld.mods_dir),
                "libs_dir": str(ld.libs_dir),
            })
        except Exception as e:
            self._send_json({"ok": False, "error": f"读取子插件失败: {e}"}, 500)

    def _handle_api_gmods_upload(self):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self._send_json({"ok": False, "error": "必须使用 multipart/form-data"}, 400)
            return
        bm = re.search(r"boundary=([^;\s]+)", ctype)
        if not bm:
            self._send_json({"ok": False, "error": "无效的 multipart 格式"}, 400)
            return
        boundary = bm.group(1).encode("ascii")
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > self._GMOD_UPLOAD_LIMIT:
            self._send_json({"ok": False, "error": "请求体为空或超过 64MB"}, 400)
            return
        body = self.rfile.read(length)

        fname = None
        fdata = None
        for part in body.split(b"--" + boundary):
            if not part or part in (b"--\r\n", b"--"):
                continue
            h_end = part.find(b"\r\n\r\n")
            if h_end == -1:
                continue
            header = part[:h_end].decode("utf-8", errors="ignore")
            content = part[h_end + 4:]
            if content.endswith(b"\r\n"):
                content = content[:-2]
            if "filename=" in header:
                fm = re.search(r'filename="([^"]*)"', header)
                fname = fm.group(1) if fm else "upload"
                fdata = content
        if not fname or fdata is None:
            self._send_json({"ok": False, "error": "未收到文件"}, 400)
            return

        base = os.path.basename(str(fname).replace("\\", "/"))
        ext = os.path.splitext(base)[1].lower()
        if ext not in self._GMOD_EXTS:
            self._send_json({"ok": False, "error": "只支持 .gmmod / .gmlib 文件"}, 400)
            return

        tmp = self._gmod_upload_dir() / (_uuid.uuid4().hex[:12] + "_" + base)
        try:
            tmp.write_bytes(fdata)
            result = ld.install(str(tmp), force=True)
        except Exception as e:
            self.plugin.logger.error(f"[子插件] 导入异常: {e}")
            self._send_json({"ok": False, "error": f"导入异常: {e}"}, 500)
            return
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

        if not result.get("ok"):
            self._send_json(result, 400)
            return
        if result.get("kind") == "gmlib":
            msg = f"依赖库 {result.get('name')} 导入完成，新增 {len(result.get('copied') or [])} 个包"
        else:
            msg = f"子插件 {result.get('name')} v{result.get('version')} 导入成功"
            if result.get("missing_libs"):
                msg += f"（缺少依赖：{', '.join(result['missing_libs'])}）"
        result["message"] = msg
        self._send_json(result)

    def _handle_api_gmods_load(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        uuid = str((data or {}).get("uuid", "") or "")
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        try:
            got = ld.import_mod(uuid)
        except Exception as e:
            self._send_json({"ok": False, "error": f"加载失败: {e}"}, 500)
            return
        if got is None:
            self._send_json({"ok": False, "error": "加载失败，详见服务端日志"}, 400)
            return
        module, setup = got
        has_setup = callable(setup)
        self._send_json({
            "ok": True,
            "message": "入口模块已加载" + ("，发现 setup(plugin) 入口" if has_setup else "，未发现 setup 入口"),
            "module": getattr(module, "__name__", ""),
            "has_setup": has_setup,
        })

    def _handle_api_gmods_unload(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        uuid = str((data or {}).get("uuid", "") or "")
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        self._send_json({"ok": ld.unload_mod(uuid), "message": "已从内存卸载"})

    def _handle_api_gmods_uninstall(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        uuid = str((data or {}).get("uuid", "") or "")
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        try:
            ok = ld.uninstall(uuid)
        except Exception as e:
            self._send_json({"ok": False, "error": f"卸载失败: {e}"}, 400)
            return
        if not ok:
            self._send_json({"ok": False, "error": "子插件不存在"}, 404)
            return
        self._send_json({"ok": True, "message": "已卸载并删除文件"})

    def _handle_api_gmods_enable(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        uuid = str((data or {}).get("uuid", "") or "")
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        try:
            r = ld.enable(uuid)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        self._send_json(r)

    def _handle_api_gmods_disable(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        uuid = str((data or {}).get("uuid", "") or "")
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        try:
            r = ld.disable(uuid)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        self._send_json(r)

    def _handle_api_gmods_files(self, query):
        ld = self._gmod_loader()
        if ld is None:
            return
        qs = urllib.parse.parse_qs(query or "")
        uuid = (qs.get("uuid") or [""])[0]
        if not uuid:
            self._send_json({"ok": False, "error": "缺少 uuid"}, 400)
            return
        rec = ld.installed(uuid)
        if not rec:
            self._send_json({"ok": False, "error": "子插件不存在"}, 404)
            return
        files = ld.list_page_files(uuid)

        wanted = (qs.get("file") or [""])[0]
        if not wanted:
            wanted = str(rec.get("index") or "index.html")
        content = ld.read_page_text(uuid, wanted)
        self._send_json({
            "ok": True, "uuid": uuid, "files": files,
            "file": wanted, "content": content,
            "editable": content is not None,
            "type": rec.get("type"),
            "root": str(rec.get("path") or ""),
        })

    def _handle_api_gmods_file_save(self, data):
        if not self._gmod_admin_only():
            return
        ld = self._gmod_loader()
        if ld is None:
            return
        d = data or {}
        uuid = str(d.get("uuid", "") or "")
        rel = str(d.get("file", "") or "")
        if not uuid or not rel:
            self._send_json({"ok": False, "error": "缺少 uuid 或 file"}, 400)
            return
        if not ld.write_page_text(uuid, rel, str(d.get("content", ""))):
            self._send_json({"ok": False, "error": "写入失败（文件类型不允许或路径非法）"}, 400)
            return
        self._send_json({"ok": True, "message": f"已保存 {rel}"})

    def _handle_api_gmods_pages(self):
        """返回当前生效的页面接管状态，供前端展示。"""
        ld = self._gmod_loader()
        if ld is None:
            return
        out = {}
        for ptype in ("index", "admin"):
            try:
                got = ld.active_page(ptype)
            except Exception:
                got = None
            out[ptype] = {"uuid": ld.enabled_uuid(ptype), "active": bool(got)}
        self._send_json({"ok": True, "pages": out})

    def _fs_root(self) -> Path:
        return (self.plugin._find_server_root() or Path.cwd()).resolve()

    def _fs_basename(self, raw) -> str:
        base = os.path.basename(str(raw or "").replace("\\", "/")).strip()
        if not base or base in (".", ".."):
            return ""
        return base

    def _fs_abs(self, raw, need=True, want="any"):
        root = self._fs_root()
        rel = str(raw or "").replace("\\", "/").strip().lstrip("/")
        parts = [s for s in rel.split("/") if s not in ("", ".")]
        for s in parts:
            if s == "..":
                return None, "路径不合法（不允许 ..）"
        target = root
        for s in parts:
            target = target / s
        try:
            target = target.resolve()
        except Exception:
            target = target.absolute()
        rs = str(root)
        ts = str(target)
        if ts != rs and not ts.startswith(rs + os.sep):
            return None, "路径超出服务器根目录"
        if need:
            if not target.exists():
                return None, "路径不存在：%s" % (raw or "")
            if want == "dir" and not target.is_dir():
                return None, "目标不是目录：%s" % (raw or "")
            if want == "file" and not target.is_file():
                return None, "目标不是文件：%s" % (raw or "")
        return target, None

    def _fs_rel(self, target: Path) -> str:
        try:
            r = target.relative_to(self._fs_root())
            if str(r) == ".":
                return ""
            return r.as_posix()
        except Exception:
            return ""

    def _handle_api_files_list(self, query):
        qs = urllib.parse.parse_qs(query)
        rel = (qs.get("path") or [""])[0]
        target, err = self._fs_abs(rel, need=True, want="dir")
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        try:
            items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            self._send_json({"ok": False, "error": "没有读取该目录的权限"}, 403)
            return
        entries = []
        for p in items:
            try:
                st = p.stat()
            except Exception:
                st = None
            entries.append({
                "name": p.name,
                "path": self._fs_rel(p),
                "is_dir": p.is_dir(),
                "size": st.st_size if st else 0,
                "mtime": int(st.st_mtime) if st else 0,
            })
        self._send_json({"ok": True, "cwd": self._fs_rel(target), "entries": entries})

    def _handle_api_files_read(self, query):
        qs = urllib.parse.parse_qs(query)
        rel = (qs.get("path") or [""])[0]
        if not rel:
            self._send_json({"ok": False, "error": "缺少文件路径"}, 400)
            return
        target, err = self._fs_abs(rel, need=True, want="file")
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        try:
            size = target.stat().st_size
        except Exception as e:
            self._send_json({"ok": False, "error": f"读取状态失败: {e}"}, 500)
            return
        if size > self._FILE_READ_LIMIT:
            self._send_json({"ok": False, "error": "文件过大，请下载后编辑", "too_large": True, "size": size})
            return
        try:
            data = target.read_bytes()
        except Exception as e:
            self._send_json({"ok": False, "error": f"读取失败: {e}"}, 500)
            return
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            self._send_json({"ok": False, "error": "非 UTF-8 文本或二进制文件，无法在线编辑，请下载后处理", "binary": True})
            return
        self._send_json({"ok": True, "content": content, "name": target.name, "path": rel, "size": size})

    def _handle_api_files_download(self, query):
        qs = urllib.parse.parse_qs(query)
        rel = (qs.get("path") or [""])[0]
        if not rel:
            self._send_json({"ok": False, "error": "缺少文件路径"}, 400)
            return
        target, err = self._fs_abs(rel, need=True, want="file")
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        try:
            data = target.read_bytes()
        except Exception as e:
            self._send_json({"ok": False, "error": f"读取失败: {e}"}, 500)
            return
        name = target.name
        ctype = "application/octet-stream"
        if name.endswith((".txt", ".log", ".properties", ".json", ".md", ".yml", ".yaml", ".cfg", ".ini")):
            ctype = "text/plain; charset=utf-8"
        try:
            cd = "attachment; filename*=UTF-8''" + urllib.parse.quote(name)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", cd)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            pass

    def _handle_api_files_mkdir(self, data):
        rel = str((data or {}).get("path", "") or "")
        if not rel:
            self._send_json({"ok": False, "error": "缺少目录路径"}, 400)
            return
        target, err = self._fs_abs(rel, need=False)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if target.exists():
            self._send_json({"ok": False, "error": "已存在同名项目"}, 400)
            return
        try:
            target.mkdir(parents=False)
        except Exception as e:
            self._send_json({"ok": False, "error": f"新建失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已创建 " + target.name})

    def _handle_api_files_delete(self, data):
        rel = str((data or {}).get("path", "") or "")
        if not rel:
            self._send_json({"ok": False, "error": "缺少路径"}, 400)
            return
        target, err = self._fs_abs(rel, need=True)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if str(target) == str(self._fs_root()):
            self._send_json({"ok": False, "error": "不能删除服务器根目录"}, 400)
            return
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except Exception as e:
            self._send_json({"ok": False, "error": f"删除失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已删除 " + target.name})

    def _handle_api_files_rename(self, data):
        rel = str((data or {}).get("path", "") or "")
        newname = self._fs_basename((data or {}).get("newname", "") or "")
        if not rel or not newname:
            self._send_json({"ok": False, "error": "缺少路径或新名称"}, 400)
            return
        target, err = self._fs_abs(rel, need=True)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if str(target) == str(self._fs_root()):
            self._send_json({"ok": False, "error": "不能重命名服务器根目录"}, 400)
            return
        newp = target.parent / newname
        try:
            newp = newp.resolve()
        except Exception:
            newp = newp.absolute()
        rs = str(self._fs_root())
        if str(newp) != rs and not str(newp).startswith(rs + os.sep):
            self._send_json({"ok": False, "error": "目标超出根目录"}, 400)
            return
        if newp.exists():
            self._send_json({"ok": False, "error": "目标已存在：" + newname}, 400)
            return
        try:
            os.rename(str(target), str(newp))
        except Exception as e:
            self._send_json({"ok": False, "error": f"重命名失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已重命名"})

    def _handle_api_files_move(self, data):
        src = str((data or {}).get("src", "") or "")
        dst = str((data or {}).get("dst", "") or "")
        if not src or not dst:
            self._send_json({"ok": False, "error": "缺少源或目标路径"}, 400)
            return
        s, err = self._fs_abs(src, need=True)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if str(s) == str(self._fs_root()):
            self._send_json({"ok": False, "error": "不能移动服务器根目录"}, 400)
            return
        d, err = self._fs_abs(dst, need=False)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if d.exists() and d.is_dir():
            d = d / s.name
        if str(d.resolve()) == str(s):
            self._send_json({"ok": False, "error": "源与目标相同"}, 400)
            return
        if d.exists():
            self._send_json({"ok": False, "error": "目标已存在：" + d.name}, 400)
            return
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(s), str(d))
        except Exception as e:
            self._send_json({"ok": False, "error": f"移动失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已移动"})

    def _handle_api_files_copy(self, data):
        src = str((data or {}).get("src", "") or "")
        dst = str((data or {}).get("dst", "") or "")
        if not src or not dst:
            self._send_json({"ok": False, "error": "缺少源或目标路径"}, 400)
            return
        s, err = self._fs_abs(src, need=True)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        d, err = self._fs_abs(dst, need=False)
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if d.exists() and d.is_dir():
            d = d / s.name
        if d.exists():
            self._send_json({"ok": False, "error": "目标已存在：" + d.name}, 400)
            return
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            if s.is_dir():
                shutil.copytree(str(s), str(d))
            else:
                shutil.copy2(str(s), str(d))
        except Exception as e:
            self._send_json({"ok": False, "error": f"复制失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已复制"})

    def _handle_api_files_write(self, data):
        rel = str((data or {}).get("path", "") or "")
        content = (data or {}).get("content")
        if not rel:
            self._send_json({"ok": False, "error": "缺少文件路径"}, 400)
            return
        if content is None:
            self._send_json({"ok": False, "error": "缺少内容"}, 400)
            return
        target, err = self._fs_abs(rel, need=False, want="file")
        if err:
            self._send_json({"ok": False, "error": err}, 400)
            return
        if str(target) == str(self._fs_root()):
            self._send_json({"ok": False, "error": "不能写入服务器根目录本身"}, 400)
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
        except Exception as e:
            self._send_json({"ok": False, "error": f"保存失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "message": "已保存 " + target.name})

    def _handle_api_files_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._send_json({"ok": False, "error": "必须使用 multipart/form-data"}, 400)
            return
        bm = re.search(r'boundary=([^;\s]+)', content_type)
        if not bm:
            self._send_json({"ok": False, "error": "无效的 multipart 格式"}, 400)
            return
        boundary = bm.group(1).encode("ascii")
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > self._FILES_UPLOAD_LIMIT:
            self._send_json({"ok": False, "error": "请求体过大或非法"}, 400)
            return
        body = self.rfile.read(length)
        parts = body.split(b"--" + boundary)
        dir_rel = ""
        files = []
        for part in parts:
            if not part or part in (b"--\r\n", b"--"):
                continue
            h_end = part.find(b"\r\n\r\n")
            if h_end == -1:
                continue
            header = part[:h_end].decode("utf-8", errors="ignore")
            content = part[h_end + 4:]
            if content.endswith(b"\r\n"):
                content = content[:-2]
            if 'name="dir"' in header:
                dir_rel = content.decode("utf-8", errors="ignore").strip()
            if 'name="file"' in header and 'filename=' in header:
                fm = re.search(r'filename="([^"]*)"', header)
                fname = fm.group(1) if fm else ""
                if fname:
                    files.append((self._fs_basename(fname), content))
        if not files:
            self._send_json({"ok": False, "error": "未收到文件"}, 400)
            return
        dir_target, derr = self._fs_abs(dir_rel, need=False)
        if derr:
            self._send_json({"ok": False, "error": derr}, 400)
            return
        saved = []
        for fname, fdata in files:
            if not fname:
                continue
            dest = dir_target / fname
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(str(dest), "wb") as f:
                    f.write(fdata)
                saved.append(fname)
            except Exception as e:
                self.plugin.logger.error(f"[文件管理] 保存上传 {fname} 失败: {e}")
        if not saved:
            self._send_json({"ok": False, "error": "所有文件均保存失败"}, 500)
            return
        self._send_json({"ok": True, "message": f"已上传 {len(saved)} 个文件"})

    def _handle_api_permission(self, data):
        player_name = data.get('player_name')
        permission = data.get('permission')
        action = data.get('action', 'set')
        value = data.get('value', True)
        if not player_name or not permission:
            self._send_json({"error": "缺少 player_name 或 permission"}, 400)
            return
        if action not in ('set', 'unset'):
            self._send_json({"error": "action 必须是 'set' 或 'unset'"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(
                self._execute_permission, player_name, permission, action, bool(value)
            )
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"权限操作失败: {e}")
            self._send_json({"error": "权限操作失败"}, 500)

    def _execute_permission(self, player_name: str, permission: str, action: str, value: bool):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        try:
            att = self.plugin._perm_attachments.get(player_name)
            if att is not None:
                try:
                    if att.permissible is not target:
                        att = None
                except Exception:
                    att = None
            if action == "unset":
                if att is not None:
                    att.unset_permission(permission)
                target.recalculate_permissions()
                return True, f"已撤销 {player_name} 的权限 {permission}"
            if att is None:
                att = target.add_attachment(self.plugin)
                self.plugin._perm_attachments[player_name] = att
            att.set_permission(permission, value)
            target.recalculate_permissions()
            return True, f"已设置 {player_name} 的权限 {permission} = {value}"
        except Exception as e:
            return False, f"权限操作失败: {e}"

    def _handle_api_objective(self, data):
        action = data.get('action')
        name = data.get('name')
        if action not in ('add', 'remove', 'display'):
            self._send_json({"error": "action 必须是 'add'/'remove'/'display'"}, 400)
            return
        if not name:
            self._send_json({"error": "缺少 name"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(
                self._execute_objective, action, name,
                data.get('display_name'), data.get('render_type'), data.get('slot'), data.get('order')
            )
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"记分板目标操作失败: {e}")
            self._send_json({"error": "记分板目标操作失败"}, 500)

    def _execute_objective(self, action: str, name: str, display_name=None, render_type=None, slot=None, order=None):
        scoreboard = self.plugin.server.scoreboard
        if not scoreboard:
            return False, "记分板不可用"
        try:
            if action == "add":
                existing = scoreboard.get_objective(name)
                if existing is not None:
                    return False, f"目标 {name} 已存在"
                scoreboard.add_objective(name, _dummy_criteria(), display_name or name, _to_render(render_type))
                return True, f"已创建目标 {name}"
            objective = scoreboard.get_objective(name)
            if objective is None:
                return False, f"目标 {name} 不存在"
            if action == "remove":
                objective.unregister()
                return True, f"已删除目标 {name}"
            if action == "display":
                objective.set_display(_to_slot(slot), _to_order(order))
                return True, f"已设置目标 {name} 的显示位置"
            return False, "未知操作"
        except Exception as e:
            return False, f"操作失败: {e}"

    def _handle_api_kill(self, data):
        player_name = data.get('player_name')
        if not player_name:
            self._send_json({"error": "缺少 player_name"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_kill, player_name)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"执行击杀失败: {e}")
            self._send_json({"error": "执行击杀失败"}, 500)

    def _execute_kill(self, player_name: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        if target.perform_command("kill"):
            return True, f"已击杀 {player_name}"
        return False, "击杀命令执行失败"

    def _handle_api_message(self, data):
        player_name = data.get('player_name')
        message = data.get('message')
        if not player_name or message is None:
            self._send_json({"error": "缺少 player_name 或 message"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_message, player_name, message)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"发送消息失败: {e}")
            self._send_json({"error": "发送消息失败"}, 500)

    def _execute_message(self, player_name: str, message: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        try:
            target.send_message(message)
            return True, f"已向 {player_name} 发送消息"
        except Exception as e:
            return False, f"发送失败: {e}"

    def _handle_api_give(self, data):
        player_name = data.get('player_name')
        item_id = data.get('item_id')
        amount = data.get('amount', 1)
        if not player_name or not item_id:
            self._send_json({"error": "缺少 player_name 或 item_id"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_give, player_name, item_id, amount)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"给予物品失败: {e}")
            self._send_json({"error": "给予物品失败"}, 500)

    def _execute_give(self, player_name: str, item_id: str, amount: int):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        if self.plugin._grant_item(player_name, item_id, amount):
            return True, f"已给予 {player_name} {amount} 个 {item_id}"
        return False, "给予物品命令执行失败"

    def _handle_api_health(self, data):
        player_name = data.get('player_name')
        operation = data.get('operation')
        value = data.get('value')
        if not player_name or operation not in ('set', 'add', 'sub') or value is None:
            self._send_json({"error": "参数错误，需要 player_name, operation(set/add/sub), value"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_health, player_name, operation, value)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"调整生命值失败: {e}")
            self._send_json({"error": "调整生命值失败"}, 500)

    def _execute_health(self, player_name: str, operation: str, value):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        if hasattr(target, 'game_mode'):
            try:
                gm = target.game_mode
                if gm is not None and str(gm).lower() == 'creative':
                    return False, "玩家处于创造模式，无法修改生命值"
            except Exception:
                pass
        try:
            val = int(round(float(value)))
            if operation == 'set':
                target.health = min(target.max_health, max(0, val))
            elif operation == 'add':
                target.health = min(target.max_health, target.health + val)
            else:
                target.health = max(0, target.health - val)
            return True, f"已调整 {player_name} 的生命值，当前: {target.health}"
        except Exception as e:
            return False, f"调整失败: {e}"

    def _handle_api_tag(self, data):
        player_name = data.get('player_name')
        tag = data.get('tag')
        action = data.get('action')
        if not player_name or not tag or action not in ('add', 'remove'):
            self._send_json({"error": "需要 player_name, tag, action(add/remove)"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_tag, player_name, tag, action)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"标签操作失败: {e}")
            self._send_json({"error": "标签操作失败"}, 500)

    def _execute_tag(self, player_name: str, tag: str, action: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        if not hasattr(target, 'add_scoreboard_tag') or not hasattr(target, 'remove_scoreboard_tag'):
            return False, "服务器不支持标签操作"
        try:
            if action == 'add':
                success = target.add_scoreboard_tag(tag)
            else:
                success = target.remove_scoreboard_tag(tag)
            if success:
                return True, f"已{action}标签 {tag} 给 {player_name}"
            return False, "标签操作失败"
        except Exception as e:
            return False, f"标签操作异常: {e}"

    def _handle_api_score(self, data):
        player_name = data.get('player_name')
        objective_name = data.get('objective')
        score = data.get('score')
        operation = data.get('operation', 'set')
        if not player_name or not objective_name or score is None:
            self._send_json({"error": "需要 player_name, objective, score"}, 400)
            return
        if operation not in ('set', 'add'):
            self._send_json({"error": "operation 必须是 'set' 或 'add'"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_score, player_name, objective_name, score, operation)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"设置记分板分数失败: {e}")
            self._send_json({"error": "设置分数失败"}, 500)

    def _execute_score(self, player_name: str, objective_name: str, score, operation: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        scoreboard = self.plugin.server.scoreboard
        if not scoreboard:
            return False, "记分板不可用"
        objective = scoreboard.get_objective(objective_name)
        if not objective:
            return False, f"记分板目标 {objective_name} 不存在"
        try:
            score_val = int(score)
            score_obj = objective.get_score(target)
            if score_obj is None:
                return False, "无法获取分数对象"
            if operation == 'set':
                score_obj.value = score_val
            else:
                score_obj.value += score_val
            return True, f"已{operation} {player_name} 的 {objective_name} 为 {score_obj.value}"
        except Exception as e:
            return False, f"设置分数失败: {e}"

    def _handle_api_kick(self, data):
        player_name = data.get('player_name')
        reason = data.get('reason', "被管理员踢出")
        if not player_name:
            self._send_json({"error": "缺少 player_name"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_kick, player_name, reason)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"踢出玩家失败: {e}")
            self._send_json({"error": "踢出玩家失败"}, 500)

    def _execute_kick(self, player_name: str, reason: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        try:
            target.kick(reason)
            return True, f"已踢出 {player_name}，原因: {reason}"
        except Exception as e:
            return False, f"踢出失败: {e}"

    def _handle_api_op(self, data):
        player_name = data.get('player_name')
        value = data.get('value')
        if not player_name:
            self._send_json({"error": "缺少 player_name"}, 400)
            return
        if value is None:
            self._send_json({"error": "缺少 value (true/false)"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_op, player_name, value)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"修改 OP 状态失败: {e}")
            self._send_json({"error": "修改 OP 状态失败"}, 500)

    def _execute_op(self, player_name: str, value):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"

        give = bool(value)
        cmd = "op" if give else "deop"
        try:
            ok = self.plugin.server.dispatch_command(
                self.plugin.server.command_sender, f"{cmd} {player_name}"
            )
        except Exception as e:
            return False, f"执行 {cmd} 命令异常: {e}"
        if ok:
            return True, f"已{'给予' if give else '移除'} {player_name} 的 OP 权限"
        return False, f"执行 {cmd} 命令失败"

    def _handle_api_teleport(self, data):
        player_name = data.get('player_name')
        x = data.get('x')
        y = data.get('y')
        z = data.get('z')
        dimension = data.get('dimension', 'overworld')
        if not player_name:
            self._send_json({"error": "缺少 player_name"}, 400)
            return
        if x is None or y is None or z is None:
            self._send_json({"error": "缺少坐标 x, y, z"}, 400)
            return
        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_teleport, player_name, float(x), float(y), float(z), dimension)
            self._send_json({"success": success, "message": msg} if success else {"error": msg})
        except Exception as e:
            self.plugin.logger.error(f"传送玩家失败: {e}")
            self._send_json({"error": "传送玩家失败"}, 500)

    def _execute_teleport(self, player_name: str, x: float, y: float, z: float, dimension_name: str):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"
        try:
            level = self.plugin.server.level
            if not level:
                return False, "无法获取服务器主世界"
            dim = level.get_dimension(dimension_name)
            if dim is None:
                alias = dimension_name.lower()
                if alias in ('overworld', 'world', '主世界'):
                    dim = level.get_dimension('overworld')
                elif alias in ('nether', '地狱', '下界'):
                    dim = level.get_dimension('nether')
                elif alias in ('the_end', 'end', '末地'):
                    dim = level.get_dimension('the_end')
                if dim is None:
                    return False, f"维度 {dimension_name} 不存在"
            from endstone.level import Location
            loc = Location(dim, x, y, z)
            target.teleport(loc)
            return True, f"已将 {player_name} 传送到 {dimension_name} ({x}, {y}, {z})"
        except Exception as e:
            return False, f"传送失败: {e}"

    def _handle_api_map(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._send_error("必须使用 multipart/form-data")
            return

        boundary_match = re.search(r'boundary=([^;\s]+)', content_type)
        if not boundary_match:
            self._send_error("无效的 multipart 格式")
            return
        boundary = boundary_match.group(1).encode('ascii')

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_error("请求体过大或非法")
            return
        body = self.rfile.read(length)

        parts = body.split(b'--' + boundary)
        player_name = None
        image_data = None

        for part in parts:
            if not part or part == b'--\r\n' or part == b'--':
                continue
            header_end = part.find(b'\r\n\r\n')
            if header_end == -1:
                continue
            header = part[:header_end].decode('utf-8', errors='ignore')
            content = part[header_end + 4:]
            if content.endswith(b'\r\n'):
                content = content[:-2]

            if 'name="player"' in header:
                player_name = content.decode('utf-8', errors='ignore').strip()
            elif 'name="image"' in header or 'filename=' in header:
                image_data = content

        if not player_name:
            self._send_error("缺少玩家名")
            return
        if not image_data:
            self._send_error("未收到图片数据")
            return

        try:
            success, msg = self.plugin._run_in_server_thread(self._execute_map, player_name, image_data)
            if success:
                self._send_success(msg)
            else:
                self._send_error(msg)
        except Exception as e:
            self.plugin.logger.error(f"生成地图失败: {e}")
            self._send_error(f"生成地图失败: {e}")

    def _execute_map(self, player_name: str, image_data: bytes):
        target = self.plugin.server.get_player(player_name)
        if not target:
            return False, f"玩家 {player_name} 不在线"

        try:
            pixels = decode_bmp_24bit(image_data)
        except Exception as e:
            return False, f"图片解码失败: {e}"

        try:
            dimension = target.dimension
        except AttributeError:
            try:
                dimension = target.location.dimension
            except AttributeError:
                return False, "无法获取玩家所在维度"

        try:
            map_view = self.plugin.server.create_map(dimension)
        except TypeError:
            try:
                map_view = self.plugin.server.create_map()
            except Exception:
                return False, "当前 Endstone 不支持创建地图"

        renderer = ImageMapRenderer(pixels)
        map_view.add_renderer(renderer)

        item_type = ItemType.get("minecraft:filled_map") or ItemType.get("filled_map")
        if not item_type:
            return False, "无法获取 filled_map 物品类型"
        item_stack = item_type.create_item_stack(1)

        meta = item_stack.item_meta
        if not isinstance(meta, MapMeta):
            from endstone.inventory import ItemMeta
            meta = ItemMeta()
        meta.map_view = map_view
        item_stack.set_item_meta(meta)

        remaining = target.inventory.add_item(item_stack)
        if remaining:

            try:
                dimension.drop_item(target.location, item_stack)
            except Exception as e:
                self.plugin.logger.warning(f"背包已满且掉落失败: {e}")
            self.plugin.logger.warning(f"{player_name} 背包已满，地图丢在地上")

        try:
            target.send_message("§c[警告] §e你收到了一张地图画，该地图渲染可能不稳定，请勿在低版本客户端使用。")
        except Exception:
            pass

        self.plugin.logger.info(f"为 {player_name} 生成地图，ID={map_view.id}")
        return True, f"已为 {player_name} 生成地图，ID: {map_view.id}（已发放）"

    def _execute_allowlist_add(self, name: str):

        try:
            ok = self.plugin.server.dispatch_command(self.plugin.server.command_sender, f"allowlist add {name}")
            return True if ok else "allowlist 命令未成功执行（请确认已开启 allow-list）"
        except Exception as e:
            return f"执行 allowlist 命令异常: {e}"

    def _handle_api_whitelist_apply(self, data):
        name = str(data.get("name", "")).strip()
        if not name:
            self._send_json({"error": "请填写游戏名"}, 400)
            return
        if len(name) > 64:
            self._send_json({"error": "游戏名过长"}, 400)
            return

        if not re.fullmatch(r"[0-9A-Za-z_\-\. ]+", name):
            self._send_json({"error": "游戏名包含非法字符"}, 400)
            return
        xuid = str(data.get("xuid", "")).strip()
        key = name.lower()
        with self.plugin._whitelist_lock:
            existing = self.plugin._whitelist_apps.get(key)
            if existing and existing.get("status") == "approved":
                self._send_json({"success": False, "message": "该游戏名已在白名单中"})
                return
        app = {
            "name": name,
            "xuid": xuid,
            "note": str(data.get("note", "")).strip(),
            "status": "pending",
            "applied_at": datetime.now().isoformat(),
            "reviewed_at": "",
            "reviewer": "",
        }
        with self.plugin._whitelist_lock:
            self.plugin._whitelist_apps[key] = app
        self.plugin._save_whitelist()
        self._send_json({"success": True, "message": "申请已提交，请等待管理员审核"})

    def _handle_api_whitelist_status(self, query):
        qs = urllib.parse.parse_qs(query)
        name = str(qs.get("name", [""])[0]).strip()
        if not name:
            self._send_json({"error": "缺少 name"}, 400)
            return
        with self.plugin._whitelist_lock:
            app = self.plugin._whitelist_apps.get(name.lower())
        self._send_json({"application": app})

    def _handle_api_whitelist_applications(self):
        try:
            apps = self.plugin._list_whitelist_apps()
            self._send_json({"applications": apps})
        except Exception as e:
            self.plugin.logger.error(f"获取白名单申请失败: {e}")
            self._send_json({"applications": []})

    def _handle_api_system_stats(self):

        try:
            stats = self.plugin.get_system_stats()
            self._send_json({"ok": True, "stats": stats})
        except Exception as e:
            self.plugin.logger.error(f"获取系统状态失败: {e}")
            self._send_json({"ok": False, "stats": {
                "cpu": 0, "mem": 0, "disk": 0,
                "mem_total_mb": 0, "mem_used_mb": 0,
                "disk_total_gb": 0, "disk_used_gb": 0, "uptime": 0}}, 500)

    def _handle_api_whitelist_review(self, data):
        name = str(data.get("name", "")).strip()
        action = str(data.get("action", "")).strip()
        note = str(data.get("note", "")).strip()
        if not name or action not in ("approve", "reject"):
            self._send_json({"error": "参数错误：需要 name 和 action(approve/reject)"}, 400)
            return
        key = name.lower()
        with self.plugin._whitelist_lock:
            app = self.plugin._whitelist_apps.get(key)
        if not app:
            self._send_json({"error": "未找到该申请"}, 404)
            return

        if action == "approve":
            stored_name = app.get("name") or name
            try:
                result = self.plugin._run_in_server_thread(self._execute_allowlist_add, stored_name, timeout=3.0)
            except Exception as e:
                self.plugin.logger.error(f"白名单审核异常: {e}")
                self._send_json({"error": "审核失败"}, 500)
                return
            if result is True:
                with self.plugin._whitelist_lock:
                    app["status"] = "approved"
                    app["reviewed_at"] = datetime.now().isoformat()
                    app["reviewer"] = "管理员"
                    app["note"] = note or app.get("note", "")
                    self.plugin._whitelist_apps[key] = app
                self.plugin._save_whitelist()
                self.plugin._log_admin_action(f"白名单通过: {name}")
                self.plugin._cross_push_whitelist()
                self._send_json({"success": True, "message": f"已通过 {name} 的白名单申请"})
            else:
                msg = result if isinstance(result, str) else "allowlist 命令执行失败"
                self._send_json({"error": f"添加白名单失败: {msg}"})
            return

        with self.plugin._whitelist_lock:
            app["status"] = "rejected"
            app["reviewed_at"] = datetime.now().isoformat()
            app["reviewer"] = "管理员"
            app["note"] = note or app.get("note", "")
            self.plugin._whitelist_apps[key] = app
        self.plugin._save_whitelist()
        self.plugin._log_admin_action(f"白名单拒绝: {name}")
        self._send_json({"success": True, "message": f"已拒绝 {name} 的申请"})

    def _handle_api_properties_get(self):
        try:
            root, content = self.plugin._read_properties_raw()
            keys = {}
            for line in content.splitlines():
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, _, v = s.partition("=")
                    keys[k.strip()] = v.strip()
            path_str = str(root / "server.properties") if root else ""
            self._send_json({"found": root is not None, "path": path_str, "raw": content, "keys": keys})
        except Exception as e:
            self.plugin.logger.error(f"读取 server.properties 失败: {e}")
            self._send_json({"found": False, "path": "", "raw": "", "keys": {}})

    def _handle_api_properties_save(self, data):
        content = data.get("content")
        if content is None:
            self._send_json({"error": "缺少 content"}, 400)
            return
        ok, msg = self.plugin._write_properties_raw(str(content))
        if ok:
            self._send_json({"success": True, "message": msg})
        else:
            self._send_json({"error": msg})

    def _handle_api_properties_set(self, data):
        key = str(data.get("key", "")).strip()
        if not key:
            self._send_json({"error": "缺少 key"}, 400)
            return
        value = data.get("value")
        value = "" if value is None else str(value)

        if "\n" in key or "\r" in key or "=" in key:
            self._send_json({"error": "key 包含非法字符"}, 400)
            return
        if "\n" in value or "\r" in value:
            self._send_json({"error": "value 包含非法字符"}, 400)
            return
        _root, content = self.plugin._read_properties_raw()
        content = self.plugin._set_property_value(content, key, value)
        ok, msg = self.plugin._write_properties_raw(content)
        if ok:
            self._send_json({"success": True, "message": f"已设置 {key} = {value}"})
        else:
            self._send_json({"error": msg})

    def _handle_api_worlds(self):
        try:
            worlds, current, root_path = self.plugin._list_worlds()
            self._send_json({"worlds": worlds, "current": current, "root": root_path})
        except Exception as e:
            self.plugin.logger.error(f"获取世界列表失败: {e}")
            self._send_json({"worlds": [], "current": "", "root": ""})

    def _handle_api_world_switch(self, data):
        world = str(data.get("world", "")).strip()
        if not world:
            self._send_json({"error": "缺少 world"}, 400)
            return
        worlds, current, _ = self.plugin._list_worlds()
        names = [w["name"] for w in worlds]
        if world not in names:
            self._send_json({"error": f"未找到名为 {world} 的世界存档，请刷新后重试"}, 400)
            return
        _, content = self.plugin._read_properties_raw()
        content = self.plugin._set_property_value(content, "level-name", world)
        ok, msg = self.plugin._write_properties_raw(content)
        if ok:
            self.plugin._log_admin_action(f"切换世界存档: {current} -> {world}（需重启生效）")
            self._send_json({"success": True, "message": f"已切换到存档 {world}，请重启服务器后生效", "restart_required": True})
        else:
            self._send_json({"error": msg})

    def _handle_api_console(self, query):
        qs = urllib.parse.parse_qs(query)
        try:
            after = int(qs.get("after", ["0"])[0])
        except Exception:
            after = 0
        try:
            logs, last_id = self.plugin._get_console_logs(after)
            self._send_json({"logs": logs, "last_id": last_id})
        except Exception as e:
            self.plugin.logger.error(f"获取控制台日志失败: {e}")
            self._send_json({"logs": [], "last_id": 0})

    def _execute_stop(self):
        try:
            self.plugin.server.dispatch_command(self.plugin.server.command_sender, "stop")
        except Exception as e:
            self.plugin.logger.error(f"执行 stop 命令失败: {e}")

    def _handle_api_restart(self, data):

        self._send_json({"success": True, "message": "已请求停止服务器；若未配置自动拉起，请手动重启服务器脚本"})
        self.plugin._log_admin_action("收到重启请求，即将停止服务器")

        def _later_stop():
            time.sleep(1.0)
            self.plugin._run_async_on_server_thread(self._execute_stop)

        threading.Thread(target=_later_stop, daemon=True).start()

    def _handle_api_qqbot_config(self):
        try:
            pl = self.plugin
            data = pl._qqbot_public_config()
            data["status"] = pl._qqbot_status()
            self._send_json({"ok": True, **data})
        except Exception as e:
            self.plugin.logger.error(f"GET /api/qqbot/config 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_qqbot_status(self):
        try:
            self._send_json({"ok": True, **self.plugin._qqbot_status()})
        except Exception as e:
            self.plugin.logger.error(f"GET /api/qqbot/status 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_qqbot_group_new(self):
        try:
            st = self.plugin._qqbot_status() or {}
            missing = [m for m in (st.get("missing") or []) if m in ("AppID", "AppSecret")]
            has_group = bool(str(st.get("group_openid") or "") or st.get("groups"))
            if missing and not has_group:
                self._send_json({"ok": False,
                                 "error": "请先完成机器人信息（AppID/AppSecret）配置并绑定主群，再来申请群绑定码"}, 400)
                return
            code = self.plugin._qqbot_new_group_code()
            self._send_json({"ok": True, "code": code})
        except Exception as e:
            self.plugin.logger.error(f"GET /api/qqbot/group/new 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_qqbot_group_rename(self, data):
        try:
            openid = str((data or {}).get("openid", "") or "").strip()
            name = str((data or {}).get("name", "") or "").strip()
            if not openid:
                self._send_json({"ok": False, "error": "缺少群 openid"}, 400)
                return
            bot = getattr(self.plugin, "qq_bot", None)
            if bot is None:
                self._send_json({"ok": False, "error": "机器人未初始化"}, 500)
                return
            bot._rename_group(openid, name or openid)
            self._send_json({"ok": True, "openid": openid, "name": name or openid})
        except Exception as e:
            self.plugin.logger.error(f"POST /api/qqbot/group/rename 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_bots_list(self):
        try:
            self._send_json(self.plugin._bots_list())
        except Exception as e:
            self.plugin.logger.error(f"GET /api/bots/list 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_bots_create(self, data):
        try:
            self._send_json(self.plugin._bots_create(data))
        except (TypeError, ValueError) as e:
            self._send_json({"ok": False, "error": f"输入有误：{e}"}, 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/bots/create 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_bots_update(self, data):
        try:
            adapter_id = str((data or {}).get("id", "") or "").strip()
            patch = (data or {}).get("patch") or {}
            if not isinstance(patch, dict):
                patch = {}
            self._send_json(self.plugin._bots_update(adapter_id, patch))
        except (TypeError, ValueError) as e:
            self._send_json({"ok": False, "error": f"输入有误：{e}"}, 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/bots/update 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_bots_toggle(self, data):
        try:
            adapter_id = str((data or {}).get("id", "") or "").strip()
            enabled = bool((data or {}).get("enabled", False))
            self._send_json(self.plugin._bots_toggle(adapter_id, enabled))
        except (TypeError, ValueError) as e:
            self._send_json({"ok": False, "error": f"输入有误：{e}"}, 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/bots/toggle 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_bots_delete(self, data):
        try:
            adapter_id = str((data or {}).get("id", "") or "").strip()
            self._send_json(self.plugin._bots_delete(adapter_id))
        except (TypeError, ValueError) as e:
            self._send_json({"ok": False, "error": f"输入有误：{e}"}, 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/bots/delete 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_api_qqbot_config_save(self, data):
        try:
            if not isinstance(data, dict):
                return self._send_json({"ok": False, "error": "数据格式不正确"}, 400)
            ok, msg = self.plugin._qqbot_save_config(data)
            self._send_json({"ok": bool(ok), "message": msg})
        except (TypeError, ValueError) as e:
            self._send_json({"ok": False, "error": f"输入有误：{e}"}, 400)
        except Exception as e:
            self.plugin.logger.error(f"POST /api/qqbot/config/save 异常: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _send_error(self, msg: str):
        self.send_response(400)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"错误: {msg}".encode())

    def _send_success(self, msg: str):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(msg.encode())

# ---- 子系统反向导入（在模块末尾，避免循环导入） ----
from .cross import CrossManager

# ---- 子系统反向导入（在模块末尾，避免循环导入） ----
from .cross import CrossManager
from .qqbot import QQBotAPI, QQBotGateway, _TokenBucket, _GatewayLimiter
