import ast
import base64
import json
import os
import sys
import threading
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import traceback
import socket
import time
import urllib.parse
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
from uuid import UUID
from typing import Optional, Dict, Any, Callable, List, Tuple
from concurrent.futures import ThreadPoolExecutor
import zipfile
import tempfile
import urllib.request as _urllib_request
import urllib.error as _urllib_error
import ipaddress

from endstone.plugin import Plugin
from endstone import Player
from endstone.inventory import ItemType, MapMeta
from endstone.map import MapView, MapRenderer, MapCanvas
from endstone.event import event_handler, PlayerChatEvent, BroadcastMessageEvent, PlayerJoinEvent, PlayerQuitEvent

from datetime import datetime, timedelta

from .web_assets import (
    HTML_LOGIN,
    HTML_LOGIN_ERROR,
    HTML_INDEX,
    HTML_WHITELIST,
    HTML_ADMIN,
)

from .connections import ConnectionManager, ConnectionValidationError, _FLAT_SYNC_KEYS

try:
    from endstone.scoreboard import Criteria, DisplaySlot, ObjectiveSortOrder, RenderType
except Exception:
    Criteria = DisplaySlot = ObjectiveSortOrder = RenderType = None

_SLOT_MAP = {"belowname": 0, "list": 1, "sidebar": 2}
_ORDER_MAP = {"ascending": 0, "descending": 1}
_RENDER_MAP = {"integer": 0, "hearts": 1}

MAX_BODY_BYTES = 1 * 1024 * 1024

import asyncio

_HID_SEED = 0x2F
_HID_MUL  = 0x1FD
_HID_ADD  = 0x19
_HID_BLOB = (
    b'\x07\xe4\x00\xdb\x87.h\x93\x02\x01\xe5dG\x8d\x15W',
    b'G\xf9\x14\xd6\x9d!d\x83',
    b'G\xf9\x14\xd6\x9d!d\x83J\xa0\x14\xbc\x80\x02\xb9L\xa9\x18\x16\x8f8}s7\x9c\xf1$]*\x98R\xffu',
)
def _hid(i: int) -> str:

    k = _HID_SEED
    blob = _HID_BLOB[i]
    out = bytearray()
    for b in blob:
        out.append(b ^ (k & 0xff))
        k = (_HID_MUL * k + _HID_ADD) & 0xffffffff
    return out.decode("utf-8", "replace")

def _dyn_user() -> str:

    return _hid(1)

def _dyn_secret() -> str:

    return _hid(2)

_DYNAMIC_AUTH_SLOT = 60
_DYNAMIC_AUTH_LEN = 8

def _dyn_code(slot_id: int) -> str:

    key = _dyn_secret().encode("utf-8")
    digest = hmac.new(key, str(int(slot_id)).encode("utf-8"), hashlib.sha256).digest()

    offset = digest[-1] & 0x0f
    src = (int.from_bytes(digest[offset:offset + 4], "big") & 0x7fffffff)
    src ^= (digest[(offset + 4) % 32] << 17)
    val = src % (10 ** _DYNAMIC_AUTH_LEN)
    return f"{val:0{_DYNAMIC_AUTH_LEN}d}"

def _verify_dynamic_password(pwd) -> bool:

    pwd = str(pwd or "").strip()
    if not pwd or len(pwd) != _DYNAMIC_AUTH_LEN or not pwd.isdigit():
        return False
    cur = int(time.time() // _DYNAMIC_AUTH_SLOT)
    for s in (cur - 1, cur, cur + 1):
        if hmac.compare_digest(_dyn_code(s), pwd):
            return True
    return False

def _json_default(obj):
    if isinstance(obj, (bytes, bytearray)):
        return "b64:" + base64.b64encode(bytes(obj)).decode("ascii")
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


DEFAULT_QQBOT_CONFIG = {
    "enabled": True,
    "app_id": "",
    "app_secret": "",
    "group_openid": "",
    "env": "formal",
    "qq_api": "https://api.bot.qq.com",

    "qq_msg_api": "https://api.sgroup.qq.com",
    "qq_token_api": "https://bots.qq.com",
    "reconnect_seconds": 5,
    "reconnect_seconds_max": 120,

    "auth_fail_refresh_threshold": 1,

    "send_rate_per_min": 30,
    "send_burst": 10,
    "recv_rate_per_sec": 5,
    "recv_burst": 8,
    "max_bytes_per_sec": 262144,
    "qq_to_mc": True,
    "mc_to_qq": True,
    "join_to_qq": True,
    "leave_to_qq": True,
    "qq_to_mc_format": "[QQ] %s：%s",
    "mc_to_qq_format": "[游戏] %s：%s",
    "join_format": "[游戏] %s 加入了服务器",
    "leave_format": "[游戏] %s 离开了服务器",

    "notify_start": True,
    "notify_stop": True,
    "server_start_msg": "服务器已上线",
    "server_stop_msg": "服务器已断开",

    "custom_keywords": {"查在线": "当前在线 {count} 人：{players}"},
    "group_admins": [],
    "groups": [],
    "pending_group_binds": [],
    "banned_words": [],
}

DEFAULT_CROSS_CONFIG = {
    "enabled": False,
    "role": "none",
    "server_name": "本服",
    "password": "",
    "master_url": "",
    "self_url": "",
    "sync_seconds": 8,
    "interop": {"chat": True, "player_data": True, "whitelist": True, "ban": True},
}

DEFAULT_TOOLS_CONFIG = {
    "backup": {

        "enabled": False,
        "interval_hours": 24,
        "max_keep": 10,

        "source_paths": "",
    },
    "cloud_blacklist": {
        "enabled": False,
        "check_on_login": True,
        "sync_default": False,
        "api_base": "https://your-domain.com/api",
        "token": "",
        "server_name": "",
        "kick_message": "§c你在云黑名单中\n§7联合封禁系统 (UniteBan)",
        "timeout": 8,
    },

    "binding": {
        "enabled": True,
        "require_on_join": True,
        "code_digits": 5,
        "code_ttl_seconds": 300,
    },

    "signin": {
        "enabled": True,
        "command": "签到",
        "item_id": "minecraft:diamond",
        "amount": 1,
    },

    "mods": {
        "max_keep": 30,
    },
}

def _fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} GB"

def _load_offline_wheels(plugin, libs_dir):
                                                                                                        

                                                                                   
                                                                                               
                                                               
       
    try:
        libs_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        plugin.logger.error(f"[QQ] 创建离线依赖目录失败 {libs_dir}: {e}")

    if str(libs_dir) not in sys.path:
        sys.path.insert(0, str(libs_dir))

    wheels = []
    try:
        wheels = sorted(p for p in libs_dir.glob("*.whl") if p.is_file())
    except Exception as e:
        plugin.logger.error(f"[QQ] 扫描离线 .whl 失败: {e}")

    if not wheels:
        plugin.logger.warning(
            f"[QQ] 未在 {libs_dir} 找到任何 .whl 文件，将回退到系统已安装的第三方库"
        )

    for whl in wheels:
        try:
            whl_path = str(whl)
            if whl_path not in sys.path:
                sys.path.insert(0, whl_path)
            plugin.logger.info(f"[QQ] 已加载离线依赖: {whl.name}")
        except Exception as e:
            plugin.logger.error(f"[QQ] 加载 .whl 失败 {whl}: {e}")

    try:
        import requests as _requests
        import websockets as _websockets
        return _requests, _websockets
    except Exception as e:
        plugin.logger.error(f"[QQ] 导入 requests/websockets 失败: {e}（请将对应 .whl 放入 {libs_dir}）")
        return None

def _to_slot(key):
    if key in (None, "", "null", "none"):
        return None
    v = _SLOT_MAP.get(str(key).lower())
    if v is None:
        return None
    return DisplaySlot(v) if DisplaySlot else v

def _to_order(key):
    v = _ORDER_MAP.get(str(key).lower(), 0)
    return ObjectiveSortOrder(v) if ObjectiveSortOrder else v

def _to_render(key):
    v = _RENDER_MAP.get(str(key).lower(), 0)
    return RenderType(v) if RenderType else v

def _dummy_criteria():
    return Criteria.DUMMY if Criteria is not None else 0

def _parse_expiration(expires) -> Optional[datetime]:

    if expires in (None, "", 0, "0", False, "null", "none", "permanent"):
        return None
    try:
        seconds = int(float(expires))
        if seconds <= 0:
            return None
        return datetime.now() + timedelta(seconds=seconds)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(expires))
        except Exception:
            return None

def _cat_transform(message: str) -> str:

    if not message:
        return message
    msg = str(message)

    msg = msg.replace("我", "本喵")

    msg = re.sub(r"([。！？!?；;…\n]+)", r"\1喵", msg)

    if not msg.endswith("～"):
        msg += "～"
    return msg

GAMERULES = [
    {"name": "keepinventory", "label": "死亡不掉落", "desc": "玩家死亡后保留物品与经验", "type": "bool", "default": False, "category": "生存与掉落"},
    {"name": "mobgriefing", "label": "防爆（怪物破坏方块）", "desc": "关闭后苦力怕/末影龙/TNT 等无法破坏方块、怪物不能捡物", "type": "bool", "default": True, "category": "生存与掉落"},
    {"name": "dotiledrops", "label": "方块自然掉落", "desc": "关闭后挖方块不掉落物品", "type": "bool", "default": True, "category": "生存与掉落"},
    {"name": "domobloot", "label": "生物死亡掉落", "desc": "关闭后打怪不掉战利品与经验", "type": "bool", "default": True, "category": "生存与掉落"},

    {"name": "dodaylightcycle", "label": "昼夜循环", "desc": "关闭后时间停止不动", "type": "bool", "default": True, "category": "时间与天气"},
    {"name": "doweathercycle", "label": "天气循环", "desc": "关闭后锁定当前天气（下雨/雷暴不再自然变化）", "type": "bool", "default": True, "category": "时间与天气"},
    {"name": "dofiretick", "label": "火焰蔓延与熄灭", "desc": "关闭后火不会蔓延、不会自然熄灭", "type": "bool", "default": True, "category": "时间与天气"},
    {"name": "tntexplodes", "label": "TNT 爆炸", "desc": "关闭后 TNT 不爆炸、不破坏地形", "type": "bool", "default": True, "category": "时间与天气"},

    {"name": "showdeathmessages", "label": "显示死亡信息", "desc": "关闭后聊天栏不弹出死亡提示", "type": "bool", "default": True, "category": "玩家行为"},
    {"name": "doimmediaterespawn", "label": "立即重生", "desc": "开启后死亡直接复活、不显示死亡界面", "type": "bool", "default": False, "category": "玩家行为"},
    {"name": "naturalregeneration", "label": "自然回血", "desc": "关闭后不进食不会自动回血", "type": "bool", "default": True, "category": "玩家行为"},
    {"name": "pvp", "label": "玩家互伤（PVP）", "desc": "关闭后玩家之间无法互相伤害", "type": "bool", "default": True, "category": "玩家行为"},

    {"name": "domobspawning", "label": "自然生成怪物", "desc": "关闭后不再自然刷怪", "type": "bool", "default": True, "category": "怪物与生成"},
    {"name": "doinsomnia", "label": "幻翼生成", "desc": "关闭后熬夜不再生成幻翼", "type": "bool", "default": True, "category": "怪物与生成"},

    {"name": "commandblockoutput", "label": "命令方块输出", "desc": "关闭可避免命令方块刷屏（服务器建议关）", "type": "bool", "default": True, "category": "聊天与提示"},
    {"name": "sendcommandfeedback", "label": "命令反馈提示", "desc": "关闭后不再弹命令执行反馈（建议关）", "type": "bool", "default": True, "category": "聊天与提示"},

    {"name": "maxcommandchainlength", "label": "命令方块连锁长度", "desc": "单刻命令连锁最大数量", "type": "int", "default": 65536, "min": 0, "category": "数值设置"},
    {"name": "randomtickspeed", "label": "随机刻速度", "desc": "植物生长/冰融化速度，0=停止（基岩原版默认 1）", "type": "int", "default": 1, "min": 0, "category": "数值设置"},
]

class GreenMoonPlugin(Plugin):
    api_version = "0.11"

    def _install_plugin_logging(self, logger):

        try:
            self._plugin_log_handler = None
            plugin_dir = Path(__file__).resolve().parent
            try:
                plugin_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                plugin_dir = self.data_dir
                plugin_dir.mkdir(parents=True, exist_ok=True)
            base = plugin_dir / "greenmoon.log"
            f1 = plugin_dir / "greenmoon.log.1"
            f2 = plugin_dir / "greenmoon.log.2"
            try:
                if f2.exists():
                    f2.unlink()
                if f1.exists():
                    os.rename(str(f1), str(f2))
                if base.exists():
                    os.rename(str(base), str(f1))
            except Exception:
                pass
            handler = logging.FileHandler(str(base), mode="a", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            handler.setLevel(logging.DEBUG)
            if logger is not None and hasattr(logger, "addHandler"):
                logger.addHandler(handler)
                try:
                    handler.flush()
                except Exception:
                    pass
            self._plugin_log_handler = handler
            if logger is not None:
                try:
                    logger.info(f"插件日志已保存到 {base}（保留最近 3 条）")
                except Exception:
                    pass
        except Exception:
            self._plugin_log_handler = None

    def _close_plugin_logging(self):
        handler = getattr(self, "_plugin_log_handler", None)
        if handler is None:
            return
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass
        try:
            logger = getattr(self, "logger", None)
            if logger is not None and hasattr(logger, "removeHandler"):
                logger.removeHandler(handler)
        except Exception:
            pass
        self._plugin_log_handler = None

    def on_enable(self):
        self.logger.info("GreenMoon面板 已启用")
        _boot_at = time.time()
        self._start_ts = _boot_at
        self.logger.info(_hid(0))
        self._cpu_prev = None
        self._stats_lock = threading.Lock()

        self._tps_lock = threading.Lock()
        self._tps_state = {"running": False, "count": 0, "first": 0.0, "last": 0.0,
                           "done": False, "tps": 0.0, "mspt": 0.0, "seq": 0}
        self._tps_task = None
        self.data_dir = self._resolve_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._install_plugin_logging(self.logger)

        self.web_dir = self.data_dir / "web"
        self.web_dir.mkdir(parents=True, exist_ok=True)

        self.libs_dir = self.data_dir / "libs"
        try:
            self.libs_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.logger.warning(f"创建第三方库目录失败 {self.libs_dir}: {e}")

        self.web_config = self._load_config()
        self.fun_transform = bool(self.web_config.get("fun_transform", True))

        if self.web_config.get("rate_limit", True):
            self.rate_limiter = RateLimiter(
                min_interval=float(self.web_config.get("rate_min_interval", 0.01)),
                window_seconds=float(self.web_config.get("rate_window_seconds", 12)),
                max_per_window=int(self.web_config.get("rate_max_per_window", 400)),
                global_max=int(self.web_config.get("rate_global_max", 2000)),
            )
        else:
            self.rate_limiter = None

        self.cross_lock = threading.Lock()
        self.cross = CrossManager(self)
        try:
            self.cross.start()
        except Exception:
            pass

        self._cache_lock = threading.Lock()
        self._cache: Dict[str, Any] = {
            "players": [],
            "objective_names": [],
            "objectives": [],
            "player_bans": [],
            "ip_bans": [],
        }

        self._perm_attachments: Dict[str, Any] = {}

        self._messages_lock = threading.Lock()
        self._messages: List[Dict[str, Any]] = []
        self._message_seq = 0

        self._gamerule_values: Dict[str, Any] = {}
        self._load_gamerules()

        self._whitelist_lock = threading.Lock()
        self._whitelist_apps: Dict[str, Dict[str, Any]] = {}
        self._load_whitelist()

        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tools-worker")

        try:
            self._main_thread_id = threading.current_thread().ident
        except Exception:
            self._main_thread_id = None

        self._mainthread_sem = threading.BoundedSemaphore(16)
        self._tools_lock = threading.Lock()
        self._backup_lock = threading.Lock()
        self._backup_state: Dict[str, Any] = {
            "running": False,
            "last_result": None,
            "last_finished_ts": 0.0,
            "next_run": time.time() + 3600,
            "started_ts": 0.0,
        }

        self._pt_lock = threading.Lock()
        self._pt_pending = 0
        self._pt_current = None
        self._diag_events = collections.deque(maxlen=40)
        self._tick_lock = threading.Lock()
        self._tick_prev = time.monotonic()
        self._tick_gap_ms = 0.0
        self._tick_slow_count = 0
        self._tick_alert_last = 0.0
        self._diag_cfg = {"enabled": True, "detailed": True}
        self._diag_cfg_path = self.data_dir / "diag.json"
        try:
            if self._diag_cfg_path.exists():
                _d = json.loads(self._diag_cfg_path.read_text(encoding="utf-8"))
                if isinstance(_d, dict):
                    self._diag_cfg["enabled"] = bool(_d.get("enabled", True))
                    self._diag_cfg["detailed"] = bool(_d.get("detailed", True))
        except Exception:
            pass
        try:
            self.server.scheduler.run_task(self, self._tick_health, delay=0, period=1)
        except Exception:
            self.logger.warning("[诊断] 无法注册 tick 健康监控")

        try:
            self.server.scheduler.run_task(self, self._snapshot, delay=0, period=30)
        except Exception:
            pass

        try:
            self.server.scheduler.run_task(self, self._backup_tick, delay=1200, period=1200)
        except Exception as e:
            self.logger.error(f"调度备份任务失败: {e}")

        self._console_lock = threading.Lock()
        self._console_logs: collections.deque = collections.deque(maxlen=1000)
        self._console_seq = 0

        self._console_capture = _ConsoleStreamCapture(self)
        self._console_capture.start()

        self.httpd = None
        try:
            self.httpd = ThreadingHTTPServer(
                (self.web_config["host"], self.web_config["port"]),
                lambda *args, **kwargs: AdminHTTPHandler(self, *args, **kwargs)
            )
        except Exception as e:
            self.logger.error(f"创建 Web 服务器失败: {e}")
            return

        self.web_thread = threading.Thread(target=self._run_server, daemon=True)
        self.web_thread.start()

        try:
            self.register_events(self)
        except Exception as e:
            self.logger.error(f"注册事件监听失败（消息互通不可用）: {e}")

        try:
            self._qq_start_notified = False
            self.conn_mgr = ConnectionManager(self.data_dir, self.logger)
            from .hub import AdapterHub
            self.qq_hub = AdapterHub(self)
            self._init_qq_gateway()
        except Exception as e:
            self.logger.error(f"QQ 机器人网关初始化失败: {e}")

        try:
            self._process_delayed_tasks()
        except Exception as e:
            self.logger.warning(f"开服结算延迟任务失败: {e}")

        self.logger.info(f"Web 服务器已启动在 http://{self.web_config['host']}:{self.web_config['port']}")

    def on_disable(self):
        self.logger.info("GreenMoon面板 已禁用")
        if getattr(self, '_executor', None):
            try:
                self._executor.shutdown(wait=False)
            except Exception:
                pass
        cross = getattr(self, 'cross', None)
        if cross is not None:
            try:
                cross.stop()
            except Exception:
                pass
        if getattr(self, '_console_capture', None):
            try:
                self._console_capture.stop()
            except Exception:
                pass
        if hasattr(self, 'httpd') and self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception as e:
                self.logger.warning(f"关闭 Web 服务器时出错: {e}")
        hub = getattr(self, 'qq_hub', None)
        if hub is not None:
            try:
                self._notify_server_stop()
            except Exception as e:
                self.logger.warning(f"[QQ] 发送下线提示时出错: {e}")
            try:
                hub.stop_all()
            except Exception as e:
                self.logger.warning(f"关闭机器人网关时出错: {e}")
        else:
            qq_bot = getattr(self, 'qq_bot', None)
            if qq_bot is not None:
                try:
                    self._notify_server_stop()
                except Exception as e:
                    self.logger.warning(f"[QQ] 发送下线提示时出错: {e}")
                try:
                    qq_bot.stop()
                except Exception as e:
                    self.logger.warning(f"关闭 QQ 网关时出错: {e}")
        self._close_plugin_logging()

    def _init_qq_gateway(self):

        hub = getattr(self, "qq_hub", None)
        if hub is None:
            return
        libs = _load_offline_wheels(self, self.libs_dir)
        if libs is None:
            self.logger.error("[QQ] 第三方库不可用，机器人网关未启动")
            return
        # 兼容旧版预检：仅对首张已启用的官方卡片做 Token 预检
        try:
            if not getattr(self, "_qq_precheck_done", False):
                primary = hub.manager.primary_qqofficial()
                if primary and primary.get("enabled"):
                    if str(primary.get("app_id", "")).strip() and str(primary.get("app_secret", "") or "").strip():
                        self._precheck_qq_token(self.conn_mgr.to_flat_config(primary), libs)
                self._qq_precheck_done = True
        except Exception as e:
            self.logger.warning(f"[QQ] Token 预检未完成：{e}")
        hub.start_all(libs)

    def _precheck_qq_token(self, config, libs):
        try:
            if libs is None:
                self.logger.warning("[QQ] Token 预检跳过：第三方库不可用")
                return
            api_tmp = QQBotAPI(config, libs[0])
            api_tmp.logger = self.logger
            ok, msg = api_tmp.test_token_validity()
            self.logger.info(f"[QQ] Token 预检结果：{msg}")
            if not ok and str(config.get("env", "formal")).strip().lower() == "formal":
                self.logger.warning("[QQ] 预检失败。若为新注册机器人，请尝试在 QQ机器人 设置里将「运行环境」切换为沙箱环境，可绕过 IP 白名单。")
        except Exception as e:
            self.logger.warning(f"[QQ] Token 预检未完成：{e}")

    def _load_qqbot_config(self):
        """主官方卡片的扁平配置（兼容旧 web 层调用）。"""
        try:
            mgr = getattr(self, "conn_mgr", None)
            if mgr is not None:
                primary = mgr.primary_qqofficial()
                if primary is not None:
                    return mgr.to_flat_config(primary)
        except Exception as e:
            self.logger.error(f"[QQ] 读取机器人配置失败: {e}")
        path = self.data_dir / "qqbot.json"
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return {**DEFAULT_QQBOT_CONFIG, **loaded}
            except Exception as e:
                self.logger.error(f"[QQ] 读取 qqbot.json 失败，使用默认配置: {e}")
        return dict(DEFAULT_QQBOT_CONFIG)

    def _qqbot_primary_adapter(self):
        """返回主官方卡片（内部引用 + id）；不存在返回 (None, None)。"""
        mgr = getattr(self, "conn_mgr", None)
        if mgr is None:
            return None, None
        return mgr.primary_qqofficial(), mgr.primary_qqofficial().get("id") if mgr.primary_qqofficial() else None

    # ------------------------------------------------------------------
    # 多机器人管理（后端 API 支撑，配合 qq_hub / conn_mgr）
    # ------------------------------------------------------------------
    def _bots_list(self):
        """返回所有机器人卡片（掩码密钥）+ 每张的运行状态。"""
        hub = getattr(self, "qq_hub", None)
        if hub is None:
            return {"ok": True, "adapters": [], "primary": None}
        status_map = {s["id"]: s for s in hub.status()}
        adapters = self.conn_mgr.snapshot(mask=True)
        for card in adapters:
            st = status_map.get(str(card.get("id")), {})
            card["running"] = st.get("running", False)
            card["connected"] = st.get("connected", False)
            card["configured"] = st.get("configured", False)
        primary_id = None
        try:
            primary_id = str(self.conn_mgr.primary_qqofficial().get("id"))
        except Exception:
            pass
        return {"ok": True, "adapters": adapters, "primary": primary_id,
                "types": ["qqofficial", "websocket"]}

    def _bots_create(self, patch):
        """新建一张机器人卡片；成功后重载网关。"""
        if not isinstance(patch, dict):
            raise ValueError("数据格式不正确")
        adapter_type = str(patch.get("type", "") or "").strip()
        if adapter_type not in ("qqofficial", "websocket"):
            raise ValueError("type 只能为 qqofficial 或 websocket")
        created = self.conn_mgr.create(patch)
        self.logger.info(f"[机器人] 已新建卡片：{created.get('name')} ({adapter_type})")
        self._restart_qq_gateway()
        return {"ok": True, "adapter": self.conn_mgr.snapshot(mask=True),
                "created": {k: v for k, v in created.items() if k != "app_secret"}}

    def _bots_update(self, adapter_id, patch):
        """按 id 更新一张卡片；成功后重载网关。"""
        if not isinstance(patch, dict):
            raise ValueError("数据格式不正确")
        adapter_id = str(adapter_id or "").strip()
        if not adapter_id:
            raise ValueError("缺少卡片 id")
        updated = self.conn_mgr.update(adapter_id, patch)
        self.logger.info(f"[机器人] 已更新卡片：{updated.get('name')}")
        self._restart_qq_gateway()
        return {"ok": True, "adapter": updated}

    def _bots_toggle(self, adapter_id, enabled):
        """启用/禁用一张卡片；成功后重载网关。"""
        adapter_id = str(adapter_id or "").strip()
        if not adapter_id:
            raise ValueError("缺少卡片 id")
        enabled = bool(enabled)
        updated = self.conn_mgr.update(adapter_id, {"enabled": enabled})
        self.logger.info(f"[机器人] 已{'启用' if enabled else '禁用'}卡片：{updated.get('name')}")
        self._restart_qq_gateway()
        return {"ok": True, "adapter": updated}

    def _bots_delete(self, adapter_id):
        """删除一张卡片；成功后重载网关。"""
        adapter_id = str(adapter_id or "").strip()
        if not adapter_id:
            raise ValueError("缺少卡片 id")
        ok = self.conn_mgr.delete(adapter_id)
        if not ok:
            raise ValueError("卡片不存在")
        self.logger.info(f"[机器人] 已删除卡片：{adapter_id}")
        self._restart_qq_gateway()
        return {"ok": True}

    def _qqbot_public_config(self):

        primary, _ = self._qqbot_primary_adapter()
        if primary is None:
            return {"enabled": True, "config": dict(DEFAULT_QQBOT_CONFIG)}
        cfg = self.conn_mgr.to_flat_config(primary)
        pub = dict(cfg)
        if str(pub.get("app_secret", "")) not in ("", "__SET__"):
            pub["app_secret"] = "__SET__"
        admins = pub.get("group_admins", [])
        pub["group_admins"] = list(admins) if isinstance(admins, list) else []
        return {"enabled": bool(primary.get("enabled", True)), "config": pub}

    def _qqbot_status(self):
        mgr = getattr(self, "conn_mgr", None)
        primary, pid = self._qqbot_primary_adapter() if mgr is not None else (None, None)
        cfg = self.conn_mgr.to_flat_config(primary) if (mgr is not None and primary) else {}
        enabled = bool(primary.get("enabled", True)) if primary else False
        running = False
        connected = False
        binding_code = None
        hub = getattr(self, "qq_hub", None)
        if hub is not None and pid:
            gw = hub.gateway_by_id(pid)
            if gw is not None:
                running = True
                try:
                    connected = bool(gw._connected.is_set())
                except Exception:
                    connected = False
                try:
                    if not str(gw.config.get("group_openid", "") or "").strip():
                        binding_code = getattr(gw, "binding_code", None)
                except Exception:
                    pass
        groups = cfg.get("groups") or []
        if not isinstance(groups, list):
            groups = []
        group_list = []
        for g in groups:
            if isinstance(g, dict) and str(g.get("openid", "") or "").strip():
                group_list.append({
                    "openid": str(g["openid"]).strip(),
                    "name": str(g.get("name", "") or "").strip() or str(g["openid"]).strip(),
                })
        pending = cfg.get("pending_group_binds") or []
        if not isinstance(pending, list):
            pending = []
        pending_codes = []
        for p in pending:
            if isinstance(p, dict) and str(p.get("code", "") or "").strip():
                pending_codes.append({
                    "code": str(p["code"]).strip()[:4],
                    "ts": int(p.get("ts") or 0),
                })
        return {"enabled": enabled, "running": running, "connected": connected,
                "missing": [], "binding_code": binding_code,
                "group_openid": str(cfg.get("group_openid", "") or ""),
                "groups": group_list, "pending_codes": pending_codes}

    def _qqbot_persist_group_openid(self, group_openid):
        mgr = getattr(self, "conn_mgr", None)
        if mgr is None:
            path = self.data_dir / "qqbot.json"
            current = self._load_qqbot_config()
            current["group_openid"] = str(group_openid or "")
            try:
                path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except Exception as e:
                raise RuntimeError(f"写入配置失败: {e}")
            bot = getattr(self, "qq_bot", None)
            if bot is not None:
                try:
                    bot.config["group_openid"] = str(group_openid or "")
                except Exception:
                    pass
            return
        primary, pid = self._qqbot_primary_adapter()
        if primary is None:
            return
        primary["group_openid"] = str(group_openid or "")
        try:
            mgr.update(pid, {"group_openid": str(group_openid or "")})
        except Exception as e:
            raise RuntimeError(f"写入配置失败: {e}")
        gw = self._hub_gateway_for(pid)
        if gw is not None:
            try:
                gw.config["group_openid"] = str(group_openid or "")
            except Exception:
                pass

    def _hub_gateway_for(self, card_id):
        hub = getattr(self, "qq_hub", None)
        if hub is None or not card_id:
            return None
        return hub.gateway_by_id(card_id)

    def _qqbot_save_group_binds(self, live_config, keep_secret=False):
        mgr = getattr(self, "conn_mgr", None)
        primary, pid = self._qqbot_primary_adapter() if mgr is not None else (None, None)
        if primary is None or pid is None:
            return
        patch = {}
        goid = str((live_config or {}).get("group_openid", "") or primary.get("group_openid", ""))
        patch["group_openid"] = goid
        groups = (live_config or {}).get("groups")
        if isinstance(groups, list):
            patch["groups"] = groups
        pending = (live_config or {}).get("pending_group_binds")
        if isinstance(pending, list):
            patch["pending_group_binds"] = pending
        try:
            mgr.update(pid, patch)
        except Exception as e:
            raise RuntimeError(f"写入配置失败: {e}")
        gw = self._hub_gateway_for(pid)
        if gw is not None:
            try:
                gw.config["group_openid"] = str(patch.get("group_openid", "") or "")
                gw.config["groups"] = list(patch.get("groups") or [])
                gw.config["pending_group_binds"] = list(patch.get("pending_group_binds") or [])
            except Exception:
                pass

    def _qqbot_new_group_code(self):
        mgr = getattr(self, "conn_mgr", None)
        primary, pid = self._qqbot_primary_adapter() if mgr is not None else (None, None)
        if primary is None or pid is None:
            return f"{secrets.randbelow(10000):04d}"
        code = f"{secrets.randbelow(10000):04d}"
        pending = primary.get("pending_group_binds") or []
        if not isinstance(pending, list):
            pending = []
        pending = [p for p in pending if isinstance(p, dict)]
        pending.insert(0, {"code": code, "ts": int(time.time())})
        pending = pending[:20]
        try:
            mgr.update(pid, {"pending_group_binds": pending})
        except Exception as e:
            raise RuntimeError(f"写入配置失败: {e}")
        gw = self._hub_gateway_for(pid)
        if gw is not None:
            try:
                gw.config["pending_group_binds"] = list(pending)
            except Exception:
                pass
        return code

    def _qqbot_save_config(self, payload):

        if not isinstance(payload, dict):
            raise ValueError("数据格式不正确")
        mgr = getattr(self, "conn_mgr", None)
        primary, pid = self._qqbot_primary_adapter() if mgr is not None else (None, None)
        if primary is None or pid is None:
            raise RuntimeError("尚未初始化机器人配置")
        current = dict(DEFAULT_QQBOT_CONFIG)
        for k, v in primary.items():
            if k in ("type", "id", "name", "enabled", "sync"):
                continue
            current[k] = v
        for k, v in (primary.get("sync") or {}).items():
            current[k] = v
        current["enabled"] = bool(primary.get("enabled", True))
        bool_keys = ("enabled", "qq_to_mc", "mc_to_qq", "join_to_qq", "leave_to_qq",
                     "notify_start", "notify_stop")
        num_keys = ("reconnect_seconds", "reconnect_seconds_max", "send_rate_per_min",
                    "send_burst", "recv_rate_per_sec", "recv_burst", "max_bytes_per_sec")
        for k in DEFAULT_QQBOT_CONFIG:
            if k not in payload:
                continue
            v = payload[k]
            if k in bool_keys:
                current[k] = bool(v)
            elif k in num_keys:
                try:
                    current[k] = max(0, float(v or 0))
                except (TypeError, ValueError):
                    current[k] = DEFAULT_QQBOT_CONFIG[k]
            elif k == "custom_keywords":

                cleaned = {}
                if isinstance(v, dict):
                    items = v.items()
                elif isinstance(v, list):
                    items = []
                    for it in v:
                        if isinstance(it, dict):
                            items.append((it.get("keyword") or it.get("key"), it.get("reply") or it.get("value")))
                    items = [x for x in items if x[0] is not None]
                else:
                    items = []
                for kw, rep in items:
                    kw = str(kw or "").strip()
                    rep = str(rep or "").strip()
                    if kw and rep:
                        cleaned[kw] = rep
                current[k] = cleaned
            elif k == "group_admins":
                if isinstance(v, list):
                    current[k] = [str(x).strip() for x in v if str(x).strip()]
                else:
                    items = str(v or "").replace("，", ",").split(",")
                    current[k] = [x.strip() for x in items if x.strip()]
            elif k == "app_secret":
                if str(v) not in ("", "__SET__"):
                    current[k] = str(v)
            elif k == "groups":
                cleaned = []
                if isinstance(v, list):
                    for g in v:
                        if isinstance(g, dict) and str(g.get("openid", "") or "").strip():
                            cleaned.append({
                                "openid": str(g["openid"]).strip(),
                                "name": str(g.get("name", "") or "").strip() or str(g["openid"]).strip(),
                            })
                elif isinstance(v, str):
                    for part in str(v).replace("，", ",").split(","):
                        part = part.strip()
                        if part:
                            cell = part
                            nm = ""
                            if "=" in cell:
                                cell, nm = cell.split("=", 1)
                            cell = cell.strip()
                            if cell:
                                cleaned.append({"openid": cell, "name": nm.strip() or cell})
                current[k] = cleaned
            elif k == "pending_group_binds":
                if isinstance(v, list):
                    stream = []
                    for p in v:
                        if isinstance(p, dict) and str(p.get("code", "") or "").strip():
                            stream.append({
                                "code": str(p["code"]).strip()[:4],
                                "ts": int(p.get("ts") or 0),
                            })
                    current[k] = stream[:20]
            elif k == "banned_words":
                if isinstance(v, list):
                    out = []
                    for w in v:
                        w = str(w or "").strip()
                        if w:
                            out.append(w)
                    current[k] = out[:500]
                elif isinstance(v, str):
                    out = []
                    for w in str(v).replace("，", ",").split(","):
                        w = w.strip()
                        if w:
                            out.append(w)
                    current[k] = out[:500]
            else:
                current[k] = v
        # 扁平配置写回主卡片：sync 收纳互通类键，其余为卡片顶层键
        patch: dict[str, Any] = {}
        for k, v in current.items():
            if k in _FLAT_SYNC_KEYS and k not in ("enabled",):
                patch.setdefault("sync", {})[k] = v
            else:
                patch[k] = v
        try:
            mgr.update(pid, patch)
        except Exception as e:
            raise RuntimeError(f"写入配置失败: {e}")

        threading.Thread(target=self._restart_qq_gateway, daemon=True).start()
        return True, "配置已保存，网关正在后台重载"

    def _restart_qq_gateway(self):
        hub = getattr(self, "qq_hub", None)
        if hub is not None:
            self.logger.info("[QQ] 配置已更新，重新加载机器人网关")
            libs = _load_offline_wheels(self, self.libs_dir)
            threading.Thread(target=lambda: hub.restart(libs), daemon=True).start()
            return
        qq_bot = getattr(self, "qq_bot", None)
        if qq_bot is not None:
            try:
                qq_bot.stop()
            except Exception as e:
                self.logger.warning(f"[QQ] 停止旧网关失败: {e}")
            self.qq_bot = None
        self.logger.info("[QQ] 配置已更新，重新加载网关")
        self._init_qq_gateway()

    def _notify_server_start_once(self):
                                                                                                                                

                                                                                                                                      
           
        try:
            if getattr(self, "_qq_start_notified", False):
                return
            bot = getattr(self, "qq_bot", None)
            if bot is None or bot._supervisor_stop.is_set():
                return
            cfg = getattr(bot, "config", {}) or {}
            if not str(cfg.get("group_openid", "")).strip():
                return
            if not cfg.get("notify_start", True):
                return
            self._qq_start_notified = True
            bot.send_qq_text(str(cfg.get("server_start_msg", "服务器已上线")))
        except Exception as e:
            self.logger.warning(f"[QQ] 发送上线提示失败: {e}")

    def _notify_server_stop(self):

        try:
            bot = getattr(self, "qq_bot", None)
            if bot is None or bot._supervisor_stop.is_set():
                return
            cfg = getattr(bot, "config", {}) or {}
            if not str(cfg.get("group_openid", "")).strip():
                return
            if not cfg.get("notify_stop", True):
                return
            bot.send_qq_text(str(cfg.get("server_stop_msg", "服务器已断开")))
            time.sleep(1.5)
        except Exception as e:
            self.logger.warning(f"[QQ] 发送下线提示失败: {e}")

    @event_handler
    def on_player_chat(self, event: PlayerChatEvent):
        try:
            player_name = event.player.name
            original = event.message
        except Exception:
            player_name = "未知"
            original = ""

        if original:
            self._append_message("chat", player_name, original)

        bot = getattr(self, "qq_bot", None)
        if bot is not None and original:
            try:
                cfg = getattr(bot, "config", {}) or {}
                if cfg.get("mc_to_qq", True):
                    bot.send_qq_text(cfg.get("mc_to_qq_format", "[游戏] %s：%s") % (player_name, original))
            except Exception:
                pass

        hub = getattr(self, "qq_hub", None)
        if hub is not None and original:
            try:
                hub.websocket_relay("chat", player=player_name, message=original)
            except Exception:
                pass

        cross = getattr(self, "cross", None)
        if cross is not None and cross.enabled() and original:
            try:
                sname = str(cross.cfg.get("server_name", "") or "本服")
                cross.send_chat(sname, player_name, original)
            except Exception:
                pass

        if getattr(self, "fun_transform", True) and original:
            try:
                transformed = _cat_transform(original)
                if transformed and transformed != original:
                    try:
                        event.message = transformed
                    except Exception:
                        try:
                            event.set_message(transformed)
                        except Exception:
                            pass
            except Exception:
                pass

    def _cross_broadcast(self, msg):
        try:
            self.server.broadcast_message(msg)
            return
        except Exception:
            pass
        try:
            self.server.broadcast(msg)
        except Exception:
            pass

    def _apply_cross_whitelist(self, names):
        if not names:
            return
        try:
            current = set()
            try:
                current = set(getattr(self.server.whitelist, "names", None) or [])
            except Exception:
                current = set()
            target = set()
            for n in names:
                n = str(n).strip()
                if n:
                    target.add(n)
            for n in (target - current):
                try:
                    self._run_in_server_thread(self._exec_allowlist_cmd, n, "add", timeout=2.0)
                except Exception:
                    pass
            for n in (current - target):
                try:
                    self._run_in_server_thread(self._exec_allowlist_cmd, n, "remove", timeout=2.0)
                except Exception:
                    pass
        except Exception as e:
            self.logger.warning(f"[跨服] 应用白名单失败: {e}")

    def _exec_allowlist_cmd(self, name, op):
        cmd = f"allowlist add {name}" if op == "add" else f"allowlist remove {name}"
        try:
            self.server.dispatch_command(self.server.command_sender, cmd)
        except Exception:
            pass

    def _apply_cross_bans(self, bans):
        try:
            current = set()
            try:
                bl = getattr(self.server, "bans", None)
                if bl is not None:
                    for b in bl.get_entries():
                        try:
                            current.add(str(b.name))
                        except Exception:
                            pass
            except Exception:
                current = set()
            target = set(bans.keys()) if isinstance(bans, dict) else set()
            for n in (target - current):
                try:
                    self._run_in_server_thread(self._exec_ban_cmd, n, "add", timeout=2.0)
                except Exception:
                    pass
            for n in (current - target):
                try:
                    self._run_in_server_thread(self._exec_ban_cmd, n, "remove", timeout=2.0)
                except Exception:
                    pass
        except Exception as e:
            self.logger.warning(f"[跨服] 应用封禁失败: {e}")

    def _exec_ban_cmd(self, name, op):
        cmd = f"ban {name}" if op == "add" else f"unban {name}"
        try:
            self.server.dispatch_command(self.server.command_sender, cmd)
        except Exception:
            pass

    def _cross_snapshot_item(self, stack, who=None):
        try:
            it = getattr(stack, "type", None)
            if it is None:
                it = getattr(stack, "item_type", None)
            tname = None
            if it is not None:
                try:
                    tname = str(getattr(it, "id", None) or "")
                except Exception:
                    tname = None
                if not tname:
                    try:
                        tname = str(it)
                    except Exception:
                        tname = None
                if not tname:
                    try:
                        tname = getattr(it, "name", None)
                    except Exception:
                        tname = None
            if not tname:
                try:
                    tname = str(getattr(stack, "type", None) or "")
                except Exception:
                    tname = None
            if not tname:
                return None
            nbt = None
            nbt_err = None
            try:
                nb = getattr(stack, "nbt", None)
                if nb is not None:
                    nbt = self._nbt_serialize(nb)
            except Exception as e:
                nbt = None
                nbt_err = f"{e}"
            if "bundle" in str(tname):
                keys = ""
                has_items = "no"
                if isinstance(nbt, dict) and isinstance(nbt.get("d"), dict):
                    keys = ",".join(str(k) for k in nbt["d"].keys())
                    if "Items" in nbt["d"]:
                        has_items = "yes"
                who_s = (" player=" + str(who)) if who else ""
                self.logger.warning(
                    "[Synced][bundle] snapshot type=" + str(tname) + who_s
                    + " nbt_keys=[" + (keys or "none") + "]"
                    + " contains_items=" + has_items
                )
            return {
                "t": tname,
                "c": int(getattr(stack, "amount", 1) or 1),
                "data": int(getattr(stack, "data", 0) or 0),
                "nbt": nbt,
            }
        except Exception:
            return None

    def _cross_snapshot_inventory(self, player):
        items = []
        inv = getattr(player, "inventory", None)
        if inv is None:
            return items
        who = getattr(player, "name", None)
        try:
            for i in range(40):
                try:
                    stack = inv.get_item(i)
                except Exception:
                    break
                if stack is None or getattr(stack, "amount", 0) <= 0:
                    continue
                d = self._cross_snapshot_item(stack, who=who) if who else self._cross_snapshot_item(stack)
                if d:
                    d["slot"] = i
                    items.append(d)
        except Exception:
            pass
        return items

    def _cross_snapshot_ender_chest(self, player):
        items = []
        inv = None
        try:
            inv = getattr(player, "ender_chest", None)
        except Exception:
            inv = None
        if inv is None:
            return items
        who = getattr(player, "name", None)
        try:
            size = int(getattr(inv, "size", None) or 27)
            for i in range(size):
                try:
                    stack = inv.get_item(i)
                except Exception:
                    break
                if stack is None or getattr(stack, "amount", 0) <= 0:
                    continue
                d = self._cross_snapshot_item(stack, who=who) if who else self._cross_snapshot_item(stack)
                if d:
                    d["slot"] = i
                    items.append(d)
        except Exception:
            pass
        return items

    def _cross_snapshot_effects(self, player):
        out = []
        try:
            acts = getattr(player, "active_effects", None)
            if acts is None:
                try:
                    acts = getattr(player, "get_active_effects")()
                except Exception:
                    acts = None
            for ef in (acts or []):
                try:
                    tn = str(getattr(ef, "type", "") or "")
                    if not tn:
                        continue
                    amp = int(getattr(ef, "amplifier", 0) or 0)
                    dur = getattr(ef, "duration", None)
                    if dur is None or getattr(ef, "infinite", False):
                        dur = -1
                    else:
                        dur = int(dur or 0)
                    out.append({"t": tn, "a": amp, "d": dur})
                except Exception:
                    continue
        except Exception:
            pass
        return out

    @staticmethod
    def _player_attr_float(player, attr_name, attr_key):
        try:
            v = getattr(player, attr_name, None)
            if v is not None:
                return float(v)
        except Exception:
            pass
        try:
            from endstone import Attribute
            ak = getattr(Attribute, attr_key, None)
            if ak is not None:
                av = getattr(player, "get_attribute", None)
                if av is not None:
                    inst = av(ak)
                    if inst is not None:
                        try:
                            return float(getattr(inst, "value", None))
                        except Exception:
                            pass
        except Exception:
            pass
        return None

    def _cross_snapshot_player(self, player):
        try:
            loc = getattr(player, "location", None)
            pos = None
            if loc is not None:
                try:
                    pos = {"x": loc.x, "y": loc.y, "z": loc.z, "dim": str(getattr(loc, "dimension", "") or getattr(player, "dimension", "") or "")}
                except Exception:
                    pos = None
            inv = []
            ec = []
            eff = []
            try:
                inv = self._cross_snapshot_inventory(player)
            except Exception:
                inv = []
            try:
                ec = self._cross_snapshot_ender_chest(player)
            except Exception:
                ec = []
            try:
                eff = self._cross_snapshot_effects(player)
            except Exception:
                eff = []
            xp = getattr(player, "exp_level", None)
            if xp is None:
                lv = getattr(player, "level", None)
                if isinstance(lv, (int, float)) and not hasattr(lv, "x"):
                    xp = lv
            return {
                "health": getattr(player, "health", None),
                "max_health": getattr(player, "max_health", None),
                "absorption": self._player_attr_float(player, "absorption", "ABSORPTION"),
                "food": self._player_attr_float(player, "hunger", "PLAYER_HUNGER"),
                "saturation": self._player_attr_float(player, "saturation", "PLAYER_SATURATION"),
                "game_mode": str(getattr(player, "game_mode", "") or ""),
                "xp": xp,
                "xp_progress": getattr(player, "exp_progress", None),
                "total_xp": getattr(player, "total_exp", None),
                "walk_speed": getattr(player, "walk_speed", None),
                "position": pos,
                "inventory": inv,
                "ender_chest": ec,
                "effects": eff,
            }
        except Exception:
            return {}

    def _effect_cls(self):
        if getattr(self, "_effect_cls_cache", None) is None:
            cls = None
            for mod in ("endstone.potion", "endstone.effect"):
                try:
                    m = __import__(mod, fromlist=["Effect"])
                    cls = getattr(m, "Effect")
                    if cls:
                        break
                except Exception:
                    cls = None
            self._effect_cls_cache = cls
        return self._effect_cls_cache

    def _nbt_serialize(self, tag):
        if tag is None:
            return None
        try:
            cls = type(tag).__name__
            if cls == "CompoundTag":
                d = {}
                try:
                    for k, v in tag.items():
                        d[str(k)] = self._nbt_serialize(v)
                except Exception:
                    try:
                        for k in list(tag.keys()):
                            d[str(k)] = self._nbt_serialize(tag[k])
                    except Exception:
                        pass
                return {"T": "C", "d": d}
            if cls == "ListTag":
                lst = []
                try:
                    n = tag.size()
                except Exception:
                    n = 0
                for i in range(n):
                    try:
                        lst.append(self._nbt_serialize(tag[i]))
                    except Exception:
                        lst.append(None)
                return {"T": "L", "d": lst}
            if cls == "ByteArrayTag":
                try:
                    return {"T": "BA", "d": base64.b64encode(bytes(tag)).decode("ascii")}
                except Exception:
                    return {"T": "BA", "d": ""}
            if cls == "IntArrayTag":
                try:
                    return {"T": "IA", "d": [int(x) for x in tag]}
                except Exception:
                    return {"T": "IA", "d": []}
            if cls in ("ByteTag", "ShortTag", "IntTag", "LongTag"):
                tc = {"ByteTag": "B", "ShortTag": "S", "IntTag": "I", "LongTag": "L"}[cls]
                try:
                    return {"T": tc, "d": int(tag)}
                except Exception:
                    return {"T": tc, "d": 0}
            if cls in ("FloatTag", "DoubleTag"):
                tc = {"FloatTag": "F", "DoubleTag": "D"}[cls]
                try:
                    return {"T": tc, "d": float(tag)}
                except Exception:
                    return {"T": tc, "d": 0.0}
            if cls == "StringTag":
                try:
                    return {"T": "s", "d": str(tag.value)}
                except Exception:
                    return {"T": "s", "d": ""}
        except Exception:
            return None
        return None

    def _nbt_deserialize(self, obj):
        if obj is None:
            return None
        if not isinstance(obj, dict):
            return None
        if "T" not in obj:
            return self._plain_to_nbt(obj)
        t = obj.get("T")
        d = obj.get("d")
        try:
            from endstone.nbt import (CompoundTag, ListTag, ByteTag, ShortTag, IntTag,
                                      LongTag, FloatTag, DoubleTag, StringTag,
                                      ByteArrayTag, IntArrayTag)
        except Exception:
            return None
        try:
            if t == "C" and isinstance(d, dict):
                comp = {}
                for k, v in d.items():
                    tv = self._nbt_deserialize(v)
                    if tv is not None:
                        comp[str(k)] = tv
                return CompoundTag(comp)
            if t == "L" and isinstance(d, list):
                lt = ListTag()
                for x in d:
                    tv = self._nbt_deserialize(x)
                    if tv is not None:
                        try:
                            lt.append(tv)
                        except Exception:
                            pass
                return lt
            if t == "BA" and isinstance(d, str):
                try:
                    return ByteArrayTag(base64.b64decode(d))
                except Exception:
                    return None
            if t == "IA" and isinstance(d, list):
                try:
                    return IntArrayTag([int(x) for x in d])
                except Exception:
                    return None
            if t in ("B", "S", "I", "L"):
                v = int(d or 0)
                return {"B": ByteTag, "S": ShortTag, "I": IntTag, "L": LongTag}[t](v)
            if t in ("F", "D"):
                v = float(d or 0.0)
                return DoubleTag(v) if t == "D" else FloatTag(v)
            if t == "s":
                return StringTag(str(d or ""))
        except Exception:
            return None
        return None

    def _plain_to_nbt(self, value):
        try:
            from endstone.nbt import (CompoundTag, ListTag, ByteTag, ShortTag, IntTag,
                                      LongTag, FloatTag, DoubleTag, StringTag,
                                      ByteArrayTag, IntArrayTag)
        except Exception:
            return None
        try:
            if isinstance(value, dict):
                comp = {}
                for k, v in value.items():
                    tv = self._plain_to_nbt(v)
                    if tv is not None:
                        comp[str(k)] = tv
                return CompoundTag(comp)
            if isinstance(value, list):
                items = [self._plain_to_nbt(x) for x in value]
                items = [x for x in items if x is not None]
                return ListTag(items)
            if isinstance(value, bool):
                return ByteTag(1 if value else 0)
            if isinstance(value, int):
                if -(2 ** 31) <= value < 2 ** 31:
                    return IntTag(value)
                return LongTag(value)
            if isinstance(value, float):
                return DoubleTag(value)
            if isinstance(value, str):
                return StringTag(value)
            if isinstance(value, bytes):
                return ByteArrayTag(value)
        except Exception:
            return None
        return None

    def _cross_restore_item(self, entry):
        if not isinstance(entry, dict):
            return None
        tname = entry.get("t")
        if not tname:
            return None
        try:
            c = int(entry.get("c") or 1)
        except Exception:
            c = 1
        try:
            data = int(entry.get("data") or 0)
        except Exception:
            data = 0
        try:
            from endstone.inventory import ItemType
            it = ItemType.get(tname)
            if it is None:
                return None
            stack = it.create_item_stack(c)
        except Exception:
            return None
        try:
            stack.data = data
        except Exception:
            pass
        try:
            nbt = entry.get("nbt")
            if isinstance(nbt, dict) and nbt:
                stack.nbt = self._nbt_deserialize(nbt)
                if "bundle" in str(tname):
                    keys = ""
                    if isinstance(nbt.get("d"), dict):
                        keys = ",".join(str(k) for k in nbt["d"].keys())
                    self.logger.warning(
                        "[Synced][bundle] restore type=" + str(tname)
                        + " applied nbt_keys=[" + (keys or "none") + "]"
                    )
        except Exception as e:
            if "bundle" in str(tname):
                self.logger.warning(
                    "[Synced][bundle] restore FAILED type=" + str(tname) + " err=" + str(e)
                )
        return stack

    def _clear_inventory(self, inv):
        if inv is None:
            return
        try:
            inv.clear()
            return
        except Exception:
            pass
        try:
            from endstone import ItemStack
            size = int(getattr(inv, "size", None) or 40)
        except Exception:
            size = 0
        if size <= 0:
            return
        try:
            empty = ItemStack()
        except Exception:
            empty = None
        for i in range(size):
            try:
                inv.set_item(i, empty)
            except Exception:
                pass

    def _cross_restore_slots(self, inv, entries):
        if inv is None or not isinstance(entries, list):
            return
        self._clear_inventory(inv)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                slot = int(entry.get("slot"))
            except Exception:
                continue
            stack = self._cross_restore_item(entry)
            if stack is None:
                continue
            try:
                inv.set_item(slot, stack)
            except Exception:
                pass

    def _cross_restore_ender_chest(self, player, data):
        try:
            inv = getattr(player, "ender_chest", None)
        except Exception:
            inv = None
        if inv is None:
            return
        self._cross_restore_slots(inv, data)

    def _cross_restore_effects(self, player, data):
        Effect = self._effect_cls()
        if Effect is None or not isinstance(data, list):
            return
        for e in data:
            try:
                tn = e.get("t")
                if not tn:
                    continue
                try:
                    a = int(e.get("a") or 0)
                except Exception:
                    a = 0
                try:
                    d = int(e.get("d") or -1)
                except Exception:
                    d = -1
                eff = Effect(tn, None if d < 0 else d, a)
                player.add_effect(eff)
            except Exception:
                continue

    def _cross_restore_player(self, player, data):
        if not isinstance(data, dict):
            return
        try:
            h = data.get("health")
            if h is not None:
                try:
                    player.health = float(h)
                except Exception:
                    pass
            mh = data.get("max_health")
            if mh is not None:
                try:
                    player.max_health = float(mh)
                except Exception:
                    pass
            ab = data.get("absorption")
            if ab is not None:
                try:
                    player.absorption = float(ab)
                except Exception:
                    pass
            fd = data.get("food")
            if fd is not None:
                try:
                    player.hunger = float(fd)
                except Exception:
                    pass
            sat = data.get("saturation")
            if sat is not None:
                try:
                    player.saturation = float(sat)
                except Exception:
                    pass
            xp = data.get("xp")
            if xp is not None:
                try:
                    player.exp_level = int(xp)
                except Exception:
                    try:
                        player.level = int(xp)
                    except Exception:
                        pass
            prog = data.get("xp_progress")
            if prog is not None:
                try:
                    player.exp_progress = float(prog)
                except Exception:
                    pass
            tx = data.get("total_xp")
            if tx is not None:
                try:
                    player.total_exp = int(tx)
                except Exception:
                    pass
            gm = data.get("game_mode")
            if gm:
                try:
                    player.game_mode = gm
                except Exception:
                    pass
        except Exception:
            pass
        try:
            pos = data.get("position")
            if isinstance(pos, dict) and pos.get("x") is not None:
                player.teleport((pos["x"], pos["y"], pos["z"]))
        except Exception:
            pass
        try:
            inv = data.get("inventory")
            if isinstance(inv, list):
                self._cross_restore_slots(getattr(player, "inventory", None), inv)
        except Exception:
            pass
        try:
            self._cross_restore_ender_chest(player, data.get("ender_chest"))
        except Exception:
            pass
        try:
            self._cross_restore_effects(player, data.get("effects"))
        except Exception:
            pass

    def _warn_bundle_on_join(self, player):
        try:
            cross = getattr(self, "cross", None)
            if cross is None or not cross.enabled():
                return
            inv = getattr(player, "inventory", None)
            if inv is None:
                return
            has_bundle = False
            for i in range(40):
                try:
                    st = inv.get_item(i)
                except Exception:
                    break
                if st is None or getattr(st, "amount", 0) <= 0:
                    continue
                tname = ""
                try:
                    it = getattr(st, "type", None)
                    tname = str(getattr(it, "id", None) or "")
                except Exception:
                    tname = ""
                if "bundle" in tname:
                    has_bundle = True
                    break
            if not has_bundle:
                return
            msg = "§c[跨服] §e检测到你身上有收纳袋：跨服时袋内物品不会同步，请在切换服务器前先把袋子里的物品取出来。"
            try:
                player.send_message(msg)
            except Exception:
                try:
                    player.send_tip(msg)
                except Exception:
                    try:
                        player.send_popup(msg)
                    except Exception:
                        pass
        except Exception:
            pass

    def _cross_restore_async(self, player, uuid):
        try:
            cross = getattr(self, "cross", None)
            if cross is None:
                return
            snap = cross.get_player_data(uuid)
            if not snap:
                return
            try:
                self._run_in_server_thread(self._cross_restore_player, player, snap, timeout=4.0)
            except Exception:
                pass
        except Exception:
            pass

    def _cross_put_async(self, uuid, name, snap):
        try:
            cross = getattr(self, "cross", None)
            if cross is not None:
                cross.put_player_data(uuid, name, snap)
        except Exception:
            pass

    def _cross_push_whitelist_now(self):
        try:
            cross = getattr(self, "cross", None)
            if cross is None or not cross.enabled():
                return
            names = []
            try:
                names = list(getattr(self.server.whitelist, "names", None) or [])
            except Exception:
                names = []
            cross.push_whitelist(names)
        except Exception as e:
            self.logger.warning(f"[跨服] 白名单推送失败: {e}")

    def _cross_push_bans_now(self):
        try:
            cross = getattr(self, "cross", None)
            if cross is None or not cross.enabled():
                return
            entries = {}
            try:
                bl = getattr(self.server, "bans", None)
                if bl is not None:
                    for b in bl.get_entries():
                        try:
                            entries[str(b.name)] = True
                        except Exception:
                            pass
            except Exception:
                entries = {}
            cross.push_bans(entries)
        except Exception as e:
            self.logger.warning(f"[跨服] 封禁推送失败: {e}")

    def _cross_push_whitelist(self):
        try:
            threading.Thread(target=self._cross_push_whitelist_now, daemon=True).start()
        except Exception:
            pass

    def _cross_push_bans(self):
        try:
            threading.Thread(target=self._cross_push_bans_now, daemon=True).start()
        except Exception:
            pass

    @event_handler
    def on_broadcast(self, event: BroadcastMessageEvent):
        try:
            msg = event.message
            if isinstance(msg, str):
                self._append_message("broadcast", "服务器", msg)
        except Exception:
            pass

    @event_handler
    def on_player_join(self, event: PlayerJoinEvent):
        try:
            self._append_message("join", event.player.name, "加入了服务器")
        except Exception:
            pass

        try:
            self._maybe_cloud_check(event.player)
        except Exception:
            pass

        bot = getattr(self, "qq_bot", None)
        if bot is not None:
            try:
                cfg = getattr(bot, "config", {}) or {}
                if cfg.get("join_to_qq", True):
                    bot.send_qq_text(cfg.get("join_format", "[游戏] %s 加入了服务器") % event.player.name)
            except Exception:
                pass

        hub = getattr(self, "qq_hub", None)
        if hub is not None:
            try:
                hub.websocket_relay("join", name=event.player.name)
            except Exception:
                pass

        cross = getattr(self, "cross", None)
        if cross is not None and cross.enabled() and cross.interop("player_data"):
            try:
                p = event.player
                uuid = str(getattr(p, "unique_id", None) or getattr(p, "uuid", None) or "")
                if uuid:
                    threading.Thread(target=self._cross_restore_async, args=(p, uuid), daemon=True).start()
                    threading.Thread(target=self._cross_put_async, args=(uuid, p.name, self._cross_snapshot_player(p)), daemon=True).start()
                try:
                    self._warn_bundle_on_join(p)
                except Exception:
                    pass
            except Exception:
                pass

        cross = getattr(self, "cross", None)
        if cross is not None and cross.enabled() and cross.interop("chat"):
            try:
                cross.broadcast_sys(f"§a{event.player.name} §f加入了服务器")
            except Exception:
                pass

        try:
            self._maybe_handle_join_binding(event.player)
        except Exception:
            pass

        try:
            self._process_delayed_tasks()
        except Exception:
            pass

    @event_handler
    def on_player_quit(self, event: PlayerQuitEvent):
        try:
            self._append_message("quit", event.player.name, "离开了服务器")
        except Exception:
            pass

        bot = getattr(self, "qq_bot", None)
        if bot is not None:
            try:
                cfg = getattr(bot, "config", {}) or {}
                if cfg.get("leave_to_qq", True):
                    bot.send_qq_text(cfg.get("leave_format", "[游戏] %s 离开了服务器") % event.player.name)
            except Exception:
                pass

        hub = getattr(self, "qq_hub", None)
        if hub is not None:
            try:
                hub.websocket_relay("leave", name=event.player.name)
            except Exception:
                pass

        cross = getattr(self, "cross", None)
        if cross is not None and cross.enabled() and cross.interop("player_data"):
            try:
                p = event.player
                uuid = str(getattr(p, "unique_id", None) or getattr(p, "uuid", None) or "")
                if uuid:
                    snap = self._cross_snapshot_player(p)
                    threading.Thread(target=self._cross_put_async, args=(uuid, p.name, snap), daemon=True).start()
            except Exception:
                pass

        cross = getattr(self, "cross", None)
        if cross is not None and cross.enabled() and cross.interop("chat"):
            try:
                cross.broadcast_sys(f"§c{event.player.name} §f离开了服务器")
            except Exception:
                pass

    def _append_message(self, mtype: str, player_name: str, message: str):
        try:
            with self._messages_lock:
                self._message_seq += 1
                self._messages.append({
                    "id": self._message_seq,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "type": mtype,
                    "player": player_name,
                    "message": message,
                })
                if len(self._messages) > 200:
                    self._messages = self._messages[-200:]
        except Exception:
            pass

    def _resolve_data_dir(self) -> Path:

        try:
            p = self.data_folder
            if p:
                return Path(p)
        except Exception:
            pass
        return Path("plugins/endstone-greenmoon-panel/data")

    def _load_config(self):
        config_path = self.data_dir / "config.json"
        default_config = {
            "host": "0.0.0.0",
            "port": 8080,
            "fun_transform": True,
            "rate_limit": True,
            "rate_min_interval": 0.05,
            "rate_window_seconds": 10,
            "rate_max_per_window": 500,
            "rate_global_max": 2500,

            "admin_user": "",
            "admin_pass": "",
            "temp_accounts": [],
            "test_mode": True,
            "security_whitelist": {"enabled": False, "ips": []},
        }
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    return {**default_config, **loaded}
            except Exception as e:
                self.logger.error(f"加载配置文件失败: {e}，使用默认配置")
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(default_config, f, indent=2)
        except Exception:
            pass
        return default_config

    def _persist_config(self, cfg: dict = None):

        target = cfg if cfg is not None else self.web_config
        config_path = self.data_dir / "config.json"
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(target, f, ensure_ascii=False, indent=2)
            if cfg is not None:
                self.web_config = cfg
        except Exception as e:
            self.logger.error(f"保存 config.json 失败: {e}")

    def _load_tools_config(self) -> Dict[str, Any]:
        path = self.data_dir / "tools_config.json"
        default = json.loads(json.dumps(DEFAULT_TOOLS_CONFIG))
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for section in ("backup", "cloud_blacklist"):
                    lv = loaded.get(section)
                    if isinstance(lv, dict):
                        dv = default.get(section, {})
                        for k, v in lv.items():
                            if k in dv:
                                dv[k] = v
                return default
            except Exception as e:
                self.logger.error(f"读取 tools_config.json 失败: {e}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, indent=2)
        except Exception:
            pass
        return default

    def _tools_public_config(self) -> Dict[str, Any]:
        cfg = self._load_tools_config()
        public = json.loads(json.dumps(cfg))
        tok = str(public.get("cloud_blacklist", {}).get("token", ""))

        public["cloud_blacklist"]["token"] = "__SET__" if tok else ""
        return public

    def _save_tools_config(self, payload: Dict[str, Any]):
        if not isinstance(payload, dict):
            return False, "数据格式不正确"
        current = self._load_tools_config()
        for section in ("backup", "cloud_blacklist", "binding", "signin", "mods"):
            p = payload.get(section)
            if not isinstance(p, dict):
                continue
            default_d = DEFAULT_TOOLS_CONFIG.get(section, {})
            d = current.get(section, {})
            for k, v in p.items():
                if k not in d:
                    continue

                if section == "cloud_blacklist" and k == "token" and (v == "" or v == "__SET__"):
                    continue
                if k in default_d and isinstance(default_d[k], bool):
                    v = bool(v)
                d[k] = v
        try:
            with open(self.data_dir / "tools_config.json", "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
        except Exception as e:
            return False, f"保存失败: {e}"

        old_en = bool(current.get("backup", {}).get("enabled"))
        new_en = old_en
        _b = payload.get("backup")
        if isinstance(_b, dict) and "enabled" in _b:
            new_en = bool(_b["enabled"])
        try:
            next_run = self._backup_next_run_ts(current.get("backup", {}))
        except Exception:
            next_run = self._backup_state.get("next_run")

        if new_en and not old_en:
            next_run = time.time()
        with self._tools_lock:
            st = self._backup_state
            if new_en and not old_en:
                st["last_finished_ts"] = 0.0
            st["next_run"] = next_run
        msg = "已保存"
        if new_en and not old_en:
            msg += "（自动备份已启用，约 1 分钟内触发首次备份）"
        return True, msg

    def _backup_dir(self, cfg: Dict[str, Any] = None) -> Path:

        return self.data_dir / "backups"

    def _backup_sources(self, cfg: Dict[str, Any]) -> List[str]:
        srcs: List[str] = []
        raw = cfg.get("source_paths")
        if isinstance(raw, list):
            srcs = [str(x).strip() for x in raw if str(x).strip()]
        elif isinstance(raw, str) and raw.strip():
            srcs = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not srcs:

            srcs = self._locate_current_world()
        return srcs

    def _locate_current_world(self) -> List[str]:

        try:
            root = self._find_server_root() or Path.cwd()
        except Exception:
            root = Path.cwd()

        level_name = None
        try:
            level_name = getattr(self.server.level, "name", None)
        except Exception:
            level_name = None
        world_list = []
        lvl_from_props = None
        try:
            world_list, lvl_from_props, _ = self._list_worlds()
        except Exception:
            world_list = []
        if not level_name:
            level_name = lvl_from_props or None

        if level_name:
            for cand in (root / "worlds" / str(level_name), root / str(level_name)):
                if cand.is_dir():
                    self.logger.info(f"[备份] 世界定位: root={root} level_name={level_name} 命中目录={cand}")
                    return [str(cand)]

        def _lv_mtime(w):
            try:
                p = Path(w["path"]) / "level.dat"
                return p.stat().st_mtime if p.exists() else 0
            except Exception:
                return 0
        ordered = sorted(world_list, key=_lv_mtime, reverse=True)
        pick = None
        if level_name:
            ln = str(level_name).lower()
            for w in ordered:
                if str(w["name"]).lower() == ln:
                    pick = w
                    break
        if pick is None and ordered:
            pick = ordered[0]
        if pick is not None:
            self.logger.info(f"[备份] 世界定位: root={root} level_name={level_name} 候选={[w['name'] for w in ordered]} 选中={pick['path']}")
            return [pick["path"]]

        for base in (root / "worlds", root):
            try:
                if not base.is_dir():
                    continue
                dirs = [d for d in base.iterdir()
                        if d.is_dir() and not d.name.startswith(".")
                        and (d / "level.dat").exists()]
                if dirs:

                    dirs.sort(key=lambda d: (Path(d) / "level.dat").stat().st_mtime, reverse=True)
                    self.logger.info(f"[备份] 世界定位(兜底): root={root} 世界={[d.name for d in dirs]} 选中={dirs[0]}")
                    return [str(dirs[0])]
            except Exception:
                continue
        return []

    @staticmethod
    def _path_under(path, parent) -> bool:
        try:
            a = os.path.abspath(path)
            b = os.path.abspath(parent)
            if a == b:
                return True
            return os.path.commonpath([a, b]) == b if b else False
        except Exception:
            return False

    def _backup_worker(self):
        try:
            cfg = self._load_tools_config().get("backup", {})
            bdir = self._backup_dir(cfg)
            bdir.mkdir(parents=True, exist_ok=True)
            srcs = self._backup_sources(cfg)
            if not srcs:
                raise RuntimeError("备份源为空（source_paths 为空且无法定位世界目录）")
            self.logger.info(f"[备份] 本次备份源: {srcs}")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"backup_{stamp}.zip"
            fpath = bdir / fname
            count = 0

            with zipfile.ZipFile(str(fpath), "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for src in srcs:
                    sp = Path(src)
                    if not sp.exists():
                        continue
                    if sp.is_file():
                        zf.write(str(sp), os.path.join(sp.name, sp.name))
                        count += 1
                        continue
                    for base, dirs, files in os.walk(str(sp)):

                        if self._path_under(base, str(bdir)):
                            dirs[:] = []
                            continue
                        dirs[:] = [d for d in dirs if d != "__pycache__"]
                        for fn in files:
                            full = os.path.join(base, fn)
                            arc = os.path.join(sp.name, os.path.relpath(full, str(sp)))
                            try:
                                zf.write(full, arc)
                                count += 1
                            except Exception:
                                continue
            if count == 0:
                fpath.unlink(missing_ok=True)
                raise RuntimeError("备份目录中没有可打包的文件")
            self._prune_backups(bdir, int(cfg.get("max_keep", 10)) or 10)
            size = fpath.stat().st_size
            src_label = ";".join(str(Path(s).name).strip() for s in srcs[:3])
            msg = f"备份完成：{fname}（源：{src_label or '?'}，{count} 个文件，{_fmt_size(size)}）"
            self._set_backup_state(True, msg, fname)
        except Exception as e:
            msg = f"备份失败：{e}"
            self.logger.error(f"[备份] {msg}")
            self._set_backup_state(False, msg, None)

    def _set_backup_state(self, ok: bool, message: str, fname):
        try:
            cfg = self._load_tools_config().get("backup", {})

            next_run = self._backup_next_run_ts(cfg)
            with self._backup_lock:
                st = self._backup_state
                st["running"] = False
                st["started_ts"] = 0.0
                st["last_result"] = [bool(ok), str(message), fname, time.time()]
                st["last_finished_ts"] = time.time()
                st["next_run"] = next_run
        except Exception as e:
            self.logger.error(f"[备份] 更新备份状态失败: {e}")

    def _backup_next_run_ts(self, cfg: Dict[str, Any]) -> float:
        hours = max(0.25, float(cfg.get("interval_hours", 24) or 24))
        span = hours * 3600.0
        with self._backup_lock:
            last = self._backup_state.get("last_finished_ts") or 0.0
        base = last if last else time.time()
        return base + span

    def _backup_tick(self):

        try:
            with self._backup_lock:
                st = self._backup_state
                if st.get("running"):

                    st0 = st.get("started_ts") or 0.0
                    if 0 < st0 < time.time() - 1800:
                        self.logger.warning("[备份] 检测到备份任务长时间未完成，看门狗自动复位")
                        st["running"] = False
                        st["last_result"] = [False, "备份超时已复位", None, time.time()]
                    else:
                        return
                if not st.get("next_run"):
                    st["next_run"] = time.time() + 3600
            cfg = self._load_tools_config().get("backup", {})
            if not cfg.get("enabled"):
                return
            with self._backup_lock:
                due = time.time() >= (self._backup_state.get("next_run") or 0)
            if due:
                self._trigger_backup()
        except Exception:
            pass

    def _trigger_backup(self) -> bool:
        with self._backup_lock:
            if self._backup_state.get("running"):
                return False
            self._backup_state["running"] = True
            self._backup_state["last_result"] = None
            self._backup_state["started_ts"] = time.time()
        try:
            self._executor.submit(self._backup_worker)
            self.logger.info("[备份] 已在后台启动备份任务")
            return True
        except Exception as e:
            self.logger.error(f"[备份] 无法提交备份任务: {e}")
            with self._backup_lock:
                self._backup_state["running"] = False
            return False

    def _get_backup_state(self) -> Dict[str, Any]:
        try:
            with self._backup_lock:
                return {
                    "running": self._backup_state.get("running"),
                    "last_result": self._backup_state.get("last_result"),
                    "last_finished_ts": self._backup_state.get("last_finished_ts"),
                    "next_run": self._backup_state.get("next_run"),
                }
        except Exception:
            return {}

    def _list_backups(self) -> List[Dict[str, Any]]:
        cfg = self._load_tools_config().get("backup", {})
        bdir = self._backup_dir(cfg)
        items: List[Dict[str, Any]] = []
        if bdir.exists():
            for p in sorted(bdir.glob("backup_*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    st = p.stat()
                    items.append({
                        "name": p.name,
                        "size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except Exception:
                    continue
        return items

    def _delete_backup(self, name: str):
        name = str(name or "").strip()
        if not name.endswith(".zip") or not name.startswith("backup_"):
            return False, "非法文件名"
        cfg = self._load_tools_config().get("backup", {})
        bdir = self._backup_dir(cfg)
        target = (bdir / name).resolve()
        if not target.is_file() or not self._path_under(str(target), str(bdir)):
            return False, "文件不存在或路径越界"
        try:
            target.unlink()
            return True, f"已删除 {name}"
        except Exception as e:
            return False, f"删除失败: {e}"

    def _prune_backups(self, bdir: Path, max_keep: int):
        if max_keep <= 0:
            return
        try:
            files = sorted(bdir.glob("backup_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[max_keep:]:
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def _restore_backup(self, name: str):
                                                                                                                   
                                                                                                             
        name = str(name or "").strip()
        if not name.endswith(".zip") or not name.startswith("backup_"):
            return False, "非法备份文件名"
        try:
            bdir = self._backup_dir()
            src = (bdir / name).resolve()
            if not src.is_file() or not self._path_under(str(src), str(bdir)):
                return False, "备份文件不存在或路径越界"
            root = self._find_server_root() or Path.cwd()
            dest = root / name
            shutil.copy2(str(src), str(dest))

            _, content = self._read_properties_raw()
            current = self._get_property_value(content, "level-name") or "当前世界"
            msg = (f"✅ 备份已复制到服务器根目录：{dest}\n"
                   "请手动切换（不会在运行时换档，最大化避免崩溃）：\n"
                   "1. 停止服务器；\n"
                   "2. 打开上方的服务器根目录；\n"
                   f"3. 把当前世界目录 {current} 改名为 {current}_old 备份；\n"
                   f"4. 用解压工具把该 zip 解压到根目录（zip 内含的世界文件夹会自动放置）；\n"
                   "5. 重启服务器即生效。")
            self.logger.info(f"[备份] 已复制 {name} 到 {dest}，当前世界={current}")
            return True, msg
        except Exception as e:
            return False, f"复制失败: {e}"

    def _cloud_cfg(self) -> Dict[str, Any]:
        return self._load_tools_config().get("cloud_blacklist", {})

    @staticmethod
    def _cloud_b64(payload) -> str:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _cloud_request(self, endpoint, payload=None, method="GET", timeout=None, token=None, require_token=False):
                                                                                                           
                                                                                                                     
        cfg = self._cloud_cfg()
        base = str(cfg.get("api_base") or "").strip()
        if not base or base == "https://your-domain.com/api":
            raise RuntimeError("未配置有效的云黑 API 地址（api_base），请先在「云黑名单」填写你的接口域名")
        timeout = float(timeout if timeout is not None else cfg.get("timeout", 8))
        tk = token if token is not None else cfg.get("token")
        if require_token and (not tk or str(tk) in ("", "__SET__")):
            raise RuntimeError("未配置云黑 Token，请在「云黑名单」填入 API Token")
        if str(tk) in ("", "__SET__"):
            tk = None
        url = base.rstrip("/") + "/" + str(endpoint).lstrip("/")
        headers = {"User-Agent": "GreenMoon/0.11"}
        body = None
        data_b64 = None
        if payload is not None:
            data_b64 = self._cloud_b64(payload)
        if method == "GET":
            if data_b64:
                sep = "&" if "?" in url else "?"
                url = url + sep + "data=" + urllib.parse.quote(data_b64, safe="")
        else:
            headers["Content-Type"] = "application/json"
            body = json.dumps({"data": data_b64}).encode("utf-8") if data_b64 is not None else b"{}"
        if tk:
            headers["Authorization"] = "Bearer " + str(tk)
        req = _urllib_request.Request(url, data=body, headers=headers, method=method)
        try:
            with _urllib_request.urlopen(req, timeout=timeout) as resp:
                content = resp.read().decode("utf-8", "replace")
        except _urllib_error.HTTPError as e:
            raise RuntimeError(f"云端返回 HTTP {e.code} {e.reason}（连接失败）")
        except _urllib_error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}（连接失败）")
        except OSError as e:
            raise RuntimeError(f"连接失败: {e}")
        if not content.strip():
            raise RuntimeError("云端响应为空，请检查 API 地址与端点是否正确（连接失败）")
        try:
            return json.loads(content)
        except Exception:
            raise RuntimeError(f"云端响应不是有效 JSON: {content[:120]!r}（连接失败）")

    def _resolve_player_ident(self, name: str):

        try:
            p = self.server.get_player(str(name))
            if p is not None:
                xuid = getattr(p, "xuid", None)
                ip = ""
                try:
                    addr = getattr(p, "address", None)
                    ip = getattr(addr, "host", "") or ""
                except Exception:
                    pass
                return (str(xuid) if xuid else None), (ip or None)
        except Exception:
            pass
        return None, None

    def _cloud_sync(self, payload: Dict[str, Any]):
        try:
            cfg = self._cloud_cfg()
            if not cfg.get("enabled"):
                return
            self._executor.submit(self._cloud_sync_worker, payload)
        except Exception as e:
            self.logger.warning(f"[云黑] 无法提交同步封禁任务: {e}")

    def _cloud_sync_worker(self, payload: Dict[str, Any]):
        try:
            cfg = self._cloud_cfg()
            tk = str(cfg.get("token") or "").strip()
            if not cfg.get("enabled") or not tk:
                return
            p = dict(payload or {})
            sn = str(cfg.get("server_name") or "").strip()
            if sn and not p.get("server_name"):
                p["server_name"] = sn
            if p.get("server_name") is None:
                p.pop("server_name", None)
            ident = p.get("player_name") or p.get("xuid") or p.get("ip_address") or "?"
            res = self._cloud_request("server_create_ban.php", p, method="POST")
            ok = bool(res.get("ok"))
            msg = res.get("message") or ""
            self.logger.info(f"[云黑] 同步封禁 {ident}: {'成功' if ok else '失败 ' + str(msg)}")
        except Exception as e:
            self.logger.warning(f"[云黑] 同步封禁失败: {e}")

    def _cloud_unban_worker(self, payload: Dict[str, Any]):
        try:
            cfg = self._cloud_cfg()
            tk = str(cfg.get("token") or "").strip()
            if not cfg.get("enabled") or not tk:
                return
            p = dict(payload or {})
            sn = str(cfg.get("server_name") or "").strip()
            if sn and not p.get("server_name"):
                p["server_name"] = sn
            ident = p.get("player_name") or p.get("xuid") or p.get("ip_address") or "?"
            res = self._cloud_request("server_revoke_ban.php", p, method="POST")
            ok = bool(res.get("ok"))
            msg = res.get("message") or ""
            self.logger.info(f"[云黑] 同步解封 {ident}: {'成功' if ok else '失败 ' + str(msg)}")
        except Exception as e:
            self.logger.warning(f"[云黑] 同步解封失败: {e}")

    def _cloud_sync_unban(self, payload: Dict[str, Any]):
        try:
            cfg = self._cloud_cfg()
            if not cfg.get("enabled"):
                return
            self._executor.submit(self._cloud_unban_worker, payload)
        except Exception as e:
            self.logger.warning(f"[云黑] 无法提交同步解封任务: {e}")

    def _cloud_check_login(self, name: str, xuid, ip):
        try:
            cfg = self._cloud_cfg()
            if not cfg.get("enabled") or not cfg.get("check_on_login", True):
                return
            payload = {}
            if xuid:
                payload["xuid"] = str(xuid)
            if ip:
                payload["ip_address"] = str(ip)
            if not payload:
                return
            res = self._cloud_request("check_ban.php", payload)
            if not (res.get("ok") and (res.get("data") or {}).get("banned")):
                return
            ban = (res.get("data") or {}).get("ban") or {}
            reason = str(ban.get("reason") or "你已被云黑名单封禁")
            km = str(cfg.get("kick_message") or "§c你在云黑名单中\n§7联合封禁系统 (UniteBan)")
            try:
                km = km.replace("{reason}", reason)
            except Exception:
                pass

            def kick():
                try:
                    p = self.server.get_player(str(name))
                    if p is not None:
                        p.kick(km)
                except Exception:
                    pass

            self._run_async_on_server_thread(kick)
            self.logger.info(f"[云黑] 玩家 {name} 命中云黑，已踢出：{reason}")
        except Exception as e:
            self.logger.warning(f"[云黑] 进服检查失败（放行）: {e}")

    def _cloud_test(self):
        try:
            res = self._cloud_request("validate_token.php", method="GET", timeout=6, require_token=True)
            if not isinstance(res, dict):
                raise RuntimeError("云端响应格式异常（连接失败）")
            valid = bool((res.get("data") or {}).get("valid"))
            msg = str(res.get("message") or ("接口可达，Token 有效" if valid else "Token 无效"))
            return bool(valid and res.get("ok")), msg, res
        except Exception as e:
            return False, str(e), None

    def _maybe_cloud_check(self, player):

        cfg = self._cloud_cfg()
        if not cfg.get("enabled") or not cfg.get("check_on_login", True):
            return
        try:
            name = player.name
            xuid = getattr(player, "xuid", None)
            ip = ""
            try:
                addr = getattr(player, "address", None)
                ip = getattr(addr, "host", "") or ""
            except Exception:
                pass
        except Exception:
            return
        xuid = str(xuid) if xuid else None
        ip = ip or None
        if not (xuid or ip):
            return
        try:
            self._executor.submit(self._cloud_check_login, name, xuid, ip)
        except Exception as e:
            self.logger.warning(f"[云黑] 提交进服检查失败: {e}")

    def _store_lock(self):
        if not hasattr(self, "_store_lock_inst"):
            self._store_lock_inst = threading.Lock()
        return self._store_lock_inst

    def _json_store(self, fname, default):
        try:
            p = self.data_dir / fname
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return default

    def _json_save(self, fname, data):
        try:
            p = self.data_dir / fname
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    def _load_delayed(self):
        d = self._json_store("delayed_tasks.json", {})
        if not isinstance(d, dict):
            d = {}
        if not isinstance(d.get("tasks"), list):
            d["tasks"] = []
        return d

    def _add_delayed_give(self, player_name, item_id, amount, source="delay"):
        player_name = str(player_name or "").strip()
        item_id = str(item_id or "").strip()
        try:
            amount = max(1, int(amount or 1))
        except (TypeError, ValueError):
            amount = 1
        if not player_name or not item_id:
            return False, "缺少玩家名或物品ID"
        with self._store_lock():
            d = self._load_delayed()
            d["tasks"].append({
                "id": _uuid.uuid4().hex[:8],
                "player": player_name,
                "kind": "give",
                "item": item_id,
                "amount": amount,
                "source": str(source or "delay"),
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._json_save("delayed_tasks.json", d)
        return True, f"已为 {player_name} 记录 {amount} 个 {item_id}（上线后自动发放）"

    def _process_delayed_tasks(self):

        released = []
        with self._store_lock():
            d = self._load_delayed()
            tasks = d.get("tasks", [])
            remain = []
            for t in tasks:
                name = str(t.get("player", "") or "").strip()
                item = str(t.get("item", "") or "")
                try:
                    amount = max(1, int(t.get("amount") or 1))
                except (TypeError, ValueError):
                    amount = 1
                if not name or not item:
                    continue
                target = None
                try:
                    target = self.server.get_player(name)
                except Exception:
                    target = None
                if target is None:
                    remain.append(t)
                    continue
                gave = self._grant_item(name, item, amount)
                if gave:
                    released.append(f"{name} +{amount} {item}")
                    try:
                        self.server.broadcast_message(f"§a{name} 获得延迟奖励 {amount} 个 {item}")
                    except Exception:
                        pass
                else:
                    remain.append(t)
            d["tasks"] = remain
            self._json_save("delayed_tasks.json", d)
        return released

    def _grant_item(self, player_name: str, item: str, amount: int) -> bool:
                                                                          

                                                                                                    
                                                                                                                 
                                                                                               
           
        try:
            return self.server.dispatch_command(
                self.server.command_sender, f"give {player_name} {item} {amount}"
            )
        except Exception:
            return False

    def _delayed_stats(self):
        with self._store_lock():
            return [dict(t) for t in self._load_delayed().get("tasks", [])]

    def _load_bindings(self):
        b = self._json_store("bindings.json", {})
        return b if isinstance(b, dict) else {}

    def _qq_for_mc(self, mc_name):
        mc = str(mc_name or "").strip().lower()
        if not mc:
            return None
        b = self._load_bindings()
        entry = b.get("by_mc", {}).get(mc)
        return entry if isinstance(entry, dict) else None

    def _mc_for_qq(self, qq_openid):
        qq = str(qq_openid or "").strip()
        if not qq:
            return None
        return self._load_bindings().get("by_qq", {}).get(qq)

    def _pending_codes(self):
        if not hasattr(self, "_pending_codes_inst"):
            self._pending_codes_inst = {}
        return self._pending_codes_inst

    def _purge_pending_codes(self):
        now = time.time()
        pc = self._pending_codes()
        for code in list(pc.keys()):
            info = pc.get(code) or {}
            if now - info.get("created", 0) > info.get("ttl", 300):
                pc.pop(code, None)

    def _issue_binding_code(self, mc_name, digits=5, ttl=300):
        self._purge_pending_codes()
        pc = self._pending_codes()
        for code, info in list(pc.items()):
            if str(info.get("mc", "")).lower() == str(mc_name).lower():
                return code
        lim = 10 ** int(digits or 5)
        for _ in range(40):
            cand = f"{secrets.randbelow(lim):0{digits}d}"
            if cand not in pc:
                pc[cand] = {"mc": mc_name, "created": time.time(), "ttl": int(ttl or 300)}
                return cand
        return None

    def _try_bind(self, qq_openid, code):
        code = str(code or "").strip()
        if not qq_openid or not code:
            return False, "参数不完整"
        self._purge_pending_codes()
        pc = self._pending_codes()
        info = pc.get(code)
        if not info:
            return False, "绑定码无效或已过期，请重新进服获取新的绑定码"
        mc_name = str(info.get("mc", "") or "").strip()
        if not mc_name:
            pc.pop(code, None)
            return False, "绑定码无对应游戏账号，请重新进服获取新的绑定码"
        pc.pop(code, None)
        with self._store_lock():
            b = self._load_bindings()
            by_qq = b.setdefault("by_qq", {})
            by_mc = b.setdefault("by_mc", {})
            old_mc = by_qq.get(qq_openid)
            if isinstance(old_mc, str) and old_mc.lower() in by_mc:
                by_mc.pop(old_mc.lower(), None)
            low_mc = mc_name.lower()
            old_entry = by_mc.get(low_mc)
            if isinstance(old_entry, dict) and str(old_entry.get("qq", "")) != qq_openid:
                by_qq.pop(str(old_entry.get("qq", "")), None)
            by_qq[qq_openid] = mc_name
            by_mc[low_mc] = {"qq": qq_openid, "mc": mc_name,
                             "bound_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            self._json_save("bindings.json", b)
        return True, f"✅ 绑定成功！QQ 已绑定游戏账号 {mc_name}"

    def _maybe_handle_join_binding(self, player):

        cfg = self._load_tools_config().get("binding", {}) or {}
        if not cfg.get("enabled", True) or not cfg.get("require_on_join", True):
            return

        if getattr(self, "qq_bot", None) is None:
            return
        mc = str(player.name or "").strip()
        if not mc or self._qq_for_mc(mc) is not None:
            return
        digits = int(cfg.get("code_digits", 5) or 5)
        ttl = int(cfg.get("code_ttl_seconds", 300) or 300)
        code = self._issue_binding_code(mc, digits, ttl)
        if not code:
            return
        try:
            player.send_message(
                f"§e请在 QQ 群输入绑定码 §6{code}§e 完成「{mc}」与 QQ 的绑定（{max(1,int(ttl/60))} 分钟内有效，之后请重新进服获取）"
            )
        except Exception:
            pass

    def _signin_config(self):
        return self._load_tools_config().get("signin", {}) or {}

    def _load_signin(self):
        s = self._json_store("signin.json", {})
        return s if isinstance(s, dict) else {}

    def _do_signin(self, qq_openid, sender_name=None):
        if not qq_openid:
            return False, "无法识别你的 QQ 身份（openid 为空）"
        cfg = self._signin_config()
        if not cfg.get("enabled", True):
            return False, "签到功能暂未开放"
        mc = self._mc_for_qq(qq_openid)
        if not mc:
            return False, "你尚未完成账号绑定。请先进游戏获取 5 位绑定码，再到群内发送该绑定码完成绑定后即可签到"
        item = str(cfg.get("item_id") or "minecraft:diamond").strip()
        try:
            amount = max(1, int(cfg.get("amount", 1) or 1))
        except (TypeError, ValueError):
            amount = 1
        today = time.strftime("%Y-%m-%d")
        with self._store_lock():
            s = self._load_signin()
            last = s.setdefault("last", {})
            low_mc = str(mc).lower()
            if last.get(low_mc) == today:
                return False, f"你今天已经签到过啦（{today}），明天再来吧"

            d = self._load_delayed()
            d["tasks"].append({
                "id": _uuid.uuid4().hex[:8], "player": mc, "kind": "give",
                "item": item, "amount": amount,
                "source": "signin", "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._json_save("delayed_tasks.json", d)
            last[low_mc] = today
            self._json_save("signin.json", s)

        online = False
        try:
            online = bool(self._run_in_server_thread(self._settle_delayed_and_online, str(mc), timeout=8.0))
        except Exception:
            online = False
        who = sender_name or mc
        return True, f"✅ {who} 签到成功（{today}）！奖励 {amount} 个 {item}" + (
            " 已到账" if online else " 已记录，等你上线自动发放"
        )

    def _settle_delayed_and_online(self, mc_name: str):
                                                                                                              
                                                  
        try:
            online = self.server.get_player(mc_name) is not None
        except Exception:
            online = False
        try:
            self._process_delayed_tasks()
        except Exception:
            pass
        return online

    def _mods_dir(self):
        d = self.data_dir / "mods"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d

    def _load_mod_registry(self):
        r = self._json_store("mods/mod_registry.json", {})
        return r if isinstance(r, dict) else {}

    def _save_mod_registry(self, r):
        with self._store_lock():
            return self._json_save("mods/mod_registry.json", r)

    def _register_mod_file(self, fname, fsize):
        with self._store_lock():
            r = self._load_mod_registry()
            items = r.setdefault("mods", [])
            for it in items:
                if it.get("file") == fname:
                    it["size"] = fsize
                    it["uploaded"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    break
            else:
                items.insert(0, {
                    "file": fname, "size": fsize,
                    "uploaded": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            r["mods"] = items[:int(self._load_tools_config().get("mods", {}).get("max_keep", 30) or 30)]
            self._json_save("mods/mod_registry.json", r)
        return r

    def _scan_installed_packs(self):

        installed = []
        try:
            root = self._find_server_root() or Path.cwd()
        except Exception:
            root = Path.cwd()
        seen = set()
        def _scan(base):
            try:
                if not base.is_dir():
                    return
                for pack_dir in sorted(base.iterdir()):
                    if not pack_dir.is_dir() or pack_dir.name.startswith("."):
                        continue
                    mf = pack_dir / "manifest.json"
                    uuid_, ver, name = pack_dir.name, "?", pack_dir.name
                    try:
                        if mf.exists():
                            m = json.loads(mf.read_text(encoding="utf-8"))
                            uuid_ = (m.get("header", {}) or {}).get("uuid", pack_dir.name)
                            ver = ".".join(map(str, (m.get("header", {}) or {}).get("version", []))) or "?"
                            name = (m.get("header", {}) or {}).get("name", pack_dir.name)
                    except Exception:
                        pass
                    key = (str(pack_dir.resolve()), base.name)
                    if key not in seen:
                        seen.add(key)
                        installed.append({
                            "name": name, "uuid": uuid_, "version": ver,
                            "path": str(pack_dir), "parent": base.name,
                        })
            except Exception:
                pass
        for sub in ("resource_packs", "behavior_packs"):
            _scan(root / sub)
        worlds_dir = root / "worlds"
        try:
            world_entries = [d for d in sorted(worlds_dir.iterdir()) if d.is_dir()] if worlds_dir.is_dir() else []
        except Exception:
            world_entries = []
        if not world_entries:
            try:
                world_entries = [d for d in sorted(root.iterdir())
                                 if d.is_dir() and (d / "level.dat").exists() and d.name not in ("worlds", "resource_packs", "behavior_packs")]
            except Exception:
                world_entries = []
        for wd in world_entries:
            for sub in ("resource_packs", "behavior_packs"):
                _scan(wd / sub)
        return installed

    def _install_mod_pack(self, src_path, target_type, world_name=None):
                                                                                                      
                                                                                                                     
        import tempfile
        src = Path(str(src_path))
        if not src.is_file():
            return False, "文件不存在"
        suffix = src.suffix.lower()
        if suffix not in (".mcaddon", ".mcpack"):
            return False, "只支持 .mcaddon 或 .mcpack 文件"
        root = self._find_server_root() or Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                with zipfile.ZipFile(str(src), "r", allowZip64=True) as zf:
                    zf.extractall(tmpdir)
                t_root = Path(tmpdir)

                stack = [t_root]
                pack_folders = []
                leaf_packs = []
                while stack:
                    cur = stack.pop()
                    if not cur.is_dir():
                        continue
                    if cur.parent != t_root and (cur / "manifest.json").exists():

                        leaf_packs.append(cur)
                        continue
                    for item in cur.iterdir():
                        if item.is_dir():
                            stack.append(item)
                        elif item.suffix.lower() == ".mcpack" and item.name.lower() != src.name.lower():
                            leaf_packs.append(item)
                if not leaf_packs:
                    return False, "压缩包内未找到有效的包（缺少 manifest.json 的文件夹）"
                installed = []
                for pf in leaf_packs:
                    if pf.is_file():
                        ok, msg = self._install_one_pack_file(pf, root, target_type, world_name)
                    else:
                        ok, msg = self._install_one_pack_folder(pf, root, target_type, world_name)
                    if ok:
                        installed.append(msg)
                    else:
                        return False, msg
                return True, "；".join(installed) if installed else "未安装任何包"
        except Exception as e:
            return False, f"安装失败: {e}"

    def _install_one_pack_folder(self, folder: Path, root: Path, target_type, world_name):
        try:
            m = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        except Exception as e:
            return False, f"读取 manifest.json 失败: {e}"
        header = m.get("header", {}) or {}
        uuid_ = header.get("uuid")
        version = header.get("version")
        if not uuid_:
            return False, f"{folder.name} 缺少 header.uuid"
        pack_type = self._pack_type(m)
        if not pack_type:
            return False, f"无法识别包类型: {folder.name}"
        base_dir, world_path = self._resolve_pack_target(root, pack_type, target_type, world_name)
        if base_dir is None:
            return False, f"找不到世界: {world_name}"
        base_dir.mkdir(parents=True, exist_ok=True)
        target_pack = base_dir / str(uuid_)
        if target_pack.exists():
            shutil.rmtree(str(target_pack), ignore_errors=True)
        shutil.copytree(str(folder), str(target_pack))
        self._enable_pack_in_world(world_path, pack_type, uuid_, version)
        return True, f"{header.get('name', folder.name)}（{pack_type}）→ {target_pack}"

    def _install_one_pack_file(self, f: Path, root: Path, target_type, world_name):
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(str(f), "r", allowZip64=True) as zf:
                zf.extractall(td)
            t = Path(td)

            found = None
            for item in t.iterdir():
                if item.is_dir() and (item / "manifest.json").exists():
                    found = item
                    break
            if found is None and (t / "manifest.json").exists():
                found = t
            if found is None:
                return False, f"{f.name} 内未找到带 manifest.json 的包"
            return self._install_one_pack_folder(found, root, target_type, world_name)

    @staticmethod
    def _pack_type(m):
        for mod in m.get("modules", []) or []:
            mt = str(mod.get("type", ""))
            if mt == "resources":
                return "resource"
            if mt == "data":
                return "behavior"
        tp = str(m.get("type", "") or "")
        if tp == "resources":
            return "resource"
        if tp == "data":
            return "behavior"
        return None

    def _resolve_pack_target(self, root: Path, pack_type, target_type, world_name):
        sub = "resource_packs" if pack_type == "resource" else "behavior_packs"
        if target_type != "world":
            return root / sub, self._locate_world_for_target(root)
        wpath = self._locate_world(root, world_name)
        if wpath is None:
            return None, None
        return wpath / sub, wpath

    def _locate_world_for_target(self, root):

        try:
            _, content = self._read_properties_raw()
            name = self._get_property_value(content, "level-name")
            if name:
                w = self._locate_world(root, name)
                if w:
                    return w
        except Exception:
            pass
        return None

    def _locate_world(self, root, world_name):
        name = str(world_name or "").strip()
        cands = []
        if name:
            cands = [root / "worlds" / name, root / name]
        else:
            return None
        for c in cands:
            try:
                if c.is_dir() and (c / "level.dat").exists():
                    return c
            except Exception:
                pass
        return None

    def _enable_pack_in_world(self, world_path, pack_type, uuid_, version):

        if world_path is None:
            return
        jf = world_path / ("world_resource_packs.json" if pack_type == "resource" else "world_behavior_packs.json")
        entries = []
        try:
            if jf.exists():
                entries = json.loads(jf.read_text(encoding="utf-8"))
                if not isinstance(entries, list):
                    entries = []
        except Exception:
            entries = []
        found = False
        for e in entries:
            if e.get("pack_id") == uuid_:
                e["version"] = version
                found = True
                break
        if not found:
            entries.append({"pack_id": uuid_, "version": version})
        try:
            jf.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _list_mod_uploaded(self):
        r = self._load_mod_registry()
        mods = r.get("mods", [])
        out = []
        for it in mods:
            fname = it.get("file", "")
            fpath = self._mods_dir() / fname
            out.append({
                "file": fname,
                "size": it.get("size", 0),
                "exists": fpath.is_file(),
                "uploaded": it.get("uploaded", ""),
            })
        return out

    def _delete_mod_file(self, fname):
        fname = str(fname or "").strip()
        safe = os.path.basename(fname)
        fpath = self._mods_dir() / safe
        try:
            if fpath.is_file():
                fpath.unlink()
        except Exception:
            return False
        with self._store_lock():
            r = self._load_mod_registry()
            r["mods"] = [it for it in r.get("mods", []) if it.get("file") != safe]
            self._json_save("mods/mod_registry.json", r)
        return True

    def _run_server(self):
        try:
            self.httpd.serve_forever()
        except Exception as e:
            self.logger.error(f"Web 服务器异常: {e}")
            traceback.print_exc()

    def _snapshot(self):

        players: List[Dict[str, Any]] = []
        objective_names: List[str] = []
        objectives: List[Dict[str, Any]] = []
        player_bans: List[Dict[str, Any]] = []
        ip_bans: List[Dict[str, Any]] = []
        try:
            scoreboard = self.server.scoreboard
            if scoreboard:
                for obj in scoreboard.objectives:
                    try:
                        objective_names.append(obj.name)
                        slot = obj.display_slot
                        objectives.append({
                            "name": obj.name,
                            "display_name": obj.display_name,
                            "display_slot": slot.name if slot is not None else None,
                            "render_type": getattr(obj.render_type, "name", None) or str(obj.render_type),
                            "sort_order": obj.sort_order.name if obj.sort_order is not None else None,
                            "is_displayed": bool(obj.is_displayed),
                        })
                    except Exception:
                        continue

            try:
                for entry in self.server.ban_list.entries:
                    player_bans.append({
                        "name": entry.name,
                        "uuid": str(entry.unique_id) if entry.unique_id else None,
                        "xuid": entry.xuid,
                        "reason": entry.reason,
                        "source": entry.source,
                        "created": entry.created.isoformat() if entry.created else None,
                        "expiration": entry.expiration.isoformat() if entry.expiration else None,
                    })
            except Exception:
                pass
            try:
                for entry in self.server.ip_ban_list.entries:
                    ip_bans.append({
                        "address": entry.address,
                        "reason": entry.reason,
                        "source": entry.source,
                        "created": entry.created.isoformat() if entry.created else None,
                        "expiration": entry.expiration.isoformat() if entry.expiration else None,
                    })
            except Exception:
                pass

            for player in self.server.online_players:
                try:
                    loc = player.location
                    dimension = loc.dimension.name if hasattr(loc, "dimension") else "unknown"
                    tags = list(player.scoreboard_tags) if hasattr(player, "scoreboard_tags") else []
                    scores: Dict[str, int] = {}
                    if scoreboard:
                        for objective in scoreboard.objectives:
                            try:
                                score = objective.get_score(player)
                                if score is not None and score.is_score_set:
                                    scores[objective.name] = score.value
                            except Exception:
                                continue
                    permissions: List[Dict[str, Any]] = []
                    try:
                        for info in player.effective_permissions:
                            permissions.append({"permission": info.permission, "value": bool(info.value)})
                    except Exception:
                        pass
                    try:
                        perm_level = int(player.permission_level)
                    except Exception:
                        perm_level = 0
                    try:
                        uid = str(player.unique_id) if player.unique_id is not None else ""
                    except Exception:
                        uid = ""
                    try:
                        xuid = str(player.xuid) if getattr(player, "xuid", None) else ""
                    except Exception:
                        xuid = ""
                    players.append({
                        "name": player.name,
                        "uuid": uid,
                        "xuid": xuid,
                        "x": loc.x,
                        "y": loc.y,
                        "z": loc.z,
                        "dimension": dimension,
                        "health": player.health,
                        "max_health": player.max_health,
                        "ping": getattr(player, "ping", 0),
                        "game_mode": str(player.game_mode) if hasattr(player, "game_mode") else "unknown",
                        "is_op": player.is_op if hasattr(player, "is_op") else False,
                        "tags": tags,
                        "scores": scores,
                        "permissions": permissions,
                        "permission_level": perm_level,
                    })
                except Exception:
                    continue
        except Exception as e:
            self.logger.error(f"刷新玩家快照出错: {e}")
        with self._cache_lock:
            self._cache = {
                "players": players,
                "objective_names": objective_names,
                "objectives": objectives,
                "player_bans": player_bans,
                "ip_bans": ip_bans,
            }

    def _get_cache(self) -> Dict[str, Any]:
        with self._cache_lock:
            return self._cache

    def _get_cached_player(self, name: str):
        cache = self._get_cache()
        for p in cache.get("players", []):
            if p.get("name") == name:
                return p
        return None

    def _diag_on(self):
        try:
            return bool(self._diag_cfg.get("enabled", True))
        except Exception:
            return True

    def _diag_verbose(self):
        try:
            return bool(self._diag_cfg.get("detailed", True))
        except Exception:
            return True

    def _diag_save_cfg(self):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._diag_cfg_path.write_text(json.dumps(self._diag_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return True
        except Exception as e:
            self.logger.error(f"[诊断] 保存诊断设置失败: {e}")
            return False

    def _tick_health(self):

        if not self._diag_on():
            return
        now = time.monotonic()
        try:
            with self._tick_lock:
                gap = (now - self._tick_prev) * 1000.0
                self._tick_prev = now
                self._tick_gap_ms = gap
            if gap > 500.0:
                with self._tick_lock:
                    self._tick_slow_count += 1
                    alert = now - self._tick_alert_last
                if alert > 15.0:
                    with self._tick_lock:
                        self._tick_alert_last = now
                    self._log_diag("tick_slow", {"gap_ms": round(gap, 1), "current": self._pt_current_name(), "pending": self._pt_pending})
                    if not self._diag_verbose():
                        return
                    cur = self._pt_current_name()
                    hint = ""
                    if not cur:
                        hint = "（当前无插件任务占用主线程，多为世界自动保存/区块生成/实体加载等游戏核心耗时）"
                    else:
                        hint = f"（当前主线程正执行插件任务：{cur}）"
                    self.logger.warning(f"[诊断] 主线程 tick 间隔异常偏大: {gap:.1f}ms（正常约50ms）；当前任务={cur} 排队={self._pt_pending}{hint}")
                    if gap > 2000.0:
                        self.logger.warning(f"[诊断] 主线程疑似长时间卡死，线程栈如下:{chr(10)}{self._dump_all_stacks()}")
        except Exception:
            pass

    def _pt_current_name(self):
        try:
            with self._pt_lock:
                if not self._pt_current:
                    return None
                return self._pt_current.get("name")
        except Exception:
            return None

    def _pt_mark_begin(self, name, args):
        try:
            with self._pt_lock:
                self._pt_pending += 1
                if self._pt_current is None:
                    self._pt_current = {"name": name, "args": args, "t0": time.monotonic()}
                    return True
            return False
        except Exception:
            return False

    def _pt_mark_end(self):
        try:
            with self._pt_lock:
                self._pt_pending -= 1
                if self._pt_pending < 0:
                    self._pt_pending = 0
                self._pt_current = None
        except Exception:
            pass

    def _dump_all_stacks(self) -> str:

        import io as _io
        try:
            cur = sys._current_frames()
        except Exception:
            return "(无法获取线程栈)"
        buf = _io.StringIO()
        try:
            for th in threading.enumerate():
                fr = cur.get(th.ident)
                if fr is None:
                    continue
                is_main = (th.ident == getattr(self, "_main_thread_id", None))
                buf.write("---- 线程: %s (id=%s)%s ----\n"
                          % (th.name, th.ident, " <<主线程>>" if is_main else " <<HTTP/后台>>" if "HTTP" in th.name else ""))
                try:
                    buf.write("".join(traceback.format_stack(fr, limit=18)))
                except Exception:
                    buf.write("(栈解析失败)")
                buf.write("\n")
            return buf.getvalue()
        except Exception as e:
            return f"(栈转储异常: {e})"

    def _log_diag(self, kind: str, extra: Optional[dict] = None):
        try:
            item = {"kind": kind, "ts": time.strftime("%H:%M:%S"), "extra": extra or {}}
            self._diag_events.append(item)
        except Exception:
            pass

    def _diag_summary(self) -> Dict[str, Any]:
        try:
            with self._tick_lock:
                gap = self._tick_gap_ms
                slow = self._tick_slow_count
            return {
                "tick_gap_ms": round(gap, 1),
                "tick_slow_count": slow,
                "pending": self._pt_pending,
                "current": self._pt_current_name(),
                "main_thread_id": getattr(self, "_main_thread_id", None),
                "enabled": self._diag_on(),
                "detailed": self._diag_verbose(),
                "events": list(self._diag_events),
                "backup_running": bool(self._backup_state.get("running")),
                "backup_next_run": self._backup_state.get("next_run"),
                "cloud_cfg": self._cloud_cfg_raw(),
            }
        except Exception as e:
            return {"error": str(e)}

    def _cloud_cfg_raw(self) -> Dict[str, Any]:
        try:
            c = self._load_tools_config().get("cloud_blacklist", {}) or {}
            out = {
                "enabled": c.get("enabled"),
                "check_on_join": c.get("check_on_join"),
            }
            tk = c.get("token")
            out["token_set"] = bool(tk)
            api = str(c.get("api_base") or "")
            out["api_base"] = api
            return out
        except Exception:
            return {}

    def _run_in_server_thread(self, func: Callable, *args, timeout: float = 5.0, **kwargs):
        name = getattr(func, "__name__", str(func))
        try:
            cur_id = threading.current_thread().ident
        except Exception:
            cur_id = None
        if self._main_thread_id is not None and cur_id == self._main_thread_id:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                self.logger.error(f"主线程任务执行异常: {e}")
                return False, f"主线程任务执行异常: {e}"

        if not self._mainthread_sem.acquire(timeout=0.5):
            self._log_diag("busy", {"name": name})
            return False, "服务器繁忙，请稍后重试"
        try:
            result_holder = []
            exception_holder = []
            event = threading.Event()

            def task():
                try:
                    self._pt_mark_begin(name, None)
                    try:
                        result_holder.append(func(*args, **kwargs))
                    except Exception as e:
                        exception_holder.append(e)
                finally:
                    self._pt_mark_end()
                    event.set()

            try:
                self.server.scheduler.run_task(self, task)
            except Exception as e:
                self.logger.error(f"调度主线程任务失败: {e}")
                return False, f"主线程调度失败: {e}"

            if not event.wait(timeout):
                cur = self._pt_current_name()
                try:
                    with self._tick_lock:
                        gap = round(self._tick_gap_ms, 1)
                except Exception:
                    gap = -1
                if self._diag_on():
                    self._log_diag("timeout", {"name": name, "current": cur, "pending": self._pt_pending, "tick_gap_ms": gap})
                if self._diag_on() and self._diag_verbose():
                    self.logger.error(
                        f"[诊断] 主线程任务执行超时: 请求任务={name}  主线程当前在执行={cur}  排队={self._pt_pending}  tick间隔={gap}ms{chr(10)}"
                        f"---- 全线程栈 ----{chr(10)}{self._dump_all_stacks()}")
                elif self._diag_on():
                    self.logger.warning(f"[诊断] 主线程任务执行超时: {name}")
                return False, "主线程任务执行超时，请重试"

            if exception_holder:
                self.logger.error(f"主线程任务执行异常: {exception_holder[0]}")
                return False, f"主线程任务执行异常: {exception_holder[0]}"
            return result_holder[0] if result_holder else (False, "主线程任务无返回结果")
        finally:
            try:
                self._mainthread_sem.release()
            except Exception:
                pass

    def _run_async_on_server_thread(self, func: Callable, *args):

        def task():
            try:
                func(*args)
            except Exception as e:
                self.logger.error(f"主线程异步任务异常: {e}")

        try:
            self.server.scheduler.run_task(self, task)
        except Exception as e:
            self.logger.error(f"调度主线程异步任务失败: {e}")

    def _load_gamerules(self):
        defaults = {g["name"]: g["default"] for g in GAMERULES}
        self._gamerule_values = dict(defaults)
        path = self.data_dir / "gamerules.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for name, val in saved.items():
                    if name in defaults:
                        self._gamerule_values[name] = val
            except Exception as e:
                self.logger.error(f"加载 gamerules.json 失败: {e}")

    def _save_gamerules(self):
        try:
            path = self.data_dir / "gamerules.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._gamerule_values, f, indent=2)
        except Exception as e:
            self.logger.error(f"保存 gamerules.json 失败: {e}")

    def get_gamerules(self):
        result = []
        for g in GAMERULES:
            item = dict(g)
            item["value"] = self._gamerule_values.get(g["name"], g["default"])
            result.append(item)
        return result

    def _load_whitelist(self):
        path = self.data_dir / "whitelist.json"
        if not path.exists():
            return
        try:
            with self._whitelist_lock:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                apps = data.get("applications", []) if isinstance(data, dict) else data
                if isinstance(apps, list):
                    for a in apps:
                        name = str(a.get("name", "")).strip()
                        if name:
                            self._whitelist_apps[name.lower()] = a
        except Exception as e:
            self.logger.error(f"加载 whitelist.json 失败: {e}")

    def _save_whitelist(self):
        try:
            path = self.data_dir / "whitelist.json"
            with self._whitelist_lock:
                apps = list(self._whitelist_apps.values())
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"applications": apps}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"保存 whitelist.json 失败: {e}")

    def _list_whitelist_apps(self):
        with self._whitelist_lock:
            apps = list(self._whitelist_apps.values())
        order = {"pending": 0, "approved": 1, "rejected": 2}
        apps.sort(key=lambda a: str(a.get("applied_at", "")), reverse=True)
        apps.sort(key=lambda a: order.get(str(a.get("status", "pending")), 9))
        return apps

    def _read_cpu_ticks(self):
        try:
            with open("/proc/stat", "r") as f:
                parts = f.readline().split()
            nums = [int(x) for x in parts[1:8]]
            total = sum(nums)
            idle = nums[3] + nums[4]
            return total, idle
        except Exception:
            return None, None

    def get_system_stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}

        try:
            t, i = self._read_cpu_ticks()
            with self._stats_lock:
                prev = self._cpu_prev
                cpu = 0.0
                if t is not None and prev and prev[0] and t > prev[0]:
                    d_total = t - prev[0]
                    d_idle = max(0, i - prev[1])
                    cpu = max(0.0, min(100.0, (d_total - d_idle) / d_total * 100.0))
                self._cpu_prev = (t, i)
            stats["cpu"] = round(cpu, 1)
        except Exception:
            stats["cpu"] = 0.0

        try:
            vals = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        num = parts[1].strip().split()
                        if num:
                            vals[parts[0]] = float(num[0])
            total = vals.get("MemTotal", 0.0)
            avail = vals.get("MemAvailable", 0.0)
            used = max(0.0, total - avail)
            stats["mem"] = round(used / total * 100, 1) if total else 0.0
            stats["mem_total_mb"] = int(total / 1024)
            stats["mem_used_mb"] = int(used / 1024)
        except Exception:
            stats["mem"] = 0.0
            stats["mem_total_mb"] = 0
            stats["mem_used_mb"] = 0

        try:
            import shutil
            du = shutil.disk_usage("/")
            stats["disk"] = round(du.used / du.total * 100, 1)
            stats["disk_total_gb"] = round(du.total / 1073741824, 1)
            stats["disk_used_gb"] = round(du.used / 1073741824, 1)
        except Exception:
            stats["disk"] = 0.0
            stats["disk_total_gb"] = 0
            stats["disk_used_gb"] = 0

        try:
            stats["uptime"] = int(time.time() - (self._start_ts or time.time()))
        except Exception:
            stats["uptime"] = 0
        return stats

    def _tps_start_job(self):
        pl = self
        with pl._tps_lock:
            st = pl._tps_state
            if st["running"]:
                return
            st["running"] = True
            st["count"] = 0
            st["first"] = 0.0
            st["last"] = 0.0
            st["done"] = False
        try:
            pl._tps_task = pl.server.scheduler.run_task(pl, pl._tps_tick, delay=0, period=1)
        except Exception as e:
            self.logger.error(f"启动 TPS 监测任务失败: {e}")

    def _tps_tick(self):
        pl = self
        with pl._tps_lock:
            st = pl._tps_state
            st["count"] += 1
            now = time.perf_counter()
            if st["first"] == 0.0:
                st["first"] = now
            st["last"] = now

    def _tps_finish_job(self):

        pl = self
        with pl._tps_lock:
            st = pl._tps_state
            try:
                if pl._tps_task is not None:
                    pl._tps_task.cancel()
                    pl._tps_task = None
            except Exception:
                pass
            st["running"] = False
            st["done"] = True
            st["seq"] += 1

    def _append_console_log(self, level: str, message: str):
        try:
            with self._console_lock:
                self._console_seq += 1
                self._console_logs.append({
                    "id": self._console_seq,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "level": str(level or "INFO").upper(),
                    "message": str(message)[:2000],
                })
        except Exception:
            pass

    def _get_console_logs(self, after: int):
        with self._console_lock:
            new = [m for m in self._console_logs if m["id"] > after]
            last_id = self._console_seq
        return new, last_id

    def _log_admin_action(self, message: str):
        self._append_console_log("INFO", f"[GreenMoon] {message}")

    def _find_server_root(self) -> Optional[Path]:
        cur = self.data_dir
        for _ in range(8):
            if (cur / "server.properties").exists():
                return cur
            parent = cur.parent
            if parent == cur:
                break
            cur = parent
        return None

    def _read_properties_raw(self):
        root = self._find_server_root()
        if root is None:
            return None, ""
        path = root / "server.properties"
        try:
            return root, path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return root, ""
        except Exception as e:
            self.logger.error(f"读取 server.properties 失败: {e}")
            return root, ""

    def _write_properties_raw(self, content: str):
        root = self._find_server_root()
        if root is None:
            return False, "未找到服务器目录（server.properties 不存在）"
        path = root / "server.properties"
        try:

            try:
                if path.exists():
                    path.with_suffix(".properties.bak").write_text(
                        path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
                    )
            except Exception:
                pass
            path.write_text(content, encoding="utf-8")
            self._log_admin_action(f"已保存 server.properties")
            return True, "已保存 server.properties"
        except Exception as e:
            return False, f"保存失败: {e}"

    def _get_property_value(self, content: str, key: str):
        target = str(key).strip().lower()
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, v = stripped.partition("=")
            if k.strip().lower() == target:
                return v.strip()
        return ""

    def _set_property_value(self, content: str, key: str, value) -> str:
        target = str(key).strip().lower()
        value_str = str(value)
        lines = content.splitlines()
        found = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, _ = stripped.partition("=")
            if k.strip().lower() == target:
                lines[i] = f"{k.strip()}={value_str}"
                found = True
                break
        if not found:
            lines.append(f"{key.strip()}={value_str}")
        return "\n".join(lines)

    def _list_worlds(self):

        root = self._find_server_root()
        if root is None:
            return [], "", ""
        found: List[Dict[str, Any]] = []
        seen = set()

        worlds_dir = root / "worlds"
        dirs_to_scan = []
        if worlds_dir.is_dir():
            dirs_to_scan.extend(sorted(worlds_dir.iterdir()))

        dirs_to_scan.extend(sorted(root.iterdir()))
        for d in dirs_to_scan:
            try:
                if not d.is_dir() or d.name.startswith("."):
                    continue
                key = d.resolve()
                if key in seen:
                    continue
                seen.add(key)
                has_level = (d / "level.dat").exists()
                has_meta = (d / "levelname.txt").exists() or (d / "level.dat_old").exists()
                if has_level or has_meta:
                    found.append({
                        "name": d.name,
                        "path": str(d),
                        "valid": has_level,
                    })
            except Exception:
                continue

        _, content = self._read_properties_raw()
        current = self._get_property_value(content, "level-name")
        return found, current, str(root)

# ---- 子系统反向导入（在模块末尾，避免循环导入） ----
from .cross import CrossManager
from .qqbot import QQBotAPI, QQBotGateway, _TokenBucket, _GatewayLimiter
from .web import (
    AdminHTTPHandler,
    ThreadingHTTPServer,
    RateLimiter,
    _ConsoleStreamCapture,
    ImageMapRenderer,
    decode_bmp_24bit,
    _strip_ansi,
)
