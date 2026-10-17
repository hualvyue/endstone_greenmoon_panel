"""玩家行为记录系统。

设计目标
--------
给 AI 提供"**过去发生了什么**"的查询能力。用户原话：
「这个是方便AI调用来查看的，不用单独板块」—— 所以这里**只提供数据层
与查询 API**，面板 UI 一概不做（行为数据本身不面向人展示）。

三档颗粒度
----------
配置项 ``behavior_record_level``：

===== ==========================================================
 0    关闭（**默认**）。不产生任何入队与 IO 开销
 1    仅记录聊天
 2    聊天 + 敏感行为（爆炸物相关的 8 类行为，见 ``_KIND_LEVEL``）
 3    全记录（上述全部 + 进出服 / 方块增删 / 物品 / 攻击 / 死亡 /
      命令 / 传送 / 容器）
===== ==========================================================

★ 为什么默认是 0（关闭）
-----------------------
player 行为属于**隐私数据**，且 level 3 在活跃服上写入量很可观
（每秒十几条不稀奇）。默认打开等于替服主做了一个他没同意的决定。
所以默认关闭，由服主显式选择档位 —— 这与项目里其它"默认保守"的
约定一致（``chat_enabled=False`` / ``enable_log_analysis=False``）。

线程模型（**这是本模块最容易写错的地方**）
------------------------------------------
Endstone 的事件回调跑在**服务器主线程（tick 线程）**上。在主线程里
做文件 IO 会直接卡 tick —— 项目里已有血泪注释警告过（``core.py``
附近 "在主线程里读文件会直接卡 tick"）。

所以本模块严格分两层：

* **主线程侧**：``record()`` 只做「判断闸门 + 塞进 Queue」，
  全程 O(1)、无锁竞争（``queue.Queue`` 自带锁且几乎不阻塞），
  **绝不碰磁盘**。
* **后台线程侧**：单个 daemon writer 线程消费 Queue，批量追加
  写 JSONL。文件打开失败、磁盘满、权限不足 —— 一律吞掉并
  ``logger.warning``，**绝不向上抛**。一条行为记录没记上，
  远好过把服务器 tick 拖垮。

内存索引
--------
后台线程写完会同时把记录推进 ``deque(maxlen=_MEM_MAX)``，供 AI 工具
**快速查询最近的行为**，避免每次查询都去读整个 JSONL 文件。
注意这是**有损**的（只保留最近 _MEM_MAX 条），查询更早的数据时
会自动回落到读文件 —— 见 ``query()``。

文件格式：JSONL
---------------
每行一个 JSON 对象。选 JSONL 而不是一次性 ``json.dump`` 整个数组，
是因为行为记录是**只追加、几乎不修改**的数据：

* 追加是 O(1)，不用把已有几万条全读进内存再全量重写；
* 写到一半崩了，最坏情况只丢最后一行（JSONL 天然可容忍尾部残缺），
  而 JSON 数组会整个文件报废；
* 可以直接用 ``tail`` 之类的工具人工排查。

滚动清理
--------
``prune()`` 按**双重上限**清理：``keep_days``（天数）+ ``max_records``
（条数）。两者都要，因为：
* 只看天数 —— 有人在一天内疯狂放 TNT，单日就能把文件撑到几百 MB；
* 只看条数 —— 一个冷清服可能攒了一年的记录，早就没有参考价值。

启动时清理一次，之后由每日任务再清理（见 ``core.py`` 的调度）。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "BehaviorRecorder",
    "LEVEL_OFF", "LEVEL_CHAT", "LEVEL_SENSITIVE", "LEVEL_FULL",
    "KIND_LEVEL", "LEVEL_LABEL", "BEHAVIOR_KINDS",
]


# ---------------------------------------------------------------------------
# 档位常量
# ---------------------------------------------------------------------------
LEVEL_OFF = 0
LEVEL_CHAT = 1
LEVEL_SENSITIVE = 2
LEVEL_FULL = 3

LEVEL_LABEL: Dict[int, str] = {
    LEVEL_OFF: "关闭",
    LEVEL_CHAT: "仅聊天",
    LEVEL_SENSITIVE: "聊天 + 敏感行为",
    LEVEL_FULL: "全记录",
}


# ---------------------------------------------------------------------------
# 行为类型 → 所需档位
#
# ★ 这张表是**唯一的事实来源**：闸门判断、档位说明、测试断言全部读它。
#   加新行为只需在这里加一行，不用去改闸门逻辑。
#
# 为什么用"所需最低档位"而不是"每档一个清单"：
#   档位是**递增包含**关系（3 ⊇ 2 ⊇ 1），用最低档位表达天然满足
#   这个偏序关系，不会出现"某行为在第 3 档有、第 2 档没有、第 1 档
#   又有"这种自相矛盾的清单。
# ---------------------------------------------------------------------------
KIND_LEVEL: Dict[str, int] = {
    # ---- 第 1 档：仅聊天 ----
    "chat": LEVEL_CHAT,

    # ---- 第 2 档：敏感行为（爆炸物相关，8 类）----
    # 用户明确要求"全部保留"，不要以"噪音大"为由删减。
    "place_tnt": LEVEL_SENSITIVE,          # 放置 TNT
    "place_bed": LEVEL_SENSITIVE,          # 放置床 / 重生锚
    "use_flint": LEVEL_SENSITIVE,          # 使用打火石
    "use_bucket": LEVEL_SENSITIVE,         # 使用岩浆桶 / 水桶
    "ignite_tnt": LEVEL_SENSITIVE,         # 点燃 TNT
    "entity_explode": LEVEL_SENSITIVE,     # 实体爆炸（TNT / 苦力怕）
    "block_explode": LEVEL_SENSITIVE,      # 方块爆炸（床 / 重生锚）
    "break_explosive": LEVEL_SENSITIVE,    # 破坏 TNT / 床 / 重生锚

    # ---- 第 3 档：全记录 ----
    "place_command_block": LEVEL_FULL,     # 放置命令方块（高危，但归全记录）
    "join": LEVEL_FULL,                    # 进入服务器
    "quit": LEVEL_FULL,                    # 离开服务器
    "block_place": LEVEL_FULL,             # 放置普通方块
    "block_break": LEVEL_FULL,             # 破坏普通方块
    "interact": LEVEL_FULL,                # 其它交互
    "command": LEVEL_FULL,                 # 执行命令
    "death": LEVEL_FULL,                   # 死亡
    "teleport": LEVEL_FULL,                # 传送
    "attack": LEVEL_FULL,                  # 攻击
    "item_pickup": LEVEL_FULL,             # 拾取物品
    "container": LEVEL_FULL,               # 容器开关
}

#: 对外可见的行为类型（供工具的参数说明使用）
BEHAVIOR_KINDS: Tuple[str, ...] = tuple(KIND_LEVEL.keys())

#: 敏感行为的类型清单（第 2 档就是它们，工具里常用）
SENSITIVE_KINDS: Tuple[str, ...] = tuple(
    k for k, v in KIND_LEVEL.items() if v == LEVEL_SENSITIVE
)


def kind_level(kind: str) -> int:
    """某个行为类型所需的最低档位。

    未知类型返回 ``LEVEL_FULL``（最严格）—— 宁可多要一档，
    也不要因为拼错名字而把敏感行为放行到低档。
    """
    return KIND_LEVEL.get(str(kind or "").strip(), LEVEL_FULL)


# ---------------------------------------------------------------------------
# 文件名常量
# ---------------------------------------------------------------------------
_FILE_NAME = "behaviors.jsonl"

#: 内存索引容量。太小 → 查询频繁回落读文件；太大 → 白占内存。
#: 2000 条在 level 3 的活跃服上大约只覆盖几分钟，所以查询基本都会
#: 走读文件路径 —— 它存在的意义是让"查最近几条"这种常见请求
#: 不必碰磁盘。
_MEM_MAX = 2000

#: 单次查询返回上限（硬夹取），防止模型传个 limit=999999
_QUERY_HARD_LIMIT = 500

#: 单条 detail 字段最大长度，避免把整段聊天/物品 NBT 塞进记录
_DETAIL_MAX = 200

#: 队列哨兵：让 writer 线程重开文件句柄（清理后调用）。
#: 用一个独立的 ``object()`` 而不是 None/字符串，避免跟真实记录
#: 或停机哨兵混淆 —— 记录一定是 dict，停机哨兵是 None。
_REOPEN = object()


class BehaviorRecorder:
    """行为记录器。

    一个实例对应一个数据目录。**所有公开方法都不抛异常** ——
    调用方（尤其是事件回调）不需要写 try/except。
    """

    def __init__(self, data_dir: Path, logger=None):
        self._dir = Path(data_dir)
        self._file = self._dir / _FILE_NAME
        self._logger = logger

        #: 待写入队列。``maxsize`` 是**背压保护**：如果磁盘卡死导致
        #: writer 线程跟不上，主线程继续无限入队会吃光内存。
        #: 满了就丢弃 —— 行为记录丢掉几条远好过 OOM 崩服。
        self._q: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=10000)

        #: 内存索引（最近 N 条），加锁保护
        self._mem: deque = deque(maxlen=_MEM_MAX)
        self._mem_lock = threading.Lock()

        #: 统计计数（供 AI 工具/诊断使用）
        self._stats_lock = threading.Lock()
        self._dropped = 0          # 因队列满被丢弃的条数
        self._written = 0          # 已落盘条数

        self._level = LEVEL_OFF
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动后台 writer 线程。幂等。"""
        if self._started:
            return
        self._started = True
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._warn(f"创建行为记录目录失败：{e}")
        self._thread = threading.Thread(
            target=self._writer_loop, name="gm-behavior-writer", daemon=True
        )
        try:
            self._thread.start()
        except Exception as e:
            self._warn(f"启动行为记录线程失败：{e}")

    def stop(self) -> None:
        """停掉 writer 线程并尽量把队列里剩余记录刷盘。"""
        self._stop.set()
        try:
            # 唤醒可能正在阻塞 get() 的 writer
            self._q.put_nowait(None)
        except Exception:
            pass
        t = self._thread
        if t is not None and t.is_alive():
            try:
                t.join(timeout=3.0)
            except Exception:
                pass

    def set_level(self, level: int) -> None:
        """更新当前档位。

        ★ 由调用方在**每次事件发生时**传入实时档位（配置是实时读的），
        这里只做一个缓存供 ``enabled()`` 查询，不作为闸门判据 ——
        闸门判据始终以传入的 ``level`` 参数为准，避免缓存陈旧导致
        "关了还在记"。
        """
        try:
            self._level = max(LEVEL_OFF, min(int(level), LEVEL_FULL))
        except Exception:
            self._level = LEVEL_OFF

    def enabled(self, level: int = -1) -> bool:
        lv = self._level if level < 0 else level
        return int(lv) > LEVEL_OFF

    # ------------------------------------------------------------------
    # 写入侧（主线程调用）
    # ------------------------------------------------------------------
    def record(
        self,
        kind: str,
        player: str,
        detail: str = "",
        level: int = LEVEL_FULL,
        x: Optional[float] = None,
        y: Optional[float] = None,
        z: Optional[float] = None,
        dimension: str = "",
    ) -> bool:
        """记一条行为。**主线程安全，绝不碰磁盘，绝不抛异常。**

        返回是否真的入队（未入队通常是档位不够或队列满）。
        """
        try:
            need = kind_level(kind)
            if int(level) < need:
                return False

            rec: Dict[str, Any] = {
                "ts": time.time(),
                "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "player": str(player or "")[:32],
                "kind": str(kind or ""),
                "detail": str(detail or "")[:_DETAIL_MAX],
            }
            # 坐标只在拿得到时写入 —— 聊天之类的行为没有坐标，
            # 写 None 进去只会让 JSONL 变胖且查询时还要处理空值。
            if x is not None:
                try:
                    rec["x"] = round(float(x), 1)
                    rec["y"] = round(float(y), 1)
                    rec["z"] = round(float(z), 1)
                    if dimension:
                        rec["dim"] = str(dimension)[:32]
                except Exception:
                    pass

            self._q.put_nowait(rec)
            return True
        except queue.Full:
            with self._stats_lock:
                self._dropped += 1
                d = self._dropped
            # 只在第 1 次和第 10 次告警，避免刷屏
            if d in (1, 10):
                self._warn(
                    f"行为记录队列已满，正在丢弃记录（累计 {d} 条）。"
                    f"通常是磁盘写入受阻或档位过高导致，请检查磁盘空间。"
                )
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 后台 writer
    # ------------------------------------------------------------------
    def _writer_loop(self) -> None:
        fh = None
        try:
            fh = open(self._file, "a", encoding="utf-8")
        except Exception as e:
            self._warn(f"打开行为记录文件失败，本模块将静默停用：{e}")
            return

        def _do_reopen():
            """关闭并重开句柄。返回新句柄；失败返回 None。"""
            nonlocal fh
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
            try:
                return open(self._file, "a", encoding="utf-8")
            except Exception as e:
                self._warn(f"重开行为记录文件失败：{e}")
                return None

        try:
            while not self._stop.is_set():
                try:
                    item = self._q.get(timeout=1.0)
                except queue.Empty:
                    try:
                        fh.flush()
                    except Exception:
                        pass
                    continue

                if item is None:      # 停机哨兵
                    break

                if item is _REOPEN:
                    # 清理后用 os.replace 换了文件 —— 当前句柄指向旧 inode，
                    # 必须重开，否则后续写入全进幽灵文件。
                    nfh = _do_reopen()
                    if nfh is None:
                        return
                    fh = nfh
                    continue

                # 攒一批一起写，降低 flush 频率
                _need_reopen = False
                batch = [item]
                while len(batch) < 200:
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        break
                    if nxt is _REOPEN:
                        # 重开指令混进本批次：先把已攒的这批写完落盘，
                        # 再执行重开 —— 顺序不能反，否则这批记录会
                        # 写进即将被替换的文件里而丢失。
                        _need_reopen = True
                        break
                    batch.append(nxt)

                try:
                    lines = "".join(
                        json.dumps(r, ensure_ascii=False) + "\n" for r in batch
                    )
                    fh.write(lines)
                    fh.flush()
                except Exception as e:
                    self._warn(f"写入行为记录失败（将丢弃本批 {len(batch)} 条）：{e}")
                    # 尝试重开文件 —— 可能是磁盘满/句柄失效
                    nfh = _do_reopen()
                    if nfh is None:
                        return
                    fh = nfh

                # 落盘后再进内存索引（保证索引里的一定是已写的）
                try:
                    with self._mem_lock:
                        for r in batch:
                            self._mem.append(r)
                except Exception:
                    pass
                with self._stats_lock:
                    self._written += len(batch)

                # 本批次末尾夹带了重开指令 → 记录写完后立刻重开
                if _need_reopen:
                    nfh = _do_reopen()
                    if nfh is None:
                        return
                    fh = nfh
        except Exception as e:
            self._warn(f"行为记录线程异常退出：{e}")
        finally:
            try:
                if fh is not None:
                    fh.flush()
                    fh.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 查询侧（AI 工具调用）
    # ------------------------------------------------------------------
    def _load_from_file(self, limit: int) -> List[dict]:
        """从 JSONL 文件尾部读最近 ``limit`` 行。

        ★ 用「从文件尾部反向读块」而不是把整个文件读进内存：
        level 3 跑一个月后文件可能上百 MB，整读会吃掉大量内存且很慢。
        这里每次读 64KB，凑够就停。
        """
        out: List[dict] = []
        try:
            if not self._file.exists():
                return out
            block = 64 * 1024
            with open(self._file, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                pos = fh.tell()
                buf = b""
                while pos > 0 and out.__len__() < limit:
                    step = min(block, pos)
                    pos -= step
                    fh.seek(pos)
                    buf = fh.read(step) + buf
                    # 按行切，保留最前面可能不完整的那段留给下一轮
                    parts = buf.split(b"\n")
                    buf = parts[0]
                    for raw in reversed(parts[1:]):
                        if not raw.strip():
                            continue
                        try:
                            out.append(json.loads(raw.decode("utf-8")))
                        except Exception:
                            continue
                        if len(out) >= limit:
                            break
            out.reverse()
        except Exception as e:
            self._warn(f"读取行为记录文件失败：{e}")
        return out

    def query(
        self,
        player: str = "",
        kind: str = "",
        limit: int = 50,
        kinds: Optional[Iterable[str]] = None,
    ) -> List[dict]:
        """查询行为记录，返回**按时间正序**的列表（最早的在前）。

        ``player`` 为空表示不限玩家；``kind`` 为空表示不限类型；
        ``kinds`` 给定多个类型时取并集（与 ``kind`` 是 AND 关系）。
        """
        try:
            n = int(limit)
        except Exception:
            n = 50
        n = max(1, min(n, _QUERY_HARD_LIMIT))

        want_player = str(player or "").strip().lower()
        want_kind = str(kind or "").strip()
        want_kinds = {str(k) for k in (kinds or []) if k}

        def _hit(r: dict) -> bool:
            if want_player and str(r.get("player", "")).lower() != want_player:
                return False
            if want_kind and str(r.get("kind", "")) != want_kind:
                return False
            if want_kinds and str(r.get("kind", "")) not in want_kinds:
                return False
            return True

        # 先看内存索引，不够再读文件补
        with self._mem_lock:
            mem = list(self._mem)
        hits = [r for r in reversed(mem) if _hit(r)]

        if len(hits) < n:
            # 内存里不够 —— 回落到文件。多读一些以补偿过滤损耗。
            # 取 max(n, 1000)：过滤条件越严，需要的原始行数越多。
            file_rows = self._load_from_file(max(n * 10, 1000))
            seen = {id(r) for r in hits}
            # 文件行也过滤后并入（去重靠 (ts, player, kind, detail)）
            existing = {
                (r.get("ts"), r.get("player"), r.get("kind"), r.get("detail"))
                for r in hits
            }
            more = []
            for r in reversed(file_rows):
                if not _hit(r):
                    continue
                key = (r.get("ts"), r.get("player"), r.get("kind"), r.get("detail"))
                if key in existing:
                    continue
                existing.add(key)
                more.append(r)
            hits.extend(more)
            del seen

        # hits 现在是倒序的，取最近 n 条后翻正
        hits = hits[:n]
        hits.reverse()
        return hits

    def list_recent(self, kind: str = "", limit: int = 50,
                    kinds: Optional[Iterable[str]] = None) -> List[dict]:
        return self.query(player="", kind=kind, limit=limit, kinds=kinds)

    def summarize(self, player: str = "", limit: int = 5000) -> Dict[str, Any]:
        """行为画像：按类型计数 + 首末时间。供 AI 快速了解一个人。"""
        rows = self.query(player=player, limit=min(limit, _QUERY_HARD_LIMIT))
        counts: Dict[str, int] = {}
        for r in rows:
            k = str(r.get("kind", "unknown"))
            counts[k] = counts.get(k, 0) + 1
        out: Dict[str, Any] = {
            "player": player or "(全员)",
            "total": len(rows),
            "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        }
        if rows:
            out["first"] = rows[0].get("t", "")
            out["last"] = rows[-1].get("t", "")
        return out

    def stats(self) -> Dict[str, Any]:
        """运行状态（供诊断/测试）。"""
        with self._stats_lock:
            dropped, written = self._dropped, self._written
        with self._mem_lock:
            mem_len = len(self._mem)
        try:
            size = self._file.stat().st_size if self._file.exists() else 0
        except Exception:
            size = 0
        return {
            "level": self._level,
            "queued": self._q.qsize(),
            "memory": mem_len,
            "written": written,
            "dropped": dropped,
            "file_bytes": size,
        }

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    def prune(self, keep_days: int = 7, max_records: int = 50000) -> int:
        """按天数 + 条数双重上限清理，返回删除的条数。

        ★ 为什么在这里用「全量重写」反而是对的：
        清理是低频操作（启动一次 + 每天一次），而清理往往要删掉
        文件开头的一大段 —— 追加模型无法"从中间删"。此时全量重写
        是唯一选择，且因为低频，代价可接受。
        写临时文件再 ``os.replace`` 保证原子性（复用项目 ``atomic_io``
        的同款策略），中途崩了不会毁掉原文件。
        """
        try:
            keep_days = max(1, int(keep_days))
            # ★ 条数下限只夹到 1，不要夹到 100 ——
            #   曾经写成 max(100, ...)，结果服主配 50 会被静默改成 100，
            #   测试传 2 更是完全失效（removed=0）。下限的唯一职责是
            #   挡住 0/负数（否则会把文件清空），不该替服主做决定。
            max_records = max(1, int(max_records))
        except Exception:
            keep_days, max_records = 7, 50000

        try:
            if not self._file.exists():
                return 0
            # 先把 writer 队列刷空，避免清理和写入互相打架
            self._drain()

            cutoff = time.time() - keep_days * 86400.0
            rows: List[str] = []
            kept = 0
            total = 0
            with open(self._file, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    total += 1
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        obj = json.loads(s)
                        ts = float(obj.get("ts") or 0)
                    except Exception:
                        continue      # 坏行直接丢弃
                    if ts and ts < cutoff:
                        continue      # 超期
                    rows.append(s)
                    kept += 1

            # 再按条数上限截断（保留最新的）
            if kept > max_records:
                rows = rows[-max_records:]
                kept = len(rows)

            removed = total - kept
            if removed <= 0:
                return 0

            tmp = self._file.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                for s in rows:
                    fh.write(s + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(tmp), str(self._file))

            # ★★ 关键：os.replace 换掉的是**目录项**，writer 线程手里
            #    那个已经打开的句柄仍指向**被替换掉的旧 inode**。
            #    如果不管它，之后所有新记录都会写进一个「没有任何路径
            #    指向它」的幽灵文件 —— 表现为「清理之后再也查不到新
            #    记录」，而且磁盘空间还在悄悄被吃掉。
            #    重开句柄是唯一正确解法：让 writer 重新绑定到新文件。
            self._reopen_writer()

            self._info(f"行为记录清理完成：删除 {removed} 条，保留 {kept} 条")
            return removed
        except Exception as e:
            self._warn(f"行为记录清理失败：{e}")
            return 0

    def _reopen_writer(self) -> None:
        """让 writer 线程重新打开目标文件（清理后必须调用）。

        实现方式：往队列里塞一个**特殊哨兵**，由 writer 线程自己执行
        关闭 + 重开。为什么不在这里直接 ``fh.close()``：句柄归 writer
        线程所有，跨线程关闭会让它下一次 ``write`` 撞上
        "I/O operation on closed file"，而且关与写之间还有竞态。
        让"拥有者"自己处理才是安全的。
        """
        try:
            self._q.put_nowait(_REOPEN)
        except Exception:
            pass

    def _drain(self, timeout: float = 2.0) -> None:
        """等队列写空（清理前调用，避免边写边清）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q.empty():
                return
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _warn(self, msg: str) -> None:
        try:
            if self._logger is not None:
                self._logger.warning(f"[行为记录] {msg}")
        except Exception:
            pass

    def _info(self, msg: str) -> None:
        try:
            if self._logger is not None:
                self._logger.info(f"[行为记录] {msg}")
        except Exception:
            pass
