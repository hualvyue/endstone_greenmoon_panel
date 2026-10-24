"""撤销窗口：第三级「完全放开」的唯一安全网。

背景
====
用户选择了「放开到不可逆」并接受「立即执行，窗口内可回滚」。这意味着
AI 在第三级下会真的删档、真的改 server.properties —— 不等服主点确认。
那么"万一判断错了"这个必须被回答的问题，答案就是**这个模块**。

设计上的三个决定
================

**1. 立刻执行，而不是延迟执行**
    曾经考虑过「先执行、30 秒后才真生效」的做法（把动作排队）。否决原因：
      · 延迟执行会让 AI 的下一轮推理基于**还没发生**的状态，它会困惑；
      · 排队期间服务器状态可能变化，让原本正确的操作变得错误；
      · 服主看到的是「AI 说删了但东西还在」，比错误更难理解。
    所以是：真做 → 留下能还原的素材 → 给 30 秒后悔。

**2. 回滚素材由工具自己在执行时提供**
    ``delete_player_data`` 删之前已经写了 ``.deleted_bak``；
    ``clear_whitelist_entry`` 手上有被删的完整记录；
    ``set_server_property`` 改之前能读到旧值。
    这些素材在**执行的同一瞬间**最准确 —— 事后再去找，
    可能文件已被覆盖、记录已被清理。所以要求执行者顺手交出来，
    而不是回滚时再反查。

**3. 回滚可以是"做不到"，但要如实说**
    ``delete_player_data`` 的 ``.deleted_bak`` 是尽力而为写下的
    （写失败会被 except 吞掉）。回滚时如果发现备份不在，
    **必须明确返回失败原因**，而不是静默成功让服主以为救回来了。
    一个假的好消息比一个真的坏消息危险得多。

过期后的记录怎么办
==================
**不删除**，标记为 ``expired`` 保留。理由与 ``runner.list_proposals``
一致：服主事后想复盘「AI 那天删了什么、我当时有没有机会拦住」——
直接删掉等于销毁证据。带 ``include_done=True`` 就能看到全部。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .autonomy import DEFAULT_UNDO_WINDOW

__all__ = ["UndoRecord", "UndoRegistry"]

#: 保留的撤销记录上限。内存队列必须封顶。
_MAX_RECORDS = 200


class UndoRecord:
    """一次不可逆操作的「后悔药」。

    ``undo_payload`` 是各工具自定义的回滚素材 —— 本模块不解释它的内容，
    只负责保管与到期回收。解释权在工具手里（见 runner 的 ``_UNDO_*``）。
    """

    __slots__ = ("rid", "ts", "tool", "args", "risk", "actor", "summary",
                 "undo_payload", "undoable", "undo_why", "deadline",
                 "undone", "undone_at", "undo_note", "notified")

    def __init__(self, rid: str, tool: str, args: Dict[str, Any], *,
                 risk: str, actor: str, summary: str,
                 undo_payload: Any = None, undoable: bool = True,
                 undo_why: str = "",
                 window: float = DEFAULT_UNDO_WINDOW,
                 now: Optional[float] = None) -> None:
        self.rid = str(rid)
        self.ts = float(now if now is not None else time.time())
        self.tool = str(tool)
        self.args = dict(args or {})
        self.risk = str(risk)
        self.actor = str(actor)
        self.summary = str(summary)
        self.undo_payload = undo_payload
        #: 是否**具备**回滚能力。False 时窗口仍然存在（服主能看到这条
        #: 记录并知道发生了什么），但回滚按钮会被禁用并说明原因。
        self.undoable = bool(undoable)
        self.undo_why = str(undo_why or "")
        self.deadline = self.ts + max(0.0, float(window))
        self.undone = False
        self.undone_at: Optional[float] = None
        self.undo_note = ""
        #: 是否已经向群里发过告警。防止重复执行时发两遍。
        self.notified = False

    # ---- 状态查询 ----
    def remaining(self, now: Optional[float] = None) -> float:
        """距窗口关闭还剩多少秒。负数表示已过期。"""
        n = float(now if now is not None else time.time())
        return self.deadline - n

    def expired(self, now: Optional[float] = None) -> bool:
        return self.remaining(now) <= 0

    def status(self, now: Optional[float] = None) -> str:
        if self.undone:
            return "undone"
        if self.expired(now):
            return "expired"
        return "undoable" if self.undoable else "no_path_back"

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        n = float(now if now is not None else time.time())
        return {
            "id": self.rid,
            "ts": self.ts,
            "tool": self.tool,
            "args": self.args,
            "risk": self.risk,
            "actor": self.actor,
            "summary": self.summary,
            "status": self.status(n),
            "undoable": self.undoable and not self.undone and not self.expired(n),
            "undo_why": self.undo_why,
            "remaining": max(0.0, round(self.remaining(n), 1)),
            "window": round(self.deadline - self.ts, 1),
            "undone_at": self.undone_at,
            "undo_note": self.undo_note,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(self.ts)),
        }


class UndoRegistry:
    """撤销记录的保管箱。

    与 ``ToolRunner`` 的关系：runner 执行完不可逆工具后把素材交到这里，
    这里只做登记 / 查询 / 标记。**真正的回滚动作在 runner 里** ——
    因为那需要用到 runner 的插件句柄与工具分发表。本类保持纯数据，
    可以独立测试。
    """

    def __init__(self, *, window: float = DEFAULT_UNDO_WINDOW,
                 logger: Any = None) -> None:
        self._lock = threading.RLock()
        self._records: List[UndoRecord] = []
        self._seq = 0
        self._window = float(window)
        self.logger = logger

    # ---- 配置 ----
    def window(self) -> float:
        return self._window

    def set_window(self, seconds: Any) -> float:
        """窗口可调（面板上配）。对**已登记**的记录不改动 ——
        改配置不该让一条正在倒计时的记录突然过期或复活。

        ★ 下限 1 秒：0 秒窗口等于「登记即过期」，撤销能力静默失效。
          面板清空输入框会存成 0，必须在这里挡住。
        """
        try:
            v = float(seconds)
        except (TypeError, ValueError):
            return self._window
        # 1 ~ 600 秒。上限 10 分钟：再长就不是"后悔药"而是"延迟执行"了，
        # 服主会忘了自己还挂着一条待回滚记录。
        self._window = max(1.0, min(v, 600.0))
        return self._window

    # ---- 登记 ----
    def register(self, tool: str, args: Dict[str, Any], *,
                 risk: str, actor: str, summary: str,
                 undo_payload: Any = None, undoable: bool = True,
                 undo_why: str = "", window: Optional[float] = None,
                 ) -> UndoRecord:
        with self._lock:
            self._seq += 1
            rec = UndoRecord(
                f"u{self._seq}", tool, args, risk=risk, actor=actor,
                summary=summary, undo_payload=undo_payload,
                undoable=undoable, undo_why=undo_why,
                window=self._window if window is None else float(window),
            )
            self._records.append(rec)
            if len(self._records) > _MAX_RECORDS:
                del self._records[:len(self._records) - _MAX_RECORDS]
        return rec

    # ---- 查询 ----
    def get(self, rid: str) -> Optional[UndoRecord]:
        with self._lock:
            for r in self._records:
                if r.rid == str(rid):
                    return r
        return None

    def list(self, *, include_done: bool = False,
             limit: int = 50) -> List[Dict[str, Any]]:
        """列出撤销记录。默认只给「窗口内、还没撤销」的那些 ——
        那是服主此刻唯一还能操作的集合。"""
        now = time.time()
        out: List[Dict[str, Any]] = []
        with self._lock:
            for r in self._records:
                st = r.status(now)
                if not include_done and st != "undoable":
                    continue
                out.append(r.to_dict(now))
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out[:limit]

    def pending_count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for r in self._records if r.status(now) == "undoable")

    # ---- 标记 ----
    def mark_undone(self, rid: str, note: str = "") -> Tuple[bool, str]:
        with self._lock:
            r = self.get(rid)
            if r is None:
                return False, "撤销记录不存在"
            if r.undone:
                return False, "这条记录已经撤销过了"
            r.undone = True
            r.undone_at = time.time()
            r.undo_note = str(note or "")
        return True, "已撤销"

    def mark_notified(self, rid: str) -> bool:
        """标记「已向群里告知」。返回 True 表示本次是第一次。

        ★ 用返回值而不是调用方自己判断：多张卡同时播报同一件事
          是重复告警最常见的来源，把去重放在这里比放在调用方可靠。
        """
        with self._lock:
            r = self.get(rid)
            if r is None or r.notified:
                return False
            r.notified = True
            return True
