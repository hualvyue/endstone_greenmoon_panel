"""游戏内管理员表单（仅 OP 可唤出）。

提供一条 BDS 指令 ``/gmpanel``（见 ``core.py`` 的 ``commands`` 字典与
``_gm_panel_command``）。OP 在游戏内敲这条指令会弹出一个 ``SimpleForm``
主菜单，含三个按钮：

    🧱 区块回档      → 选时间窗 + 区域 → 预览影响面 → 确认执行 → 可撤销
    🤖 机器人设置    → 查看并切换关键开关（AI 群聊 / 接管级别 / 机器人启用）
    🧠 AI 设置       → 查看并切换 AI 总开关 / 群聊 / 接管级别 / 模型端点

⚠ 关于 endstone.form
---------------------
endstone 的表单 API 在不同版本里偶有差异，且本沙箱（打包/单测环境）往往
**没有安装 endstone**。所以这里对 ``endstone.form`` / ``endstone.Player``
做**防御式可选导入**：导入失败就把它们置成 ``None``，所有发送路径在
真正调用前都会判空，绝不因为"导入不到"就崩。单测用 mock 替换即可。

表单回调（endstone 在主线程上调用）里任何异常都被吞掉并回一条红字，
绝不把异常抛回服务端 —— 否则可能污染指令返回值、吞掉别的插件的指令。
"""
from __future__ import annotations

import time
from typing import Any, Callable, List, Optional

try:  # pragma: no cover - 取决于运行环境是否装了 endstone
    from endstone.form import SimpleForm, CustomForm, ModalForm
    from endstone import Player
except Exception:  # pragma: no cover
    SimpleForm = CustomForm = ModalForm = Player = None  # type: ignore[assignment]

_TITLE = "§aGreenMoon 管理面板"
_ICON = "§7"

# 区域记录子插件里用于留痕的 actor 标识（区分"谁发起的回档"）。
_FORM_ACTOR = "ingame_form"

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def available() -> bool:
    """endstone 表单 API 是否可用（打包/单测环境下可能为 False）。"""
    return SimpleForm is not None and CustomForm is not None and ModalForm is not None


def _is_player(obj: Any) -> bool:
    if Player is not None and isinstance(obj, Player):
        return True
    # 兜底：只要是看起来像 Player 的对象（有 send_message / is_op）就当它是。
    return bool(getattr(obj, "send_message", None) and hasattr(obj, "is_op"))


def _msg(player: Any, text: str) -> None:
    try:
        player.send_message(str(text))
    except Exception:
        pass


def _send(plugin: Any, player: Any, form: Any, cb: Callable[[Any], None]) -> None:
    """安全发送表单；失败降级为日志 + 红字。"""
    try:
        player.send_form(form, cb)
    except Exception as e:  # pragma: no cover - 取决于端石版本
        try:
            plugin.logger.error(f"[forms] 发送表单失败: {e}")
        except Exception:
            pass
        _msg(player, "§c表单发送失败，请查看服务端日志。")


def _to_int(text: str, default: int = 0) -> int:
    try:
        return int(str(text or "").strip() or default)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 主菜单
# ---------------------------------------------------------------------------


def open_admin_form(plugin: Any, player: Any) -> None:
    """入口：弹主菜单。非 OP 直接拒绝。"""
    if not _is_player(player):
        _msg(player, "§c该指令只能在游戏内由玩家使用。")
        return
    if not getattr(player, "is_op", False):
        _msg(player, "§c只有管理员(OP)才能打开管理面板。")
        return
    if not available():
        _msg(player, "§c当前服务端不支持游戏内表单（endstone.form 不可用）。"
                      "请在网页面板操作。")
        return

    form = SimpleForm(_TITLE, "请选择要执行的操作：")
    form.add_button("🧱 区块回档")
    form.add_button("🤖 机器人设置")
    form.add_button("🧠 AI 设置")

    def _cb(res: Any) -> None:
        try:
            idx = int(res) if res is not None else -1
        except (TypeError, ValueError):
            idx = -1
        if idx == 0:
            build_rollback_form(plugin, player)
        elif idx == 1:
            build_bot_form(plugin, player)
        elif idx == 2:
            build_ai_form(plugin, player)

    _send(plugin, player, form, _cb)


# ---------------------------------------------------------------------------
# 区块回档
# ---------------------------------------------------------------------------


def _parse_time(unit_idx: int, value: str) -> dict:
    """下拉单位(0=分钟,1=小时,2=天) + 数值 → 回档 API 的时间关键字。"""
    n = _to_int(value, 0)
    n = max(1, n)
    if unit_idx == 1:
        return {"hours": n}
    if unit_idx == 2:
        return {"days": n}
    return {"minutes": n}


def _build_region(mode_idx: int, vals: List[str]):
    """根据区域模式构造回档区域参数。

    mode_idx==0（两点对角）：用 vals[3..8] 的 x1/y1/z1/x2/y2/z2
    mode_idx==1（中心+半径）：中心 = vals[3..5]，半径单独从 vals[6] 取
    """
    x1, y1, z1 = _to_int(vals[3]), _to_int(vals[4]), _to_int(vals[5])
    if mode_idx == 0:
        x2, y2, z2 = _to_int(vals[6]), _to_int(vals[7]), _to_int(vals[8])
        return {"p1": (x1, y1, z1), "p2": (x2, y2, z2)}
    radius = max(1, _to_int(vals[6], 16))
    return {"center": (x1, y1, z1), "radius": radius}


def _preview_text(prev: dict) -> str:
    lines = ["§e=== 回档影响面预览 ==="]
    lines.append(f"§7命中记录数：§f{prev.get('records_hit', 0)}")
    by_action = prev.get("by_action") or {}
    if by_action:
        act = "，".join(f"{k}:{v}" for k, v in by_action.items())
        lines.append(f"§7按操作：§f{act}")
    by_actor = prev.get("by_actor") or {}
    if by_actor:
        top = sorted(by_actor.items(), key=lambda kv: kv[1], reverse=True)[:5]
        who = "，".join(f"{k}({v})" for k, v in top)
        lines.append(f"§7主要操作者：§f{who}")
    top_blocks = prev.get("top_blocks") or {}
    if top_blocks:
        blk = "，".join(f"{k}:{v}" for k, v in list(top_blocks.items())[:6])
        lines.append(f"§7主要方块：§f{blk}")
    if prev.get("truncated"):
        lines.append("§c⚠ 结果被上限截断，实际影响可能更大。")
    lines.append("")
    lines.append("§7确认执行回档？此操作会改写世界方块。")
    return "\n".join(lines)


#: 内置维度，顺序必须与回档表单里的下拉选项一致（提交回来的是索引）
_DIMENSIONS = ("overworld", "nether", "the_end")


def _parse_dimension(sel: Any, custom: Any = "") -> str:
    """把「维度下拉 + 自定义输入框」解析成一个维度名。

    ★ 与网页端 ``regionDim()`` 保持同一套语义（改进 #6）：下拉给三个内置
      维度（主世界 / 下界 / 末地），第 4 项「自定义」留给装了模组/附加包、
      有自己维度名的服，此时以手填的内容为准。
    ★ Endstone 的 CustomForm 下拉提交回来的是**索引**（int），不是文本 ——
      与表单里"时间单位 / 区域模式"两个下拉同样处理。
    """
    try:
        idx = int(sel)
    except (TypeError, ValueError):
        idx = 0
    if idx == len(_DIMENSIONS):          # 最后一项 = 「自定义（下方填写）」
        return str(custom or "").strip()
    if 0 <= idx < len(_DIMENSIONS):
        return _DIMENSIONS[idx]
    return ""


def build_rollback_form(plugin: Any, player: Any) -> None:
    """回档子表单：时间窗 + 区域 → 预览 → 确认 → 执行。"""
    form = CustomForm("🧱 区块回档")
    form.add_dropdown("时间单位", ["分钟", "小时", "天"], 1)      # 0
    form.add_input("时间数值", "例如 30", "30")                  # 1
    form.add_dropdown("区域模式", ["两点对角", "中心+半径"], 0)   # 2
    form.add_input("坐标 X1 / 中心X", "整数", "0")               # 3
    form.add_input("坐标 Y1 / 中心Y", "整数", "64")              # 4
    form.add_input("坐标 Z1 / 中心Z", "整数", "0")               # 5
    form.add_input("坐标 X2 / 半径", "对角模式填X2，中心模式填半径", "100")  # 6
    form.add_input("坐标 Y2", "整数（对角模式）", "80")          # 7
    form.add_input("坐标 Z2", "整数（对角模式）", "100")          # 8
    form.add_dropdown("维度", ["主世界(overworld)", "下界(nether)",
                               "末地(the_end)", "自定义(下方填写)"], 0)  # 9
    form.add_input("自定义维度名", "模组/附加包自定义维度，留空用上方选择",
                   "")                                            # 10

    def _cb(vals: Any) -> None:
        if not isinstance(vals, (list, tuple)) or len(vals) < 11:
            _msg(player, "§c表单数据异常，已取消。")
            return
        try:
            time_kw = _parse_time(int(vals[0]), str(vals[1]))
            region_kw = _build_region(int(vals[2]), [str(v) for v in vals])
            dim = _parse_dimension(vals[9], vals[10])
        except Exception as e:
            _msg(player, f"§c参数解析失败：{e}")
            return

        def _work() -> None:
            try:
                from gm import region as _region
            except Exception as e:
                _msg(player, f"§c无法加载回档模块：{e}")
                return
            try:
                prev = _region.preview(dimension=dim, **region_kw, **time_kw)
            except Exception as e:
                _msg(player, f"§c预览失败：{e}")
                return

            def _confirm(cres: Any) -> None:
                if cres is not True:
                    _msg(player, "§7已取消回档。")
                    return

                def _do() -> None:
                    try:
                        res = _region.rollback(
                            actor=_FORM_ACTOR, reason="游戏内表单回档",
                            dimension=dim, **region_kw, **time_kw)
                    except Exception as e:
                        _msg(player, f"§c回档失败：{e}")
                        return
                    if not res.get("started"):
                        _msg(player, "§7该时间窗/区域内没有可回档的记录。")
                        return
                    sid = res.get("session_id")
                    _msg(player, f"§a回档已受理（会话 #{sid}）。"
                                  f"稍后可在网页「区块日志与恢复」查看，"
                                  f"或让我帮你撤销。")

                # 回档可能较重，丢到调度器离线执行，结果回消息。
                try:
                    plugin.server.scheduler.run_task(plugin, _do)
                except Exception:
                    _do()

            if available():
                f2 = ModalForm("确认回档", _preview_text(prev),
                               "执行回档", "取消")
                _send(plugin, player, f2, _confirm)
            else:
                _msg(player, "§c无法显示确认框（表单不可用）。")

        try:
            plugin.server.scheduler.run_task(plugin, _work)
        except Exception:
            _work()

    _send(plugin, player, form, _cb)


# ---------------------------------------------------------------------------
# 机器人设置
# ---------------------------------------------------------------------------


def _read_ai_cfg(plugin: Any) -> dict:
    mgr = getattr(plugin, "ai_manager", None)
    if mgr is None:
        return {}
    try:
        return dict(mgr.cfg() or {})
    except Exception:
        return {}


def build_bot_form(plugin: Any, player: Any) -> None:
    """机器人/AI 设置子表单：切换关键开关并即时生效。"""
    cfg = _read_ai_cfg(plugin)
    chat_on = bool(cfg.get("chat_enabled", False))
    ai_on = bool(cfg.get("enabled", False))
    try:
        from gm import ai as _ai
        autonomy = int(_ai.autonomy())
    except Exception:
        autonomy = int(cfg.get("autonomy_level", 0) or 0)

    form = CustomForm("🤖 机器人 / AI 设置")
    form.add_label("§7以下开关修改后立即生效（详细配置请在网页面板）。")
    form.add_toggle("AI 总开关", ai_on)                       # 0
    form.add_toggle("群友 @ 聊天", chat_on)                   # 1
    form.add_step_slider("AI 接管级别",                       # 2
                         ["0 纯建议", "1 白名单自动", "2 完全放开"],
                         autonomy)
    form.add_input("AI 模型端点(可选)", "http(s) 兼容 OpenAI",
                   str(cfg.get("base_url", "") or ""))         # 3
    form.add_input("模型名(可选)", "如 gpt-4o-mini",
                   str(cfg.get("model", "") or ""))            # 4

    def _cb(vals: Any) -> None:
        if not isinstance(vals, (list, tuple)) or len(vals) < 5:
            _msg(player, "§c表单数据异常，已取消。")
            return
        ai_enabled = bool(vals[0])
        chat_enabled = bool(vals[1])
        level = int(vals[2]) if not isinstance(vals[2], bool) else 0
        level = max(0, min(2, level))
        base_url = str(vals[3] or "").strip()
        model = str(vals[4] or "").strip()
        payload = {
            "enabled": ai_enabled,
            "chat_enabled": chat_enabled,
            "autonomy_level": level,
        }
        if base_url:
            payload["base_url"] = base_url
        if model:
            payload["model"] = model
        try:
            saver = getattr(plugin, "_save_ai_config", None)
            if callable(saver):
                ok, msg = saver(payload)
            else:
                ok, msg = False, "无保存入口"
            if not ok:
                _msg(player, f"§c保存失败：{msg}")
                return
        except Exception as e:
            _msg(player, f"§c保存失败：{e}")
            return
        # 接管级别需要单独应用（可能涉及工具权限重算）
        try:
            from gm import ai as _ai
            _ai.set_autonomy(level)
        except Exception:
            pass
        _msg(player, "§a机器人/AI 设置已保存。"
                      f"接管级别={level}，群聊={'开' if chat_enabled else '关'}。")

    _send(plugin, player, form, _cb)


# 兼容别名：主菜单里「AI设置」与「机器人设置」共用同一张表单即可，
# 但为语义清晰保留独立入口（目前实现一致）。
build_ai_form = build_bot_form
