"""区块日志与恢复系统 —— **人机共用**的能力层。

这个文件是整个移植的枢纽，先说清楚它要解决的问题。

背景
----
区块回档原本是一个**给人用的游戏内功能**：拿着木斧点两下选区，
弹出表单选「回档到多久以前」。它的所有状态流转都是围绕
"当前有个玩家正在表单里"设计的。

搬迁到 GreenMoon 之后，同一份能力必须同时被三种角色驱动：

1. **人**：在 Web 面板上填坐标 / 时间，点按钮，看实时进度；
2. **AI**：作为工具被调用，参数由模型给出，结果要能回喂进对话；
3. **其它插件**：通过 Python API 调用（见 ``gm/region.py``）。

如果给三种角色各写一套入口，它们很快就会分叉 —— 人能做的事
AI 做不了、或者 AI 能越过人设的限制。**安全策略漏在第一条路径上，
另外两条就都漏了。**

所以这里是唯一入口：``RegionAPI``。三条路径都调到它，它负责
参数规整、额度校验、危险操作二次要求和审计。区别只体现在
``caller`` 上 —— 它决定"要不要先征求同意"以及"记谁的账"。

关于「不可逆」
--------------
把一片区域的方块恢复到某个过去时刻，本质上是**不可逆的**：
虽然我们保留了 undo 快照，但快照本身有窗口（未清理前有效），
而且在这个窗口里世界已经继续变化了。
所以：

* **只读**（查流水 / 查会话 / 预览影响面）→ 谁都能做；
* **写**（执行回档 / 执行撤销）→ 一律带 ``dry_run`` 与配额；
  AI 调用时先返回一份「影响预览」，由策略层决定是否要人确认。

这不是绕圈子：AI 是**提议者**，人是**批准者**。分工写在代码里，
而不是靠系统提示词约束模型别乱来。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .store import RegionStore

__all__ = [
    "RegionAPI", "RegionConfig", "ApiResult",
    "CALLER_HUMAN", "CALLER_AI", "CALLER_PLUGIN",
]

CALLER_HUMAN = "human"      # 面板上的真人
CALLER_AI = "ai"            # AI 工具
CALLER_PLUGIN = "plugin"    # 另一个插件（经 gm.region）

# ---- 默认值 ---------------------------------------------------------------
# 这些是**兜底**值：真正的缺省来自 GreenMoon 的 tools_config.block_record
# 一节。之所以要在代码里再写一遍，是为了保证在某个版本的面板配置里
# 缺了这一节时，功能仍能以一个保守但可用的状态跑起来 ——
# 而不是直接 500。
_DEFAULTS: Dict[str, Any] = {
    "enabled": False,               # 默认关闭：记录全服方块变更开销不小
    #: 记录颗粒度：0 关闭 / 1 仅破坏 / 2 破坏 + 放置。
    #: ★ 必须在这里声明 —— ``_ConfigView.as_dict()`` 只保留本表里出现过的键，
    #:   漏声明的话配置里的 level 会被静默丢掉，档位看起来"怎么改都没用"。
    "max_records_per_rollback": 5000,
    "blocks_per_batch": 400,
    "batch_interval_ticks": 1,
    "max_area_blocks": 4_000_000,   # 约 160x160x160，防一次框整个地图
    "max_delta_seconds": 30 * 86400,
    "undo_requires_confirm": True,
    "rollback_requires_confirm": True,
    "retention_days": 30,
    "max_rows_per_query": 200,
    "auto_teleport": True,
    "teleport_wait_ticks": 200,
}

ACTION_LABELS: Dict[str, str] = {
    "break": "破坏", "place": "放置", "explode": "炸毁",
    "leaves_decay": "树叶凋零", "block_grow": "方块生长",
    "block_form": "方块形成", "liquid_flow": "液体流动",
    # 活塞推/拉造成的方块位移（actor="(活塞)"）。与 block_form 一样属于
    # "记变化前"的动作，回档会按 block_type 把每格还原回去。
    "block_piston": "活塞推拉",
    # 旧版审计动作（仅历史库可能出现，保留标签以便正常显示）
    "piston_extend": "活塞伸出", "piston_retract": "活塞收回",
}


class RegionConfig:
    """配置读取器。

    用 ``get(section, key, default)`` 三参数签名，是为了让从 MxBack
    转录过来的引擎代码**一行都不用改** —— 它原本就是这么读的。

    同时提供 ``get(key, default)`` 的两参数简写供新代码使用。
    """

    def __init__(self, loader: Optional[Callable[[], Dict[str, Any]]] = None):
        self._loader = loader

    def _raw(self) -> Dict[str, Any]:
        if self._loader is None:
            return {}
        try:
            d = self._loader()
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def get(self, *args, **kwargs) -> Any:
        """兼容两种调用形式。

        * ``get(section, key, default=...)``（引擎侧沿用）
        * ``get(key, default=...)``（新代码）
        """
        default = kwargs.get("default")
        if len(args) >= 2:
            section, key = args[0], args[1]
            cur = self._raw()
            node = cur.get(section) if isinstance(cur.get(section), dict) else (cur if section in ("", None) else {})
            val = None if not isinstance(node, dict) else node.get(key)
            if val is None:
                val = cur.get(key)
            return default if val is None else val
        if len(args) == 1:
            key = args[0]
            val = self._raw().get(key)
            return default if val is None else val
        return default

    def as_dict(self) -> Dict[str, Any]:
        out = dict(_DEFAULTS)
        raw = self._raw()
        for k in list(out.keys()):
            if k in raw and raw[k] is not None:
                out[k] = raw[k]
        try:
            out["max_records_per_rollback"] = max(1, int(out["max_records_per_rollback"]))
        except Exception:
            out["max_records_per_rollback"] = _DEFAULTS["max_records_per_rollback"]
        try:
            out["blocks_per_batch"] = max(1, int(out["blocks_per_batch"]))
        except Exception:
            out["blocks_per_batch"] = _DEFAULTS["blocks_per_batch"]
        try:
            out["retention_days"] = max(1, int(out["retention_days"]))
        except Exception:
            out["retention_days"] = _DEFAULTS["retention_days"]
        try:
            out["max_rows_per_query"] = max(1, min(int(out["max_rows_per_query"]), 2000))
        except Exception:
            out["max_rows_per_query"] = _DEFAULTS["max_rows_per_query"]
        return out


class ApiResult:
    """统一的返回包。

    ★ 为什么不直接返回裸 dict
    ------------------------
    三种调用方需要的东西不一样：
      * 前端要 ``ok`` + 文案；
      * AI 工具需要一个能塞进对话的稳定结构；
      * 插件要能判断"该不该重试"。
    散着返回会让每处调用各自判断什么叫成功，迟早判断错。这里统一
    用 ``ok / code / message / data``，并且 ``to_text()`` 给出给人读的
    一句话摘要 —— AI 工具直接拿它当 ``content`` 回传即可。
    """

    __slots__ = ("ok", "code", "message", "data")

    def __init__(self, ok: bool, message: str = "", data: Any = None,
                 code: str = ""):
        self.ok = bool(ok)
        self.message = str(message or "")
        self.data = data if data is not None else {}
        self.code = str(code or ("ok" if ok else "error"))

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "code": self.code,
                "message": self.message, "data": self.data}

    def to_text(self, limit: int = 4000) -> str:
        """给 AI 回传的纯文本摘要。"""
        head = ("成功" if self.ok else "失败")
        parts = [f"[{head}] {self.message}"] if self.message else [f"[{head}]"]
        d = self.data
        if isinstance(d, dict):
            skip = {"ok", "message", "raw", "rows", "items", "entries", "fields"}
            kv = [f"{k}={v}" for k, v in d.items()
                  if k not in skip and isinstance(v, (str, int, float, bool))]
            if kv:
                parts.append("；".join(kv))
            for key in ("rows", "items", "entries", "fields"):
                rows = d.get(key)
                if isinstance(rows, list) and rows:
                    try:
                        body = json.dumps(rows[:80], ensure_ascii=False, default=str)
                    except Exception:
                        body = str(rows[:5])
                    if len(body) > 2200:
                        body = body[:2200] + "…（已截断）"
                    parts.append(body)
                    break
        txt = "\n".join(parts)
        return txt[:limit]

    def __bool__(self) -> bool:
        return self.ok


class _WriteQueue:
    """回档增量的落库队列（FIFO）。

    从 MxBack 移植而来。为什么回档相关的写必须严格 FIFO：
    批次波「增量」（undo 快照 / 会话绑定）与「收尾」（finalize_*）
    之间有因果顺序 —— 收尾必须看到它之前所有的批次都已提交，
    否则会话会被标记为"已回档 0 个方块"。
    """

    def __init__(self, store: RegionStore, logger=None):
        import queue as _q
        self._q: "_q.Queue" = _q.Queue()
        self._store = store
        self._logger = logger
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="gm-region-writer", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._thread = None

    def stop(self, drain: bool = True) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        t = self._thread
        if t is not None and t.is_alive():
            try:
                t.join(timeout=8.0)
            except Exception:
                pass

    def put(self, kind: str, payload: Any) -> None:
        try:
            self._q.put_nowait((kind, payload))
        except Exception:
            pass

    def wait_flush(self, timeout: float = 10.0) -> bool:
        """FIFO 屏障：返回 True 表示 barrier 之前入队的任务都已落库。"""
        t = self._thread
        if t is None or not t.is_alive():
            # 降级为同步模式：此刻没有后台消费者，队列里理应为空
            return True
        import threading as _th
        ev = _th.Event()
        try:
            self._q.put_nowait(("__barrier__", ev))
        except Exception:
            return False
        return ev.wait(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except Exception:
                continue
            if item is None:
                break
            kind, payload = item
            if kind == "__barrier__":
                try:
                    payload.set()
                except Exception:
                    pass
                continue
            try:
                self._apply(kind, payload)
            except Exception as e:
                try:
                    if self._logger is not None:
                        self._logger.error(f"[区块日志] 增量落库失败({kind}): {e}")
                except Exception:
                    pass

    def _apply(self, kind: str, payload: Any) -> None:
        st = self._store
        if kind == "session_logs":
            sid, ids = payload
            st.insert_session_logs(sid, ids)
        elif kind == "undo_rows":
            st.insert_undo_rows(payload)
        elif kind == "finalize_session":
            sid, applied = payload
            st.finalize_session(sid, applied)
        elif kind == "finalize_undo":
            st.finalize_undo(payload)
        elif kind == "pending_restore":
            # 区块未加载的恢复项：先落库，等 ChunkLoadEvent 补做
            st.enqueue_pending_restore(payload)
        elif kind == "pending_clear_session":
            # 撤销回档时取消该会话尚未执行的待恢复项
            st.delete_pending_restore_for_session(int(payload))


class RegionAPI:
    """区块日志与恢复的**唯一入口**。人 / AI / 插件都走这里。"""

    def __init__(self, plugin, data_dir, logger=None,
                 config_loader: Optional[Callable[[], Dict[str, Any]]] = None,
                 engine_factory: Optional[Callable[[Any], Any]] = None):
        self.plugin = plugin                    # GreenMoonPlugin
        self.logger = logger if logger is not None else getattr(plugin, "logger", None)
        self.config = RegionConfig(config_loader)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = RegionStore(data_dir, logger=self.logger)
        self.writer = _WriteQueue(self.store, logger=self.logger)
        self.writer.start()

        self._lock = threading.RLock()
        self._listeners: List[Callable[[str, dict], None]] = []
        self._stats = {"records": 0, "rollbacks": 0, "undos": 0,
                       "rejected": 0, "last_rollback_at": 0.0}

        # 引擎是**惰性**构造的：它 import 了 endstone.level，在没有
        # 服务器环境时（单元测试 / 纯配置场景）不该把模块顶崩。
        self._engine_factory = engine_factory
        self._engine = None
        # 记录缓冲
        self._pending: List[dict] = []
        self._pending_ev: List[dict] = []
        self._pending_lock = threading.Lock()

        # 待恢复队列的内存索引。ChunkLoadEvent 每个区块都会触发，
        # 不能在那里查库；命中集合才走数据库。首次使用时加载一次。
        self._restore_lock = threading.Lock()
        self._restore_chunks: set = set()
        self._restore_count = 0
        self._restore_loaded = False

    # ------------------------------------------------------------------
    # 引擎惰性装配
    # ------------------------------------------------------------------
    @property
    def engine(self):
        if self._engine is None:
            if self._engine_factory is not None:
                self._engine = self._engine_factory(self)
            else:
                from .host import build_engine
                self._engine = build_engine(self)
        return self._engine

    # ------------------------------------------------------------------
    # 记录侧（主线程调用，只入队不落盘）
    # ------------------------------------------------------------------
    @property
    def level(self) -> int:
        """当前的**记录颗粒度**。

        ``0`` 关闭 / ``1`` 仅破坏 / ``2`` 破坏 + 放置

        为什么要有颗粒度，而不是一个开/关：
          方块日志是"持续往磁盘写"的功能，而服主的需求差别很大 ——
          有人只想在建筑被炸后能恢复（那只需要记 ``break``/``explode``），
          有人还要能回档"谁偷偷盖了违章建筑"（那就得连 ``place`` 一起记）。
          全开是几百 MB 的库，只记破坏可能只有几十 MB。给一个档位让服主
          自己权衡，比逼他在"什么都记"和"什么都不记"之间二选一合理得多。

        ★ 兼容旧配置：早期只有 ``enabled`` 布尔开关，语义等价于"全部记录"，
          所以 ``enabled=true`` 视为 ``level=2``。老配置文件不用改就能跑。
        """
        # 运行时 override（set_enabled 用）优先 —— 否则那个开关是死的
        ov = getattr(self, "_enabled_override", None)
        if ov is not None:
            return 2 if ov else 0
        # ★ 这里读的是**原始配置**而不是 ``config.as_dict()``：
        #   as_dict() 会先用 _DEFAULTS 填一遍默认值，于是"配置里到底写没写
        #   level"这个信息就丢了 —— 会把下面那条旧配置兼容分支永远短路掉
        #   （as_dict 总给出 level=0，`is not None` 恒真）。用 raw 才能
        #   区分"显式配了 0"和"根本没配、只是默认值恰好是 0"。
        raw = self._raw_cfg()
        if raw.get("level") is not None:
            return max(0, min(int(raw.get("level")), 2))
        # 兼容旧配置（那时只有 enabled 布尔开关，语义 = 全部记录）
        return 2 if raw.get("enabled") else 0

    def _raw_cfg(self) -> Dict[str, Any]:
        """取未填默认值的原始配置段；失败返回空 dict。"""
        try:
            cn = getattr(self.config, "_raw", None)
            if callable(cn):
                got = cn()
                return got if isinstance(got, dict) else {}
        except Exception:
            pass
        try:
            got = self.config.as_dict()
            return got if isinstance(got, dict) else {}
        except Exception:
            return {}

    @property
    def enabled(self) -> bool:
        return self.level > 0

    #: 每种动作需要的最低颗粒度。低于它就不记。
    #:
    #: ★ 这个映射关系到「回档能做什么」，不是随手定的：
    #:   · ``break`` / ``explode`` —— 记的是"这里原来是什么"，有它才能把
    #:     被破坏的建筑恢复回去。所以档位 1 就能满足最常见的需求。
    #:   · ``place`` —— 记的是"这里被放了什么"，有它才能回档"谁盖的违规
    #:     建筑"。缺了它不影响恢复破坏（只是撤销不了放置）。
    #:   未列出的动作按 2 处理：宁可少记，也不要在低档位下悄悄多记 ——
    #:   颗粒度是服主用来"少写点磁盘"的手段，多记等于骗他。
    _ACTION_MIN_LEVEL: "dict[str, int]" = {
        "break": 1,
        "explode": 1,
        "place": 2,
        # 2026-05 修复（P0 Bug #2）：自然生成的方块（岩浆/水生成的石头、
        # 冰、黑曜石）原本完全不被记录，导致这类覆盖无法回档。把它放到
        # 最低档（与 break 同级），只要区块记录开着（level>=1）就记，
        # 让"被生成的石头/冰埋掉的东西"也能回档还原。
        "block_form": 1,
        # 2026-05 修复（P0 Bug #2）：活塞推/拉的方块位移。事件触发时按
        # direction 逐格快照"移动前"的方块，回档按 block_type 还原即可
        # 精确撤销。同样放在最低档：它属于"破坏/变化"这一类，而不是
        # 玩家主动放置。
        "block_piston": 1,
    }

    def record_block(self, *, actor: str, action: str, block_type: str,
                     block_states: str = "{}", x: int, y: int, z: int,
                     dimension: str, replaced_type: Optional[str] = None,
                     replaced_states: Optional[str] = None, ts: Optional[float] = None,
                     extra: Optional[str] = None) -> None:
        """记一条方块变更。**主线程安全，不碰磁盘。**"""
        lvl = self.level
        if lvl <= 0:
            return
        # 颗粒度过滤：集中在这一处，所有调用方（含子插件）自动遵守，
        # 不会出现"某个事件忘了判断档位"的漏洞。
        if self._ACTION_MIN_LEVEL.get(str(action), 2) > lvl:
            return
        with self._pending_lock:
            self._pending.append({
                "ts": ts if ts is not None else time.time(),
                "actor": str(actor or "未知")[:48],
                "action": str(action),
                "block_type": str(block_type or "minecraft:air"),
                "block_states": str(block_states or "{}"),
                "replaced_type": replaced_type,
                "replaced_states": replaced_states,
                "x": int(x), "y": int(y), "z": int(z),
                "dimension": str(dimension or ""),
                "extra": extra,
            })
            self._stats["records"] += 1

    def record_event(self, *, actor: str, action: str, detail: str = "",
                     x: int = 0, y: int = 0, z: int = 0, dimension: str = "-",
                     ts: Optional[float] = None) -> None:
        if not self.enabled:
            return
        with self._pending_lock:
            self._pending_ev.append({
                "ts": ts if ts is not None else time.time(),
                "actor": str(actor or "未知")[:48],
                "action": str(action),
                "detail": str(detail or "")[:2000],
                "x": int(x), "y": int(y), "z": int(z),
                "dimension": str(dimension or "-"),
            })

    def flush_records(self) -> int:
        """把内存缓冲写进数据库（由主线程定时任务调用）。"""
        with self._pending_lock:
            blocks, self._pending = self._pending, []
            evs, self._pending_ev = self._pending_ev, []
        n = 0
        try:
            if blocks:
                self.store.insert_records(blocks)
                n += len(blocks)
            if evs:
                self.store.insert_event_records(evs)
                n += len(evs)
        except Exception as e:
            if self.logger is not None:
                try:
                    self.logger.error(f"[区块日志] 落盘失败: {e}")
                except Exception:
                    pass
        return n

    def wait_write_flush(self, timeout: float = 10.0) -> bool:
        return self.writer.wait_flush(timeout)

    # ------------------------------------------------------------------
    # 待恢复队列（区块未加载 → 加载后补做）
    # ------------------------------------------------------------------
    def _ensure_restore_index(self) -> None:
        """首次使用时从库里加载"哪些区块有待恢复项"的内存索引。

        ★ 全程持有 ``_restore_lock``，**且 loaded 标记只在数据真正填充
          之后才置位**。

          之前的写法是"先置 loaded=True → 释放锁 → 再查库 → 回来填充"，
          中间那几毫秒是个真实的 TOCTOU 窗口：Web 面板的轮询线程（读
          pending_restore_count）刚把标记置上、还没查完库，服务器主线程
          的 ChunkLoadEvent 就进来了，一看 loaded 为真便直接放行，结果
          读到的是**还是空的** ``_restore_chunks`` —— 判定"这个区块没有
          欠账"，于是该区块的待恢复项被永久跳过，而且没有任何重试。

          有人会觉得"只漏几毫秒而已"。问题是那一瞬间恰好是服务器刚重启、
          待恢复项最多的时刻，被跳过的往往是最该补的那一批。

          现在改成：并发的第二个线程会在锁上等到第一个线程把数据填完，
          要么拿到完整索引，要么自己去做这次加载。代价只是首次加载期间
          其它线程多等几毫秒，而这一切只发生一次。
        """
        with self._restore_lock:
            if self._restore_loaded:
                return
            try:
                keys = self.store.list_pending_chunks()
                cnt = self.store.count_pending_restore()
            except Exception:
                # 库暂时不可用：**不**置位，下一次区块加载再试。
                # 否则一次瞬时失败会让待恢复项"永远不再被尝试"。
                return
            self._restore_chunks = {
                (str(d), int(cx), int(cz)) for d, cx, cz in keys}
            self._restore_count = int(cnt)
            self._restore_loaded = True

    def has_pending_restore(self) -> bool:
        self._ensure_restore_index()
        with self._restore_lock:
            return self._restore_count > 0

    def pending_restore_count(self) -> int:
        self._ensure_restore_index()
        with self._restore_lock:
            return int(self._restore_count)

    def enqueue_pending_restore(self, rows: List[tuple]) -> None:
        """登记待恢复项（引擎在主线程调用）。

        只进 FIFO 写队列，不在主线程碰磁盘；同时更新内存索引，
        让后续区块加载能 O(1) 命中。
        """
        if not rows:
            return
        # ★ 先把基线索引读出来，再往里加。否则可能发生：
        #   先 enqueue（写队列还没落库）→ 首次区块加载触发 _ensure
        #   → 用库里的旧数据**覆盖**掉刚加进去的集合 → 这些区块再也
        #   不会被补做。ensure 只查一次，开销可接受。
        self._ensure_restore_index()
        self.writer.put("pending_restore", rows)
        with self._restore_lock:
            self._restore_count += len(rows)
            for r in rows:
                try:
                    # (session_id, ts, dimension, x, y, z, type, states)
                    self._restore_chunks.add(
                        (str(r[2]), int(r[3]) >> 4, int(r[5]) >> 4))
                except Exception:
                    continue

    def clear_pending_session(self, session_id: int) -> None:
        """撤销回档时取消该会话的待恢复项，并让内存索引下次重载。

        索引重载是必须的：否则 ``has_pending_restore()`` 会在界面上一
        直显示"还有 N 个待恢复"，实际已经被撤销取消了。
        """
        self.writer.put("pending_clear_session", int(session_id))
        with self._restore_lock:
            self._restore_loaded = False

    def mark_pending_chunk(self, dimension: str, x: int, z: int) -> None:
        """把区块重新标记为"有待恢复"（补做失败时的兜底）。

        例如实体方块的两段式写入在第二阶段失败：行还留在库里，但索引
        已经被移除。重新加回去，下一次区块加载会再试一次。
        """
        with self._restore_lock:
            self._restore_chunks.add(
                (str(dimension), int(x) >> 4, int(z) >> 4))

    def apply_pending_chunk(self, cx: int, cz: int, dimension: str) -> int:
        """区块加载后补做该区块的恢复。返回本次处理条数。

        ★ ChunkLoadEvent 对每个加载的区块都会触发，这里第一步查内存集合，
          没有待办立即返回 —— 绝不能让所有区块加载都去查数据库。
        """
        key = (str(dimension), int(cx), int(cz))
        self._ensure_restore_index()
        with self._restore_lock:
            if key not in self._restore_chunks:
                return 0
        try:
            done, more = self.engine.apply_pending_chunk(
                int(cx), int(cz), str(dimension))
        except Exception as e:
            if self.logger is not None:
                try:
                    self.logger.warning(f"[区块日志] 补做待恢复失败: {e}")
                except Exception:
                    pass
            return 0
        with self._restore_lock:
            self._restore_count = max(0, self._restore_count - int(done))
            if not more:
                self._restore_chunks.discard(key)
        return int(done)

    # ------------------------------------------------------------------
    # 事件总线：WebUI / AI / 第三方插件的实时通路
    # ------------------------------------------------------------------
    def subscribe(self, fn: Callable[[str, dict], None]) -> Callable[[], None]:
        """订阅回档事件。返回取消订阅函数。

        事件名：``started`` / ``progress`` / ``done`` / ``no_change`` /
        ``error``。payload 至少含 ``session_id`` / ``actor`` / ``caller``。
        """
        with self._lock:
            self._listeners.append(fn)

        def _off():
            with self._lock:
                try:
                    self._listeners.remove(fn)
                except ValueError:
                    pass
        return _off

    def publish(self, event: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            fns = list(self._listeners)
        for fn in fns:
            try:
                fn(str(event), dict(payload or {}))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 只读能力（三类调用者都能直接用）
    # ------------------------------------------------------------------
    def _coords(self, data: Dict[str, Any], prefix: str = "") -> Tuple[int, int, int]:
        def pick(name: str, alt: str) -> int:
            raw = data.get(prefix + name, data.get(alt))
            if raw is None:
                raise ValueError(f"缺少坐标 {name}")
            return int(round(float(raw)))
        if prefix:
            return pick("x", "x1"), pick("y", "y1"), pick("z", "z1")
        return int(data["x"]), int(data["y"]), int(data["z"])

    @staticmethod
    def _norm_region(spec: Dict[str, Any]) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
        """把各种书写形式归一成 ``((x1,y1,z1), (x2,y2,z2))``。

        支持的写法（三种混着给也能识别）：
          * ``p1/p2`` 为 ``[x,y,z]`` 三元组；
          * ``x1,y1,z1,x2,y2,z2`` 六个标量；
          * ``center`` + ``radius``（立方体）。
        """
        if isinstance(spec.get("p1"), (list, tuple)) and isinstance(spec.get("p2"), (list, tuple)):
            a, b = spec["p1"], spec["p2"]
            if len(a) != 3 or len(b) != 3:
                raise ValueError("p1 / p2 必须是 [x, y, z] 三元组")
            return (int(a[0]), int(a[1]), int(a[2])), (int(b[0]), int(b[1]), int(b[2]))
        if all(k in spec for k in ("x1", "y1", "z1", "x2", "y2", "z2")):
            return ((int(spec["x1"]), int(spec["y1"]), int(spec["z1"])),
                    (int(spec["x2"]), int(spec["y2"]), int(spec["z2"])))
        c = spec.get("center")
        r = spec.get("radius", spec.get("r"))
        if isinstance(c, (list, tuple)) and len(c) == 3 and r is not None:
            r = int(r)
            return ((int(c[0]) - r, int(c[1]) - r, int(c[2]) - r),
                    (int(c[0]) + r, int(c[1]) + r, int(c[2]) + r))
        raise ValueError("无法识别的区域描述：需要 p1+p2，或 x1..z2，或 center+radius")

    def _guard_region(self, pos1, pos2, cfg: Dict[str, Any]) -> Optional[str]:
        vol = (abs(pos1[0] - pos2[0]) + 1) * (abs(pos1[1] - pos2[1]) + 1) * (abs(pos1[2] - pos2[2]) + 1)
        if vol > int(cfg.get("max_area_blocks", _DEFAULTS["max_area_blocks"])):
            return (
                f"区域体积 {vol:,} 格超出上限 "
                f"{int(cfg.get('max_area_blocks', _DEFAULTS['max_area_blocks'])):,} 格。"
                f"请缩小范围或分几次处理 —— 一次性框住整个地图会让服务端"
                f"长时间被单个任务占满。"
            )
        return None

    @staticmethod
    def _norm_seconds(spec: Dict[str, Any], cfg: Dict[str, Any]) -> float:
        """时间窗归一成秒。支持多种书写形式。"""
        if spec.get("seconds") is not None:
            s = float(spec["seconds"])
        elif spec.get("minutes") is not None:
            s = float(spec["minutes"]) * 60.0
        elif spec.get("hours") is not None:
            s = float(spec["hours"]) * 3600.0
        elif spec.get("days") is not None:
            s = float(spec["days"]) * 86400.0
        else:
            s = float(cfg.get("default_delta_seconds", 3600.0))
        cap = float(cfg.get("max_delta_seconds", _DEFAULTS["max_delta_seconds"]))
        return max(1.0, min(s, cap))

    def query_recent(self, *, limit: int = 50, actor: str = "", action: str = "",
                     dimension: str = "", caller: str = CALLER_HUMAN) -> ApiResult:
        """最近的方块变更流水（只读）。"""
        try:
            cfg = self.config.as_dict()
            n = max(1, min(int(limit or 50), int(cfg.get("max_rows_per_query", 200))))
            rows = self.store.recent_block_log(actor=actor, action=action,
                                               dimension=dimension, limit=n)
            return ApiResult(True, f"最近 {len(rows)} 条方块变更", {
                "rows": rows, "count": len(rows),
                "action_labels": ACTION_LABELS,
            })
        except Exception as e:
            return ApiResult(False, f"查询失败：{e}", code="query_failed")

    def query_position(self, *, x: int, y: int, z: int, dimension: str,
                       limit: int = 20, caller: str = CALLER_HUMAN) -> ApiResult:
        """某个坐标的历史（只读）——"这一格以前是什么"。"""
        try:
            n = max(1, min(int(limit or 20), 200))
            # ★ 维度留空必须回退到默认维度。
            #   store 层是 ``WHERE dimension=?`` 精确匹配，传空串会
            #   **一条都查不到**；而工具描述里明写"留空用服务端默认维度"。
            #   不回退的话，AI 不填维度就会得出"这一格从未被改动过"
            #   这种完全错误的结论（preview / rollback 早就有这个回退，
            #   唯独这里漏了）。
            dim = str(dimension or self._default_dimension())
            rows = self.store.query_position(int(x), int(y), int(z), dim, n)
            out = [dict(r) for r in rows]
            for r in out:
                r["time"] = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
                r["action_label"] = ACTION_LABELS.get(r.get("action"), r.get("action"))
            return ApiResult(True, f"坐标 ({x}, {y}, {z}) 共 {len(out)} 条记录",
                             {"rows": out, "count": len(out)})
        except Exception as e:
            return ApiResult(False, f"查询失败：{e}", code="query_failed")

    def list_sessions(self, *, limit: int = 20, actor: str = "",
                      caller: str = CALLER_HUMAN) -> ApiResult:
        """回档会话列表（只读）。"""
        try:
            n = max(1, min(int(limit or 20), 200))
            rows = self.store.list_sessions(
                player=(str(actor) if actor else None), limit=n)
            out = []
            for r in rows:
                d = dict(r)
                d["time"] = datetime.fromtimestamp(d["ts"]).strftime("%Y-%m-%d %H:%M:%S")
                d["volume"] = ((abs(d["x1"] - d["x2"]) + 1)
                               * (abs(d["y1"] - d["y2"]) + 1)
                               * (abs(d["z1"] - d["z2"]) + 1))
                out.append(d)
            return ApiResult(True, f"共 {len(out)} 个回档会话", {"rows": out, "count": len(out)})
        except Exception as e:
            return ApiResult(False, f"查询失败：{e}", code="query_failed")

    def status(self, *, caller: str = CALLER_HUMAN) -> ApiResult:
        """运行状态 + 库统计（只读）。"""
        try:
            st = self.store.stats()
            cfg = self.config.as_dict()
            busying = False
            try:
                busying = bool(self.engine.is_busy())
            except Exception:
                pass
            return ApiResult(True, "区块日志状态", {
                "enabled": self.enabled,
                # 记录颗粒度（0 关闭 / 1 仅破坏 / 2 破坏+放置）——
                # 前端拿它回显滑块，也让 AI 知道"现在记到什么程度"。
                "level": self.level,
                "busy": busying,
                "records": st.get("total", 0),
                "events": st.get("events", 0),
                "sessions": st.get("sessions", 0),
                "rolled_back": st.get("rolled_back", 0),
                "oldest": datetime.fromtimestamp(st["oldest_ts"]).strftime("%Y-%m-%d %H:%M:%S")
                if st.get("oldest_ts") else None,
                "file_bytes": int(st.get("size_bytes", 0) or 0),
                "queued_records": len(self._pending) + len(self._pending_ev),
                # 区块未加载而暂存的恢复项数量（加载后自动补做）。
                # 面板可以据此显示"还有 N 个方块待恢复"，避免服主以为
                # 回档失败了。
                "pending_restore": self.pending_restore_count(),
                "limits": {
                    "max_records_per_rollback": cfg.get("max_records_per_rollback"),
                    "max_area_blocks": cfg.get("max_area_blocks"),
                    "max_delta_seconds": cfg.get("max_delta_seconds"),
                },
                # ★ 把完整配置一并返回，而不是让前端去调 /api/tools/config。
                #   为什么：那个端点的 scope 是 ``backup``/``cloud``。
                #   一个只被授予了 ``region`` 的临时账号去读它会被 403 ——
                #   表现为"设置区一片空白"，而且前端拿不到错误原因
                #   （因为它是"顺便刷一下"的调用，不会弹提示）。
                #   这里把这一节自己的配置随状态一起给出，权限边界更干净：
                #   看区块设置只需要 region 权限，不需要顺手拿到备份路径、
                #   云黑 token 这些跟它无关的东西。
                "settings": dict(
                    {
                        k: cfg.get(k) for k in (
                            "enabled", "retention_days", "max_records_per_rollback",
                            "blocks_per_batch", "batch_interval_ticks",
                            "max_area_blocks", "max_delta_seconds",
                            "max_rows_per_query", "auto_teleport",
                        )
                    },
                    # ★ level 单独取属性而不是 cfg.get("level")：
                    #   cfg 来自 as_dict()，它会先用 _DEFAULTS 填一遍默认值，
                    #   于是"配置里到底写没写 level"就丢了；而 self.level
                    #   已经处理过旧 enabled 兼容与 0~2 夹取，是唯一权威值。
                    level=self.level,
                ),
                "counters": dict(self._stats),
            })
        except Exception as e:
            return ApiResult(False, f"获取状态失败：{e}", code="status_failed")

    # ------------------------------------------------------------------
    # 写能力
    # ------------------------------------------------------------------
    def preview(self, *, region: Dict[str, Any], dimension: str = "",
                caller: str = CALLER_HUMAN, **timekw) -> ApiResult:
        """回档**影响面预览**（只读，不改动世界）。

        ★ 这是 AI 调用回档前**必须**先走的一步。
          它回答"这次会动多少个方块、主要涉及哪些类型、动了谁的"。
          让人拿着这份预览点头，比让他对着一堆坐标点头有意义得多。
        """
        try:
            cfg = self.config.as_dict()
            pos1, pos2 = self._norm_region(region)
            err = self._guard_region(pos1, pos2, cfg)
            if err:
                return ApiResult(False, err, code="region_too_large")
            seconds = self._norm_seconds(dict(region, **timekw), cfg)
            since = time.time() - seconds
            summary = self.store.preview_region(
                pos1, pos2, str(dimension or self._default_dimension()), since,
                limit=int(cfg.get("max_records_per_rollback", 5000)))
            summary.update({
                "p1": list(pos1), "p2": list(pos2),
                "dimension": str(dimension or self._default_dimension()),
                "seconds": seconds,
                "since_text": datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M:%S"),
                "action_labels": ACTION_LABELS,
            })
            return ApiResult(True, "影响面预览", summary)
        except ValueError as e:
            return ApiResult(False, str(e), code="bad_params")
        except Exception as e:
            return ApiResult(False, f"预览失败：{e}", code="preview_failed")

    def rollback(self, *, region: Dict[str, Any], dimension: str = "",
                 actor: str = "", caller: str = CALLER_HUMAN,
                 notify: str = "", dry_run: bool = False,
                 exclude_blocks=None, only_blocks=None,
                 rollback_water: bool = True, rollback_lava: bool = True,
                 **timekw) -> ApiResult:
        """执行区域回档。

        ★ 为什么还要单独留一个 ``dry_run``，而不是让调用方去用 ``preview()``
        --------------------------------------------------------------------
        ``preview()`` 是纯 SQL 统计，只知道"命中多少条"，并不推导每条
        记录最终要落到哪个目标类型（``place`` 要还原成 ``replaced_*``、
        ``break`` 要还原成 ``block_type``）。这两者数量可能差很多：
        同一格坐标在窗内被反复改动时，真正会写的只有最早那一次。

        ``dry_run=True`` 会走完整的查询与选点路径，但**在写世界之前停手**，
        给出真正会动的方块数 —— 这是批准之前最可靠的一份依据。
        """
        try:
            cfg = self.config.as_dict()
            if not self.enabled:
                return ApiResult(False, "区块记录功能未开启，无法回档", code="disabled")
            pos1, pos2 = self._norm_region(region)
            err = self._guard_region(pos1, pos2, cfg)
            if err:
                self._stats["rejected"] += 1
                return ApiResult(False, err, code="region_too_large")
            seconds = self._norm_seconds(dict(region, **timekw), cfg)
            dim = str(dimension or self._default_dimension())
            who = str(actor or "")
            if not who:
                return ApiResult(False, "缺少 actor（谁发起的），无法确定责任人",
                                 code="missing_actor")

            eng = self.engine
            if eng.is_busy():
                return ApiResult(False, "已有回档/撤销任务正在进行，请稍候",
                                 code="busy")

            # dry_run：拿到将要作用的记录集合，但不执行
            if dry_run:
                prev = self.store.preview_region(
                    pos1, pos2, dim, time.time() - seconds,
                    limit=int(cfg.get("max_records_per_rollback", 5000)))
                prev.update({"p1": list(pos1), "p2": list(pos2), "dimension": dim,
                             "seconds": seconds, "dry_run": True})
                return ApiResult(True, "试运行：未对世界做任何修改", prev)

            # 先把内存里的记录推到库里，否则刚发生的变更会被漏掉
            try:
                self.flush_records()
            except Exception:
                pass
            if not self.wait_write_flush(10.0):
                return ApiResult(False, "数据库写入繁忙，请稍后再试", code="db_busy")

            res = eng.execute(
                who, pos1, pos2, dim, seconds,
                notify_name=str(notify or "") or None,
                exclude_blocks=exclude_blocks, only_blocks=only_blocks,
                rollback_water=rollback_water, rollback_lava=rollback_lava,
                caller=caller,
            )
            if not isinstance(res, dict):
                return ApiResult(False, "回档未能启动", code="not_started")
            if res.get("busy"):
                return ApiResult(False, "已有回档/撤销任务正在进行", code="busy")
            if "error" in res:
                self._stats["rejected"] += 1
                return ApiResult(False, str(res["error"]), code="engine_error")
            if res.get("selected", 0) == 0:
                return ApiResult(
                    True, "该区域在指定时间窗内没有需要回档的记录",
                    {"started": False, "session_id": None, "records_hit": 0})
            self._stats["rollbacks"] += 1
            self._stats["last_rollback_at"] = time.time()
            self.publish("started", {
                "session_id": res.get("session_id"), "actor": who,
                "caller": caller, "p1": list(pos1), "p2": list(pos2),
                "dimension": dim, "seconds": seconds,
                "records_selected": res.get("selected"),
                "truncated": bool(res.get("truncated")),
            })
            return ApiResult(True, "回档已启动，分批执行中", dict(res))
        except ValueError as e:
            return ApiResult(False, str(e), code="bad_params")
        except Exception as e:
            if self.logger is not None:
                try:
                    self.logger.error(f"[区块日志] 回档失败: {e}")
                except Exception:
                    pass
            return ApiResult(False, f"回档失败：{e}", code="exception")

    def undo(self, *, session_id: Optional[int] = None, actor: str = "",
             caller: str = CALLER_HUMAN, notify: str = "",
             dry_run: bool = False) -> ApiResult:
        """撤销一次回档。"""
        try:
            if not self.enabled:
                return ApiResult(False, "区块记录功能未开启", code="disabled")
            who = str(actor or "")
            if not who:
                return ApiResult(False, "缺少 actor", code="missing_actor")
            eng = self.engine
            if eng.is_busy():
                return ApiResult(False, "已有回档/撤销任务正在进行", code="busy")

            sid = None if session_id in (None, "", 0) else int(session_id)
            if sid is None:
                row = self.store.get_last_session_any()
                if row is None:
                    return ApiResult(True, "没有可撤销的回档记录",
                                     {"started": False, "session_id": None})
                sid = int(row["id"])
            row = self.store.get_session(sid)
            if row is None:
                return ApiResult(False, f"找不到回档会话 #{sid}", code="no_session")
            if row["undone"]:
                return ApiResult(False, "该回档已被撤销过，不能重复撤销", code="already_undone")

            if dry_run:
                return ApiResult(True, "试运行：未对世界做任何修改", {
                    "dry_run": True, "session_id": sid,
                    "session_time": datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
                    "actor_of_record": row["player"],
                    "dimension": row["dimension"],
                    "p1": [row["x1"], row["y1"], row["z1"]],
                    "p2": [row["x2"], row["y2"], row["z2"]],
                    "seconds": row["seconds"],
                    "block_count": row["block_count"],
                })

            if not self.wait_write_flush(10.0):
                return ApiResult(False, "数据库写入繁忙，请稍后再试", code="db_busy")

            res = eng.undo(who, any_player=True, session_id=sid,
                           notify_name=str(notify or "") or None,
                           allow_any=True, caller=caller)
            if res is None:
                return ApiResult(True, "没有可撤销的回档记录",
                                 {"started": False, "session_id": None})
            if not isinstance(res, dict):
                return ApiResult(False, "撤销未能启动", code="not_started")
            if res.get("busy"):
                return ApiResult(False, "已有回档/撤销任务正在进行", code="busy")
            if "error" in res:
                return ApiResult(False, str(res["error"]), code="engine_error")
            if res.get("no_snapshot"):
                return ApiResult(
                    True, "该回档没有产生实际的方块变化，无需撤销；"
                          "相关日志已恢复为未回档状态，可再次回档。",
                    dict(res))
            self._stats["undos"] += 1
            self.publish("started", {
                "session_id": res.get("session_id"), "actor": who,
                "caller": caller, "kind": "undo",
                "expected": res.get("expected"),
            })
            return ApiResult(True, "撤销已启动，分批执行中", dict(res))
        except ValueError as e:
            return ApiResult(False, str(e), code="bad_params")
        except Exception as e:
            if self.logger is not None:
                try:
                    self.logger.error(f"[区块日志] 撤销失败: {e}")
                except Exception:
                    pass
            return ApiResult(False, f"撤销失败：{e}", code="exception")

    def purge(self, days: int = 0, *, caller: str = CALLER_HUMAN) -> ApiResult:
        """按天数清理过期记录。"""
        try:
            cfg = self.config.as_dict()
            d = int(days or cfg.get("retention_days", 30))
            if d <= 0:
                return ApiResult(False, "天数必须为正整数", code="bad_params")
            row = self.store.purge(d)
            return ApiResult(True, f"已清理 {d} 天前的记录", dict(row))
        except Exception as e:
            return ApiResult(False, f"清理失败：{e}", code="purge_failed")

    def set_enabled(self, on: bool, *, caller: str = CALLER_HUMAN) -> ApiResult:
        """运行时开关记录（不改配置文件；持久开关请写 ``block_record.level``）。

        ★ 顺手修掉的一个真 bug：这里原本只写 ``self._enabled_override``，
          但 ``enabled`` 属性**从来没读过它** —— 于是这个开关是完全无效的：
          调用返回"区块记录已开启"，实际一条都不记，而且没有任何报错。
          现在 ``level`` 会优先读 override，开关真的生效了。
        """
        try:
            self._enabled_override = bool(on)
            return ApiResult(True, f"区块记录已{'开启' if on else '关闭'}",
                             {"enabled": bool(on), "level": self.level})
        except Exception as e:
            return ApiResult(False, f"切换失败：{e}", code="toggle_failed")

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _default_dimension(self) -> str:
        try:
            lv = getattr(self.plugin.server, "level", None)
            if lv is not None:
                dims = list(lv.dimensions)
                if dims:
                    return str(getattr(dims[0], "name", "") or "overworld")
        except Exception:
            pass
        return "overworld"

    def shutdown(self) -> None:
        try:
            self.flush_records()
        except Exception:
            pass
        try:
            self.writer.stop()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass
