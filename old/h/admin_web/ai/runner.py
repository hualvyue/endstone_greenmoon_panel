"""工具执行层：把 ``tools.py`` 定义的工具接到**真实数据源**上。

在三层里的位置
==============
``tools.py``   有哪些工具、各自多危险（纯数据）
``gate.py``    一次提议在什么条件下才被放行（纯判定）
``runner.py``  **真正去读数据 / 真正去动手**（唯一有副作用的一层）

为什么执行层要单独一个文件、而不是塞进 manager
=============================================
因为它是唯一"会改变服务器状态"的地方。放在单独文件里意味着：
  · 想看"AI 到底能对服务器做什么"，只需要读这一个文件；
  · 想审计"哪里可能误伤玩家"，grep 这一个文件就够；
  · 将来要加工具，改动被**物理隔离**在这一层，不会污染
    配置管理、节流、提示词组装那些毫不相干的逻辑。

三类工具的执行策略完全不同
==========================
**只读（READ）** —— 直接查缓存/配置，立即返回。
    绝不做网络请求、绝不做重活。模型可能一次提十个查询，
    每个都去打网络就是十次等待。

**可逆（REVERSIBLE）** —— 真执行，但**必须记审计**。
    踢人、封禁这类能撤销的操作，过完闸门就执行。
    审计里带 ``actor="ai"``，出事时能分清是谁干的。

**不可逆（IRREVERSIBLE）** —— 默认**只提议**，人工确认后才执行。
    默认（接管级别 0/1）生成一条"待确认提议"（pending proposal），
    放进队列，等服主在面板上点确认。只有 ``confirm_proposal()``
    能走到执行 —— 而那个方法只从 web 层（管理员会话）调用。

    ★ v7.9 起多了一个入口：接管级别 2（完全放开）时，
      ``run_tool(allow_irreversible=True)`` 会**直接执行**不可逆工具，
      但**强制**同时登记一条撤销记录（``_undo``）—— 执行者必须提供
      回滚素材，否则这次执行在窗口内无法还原，会被如实标记为
      "无回滚路径"。这个入口只有在 ``manager`` 判定级别为 2 时才会传
      该参数，且每次调用都写审计。

    ★ 核心不变量仍然成立：**默认参数下**，``run_tool()`` 里不存在
      一条能让 IRREVERSIBLE 工具直接执行的路径。要放开必须显式传
      ``allow_irreversible=True``，那是一个会被 grep 出来的、显眼的开关。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .tools import RiskLevel, ToolSpec, get_tool
from .undo import UndoRegistry

try:
    # 用途常量定义在 connections.py（配置层的真源）。
    # 这里用 try 是因为本模块要能脱离整个插件单独 import 做测试 ——
    # connections.py 本身依赖很轻，正常情况一定能拿到；
    # 万一独立加载失败，用一个等值字面量兜底，功能不受影响。
    from ..connections import PURPOSE_BRIDGE
except Exception:  # pragma: no cover - 仅独立测试场景
    PURPOSE_BRIDGE = "bridge"

#: 单次工具返回给模型的文本上限。超了截断 —— 模型上下文很贵，
#: 而"最近 500 条日志"这种东西原样塞进去会瞬间打满窗口。
_MAX_RESULT_CHARS = 6000

#: 待确认提议的保留上限。这是内存队列，必须封顶。
_MAX_PROPOSALS = 100

#: 提议有效期（秒）。三天没人理就作废 —— 一个基于"三天前日志"
#: 做出的处置决定，三天后再执行可能是错的（人可能已经解释清楚了）。
_PROPOSAL_TTL = 72 * 3600


class ToolResult:
    """一次工具调用的结果。

    ``ok`` 之外还带 ``payload``（给模型看的数据）与 ``note``（给人看的说明）。
    分开的原因：模型需要结构化数据，而面板/日志需要一句话总结。
    混成一个字符串的后果是两边都不好用。
    """

    __slots__ = ("ok", "payload", "note", "elapsed")

    def __init__(self, ok: bool, payload: Any = None, note: str = "",
                 elapsed: float = 0.0) -> None:
        self.ok = ok
        self.payload = payload
        self.note = note
        self.elapsed = elapsed

    def as_text(self) -> str:
        """压成给模型看的文本（自动截断）。"""
        if isinstance(self.payload, str):
            body = self.payload
        else:
            try:
                body = json.dumps(self.payload, ensure_ascii=False, default=str)
            except Exception:
                body = str(self.payload)
        if not self.ok:
            return f"[失败] {self.note or body}"
        if len(body) > _MAX_RESULT_CHARS:
            body = body[:_MAX_RESULT_CHARS] + "…（已截断）"
        return body

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "note": self.note,
                "payload": self.payload, "elapsed": round(self.elapsed, 3)}


class ToolRunner:
    """工具的真实执行器。

    依赖注入（duck-typing）而非 import 插件类 —— 与 ``cloud.py``
    ``log_tailer.py`` 同一约定，本模块能脱离 Endstone 单独测试。
    """

    def __init__(self, plugin: Any) -> None:
        self.p = plugin
        self.logger = getattr(plugin, "logger", None)
        self._lock = threading.RLock()
        #: 待人工确认的不可逆提议
        self._proposals: List[Dict[str, Any]] = []
        self._proposal_seq = 0
        #: 已执行记录（供面板展示"AI 干过什么"）
        self._executed: List[Dict[str, Any]] = []
        self._executed_max = 200
        #: 撤销记录保管箱。窗口秒数由 manager 按配置同步过来。
        self.undo_registry = UndoRegistry(logger=self.logger)

    # ------------------------------------------------------------------ 日志
    def _log(self, level: str, msg: str) -> None:
        fn = getattr(self.logger, level, None)
        if callable(fn):
            try:
                fn(msg)
            except Exception:
                pass

    def _audit(self, message: str) -> None:
        """写进面板操作日志（与人工操作同一条流，但带 [AI] 前缀）。

        为什么要进**同一条**流：出事时服主会去翻面板日志。
        AI 的操作如果只写在别的地方，他翻遍日志也找不到 —— 那等于没有审计。
        """
        try:
            fn = getattr(self.p, "_log_admin_action", None)
            if callable(fn):
                fn(f"[AI] {message}")
            else:
                self._log("info", f"[AI] {message}")
        except Exception:
            pass

    # ------------------------------------------------------------------ 入口
    def run_tool(self, name: str, args: Optional[Dict[str, Any]] = None,
                 *, actor: str = "ai",
                 allow_irreversible: bool = False) -> ToolResult:
        """执行一个工具。**调用方必须保证这个调用已经过 Gate 校验。**

        ★ 这里**不重复**做闸门校验（那是 gate 的职责，重复实现必然漂移），
          但**必须**自己再确认一次风险等级 —— 因为执行是唯一有后果的动作，
          多一次 O(1) 的判断换"不可逆操作绝不会自动发生"，非常划算。

        ``allow_irreversible``
        ----------------------
        ★ 默认 ``False``，即不可逆工具只登记提议（v7.8 行为，一字未改）。

        只有 ``manager`` 在接管级别为 2（完全放开）时才会传 ``True``。
        开启后不可逆工具**直接执行**，并且：

          · 执行前先抓回滚素材（``_capture_undo``）；
          · 执行成功后按素材登记撤销记录（``_register_undo``）；
          · 抓不到素材时**照样执行**，但撤销记录标记为
            ``undoable=False`` 并说明原因 —— 服主至少能知道
            「这条动过了、而且救不回来」。

        为什么抓素材要在执行**前**：删档的 ``.deleted_bak`` 是执行中写的，
        但白名单被删的完整记录、server.properties 的旧值，一旦执行就
        只能靠反查，而那些反查路径可能已经变了。前置抓取最准。
        """
        t0 = time.time()
        spec = get_tool(name)
        if spec is None:
            return ToolResult(False, None, f"没有名为 {name} 的工具")

        args = dict(args or {})

        try:
            if spec.risk == RiskLevel.IRREVERSIBLE and not allow_irreversible:
                # ★ 默认路径：不执行，只登记提议。
                return self._propose(spec, args, t0)
            if spec.risk == RiskLevel.FORBIDDEN:
                # 结构上不该出现（FORBIDDEN 根本注册不进来）。
                # 留着这个分支是为了"万一注册表被改坏了也不执行"。
                self._log("error", f"[AI] 拒绝执行禁区工具 {name}")
                return ToolResult(False, None, "禁区工具不允许执行")
            fn = self._DISPATCH.get(spec.name)
            if fn is None:
                return ToolResult(False, None, f"工具 {name} 已注册但没有实现")

            # ---- 不可逆直执行路径：先抓回滚素材 ----
            undo_pre = None
            if spec.risk == RiskLevel.IRREVERSIBLE:
                try:
                    undo_pre = self._capture_undo(spec.name, args)
                except Exception as e:
                    undo_pre = (None, False, f"抓取回滚素材失败：{e}")
                self._log("warning",
                          f"[AI] 接管级别=完全放开，直接执行不可逆工具 {spec.name}"
                          f"（参数 {self._fmt_args(args)}）。执行后将登记撤销记录。")

            ok, payload, note = fn(self, **args)
            elapsed = time.time() - t0
            self._record_executed(spec, args, ok, note, actor)

            # ---- 不可逆执行成功 → 登记撤销记录 ----
            if ok and spec.risk == RiskLevel.IRREVERSIBLE:
                rec = self._register_undo(spec, args, payload, note,
                                          actor=actor, pre=undo_pre)
                # 把撤销信息塞进返回值，让模型和面板都能看到"能撤回"
                if rec is not None:
                    if isinstance(payload, dict):
                        payload = dict(payload)
                        payload["undo_id"] = rec.rid
                        payload["undo_remaining"] = round(
                            rec.remaining(), 1)
                        payload["undoable"] = rec.undoable
                    extra = (f"，可在 {int(rec.remaining())} 秒内撤销（{rec.rid}）"
                             if rec.undoable else "，⚠ 该操作无法回滚")
                    note = f"{note}{extra}"

            if ok and spec.risk in (RiskLevel.REVERSIBLE,
                                    RiskLevel.IRREVERSIBLE):
                self._audit(f"{note}（工具 {spec.name}，参数 {self._fmt_args(args)}）")
            return ToolResult(ok, payload, note, elapsed)
        except TypeError as e:
            # 参数签名不匹配 = 闸门漏了一个参数。这类 bug 必须在日志里
            # 明明白白地暴露，不能被吞成"工具执行失败"这种含糊提示。
            self._log("error", f"[AI] 工具 {name} 参数不匹配（闸门可能漏校验）: {e}")
            return ToolResult(False, None, f"参数不匹配：{e}")
        except Exception as e:
            self._log("error", f"[AI] 工具 {name} 执行异常: {e}")
            return ToolResult(False, None, f"执行失败：{e}")

    @staticmethod
    def _fmt_args(args: Dict[str, Any]) -> str:
        try:
            return json.dumps(args, ensure_ascii=False)
        except Exception:
            return str(args)

    def _record_executed(self, spec: ToolSpec, args: Dict[str, Any],
                         ok: bool, note: str, actor: str) -> None:
        rec = {"ts": time.time(), "tool": spec.name, "risk": spec.risk.value,
               "args": args, "ok": bool(ok), "note": note, "actor": actor}
        with self._lock:
            self._executed.append(rec)
            if len(self._executed) > self._executed_max:
                del self._executed[:len(self._executed) - self._executed_max]

    # ------------------------------------------------------------- 提议队列
    def _propose(self, spec: ToolSpec, args: Dict[str, Any],
                 t0: float) -> ToolResult:
        with self._lock:
            self._proposal_seq += 1
            pid = f"p{self._proposal_seq}"
            rec = {
                "id": pid,
                "ts": time.time(),
                "tool": spec.name,
                "risk": spec.risk.value,
                "args": args,
                "summary": self._summarize(spec.name, args),
                "status": "pending",
            }
            self._proposals.append(rec)
            if len(self._proposals) > _MAX_PROPOSALS:
                del self._proposals[:len(self._proposals) - _MAX_PROPOSALS]
        self._audit(f"提出待确认操作：{rec['summary']}")
        return ToolResult(
            True,
            {"proposal_id": pid, "status": "pending",
             "message": "该操作不可逆，已登记为待确认提议，需服主在面板确认后才会执行。"},
            f"已登记待确认提议 {pid}：{rec['summary']}",
            time.time() - t0,
        )

    def _summarize(self, tool: str, args: Dict[str, Any]) -> str:
        """给不可逆操作生成一句人类可读的摘要（面板上显示的决策依据）。"""
        name = str(args.get("name") or args.get("key") or "").strip()
        reason = str(args.get("reason") or "").strip()
        table = {
            "delete_player_data": f"删除玩家 {name} 的存档数据",
            "clear_whitelist_entry": f"移除白名单记录 {name}",
            "set_server_property": f"修改服务器属性 {name}={args.get('value', '')}",
        }
        base = table.get(tool, f"{tool}({self._fmt_args(args)})")
        return f"{base}；原因：{reason}" if reason else base

    def list_proposals(self, *, include_done: bool = False,
                       limit: int = 50) -> List[Dict[str, Any]]:
        """列出待确认提议。过期的自动标记为 expired（**不删除**）。

        为什么不删除而标记：服主可能想知道"AI 三天前提过什么"。
        直接删掉等于销毁证据 —— 而且如果那个提议其实是对的、
        只是没人看到，标记出来才能让他发现"我漏了一次处置"。
        """
        now = time.time()
        out: List[Dict[str, Any]] = []
        with self._lock:
            for rec in self._proposals:
                if rec["status"] == "pending" and now - rec["ts"] > _PROPOSAL_TTL:
                    rec["status"] = "expired"
                if include_done or rec["status"] == "pending":
                    out.append(dict(rec))
        out.sort(key=lambda r: r["ts"], reverse=True)
        return out[:limit]

    def get_proposal(self, pid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for rec in self._proposals:
                if rec["id"] == str(pid):
                    return dict(rec)
        return None

    def reject_proposal(self, pid: str, note: str = "") -> Tuple[bool, str]:
        with self._lock:
            for rec in self._proposals:
                if rec["id"] == str(pid):
                    if rec["status"] != "pending":
                        return False, f"该提议已是 {rec['status']} 状态"
                    rec["status"] = "rejected"
                    rec["note"] = str(note or "")
                    break
            else:
                return False, "提议不存在"
        self._audit(f"驳回 AI 提议 {pid}：{rec.get('summary', '')}")
        return True, "已驳回"

    def confirm_proposal(self, pid: str, *, actor: str = "admin") -> Tuple[bool, str]:
        """**唯一**能让不可逆操作真正执行的入口。

        只应由 web 层在「管理员会话 + 明确点击确认」时调用。
        ``actor`` 记为人工（不是 "ai"）—— 因为最终决定权是人，
        这条记录的语义是"人批准执行了这个操作"。
        """
        with self._lock:
            target = None
            for rec in self._proposals:
                if rec["id"] == str(pid):
                    target = rec
                    break
            if target is None:
                return False, "提议不存在"
            if target["status"] != "pending":
                return False, f"该提议已是 {target['status']} 状态，不能重复执行"
            # 先占位再执行（不持锁执行）：执行可能很慢，持锁会让整个
            # 提议队列阻塞 —— 面板同时刷新列表就会卡住。
            target["status"] = "executing"

        tool = str(target.get("tool") or "")
        args = dict(target.get("args") or {})
        spec = get_tool(tool)
        if spec is None:
            with self._lock:
                target["status"] = "failed"
                target["note"] = "工具已不存在"
            return False, f"工具 {tool} 已不存在，提议作废"

        fn = self._DISPATCH.get(tool)
        if fn is None:
            with self._lock:
                target["status"] = "failed"
                target["note"] = "无实现"
            return False, f"工具 {tool} 没有实现"

        try:
            ok, payload, note = fn(self, **args)
        except Exception as e:
            ok, payload, note = False, None, f"执行失败：{e}"

        with self._lock:
            target["status"] = "done" if ok else "failed"
            target["note"] = note
            target["executed_at"] = time.time()

        if ok:
            self._audit(f"服主确认执行：{target.get('summary', tool)}（提议 {pid}）")
            self._record_executed(spec, args, True, note, actor)
        else:
            self._log("warning", f"[AI] 确认执行提议 {pid} 失败：{note}")
        return ok, note

    def executed_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._executed[-limit:])

    # ==================================================================
    # 撤销窗口（仅接管级别 2 会用到）
    # ==================================================================

    def set_undo_window(self, seconds: Any) -> float:
        """同步撤销窗口配置（manager 在每次读取配置时调用）。"""
        return self.undo_registry.set_window(seconds)

    def _capture_undo(self, tool: str, args: Dict[str, Any],
                      ) -> Tuple[Any, bool, str]:
        """执行**前**抓取回滚素材。返回 ``(payload, undoable, why)``。

        ★ 为什么前置抓：白名单被删的完整记录、server.properties 的旧值，
          执行后只能靠反查，而反查路径可能已经不成立（记录已被 pop、
          文件已被重写）。前置抓是唯一准确的时机。

        ★ 这里**不抛异常**：素材抓不到不该阻止执行（服主已经选了
          完全放开），但必须让撤销记录如实标注"救不回来"。
          调用方用 try 包住本方法，任何异常都会被转成
          ``(None, False, 原因)``。
        """
        fn = self._UNDO_CAPTURE.get(tool)
        if fn is None:
            return None, False, f"工具 {tool} 没有提供回滚素材抓取器"
        return fn(self, args)

    def _register_undo(self, spec: ToolSpec, args: Dict[str, Any],
                       payload: Any, note: str, *, actor: str,
                       pre: Optional[Tuple[Any, bool, str]] = None):
        """执行成功后登记撤销记录。返回 ``UndoRecord``（失败返回 None）。

        素材优先用**工具自己交出来的**（``payload["undo"]``）——
        那是执行过程中最准确的现场快照（比如 ``delete_player_data``
        删完才知道实际删了哪几个文件）。前置抓的素材作为兜底。
        """
        material = None
        undoable = True
        why = ""

        # ① 工具在 payload 里自带 undo 素材 —— 最准
        if isinstance(payload, dict) and "undo" in payload:
            material = payload.get("undo")
            undoable = True
        elif pre is not None:
            material, undoable, why = pre
        else:
            undoable, why = False, "没有可用的回滚素材"

        if not undoable and not why:
            why = "该操作没有留下可还原的数据"

        try:
            rec = self.undo_registry.register(
                spec.name, args, risk=str(spec.risk.value), actor=str(actor),
                summary=self._summarize(spec.name, args),
                undo_payload=material, undoable=undoable, undo_why=why,
            )
        except Exception as e:
            self._log("error", f"[AI] 登记撤销记录失败（{spec.name}）：{e}")
            return None

        if undoable:
            self._audit(
                f"⚠ 不可逆操作已执行：{rec.summary}；"
                f"撤销 id={rec.rid}，窗口 {int(rec.remaining())} 秒")
        else:
            # 无法回滚是重要信息，必须用更强的措辞进审计
            self._audit(
                f"⚠⚠ 不可逆操作已执行且**无法回滚**：{rec.summary}；"
                f"原因：{why}；撤销 id={rec.rid}")
            self._log("warning",
                      f"[AI] 不可逆操作 {spec.name} 无法回滚：{why}")
        return rec

    def list_undoable(self, *, include_done: bool = False,
                      limit: int = 50) -> List[Dict[str, Any]]:
        """列出撤销记录（默认只给窗口内还能撤回的那些）。"""
        return self.undo_registry.list(include_done=include_done, limit=limit)

    def undo_pending_count(self) -> int:
        return self.undo_registry.pending_count()

    def undo(self, rid: str, *, actor: str = "admin") -> Tuple[bool, str]:
        """回滚一条已执行的不可逆操作。

        ★ 返回 ``(是否成功, 说明)``。三种失败各自有不同的说明：
            · 记录不存在 / 已撤销 / 已过期 —— 状态类失败；
            · 素材缺失 —— **如实告知"救不回来"**，不能让服主以为成功了；
            · 回滚动作本身抛异常 —— 把原始异常写进说明，便于排查。

        ★ 回滚动作由 ``_UNDO_APPLY`` 里的工具专用函数执行。没有那个
          函数时直接判失败 —— 宁可报错也不要"假装回滚成功"。
        """
        rec = self.undo_registry.get(rid)
        if rec is None:
            return False, "撤销记录不存在"
        if rec.undone:
            return False, "这条记录已经撤销过了"
        if rec.expired():
            left = round(-rec.remaining(), 1)
            return False, (f"撤销窗口已关闭（超出 {left} 秒）。"
                           f"该操作现在真的不可逆了。")
        if not rec.undoable:
            return False, (f"该操作没有留下可还原的数据，无法回滚。"
                           f"原因：{rec.undo_why or '未说明'}")

        fn = self._UNDO_APPLY.get(rec.tool)
        if fn is None:
            return False, f"工具 {rec.tool} 没有提供回滚实现"

        try:
            ok, note = fn(self, rec.undo_payload, rec.args)
        except Exception as e:
            self._log("error", f"[AI] 撤销 {rid}（{rec.tool}）异常：{e}")
            return False, f"回滚执行异常：{e}"
        if not ok:
            return False, note or "回滚失败"

        self.undo_registry.mark_undone(rid, note)
        self._audit(f"服主撤销了不可逆操作：{rec.summary}"
                    f"（工具 {rec.tool}，撤销 id={rid}）")
        self._record_executed(
            get_tool(rec.tool) or ToolSpec(name=rec.tool,
                                           risk=RiskLevel.IRREVERSIBLE,
                                           description=""),
            dict(rec.args), True,
            f"[撤销] {note or rec.summary}", actor)
        return True, note or "已回滚"

    def mark_undo_notified(self, rid: str) -> bool:
        """标记「已向群里告警」。返回 True 表示本次是第一次。"""
        return self.undo_registry.mark_notified(rid)

    # ==================================================================
    # 只读工具实现
    # ==================================================================

    def _t_players(self) -> Tuple[bool, Any, str]:
        try:
            players = list((self.p._get_cache() or {}).get("players", []))
        except Exception as e:
            return False, None, f"读取玩家列表失败：{e}"
        rows = [{
            "name": p.get("name"), "xuid": p.get("xuid") or "",
            "ping": p.get("ping"), "dimension": p.get("dimension"),
            "health": p.get("health"), "is_op": bool(p.get("is_op")),
        } for p in players]
        return True, rows, f"当前在线 {len(rows)} 人"

    def _t_player_info(self, name: str) -> Tuple[bool, Any, str]:
        """玩家详情：快照 + 可信度档案 + 是否被封。

        ★ 三个来源合并。单看快照只能拿到"现在在线吗"，而 AI 判断
          "这个玩家可不可信"需要的恰恰是**历史信任分**与**设备数**。
          分三次调工具既慢又容易漏，所以合成一个。
        """
        name = str(name or "").strip()
        info: Dict[str, Any] = {"name": name, "online": False}
        try:
            for p in list((self.p._get_cache() or {}).get("players", [])):
                if str(p.get("name")) == name:
                    info["online"] = True
                    info.update({
                        "xuid": p.get("xuid") or "",
                        "uuid": p.get("uuid") or "",
                        "ping": p.get("ping"),
                        "dimension": p.get("dimension"),
                        "health": p.get("health"),
                        "is_op": bool(p.get("is_op")),
                    })
                    break
        except Exception:
            pass

        # 可信度档案
        try:
            mgr = getattr(self.p, "trust", None)
            if mgr is not None:
                key, kind = mgr.storage_key_for_name(name) if hasattr(
                    mgr, "storage_key_for_name") else (None, None)
                rec = None
                if key:
                    rec = mgr.load(str(key))
                if rec is None:
                    # 回退：按名字在档案里找（xuid 未知时）
                    for k, r in mgr.iter_records():
                        if str(r.get("name") or "") == name:
                            rec = r
                            break
                if rec is not None:
                    info["trust_score"] = rec.get("score")
                    info["trust_devices"] = len(rec.get("devices") or [])
                    info["trust_bound_qq"] = rec.get("bound_qq") or ""
                    info["trust_last_seen"] = rec.get("last_seen") or ""
        except Exception as e:
            info["trust_error"] = str(e)

        # 封禁状态
        try:
            for b in list((self.p._get_cache() or {}).get("player_bans", [])):
                if str(b.get("name")) == name:
                    info["banned"] = True
                    info["ban_reason"] = b.get("reason")
                    info["ban_expiration"] = b.get("expiration")
                    break
            else:
                info["banned"] = False
        except Exception:
            pass

        if not info.get("online") and "trust_score" not in info:
            return False, info, f"未找到玩家 {name} 的记录"
        return True, info, f"玩家 {name} 详情已取回"

    def _t_query_logs(self, level: str = "", keyword: str = "",
                      limit: Any = 30) -> Tuple[bool, Any, str]:
        """检索日志。

        ★ 数据来源优先级：**latest.log 文件 > 面板内存缓冲**。
        面板缓冲只含 Python 侧写的那几类日志（见 log_tailer.py 的说明），
        而用户问"最近有没有报错"时想看的是**真正**的服务端输出。
        所以先试着读文件，读不到再退回内存缓冲。
        """
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            lim = 30
        lim = max(1, min(lim, 200))

        lv = str(level or "").strip().upper()
        # ★ 级别别名归一（v9.0.1）
        # ---------------------------------
        # parse_log_line 用 _LEVEL_ALIAS 把级别**归一化**了：
        #   WARNING → WARN、INFORMATION → INFO、SEVERE → ERROR、
        #   CRITICAL → FATAL
        # 而这里原来直接拿用户/模型给的值去比，于是模型写 "WARNING"
        # 时 `"WARNING" != "WARN"` 恒真 → **静默返回空**，
        # 模型会得出"日志里没有警告"的错误结论。
        # 这里做同一套归一化，保证「怎么问都能问到」。
        if lv:
            try:
                from ..log_tailer import _LEVEL_ALIAS as _LA
                lv = _LA.get(lv, lv)
            except Exception:
                # 别名表取不到时用手写的等价映射兜底
                lv = {"WARNING": "WARN", "INFORMATION": "INFO",
                      "SEVERE": "ERROR", "CRITICAL": "FATAL"}.get(lv, lv)
        # "ALL"/空 = 不限级别
        if lv in ("ALL", "*"):
            lv = ""
        kw = str(keyword or "").strip()

        lines: List[str] = []
        source = ""

        # ① latest.log 文件
        try:
            from ..log_tailer import snapshot_tail, parse_log_line
            root = None
            finder = getattr(self.p, "_find_server_root", None)
            if callable(finder):
                root = finder()
            if root is not None:
                path = root / "logs" / "latest.log"
                if path.exists():
                    raw = snapshot_tail(path, max_lines=500)
                    source = str(path)
                    # ★★ 续行级别继承（v9.0.1）
                    # ------------------------------------
                    # 日志里的堆栈跟踪是这样的：
                    #     [12:00:01] [Server/ERROR]: 出错了
                    #         at foo.bar(baz.py:12)
                    #         at qux.quux(quux.py:34)
                    # 只有**第一行**带级别前缀，后面几行 parse_log_line
                    # 返回 level="UNKNOWN"。
                    #
                    # 原实现逐行独立判断 `rec.get("level") != lv`，
                    # 于是按 ERROR 过滤时，**根因所在的堆栈行全被丢掉**，
                    # 只剩一句"出错了" —— 排查等于白查。
                    #
                    # 修复：像 LogTailer 那样，让无前缀的续行**继承**
                    # 上一条真实日志的级别。这样「ERROR 后面跟 5 行堆栈」
                    # 的 5 行也会带上 ERROR，级别过滤不再漏掉根因。
                    _last_lv = ""
                    for line in raw:
                        rec = parse_log_line(line)
                        if rec is None:
                            continue
                        _this_lv = rec.get("level")
                        if _this_lv and _this_lv != "UNKNOWN":
                            # 带级别前缀 = 一条新日志，记下来供后续续行继承
                            _last_lv = _this_lv
                            _eff_lv = _this_lv
                        else:
                            # 无前缀行（堆栈续行 / 空行后的碎片）→ 继承
                            # 上一条真实日志的级别；若开头就没有过任何
                            # 带级别的行，则退化为 UNKNOWN，不做过滤穿透。
                            _eff_lv = _last_lv or _this_lv
                        if lv and _eff_lv != lv:
                            continue
                        if kw and kw.lower() not in str(rec.get("raw_line", "")).lower():
                            continue
                        lines.append(str(rec.get("raw_line", line)))
        except Exception as e:
            self._log("debug", f"[AI] 读 latest.log 失败，回退内存缓冲: {e}")

        # ② 内存缓冲兜底
        if not lines:
            try:
                buf = getattr(self.p, "_console_log", None) or []
                source = source or "面板内存缓冲"
                for item in list(buf)[-500:]:
                    text = str(item.get("message", item) if isinstance(item, dict) else item)
                    lvl = str(item.get("level", "") if isinstance(item, dict) else "").upper()
                    if lv and lv not in ("", "ALL") and lvl != lv:
                        continue
                    if kw and kw.lower() not in text.lower():
                        continue
                    lines.append(text)
            except Exception:
                pass

        tail = lines[-lim:]
        if not tail:
            hint = []
            if lv:
                hint.append(f"级别={lv}")
            if kw:
                hint.append(f"关键词={kw!r}")
            cond = "、".join(hint) or "无过滤条件"
            return True, [], f"没有匹配的日志（{cond}；来源：{source or '无可用来源'}）"
        return True, tail, f"返回 {len(tail)} 行日志（来源：{source}）"

    def _t_list_whitelist(self, status: str = "") -> Tuple[bool, Any, str]:
        try:
            apps = list(self.p._list_whitelist_apps() or [])
        except Exception as e:
            return False, None, f"读取白名单失败：{e}"
        st = str(status or "").strip().lower()
        if st:
            apps = [a for a in apps if str(a.get("status", "pending")).lower() == st]
        rows = [{
            "name": a.get("name"), "status": a.get("status"),
            "reason": a.get("reason"), "ip": a.get("ip"),
            "xuid": a.get("xuid"), "applied_at": a.get("applied_at"),
        } for a in apps]
        return True, rows[:100], f"共 {len(rows)} 条白名单申请"

    def _t_list_bans(self, scope: str = "local") -> Tuple[bool, Any, str]:
        sc = str(scope or "local").strip().lower() or "local"
        if sc == "cloud":
            try:
                mgr = getattr(self.p, "cloud_mgr", None)
                if mgr is None:
                    return False, None, "云黑模块未就绪"
                ok, msg, bans = mgr._list_bans()
                if not ok:
                    return False, None, f"云黑查询失败：{msg}"
                rows = [{"name": b.get("name"), "xuid": b.get("xuid"),
                         "reason": b.get("reason"),
                         "time": b.get("ban_time") or b.get("created")}
                        for b in (bans or [])[:100]]
                return True, rows, f"云黑共 {len(bans or [])} 条"
            except Exception as e:
                return False, None, f"云黑查询异常：{e}"

        cache = {}
        try:
            cache = self.p._get_cache() or {}
        except Exception:
            pass
        rows = [{
            "name": b.get("name"), "xuid": b.get("xuid"),
            "reason": b.get("reason"), "source": b.get("source"),
            "expiration": b.get("expiration"),
        } for b in (cache.get("player_bans") or [])[:100]]
        ips = [{"address": b.get("address"), "reason": b.get("reason")}
               for b in (cache.get("ip_bans") or [])[:100]]
        return True, {"players": rows, "ips": ips}, \
            f"本地封禁：玩家 {len(rows)} 条、IP {len(ips)} 条"

    # ------------------------------------------------------------------
    # 玩家行为记录（只读）
    # ------------------------------------------------------------------
    #
    # ★ 三个工具共用的取值与说明逻辑收口在下面两个私有辅助里，
    #   避免三处各写一遍"记录器没开时返回什么话"。

    def _behavior_recorder(self):
        """取行为记录器；未初始化返回 None。"""
        try:
            return getattr(self.p, "_behavior", None)
        except Exception:
            return None

    def _behavior_off_note(self) -> str:
        """统一的"功能未开启"说明。

        ★ 这句提示很关键：如果只返回空列表，模型很可能把"没开功能"
          误判成"这个人很干净"，进而给出错误结论。
        """
        return ("行为记录功能未开启（服主可在面板「AI → 玩家行为记录」中调整档位），"
                "因此查不到任何记录。")

    def _behavior_level(self) -> int:
        try:
            return int(self.p._behavior_level())
        except Exception:
            return 0

    def _behavior_limit(self, limit) -> int:
        """解析并夹取 limit。模型常传字符串或离谱数值。"""
        try:
            n = int(limit)
        except Exception:
            n = 0
        if n <= 0:
            try:
                n = int(self.p._behavior_query_limit())
            except Exception:
                n = 200
        return max(1, min(n, 500))

    def _t_query_player_behavior(self, player: str = "", kind: str = "",
                                 limit=None) -> Tuple[bool, Any, str]:
        """查某个玩家的行为记录。"""
        rec = self._behavior_recorder()
        if rec is None:
            return True, [], self._behavior_off_note()
        if self._behavior_level() <= 0:
            return True, [], self._behavior_off_note()

        name = str(player or "").strip()
        if not name:
            # player 在 ToolSpec 里是必填项，走到这里说明闸门没拦住 ——
            # 明确报错而不是静默查全员，否则模型会以为"这就是所有人的记录"。
            return False, None, "必须指定玩家名"

        n = self._behavior_limit(limit)
        try:
            rows = rec.query(player=name, kind=str(kind or "").strip(), limit=n)
        except Exception as e:
            return False, None, f"查询行为记录失败：{e}"

        if not rows:
            return True, [], (
                f"没有找到玩家「{name}」的行为记录。"
                f"可能原因：该玩家近期无此类行为、记录档位较低未覆盖该类型、"
                f"或记录已被清理（默认只保留有限天数）。"
            )
        return True, rows, f"查到 {len(rows)} 条「{name}」的行为记录"

    def _t_list_recent_behavior(self, kind: str = "",
                                limit=None) -> Tuple[bool, Any, str]:
        """查全服最近的行为记录。"""
        rec = self._behavior_recorder()
        if rec is None:
            return True, [], self._behavior_off_note()
        if self._behavior_level() <= 0:
            return True, [], self._behavior_off_note()

        n = self._behavior_limit(limit)
        try:
            rows = rec.list_recent(kind=str(kind or "").strip(), limit=n)
        except Exception as e:
            return False, None, f"查询行为记录失败：{e}"

        k = str(kind or "").strip()
        if not rows:
            return True, [], (
                f"最近没有{'「' + k + '」类型的' if k else ''}行为记录。"
                f"注意：记录按档位过滤（档位越低记的越少），且只保留有限天数。"
            )
        return True, rows, f"查到最近 {len(rows)} 条行为记录"

    def _t_summarize_player_behavior(self, player: str = "",
                                     ) -> Tuple[bool, Any, str]:
        """玩家行为画像。"""
        rec = self._behavior_recorder()
        if rec is None:
            return True, {}, self._behavior_off_note()
        if self._behavior_level() <= 0:
            return True, {}, self._behavior_off_note()

        name = str(player or "").strip()
        if not name:
            return False, None, "必须指定玩家名"

        try:
            data = rec.summarize(player=name, limit=500)
        except Exception as e:
            return False, None, f"生成行为画像失败：{e}"

        if not data.get("total"):
            return True, data, f"玩家「{name}」近期没有行为记录"
        # 画像里带上档位，让模型知道"看不到的不是没有，而是没记"
        try:
            from ..behaviors import LEVEL_LABEL
            data["record_level"] = LEVEL_LABEL.get(self._behavior_level(), "?")
        except Exception:
            pass
        return True, data, f"「{name}」共 {data.get('total')} 条记录"

    def _t_server_stats(self) -> Tuple[bool, Any, str]:
        info: Dict[str, Any] = {}
        try:
            info.update(self.p.get_system_stats() or {})
        except Exception as e:
            info["stats_error"] = str(e)
        try:
            info["online"] = len((self.p._get_cache() or {}).get("players", []))
        except Exception:
            info["online"] = 0
        try:
            hub = getattr(self.p, "qq_hub", None)
            info["bots"] = hub.status() if hub is not None else []
        except Exception:
            info["bots"] = []
        try:
            info["tps"] = getattr(self.p, "_last_tps", None)
        except Exception:
            pass
        return True, info, f"在线 {info.get('online', 0)} 人"

    def _t_list_mods(self) -> Tuple[bool, Any, str]:
        ld = getattr(self.p, "mod_loader", None)
        if ld is None:
            return False, None, "子插件加载器未初始化"
        try:
            loaded = set(ld.loaded_uuids())
        except Exception:
            loaded = set()
        rows = []
        try:
            for m in ld.list_mods():
                rows.append({
                    "name": m.get("name"), "uuid": m.get("uuid"),
                    "type": m.get("type"), "version": m.get("version"),
                    "enabled": m.get("enabled"),
                    "running": str(m.get("uuid")) in loaded,
                })
        except Exception as e:
            return False, None, f"读取子插件列表失败：{e}"
        return True, rows[:100], f"共 {len(rows)} 个子插件"

    # ==================================================================
    # 可逆工具实现
    # ==================================================================

    def _t_kick_player(self, name: str, reason: str = "") -> Tuple[bool, Any, str]:
        """踢人。**只踢在线玩家** —— 对不在线的人执行 kick 会静默失败，
        而静默失败最容易被当成"AI 干了坏事但没生效"，徒增困惑。
        """
        name = str(name or "").strip()
        reason = str(reason or "").strip() or "被服务器管理踢出"
        target = self._online_player(name)
        if target is None:
            return False, {"name": name}, f"玩家 {name} 不在线，无法踢出"
        # 名字里可能含空格/引号，直接拼进命令会断句。
        # BDS 的 kick 语法没有转义机制，所以只能拒绝这类名字 ——
        # 比"拼出一条被截断的命令去踢错人"安全得多。
        if _unsafe_for_command(name):
            return False, {"name": name}, f"玩家名含特殊字符，出于安全不执行命令"
        try:
            ok = bool(self.p.server.dispatch_command(
                self.p.server.command_sender, f'kick "{name}" {reason}'))
        except Exception as e:
            return False, {"name": name}, f"踢出失败：{e}"
        if not ok:
            return False, {"name": name}, f"踢出 {name} 的命令被服务器拒绝"
        return True, {"name": name, "reason": reason}, f"已踢出玩家 {name}"

    def _t_ban_player(self, name: str, reason: str = "",
                      duration: Any = "") -> Tuple[bool, Any, str]:
        name = str(name or "").strip()
        reason = str(reason or "").strip() or "被服务器管理封禁"
        if _unsafe_for_command(name):
            return False, {"name": name}, "玩家名含特殊字符，出于安全不执行命令"
        # 目标必须真实存在：在线，或曾在封禁/信任记录里出现过。
        # 纯粹凭模型编一个名字就封，等于给模型一个"封任意字符串"的能力，
        # 而拼错的名字会被写进封禁库，之后真人用这个名字进服会被误封。
        if not self._player_known(name):
            return False, {"name": name}, f"系统中没有玩家 {name} 的记录，拒绝封禁"

        cmd = f'ban "{name}" {reason}'
        secs = 0
        if str(duration or "").strip():
            try:
                minutes = int(str(duration).strip())
            except (TypeError, ValueError):
                return False, {"name": name}, "duration 必须是整数分钟"
            if minutes <= 0:
                return False, {"name": name}, "duration 必须为正数"
            secs = minutes * 60
        if secs:
            cmd = f'ban "{name}" {reason} {secs}'
        try:
            ok = bool(self.p.server.dispatch_command(
                self.p.server.command_sender, cmd))
        except Exception as e:
            return False, {"name": name}, f"封禁失败：{e}"
        if not ok:
            return False, {"name": name}, f"封禁 {name} 的命令被服务器拒绝"
        span = f"{secs} 秒" if secs else "永久"
        return True, {"name": name, "reason": reason, "duration": span}, \
            f"已封禁 {name}（{span}）"

    def _t_unban_player(self, name: str, scope: str = "local") -> Tuple[bool, Any, str]:
        name = str(name or "").strip()
        if _unsafe_for_command(name):
            return False, {"name": name}, "玩家名含特殊字符，出于安全不执行命令"
        sc = str(scope or "local").strip().lower() or "local"
        if sc == "cloud":
            try:
                mgr = getattr(self.p, "cloud_mgr", None)
                if mgr is None:
                    return False, None, "云黑模块未就绪"
                ok, msg = mgr._sync_unban({"name": name})
                if ok:
                    return True, {"name": name, "scope": "cloud"}, f"已解除云黑：{msg}"
                return False, {"name": name}, f"解除云黑失败：{msg}"
            except Exception as e:
                return False, {"name": name}, f"解除云黑异常：{e}"
        try:
            ok = bool(self.p.server.dispatch_command(
                self.p.server.command_sender, f'unban "{name}"'))
        except Exception as e:
            return False, {"name": name}, f"解封失败：{e}"
        if not ok:
            return False, {"name": name}, f"解封 {name} 的命令被服务器拒绝"
        return True, {"name": name, "scope": "local"}, f"已解封 {name}"

    def _t_mute_player(self, name: str, duration: Any = 10,
                       reason: str = "") -> Tuple[bool, Any, str]:
        """禁言。**仅 OneBot 支持** —— 官方协议没有群禁言接口。

        在官方域上"成功禁言"是不可能的。所以这里显式返回失败并说明原因，
        而不是假装成功：假装成功会让模型以为处置生效了，转头在结论里
        写"已禁言该玩家"——服主看到会以为问题解决了，实际什么都没做。
        """
        name = str(name or "").strip()
        try:
            minutes = int(str(duration).strip())
        except (TypeError, ValueError):
            minutes = 10
        minutes = max(1, min(minutes, 43200))
        secs = minutes * 60

        target = self._online_player(name)
        if target is None:
            return False, {"name": name}, f"玩家 {name} 不在线，无法禁言"

        # 找一张 bridge 用途的 OneBot 卡去执行群禁言
        gw = None
        try:
            for g in self.p._gateways_by_purpose(PURPOSE_BRIDGE):
                if callable(getattr(g, "set_group_ban", None)):
                    gw = g
                    break
        except Exception:
            gw = None
        if gw is None:
            return False, {"name": name}, \
                "没有可用的 OneBot 机器人（官方机器人协议不支持群禁言）"
        return False, {"name": name}, \
            "群禁言需要 QQ 号而非玩家名，当前工具未建立玩家↔QQ 映射，未执行"

    # ==================================================================
    # 白名单审核（可逆；接管级别 1 起由 AI 自动完成）
    # ==================================================================

    def _t_approve_whitelist_entry(self, name: str, note: str = "",
                                   ) -> Tuple[bool, Any, str]:
        """批准一条**已提交的**白名单申请。

        ★ 两条硬约束（缺一不可）：
          1. 申请必须**已经存在**且处于 pending 状态。凭一个名字直接放行
             就是 ``FORBIDDEN_CAPABILITIES`` 里的 ``whitelist_add`` ——
             那条明确禁止。这里用"找不到待审申请就拒绝"来强制它。
          2. 批准后要同时写服务器 allowlist，否则面板里"已通过"、
             服务器却不认，是一个静默失败。
        """
        name = str(name or "").strip()
        if not name:
            return False, None, "缺少玩家名"
        key = name.lower()
        try:
            apps = self.p._list_whitelist_apps() or []
        except Exception as e:
            return False, None, f"读取白名单失败：{e}"

        hit = None
        for a in apps:
            if str(a.get("name") or "").strip().lower() == key:
                hit = a
                break
        if hit is None:
            # ★ 关键拒绝路径：不允许"凭空放人"
            return False, {"name": name}, (
                f"待审列表里没有 {name} 的申请。"
                f"批准只能作用于玩家自己提交的申请 —— "
                f"不能凭名字直接放行（那属于禁区能力）。"
            )
        cur = str(hit.get("status") or "pending")
        if cur != "pending":
            return False, {"name": name, "status": cur}, \
                f"{name} 的申请当前状态是 {cur}，只能审核 pending 状态的申请"

        # 写回状态 + 平台侧标记
        try:
            with self.p._whitelist_lock:
                rec = self.p._whitelist_apps.get(key)
                if isinstance(rec, dict):
                    rec["status"] = "approved"
                    rec["reviewed_by"] = "ai"
                    rec["review_note"] = str(note or "")
                    rec["reviewed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.p._save_whitelist()
        except Exception as e:
            return False, {"name": name}, f"写入白名单状态失败：{e}"

        # 服务器侧放行。失败**不当作整体失败** —— 面板状态已经改对了，
        # 只是需要重启/手动 add；如实说明比回滚更符合预期。
        cmd_ok = True
        try:
            self.p.server.dispatch_command(
                self.p.server.command_sender, f'allowlist add "{name}"')
        except Exception as e:
            cmd_ok = False
            self._log("warning", f"[AI] 白名单已批准但 allowlist add 失败：{e}")

        umsg = "" if cmd_ok else "（服务器 allowlist 同步失败，可能需重启生效）"
        return True, {"name": name, "status": "approved", "note": str(note or "")}, \
            f"已批准 {name} 的白名单申请{umsg}"

    def _t_reject_whitelist_entry(self, name: str, reason: str = "",
                                  ) -> Tuple[bool, Any, str]:
        """驳回一条**已提交的**白名单申请。约束同 approve。"""
        name = str(name or "").strip()
        if not name:
            return False, None, "缺少玩家名"
        key = name.lower()
        try:
            apps = self.p._list_whitelist_apps() or []
        except Exception as e:
            return False, None, f"读取白名单失败：{e}"

        hit = None
        for a in apps:
            if str(a.get("name") or "").strip().lower() == key:
                hit = a
                break
        if hit is None:
            return False, {"name": name}, f"待审列表里没有 {name} 的申请"
        cur = str(hit.get("status") or "pending")
        if cur != "pending":
            return False, {"name": name, "status": cur}, \
                f"{name} 的申请当前状态是 {cur}，只能审核 pending 状态的申请"

        try:
            with self.p._whitelist_lock:
                rec = self.p._whitelist_apps.get(key)
                if isinstance(rec, dict):
                    rec["status"] = "rejected"
                    rec["reviewed_by"] = "ai"
                    rec["review_note"] = str(reason or "")
                    rec["reviewed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.p._save_whitelist()
        except Exception as e:
            return False, {"name": name}, f"写入白名单状态失败：{e}"
        return True, {"name": name, "status": "rejected",
                      "reason": str(reason or "")}, \
            f"已驳回 {name} 的白名单申请"

    # ==================================================================
    # 不可逆工具实现（**只能**由 confirm_proposal 调用）
    # ==================================================================

    def _t_delete_player_data(self, name: str, reason: str = "") -> Tuple[bool, Any, str]:
        """删除玩家存档数据。极其危险的操作。

        实现上只删除**玩家数据文件**（world 下的 playerdata），不碰
        任何备份。删除前先做一份临时拷贝 —— 这不是"备份"（不列入
        备份管理），只是给这次操作留一条救命的退路：万一名字拼错了，
        服主还能从临时目录捞回来。
        """
        name = str(name or "").strip()
        root = None
        try:
            finder = getattr(self.p, "_find_server_root", None)
            if callable(finder):
                root = finder()
        except Exception:
            root = None
        if root is None:
            return False, None, "未找到服务器目录"

        # 先定位 uuid：靠扫 playerdata 目录用玩家名匹配太脆弱，
        # 直接查信任档案/在线玩家拿 uuid 更稳。
        uid = ""
        try:
            for p in list((self.p._get_cache() or {}).get("players", [])):
                if str(p.get("name")) == name:
                    uid = str(p.get("uuid") or "")
                    break
        except Exception:
            pass
        if not uid:
            try:
                mgr = getattr(self.p, "trust", None)
                if mgr is not None:
                    for k, r in mgr.iter_records():
                        if str(r.get("name") or "") == name:
                            uid = str(r.get("uuid") or "")
                            break
            except Exception:
                pass
        if not uid:
            return False, {"name": name}, f"无法确定 {name} 的 UUID，拒绝删除"

        removed = []
        #: 记录每个被删文件的「原路径 → 备份路径」，供撤销窗口回滚。
        #: ★ 只在备份**确实写成功**时才记进 undo 素材 —— 没备份却
        #:   声称"可回滚"是比不可回滚更坏的情况：服主以为有退路。
        undo_pairs = []
        for world_dir in root.glob("world*"):
            pd = world_dir / "playerdata"
            if not pd.is_dir():
                continue
            for cand in pd.glob(f"{uid}*"):
                try:
                    # 先拷一份 .deleted_bak 再删
                    bak = cand.with_suffix(cand.suffix + ".deleted_bak")
                    bak_ok = False
                    try:
                        if not bak.exists():
                            bak.write_bytes(cand.read_bytes())
                        bak_ok = bak.exists()
                    except Exception:
                        bak_ok = bak.exists()
                    cand.unlink()
                    removed.append(str(cand))
                    if bak_ok:
                        undo_pairs.append({"orig": str(cand), "bak": str(bak)})
                except Exception as e:
                    return False, {"name": name, "removed": removed}, f"删除 {cand} 失败：{e}"

        if not removed:
            return False, {"name": name, "uuid": uid}, \
                f"没有找到 {name} 的存档文件（可能从未进服过）"
        payload = {"name": name, "uuid": uid, "removed": removed,
                   "reason": reason}
        if undo_pairs:
            payload["undo"] = {"kind": "restore_bak", "pairs": undo_pairs}
        if len(undo_pairs) < len(removed):
            note = (f"已删除 {name} 的 {len(removed)} 个存档文件"
                    f"（其中 {len(undo_pairs)} 个留有 .deleted_bak 可还原，"
                    f"{len(removed) - len(undo_pairs)} 个未能备份）")
        else:
            note = f"已删除 {name} 的 {len(removed)} 个存档文件（已留 .deleted_bak 副本）"
        return True, payload, note

    def _t_clear_whitelist_entry(self, name: str, reason: str = "") -> Tuple[bool, Any, str]:
        name = str(name or "").strip()
        try:
            apps = self.p._list_whitelist_apps() or []
        except Exception as e:
            return False, None, f"读取白名单失败：{e}"
        hit = None
        for a in apps:
            if str(a.get("name") or "").strip().lower() == name.lower():
                hit = a
                break
        if hit is None:
            return False, {"name": name}, f"白名单里没有 {name}"

        key = name.lower()
        try:
            with self.p._whitelist_lock:
                self.p._whitelist_apps.pop(key, None)
            self.p._save_whitelist()
        except Exception as e:
            return False, {"name": name}, f"移除失败：{e}"

        # 同步从服务器 allowlist 里摘掉（否则白名单文件删了、服务器还认）
        try:
            self.p.server.dispatch_command(
                self.p.server.command_sender, f'allowlist remove "{name}"')
        except Exception:
            pass
        return True, {"name": name, "reason": reason,
                      # 完整记录交给撤销窗口 —— 有了它就能一字不差地写回
                      "undo": {"kind": "restore_record", "record": dict(hit)}}, \
            f"已移除白名单记录 {name}"

    def _t_set_server_property(self, key: str, value: str,
                               reason: str = "") -> Tuple[bool, Any, str]:
        """修改 server.properties 的某一项。

        ★ 只允许改**已存在**的键。模型编一个新键名出来，写进 properties
          不会报错但也不生效，用户以为改了其实没有 —— 静默失败。
          限定在现有键集合内，至少能保证"改动真的落到了某个配置项上"。
        """
        key = str(key or "").strip()
        value = str(value or "").strip()
        if not key:
            return False, None, "缺少 key"
        try:
            root, content = self.p._read_properties_raw()
        except Exception as e:
            return False, None, f"读取 server.properties 失败：{e}"
        if root is None:
            return False, None, "未找到 server.properties"

        existing = set()
        for line in str(content or "").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            existing.add(s.split("=", 1)[0].strip())
        if key not in existing:
            return False, {"key": key, "existing": sorted(existing)[:80]}, \
                f"server.properties 里没有 {key} 这一项（不允许新增未知键）"

        lines = str(content or "").splitlines()
        out = []
        done = False
        old_value = ""
        for line in lines:
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k = s.split("=", 1)[0].strip()
                if k == key:
                    old_value = s.split("=", 1)[1].strip()
                    out.append(f"{key}={value}")
                    done = True
                    continue
            out.append(line)
        if not done:
            out.append(f"{key}={value}")

        try:
            ok, msg = self.p._write_properties_raw("\n".join(out) + "\n")
        except Exception as e:
            return False, {"key": key}, f"写入失败：{e}"
        if not ok:
            return False, {"key": key}, f"写入失败：{msg}"
        return True, {"key": key, "value": value, "reason": reason,
                      "old_value": old_value,
                      # 改回旧值就是回滚。这个键被限定在「已存在的键」内，
                      # 所以旧值一定存在（可能是空串，那也是合法旧值）。
                      "undo": {"kind": "set_property",
                               "key": key, "old_value": old_value}}, \
            f"已修改 {key}={value}（需重启服务器生效）"

    # ==================================================================
    # 内部工具
    # ==================================================================

    def _online_player(self, name: str):
        name = str(name or "").strip()
        if not name:
            return None
        for p in list(self.p.server.online_players or []):
            try:
                if str(p.name) == name:
                    return p
            except Exception:
                continue
        return None

    def _player_known(self, name: str) -> bool:
        """玩家是否真实存在（在线 / 封禁记录 / 信任档案里有）。"""
        name = str(name or "").strip()
        if not name:
            return False
        if self._online_player(name) is not None:
            return True
        try:
            cache = self.p._get_cache() or {}
            for b in (cache.get("player_bans") or []):
                if str(b.get("name")) == name:
                    return True
        except Exception:
            pass
        try:
            mgr = getattr(self.p, "trust", None)
            if mgr is not None:
                for _k, r in mgr.iter_records():
                    if str(r.get("name") or "") == name:
                        return True
        except Exception:
            pass
        try:
            for a in (self.p._list_whitelist_apps() or []):
                if str(a.get("name") or "").strip().lower() == name.lower():
                    return True
        except Exception:
            pass
        return False

    #: 工具名 → 实现。**显式枚举**而不是 getattr(name)。
    #: getattr 会让"模型编一个与某个内部方法同名的工具名"有机会命中内部方法
    #: —— 那是提权。显式表保证只有在这里写下的名字能被执行。
    _DISPATCH: Dict[str, Any] = {}

    #: 工具名 → 执行**前**的回滚素材抓取器（可选）。见文件下的撤销实现区段。
    _UNDO_CAPTURE: Dict[str, Any] = {}

    #: 工具名 → 回滚动作实现。没有登记的工具会被判定"无法回滚"。
    _UNDO_APPLY: Dict[str, Any] = {}


def _unsafe_for_command(text: str) -> bool:
    """名字里是否含会破坏命令拼接的字符。

    BDS 的 kick/ban 命令不支持参数转义。名字里出现引号、反斜杠、
    换行、分号之类，要么断句、要么注入额外命令参数。
    宁可不执行（并给出明确理由），也不要拼出一条我们控制不了的命令。
    """
    s = str(text or "")
    if not s:
        return True
    for ch in ('"', "'", "\\", "\n", "\r", "\t", ";", "|", "&", "`", "$", "<", ">"):
        if ch in s:
            return True
    return False


def _bind(spec_name: str, fn) -> None:
    ToolRunner._DISPATCH[spec_name] = fn


# ---- 只读 ----
_bind("list_players", ToolRunner._t_players)
_bind("get_player_info", ToolRunner._t_player_info)
_bind("query_logs", ToolRunner._t_query_logs)
_bind("list_whitelist", ToolRunner._t_list_whitelist)
_bind("list_bans", ToolRunner._t_list_bans)
_bind("server_stats", ToolRunner._t_server_stats)
_bind("list_mods", ToolRunner._t_list_mods)
# ---- 只读：玩家行为记录 ----
_bind("query_player_behavior", ToolRunner._t_query_player_behavior)
_bind("list_recent_behavior", ToolRunner._t_list_recent_behavior)
_bind("summarize_player_behavior", ToolRunner._t_summarize_player_behavior)
# ---- 可逆 ----
_bind("kick_player", ToolRunner._t_kick_player)
_bind("ban_player", ToolRunner._t_ban_player)
_bind("unban_player", ToolRunner._t_unban_player)
_bind("mute_player", ToolRunner._t_mute_player)
_bind("approve_whitelist_entry", ToolRunner._t_approve_whitelist_entry)
_bind("reject_whitelist_entry", ToolRunner._t_reject_whitelist_entry)
# ---- 不可逆（只有 confirm_proposal / 级别 2 直执行能走到）----
_bind("delete_player_data", ToolRunner._t_delete_player_data)
_bind("clear_whitelist_entry", ToolRunner._t_clear_whitelist_entry)
_bind("set_server_property", ToolRunner._t_set_server_property)


# ===========================================================================
# 撤销实现（接管级别 2 的后悔药）
# ===========================================================================
#
# 分两张表：
#   ``_UNDO_CAPTURE`` —— 执行**前**抓素材。只在工具自己交不出 undo 时兜底。
#   ``_UNDO_APPLY``   —— 回滚动作本身。
#
# ★ 为什么要求工具在 payload 里自带 ``undo`` 优先：
#   白名单那条最典型 —— 被删的完整记录只有 ``_t_clear_whitelist_entry``
#   在删的那一瞬间手上才有，执行前抓取拿不到（记录还在，但抓取器
#   要重写一遍查找逻辑，等于把同一件事实现两遍，两边会漂移）。
#   所以：**就近交出**是首选，前置抓取只做兜底。
#
# ★ 前置抓取器对 delete_player_data 尤其重要：它无法预知删哪些文件
#   （取决于 world* 目录的实际内容），所以只能在 payload 里给。
#   这里刻意**不给** delete 配前置抓取器 —— 给了会给人一种"前置也能救"
#   的错觉，而它其实拿不到 pairs。宁缺勿假。


def _undo_capture_set_property(runner: "ToolRunner",
                                args: Dict[str, Any]) -> Tuple[Any, bool, str]:
    """server.properties 的旧值可以在执行前准确读到 —— 前置抓取最合适。"""
    key = str((args or {}).get("key") or "").strip()
    if not key:
        return None, False, "缺少 key，无法记录旧值"
    try:
        _root, content = runner.p._read_properties_raw()
    except Exception as e:
        return None, False, f"读取旧值失败：{e}"
    for line in str(content or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip() == key:
            return ({"kind": "set_property", "key": key,
                     "old_value": v.strip()}, True, "")
    return None, False, f"server.properties 里没有 {key}，无旧值可记录"


def _undo_apply_restore_bak(runner: "ToolRunner", payload: Any,
                            args: Dict[str, Any]) -> Tuple[bool, str]:
    """把 ``.deleted_bak`` 还原回原文件名。

    ★ 三条如实报告的失败情况：
        · 备份文件不在了（被清理 / 从没写成功）→ 报"备份已不存在"；
        · 原文件已经被重新创建（玩家又进服了）→ **不覆盖**，
          报"原文件已存在，未覆盖" —— 覆盖会把玩家的新存档删掉，
          那是在"撤销"的名义下造成新的数据损失。
        · 路径不合法（穿越到根目录外）→ 拒绝。
    """
    pairs = (payload or {}).get("pairs") if isinstance(payload, dict) else None
    if not isinstance(pairs, list) or not pairs:
        return False, "没有可还原的备份记录"

    import os
    restored = []
    failed = []
    for item in pairs:
        if not isinstance(item, dict):
            continue
        orig = str(item.get("orig") or "")
        bak = str(item.get("bak") or "")
        if not orig or not bak:
            continue
        # 防路径穿越：两个路径都必须落在同一个已存在的目录里
        try:
            odir = os.path.dirname(os.path.abspath(orig))
            bdir = os.path.dirname(os.path.abspath(bak))
        except Exception:
            failed.append(f"{orig}（路径异常）")
            continue
        if odir != bdir:
            failed.append(f"{orig}（备份与原路径不在同一目录，拒绝）")
            continue

        from pathlib import Path
        op = Path(orig)
        bp = Path(bak)
        if not bp.exists():
            failed.append(f"{op.name}（备份已不存在）")
            continue
        if op.exists():
            # ★ 不覆盖。玩家可能已经重新进服生成了新存档。
            failed.append(f"{op.name}（原文件已存在，未覆盖）")
            continue
        try:
            op.write_bytes(bp.read_bytes())
            restored.append(op.name)
        except Exception as e:
            failed.append(f"{op.name}（写入失败：{e}）")

    if not restored:
        return False, "没有任何文件被还原：" + "；".join(failed)
    msg = f"已还原 {len(restored)} 个存档文件（{', '.join(restored)}）"
    if failed:
        msg += f"；{len(failed)} 个未还原：" + "；".join(failed)
    return True, msg


def _undo_apply_restore_record(runner: "ToolRunner", payload: Any,
                               args: Dict[str, Any]) -> Tuple[bool, str]:
    """把被移除的白名单记录写回去。"""
    rec = (payload or {}).get("record") if isinstance(payload, dict) else None
    if not isinstance(rec, dict) or not rec:
        return False, "没有可还原的白名单记录"
    name = str(rec.get("name") or "").strip()
    if not name:
        return False, "备份记录里没有玩家名"
    key = name.lower()
    try:
        with runner.p._whitelist_lock:
            runner.p._whitelist_apps[key] = dict(rec)
        runner.p._save_whitelist()
    except Exception as e:
        return False, f"写回白名单失败：{e}"
    # 服务器侧的 allowlist 也要加回来，否则文件对了、服务器还不认
    try:
        runner.p.server.dispatch_command(
            runner.p.server.command_sender, f'allowlist add "{name}"')
    except Exception:
        pass
    return True, f"已恢复白名单记录 {name}"


def _undo_apply_set_property(runner: "ToolRunner", payload: Any,
                             args: Dict[str, Any]) -> Tuple[bool, str]:
    """把 server.properties 的某项改回旧值。

    ★ 复用 ``_t_set_server_property`` 而不是另写一份写入逻辑 ——
      另写一份意味着"写文件"这件事有两处实现，早晚漂移。
    """
    key = str((payload or {}).get("key") or "").strip()
    old = str((payload or {}).get("old_value") or "")
    if not key:
        return False, "没有记录要还原的键名"
    ok, _payload, note = runner._t_set_server_property(
        key, old, "撤销窗口回滚")
    if not ok:
        return False, f"改回失败：{note}"
    return True, f"已把 {key} 改回 {old!r}（需重启服务器生效）"


ToolRunner._UNDO_CAPTURE: Dict[str, Any] = {
    "set_server_property": _undo_capture_set_property,
}

ToolRunner._UNDO_APPLY: Dict[str, Any] = {
    "delete_player_data": _undo_apply_restore_bak,
    "clear_whitelist_entry": _undo_apply_restore_record,
    "set_server_property": _undo_apply_set_property,
}


def undo_table() -> Dict[str, Any]:
    """返回工具名 → 回滚实现的副本，供测试断言「不可逆工具都能回滚」。"""
    return dict(ToolRunner._UNDO_APPLY)


def dispatch_table() -> Dict[str, Any]:
    """返回工具名 → 实现的副本，供测试断言「注册表与实现表一一对应」。"""
    return dict(ToolRunner._DISPATCH)
