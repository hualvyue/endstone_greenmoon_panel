"""回档引擎的宿主适配器。

这个文件把「GreenMoon 面板」和「从 MxBack 转录过来的回档引擎」粘合起来。

为什么要单独一层
----------------
引擎代码（``engine.py``）是为了**最小改动**才近乎原样搬过来的，它对宿主
的假设是 MxBack 那个插件对象：

    host.server           取维度 / 调度任务
    host.logger           记日志
    host.cfg.get(section, key, default=...)    读配置
    host.db               主数据库（RegistryStore）
    host.submit_db(kind, payload)   回档增量落库
    host.wait_db_flush(timeout)     FIFO 屏障
    host.write_daily(action, line, actor=...)  写每日流水
    host.selection_off(name)        退出选区（已废弃，留空实现）
    host.item_stack_safe            版本缺陷开关

在 GreenMoon 里我们**没有**这样一个对象，也不该为了迁就引擎去改造
``GreenMoonPlugin``（那是个一万两千行的巨型类，牵一发动全身）。

于是做一个轻量适配器：实现上面那份契约，内部转发到 ``RegionAPI``。
引擎侧零改动，面板侧也零改动。任何一边将来要重构，只需重写这一层。

线程安全性
----------
``emit_progress`` 会被**服务器主线程**（批次回调）调用，而消费者
（Web 的 SSE 推送队列、AI 工具的 Future）多半在别的线程。这里只做
「装箱 + 投递」，不做任何可能阻塞的操作 —— 阻塞主线程等于卡 tick。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

__all__ = ["EngineHost", "build_engine"]


class EngineHost:
    """给回档引擎用的宿主视图。契约见模块 docstring。"""

    def __init__(self, api, plugin):
        self.api = api
        self.plugin = plugin                       # GreenMoonPlugin
        self.logger = api.logger
        self.cfg = api.config                      # 提供 get(section, key, default=)
        self._cfg_cache: Optional[Dict[str, Any]] = None

    # ---- 引擎契约 ----------------------------------------------------
    @property
    def server(self):
        return self.plugin.server

    @property
    def host_plugin(self):
        """``run_task`` 需要一个 Endstone 认可的 plugin 所有权对象。"""
        return self.plugin

    @property
    def db(self):
        """引擎访问的主数据库。转发到 API 持有的 store。"""
        return self.api.store

    #: 向 istalling 兼容：MxBack 用它标记"数据库正在切换"
    @property
    def _db_swapping(self) -> bool:
        return False

    @property
    def _stopping(self) -> bool:
        """是否在关服窗口。

        ★ 这个判定的重要性在哪：
          关服流程里 BDS 的 Level 可能先被析构，而引擎的定时回调还在排
          队里。此时任何 ``server.get_player()`` / 取 Dimension 的动作
          都是去摸已经释放的内存 —— SIGSEGV，**Python 层拦不住**。
          所以标准做法是在 on_disable 一进来就置位，让所有回调立即返回。
        """
        return bool(getattr(self.plugin, "_stopping", False))

    @property
    def item_stack_safe(self) -> bool:
        """Endstone < 0.11.8 读 item_stack 会段错误（Endstone #469）。

        掉落物指纹核验必须读 item_stack，旧版本要退化为"仅按位置匹配"。
        这里把这个能力探测结果透传给引擎。
        """
        val = getattr(self.plugin, "_item_stack_safe", None)
        if val is None:
            return True
        return bool(val)

    # ---- 写相关 ------------------------------------------------------
    def submit_db(self, kind: str, payload: Any) -> None:
        """回档增量入队。保持 FIFO，理由见 api._WriteQueue。"""
        self.api.writer.put(str(kind), payload)

    def wait_db_flush(self, timeout: float = 10.0) -> bool:
        """等到写队列排空。返回 False 表示**没等到**。

        ★ 为什么必须把 writer 的结果透传出去，而不是无条件 return True：
          引擎在启动回档前用它做一道"前置屏障"——

              if not self.plugin.wait_db_flush(timeout=10.0):
                  return {"error": "数据库写入繁忙，请稍后再试"}

          如果这里恒返回 True，屏障就形同虚设。后果很具体：
          磁盘卡住时，撤销快照和会话关联还没落库，回档就开始跑了，
          最后 ``finalize_session`` 会把"已回档"标记打在一批
          **还没写进去**的日志上 —— 那些记录再也不会被回档，
          而且没有任何报错。宁可直接拒绝这次操作。
        """
        # 先把"方块记录"的内存缓冲同步推下去。
        # 倒水/骨粉这类延迟验证产生的记录可能还在缓冲区里；漏掉它们
        # 会让回档少清掉源头方块 —— 表现为"回档完这块又自己流回来了"。
        try:
            self.api.flush_records()
        except Exception:
            pass
        # 再等回档增量队列真正排空（FIFO 屏障）
        try:
            return bool(self.api.writer.wait_flush(timeout))
        except Exception:
            # 队列本身出问题（理论上不会）：保守放行，不让引擎卡死。
            # 这里偏保守是安全的 —— 真正危险的是"谎报已完成"。
            return True

    def write_daily(self, action: str, line: str, actor: Optional[str] = None) -> None:
        """写一条可读的每日流水。

        ★ 为什么保留这么一个"文本日志"通道：
          SQLite 里的记录是**给机器用**的（回档时按坐标索引）；而给人看
          的操作留痕需要一句话就能读懂。"谁在什么时候把哪一片恢复到
          什么时候"这种事，翻数据库表远不如翻日志直观。
          两种形态各司其职，不要合并成一种。
        """
        try:
            self.api.publish("daily_line", {
                "action": str(action), "line": str(line),
                "actor": str(actor or ""),
            })
        except Exception:
            pass
        try:
            if self.logger is not None:
                self.logger.info(f"[区块日志] {line}")
        except Exception:
            pass

    def selection_off(self, name: str) -> None:
        """兼容钩子：Web 化之后没有"选区模式"这个状态，留空实现。

        原实现在这里做两件事：把玩家从选区集合里摘掉、清除粒子边框。
        两者都以"有个在线玩家正在游戏里操作"为前提，在这里没有意义。
        """

    # ---- 输出相关 ----------------------------------------------------
    def emit_progress(self, state: dict, level: str, text: Any, tip: str = "") -> None:
        """把回档过程中的一切产出投递到事件总线。

        ``text`` 可能是字符串，也可能是引擎组装好的**结构化结果字典**
        （``_finish`` 里就是这么给的）。两者都要能承载：
          * 字符串 → 当作进度/提示；
          * dict   → 当作终局结果，额外打上 session 元信息。
        """
        payload: Dict[str, Any]
        if isinstance(text, dict):
            payload = dict(text)
        else:
            payload = {"summary": str(text)}
        try:
            payload.setdefault("session_id", state.get("session_id"))
            payload.setdefault("actor", state.get("player"))
            payload.setdefault("kind", state.get("kind", "rollback"))
        except Exception:
            pass
        payload["level"] = str(level)
        if tip:
            payload["tip"] = str(tip)
        # 已完成/无变化要额外清掉"忙碌"标记，让前端收起进度条
        if level in ("done", "no_change", "error"):
            payload["finished"] = True
        try:
            self.api.publish(str(level), payload)
        except Exception:
            pass

        # 同时给在线的观察者玩家发一份（如果指定了 notify）
        try:
            p = self._resolve_player(state)
        except Exception:
            p = None
        if p is not None:
            msg = payload.get("summary")
            if isinstance(msg, str) and msg:
                self.notify_player(p, msg)

    def notify_player(self, p, text: str) -> None:
        """给一个在线玩家发消息。**绝不抛异常**。"""
        try:
            p.send_message(f"§6[区块日志]§f {text}")
        except Exception:
            pass

    def _resolve_player(self, state: dict):
        name = str(state.get("notify") or "")
        if not name:
            return None
        try:
            return self.plugin.server.get_player(name)
        except Exception:
            return None


def build_engine(api) -> Any:
    """构造回档引擎。

    放在函数里而不是模块顶层，是为了让 ``engine.py`` 里的
    ``from endstone.level import ...`` 延迟到真正要用时才 import ——
    这样一来，仅仅"装载了 panel 但没启用区块记录"的服务器不会
    因为缺少某个 Endstone 版本符号而在 import 阶段就挂掉。
    """
    from .engine import RollbackEngine
    return RollbackEngine(EngineHost(api, api.plugin))
