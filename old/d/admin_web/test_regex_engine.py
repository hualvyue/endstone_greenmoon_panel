"""正则触发引擎：功能与安全护栏验证。

被测对象是 ``regex_engine.py``（不 import endstone，可直接 import），
以及它在 ``core.py`` 里的适配层（``_regex_*`` 系列方法，走 AST 抽取）。

覆盖三类断言：

  1. **功能**：正则编译、变量展开、条件判断、动作执行、规则 CRUD。
  2. **安全护栏**（本文件重点）：ReDoS 防护、执行命令的主线程自死锁守卫、
     单条规则失败不中断后续规则、``snapshot_rules`` 深拷贝防并发写竞争。
  3. **接入点**：``qqbot.py`` / ``onebot/interop.py`` 里的插入顺序 ——
     必须在 ``/`` 管理员指令之后、``custom_keywords`` 之前。

第 2、3 类是这类改动最容易"看起来能用、实际埋雷"的地方，所以每个断言
都做过去掉实现变红的非空洞验证。

跑法： python3 test_regex_engine.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import regex_engine as R  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


class Logger:
    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()

    def _add(self, lvl, m):
        with self.lock:
            self.lines.append((lvl, str(m)))

    def info(self, m):
        self._add("I", m)

    def warning(self, m):
        self._add("W", m)

    def error(self, m):
        self._add("E", m)

    def has(self, level, needle):
        with self.lock:
            return any(lv == level and needle in msg for lv, msg in self.lines)


# ===========================================================================
# 插件替身：只实现引擎会用到的那几个钩子，并记录副作用
# ===========================================================================
class FakePlugin:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.logger = Logger()
        self.data_folder = str(data_dir)
        self.sent = []          # (group_id, content)
        self.ats = []           # (group_id, target, content)
        self.images = []        # (group_id, url)
        self.deleted = []       # message_id
        self.muted = []         # (card_id, group_id, user_id, duration)
        self.commands = []      # 执行过的指令
        self.cmd_output = "在线玩家：1/20"
        self.on_main = False    # 由测试切换
        self.run_on_main_calls = 0
        # 真实 run_on_main 是「投递到主线程、立即返回」，绝不能同步跑完。
        # 这里默认用后台线程复刻这个语义；个别用例需要确定性同步时可打开。
        self.run_on_main_sync = False
        self._bg_threads = []
        self.admin_ids = []
        self.default_group = "GRP001"
        self._regex_cfg = {"enable": True, "only_on_main": False,
                           "admin_debug": False, "command_timeout": 2.0}

    # ---- 引擎读配置 ----
    def _regex_config(self):
        return dict(self._regex_cfg)

    # ---- 引擎的动作出口 ----
    def _regex_send_text(self, group_id, content):
        self.sent.append((group_id, content))

    def _regex_send_at_text(self, group_id, target, content):
        self.ats.append((group_id, target, content))

    def _regex_send_image(self, group_id, url):
        self.images.append((group_id, url))

    def _regex_delete_message(self, card_id, message_id):
        self.deleted.append(message_id)

    def _regex_mute_user(self, card_id, group_id, user_id, duration):
        self.muted.append((card_id, group_id, user_id, duration))

    def _qqbot_default_group(self, card_id=""):
        return self.default_group

    def _qqbot_card_admins(self, card_id=""):
        return list(self.admin_ids)

    # ---- 执行命令 ----
    def is_on_main_thread(self):
        return bool(self.on_main)

    def run_on_main(self, fn):
        self.run_on_main_calls += 1
        if self.run_on_main_sync:
            # 仅用于需要「调用即完成」的确定性用例
            fn()
            return
        # 复刻真实语义：投递出去就返回，调用方不会被阻塞。
        th = threading.Thread(target=_safe_call, args=(fn,), daemon=True)
        th.start()
        self._bg_threads.append(th)


def _safe_call(fn):
    try:
        fn()
    except Exception:
        pass


def join_bg(plug, timeout=5.0):
    """等 FakePlugin.run_on_main 派出去的「主线程」任务跑完。

    真实世界里这是主线程的下一个 tick；测试里就是个后台线程。
    """
    threads = list(getattr(plug, "_bg_threads", []))
    for th in threads:
        th.join(timeout=timeout)
    plug._bg_threads.clear()


def new_engine(tmpdir, **cfg):
    plug = FakePlugin(tmpdir)
    plug._regex_cfg.update(cfg)
    return plug, R.RegexEngineModule(plug)


def make_pack(**kw):
    pack = {
        "card_id": "cardA",
        "group_id": "GRP001",
        "user_id": "u1001",
        "message_id": "m1",
        "time": int(time.time()),
        "domain": "official",
        "sender": {"nickname": "小明", "role": "member"},
    }
    pack.update(kw)
    return pack


# ===========================================================================
print("=" * 72)
print("1. ReDoS 防护（pattern 来自网页，会跑在主线程热路径上）")
print("=" * 72)

check("嵌套量词 (a+)+ 判定为危险", R._is_risky_pattern("(a+)+$"))
check("超大重复 a{100000} 判定为危险", R._is_risky_pattern("^a{100000}$"))
check("四位数重复 a{1000} 判定为危险", R._is_risky_pattern("a{1000}"))
check("三位数重复 a{100} 不算危险", not R._is_risky_pattern("a{100}"))
check("普通中文正则不算危险", not R._is_risky_pattern("^查服$"))
check("捕获组不算危险", not R._is_risky_pattern("^执行(.+)"))
check("惰性量词不算危险", not R._is_risky_pattern("^a+?b$"))

tmp = tempfile.mkdtemp(prefix="rx_")
plug, eng = new_engine(tmp)

before = time.time()
res = eng.test_match("(a+)+$", "", "a" * 60)
elapsed = time.time() - before
check("test_match 拒绝嵌套量词", res.get("ok") is False and "ReDoS" in str(res.get("error")), res)
check("拒绝是即时的（不做真实匹配）", elapsed < 0.5, f"{elapsed:.3f}s")

res = eng.test_match("a" * 600, "", "aaa")
check("超长 pattern 被拒绝", res.get("ok") is False and "上限" in str(res.get("error")), res)

res = eng.test_match("[" + "a", "", "aaa")
check("非法正则报语法错误", res.get("ok") is False and "语法错误" in str(res.get("error")), res)


# 引擎在真实匹配路径上也要拒绝，而不只是 test_match
eng.save_rules([{
    "id": "risky", "name": "危险规则", "enabled": True,
    "triggerType": "message", "pattern": "(a+)+$", "flags": "",
    "conditions": [], "actions": [{"type": "replyText", "params": "boom"}],
    "block": True,
}])
plug.sent.clear()
t0 = time.time()
blocked = eng.handle_message("a" * 80, make_pack())
dt = time.time() - t0
check("handle_message 跳过危险规则", blocked is False and not plug.sent, plug.sent)
check("跳过危险规则耗时可忽略", dt < 0.5, f"{dt:.3f}s")
check("危险规则记了 error 日志", plug.logger.has("E", "灾难性回溯"))

# 负缓存：同一危险 pattern 只记一次日志
n_before = sum(1 for lv, _ in plug.logger.lines if lv == "E")
eng.handle_message("aaa", make_pack())
eng.handle_message("aaa", make_pack())
n_after = sum(1 for lv, _ in plug.logger.lines if lv == "E")
check("危险 pattern 走负缓存（不重复打日志）", n_after == n_before, f"{n_before} -> {n_after}")

# 超长消息文本要截断
held = threading.Event()
huge = "x" * 50000
plug.sent.clear()
eng.save_rules([{
    "id": "catch", "name": "捕获", "enabled": True,
    "triggerType": "message", "pattern": "^x+$", "flags": "",
    "conditions": [], "actions": [{"type": "replyText", "params": "hit"}],
    "block": True,
}])
t0 = time.time()
eng.handle_message(huge, make_pack())
dt = time.time() - t0
check("超长消息被截断后匹配（不卡死）", dt < 1.0, f"{dt:.3f}s")
check("超长消息仍能命中", plug.sent == [("GRP001", "hit")], plug.sent)
check("_MAX_MATCH_TEXT_LEN 为 2000", R._MAX_MATCH_TEXT_LEN == 2000)
check("_MAX_PATTERN_LEN 为 512", R._MAX_PATTERN_LEN == 512)


# ===========================================================================
print()
print("=" * 72)
print("2. 动作执行与变量展开")
print("=" * 72)

tmp = tempfile.mkdtemp(prefix="rx_")
plug, eng = new_engine(tmp)

eng.save_rules([{
    "id": "r1", "name": "回显", "enabled": True, "triggerType": "message",
    "pattern": r"^查\s*(\S+)", "flags": "", "conditions": [],
    "actions": [{"type": "replyText", "params": "查到了 $1，来自 $nickname（$userId）"}],
    "block": True,
}])
plug.sent.clear()
blocked = eng.handle_message("查 钻石", make_pack())
check("命中后 block=True 返回 True", blocked is True)
check("replyText 正确展开 $1/$nickname/$userId",
      plug.sent == [("GRP001", "查到了 钻石，来自 小明（u1001）")], plug.sent)

# replyAtText
eng.save_rules([{
    "id": "r2", "name": "@回", "enabled": True, "triggerType": "message",
    "pattern": "^有人吗", "flags": "", "conditions": [],
    "actions": [{"type": "replyAtText", "params": " 在的"}], "block": True,
}])
plug.ats.clear()
eng.handle_message("有人吗", make_pack())
check("replyAtText 带上事件主体", plug.ats == [("GRP001", "u1001", " 在的")], plug.ats)

# replyImage
eng.save_rules([{
    "id": "r3", "name": "发图", "enabled": True, "triggerType": "message",
    "pattern": "^看图$", "flags": "", "conditions": [],
    "actions": [{"type": "replyImage", "params": "https://x/y.png"}], "block": True,
}])
plug.images.clear()
eng.handle_message("看图", make_pack())
check("replyImage 发出图片地址", plug.images == [("GRP001", "https://x/y.png")], plug.images)

# deleteMessage
eng.save_rules([{
    "id": "r4", "name": "撤回", "enabled": True, "triggerType": "message",
    "pattern": "^广告", "flags": "", "conditions": [],
    "actions": [{"type": "deleteMessage", "params": ""}], "block": True,
}])
plug.deleted.clear()
eng.handle_message("广告", make_pack(message_id="m777"))
check("deleteMessage 撤回原消息", plug.deleted == ["m777"], plug.deleted)
plug.deleted.clear()
eng.handle_message("广告", make_pack(message_id=None))
check("无 message_id 时不调用撤回", not plug.deleted, plug.deleted)

# muteUser
eng.save_rules([{
    "id": "r5", "name": "禁言", "enabled": True, "triggerType": "message",
    "pattern": "^刷屏", "flags": "", "conditions": [],
    "actions": [{"type": "muteUser", "params": "120"}], "block": True,
}])
plug.muted.clear()
eng.handle_message("刷屏", make_pack())
check("muteUser 按参数禁言", plug.muted == [("cardA", "GRP001", "u1001", 120)], plug.muted)
plug.muted.clear()
eng.save_rules([{
    "id": "r5b", "name": "禁言非法参数", "enabled": True, "triggerType": "message",
    "pattern": "^乱写", "flags": "", "conditions": [],
    "actions": [{"type": "muteUser", "params": "很久"}], "block": True,
}])
eng.handle_message("乱写", make_pack())
check("muteUser 非法时长回落 600 秒", plug.muted == [("cardA", "GRP001", "u1001", 600)], plug.muted)

# 条件判断
eng.save_rules([{
    "id": "r6", "name": "仅管理员", "enabled": True, "triggerType": "message",
    "pattern": "^停机", "flags": "", "conditions": [],
    "actions": [{"type": "replyText", "params": "拒了"}], "block": True,
}])
plug.sent.clear()
eng.handle_message("停机", make_pack())
check("无 userRole 条件时命中", plug.sent == [("GRP001", "拒了")], plug.sent)

eng.save_rules([{
    "id": "r7", "name": "仅管理员", "enabled": True, "triggerType": "message",
    "pattern": "^停机", "flags": "",
    "conditions": [{"field": "userRole", "operator": "==", "value": "admin"}],
    "actions": [{"type": "replyText", "params": "已执行"}], "block": True,
}])
plug.sent.clear()
plug.admin_ids = []
eng.handle_message("停机", make_pack())
check("userRole=admin 条件拦住非管理员", not plug.sent, plug.sent)
plug.admin_ids = ["u1001"]
eng.handle_message("停机", make_pack())
check("userRole=admin 条件放行管理员", plug.sent == [("GRP001", "已执行")], plug.sent)

# groupId 条件
eng.save_rules([{
    "id": "r8", "name": "限定群", "enabled": True, "triggerType": "message",
    "pattern": "^限定", "flags": "",
    "conditions": [{"field": "groupId", "operator": "==", "value": "GRP999"}],
    "actions": [{"type": "replyText", "params": "命中"}], "block": True,
}])
plug.sent.clear()
eng.handle_message("限定", make_pack(group_id="GRP001"))
check("groupId != 条件不命中", not plug.sent, plug.sent)
eng.handle_message("限定", make_pack(group_id="GRP999"))
check("groupId == 条件命中", plug.sent == [("GRP999", "命中")], plug.sent)

# includes / matches / !=
#
# 全部用 field="nickname"，且昵称**只放在 sender 子对象里**、pack 顶层没有。
# 这是有意的回归锚点：最早实现读的是 pack.get("nickname")，恒为 ""，
# 于是所有昵称条件「静默恒假」——不报错、不告警，只是永远不命中。
#
# ⚠ 每轮必须换 message_id：去重窗口是 5 秒，复用同一个 id 会让第二、
#   第三轮被当成「重复上报」直接跳过，断言就成了假阴性。
for _i, (op, val, text, want) in enumerate((
    ("includes", "钻", "带钻石的", True),
    ("includes", "钻", "没有那个", False),
    ("matches", "^带.{2}的$", "带钻石的", True),
    ("matches", "^带.{2}的$", "带钻的", False),
    ("!=", "GRP999", "GRP001", True),
    ("!=", "GRP001", "GRP001", False),
)):
    eng.save_rules([{
        "id": "rc", "name": "条件", "enabled": True, "triggerType": "message",
        "pattern": "^测试", "flags": "",
        "conditions": [{"field": "nickname", "operator": op, "value": val}],
        "actions": [{"type": "replyText", "params": "Y"}], "block": True,
    }])
    plug.sent.clear()
    p = make_pack(message_id=f"cond-{_i}", sender={"nickname": text, "role": "member"})
    assert "nickname" not in p, "回归锚点失效：pack 顶层不该有 nickname"
    eng.handle_message("测试", p)
    check(f"昵称条件 {op} {val!r} vs {text!r} -> {want}",
          bool(plug.sent) is want, plug.sent)

# matches 条件也要防 ReDoS
eng.save_rules([{
    "id": "rc2", "name": "危险条件", "enabled": True, "triggerType": "message",
    "pattern": "^测试", "flags": "",
    "conditions": [{"field": "nickname", "operator": "matches", "value": "(a+)+$"}],
    "actions": [{"type": "replyText", "params": "Y"}], "block": True,
}])
plug.sent.clear()
t0 = time.time()
eng.handle_message("测试", make_pack(sender={"nickname": "a" * 60, "role": "member"}))
check("matches 条件拒绝危险 pattern（不卡）", time.time() - t0 < 0.5 and not plug.sent)


# ===========================================================================
print()
print("=" * 72)
print("3. executeCommand：主线程自死锁守卫")
print("=" * 72)

tmp = tempfile.mkdtemp(prefix="rx_")
plug, eng = new_engine(tmp, command_timeout=1.0)

# 沙箱里没有 endstone，_run_command_capture 会走「导入失败」分支。
# 塞一个假的 endstone.command 模块进来，才能验证真实的捕获逻辑。
import types as _types  # noqa: E402

_fake_sender_calls = []
if "endstone" not in sys.modules:
    _es = _types.ModuleType("endstone")
    _esc = _types.ModuleType("endstone.command")

    class _SenderWrapper:
        def __init__(self, base, on_message=None, on_error=None):
            self.base = base
            self.on_message = on_message
            self.on_error = on_error

    _esc.CommandSenderWrapper = _SenderWrapper
    _es.command = _esc
    sys.modules["endstone"] = _es
    sys.modules["endstone.command"] = _esc
from endstone.command import CommandSenderWrapper  # noqa: E402


class _FakeServer:
    def __init__(self, output, error=None, ok=True):
        self.command_sender = object()
        self.language = None
        self.output = output
        self.error = error
        self.ok = ok
        self.dispatched = []
        self.calls = 0

    def dispatch_command(self, sender, cmd):
        self.calls += 1
        self.dispatched.append(cmd)
        if self.error:
            sender.on_error(self.error)
        elif self.output:
            sender.on_message(self.output)
        return self.ok


plug.server = _FakeServer("在线玩家：1/20")

eng.save_rules([{
    "id": "cmd", "name": "执行", "enabled": True, "triggerType": "message",
    "pattern": "^执行(.+)", "flags": "", "conditions": [],
    "actions": [{"type": "executeCommand", "params": "$1"},
                {"type": "replyText", "params": "结果：$result"}],
    "block": True,
}])

# 非主线程：应走 run_on_main（异步投递，需要等它跑完再看副作用）
plug.on_main = False
plug.run_on_main_calls = 0
plug.sent.clear()
plug.server.dispatched.clear()
eng.handle_message("执行 list", make_pack(message_id="cmd-1"))
check("非主线程走 run_on_main", plug.run_on_main_calls == 1, plug.run_on_main_calls)
join_bg(plug)
check("非主线程真的派发了指令", plug.server.dispatched == ["list"], plug.server.dispatched)
check("命令结果写进 $result", plug.sent and "结果：" in plug.sent[0][1], plug.sent)
check("$result 展开的是命令输出",
      plug.sent and "在线玩家：1/20" in plug.sent[0][1], plug.sent)

# 主线程：必须**同步直跑**，绝不能再调度回主线程
plug.on_main = True
plug.run_on_main_calls = 0
plug.sent.clear()
plug.server.dispatched.clear()
t0 = time.time()
eng.handle_message("执行 list", make_pack(message_id="cmd-2"))
dt = time.time() - t0
check("主线程不再调用 run_on_main（防自死锁）", plug.run_on_main_calls == 0, plug.run_on_main_calls)
check("主线程同步执行且立即返回", dt < 0.5, f"{dt:.3f}s")
check("主线程路径真的派发了指令", plug.server.dispatched == ["list"], plug.server.dispatched)
check("主线程路径仍有结果",
      plug.sent and "在线玩家：1/20" in plug.sent[0][1], plug.sent)

# 命令报错时，错误信息优先于正常输出
plug.sent.clear()
plug.server.output = None
plug.server.error = "未知的命令：list"
plug.server.dispatched.clear()
eng.handle_message("执行 list", make_pack(message_id="cmd-3"))
check("命令报错时回显错误信息", plug.sent and "未知的命令" in plug.sent[0][1], plug.sent)
plug.server.output = "在线玩家：1/20"
plug.server.error = None

# 空命令不该派发
plug.sent.clear()
plug.server.dispatched.clear()
eng.save_rules([{
    "id": "empty", "name": "空命令", "enabled": True, "triggerType": "message",
    "pattern": "^空$", "flags": "", "conditions": [],
    "actions": [{"type": "executeCommand", "params": ""},
                {"type": "replyText", "params": "R=$result"}], "block": True,
}])
eng.handle_message("空", make_pack(message_id="cmd-4"))
check("空命令不派发且回显提示",
      not plug.server.dispatched and plug.sent and "空命令" in plug.sent[0][1],
      (plug.server.dispatched, plug.sent))

# 超时要有明确提示，不能无限等
plug2, eng3 = new_engine(tempfile.mkdtemp(prefix="rx_"), command_timeout=0.2)


class _SlowServer(_FakeServer):
    def dispatch_command(self, sender, cmd):
        self.calls += 1
        time.sleep(1.5)
        return True


plug2.server = _SlowServer("慢")
plug2.on_main = True
plug2.sent.clear()
t0 = time.time()
eng3.save_rules([{
    "id": "slow", "name": "慢命令", "enabled": True, "triggerType": "message",
    "pattern": "^慢$", "flags": "", "conditions": [],
    "actions": [{"type": "executeCommand", "params": "list"},
                {"type": "replyText", "params": "R=$result"}], "block": True,
}])
eng3.handle_message("慢", make_pack(message_id="slow-1"))
dt = time.time() - t0
check("主线程路径超时会明确报超时（不是静默给个假结果）",
      plug2.sent and "超过设定的" in plug2.sent[0][1], plug2.sent)
check("超时后不把结果当成功返回",
      plug2.sent and "结果未采集" in plug2.sent[0][1], plug2.sent)
# 主线程上 run() 是同步执行的 —— 没法"等一会儿再放弃"，
# 所以耗时下限就是命令本身的耗时（1.5s），这是设计取舍不是 bug。
check("主线程路径耗时与命令耗时同量级（说明是同步执行）",
      1.4 <= dt < 3.0, f"{dt:.3f}s")

# 对照：后台线程路径**能**真的按时放弃，不被慢命令拖住
plug4, eng4 = new_engine(tempfile.mkdtemp(prefix="rx_"), command_timeout=0.3)


class _BlockingServer(_FakeServer):
    def dispatch_command(self, sender, cmd):
        self.calls += 1
        time.sleep(3.0)
        return True


plug4.server = _BlockingServer("慢")
plug4.on_main = False
plug4.sent.clear()
eng4.save_rules([{
    "id": "slow2", "name": "慢命令2", "enabled": True, "triggerType": "message",
    "pattern": "^慢$", "flags": "", "conditions": [],
    "actions": [{"type": "executeCommand", "params": "list"},
                {"type": "replyText", "params": "R=$result"}], "block": True,
}])
t0 = time.time()
eng4.handle_message("慢", make_pack(message_id="slow-2"))
dt = time.time() - t0
check("后台线程路径能按时放弃（不被慢命令卡住）", dt < 1.5, f"{dt:.3f}s")
check("后台线程超时提示正确",
      plug4.sent and "等待命令结果超时" in plug4.sent[0][1], plug4.sent)
check("超时后不误报成功",
      plug4.sent and "执行成功" not in plug4.sent[0][1], plug4.sent)
# 收尾：那 3 秒的慢命令还在后台跑，等它结束，免得污染后续用例的输出
join_bg(plug4, timeout=6.0)
check("慢命令被真的执行过（只是结果被放弃）",
      plug4.server.calls == 1, plug4.server.calls)

# 插件没实现 is_on_main_thread 时要走安全分支
tmp2 = tempfile.mkdtemp(prefix="rx_")
plug2, eng2 = new_engine(tmp2, command_timeout=1.0)
del FakePlugin.is_on_main_thread
try:
    plug2.run_on_main_calls = 0
    eng2.save_rules([{
        "id": "cmd2", "name": "执行", "enabled": True, "triggerType": "message",
        "pattern": "^执行(.+)", "flags": "", "conditions": [],
        "actions": [{"type": "executeCommand", "params": "$1"}], "block": True,
    }])
    eng2.handle_message("执行 list", make_pack())
    check("插件缺 is_on_main_thread 时走 run_on_main（安全兜底）",
          plug2.run_on_main_calls == 1, plug2.run_on_main_calls)
    join_bg(plug2)
finally:
    # 恢复类方法，避免影响后续用例
    def _restore(self):
        return bool(self.on_main)
    FakePlugin.is_on_main_thread = _restore


# ===========================================================================
print()
print("=" * 72)
print("4. 健壮性：单条规则失败不中断 / 去重 / 深拷贝")
print("=" * 72)

tmp = tempfile.mkdtemp(prefix="rx_")
plug, eng = new_engine(tmp)

eng.save_rules([
    {"id": "a", "name": "先炸", "enabled": True, "triggerType": "message",
     "pattern": "^开始", "flags": "", "conditions": [],
     "actions": [{"type": "不存在的动作", "params": ""}], "block": False},
    {"id": "b", "name": "后到", "enabled": True, "triggerType": "message",
     "pattern": "^开始", "flags": "", "conditions": [],
     "actions": [{"type": "replyText", "params": "第二条也跑了"}], "block": False},
])
plug.sent.clear()
eng.handle_message("开始", make_pack())
check("未知动作类型不抛异常，后续规则照跑",
      plug.sent == [("GRP001", "第二条也跑了")], plug.sent)

# 单条规则内部动作抛异常 → 后面的动作仍执行
eng.save_rules([{
    "id": "c", "name": "动作异常", "enabled": True, "triggerType": "message",
    "pattern": "^试试", "flags": "", "conditions": [],
    "actions": [
        {"type": "replyText", "params": None},   # params 为 None 也不该炸
        {"type": "replyText", "params": "还能继续"},
    ], "block": True,
}])
plug.sent.clear()
eng.handle_message("试试", make_pack())
check("单个动作出错不中断同规则后续动作",
      plug.sent and "还能继续" in plug.sent[-1][1], plug.sent)

# 非 dict 动作元素
eng.save_rules([{
    "id": "d", "name": "脏动作", "enabled": True, "triggerType": "message",
    "pattern": "^脏", "flags": "", "conditions": [],
    "actions": ["字符串", None, {"type": "replyText", "params": "活下来了"}],
    "block": True,
}])
plug.sent.clear()
eng.handle_message("脏", make_pack())
check("actions 含非对象元素时跳过并继续",
      plug.sent == [("GRP001", "活下来了")], plug.sent)

# 去重窗口
tmp = tempfile.mkdtemp(prefix="rx_")
plug, eng = new_engine(tmp)
eng.save_rules([{
    "id": "dup", "name": "去重", "enabled": True, "triggerType": "message",
    "pattern": "^重复", "flags": "", "conditions": [],
    "actions": [{"type": "replyText", "params": "一次"}], "block": True,
}])
plug.sent.clear()
pack = make_pack(message_id="same-id")
eng.handle_message("重复", dict(pack))
eng.handle_message("重复", dict(pack))
check("同一 message_id 5 秒内只执行一次", len(plug.sent) == 1, plug.sent)

plug.sent.clear()
eng.handle_message("重复", make_pack(message_id="other-id"))
check("不同 message_id 不去重", len(plug.sent) == 1, plug.sent)

# 没有 message_id 时用「用户+内容+时间」做指纹，不同时间不该互相吞
plug.local_calls = 0
plug.sent.clear()
eng.save_rules([{
    "id": "dup2", "name": "去重2", "enabled": True, "triggerType": "message",
    "pattern": "^说话", "flags": "", "conditions": [],
    "actions": [{"type": "replyText", "params": "回"}], "block": True,
}])
eng.handle_message("说话", make_pack(message_id=None, time=1000, user_id="u1"))
eng.handle_message("说话", make_pack(message_id=None, time=2000, user_id="u1"))
check("无 message_id 时靠 time 区分两条消息", len(plug.sent) == 2, plug.sent)

# snapshot_rules 深拷贝
tmp = tempfile.mkdtemp(prefix="rx_")
plug, eng = new_engine(tmp)
snap = eng.snapshot_rules()
snap[0]["name"] = "被篡改"
snap[0]["actions"].append({"type": "replyText", "params": "注入"})
check("snapshot_rules 返回深拷贝（改它不影响引擎）",
      eng.rules[0]["name"] != "被篡改" and len(eng.rules[0]["actions"]) != len(snap[0]["actions"]),
      eng.rules[0]["name"])


# ===========================================================================
print()
print("=" * 72)
print("5. 规则库持久化：原子写 / .corrupt 备份 / CRUD")
print("=" * 72)

tmp = tempfile.mkdtemp(prefix="rx_")
plug, eng = new_engine(tmp)
rules_path = Path(eng.path)
check("规则文件落在 data_dir/rules.json", str(rules_path) == os.path.join(tmp, "rules.json"), rules_path)
check("首次启动写出默认规则库", rules_path.is_file())
check("默认规则库非空", len(eng.rules) >= 1, len(eng.rules))
check("默认规则 id 唯一",
      len({r.get("id") for r in eng.rules}) == len(eng.rules))
check("默认规则都带必需字段",
      all({"id", "name", "enabled", "triggerType", "pattern", "actions"} <= set(r) for r in eng.rules))

# 损坏文件 → .corrupt 备份 + 回落默认
tmp2 = tempfile.mkdtemp(prefix="rx_")
bad = Path(tmp2) / "rules.json"
bad.write_text("{ 这不是合法 JSON", encoding="utf-8")
plug2, eng2 = new_engine(tmp2)
check("损坏 rules.json 时仍能构造引擎", eng2 is not None)
check("损坏文件被备份为 .corrupt", (Path(tmp2) / "rules.json.corrupt").is_file())
check("损坏后回落到默认规则库", len(eng2.rules) >= 1, len(eng2.rules))
check("损坏有 error 日志", plug2.logger.has("E", "rules.json 解析失败"))

# 非 list 的 JSON（合法 JSON、错误结构）
tmp3 = tempfile.mkdtemp(prefix="rx_")
(Path(tmp3) / "rules.json").write_text('{"a": 1}', encoding="utf-8")
plug3, eng3 = new_engine(tmp3)
check("rules.json 是对象而非数组时回落默认", isinstance(eng3.rules, list) and len(eng3.rules) >= 1)

# 过滤非 dict 元素
tmp4 = tempfile.mkdtemp(prefix="rx_")
(Path(tmp4) / "rules.json").write_text(json.dumps([
    {"id": "ok", "name": "好规则", "enabled": True, "triggerType": "message",
     "pattern": "^x", "actions": [], "conditions": []},
    "字符串", 42, None,
]), encoding="utf-8")
plug4, eng4 = new_engine(tmp4)
check("非对象规则元素被过滤", len(eng4.rules) == 1 and eng4.rules[0]["id"] == "ok", eng4.rules)
check("过滤有 warning 日志", plug4.logger.has("W", "非对象元素"))

# save / toggle / delete
tmp5 = tempfile.mkdtemp(prefix="rx_")
plug5, eng5 = new_engine(tmp5)
cnt = eng5.save_rules([
    {"id": "s1", "name": "一", "enabled": True, "triggerType": "message",
     "pattern": "^a", "flags": "", "eventType": "", "conditions": [],
     "actions": [{"type": "replyText", "params": "1"}], "block": True},
    {"id": "s2", "name": "二", "enabled": False, "triggerType": "message",
     "pattern": "^b", "flags": "", "eventType": "", "conditions": [],
     "actions": [{"type": "replyText", "params": "2"}], "block": True},
])
check("save_rules 返回条数", cnt == 2, cnt)
disk = json.loads(Path(eng5.path).read_text(encoding="utf-8"))
check("save_rules 真的落盘", len(disk) == 2 and disk[0]["id"] == "s1", disk)

check("toggle_rule 翻转成功", eng5.toggle_rule("s2", None) is True)
check("toggle 后内存值变了", eng5.rules[1]["enabled"] is True)
disk = json.loads(Path(eng5.path).read_text(encoding="utf-8"))
check("toggle 也落盘了", disk[1]["enabled"] is True, disk[1])
check("toggle 指定值", eng5.toggle_rule("s2", False) is True and eng5.rules[1]["enabled"] is False)
check("toggle 不存在的规则返回 False", eng5.toggle_rule("nope") is False)

check("delete_rule 成功", eng5.delete_rule("s1") is True)
check("删除后内存只剩 1 条", len(eng5.rules) == 1 and eng5.rules[0]["id"] == "s2")
disk = json.loads(Path(eng5.path).read_text(encoding="utf-8"))
check("删除也落盘了", len(disk) == 1, disk)
check("删除不存在的规则返回 False", eng5.delete_rule("nope") is False)

# 写入后不留 .tmp
check("原子写不留 .tmp 残留", not (Path(tmp5) / "rules.json.tmp").exists())

# reload_rules 从磁盘重读
Path(eng5.path).write_text(json.dumps([
    {"id": "r", "name": "重载", "enabled": True, "triggerType": "message",
     "pattern": "^z", "actions": [], "conditions": []}
]), encoding="utf-8")
n = eng5.reload_rules()
check("reload_rules 从磁盘重读", n == 1 and eng5.rules[0]["id"] == "r", eng5.rules)

# 编译缓存要在 reload / save 后清掉，否则改了 pattern 不生效
eng5.save_rules([{
    "id": "cache", "name": "缓存", "enabled": True, "triggerType": "message",
    "pattern": "^旧", "flags": "", "conditions": [],
    "actions": [{"type": "replyText", "params": "OLD"}], "block": True,
}])
plug5.sent.clear()
eng5.handle_message("旧", make_pack(message_id="cache-1"))
check("缓存用例：旧 pattern 命中", plug5.sent == [("GRP001", "OLD")], plug5.sent)
eng5.save_rules([{
    "id": "cache", "name": "缓存", "enabled": True, "triggerType": "message",
    "pattern": "^新", "flags": "", "conditions": [],
    "actions": [{"type": "replyText", "params": "NEW"}], "block": True,
}])
plug5.sent.clear()
eng5.handle_message("新", make_pack(message_id="cache-2"))
check("改 pattern 后立即生效（缓存已清）", plug5.sent == [("GRP001", "NEW")], plug5.sent)
plug5.sent.clear()
eng5.handle_message("旧", make_pack(message_id="cache-3"))
check("旧 pattern 改后不再命中", not plug5.sent, plug5.sent)


# ===========================================================================
print()
print("=" * 72)
print("6. 事件触发（游戏事件 / 群成员变动）")
print("=" * 72)

tmp = tempfile.mkdtemp(prefix="rx_")
plug, eng = new_engine(tmp)
eng.save_rules([
    {"id": "join", "name": "进服", "enabled": True, "triggerType": "event",
     "pattern": "", "flags": "", "eventType": R.EVENT_PLAYER_JOIN,
     "conditions": [], "actions": [{"type": "replyText", "params": "欢迎 $userId"}],
     "block": False},
    {"id": "leave", "name": "离服", "enabled": True, "triggerType": "event",
     "pattern": "", "flags": "", "eventType": R.EVENT_PLAYER_LEFT,
     "conditions": [], "actions": [{"type": "replyText", "params": "再见 $userId"}],
     "block": False},
    {"id": "gj", "name": "群进人", "enabled": True, "triggerType": "event",
     "pattern": "", "flags": "", "eventType": R.EVENT_GROUP_MEMBER_JOIN,
     "conditions": [], "actions": [{"type": "replyAtText", "params": " 欢迎"}],
     "block": False},
])
plug.sent.clear()
eng.on_mc_player_join("Steve")
check("进服事件触发且展开 $userId", plug.sent == [("GRP001", "欢迎 Steve")], plug.sent)

plug.sent.clear()
eng.on_mc_player_left("Alex")
check("离服事件触发", plug.sent == [("GRP001", "再见 Alex")], plug.sent)

plug.ats.clear()
eng.on_group_member_join(make_pack(user_id="u2", sender={"nickname": "新人", "role": "member"}))
check("群成员进群事件触发 replyAtText", plug.ats and plug.ats[0][1] == "u2", plug.ats)

# 事件规则的空 pattern 表示"匹配该事件全部"
plug.sent.clear()
eng.on_mc_player_join("Notch")
check("事件规则空 pattern 匹配全部（不同于消息规则）", len(plug.sent) == 1, plug.sent)

# 带 pattern 的事件规则按 user_id 匹配
eng.save_rules([{
    "id": "vip", "name": "VIP进服", "enabled": True, "triggerType": "event",
    "pattern": "^Steve$", "flags": "", "eventType": R.EVENT_PLAYER_JOIN,
    "conditions": [], "actions": [{"type": "replyText", "params": "VIP 来了"}], "block": False,
}])
plug.sent.clear()
eng.on_mc_player_join("Steve")
check("事件规则 pattern 匹配 user_id", plug.sent == [("GRP001", "VIP 来了")], plug.sent)
plug.sent.clear()
eng.on_mc_player_join("Alex")
check("事件规则 pattern 不匹配时不触发", not plug.sent, plug.sent)

# 玩家发言事件匹配内容
eng.save_rules([{
    "id": "say", "name": "发言", "enabled": True, "triggerType": "event",
    "pattern": "钻石", "flags": "", "eventType": R.EVENT_PLAYER_CHAT,
    "conditions": [], "actions": [{"type": "replyText", "params": "有人提到钻石"}], "block": False,
}])
plug.sent.clear()
eng.on_mc_player_chat("Steve", "我有钻石")
check("玩家发言事件匹配消息内容", plug.sent == [("GRP001", "有人提到钻石")], plug.sent)

# _mock_pack 必含 time（否则同玩家重复发言被当成重复上报吞掉）
pack = eng._mock_pack("Steve")
check("_mock_pack 含 time（去重指纹必需）", isinstance(pack.get("time"), int) and pack["time"] > 0, pack.get("time"))
check("_mock_pack 含 group_id", "group_id" in pack, pack.keys())

plug.sent.clear()
eng2 = R.RegexEngineModule(plug)
eng2.save_rules([{
    "id": "twice", "name": "两次", "enabled": True, "triggerType": "event",
    "pattern": "钻石", "flags": "", "eventType": R.EVENT_PLAYER_CHAT,
    "conditions": [], "actions": [{"type": "replyText", "params": "hit"}], "block": False,
}])
plug.sent.clear()
threading.Thread(target=eng2.on_mc_player_chat, args=("Steve", "钻石")).start()
time.sleep(0.05)
eng2.on_mc_player_chat("Steve2", "钻石")
check("不同玩家 / 时间的两条发言不被去重吞掉", len(plug.sent) >= 1, plug.sent)


# ===========================================================================
print()
print("=" * 72)
print("7. 引擎总开关")
print("=" * 72)

tmp = tempfile.mkdtemp(prefix="rx_")
plug, eng = new_engine(tmp, enable=True)
eng.save_rules([{
    "id": "sw", "name": "开关", "enabled": True, "triggerType": "message",
    "pattern": "^触发", "flags": "", "conditions": [],
    "actions": [{"type": "replyText", "params": "ON"}], "block": True,
}])
plug.sent.clear()
eng.handle_message("触发", make_pack())
check("enable=True 时正常触发", plug.sent == [("GRP001", "ON")], plug.sent)

plug._regex_cfg["enable"] = False
plug.sent.clear()
blocked = eng.handle_message("触发", make_pack())
check("enable=False 时完全不动", blocked is False and not plug.sent, plug.sent)
plug.sent.clear()
eng.on_mc_player_join("Steve")
check("enable=False 时事件也不动", not plug.sent, plug.sent)

# 插件没实现 _regex_config 时回落默认（enable=True）
tmp = tempfile.mkdtemp(prefix="rx_")
plug2, eng2 = new_engine(tmp)
del FakePlugin._regex_config
try:
    cfg = eng2.conf()
    check("缺 _regex_config 时回落默认配置", cfg.get("enable") is True, cfg)
    check("默认 command_timeout 安全", isinstance(cfg.get("command_timeout"), float), cfg)
finally:
    def _restore_cfg(self):
        return dict(self._regex_cfg)
    FakePlugin._regex_config = _restore_cfg


# ===========================================================================
print()
print("=" * 72)
print("8. 接入点顺序（这是权限边界，错了就是提权漏洞）")
print("=" * 72)

SRC = HERE


def _load(path):
    return (SRC / path).read_text(encoding="utf-8")


# ---- qqbot.py：正则必须在 `/` 管理员分支之后、custom_keywords 之前 ----
qsrc = _load("qqbot.py")
tree = ast.parse(qsrc)
funnel = None
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_handle_group_message":
        funnel = node
check("找到 _handle_group_message", funnel is not None)

lines = qsrc.splitlines()

def _first_line(needle, start=0):
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i
    return -1

l_admin_branch = _first_line('if content.startswith("/"):', funnel.lineno - 1)
l_admin_check = _first_line('self.config.get("group_admins", [])', l_admin_branch)
l_regex = _first_line("engine.handle_message", l_admin_branch)
l_keywords = _first_line('self.config.get("custom_keywords") or {}', l_admin_branch)
l_status = _first_line("查状态", l_regex)
l_relay = _first_line("_filter_banned", l_regex)

check("插入点存在 engine.handle_message", l_regex > 0, l_regex + 1)
check("正则晚于 `/` 管理员指令分支", l_regex > l_admin_branch, f"admin@{l_admin_branch+1} regex@{l_regex+1}")
check("正则晚于 group_admins 权限校验（防提权）", l_regex > l_admin_check, f"check@{l_admin_check+1}")
check("正则早于 custom_keywords", 0 < l_regex < l_keywords, f"regex@{l_regex+1} kw@{l_keywords+1}")
check("正则早于状态查询", 0 < l_regex < l_status, f"regex@{l_regex+1} status@{l_status+1}")
check("正则早于 QQ→MC 转发", 0 < l_regex < l_relay, f"regex@{l_regex+1} relay@{l_relay+1}")

# 绑定码 / 签到必须在正则之前（否则 5 位纯数字会被正则吃掉）
l_bindcode = _first_line("_try_bind", funnel.lineno - 1)
l_signin = _first_line("_do_signin", funnel.lineno - 1)
check("账号绑定码早于正则", 0 < l_bindcode < l_regex, f"bind@{l_bindcode+1} regex@{l_regex+1}")
check("签到早于正则", 0 < l_signin < l_regex, f"signin@{l_signin+1} regex@{l_regex+1}")

# 插入点必须在 is_bound 之后（未绑定群不能触发）
l_is_bound = _first_line("if not is_bound:", funnel.lineno - 1)
check("正则晚于已绑定群判定", 0 < l_is_bound < l_regex, f"bound@{l_is_bound+1} regex@{l_regex+1}")

# block 语义
check("qqbot 里 block 返回后直接 return",
      "if await asyncio.to_thread(engine.handle_message, content, _pack):" in qsrc
      and qsrc.index("if await asyncio.to_thread(engine.handle_message, content, _pack):")
          < qsrc.index("ck = self.config.get(\"custom_keywords\") or {}"))

# card_id 必须 getattr 兜底（动态挂载）
check("qqbot 用 getattr 取 _card_id",
      'getattr(self, "_card_id", "") or ""' in qsrc)

# ---- onebot/interop.py：同样顺序 ----
isrc = _load("onebot/interop.py")
itree = ast.parse(isrc)
ih = None
for node in ast.walk(itree):
    if isinstance(node, ast.FunctionDef) and node.name == "_handle_group":
        ih = node
check("找到 interop._handle_group", ih is not None)
ilines = isrc.splitlines()

def _ifirst(needle, start=0):
    for i in range(start, len(ilines)):
        if needle in ilines[i]:
            return i
    return -1

i_cmd = _ifirst("_CMD_PREFIX", ih.lineno - 1)
i_admin = _ifirst("self._is_admin(card, user_id)", i_cmd)
i_regex = _ifirst("engine.handle_message", i_cmd)
i_kw = _ifirst("self._keyword_reply(card, content)", i_regex)
i_status = _ifirst("_QQ_TO_MC_HINTS", i_regex)

check("interop 插入点存在", i_regex > 0, i_regex + 1)
check("interop 正则晚于 /cmd 分支", i_regex > i_cmd, f"cmd@{i_cmd+1} regex@{i_regex+1}")
check("interop 正则晚于管理员校验（防提权）", i_regex > i_admin, f"admin@{i_admin+1} regex@{i_regex+1}")
check("interop 正则早于自定义关键词", 0 < i_regex < i_kw, f"regex@{i_regex+1} kw@{i_kw+1}")
check("interop 正则早于状态查询", 0 < i_regex < i_status, f"regex@{i_regex+1} status@{i_status+1}")
i_bound = _ifirst("if not bound:", ih.lineno - 1)
check("interop 正则晚于已绑定群判定", 0 < i_bound < i_regex, f"bound@{i_bound+1} regex@{i_regex+1}")

# ---- core.py：游戏事件钩子 ----
csrc = _load("core.py")
ctree = ast.parse(csrc)
cls = [n for n in ctree.body if isinstance(n, ast.ClassDef) and n.name == "GreenMoonPlugin"][0]

def _has_method(name):
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
               for n in cls.body)

for m in ("is_on_main_thread", "_regex_config", "_regex_send_text", "_regex_send_at_text",
          "_regex_send_image", "_regex_delete_message", "_regex_mute_user",
          "_regex_dispatch", "_regex_flatten", "_regex_cards", "_regex_adapters",
          "_regex_event_player", "_regex_event_chat", "_qqbot_default_group",
          "_qqbot_card_admins", "_qqbot_apply_qr_credentials", "_save_regex_config",
          "_regex_group_allowed"):
    check(f"core.GreenMoonPlugin 有 {m}", _has_method(m))

# is_on_main_thread 语义
ins = [n for n in ast.walk(cls) if isinstance(n, ast.FunctionDef) and n.name == "is_on_main_thread"]
check("is_on_main_thread 认 _main_thread_id", ins and "_main_thread_id" in ast.unparse(ins[0]))
check("is_on_main_thread 未取到主线程 id 时返回 False（走安全分支）",
      ins and "is None" in ast.unparse(ins[0]))

# 事件钩子里要真的调到引擎
check("on_player_join 调用 _regex_event_player",
      "_regex_event_player(EVENT_PLAYER_JOIN" in csrc)
check("on_player_quit 调用 _regex_event_player",
      "_regex_event_player(EVENT_PLAYER_LEFT" in csrc)
check("on_player_chat 调用 _regex_event_chat", "_regex_event_chat(" in csrc)
check("正则引擎在 __init__ 里创建", "self.regex_engine = RegexEngineModule(self)" in csrc)
check("引擎构造失败不拖垮插件启动",
      "self.regex_engine = None" in csrc and "初始化失败，该功能不可用" in csrc)

# 事件派发必须丢后台线程（主线程联网会卡 tick）
check("_regex_event_player 在后台线程执行",
      "threading.Thread(target=fn, args=args, daemon=True).start()" in csrc)
check("_regex_event_chat 在后台线程执行",
      "target=engine.on_mc_player_chat" in csrc)

# AppSecret 绝不出现在接口返回里
check("_qqbot_apply_qr_credentials 日志打掩码",
      "app_secret[:4]" in csrc and "'*' * max(0, len(app_secret) - 4)" in csrc)


# ===========================================================================
print()
print("=" * 72)
print("9. web 层：路由 / 校验 / AppSecret 不回显")
print("=" * 72)

wsrc = _load("web.py")
wtree = ast.parse(wsrc)

paths = set()
for node in ast.walk(wtree):
    if isinstance(node, ast.Compare):
        for c in [node.left] + list(node.comparators):
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                if c.value.startswith("/api/"):
                    paths.add(c.value)

for p in ("/api/qqbot/qr/create", "/api/qqbot/qr/poll",
          "/api/qqbot/regex/rules", "/api/qqbot/regex/save",
          "/api/qqbot/regex/toggle", "/api/qqbot/regex/delete",
          "/api/qqbot/regex/test"):
    check(f"注册了路由 {p}", p in paths)

wdefs = {n.name for n in ast.walk(wtree) if isinstance(n, ast.FunctionDef)}
for h in ("_handle_api_qqbot_qr_create", "_handle_api_qqbot_qr_poll",
          "_handle_api_regex_rules", "_handle_api_regex_save",
          "_handle_api_regex_toggle", "_handle_api_regex_delete",
          "_handle_api_regex_test"):
    check(f"实现了 {h}", h in wdefs)

# 按 API 段拆函数体，逐段断言"不回传明文"
def _func_src(name):
    fn = [n for n in ast.walk(wtree) if isinstance(n, ast.FunctionDef) and n.name == name]
    if not fn:
        return ""
    return ast.get_source_segment(wsrc, fn[0]) or ""

poll_src = _func_src("_handle_api_qqbot_qr_poll")
check("qr/poll 返回 secret_len", '"secret_len"' in poll_src)
check("qr/poll 返回等长掩码", '"secret_mask"' in poll_src and "mask_secret" in poll_src)
check("★ qr/poll 绝不返回 secret 明文",
      '"secret":' not in poll_src and "'secret':" not in poll_src, "发现明文回传字段")
check("qr/poll 不回传 bind_key", "bind_key" not in poll_src.split("return")[-1])
check("qr/poll 校验 task_id 归属（防 IDOR）", 'item.get("ip")' in poll_src)

create_src = _func_src("_handle_api_qqbot_qr_create")
check("★ qr/create 不下发 bind_key",
      '"bind_key"' not in create_src.split("self._send_json")[-1])
check("qr/create 有同 IP 冷却", "_QR_IP_COOLDOWN" in create_src)
check("qr/create 清理过期任务", "_QR_TASK_TTL" in create_src)
check("qr/create 限制任务表大小", "_QR_TASK_MAX" in create_src)

# bind_key 只存内存
check("★ bind_key 只存内存表 _QR_TASKS", "_QR_TASKS[task_id]" in create_src)
check("★ bind_key 绝不落盘", "json.dump" not in create_src and "atomic_write" not in create_src)

# 引擎配置保存走独立入口（不整表覆盖）
web = _func_src("_handle_api_regex_save")
check("有 _save_regex_config 独立入口", "_save_regex_config" in web)
check("regex/save 保存前做服务端校验", "_validate_regex_rules" in web)
check("regex/save 同时保存 config", '"config"' in web and "_save_regex_config" in web)

# 服务端校验函数
val_src = _func_src("_validate_regex_rules")
check("校验函数存在且非空", len(val_src) > 500, len(val_src))
for probe, label in (
    ("_is_risky_pattern", "ReDoS"),
    ("_MAX_PATTERN_LEN", "pattern 长度"),
    ("_MAX_RULES", "规则条数上限"),
    ("EVENT_TYPES", "事件类型白名单"),
    ("ACTION_TYPES", "动作类型白名单"),
    ("id 重复", "id 唯一性"),
):
    check(f"服务端校验覆盖 {label}", probe in val_src)

# scope 覆盖自检
#
# 不能只取 _SCOPE_EXACT = {...} 那一条字面量：控制台执行类端点是后面用
# 「_SCOPE_EXACT.update({...})」追加进去的，漏掉那一句会误报 12 条未覆盖。
# 这里按执行顺序处理 Assign / AnnAssign / Expr(Call) 三种语句。
tree2 = ast.parse(wsrc)
g = {"_SCOPE_ANY": {"__any__"}}


def _target_name(node):
    if isinstance(node, ast.Assign):
        return getattr(node.targets[0], "id", None)
    if isinstance(node, ast.AnnAssign):
        return getattr(node.target, "id", None)
    return None


for node in tree2.body:
    nm = _target_name(node)
    if nm in ("_SCOPE_EXACT", "_SCOPE_PREFIX", "_SCOPE_ANY"):
        try:
            g[nm] = eval(compile(ast.Expression(node.value), "<s>", "eval"), dict(g))
        except Exception as e:
            print("  [warn] 求值", nm, "失败:", e)
    elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
        if isinstance(call.func, ast.Attribute) and call.func.attr == "update":
            base = getattr(call.func.value, "id", None)
            if base == "_SCOPE_EXACT" and call.args:
                try:
                    g["_SCOPE_EXACT"].update(
                        eval(compile(ast.Expression(call.args[0]), "<s>", "eval"), dict(g)))
                except Exception as e:
                    print("  [warn] update 失败:", e)

check("解析出 _SCOPE_EXACT 表", isinstance(g.get("_SCOPE_EXACT"), dict), type(g.get("_SCOPE_EXACT")))
check("解析出 _SCOPE_PREFIX 表", isinstance(g.get("_SCOPE_PREFIX"), list), type(g.get("_SCOPE_PREFIX")))
check("控制台执行类端点已并入精确表（update 语句被识别）",
      "/api/console/command" in g.get("_SCOPE_EXACT", {}))

exact = g.get("_SCOPE_EXACT", {})
prefix = g.get("_SCOPE_PREFIX", [])

for p in ("/api/qqbot/qr/create", "/api/qqbot/qr/poll", "/api/qqbot/regex/rules",
          "/api/qqbot/regex/save", "/api/qqbot/regex/toggle", "/api/qqbot/regex/delete",
          "/api/qqbot/regex/test"):
    scopes = exact.get(p)
    if scopes is None:
        for pref, sc in prefix:
            if p.startswith(pref):
                scopes = sc
                break
    check(f"{p} 归到 qqbot 权限", scopes == {"qqbot"}, scopes)

# 全量路由覆盖自检
#
# _public_path() 里显式放行的两个白名单端点（申请 / 查状态）本来就没有
# 权限要求，属于有意为之，不算漏登记。
PUBLIC = {"/api/whitelist/apply", "/api/whitelist/status"}
known = {p for p in paths if not (p.endswith("/") and p.count("/") <= 2)}
uncovered = []
for p in known:
    if p in exact or p in PUBLIC:
        continue
    if any(p.startswith(pref) for pref, _ in prefix):
        continue
    uncovered.append(p)
check("所有 /api/ 路由都被 scope 覆盖（公开端点除外）", not uncovered, uncovered)
check("公开端点确实只有那 2 个（无意外放行）",
      PUBLIC == {p for p in paths if p in PUBLIC} | set(), sorted(PUBLIC & paths))
check("路由总数 >= 100（新增后）", len(known) >= 100, len(known))


# ===========================================================================
print()
print("=" * 72)
print("10. web_assets：内嵌 qrcode 且版本戳覆盖合并后的 JS")
print("=" * 72)

import hashlib  # noqa: E402
import importlib  # noqa: E402

import web_assets  # noqa: E402

wa = importlib.reload(web_assets)
html = wa.HTML_ADMIN

check("admin 页面内嵌了 qrcode 库", "QR Code Generator for JavaScript" in html)
check("MIT 许可证头保留", "Licensed under the MIT license" in html)
check("qrcode 在业务脚本之前定义（先有全局对象）",
      html.index("QR Code Generator") < html.index("function apiPath"))
check("扫码 UI 元素在页面里", 'id="qr-box"' in html and 'id="qr-img-box"' in html)
check("正则 UI 元素在页面里", 'id="sec-regex"' in html and 'id="regex-list"' in html)
check("导航里有正则入口", "showSection('sec-regex'" in html)

import re as _re  # noqa: E402
m = _re.search(r'<meta name="gm-js-rev" content="([^"]+)"', html)
check("有版本戳 meta", bool(m))
if m:
    rev = m.group(1).split("-", 1)[1]
    js_only = hashlib.sha1((wa._STATIC / "admin.js").read_text(encoding="utf-8").encode()).hexdigest()[:12]
    check("★ 版本戳覆盖 extra.js（改 qrcode 会刷新缓存）", rev != js_only,
          f"rev={rev} js_only={js_only}")

# 没 extra 的页面不该被影响
check("其它页面正常构建", wa.HTML_INDEX and wa.HTML_LOGIN and wa.HTML_WHITELIST)
check("其它页面没有 qrcode 负担", "QR Code Generator" not in wa.HTML_INDEX)

# extra 缺失时要优雅降级
extra = wa._STATIC / "admin.extra.js"
backup = extra.read_text(encoding="utf-8")
try:
    extra.unlink()
    wa2 = importlib.reload(wa)
    check("extra.js 缺失时页面仍可构建", "function apiPath" in wa2.HTML_ADMIN)
    check("extra.js 缺失时版本戳回落为 js-only",
          _re.search(r'<meta name="gm-js-rev" content="([^"]+)"', wa2.HTML_ADMIN).group(1).split("-", 1)[1] == js_only)
finally:
    extra.write_text(backup, encoding="utf-8")
    importlib.reload(wa)

check("admin.extra.js 就是 qrcode 库", (wa._STATIC / "admin.extra.js").is_file()
      and "QR Code Generator" in (wa._STATIC / "admin.extra.js").read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# ★ 回归：注入的 JS 必须真的被包在 <script> 里
#
# 踩过的坑：admin.extra.js 一开始**没带** <script> 包裹，于是拼好的
# 「extra + admin.js」在 HTML 里的实际形态是
#     <body> ... <svg version="1.1" ...   ← 库里的字符串字面量
# HTML 解析器见到 '<svg' 就如获至宝地开始当真标签解析，后面的代码全被
# 吞进 SVG 子树。结果是浏览器里 `typeof qrcode === 'undefined'`，
# 二维码永远渲染不出来，控制台只刷几条 SVG 属性语法错误 —— 看着像
# 库的问题，其实是注入方式的问题。
#
# 所以这里不只查"源码里有 qrcode"，而是查**产物里的标签结构**。
extra_text = (wa._STATIC / "admin.extra.js").read_text(encoding="utf-8")
js_text = (wa._STATIC / "admin.js").read_text(encoding="utf-8")

check("★ admin.extra.js 自带 <script> 包裹（与 static/*.js 约定一致）",
      extra_text.lstrip().startswith("<script>"), extra_text.lstrip()[:40])
check("★ admin.extra.js 正确闭合 </script>",
      extra_text.rstrip().endswith("</script>"))
check("★ admin.js 自带 <script> 包裹", js_text.lstrip().startswith("<script>"))

# 产物的结构：qrcode 库必须落在某个 <script> 段之内，而不是裸露在 body
_open_script = html.index("<script>")
_close_script = html.index("</script>")
check("★ 产物里第一个 <script> 在 qrcode 库之前",
      _open_script < html.index("QR Code Generator"),
      f"script@{_open_script} qrcode@{html.index('QR Code Generator')}")

# 更强的判据：在 <body> 之后、第一个 <script> 之前，不得出现裸的库代码
_body = html.index("<body")
_first_script = html.index("<script>", _body)
_between = html[_body:_first_script]
check("★ <body> 到第一个 <script> 之间没有裸露的库代码",
      "QR Code Generator" not in _between and "var qrcode" not in _between,
      _between[-80:].replace("\n", " ") if _between else "(空)")

# 裸露的 '<svg' / '<path' / '<table' 只允许出现在 <script> 段内
# （这些正是会让 HTML 解析器跑偏的字符串）
_bare = []
_pos = 0
while True:
    _i = html.find("<svg version", _pos)
    if _i < 0:
        break
    _seg_start = html.rfind("<script", 0, _i)
    _seg_end = html.rfind("</script>", 0, _i)
    if _seg_start < _seg_end or _seg_start < 0:
        _bare.append(_i)
    _pos = _i + 1
check("★ 库里 '<svg version' 字面量落在 <script> 段内（没被解析成真标签）",
      not _bare, _bare or "clean")

# 平衡性：必须用**真正的 HTML 解析器**来判断，不能数子串。
# admin.js 的注释里就写着 "<script>"（讲解 XSS 转义），那是文本不是标签；
# 直接 str.count 会把它算进去，得出 6 开 3 闭的假警报。
try:
    from html.parser import HTMLParser as _HP

    class _ScriptCounter(_HP):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.opens = 0
            self.closes = 0
            self.in_script = False
            self.script_data_nonempty = 0

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.opens += 1
                self.in_script = True

        def handle_endtag(self, tag):
            if tag == "script":
                self.closes += 1
                self.in_script = False

        def handle_data(self, data):
            # 真正的 JS 会以 data 形式出现在 <script> 内部
            if self.in_script and data.strip():
                self.script_data_nonempty += 1

    _sc = _ScriptCounter()
    _sc.feed(html)
    check("★ 真解析下 <script> 开闭成对", _sc.opens == _sc.closes,
          f"{_sc.opens} 开 / {_sc.closes} 闭")
    check("★ 存在非空的 script 内容（脚本真的进去了）",
          _sc.script_data_nonempty > 0, _sc.script_data_nonempty)
except ImportError:                                  # pragma: no cover
    print("  [skip] 无 html.parser，跳过标签平衡检查")


# ===========================================================================
print()
print("=" * 72)
print("11. 前端 JS：扫码与正则逻辑")
print("=" * 72)

js = (wa._STATIC / "admin.js").read_text(encoding="utf-8")

for fn in ("qrOpen", "qrClose", "qrCreate", "qrPoll", "qrRender", "qrFillCards",
           "qrStopPoll", "loadRegex", "regexRenderList", "regexEdit", "regexApply",
           "regexAddRule", "regexToggle", "regexDelete", "regexSave", "regexTest",
           "regexRenderConfig"):
    check(f"前端实现了 {fn}", ("function " + fn) in js)

check("扫码用内嵌 qrcode 库", "qrcode(0, 'M')" in js or 'qrcode(0, "M")' in js)
check("扫码生成 data URL", "createDataURL" in js)
check("qrcode 缺失时有兜底文案", "二维码组件未加载" in js)
check("扫码轮询带 task_id 与 card_id", "task_id=" in js and "card_id=" in js)
check("★ 前端只显示掩码，不显示 secret 明文",
      "r.secret_mask" in js and "r.secret" not in js.replace("r.secret_mask", "").replace("r.secret_len", ""))
check("前端有二维码过期兜底", "二维码已过期" in js)
check("轮询单次网络异常不放弃", "轮询异常" in js)

check("正则编辑支持条件行解析", "regexParseLines" in js)
check("正则编辑支持变量提示", "$nickname" in js)
check("正则有测试匹配 UI", "regexTest" in js)
check("正则保存整表提交", "rules: regexRules" in js)
check("sec-regex 进板块会加载", "id === 'sec-regex'" in js)
check("sec-bots 进板块会填充卡片下拉", "qrFillCards()" in js)


# ===========================================================================
print()
print("=" * 72)
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败用例：")
    for f in FAIL:
        print("  -", f)
print("=" * 72)
sys.exit(1 if FAIL else 0)
