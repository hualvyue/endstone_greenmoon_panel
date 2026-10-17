"""区块日志存储层（SQLite）。

来源
----
本文件由 ``endstone_mxback``（MxBack 方块日志与区域回档）的
``database.py`` 转录而来。移植到 GreenMoon 之后，它的身份变了：

* 原来：一个**独立插件**的主数据库，自己管生命周期、`/mxback db`
  图形化管理、每日日志文本归档；
* 现在：**行为记录系统的数据底座**，挂在 GreenMoon 面板下面，被
  WebUI（人）和 AI 工具（AI）**同一套 API** 访问。

为什么保留 SQLite 而不是接进既有的 JSONL 行为记录
-------------------------------------------------
行为记录（``behaviors.py``）是**追加流水**，只回答"最近发生了什么"，
从未支持过"把这个区域恢复到一小时前"。后者需要：

* 按 ``(x, y, z, dimension)`` 索引做区域扫描 —— JSONL 做不到，
  SQLite 有 ``idx_log_pos``；
* 记录上带 ``rolled_back`` 标记，防止同一个方块被回档两次；
* 回档前快照（``undo_log``）+ 会话绑定（``session_logs``），
  让"回档"本身可撤销。

这几件事只有二维查询 + 事务能做，所以这里保留独立的 SQLite 库。

表职责
------
``block_log``          方块变更流水（谁在何时把哪格变成了什么）
``event_log``          非方块的世界事件（含掉落物指纹快照）
``rollback_sessions``  每次回档一份，含区域 / 时间窗 / 执行者
``undo_log``           回档改动前的现场快照（撤销时回放）
``session_logs``       会话 ↔ 流水行的多对多（断定"已被回档"）

线程模型
--------
沿用原设计的三层分工：

* **主线程**：``record()`` 只入队，O(1)，**绝不碰磁盘**；
* **写线程**：单个 daemon writer 批量提交；
* **读**：大区域回档走 ``stream_*`` 流式游标，用**独立只读连接**
  + WAL 快照隔离，内存占用 O(单批) 而不是 O(区域)。

绝不做的事
----------
本层不使用任何 Endstone API，也不 import endstone ——
它可以在纯 Python 环境里被单元测试直接跑起来。
"""

from __future__ import annotations

import gzip
import json
import lzma
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

# 字段语义：break/leaves_decay/block_grow/block_form/explode 记录变化前方块；
# place/liquid_flow 记录变化后方块，replaced_* 为变化前方块
SCHEMA = """
CREATE TABLE IF NOT EXISTS block_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    block_type      TEXT NOT NULL,
    block_states    TEXT NOT NULL DEFAULT '{}',
    replaced_type   TEXT,
    replaced_states TEXT,
    x               INTEGER NOT NULL,
    y               INTEGER NOT NULL,
    z               INTEGER NOT NULL,
    dimension       TEXT NOT NULL,
    rolled_back     INTEGER NOT NULL DEFAULT 0,
    extra           TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_pos   ON block_log(x, y, z, ts);
CREATE INDEX IF NOT EXISTS idx_log_rb    ON block_log(rolled_back);
CREATE INDEX IF NOT EXISTS idx_log_ts    ON block_log(ts);
CREATE INDEX IF NOT EXISTS idx_log_dim  ON block_log(dimension);
CREATE INDEX IF NOT EXISTS idx_log_actor ON block_log(actor);

CREATE TABLE IF NOT EXISTS rollback_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    player      TEXT NOT NULL,
    dimension   TEXT NOT NULL,
    x1 INTEGER NOT NULL, y1 INTEGER NOT NULL, z1 INTEGER NOT NULL,
    x2 INTEGER NOT NULL, y2 INTEGER NOT NULL, z2 INTEGER NOT NULL,
    seconds     REAL NOT NULL,
    block_count INTEGER NOT NULL DEFAULT 0,
    undone      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS undo_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL,
    x INTEGER NOT NULL, y INTEGER NOT NULL, z INTEGER NOT NULL,
    dimension    TEXT NOT NULL,
    block_type   TEXT NOT NULL,
    block_states TEXT NOT NULL DEFAULT '{}',
    extra        TEXT
);
CREATE INDEX IF NOT EXISTS idx_undo_session ON undo_log(session_id);

CREATE TABLE IF NOT EXISTS session_logs (
    session_id INTEGER NOT NULL,
    log_id     INTEGER NOT NULL,
    PRIMARY KEY (session_id, log_id)
);

CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    x           INTEGER NOT NULL,
    y           INTEGER NOT NULL,
    z           INTEGER NOT NULL,
    dimension   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evt_ts    ON event_log(ts);
CREATE INDEX IF NOT EXISTS idx_evt_actor ON event_log(actor);
CREATE INDEX IF NOT EXISTS idx_evt_action ON event_log(action);
-- 掉落物指纹查询：action+维度+时间窗前缀命中，避免大表全扫
CREATE INDEX IF NOT EXISTS idx_evt_drop ON event_log(action, dimension, ts);
"""

ROLLBACK_ACTIONS = (
    "break", "place", "explode",
    "leaves_decay", "block_grow", "block_form", "liquid_flow",
)

WATER_TYPES = ("minecraft:water", "minecraft:flowing_water")
LAVA_TYPES = ("minecraft:lava", "minecraft:flowing_lava")

# 单条日志回档后该坐标将变成的方块类型：place/liquid_flow 恢复 replaced_*（无则空气）
_TARGET_EXPR = (
    "CASE WHEN action IN ('place','liquid_flow') "
    "THEN COALESCE(replaced_type, 'minecraft:air') ELSE block_type END"
)

_STATS_TTL = 30.0


def _region_params(pos1: tuple, pos2: tuple) -> tuple:
    x1, x2 = sorted((pos1[0], pos2[0]))
    y1, y2 = sorted((pos1[1], pos2[1]))
    z1, z2 = sorted((pos1[2], pos2[2]))
    return (x1, x2, y1, y2, z1, z2)


def _filter_clause(
    exclude_blocks: Optional[set],
    only_blocks: Optional[set],
    rollback_water: bool,
    rollback_lava: bool,
) -> tuple[str, list[Any]]:
    """构建方块过滤 WHERE 片段；过滤依据是回档后的目标方块类型。"""
    clauses: list[str] = []
    params: list[Any] = []
    if exclude_blocks:
        ph = ",".join("?" * len(exclude_blocks))
        clauses.append(f"{_TARGET_EXPR} NOT IN ({ph})")
        params.extend(sorted(exclude_blocks))
    if only_blocks:
        ph = ",".join("?" * len(only_blocks))
        clauses.append(f"{_TARGET_EXPR} IN ({ph})")
        params.extend(sorted(only_blocks))
    if not rollback_water:
        clauses.append(f"{_TARGET_EXPR} NOT IN (?, ?)")
        params.extend(WATER_TYPES)
    if not rollback_lava:
        clauses.append(f"{_TARGET_EXPR} NOT IN (?, ?)")
        params.extend(LAVA_TYPES)
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


class RegionStore:
    #: 库文件名。放在行为记录系统自己的子目录里，避免与别的既定文件名撞车。
    DB_NAME = "blocks.db"

    def __init__(self, data_dir, logger=None):
        self._lock = threading.RLock()
        self._logger = logger
        self._data_folder = Path(data_dir)
        try:
            self._data_folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._db_path = str(self._data_folder / self.DB_NAME)
        # 定时任务/命令回调可能运行在非主线程，连接须允许跨线程使用
        self.conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._stats_cache: Optional[tuple[float, dict[str, Any]]] = None
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def _open_read_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        return conn

    @staticmethod
    def _close_conn(cur: Optional[sqlite3.Cursor],
                    conn: Optional[sqlite3.Connection]) -> None:
        for closer in (cur, conn):
            if closer is None:
                continue
            try:
                closer.close()
            except sqlite3.Error:
                pass

    def stream_region(
        self,
        pos1: tuple, pos2: tuple, dimension: str,
        since_ts: float,
        exclude_blocks: Optional[set] = None,
        only_blocks: Optional[set] = None,
        rollback_water: bool = True,
        rollback_lava: bool = True,
        limit: int = 0,
    ) -> tuple[sqlite3.Connection, sqlite3.Cursor, int]:
        """流式查询区域+时间窗内未回档的候选记录，返回 (连接, 游标, 命中总数)。

        游标按 ts ASC, id ASC 返回：窗内最早一条记录的"变化前方块"
        即该坐标在窗口起点的状态，正序是回档正确性的关键
        （例：先放置后挖掉，回档应恢复为空气）。
        同一连接内 COUNT 与游标共享读事务快照，结果严格一致。
        """
        x1, x2, y1, y2, z1, z2 = _region_params(pos1, pos2)
        actions = ",".join("?" * len(ROLLBACK_ACTIONS))
        filt, fparams = _filter_clause(
            exclude_blocks, only_blocks, rollback_water, rollback_lava
        )
        where = (
            "x BETWEEN ? AND ? AND y BETWEEN ? AND ? AND z BETWEEN ? AND ?"
            f" AND dimension=? AND ts>=? AND rolled_back=0"
            f" AND action IN ({actions})"
        )
        base_params: list[Any] = [x1, x2, y1, y2, z1, z2, dimension, since_ts]
        base_params.extend(ROLLBACK_ACTIONS)
        inner_sql = f"SELECT * FROM block_log WHERE {where}{filt}"
        limit_sql = f" LIMIT {int(limit)}" if limit and limit > 0 else ""

        conn = self._open_read_conn()
        try:
            conn.execute("BEGIN")
            count = conn.execute(
                f"SELECT COUNT(*) c FROM ({inner_sql})", [*base_params, *fparams]
            ).fetchone()["c"]
            cur = conn.execute(inner_sql + limit_sql, [*base_params, *fparams])
        except sqlite3.Error:
            self._close_conn(None, conn)
            raise
        return conn, cur, int(count)

    def stream_undo_rows(
        self, session_id: int,
    ) -> tuple[sqlite3.Connection, sqlite3.Cursor, int]:
        """流式读取一次回档会话的撤销快照，返回 (连接, 游标, 总数)。"""
        conn = self._open_read_conn()
        try:
            conn.execute("BEGIN")
            count = conn.execute(
                "SELECT COUNT(*) c FROM undo_log WHERE session_id=?", (session_id,)
            ).fetchone()["c"]
            cur = conn.execute(
                "SELECT * FROM undo_log WHERE session_id=? ORDER BY id ASC",
                (session_id,),
            )
        except sqlite3.Error:
            self._close_conn(None, conn)
            raise
        return conn, cur, int(count)

    def stream_drop_snapshots(
        self, pos1: tuple, pos2: tuple, dimension: str,
        since_ts: float, limit: int = 4000,
    ) -> tuple[sqlite3.Connection, sqlite3.Cursor]:
        """流式查询区域+时间窗内的掉落物指纹（drop_snapshot 记录）。

        detail 为 JSON 数组 [{"id","n","x","y","z"}, ...]，由回档引擎
        分批解析后清理残留掉落物。
        """
        x1, x2, y1, y2, z1, z2 = _region_params(pos1, pos2)
        conn = self._open_read_conn()
        try:
            conn.execute("BEGIN")
            cur = conn.execute(
                """
                SELECT detail FROM event_log
                WHERE action='drop_snapshot' AND dimension=? AND ts>=?
                  AND x BETWEEN ? AND ? AND y BETWEEN ? AND ? AND z BETWEEN ? AND ?
                ORDER BY ts ASC
                LIMIT ?
                """,
                (dimension, since_ts, x1, x2, y1, y2, z1, z2, limit),
            )
        except sqlite3.Error:
            self._close_conn(None, conn)
            raise
        return conn, cur

    def insert_records(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        with self._lock:
            self.conn.executemany(
                """
                INSERT INTO block_log
                    (ts, actor, action, block_type, block_states,
                     replaced_type, replaced_states, x, y, z, dimension,
                     rolled_back, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                [
                    (
                        r["ts"], r["actor"], r["action"], r["block_type"],
                        r["block_states"], r.get("replaced_type"), r.get("replaced_states"),
                        r["x"], r["y"], r["z"], r["dimension"], r.get("extra"),
                    )
                    for r in records
                ],
            )
            self.conn.commit()
            # stats() 读这三张表，写入后让 TTL 缓存失效
            self._stats_cache = None

    def insert_event_records(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        with self._lock:
            self.conn.executemany(
                """
                INSERT INTO event_log
                    (ts, actor, action, detail, x, y, z, dimension)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r["ts"], r["actor"], r["action"], r.get("detail", ""),
                        r["x"], r["y"], r["z"], r["dimension"],
                    )
                    for r in records
                ],
            )
            self.conn.commit()
            self._stats_cache = None

    def create_session(
        self, player: str, dimension: str,
        pos1: tuple, pos2: tuple, seconds: float,
    ) -> int:
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO rollback_sessions
                    (ts, player, dimension, x1, y1, z1, x2, y2, z2, seconds, block_count, undone)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (time.time(), player, dimension, *pos1, *pos2, seconds),
            )
            self.conn.commit()
            self._stats_cache = None
            return int(cur.lastrowid)

    def insert_session_logs(self, session_id: int, log_ids: list[int]) -> None:
        """按批写入会话-日志关联：崩溃/中断时已写部分仍可撤销。"""
        if not log_ids:
            return
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO session_logs (session_id, log_id) VALUES (?, ?)",
                [(session_id, i) for i in log_ids],
            )
            self.conn.commit()

    def insert_undo_rows(self, rows: list[tuple]) -> None:
        """按批写入撤销快照行：(session_id, x, y, z, dimension, block_type, block_states)。"""
        if not rows:
            return
        with self._lock:
            self.conn.executemany(
                """
                INSERT INTO undo_log
                    (session_id, x, y, z, dimension, block_type, block_states)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self.conn.commit()

    def finalize_session(self, session_id: int, applied: int) -> None:
        """回档收尾：给关联日志打回档标记并更新会话方块数（原子）。"""
        with self._lock:
            self.conn.execute(
                """
                UPDATE block_log SET rolled_back=1
                WHERE id IN (SELECT log_id FROM session_logs WHERE session_id=?)
                """,
                (session_id,),
            )
            self.conn.execute(
                "UPDATE rollback_sessions SET block_count=? WHERE id=?",
                (applied, session_id),
            )
            self.conn.commit()
            self._stats_cache = None

    def finalize_undo(self, session_id: int) -> None:
        """撤销收尾：日志恢复未回档、会话标记已撤销（原子）。"""
        with self._lock:
            self.conn.execute(
                """
                UPDATE block_log SET rolled_back=0
                WHERE id IN (SELECT log_id FROM session_logs WHERE session_id=?)
                """,
                (session_id,),
            )
            self.conn.execute(
                "UPDATE rollback_sessions SET undone=1 WHERE id=?", (session_id,)
            )
            self.conn.commit()
            self._stats_cache = None

    def query_position(
        self, x: int, y: int, z: int, dimension: str, limit: int = 20,
    ) -> list[sqlite3.Row]:
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT * FROM block_log
                WHERE x=? AND y=? AND z=? AND dimension=?
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                (x, y, z, dimension, limit),
            )
            return cur.fetchall()

    def _page_filters(
        actions: list[str], since_ts: float, actor: Optional[str],
    ) -> tuple[str, list[Any]]:
        ph = ",".join("?" * len(actions))
        where = f"action IN ({ph}) AND ts>=?"
        params: list[Any] = [*actions, since_ts]
        if actor:
            where += " AND actor=?"
            params.append(actor)
        return where, params

    def query_block_log(
        self, actions: list[str], since_ts: float, actor: Optional[str],
        page: int, page_size: int,
    ) -> tuple[list[sqlite3.Row], int]:
        where, params = self._page_filters(actions, since_ts, actor)
        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) c FROM block_log WHERE {where}", params
            ).fetchone()["c"]
            rows = self.conn.execute(
                f"SELECT * FROM block_log WHERE {where} "
                "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return rows, total

    def query_events(
        self, actions: list[str], since_ts: float, actor: Optional[str],
        page: int, page_size: int,
    ) -> tuple[list[sqlite3.Row], int]:
        where, params = self._page_filters(actions, since_ts, actor)
        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) c FROM event_log WHERE {where}", params
            ).fetchone()["c"]
            rows = self.conn.execute(
                f"SELECT * FROM event_log WHERE {where} "
                "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return rows, total

    def query_region_block_log(
        self, pos1: tuple, pos2: tuple, dimension: str,
        actions: list[str], since_ts: float, page: int, page_size: int,
    ) -> tuple[list[sqlite3.Row], int]:
        x1, x2, y1, y2, z1, z2 = _region_params(pos1, pos2)
        ph = ",".join("?" * len(actions))
        where = (
            "x BETWEEN ? AND ? AND y BETWEEN ? AND ? AND z BETWEEN ? AND ?"
            f" AND dimension=? AND action IN ({ph}) AND ts>=?"
        )
        params: list[Any] = [x1, x2, y1, y2, z1, z2, dimension, *actions, since_ts]
        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) c FROM block_log WHERE {where}", params
            ).fetchone()["c"]
            rows = self.conn.execute(
                f"SELECT * FROM block_log WHERE {where} "
                "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return rows, total

    def get_last_session(self, player: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM rollback_sessions WHERE player=? AND undone=0 "
                "ORDER BY id DESC LIMIT 1",
                (player,),
            )
            return cur.fetchone()

    def get_last_session_any(self) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM rollback_sessions WHERE undone=0 "
                "ORDER BY id DESC LIMIT 1"
            )
            return cur.fetchone()

    def get_session(self, session_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM rollback_sessions WHERE id=?", (session_id,)
            )
            return cur.fetchone()

    def list_sessions(
        self, player: Optional[str] = None, limit: int = 15,
    ) -> list[sqlite3.Row]:
        with self._lock:
            if player:
                cur = self.conn.execute(
                    "SELECT * FROM rollback_sessions WHERE player=? "
                    "ORDER BY id DESC LIMIT ?",
                    (player, limit),
                )
            else:
                cur = self.conn.execute(
                    "SELECT * FROM rollback_sessions ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            return cur.fetchall()

    def purge(self, days: int) -> dict[str, int]:
        """清理 N 天前的数据库记录（撤销快照随会话一并清理，防孤儿数据）。"""
        cutoff = time.time() - days * 86400
        with self._lock:
            cur1 = self.conn.execute(
                "DELETE FROM block_log WHERE ts<?", (cutoff,)
            )
            cur3 = self.conn.execute(
                "DELETE FROM event_log WHERE ts<?", (cutoff,)
            )
            self.conn.execute(
                """
                DELETE FROM session_logs WHERE session_id IN
                    (SELECT id FROM rollback_sessions WHERE ts<?)
                """,
                (cutoff,),
            )
            self.conn.execute(
                "DELETE FROM undo_log WHERE session_id IN "
                "(SELECT id FROM rollback_sessions WHERE ts<?)",
                (cutoff,),
            )
            cur2 = self.conn.execute(
                "DELETE FROM rollback_sessions WHERE ts<?", (cutoff,)
            )
            self.conn.commit()
            self._stats_cache = None
        return {
            "logs": cur1.rowcount,
            "sessions": cur2.rowcount,
            "events": cur3.rowcount,
        }

    def stats(self) -> dict[str, Any]:
        """数据库统计：两次 GROUP BY 聚合 + 小表计数，带 30 秒 TTL 缓存。"""
        now = time.monotonic()
        if self._stats_cache is not None and now - self._stats_cache[0] < _STATS_TTL:
            return self._stats_cache[1]
        with self._lock:
            total = 0
            rolled = 0
            oldest: Optional[float] = None
            per_action: dict[str, int] = {}
            for r in self.conn.execute(
                "SELECT action, COUNT(*) c, SUM(rolled_back) rb, MIN(ts) mn "
                "FROM block_log GROUP BY action"
            ).fetchall():
                total += r["c"]
                rolled += r["rb"] or 0
                mn = r["mn"]
                if mn is not None and (oldest is None or mn < oldest):
                    oldest = mn
                per_action[r["action"]] = r["c"]
            events = 0
            for r in self.conn.execute(
                # drop_snapshot 是内部掉落物指纹记录，不属于用户可见分类
                "SELECT action, COUNT(*) c FROM event_log "
                "WHERE action!='drop_snapshot' GROUP BY action"
            ).fetchall():
                events += r["c"]
                per_action[r["action"]] = per_action.get(r["action"], 0) + r["c"]
            sessions = self.conn.execute(
                "SELECT COUNT(*) c FROM rollback_sessions"
            ).fetchone()["c"]
        result = {
            "total": total,
            "events": events,
            "rolled_back": rolled,
            "sessions": sessions,
            "per_action": per_action,
            "oldest_ts": oldest,
            # 库文件实际占用。面板要展示"这个数据花了多少磁盘"，
            # 原来这份信息在 _cmd_db_status 里临时读一次，Web 化之后
            # 状态接口需要它，就并到这里一起返回。
            "size_bytes": self.db_file_size(),
        }
        self._stats_cache = (now, result)
        return result

    #: 关闭标记。见 ``close()`` —— 重复 close 必须安全。
    _closed = False

    def close(self) -> None:
        """关闭主连接。**幂等**：重复调用原样返回，不报错。

        原实现没有幂等保护。移植后 ``close()`` 会被**多条路径**触发
        （插件禁用、配置文件热重载触发重建、数据库归档切换），
        第二次 ``self.conn.close()`` 之后再有查询会抛
        ``ProgrammingError: Cannot operate on a closed database``。
        而这些调用方大多处在 ``finally`` 里 —— 一抛就把真正的
        收尾逻辑挤掉了，症状是"关服时数据没落干净"。

        加标记而不是 try/except 包住每一次 close：后者会把"真的
        关失败"（WAL 没 checkpoint 完）也一起吞掉。
        """
        if self._closed:
            return
        with self._lock:
            if self._closed:
                return
            try:
                self.conn.commit()
            except Exception:
                pass
            try:
                self.conn.close()
            except Exception:
                pass
            self._closed = True

    # 数据库归档/轮换：现行库被回档反复回写不能按天分文件，
    # 归档库从轮换起不再修改，压缩安全

    @property
    def archive_dir(self) -> Path:
        return self._data_folder / "db_archive"

    def _reopen(self) -> None:
        self.conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._stats_cache = None

    def _checkpoint_close(self) -> None:
        """提交 + WAL 折叠回主文件 + 关闭（切换库文件前必做）。"""
        try:
            self.conn.commit()
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.conn.close()
        self._stats_cache = None

    def _next_archive_path(self) -> Path:
        adir = self.archive_dir
        adir.mkdir(parents=True, exist_ok=True)
        stem = f"mxback_{time.strftime('%Y%m%d_%H%M%S')}"
        base = stem
        n = 1
        # 撞名检查须覆盖所有形态：同一秒内归档->压缩->再归档时
        # 只查 .db 会漏检，导致换出库与被恢复归档同名互相覆盖
        while self._base_taken(adir, base):
            base = f"{stem}_{n}"
            n += 1
        return adir / f"{base}.db"

    @staticmethod
    def _base_taken(adir: Path, base: str) -> bool:
        return any(
            (adir / f"{base}{suffix}").exists()
            for suffix in (
                ".db", ".db-wal", ".db-shm",
                ".db.gz", ".db.xz", ".db.json",
            )
        )

    def _snapshot_summary(self) -> dict:
        info = dict(self.stats())
        info.pop("per_action", None)
        try:
            info["size_bytes"] = Path(self._db_path).stat().st_size
        except OSError:
            info["size_bytes"] = 0
        info["archived_at"] = time.time()
        return info

    def _write_sidecar(self, base: str, info: dict) -> None:
        try:
            (self.archive_dir / f"{base}.db.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _read_sidecar(self, base: str) -> Optional[dict]:
        try:
            data = json.loads(
                (self.archive_dir / f"{base}.db.json").read_text(
                    encoding="utf-8"
                )
            )
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def update_sidecar(self, base: str, patch: dict) -> None:
        info = self._read_sidecar(base) or {}
        info.update(patch)
        self._write_sidecar(base, info)

    @staticmethod
    def _move_with_wal(src: Path, dest: Path) -> None:
        # 残留 -wal/-shm 一并搬走，极端情况不丢数据
        src.rename(dest)
        for ext in ("-wal", "-shm"):
            side = src.with_name(src.name + ext)
            if side.exists():
                try:
                    side.rename(dest.with_name(dest.name + ext))
                except OSError:
                    pass

    def archive_current(self) -> tuple[Path, dict]:
        """归档当前数据库并新建空库。前置：写队列已排空、无回档进行中。

        全程持 _lock 与后台写线程互斥，期间新入队的写入落进新库。
        """
        with self._lock:
            info = self._snapshot_summary()
            self._checkpoint_close()
            dest = self._next_archive_path()
            try:
                self._move_with_wal(Path(self._db_path), dest)
            except OSError:
                # 搬移失败：旧库仍在原位，重开即恢复原样
                self._reopen()
                raise
            self._reopen()
        base = dest.name[: -len(".db")]
        info["name"] = dest.name
        self._write_sidecar(base, info)
        return dest, info

    def list_archives(self) -> list[dict]:
        """列出归档库（新->旧）。每项：name/base/size/mtime/compressed/info。"""
        adir = self.archive_dir
        if not adir.is_dir():
            return []
        out: list[dict] = []
        for p in adir.iterdir():
            name = p.name
            if name.endswith(".json") or not p.is_file():
                continue
            if name.endswith(".db"):
                base, comp = name[:-3], ""
            elif name.endswith(".db.gz"):
                base, comp = name[:-6], "gzip"
            elif name.endswith(".db.xz"):
                base, comp = name[:-7], "xz"
            else:
                continue
            if not base.startswith("mxback_"):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "name": name,
                "base": base,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "compressed": comp,
                "info": self._read_sidecar(base),
            })
        out.sort(key=lambda e: e["mtime"], reverse=True)
        return out

    def find_archive(self, key: str) -> Optional[dict]:
        """按文件名或列表序号（0 起，新->旧）查找归档。"""
        for i, e in enumerate(self.list_archives()):
            if e["name"] == key or e["base"] == key or str(i) == key:
                return e
        return None

    def prepare_restore(self, entry: dict) -> Path:
        """恢复第 1 步（后台线程，不碰现行库）：归档解压/搬移为临时文件，
        move 的原子性防止并发重复操作同一归档。"""
        src = self.archive_dir / entry["name"]
        tmp = self._data_folder / ".mxback_restore_tmp"
        comp = entry["compressed"]
        if comp == "gzip":
            with gzip.open(src, "rb") as s, open(tmp, "wb") as d:
                shutil.copyfileobj(s, d, 1 << 20)
        elif comp == "xz":
            with lzma.open(src, "rb") as s, open(tmp, "wb") as d:
                shutil.copyfileobj(s, d, 1 << 20)
        else:
            shutil.move(str(src), str(tmp))
        return tmp

    def commit_restore(self, tmp: Path, entry: dict) -> dict:
        """恢复第 2 步（持 _lock，纯改名秒级完成）：当前库转归档 ->
        临时文件顶上成为现行库 -> 重开连接。返回被换出的旧库信息。"""
        with self._lock:
            info = self._snapshot_summary()
            info["restored_from"] = entry["name"]
            self._checkpoint_close()
            dest = self._next_archive_path()
            live = Path(self._db_path)
            try:
                live.rename(dest)
                tmp.rename(live)
            except OSError:
                # 绝不让现行库缺位
                try:
                    if not live.exists() and dest.exists():
                        dest.rename(live)
                except OSError:
                    pass
                self._reopen()
                raise
            self._reopen()
        base = dest.name[: -len(".db")]
        info["name"] = dest.name
        self._write_sidecar(base, info)
        # 原归档已成为现行库：删除其归档文件与 sidecar
        if entry["compressed"]:
            try:
                (self.archive_dir / entry["name"]).unlink()
            except OSError:
                pass
        try:
            (self.archive_dir / f"{entry['base']}.db.json").unlink()
        except OSError:
            pass
        return info

    # ------------------------------------------------------------------
    # WebUI / AI 专用查询（原插件里没有 —— 它走游戏内表单分页）
    #
    # ★ 为什么要专门加这两个，而不是复用 query_block_log：
    #   原来给表单用的查询返回一个 py sqlite3.Row + "总共多少条"，
    #   前端拿着 Row 直接渲染。而 Web / AI 要的是**纯 dict**（能 JSON
    #   序列化）、自带人类可读时间、且统计维度更丰富（谁动得最多、
    #   哪类方块最常见）。在调用方再做一层转换会把这个逻辑复制三份。
    # ------------------------------------------------------------------
    def recent_block_log(self, *, actor: str = "", action: str = "",
                         dimension: str = "", limit: int = 50
                         ) -> "list[dict[str, Any]]":
        """最近的方块变更流水，返回纯 dict + 已格式化的时间。"""
        from datetime import datetime as _dt
        clauses: "list[str]" = []
        params: "list[Any]" = []
        if actor:
            clauses.append("actor=?")
            params.append(str(actor))
        if action:
            clauses.append("action=?")
            params.append(str(action))
        if dimension:
            clauses.append("dimension=?")
            params.append(str(dimension))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (f"SELECT * FROM block_log{where} "
               "ORDER BY ts DESC, id DESC LIMIT ?")
        with self._lock:
            rows = self.conn.execute(sql, [*params, int(limit)]).fetchall()
        out: "list[dict[str, Any]]" = []
        for r in rows:
            d = dict(r)
            try:
                d["time"] = _dt.fromtimestamp(d["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                d["time"] = ""
            out.append(d)
        return out

    def recent_events(self, *, actor: str = "", action: str = "",
                      dimension: str = "", limit: int = 50
                      ) -> "list[dict[str, Any]]":
        """最近的世界事件流水。内部指纹记录（drop_snapshot）不对外。"""
        from datetime import datetime as _dt
        clauses: "list[str]" = ["action!='drop_snapshot'"]
        params: "list[Any]" = []
        if actor:
            clauses.append("actor=?")
            params.append(str(actor))
        if action:
            clauses.append("action=?")
            params.append(str(action))
        if dimension:
            clauses.append("dimension=?")
            params.append(str(dimension))
        where = " WHERE " + " AND ".join(clauses)
        sql = f"SELECT * FROM event_log{where} ORDER BY ts DESC, id DESC LIMIT ?"
        with self._lock:
            rows = self.conn.execute(sql, [*params, int(limit)]).fetchall()
        out: "list[dict[str, Any]]" = []
        for r in rows:
            d = dict(r)
            try:
                d["time"] = _dt.fromtimestamp(d["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                d["time"] = ""
            out.append(d)
        return out

    def preview_region(self, pos1: tuple, pos2: tuple, dimension: str,
                       since_ts: float, limit: int = 5000) -> "dict[str, Any]":
        """回档影响面统计：**只读**，不碰 world。

        返回的东西是给人（和 AI）在按下确认前看的：
          * ``records_hit``      命中多少条记录
          * ``by_action``        按动作分类
          * ``by_actor``         主要涉及哪些人（取前 10）
          * ``top_blocks``       最常出现的方块类型（取前 10）
          * ``truncated``        是否因为超限被截断 —— 必须让调用方知道，
                                否则他会以为预览的就是全部，从而低估影响。
        """
        from .store import _region_params
        x1, x2, y1, y2, z1, z2 = _region_params(pos1, pos2)
        base: "list[Any]" = [x1, x2, y1, y2, z1, z2, dimension, since_ts]
        where = (
            "x BETWEEN ? AND ? AND y BETWEEN ? AND ? AND z BETWEEN ? AND ?"
            " AND dimension=? AND ts>=? AND rolled_back=0"
        )
        out: "dict[str, Any]" = {
            "records_hit": 0, "truncated": False, "by_action": {},
            "by_actor": {}, "top_blocks": [],
        }
        with self._lock:
            try:
                total = self.conn.execute(
                    f"SELECT COUNT(*) c FROM block_log WHERE {where}",
                    base).fetchone()["c"]
            except Exception:
                return out
            out["records_hit"] = int(total)
            out["truncated"] = bool(limit and total > int(limit))

            for r in self.conn.execute(
                f"SELECT action, COUNT(*) c FROM block_log WHERE {where} "
                "GROUP BY action ORDER BY c DESC", base
            ).fetchall():
                out["by_action"][r["action"]] = int(r["c"])

            for r in self.conn.execute(
                f"SELECT actor, COUNT(*) c FROM block_log WHERE {where} "
                "GROUP BY actor ORDER BY c DESC LIMIT 10", base
            ).fetchall():
                out["by_actor"][r["actor"]] = int(r["c"])

            blocks: "list[list]" = []
            for r in self.conn.execute(
                f"SELECT block_type, COUNT(*) c FROM block_log WHERE {where} "
                "GROUP BY block_type ORDER BY c DESC LIMIT 10", base
            ).fetchall():
                blocks.append([r["block_type"], int(r["c"])])
            out["top_blocks"] = blocks
        return out

    def db_file_size(self) -> int:
        try:
            return Path(self._db_path).stat().st_size
        except OSError:
            return 0


def compress_archive(path: Path, algorithm: str = "gzip") -> Path:
    """压缩归档库文件（后台线程），原地替换为 .db.gz / .db.xz。"""
    if algorithm == "xz":
        suffix = ".xz"

        def opener(w):
            return lzma.open(w, "wb", format=lzma.FORMAT_XZ, preset=6)
    else:
        algorithm = "gzip"
        suffix = ".gz"

        def opener(w):
            return gzip.open(w, "wb", compresslevel=6)

    final = path.with_name(path.name + suffix)
    tmp = final.with_name(final.name + ".part")
    try:
        with open(path, "rb") as src, opener(tmp) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        tmp.replace(final)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    path.unlink()
    return final


def states_to_json(states: Any) -> str:
    if not states:
        return "{}"
    try:
        return json.dumps(dict(states), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


def json_to_states(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
