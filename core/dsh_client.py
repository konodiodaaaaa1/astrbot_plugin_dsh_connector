"""Typed convenience client for the DSH Web RPC surface."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import aiohttp

try:
    from ..dsh_bridge_helpers import DshReply, event_seq, merge_replies
except ImportError:
    from dsh_bridge_helpers import DshReply, event_seq, merge_replies


class DshError(Exception):
    """A DSH business or transport error suitable for presenting to users."""


class DshConnectionError(DshError):
    """The configured DSH Web host could not be reached."""


class DshTimeout(DshError):
    """A DSH operation exceeded the connector timeout."""


class DshHttpClient:
    """Client for the DSH ``/api/<rpc-method>`` JSON transport."""

    def __init__(self, base_url: str, timeout: float, poll_interval: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval

    async def rpc(self, session: aiohttp.ClientSession, method: str, payload: dict[str, Any]) -> Any:
        envelope = {
            "type": "client-request",
            "rpcId": str(uuid.uuid4()),
            "method": method,
            "payload": payload,
        }
        url = f"{self.base_url}/api/{method}"
        try:
            async with session.post(
                url,
                json=envelope,
                headers={"content-type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    raise DshError(f"DSH 返回 HTTP {response.status}: {body[:300]}")
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise DshConnectionError(f"连接 DeepSeek Harness 失败（{url}）：{exc}") from exc

        if not isinstance(data, dict) or data.get("type") != "server-response":
            raise DshError(f"DSH 返回了意外响应：{str(data)[:300]}")
        result = data.get("result") or {}
        if not result.get("ok"):
            error = result.get("error") or {}
            details = error.get("details")
            suffix = f"；{details}" if details else ""
            raise DshError(f"DSH 错误 {error.get('code')}: {error.get('message')}{suffix}")
        return result.get("value")

    async def describe(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        return await self.rpc(session, "host.describe", {})

    async def list_sessions(self, session: aiohttp.ClientSession) -> list[dict[str, Any]]:
        value = await self.rpc(session, "session.list", {})
        return value.get("items", []) if isinstance(value, dict) else []

    async def search_sessions(self, session: aiohttp.ClientSession, query: str) -> dict[str, Any]:
        return await self.rpc(session, "session.search", {"query": query})

    async def create_session(
        self,
        session: aiohttp.ClientSession,
        cwd: str = "",
        agent_preset: str = "",
        workspace_id: str = "",
    ) -> str:
        payload: dict[str, Any] = {}
        if workspace_id:
            payload["workspaceId"] = workspace_id
        elif cwd:
            payload["cwd"] = cwd
        if agent_preset:
            payload["agentPreset"] = agent_preset
        value = await self.rpc(session, "session.create", payload)
        return str((value or {}).get("sessionId") or "")

    async def rename_session(self, session: aiohttp.ClientSession, session_id: str, title: str) -> dict[str, Any]:
        return await self.rpc(session, "session.rename", {"sessionId": session_id, "title": title})

    async def fork_session(
        self, session: aiohttp.ClientSession, session_id: str, at_seq: int | None = None
    ) -> str:
        payload: dict[str, Any] = {"sessionId": session_id}
        if at_seq is not None:
            payload["atSeq"] = at_seq
        value = await self.rpc(session, "session.fork", payload)
        return str((value or {}).get("sessionId") or "")

    async def prompt(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        text: str,
        mode: str = "queue",
    ) -> dict[str, Any]:
        return await self.rpc(
            session,
            "session.prompt",
            {
                "sessionId": session_id,
                "mode": mode,
                "content": [{"type": "text", "text": text}],
                "clientTimeZone": time.tzname[0],
            },
        )

    async def cancel(self, session: aiohttp.ClientSession, session_id: str) -> bool:
        value = await self.rpc(session, "session.cancel", {"sessionId": session_id})
        return bool((value or {}).get("accepted"))

    async def history_value(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        max_messages: int = 100,
        before_seq: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"sessionId": session_id, "maxMessages": max_messages}
        if before_seq is not None:
            payload["beforeSeq"] = before_seq
        value = await self.rpc(session, "session.history", payload)
        return value if isinstance(value, dict) else {}

    async def history(self, session: aiohttp.ClientSession, session_id: str, max_messages: int = 100) -> list:
        return (await self.history_value(session, session_id, max_messages)).get("events", [])

    async def models(self, session: aiohttp.ClientSession, session_id: str) -> dict[str, Any]:
        value = await self.rpc(session, "session.models", {"sessionId": session_id})
        return value if isinstance(value, dict) else {}

    async def select_model(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        provider: str,
        model: str,
        reasoning_effort: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"sessionId": session_id, "provider": provider, "model": model}
        if reasoning_effort:
            payload["reasoningEffort"] = reasoning_effort
        value = await self.rpc(session, "session.selectModel", payload)
        return value if isinstance(value, dict) else {}

    async def update_queue(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        item_id: str,
        action: str,
        text: str = "",
    ) -> dict[str, Any]:
        action_value: dict[str, Any] = {"kind": action}
        if action == "edit":
            action_value["content"] = [{"type": "text", "text": text}]
        return await self.rpc(
            session,
            "session.updateQueue",
            {"sessionId": session_id, "itemId": item_id, "action": action_value},
        )

    async def attachment(
        self, session: aiohttp.ClientSession, session_id: str, attachment_id: str
    ) -> dict[str, Any]:
        value = await self.rpc(
            session,
            "session.attachment",
            {"sessionId": session_id, "attachmentId": attachment_id},
        )
        return value if isinstance(value, dict) else {}

    async def resolve_reply_attachments(
        self, session: aiohttp.ClientSession, session_id: str, reply: DshReply
    ) -> DshReply:
        resolved: list[str] = []
        for source in reply.image_sources:
            if not source.startswith("dsh-attachment:"):
                if source not in resolved:
                    resolved.append(source)
                continue
            attachment_id = source.removeprefix("dsh-attachment:")
            try:
                value = await self.attachment(session, session_id, attachment_id)
            except DshError:
                continue
            attachment = value.get("attachment") or {}
            media_type = str(attachment.get("mediaType") or "image/png")
            data = value.get("data")
            if isinstance(data, str) and data:
                data_url = f"data:{media_type};base64,{data}"
                if data_url not in resolved:
                    resolved.append(data_url)
        return DshReply(text=reply.text, image_sources=resolved)

    async def settings(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        value = await self.rpc(session, "settings.describe", {})
        return value if isinstance(value, dict) else {}

    async def mutate_settings(
        self,
        session: aiohttp.ClientSession,
        namespace: str,
        operations: list[dict[str, Any]],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"ns": namespace, "ops": operations}
        if expected_revision is not None:
            payload["expectedRevision"] = expected_revision
        return await self.rpc(session, "settings.mutate", payload)

    async def providers(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        return await self.rpc(session, "llm.providers", {})

    async def global_models(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        return await self.rpc(session, "llm.models", {})

    async def presets(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        return await self.rpc(session, "agentPreset.list", {})

    async def select_preset(
        self, session: aiohttp.ClientSession, session_id: str, agent_preset: str
    ) -> dict[str, Any]:
        return await self.rpc(
            session,
            "agentPreset.select",
            {"sessionId": session_id, "agentPreset": agent_preset},
        )

    async def read_preset(self, session: aiohttp.ClientSession, agent_preset: str) -> dict[str, Any]:
        return await self.rpc(session, "agentPreset.read", {"agentPreset": agent_preset})

    async def skills(self, session: aiohttp.ClientSession, session_id: str) -> dict[str, Any]:
        return await self.rpc(session, "skill.list", {"sessionId": session_id})

    async def subagents(self, session: aiohttp.ClientSession, session_id: str) -> dict[str, Any]:
        return await self.rpc(session, "subagent.list", {"parentSessionId": session_id})

    async def interrupt_subagent(
        self, session: aiohttp.ClientSession, parent_session_id: str, child_session_id: str
    ) -> dict[str, Any]:
        return await self.rpc(
            session,
            "subagent.interrupt",
            {"parentSessionId": parent_session_id, "childSessionId": child_session_id, "mode": "continuable"},
        )

    async def workspaces(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        return await self.rpc(session, "workspace.list", {})

    async def create_workspace(self, session: aiohttp.ClientSession, path: str) -> dict[str, Any]:
        return await self.rpc(session, "workspace.create", {"path": path})

    async def rename_workspace(
        self, session: aiohttp.ClientSession, workspace_id: str, title: str
    ) -> dict[str, Any]:
        return await self.rpc(
            session,
            "workspace.rename",
            {"workspaceId": workspace_id, "title": title},
        )

    async def goal_action(
        self,
        session: aiohttp.ClientSession,
        action: str,
        session_id: str,
        goal: dict[str, Any] | None = None,
        objective: str = "",
        max_rounds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"sessionId": session_id}
        if action == "create":
            payload["objective"] = objective
            if max_rounds is not None:
                payload["maxGoalRounds"] = max_rounds
        else:
            if not goal:
                raise DshError("当前会话没有可操作的 goal。")
            payload["ref"] = {"id": goal.get("id"), "revision": goal.get("revision")}
            if action == "edit":
                if objective:
                    payload["objective"] = objective
                if max_rounds is not None:
                    payload["maxGoalRounds"] = max_rounds
        return await self.rpc(session, f"goal.{action}", payload)

    async def run_prompt(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        text: str,
        mode: str = "queue",
    ) -> DshReply:
        if mode == "queue":
            await self._wait_idle(session, session_id)
        baseline = await self._last_seq(session, session_id)
        await self.prompt(session, session_id, text, mode=mode)
        return await self._await_reply(session, session_id, baseline)

    async def _last_seq(self, session: aiohttp.ClientSession, session_id: str) -> int:
        events = await self.history(session, session_id, max_messages=5)
        return max((event_seq(entry.get("event") if isinstance(entry, dict) else entry) for entry in events), default=0)

    async def _wait_idle(self, session: aiohttp.ClientSession, session_id: str) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            sessions = await self.list_sessions(session)
            current = next((item for item in sessions if item.get("sessionId") == session_id), None)
            if current is None or not current.get("running"):
                return
            await asyncio.sleep(self.poll_interval)
        raise DshTimeout(f"等待 DeepSeek Harness 会话空闲超时（{self.timeout} 秒）")

    async def _await_reply(
        self, session: aiohttp.ClientSession, session_id: str, baseline: int
    ) -> DshReply:
        deadline = time.monotonic() + self.timeout
        last_reply = DshReply()
        while time.monotonic() < deadline:
            events = await self.history(session, session_id, max_messages=100)
            reason = None
            for entry in events:
                event = entry.get("event") if isinstance(entry, dict) else None
                if not event or event_seq(event) <= baseline:
                    continue
                if event.get("type") == "assistant/message":
                    last_reply = merge_replies(events, after_seq=baseline)
                elif event.get("type") == "turn/end":
                    reason = (event.get("data") or {}).get("reason") or {}
            if reason is not None:
                if reason.get("kind") == "error":
                    error = reason.get("error") or {}
                    raise DshError(
                        f"DeepSeek Harness 任务失败：{error.get('code', 'error')} {error.get('message', '')}".strip()
                    )
                return await self.resolve_reply_attachments(session, session_id, last_reply)
            await asyncio.sleep(self.poll_interval)
        if last_reply.text or last_reply.image_sources:
            timed_out = DshReply(
                text=(last_reply.text + "\n\n任务等待超时，DSH 可能仍在后台运行。").strip(),
                image_sources=last_reply.image_sources,
            )
            return await self.resolve_reply_attachments(session, session_id, timed_out)
        raise DshTimeout(f"等待 DeepSeek Harness 回复超时（{self.timeout} 秒）")
