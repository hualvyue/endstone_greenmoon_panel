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
import hmac
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
# 一个包最多重试几次（超过就丢弃，避免队头阻塞 / 无限重发）
SEND_MAX_TRIES = 5

class OneBotAPIError(RuntimeError):
    """OneBot 对端返回了非 0 的 retcode。

    单独定义成一个类型，是为了让调用方能明确区分
    「网络/连接层失败」和「协议层拒绝了这次调用」——
    两者该做的补救完全不同（前者重试，后者改参数）。
    """
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
        # 重试暂存位（代替「把整条队列掏空再塞回去」的做法，见 _requeue_head）
        self._retry_head = None
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

    @staticmethod
    def _extract_bearer(value: str) -> str:
        """从 Authorization 头里取出 token。

        兼容三种常见写法：``Bearer xxx`` / ``Token xxx`` / 裸 ``xxx``。
        原来只认字面量 ``Bearer ``，而 NapCat / Lagrange 的部分版本
        发的是 ``Token xxx``，另有一些网关直接发裸 token ——
        结果是「配置里 token 填得完全正确，反向连接还是被 4001 拒掉」。
        """
        s = str(value or "").strip()
        if not s:
            return ""
        parts = s.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
            return parts[1].strip()
        return s

    async def _reverse_serve(self) -> None:
        async def handler(ws: Any) -> None:
            headers = getattr(ws, "request", None)
            headers = getattr(headers, "headers", {}) or {}
            auth = headers.get("Authorization", "")
            # 头名大小写不敏感：http 库会把 header 规范化成 Title-Case
            # （X-Self-Id），而 OneBot 规范写的是 X-Self-ID。
            self_id = headers.get("X-Self-ID", "") or headers.get("X-Self-Id", "")

            if self.access_token:
                got = self._extract_bearer(auth)
                if not hmac.compare_digest(str(got), str(self.access_token)):
                    self.logger.warning("[OneBot] 反向连接鉴权失败")
                    await ws.close(code=4001, reason="unauthorized")
                    return

            # ★ 只接收「属于本卡片」的客户端。
            # 原来无论 X-Self-ID 是什么都 ``self._clients.add(ws)``，
            # 而广播/调用又是发给 _clients 里的**所有**连接。
            # 于是：一个 QQ 号配置错误、或者同一个 NapCat 实例连到了
            # 两张卡片上，就会让「本该走 A 卡的回复」被 B 卡也发一遍，
            # 玩家看到重复消息；反过来 A 卡的调用可能落在 B 号上执行。
            # 配置了 bot_qq 且客户端自报的 self_id 不匹配时，明确拒绝。
            if self.bot_qq and self_id and str(self_id) != str(self.bot_qq):
                self.logger.warning(
                    f"[OneBot] 反向连接 self_id={self_id} 与卡片配置的 "
                    f"bot_qq={self.bot_qq} 不一致，已拒绝"
                )
                await ws.close(code=4003, reason="self_id mismatch")
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
                # ★ retcode 判定要覆盖三种失败表示。
                # OneBot v11 里 status 可能是 "ok"/"async"/"failed"，
                # 而**部分实现（含 NapCat 某些版本）根本不发 status**，
                # 只给 retcode。原判断用 ``data.get("status") == "failed"``
                # 只覆盖了 status，一旦对端只发 retcode=100（参数错）
                # 或 1400（群不存在），这里会当成功放过去 ——
                # 上层 await 到的结果是「成功」，于是「消息发不出去但界面显示已发送」。
                failed = False
                status = data.get("status")
                retcode = data.get("retcode")
                if status == "failed":
                    failed = True
                elif retcode is not None:
                    try:
                        if int(retcode) != 0:
                            failed = True
                    except (TypeError, ValueError):
                        failed = True
                if failed:
                    self.logger.warning(
                        f"[OneBot] API 失败 retcode={retcode} status={status} "
                        f"wording={data.get('wording') or data.get('message') or ''}"
                    )
                if not fut.done():
                    # 失败时给 awaiting 的上层一个明确的异常，而不是 None，
                    # 否则调用方无法区分「成功但没数据」和「失败了」。
                    if failed:
                        fut.set_exception(
                            OneBotAPIError(
                                f"retcode={retcode} status={status} "
                                f"{data.get('wording') or data.get('message') or ''}".strip()
                            )
                        )
                    else:
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
        """把失败的包放回队首重试。

        ⚠ 只放回**原包**，不要连同整个队列一起重排。
        原实现先把队列里所有元素 ``get_nowait()`` 掏空，再依次 put 回去 ——
        这在并发下会把别的协程刚入队的包也一起搬一遍；更糟的是
        ``q.get_nowait()`` 与 ``_evict_oldest`` 的淘汰可能同时发生，
        导致同一个包被 put 两次（重复发送）。
        真正的「放回队首」用 ``putleft`` 语义：asyncio.Queue 没有
        ``putleft``，所以这里用一个显式的``_retry_head`` 暂存位，
        sender_loop 每轮优先取它。
        """
        self._retry_head = pack

    async def _sender_loop(self) -> None:
        while self._running:
            try:
                await self._connected_event.wait()
                if getattr(self, "_retry_head", None) is not None:
                    pack = self._retry_head
                    self._retry_head = None
                else:
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
                # 退避 + 上限：原实现是「失败 → 原样重排队 → sleep 2s」，
                # 对端长时间不可达时这个包会被无限重发，
                # 而且每轮都占着队列头，后面的消息永远发不出去（队头阻塞）。
                pack["_send_tries"] = int(pack.get("_send_tries", 0) or 0) + 1
                if pack["_send_tries"] <= SEND_MAX_TRIES:
                    self._requeue_head(pack)
                else:
                    self.logger.error(
                        f"[OneBot] 包重试 {SEND_MAX_TRIES} 次仍失败，丢弃 "
                        f"action={pack.get('action')}"
                    )
                    self._drop_pack(pack)
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

    # ------------------------------------------------- 群文件 / 图片 / 语音
    #
    # 下面这些是 OneBot v11 的「富媒体」能力。packets.py 里早就把包构建器
    # 写好了，但一直没接上来，所以子插件拿不到。
    #
    # ⚠ 与 send_group_msg 的区别：这些 action **必须等待返回值**
    #   （拿文件列表 / 拿下载链接 / 确认上传结果），所以走 call_api，
    #   由调用方传 callback。callback 在适配器的 asyncio 线程里执行，
    #   不要去碰游戏世界状态。

    def get_group_root_files(
        self, group_id: int, file_count: int = 50,
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        """列出群文件根目录（返回 files / folders）。"""
        self.call_api(packets.group_root_files(group_id, file_count), callback, timeout)

    def get_group_files_by_folder(
        self, group_id: int, folder_id: str, file_count: int = 50,
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        """列出群文件某个子目录。"""
        self.call_api(
            packets.group_files_by_folder(group_id, folder_id, file_count),
            callback, timeout,
        )

    def get_group_file_url(
        self, group_id: int, file_id: str,
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        """取群文件下载直链（返回 {"url": ...}）。"""
        self.call_api(packets.group_file_url(group_id, file_id), callback, timeout)

    def get_group_file_system_info(
        self, group_id: int,
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        """取群文件系统信息（文件数 / 上限等）。"""
        self.call_api(packets.group_file_system_info(group_id), callback, timeout)

    def upload_group_file(
        self, group_id: int, file: str, name: str, folder_id: str | None = None,
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        """上传文件到群。

        ``file`` 可以是本地绝对路径，也可以是 ``file://`` URI。
        是否支持远程 URL / base64 取决于具体的 OneBot 实现。
        """
        self.call_api(packets.upload_group_file(group_id, file, name, folder_id),
                      callback, timeout)

    def delete_group_file(
        self, group_id: int, file_id: str,
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        """删除群文件（注意：不可恢复，调用方需自行确认）。"""
        self.call_api(packets.delete_group_file(group_id, file_id), callback, timeout)

    def get_image(
        self, file: str,
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        """用图片 ``file`` 标识换取可下载的 url。

        消息段里的 ``file`` 通常就是一个文件名（如 ``abc.jpg``），
        拿到 url 后才能下载真实图片内容。
        """
        self.call_api(packets.image_info(file), callback, timeout)

    def get_record(
        self, file: str, out_format: str = "mp3",
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        """用语音 ``file`` 标识换取音频文件路径。"""
        self.call_api(packets.record_info(file, out_format), callback, timeout)

    # ---------------------------------------------------- 同步调用（阻塞）
    #
    # gm.bot 的对外 API 是**同步**风格（子插件里直接 `data = bot.xxx()`），
    # 而上面那些 call_xxx 都是回调式。这里提供一个把回调转成阻塞等待的
    # 适配器，专门给 gm.bot 用。
    #
    # ⚠ 绝不能在服务器主线程调用（会卡服）。gm.bot 里的这些入口一律是
    #   「异步版」或者「由调用方在后台线程里用」，不要在 tick 里直接用。

    def call_api_sync(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> Any:
        """同步调用一个 OneBot action，返回结果（失败返回 None）。

        内部用一个 threading.Event 等待 asyncio 线程回调；超时返回 None。
        """
        done = threading.Event()
        box: dict[str, Any] = {"data": None}

        def _cb(data: Any) -> None:
            box["data"] = data
            done.set()

        try:
            self.call_action(action, params, _cb, timeout)
        except Exception:
            return None
        # 多给 1 秒余量，让 call_api 内部的 wait_for 先超时并回调 None
        if not done.wait(timeout + 1.0):
            return None
        return box["data"]

    def get_group_root_files_sync(self, group_id: int, file_count: int = 50,
                                  timeout: float = API_TIMEOUT) -> Any:
        """``get_group_root_files`` 的同步版本。"""
        return self.call_api_sync("get_group_root_files",
                                  {"group_id": group_id, "file_count": file_count},
                                  timeout)

    def get_group_file_url_sync(self, group_id: int, file_id: str,
                                timeout: float = API_TIMEOUT) -> Any:
        """``get_group_file_url`` 的同步版本。"""
        return self.call_api_sync("get_group_file_url",
                                  {"group_id": group_id, "file_id": file_id},
                                  timeout)

    def upload_group_file_sync(self, group_id: int, file: str, name: str,
                               folder_id: str | None = None,
                               timeout: float = 60.0) -> Any:
        """``upload_group_file`` 的同步版本（上传大文件较慢，超时给足）。"""
        params: dict[str, Any] = {"group_id": group_id, "file": file, "name": name}
        if folder_id:
            params["folder_id"] = folder_id
        return self.call_api_sync("upload_group_file", params, timeout)

    def get_image_sync(self, file: str, timeout: float = API_TIMEOUT) -> Any:
        """``get_image`` 的同步版本。"""
        return self.call_api_sync("get_image", {"file": file}, timeout)