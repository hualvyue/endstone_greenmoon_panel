# -*- coding: utf-8 -*-
"""GreenMoon 面板 —— 玩家可信度（权重）安全系统。

设计要点（详见实现内注释）：

* **判定依据是设备指纹 ``device_id``，不是 IP。**
  ``device_id`` 是客户端自报的持久 id，与网络无关 —— 换 Wi-Fi、
  开移动流量跑到外省，``device_id`` 都不变，因此不会被误判成新设备。
  IP 只记录进档案供服主查看，**绝不参与任何计分**。

* **加减分全部可在 WebUI 调节**，配置在 ``tools_config.json`` 的 ``trust`` 分区。

* **未打卡扣分走独立扣分池**（``signin.total_penalty``），有下限，
  打卡成功后清零 —— 所以不会出现「断签一个月直接扣到没法进服」。

* **权限联动默认关闭**，且只走 ``add_attachment`` / ``set_permission``。
  ``Player.permission_level`` 在 0.11.x 是**只读**的，
  本系统**永远不碰 OP**、不调 ``is_op()``。

* **主线程约束**：``PlayerLoginEvent`` / ``PlayerJoinEvent`` 都在服务器
  tick 线程上跑。登录阶段只查内存缓存（未命中一律放行 = fail-open）；
  进服阶段在主线程把 ``player`` 的标量抠出来，再丢给线程池读盘。
  后台线程**绝不触碰 ``player`` 对象**（对象可能已析构），
  需要玩家实例时只用 ``server.get_player(name)`` 且必须在主线程调用。
"""

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from .atomic_io import atomic_write_json
except Exception:  # pragma: no cover - 独立导入（测试）时的兜底
    def atomic_write_json(path, data, indent=2):
        """原子写 JSON：先写临时文件再 ``os.replace``。

        直接 ``open(path,"w")`` 写到一半崩溃会留下截断 JSON，
        整份玩家档案就读不出来了。
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
            return True
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return False


#: 可信度配置分区的默认值。必须与 ``core.DEFAULT_TOOLS_CONFIG["trust"]`` 保持一致。
TRUST_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "min_score": 30,
    "kick_message": (
        "§c你的可信度不足，暂时无法进入服务器\n"
        "§7当前可信度：§e{score}§7  要求：§a{required}\n"
        "§7还差：§e{gap}\n"
        "§7原因：{reason}\n"
        "§7提升方式：§f每日进服游玩、群内签到、绑定 QQ"
    ),
    "notify_on_join": True,

    "add_device_match": 3.0,
    "add_device_new_first": 2.0,
    "add_bound_qq": 5.0,
    "add_daily_play": 2.0,
    "add_signin_base": 1.0,
    "add_signin_streak_per_day": 0.5,
    "add_signin_streak_cap": 10,
    "add_whitelist_device": 4.0,

    "sub_device_burst": -5.0,
    "device_burst_window_hours": 24,
    "device_burst_threshold": 3,
    "sub_signin_miss_per_day": -2.0,
    "signin_grace_days": 2,
    "signin_miss_floor": -20.0,
    "signin_miss_floor_enabled": True,

    "hard_floor": 0.0,
    "hard_ceiling": 100.0,
    "manual_step_max": 50.0,
    "history_limit": 200,

    "perm_tier_enabled": False,
    "perm_tiers": [
        {"score": 90.0, "group": "trusted_vip", "label": "高信任"},
        {"score": 60.0, "group": "trusted", "label": "可信玩家"},
    ],

    "backup": {"enabled": True, "keep": 5},
}

#: 人类可读的计分原因（写进历史、也用于踢出提示）
REASON_LABELS = {
    "device_match": "使用已记录设备",
    "device_new_first": "首次使用新设备",
    "device_burst": "短期内频繁使用全新设备",
    "bound_qq": "绑定 QQ 账号",
    "daily_play": "每日进服游玩",
    "signin": "每日打卡",
    "signin_miss": "连续未打卡",
    "whitelist_device": "白名单申请设备被信任",
    "manual_admin": "服主手动调整",
    "manual_add": "服主手动加权",
    "manual_sub": "服主手动扣权",
    "init": "档案初始化",
}


def reason_text(code: str) -> str:
    """把原因代码翻译成中文，未知代码原样返回。"""
    code = str(code or "").strip()
    return REASON_LABELS.get(code, code or "综合评估")


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def _parse_day(s: str):
    """``"2026-09-20"`` → ``date``；解析失败返回 ``None``。

    只认 ``%Y-%m-%d``，与 ``_do_signin`` 里 ``time.strftime("%Y-%m-%d")``
    的写法保持一致；不引入时区库，避免跨时区服务器上出现「今天」不一致。
    """
    s = str(s or "").strip()
    if not s:
        return None
    try:
        t = time.strptime(s[:10], "%Y-%m-%d")
        return (t.tm_year, t.tm_mon, t.tm_mday)
    except Exception:
        return None


def _day_diff(a: str, b: str) -> Optional[int]:
    """``b - a`` 的天数差（整数）。任一解析失败返回 ``None``。"""
    da, db = _parse_day(a), _parse_day(b)
    if da is None or db is None:
        return None
    import datetime
    try:
        return (datetime.date(*db) - datetime.date(*da)).days
    except Exception:
        return None


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


class TrustManager:
    """玩家可信度管理器。

    由 ``GreenMoonPlugin`` 持有（``self.trust``），所有状态都落在这个
    对象上；``core.py`` 只做事件转发。

    典型调用顺序：

    1. 插件 ``__init__``：``self.trust = TrustManager(self)``
    2. 插件启动后：``self.trust.preload()``（后台线程读盘填缓存）
    3. 登录事件：``self.trust.check_login(player)`` → ``(ok, kick_text)``
    4. 进服事件：``self.trust.begin_join(player)``（主线程只抠标量）
    5. 定时任务：``self.trust.tick()``（每日结算）
    """

    #: 缓存 TTL：``_load_tools_config`` 本身要读盘，不能每次登录都调
    CONFIG_TTL = 5.0

    # ------------------------------------------------------------------ 初始化

    def __init__(self, plugin):
        self.plugin = plugin
        self.logger = plugin.logger

        #: 主存储目录：``plugins/<插件名>/player/``（用户指定）
        self._player_dir = plugin.data_dir / "player"
        #: 自动备份目录：``plugins/<插件名>/player_backup/<时间戳>/``
        #: 用户已知并接受「插件包目录可能被覆盖」的风险，所以额外在这里
        #: 留一份镜像，更新/重装后还能捞回来。
        self._backup_dir = plugin.data_dir / "player_backup"

        #: 内存档案缓存：``{key: record}``。登录阶段只读它，绝不读盘。
        self._cache: Dict[str, dict] = {}
        self._cache_ready = threading.Event()
        self._lock = threading.RLock()

        self._cfg_cache: Optional[dict] = None
        self._cfg_ts = 0.0

        #: 进程级「今天是否已做过全局结算」，避免每次 tick 都全量扫盘
        self._last_settle_day = ""

    # ------------------------------------------------------------------ 配置

    @staticmethod
    def normalize_cfg(raw: Any) -> dict:
        """把 ``trust`` 分区补全成完整配置（缺失项填默认值，数值项纠类型）。"""
        cfg = json.loads(json.dumps(TRUST_DEFAULTS))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in cfg:
                    cfg[k] = v
                else:
                    cfg[k] = v
        for k in ("add_device_match", "add_device_new_first", "add_bound_qq",
                  "add_daily_play", "add_signin_base", "add_signin_streak_per_day",
                  "add_whitelist_device", "sub_device_burst",
                  "sub_signin_miss_per_day", "signin_miss_floor",
                  "hard_floor", "hard_ceiling", "manual_step_max"):
            cfg[k] = _num(cfg.get(k), TRUST_DEFAULTS[k])
        for k in ("device_burst_window_hours", "device_burst_threshold",
                  "signin_grace_days", "add_signin_streak_cap", "history_limit"):
            cfg[k] = _int(cfg.get(k), TRUST_DEFAULTS[k])
        cfg["min_score"] = _num(cfg.get("min_score"), 0.0)
        if not isinstance(cfg.get("perm_tiers"), list):
            cfg["perm_tiers"] = json.loads(json.dumps(TRUST_DEFAULTS["perm_tiers"]))
        if not isinstance(cfg.get("backup"), dict):
            cfg["backup"] = dict(TRUST_DEFAULTS["backup"])
        return cfg

    def cfg(self, force: bool = False) -> dict:
        """取可信度配置（带 TTL 内存缓存）。

        为什么要缓存：登录事件跑在主线程，而 ``_load_tools_config`` 要读盘。
        每个玩家登录都读一次磁盘会拖 tick。
        """
        now = time.time()
        if (not force) and self._cfg_cache is not None \
                and (now - self._cfg_ts) < self.CONFIG_TTL:
            return self._cfg_cache
        try:
            raw = (self.plugin._load_tools_config() or {}).get("trust", {})
        except Exception as e:
            self.logger.warning(f"[可信度] 读取配置失败，使用默认值: {e}")
            raw = {}
        cfg = self.normalize_cfg(raw)
        self._cfg_cache = cfg
        self._cfg_ts = now
        return cfg

    def invalidate_cfg(self):
        """配置被 WebUI 改动后调用，让下一次读取立即生效。"""
        self._cfg_cache = None
        self._cfg_ts = 0.0

    # ------------------------------------------------------------------ 存储键

    def storage_key(self, player) -> Tuple[str, str]:
        """算出存档文件名与降级类型。

        优先级：``xuid`` → ``unique_id``（前缀 ``u_``）→ ``name``（前缀 ``n_``）。

        为什么要降级：离线（盗版/自建鉴权）玩家拿不到 xuid，
        此时若直接不记录，等于安全系统对这类玩家完全失效。
        降级档案在 UI 上会标黄，并提示「改名会重置权重」。
        """
        xuid = ""
        try:
            xuid = str(getattr(player, "xuid", "") or "").strip()
        except Exception:
            xuid = ""
        if xuid:
            return xuid, "xuid"

        uid = ""
        for attr in ("unique_id", "uuid"):
            try:
                v = getattr(player, attr, None)
                if v:
                    uid = str(v).strip()
                    break
            except Exception:
                continue
        if uid:
            return "u_" + uid, "uuid"

        name = ""
        try:
            name = str(getattr(player, "name", "") or "").strip().lower()
        except Exception:
            name = ""
        if name:
            return "n_" + name, "name"
        return "", "none"

    @staticmethod
    def key_from_payload(payload: dict) -> Tuple[str, str]:
        """从「标量快照」算存储键（后台线程用，不碰 player 对象）。"""
        xuid = str((payload or {}).get("xuid") or "").strip()
        if xuid:
            return xuid, "xuid"
        uid = str((payload or {}).get("unique_id") or "").strip()
        if uid:
            return "u_" + uid, "uuid"
        name = str((payload or {}).get("name") or "").strip().lower()
        if name:
            return "n_" + name, "name"
        return "", "none"

    @staticmethod
    def safe_filename(key: str) -> str:
        """把存储键转成安全的文件名。

        key 可能是 ``n_<玩家名>``，而玩家名理论上包含路径分隔符等字符，
        必须过滤掉，否则会写到 ``player/`` 目录之外。
        """
        out = []
        for ch in str(key or ""):
            if ch.isalnum() or ch in "-_.":
                out.append(ch)
            else:
                out.append("_")
        s = "".join(out).strip("._")
        return s or "unknown"

    def _path_for(self, key: str) -> Path:
        return self._player_dir / (self.safe_filename(key) + ".json")

    # ------------------------------------------------------------------ 档案读写

    def _blank(self, key: str, key_type: str, name: str = "") -> dict:
        return {
            "schema": 1,
            "xuid": key if key_type == "xuid" else "",
            "unique_id": key[2:] if key_type == "uuid" else "",
            "name": name,
            "name_lower": str(name or "").lower(),
            "key_type": key_type,
            "first_seen": _now(),
            "last_seen": _now(),
            "score": 0.0,
            "last_score_change": {"at": "", "delta": 0.0, "reason": "init"},
            "binding": {},
            "devices": [],
            "device_burst": {"window_start": _now(), "new_ids": [],
                             "new_id_sessions": [], "penalty_applied_at": ""},
            "signin": {"last": "", "streak": 0, "best_streak": 0,
                       "last_settle_date": "", "total_penalty": 0.0},
            "daily_play": {"last": ""},
            "flags": {},
            "history": [],
        }

    @staticmethod
    def _repair(rec: dict) -> dict:
        """把磁盘上读到的档案补齐字段（应对旧版本 / 手工编辑过的文件）。"""
        if not isinstance(rec, dict):
            return {}
        rec.setdefault("schema", 1)
        rec.setdefault("devices", [])
        if not isinstance(rec["devices"], list):
            rec["devices"] = []
        rec.setdefault("history", [])
        if not isinstance(rec["history"], list):
            rec["history"] = []
        rec.setdefault("flags", {})
        if not isinstance(rec["flags"], dict):
            rec["flags"] = {}
        rec.setdefault("binding", {})
        if not isinstance(rec["binding"], dict):
            rec["binding"] = {}
        db = rec.setdefault("device_burst", {})
        if not isinstance(db, dict):
            db = {}
            rec["device_burst"] = db
        db.setdefault("window_start", "")
        if not isinstance(db.get("new_ids"), list):
            db["new_ids"] = []
        if not isinstance(db.get("new_id_sessions"), list):
            db["new_id_sessions"] = []
        db.setdefault("penalty_applied_at", "")
        sg = rec.setdefault("signin", {})
        if not isinstance(sg, dict):
            sg = {}
            rec["signin"] = sg
        sg.setdefault("last", "")
        sg["streak"] = _int(sg.get("streak"), 0)
        sg["best_streak"] = _int(sg.get("best_streak"), 0)
        sg.setdefault("last_settle_date", "")
        sg["total_penalty"] = _num(sg.get("total_penalty"), 0.0)
        dp = rec.setdefault("daily_play", {})
        if not isinstance(dp, dict):
            dp = {}
            rec["daily_play"] = dp
        dp.setdefault("last", "")
        rec["score"] = _num(rec.get("score"), 0.0)
        if not isinstance(rec.get("last_score_change"), dict):
            rec["last_score_change"] = {"at": "", "delta": 0.0, "reason": "init"}
        return rec

    def load(self, key: str) -> Optional[dict]:
        """从磁盘读一份档案。不存在/损坏返回 ``None``。"""
        key = str(key or "").strip()
        if not key:
            return None
        p = self._path_for(key)
        try:
            if not p.exists():
                return None
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
            rec = self._repair(rec)
            return rec or None
        except Exception as e:
            self.logger.warning(f"[可信度] 读取档案失败 {p.name}: {e}")
            return None

    def load_or_blank(self, key: str, key_type: str = "xuid", name: str = "") -> dict:
        rec = self.load(key)
        return rec if rec else self._blank(key, key_type, name)

    def save(self, key: str, rec: dict) -> bool:
        if not key or not isinstance(rec, dict):
            return False
        try:
            self._player_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return bool(atomic_write_json(self._path_for(key), rec, indent=2))

    def iter_keys(self) -> List[str]:
        """列出全部档案键（用于结算 / 列表 / 备份）。"""
        out: List[str] = []
        try:
            if not self._player_dir.exists():
                return out
            for p in sorted(self._player_dir.glob("*.json")):
                if p.name.endswith(".tmp") or p.name.endswith(".bak"):
                    continue
                out.append(p.stem)
        except Exception:
            pass
        return out

    def iter_records(self) -> List[Tuple[str, dict]]:
        out = []
        for k in self.iter_keys():
            rec = self.load(k)
            if rec:
                out.append((k, rec))
        return out

    # ------------------------------------------------------------------ 缓存

    def cache_get(self, key: str) -> Optional[dict]:
        with self._lock:
            rec = self._cache.get(key)
            # 返回浅拷贝：调用方（登录拦截）只读分数，不该被并发改写影响
            return json.loads(json.dumps(rec)) if rec else None

    def cache_put(self, key: str, rec: dict):
        if not key:
            return
        with self._lock:
            self._cache[key] = json.loads(json.dumps(rec))

    def cache_drop(self, key: str):
        with self._lock:
            self._cache.pop(key, None)

    def flush(self) -> int:
        """把内存缓存里**尚未落盘**的档案写回磁盘。返回写出的条数。

        ★ 为什么需要它（关闭时的真实丢数据点）
        ------------------------------------
        本模块的设计是"启动时预载进内存、之后读走内存、写才落盘"。
        但 ``plugin._executor`` 是个 ``ThreadPoolExecutor``，而
        ``on_disable`` 老代码用的是 ``shutdown(wait=False)`` ——
        它会**丢弃排队中的任务**。于是：

          * 玩家刚进服触发的 ``_join_worker``（算分后落盘）可能还在队列里；
          * ``daily_settle`` 的整批写也可能没跑完；
          服务器一停，这些改动只存在于内存，随进程消失。用户看到的是
          "关服前明明扣了分，重开后分数又回来了"。

        这里在关池子前主动把缓存整体刷一遍。已经落过盘的档案再写一次
        是无害的（内容相同），所以不做"脏标记"也不会出问题 ——
        档案数量是"服的玩家数"量级，一次全量写在关服时刻完全可以接受。

        ⚠ 必须容忍任何单条失败：某一份档案写不进去（磁盘满 / 文件被占）
          不能连累其余档案，更不能抛异常打断 ``on_disable`` 的后续清理。
        """
        written = 0
        try:
            with self._lock:
                snapshot = {k: json.loads(json.dumps(v))
                            for k, v in self._cache.items()}
        except Exception:
            return 0
        for key, rec in snapshot.items():
            try:
                if self.save(key, rec):
                    written += 1
            except Exception:
                continue
        if written:
            try:
                self.logger.info(f"[可信度] 关闭前已刷写 {written} 份档案")
            except Exception:
                pass
        return written

    def preload(self):
        """把 ``player/*.json`` 全部读进内存缓存。

        启动时在后台线程跑一次。之后所有「读」都走缓存，
        只有「写」才落盘并同步缓存 —— 这样登录阶段永远不会碰磁盘。
        """
        try:
            n = 0
            for key, rec in self.iter_records():
                self.cache_put(key, rec)
                n += 1
            self.logger.info(f"[可信度] 已预载 {n} 份玩家档案")
        except Exception as e:
            self.logger.warning(f"[可信度] 预载失败: {e}")
        finally:
            self._cache_ready.set()

    # ------------------------------------------------------------------ 计分

    def clamp(self, score, cfg: Optional[dict] = None) -> float:
        """把分数钳到 ``[hard_floor, hard_ceiling]``。"""
        cfg = cfg or self.cfg()
        lo = _num(cfg.get("hard_floor"), 0.0)
        hi = _num(cfg.get("hard_ceiling"), 100.0)
        if hi < lo:
            lo, hi = hi, lo
        return max(lo, min(hi, _num(score, 0.0)))

    def _push_history(self, rec: dict, delta: float, reason: str, by: str = "auto"):
        """写一条历史记录（环形，超限裁掉最旧的）。"""
        cfg = self.cfg()
        limit = max(1, _int(cfg.get("history_limit"), 200))
        hist = rec.setdefault("history", [])
        if not isinstance(hist, list):
            hist = []
            rec["history"] = hist
        hist.append({
            "at": _now(),
            "delta": round(_num(delta), 3),
            "reason": str(reason or ""),
            "score": round(_num(rec.get("score")), 3),
            "by": str(by or "auto"),
        })
        if len(hist) > limit:
            del hist[:len(hist) - limit]
        rec["last_score_change"] = {
            "at": _now(),
            "delta": round(_num(delta), 3),
            "reason": str(reason or ""),
        }

    def apply_delta(self, rec: dict, delta: float, reason: str,
                    by: str = "auto", cfg: Optional[dict] = None) -> float:
        """给档案加减分并钳位、写历史。返回**实际生效**的 delta。

        实际 delta 可能与请求值不同（被 ``hard_floor``/``hard_ceiling`` 截断），
        历史里记的是实际值 —— 否则服主会看到「加了 50 分但分数没变」。
        """
        cfg = cfg or self.cfg()
        before = _num(rec.get("score"), 0.0)
        after = self.clamp(before + _num(delta, 0.0), cfg)
        rec["score"] = after
        real = after - before
        if abs(real) > 1e-9:
            self._push_history(rec, real, reason, by)
        return real

    # ------------------------------------------------------------------ 设备算法

    @staticmethod
    def _find_device(rec: dict, device_id: str) -> Optional[dict]:
        did = str(device_id or "").strip()
        if not did:
            return None
        for d in rec.get("devices") or []:
            if isinstance(d, dict) and str(d.get("device_id") or "") == did:
                return d
        return None

    def _burst_expired(self, rec: dict, now_ts: float, cfg: dict) -> bool:
        db = rec.setdefault("device_burst", {})
        ws = _parse_day(db.get("window_start", ""))
        if ws is None:
            return True
        try:
            import datetime
            t = time.strptime(str(db.get("window_start"))[:19], "%Y-%m-%d %H:%M:%S")
            start_ts = time.mktime(t)
        except Exception:
            return True
        hours = max(1, _int(cfg.get("device_burst_window_hours"), 24))
        return (now_ts - start_ts) > hours * 3600.0

    def score_device(self, rec: dict, payload: dict, cfg: Optional[dict] = None) -> List[dict]:
        """处理一次设备上报，返回变更列表 ``[{"delta":…, "reason":…}]``。

        算法：

        * 命中已有 ``device_id`` → 加 ``add_device_match``，刷新 last_seen /
          seen_count / last_ip。**IP 变化不影响判定**。
        * 全新 ``device_id`` → 记进 ``devices``；
          窗口过期就重置窗口；把新 id 塞进 ``new_ids``；
          若 ``len(new_ids) >= threshold`` 且本窗口还没扣过 → 扣 ``sub_device_burst``
          并打上 ``penalty_applied_at``（**窗口内只扣一次**）；
          否则加 ``add_device_new_first``。

        **关键语义**：A/B/C 三个设备换着用 → 每次都命中已有记录，
        永不触发突发；只有短时间内冒出一堆**从未见过**的设备才会扣分。
        """
        cfg = cfg or self.cfg()
        changes: List[dict] = []
        did = str((payload or {}).get("device_id") or "").strip()
        now_ts = time.time()
        now_s = _now()

        if not did:
            return changes

        hit = self._find_device(rec, did)
        if hit is not None:
            hit["last_seen"] = now_s
            hit["seen_count"] = _int(hit.get("seen_count"), 0) + 1
            for f in ("device_os", "game_version", "locale"):
                v = str((payload or {}).get(f) or "").strip()
                if v:
                    hit[f] = v
            self._touch_ip(hit, payload)
            d = _num(cfg.get("add_device_match"), 0.0)
            if d:
                changes.append({"delta": d, "reason": "device_match"})
            return changes

        # ---- 全新设备 ----
        db = rec.setdefault("device_burst", {})
        if self._burst_expired(rec, now_ts, cfg):
            db["window_start"] = now_s
            db["new_ids"] = []
            db["new_id_sessions"] = []
            db["penalty_applied_at"] = ""

        new_dev = {
            "device_id": did,
            "device_os": str((payload or {}).get("device_os") or ""),
            "game_version": str((payload or {}).get("game_version") or ""),
            "locale": str((payload or {}).get("locale") or ""),
            "first_seen": now_s,
            "last_seen": now_s,
            "seen_count": 1,
            "trusted": False,
            "trusted_by": "",
            "note": "",
            "last_ip": "",
            "last_port": 0,
            "ip_history": [],
        }
        self._touch_ip(new_dev, payload)
        rec.setdefault("devices", []).append(new_dev)

        ids = db.get("new_ids")
        if not isinstance(ids, list):
            ids = []
            db["new_ids"] = ids
        if did not in ids:
            ids.append(did)

        threshold = max(2, _int(cfg.get("device_burst_threshold"), 3))
        # 被服主标记为「信任此设备」的不计入突发统计（误伤补救）
        counted = [i for i in ids
                   if not self._device_trusted(rec, i)]

        # ★ 关键防误伤：只有「跨会话陆续冒出」的新设备才算突发。
        #
        # 为什么需要这个门：新玩家第一次进服时，客户端可能在同一分钟内依次
        # 上报好几个 device_id（多端登录、切服、客户端重连都可能这样）。
        # 如果一口气凑够阈值就扣分，那所有新玩家开局就被扣 —— 这是最典型的
        # 误伤，而且和用户说的「不是两三个设备换着用」完全相反。
        #
        # 判据：统计窗口内出现过「不同场次（session）」的新设备数。
        # 同一次进服里冒出来的多个新设备只算一场。
        sessions = db.get("new_id_sessions")
        if not isinstance(sessions, list):
            sessions = []
            db["new_id_sessions"] = sessions
        sid = str((payload or {}).get("session") or "").strip()
        if not sid:
            # 没给 session 时用「进服时间戳（精确到十秒）」退化成一个场次标识
            sid = time.strftime("%Y%m%d%H%M%S", time.localtime(now_ts))[:13]

        # 场次数要把「本次」算进去（本次是新场次就 +1），否则阈值会晚一场才触发。
        already = sid in sessions
        session_count = len(sessions) + (0 if already else 1)

        should_penalize = (session_count >= threshold
                           and not str(db.get("penalty_applied_at") or ""))

        if should_penalize:
            db["penalty_applied_at"] = now_s
            d = _num(cfg.get("sub_device_burst"), 0.0)
            if d:
                changes.append({"delta": d, "reason": "device_burst"})
        else:
            d = _num(cfg.get("add_device_new_first"), 0.0)
            if d:
                changes.append({"delta": d, "reason": "device_new_first"})

        if not already:
            sessions.append(sid)
            if len(sessions) > 50:
                del sessions[:len(sessions) - 50]
        return changes

    @staticmethod
    def _device_trusted(rec: dict, device_id: str) -> bool:
        for d in rec.get("devices") or []:
            if isinstance(d, dict) and str(d.get("device_id") or "") == str(device_id):
                return bool(d.get("trusted"))
        return False

    @staticmethod
    def _touch_ip(dev: dict, payload: dict):
        """记录 IP（**仅供服主查看，不参与任何判定**）。"""
        ip = str((payload or {}).get("ip") or "").strip()
        port = _int((payload or {}).get("port"), 0)
        if ip:
            dev["last_ip"] = ip
            hist = dev.setdefault("ip_history", [])
            if not isinstance(hist, list):
                hist = []
                dev["ip_history"] = hist
            if ip not in hist:
                hist.append(ip)
                if len(hist) > 20:
                    del hist[:len(hist) - 20]
        if port:
            dev["last_port"] = port

    def trust_device(self, key: str, device_id: str, on: bool = True,
                     by: str = "admin") -> Tuple[bool, str]:
        """标记 / 取消「信任此设备」（误伤补救）。"""
        with self._lock:
            rec = self.load_or_blank(key)
            dev = self._find_device(rec, device_id)
            if dev is None:
                return False, "未找到该设备"
            dev["trusted"] = bool(on)
            dev["trusted_by"] = str(by or "admin") if on else ""
            if on:
                # 从当前窗口的突发统计里摘掉，避免已经踩进阈值
                db = rec.setdefault("device_burst", {})
                ids = db.get("new_ids")
                if isinstance(ids, list) and device_id in ids:
                    ids.remove(device_id)
            self.save(key, rec)
            self.cache_put(key, rec)
        return True, ("已信任该设备" if on else "已取消信任")

    # ------------------------------------------------------------------ 打卡结算

    def settle_signin(self, rec: dict, today: Optional[str] = None,
                      cfg: Optional[dict] = None) -> Tuple[float, str]:
        """结算签到相关的加减分。返回 ``(delta, reason)``。

        幂等：同一份档案同一天只会结算一次（靠 ``signin.last_settle_date``）。

        逻辑：

        * ``days_missed = (today - signin.last) - 1``
        * 超过宽限期才扣，扣的是**独立扣分池** ``total_penalty``，
          受 ``signin_miss_floor`` 下限约束；
        * 当天已打卡 → 加分，并把 ``total_penalty`` **清零**。

        为什么要独立扣分池：若直接扣总分，断签一个月会把分数扣穿，
        玩家永远回不来（也就要永久被挡在门外）。用带下限的独立池，
        最多扣到 ``signin_miss_floor`` 就停，打卡一次立刻归零。
        """
        cfg = cfg or self.cfg()
        today = today or _today()
        sg = rec.setdefault("signin", {})
        sg.setdefault("last", "")
        sg["total_penalty"] = _num(sg.get("total_penalty"), 0.0)

        if str(sg.get("last_settle_date") or "") == today:
            return 0.0, ""              # 幂等

        delta = 0.0
        reason = ""

        last = str(sg.get("last") or "")
        if last:
            d = _day_diff(last, today)
            grace = max(0, _int(cfg.get("signin_grace_days"), 2))
            if d is not None and d > grace:
                charge_days = d - grace                    # 超出宽限的天数
                raw = _num(cfg.get("sub_signin_miss_per_day"), 0.0) * charge_days
                if cfg.get("signin_miss_floor_enabled", True):
                    floor = _num(cfg.get("signin_miss_floor"), -20.0)
                    acc = sg["total_penalty"]
                    new_acc = max(floor, acc + raw)        # ← 下限
                    delta += (new_acc - acc)
                    sg["total_penalty"] = new_acc
                else:
                    delta += raw
                    sg["total_penalty"] = sg["total_penalty"] + raw
                reason = "signin_miss"

        if last == today:
            base = _num(cfg.get("add_signin_base"), 0.0)
            per = _num(cfg.get("add_signin_streak_per_day"), 0.0)
            cap = max(0, _int(cfg.get("add_signin_streak_cap"), 0))
            streak = max(0, _int(sg.get("streak"), 0))
            bonus = per * min(streak, cap) if cap > 0 else 0.0
            delta += base + bonus
            sg["total_penalty"] = 0.0                      # ← 打卡后清零
            reason = "signin" if not reason else reason

        sg["last_settle_date"] = today
        return delta, reason

    # ------------------------------------------------------------------ 进服计分

    def process_join_payload(self, payload: dict) -> dict:
        """后台线程：读盘 → 计分 → 落盘 → 更新缓存。

        ``payload`` 必须是**标量快照**（主线程抠出来的），
        因为后台线程碰 ``player`` 对象会踩到「对象已析构」。
        """
        result = {"action": "none", "score": 0.0, "needed": 0.0,
                  "delta": 0.0, "reason": "", "name": "", "key": "", "reasons": []}
        cfg = self.cfg(force=True)
        name = str((payload or {}).get("name") or "")
        result["name"] = name
        if not cfg.get("enabled"):
            return result

        key, key_type = self.key_from_payload(payload)
        if not key:
            return result
        result["key"] = key

        with self._lock:
            rec = self.load_or_blank(key, key_type, name)
            rec["name"] = name
            rec["name_lower"] = name.lower()
            rec["key_type"] = key_type
            rec["last_seen"] = _now()

            # ===========================================================
            # ★★「信任的人」短路：永远保持满分
            #
            # 为什么必须在**这里** return、而不是在末尾钳制
            # ------------------------------------------
            # 下面的第 5 步会调 ``settle_signin`` 结算打卡 ——
            # 玩家今天没签到会产生**扣分**。如果在末尾才把分数钳到上限，
            # 那些扣分已经通过 ``apply_delta`` 写进档案历史了，
            # 取消信任后分数会带着一堆不该有的惩罚。
            # 提前返回才是真正的「不受任何加减分影响」。
            #
            # 为什么取 cfg["hard_ceiling"] 而不是硬编码 100
            # ------------------------------------------
            # 「满分」是用户配置的概念：把上限调成 120 的人，
            # 期望信任的人也拿 120，而不是被代码钉死在 100。
            # ``clamp`` 会连同硬地板一起兜住，不必自己判边界。
            # ===========================================================
            flags0 = rec.setdefault("flags", {})
            if flags0.get("trusted_person"):
                full = _num(cfg.get("hard_ceiling"), 100.0)
                rec["score"] = self.clamp(full, cfg)
                self.save(key, rec)
                self.cache_put(key, rec)
                result.update({
                    "score": _num(rec.get("score")),
                    "needed": _num(cfg.get("min_score"), 0.0),
                    "delta": 0.0,
                    "action": "trusted_person",
                    "reason": "信任的人（保持满分）",
                })
                return result

            changes: List[dict] = []

            # 1) 设备指纹
            changes += self.score_device(rec, payload, cfg)

            # 2) QQ 绑定（一次性奖励，靠 flags 幂等）
            flags = rec.setdefault("flags", {})
            qq = str((payload or {}).get("qq") or "").strip()
            if qq:
                rec["binding"] = {"qq": qq, "mc": name,
                                  "bound_at": (rec.get("binding") or {}).get("bound_at") or _now()}
            if qq and not flags.get("bonus_bound_qq"):
                d = _num(cfg.get("add_bound_qq"), 0.0)
                if d:
                    changes.append({"delta": d, "reason": "bound_qq"})
                flags["bonus_bound_qq"] = True

            # 3) 白名单设备（一次性）
            if flags.get("whitelisted") and not flags.get("bonus_whitelist_device"):
                d = _num(cfg.get("add_whitelist_device"), 0.0)
                if d:
                    changes.append({"delta": d, "reason": "whitelist_device"})
                flags["bonus_whitelist_device"] = True

            # 4) 每日游玩（每日一次，跨天判定用字符串比较）
            today = _today()
            dp = rec.setdefault("daily_play", {})
            if str(dp.get("last") or "") != today:
                dp["last"] = today
                d = _num(cfg.get("add_daily_play"), 0.0)
                if d:
                    changes.append({"delta": d, "reason": "daily_play"})

            # 5) 打卡结算（把「今天没打卡」的后果算上）
            sd, sreason = self.settle_signin(rec, today, cfg)
            if abs(sd) > 1e-9:
                changes.append({"delta": sd, "reason": sreason})

            # 6) 汇总写分
            total_delta = 0.0
            for ch in changes:
                real = self.apply_delta(rec, ch["delta"], ch["reason"], "auto", cfg)
                total_delta += real
            if not changes:
                rec["score"] = self.clamp(_num(rec.get("score")), cfg)

            self.save(key, rec)
            self.cache_put(key, rec)

            score = _num(rec.get("score"))
            need = _num(cfg.get("min_score"), 0.0)
            result.update({
                "score": score,
                "needed": need,
                "delta": total_delta,
                "reason": (rec.get("last_score_change") or {}).get("reason", ""),
                "reasons": [ch["reason"] for ch in changes],
            })
            if score < need:
                result["action"] = "kick"
                result["kick_text"] = self.kick_text(cfg, score, need, result["reason"])
            elif changes and total_delta != 0 and cfg.get("notify_on_join", True):
                result["action"] = "notify"
                result["notice"] = self.notify_text(score, total_delta, result["reasons"])
        return result

    # ------------------------------------------------------------------ 文案

    def kick_text(self, cfg: dict, score: float, need: float, reason: str) -> str:
        """生成踢出提示。**必须**包含当前分、要求分、差值、原因。

        这是用户的硬性要求：不管怎么处理，都要让玩家知道为什么进不去。
        """
        tpl = str((cfg or {}).get("kick_message") or "")
        reason_cn = reason_text(reason)
        try:
            t = (tpl
                 .replace("{score}", f"{_num(score):.0f}")
                 .replace("{required}", f"{_num(need):.0f}")
                 .replace("{gap}", f"{max(0.0, _num(need) - _num(score)):.0f}")
                 .replace("{reason}", reason_cn))
        except Exception:
            t = ""
        t = (t or "").strip()
        if not t:
            t = (f"§c可信度不足：当前 {_num(score):.0f} / 要求 {_num(need):.0f}\n"
                 f"§7还差：§e{max(0.0, _num(need) - _num(score)):.0f}\n"
                 f"§7原因：{reason_cn}")
        return t

    @staticmethod
    def notify_text(score: float, delta: float, reasons: List[str]) -> str:
        cn = "、".join(reason_text(r) for r in (reasons or []) if r) or "综合评估"
        sign = "+" if delta >= 0 else ""
        return f"§7[可信度] §e{sign}{delta:.0f}§7（{cn}）→ 当前 §a{score:.0f}"

    # ------------------------------------------------------------------ 登录拦截

    def check_login(self, player) -> Tuple[bool, str]:
        """登录阶段判定。返回 ``(是否放行, 踢出文案)``。

        **只读内存缓存，绝不读盘**（主线程）。缓存未命中一律放行
        （fail-open）—— 宁可放过一个，也不要因为档案没来得及加载
        把所有玩家挡在门外。
        """
        try:
            cfg = self.cfg()
            if not cfg.get("enabled"):
                return True, ""
            key, _ = self.storage_key(player)
            if not key:
                return True, ""
            rec = self.cache_get(key)
            if rec is None:
                return True, ""                      # fail-open
            score = _num(rec.get("score"), 0.0)
            need = _num(cfg.get("min_score"), 0.0)
            if score >= need:
                return True, ""
            reason = (rec.get("last_score_change") or {}).get("reason", "")
            return False, self.kick_text(cfg, score, need, reason)
        except Exception as e:
            self.logger.warning(f"[可信度] 登录检查异常（放行）: {e}")
            return True, ""

    # ------------------------------------------------------------------ 每日结算

    def tick(self):
        """定时任务入口：每天做一次全量结算。"""
        try:
            cfg = self.cfg(force=True)
            if not cfg.get("enabled"):
                return
            today = _today()
            if self._last_settle_day == today:
                return
            self._last_settle_day = today
            self.plugin._executor.submit(self.daily_settle, today)
        except Exception as e:
            self.logger.warning(f"[可信度] 结算任务异常: {e}")

    def daily_settle(self, today: Optional[str] = None):
        """后台线程：遍历所有档案做结算。

        注意 ``delta == 0`` 也要落盘 —— 必须把 ``last_settle_date`` 写进去，
        否则下次 tick 会重复结算（虽然结果是 0，但白扫一遍盘）。
        """
        today = today or _today()
        cfg = self.cfg(force=True)
        changed = 0
        try:
            for key, rec in self.iter_records():
                try:
                    delta, reason = self.settle_signin(rec, today, cfg)
                    if abs(delta) > 1e-9:
                        self.apply_delta(rec, delta, reason, "auto", cfg)
                    rec["score"] = self.clamp(_num(rec.get("score")), cfg)
                    self.save(key, rec)
                    self.cache_put(key, rec)
                    changed += 1
                except Exception as e:
                    self.logger.warning(f"[可信度] 结算 {key} 失败: {e}")
            self.logger.info(f"[可信度] 每日结算完成（{today}），处理 {changed} 份档案")
            if (cfg.get("backup") or {}).get("enabled", True):
                self.backup_all()
        except Exception as e:
            self.logger.warning(f"[可信度] 每日结算异常: {e}")

    # ------------------------------------------------------------------ 备份

    def backup_all(self) -> Tuple[bool, str]:
        """把 ``player/`` 整个镜像到 ``player_backup/<时间戳>/``，并按 keep 轮转。"""
        try:
            cfg = self.cfg()
            keep = max(1, _int((cfg.get("backup") or {}).get("keep"), 5))
            src = self._player_dir
            if not src.exists():
                return False, "暂无档案可备份"
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dst = self._backup_dir / stamp
            dst.mkdir(parents=True, exist_ok=True)
            n = 0
            for p in src.glob("*.json"):
                try:
                    shutil.copy2(p, dst / p.name)
                    n += 1
                except Exception:
                    pass
            # 轮转：只保留最近 keep 份
            try:
                subs = sorted([d for d in self._backup_dir.iterdir() if d.is_dir()],
                              key=lambda d: d.name)
                for old in subs[:-keep]:
                    shutil.rmtree(old, ignore_errors=True)
            except Exception:
                pass
            return True, f"已备份 {n} 份档案到 {dst}"
        except Exception as e:
            return False, f"备份失败: {e}"

    def export_all(self) -> dict:
        """导出全部档案（供 WebUI 下载）。"""
        items = []
        for key, rec in self.iter_records():
            items.append({"key": key, "record": rec})
        return {
            "exported_at": _now(),
            "count": len(items),
            "player_dir": str(self._player_dir),
            "players": items,
        }

    def clear_all(self, cfg: Optional[dict] = None) -> Tuple[bool, str]:
        """清空全部档案（**先备份再删**）。"""
        cfg = cfg or self.cfg()
        ok, msg = self.backup_all()
        if not ok:
            return False, f"清空前备份失败，已中止：{msg}"
        n = 0
        try:
            for p in self._player_dir.glob("*.json"):
                try:
                    p.unlink()
                    n += 1
                except Exception:
                    pass
        except Exception as e:
            return False, f"清空失败: {e}"
        with self._lock:
            self._cache.clear()
        return True, f"已清空 {n} 份档案（备份已保留：{msg}）"

    # ------------------------------------------------------------------ 手动调整

    def manual_adjust(self, key: str, delta: float, note: str = "",
                      by: str = "admin") -> Tuple[bool, str, float]:
        """服主手动加减权。受 ``manual_step_max`` 限制。"""
        cfg = self.cfg(force=True)
        step_max = abs(_num(cfg.get("manual_step_max"), 50.0))
        d = _num(delta, 0.0)
        if abs(d) > step_max + 1e-9:
            return False, f"单次调整不能超过 ±{step_max:.0f}", 0.0
        if abs(d) < 1e-9:
            return False, "调整值不能为 0", 0.0
        with self._lock:
            rec = self.load_or_blank(key)
            if not rec:
                return False, "档案不存在", 0.0
            reason = "manual_add" if d > 0 else "manual_sub"
            real = self.apply_delta(rec, d, reason, by, cfg)
            if note:
                flags = rec.setdefault("flags", {})
                flags["last_manual_note"] = str(note)
            self.save(key, rec)
            self.cache_put(key, rec)
            score = _num(rec.get("score"))
        return True, f"已调整 {real:+.0f}，当前 {score:.0f}", score

    # ------------------------------------------------------------------ 权限联动

    def apply_tier(self, name: str, score: float, cfg: Optional[dict] = None):
        """按可信度给玩家套权限组。**默认关闭，且永远不碰 OP。**

        ``Player.permission_level`` 在 0.11.x 只读，所以只能走
        ``add_attachment`` + ``set_permission``。复用 ``_perm_attachments``，
        但**只 set/unset 自己 ``perm_tiers`` 里列出的组名**，
        不动权限管理板块设的键，避免两边互相覆盖。
        """
        cfg = cfg or self.cfg()
        if not cfg.get("perm_tier_enabled"):
            return
        tiers = cfg.get("perm_tiers") or []
        if not isinstance(tiers, list) or not tiers:
            return
        groups = []
        for t in tiers:
            if not isinstance(t, dict):
                continue
            g = str(t.get("group") or "").strip()
            if g:
                groups.append((g, _num(t.get("score"), 0.0)))
        if not groups:
            return
        groups.sort(key=lambda x: x[1], reverse=True)
        want = next((g for g, s in groups if score >= s), "")

        def _apply():
            try:
                p = self.plugin.server.get_player(str(name))
            except Exception:
                p = None
            if p is None:
                return
            try:
                att = self.plugin._perm_attachments.get(name)
                if att is not None:
                    try:
                        if att.permissible is not p:
                            att = None
                    except Exception:
                        att = None
                if att is None:
                    att = p.add_attachment(self.plugin)
                    self.plugin._perm_attachments[name] = att
                for g, _s in groups:
                    att.set_permission(g, (g == want) if want else False)
                p.recalculate_permissions()
            except Exception as e:
                self.logger.warning(f"[可信度] 应用权限组失败 {name}: {e}")

        try:
            self.plugin._run_async_on_server_thread(_apply)
        except Exception as e:
            self.logger.warning(f"[可信度] 调度权限组任务失败: {e}")

    # ------------------------------------------------------------------ 外部同步

    def find_key_by_name(self, name: str) -> str:
        """按 MC 名反查存储键（用于 QQ 签到 / 白名单审核等只有名字的场景）。"""
        low = str(name or "").strip().lower()
        if not low:
            return ""
        # 先看缓存（快），再落盘扫一遍
        with self._lock:
            for k, rec in self._cache.items():
                if str(rec.get("name_lower") or rec.get("name") or "").lower() == low:
                    return k
        for k, rec in self.iter_records():
            if str(rec.get("name_lower") or rec.get("name") or "").lower() == low:
                return k
        return ""

    def note_signin(self, name: str, day: Optional[str] = None) -> bool:
        """``_do_signin`` 成功后调用：把这次打卡反射进可信度档案。

        为什么必须做：可信度结算靠 ``signin.last`` 判断有没有断签。
        如果群内签到不写进这份档案，结算时会认为玩家没打卡而扣分 ——
        玩家明明打卡了却被扣，是最典型的误伤。

        没有档案的玩家会被跳过（不主动建档案，避免给从没进服的人建档）。
        """
        day = day or _today()
        key = self.find_key_by_name(name)
        if not key:
            return False
        with self._lock:
            rec = self.load(key)
            if not rec:
                return False
            sg = rec.setdefault("signin", {})
            last = str(sg.get("last") or "")
            if last == day:
                return False
            # 连续天数：昨天打过就 +1，断了就从 1 重新开始
            diff = _day_diff(last, day) if last else None
            if diff == 1:
                sg["streak"] = _int(sg.get("streak"), 0) + 1
            else:
                sg["streak"] = 1
            sg["best_streak"] = max(_int(sg.get("best_streak"), 0), sg["streak"])
            sg["last"] = day
            self.save(key, rec)
            self.cache_put(key, rec)
        return True

    def note_whitelist(self, name: str, device_id: str = "") -> bool:
        """白名单审核通过时调用：给档案打上白名单标记 / 信任该设备。

        只标记已有档案（不被「主动建档」牵连出脏数据）。
        """
        key = self.find_key_by_name(name)
        if not key:
            return False
        with self._lock:
            rec = self.load(key)
            if not rec:
                return False
            flags = rec.setdefault("flags", {})
            flags["whitelisted"] = True
            if device_id:
                dev = self._find_device(rec, device_id)
                if dev is not None:
                    dev["trusted"] = True
                    dev["trusted_by"] = "whitelist"
            self.save(key, rec)
            self.cache_put(key, rec)
        return True

    # ------------------------------------------------------------------ 信任的人

    def is_trusted_person(self, name: str) -> bool:
        """该玩家是否被标记为「信任的人」。

        ★ 只查已有档案，不建档 —— 与 ``note_whitelist`` 同口径，
          避免一次查询就在磁盘上留下一堆空档案。
        """
        key = self.find_key_by_name(name)
        if not key:
            return False
        rec = self.load(key)
        if not rec:
            return False
        flags = rec.get("flags") or {}
        return bool(flags.get("trusted_person"))

    def set_trusted_person(self, name: str, on: bool) -> bool:
        """添加 / 移除「信任的人」。

        ★ 语义（务必写清楚，这是个"信任"概念，容易被误解）
        -------------------------------------------------
        * 加入后：该玩家**每次登录直接保持满分**（``hard_ceiling``），
          绕过签到扣分、设备异常、断签惩罚等全部扣分逻辑；
        * 移除后：**立即回落到真实历史分** —— 注意这里不是"回到加入前的
          那个分数"（那是快照语义，会让分数变成可以来回套利的开关），
          而是按档案里累计的历史计分重新钳制后的结果。
          下次登录会走完整计分流程，把期间的加减分补上。
          ```` **移除时**会清掉 ``trusted_person`` 标记并立刻重算一次，
          所以面板上不会出现"已取消信任但还显示满分"的中间态。

        只标记**已有档案**的玩家（与 ``note_whitelist`` 一致）：
        给一个从没进过服的玩家标记"信任"没有意义，还会污染档案目录。
        返回 False 表示没找到该玩家的档案。
        """
        key = self.find_key_by_name(name)
        if not key:
            return False
        with self._lock:
            rec = self.load(key)
            if not rec:
                return False
            flags = rec.setdefault("flags", {})
            if on:
                # ★★ 必须先**备份真实分**再改分，否则取消信任时无从回落。
                #
                # 踩过的坑：第一版直接把 rec["score"] 覆盖成 hard_ceiling，
                # 结果取消信任时只能对那个已经是 100 的值再 clamp 一次，
                # 分数永远回不去 —— 用户表现为「取消了信任但分数还是满分」，
                # 而且**原始分已经永久丢失**，无法恢复。
                #
                # 备份键只在「首次加入」时写入：重复点「信任」不该覆盖掉
                # 最初的真实分（否则来回点几次就把备份冲成 100 了）。
                if not flags.get("trusted_person"):
                    flags["trusted_person_score_backup"] = _num(rec.get("score"), 0.0)
                flags["trusted_person"] = True
                # 立刻置为满分，不必等下次登录 —— 否则用户在面板上点了
                # 「信任」，列表里分数还是旧的，会以为没生效。
                cfg = self.cfg(force=True)
                rec["score"] = self.clamp(_num(cfg.get("hard_ceiling"), 100.0), cfg)
            else:
                flags.pop("trusted_person", None)
                # ★ 回落真实历史分：优先用加入时的备份。
                #   备份缺失（老档案 / 手工改过）时退化为对当前分 clamp，
                #   保证不会因为缺字段就抛异常或把分数清零。
                if "trusted_person_score_backup" in flags:
                    real = _num(flags.pop("trusted_person_score_backup"), 0.0)
                else:
                    real = _num(rec.get("score"), 0.0)
                rec["score"] = self.clamp(real)
            self.save(key, rec)
            self.cache_put(key, rec)
        return True

    # ------------------------------------------------------------------ 主线程桥接

    @staticmethod
    def snapshot_player(player) -> dict:
        """在主线程把 ``player`` 的标量抠成 dict（后台线程只认它）。"""
        def _g(obj, attr, default=""):
            try:
                v = getattr(obj, attr, None)
                return v if v is not None else default
            except Exception:
                return default
        snap = {
            "name": str(_g(player, "name", "") or ""),
            "xuid": str(_g(player, "xuid", "") or ""),
            "unique_id": str(_g(player, "unique_id", "") or _g(player, "uuid", "") or ""),
            "device_id": str(_g(player, "device_id", "") or ""),
            "device_os": str(_g(player, "device_os", "") or ""),
            "game_version": str(_g(player, "game_version", "") or ""),
            "locale": str(_g(player, "locale", "") or ""),
            "ip": "",
            "port": 0,
        }
        try:
            addr = getattr(player, "address", None)
            if addr is not None:
                # SocketAddress 的属性是 hostname（不是 host）
                snap["ip"] = str(getattr(addr, "hostname", "") or "")
                snap["port"] = _int(getattr(addr, "port", 0), 0)
        except Exception:
            pass
        return snap

    def self_qq(self, name: str) -> str:
        """查该 MC 名对应的 QQ（用于 QQ 绑定加分）。"""
        try:
            entry = self.plugin._qq_for_mc(str(name))
        except Exception:
            entry = None
        if isinstance(entry, dict):
            return str(entry.get("qq") or entry.get("openid") or "").strip()
        if isinstance(entry, str):
            return entry.strip()
        return ""

    def begin_join(self, player):
        """进服事件入口（主线程）：抠标量 → 丢线程池。"""
        try:
            if not self.cfg().get("enabled"):
                return
            snap = self.snapshot_player(player)
            if not snap.get("name"):
                return
            snap["qq"] = self.self_qq(snap["name"])
            # 场次标识：用于「跨会话冒出多个新设备」的突发判定。
            # 同一次进服里连续上报的多个 device_id 属于同一场，只算一次 ——
            # 这样新玩家开局一口气注册几个设备不会被误判成突发。
            snap["session"] = f"{snap['name'].lower()}#{int(time.time())}"
            self.plugin._executor.submit(self._join_worker, snap)
        except Exception as e:
            self.logger.warning(f"[可信度] 进服处理失败: {e}")

    def _join_worker(self, snap: dict):
        """后台线程：计分 → 回主线程踢出或提示 → 权限联动。"""
        try:
            res = self.process_join_payload(snap)
        except Exception as e:
            self.logger.warning(f"[可信度] 进服计分异常: {e}")
            return
        if res.get("action") == "none" and not res.get("key"):
            return

        name = str(res.get("name") or "")
        if res.get("action") == "kick":
            text = str(res.get("kick_text") or "可信度不足")
            self._run_on_main(lambda: self._kick_now(name, text), "踢出")
        elif res.get("action") == "notify":
            notice = str(res.get("notice") or "")
            if notice:
                self._run_on_main(lambda: self._notify_now(name, notice), "提示")

        cfg = self.cfg()
        if cfg.get("perm_tier_enabled") and name:
            self.apply_tier(name, _num(res.get("score"), 0.0), cfg)

    def _run_on_main(self, func, tag: str = ""):
        try:
            self.plugin._run_async_on_server_thread(func)
        except Exception as e:
            self.logger.warning(f"[可信度] 调度主线程任务({tag})失败: {e}")

    def _kick_now(self, name: str, text: str):
        """主线程：把玩家踢出去（进服兜底那道）。"""
        try:
            p = self.plugin.server.get_player(str(name))
            if p is None:
                return
            p.kick(text)
        except Exception as e:
            self.logger.warning(f"[可信度] 踢出失败 {name}: {e}")

    def _notify_now(self, name: str, text: str):
        """主线程：给玩家发提示（三级降级：send_message → send_tip → send_popup）。"""
        try:
            p = self.plugin.server.get_player(str(name))
            if p is None:
                return
            for meth in ("send_message", "send_tip", "send_popup"):
                fn = getattr(p, meth, None)
                if not callable(fn):
                    continue
                try:
                    fn(text)
                    return
                except Exception:
                    continue
        except Exception:
            pass

    # ------------------------------------------------------------------ 展示

    def public_list(self, keyword: str = "", limit: int = 500) -> dict:
        """给 WebUI 的玩家列表 + 统计。"""
        kw = str(keyword or "").strip().lower()
        rows = []
        total = 0
        for key, rec in self.iter_records():
            total += 1
            name = str(rec.get("name") or "")
            if kw and kw not in name.lower() and kw not in key.lower():
                continue
            if len(rows) >= max(1, _int(limit, 500)):
                continue
            devices = rec.get("devices") or []
            sg = rec.get("signin") or {}
            rows.append({
                "key": key,
                "name": name,
                "key_type": rec.get("key_type") or "xuid",
                "degraded": str(rec.get("key_type") or "xuid") != "xuid",
                "score": round(_num(rec.get("score")), 2),
                "device_count": len(devices),
                "trusted_devices": sum(1 for d in devices
                                       if isinstance(d, dict) and d.get("trusted")),
                "streak": _int(sg.get("streak"), 0),
                "best_streak": _int(sg.get("best_streak"), 0),
                "signin_last": str(sg.get("last") or ""),
                "total_penalty": round(_num(sg.get("total_penalty")), 2),
                "qq": str((rec.get("binding") or {}).get("qq") or ""),
                "last_seen": str(rec.get("last_seen") or ""),
                "last_change": rec.get("last_score_change") or {},
                "flags": rec.get("flags") or {},
                # 显式冗余一个布尔字段：前端列表要画「信任的人」徽标，
                # 让它读 flags.trusted_person 也行，但多这一层能避免
                # 前端写 `(r.flags||{}).trusted_person` 这种易错写法。
                "trusted_person": bool((rec.get("flags") or {}).get("trusted_person")),
            })
        rows.sort(key=lambda r: r["score"], reverse=True)
        cfg = self.cfg()
        need = _num(cfg.get("min_score"), 0.0)
        return {
            "total": total,
            "shown": len(rows),
            "players": rows,
            "stats": {
                "enabled": bool(cfg.get("enabled")),
                "min_score": need,
                "pass": sum(1 for r in rows if r["score"] >= need),
                "fail": sum(1 for r in rows if r["score"] < need),
                "degraded": sum(1 for r in rows if r["degraded"]),
            },
        }

    def detail(self, key: str) -> Optional[dict]:
        """单个玩家的完整档案（含设备与历史）。"""
        rec = self.load(str(key or ""))
        if not rec:
            return None
        rec = json.loads(json.dumps(rec))
        rec["key"] = str(key)
        rec["history"] = list(reversed(rec.get("history") or []))[:50]
        return rec
