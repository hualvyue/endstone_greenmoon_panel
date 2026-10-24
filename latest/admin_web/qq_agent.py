"""QQ 腐竹（服主）直连任务辅助。

设计定位（与用户的约定）
------------------------
* 腐竹 = 在群里发送**绑定码**完成绑定的那个人（见 ``qqbot.py`` 的
  ``_claim_owner``）。
* 普通群友 / 熊孩**没有**这条直连通道：他们的破坏性请求在
  ``qqbot.py`` 的路由层就被挡下，只走普通 AI 聊天（熊孩还是冷漠语气）。

★ 2026-05 变更（改进 #7）：**移除了原有的「中文关键词识别器」**
  （``interpret`` 那一套按「炸服 / 回档 / 封禁」等关键词硬匹配意图、
  再用聚合逻辑点名"炸服者"的实现）。它有两个硬伤：
    1. 会把爆炸记录的 actor（``minecraft:tnt`` / ``Block of TNT`` 这类
       方块/实体标识）当成"炸服的人"，一本正经地建议封禁一个方块；
    2.  brittle 的关键词匹配经常误判或用不上大模型的理解力。

  现在腐竹的回档 / 封禁 / 查询指令**统一交给 LLM 智能体**
  （``ai/agent`` + ``ai_manager``，自带 ``rollback_region`` /
  ``ban_player`` 等工具，且复用本模块的 ``is_player_actor`` 过滤器，
   绝不会把 TNT 当人）。自然语言理解更准，也不会臆造责任人。

本模块现在只保留：玩家判定（``is_player_actor``）、封禁追问的
挂起/消费（``consume_pending`` / ``_clear_pending``）、群发报告
（``_send_report``）。消息路由仍由 ``qqbot.py`` 负责。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

# 分档常量（与 qqbot.py / Web 保持一致）
TIER_OWNER = "owner"
TIER_NEWBIE = "newbie"
TIER_NORMAL = "normal"
TIER_BRAT = "brat"
_TIERS = (TIER_OWNER, TIER_NEWBIE, TIER_NORMAL, TIER_BRAT)

_ACTOR = "qq_owner_agent"
_PENDING_TTL = 300  # 封禁追问有效期（秒）


# ---------------------------------------------------------------------------
# 群发报告
# ---------------------------------------------------------------------------


def _send_report(plugin: Any, text: str, group: str = "") -> None:
    try:
        from gm import bot as _bot
        cid = _bot.primary_id()
        target = group or ""
        if cid and target:
            _bot.send_group_async(cid, target, str(text))
            return
        if cid:
            _bot.send_text(cid, str(text))
            return
    except Exception as e:  # pragma: no cover
        try:
            plugin.logger.warning(f"[owner-agent] 群发报告失败: {e}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 玩家判定与封禁追问
# ---------------------------------------------------------------------------


#: 实体的**显示名**（Bedrock 客户端里看到的名字），全部小写。
#:
#: 判断"这个 actor 是不是玩家"时用来兜底。之所以需要这份名单：
#: Endstone 的 ``Actor.name`` 返回的是可读名而不是 ``minecraft:xxx``，
#: 旧版写入端又优先存了 ``name``，于是库里有大量 "Creeper" / "Zombie"
#: 这种看不出身份的字符串。只认冒号和括号的判定会全部放行。
#:
#: 只收**会造成方块破坏或爆炸**的（爆炸/火焰/弹射物/近战敌对生物），
#: 不做全量实体表 —— 名单越长，误伤真实玩家名的概率越高。
_NON_PLAYER_DISPLAY_NAMES = frozenset({
    # 爆炸 / 引爆物
    "tnt", "primed tnt", "tnt minecart", "bed", "respawn anchor",
    "end crystal", "crystal", "fireball", "small fireball",
    "dragon fireball", "wither skull", "ghast fireball",
    # 会主动破坏方块的敌对生物
    "creeper", "charged creeper", "zombie", "husk", "drowned",
    "skeleton", "stray", "bogged", "wither skeleton", "wither",
    "spider", "cave spider", "silverfish", "endermite", "enderman",
    "witch", "evoker", "vindicator", "pillager", "ravager", "vex",
    "blaze", "ghast", "magma cube", "slime", "shulker",
    "guardian", "elder guardian", "warden", "breeze", "piglin",
    "piglin brute", "zoglin", "hoglin", "phantom",
    # 弹射物 / 其它
    "arrow", "trident", "snowball", "egg", "ender pearl", "llama spit",
    "firework rocket", "firework", "fishing hook",
    # 非生物标记
    "lightning bolt", "lightning", "unknown", "entity",
})


def is_player_actor(actor: str) -> bool:
    """这个操作者**看起来是不是一个真实玩家**（而不是方块 / 实体 / 自然因素）？

    ★ 2026-05 修复（Bug #4/#5）的核心。为什么需要它：
      区块日志里 ``actor`` 字段记的是"这一条是谁造成的"，而爆炸类记录
      根本没有玩家 —— 它是**肇事物本身**：
        · TNT 实体   → ``minecraft:tnt``
        · 床/重生锚  → ``minecraft:bed`` / ``Block of TNT`` 这类显示名
        · 活塞       → ``(活塞)``
        · 岩浆遇水   → ``(自然生成)``
      原先的代码直接"按 actor 分组、取条数最多的那个"当炸服者，于是会
      一本正经地报告「炸服的人是 Block of TNT」，还追问要不要封禁它 ——
      那是把一个方块当成人，既荒唐又危险（真点了"是"就去封禁一个不存在的
      玩家名）。这里把非玩家一律挡掉；认不出就如实说"确认不了责任人"，
      **绝不编一个**。一个假的好消息比一个真的坏消息危险得多。
    """
    s = str(actor or "").strip()
    if not s:
        return False
    # 自然/机械标记（本模块与 core.py 约定的写法）
    if s.startswith("("):
        return False
    # 命名空间 ID：minecraft:tnt / minecraft:creeper …
    if ":" in s:
        return False
    # 英文显示名："Block of TNT" / "Block of Bedrock" …
    low = s.lower()
    if low.startswith("block") or " of " in low:
        return False
    if low in ("未知", "unknown", "-", "none", "null"):
        return False
    # ★ 已知识别：实体 / 方块 / 弹射物的**显示名**。
    #
    #   为什么单靠"含冒号"不够 —— 旧版本写库时优先取的是 Actor.name，
    #   苦力怕落盘就是纯英文单词 "Creeper"，既没有冒号也不带括号，
    #   上面所有规则都拦不住。历史记录已经这么存了，光改写入端救不了
    #   存量数据，读端也必须认得出这些名字。
    #
    #   取舍：真有玩家把 ID 起成 "Creeper"，这里会被当成的确不是玩家，
    #   于是漏报一次。但反过来的代价是**诬告并可能封禁一个不存在的账号**，
    #   两害相权，宁可漏报。而且真实玩家名不会出现在 explode 记录的
    #   actor 里（爆炸事件的 actor 永远是肇事实体）。
    if low in _NON_PLAYER_DISPLAY_NAMES:
        return False
    # 玩家名长度上限（MC 规范 3~16，这里放宽到 32 兼容部分平台的长名）
    if len(s) > 32:
        return False
    return True


def _clear_pending(plugin: Any) -> None:
    pend = getattr(plugin, "_owner_pending", None)
    if isinstance(pend, dict):
        pend.pop("__owner_ban__", None)


def consume_pending(plugin: Any, text: str) -> Optional[str]:
    """腐竹下一条消息若是确认/否定，处理挂起的封禁追问。

    返回报告文本（已处理）或 None（与 pending 无关）。
    """
    pend = getattr(plugin, "_owner_pending", None)
    if not isinstance(pend, dict):
        return None
    item = pend.get("__owner_ban__")
    if not item:
        return None
    if time.time() > item.get("expires", 0):
        _clear_pending(plugin)
        return None
    t = str(text or "").strip().lower()
    yes = any(k in t for k in ("是", "好", "封", "ban", "yes", "y", "执行"))
    no = any(k in t for k in ("否", "不", "cancel", "别", "不用"))
    if not (yes or no):
        return None
    actor = item.get("actor")
    group = item.get("group", "")
    _clear_pending(plugin)
    if no:
        return f"§7已取消对「{actor}」的封禁。"
    try:
        from gm import ban as _ban
        r = _ban.add(actor, "腐竹指令封禁（回档后）")
        ok = bool((r or {}).get("ok"))
    except Exception as e:
        return f"§c封禁「{actor}」失败：{e}"
    if not ok:
        return f"§e封禁「{actor}」未完成：{(r or {}).get('message') or '未知'}"
    report = f"§a已封禁「{actor}」。"
    _send_report(plugin, f"🚫 已按腐竹指令封禁玩家：{actor}", group)
    return report
