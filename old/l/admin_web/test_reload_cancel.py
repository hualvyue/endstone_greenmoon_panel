"""回归测试：/reload 崩溃（SIGSEGV）修复。

根因：旧 on_disable 只置 ``_periodic_stopped=True`` 让周期回调早返回，
但任务句柄被丢弃、从未真正从 Endstone 调度器摘除。/reload 后旧 plugin
实例的周期回调仍被 C++ 调度器按 tick 反复调用，捕获的对象已失效 →
``EndstoneScheduler::mainThreadHeartbeat`` 段错误。

修复：on_disable 改用 Endstone 公开的 ``scheduler.cancel_tasks(plugin)``
把本插件名下全部任务整体取消（含子模块经 self.plugin 注册的）。

本测试不依赖真实 Endstone 运行时（用 stub 即可），只验证 on_disable
确实调用了 cancel_tasks(self)。
"""

import os
import sys
import unittest
from unittest import mock

# 让测试在“无真实 Endstone”的环境里也能跑：优先用仓库里的 stub。
for _cand in ("/tmp/es/whl011", "/tmp/es/v14", "/tmp/es/v0"):
    if _cand not in sys.path and os.path.isdir(_cand):
        sys.path.insert(0, _cand)

_IMPORT_ERR = None
try:
    import admin_web.core as core  # noqa: E402
    _CAN_IMPORT = True
except Exception as _e:  # pragma: no cover - 仅在缺少依赖时触发
    _CAN_IMPORT = False
    _IMPORT_ERR = _e


@unittest.skipUnless(_CAN_IMPORT, f"无法导入 admin_web.core: {_IMPORT_ERR}")
class _TestPlugin(core.GreenMoonPlugin):
    """把只读 property 覆盖成普通类属性，便于桩测试。"""

    server = None
    logger = None


@unittest.skipUnless(_CAN_IMPORT, f"无法导入 admin_web.core: {_IMPORT_ERR}")
class TestReloadCancelTasks(unittest.TestCase):
    def _make_plugin(self):
        """用 __new__ 绕过重型 __init__，桩好 on_disable 读取的全部属性。"""
        inst = _TestPlugin.__new__(_TestPlugin)

        # 调度器：记录 cancel_tasks 的调用
        scheduler = mock.MagicMock()
        server = mock.MagicMock()
        server.scheduler = scheduler
        inst.server = server
        inst._scheduler_mock = scheduler

        # 日志
        inst.logger = mock.MagicMock()

        # on_disable 通过 getattr 安全读取的子系统（全部置 None → 走跳过分支）
        inst._behavior = None
        inst.region_api = None
        inst.trust = None
        inst._executor = None
        inst.cross = None
        inst.mod_loader = None
        inst._panel_log_handler = None
        inst.httpd = None
        inst.qq_hub = None
        inst.qq_bot = None
        inst._tps_task = None

        # 内部调用的方法直接桩成空操作，避免触碰更多状态
        inst._stop_log_tailer = lambda: None
        inst._notify_server_stop = lambda: None
        inst._close_plugin_logging = lambda: None
        return inst

    def test_on_disable_calls_cancel_tasks_with_self(self):
        inst = self._make_plugin()
        inst.on_disable()

        # 关键断言：本插件名下任务被整体取消，且参数是插件自身
        self.assertTrue(
            inst._scheduler_mock.cancel_tasks.called,
            "on_disable 必须调用 server.scheduler.cancel_tasks(self)",
        )
        call_args = [
            c.args for c in inst._scheduler_mock.cancel_tasks.call_args_list
        ]
        self.assertTrue(
            any(cargs and cargs[0] is inst for cargs in call_args),
            "cancel_tasks 必须以插件自身(self)为参数被调用",
        )

    def test_on_disable_does_not_raise_without_scheduler(self):
        """即便 scheduler.cancel_tasks 抛异常（极旧版本缺失），
        on_disable 也应吞掉异常、不崩溃。"""
        inst = self._make_plugin()
        inst._scheduler_mock.cancel_tasks.side_effect = AttributeError("nope")
        try:
            inst.on_disable()
        except Exception as e:  # pragma: no cover
            self.fail(f"on_disable 不应因 cancel_tasks 异常而崩溃: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
