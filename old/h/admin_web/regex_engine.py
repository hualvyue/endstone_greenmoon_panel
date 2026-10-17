"""正则触发引擎：基于 rules.json 对群消息与游戏事件做正则匹配并执行动作。

移植自 LumenBridge ``modules/regex_engine.py``，按 GreenMoon 架构做了三处适配：

1. **去掉 event_bus**：事件改为直接调用 ``handle_event()`` / ``handle_message()``，
   由 ``qqbot.py`` / ``onebot/interop.py`` / ``core.py`` 在各自链路里显式调用。
2. **去掉 i18n**：文案直接写中文。
3. **去掉 config_manager**：规则存 ``<data_dir>/rules.json``，
   引擎开关读插件上的 ``_regex_config()``（无则用内置默认值）。

⚠ 安全边界（改动本文件务必守住）：

* **ReDoS 防护必留**：``pattern`` 来自可被网页编辑的 ``rules.json``，
  灾难性回溯会阻塞服务器主线程。超长 / 嵌套量词 / 超大重复限定一律跳过。
* **``executeCommand`` 必须走 ``is_on_main_thread()`` 分支**：
  主线程里再 ``run_on_main`` 然后阻塞等待 = 永久死锁。
* 单条规则报错只记日志，绝不中断后续规则。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from core import GreenMoonPlugin

# 动作去抖：同一规则对同一事件指纹在窗口内只执行一次，防上游重复上报
_ACTION_DEDUP_WINDOW = 5.0
_ACTION_DEDUP_MAX = 512

# 事件类型（游戏侧）
EVENT_PLAYER_JOIN = "server.player_join"
EVENT_PLAYER_LEFT = "server.player_left"
EVENT_PLAYER_CHAT = "server.player_chat"
# 事件类型（控制台日志侧）
#
# 这条事件由 log_tailer.py 从服务器 latest.log 里逐行喂进来，
# pack 里带 level / source / raw_message。**只匹配消息正文**，
# 「[ERROR] 这种开头标志」走 pack 的 level 字段 + 条件过滤，
# 不要求用户去写转义正则 —— 见 handle_event 里 EVENT_LOG_LINE 分支。
EVENT_LOG_LINE = "server.log_line"
# 事件类型（群侧）
EVENT_GROUP_MEMBER_JOIN = "group.member_join"
EVENT_GROUP_MEMBER_LEAVE = "group.member_leave"

EVENT_TYPES = (
    EVENT_PLAYER_JOIN,
    EVENT_PLAYER_LEFT,
    EVENT_PLAYER_CHAT,
    EVENT_LOG_LINE,
    EVENT_GROUP_MEMBER_JOIN,
    EVENT_GROUP_MEMBER_LEAVE,
)

# ---------------------------------------------------------------- 日志级别
# 与 BDS 原生输出一致。**大写且按名字精确匹配**，因为条件过滤是字符串
# 相等比较（"ERROR" != "error"），大小写在 pack 生成时就归一化掉。
LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL", "UNKNOWN")

#: 会触发内置「服务器报错」规则的那几级。抽成常量是因为 tailer 与
#: 内置规则都要用，散在两地就会漂移（改了一边忘了另一边）。
LOG_ERROR_LEVELS = ("ERROR", "FATAL")

ACTION_TYPES = (
    "replyText",
    "replyAtText",
    "replyImage",
    "deleteMessage",
    "muteUser",
    "executeCommand",
    "callPluginCommand",
)

DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "id": "rule_query_server",
        "name": "查服",
        "enabled": True,
        "triggerType": "message",
        "pattern": "^查服$",
        "flags": "",
        "eventType": "",
        "conditions": [],
        "actions": [
            {"type": "executeCommand", "params": "list"},
            {"type": "replyText", "params": "$result"},
        ],
        "block": True,
    },
    {
        "id": "rule_admin_execute",
        "name": "管理员执行命令",
        "enabled": False,
        "triggerType": "message",
        "pattern": "^执行(.+)",
        "flags": "",
        "eventType": "",
        "conditions": [
            {"field": "userRole", "operator": "==", "value": "admin"}
        ],
        "actions": [
            {"type": "executeCommand", "params": "$1"},
            {"type": "replyText", "params": "执行结果：$result"},
        ],
        "block": True,
    },
    {
        "id": "rule_welcome",
        "name": "入群欢迎",
        "enabled": True,
        "triggerType": "event",
        "pattern": "",
        "flags": "",
        "eventType": EVENT_GROUP_MEMBER_JOIN,
        "conditions": [],
        "actions": [
            # replyAtText：先真实 @ 进群成员再发文本
            {"type": "replyAtText", "params": " 欢迎新成员！"},
        ],
        "block": False,
    },
    {
        "id": "rule_mc_join",
        "name": "玩家进服播报",
        "enabled": False,
        "triggerType": "event",
        "pattern": "",
        "flags": "",
        "eventType": EVENT_PLAYER_JOIN,
        "conditions": [],
        "actions": [
            {"type": "replyText", "params": "🎮 玩家 $userId 加入了服务器"},
        ],
        "block": False,
    },

    # ------------------------------------------------------------ 日志规则
    # 下面 4 条预置规则**只有「服务器报错」默认开启**，其余一律关闭。
    #
    # 为什么默认关掉三条：日志事件是高频流（冷启动一次就有几十行），
    # 全开会把绑定的 QQ 群刷屏 —— 用户会以为插件有毛病。让服主按需打开，
    # 比默认全开再让用户去关更不容易被骂。
    #
    # ★ 级别过滤的写法
    #   不写 pattern="^\[ERROR\]" 这类转义正则（易错、且日志前缀格式
    #   一改动就失效），而是用 **conditions 里的 level 字段**。
    #   level 由 log_tailer 归一化成大写（ERROR/WARN/INFO/...），
    #   相等比较即可。要"ERROR 或 FATAL"就再加一条 —— 条件之间是 AND，
    #   同级别多选可以让用户复制规则、改 value。
    {
        "id": "rule_log_error",
        "name": "服务器报错",
        "enabled": True,
        "triggerType": "event",
        "pattern": "",
        "flags": "",
        "eventType": EVENT_LOG_LINE,
        "conditions": [
            {"field": "level", "operator": "==", "value": "ERROR"},
        ],
        "actions": [
            {"type": "replyText",
             "params": "⚠️ 服务器报错\n$raw_message"},
        ],
        "block": False,
    },
    {
        "id": "rule_log_warn",
        "name": "服务器警告",
        "enabled": False,
        "triggerType": "event",
        "pattern": "",
        "flags": "",
        "eventType": EVENT_LOG_LINE,
        "conditions": [
            {"field": "level", "operator": "==", "value": "WARN"},
        ],
        "actions": [
            {"type": "replyText", "params": "⚠ 警告：$raw_message"},
        ],
        "block": False,
    },
    {
        "id": "rule_log_player_cmd",
        "name": "玩家执行命令",
        "enabled": False,
        "triggerType": "event",
        # 只认带玩家名前缀的动作行，避免把插件自己的日志也捞进来
        "pattern": "^\\[([A-Za-z0-9_]{1,16})\\]",
        "flags": "",
        "eventType": EVENT_LOG_LINE,
        "conditions": [],
        "actions": [
            {"type": "replyText", "params": "🔧 $1 执行了命令：$raw_message"},
        ],
        "block": False,
    },
    {
        "id": "rule_log_mods",
        "name": "模组/脚本报错",
        "enabled": False,
        "triggerType": "event",
        # 匹配模组加载器与脚本引擎的报错关键词
        "pattern": "(?i)(failed to load|模组|script error|addon)",
        "flags": "",
        "eventType": EVENT_LOG_LINE,
        "conditions": [],
        "actions": [
            {"type": "replyText", "params": "🧩 模组相关问题：$raw_message"},
        ],
        "block": False,
    },
]

_JS_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}

# ---------------------------------------------------------------- ReDoS 防护
#
# 背景：pattern 来自可被网页编辑的 rules.json，而匹配发生在**服务端主线程**
# 的聊天路径上。一条灾难性回溯的正则可以把主线程挂死几分钟 ——
# 整个服务器卡住，不是「规则失效」这么轻。
#
# ⚠ 为什么原来的启发式不够
# ------------------------
# 原实现只拦两种形态：
#     ① a{100000}   （超大重复限定）
#     ② (a+)+       （量词紧跟在右括号后）
# 但真正吃满 CPU 的写法**不需要**量词直接贴着括号。实测（本机 CPython）：
#
#     (\w+\s?)*$    在 "a"*24 + "!" 上耗时 2.38s，每加 2 个字符约 ×4；
#                   30 字符约 10 分钟
#     (a|a)*$       2.71s
#     ([a-z]+)*$    1.39s
#
# 这三条全都逃过了 _is_risky_pattern → 2000 字符的截断也拦不住
# （2.38s 的输入只有 25 个字符）。所以启发式只能当**第一道**粗筛，
# 真正兜底必须是「跑起来测一下耗时」。
#
# 现在的三层防护：
#   ① 长度上限（廉价，挡住病态长 pattern）
#   ② 静态启发式（廉价，挡住最常见的显式嵌套量词，给出更友好的报错）
#   ③ **实测熔断**（可靠）：在规则保存时用真实文本做一次限时探针，
#      超时即判定为危险并拒绝保存；同时记录运行时熔断器，
#      万一有漏网的也能在第一次变慢时自动停用该条规则。
#
_MAX_PATTERN_LEN = 512        # pattern 最大长度
_MAX_MATCH_TEXT_LEN = 2000    # 参与匹配的消息文本最大长度
# 危险重复：{4位数及以上} 有界重复（如 a{100000}）
_HUGE_REPEAT_RE = re.compile(r"\{\d{4,}")
# 嵌套量词：量词+组结束+量词，如 (a+)+
_NESTED_QUANT_RE = re.compile(r"[+*}]\s*\)\s*[+*{]")

# 单次匹配的超时预算（秒）。正常规则在 2000 字符上远低于 1ms；
# 给到 0.25s 已经是三个数量级的余量，只有回溯爆炸才会碰到。
_MATCH_TIMEOUT = 0.25
# 静态探针用的输入：一段会诱发回溯的窄字符集长串 + 一个不匹配的尾巴。
# 之所以不匹配，是因为灾难性回溯恰恰发生在「匹配失败前的穷举」阶段。
_PROBE_TEXT = ("a" * 24) + "!"
# 运行时熔断阈值：单次匹配超过这个时间就永久停用该规则
_RUNTIME_TRIP_SECONDS = 0.5
# 连续多少次超时后把规则标记为已熔断
_RUNTIME_TRIP_COUNT = 2


def _is_risky_pattern(pattern: str) -> bool:
    """启发式粗筛（第一道）。命中即拒，但这**不是**安全边界。"""
    if _HUGE_REPEAT_RE.search(pattern):
        return True
    if _NESTED_QUANT_RE.search(pattern):
        return True
    return False


def probe_pattern(pattern: str, flags: str) -> "tuple[bool, str]":
    """限时实测一个 pattern 是否安全。

    返回 ``(safe, reason)``。做得是「拿真实正则引擎跑一小段会诱发
    回溯的输入，量它花了多久」—— 这比任何静态特征都准，
    因为它测的就是实际会执行的那段代码。

    实现上用 ``signal.setitimer`` 在**主线程**里做真超时；不在主线程
    时退化为「跑完看耗时」（仍然有效，只是不能中途打断）。
    """
    try:
        regex = compile_pattern(pattern, flags)
    except re.error as exc:
        return False, f"正则语法错误: {exc}"

    started = time.perf_counter()
    timed_out = False
    if threading.current_thread() is threading.main_thread():
        import signal

        def _on_alarm(signum, frame):
            raise _ProbeTimeout()

        old = None
        try:
            old = signal.signal(signal.SIGALRM, _on_alarm)
            signal.setitimer(signal.ITIMER_REAL, _MATCH_TIMEOUT)
        except Exception:
            old = None
        try:
            regex.search(_PROBE_TEXT)
        except _ProbeTimeout:
            timed_out = True
        except Exception:
            pass
        finally:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
                if old is not None:
                    signal.signal(signal.SIGALRM, old)
            except Exception:
                pass
    else:
        try:
            regex.search(_PROBE_TEXT)
        except Exception:
            pass
        if (time.perf_counter() - started) > _MATCH_TIMEOUT:
            timed_out = True

    if timed_out:
        return False, (f"该正则在 25 字符的测试输入上超过 {_MATCH_TIMEOUT}s "
                       f"仍未返回（灾难性回溯），已拒绝保存")
    elapsed = time.perf_counter() - started
    if elapsed > _MATCH_TIMEOUT / 4:
        return False, f"该正则匹配耗时 {elapsed * 1000:.0f}ms，过于接近安全上限，已拒绝"
    return True, ""


class _ProbeTimeout(Exception):
    """静态探针超时信号（内部使用）。"""


def compile_pattern(pattern: str, flags: str) -> "re.Pattern[str]":
    """将 JS 风格 flags 字符串转换为 Python re 标志并编译。"""
    py_flags = 0
    for ch in flags or "":
        py_flags |= _JS_FLAG_MAP.get(ch, 0)
    return re.compile(pattern, py_flags)


def _mask_secret(value: str, keep: int = 4) -> str:
    """日志用掩码：只保留前 ``keep`` 位，其余打点。"""
    text = str(value or "")
    if len(text) <= keep:
        return "*" * len(text)
    return text[:keep] + "..." + "*" * min(8, max(0, len(text) - keep))


class RegexEngineModule:
    """消息 / 事件正则触发引擎。

    与 LumenBridge 版的差异：不再注册到 event_bus，也不读 config_manager，
    所有入口都是显式方法调用。
    """

    def __init__(self, plugin: "GreenMoonPlugin") -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "logger", None)
        # 规则文件：<数据目录>/rules.json
        data_dir = getattr(plugin, "data_dir", None) or getattr(plugin, "data_folder", "") or "."
        self.path = os.path.join(str(data_dir), "rules.json")

        self.rules: list[dict[str, Any]] = self._load_rules()
        self.custom_actions: dict[str, Callable[..., Any]] = {}
        # pattern 编译缓存（非法 / 高风险缓存为 None 负缓存）：聊天在主线程匹配，
        # 避免逐条编译。
        self._pattern_cache: dict[str, "re.Pattern[str] | None"] = {}

        self._action_dedup: "OrderedDict[tuple[str, ...], float]" = OrderedDict()
        self._action_dedup_lock = threading.Lock()
        self._rules_lock = threading.RLock()

    # ------------------------------------------------------------ 配置
    def conf(self) -> dict[str, Any]:
        """引擎配置。插件提供 ``_regex_config()`` 时用它的，否则用内置默认值。

        实时读取（不缓存）—— 网页改了开关要立刻生效。
        """
        getter = getattr(self.plugin, "_regex_config", None)
        if callable(getter):
            try:
                data = getter()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {"enable": True, "only_on_main": True, "admin_debug": False,
                "command_timeout": 5.0}

    def _is_enabled(self) -> bool:
        return bool(self.conf().get("enable", True))

    # ------------------------------------------------------------ 指纹 / 去抖
    @staticmethod
    def _pack_fingerprint(trigger: str, event_type: str, pack: dict[str, Any]) -> tuple[str, ...]:
        """触发源指纹：消息按 message_id（缺失时按用户+内容+时间），事件按类型+用户+群+时间。"""
        if trigger == "message":
            mid = pack.get("message_id")
            if mid is not None and str(mid):
                return ("m", str(mid), str(pack.get("self_id", "")))
            return (
                "m",
                str(pack.get("user_id", "")),
                str(pack.get("group_id", "")),
                str(pack.get("raw_message", ""))[:128],
                str(pack.get("time", "")),
            )
        return (
            "e",
            str(event_type),
            str(pack.get("user_id", "")),
            str(pack.get("group_id", "")),
            # 聊天类事件纳入内容，避免同一玩家短窗口内的不同发言被误判为重复
            str(pack.get("raw_message", ""))[:128],
            str(pack.get("time", "")),
        )

    def _skip_duplicated(self, rule: dict[str, Any], fp: tuple[str, ...]) -> bool:
        """同一规则对同一指纹在窗口内重复触发时返回 True（跳过动作执行）。"""
        key = (str(rule.get("id", "")),) + fp
        now = time.monotonic()
        with self._action_dedup_lock:
            while len(self._action_dedup) > _ACTION_DEDUP_MAX:
                self._action_dedup.popitem(last=False)
            seen = self._action_dedup.get(key)
            if seen is not None and now - seen < _ACTION_DEDUP_WINDOW:
                return True
            self._action_dedup[key] = now
            return False

    # ------------------------------------------------------------ 持久化
    def _write_rules_atomic(self, rules: list[dict[str, Any]]) -> None:
        """tmp 文件 + 原子替换写 rules.json，防止进程中断留下半个文件。"""
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(rules, fh, ensure_ascii=False, indent=4)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(temp, self.path)

    def _load_rules(self) -> list[dict[str, Any]]:
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    # 过滤非 dict 元素，防热路径 rule.get() 抛 AttributeError
                    cleaned = [r for r in data if isinstance(r, dict)]
                    if len(cleaned) != len(data):
                        self._log("warning", "rules.json 含非对象元素，已自动过滤")
                    return cleaned
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                # 损坏时先备份 .corrupt 再回退默认，避免用户规则被覆写丢失
                try:
                    shutil.copy2(self.path, self.path + ".corrupt")
                except OSError:
                    pass
                self._log("error", f"rules.json 解析失败（{exc}），已备份为 .corrupt 并回退默认规则库")
        self._write_rules_atomic(DEFAULT_RULES)
        return json.loads(json.dumps(DEFAULT_RULES))

    def reload_rules(self) -> int:
        """重载规则库，返回规则数量。"""
        with self._rules_lock:
            self.rules = self._load_rules()
            if self._pattern_cache:
                self._pattern_cache.clear()
            return len(self.rules)

    def save_rules(self, rules: list[dict[str, Any]]) -> int:
        """保存规则库并立即热加载（供 WebUI 调用），返回保存后的条数。

        ★ 保存前对流式规则的 pattern 做一次**限时实测**（见 probe_pattern）。
        静态启发式拦不住 ``(\\w+\\s?)*$`` 这类写法，而它一旦进库，
        第一条命中它的群消息就能把主线程挂住几分钟。
        """
        # 过滤非 dict 元素，防热路径 rule.get() 抛 AttributeError
        clean = [r for r in rules if isinstance(r, dict)]
        self._probe_new_patterns(clean)
        with self._rules_lock:
            self._write_rules_atomic(clean)
            self.rules = clean
            if self._pattern_cache:
                self._pattern_cache.clear()
            # 新库 = 新机会，把熔断记录里已经不存在的规则清掉
            self._prune_tripped(clean)
        self._log("info", f"[正则引擎] 规则库已保存并热加载，共 {len(clean)} 条规则")
        return len(clean)

    def _probe_new_patterns(self, rules: list[dict[str, Any]]) -> None:
        """对新增/改动过的 pattern 做限时实测，危险的就地标记为停用。

        这里**不抛异常**（抛了会让整次保存失败，用户改了十条规则因为
        其中一条危险而全丢）。做法是把那条规则 ``enabled`` 置 False
        并写一条 ``_probe_error``，前端能看到原因、其它规则照常保存。
        """
        cache = getattr(self, "_probe_cache", None)
        if cache is None:
            cache = self._probe_cache = {}
        for rule in rules:
            try:
                pattern = str(rule.get("pattern", "") or "")
                if not pattern:
                    continue
                flags = str(rule.get("flags", "") or "")
                key = f"{flags}\x00{pattern}"
                if key in cache:
                    safe, reason = cache[key]
                else:
                    if len(pattern) > _MAX_PATTERN_LEN:
                        safe, reason = False, f"pattern 超过 {_MAX_PATTERN_LEN} 字符上限"
                    elif _is_risky_pattern(pattern):
                        safe, reason = False, "含嵌套量词或超大重复限定（ReDoS 风险）"
                    else:
                        safe, reason = probe_pattern(pattern, flags)
                    cache[key] = (safe, reason)
                if not safe:
                    rule["enabled"] = False
                    rule["_probe_error"] = reason
                    self._log(
                        "error",
                        f"规则 [{rule.get('name', rule.get('id', '?'))}] 的 pattern 未通过安全实测："
                        f"{reason}；已自动停用该规则（其余规则不受影响）",
                    )
                else:
                    rule.pop("_probe_error", None)
            except Exception as exc:
                self._log("warning", f"规则安全实测异常（已跳过该条）: {exc}")

    def _prune_tripped(self, rules: list[dict[str, Any]]) -> None:
        tripped = getattr(self, "_tripped", None)
        if not tripped:
            return
        alive = set()
        for r in rules:
            alive.add(self._rule_key(r))
        for k in list(tripped.keys()):
            if k not in alive:
                tripped.pop(k, None)

    @staticmethod
    def _rule_key(rule: dict[str, Any]) -> str:
        return f"{rule.get('flags', '')}\x00{rule.get('pattern', '')}"

    def snapshot_rules(self) -> list[dict[str, Any]]:
        """返回规则深拷贝，供 GET 接口使用（避免与并发写竞争）。"""
        with self._rules_lock:
            return json.loads(json.dumps(self.rules, ensure_ascii=False))

    def toggle_rule(self, rule_id: str, enabled: Any = None) -> bool:
        """启停一条规则；``enabled`` 为 None 时取反。返回是否命中规则。"""
        with self._rules_lock:
            for rule in self.rules:
                if str(rule.get("id", "")) == str(rule_id):
                    if enabled is None:
                        rule["enabled"] = not bool(rule.get("enabled"))
                    else:
                        rule["enabled"] = bool(enabled)
                    updated = json.loads(json.dumps(self.rules, ensure_ascii=False))
                    break
            else:
                return False
            self._write_rules_atomic(updated)
            self.rules = updated
        return True

    def delete_rule(self, rule_id: str) -> bool:
        """删除一条规则，返回是否命中。"""
        with self._rules_lock:
            kept = [r for r in self.rules if str(r.get("id", "")) != str(rule_id)]
            if len(kept) == len(self.rules):
                return False
            self._write_rules_atomic(kept)
            self.rules = kept
            if self._pattern_cache:
                self._pattern_cache.clear()
        return True

    # ------------------------------------------------------------ 编译
    def _safe_search(self, regex: "re.Pattern[str]", text: str, rule: dict[str, Any]):
        """带耗时熔断的 ``regex.search``。

        为什么需要它：pattern 是用户在网页上手写的，静态检查总有漏网。
        这一步是最后一道防线 —— 万一某条规则真的开始回溯爆炸，
        在**第一次出现明显变慢时就永久停用该规则**，
        而不是每次都让主线程陪它烧一遍 CPU。
        熔断是记账式（连续 N 次超时才熔断），避免偶发的 GC 抖动误杀好规则。
        """
        key = self._rule_key(rule)
        tripped = getattr(self, "_tripped", None)
        if tripped is None:
            tripped = self._tripped = {}
        if key in tripped:
            return None
        started = time.perf_counter()
        try:
            match = regex.search(text)
        except Exception as exc:
            self._log("warning", f"规则 [{rule.get('name', '?')}] 匹配异常: {exc}")
            return None
        elapsed = time.perf_counter() - started
        if elapsed >= _RUNTIME_TRIP_SECONDS:
            n = tripped.get(key, 0) + 1
            if n >= _RUNTIME_TRIP_COUNT:
                tripped[key] = n
                self._log(
                    "error",
                    f"规则 [{rule.get('name', rule.get('id', '?'))}] 单次匹配耗时 "
                    f"{elapsed:.2f}s（阈值 {_RUNTIME_TRIP_SECONDS}s），已熔断停用。"
                    f"请检查 pattern 是否存在灾难性回溯。",
                )
            else:
                tripped[key] = n
                self._log(
                    "warning",
                    f"规则 [{rule.get('name', '?')}] 单次匹配耗时 {elapsed:.2f}s，"
                    f"再出现一次将自动熔断",
                )
        return match

    def _compile_cached(self, pattern: str, flags: str, rule_name: Any) -> "re.Pattern[str] | None":
        """带缓存编译 pattern；非法 / 超长 / 高风险返回 None（负缓存，日志只打一次）。"""
        # getattr 兜底：测试桩经 __new__ 构造时不走 __init__
        cache = getattr(self, "_pattern_cache", None)
        if cache is None:
            cache = self._pattern_cache = {}
        key = f"{flags}\x00{pattern}"
        if key in cache:
            return cache[key]
        regex: "re.Pattern[str] | None" = None
        if pattern and len(pattern) <= _MAX_PATTERN_LEN and not _is_risky_pattern(pattern):
            try:
                regex = compile_pattern(pattern, flags)
            except re.error:
                regex = None
        if regex is None:
            self._log(
                "error",
                f"规则 [{rule_name}] 的正则过长或含嵌套量词/超大重复限定"
                f"（如 (a+)+、a{{100000}}），已跳过以防灾难性回溯（ReDoS）",
            )
        cache[key] = regex
        return regex

    def register_action(self, action_type: str, handler: Callable[..., Any]) -> None:
        """注册自定义动作（``callPluginCommand`` 用）。"""
        if callable(handler):
            self.custom_actions[str(action_type)] = handler
            self._log("info", f"[正则引擎] 已注册自定义动作: [{action_type}]")

    # ------------------------------------------------------------ 变量 / 条件
    @staticmethod
    def _get_at(pack: dict[str, Any]) -> "str | None":
        """从 pack 里取第一个 @ 目标。

        兼容两种形态：OneBot 的 ``message`` 消息段数组，
        以及官方域直接给出的 ``at`` 字段。
        """
        message = pack.get("message")
        if isinstance(message, list):
            for seg in message:
                # 跳过协议端畸形消息段，防 .get 抛异常
                if isinstance(seg, dict) and seg.get("type") == "at":
                    data = seg.get("data")
                    return str(data.get("qq", "")) if isinstance(data, dict) else ""
        at_direct = pack.get("at")
        if at_direct:
            return str(at_direct)
        return None

    @staticmethod
    def _get_at_excluding(pack: dict[str, Any], exclude: set[str]) -> "str | None":
        """取第一个 @ 目标，跳过指定 id（如官方域 @机器人 自身的 AppID）。"""
        message = pack.get("message")
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    qq = str((seg.get("data") or {}).get("qq", "") or "")
                    if qq and qq not in exclude:
                        return qq
        return None

    def _parse_variables(self, text: str, pack: dict[str, Any], context: dict[str, Any]) -> str:
        if not text:
            return ""

        def _sub(match: "re.Match[str]") -> str:
            var = match.group(1)
            if var == "userId":
                return str(pack.get("user_id", ""))
            if var == "groupId":
                return str(pack.get("group_id", ""))
            if var == "at":
                at_target = self._get_at(pack)
                return at_target if at_target else str(pack.get("user_id", ""))
            if var == "nickname":
                return str((pack.get("sender") or {}).get("nickname", ""))
            # 正文与日志级别：日志规则要用 $raw_message 回显被命中的那一行，
            # 用 $level 标出级别。缺了这两个变量，内置日志规则会原样发出
            # "$raw_message" 这十几个字符 —— 静默失败，最难查。
            if var == "raw_message":
                return str(pack.get("raw_message", ""))
            if var == "level":
                return str(pack.get("level", ""))
            if var == "source":
                return str(pack.get("source", ""))
            if var in context:
                return str(context[var])
            return match.group(0)

        return re.sub(r"\$([a-zA-Z0-9_]+)", _sub, text)

    def _check_conditions(self, conditions: "list[dict[str, Any]] | None", pack: dict[str, Any]) -> bool:
        if not conditions:
            return True

        for cond in conditions:
            if not isinstance(cond, dict):
                continue
            field = cond.get("field")
            operator = cond.get("operator")
            value = cond.get("value")

            if field == "userRole":
                roles = [str((pack.get("sender") or {}).get("role") or "member")]
                # 群管理员名单来自卡片配置（group_admins），官方域存的是 openid
                if str(pack.get("user_id", "")) in self._admin_ids(pack):
                    roles.append("admin")
                if operator == "==":
                    ok = str(value) in roles
                elif operator == "!=":
                    ok = str(value) not in roles
                else:
                    ok = False
                if not ok:
                    return False
                continue

            if field == "userId":
                actual = str(pack.get("user_id", ""))
            elif field == "groupId":
                actual = str(pack.get("group_id", ""))
            elif field == "nickname":
                # 昵称在 sender 子对象里，不在 pack 顶层。
                # 直接 pack.get("nickname") 永远拿到 ""，于是所有 nick
                # 相关条件恒不成立 —— 这种「静默恒假」的条件比报错更难查。
                actual = str((pack.get("sender") or {}).get("nickname", ""))
            else:
                actual = str(pack.get(field, "")) if pack.get(field) is not None else ""

            if operator == "==":
                ok = actual == str(value)
            elif operator == "!=":
                ok = actual != str(value)
            elif operator == "includes":
                ok = str(value) in actual
            elif operator == "matches":
                # ReDoS 防护：超长 / 高风险 pattern 判不匹配，文本截断
                pattern_val = str(value)
                if len(pattern_val) > _MAX_PATTERN_LEN or _is_risky_pattern(pattern_val):
                    ok = False
                else:
                    try:
                        ok = re.search(
                            pattern_val, actual[:_MAX_MATCH_TEXT_LEN], re.IGNORECASE
                        ) is not None
                    except re.error:
                        ok = False
            else:
                ok = False
            if not ok:
                return False
        return True

    def _admin_ids(self, pack: dict[str, Any]) -> set[str]:
        """本事件所属卡片的管理员 id 集合（官方域为 openid）。"""
        card_id = str(pack.get("card_id", "") or "")
        getter = getattr(self.plugin, "_qqbot_card_admins", None)
        if callable(getter):
            try:
                return set(str(x) for x in (getter(card_id) or []))
            except Exception:
                return set()
        return set()

    # ------------------------------------------------------------ 动作执行
    def _reply_target_group(self, pack: dict[str, Any]) -> str:
        """动作的默认回复目标群。"""
        group_id = str(pack.get("group_id", "") or "").strip()
        if group_id:
            return group_id
        getter = getattr(self.plugin, "_qqbot_default_group", None)
        if callable(getter):
            try:
                return str(getter(pack.get("card_id", "")) or "")
            except Exception:
                return ""
        return ""

    def _send_text(self, group_id: str, content: str) -> None:
        getter = getattr(self.plugin, "_regex_send_text", None)
        if callable(getter):
            getter(group_id, content)

    def _send_at_text(self, group_id: str, target: Any, content: str) -> None:
        getter = getattr(self.plugin, "_regex_send_at_text", None)
        if callable(getter):
            getter(group_id, target, content)
        else:
            self._send_text(group_id, content)

    def _send_image(self, group_id: str, url: str) -> None:
        getter = getattr(self.plugin, "_regex_send_image", None)
        if callable(getter):
            getter(group_id, url)
        else:
            self._send_text(group_id, str(url))

    def _delete_message(self, pack: dict[str, Any]) -> None:
        getter = getattr(self.plugin, "_regex_delete_message", None)
        if callable(getter):
            getter(pack.get("card_id", ""), pack.get("message_id"))

    def _mute_user(self, pack: dict[str, Any], group_id: str, duration: int) -> None:
        getter = getattr(self.plugin, "_regex_mute_user", None)
        if callable(getter):
            getter(pack.get("card_id", ""), group_id, pack.get("user_id"), duration)

    def _execute_actions(
        self,
        actions: list[dict[str, Any]],
        pack: dict[str, Any],
        match_groups: list[str],
    ) -> None:
        context: dict[str, Any] = {"result": ""}
        for idx, val in enumerate(match_groups):
            context[str(idx)] = val if val is not None else ""

        group_id = self._reply_target_group(pack)

        for action in actions or []:
            # 非法动作元素（字符串 / null 等）跳过，防 .get 抛异常中断后续动作
            if not isinstance(action, dict):
                continue
            action_type = action.get("type")
            params = action.get("params", "")
            try:
                if action_type == "replyText":
                    content = self._parse_variables(params, pack, context)
                    self._send_text(group_id, content)

                elif action_type == "replyAtText":
                    # 真实 @ 事件主体再发文本
                    content = self._parse_variables(params, pack, context)
                    target = pack.get("user_id")
                    if target is not None and str(target):
                        self._send_at_text(group_id, target, content)
                    else:
                        self._send_text(group_id, content)

                elif action_type == "replyImage":
                    parsed = self._parse_variables(params, pack, context)
                    self._send_image(group_id, parsed)

                elif action_type == "deleteMessage":
                    if pack.get("message_id") is not None:
                        self._delete_message(pack)

                elif action_type == "muteUser":
                    parsed = self._parse_variables(str(params), pack, context)
                    try:
                        duration = int(parsed)
                    except (TypeError, ValueError):
                        duration = 600
                    if pack.get("user_id"):
                        self._mute_user(pack, group_id, duration)

                elif action_type == "executeCommand":
                    cmd = self._parse_variables(params, pack, context)
                    context["result"] = self._run_command_capture(cmd)

                elif action_type == "callPluginCommand":
                    # 先切分再逐段替换，防替换值内含逗号被再次拆散
                    parts = [p.strip() for p in str(params).split(",")]
                    command = parts[0]
                    handler = self.custom_actions.get(command)
                    if handler is None:
                        self._log("error", f"[正则引擎] 插件动作 {command} 未注册")
                        continue
                    command_params = [self._parse_variables(p, pack, context) for p in parts[1:]]
                    ret = handler(command_params, pack, context)
                    if isinstance(ret, dict):
                        context.update(ret)
                    elif ret is not None:
                        context["result"] = str(ret)

                else:
                    self._log("error", f"[正则引擎] 未知动作类型: {action_type}")
            except Exception as e:
                self._log("error", f"[正则引擎] 执行动作 {action_type} 失败: {e}")

    # ------------------------------------------------------------ 执行命令
    def _command_message_text(self, message: Any) -> str:
        """把 Endstone ``Translatable`` 输出转换为可直接发送到 QQ 的文本。"""
        if isinstance(message, str):
            return message

        key = str(getattr(message, "text", "") or "")
        params = [str(value) for value in (getattr(message, "params", None) or [])]
        translated = ""
        try:
            language = getattr(getattr(self.plugin, "server", None), "language", None)
            if language is not None:
                translated = str(language.translate(message))
        except Exception:
            try:
                translated = str(self.plugin.server.language.translate(key, params))
            except Exception:
                pass

        # 部分 BDS/Endstone 缺少语言资源时 translate 返回原键，提供稳定兜底
        if translated and translated not in {key, str(message)} and not translated.startswith("commands."):
            return translated
        if key == "commands.players.list":
            if len(params) >= 2:
                return f"在线玩家：{params[0]}/{params[1]}"
            return "在线玩家列表："
        if key == "commands.players.list.names":
            names = " ".join(params).strip()
            return f"玩家：{names}" if names else "当前没有在线玩家"
        if params:
            return f"{key}：{' '.join(params)}" if key else " ".join(params)
        return key or str(message)

    def _run_command_capture(self, cmd: str) -> str:
        """在游戏主线程执行命令，捕获并翻译命令输出。"""
        cmd = str(cmd or "").strip().lstrip("/")
        if not cmd:
            return "空命令"

        outputs: list[str] = []
        errors: list[str] = []
        done = threading.Event()
        result: dict[str, Any] = {"success": False, "exception": ""}
        # 主线程路径下 run() 是同步执行的，耗时必须自己量 —— 见下方说明。
        elapsed = {"seconds": 0.0}

        def run() -> None:
            started = time.monotonic()
            try:
                def capture(msg: Any) -> None:
                    try:
                        text = self._command_message_text(msg).strip()
                        if text:
                            outputs.append(text)
                    except Exception:
                        pass

                def capture_error(msg: Any) -> None:
                    try:
                        text = self._command_message_text(msg).strip()
                        if text:
                            errors.append(text)
                    except Exception:
                        pass

                from endstone.command import CommandSenderWrapper

                # Endstone 0.11：on_message / on_error 双回调捕获正常与错误输出
                sender = CommandSenderWrapper(
                    self.plugin.server.command_sender,
                    on_message=capture,
                    on_error=capture_error,
                )
                result["success"] = bool(self.plugin.server.dispatch_command(sender, cmd))
            except Exception as exc:
                result["exception"] = str(exc)
            finally:
                elapsed["seconds"] = time.monotonic() - started
                done.set()

        timeout = max(0.1, float(self.conf().get("command_timeout", 5.0) or 5.0))
        run_on_main = None
        try:
            # 防自死锁：主线程触发时须同步执行；调度回主线程再阻塞等待会永久卡死。
            # getattr 兼容未实现该方法的测试桩。
            _on_main = getattr(self.plugin, "is_on_main_thread", None)
            if callable(_on_main) and _on_main():
                run()
            else:
                run_on_main = True
                self.plugin.run_on_main(run)
        except Exception as exc:
            return f"命令调度失败：{exc}"

        if run_on_main is not None:
            # 后台线程路径：dispatch 在别的线程跑，用 Event 等它。
            if not done.wait(timeout=timeout):
                return f"等待命令结果超时（{timeout:g} 秒），命令可能仍在执行"
        elif elapsed["seconds"] > timeout:
            # ★ 主线程路径：run() 是同步跑完的，``done`` 必然已置位，
            #   ``wait(timeout)`` 永远不会超时 —— 超时设置形同虚设，
            #   一条卡住的指令会把整个服务器拖死在这里。
            #   主线程上没法"等一会儿再放弃"（等就是阻塞），所以只能
            #   事后判定耗时是否超限，并明确告诉用户结果不可信。
            #
            #   ⚠ 这是尽力而为的护栏，不是硬性中断：真正的兜底是别写
            #     会卡住的命令。因此超时后不再往下解析输出。
            return (f"命令执行耗时 {elapsed['seconds']:.1f} 秒，超过设定的 "
                    f"{timeout:g} 秒，结果未采集（命令可能仍在执行）")

        clean_errors = re.sub(r"§.", "", "\n".join(errors)).strip()
        clean_outputs = re.sub(r"§.", "", "\n".join(outputs)).strip()
        if result.get("exception"):
            return f"命令执行异常：{result['exception']}"
        if clean_errors:
            return clean_errors

        command_name = cmd.split(None, 1)[0].casefold()
        # BDS 的 stop 部分版本返回 False 但命令已提交，无异常时不误报失败
        if command_name == "stop":
            return clean_outputs or "服务器停止指令已执行，正在安全关闭"
        if clean_outputs:
            return clean_outputs
        return "执行成功" if result.get("success") else "执行失败：命令不存在或服务器拒绝执行"

    # ------------------------------------------------------------ 触发入口
    @staticmethod
    def _rebuild_raw_message(message: Any) -> str:
        if isinstance(message, list):
            parts: list[str] = []
            for seg in message:
                if not isinstance(seg, dict) or seg.get("type") != "text":
                    continue
                data = seg.get("data")
                if isinstance(data, dict):
                    parts.append(str(data.get("text", "")))
            return "".join(parts)
        return str(message or "")

    def handle_message(self, content: str, pack: dict[str, Any]) -> bool:
        """处理一条群消息。

        返回 ``True`` 表示有 ``block=True`` 的规则命中，调用方应停止后续处理
        （不再走 custom_keywords / 状态查询 / QQ→MC 转发）。

        ⚠ 调用点必须在「绑定码 / 签到 / `/` 管理员指令」之后 ——
        前移会让正则吃掉 5 位绑定码，或让 ``executeCommand`` 绕过管理员校验。
        """
        if not self._is_enabled():
            return False

        msg_text = str(content or "")
        # ReDoS 防护：截断超长文本，限制最坏回溯规模
        if len(msg_text) > _MAX_MATCH_TEXT_LEN:
            msg_text = msg_text[:_MAX_MATCH_TEXT_LEN]
        if not msg_text.strip():
            return False

        pack = {**pack, "raw_message": pack.get("raw_message") or msg_text}

        for rule in list(self.rules):
            if not isinstance(rule, dict):
                continue
            if not rule.get("enabled") or rule.get("triggerType") != "message":
                continue
            try:
                pattern = str(rule.get("pattern") or "").strip()
                # 空 pattern 会匹配任意消息，显式跳过
                if not pattern:
                    continue
                # ReDoS 防护：超长 / 高风险 pattern 直接跳过（缓存后零重复扫描）
                regex = self._compile_cached(pattern, rule.get("flags", ""), rule.get("name"))
                if regex is None:
                    continue
                # 运行时熔断版 search：真的回溯爆炸时自动停用该规则
                match = self._safe_search(regex, msg_text, rule)
                if not match:
                    continue
                if not self._check_conditions(rule.get("conditions"), pack):
                    continue
                if self.conf().get("admin_debug"):
                    self._log("info", f"[正则引擎] 命中规则: {rule.get('name')}")
                groups = [match.group(0)] + [g or "" for g in match.groups()]
                if self._skip_duplicated(rule, self._pack_fingerprint("message", "", pack)):
                    if rule.get("block"):
                        break
                    continue
                self._execute_actions(rule.get("actions", []), pack, groups)
                if rule.get("block"):
                    return True
            except Exception as e:
                # 单条规则失败只记日志，不中断后续规则执行
                self._log("error", f"[正则引擎] 规则 [{rule.get('name')}] 执行失败: {e}")
        return False

    def handle_event(self, event_type: str, pack: dict[str, Any]) -> None:
        """处理一个事件（游戏事件 / 群成员变动）。"""
        if not self._is_enabled():
            return
        if not isinstance(pack, dict):
            return
        if not pack.get("sender"):
            # 共享 pack 被多个 handler 消费，不能原地注入假 sender，浅拷贝后注入
            pack = {
                **pack,
                "sender": {
                    "role": "member",
                    "nickname": str(pack.get("user_id", "") or "未知用户"),
                },
            }

        for rule in list(self.rules):
            if not isinstance(rule, dict):
                continue
            if (
                not rule.get("enabled")
                or rule.get("triggerType") != "event"
                or rule.get("eventType") != event_type
            ):
                continue
            try:
                pattern = str(rule.get("pattern") or "").strip()
                # 匹配目标的选择：
                #   发言 / 日志 → 匹配**正文**（raw_message）
                #   其他事件   → 匹配 user_id（玩家名 / openid）
                #
                # 日志为什么也走 raw_message：level（[ERROR] 那种）已经在
                # pack 的独立字段里，用条件过滤更准且不用用户写转义正则；
                # pattern 只管正文。两根线分开，规则才读得懂。
                if event_type in (EVENT_PLAYER_CHAT, EVENT_LOG_LINE):
                    target = str(pack.get("raw_message", ""))
                else:
                    target = str(pack.get("user_id", ""))
                # ReDoS 防护：截断 + 高风险跳过（主线程执行，回溯会冻结服务器）
                if len(target) > _MAX_MATCH_TEXT_LEN:
                    target = target[:_MAX_MATCH_TEXT_LEN]
                if pattern:
                    regex = self._compile_cached(pattern, rule.get("flags", "i"), rule.get("name"))
                    if regex is None:
                        continue
                    match = self._safe_search(regex, target, rule)
                    if not match:
                        continue
                    groups = [match.group(0)] + [g or "" for g in match.groups()]
                else:
                    # 事件规则空 pattern 表示匹配该事件全部发生；与消息规则跳过空 pattern 不同。
                    groups = [target]

                if not self._check_conditions(rule.get("conditions"), pack):
                    continue
                if self._skip_duplicated(rule, self._pack_fingerprint("event", event_type, pack)):
                    if rule.get("block"):
                        break
                    continue
                self._execute_actions(rule.get("actions", []), pack, groups)
                if rule.get("block"):
                    break
            except Exception as e:
                self._log("error", f"[正则引擎] 事件规则 [{rule.get('name')}] 执行失败: {e}")

    # ------------------------------------------------------------ 游戏事件快捷入口
    def _mc_target_group(self) -> str:
        """MC 事件规则的回复目标群。

        没有 ``group_id`` 可依据时回退到插件提供的默认群
        （见 ``plugin._qqbot_default_group``）——否则回复会被静默丢弃。
        """
        getter = getattr(self.plugin, "_qqbot_default_group", None)
        if callable(getter):
            try:
                return str(getter("") or "")
            except Exception:
                return ""
        return ""

    def _mock_pack(self, player_name: str) -> dict[str, Any]:
        # ⚠ time 参与去重指纹，缺失时同玩家重复发言会被误判为重复上报吞掉
        return {
            "user_id": player_name,
            "group_id": self._mc_target_group(),
            "card_id": "",
            "time": int(time.time()),
            "sender": {"nickname": player_name, "role": "member"},
        }

    def on_mc_player_join(self, player_name: str) -> None:
        self.handle_event(EVENT_PLAYER_JOIN, self._mock_pack(player_name))

    def on_mc_player_left(self, player_name: str) -> None:
        self.handle_event(EVENT_PLAYER_LEFT, self._mock_pack(player_name))

    def on_mc_player_chat(self, player_name: str, message: str) -> None:
        pack = self._mock_pack(player_name)
        pack["raw_message"] = str(message or "")
        pack["message"] = [{"type": "text", "data": {"text": str(message or "")}}]
        self.handle_event(EVENT_PLAYER_CHAT, pack)

    def on_group_member_join(self, pack: dict[str, Any]) -> None:
        self.handle_event(EVENT_GROUP_MEMBER_JOIN, pack)

    def on_group_member_leave(self, pack: dict[str, Any]) -> None:
        self.handle_event(EVENT_GROUP_MEMBER_LEAVE, pack)

    # ------------------------------------------------------------ 测试辅助
    def test_match(self, pattern: str, flags: str, text: str) -> dict[str, Any]:
        """网页「测试匹配」用：返回是否命中、捕获组、以及被拒绝的原因。

        ★ 这里也要做限时实测。
        这是**唯一**一个能被前端直接触发执行的正则入口（临时账号只要
        有相应板块权限就能点「测试」），如果只做静态启发式，
        攻击者用一条 ``(\\w+\\s?)*$`` 就能靠「测试」按钮把主线程打死 ——
        连保存规则那一步都省了。
        """
        pattern = str(pattern or "")
        text = str(text or "")
        if not pattern:
            return {"ok": False, "error": "pattern 为空"}
        if len(pattern) > _MAX_PATTERN_LEN:
            return {"ok": False, "error": f"pattern 超过 {_MAX_PATTERN_LEN} 字符上限"}
        if _is_risky_pattern(pattern):
            return {"ok": False, "error": "pattern 含嵌套量词或超大重复限定（ReDoS 风险），已拒绝"}
        # 关键：先跑安全探针，再拿用户输入做真实匹配
        safe, reason = probe_pattern(pattern, flags)
        if not safe:
            return {"ok": False, "error": reason}
        try:
            regex = compile_pattern(pattern, flags)
        except re.error as exc:
            return {"ok": False, "error": f"正则语法错误: {exc}"}
        if len(text) > _MAX_MATCH_TEXT_LEN:
            text = text[:_MAX_MATCH_TEXT_LEN]
        match = self._safe_search(regex, text, {"name": "(测试)", "pattern": pattern,
                                                "flags": flags})
        if not match:
            # 被熔断时返回的是 None，和「不匹配」长得一样 ——
            # 用 _tripped 区分一下，别让用户以为规则只是没命中。
            if self._rule_key({"pattern": pattern, "flags": flags}) in getattr(self, "_tripped", {}):
                return {"ok": False, "error": "该正则匹配超时，已被熔断停用（疑似灾难性回溯）"}
            return {"ok": True, "matched": False, "groups": []}
        return {
            "ok": True,
            "matched": True,
            "groups": [match.group(0)] + [g or "" for g in match.groups()],
        }

    def _log(self, level: str, message: str) -> None:
        logger = getattr(self, "logger", None)
        if logger is None:
            return
        # 外部输入（群名 / 昵称等）进日志前截断，避免日志注入与刷屏
        safe = str(message)[:800].replace("\n", " ")
        fn = getattr(logger, level, None) or getattr(logger, "info", None)
        if callable(fn):
            try:
                fn(safe)
            except Exception:
                pass
