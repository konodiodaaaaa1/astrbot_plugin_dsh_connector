"""WebSocket bridge for DSH host-to-client waterfalls.

Approval requests are not part of the HTTP RPC surface: DSH forwards them over
the ``/api/remote.mux`` WebSocket as ``approval/request`` waterfall frames, and
a client answers through the ``$events/result`` RPC.  This bridge owns that
channel (with reconnects) and hands every frame to a plugin callback.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from typing import Any, Awaitable, Callable

import aiohttp

try:
    from .dsh_client import DshError, DshHttpClient
except ImportError:  # AstrBot loads a plugin main.py as a module too.
    from dsh_client import DshError, DshHttpClient


MUX_PATH = "/api/remote.mux"
EVENTS_ENDPOINT = "$events"
EVENTS_RESULT_ENDPOINT = "$events/result"
APPROVAL_EVENT = "approval/request"

FrameHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class DshEventBridge:
    """Maintain one forwarded-event WebSocket and answer pending waterfalls."""

    def __init__(
        self,
        client: DshHttpClient,
        http_session: aiohttp.ClientSession,
        on_frame: FrameHandler,
        logger: Any,
        reconnect_seconds: float = 2.0,
        max_reconnect_seconds: float = 30.0,
        client_factory: Callable[[], DshHttpClient] | None = None,
    ) -> None:
        self.client = client
        self.client_factory = client_factory
        self.http_session = http_session
        self.on_frame = on_frame
        self.logger = logger
        self.reconnect_seconds = reconnect_seconds
        self.max_reconnect_seconds = max_reconnect_seconds
        self.client_id: str | None = None
        self.connected = False
        self._task: asyncio.Task | None = None
        self._closed = False
        self._handlers: set[asyncio.Task] = set()

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
        return self._task

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        for task in list(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        self._handlers.clear()
        self.connected = False
        self.client_id = None

    async def run(self) -> None:
        delay = self.reconnect_seconds
        while not self._closed:
            try:
                await self._connect_once()
                delay = self.reconnect_seconds
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError, DshError) as exc:
                self.logger.warning("DSH 事件桥断开：%s", exc)
            except Exception as exc:  # keep the bridge alive across protocol surprises
                self.logger.exception("DSH 事件桥异常：%s", exc)
            self.connected = False
            self.client_id = None
            if self._closed:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_reconnect_seconds)

    def _refresh_client(self) -> DshHttpClient:
        """Re-resolve the transport before a reconnect (the app may have restarted)."""
        if self.client_factory is not None:
            self.client = self.client_factory()
        return self.client

    async def _connect_once(self) -> None:
        client = self._refresh_client()
        cookie = client.auth_cookie()
        url = client.websocket_url(MUX_PATH)
        async with self.http_session.ws_connect(
            url,
            headers={"cookie": cookie},
            heartbeat=30,
            max_msg_size=8 * 1024 * 1024,
        ) as websocket:
            await websocket.send_json({
                "type": "open",
                "streamId": "dsh-connector-events",
                "endpoint": EVENTS_ENDPOINT,
                "payload": {"args": {}},
            })
            async for message in websocket:
                if self._closed:
                    return
                if message.type == aiohttp.WSMsgType.TEXT:
                    self._receive(message.data)
                elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    return

    def _receive(self, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            return
        if not isinstance(frame, dict) or frame.get("type") != "item":
            return
        value = frame.get("value")
        if not isinstance(value, dict):
            return
        if value.get("type") == "ready":
            self.client_id = str(value.get("clientId") or "") or None
            self.connected = self.client_id is not None
            return
        task = asyncio.create_task(self._dispatch(value))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        try:
            result = self.on_frame(frame)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.exception("处理 DSH 远端事件失败：%s", exc)

    async def answer(self, event_id: str, outcome: str) -> None:
        """Resolve one pending waterfall with an approval outcome."""
        if not self.client_id:
            raise DshError("DSH 事件桥尚未就绪，无法回送授权结果。")
        await self.client.rpc(
            self.http_session,
            EVENTS_RESULT_ENDPOINT,
            {
                "clientId": self.client_id,
                "eventId": event_id,
                "outcome": {"kind": "result", "value": outcome},
            },
        )
