"""AI Agent 巡检模块。

把 AI 从「被动聊天」升级成「主动 agent」：

* **定时巡检**：按配置间隔（默认 10 分钟）检查服务器有没有问题
  （区块日志里的破坏集群、TPS 骤降等）。
* **做一步 → 核验 → 写报告**：对发现的问题，先做一个安全动作
  （例如对疑似炸服区域做一次只读 ``preview`` 拿到影响面），核验结果，
  再把结构化的中文报告发到 QQ 主群。默认**只报告、不自动改写世界**；
  只有开启 ``auto_fix`` 且 AI 接管级别 ≥ 2 才会真正动手。
* **腐竹任务入口**：QQ 腐竹消息 / 游戏内指令现已统一交给 LLM 智能体
  处理（``ai_manager`` + 自带 ``rollback_region`` / ``ban_player`` 等工具），
  不再经过 ``qq_agent`` 的确定性关键词解析。本模块只负责**巡检**与
  **核验**，以及复用 ``qq_agent.is_player_actor`` 把方块/实体挡在"责任人"
  之外。

安全边界（和现有 AI 接管级别一致）
----------------------------------
* 巡检默认不自动回档 / 封禁，只发"发现 + 建议"。
* 真正改写世界（auto_fix 回档）需要 ``auto_fix=True`` **且**
  ``ai_manager.autonomy() >= 2``，二者缺一不可。
* 所有动作都经 ``gm`` 官方 API 并留痕（actor），不会绕过权限。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "check_interval_min": 10,
    "report_to_group": True,
    "auto_fix": False,
    "grief_event_threshold": 20,   # 单个 actor 在巡检窗口内的破坏事件数阈值
    "tps_warn": 18.0,              # TPS 低于此值报警
    "dedup_minutes": 30,           # 同一责任人重复上报的最小间隔
}

_GRIEF_ACTIONS = ("explode", "break")

#: 报告里给腐竹看的动作中文名。不翻译的话群里会出现
#: 「近期 86 次 explode 事件」这种中英混排。
_ACTION_CN = {"explode": "爆炸", "break": "破坏"}

#: Endstone 的 ``scheduler.run_task`` 里 delay / period 的单位是**服务器 tick**，
#: 不是秒（官方文档：“starting after the specified number of server ticks”，
#: 20 tick ≈ 1 秒）。这里统一换算，别再把秒直接喂进去。
_TICKS_PER_SEC = 20


def _cfg(plugin: Any) -> Dict[str, Any]:
    try:
        raw = plugin._load_config() or {}
        sub = raw.get("agent") or {}
        if isinstance(sub, dict):
            return sub
    except Exception:
        pass
    return {}


def _save_cfg(plugin: Any, payload: Dict[str, Any]) -> None:
    try:
        plugin._save_agent_config(payload)
    except Exception as e:  # pragma: no cover
        try:
            plugin.logger.warning(f"[agent] 保存配置失败: {e}")
        except Exception:
            pass


def _send_report(plugin: Any, text: str) -> None:
    if not _cfg(plugin).get("report_to_group", True):
        return
    try:
        from gm import bot as _bot
        cid = _bot.primary_id()
        if cid:
            _bot.send_text(cid, str(text))
    except Exception as e:  # pragma: no cover
        try:
            plugin.logger.warning(f"[agent] 群发报告失败: {e}")
        except Exception:
            pass


class AgentMonitor:
    """Agent 巡检调度器。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._handle = None
        #: actor -> (上次上报时的事件数, 上次上报的时间戳)。
        #: 用于抑制"同一个责任人反复刷同一条报告"。
        self._grief_seen: Dict[str, tuple] = {}

    # -- 生命周期 ----------------------------------------------------------
    def start(self) -> None:
        cfg = _cfg(self.plugin)
        if not cfg.get("enabled", _DEFAULTS["enabled"]):
            return
        interval = max(1, int(cfg.get("check_interval_min",
                                      _DEFAULTS["check_interval_min"])))
        # ★ delay / period 的单位是 **tick**（20 tick ≈ 1 秒），不是秒。
        #
        #   原来写的是 delay=interval*30 / period=interval*60，注释里按“秒”
        #   理解，实际被 Endstone 当成 tick：10 分钟的配置变成 600 tick
        #   ＝ 30 秒。于是巡检每半分钟跑一次，同一个苦力怕的同一批爆炸
        #   记录在几分钟里被反复上报（截图里连发三条就是这个原因）。
        #   这里显式乘 20，并且把换算集中在这一处，避免以后再踩。
        delay_ticks = max(1, int(interval * 30 * _TICKS_PER_SEC))
        period_ticks = max(_TICKS_PER_SEC, int(interval * 60 * _TICKS_PER_SEC))
        try:
            self._handle = self.plugin.server.scheduler.run_task(
                self.plugin, self.scan, delay=delay_ticks, period=period_ticks)
            self.plugin.logger.info(
                f"[agent] 巡检已启动，间隔 {interval} 分钟"
                f"（{period_ticks} tick，首次延迟 {delay_ticks} tick）")
        except Exception as e:  # pragma: no cover
            self.plugin.logger.warning(f"[agent] 无法注册巡检任务: {e}")

    def stop(self) -> None:
        try:
            if self._handle is not None and hasattr(self._handle, "cancel"):
                self._handle.cancel()
        except Exception:
            pass
        self._handle = None

    # -- 巡检 --------------------------------------------------------------
    def scan(self) -> None:
        try:
            issues = self._detect()
        except Exception as e:  # pragma: no cover
            self.plugin.logger.warning(f"[agent] 巡检检测异常: {e}")
            return
        if not issues:
            return
        report = self._compose(issues)
        _send_report(self.plugin, report)

    def _detect(self) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        issues.extend(self._detect_grief())
        issues.extend(self._detect_tps())
        return issues

    def _detect_grief(self) -> List[Dict[str, Any]]:
        try:
            from gm import region as _region
        except Exception:
            return []
        threshold = int(_cfg(self.plugin).get(
            "grief_event_threshold", _DEFAULTS["grief_event_threshold"]))
        # 长期运行的服会不断换人，去重表要能自己收口，否则只增不减。
        if len(self._grief_seen) > 64:
            cutoff = time.time() - 4 * 3600
            self._grief_seen = {
                k: v for k, v in self._grief_seen.items() if v[1] >= cutoff}
        by_actor: Dict[str, list] = {}
        for action in _GRIEF_ACTIONS:
            try:
                rows = _region.recent(action=action, limit=150)
            except Exception:
                continue
            for r in rows:
                a = str(r.get("actor") or r.get("player") or "").strip()
                if a:
                    by_actor.setdefault(a, []).append(r)
        out: List[Dict[str, Any]] = []
        # ★ 2026-05 修复（Bug #4/#5 的另一半）：与 qq_agent._find_grief 同因 ——
        #   爆炸记录的 actor 是肇事物本身（minecraft:tnt / "Block of TNT" / 床），
        #   不是玩家。不过滤的话，巡检报告会写成「玩家「Block of TNT」近期 N 次
        #   explode 事件」，把一个方块当成破坏者上报，还可能触发对它的自动回档
        #   处置。这里统一用同一个判定函数把非玩家挡掉。
        try:
            from .. import qq_agent as _qa
            _is_player = _qa.is_player_actor
        except Exception:  # pragma: no cover
            def _is_player(a: str) -> bool:
                s = str(a or "").strip()
                return bool(s) and not s.startswith("(") and ":" not in s
        for actor, rows in by_actor.items():
            if not _is_player(actor):
                continue
            if len(rows) < threshold:
                continue
            # 信任的人不报
            try:
                from gm import trust as _trust
                if _trust.is_trusted_person(actor):
                    continue
            except Exception:
                pass
            # ★ 去重：同一个责任人，冷却期内且事态没有明显恶化，就不再重复报。
            #
            #   为什么需要它（即使周期已经修对）：一条「86 次爆炸」的记录会在
            #   时间窗里挂很久，只要没人清理，每轮巡检都会重新算一遍、重新发
            #   一遍，群里就是刷屏。而"什么时候值得再报一次"其实有客观标准 ——
            #   破坏量翻倍了才叫恶化；数量不变说明还是那批旧账，没必要提醒第二遍。
            count = len(rows)
            now = time.time()
            dedup_min = int(_cfg(self.plugin).get(
                "dedup_minutes", _DEFAULTS["dedup_minutes"]))
            dedup_sec = max(60, dedup_min * 60)
            prev = self._grief_seen.get(actor)
            if prev is not None:
                prev_count, prev_ts = prev
                if (now - prev_ts) < dedup_sec and count < prev_count * 2:
                    continue
            self._grief_seen[actor] = (count, now)
            xs = [int(r.get("x", 0) or 0) for r in rows]
            ys = [int(r.get("y", 0) or 0) for r in rows]
            zs = [int(r.get("z", 0) or 0) for r in rows]
            if not xs:
                continue
            pad = 8
            bbox = {
                "p1": (min(xs) - pad, max(min(ys) - pad, 0), min(zs) - pad),
                "p2": (max(xs) + pad, max(ys) + pad, max(zs) + pad),
            }
            out.append({
                "type": "grief",
                "actor": actor,
                "count": len(rows),
                "bbox": bbox,
                "action": rows[0].get("action", "break"),
            })
        return out

    def _detect_tps(self) -> List[Dict[str, Any]]:
        warn = float(_cfg(self.plugin).get("tps_warn", _DEFAULTS["tps_warn"]))
        try:
            from gm import admin as _admin
            snap = _admin.snapshot() if hasattr(_admin, "snapshot") else {}
        except Exception:
            return []
        tps = snap.get("tps")
        if isinstance(tps, (int, float)) and tps < warn:
            return [{"type": "tps", "tps": tps}]
        return []

    # -- 报告合成（做一步 → 核验 → 写报告）-------------------------------
    def _compose(self, issues: List[Dict[str, Any]]) -> str:
        parts = ["【GreenMoon 巡检报告】",
                 time.strftime("%Y-%m-%d %H:%M", time.localtime())]
        auto = self._may_auto_fix()
        for it in issues:
            if it["type"] == "grief":
                # 用「操作者」而不是「玩家」：区块日志里的 actor 是"这条记录
                # 由谁造成"，理论上可能是玩家，也可能是我们还没收录的新实体。
                # 说成"玩家 X"等于替它下了身份结论，一旦漏判就是诬告；
                # 说"操作者 X"只陈述事实，腐竹自己一看名字就知道是不是人。
                act = _ACTION_CN.get(str(it.get("action") or ""),
                                     str(it.get("action") or "破坏"))
                line = (f"· 发现疑似破坏：操作者「{it['actor']}」"
                        f"近期 {it['count']} 次{act}事件")
                step = self._safe_preview(it["bbox"])
                if step:
                    line += f"，影响记录约 {step} 条"
                if auto:
                    done = self._try_autofix(it)
                    line += f"。已自动预览回档：{done}"
                else:
                    line += "。建议：让腐竹确认后回档该区域。"
                parts.append(line)
            elif it["type"] == "tps":
                parts.append(f"· 服务器 TPS 偏低：{it['tps']}（阈值 {_cfg(self.plugin).get('tps_warn', _DEFAULTS['tps_warn'])}）。"
                             f"建议排查卡顿源 / 降载。")
        parts.append("— 由 GreenMoon Agent 自动巡检生成")
        return "\n".join(parts)

    def _may_auto_fix(self) -> bool:
        cfg = _cfg(self.plugin)
        if not cfg.get("auto_fix", _DEFAULTS["auto_fix"]):
            return False
        try:
            from gm import ai as _ai
            return int(_ai.autonomy()) >= 2
        except Exception:
            return False

    def _safe_preview(self, bbox: Dict[str, Any]) -> Optional[int]:
        """只读一步：预览影响面，返回命中记录数（失败返回 None）。"""
        try:
            from gm import region as _region
            prev = _region.preview(dimension="", **bbox, minutes=120)
            return int(prev.get("records_hit", 0))
        except Exception:
            return None

    def _try_autofix(self, it: Dict[str, Any]) -> str:
        """在 auto_fix + 接管级别2 下真正执行回档（已在校验后调用）。"""
        try:
            from gm import region as _region
            res = _region.rollback(actor="ai_agent", reason="Agent 自动巡检回档",
                                   dimension="", **it["bbox"], minutes=120)
            if res.get("started"):
                return f"会话 #{res.get('session_id')}"
            return "无命中"
        except Exception as e:
            return f"失败（{e}）"

    # -- 腐竹任务入口 ------------------------------------------------------
    def run_task(self, text: str, group: str = "") -> Optional[str]:
        """腐竹在群里/游戏内下发的任务。

        ★ 2026-05 改进 #7：确定性关键词解析（``qq_agent.interpret`` /
        ``execute``）已整体移除 —— 它会把爆炸记录的 actor
        （``minecraft:tnt`` / ``Block of TNT``）误当成"炸服者"去封禁。
        现在统一交给 LLM 智能体：``ai_manager.owner_chat`` 以服主身份、
        带 ``rollback_region`` / ``ban_player`` 等工具，能按自然语言
        正确理解并执行，也不会臆造责任人。

        返回报告文本；AI 未启用 / 调用失败返回 ``None``，由调用方
        落到通用聊天兜底（行为与原先"识别不到"完全一致）。

        ★ 先做一次 ``available()``（纯本地配置校验，不发请求）：
          不可用时返回 ``None`` 交给兜底；可用但这次调用失败时**直接
          把错误回给腐竹**，不再返回 None —— 否则调用方会再走一遍
          ``_try_ai_chat``，同一句话连烧两次额度、延迟也翻倍。
        """
        try:
            mgr = getattr(self.plugin, "ai_manager", None)
            if mgr is None:
                return None
            try:
                if not mgr.available():
                    return None
            except Exception:
                return None
            res = mgr.owner_chat(text, [])
        except Exception as e:
            try:
                self.plugin.logger.warning(f"[agent] 腐竹任务转 LLM 失败: {e}")
            except Exception:
                pass
            return f"§cAI 调用失败：{e}"
        if not isinstance(res, dict):
            return str(res) if res else None
        if res.get("ok") and str(res.get("text") or "").strip():
            return str(res["text"])
        err = str(res.get("error") or "").strip()
        if err:
            # 已经调过一次了，把原因原样告诉腐竹，别再触发第二次调用
            return f"§c{err}"
        return None


# 便于 gm/ai.py 暴露的探测入口
def default_config() -> Dict[str, Any]:
    return dict(_DEFAULTS)
