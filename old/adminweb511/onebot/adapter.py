"""OneBot v11 WebSocket 适配器（正向 / 反向双模式）。

移植自 LumenBridge，仅移除 i18n / vendor 依赖：
  - 第三方 ``websockets`` 模块由构造参数注入（沿用 GreenMoon 离线库加载机制）；
  - 日志直接输出中文；
  - 断线 / 上线时除经 event_bus 广播 ``bot.online`` / ``bot.offline`` 外，
    还会通过 ``on_state_change`` 回调通知插件刷新连接状态。

网络 IO 运行在独立 asyncio 线程，回调游戏 API 必须经由 plugin.run_on_main。
"""

from __future__ import annotations

import asyncio
import json
import queue
import random
import threading
import time
import uuid
from typing import Any, Callable

from . import packets
from .message import format_message

# WebSocket 握手身份标识：QQ 开放平台等网关据此显示客户端名称
USER_AGENT = "GreenMoonPanel (Endstone)"

API_TIMEOUT = 10.0
SEND_QUEUE_SIZE = 100
# 入站事件派发队列容量：超出时丢最旧保内存（与发送队列同策略）
DISPATCH_QUEUE_SIZE = 2000
# websockets 连接状态枚举值 OPEN（IntEnum，值为 1）
_WS_STATE_OPEN = None
_WS_STATE_OPEN_SENTINEL = object()


def _ws_open_constant(_websockets) -> int:
    """解析 websockets 库的 State.OPEN 枚举值；解析失败回退 1。"""
    try:
        state = getattr(_websockets, "State", None)
        if state is not None:
            return int(state.OPEN)
    except Exception:
        pass
    return 1


class OneBotAdapter:
    """OneBot v11 WebSocket 适配器（正向 / 反向双模式）"""

    def __init__(
        self,
        websockets_module,
        logger: Any,
        event_bus: Any,
        *,
        ws_type: int = 0,
        target: str = "ws://127.0.0.1:3001",
        listen_host: str = "0.0.0.0",
        listen_port: int = 3002,
        access_token: str = "",
        bot_qq: int = 0,
        adapter_id: str = "",
        adapter_name: str = "",
        adapter_type: str = "websocket",
        groups: list[int] | None = None,
        on_state_change: Callable[..., Any] | None = None,
    ) -> None:
        self.websockets = websockets_module
        self.logger = logger
        self.bus = event_bus
        self.on_state_change = on_state_change
        self.ws_type = ws_type
        self.target = target
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.access_token = access_token
        self.bot_qq = bot_qq
        # 多适配器元数据：id 对应连接卡片
        self.adapter_id = adapter_id
        self.adapter_name = adapter_name
        self.adapter_type = adapter_type
        self.groups: list[int] = list(groups or [])
        # 由 AdapterHub 维护的配置快照，用于热重载 diff 判断是否需要重建连接
        self.config_snapshot: dict[str, Any] | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        self._ws: Any = None
        self._server: Any = None
        self._clients: set[Any] = set()
        self._announced = False
        self._connected_event: asyncio.Event | None = None
        self._send_queue: asyncio.Queue | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._sender_task: asyncio.Task | None = None
        self._main_future: Any = None
        self._dispatch_queue: "queue.Queue[dict[str, Any] | None] | None" = None
        self._dispatch_thread: threading.Thread | None = None
        # 实时解析 websockets 的 State.OPEN 枚举值
        self._open_state = _ws_open_constant(self.websockets)
        # 与 __getattr__ 配合，避免属性名冲突
        self.__is_connected = False

    @property
    def is_connected(self) -> bool:
        if self.__is_connected:
            return True
        return self._poll_connected()

    def _poll_connected(self) -> bool:
        """实时读取底层连接状态。"""
        ws = getattr(self, "_ws", None)
        if self.ws_type == 0:
            if ws is None:
                return False
            state = getattr(ws, "state", None)
            return state == self._open_state if state is not None else not getattr(ws, "closed", True)
        return len(self._clients) > 0

    @property
    def mode_name(self) -> str:
        return "正向连接" if self.ws_type == 0 else "反向监听"

    @property
    def display_name(self) -> str:
        name = self.adapter_name or "WebSocket"
        return f"{name} ({self.mode_name})"

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.__is_connected = False
        self._dispatch_queue = queue.Queue(maxsize=DISPATCH_QUEUE_SIZE)
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_worker,
            name=f"GreenMoon-Dispatch-{self.adapter_id or 'default'}",
            daemon=True,
        )
        self._dispatch_thread.start()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"GreenMoon-WS-{self.adapter_id or 'default'}",
            daemon=True,
        )
        self._thread.start()
        self._main_future = asyncio.run_coroutine_threadsafe(self._main(), self._loop)

    def _dispatch_worker(self) -> None:
        """串行消费入站事件派发队列（FIFO 保序）"""
        q = self._dispatch_queue
        if q is None:
            return
        while True:
            try:
                data = q.get(timeout=1.0)
            except queue.Empty:
                if not self._running:
                    return
                continue
            if data is None:
                return
            if not self._running:
                return
            try:
                self.bus.emit("onebot.pack", data)
            except Exception:
                self.logger.exception("[OneBot] 转发入站事件出错")

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        if loop is None or loop.is_closed():
            self._cleanup_after_stop()
            return

        async def _shutdown() -> None:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.cancel()
            self._pending.clear()
            if self._connected_event is not None:
                self._connected_event.set()
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            for client in list(self._clients):
                try:
                    await client.close()
                except Exception:
                    pass
            self._clients.clear()
            if self._server is not None:
                try:
                    self._server.close()
                    await self._server.wait_closed()
                except Exception:
                    pass

            current = asyncio.current_task()
            tasks = [task for task in asyncio.all_tasks() if task is not current]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=5)
        except Exception:
            pass
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop())
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        if self._dispatch_queue is not None:
            try:
                self._dispatch_queue.put_nowait(None)
            except queue.Full:
                pass
        if self._dispatch_thread and self._dispatch_thread is not threading.current_thread():
            self._dispatch_thread.join(timeout=2)
        self._cleanup_after_stop()
        self.logger.info("[OneBot] 适配器已停止")

    def _cleanup_after_stop(self) -> None:
        self._dispatch_queue = None
        self._dispatch_thread = None
        if self._main_future is not None:
            try:
                self._main_future.cancel()
            except Exception:
                pass
        self._loop = None
        self._thread = None
        self._main_future = None
        self._sender_task = None
        self._ws = None
        self._server = None
        self._pending.clear()
        self._clients.clear()
        self.__is_connected = False
        self._announced = False
        self._connected_event = None
        self._send_queue = None

    def _run_loop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                pending = list(asyncio.all_tasks(loop))
            except RuntimeError:
                pending = []
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def _notify_state(self, connected: bool) -> None:
        """同步连接状态到实例标志，并通过回调通知插件。"""
        self.__is_connected = bool(connected)
        if self.on_state_change is not None:
            try:
                self.on_state_change(self, connected)
            except Exception:
                self.logger.exception("[OneBot] 状态回调异常")

    async def _main(self) -> None:
        self._connected_event = asyncio.Event()
        self._send_queue = asyncio.Queue(maxsize=SEND_QUEUE_SIZE)
        self._sender_task = asyncio.create_task(
            self._sender_loop(),
            name=f"GreenMoon-OneBot-Sender-{self.adapter_id or 'default'}",
        )
        try:
            if self.ws_type == 0:
                await self._forward_loop()
            else:
                await self._reverse_serve()
        finally:
            if self._sender_task is not None and not self._sender_task.done():
                self._sender_task.cancel()
            if self._sender_task is not None:
                await asyncio.gather(self._sender_task, return_exceptions=True)
            self._sender_task = None

    async def _forward_loop(self) -> None:
        headers = {"User-Agent": USER_AGENT}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        attempt = 0
        while self._running:
            try:
                self.logger.info(f"[OneBot] 正在连接 {self.target}")
                async with self.websockets.connect(
                    self.target,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=10,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._connected_event.set()
                    attempt = 0
                    self.logger.info(f"[OneBot] 已连接：{self.display_name}")
                    self._notify_state(True)
                    self.bus.emit("bot.online", self)
                    async for raw in ws:
                        try:
                            self._dispatch_raw(raw)
                        except Exception:
                            self.logger.exception("[OneBot] 处理入站包出错")
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    self.logger.warning(f"[OneBot] 连接错误: {e}")
            finally:
                was_online = self._ws is not None
                self._ws = None
                self._connected_event.clear()
                if was_online:
                    self._notify_state(False)
                    try:
                        self.bus.emit("bot.offline", self)
                    except Exception:
                        pass

            if not self._running:
                break
            attempt += 1
            delay = min(60.0, 2.0 ** min(attempt, 6) + random.uniform(0, 2))
            self.logger.warning(f"[OneBot] 已断开，{delay:.1f}s 后重连（第 {attempt} 次）")
            await asyncio.sleep(delay)

    async def _reverse_serve(self) -> None:
        async def handler(ws: Any) -> None:
            headers = getattr(ws, "request", None)
            headers = getattr(headers, "headers", {}) or {}
            auth = headers.get("Authorization", "")
            self_id = headers.get("X-Self-ID", "")

            if self.access_token and auth != f"Bearer {self.access_token}":
                self.logger.warning("[OneBot] 反向连接鉴权失败")
                await ws.close(code=4001, reason="unauthorized")
                return

            self._clients.add(ws)
            self._connected_event.set()
            self.logger.info(f"[OneBot] 反向客户端已连接 (self_id={self_id})")
            announced = not self.bot_qq or str(self_id) == str(self.bot_qq)
            if announced and not self._announced:
                self._announced = True
                self._notify_state(True)
                self.bus.emit("bot.online", self)
            try:
                async for raw in ws:
                    try:
                        self._dispatch_raw(raw)
                    except Exception:
                        self.logger.exception("[OneBot] 反向包处理出错")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("[OneBot] 反向接收出错")
            finally:
                self._clients.discard(ws)
                if not self._clients:
                    self._connected_event.clear()
                    if self._announced:
                        self._announced = False
                        self._notify_state(False)
                        try:
                            self.bus.emit("bot.offline", self)
                        except Exception:
                            pass
                self.logger.info("[OneBot] 反向客户端已断开")

        while self._running:
            try:
                self._server = await self.websockets.serve(
                    handler, self.listen_host, self.listen_port,
                    max_size=16 * 1024 * 1024,
                )
                self.logger.info(
                    f"[OneBot] 反向监听已启动 {self.listen_host}:{self.listen_port}"
                )
                await self._server.wait_closed()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"[OneBot] 反向监听出错: {e}")
                await asyncio.sleep(10)
            if not self._running:
                break

    def _dispatch_raw(self, raw: Any) -> None:
        try:
            data = json.loads(raw if isinstance(raw, str) else raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.logger.error("[OneBot] JSON 解析失败")
            return
        if not isinstance(data, dict):
            self.logger.warning(f"[OneBot] 忽略非对象数据包: {data}")
            return

        echo = data.get("echo")
        if echo is not None:
            echo_key = str(echo)
            fut = self._pending.pop(echo_key, None)
            if fut is not None:
                retcode = data.get("retcode")
                if data.get("status") == "failed" or (retcode is not None and retcode != 0):
                    self.logger.warning(
                        f"[OneBot] API 失败 retcode={retcode} status={data.get('status')} "
                        f"wording={data.get('wording') or data.get('message') or ''}"
                    )
                if not fut.done():
                    fut.set_result(data.get("data"))
                self.bus.emit(f"packid_{echo}", data.get("data"))

        if self.adapter_id and "_lumen_adapter_id" not in data:
            data["_lumen_adapter_id"] = self.adapter_id
        self._dispatch_pack(data)

    def _dispatch_pack(self, data: dict[str, Any]) -> None:
        q = self._dispatch_queue
        if q is None:
            self.bus.emit("onebot.pack", data)
            return
        try:
            q.put_nowait(data)
        except queue.Full:
            try:
                q.get_nowait()
                q.put_nowait(data)
            except (queue.Empty, queue.Full):
                pass
            self.logger.warning("[OneBot] 入站派发队列积压，已丢弃最旧事件")

    def _drop_pack(self, dropped: Any) -> None:
        echo = dropped.get("echo") if isinstance(dropped, dict) else None
        if echo:
            fut = self._pending.pop(str(echo), None)
            if fut is not None and not fut.done():
                fut.set_result(None)
        params = dropped.get("params") if isinstance(dropped, dict) else None
        action = str(dropped.get("action") or "?") if isinstance(dropped, dict) else "?"
        target = "-"
        if isinstance(params, dict):
            if params.get("group_id") is not None:
                target = f"group_id={params.get('group_id')}"
            elif params.get("user_id") is not None:
                target = f"user_id={params.get('user_id')}"
        self.logger.warning(f"[OneBot] 发送队列已丢弃最旧包 action={action} target={target}")

    def _evict_oldest(self) -> None:
        q = self._send_queue
        if q is None or q.empty():
            return
        try:
            dropped = q.get_nowait()
        except asyncio.QueueEmpty:
            return
        self._drop_pack(dropped)

    def _requeue_head(self, pack: dict[str, Any]) -> None:
        q = self._send_queue
        if q is None:
            return
        try:
            items: list[Any] = []
            while True:
                try:
                    items.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            maxsize = q.maxsize or 0
            if maxsize and items and len(items) + 1 > maxsize:
                self._drop_pack(items.pop(0))
            q.put_nowait(pack)
            for it in items:
                q.put_nowait(it)
        except Exception:
            self.logger.exception("[OneBot] 发送队列重排失败")

    async def _sender_loop(self) -> None:
        while self._running:
            try:
                await self._connected_event.wait()
                pack = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except (asyncio.CancelledError, RuntimeError, GeneratorExit):
                return
            try:
                text_pack = json.dumps(pack, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                self.logger.error(f"[OneBot] 发送序列化失败: {exc}")
                continue
            try:
                if self.ws_type == 0 and self._ws is not None:
                    await self._ws.send(text_pack)
                elif self.ws_type == 1 and self._clients:
                    sent_any = False
                    for client in list(self._clients):
                        try:
                            await client.send(text_pack)
                            sent_any = True
                        except Exception:
                            self.logger.warning("[OneBot] 反向客户端发送失败")
                    if not sent_any:
                        raise ConnectionError("[OneBot] 连接不可用")
                else:
                    raise ConnectionError("[OneBot] 连接不可用")
            except asyncio.CancelledError:
                self._requeue_head(pack)
                raise
            except Exception:
                self._requeue_head(pack)
                await asyncio.sleep(2.0)

    def send_pack(self, pack: dict[str, Any]) -> None:
        loop = self._loop
        if not self._running or loop is None or loop.is_closed():
            return

        async def _enqueue() -> None:
            q = self._send_queue
            if q is None:
                return
            if q.full():
                self._evict_oldest()
            await q.put(pack)

        try:
            asyncio.run_coroutine_threadsafe(_enqueue(), loop)
        except RuntimeError:
            pass

    def call_api(
        self,
        pack: dict[str, Any],
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        loop = self._loop
        if not self._running or loop is None or loop.is_closed():
            if callback:
                callback(None)
            return
        if callback is None:
            self.send_pack(pack)
            return
        echo = uuid.uuid4().hex
        pack = {**pack, "echo": echo}

        async def _request() -> None:
            q = self._send_queue
            if q is None:
                callback(None)
                return
            fut: asyncio.Future = loop.create_future()
            self._pending[echo] = fut
            if q.full():
                self._evict_oldest()
            await q.put(pack)
            try:
                data = await asyncio.wait_for(fut, timeout=timeout)
                try:
                    callback(data)
                except Exception:
                    self.logger.exception("[OneBot] API 回调异常")
            except asyncio.TimeoutError:
                self._pending.pop(echo, None)
                self.logger.warning(f"[OneBot] API 超时 action={pack.get('action')}")
                try:
                    callback(None)
                except Exception:
                    self.logger.exception("[OneBot] API 回调异常")
            except asyncio.CancelledError:
                self._pending.pop(echo, None)
                try:
                    callback(None)
                except Exception:
                    pass
                raise

        try:
            asyncio.run_coroutine_threadsafe(_request(), loop)
        except RuntimeError:
            if callback:
                callback(None)

    # ------------------------------------------------------------ OneBot 动作
    def send_group_msg(self, group_id: int, message: Any) -> None:
        self.send_pack(packets.group_message(group_id, format_message(message)))

    def send_private_msg(self, user_id: int, message: Any) -> None:
        self.send_pack(packets.private_message(user_id, format_message(message)))

    def send_group_forward_msg(self, group_id: int, messages: Any) -> None:
        self.send_pack(packets.group_forward_message(group_id, messages))

    def delete_msg(self, message_id: int) -> None:
        self.send_pack(packets.delete_message(message_id))

    def set_group_ban(self, group_id: int, user_id: int, duration: int) -> None:
        self.send_pack(packets.group_ban(group_id, user_id, duration))

    def set_group_whole_ban(self, group_id: int, enable: bool) -> None:
        self.send_pack(packets.group_whole_ban(group_id, enable))

    def set_group_kick(self, group_id: int, user_id: int, reject: bool = False) -> None:
        self.send_pack(packets.group_kick(group_id, user_id, reject))

    def set_group_name(self, group_id: int, name: str) -> None:
        self.send_pack(packets.group_name(group_id, name))

    def set_group_card(self, group_id: int, user_id: int, card: str) -> None:
        self.send_pack(packets.group_card(group_id, user_id, card))

    def get_login_info(self, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.login_info(), callback)

    def get_group_member_list(self, group_id: int, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.group_member_list(group_id), callback)

    def get_group_list(self, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.group_list(), callback)

    def call_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        pack = packets.build(action, params)
        if callback is not None:
            self.call_api(pack, callback, timeout)
        else:
            self.send_pack(pack)