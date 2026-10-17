# -*- coding: utf-8 -*-
"""QQ 群消息 → 游戏内广播的文本格式化（官方网关与 OneBot 共用）。

为什么单独抽一个模块
--------------------
这段逻辑原先在两处各写了一份（``qqbot.py`` 与 ``onebot/interop.py``），
于是两边各错各的：

* 官方网关把格式串的第一个参数填成**群名**，取不到群名时又回退到
  ``sender_name`` —— 一个位置既可能是群又可能是人，语义错位。
  日志里因此出现过 ``[QQ] 7FCD011F67733A4572DCDBF2D525F201：？``
  这种一串 ID 当人名的输出。
* OneBot 那边干脆把 ``group_id``（一串数字）当第一个参数，
  于是永远显示数字。

现在只有一份实现，语义固定、向后兼容，两边不会再分叉。

占位符约定
----------
格式串支持 1~3 个位置占位符，按顺序是::

    %s  → 发言人昵称
    %s  → 群名
    %s  → 消息内容

为了不打破已经存进配置文件的旧格式串（那时只有 2 个占位符，
含义是「群名：内容」），这里做**按个数自动适配**：

* 3 个占位符 → ``(昵称, 群名, 内容)``      ← 新推荐写法
* 2 个占位符 → ``(昵称, 内容)``            ← 兼容，但把第一个从「群名」纠正为「昵称」
* 1 个占位符 → ``(内容,)``
* 0 个占位符 → 直接原样输出（不做 % 运算，避免 ``%`` 转义问题）

> 2 个占位符的旧配置会被**纠正语义**：原先填的是群名，现在填昵称。
> 这是有意的 —— 用户明确反馈「游戏里应该显示谁在说话」，而群名在
> 单群场景下是冗余信息（一个群的消息都来自同一个群）。
"""

from __future__ import annotations

from typing import Any, List

__all__ = ["DEFAULT_FORMAT", "PLACEHOLDER_HELP", "format_relay"]

#: 默认格式：昵称@群名：内容
DEFAULT_FORMAT = "[QQ] %s@%s：%s"

#: 面板里给用户看的说明
PLACEHOLDER_HELP = "%s=发言人昵称, %s=群名, %s=内容"


def _count_placeholders(fmt: str) -> int:
    """数出格式串里有多少个 %s 占位符（容忍 ``%%`` 转义）。

    只认 ``%s``；``%%`` 是字面量百分号，不算占位符。
    """
    n = 0
    i = 0
    ln = len(fmt)
    while i < ln:
        if fmt[i] == "%" and i + 1 < ln:
            nxt = fmt[i + 1]
            if nxt == "%":
                i += 2          # %% → 字面量，跳过
                continue
            if nxt == "s":
                n += 1
            else:
                # %d / %r 之类：当作未知占位符，也计一个（下面会按位置填）
                n += 1
        i += 1
    return n


def format_relay(fmt: Any, sender_name: Any, group_name: Any, content: Any) -> str:
    """按格式串生成游戏内显示文本。

    参数
    ----
    fmt          格式串（用户可自定义）
    sender_name  发言人昵称
    group_name   群名
    content      消息内容

    返回构造好的字符串。任何异常（格式串写错、占位符数量对不上）
    都退化为默认格式，**绝不把异常抛给调用方** —— 群消息处理链路上
    抛异常会导致整条消息被丢弃，比显示得难看严重得多。
    """
    text = str(content if content is not None else "")
    who = str(sender_name or "").strip()
    grp = str(group_name or "").strip()

    # 昵称拿不到时，不要显示空白或 "None"：
    # 用一个明确的占位词，让服主一眼看出「昵称没取到」而不是
    # 以为消息本身就长这样（旧实现会显示成一串 openid）。
    if not who:
        who = "群友"
    if not grp:
        grp = ""

    s_fmt = str(fmt if fmt is not None else "").strip()
    if not s_fmt:
        s_fmt = DEFAULT_FORMAT

    # 群名缺席时，"昵称@：" 后面跟着空群名很难看 —— 用别名回退，
    # 让 3 占位符的默认格式在单群场景下也读得通。
    grp_for_fmt = grp or "群"

    n = _count_placeholders(s_fmt)

    try:
        if n >= 3:
            return s_fmt % (who, grp_for_fmt, text)
        if n == 2:
            # 旧配置：按「昵称：内容」解释（有意纠正，见模块 docstring）
            return s_fmt % (who, text)
        if n == 1:
            return s_fmt % (text,)
        # 没有占位符：原样输出，但要处理 % 转义
        return s_fmt.replace("%%", "%")
    except (TypeError, ValueError):
        # 格式串非法（例如 %d 配字符串）→ 退回默认格式，绝不抛出
        try:
            return DEFAULT_FORMAT % (who, grp_for_fmt, text)
        except Exception:
            return f"[QQ] {who}：{text}"
