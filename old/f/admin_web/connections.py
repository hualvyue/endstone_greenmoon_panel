"""机器人连接卡片存储层（多机器人支持的地基）。

设计对齐明流桥(LumenBridge)的 ConnectionManager，但为 GreenMoon 做精简：
每张「卡片」就是一个机器人实例的完整配置，按类型分文件持久化在
``data/connections/`` 下；旧的单机器人配置 ``data/qqbot.json`` 在首次加载时
自动迁移成一张 ``qqofficial`` 卡片，保证升级后机器人不掉线。

类型约定：
  - ``qqofficial``  QQ 官方机器人（走网关鉴权：AppID/AppSecret），
    卡片内部承载现有扁平配置结构（即旧 qqbot.json 的全部字段），
    使 Gateway 能零改动消费。
  - ``websocket``   OneBot v11 协议（正向/反向 WebSocket 直连个人号），
    参考 LumenBridge 字段。

调用方约定：
  - 新增 / 修改卡片统一走 create() / update()；读取热路径用
    get_view()（返回只读内部引用，避免拷贝开销）。
  - 配置读走 adapters / snapshot()；涉及密钥展示时用 mask 隐藏。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

try:
    from .atomic_io import atomic_write_json
except ImportError:  # 允许该模块被单独 import（测试/脚本场景）
    from atomic_io import atomic_write_json

# 支持的机器人类型
ADAPTER_TYPES = ("qqofficial", "websocket")

# ---------------------------------------------------------------------------
# 机器人「用途」
#
# 一台服务器上可以挂多个机器人，但它们**不一定都用来做群服互通**。
# 典型的三种用法：
#
#   bridge     群服互通 —— 群消息进游戏、游戏消息进群、进服/退服播报。
#   broadcast  AI 播报 —— 只接收插件主动推送的内容（AI 分析结论、
#              服务器报错、定时播报），**不**参与玩家聊天转发。
#   reserved   空着 —— 卡片照常建连、保持在线，但**不接入任何内置管线**，
#              留给第三方子插件通过 card_id 自行接管。
#
# ★ 为什么这必须是一个显式字段，而不能靠"开哪些开关"推断
# -------------------------------------------------------
# 一张卡的 sync 里 qq_to_mc=False / mc_to_qq=False 看起来就像"不互通"，
# 但那是**互通内部**的两个方向开关，语义是「互通开着，只是暂时关掉某方向」。
# 把它当成"这张卡不干互通"来推断，一旦用户只是临时关掉群→服，
# 这张卡就会从互通链路里彻底消失，回头打开开关也不生效（因为路由
# 判定发生在更外层）。用途必须由用户显式声明，不与任何开关互相推导。
#
# ★ 默认值取 bridge 是为了**升级不掉线**：老配置里没有这个键，
#   而这些卡当初就是按群服互通配的。默认成 reserved 会让升级后
#   所有群消息忽然不通 —— 那是最糟的默认。
PURPOSE_BRIDGE = "bridge"
PURPOSE_BROADCAST = "broadcast"
PURPOSE_RESERVED = "reserved"

PURPOSES = (PURPOSE_BRIDGE, PURPOSE_BROADCAST, PURPOSE_RESERVED)

#: 用途 → 人话（前端下拉、日志、面板徽章共用一份，避免两处漂移）
PURPOSE_LABELS: dict[str, str] = {
    PURPOSE_BRIDGE: "群服互通",
    PURPOSE_BROADCAST: "AI 播报",
    PURPOSE_RESERVED: "空着（等第三方插件）",
}

#: 用途 → 一句话说明（前端 tooltip / 帮助文案）
PURPOSE_HINTS: dict[str, str] = {
    PURPOSE_BRIDGE: "群消息转发到游戏、游戏消息转发到群、进服退服播报。",
    PURPOSE_BROADCAST: "只接收插件主动推送（AI 分析结论、服务器报错、定时播报），不转发玩家聊天。",
    PURPOSE_RESERVED: "保持连接但不接入内置功能，留给第三方子插件按 card_id 接管。",
}

DEFAULT_PURPOSE = PURPOSE_BRIDGE


def normalize_purpose(value: Any) -> str:
    """把任意输入归一化成合法用途值。

    ★ **静默回落**而不是抛异常。理由：这个函数会在 ``_normalize()`` 里
      对**每一张从磁盘读出来的卡**调用。如果历史配置文件里存了
      一个我们已经不再支持的值（比如将来收窄枚举），抛异常会让
      整张卡被 ``_normalize`` 判为非法而**丢弃** —— 用户升级后
      机器人直接消失了。回落到默认值最多是"用途不对"，用户改一下就行；
      丢卡是不可恢复的。
    """
    v = str(value or "").strip().lower()
    return v if v in PURPOSES else DEFAULT_PURPOSE


def _default_sync() -> dict[str, Any]:
    """单个适配器的群服互通默认配置（与旧 qqbot.json 扁平键一一对应）。"""
    return {
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
        "banned_words": [],
        "max_message_length": 256,
    }


# 默认展示两张卡片：QQ 官方机器人 + WebSocket（均未启用 / 未配置）
DEFAULT_ADAPTERS: list[dict[str, Any]] = [
    {
        "id": "qqofficial_default",
        "type": "qqofficial",
        "name": "QQ 官方机器人",
        "enabled": False,
        # 用途：群服互通（老配置默认值，见 PURPOSE_BRIDGE 的注释）
        "purpose": PURPOSE_BRIDGE,
        # —— qqofficial 专属字段（网关鉴权）——
        "app_id": "",
        "app_secret": "",
        "env": "formal",
        "qq_api": "https://api.bot.qq.com",
        "qq_msg_api": "https://api.sgroup.qq.com",
        "qq_token_api": "https://bots.qq.com",
        # 连接参数
        "reconnect_seconds": 5,
        "reconnect_seconds_max": 120,
        "auth_fail_refresh_threshold": 1,
        "send_rate_per_min": 30,
        "send_burst": 10,
        "recv_rate_per_sec": 5,
        "recv_burst": 8,
        "max_bytes_per_sec": 262144,
        "suppress_connection_log": True,
        "connect_interval": 60000,
        "extra_intents": 0,
        # 群绑定（官方域用 openid）
        "group_openid": "",
        "groups": [],
        "pending_group_binds": [],
        "group_admins": [],
        "sync": _default_sync(),
    },
    {
        "id": "ws_default",
        "type": "websocket",
        "name": "WebSocket (OneBot v11)",
        "enabled": False,
        # 用途：群服互通（与官方卡保持一致）
        "purpose": PURPOSE_BRIDGE,
        # —— websocket 专属字段 ——
        "ws_type": 0,            # 0 正向(连接 Go-CQHTTP) / 1 反向(搭建监听服务)
        "target": "",            # 正向时必填 ws:// 或 wss://
        "listen_host": "0.0.0.0",
        "listen_port": 3002,
        "access_token": "",
        "bot_qq": 0,
        "admin_qq": [],
        "main_group": "",
        "sync": _default_sync(),
    },
]

# 全类型空白模板（构建时深拷贝，避免与 DEFAULT_ADAPTERS 共享内部 dict 引用）
ADAPTER_TEMPLATES: dict[str, dict[str, Any]] = {
    a["type"]: copy.deepcopy(a) for a in DEFAULT_ADAPTERS
}

# 类型专属连接字段：qqofficial 卡片不含 websocket 五项，归一化时剔除残留键
_WS_ONLY_FIELDS = ("ws_type", "target", "listen_host", "listen_port", "access_token", "bot_qq", "admin_qq", "main_group")
_QQO_ONLY_FIELDS = ("app_id", "app_secret", "env", "qq_api", "qq_msg_api", "qq_token_api")
# 旧的扁平配置键（迁移成 sync 子对象）
_FLAT_SYNC_KEYS = tuple(_default_sync().keys())

_ADAPTER_FILES: dict[str, str] = {
    "qqofficial": "qqofficial.json",
    "websocket": "websocket.json",
}

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class ConnectionValidationError(ValueError):
    """连接配置不符合 schema 时抛出。"""


def _norm_id_list(value: Any) -> list[Any]:
    """把 int / csv 字符串 / 列表统一为列表（元素保持原样）。"""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value] if value not in (None, "") else []


def _merge_adapter(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """深合并适配器补丁（sync 子对象递归合并）。"""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if key == "sync" and isinstance(value, dict) and isinstance(result.get("sync"), dict):
            result["sync"].update(copy.deepcopy(value))
        else:
            result[key] = copy.deepcopy(value)
    return result


def _validate_adapter(adapter: dict[str, Any]) -> None:
    """校验完整的适配器配置对象。"""
    if not isinstance(adapter, dict):
        raise ConnectionValidationError("adapter 必须是对象")

    def _get(key: str, default: Any) -> Any:
        return adapter[key] if key in adapter else default

    atype = str(_get("type", ""))
    if atype not in ADAPTER_TYPES:
        raise ConnectionValidationError("adapter.type 只能为 qqofficial 或 websocket")

    name = str(_get("name", "")).strip()
    if not name or len(name) > 64:
        raise ConnectionValidationError("adapter.name 必须是 1 至 64 个字符")
    if type(_get("enabled", False)) is not bool:
        raise ConnectionValidationError("adapter.enabled 必须是布尔值")

    # 用途：三选一。这里**严格校验**（不像 normalize_purpose 那样静默回落）
    # —— 走到这一步的是用户刚在面板上提交的值，写错就该当场报错，
    # 而不是悄悄存成别的用途让用户以为改成功了。
    purpose = _get("purpose", DEFAULT_PURPOSE)
    if str(purpose) not in PURPOSES:
        raise ConnectionValidationError(
            "adapter.purpose 只能为 bridge（群服互通）/ broadcast（AI 播报）/ "
            "reserved（空着等第三方插件）"
        )

    if atype == "qqofficial":
        app_id = str(_get("app_id", "")).strip()
        if not re.fullmatch(r"[0-9A-Za-z]{0,32}", app_id):
            raise ConnectionValidationError("adapter.app_id 只能是不超过 32 位的字母数字")
        secret = _get("app_secret", "")
        if not isinstance(secret, str) or not (0 <= len(secret) <= 128):
            raise ConnectionValidationError("adapter.app_secret 必须是长度不超过 128 的字符串")
        env = str(_get("env", "formal")).strip().lower()
        if env not in ("formal", "sandbox"):
            raise ConnectionValidationError("adapter.env 只能为 formal 或 sandbox")
        interval = _get("connect_interval", 60000)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) \
                or not 0 <= float(interval) <= 86400000:
            raise ConnectionValidationError("adapter.connect_interval 必须是 0 至 86400000 之间的毫秒数")
        intents = _get("extra_intents", 0)
        if isinstance(intents, bool) or not isinstance(intents, int) \
                or not 0 <= intents <= (1 << 31) - 1:
            raise ConnectionValidationError("adapter.extra_intents 必须是 0 至 2^31-1 之间的整数")
        for key in ("group_openid",):
            gid = str(_get(key, "")).strip()
            if gid and not re.fullmatch(r"[0-9A-Za-z_-]{4,64}", gid):
                raise ConnectionValidationError(f"adapter.{key} 中包含无效的 openid")
        groups = _get("groups", [])
        if isinstance(groups, list):
            for g in groups:
                if isinstance(g, dict) and not str(g.get("openid", "") or "").strip():
                    raise ConnectionValidationError("adapter.groups 中包含无效的群绑定条目")
    else:
        ws = _get("ws_type", 0)
        if isinstance(ws, bool) or ws not in (0, 1):
            raise ConnectionValidationError("adapter.ws_type 只能为 0（正向）或 1（反向）")
        target = str(_get("target", "") or "").strip()
        if target:
            from urllib.parse import urlparse
            parsed = urlparse(target)
            if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
                raise ConnectionValidationError("adapter.target 必须是有效的 ws:// 或 wss:// 地址")
        host = str(_get("listen_host", "")).strip()
        if not host or "://" in host:
            raise ConnectionValidationError("adapter.listen_host 必须是有效监听地址")
        port = _get("listen_port", 0)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ConnectionValidationError("adapter.listen_port 必须位于 1 至 65535 之间")
        token = _get("access_token", "")
        if not isinstance(token, str) or len(token) > 4096:
            raise ConnectionValidationError("adapter.access_token 必须是长度不超过 4096 的字符串")

    qq = _get("bot_qq", 0)
    if isinstance(qq, bool) or not isinstance(qq, int) or not 0 <= qq <= 999999999999999:
        raise ConnectionValidationError("adapter.bot_qq 必须是非负整数")

    for key in ("admin_qq", "main_group"):
        items = _norm_id_list(_get(key, []))
        if len(items) > 100:
            raise ConnectionValidationError(f"adapter.{key} 最多包含 100 个号码")
        for item in items:
            if isinstance(item, bool):
                raise ConnectionValidationError(f"adapter.{key} 中不能包含布尔值")
            try:
                number = int(str(item).strip())
            except (TypeError, ValueError) as exc:
                raise ConnectionValidationError(f"adapter.{key} 中包含无效号码") from exc
            if number <= 0 or number > 999999999999999:
                raise ConnectionValidationError(f"adapter.{key} 中包含超出范围的号码")

    sync = _get("sync", {})
    if not isinstance(sync, dict):
        raise ConnectionValidationError("adapter.sync 必须是对象")
    merged = copy.deepcopy(_default_sync())
    for key, value in sync.items():
        if key not in merged:
            raise ConnectionValidationError(f"adapter.sync 不支持的配置项：{key}")
        expected = merged[key]
        if isinstance(expected, bool) and type(value) is not bool:
            raise ConnectionValidationError(f"adapter.sync.{key} 必须是布尔值")
        if isinstance(expected, (dict, list)):
            if not isinstance(value, type(expected)):
                raise ConnectionValidationError(f"adapter.sync.{key} 必须是 {'对象' if isinstance(expected, dict) else '列表'}")
        elif isinstance(expected, str) and not isinstance(value, str):
            raise ConnectionValidationError(f"adapter.sync.{key} 必须是字符串")
    if "max_message_length" in sync:
        length = int(sync["max_message_length"])
        if not 1 <= length <= 4096:
            raise ConnectionValidationError("adapter.sync.max_message_length 必须位于 1 至 4096 之间")


class ConnectionManager:
    """机器人连接卡片加载器：data/connections/ 目录分类型持久化 + 旧配置迁移。

    存储布局::

        plugins/endstone-greenmoon-panel/data/
        ├── qqbot.json                 # 旧版单机器人配置（迁移后重命名 .migrated）
        └── connections/
            ├── qqofficial.json
            └── websocket.json

    兼容：connections/ 目录不存在时回退读取旧版 qqbot.json（机器人不掉线），
    首次写盘自动切换为新结构并把旧文件改名 qqbot.json.migrated。
    之后旧 qqbot.json 的字段归档进首张 qqofficial 卡片的 sync 子对象。
    """

    def __init__(self, data_folder: Path, logger: Any) -> None:
        self.logger = logger
        self.dir = Path(data_folder) / "connections"
        self.legacy_path = Path(data_folder) / "qqbot.json"
        self.adapters: list[dict[str, Any]] = []
        self._legacy_layout = False
        self._lock = threading.RLock()
        self._group_keys_cache: frozenset[str] | None = None
        self._admin_keys_cache: frozenset[str] | None = None
        self.load()

    # ------------------------------------------------------------------ load
    def _invalidate_key_caches(self) -> None:
        self._group_keys_cache = None
        self._admin_keys_cache = None

    def _read_items(self, fpath: Path) -> list[Any]:
        if not fpath.is_file():
            return []
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            self.logger.error(f"[机器人] 读取 {fpath.name} 失败: {e}")
            self._backup_file(fpath)
            return []
        items = data.get("adapters") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    def _backup_file(self, fpath: Path) -> None:
        if not fpath.is_file():
            return
        try:
            shutil.copy2(fpath, fpath.with_suffix(".json.bak"))
        except OSError:
            pass

    def load(self) -> None:
        """加载 connections/ 目录；旧版 qqbot.json 单文件回退并迁移。"""
        with self._lock:
            self._legacy_layout = False
            fresh = not any((self.dir / name).is_file() for name in _ADAPTER_FILES.values())
            sourced: list[tuple[Path | None, Any]] = []
            if fresh and self.legacy_path.is_file():
                legacy_cfg = self._read_legacy_flat()
                if legacy_cfg is not None:
                    self._legacy_layout = True
                    card = self._card_from_legacy(legacy_cfg)
                    if card is not None:
                        sourced.append((self.legacy_path, card))
                    else:
                        self.logger.warning("[机器人] 旧 qqbot.json 未启用，按新结构初始化")
            else:
                for name in _ADAPTER_FILES.values():
                    fpath = self.dir / name
                    sourced.extend((fpath, item) for item in self._read_items(fpath))

            original_ids = [str(item.get("id")) for _, item in sourced if isinstance(item, dict)]
            adapters = self._normalize(sourced)
            if not adapters:
                adapters = copy.deepcopy(DEFAULT_ADAPTERS)
            self.adapters = adapters
            self._invalidate_key_caches()
            if fresh or original_ids != [str(a.get("id")) for a in self.adapters]:
                self._write_locked()

    def _read_legacy_flat(self) -> dict[str, Any] | None:
        """读取旧 qqbot.json（扁平结构），损坏或缺数据库返回 None。"""
        if not self.legacy_path.is_file():
            return None
        try:
            data = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            self.logger.error(f"[机器人] 读取旧 qqbot.json 失败: {e}")
            self._backup_file(self.legacy_path)
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _card_from_legacy(flat: dict[str, Any]) -> dict[str, Any]:
        """把旧扁平配置包成一张 qqofficial 卡片：sync 收纳互通类键。"""
        card = copy.deepcopy(ADAPTER_TEMPLATES["qqofficial"])
        card["id"] = "qqofficial_default"
        card["name"] = "QQ 官方机器人"
        for key, value in flat.items():
            if key == "sync" or key in ("enabled",):
                continue
            if key in _FLAT_SYNC_KEYS:
                card["sync"][key] = copy.deepcopy(value)
            else:
                card[key] = copy.deepcopy(value)
        card["enabled"] = bool(flat.get("enabled", True))
        return card

    def _normalize(self, sourced: list[tuple[Path | None, Any]]) -> list[dict[str, Any]]:
        """补全字段、按类型归位并逐张校验；单个非法仅告警跳过。"""
        normalized: list[dict[str, Any]] = []
        dropped: set[Path] = set()
        for source, item in sourced:
            if not isinstance(item, dict) or str(item.get("type")) not in ADAPTER_TYPES:
                dropped.add(source) if source else None
                continue
            merged = _merge_adapter(self._blank(item.get("type", "qqofficial")), item)
            # 用途归一化放在**校验之前**：老配置里没有这个键（或存了将来
            # 可能被移除的值）时静默回落到默认，而不是让整张卡因校验
            # 不过被丢弃。丢卡是不可恢复的，用途错只是改一下的事。
            merged["purpose"] = normalize_purpose(merged.get("purpose"))
            if merged.get("type") == "qqofficial":
                for key in _WS_ONLY_FIELDS:
                    merged.pop(key, None)
            else:
                for key in _QQO_ONLY_FIELDS:
                    merged.pop(key, None)
            if not str(merged.get("id") or "") or not _ID_RE.match(str(merged["id"])):
                merged["id"] = self._gen_id(str(merged.get("type")))
            else:
                merged["id"] = str(merged["id"])
            try:
                _validate_adapter(merged)
            except ConnectionValidationError as e:
                self.logger.warning(f"[机器人] 卡片 {merged.get('name')} 非法，已跳过: {e}")
                if source is not None:
                    dropped.add(source)
                continue
            normalized.append(merged)
        for source in dropped:
            if source is not None:
                self._backup_file(source)
        return normalized

    def _blank(self, atype: str) -> dict[str, Any]:
        template = ADAPTER_TEMPLATES.get(atype)
        if template is None:
            raise ConnectionValidationError(f"未知适配器类型：{atype}")
        result = copy.deepcopy(template)
        result["id"] = self._gen_id(atype)
        result["name"] = {"qqofficial": "QQ 官方机器人", "websocket": "WebSocket"}.get(atype, atype)
        # 新建卡片默认「群服互通」—— 这是绝大多数用户的用途，
        # 让默认值直接可用，用户想改成播报/空着再去下拉里选。
        result["purpose"] = normalize_purpose(result.get("purpose"))
        return result

    @staticmethod
    def _gen_id(atype: str) -> str:
        prefix = {"qqofficial": "qo", "websocket": "ws"}.get(atype, "ad")
        return f"{prefix}_{uuid.uuid4().hex[:8]}"

    def _write_locked(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        groups: dict[str, list[dict[str, Any]]] = {t: [] for t in _ADAPTER_FILES}
        for adapter in self.adapters:
            groups.setdefault(str(adapter.get("type", "qqofficial")), []).append(adapter)
        # 走统一的原子写（临时文件 + fsync + os.replace）。
        # 各类型分文件存储，逐份替换 —— 单个文件内保证原子性。
        for atype, fname in _ADAPTER_FILES.items():
            fpath = self.dir / fname
            if not atomic_write_json(
                fpath, {"version": 1, "adapters": groups.get(atype, [])}, indent=4
            ):
                raise RuntimeError(f"写入适配器配置失败: {fname}")
        if self._legacy_layout and self.legacy_path.is_file():
            try:
                self.legacy_path.rename(self.legacy_path.with_suffix(".json.migrated"))
            except OSError:
                pass
            self._legacy_layout = False

    # ------------------------------------------------------------------ CRUD
    # 掩码的长度上限：真·密钥远比这个长，用长度把「纯星号的真密码」
    # 和「我们生成的掩码」区分开。掩码一定等于原文长度，
    # 而真实 token/secret 极不可能刚好是 1~64 个星号。
    MASK_MAX_LEN = 64

    @staticmethod
    def is_masked(value: Any) -> bool:
        if not isinstance(value, str) or not value:
            return False
        if set(value) != {"*"}:
            return False
        # ★ 只认「合理长度的纯星号」为掩码。
        # 原实现是 ``set(value) == {"*"}`` —— 一个**真实**的
        # access_token 如果恰好全是星号（虽然罕见，但这是用户自己
        # 粘贴进来的值，我们不能假定它的内容），就会被误判成
        # 「用户没改」，提交后被静默丢弃 → 表现为「token 怎么改都保存不上」。
        # 加长度上限后，超长纯星号按真实值处理。
        return len(value) <= ConnectionManager.MASK_MAX_LEN

    def snapshot(self, *, mask: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            data = copy.deepcopy(self.adapters)
        if mask:
            for adapter in data:
                for key in ("access_token", "app_secret"):
                    value = adapter.get(key)
                    if value:
                        adapter[key] = "*" * len(str(value))
        return data

    def get(self, adapter_id: str) -> dict[str, Any] | None:
        with self._lock:
            for adapter in self.adapters:
                if adapter.get("id") == adapter_id:
                    return copy.deepcopy(adapter)
        return None

    def get_view(self, adapter_id: str) -> dict[str, Any] | None:
        """按 id 返回卡片内部字典的只读引用（不拷贝，消息热路径用）。"""
        with self._lock:
            for adapter in self.adapters:
                if adapter.get("id") == adapter_id:
                    return adapter
        return None

    def create(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ConnectionValidationError("adapter 必须是对象")
        atype = str(patch.get("type", "")).strip()
        if atype not in ADAPTER_TYPES:
            raise ConnectionValidationError("adapter.type 只能为 qqofficial 或 websocket")
        created = self._blank(atype)
        created["enabled"] = True
        with self._lock:
            same = [a for a in self.adapters if a.get("type") == atype]
            base_name = {"qqofficial": "QQ 官方机器人", "websocket": "WebSocket"}.get(atype, atype)
            created["name"] = base_name if not same else f"{base_name} {len(same) + 1}"
        patch = {k: v for k, v in patch.items() if k not in ("id",)}
        patch = self._unmask_patch(patch, created)
        created = _merge_adapter(created, patch)
        _validate_adapter(created)
        with self._lock:
            self._ensure_unique_name(created, exclude=None)
            self.adapters.append(created)
            self._invalidate_key_caches()
            self._write_locked()
            return copy.deepcopy(created)

    def update(self, adapter_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ConnectionValidationError("adapter 必须是对象")
        patch = {k: v for k, v in patch.items() if k not in ("id", "type")}
        with self._lock:
            current: dict[str, Any] | None = None
            current_index = -1
            for index, adapter in enumerate(self.adapters):
                if adapter.get("id") == adapter_id:
                    current, current_index = adapter, index
                    break
            if current is None:
                raise ConnectionValidationError(f"未找到卡片：{adapter_id}")
            patch = self._unmask_patch(patch, current)
            merged = _merge_adapter(current, patch)
            _validate_adapter(merged)
            self._ensure_unique_name(merged, exclude=adapter_id)
            self.adapters[current_index] = merged
            self._invalidate_key_caches()
            self._write_locked()
            return copy.deepcopy(merged)

    def delete(self, adapter_id: str) -> bool:
        with self._lock:
            for index, adapter in enumerate(self.adapters):
                if adapter.get("id") == adapter_id:
                    if len(self.adapters) <= 1:
                        raise ConnectionValidationError("至少需要保留一张机器人卡片")
                    self.adapters.pop(index)
                    self._invalidate_key_caches()
                    self._write_locked()
                    return True
        return False

    def _unmask_patch(self, patch: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        if self.is_masked(patch.get("access_token")) or self.is_masked(patch.get("app_secret")):
            patch = dict(patch)
            if self.is_masked(patch.get("access_token")):
                patch["access_token"] = str(current.get("access_token", "") or "")
            if self.is_masked(patch.get("app_secret")):
                patch["app_secret"] = str(current.get("app_secret", "") or "")
        return patch

    def _ensure_unique_name(self, adapter: dict[str, Any], *, exclude: str | None) -> None:
        names = {
            str(a.get("name")) for a in self.adapters
            if a.get("id") != exclude and a is not adapter
        }
        name = str(adapter.get("name", "")).strip()
        if name not in names:
            adapter["name"] = name
            return
        for i in range(2, 100):
            suffix = f" {i}"
            candidate = f"{name[: 64 - len(suffix)]}{suffix}"
            if candidate not in names:
                adapter["name"] = candidate
                return
        suffix = f" {uuid.uuid4().hex[:4]}"
        adapter["name"] = f"{name[: 64 - len(suffix)]}{suffix}"

    # ------------------------------------------------------------------ views
    def adapters_view(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.adapters)

    def adapters_of_type(self, atype: str) -> list[dict[str, Any]]:
        with self._lock:
            return [a for a in self.adapters if a.get("type") == atype]

    def adapters_of_purpose(self, purpose: str) -> list[dict[str, Any]]:
        """按用途筛卡片（只返回**已启用**的）。

        路由方（hub 出站、AI 播报、正则回复）**只关心启用的卡** ——
        把「是否启用」也在这里一并过滤掉，避免每个调用点各写一遍
        ``if card.get("enabled")``，漏一处就是"禁用的卡还在收发消息"。
        """
        want = normalize_purpose(purpose)
        with self._lock:
            return [
                a for a in self.adapters
                if a.get("enabled") and normalize_purpose(a.get("purpose")) == want
            ]

    def purpose_of(self, adapter_id: str) -> str:
        """取某张卡的用途；卡不存在时返回默认值。

        返回默认值而不是空串：调用方（比如 hub 出站）会拿它做 ``in`` 判断，
        空串会滑过所有分支落进"谁都不认"的境地 —— 那张卡就彻底哑了。
        """
        cid = str(adapter_id or "").strip()
        if not cid:
            return DEFAULT_PURPOSE
        with self._lock:
            for a in self.adapters:
                if str(a.get("id")) == cid:
                    return normalize_purpose(a.get("purpose"))
        return DEFAULT_PURPOSE

    def purpose_map(self) -> dict[str, str]:
        """``{card_id: purpose}`` 快照。

        出站链路每次发消息都要查用途，逐张 ``purpose_of()`` 就要反复
        拿锁。这里一次拿完锁返回一个 dict，调用方在本地查表 ——
        热路径（每次玩家聊天都走）值得这么写。
        """
        with self._lock:
            return {
                str(a.get("id") or ""): normalize_purpose(a.get("purpose"))
                for a in self.adapters
            }

    @staticmethod
    def is_configured(adapter: dict[str, Any]) -> bool:
        """卡片是否已填有效连接信息（决定 Hub 是否为其建连）。"""
        if str(adapter.get("type")) == "qqofficial":
            return bool(str(adapter.get("app_id", "") or "").strip()
                        and str(adapter.get("app_secret", "") or "").strip())
        if int(adapter.get("ws_type", 0) or 0) == 0:
            return bool(str(adapter.get("target", "") or "").strip())
        return int(adapter.get("listen_port", 0) or 0) > 0

    @staticmethod
    def parse_groups_loose(value: Any) -> list[str]:
        result: list[str] = []
        for item in _norm_id_list(value):
            token = str(item).strip()
            if token and token not in result:
                result.append(token)
        return result

    @staticmethod
    def parse_groups(value: Any) -> list[int]:
        """把 ``main_group`` 的 csv 解析成 int 群号列表（供 OneBot 适配器使用）。"""
        result: list[int] = []
        for raw in ConnectionManager.parse_groups_loose(value):
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue
            if number > 0:
                result.append(number)
        return result

    def group_key_set(self) -> frozenset[str]:
        cached = self._group_keys_cache
        if cached is not None:
            return cached
        with self._lock:
            if self._group_keys_cache is not None:
                return self._group_keys_cache
            keys: set[str] = set()
            for adapter in self.adapters:
                keys.update(self.parse_groups_loose(adapter.get("group_openid")))
                keys.update(self.parse_groups_loose(adapter.get("main_group")))
            frozen = frozenset(keys)
            self._group_keys_cache = frozen
            return frozen

    def admin_key_set(self) -> frozenset[str]:
        cached = self._admin_keys_cache
        if cached is not None:
            return cached
        with self._lock:
            if self._admin_keys_cache is not None:
                return self._admin_keys_cache
            keys: set[str] = set()
            for adapter in self.adapters:
                keys.update(self.parse_groups_loose(adapter.get("group_admins")))
                keys.update(self.parse_groups_loose(adapter.get("admin_qq")))
            frozen = frozenset(keys)
            self._admin_keys_cache = frozen
            return frozen

    def primary_qqofficial(self) -> dict[str, Any] | None:
        """主 QQ 官方卡片（深拷贝）：第一个启用的，否则第一张。"""
        candidates = self.adapters_of_type("qqofficial")
        if not candidates:
            return None
        for adapter in candidates:
            if adapter.get("enabled"):
                return copy.deepcopy(adapter)
        return copy.deepcopy(candidates[0])

    def to_flat_config(self, adapter: dict[str, Any]) -> dict[str, Any]:
        """把一张 qqofficial 卡片展开成网关消费的扁平配置（兼容旧键）。

        ★ ``purpose`` 必须在这里剔除。它是**路由元数据**，只有 hub / 插件
          侧的路由判定需要它；网关拿到它没有任何用途，反而会因为
          ``QQBotGateway`` 把 config 原样存进 ``self.config`` 而消失在
          一堆网关配置里 —— 以后想找"这张卡是干嘛的"就得翻网关内部状态。
          插件侧要读用途一律走 ``conn_mgr.purpose_of(card_id)``。
        """
        flat: dict[str, Any] = {}
        for key, value in adapter.items():
            if key in ("type", "id", "name", "enabled", "sync", "purpose"):
                continue
            if key in ("suppress_connection_log", "connect_interval", "extra_intents"):
                continue
            flat[key] = value
        sync = adapter.get("sync") or {}
        for key, value in sync.items():
            flat[key] = value
        return flat
