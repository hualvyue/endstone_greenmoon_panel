"""回档 / 撤销引擎。

来源与定位
----------
本文件由 ``endstone_mxback``（MxBack）的 ``rollback.py`` 转录而来。
它把「把某个区域恢复到若干秒之前的样子」这件事做完了最难的部分，
这里**不做算法重构，只换宿主** —— 理由写在下面每一节的 ★ 注释里。

为什么必须分批 + 主线程
-----------------------
世界方块写操作只能发生在**服务器主线程**。一次回档可能命中上万个
方块，如果一口气做完，主线程会被独占几十秒 —— 表现为全服卡死、
其它插件收不到 tick、客户端掉线。所以这里是「每批处理 N 个，批间
隔几 tick 让出主线程」的模型。

为什么 Dimension / Block 引用绝不跨 tick 缓存
---------------------------------------------
Endstone 的 Dimension / Block 是 **C++ 侧的视图对象**，pybind 返回它
时不会延长被包装对象的生命周期。跨 tick 握住一个旧引用，下次调用
就是去碰一块已释放的内存 —— **SIGSEGV，且 Python 层捕获不到**
（try/except 对段错误无效）。

所以代码里每一处需要 Dimension / Block 的地方，都是**当场按名字 /
坐标重新取**再用完即弃。看到重复取用会觉得啰嗦，请不要"优化"掉。

未加载区块必须跳过
------------------
``get_block_at`` 对未加载区块返回的是悬空引用。若不检查就继续操作，
症状是"回档跑到一半服务器崩了"。这里用两种方式处理：
  * 有可用的在线观察者玩家且开了 auto_tp → 传送过去触发区块加载；
  * 否则按失败计数，留到最终校验轮重试。

含水方块的三阶段流水线（最容易踩的坑）
--------------------------------------
Bedrock 里含水方块的水存放在**层 1**，而层 0 的状态跟干燥方块**完全
一样**，并且 API 只能读层 0。这意味着：

    block.set_type("石头")   # 同型替换时，层 1 的水会原样保留

于是"回档后树叶里还在流水"。有效的清除序列必须是：

    phase1  无物理置 air（层 1 水保留，1~2 tick 后归一化为层 0 water）
    phase2  到期检测：读到 water 说明原方块含水 → 再清一次；
            读到 air 有歧义（可能真的无水、也可能归一化还没完成）
            → 顺延复查，超过上限才判定为干
    phase3  再隔一个周期，物理复原目标方块

这三个阶段的实现见 ``_apply_target`` / ``_drain_pending``。它看起来很绕，
但每一行的存在理由都在上面，不要凭直觉删。

最终校验滚动复检
----------------
复原一个方块会在数 tick 后引发相邻液体的重新流动（水约 5 tick/格），
把刚修好的位置再冲掉。所以流处理完还会跑最多 3 轮校验：连续一轮
没有任何修正才算稳定。

人机共用
--------
本引擎不区分调用者是谁。``execute()`` / ``undo()`` 是**唯一的两个入口**，
WebUI（人）与 AI 工具（AI）走的是同一条路径、同一套配额与幂等约束 ——
区别只在谁填 ``actor`` 字段。详见 ``api.py``。
"""

from __future__ import annotations

import json
import json
import time
from typing import Any, Optional

from .store import json_to_states, states_to_json

# place 语义动作：回档时恢复 replaced_* 记录的变化前方块
_PLACE_LIKE = ("place", "liquid_flow")

# 清水流水线：phase1 置 air 后须等 BDS 把层 1 水归一化为层 0 water
# （实测 1-2 tick，密集回档可达数十 tick），固定 2 tick 会卡在归一化
# 边界误判"无水"直接复原，把层 1 水封进复原出的方块 -> 回档后继续流水
_PENDING_WAIT_TICKS = 4
# phase2 读到 air 时无法区分"层 1 无水"与"归一化未完成"，顺延复查上限；
# 总窗口 4*(12+1)=52 tick，超出窗口被密封的层 1 水由复原后观察兜底
_AIR_RECHECK_MAX = 12
# 复原后观察延迟：若复原封入了层 1 水，该方块会向邻居冒水，
# 延迟检查 6 邻居发现水即重跑清水流水线
_WATCH_DELAY = 30
# 最终校验滚动复检：修正的物理 set 需数 tick 引发新的液体回流
# （水约 5 tick/格），轮间空转留出回流时间，连续一轮无修正视为稳定
_VERIFY_MAX_ROUNDS = 3
_VERIFY_ROUND_DELAY = 10


def parse_block_ids(raw: str) -> set:
    """逗号分隔的方块 ID 串（表单输入）-> 规范化集合，无命名空间自动补 minecraft:。"""
    ids = set()
    for part in str(raw or "").replace("，", ",").split(","):
        b = part.strip().lower()
        if not b:
            continue
        if ":" not in b:
            b = f"minecraft:{b}"
        ids.add(b)
    return ids


class RollbackEngine:
    """按区域+时间窗回档（流式分批），支持撤销最近一次或任意一次会话。"""

    def __init__(self, host):
        """``host`` 是宿主适配器，需提供见 ``api.py`` 里 ``EngineHost`` 的契约。"""
        self.host = host
        self._active: dict[int, dict] = {}
        self._undo_active: dict[int, dict] = {}

    # ------------------------------------------------------------------
    # 统一的对外输出出口
    #
    # ★ 这是移植过程中**唯一被有意改动的设计**：
    #   原实现到处直接 ``p.send_message(...)``，把"进度汇报"焊死在
    #   "有个在线玩家"这个前提上。而 WebUI / AI 发起的回档根本没有
    #   对应的在线玩家 —— 那时进度就彻底丢失了，调用方只能干等。
    #   现在所有输出都从这里出去，由宿主决定去向：
    #      · 有 notify 玩家 → 游戏内消息 + 提示条
    #      · 无论如何     → 事件总线（Web 面板实时进度 / AI 工具结果 / 日志）
    # ------------------------------------------------------------------
    def _emit(self, state: dict, level: str, text, *, tip: str = "") -> None:
        """把一次产出交给宿主投递。

        ★ ``text`` 可能是字符串（进度提示），也可能是**结构化结果 dict**
          （``_finish`` 里就是直接给 dict 的）。

          这里**绝不能**写成 ``str(text)`` —— 那会把结果字典压成
          ``"{'ok': True, 'applied': 3, ...}"`` 这种 Python repr 字符串。
          下游（Web 面板 / AI 工具 / 订阅的插件）再也拿不到结构化字段，
          只能去正则解析这坨文本。这个 bug 的隐蔽之处在于：**它不报错**，
          UI 上还照样显示出一行"摘要"，显得一切正常。
        """
        try:
            self.host.emit_progress(state, str(level), text, tip=str(tip))
        except Exception:
            pass

    def _tip(self, state: dict, text: str) -> None:
        """每批次的行进式提示（原来直接调 send_tip）。"""
        self._emit(state, "progress", text, tip=text)

    def is_busy(self) -> bool:
        return bool(self._active or self._undo_active)

    def _get_notify(self, state: dict):
        """按名字重新解析玩家（跨 tick 不能持有旧引用）。"""
        name = state.get("notify")
        if not name:
            return None
        try:
            return self.host.server.get_player(name)
        except Exception:
            return None

    def execute(
        self,
        player_name: str,
        pos1: tuple,
        pos2: tuple,
        dimension: str,
        seconds: float,
        notify_name: Optional[str] = None,
        exclude_blocks: Optional[set] = None,
        only_blocks: Optional[set] = None,
        rollback_water: bool = True,
        rollback_lava: bool = True,
        #: 谁发起的：``human`` / ``ai`` / ``plugin``。 purely 用于留痕 ——
        #  执行路径与权限判定三条通道完全一致，不存在"某类调用者能
        #  多做一点"的后门。这是刻意的：**安全策略只有一处**。
        caller: str = "human",
    ) -> dict[str, Any]:
        """启动一次回档（异步分批执行，立即返回）。

        同一坐标只处理窗内最早一条记录（按 ts 正序）——最早记录的
        "变化前方块"正是回档目标时刻该坐标的状态。例：窗内先放置后
        挖掉 -> 最早是"放置"记录 -> 恢复为空气，方块不该回来。
        """
        if self.is_busy() or self.host._db_swapping:
            return {"busy": True}
        # 必须先同步移交内存缓冲再排空后台写队列：倒水/骨粉等延迟验证
        # 记录可能仍在 _queue 缓冲里，查询漏掉会表现为"源头方块没被清"
        try:
            self.host._flush()
        except Exception:
            pass
        if not self.host.wait_db_flush(timeout=10.0):
            return {"error": "数据库写入繁忙，请稍后再试"}
        cfg = self.host.cfg
        max_records = max(
            1, int(cfg.get("rollback", "max_records", default=5000))
        )
        if self._get_dimension(dimension) is None:
            return {"error": f"找不到维度 {dimension}"}

        since = time.time() - seconds
        try:
            conn, cur, total = self.host.db.stream_region(
                pos1, pos2, dimension, since,
                exclude_blocks=exclude_blocks,
                only_blocks=only_blocks,
                rollback_water=rollback_water,
                rollback_lava=rollback_lava,
                limit=max_records,
            )
        except Exception as e:
            self.host.logger.error(f"回档查询失败: {e}")
            return {"error": f"查询日志失败：{e}"}

        if total == 0:
            self.host.db._close_conn(cur, conn)
            result = {"selected": 0, "applied": 0, "truncated": False}
            if (
                exclude_blocks or only_blocks
                or not rollback_water or not rollback_lava
            ):
                result["filtered"] = True
            return result

        truncated = total > max_records
        selected = min(total, max_records)
        session_id = self.host.db.create_session(
            player_name, dimension, pos1, pos2, seconds
        )
        state = {
            "kind": "rollback",
            "session_id": session_id,
            "conn": conn,
            "cur": cur,
            "total": total,
            "selected": selected,
            "truncated": truncated,
            "i": 0,
            "applied": 0,
            "failed": 0,
            "dimension": dimension,
            "pos1": tuple(pos1),
            "pos2": tuple(pos2),
            "since_ts": since,
            "player": player_name,
            "notify": notify_name,
            "caller": str(caller or "human"),
            "batches": 0,
            "dim": None,
            "chunks": None,
            # 该坐标已处理（更早记录 = 目标时刻真实状态），大小受选区体积上界约束
            "touched": set(),
            "batch_ids": [],
            "batch_undo": [],
            # 最终校验清单 (x, y, z, 目标类型, 目标状态)：全部目标进入，
            # 兜底批次/校验间隙被残留液体回灌或异常路径失败的位置
            "verify": [],
            "vi": 0,
            "refixed": 0,
            "verify_round": 0,
            "verify_base": 0,
            "verify_wait": 0,
            # Bedrock 含水方块的水在层 1，层 0 状态与干燥方块完全相同，
            # set_type/set_data 只写层 0 -> 同型替换会保留层 1 水
            #（"回档后树叶里还有水"的根因）。有效清除序列：
            #   phase1: 无物理置 air（层 1 水保留，1-2 tick 后归一化为层 0 water）
            #   phase2: 到期检测——water 则再清转 phase3；air 有歧义
            #           （无水 vs 归一化未完成），顺延复查再复原
            #   phase3: 再隔一周期物理复原目标方块
            "tick": 0,
            "air_retries": {},
            "pending1": [],
            "pending2": [],
            "watch": [],
            "watched": set(),
            # 未加载区块延迟恢复：传送发起者到目标区块触发加载，
            # 加载完成后续跑；超时/无法传送按失败收场
            "auto_tp": bool(cfg.get("rollback", "auto_teleport", default=True))
            and notify_name is not None,
            "deferred": [],
            "tele_target": None,
            "tele_wait": 0,
            "tele_loaded": 0,
        }
        self._active[session_id] = state
        self._emit(
            state, "info",
            "回档已启动：命中 %d 条记录%s，分批执行中……" % (
                total, "（已达单次上限，超出部分本次不处理）" if truncated else "")
        )
        self._schedule(state)
        return {
            "started": True,
            "selected": selected,
            "truncated": truncated,
            "session_id": session_id,
        }

    def undo(
        self,
        player_name: str,
        any_player: bool = False,
        session_id: Optional[int] = None,
        #: 谁发起的：``human`` / ``ai`` / ``plugin``。 purely 用于留痕。
        caller: str = "human",
        notify_name: Optional[str] = None,
        allow_any: bool = False,
    ) -> Optional[dict[str, Any]]:
        """撤销一次回档会话（异步分批执行，立即返回）。

        allow_any=False（默认）时只能撤销自己执行的会话；OP/控制台
        路径必须显式传 True 才能撤销他人会话。
        """
        if self.is_busy() or self.host._db_swapping:
            return {"busy": True}
        db = self.host.db
        if session_id is not None:
            session = db.get_session(session_id)
            if session is None:
                return {"error": f"找不到回档会话 #{session_id}"}
            if not allow_any and session["player"] != player_name:
                return {"error": "只能撤销自己执行的回档会话"}
        elif any_player:
            session = db.get_last_session_any()
        else:
            session = db.get_last_session(player_name)
        if session is None:
            return None
        if session["undone"]:
            return {"error": "该回档已被撤销过，不能重复撤销"}

        # 刚结束的回档其撤销快照可能仍在后台写队列：先排空再读
        if not self.host.wait_db_flush(timeout=10.0):
            return {"error": "数据库写入繁忙，请稍后再试"}

        if self._get_dimension(session["dimension"]) is None:
            return {"error": f"找不到维度 {session['dimension']}"}
        try:
            conn, cur, total = db.stream_undo_rows(session["id"])
        except Exception as e:
            self.host.logger.error(f"撤销查询失败: {e}")
            return {"error": f"查询撤销快照失败：{e}"}

        if total == 0:
            db._close_conn(cur, conn)
            # 无快照：no-op 回档会话不产生撤销快照，仅恢复日志状态
            self.host.submit_db("finalize_undo", session["id"])
            return {"restored": 0, "failed": 0, "expected": 0, "no_snapshot": True}

        state = {
            "kind": "undo",
            "session_id": session["id"],
            "conn": conn,
            "cur": cur,
            "total": total,
            "selected": total,
            "i": 0,
            "restored": 0,
            "failed": 0,
            "dim": None,
            "dimension": session["dimension"],
            "player": player_name,
            "notify": notify_name,
            "caller": str(caller or "human"),
            "batches": 0,
            "chunks": None,
        }
        self._undo_active[session["id"]] = state
        self._emit(
            state, "info",
            "撤销已启动：共 %d 个方块，分批执行中……" % total
        )
        self._schedule(state)
        return {"started": True, "expected": total, "session_id": session["id"]}

    def _schedule(self, state: dict) -> None:
        interval = max(
            1, int(self.host.cfg.get("rollback", "batch_interval_ticks", default=1))
        )
        try:
            self.host.server.scheduler.run_task(
                self.host.host_plugin, lambda: self._step(state), delay=interval
            )
        except Exception:
            # 调度失败（如插件正在卸载）：同步走完剩余批次，保证数据一致
            self._drain(state)

    def _drain(self, state: dict) -> None:
        for _ in range(1_000_000):
            if self._step(state, schedule_next=False):
                break

    def _step(self, state: dict, schedule_next: bool = True) -> bool:
        """执行一个批次。返回 True 表示全部完成。"""
        # 关服窗口：不再触碰 dimension/server（悬空引用段错误不可捕获）
        if self.host._stopping:
            self._close_stream(state)
            self._pop_state(state)
            return True
        # 每批开始按名字重取新鲜 Dimension 引用（绝不跨 tick 复用），
        # 维度已不存在则安全终止会话
        dim = self._get_dimension(state["dimension"])
        if dim is None:
            try:
                self.host.logger.warning(
                    f"维度 {state['dimension']} 已不可用，中止"
                    f"{state['kind']}会话 #{state['session_id']}"
                )
            except Exception:
                pass
            self._close_stream(state)
            self._pop_state(state)
            return True
        state["dim"] = dim
        kind = state["kind"]
        try:
            batch = max(
                1, int(self.host.cfg.get("rollback", "blocks_per_batch", default=400))
            )
            # 区块快照刷新必须无条件执行：pending 的复原仍需区块加载
            # 检查（过期快照会漏判卸载 -> 悬空引用硬崩溃）
            self._refresh_chunks(state)
            if kind == "rollback":
                state["tick"] += 1
                self._drain_pending(state)
                if state["deferred"]:
                    if self._handle_deferred(state):
                        # 传送等待区块加载中：本批不取新记录
                        if schedule_next:
                            self._schedule(state)
                        return False
            cur = state.get("cur")
            rows = cur.fetchmany(batch) if cur is not None else []
            if rows:
                state["batches"] += 1
                for row in rows:
                    state["i"] += 1
                    if kind == "rollback":
                        self._apply_rollback_row(state, row)
                    else:
                        self._apply_undo_row(state, row)
                if kind == "rollback":
                    # 批次增量即刻移交后台落盘并释放批缓冲（核心内存约束）
                    self._flush_batch(state)
                if schedule_next:
                    if state["batches"] % 5 == 0 or state["i"] >= state["selected"]:
                        self._tip(
                            state,
                            "%s进行中 %d/%d" % (
                                "回档" if kind == "rollback" else "撤销",
                                state["i"], state["selected"])
                        )
                    self._schedule(state)
                return False

            # 流取尽：释放游标与 WAL 读快照，再进入校验/收尾
            if cur is not None:
                self._close_stream(state)
            if kind == "rollback":
                # 清水流水线未走完（phase1 已置 air）时不能收尾，
                # 否则这些方块永远停留在 air
                if state["pending1"] or state["pending2"] or state["watch"]:
                    if schedule_next:
                        self._schedule(state)
                    return False
                # 轮间空转：给校验修正引发的液体回流留出时间
                if state["verify_wait"] > 0:
                    state["verify_wait"] -= 1
                    if schedule_next:
                        self._schedule(state)
                    return False
                if state["vi"] < len(state["verify"]):
                    vend = min(state["vi"] + batch, len(state["verify"]))
                    while state["vi"] < vend:
                        self._verify_one(state, state["verify"][state["vi"]])
                        state["vi"] += 1
                    if state["vi"] < len(state["verify"]):
                        if schedule_next:
                            self._tip(
                                state,
                                "回档最终校验中 %d/%d" % (
                                    state["vi"], len(state["verify"]))
                            )
                            self._schedule(state)
                        return False
                # 本轮跑完：有修正且未达上限 -> 空转后滚动再验一轮
                if (
                    state["refixed"] > state["verify_base"]
                    and state["verify_round"] < _VERIFY_MAX_ROUNDS - 1
                ):
                    state["verify_round"] += 1
                    state["verify_base"] = state["refixed"]
                    state["vi"] = 0
                    state["verify_wait"] = _VERIFY_ROUND_DELAY
                    if schedule_next:
                        self._schedule(state)
                    return False
                state["verify"] = []
                self._finish(state)
                return True
            self._finish(state)
            return True
        except Exception as e:
            # 出错也要收尾，避免会话悬空（未 finalize 的会话可再次回档）
            self._close_stream(state)
            # 兜底：流水线里的方块已被 phase1 置 air，尽力同步复原
            try:
                for item in list(state.get("pending1", [])) + list(
                    state.get("pending2", [])
                ):
                    born, x, y, z, ttype, tstates = item
                    block = self._get_block(state, x, y, z)
                    if block is not None:
                        self._apply(block, ttype, tstates)
            except Exception:
                pass
            try:
                self._finish(state)
            except Exception:
                pass
            self._pop_state(state)
            self.host.logger.error(f"回档/撤销批次执行失败: {e}")
            self._emit(state, "error", "回档/撤销过程出错，已提前收尾")
            return True

    def _finish(self, state: dict) -> None:
        """会话收尾：写库、结清会话表、产出一份**结构化结果**。

        ★ 移植要点
        ----------
        原实现在这里把结果拼成一坨带 ``§`` 颜色码的字符串，然后三选一
        发出去：有玩家就发消息、没玩家就打日志、**返回值丢了**。
        WebUI / AI 调用时根本没有"发消息"这个出口，只能去看日志。

        现在改成：先组装一份**机器可读**的 ``result`` 字典，再从中派生
        出给人看的一句话摘要。``_emit`` 只负责投递，调用方（Web 会话
        / AI 工具）通过各自的通道拿到同一份结构化结果。
        """
        kind = state["kind"]
        session_id = state["session_id"]
        now = time.strftime("%H:%M:%S")
        actor = str(state.get("player") or "") or "(系统)"
        if kind == "rollback":
            # 收尾走后台队列：FIFO 保证在所有批次写完之后执行
            self.host.submit_db(
                "finalize_session", (session_id, state["applied"])
            )
            self._active.pop(session_id, None)
            state["verify"] = []
            state["pending1"] = []
            state["pending2"] = []
            state["air_retries"] = {}
            state["watch"] = []
            state["watched"] = set()
            # 原实现在这里调 selection_off 退出"选区模式"。Web 化的新版
            # 没有"手持木斧选区"这个概念 —— 区域是调用方显式给出的
            # 一组坐标，回档结束即失效，不需要也不应该去动别的状态。
            try:
                cleanup_scheduled = self._cleanup_drops(state)
            except Exception as e:
                self.host.logger.warning(f"掉落物清理失败: {e}")
                cleanup_scheduled = False
            applied, failed, refixed = (
                int(state["applied"]), int(state["failed"]), int(state.get("refixed") or 0))
            selected, total = int(state["selected"]), int(state["total"])
            tele_loaded = int(state.get("tele_loaded") or 0)

            # ---- 全程无实际恢复：目标是"已经就是那个样子" ----
            if applied == 0 and failed == 0 and not refixed:
                self.host.write_daily(
                    "rollback",
                    f"[{now}] {actor} 执行了回档（会话 #{session_id}）："
                    f"无可恢复操作，命中 {total} 条记录均与现状一致"
                    + ("，掉落物清理已排定" if cleanup_scheduled else ""),
                    actor=actor,
                )
                self._emit(state, "no_change", {
                    "ok": True, "changed": False, "session_id": session_id,
                    "actor": actor, "caller": state.get("caller", "human"),
                    "summary": "没有可恢复的操作：区域内方块与该时间点状态一致",
                    "records_hit": total, "records_processed": selected,
                    "applied": 0, "failed": 0, "refixed": 0,
                    "tele_loaded": tele_loaded,
                    "drops_cleanup_scheduled": bool(cleanup_scheduled),
                })
                return

            extra = f"，修正 {refixed} 处批次间隙回流的液体" if refixed else ""
            if tele_loaded:
                extra += f"，传送加载区块后处理 {tele_loaded} 个方块"
            if cleanup_scheduled:
                extra += "，掉落物清理已排定（稍后执行）"
            self.host.write_daily(
                "rollback",
                f"[{now}] {actor} 执行了回档（会话 #{session_id}）："
                f"恢复 {applied} 个方块，失败 {failed} 个"
                f"（命中 {total} 条记录，处理 {selected} 条）{extra}",
                actor=actor,
            )
            self._emit(state, "done", {
                "ok": True, "changed": True, "session_id": session_id,
                "actor": actor, "caller": state.get("caller", "human"),
                "summary": (
                    f"回档完成：恢复 {applied} 个方块，"
                    f"失败 {failed} 个（处理 {selected} 条记录）"),
                "applied": applied, "failed": failed, "refixed": refixed,
                "records_hit": total, "records_processed": selected,
                "tele_loaded": tele_loaded,
                "drops_cleanup_scheduled": bool(cleanup_scheduled),
                "undo_hint": f"如需撤销本次回档，请针对会话 #{session_id} 执行撤销",
            })
        else:
            self.host.submit_db("finalize_undo", session_id)
            self._undo_active.pop(session_id, None)
            restored, failed = int(state["restored"]), int(state["failed"])
            self.host.write_daily(
                "undo",
                f"[{now}] {actor} 撤销了回档 #{session_id}："
                f"恢复 {restored} 个方块（失败 {failed}）",
                actor=actor,
            )
            self._emit(state, "done", {
                "ok": True, "kind": "undo", "session_id": session_id,
                "actor": actor, "caller": state.get("caller", "human"),
                "summary": (
                    f"已撤销回档 #{session_id}：恢复 {restored} 个方块"
                    f"（失败 {failed}）。相关日志已恢复为未回档状态，可再次回档。"),
                "restored": restored, "failed": failed,
            })

    def _notify_done(self, p, title: str, msg: str, count: int) -> None:
        """保留给内部逻辑引用的兼容钩子。

        ★ 不再承担实际投递职责 —— 收尾通知统一走 ``_emit`` / 事件总线，
          由宿主决定要不要给在线玩家补一条带音效的通知。
          空实现而不是删掉，是为了让 `_finish` 的改写面尽量小。
        """
        return None

    def _apply_rollback_row(self, state: dict, row) -> None:
        state["batch_ids"].append(row["id"])
        pos = (row["x"], row["y"], row["z"])
        if pos in state["touched"]:
            return  # 已由更早记录恢复（更早记录 = 目标时刻真实状态）
        state["touched"].add(pos)
        action = row["action"]
        if action in _PLACE_LIKE:
            target_type = row["replaced_type"] or "minecraft:air"
            target_states = row["replaced_states"] or "{}"
        else:  # break / explode / leaves_decay / block_grow / block_form
            target_type = row["block_type"]
            target_states = row["block_states"] or "{}"

        liquid_or_air = (
            target_type == "minecraft:air"
            or "water" in target_type
            or "lava" in target_type
        )

        # 改动前捕获现场快照（随批后台落盘），供撤销
        block = self._get_block(state, pos[0], pos[1], pos[2])
        # no-op 快速路径：已是目标状态则跳过。仅空气/液体目标可安全
        # 跳过——实体方块层 1 可能含水且 API 不可读，同型跳过会把水
        # 封进方块，必须走 pending 清水流水线
        if (
            block is not None
            and liquid_or_air
            and self._matches(block, target_type, target_states)
        ):
            return
        # 目标进最终校验清单：区块未加载的失败位置留待校验轮重试
        state["verify"].append(
            (pos[0], pos[1], pos[2], target_type, target_states)
        )
        if block is None:
            if self._can_defer(state, pos[0], pos[2]):
                state["deferred"].append(
                    (pos[0], pos[1], pos[2], target_type, target_states)
                )
            else:
                state["failed"] += 1
            return
        self._apply_target(state, block, pos, target_type, target_states)

    def _can_defer(self, state: dict, x: int, z: int) -> bool:
        """未加载区块是否可延迟恢复（需开启自动传送且有通知玩家）。"""
        chunks = state.get("chunks")
        if (
            not state.get("auto_tp")
            or not state.get("notify")
            or chunks is None
            or (x >> 4, z >> 4) in chunks
        ):
            return False
        return True

    def _apply_target(
        self, state: dict, block, pos: tuple,
        target_type: str, target_states: str,
    ) -> None:
        """快照 + 按目标类型应用（直接 set 或清水流水线）。"""
        try:
            state["batch_undo"].append((
                state["session_id"], pos[0], pos[1], pos[2],
                state["dimension"], block.type,
                states_to_json(block.data.block_states),
            ))
        except Exception:
            pass

        liquid_or_air = (
            target_type == "minecraft:air"
            or "water" in target_type
            or "lava" in target_type
        )
        if liquid_or_air:
            # 直接应用：若当前方块含水，层 1 水随后归一化为层 0 water，
            # 与目标不符 -> 最终校验复检时重放即可清掉
            if self._apply(block, target_type, target_states):
                state["applied"] += 1
            else:
                state["failed"] += 1
        else:
            # 实体方块目标：直接 set_data/set_type 是层 0 同型替换，
            # 当前方块若含水则层 1 水原样保留 -> 必须走 phase1 清水序列：
            # 先无物理置 air 暴露层 1 水，归一化后由 _drain_pending 清除复原
            state["pending1"].append((
                state["tick"], pos[0], pos[1], pos[2],
                target_type, target_states,
            ))
            try:
                block.set_type("minecraft:air", False)
                state["applied"] += 1
            except Exception:
                state["failed"] += 1

    def _matches(self, block, target_type: str, target_states: str) -> bool:
        """当前方块是否已是目标类型+状态。宁误判为不匹配（代价只是
        多执行一次等效 set，方向安全）。"""
        try:
            if block.type != target_type:
                return False
            states = json_to_states(target_states)
            if not states:
                return True
            cur = {k: str(v) for k, v in block.data.block_states.items()}
            return cur == states
        except Exception:
            return False

    def _handle_deferred(self, state: dict) -> bool:
        """处理未加载区块的延迟项：已加载的立即恢复，未加载的传送等待。
        返回 True 表示仍在等待（本批不取新记录）。"""
        chunks = state["chunks"]
        if chunks is None:
            state["failed"] += len(state["deferred"])
            state["deferred"] = []
            return False
        rest = []
        for item in state["deferred"]:
            x, y, z, ttype, tstates = item
            if (x >> 4, z >> 4) not in chunks:
                rest.append(item)
                continue
            if not self._process_deferred_item(state, x, y, z, ttype, tstates):
                rest.append(item)
        state["deferred"] = rest
        if not rest:
            state["tele_target"] = None
            state["tele_wait"] = 0
            return False
        p = self._get_notify(state)
        if p is None:
            # 发起者已离线：无法传送，剩余按失败收场
            state["failed"] += len(rest)
            state["deferred"] = []
            state["tele_target"] = None
            return False
        unloaded = {(x >> 4, z >> 4) for x, y, z, *_ in rest}
        target = state.get("tele_target")
        if target is not None and target in unloaded:
            if state["tele_wait"] > 0:
                state["tele_wait"] -= 1
                return True
            # 等待超时：剩余按失败收场，回档继续
            state["failed"] += len(rest)
            state["deferred"] = []
            state["tele_target"] = None
            return False
        # 新一轮传送：未加载区块集合的质心（一次传送可覆盖视距内多个区块）
        cx = sum(c[0] for c in unloaded) // len(unloaded)
        cz = sum(c[1] for c in unloaded) // len(unloaded)
        if not self._teleport_for_load(state, p, cx, cz, rest):
            state["failed"] += len(rest)
            state["deferred"] = []
            state["tele_target"] = None
            return False
        state["tele_target"] = (cx, cz)
        state["tele_wait"] = max(
            20, int(self.host.cfg.get(
                "rollback", "teleport_wait_ticks", default=200
            ))
        )
        return True

    def _process_deferred_item(
        self, state: dict, x: int, y: int, z: int,
        target_type: str, target_states: str,
    ) -> bool:
        """区块加载后恢复单个延迟项（与常规行同逻辑：no-op 检查 + 快照）。
        返回 False 表示方块仍不可用。"""
        liquid_or_air = (
            target_type == "minecraft:air"
            or "water" in target_type
            or "lava" in target_type
        )
        block = self._get_block(state, x, y, z)
        if block is None:
            return False
        state["tele_loaded"] += 1
        if liquid_or_air and self._matches(block, target_type, target_states):
            return True
        self._apply_target(state, block, (x, y, z), target_type, target_states)
        return True

    def _teleport_for_load(
        self, state: dict, p, cx: int, cz: int, items: list,
    ) -> bool:
        """传送玩家到未加载区块触发加载。区块是 16x16 立柱，落点取
        区块中心的地表高度+1（高度图查询失败退回该批最高记录 y+2）。"""
        dim = state.get("dim")
        if dim is None:
            return False
        tx = cx * 16 + 8.5
        tz = cz * 16 + 8.5
        ty = None
        try:
            h = dim.get_highest_block_y_at(int(tx), int(tz))
            if -64 <= h <= 500:
                ty = h + 1
        except Exception:
            ty = None
        if ty is None:
            ys = [y for _, y, _, *_ in items]
            ty = max(ys) + 2 if ys else 64
        try:
            if not p.teleport(Location(dim, tx, ty, tz)):
                return False
        except Exception:
            return False
        note = (
            f"部分区块未加载，已将 {state.get('notify') or '观察者'} 传送到 "
            f"({int(tx)}, {ty}, {int(tz)}) 触发区块加载，回档将在区块加载后继续"
        )
        # 传送需要真人在线；消息本身则一律进事件总线，好让 WebUI 上
        # 看到"为什么这一批慢了"，而不是只看到进度卡住。
        try:
            p = self._get_notify(state)
            if p is not None:
                self.host.notify_player(p, note)
        except Exception:
            pass
        self._emit(state, "progress", note)
        return True

    def _apply_undo_row(self, state: dict, row) -> None:
        block = self._get_block(state, row["x"], row["y"], row["z"])
        if block is None:
            state["failed"] += 1
        elif self._apply(block, row["block_type"], row["block_states"]):
            state["restored"] += 1
        else:
            state["failed"] += 1

    def _apply(self, block, target_type: str, target_states: str) -> bool:
        """恢复为目标类型+状态；优先精确状态，失败退回 set_type（单条兜底）。"""
        server = self.host.server
        try:
            states = json_to_states(target_states)
            if states:
                block.set_data(server.create_block_data(target_type, states))
            else:
                # 无状态记录时按类型恢复（物理更新触发液体回流处理）
                block.set_type(target_type, True)
            return True
        except Exception:
            try:
                block.set_type(target_type, True)
                return True
            except Exception:
                return False

    def _drain_pending(self, state: dict) -> None:
        """清水流水线的延迟阶段（每 tick 与批次交错，未到期项原样留队）。

        pending2（已清水）：到期复原目标方块。
        pending1（已置 air）：到期检测——water 说明原方块含水，再清
        转 pending2；air 有歧义，顺延复查（上限 _AIR_RECHECK_MAX）才
        判干复原；其他实体方块是被新方块占据，直接以目标覆盖复原。
        Block 引用同一回调内获取、使用、丢弃，绝不跨 tick。
        """
        # phase3：复原（先处理更早批次的 pending2，保证 FIFO）
        if state["pending2"]:
            rest = []
            for item in state["pending2"]:
                born, x, y, z, ttype, tstates = item
                if state["tick"] - born < _PENDING_WAIT_TICKS:
                    rest.append(item)
                    continue
                block = self._get_block(state, x, y, z)
                if block is None:
                    state["failed"] += 1
                elif not self._apply(block, ttype, tstates):
                    state["failed"] += 1
            state["pending2"] = rest
        # phase2：检测并清除归一化水
        if state["pending1"]:
            retries = state["air_retries"]
            rest = []
            for item in state["pending1"]:
                born, x, y, z, ttype, tstates = item
                if state["tick"] - born < _PENDING_WAIT_TICKS:
                    rest.append(item)
                    continue
                block = self._get_block(state, x, y, z)
                if block is None:
                    state["failed"] += 1
                    retries.pop((x, y, z), None)
                    continue
                try:
                    btype = block.type
                except Exception:
                    state["failed"] += 1
                    retries.pop((x, y, z), None)
                    continue
                if btype == "minecraft:water" or btype == "minecraft:flowing_water":
                    # 原方块含水：清掉归一化到层 0 的水，再复原
                    retries.pop((x, y, z), None)
                    try:
                        block.set_type("minecraft:air", False)
                        state["pending2"].append((
                            state["tick"], x, y, z, ttype, tstates,
                        ))
                    except Exception:
                        # 清水失败：直接复原（含水残留风险，但比停在 air 强）
                        if not self._apply(block, ttype, tstates):
                            state["failed"] += 1
                elif btype == "minecraft:air":
                    # 顺延复查：直接复原会把层 1 水封进复原出的方块
                    n = retries.get((x, y, z), 0)
                    if n < _AIR_RECHECK_MAX:
                        retries[(x, y, z)] = n + 1
                        rest.append((state["tick"], x, y, z, ttype, tstates))
                    else:
                        retries.pop((x, y, z), None)
                        if not self._apply(block, ttype, tstates):
                            state["failed"] += 1
                        elif (x, y, z) not in state["watched"]:
                            # 复原若封入层 1 水会向邻居冒水，交给观察兜底
                            state["watched"].add((x, y, z))
                            state["watch"].append((
                                state["tick"], x, y, z, ttype, tstates,
                            ))
                else:
                    # 被回灌以外的新方块占据，直接以目标覆盖复原
                    retries.pop((x, y, z), None)
                    if not self._apply(block, ttype, tstates):
                        state["failed"] += 1
            state["pending1"] = rest
        # 复原后观察：邻居冒水 = 复原可能封入层 1 水，重跑流水线二次甄别
        #（watched 已含该坐标，不会循环；边界正常来水误报的代价只是
        # 一次流水线往返）
        if state["watch"]:
            wrest = []
            for item in state["watch"]:
                born, x, y, z, ttype, tstates = item
                if state["tick"] - born < _WATCH_DELAY:
                    wrest.append(item)
                    continue
                block = self._get_block(state, x, y, z)
                if block is None:
                    continue  # 区块卸载：放弃观察
                try:
                    if block.type != ttype:
                        continue  # 复原后又被改动：不再归因
                except Exception:
                    continue
                hit = False
                for dx, dy, dz in (
                    (1, 0, 0), (-1, 0, 0), (0, 1, 0),
                    (0, -1, 0), (0, 0, 1), (0, 0, -1),
                ):
                    nb = self._get_block(state, x + dx, y + dy, z + dz)
                    if nb is None:
                        continue
                    try:
                        if (
                            nb.type == "minecraft:water"
                            or nb.type == "minecraft:flowing_water"
                        ):
                            hit = True
                            break
                    except Exception:
                        continue
                if hit:
                    try:
                        block.set_type("minecraft:air", False)
                        state["pending1"].append((
                            state["tick"], x, y, z, ttype, tstates,
                        ))
                    except Exception:
                        if not self._apply(block, ttype, tstates):
                            state["failed"] += 1
            state["watch"] = wrest

    def _verify_one(self, state: dict, item: tuple) -> None:
        """最终校验单个坐标：与目标不符（如液体回流）则重新应用。"""
        x, y, z, target_type, target_states = item
        block = self._get_block(state, x, y, z)
        if block is None:
            return
        try:
            if block.type == target_type:
                return
            if self._apply(block, target_type, target_states):
                state["refixed"] += 1
        except Exception:
            pass

    def _cleanup_drops(self, state: dict) -> bool:
        """清理回档区域内"被回档破坏产生的"残留掉落物（延迟执行）。

        用破坏/爆炸时采集的掉落物指纹（drop_snapshot）匹配现存掉落物。
        保守策略（宁少清勿误清）：位置在选区内 + 距指纹 3 格内 + 类型一致。
        崩溃安全：扫描延迟 3 tick 避开 BDS 掉落物堆数据迁移窗口；
        item_stack 只对"位置+指纹"双命中的候选读取；移除前检查 is_valid。
        返回 True 表示清理任务已排定。
        """
        cfg = self.host.cfg
        if not bool(cfg.get("rollback", "cleanup_drops", default=True)):
            return False
        conn, cur = self.host.db.stream_drop_snapshots(
            state["pos1"], state["pos2"],
            state["dimension"], state["since_ts"],
        )
        item_types: set[str] = set()
        # 指纹空间索引：4 格桶 -> 指纹点列表（近邻判定只查 27 桶）
        grid: dict[tuple[int, int, int], list[tuple[float, float, float]]] = {}
        try:
            while True:
                rows = cur.fetchmany(256)
                if not rows:
                    break
                for r in rows:
                    try:
                        drops = json.loads(r["detail"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(drops, list):
                        continue
                    for d in drops:
                        if not isinstance(d, dict) or "id" not in d:
                            continue
                        item_types.add(d["id"])
                        try:
                            px, py, pz = (
                                float(d["x"]), float(d["y"]), float(d["z"]),
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
                        grid.setdefault(
                            (int(px) // 4, int(py) // 4, int(pz) // 4), []
                        ).append((px, py, pz))
        finally:
            self.host.db._close_conn(cur, conn)
        if not grid or not item_types:
            return False

        x1, x2 = sorted((state["pos1"][0], state["pos2"][0]))
        y1, y2 = sorted((state["pos1"][1], state["pos2"][1]))
        z1, z2 = sorted((state["pos1"][2], state["pos2"][2]))
        dim_name = state["dimension"]
        session_id = state["session_id"]
        player_name = state["player"]
        notify_name = state.get("notify")
        # Endstone <0.11.8 物品堆读取有段错误缺陷（#469，0.11.8 修复），
        # 旧版本自动跳过类型校验（宁少校验不崩服）
        verify_type = bool(cfg.get("rollback", "drop_verify_type", default=True))
        if not getattr(self.host, "item_stack_safe", True):
            verify_type = False

        def sweep() -> None:
            if self.host._stopping:
                return
            dim = self._get_dimension(dim_name)
            if dim is None:
                return
            removed = 0
            for actor in list(dim.actors):
                try:
                    if actor.type != "minecraft:item":
                        continue
                    if not actor.is_valid():
                        continue
                    loc = actor.location
                    if not (x1 - 1 <= loc.x <= x2 + 1
                            and y1 - 1 <= loc.y <= y2 + 1
                            and z1 - 1 <= loc.z <= z2 + 1):
                        continue
                    # 必须落在某条指纹 3 格内，防误清合法丢弃物
                    if not self._near_fingerprint(grid, loc):
                        continue
                    # 最后才读 item_stack：把堆读取风险压缩到双命中候选
                    if verify_type:
                        item = actor.item_stack
                        if item.type.id not in item_types:
                            continue
                    actor.remove()
                    removed += 1
                except Exception:
                    continue
            if removed <= 0:
                return
            note = f"会话 #{session_id} 掉落物清理完成：移除 {removed} 个残留掉落物"
            self.host.write_daily(
                "rollback",
                f"[{now}] {note}",
                actor=player_name,
            )
            if notify_name:
                try:
                    p = self.host.server.get_player(notify_name)
                except Exception:
                    p = None
                if p is not None:
                    try:
                        self.host.notify_player(p, note)
                    except Exception:
                        pass
            # 无论有没有在线观察者都记一条 —— WebUI / AI 需要知道
            # 清理确实发生了，否则"掉了东西却消失了"会变成悬案。
            self._emit({"session_id": session_id}, "done", note)

        try:
            self.host.server.scheduler.run_task(self.host.host_plugin, sweep, delay=3)
        except Exception as e:
            self.host.logger.warning(f"掉落物清理任务排定失败: {e}")
            return False
        return True

    @staticmethod
    def _near_fingerprint(
        grid: dict[tuple[int, int, int], list[tuple[float, float, float]]],
        loc,
    ) -> bool:
        """掉落物坐标是否落在某条指纹点 3 格内（空间索引近邻查询）。"""
        bx, by, bz = int(loc.x) // 4, int(loc.y) // 4, int(loc.z) // 4
        for cx in (bx - 1, bx, bx + 1):
            for cy in (by - 1, by, by + 1):
                for cz in (bz - 1, bz, bz + 1):
                    pts = grid.get((cx, cy, cz))
                    if not pts:
                        continue
                    for px, py, pz in pts:
                        if (abs(loc.x - px) <= 3 and abs(loc.y - py) <= 3
                                and abs(loc.z - pz) <= 3):
                            return True
        return False

    def is_in_active_region(self, x: int, y: int, z: int, dimension: str) -> bool:
        """坐标是否在某次进行中回档的选区内（供监听器抑制回档自身的
        物理副作用，避免瞬态变化污染日志；回档结束有最终校验兜底）。"""
        for st in self._active.values():
            if st.get("dimension") != dimension:
                continue
            x1, x2 = sorted((st["pos1"][0], st["pos2"][0]))
            y1, y2 = sorted((st["pos1"][1], st["pos2"][1]))
            z1, z2 = sorted((st["pos1"][2], st["pos2"][2]))
            if x1 <= x <= x2 and y1 <= y <= y2 and z1 <= z <= z2:
                return True
        return False

    def _get_dimension(self, name: str) -> Optional[Dimension]:
        try:
            return self.host.server.level.get_dimension(name)
        except Exception:
            return None

    def _refresh_chunks(self, state: dict) -> None:
        """刷新已加载区块快照（每批一次）。API 不可用时退化为不检查。"""
        try:
            state["chunks"] = {(c.x, c.z) for c in state["dim"].loaded_chunks}
        except Exception:
            state["chunks"] = None

    def _get_block(self, state: dict, x: int, y: int, z: int):
        """获取方块引用；区块未加载返回 None（防悬空引用硬崩溃）。"""
        chunks = state.get("chunks")
        if chunks is not None and (x >> 4, z >> 4) not in chunks:
            return None
        try:
            return state["dim"].get_block_at(x, y, z)
        except Exception:
            return None

    def _flush_batch(self, state: dict) -> None:
        """本批的会话关联与撤销快照移交后台写队列，随即释放批缓冲。"""
        ids = state["batch_ids"]
        undo = state["batch_undo"]
        if ids:
            self.host.submit_db("session_logs", (state["session_id"], ids))
        if undo:
            self.host.submit_db("undo_rows", undo)
        state["batch_ids"] = []
        state["batch_undo"] = []

    def _close_stream(self, state: dict) -> None:
        """释放流式游标与只读连接（幂等）。"""
        cur = state.pop("cur", None)
        conn = state.pop("conn", None)
        if cur is None and conn is None:
            return
        self.host.db._close_conn(cur, conn)

    def _pop_state(self, state: dict) -> None:
        active = self._active if state["kind"] == "rollback" else self._undo_active
        active.pop(state["session_id"], None)
