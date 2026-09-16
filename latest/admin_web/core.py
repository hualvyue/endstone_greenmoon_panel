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
from endstone.event import event_handler, PlayerChatEvent, BroadcastMessageEvent, PlayerJoinEvent, PlayerQuitEvent, MapInitializeEvent

# /gmback 救援指令需要这两个类型做签名标注。做成防御式导入：
# 旧版 Endstone 没有 endstone.command，测试桩也可能缺，此时降级为 object
# 也不会影响插件加载（on_command 内部对入参只做鸭子类型访问）。
try:
    from endstone.command import Command, CommandSender
except Exception:  # pragma: no cover - 仅在缺失该模块的环境命中
    Command = CommandSender = object

from datetime import datetime, timedelta

from .web_assets import (
    HTML_LOGIN,
    HTML_LOGIN_ERROR,
    HTML_INDEX,
    HTML_WHITELIST,
    HTML_ADMIN,
)

from .connections import ConnectionManager, ConnectionValidationError, _FLAT_SYNC_KEYS
from .mod_loader import ModLoader, ModLoadError
from .atomic_io import atomic_write_json, atomic_write_text, atomic_write_bytes

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
    "qq_to_mc_format": "[QQ] %s@%s：%s",
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
    """确保 QQ 网关所需的 requests / websockets 可用，返回 ``(requests, websockets)``。

    ⚠ 这里**只负责把库取到手**，不负责往 sys.path 里插路径。
    真正的路径注册统一交给 ``libs_loader.install()``（在 ``on_enable``
    里、比本函数更早执行），原因见下。

    历史坑（已修）：本函数原先自己动手挂路径 ——
    ``sys.path.insert(0, libs_dir)`` 加上对每个 ``*.whl`` 的
    ``insert(0, whl)``。而 ``libs_loader`` 是把**解压目录**追加到
    ``sys.path`` 末尾的。两者叠加的结果是：whl 路径排到了解压目录**之前**，
    于是 ``import PIL`` 被 zipimport 抢先命中 wheel 内部那份
    （只有纯 Python 部分、没有 ``.so``），Pillow 当场变空壳 ——
    ``_imaging`` 加载失败，地图画每张图都静默失败。

    实测证据（``python3 -S``，剥离 site-packages）::

        libs_loader.install() 之后:
            PIL -> <libs>/_unpacked/pillow-.../PIL/__init__.py
            _imaging_ok=True   resize=ok
        _load_offline_wheels() 之后（旧实现）:
            PIL -> <libs>/pillow-....whl/PIL/__init__.py   ← 掉回 wheel 里
            _imaging_ok=False  resize=ImportError

    所以现在这里**一律不碰 sys.path** —— 依赖由 libs_loader 负责注册，
    本函数只做「取库」和「报缺库」。
    """
    try:
        libs_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        plugin.logger.error(f"[QQ] 创建离线依赖目录失败 {libs_dir}: {e}")

    try:
        import requests as _requests
        import websockets as _websockets
        return _requests, _websockets
    except Exception as e:
        # 明确点名缺的是哪两个包。
        #
        # 为什么要这么啰嗦：libs/ 现在是**通用**的离线依赖目录，用户可能
        # 往里面放 Pillow（给地图画用）。如果只报一句「第三方库不可用」，
        # 放了 Pillow 却看到 QQ 网关起不来的人会完全摸不着头脑 ——
        # 容易误以为"我明明放了库啊"，甚至去删 Pillow 的 whl 反复试。
        # 所以这里点破：这两个包是做 QQ 通信用的，与图像无关。
        missing = []
        try:
            import requests  # noqa: F401
        except Exception:
            missing.append("requests")
        try:
            import websockets  # noqa: F401
        except Exception:
            missing.append("websockets")
        names = "、".join(missing) if missing else "requests 或 websockets"
        plugin.logger.error(
            f"[QQ] 机器人网关无法启动：缺少第三方库 {names}。\n"
            f"    请把对应 .whl 放进 {libs_dir} 后重启服务器。\n"
            f"    （这两个库用于 QQ 通信，和图像功能无关 —— "
            f"Pillow / PIL 的 whl 不解决这个问题，也不要把它们删掉。）\n"
            f"    底层错误：{type(e).__name__}: {e}"
        )
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

def _format_template(template: str, values: dict):
    """把用户自定义的播报模板套上变量，返回**从新到旧**尝试出的结果。

    模板来自面板输入框，用户可能写成 ``%s`` 也可能写成 ``%(player)s``，
    还可能出现占位符个数对不上（多写一个 ``%s``）的情况。这里依次尝试：

      1. ``%(key)s`` 命名式
      2. 按 ``values`` 的插入顺序展开 ``%s``
      3. 模板整体替换失败时的降级：把占位符替换成对应值，不报错

    任何一种成功就返回；全部失败返回 ``None``，由调用方决定是否放弃这条
    播报。**绝不抛异常** —— 播报失败不该影响聊天/进服/退服主流程。
    """
    text = str(template or "")
    if not text:
        return None
    vals = list(values.values())
    # 1) 命名式：%(player)s
    try:
        out = text % values
        if "%(" not in text:
            raise ValueError("非命名式模板，交给下一步处理")
        return out
    except Exception:
        pass
    # 2) 位置式：按插入顺序补 %s
    try:
        return text % tuple(vals)
    except Exception:
        pass
    # 3) 个数对不上时的宽松降级：逐个吃掉 %s，多余的原样保留
    try:
        out = text
        for v in vals:
            if "%s" not in out:
                break
            out = out.replace("%s", str(v), 1)
        return out
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

    # ---- /gmback 救援指令 ------------------------------------------------
    # 必须是类属性：Endstone 在插件注册阶段（早于 on_enable）就读取它们登记
    # 指令，运行时再往 self.commands 里塞是无效的。
    commands = {
        "gmback": {
            "description": "停用 admin 型子插件，把 /admin 还原为面板自带管理页",
            "usages": ["/gmback", "/gmback list"],
            "permissions": ["greenmoon.command.gmback"],
        },
        "gmreload": {
            "description": "热重载主插件：重新读取配置并重启 QQ 机器人，无需重启服务器",
            "usages": [
                "/gmreload",              # 配置 + 机器人
                "/gmreload config",       # 只重载配置
                "/gmreload bot",          # 只重启机器人
                "/gmreload status",       # 只看当前状态，不做变更
            ],
            "permissions": ["greenmoon.command.gmreload"],
        },
    }

    permissions = {
        # 默认 op：后台控制台以最高权限执行不受此限制，游戏内则只有 OP 能用。
        # 普通玩家拿不到，符合「救援指令」的定位。
        "greenmoon.command.gmback": {
            "description": "允许使用 /gmback 救援指令（停用 admin 型子插件）",
            "default": "op",
        },
        # 热重载会影响全服（机器人短暂掉线、配置即时生效），同样限 OP。
        "greenmoon.command.gmreload": {
            "description": "允许使用 /gmreload 热重载主插件（配置与 QQ 机器人）",
            "default": "op",
        },
    }

    # ---- 主线程身份（供 _run_in_server_thread 判定"要不要调度"）-----------
    #
    # on_enable 跑在服务器的 tick 主线程上，所以在这里记下的 ident 就是
    # "主线程"的权威身份。
    #
    # ⚠ 为什么要在 on_enable 里**快照**，而不是在要用的时候现取：
    # Endstone 可能在**后台线程**里调用插件的事件处理器（事件是原版主动
    # 派发的，不保证线程）。而 self._main_thread_id 一旦被后台线程写坏，
    # _run_in_server_thread 就会把「后台线程」误认成主线程，于是**直接
    # 同步执行**本该在主线程跑的逻辑（给玩家发贴图、改背包……）。
    # 那会从后台线程去碰服务器状态 —— 轻则无效，重则直接崩服。
    #
    # 快照之后，_main_thread_id 只被赋值一次（on_enable 之前为 None），
    # 之后任何线程都删不掉、改不了，判定永远正确。
    _main_thread_id = None

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

        # ★ 抢占主线程身份的"第一写"。
        #
        # on_enable 由 Endstone 在服务器主线程上调用，所以此刻的 ident 就是
        # 主线程。上面的类属性 _main_thread_id = None 是兜底；能成功赋上值
        # 就走这条路。
        #
        # 这个赋值之所以安全（且必须在这里做），是因为它是**唯一一次**写：
        # 谁先写谁定音，之后即使事件处理器被调度到后台线程、走到 __init__ 里
        # 那句赋值，也只会覆盖成同样的值（on_enable 与 __init__ 都在主线程）。
        try:
            self._main_thread_id = threading.current_thread().ident
        except Exception:
            pass

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

        # 离线依赖：把 libs/ 下的 .whl 解压并挂进 sys.path。
        #
        # ⚠ 位置很关键 —— 必须在下面 ModLoader.bootstrap() **之前**。
        # bootstrap 会重载已启用的 py 型子插件（例如地图画子插件），
        # 而子插件的 setup() 里就要 ``import PIL``。挂晚了子插件会拿到
        # 「缺库」的结论并打出误导性的警告。
        #
        # 为什么是解压而不是直接把 .whl 塞进 sys.path：
        # 纯 Python 包两者都行，但含 C 扩展的包（Pillow）从 whl 里
        # 加载不了 .so —— ``import PIL`` 会成功，一调用就炸。
        # 详见 admin_web/libs_loader.py 的模块级说明。
        try:
            from . import libs_loader
            libs_loader.install(self.libs_dir, self.logger)
        except Exception as e:
            # 依赖加载失败绝不能拖垮插件启动
            self.logger.warning(f"离线依赖加载失败（可忽略，若不需要 libs 依赖）: {e}")

        # 子插件：mod/ 目录存放 .gmmod 与 liblist.json，依赖 whl 统一落在 libs/
        self.mods_dir = self.data_dir / "mod"
        try:
            self.mods_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.logger.warning(f"创建子插件目录失败 {self.mods_dir}: {e}")
        self.mod_loader = ModLoader(self)

        # 子插件 API：必须在 bootstrap() 之前挂载 ——
        # bootstrap 会重新加载已启用的 py 型子插件，那些插件的 main.py
        # 在模块顶层就可能 from gm import ...，此时插件实例必须已经可见。
        self._gm_event_map: Dict[Any, List[Any]] = {}
        self._gm_event_owner: Dict[str, List[Any]] = {}
        try:
            from . import gm as _gm
            _gm.attach(self)
            self.logger.info(f"子插件 API 已就绪（gm {_gm.api_version()}）")
        except Exception as e:
            self.logger.warning(f"子插件 API 挂载失败: {e}")

        try:
            boot = self.mod_loader.bootstrap()
            if boot.get("mods"):
                self.logger.info(f"已登记 {len(boot['mods'])} 个子插件")
        except Exception as e:
            self.logger.warning(f"子插件初始化失败: {e}")

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
        # deque(maxlen=200)：聊天刷屏时追加是 O(1)，
        # 不会像列表那样在超长时整表切片复制。
        self._messages: collections.deque = collections.deque(maxlen=200)
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
            # 每卡一份播报标志的集合形式（详见 _notify_server_start_once 注释）
            self._qq_start_notified = set()
            if getattr(self, "conn_mgr", None) is None:
                self.conn_mgr = ConnectionManager(self.data_dir, self.logger)
            from .hub import AdapterHub
            self.qq_hub = AdapterHub(self)   # Hub 会复用 self.conn_mgr
            self._init_qq_gateway()
        except Exception as e:
            self.logger.error(f"QQ 机器人网关初始化失败: {e}")

        try:
            self._process_delayed_tasks()
        except Exception as e:
            self.logger.warning(f"开服结算延迟任务失败: {e}")

        self.logger.info(f"Web 服务器已启动在 http://{self.web_config['host']}:{self.web_config['port']}")
        # 首次启动自动生成的密码必须让用户看到，否则他进不去面板。
        # 只打印一次（打印后清掉内存里的引用），之后靠用户自己保存。
        gen_pwd = getattr(self, "_generated_admin_password", None)
        if gen_pwd:
            self._generated_admin_password = None
            self.logger.warning(
                "=" * 62 + "\n"
                "  GreenMoon 面板已为你生成管理员账号，请立即保存：\n"
                f"    用户名：{self.web_config.get('admin_user', 'admin')}\n"
                f"    密  码：{gen_pwd}\n"
                "  这条信息只在首次启动时打印一次。登录后请到面板内修改密码。\n"
                "  （也可在 data/config.json 里手工修改 admin_user / admin_pass）\n"
                + "=" * 62
            )

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
        mod_loader = getattr(self, 'mod_loader', None)
        if mod_loader is not None:
            try:
                mod_loader.shutdown()
            except Exception:
                pass
        # 子插件 API 解绑：放在 mod_loader.shutdown() 之后，
        # 保证子插件的 teardown 里仍能正常调用 gm。
        try:
            from . import gm as _gm
            _gm.detach()
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

    # ------------------------------------------------------- 内置指令入口

    def on_command(self, sender, command, args) -> bool:
        """BDS 指令入口，处理 ``gmback`` 与 ``gmreload`` 两条指令。

        ``/gmback``        停用当前启用的 admin 型子插件，把 /admin 还原为面板自带页
        ``/gmback list``   只列出 admin 型子插件的启用状态，不做任何变更

        存在意义：admin 型子插件一旦启用就会完全接管 ``/admin``，若该页面本身
        坏了（白屏 / JS 报错），管理员在网页上已经没有入口去停用它。这条指令
        提供一条不依赖 Web 界面的退路 —— 服务端后台控制台、游戏内 OP 都能用。

        ``/gmreload``        重读主插件配置 + 重启全部 QQ 机器人（不动服务器）
        ``/gmreload config`` 只重读配置
        ``/gmreload bot``    只重启机器人
        ``/gmreload status`` 只报告当前状态，不做任何变更

        存在意义：改完 config.json 的限流/白名单，或在面板改了机器人卡片后，
        原先只能重启整个服务器才能生效 —— 对在线玩家是硬中断。这条指令把这
        两件事变成秒级、可回执、且不动世界的操作。
        """
        handled = False
        try:
            # 部分版本会把命名空间带进 name（如 greenmoon:gmback），归一化处理
            name = str(getattr(command, "name", "") or "")
            if ":" in name:
                name = name.rsplit(":", 1)[-1]
            if name not in ("gmback", "gmreload"):
                # ★ 必须显式返回 False，让服务端继续把这条指令交给别的插件。
                #
                # 这里曾经是「if name != "gmback": return self._gm_reload_command(...)」，
                # 而 _gm_reload_command 内部对未知指令**也会** return True —— 于是
                # 任何一条与本插件无关的指令都被当成「已处理」吞掉，别的插件
                # 再也收不到。指令注册是全服共享的，吞别人的指令属于越权行为。
                return False
            handled = True
            if name == "gmreload":
                return self._gm_reload_command(sender, command, args, name)

            argv = [str(a) for a in (args or [])]
            sub = argv[0].lower() if argv else ""

            ld = getattr(self, "mod_loader", None)
            if ld is None:
                self._gmback_reply(sender, "子插件系统未初始化，无法执行。")
                return True

            if sub == "list":
                self._gmback_list(sender, ld)
                return True
            if sub:
                self._gmback_reply(sender, f"未知参数 {argv[0]}，用法：/gmback 或 /gmback list")
                return True

            # ---- 无参数：执行救援 ----
            try:
                uid = ld.enabled_uuid("admin")
            except Exception as e:
                self.logger.error(f"[gmback] 读取 admin 启用状态失败: {e}")
                uid = None

            if not uid:
                self._gmback_reply(
                    sender, "当前没有启用任何 admin 型子插件，/admin 已是面板自带管理页。"
                )
                return True

            try:
                disp = str((ld.installed(uid) or {}).get("name") or uid)
            except Exception:
                disp = str(uid)

            # disable() 对页面型只改内存状态 + 写 gmod.json，不触碰任何服务端
            # 对象，因此无需切主线程（切了反而可能撞上超时返回元组）。
            try:
                r = ld.disable(uid)
            except Exception as e:
                self.logger.error(f"[gmback] 停用 {uid} 失败: {e}")
                self._gmback_reply(sender, f"停用失败：{e}")
                return True

            if bool((r or {}).get("ok")):
                self._gmback_reply(
                    sender,
                    f"已停用 admin 型子插件「{disp}」，/admin 已还原为面板自带管理页。"
                    f"若浏览器仍显示旧页面请强制刷新（Ctrl+F5）。",
                )
                self._log_admin_action(
                    f"[gmback] 已停用 admin 型子插件「{disp}」"
                    f"（由 {self._gmback_sender_name(sender)} 执行）"
                )
                self.logger.info(f"[gmback] 已停用 admin 子插件 {uid}({disp})")
            else:
                self._gmback_reply(
                    sender, f"停用未完成：{(r or {}).get('message') or '未知原因'}"
                )
            return True

        except Exception as e:
            # 绝不能把异常抛回服务端：一旦抛出去，指令可能被其他插件再执行一遍。
            #
            # 但返回值要诚实：只有**确认是本插件的指令**时才返回 True，
            # 否则异常发生在归一化/判断之前，盲目 True 会吞掉别人的指令。
            try:
                self.logger.error(f"[gmback] 指令处理异常: {e}")
                if handled:
                    self._gmback_reply(sender, f"指令执行异常：{e}")
            except Exception:
                pass
            return handled

    def _gmback_reply(self, sender, text: str) -> None:
        """安全回消息。后台控制台的 sender 同样实现 send_message。

        桩环境或极旧版本可能没有该方法，此时降级到日志，保证不抛异常。
        """
        try:
            sender.send_message(str(text))
        except Exception:
            try:
                self.logger.info(str(text))
            except Exception:
                pass

    def _gmback_sender_name(self, sender) -> str:
        """取执行者名字，用于审计日志。控制台 sender 没有 name 时回落「控制台」。"""
        try:
            got = getattr(sender, "name", None)
            if got:
                return str(got)
        except Exception:
            pass
        return "控制台"

    def _gmback_list(self, sender, ld) -> None:
        """只读：报告 admin 型子插件的启用状态，绝不改动任何状态。"""
        try:
            uid = ld.enabled_uuid("admin")
        except Exception as e:
            self._gmback_reply(sender, f"读取 admin 型子插件状态失败：{e}")
            return
        if not uid:
            self._gmback_reply(
                sender, "admin 型子插件：当前无启用项（/admin = 面板自带管理页）"
            )
            return
        try:
            disp = str((ld.installed(uid) or {}).get("name") or uid)
        except Exception:
            disp = str(uid)
        self._gmback_reply(
            sender,
            f"admin 型子插件：已启用「{disp}」(uuid={uid})，/admin 当前被其接管。"
            f"执行 /gmback 可停用它。",
        )

    # --------------------------------------------------- /gmreload 热重载

    def _gm_reload_command(self, sender, command, args, name: str) -> bool:
        """``/gmreload`` 热重载入口（配置 + QQ 机器人）。

        设计要点
        --------
        * **不重启服务器**：只重读配置、重启机器人网关。世界 / 玩家不受影响。
        * **重活丢后台线程**：重启网关要 join 线程、做网络请求，在
          ``on_command`` 里同步做会把主线程卡住（机器人多时可能几秒）。
          因此这里立刻回一句「正在重载」，真正的活在后台线程跑完再回执。
        * **每个子项独立 try**：配置重载失败不应阻止机器人重启，反之亦然。
          否则一个坏配置会让用户连机器人都重启不了。
        * **回执走 sender**：但若执行者下线了，``_gmback_reply`` 会降级到日志。

        子命令：``config`` / ``bot`` / ``status``；无参数 = 两个都做。
        """
        try:
            argv = [str(a) for a in (args or [])]
            sub = argv[0].lower() if argv else ""
            who = self._gmback_sender_name(sender)

            if sub == "status":
                self._gm_reload_status(sender)
                return True
            if sub and sub not in ("config", "bot"):
                self._gmback_reply(
                    sender,
                    f"未知参数 {argv[0]}。用法：/gmreload [config|bot|status]",
                )
                return True

            do_cfg = (not sub) or sub == "config"
            do_bot = (not sub) or sub == "bot"

            self.logger.info(f"[gmreload] 开始热重载（cfg={do_cfg} bot={do_bot}）由 {who} 触发")

            def _work() -> None:
                lines = []
                # ---- 1. 重载主插件配置 ----
                if do_cfg:
                    lines.append(self._reload_web_config())
                # ---- 2. 重启机器人网关 ----
                if do_bot:
                    lines.append(self._restart_qq_bot_for_reload())
                # ---- 汇总回执 ----
                try:
                    self._gmback_reply(
                        sender,
                        "【热重载完成】" + "；".join([x for x in lines if x]),
                    )
                except Exception:
                    pass
                # 审计日志：热重载属于「改全服行为」的操作，必须留痕
                try:
                    self._log_admin_action(
                        f"[gmreload] 热重载完成（cfg={do_cfg} bot={do_bot}）："
                        + "；".join([x for x in lines if x])
                        + f"（由 {who} 执行）"
                    )
                except Exception:
                    pass

            threading.Thread(target=_work, name="gm-reload", daemon=True).start()

            # 立刻给个回执，避免用户以为没反应又敲一遍
            self._gmback_reply(
                sender,
                "正在热重载"
                + ("配置" if do_cfg and not do_bot else "")
                + ("与" if do_cfg and do_bot else "")
                + ("QQ 机器人" if do_bot else "")
                + "…完成后会有第二条回执。服务器不会重启，玩家不受影响。",
            )
            return True
        except Exception as e:
            try:
                self.logger.error(f"[gmreload] 指令处理异常: {e}")
                self._gmback_reply(sender, f"热重载异常：{e}")
            except Exception:
                pass
            return True

    def _reload_web_config(self) -> str:
        """重读 ``config.json`` 并就地更新 ``self.web_config``。返回一句描述。"""
        try:
            old = getattr(self, "web_config", None) or {}
            new = self._load_config()
            if not isinstance(new, dict):
                return "配置：返回体异常，已保留原配置"

            # ★★★ 必须在 update 之前把「旧值」抓下来。
            #
            # 第一版把 old_host/old_port 的读取放在 old.update(new) **之后**，
            # 于是拿到的其实是刚写进去的新值，两边永远相等，
            # 「端口变了要提醒重启」这条提示**永远不会出现** —— 用户改了
            # 端口、看到「已重新读取」，以为生效了，实际服务还听在旧端口上。
            # 这类「快照时机」错误不会报错，只会静默给出错误结论。
            old_host, old_port = old.get("host"), old.get("port")

            # ★ 就地更新而不是整体替换。
            #
            # Web 服务持有的是 web_config 这个**对象引用**（构造时传进去的），
            # 若直接 self.web_config = new，运行中的 HTTP 服务仍指着旧 dict，
            # 表现为「指令说重载好了，但面板行为没变」—— 排查起来极费劲。
            # update + 删除多余键，既换内容又保引用。
            if isinstance(old, dict):
                for k in list(old.keys()):
                    if k not in new:
                        try:
                            del old[k]
                        except Exception:
                            pass
                old.update(new)
                self.web_config = old
            else:
                self.web_config = new

            # 端口 / 绑定地址变了必须提示：这两项**不支持**热重载
            # （socket 已经 listen 了）。说清楚，别让用户以为生效了。
            note = ""
            if old_host is not None and (old_host != new.get("host")
                                         or old_port != new.get("port")):
                note = (f"（注意：监听地址 {old_host}:{old_port} → "
                        f"{new.get('host')}:{new.get('port')} 需重启服务器才生效）")
            return f"配置：已重新读取{note}"
        except Exception as e:
            self.logger.warning(f"[gmreload] 重载配置失败: {e}")
            return f"配置：失败（{e}）"

    def _restart_qq_bot_for_reload(self) -> str:
        """重启全部机器人网关。返回一句描述。"""
        hub = getattr(self, "qq_hub", None)
        if hub is None:
            return "机器人：子系统未初始化，已跳过"
        try:
            libs = _load_offline_wheels(self, self.libs_dir)
            if libs is None:
                return "机器人：第三方库不可用，未重启"
            # restart() = stop_all + _boot，卡片配置会被重新读取，
            # 所以「在面板上改了机器人的账号/开关但不重启服务器」这个
            # 最常见的诉求，靠这一步就能满足。
            hub.restart(libs)
            try:
                running = [x for x in hub.status() if x.get("running")]
                return f"机器人：已重启 {len(running)} 个网关"
            except Exception:
                return "机器人：已重启"
        except Exception as e:
            self.logger.warning(f"[gmreload] 重启机器人失败: {e}")
            return f"机器人：失败（{e}）"

    def _gm_reload_status(self, sender) -> None:
        """只读报告：当前配置与服务监听状态、机器人卡片状态。"""
        try:
            cfg = getattr(self, "web_config", None) or {}
            port = cfg.get("port", "?")
            host = cfg.get("host", "?")
        except Exception:
            host, port = "?", "?"

        lines = [f"服务监听：{host}:{port}（改这两项需重启服务器）"]

        hub = getattr(self, "qq_hub", None)
        if hub is None:
            lines.append("机器人：子系统未初始化")
        else:
            try:
                items = hub.status() or []
            except Exception as e:
                items = []
                lines.append(f"机器人：状态读取失败（{e}）")
            if not items:
                lines.append("机器人：未配置任何卡片")
            for it in items:
                tone = ("已连接" if it.get("connected") else
                        ("未运行" if not it.get("running") else "连接中"))
                en = "启用" if it.get("enabled") else "禁用"
                lines.append(
                    f"· {it.get('name') or it.get('id')} [{it.get('type')}] "
                    f"{en}/{tone}"
                )
        self._gmback_reply(sender, "【当前状态】" + "　".join(lines))

    def _init_qq_gateway(self):

        hub = getattr(self, "qq_hub", None)
        if hub is None:
            return
        libs = _load_offline_wheels(self, self.libs_dir)
        if libs is None:
            self.logger.error("[QQ] 第三方库不可用，机器人网关未启动")
            return
        # 兼容旧版预检：对**每张**已启用的官方卡片各做一次 Token 预检。
        # 之前只预检主卡（primary_qqofficial 的第一张），第二张卡填错
        # AppSecret 时日志里毫无提示，用户只能对着「连不上」干瞪眼。
        #
        # 去重键必须带上「凭证指纹」而不只是 card_id：
        # _init_qq_gateway 会被 _restart_qq_gateway 反复调用（每次增删改
        # 卡片都触发一次重载）。若只按 card_id 去重，用户改完 AppID 点保存
        # 后新凭证永远不会被预检 —— 而「改完想验证一下」恰恰是最需要预检
        # 的时刻。加上指纹后：凭证一变就重新预检，没变则跳过。
        #
        # 每轮都重建一个全新的 dict（不用增量更新），这样删卡、清空凭证、
        # 改回旧值等各种边界情况下都不会残留脏条目。
        try:
            prev = getattr(self, "_qq_precheck_done", None)
            if not isinstance(prev, dict):
                prev = {}
            current: dict[str, str] = {}
            # 顺手检测「同一个 AppID 被多张卡重复配置」。
            # QQ 开放平台按 AppID 限频，两个网关实例拿着同一 AppID 并发
            # 请求 /app/getAppAccessToken 与 /gateway，会撞出
            # code 100017「接口调用超过频率限制」，表现为网关每 5 秒
            # 退出重启、时好时坏。这是配置问题，不是插件 bug，但用户
            # 从满屏重试日志里根本看不出来，所以这里主动点破。
            seen_appid: dict[str, str] = {}
            for card in hub.manager.adapters_of_type("qqofficial"):
                cid = str(card.get("id") or "")
                if not cid or not card.get("enabled"):
                    continue
                app_id = str(card.get("app_id", "") or "").strip()
                app_secret = str(card.get("app_secret", "") or "").strip()
                if not (app_id and app_secret):
                    continue          # 凭证不全，不预检也不记录
                name = str(card.get("name") or cid)
                if app_id in seen_appid:
                    self.logger.error(
                        "[QQ] 机器人 [%s] 与 [%s] 使用了同一个 AppID（%s…）。"
                        "同一 AppID 只能有一个网关在线，重复配置会互相抢登录"
                        "并触发接口频率限制，导致频繁掉线重连。"
                        "请为其中一张卡换一个 AppID，或直接停用它。",
                        name, seen_appid[app_id], app_id[:8],
                    )
                else:
                    seen_appid[app_id] = name
                # 只记指纹（不落明文密钥），既够用又不留敏感信息
                current[cid] = hashlib.sha1(
                    f"{app_id}\x00{app_secret}".encode("utf-8")
                ).hexdigest()[:16]
                if prev.get(cid) == current[cid]:
                    continue          # 凭证没变，跳过
                self.logger.info(f"[QQ] 开始 Token 预检：{name}")
                self._precheck_qq_token(self.conn_mgr.to_flat_config(card), libs)
            self._qq_precheck_done = current
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

    def _qqbot_resolve_adapter(self, card_id=None):
        """按 ``card_id`` 解析官方机器人卡片，返回 ``(卡片, id)``。

        多机器人场景下「当前操作的是哪张卡」必须由调用方显式指定，
        否则所有请求都会落到「第一张卡」上，表现为 A 卡申请的多群验证码
        出现在 B 卡的编辑页里。

        ``card_id=None`` 时退化为「主卡」（第一张启用的，否则第一张），
        仅为兼容不传 id 的旧调用点（如启动提示、跨服转发）而保留。

        注意返回的是**深拷贝**：这里是读取语义，调用方不应该通过修改
        返回值来间接改配置，需要写回请一律走 ``mgr.update(pid, patch)``。
        """
        mgr = getattr(self, "conn_mgr", None)
        if mgr is None:
            return None, None
        cid = str(card_id or "").strip()
        if cid:
            card = mgr.get(cid)          # get() 已返回深拷贝
            if card is not None:
                return card, cid
            # 指定的卡片不存在：明确告知调用方，绝不静默回落到主卡，
            # 否则「按 id 操作」会退化成「操作主卡」，正是串台的成因。
            self.logger.warning(f"[QQ] 指定的机器人卡片不存在：{cid}")
            return None, None
        card = mgr.primary_qqofficial()
        if card is None:
            return None, None
        return card, str(card.get("id") or "") or None

    def _qqbot_primary_adapter(self):
        """返回主官方卡片（深拷贝 + id）；不存在返回 (None, None)。"""
        return self._qqbot_resolve_adapter(None)

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

    def _qqbot_public_config(self, card_id=None):

        primary, _ = self._qqbot_resolve_adapter(card_id)
        if primary is None:
            return {"enabled": True, "config": dict(DEFAULT_QQBOT_CONFIG)}
        cfg = self.conn_mgr.to_flat_config(primary)
        pub = dict(cfg)
        if str(pub.get("app_secret", "")) not in ("", "__SET__"):
            pub["app_secret"] = "__SET__"
        admins = pub.get("group_admins", [])
        pub["group_admins"] = list(admins) if isinstance(admins, list) else []
        return {"enabled": bool(primary.get("enabled", True)), "config": pub}

    def _qqbot_status(self, card_id=None):
        mgr = getattr(self, "conn_mgr", None)
        primary, pid = self._qqbot_resolve_adapter(card_id) if mgr is not None else (None, None)
        cfg = self.conn_mgr.to_flat_config(primary) if (mgr is not None and primary) else {}
        enabled = bool(primary.get("enabled", True)) if primary else False
        # 缺哪些必填项 —— 必须按「当前这张卡」算。之前这里是硬编码的 []，
        # 导致 web 层那句「请先完成 AppID/AppSecret 配置」的前置校验从来
        # 没生效过；同时也让前端没法提示用户到底缺什么。
        missing = []
        if primary is not None:
            if not str(cfg.get("app_id", "") or "").strip():
                missing.append("AppID")
            if not str(cfg.get("app_secret", "") or "").strip():
                missing.append("AppSecret")
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
                "missing": missing, "binding_code": binding_code,
                "group_openid": str(cfg.get("group_openid", "") or ""),
                "groups": group_list, "pending_codes": pending_codes}

    def _qqbot_persist_group_openid(self, group_openid, card_id=None):
        mgr = getattr(self, "conn_mgr", None)
        if mgr is None:
            path = self.data_dir / "qqbot.json"
            current = self._load_qqbot_config()
            current["group_openid"] = str(group_openid or "")
            if not atomic_write_json(path, current, indent=2):
                raise RuntimeError("写入配置失败")
            bot = getattr(self, "qq_bot", None)
            if bot is not None:
                try:
                    bot.config["group_openid"] = str(group_openid or "")
                except Exception:
                    pass
            return
        primary, pid = self._qqbot_resolve_adapter(card_id)
        if primary is None or pid is None:
            return
        # primary 是深拷贝，改它不会影响配置；真正的写入在下面的 update
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

    def _qqbot_save_group_binds(self, live_config, keep_secret=False, card_id=None):
        mgr = getattr(self, "conn_mgr", None)
        primary, pid = self._qqbot_resolve_adapter(card_id) if mgr is not None else (None, None)
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

    def _qqbot_new_group_code(self, card_id=None):
        """为「加入新群」生成一个 4 位验证码，返回该码。

        ⚠ **一次只保留一个有效码** —— 这里刻意是**覆盖**写入，不是追加。

        历史行为（已改）：原实现是 ``pending.insert(0, 新码)`` 且保留最近
        20 个，等于把验证码当成一个**可累积的队列**。但群消息那边只比对
        ``pending[0]``（最新那个），于是界面上列出的一串码里，**只有最新
        的那个真正能用** —— 用户照着旧码发过去必然绑定失败，看起来就是
        「验证码失效了」，实则是显示与行为对不上。

        现在语义收紧为一码一用：
          * 申请新码 → 旧码立即作废（同一个群里同时只可能有一个待用码）；
          * 成功加入一个群 → 该码被消费掉（见 ``_consume_pending_code``）。

        注意 ``_qqbot_resolve_adapter`` 返回的是**深拷贝**，读它的
        ``pending_group_binds`` 反映不了同一进程内上一次的写入。所幸改成
        覆盖后不再需要读旧值，这个隐患自然消失。
        """
        mgr = getattr(self, "conn_mgr", None)
        primary, pid = self._qqbot_resolve_adapter(card_id) if mgr is not None else (None, None)
        if primary is None or pid is None:
            return f"{secrets.randbelow(10000):04d}"
        code = f"{secrets.randbelow(10000):04d}"
        # 覆盖式：列表里永远最多一条，与群消息只认 pending[0] 的行为严格一致。
        pending = [{"code": code, "ts": int(time.time())}]
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

    def _qqbot_save_config(self, payload, card_id=None):

        if not isinstance(payload, dict):
            raise ValueError("数据格式不正确")
        mgr = getattr(self, "conn_mgr", None)
        primary, pid = self._qqbot_resolve_adapter(card_id) if mgr is not None else (None, None)
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

    def _notify_server_start_once(self, gateway=None):
        """发送「服务器已上线」播报。

        ``gateway`` 由触发它的网关传入（QQBotGateway 收到 READY 时回调）。
        多机器人场景下这个入参是**必需的**：

          1. 不传时会退回 ``plugin.qq_bot``，那永远只是「主卡」。
             第二张卡连上后播报会走主卡的群，等于串台。
          2. 去重标志必须是**每卡一份**（``_qq_start_notified`` 从布尔值
             改成按 card_id 的集合）。用全局布尔值时，A 卡连上就把标志
             置了 True，B 卡随后连上会直接 return —— B 的群永远收不到
             上线播报，即使用户明确给 B 打开了 notify_start。
        """
        try:
            bot = gateway if gateway is not None else getattr(self, "qq_bot", None)
            if bot is None:
                return
            try:
                if bot._supervisor_stop.is_set():
                    return
            except Exception:
                pass
            cid = str(getattr(bot, "_card_id", "") or "")

            notified = getattr(self, "_qq_start_notified", None)
            if isinstance(notified, bool):
                # 兼容旧布尔值（升级前留下的实例属性），丢弃不用
                notified = None
            if not isinstance(notified, set):
                notified = set()
            if cid:
                if cid in notified:
                    return
            elif notified:
                # 非官方网关（无 card_id）没有播报语义，保持旧行为
                return

            cfg = getattr(bot, "config", {}) or {}
            if not str(cfg.get("group_openid", "")).strip():
                return
            if not cfg.get("notify_start", True):
                return
            if cid:
                notified.add(cid)
            else:
                notified.add("__legacy__")
            self._qq_start_notified = notified
            bot.send_qq_text(str(cfg.get("server_start_msg", "服务器已上线")))
        except Exception as e:
            self.logger.warning(f"[QQ] 发送上线提示失败: {e}")

    def _notify_server_stop(self):
        """发送「服务器已断开」播报。

        停机路径没有「由谁触发」的信息，因此这里遍历所有在跑的官方网关：
        用户给哪张卡开了 notify_stop，哪张卡的群就该收到。
        之前只发 ``plugin.qq_bot``（主卡），第二张卡的群永远收不到。
        """
        try:
            hub = getattr(self, "qq_hub", None)
            gateways = []
            if hub is not None:
                try:
                    for card in hub.manager.adapters_of_type("qqofficial"):
                        gw = hub.gateway_by_id(str(card.get("id") or ""))
                        if gw is not None:
                            gateways.append(gw)
                except Exception:
                    gateways = []
            if not gateways:
                legacy = getattr(self, "qq_bot", None)
                if legacy is not None:
                    gateways = [legacy]

            for bot in gateways:
                try:
                    if bot._supervisor_stop.is_set():
                        continue
                    cfg = getattr(bot, "config", {}) or {}
                    if not str(cfg.get("group_openid", "")).strip():
                        continue
                    if not cfg.get("notify_stop", True):
                        continue
                    bot.send_qq_text(str(cfg.get("server_stop_msg", "服务器已断开")))
                    time.sleep(1.5)
                except Exception as e:
                    self.logger.warning(f"[QQ] 发送下线提示失败: {e}")
        except Exception as e:
            self.logger.warning(f"[QQ] 发送下线提示失败: {e}")

    def _official_gateways(self):
        """所有在跑的官方（qqofficial）网关实例，主卡排在前面。

        WebSocket 卡的出站转发由 ``hub.websocket_relay`` 负责；官方卡则
        需要插件侧主动广播（每个网关的目标群集合都不一样）。抽成公共方法，
        避免各处各写一遍、又漏掉非主卡。
        """
        hub = getattr(self, "qq_hub", None)
        out = []
        if hub is not None:
            try:
                primary = hub.primary_gateway()
                if primary is not None:
                    out.append(primary)
                for card in hub.manager.adapters_of_type("qqofficial"):
                    gw = hub.gateway_by_id(str(card.get("id") or ""))
                    if gw is not None and gw is not primary:
                        out.append(gw)
            except Exception:
                out = []
        if not out:
            legacy = getattr(self, "qq_bot", None)
            if legacy is not None:
                out = [legacy]
        return out

    def _broadcast_official(self, flag_key, text_key, default_fmt, **fmt):
        """把一条播报发到每张「已开启该开关」的官方卡自己的群里。

        ``flag_key`` 形如 ``join_to_qq``，``text_key`` 形如 ``join_format``。
        之前这里只读 ``plugin.qq_bot``（主卡），导致第二张官方卡即使打开
        了同样的开关也收不到任何播报。
        """
        try:
            for bot in self._official_gateways():
                try:
                    cfg = getattr(bot, "config", {}) or {}
                    if not cfg.get(flag_key, True):
                        continue
                    template = str(cfg.get(text_key, default_fmt))
                    # 模板是用户可改的，两种写法都有人用：
                    #   "[游戏] %s：%s"            按位置
                    #   "[游戏] %(player)s: ..."   按名字
                    # 只能分别尝试，不能用 ``% fmt`` 一把梭 —— 那样位置式
                    # 会因「参数不够」直接抛异常，白白吞掉整条播报。
                    body = _format_template(template, fmt)
                    if body:
                        bot.send_qq_text(body)
                except Exception:
                    pass
        except Exception:
            pass

    @event_handler
    def on_player_chat(self, event: PlayerChatEvent):
        # 类型标注不能省：Endstone 在注册时会校验处理器签名，
        # 无标注会被判为 invalid event handler signature 而整条丢弃，
        # 表现为「聊天完全不通，但其他功能都正常」。
        try:
            player_name = event.player.name
            original = event.message
        except Exception:
            player_name = "未知"
            original = ""

        if original:
            self._append_message("chat", player_name, original)
            self._broadcast_official(
                "mc_to_qq", "mc_to_qq_format", "[游戏] %s：%s",
                player=player_name, message=original,
            )

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
        # broadcast_message(message) 只需一个参数；
        # broadcast(message, permission) 需要**两个**。原来兜底那句漏了
        # permission，所以主接口一失败，兜底也必然失败 —— 等于没有兜底。
        # 这里改用 broadcast_message 的等价能力：先试原方法，
        # 再退回「带权限名的 broadcast」，最后退回逐玩家 send_message。
        try:
            self.server.broadcast_message(msg)
            return
        except Exception:
            pass
        try:
            self.server.broadcast(msg, "endstone.broadcast.user")
            return
        except Exception:
            pass
        try:
            for p in list(self.server.online_players):
                try:
                    p.send_message(msg)
                except Exception:
                    pass
        except Exception:
            pass

    def _local_whitelist_names(self):
        """取当前服务器的白名单玩家名集合。

        Endstone 的 ``Server`` 对象**没有** ``whitelist`` 属性（0.11.10
        整个包都搜不到 whitelist API），白名单只能通过服务端指令操作。
        因此这里改走 ``allowlist list``：在**主线程**派发并把控制台
        输出抓回来解析 —— 原来读 ``getattr(self.server, "whitelist", None)``
        永远拿到 None，导致「当前名单」恒为空集，跨服同步时该删的删不掉。
        """
        out = set()
        try:
            text = self._run_in_server_thread(
                self._capture_command_output, "allowlist list", timeout=3.0
            )
        except Exception:
            return out
        if not isinstance(text, str):
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            # 跳过提示语（"There are N whitelisted players:" / "暂无" 等）
            if low.endswith(":") or "whitelisted" in low or "player" in low and " " not in line:
                continue
            for part in line.split(","):
                name = part.strip().strip("-*").strip()
                if not name or " " in name:
                    continue
                if name.lower() in ("there", "are", "no", "whitelist", "whitelisted", "players"):
                    continue
                out.add(name)
        return out

    def _capture_command_output(self, command):
        """在主线程派发指令并抓取控制台输出（尽力而为，失败返回空串）。

        BDS 的 `allowlist list` 没有查询 API，只能解析它的回显。
        这里把 ``server.command_sender`` 的输出钩子临时替换掉 ——
        若该版本不支持替换，则退化为「只派发不抓取」并返回空串，
        调用方需要能容忍空结果（见 ``_local_whitelist_names``）。
        """
        sender = None
        try:
            sender = self.server.command_sender
        except Exception:
            return ""
        buf = []
        orig = None
        try:
            orig = getattr(sender, "send_message", None)
            if orig is None:
                raise AttributeError("no send_message")
            sender.send_message = lambda msg, *a, **k: buf.append(str(msg))
        except Exception:
            # 无法挂钩（C 扩展对象通常不允许设置属性）→ 放弃抓取
            try:
                self.server.dispatch_command(sender, command)
            except Exception:
                pass
            return ""
        try:
            self.server.dispatch_command(sender, command)
        except Exception:
            return ""
        finally:
            try:
                sender.send_message = orig
            except Exception:
                pass
        return "\n".join(buf)

    def _apply_cross_whitelist(self, names):
        if not names:
            return
        try:
            current = self._local_whitelist_names()
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
        # 同 _exec_ban_cmd：名字来自跨服对端，拼指令前必须校验，
        # 否则换行即可注入任意控制台指令。
        safe = self._safe_player_name(name)
        if safe is None:
            return
        cmd = f"allowlist add {safe}" if op == "add" else f"allowlist remove {safe}"
        try:
            self.server.dispatch_command(self.server.command_sender, cmd)
        except Exception:
            pass

    def _apply_cross_bans(self, bans):
        try:
            # 正确属性是 ``ban_list``（``bans`` 不存在；0.11.10 里
            # PlayerBanList 提供 .entries，每项有 .name）。
            current = set()
            try:
                bl = getattr(self.server, "ban_list", None)
                if bl is not None:
                    for b in bl.entries:
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

    # 合法的玩家名：BDS 允许字母/数字/下划线，加上部分插件会加空格与点。
    # 这是一条**安全边界**，宁可放宽到「看起来像名字」也不能让换行/引号进来。
    _SAFE_NAME_RE = re.compile(r"^[0-9A-Za-z_\-\. ]{1,64}$")

    def _safe_player_name(self, name):
        """校验从跨服/外部来的玩家名，不合法返回 None。

        ★ 为什么必须有这个函数
        ---------------------
        ``_exec_ban_cmd`` / ``_exec_allowlist_cmd`` 会把名字**拼进控制台指令**
        再交给 ``dispatch_command``，也就是以**控制台权限**执行。而名字来自
        跨服对端（``ban_set`` / ``wl_set`` 报文），对端只要拿到跨服口令就能
        塞进 ``"x\\nop Steve"`` 这种值 —— 换行后 BDS 会当作第二条指令执行，
        等于**远程控制台接管**（可 op / deop / stop）。

        跨服口令是多机共享、常常明文写在配置里的，泄露面比面板密码大得多，
        所以这里必须挡死，而不是「相信对端」。
        """
        s = str(name or "").strip()
        if not s:
            return None
        if not self._SAFE_NAME_RE.match(s):
            try:
                self.logger.warning(f"[跨服] 丢弃非法玩家名（疑似指令注入）: {s[:80]!r}")
            except Exception:
                pass
            return None
        return s

    def _exec_ban_cmd(self, name, op):
        safe = self._safe_player_name(name)
        if safe is None:
            return
        cmd = f"ban {safe}" if op == "add" else f"unban {safe}"
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
            # ⚠ Attribute 在 ``endstone.attribute``，**顶层没有**。
            # 写成 ``from endstone import Attribute`` 会抛 ImportError，
            # 而这里正被 try/except 兜着 —— 于是不报错、不崩溃，
            # 只是**永远读不到真实属性值**，静默失效（最难查的一类）。
            from endstone.attribute import Attribute
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
            # ⚠ ItemStack 在 ``endstone.inventory``，**顶层没有**。
            # 同样的问题：ImportError 被 except 吞掉后 size 恒为 0，
            # 下面的 ``if size <= 0: return`` 直接让整段逻辑失效。
            from endstone.inventory import ItemStack
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
        """恢复玩家药水效果。

        Endstone 0.11.10 **没有** ``Effect`` / ``add_effect`` API
        （``endstone.potion`` 里无 ``Effect``，``Player``/``Mob`` 上也没有
        ``add_effect``）。原实现直接调 ``player.add_effect(eff)``，
        AttributeError 被吞掉 —— 跨服传送后药水效果**全部静默丢失**。

        这里改为用原版指令实现，效果等价且不依赖不存在的 API：
        先 ``effect clear`` 清干净，再逐条 ``effect give``。
        """
        if not isinstance(data, list):
            return
        try:
            name = str(getattr(player, "name", "") or "").strip()
        except Exception:
            return
        if not name:
            return
        try:
            self._run_in_server_thread(self._exec_effect_restore, name, data, timeout=3.0)
        except Exception:
            pass

    def _exec_effect_restore(self, name, data):
        """在主线程用 effect 指令重建药水效果（被 _cross_restore_effects 调度）。"""
        def _dispatch(cmd):
            try:
                self.server.dispatch_command(self.server.command_sender, cmd)
            except Exception:
                pass

        _dispatch(f"effect {name} clear")
        for e in data:
            try:
                tn = str(e.get("t") or "").strip()
                if not tn:
                    continue
                try:
                    a = int(e.get("a") or 0)
                except Exception:
                    a = 0
                a = max(1, a)
                try:
                    d = int(e.get("d") or -1)
                except Exception:
                    d = -1
                # effect give <target> <effect> [seconds] [amplifier] [hideParticles]
                # d 是剩余 tick 数，换算成秒；d<0 视为「无限」用大值近似。
                seconds = 1000000 if d < 0 else max(1, d // 20)
                amp = max(0, a - 1)          # 指令里的 amplifier 从 0 起算
                _dispatch(f"effect {name} {tn} {seconds} {amp} true")
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
            # 下面这几项 Endstone 0.11.10 里没有对应写法：
            #   absorption / hunger / saturation 都不是 Mob 的成员
            #   （Mob 只有 health / max_health / is_gliding）。
            # 保留读取逻辑不删，是为了「将来 Endstone 补上这些属性后
            # 无需改动即可生效」；但现在它们必然走 except 分支，
            # 所以改为用指令做等效补偿（见下），不再静默丢状态。
            ab = data.get("absorption")
            if ab is not None and float(ab or 0) > 0:
                # 吸收伤害：原版没有直接指令，用 absorption 效果的近似替代
                try:
                    name = str(getattr(player, "name", "") or "").strip()
                    if name:
                        self._run_in_server_thread(
                            self._exec_effect_restore, name,
                            [{"t": "absorption", "a": 1, "d": -1}], timeout=3.0)
                except Exception:
                    pass
            fd = data.get("food")
            if fd is not None:
                # 饥饿值：Mob 无 hunger 属性。用 saturation 指令做近似补偿
                try:
                    name = str(getattr(player, "name", "") or "").strip()
                    if name:
                        self._run_in_server_thread(
                            self._exec_effect_restore, name,
                            [{"t": "saturation", "a": 1, "d": 40}], timeout=3.0)
                except Exception:
                    pass
            sat = data.get("saturation")
            if sat is not None and float(sat or 0) > 0:
                try:
                    name = str(getattr(player, "name", "") or "").strip()
                    if name:
                        self._run_in_server_thread(
                            self._exec_effect_restore, name,
                            [{"t": "saturation", "a": 1, "d": 40}], timeout=3.0)
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
            # server.whitelist 不存在，改走指令回显（见 _local_whitelist_names）
            names = sorted(self._local_whitelist_names())
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
                # 正确属性是 ban_list（bans 不存在），条目列表是 .entries
                bl = getattr(self.server, "ban_list", None)
                if bl is not None:
                    for b in bl.entries:
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

    # ---------------------------------------------------- 子插件事件转发
    #
    # Endstone 的事件注册是 Plugin 级、面向插件自身方法的，子插件模块没法直接注册。
    # 所以这里给常用事件各挂一个转发方法，统一走 _gm_dispatch()，
    # 由 _gm_event_map 按事件类型分发给子插件通过 gm.event.on() 注册的函数。

    #: 已挂好转发的事件类型名，供 gm.event.supported() 查询
    _gm_event_supported = (
        "PlayerChatEvent", "BroadcastMessageEvent", "PlayerJoinEvent",
        "PlayerQuitEvent",
    )

    def _gm_dispatch(self, event) -> None:
        """把事件分发给子插件注册的处理函数。每个 handler 独立隔离异常。"""
        table = getattr(self, "_gm_event_map", None)
        if not table:
            return
        try:
            handlers = list(table.get(type(event)) or [])
        except Exception:
            return
        for fn in handlers:
            try:
                fn(event)
            except Exception as e:
                self.logger.error(f"[子插件] 事件 {type(event).__name__} 处理异常: {e}")

    @event_handler
    def on_gm_chat(self, event: PlayerChatEvent):
        self._gm_dispatch(event)

    @event_handler
    def on_gm_broadcast(self, event: BroadcastMessageEvent):
        self._gm_dispatch(event)

    @event_handler
    def on_gm_join(self, event: PlayerJoinEvent):
        self._gm_dispatch(event)

    @event_handler
    def on_gm_quit(self, event: PlayerQuitEvent):
        self._gm_dispatch(event)

    @event_handler
    def on_broadcast(self, event: BroadcastMessageEvent):
        try:
            msg = event.message
            if isinstance(msg, str):
                self._append_message("broadcast", "服务器", msg)
        except Exception:
            pass

    @event_handler
    def on_map_initialize(self, event: MapInitializeEvent):
        """地图被初始化时，顺手把「渲染器丢了」的地图画补回来。

        为什么要单独挂这个事件 —— 进服恢复（``_restore_mapart_async``）
        只在玩家**进服那一刻**跑一次，覆盖不到下面两种情况：

          1. **服务器重启后，玩家一直没重登**
             地图对象从存档重建时渲染器就是空的，仅靠进服恢复要等到
             玩家下次登录才补。而这期间别人看展示框上的地图、或者
             玩家自己打开地图，看到的都是白的。

          2. **地图被放进展示框 / 交给别人**
             展示框走实体渲染路径，不经过 ``send_map``。但只要渲染器
             在，原版就会正常把像素广播出去 —— 所以这里补上渲染器，
             展示框那边也就正常了。

        ⚠ 这个事件是**原版主动触发**的，意味着它可能：
          * 在任何线程被触发 → 只做「查表 + 挂渲染器」，不碰磁盘
          * 被非常频繁地触发（每次地图初始化一次）→ 必须极快
            （一次字典查表 + 一次 renderers 判空，纳秒级）

        所以这里**绝不读盘、绝不 send_map**，只挂渲染器。
        像素从哪来？——正常路径下根本走不到这里（渲染器还在）；
        只有服务端重启后才需要补，而那时像素文件在磁盘上 ——
        但我们不能在事件里读盘。折中：**交给进服恢复去读盘补**，
        这里只做「发现渲染器丢失就记一条日志」，方便排查。

        换句话说：这个事件的作用是**让问题可见**，真正的修复仍走进服恢复。
        """
        try:
            view = event.map
            if view is None:
                return
            try:
                mid = int(view.id)
            except Exception:
                return

            # 只关心我们自己的地图画。查索引是纯内存操作（有缓存的话）。
            # 这里刻意**不用** _mapart_of —— 那是按玩家查的。
            # 用一份轻量的「我们管过哪些 id」判断更合适，见 _mapart_has。
            if not self._mapart_has(mid):
                return

            try:
                have = len(list(view.renderers or []))
            except Exception:
                have = 0

            if have <= 0:
                self.logger.warning(
                    f"[地图画] 地图 {mid} 被初始化，但渲染器为空"
                    f"（服务端重启后属正常现象；进入对应的服务器后玩家"
                    f"重新登录会自动补回。若长期如此请检查像素文件）")
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

        self._broadcast_official(
            "join_to_qq", "join_format", "[游戏] %s 加入了服务器",
            name=event.player.name,
        )

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

        # 地图画恢复：玩家重登后补发地图贴图。
        #
        # 为什么必须放后台线程：这里跑在主线程（tick 线程），而恢复要读磁盘
        # （每人几张、每张几十 KB）。在主线程里读文件会直接卡 tick，
        # 张数一多 TPS 就掉。所以只起一个线程，真正的读盘在主线程之外做，
        # 读完了再由那个线程调度回主线程执行 send_map。
        #
        # 与上面的 _cross_restore_async 是同一个模式。
        try:
            threading.Thread(
                target=self._restore_mapart_async,
                args=(event.player, event.player.name),
                daemon=True,
            ).start()
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

        self._broadcast_official(
            "leave_to_qq", "leave_format", "[游戏] %s 离开了服务器",
            name=event.player.name,
        )

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
            # test_mode 默认**关闭**：开启时可用动态口令直接登录为 admin，
            # 属于无凭证提权路径。需要时（开发者调试）再显式打开。
            "test_mode": False,
            "security_whitelist": {"enabled": False, "ips": []},
        }
        loaded = None
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    cfg = {**default_config, **loaded}
            except Exception as e:
                self.logger.error(f"加载配置文件失败: {e}，使用默认配置")
                cfg = dict(default_config)
        else:
            cfg = dict(default_config)

        # 首次启动（或用户把账号密码清空了）时，自动生成一个随机强密码。
        # 原来「账号或密码为空即放行」等于完全没有认证：任何人访问面板
        # 都是管理员，可执行控制台指令、删文件、封禁玩家。
        # 这里改为生成随机密码并**打印到控制台**，用户复制登录后可在
        # 面板内修改 —— 既不阻碍正常使用，也不再有裸奔窗口。
        need_gen = (not str(cfg.get("admin_user") or "").strip()) or \
                   (not str(cfg.get("admin_pass") or "").strip())
        if need_gen:
            cfg["admin_user"] = str(cfg.get("admin_user") or "").strip() or "admin"
            cfg["admin_pass"] = secrets.token_urlsafe(12)
            self._generated_admin_password = cfg["admin_pass"]

        # 只有当配置与磁盘不一致时才落盘（首次生成 / 自动补密码）
        if loaded is None or need_gen:
            try:
                atomic_write_json(config_path, cfg, indent=2)
            except Exception as e:
                self.logger.error(f"写入 config.json 失败: {e}")
        return cfg

    def _persist_config(self, cfg: dict = None):

        target = cfg if cfg is not None else self.web_config
        config_path = self.data_dir / "config.json"
        try:
            if not atomic_write_json(config_path, target, indent=2):
                raise RuntimeError("原子写入失败")
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
            atomic_write_json(path, default, indent=2)
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
            if not atomic_write_json(self.data_dir / "tools_config.json", current, indent=2):
                raise RuntimeError("原子写入失败")
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
        # 走原子写：这份文件承载绑定/签到等玩家数据，
        # 直接覆盖写崩溃时会留下截断 JSON，导致数据读不出来。
        try:
            return atomic_write_json(self.data_dir / fname, data, indent=2)
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

        # 没有可用的官方网关时发码没有意义（玩家拿不到码也回不来）。
        # 用 _official_gateways() 而不是 plugin.qq_bot：后者在「主卡被禁用、
        # 第二张卡在跑」时会漏判，导致绑定码发不出去。
        if not self._official_gateways():
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

    def _register_mod_file(self, fname, fsize, display=None):
        """登记一个已上传的模组包。

        ``fname`` 是磁盘上的真实文件名（纯 ASCII，用于定位文件与进 HTTP 头），
        ``display`` 是给用户看的原始名（可以含中文）。两者刻意分开：中文名
        一旦混进 URL / header，latin-1 编码就会直接抛异常。
        """
        fname = str(fname or "")
        display = str(display or "").strip() or fname
        with self._store_lock():
            r = self._load_mod_registry()
            items = r.setdefault("mods", [])
            for it in items:
                if it.get("file") == fname:
                    it["size"] = fsize
                    it["display"] = display
                    it["uploaded"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    break
            else:
                items.insert(0, {
                    "file": fname, "size": fsize, "display": display,
                    "uploaded": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            r["mods"] = items[:int(self._load_tools_config().get("mods", {}).get("max_keep", 30) or 30)]
            self._json_save("mods/mod_registry.json", r)
        return r

    def _scan_installed_packs(self, world_name=None):

        installed = []
        try:
            root = self._find_server_root() or Path.cwd()
        except Exception:
            root = Path.cwd()

        # 目标世界：显式指定优先，否则取 server.properties 的 level-name。
        # 启用状态（world_*_packs.json）只在「世界级」目录里找，全局目录没有这两个文件。
        target_world = None
        world_source = "none"
        if world_name:
            target_world = self._locate_world(root, world_name)
            if target_world is not None:
                world_source = "given"
        if target_world is None:
            target_world = self._locate_world_for_target(root)
            if target_world is not None:
                world_source = "level-name"

        enabled_res, enabled_beh = self._read_world_enabled(target_world)

        seen = set()
        def _scan(base, scope):
            try:
                if not base.is_dir():
                    return
                for pack_dir in sorted(base.iterdir()):
                    if not pack_dir.is_dir() or pack_dir.name.startswith("."):
                        continue
                    mf = pack_dir / "manifest.json"
                    uuid_, ver, name = pack_dir.name, "?", pack_dir.name
                    ptype = None
                    mod_types = []
                    try:
                        if mf.exists():
                            m = json.loads(mf.read_text(encoding="utf-8"))
                            uuid_ = (m.get("header", {}) or {}).get("uuid", pack_dir.name)
                            ver = ".".join(map(str, (m.get("header", {}) or {}).get("version", []))) or "?"
                            name = (m.get("header", {}) or {}).get("name", pack_dir.name)
                            ptype = self._pack_type(m)
                            mod_types = sorted({
                                str(mod.get("type", ""))
                                for mod in (m.get("modules") or [])
                                if str(mod.get("type", ""))
                            })
                    except Exception:
                        pass
                    key = (str(pack_dir.resolve()), scope)
                    if key in seen:
                        continue
                    seen.add(key)
                    # 只有世界级目录里的包才谈得上「启用/停用」——
                    # 全局 resource_packs/behavior_packs 是给存档引用的仓库，
                    # 是否生效取决于各存档自己的 world_*_packs.json。
                    if scope == "world":
                        enabled = (uuid_ in enabled_res) if ptype == "resource" else (
                            (uuid_ in enabled_beh) if ptype == "behavior" else False)
                    else:
                        enabled = None
                    installed.append({
                        "name": name,
                        "uuid": uuid_,
                        "version": ver,
                        "path": str(pack_dir),
                        "parent": base.name,
                        # kind: resource / behavior / unknown，用于前后端分组
                        "kind": ptype or "unknown",
                        "scope": scope,
                        "enabled": enabled,
                        "mod_types": mod_types,
                    })
            except Exception:
                pass
        for sub in ("resource_packs", "behavior_packs"):
            _scan(root / sub, "global")
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
                _scan(wd / sub, "world")

        meta = {
            "world": target_world.name if target_world is not None else "",
            "world_path": str(target_world) if target_world is not None else "",
            "world_source": world_source,
            "enabled_resource": sorted(enabled_res),
            "enabled_behavior": sorted(enabled_beh),
            "resource_json": str(target_world / "world_resource_packs.json") if target_world else "",
            "behavior_json": str(target_world / "world_behavior_packs.json") if target_world else "",
        }
        return installed, meta

    def _read_world_enabled(self, world_path):
        """读取世界的 world_resource_packs.json / world_behavior_packs.json。

        返回 ``(已启用资源包 uuid 集合, 已启用行为包 uuid 集合)``。
        文件不存在或格式异常都按「空集合」处理，不影响页面展示。
        """
        empty = (set(), set())
        if world_path is None:
            return empty
        out = []
        for fname in ("world_resource_packs.json", "world_behavior_packs.json"):
            ids = set()
            try:
                jf = world_path / fname
                if jf.is_file():
                    data = json.loads(jf.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        for e in data:
                            if isinstance(e, dict) and e.get("pack_id"):
                                ids.add(str(e["pack_id"]))
            except Exception as e:
                self.logger.warning(f"[模组] 解析 {fname} 失败: {e}")
            out.append(ids)
        return out[0], out[1]

    def _write_world_packs(self, world_path, pack_type, entries):
        """把启用清单写回世界的 world_*_packs.json，写前留 .bak 备份。"""
        if world_path is None:
            return False, "未找到目标存档"
        fname = "world_resource_packs.json" if pack_type == "resource" else "world_behavior_packs.json"
        jf = Path(world_path) / fname
        # 原子写 + .bak：原来只有 .bak 没有原子性，写到一半崩溃照样会
        # 留下截断的 JSON，备份也救不回「当前这份已经坏了」。
        ok = atomic_write_json(jf, entries, indent=2, backup=True)
        if not ok:
            return False, f"写入 {fname} 失败"
        return True, fname

    def _find_pack_version(self, roots, uuid_, pack_type):
        """在若干候选根目录下按 uuid 找包的 header.version。

        BDS 通常用 ``uuid`` 作为包目录名，但不保证 —— 手动解压、
        第三方工具安装的包目录名可能是任意值。所以这里先试「同名目录」
        这条快路径，命中不了再遍历一遍读 manifest 比对 uuid。
        """
        sub = "resource_packs" if pack_type == "resource" else "behavior_packs"

        def _read_version(mf):
            try:
                if mf.is_file():
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    v = (m.get("header", {}) or {}).get("version")
                    return v if isinstance(v, list) and v else None
            except Exception:
                pass
            return None

        for root in roots:
            if root is None:
                continue
            base = Path(root) / sub
            # 快路径：目录名 == uuid
            v = _read_version(base / uuid_ / "manifest.json")
            if v:
                return v
        for root in roots:
            if root is None:
                continue
            base = Path(root) / sub
            try:
                if not base.is_dir():
                    continue
                for d in base.iterdir():
                    if not d.is_dir():
                        continue
                    mf = d / "manifest.json"
                    try:
                        if not mf.is_file():
                            continue
                        m = json.loads(mf.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if str((m.get("header", {}) or {}).get("uuid", "")) == uuid_:
                        v = (m.get("header", {}) or {}).get("version")
                        if isinstance(v, list) and v:
                            return v
            except Exception:
                continue
        return None

    #: 包目录名与 uuid 的对照表缓存，改包目录后由 _heal_legacy_pack_dir 写入。
    _PACK_ALIAS_FILE = "mods/pack_alias.json"

    def _pack_alias_map(self):
        d = self._json_store(self._PACK_ALIAS_FILE, {})
        return d if isinstance(d, dict) else {}

    def _save_pack_alias(self, alias):
        with self._store_lock():
            return self._json_save(self._PACK_ALIAS_FILE, alias)

    def _pack_kind_of(self, m):
        return self._pack_type(m) or ("behavior" if str(m.get("type", "")) == "data" else "unknown")

    def _regenerate_pack_uuid(self, folder: Path) -> bool:
        """给一个包目录里的 manifest.json 换上新的 uuid（三处一起换）。"""
        mf = folder / "manifest.json"
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            return False
        new_uuid = str(_uuid.uuid4())
        try:
            if isinstance(m.get("header"), dict):
                m["header"]["uuid"] = new_uuid
            else:
                m["header"] = {"uuid": new_uuid}
            ids = m.get("dependencies")
            if isinstance(ids, list):
                for dep in ids:
                    if isinstance(dep, dict):
                        dep["uuid"] = str(_uuid.uuid4())
            ok = True
        except Exception:
            ok = False
        if ok:
            # 写前留一份 .bak，改坏了还能手工还原。
            try:
                if not (folder / "manifest.json.bak").exists():
                    shutil.copyfile(str(mf), str(folder / "manifest.json.bak"))
            except Exception:
                pass
            try:
                if not atomic_write_json(mf, m, indent=2):
                    return False
            except Exception:
                return False
        return ok

    def _heal_legacy_pack_dir(self, uuid_, pack_type):
        """把「目录名不是 uuid」的包目录改名成 uuid，并把清单里的 uuid 换掉。

        旧版本上传模组时，落盘用的是 ``<随机前缀>_<原始文件名>``，解压后又直接
        拿这个名字当包目录名，于是目录名里带了中文。这种包在启动时会被 BDS
        直接忽略，也让启用列表对不上号。这里做一次性自愈：

        1. 目录名合法（等于 uuid）→ 什么都不做；
        2. 目录名非法、但已经出现在世界的启用列表里 → 只把目录改名成该 uuid
           （确保已经生效的存档不会因为改 uuid 而失效）；
        3. 目录名非法且未启用 → 改名成 uuid，同时给 manifest 换一个新 uuid，
           避免和别的世界、或者后装的同名包撞 uuid；
        4. 目录名合法但 manifest 里的 uuid 不是它 → 只把 manifest 改成目录名。

        整个过程是尽力而为的，任何异常都只记日志，绝不影响启用/停用主流程。
        """
        try:
            root = self._find_server_root() or Path.cwd()
            sub = "resource_packs" if pack_type == "resource" else "behavior_packs"
            worlds_dirs = []
            worlds_root = root / "worlds"
            if worlds_root.is_dir():
                try:
                    worlds_dirs = [w for w in sorted(worlds_root.iterdir()) if w.is_dir()]
                except Exception:
                    worlds_dirs = []
            targets = []
            gsub = root / sub
            if gsub.is_dir():
                # 关键：全局目录里的包，启用状态记在「某个存档」的 json 里，
                # 而不是某个固定的 world_path。这里把全局目录和每个存档都
                # 配一次对，否则 used 永远是空集，会把已启用的包误判成没在用。
                if worlds_dirs:
                    for wdir in worlds_dirs:
                        targets.append((gsub, wdir))
                else:
                    e = self._locate_world_for_target(root)
                    targets.append((gsub, e) if e is not None else (gsub, None))
            # 注意：不能要求 (wdir / sub) 已经存在。包装在全局目录、而世界
            # 启用列表引用它的场景很常见，此时存档里根本没有那个子目录，
            # 一旦在这里筛掉，读取到的 used 就是空的，会导致已经生效的包
            # 被误判成「没在用」而换掉 uuid。
            for wdir in worlds_dirs:
                targets.append((wdir / sub, wdir))
            alias = self._pack_alias_map()
            changed = False
            for base_dir, world_path in targets:
                if not base_dir.is_dir():
                    # 存档目录里没有 resource_packs/ 很常见（包装在全局目录），
                    # 这里必须跳过而不是 iterdir() 抛错 —— 一抛错就会中断整个
                    # 遍历，后面的包全都自愈不到。
                    continue
                for folder in sorted(base_dir.iterdir()):
                    if not folder.is_dir() or not (folder / "manifest.json").is_file():
                        continue
                    if folder.name == uuid_:
                        continue
                    try:
                        m = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    hdr_uuid = str(((m.get("header") or {}).get("uuid")) or "")
                    if hdr_uuid != uuid_:
                        continue  # 不是目标包，跳过
                    if self._pack_kind_of(m) != pack_type:
                        continue
                    # 这个 uuid 有没有被「任何一个存档」的启用列表引用？
                    # 只删 uuid、不改 uuid 的决定完全取决于它。挨个存档直接读
                    # json，而不是用 _read_world_enabled —— 那个函数当前实现
                    # 在部分路径上会返回空集，一旦判空就会把已经生效的包换掉
                    # uuid，后果是玩家的存档直接丢包，不值得赌。
                    referenced = False
                    for cand in ([world_path] if world_path is not None else []):
                        if cand is None:
                            continue
                        try:
                            jf = cand / ("world_resource_packs.json"
                                         if pack_type == "resource"
                                         else "world_behavior_packs.json")
                            if jf.is_file():
                                for e in json.loads(jf.read_text(encoding="utf-8")) or []:
                                    if isinstance(e, dict) and str(e.get("pack_id") or "") == uuid_:
                                        referenced = True
                                        break
                        except Exception:
                            pass
                        if referenced:
                            break
                    # 全局目录里的包可能被任意一个存档引用，需要扫全部存档。
                    if not referenced:
                        for wdir in worlds_dirs:
                            if wdir == world_path:
                                continue
                            try:
                                jf = wdir / ("world_resource_packs.json"
                                             if pack_type == "resource"
                                             else "world_behavior_packs.json")
                                if jf.is_file():
                                    for e in json.loads(jf.read_text(encoding="utf-8")) or []:
                                        if isinstance(e, dict) and str(e.get("pack_id") or "") == uuid_:
                                            referenced = True
                                            break
                            except Exception:
                                pass
                            if referenced:
                                break
                    dest = base_dir / uuid_
                    if dest.exists():
                        self.logger.warning(
                            f"[模组] 包 {folder.name} 想改名为 {uuid_}，但目标目录已存在，跳过"
                        )
                        continue
                    try:
                        os.rename(str(folder), str(dest))
                    except Exception as e:
                        self.logger.warning(f"[模组] 包目录改名失败 {folder.name}: {e}")
                        continue
                    self.logger.info(f"[模组] 包目录已规范化: {folder.name} → {uuid_}")
                    alias[folder.name] = uuid_
                    changed = True
                    # 没有任何存档引用它 → 换发新 uuid，免得和别处或后装的
                    # 同名包撞车。只要被引用过就一律不换：换了存档就废了。
                    if not referenced:
                        if self._regenerate_pack_uuid(dest):
                            self.logger.info(f"[模组] 包 {uuid_} 的 manifest uuid 已重置（无存档引用）")
                    else:
                        self.logger.info(f"[模组] 包 {uuid_} 已被存档引用，保留原 uuid")
                    break
            if changed:
                self._save_pack_alias(alias)
        except Exception as e:
            self.logger.warning(f"[模组] 规范化包目录失败（已忽略）: {e}")

    def set_pack_enabled(self, world_name, uuid_, pack_type, enabled):
        """启用/停用某存档里的一个资源包或行为包。

        直接增删世界目录下 ``world_resource_packs.json`` /
        ``world_behavior_packs.json`` 里的条目，改完需重启服务器生效。
        """
        uuid_ = str(uuid_ or "").strip()
        if not uuid_:
            return False, "缺少包 UUID"
        if pack_type not in ("resource", "behavior"):
            return False, "包类型必须是 resource 或 behavior"
        self._heal_legacy_pack_dir(uuid_, pack_type)
        root = self._find_server_root() or Path.cwd()
        world_path = self._locate_world(root, world_name) if world_name else None
        if world_path is None:
            world_path = self._locate_world_for_target(root)
        if world_path is None:
            return False, "未找到目标存档（请确认 level-name 或传入存档名）"

        fname = "world_resource_packs.json" if pack_type == "resource" else "world_behavior_packs.json"
        jf = world_path / fname
        entries = []
        try:
            if jf.is_file():
                data = json.loads(jf.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    entries = [e for e in data if isinstance(e, dict)]
        except Exception:
            entries = []

        exists = any(str(e.get("pack_id")) == uuid_ for e in entries)
        if enabled:
            if exists:
                return True, "该包已是启用状态"
            # 版本号从包目录的 manifest.json 里取；目录名不一定等于 uuid，
            # 交给 _find_pack_version 兜住
            version = self._find_pack_version([world_path, root], uuid_, pack_type)
            entries.append({"pack_id": uuid_, "version": version or [1, 0, 0]})
        else:
            if not exists:
                return True, "该包本就未启用"
            entries = [e for e in entries if str(e.get("pack_id")) != uuid_]

        ok, msg = self._write_world_packs(world_path, pack_type, entries)
        if not ok:
            return False, msg
        self.logger.info(
            f"[模组] 存档 {world_path.name}：{'启用' if enabled else '停用'} "
            f"{pack_type} 包 {uuid_}"
        )
        action = "启用" if enabled else "停用"
        # 立刻关服才能生效：不改运行中的服务器内存状态，只改文件并在返回值里说清楚
        return True, f"已在 {world_path.name}/{msg} 中{action}（重启服务器后生效）"

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
        # 原子写入 + 备份：这个文件写坏会让该存档**所有模组失效、玩家进不去**，
        # 且原来失败是静默吞掉的（用户只会发现世界打不开，查不到原因）。
        # 与 _write_world_packs 保持一致的 .bak 备份策略。
        ok = atomic_write_json(jf, entries, indent=2, backup=True)
        if not ok:
            self.logger.error(
                f"[模组] 写入 {jf.name} 失败，世界的模组启用状态可能未生效：{jf}"
            )

    def _list_mod_uploaded(self):
        r = self._load_mod_registry()
        mods = r.get("mods", [])
        out = []
        for it in mods:
            fname = it.get("file", "")
            fpath = self._mods_dir() / fname
            out.append({
                "file": fname,
                # 旧版本清单没有 display 字段，此时退回磁盘名，界面不会缺字。
                "name": it.get("display") or fname,
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
            if not atomic_write_json(self._diag_cfg_path, self._diag_cfg, indent=2):
                raise RuntimeError("原子写入失败")
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

    # ------------------------------------------------------------ 地图画

    def _execute_map(self, player_name: str, image_data: bytes):
        """把一张 128×128 的 24 位无压缩 BMP 画成地图并发放给玩家。

        返回 ``(是否成功, 说明文字)``。

        这是**主插件侧的权威实现**，供两条路径共用：
          * Web 面板「地图画」上传（``web.py`` 的 handler 调它）
          * 子插件通过 ``gm.mapart.give()`` 调用（``gm/mapart.py`` 调它）

        ⚠ 历史 bug（已修）：本方法原先只定义在 ``web.AdminHTTPHandler``
        **这个 HTTP 处理器类**上。面板自己调得到，但 ``gm/mapart.py`` 里
        写的是 ``plugin._execute_map(...)`` —— 插件对象上根本没有这个方法，
        于是子插件一调用就炸：

            ❌ 生成失败：生成地图失败:
               'GreenMoonPlugin' object has no attribute '_execute_map'

        修复方式是把实现搬到插件类上（所有权本就该在插件侧：它要碰
        server / scheduler / 玩家背包），web 那边改为转发调用，避免两套
        逻辑各自演化。

        调用方需要自己保证在主线程执行 —— ``gm/_sched.py`` 的
        ``call()`` 已做这一步调度。
        """
        try:
            target = self.server.get_player(player_name)
        except Exception as e:
            return False, f"查询玩家失败: {e}"
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
            map_view = self.server.create_map(dimension)
        except TypeError:
            # 老版本 endstone 的 create_map() 不接受 dimension 参数
            try:
                map_view = self.server.create_map()
            except Exception:
                return False, "当前 Endstone 不支持创建地图"
        except Exception as e:
            return False, f"创建地图失败: {e}"

        # ★ 用 attach_mapart_renderer 而不是裸 add_renderer：
        # 它保证我们的渲染器排在渲染器列表**末尾**（后画的赢），
        # 并做位置复核。理由见 ImageMapRenderer 的类 docstring ——
        # EndstoneMapView 构造时会先挂一个内置渲染器（画存档像素，
        # 对地图画恒为白色），顺序错了就会把我们的像素洗掉。
        renderer = ImageMapRenderer(pixels)
        attach_mapart_renderer(map_view, renderer)

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
                self.logger.warning(f"背包已满且掉落失败: {e}")
            self.logger.warning(f"{player_name} 背包已满，地图丢在地上")

        # ★ 记账：把地图登记进持久化索引。
        #
        # 放在「发放之后」是有意的 —— 记账失败绝不能连累玩家拿到地图，
        # 所以它整段被 try 包住，且不影响下面的返回值。
        #
        # 为什么必须记账：渲染器是**纯内存态**，玩家一掉线/服务器一重启
        # 就随地图对象一起没了；而地图画从没进过存档（endstone 没有写
        # 存档的 API），所以不记账就**没有任何东西能把它画回来**。
        # 只有记下「谁有哪张图 + 图的像素」，才能在重登时重新补挂渲染器
        # 并补发地图包（见 _restore_mapart_now）。
        marked = False
        try:
            marked = bool(self._record_mapart(int(map_view.id), player_name, image_data))
        except Exception as e:
            self.logger.warning(f"地图画登记失败（不影响本次发放）: {e}")
        # ★ 诊断：把登记结果记下来。这是恢复功能的起点 ——
        # 没记上，后面重登时一定恢复不了，而玩家只会看到"地图还是白的"。
        try:
            if marked:
                self.logger.info(
                    f"[地图画] 已登记：地图 {map_view.id} → {player_name}"
                    f"（像素 {len(image_data)} 字节）")
                # ★ 关键提醒（反汇编确认的机制，见 _restore_mapart_now）：
                # 玩家拿到地图后，像素是靠**发包链路的 hook** 才被填进
                # 地图包的（hook 会调 view->render）。若渲染链路有问题，
                # 此刻看到的就会是白图 —— 而那与"有没有登记"无关，
                # 所以这里给一句指路，避免又绕回索引去查。
                self.logger.info(
                    f"[地图画] 地图 {map_view.id} 已发放。"
                    f"若玩家看到的是白图，请看有没有"
                    f"「渲染器已被调用」那一行："
                    f"有=渲染链路正常，无=渲染器没被用上")
            else:
                self.logger.warning(
                    f"[地图画] 登记失败：地图 {map_view.id} → {player_name}，"
                    f"重登后将无法自动恢复")
        except Exception:
            pass

        # ★★ 锁定：只在**确定能保住画面**时才锁。
        #
        # ⚠ 这里踩过一个坑，别再犯：
        # 最初写的是无条件 ``map_view.locked = True``，理由是「地图画不该被
        # 原版继续探索覆盖」。但 locked 的官方语义是
        #     "A locked map may not be explored further."
        # 也就是说它会**同时禁止原版把地形画上去**。
        #
        # 而插件挂的渲染器是**内存态**，服务器重启必然丢失。于是一个
        # 「锁着 + 渲染器丢了」的地图就进退两难：
        #     * 没有渲染器 → 没人画地图画
        #     * 又被 locked   → 原版也不许画地形
        #     → **永久白板**，比不锁还糟。
        #
        # 所以锁的判据必须是「现在到底有没有渲染器」，而不是「是不是刚发的」：
        #     有渲染器 → 锁，防止原版把地图画覆盖成地形图
        #     没渲染器 → 不锁，放开让原版能探索出地形（至少不是一块白板）
        #
        # 恢复流程（_restore_mapart_now）遵循同样的判据：补挂成功才锁。
        self._apply_map_lock(map_view)

        try:
            target.send_message(
                "§c[警告] §e你收到了一张地图画，该地图渲染可能不稳定，请勿在低版本客户端使用。"
            )
        except Exception:
            pass

        self.logger.info(f"为 {player_name} 生成地图，ID={map_view.id}")
        note = "" if marked else "（未能登记，重登后可能无法恢复）"
        return True, f"已为 {player_name} 生成地图，ID: {map_view.id}（已发放）{note}"

    # ------------------------------------------------- 地图画持久化与恢复
    #
    # 背景：地图画为什么会变白
    # ------------------------
    # 两层原因，都得治：
    #
    # 1) **渲染器是内存态，重启必丢；像素也从没进过存档。**
    #    endstone 没有任何 API 能把插件画的像素写进存档
    #    （``MapItemSavedData::getPixels()`` 在全运行时只有一个调用者，
    #      就是内置渲染器，且只读）。所以渲染器一没，地图画就**无处可寻**。
    #
    # 2) **地图对象天生带一个内置渲染器**（EndstoneMapView 构造函数里挂的），
    #    它画的是**存档像素** —— 对地图画而言恒为全 0 = 纯白。
    #    而 ``EndstoneMapView::render(player)`` 是**顺序遍历**渲染器、
    #    每个都往同一张 canvas 上画、各自先 setBase 打底 —— **后画的赢**。
    #    所以我们的渲染器必须排在**末尾**，否则会被内置那个洗成白色。
    #    ⚠ 也正因为内置渲染器恒存在，**绝不能**用
    #    ``len(view.renderers)`` 判断"我们的渲染器还在不在"。
    #
    # 玩家掉线重连后，客户端贴图缓存已经清空；服务端又认为「早就发过了」，
    # 不再补发 —— 两边都以为对方有，玩家看到的就是一张白图。
    #
    # 解法不是"再画一遍"，而是**做两件事**：
    #   (a) 用磁盘上的 BMP 把我们的渲染器补挂回去（且必须排在末尾）；
    #   (b) **主动把地图整个推给玩家**：
    #
    #     player.send_map(map_view)
    #
    # 官方文档对它的说明正是这个用途：
    #   "Render a map and send it to the player in its entirety.
    #    This may be used when streaming the map in the normal manner
    #    is not desirable."
    #
    # 存储布局
    # --------
    #   <插件包>/mapart_data/mapart.json        索引：谁有哪些地图
    #   <插件包>/mapart_data/<id>.bmp           像素（正数 123.bmp / 负数 n123.bmp）
    #
    # ⚠ 为什么放在**插件包内**而不是 data_dir：
    # 地图画是插件自己的私有资源，跟着插件走。这样备份/迁移/整体打包
    # 插件目录时，地图画数据自动跟着走，不需要记得单独捞一份 data 目录。
    # 具体解析见 _mapart_root（含"包目录不可写则退回 data_dir"的兜底）。
    #
    # 为什么原样存 BMP 而不是存像素/转码：
    #   _execute_map 的输入本来就是 24 位无压缩 BMP，存原始字节 → 恢复时
    #   直接喂给同一个 decode_bmp_24bit → **来回零转换、零精度损失**。
    #   代价只是每张几十 KB，而这是本机磁盘读写，完全可接受。

    #: 地图画数据在插件目录里的子目录名。
    _MAPART_SUBDIR = "mapart_data"

    def _mapart_root(self):
        """地图画数据的根目录 —— **放在插件包内**，与代码同处一地。

        为什么不再放 ``data_dir``（也就是 ``plugins/<插件>/data/``）：
        地图画是「这个插件自己的私有资源」，跟着插件走更方便 ——
        备份、迁移、整个插件目录打包带走时，地图画数据自动跟着走，
        不用记得单独捞一份 data 目录。

        实现方式：以 ``__file__`` 为锚点定位插件包目录，
        得到 ``admin_web/mapart_data/``。这样无论 Endstone 把插件装在
        哪里、``data_folder`` 指向何处，数据始终落在插件自己身边。

        兜底：万一包目录不可写（只读挂载等），退回 ``data_dir``，
        宁可位置难看也不要功能直接失效。

        可用 ``_mapart_root_override`` 覆盖「插件包目录」（测试用；
        正常运行时该属性不存在）。注意覆盖的是**包目录**，子目录
        ``mapart_data`` 仍然会拼上去 —— 这样测试和生产走完全相同的
        目录层级，避免"测试通过但结构不一样"。
        """
        override = getattr(self, "_mapart_root_override", None)
        if override:
            try:
                d = Path(override) / self._MAPART_SUBDIR
                d.mkdir(parents=True, exist_ok=True)
                self._mapart_migrate_old(d)
                return d
            except Exception:
                pass

        candidates = []
        try:
            candidates.append(Path(__file__).resolve().parent / self._MAPART_SUBDIR)
        except Exception:
            pass
        try:
            candidates.append(self.data_dir / self._MAPART_SUBDIR)
        except Exception:
            pass

        last_err = None
        for d in candidates:
            try:
                d.mkdir(parents=True, exist_ok=True)
                # 真写一下就验明可写性 —— mkdir 成功不代表能创建文件
                # （只读挂载/权限位异常时表现不一致）。
                probe = d / ".write_test"
                probe.write_bytes(b"")
                try:
                    probe.unlink()
                except Exception:
                    pass
                if d != candidates[0]:
                    try:
                        self.logger.info(
                            f"[地图画] 插件目录不可写，像素数据改用备用目录: {d}")
                    except Exception:
                        pass
                # 换目录后老用户的数据要跟着走，否则升级即"图全没了"。
                # 抽成独立方法且全程吞异常：迁移失败不能拖垮启动。
                self._mapart_migrate_old(d)
                return d
            except Exception as e:
                last_err = e
                continue

        try:
            self.logger.warning(f"创建地图画目录失败: {last_err}")
        except Exception:
            pass
        return None

    def _mapart_dir(self):
        """返回地图画像素目录（并确保存在）。失败返回 None。"""
        return self._mapart_root()

    def _mapart_index_path(self):
        """地图画索引文件路径（与像素目录同级，都在插件包内）。"""
        d = self._mapart_root()
        if d is None:
            return None
        return d / "mapart.json"

    def _mapart_migrate_old(self, new_dir):
        """把老版本的 ``data_dir/mapart*`` 数据搬到新的插件包目录。

        老版本把索引放在 ``data_dir/mapart.json``、像素放在
        ``data_dir/mapart/<id>.bmp``。换目录后如果不搬，老用户一升级
        就会发现"图全没了"——虽然地图本身在存档里，但索引丢了就没人
        知道哪张图是谁的，恢复功能等于失效。

        只在**新目录还没有索引**时搬（避免覆盖用户新产生的数据），
        且搬完不删老文件（留着当备份，占不了多少空间）。
        任何异常都吞掉——迁移失败绝不能拖垮启动。
        """
        try:
            if new_dir is None:
                return
            if (new_dir / "mapart.json").exists():
                return  # 新目录已有索引，不搬
            old_dir = self.data_dir / "mapart"
            old_idx = self.data_dir / "mapart.json"
            moved_idx = 0
            moved_bmp = 0

            if old_idx.is_file():
                try:
                    shutil.copy2(str(old_idx), str(new_dir / "mapart.json"))
                    moved_idx = 1
                except Exception:
                    pass

            if old_dir.is_dir():
                for p in old_dir.glob("*.bmp"):
                    try:
                        shutil.copy2(str(p), str(new_dir / p.name))
                        moved_bmp += 1
                    except Exception:
                        continue

            if moved_idx or moved_bmp:
                try:
                    self.logger.info(
                        f"[地图画] 已把老数据迁移到插件目录："
                        f"索引 {moved_idx} 个，像素 {moved_bmp} 个")
                except Exception:
                    pass
        except Exception:
            pass

    def _mapart_file_name(self, map_id):
        """地图 ID → 像素文件名。

        ⚠ 为什么不直接用 ``f"{map_id}.bmp"``：地图 ID 是**有符号** 64 位，
        负数会出现 ``-219043332039.bmp`` 这种文件名。前导 ``-`` 在部分
        文件系统、打包工具、SSH/SCP 命令行里都要额外转义，很容易在某处
        被吃掉或报错。更糟的是运维手工排查时，``rm -219043332039.bmp``
        会被当成参数解析。

        所以做一次确定性编码：正数 ``123.bmp``，负数 ``n219043332039.bmp``。
        纯 ASCII、无特殊字符、正负不撞名、可肉眼反推。

        刻意写成普通方法而不是 ``@staticmethod``：测试脚本会把这个方法
        单独抠出来重装到假插件上跑，带装饰器会让签名对不上（与
        ``_pixels_ok`` 是同一个理由）。
        """
        try:
            mid = int(map_id)
        except (TypeError, ValueError):
            return None
        return f"n{-mid}.bmp" if mid < 0 else f"{mid}.bmp"

    def _apply_map_lock(self, view):
        """按「我们自己的渲染器在不在」决定这张地图锁不锁。

        这是本功能里**最容易写错**的一处，所以单独抽成一个方法，
        让生成路径和恢复路径共用同一套判据，避免两边慢慢长歪。

        ``locked`` 的官方语义（endstone 0.11.10）::

            Whether the map is locked or not.
            A locked map may not be explored further.

        关键在于它**同时**做了两件事：
          * 不让原版地图机制把地形画上去（—— 这正是地图画需要的）
          * 一旦锁上就不会再自动解锁，重启也不会

        而挂在地图上的渲染器是**纯内存态**，服务器重启必然丢失。所以：

            有渲染器 + 锁上   → 画面正确，且不会被地形覆盖     ✅ 理想
            无渲染器 + 锁上   → 没人画地图画，原版也不许画地形  ❌ 永久白板
            无渲染器 + 不锁   → 原版会探索出地形                ⚠ 画面丢了，但可用

        第三条虽然丢了地图画，但**至少不是一块焊死的白板** ——
        这比第二条好得多。所以判据是「有没有渲染器」，不是「是不是刚发的」。

        ★★ 这里的「有没有渲染器」必须是**我们自己的**，不能数总数。
        用户日志里「现有渲染器 1 个」就是这么被误读的：
        ``EndstoneMapView`` 构造时会自动挂一个内置
        ``EndstoneMapRenderer``（画存档像素），所以任何地图**天生就有 1 个**。
        数总数会让 ``have`` 恒为 True —— 于是永远锁上，而我们的渲染器
        早丢了，正好造出上面第二条描述的**永久白板**。
        改用 ``has_mapart_renderer`` 按类型认人（见 web.py 的说明）。

        返回最终是否处于锁定态。失败一律按「不锁」处理（宁可丢画面，
        也不能造出永久白板）。
        """
        try:
            have = bool(has_mapart_renderer(view, ImageMapRenderer))
        except Exception:
            have = False
        try:
            view.locked = bool(have)
            return bool(have)
        except Exception:
            return False

    def _mapart_load(self):
        """读取地图画索引，并做**结构自愈**。

        索引文件是手工可编辑的，也可能在断电时被写坏。所以这里不信任
        内容：字段类型不对就就地归一化，任何异常都退回空索引。
        返回的结构保证是 ``{"maps": dict, "players": dict}``。

        索引位置见 ``_mapart_index_path``（插件包内，与像素文件同级）。
        """
        empty = {"maps": {}, "players": {}}
        try:
            p = self._mapart_index_path()
            if p is None or not p.is_file():
                return empty
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return empty
        if not isinstance(d, dict):
            return empty

        maps = d.get("maps")
        players = d.get("players")
        if not isinstance(maps, dict):
            maps = {}
        if not isinstance(players, dict):
            players = {}

        # 逐条校验：ID 必须是整数，owner 必须是非空字符串。
        # 坏条目直接丢弃 —— 留着只会在恢复时制造无意义的报错。
        #
        # ⚠ 别再加「ID 必须 > 0」这种判断，踩过大坑：
        # Bedrock 的地图 ID 就是 ActorUniqueID，是**有符号 64 位整数**，
        # 完全可能是负数（实测见过 -219043332039）。当初想当然地按
        # 「地图 ID 是正整数」过滤，结果玩家一发图就登记失败：
        #     [地图画] 登记失败：地图 -219043332039 → xxx，重登后将无法自动恢复
        # 所以这里只要求「是个 int」，不要求符号。
        # bool 要单独排除 —— isinstance(True, int) 是 True，
        # 而 True 一旦混进来会变成 map_id=1，是纯粹的数据污染。
        clean_maps = {}
        for k, v in maps.items():
            try:
                mid = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(k, bool) or not isinstance(v, dict):
                continue
            owner = str(v.get("owner") or "").strip()
            if not owner:
                continue
            try:
                ts = float(v.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            clean_maps[mid] = {"owner": owner, "ts": ts}

        # players 索引以 maps 为准重建 —— 两边不一致时，maps 是权威。
        # 这样即使 players 被手工改坏，也能自动找回正确归属。
        by_player = {}
        for mid, info in clean_maps.items():
            by_player.setdefault(info["owner"], []).append(mid)
        for name in by_player:
            by_player[name].sort()

        return {"maps": clean_maps, "players": by_player}

    def _mapart_save(self, data):
        """原子写回索引。失败返回 False（不抛）。

        用 ``atomic_write_json`` 而不是 ``_json_save``：后者的路径绑定在
        ``data_dir`` 上，而地图画索引要落在插件包内（见 _mapart_root）。
        原子写的语义两者一致 —— 同目录临时文件 + fsync + os.replace。
        """
        try:
            p = self._mapart_index_path()
            if p is None:
                return False
            return bool(atomic_write_json(p, data, indent=2, ensure_ascii=False))
        except Exception:
            return False

    def _record_mapart(self, map_id, owner, image_data):
        """登记一张地图画：落盘像素 + 更新双向索引。

        返回是否成功。**调用方已经把本方法包在 try 里**，所以这里
        只保证不抛、返回值可判。
        """
        # bool 要在 int() 之前挡 —— isinstance(True, int) 为真，
        # 转完就分不出来了（True 会悄悄变成 map_id=1）。
        if isinstance(map_id, bool):
            return False
        try:
            map_id = int(map_id)
        except (TypeError, ValueError):
            return False
        owner = str(owner or "").strip()
        # ⚠ 不要判 map_id <= 0 —— 地图 ID 是**有符号** 64 位（见
        # _mapart_load 的注释），负数完全合法。判正负会把所有负 ID 的
        # 地图画都变成"登记失败"，玩家看到的却是"生成成功"，极难查。
        if not owner:
            return False
        if not image_data:
            return False

        d = self._mapart_dir()
        if d is None:
            return False

        try:
            # 文件名带上符号会让负 ID 变成 "-219043332039.bmp"，
            # 在部分文件系统/工具链上容易出幺蛾子。这里统一做一次
            # 「把符号映射成前缀」的编码，正负 ID 都不会撞名。
            if not atomic_write_bytes(d / self._mapart_file_name(map_id), image_data):
                return False
        except Exception:
            return False

        with self._store_lock():
            cur = self._mapart_load()
            maps = cur["maps"]

            # 同一张地图若之前登记在别人名下，先从旧主人的列表里摘掉。
            # （理论上 ID 不会重复，但服务端存档回滚等情况下可能出现。）
            old = maps.get(map_id)
            if old and old.get("owner") != owner:
                old_list = cur["players"].get(old["owner"]) or []
                cur["players"][old["owner"]] = [i for i in old_list if i != map_id]

            maps[map_id] = {"owner": owner, "ts": time.time()}
            lst = cur["players"].get(owner) or []
            if map_id not in lst:
                lst.append(map_id)
                lst.sort()
            cur["players"][owner] = lst

            return self._mapart_save(cur)

    def _forget_mapart(self, map_id):
        """注销一张地图画：删像素文件 + 摘索引。

        当前恢复流程用不到它，留给将来的「玩家删图」「清理失效记录」用 ——
        有注销能力，索引才不会只增不减。
        """
        try:
            map_id = int(map_id)
        except (TypeError, ValueError):
            return False

        ok = False
        with self._store_lock():
            cur = self._mapart_load()
            info = cur["maps"].pop(map_id, None)
            if info:
                owner = info.get("owner")
                lst = cur["players"].get(owner) or []
                cur["players"][owner] = [i for i in lst if i != map_id]
                if not cur["players"][owner]:
                    cur["players"].pop(owner, None)
                ok = self._mapart_save(cur)

        d = self._mapart_dir()
        if d is not None:
            try:
                fn = self._mapart_file_name(map_id)
                if fn:
                    p = d / fn
                    if p.exists():
                        p.unlink()
            except Exception:
                pass
        return ok

    def _mapart_of(self, player_name):
        """查某个玩家名下的地图 ID 列表（只读，返回副本）。"""
        try:
            name = str(player_name or "").strip()
            if not name:
                return []
            with self._store_lock():
                cur = self._mapart_load()
                return list(cur["players"].get(name) or [])
        except Exception:
            return []

    def _mapart_has(self, map_id):
        """这张地图是不是我们管过的地图画？

        给 ``on_map_initialize`` 用 —— 那个事件触发极频繁，
        所以这里只做一次字典查表。**绝不读盘**（索引本身是个小 JSON，
        每次地图初始化都读一遍磁盘是不可接受的）。
        """
        try:
            mid = int(map_id)
        except (TypeError, ValueError):
            return False
        try:
            with self._store_lock():
                return mid in self._mapart_load()["maps"]
        except Exception:
            return False

    def _restore_mapart_async(self, player, player_name):
        """后台线程入口：读索引 + 读像素 → 回主线程补发。

        为什么要绕后台线程：``on_player_join`` 跑在主线程（也就是 tick 线程），
        恢复要读磁盘 —— 每人几张、每张几十 KB，在主线程里读会直接卡 tick。
        这里先把该读的都读完，再一次性交给主线程，主线程只做「发」这一个动作。

        这个模式与既有的 ``_cross_restore_async`` 完全一致，不另发明一套。

        延迟 2 秒：玩家刚进服那一瞬间，客户端还在收区块、服务端还在补资源，
        这时候塞贴图包容易丢。等两秒更稳 —— 而 sleep 在后台线程里，不阻塞 tick。
        """
        try:
            name = str(player_name or "").strip()
            if not name:
                return

            ids = self._mapart_of(name)
            # ★ 诊断日志：恢复流程的每一步都要留痕。
            # 这个功能横跨「磁盘 / 主线程 / 客户端缓存」三层，一旦哪里断链，
            # 从外部看就是「地图还是白的」——完全无法区分是没记录、没读到、
            # 还是发了但客户端没收。所以每一步都打点，出问题一眼看出卡在哪。
            self.logger.info(
                f"[地图画恢复] {name} 进服，索引里有 {len(ids)} 张：{ids}")
            if not ids:
                return

            try:
                time.sleep(2.0)
            except Exception:
                pass

            # 读盘阶段：只做 IO，不碰 server。
            # 索引里有、磁盘上没有的条目直接跳过 —— 这正是"跳过该张、
            # 其余正常"这条边界的落点。
            d = self._mapart_dir()
            self.logger.info(f"[地图画恢复] 像素目录 = {d}")
            payload = []
            for mid in ids:
                data = None
                fn = self._mapart_file_name(mid)
                if d is not None and fn:
                    try:
                        p = d / fn
                        if p.is_file():
                            with open(p, "rb") as f:
                                data = f.read()
                    except Exception as e:
                        self.logger.warning(
                            f"[地图画恢复] 读取 {fn} 失败：{e}")
                        data = None
                self.logger.info(
                    f"[地图画恢复] 地图 {mid}：本地像素文件 {fn}，"
                    f"{'已读到 ' + str(len(data)) + ' 字节' if data else '缺失'}")
                payload.append((mid, data))

            # 回主线程补发。
            # 超时给得比常规宽（10s）：张数多时主线程要把每张的像素都推一遍。
            try:
                ret = self._run_in_server_thread(
                    self._restore_mapart_now, player, payload, timeout=10.0)
                # ★ 诊断：调度失败和"跑到了但一张没成功"必须能区分开。
                # _run_in_server_thread 失败时返回 (False, 原因)。
                if ret is not True and not isinstance(ret, int):
                    self.logger.warning(
                        f"[地图画恢复] 主线程调度未完成：{ret!r}")
                else:
                    self.logger.info(
                        f"[地图画恢复] 主线程执行完毕，成功张数 = {ret!r}")
                    if ret:
                        return
            except Exception as e:
                self.logger.warning(f"[地图画恢复] 调度回主线程异常：{e}")

            # ★★ 兜底：主线程那条路没成功，再退一步用**调度器轮询**重试。
            #
            # 为什么还要这一层：上面那句依赖 _run_in_server_thread 的语义，
            # 而它的失败模式里有一个很隐蔽的——"主线程任务执行超时"时它
            # **只返回失败、不取消已排队的任务**。于是可能出现「恢复其实
            # 跑成功了，但线程已经放弃等待、player 对象也过期了」这种
            # 竞态。更糟的是在 10s 之后经手一个陈旧 player 对象。
            #
            # 这里改用「每 tick 回调」的方式：把恢复动作塞进调度器，
            # 由调度器在**它自己的主线程**上执行，不依赖跨线程等待，
            # 也不需要在后台线程里持有 player 那么久。
            self._schedule_mapart_retry(player, payload, attempt=1)
        except Exception as e:
            self.logger.warning(f"[地图画恢复] 后台流程异常：{e}")

    def _schedule_mapart_retry(self, player, payload, attempt=1):
        """用调度器把恢复动作再排一次（兜底路径，最多两轮）。

        与 ``_restore_mapart_async`` 的差别：这里不再起线程、不再 sleep、
        不再跨线程等待 —— 直接让调度器在下一个 tick 里执行
        ``_restore_mapart_now``。执行结果通过闭包变量回看。

        ``attempt`` 用于限流：恢复失败最坏情况是玩家没看到地图画，
        但它**绝不会**因此卡住 tick，所以两轮足够，不搞无限重试。
        """
        try:
            holder: Dict[str, Any] = {}

            def _tick_task():
                try:
                    n = self._restore_mapart_now(player, payload)
                    holder["n"] = n
                    if not n and attempt < 2:
                        # 一张都没成功：再排一轮。
                        # 放到下一 tick 而不是立刻重排，避免和还在排队里的
                        # 上一轮任务打架（_run_in_server_thread 超时后任务
                        # 仍留在队列里，可能马上就会跑）。
                        try:
                            self.server.scheduler.run_task(
                                self,
                                lambda: self._schedule_mapart_retry(player, payload, attempt + 1),
                            )
                        except Exception:
                            pass
                    return None
                except Exception as e:
                    try:
                        self.logger.warning(f"[地图画恢复] 兜底重试异常：{e}")
                    except Exception:
                        pass
                    return None

            self.server.scheduler.run_task(self, _tick_task)
            self.logger.info(
                f"[地图画恢复] 已排入兜底重试（第 {attempt} 轮），"
                f"{len(payload)} 张")
        except Exception as e:
            try:
                self.logger.warning(f"[地图画恢复] 兜底重试排期失败：{e}")
            except Exception:
                pass

    def _restore_mapart_now(self, player, payload):
        """**主线程**：把 ``payload`` 里的地图逐张补发给玩家。

        ``payload`` 形如 ``[(map_id, bmp_bytes_or_None), ...]``。

        幂等性说明
        ----------
        ``_run_in_server_thread`` 超时后**不会取消**已排队的任务（超时分支
        只记日志就返回，task 仍在队列里，稍后照样执行）。所以本方法可能被
        执行两次，必须保证重复执行无副作用 —— ``send_map`` 是幂等的
        （重发一遍贴图包而已），``locked`` 设两次也无所谓，补挂渲染器前
        会先检查 ``renderers`` 是否为空。因此天然幂等。

        每张地图独立 try/except：一张坏掉绝不能影响其余的张。
        **本方法在任何情况下都不抛异常。**
        """
        if not payload:
            return 0

        restored = 0
        for mid, data in payload:
            try:
                # ★ 现取现用，绝不缓存 MapView。
                # server.get_map() 返回的是 C++ 侧包装对象，地图被卸载/
                # 存档重载后就会悬空 —— 持有它当缓存迟早出问题。
                view = None
                try:
                    view = self.server.get_map(int(mid))
                except Exception:
                    view = None

                if view is None:
                    self.logger.warning(
                        f"[地图画恢复] 地图 {mid}：server.get_map() 返回 None，"
                        f"该地图不存在（可能已被删除），跳过")
                    continue

                # ★ 诊断：把"查到的对象到底是什么状态"记下来。
                #
                # ⚠ 上一版这里只打「现有渲染器 N 个」，结果**误导了自己**：
                # EndstoneMapView 构造时会自动挂一个内置渲染器（画存档像素），
                # 所以 N 永远是 1，看起来"渲染器还在"。现在把**内置**和
                # **我们的**分开数，一眼就能看出我们的渲染器丢没丢。
                try:
                    _all_r = list(view.renderers or [])
                except Exception as e:
                    _all_r = []
                    _r_err = f"读取失败({e})"
                else:
                    _r_err = ""
                _n_r = len(_all_r)
                try:
                    _n_ours = sum(
                        1 for r in _all_r if isinstance(r, ImageMapRenderer))
                except Exception:
                    _n_ours = "?"
                try:
                    _lk = bool(view.locked)
                except Exception as e:
                    _lk = f"读取失败({e})"
                try:
                    _vid = int(view.id)
                except Exception:
                    _vid = "?"
                self.logger.info(
                    f"[地图画恢复] 地图 {mid}：已取回视图（id={_vid}），"
                    f"渲染器共 {_n_r} 个（其中我们的 {_n_ours} 个），"
                    f"locked={_lk}"
                    + (f" [{_r_err}]" if _r_err else ""))
                if _n_r > 0 and _n_ours == 0:
                    # 这一条是白图 bug 的直接证据，必须显式说出来，
                    # 否则下一个人又会把"共 1 个"读成"渲染器还在"。
                    self.logger.info(
                        f"[地图画恢复] 地图 {mid}：那 {_n_r} 个是 endstone 的**内置**"
                        f"渲染器（画存档像素，对地图画恒为白色），"
                        f"不是我们的 —— 我们的已丢失，将重新补挂")

                # 渲染器丢了就补挂。
                #
                # 这是本方案能覆盖「服务端重启」的原因：重启后地图对象
                # 仍在存档里，但挂在它上面的渲染器是内存态、必然丢失。
                # 只要 BMP 还在，就能重建一个等价的渲染器 —— 像素完全一致。
                #
                # 补挂前会先验尺寸：坏数据绝不能进入渲染流程，
                # 否则会在 tick 里抛异常，比画错严重得多。
                #
                # ⚠ 这里有一个必须兜住的失败路径：**补挂失败时必须解锁**。
                # 若地图还留着上次设的 locked=True，而渲染器又没补上，
                # 就会变成「没人画地图画 + 原版也不许画地形」= 永久白板。
                # 所以下面每一条 continue 之前都要先解锁。
                # ★★ 判据必须是"有没有**我们的**渲染器"，不是"总数是否为空"。
                #
                # 用户日志里那句「现有渲染器 1 个」是本 bug 的误判之源：
                # EndstoneMapView 构造时会自动挂一个**内置**
                # EndstoneMapRenderer（画存档像素，对地图画恒为全 0 = 白）。
                # 所以任何地图天生就有 1 个渲染器 —— 数总数永远 >= 1，
                # 于是恢复逻辑以为"渲染器还在"就什么都不做，
                # 而真正画地图画的那个早就随重启没了。**白图就这么来的。**
                #
                # 所以改用 has_mapart_renderer：按类型认人（isinstance），
                # 内置那个是 C++ 对象，绝不可能是 Python 类的实例。
                try:
                    have = has_mapart_renderer(view, ImageMapRenderer)
                except Exception:
                    have = False
                if not have:
                    if not data:
                        self.logger.warning(
                            f"[地图画恢复] 地图 {mid}：没有我们的渲染器且本地无像素"
                            f"文件，跳过（已解锁，交还原版探索）")
                        self._apply_map_lock(view)
                        continue
                    try:
                        pixels = decode_bmp_24bit(data)
                    except Exception as e:
                        self.logger.warning(
                            f"[地图画恢复] 地图 {mid}：像素解码失败（{e}），"
                            f"跳过（已解锁，交还原版探索）")
                        self._apply_map_lock(view)
                        continue
                    if not self._pixels_ok(pixels):
                        self.logger.warning(
                            f"[地图画恢复] 地图 {mid}：像素尺寸不合法，"
                            f"跳过（已解锁，交还原版探索）")
                        self._apply_map_lock(view)
                        continue
                    # ★ 用 attach_mapart_renderer 而不是裸 add_renderer：
                    # 它保证我们的渲染器排在列表**末尾**（后画的赢），
                    # 并做位置复核。理由见 ImageMapRenderer 的类 docstring。
                    try:
                        ok_attach = attach_mapart_renderer(
                            view, ImageMapRenderer(pixels))
                        if ok_attach:
                            self.logger.info(
                                f"[地图画恢复] 地图 {mid}：渲染器已补挂成功"
                                f"（已确认排在末尾）")
                        else:
                            self.logger.warning(
                                f"[地图画恢复] 地图 {mid}：渲染器补挂后未能确认"
                                f"排在末尾（顺序可能有误），继续尝试发送")
                    except Exception as e:
                        self.logger.warning(
                            f"[地图画恢复] 地图 {mid}：补挂渲染器失败（{e}），"
                            f"跳过（已解锁，交还原版探索）")
                        self._apply_map_lock(view)
                        continue

                # 锁定与否交给统一判据：有渲染器才锁。
                # 走到这里渲染器必然存在（要么本来就在，要么刚补上），
                # 所以正常情况下会锁上 —— 但判据仍走同一条路，不搞特例。
                self._apply_map_lock(view)

                # ★ 真正的恢复动作：把整张地图推给玩家。
                #
                # ⚠ 重要认知（反汇编 endstone 0.11.10 确认），别再被误导：
                # ``send_map`` **自己发的是一张全 0 的白图**（packet 里
                # 硬编码 128×128 并 resize 成全 0）。真正把我们的像素填
                # 进去的是**发包链路上的 hook**：
                #     BatchedNetworkPeer::sendPacket 发现待发的是地图包
                #       → 调 view->render(player)
                #       → 用渲染结果覆盖 packet.pixels
                # 而 ``render()`` 在整个运行时**只有这一个调用点**。
                #
                # 所以「send_map 执行成功」≠「画面出来了」：
                # 前者只说明包发出去了，后者还取决于 render() 有没有被
                # 触发、渲染器有没有被原版用上。
                # 这个二分法靠渲染器自己的首次调用日志来看（见
                # ImageMapRenderer.render 的埋点）——日志里出现
                # 「[地图画] 渲染器已被调用」= 渲染链路是通的。
                player.send_map(view)
                restored += 1

                # 把恢复后的关键状态一次记全，省得再来回问。
                try:
                    _n_after = len(list(view.renderers or []))
                except Exception:
                    _n_after = "?"
                try:
                    _lk_after = bool(view.locked)
                except Exception:
                    _lk_after = "?"
                self.logger.info(
                    f"[地图画恢复] 地图 {mid}：send_map 已执行 ✓"
                    f"（渲染器 {_n_after} 个，locked={_lk_after}）"
                    f" —— 若画面仍空白，请看日志里有没有"
                    f"「渲染器已被调用」那一行："
                    f"有=渲染链路正常（像素问题），"
                    f"无=渲染器没被原版用上（挂载问题）")
            except Exception as e:
                try:
                    self.logger.warning(
                        f"[地图画恢复] 地图 {mid}：处理失败（{e}），跳过")
                except Exception:
                    pass

        if restored:
            try:
                player.send_message(f"§a[地图画] §e已为你恢复 {restored} 张地图画。")
            except Exception:
                pass
            try:
                self.logger.info(
                    f"已为 {getattr(player, 'name', '?')} 恢复 {restored} 张地图画")
            except Exception:
                pass
        return restored

    def _pixels_ok(self, pixels):
        """校验点阵是合法的 128×128 RGB —— 坏数据不许进渲染流程。

        刻意写成普通方法（而不是 ``@staticmethod``）：测试脚本会把这个方法
        单独抠出来重装到假插件上跑，带装饰器会让签名对不上。
        为一个纯校验函数多传一个 ``self`` 不值得争议。
        """
        try:
            if not pixels or len(pixels) != 128:
                return False
            for row in pixels:
                if len(row) != 128:
                    return False
            return True
        except Exception:
            return False

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
            if not atomic_write_json(path, self._gamerule_values, indent=2):
                raise RuntimeError("原子写入失败")
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
            if not atomic_write_json(path, {"applications": apps}, indent=2):
                raise RuntimeError("原子写入失败")
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
            last_id = self._console_seq
            # deque 内 id 单调递增，从尾部倒着找即可；
            # 正常增量轮询只取最后几条，是 O(1)，不必每次扫满 1000 条。
            new: List[Dict[str, Any]] = []
            for m in reversed(self._console_logs):
                if m["id"] <= after:
                    break
                new.append(m)
            new.reverse()
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
        # server.properties 被写坏会导致服务端启动参数异常，
        # 所以走「备份 + 原子替换」；备份失败不阻断写入。
        if not atomic_write_text(path, content, backup=True):
            return False, "保存失败: 写入 server.properties 时出错"
        self._log_admin_action(f"已保存 server.properties")
        return True, "已保存 server.properties"

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
    attach_mapart_renderer,
    has_mapart_renderer,
    decode_bmp_24bit,
    _strip_ansi,
)
