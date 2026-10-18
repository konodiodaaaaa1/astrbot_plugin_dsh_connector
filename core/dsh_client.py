"""Typed convenience client for the DSH 0.2+ Web RPC surface.

DSH 0.2 replaced the dotted RPC names with a Typert gateway whose endpoints are
``<namespace>/<method>`` and whose payload is ``{"args": {...}}``.  Every
``/api`` request also carries the signed browser-session cookie that the Web
transport mints during its root token exchange.  The cookie can be derived
locally from the ``client-connection/browser-session`` signing secret stored in
``$DSH_HOME/.credentials.yaml``, which keeps this connector restart-proof
without asking the operator to copy the per-process launch token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

try:
    from ..dsh_connector_helpers import DshReply, assistant_reply, event_seq
except ImportError:
    from dsh_connector_helpers import DshReply, assistant_reply, event_seq


class DshError(Exception):
    """A DSH business or transport error suitable for presenting to users."""


class DshConnectionError(DshError):
    """The configured DSH Web host could not be reached."""


class DshTimeout(DshError):
    """A DSH operation exceeded the connector timeout."""


WINDOWS_TIME_ZONE_MAP = {
    "中国标准时间": "Asia/Shanghai",
    "China Standard Time": "Asia/Shanghai",
}

DSH_HOME_ENV = "DSH_HOME"
CREDENTIALS_FILENAME = ".credentials.yaml"
WORKSPACE_STORAGE = ("storages", "workspace.json")
BROWSER_SESSION_RECORD = "client-connection/browser-session"
AUTH_COOKIE_PREFIX = "dsh-auth-"
AUTH_COOKIE_VERSION = 1
AUTH_COOKIE_TTL_MS = 60 * 60 * 1000


def normalize_client_time_zone(value: str = "") -> str:
    """Return the IANA/UTC identifier accepted by DSH's prompt endpoint."""
    candidate = str(value or "").strip()
    if not candidate:
        candidate = WINDOWS_TIME_ZONE_MAP.get(time.tzname[0], "UTC")
    candidate = WINDOWS_TIME_ZONE_MAP.get(candidate, candidate)
    if candidate != "UTC" and "/" not in candidate:
        return "UTC"
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return candidate


def resolve_dsh_home(explicit: str = "") -> Path:
    """Resolve the DSH home directory from config, environment, or default."""
    candidate = str(explicit or "").strip()
    if not candidate:
        candidate = str(os.environ.get(DSH_HOME_ENV) or "").strip()
    return Path(candidate).expanduser() if candidate else Path.home() / ".dsh"


def _base64url(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").replace("+", "-").replace("/", "_").rstrip("=")


def _decode_base64url(value: str) -> bytes | None:
    text = str(value or "").strip()
    if not text or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in text):
        return None
    padded = text + "=" * ((4 - len(text) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, TypeError):
        return None
    return decoded if _base64url(decoded) == text else None


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def extract_browser_session_secret(text: str) -> bytes | None:
    """Read ``client-connection/browser-session``'s secret from the YAML text.

    The credentials file is a flat mapping of record keys to small YAML blocks;
    scanning the one record keeps the connector free of a PyYAML dependency.
    """
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip() != BROWSER_SESSION_RECORD + ":":
            continue
        record_indent = _indent_width(line)
        for follow in lines[index + 1:]:
            stripped = follow.strip()
            if stripped and _indent_width(follow) <= record_indent:
                break
            if stripped.startswith("secret:"):
                raw = stripped.partition(":")[2].strip().strip("'\"")
                return _decode_base64url(raw) if raw else None
    return None


def load_browser_session_secret(home: Path) -> bytes:
    """Load the cookie-signing secret DSH persists for its Web profile."""
    path = Path(home) / CREDENTIALS_FILENAME
    if not path.is_file():
        raise DshError(
            f"未找到 DSH 浏览器会话凭据：{path}。请把插件配置 dsh_home（或环境变量 DSH_HOME）"
            "指向正在运行的 DSH 主目录（默认 %USERPROFILE%\\.dsh），并先运行一次 dsh web。"
        )
    try:
        text = path.read_text("utf-8")
    except OSError as exc:
        raise DshError(f"读取 DSH 凭据失败：{path}：{exc}") from exc
    secret = extract_browser_session_secret(text)
    if secret is None:
        raise DshError(
            f"DSH 凭据文件里没有 {BROWSER_SESSION_RECORD} 记录：{path}。"
            "请重启 dsh web 让该进程生成浏览器会话密钥。"
        )
    return secret


def build_auth_cookie(secret: bytes, authority: str, ttl_ms: int = AUTH_COOKIE_TTL_MS) -> str:
    """Build the signed ``dsh-auth-<authority hash>`` cookie for one request."""
    name = AUTH_COOKIE_PREFIX + _base64url(hashlib.sha256(authority.encode("utf-8")).digest())
    now = int(time.time() * 1000)
    payload = {
        "version": AUTH_COOKIE_VERSION,
        "authority": authority,
        "issuedAt": now,
        "expiresAt": now + int(ttl_ms),
    }
    body = _base64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = _base64url(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{name}=v1.{body}.{signature}"


class DshHttpClient:
    """Client for the DSH 0.2+ ``/api/<namespace>/<method>`` JSON transport."""

    def __init__(
        self,
        base_url: str,
        timeout: float,
        poll_interval: float,
        dsh_home: str = "",
        default_cwd: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.dsh_home = resolve_dsh_home(dsh_home)
        self.default_cwd_hint = str(default_cwd or "").strip()
        self.authority = urlparse(self.base_url).netloc or self.base_url
        self._secret: bytes | None = None
        self._secret_error: str | None = None

    # ---------------- 传输与认证 ----------------

    def _auth_cookie(self) -> str:
        if self._secret is None:
            if self._secret_error is not None:
                raise DshError(self._secret_error)
            try:
                self._secret = load_browser_session_secret(self.dsh_home)
            except DshError as exc:
                self._secret_error = str(exc)
                raise
        return build_auth_cookie(self._secret, self.authority)

    @staticmethod
    def _http_error(method: str, status: int, body: str) -> DshError:
        if status == 401:
            return DshError(
                "DSH 拒绝请求（401）：缺少浏览器会话凭据。请确认 dsh_home 指向正在运行的 DSH 的 "
                "DSH_HOME（默认 ~/.dsh），且其中存在 .credentials.yaml。"
            )
        if status == 403:
            return DshError(
                "DSH 拒绝请求（403）：Host/Origin 校验失败。请把 http_base_url 设为 "
                "http://127.0.0.1:<端口> 这类回环地址。"
            )
        if status == 404:
            return DshError(
                f"DSH 不支持接口 {method}（404）。请确认 DSH 版本为 0.2 及以上，且接口名没有被改动。"
            )
        return DshError(f"DSH 返回 HTTP {status}: {body[:300]}")

    async def rpc(self, session: aiohttp.ClientSession, method: str, args: dict[str, Any] | None = None) -> Any:
        envelope = {
            "type": "client-request",
            "rpcId": str(uuid.uuid4()),
            "method": method,
            "payload": {"args": dict(args or {})},
        }
        url = f"{self.base_url}/api/{method}"
        headers = {"content-type": "application/json", "cookie": self._auth_cookie()}
        try:
            async with session.post(
                url,
                json=envelope,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    raise self._http_error(method, response.status, body)
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

    async def execute_command(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        line: str,
    ) -> dict[str, Any]:
        """Execute one DSH host slash command for an Agent."""
        value = await self.rpc(
            session,
            "commands/execute",
            {"agentId": session_id, "line": line, "submittedAttachments": []},
        )
        if not isinstance(value, dict):
            raise DshError("DSH 命令返回了意外结果")
        command_result = value.get("result") or {}
        if command_result.get("kind") == "error":
            raise DshError(str(command_result.get("text") or "DSH 命令执行失败"))
        return value

    # ---------------- 主机描述 ----------------

    async def describe(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        """Describe the host from the settings document and local DSH state."""
        description = await self.settings(session)
        default_model: dict[str, Any] = {}
        for row in description.get("namespaces") or []:
            if isinstance(row, dict) and row.get("ns") == "agent-default-model":
                default_model = row.get("value") or {}
                break
        return {
            "cwd": self.default_cwd(),
            "home": str(self.dsh_home),
            "provider": default_model.get("provider") or "?",
            "model": default_model.get("model") or "?",
            "reasoningEffort": default_model.get("reasoningEffort") or "",
            "version": "0.2+ (Typert)",
        }

    def default_cwd(self) -> str:
        if self.default_cwd_hint:
            return self.default_cwd_hint
        for item in self.workspace_items():
            path = str(item.get("path") or "").strip()
            if path:
                return path
        return str(Path.home())

    def workspace_items(self) -> list[dict[str, Any]]:
        """Read the Workspace registry DSH persists for its Web sidebar."""
        path = self.dsh_home.joinpath(*WORKSPACE_STORAGE)
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, dict):
            return []
        tables = ((raw.get("tables") or {}).get("workspaces") or {})
        if not isinstance(tables, dict):
            return []
        order = ((raw.get("global") or {}).get("workspaceIds") or [])
        items: list[dict[str, Any]] = []
        for workspace_id in order:
            row = tables.get(workspace_id)
            if isinstance(row, dict):
                items.append({"workspaceId": workspace_id, **row})
        for workspace_id, row in tables.items():
            if workspace_id not in order and isinstance(row, dict):
                items.append({"workspaceId": workspace_id, **row})
        return items

    async def workspaces(self, _session: aiohttp.ClientSession | None = None) -> dict[str, Any]:
        return {"items": self.workspace_items()}

    # ---------------- 会话 ----------------

    async def list_sessions(self, session: aiohttp.ClientSession) -> list[dict[str, Any]]:
        value = await self.rpc(session, "session/list", {"_request": {}})
        return value.get("items", []) if isinstance(value, dict) else []

    async def search_sessions(self, session: aiohttp.ClientSession, query: str) -> dict[str, Any]:
        value = await self.rpc(session, "session/search", {"request": {"query": query}})
        return value if isinstance(value, dict) else {}

    async def create_session(
        self,
        session: aiohttp.ClientSession,
        cwd: str = "",
        agent_preset: str = "",
        workspace_id: str = "",
    ) -> str:
        request: dict[str, Any] = {}
        if workspace_id:
            request["workspaceId"] = workspace_id
        elif cwd:
            request["cwd"] = cwd
        if agent_preset:
            request["agentPreset"] = agent_preset
        value = await self.rpc(session, "session/create", {"request": request})
        return str((value or {}).get("sessionId") or "")

    async def rename_session(self, session: aiohttp.ClientSession, session_id: str, title: str) -> dict[str, Any]:
        value = await self.rpc(session, "session/rename", {"request": {"sessionId": session_id, "title": title}})
        return value if isinstance(value, dict) else {}

    async def fork_session(
        self, session: aiohttp.ClientSession, session_id: str, at_seq: int | None = None
    ) -> str:
        request: dict[str, Any] = {"sessionId": session_id}
        if at_seq is not None:
            request["atSeq"] = at_seq
        value = await self.rpc(session, "session/fork", {"request": request})
        return str((value or {}).get("sessionId") or "")

    async def prompt(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        text: str,
        mode: str = "queue",
        client_time_zone: str = "",
    ) -> dict[str, Any]:
        request = {
            "requestId": str(uuid.uuid4()),
            "sessionId": session_id,
            "mode": mode,
            "content": [{"type": "text", "text": text}],
            "clientTimeZone": normalize_client_time_zone(client_time_zone),
        }
        return await self.rpc(session, "session/prompt", {"request": request})

    async def cancel(self, session: aiohttp.ClientSession, session_id: str) -> bool:
        value = await self.rpc(session, "session/cancel", {"request": {"sessionId": session_id}})
        return bool((value or {}).get("accepted"))

    async def archive_session(self, session: aiohttp.ClientSession, session_id: str) -> dict[str, Any]:
        """Remove a session from DSH's visible workspace list durably."""
        value = await self.rpc(session, "workspace/archiveSession", {"request": {"sessionId": session_id}})
        return value if isinstance(value, dict) else {}

    async def projections(self, session: aiohttp.ClientSession, session_id: str) -> dict[str, Any]:
        value = await self.rpc(session, "session/projections", {"request": {"sessionId": session_id}})
        return value if isinstance(value, dict) else {}

    async def cursor(self, session: aiohttp.ClientSession, session_id: str) -> int:
        """Return the Session's current log cursor (the ``throughSeq`` ceiling)."""
        value = await self.projections(session, session_id)
        try:
            return int(value.get("asOfSeq") or 0)
        except (TypeError, ValueError):
            return 0

    async def page(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        through_seq: int,
        max_messages: int = 100,
        before_seq: int | None = None,
    ) -> dict[str, Any]:
        """Read one message-aligned backwards page ending at ``through_seq``."""
        request: dict[str, Any] = {
            "address": {"kind": "session", "sessionId": session_id},
            "throughSeq": int(through_seq),
            "maxMessages": int(max_messages),
        }
        if before_seq is not None:
            request["beforeSeq"] = int(before_seq)
        value = await self.rpc(session, "session/page", {"request": request})
        return value if isinstance(value, dict) else {}

    async def history_value(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        max_messages: int = 100,
        before_seq: int | None = None,
    ) -> dict[str, Any]:
        projections = await self.projections(session, session_id)
        try:
            through_seq = int(projections.get("asOfSeq") or 0)
        except (TypeError, ValueError):
            through_seq = 0
        value = await self.page(session, session_id, through_seq, max_messages, before_seq)
        return {
            "events": value.get("records") or [],
            "hasMore": bool(value.get("hasMore")),
            "projections": projections,
            "cursor": through_seq,
        }

    async def history(self, session: aiohttp.ClientSession, session_id: str, max_messages: int = 100) -> list:
        return (await self.history_value(session, session_id, max_messages)).get("events", [])

    async def models(self, session: aiohttp.ClientSession, session_id: str) -> dict[str, Any]:
        """Return the model catalog plus the Session's current selection."""
        catalog = await self.rpc(session, "session/modelCatalog", {})
        catalog = catalog if isinstance(catalog, dict) else {}
        selection: dict[str, Any] = {}
        try:
            projections = await self.projections(session, session_id)
            selection = ((projections.get("values") or {}).get("modelSelection") or {})
        except DshError:
            selection = {}
        current = selection.get("next") or selection.get("lastUsed") or catalog.get("default") or {}
        value = dict(catalog)
        value["current"] = current
        return value

    async def select_model(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        provider: str,
        model: str,
        reasoning_effort: str = "",
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"sessionId": session_id, "provider": provider, "model": model}
        if reasoning_effort:
            request["reasoningEffort"] = reasoning_effort
        value = await self.rpc(session, "session/selectModel", {"request": request})
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
        value = await self.rpc(
            session,
            "session/updateQueue",
            {"request": {"sessionId": session_id, "itemId": item_id, "action": action_value}},
        )
        return value if isinstance(value, dict) else {}

    async def attachment(
        self, session: aiohttp.ClientSession, session_id: str, attachment_id: str
    ) -> dict[str, Any]:
        value = await self.rpc(
            session,
            "session/attachment",
            {"request": {"sessionId": session_id, "attachmentId": attachment_id}},
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

    # ---------------- Settings ----------------

    async def settings(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        value = await self.rpc(session, "settings/describe", {})
        return value if isinstance(value, dict) else {}

    async def permission_presets(self, session: aiohttp.ClientSession) -> list[str]:
        """Read the permission preset catalog published by the DSH host."""
        try:
            catalog = await self.rpc(session, "permissionPresets/catalog", {})
        except DshError:
            catalog = {}
        values = [
            str(entry.get("value"))
            for entry in (catalog.get("options") if isinstance(catalog, dict) else []) or []
            if isinstance(entry, dict) and entry.get("value")
        ]
        if values:
            return values
        description = await self.settings(session)
        namespace = next(
            (row for row in description.get("namespaces") or []
             if isinstance(row, dict) and row.get("ns") == "permission"),
            None,
        )
        if not isinstance(namespace, dict):
            return []
        schema = namespace.get("schema") or {}
        refs = schema.get("refs") or {}
        root = refs.get(str(schema.get("uid"))) or {}
        default_ref = (root.get("dict") or {}).get("defaultPreset")
        union = refs.get(str(default_ref)) or {}
        fallback: list[str] = []
        for ref_id in union.get("list") or []:
            value = (refs.get(str(ref_id)) or {}).get("value")
            if isinstance(value, str) and value:
                fallback.append(value)
        return fallback

    async def mutate_settings(
        self,
        session: aiohttp.ClientSession,
        namespace: str,
        operations: list[dict[str, Any]],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"ns": namespace, "ops": operations}
        if expected_revision is not None:
            args["expectedRevision"] = expected_revision
        value = await self.rpc(session, "settings/mutate", args)
        return value if isinstance(value, dict) else {}

    # ---------------- LLM / Preset / Skill / Subagent ----------------

    async def providers(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        value = await self.rpc(session, "llm/listConfigurableProviders", {})
        return {"providers": value if isinstance(value, list) else []}

    async def global_models(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        value = await self.rpc(session, "session/modelCatalog", {})
        return value if isinstance(value, dict) else {}

    async def presets(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        value = await self.rpc(session, "agentPresets/list", {})
        return value if isinstance(value, dict) else {}

    async def select_preset(
        self, session: aiohttp.ClientSession, session_id: str, agent_preset: str
    ) -> dict[str, Any]:
        value = await self.rpc(
            session,
            "agentPresets/select",
            {"agentId": session_id, "agentPreset": agent_preset},
        )
        return value if isinstance(value, dict) else {}

    async def read_preset(self, session: aiohttp.ClientSession, agent_preset: str) -> dict[str, Any]:
        value = await self.rpc(session, "agentPresets/read", {"agentPreset": agent_preset})
        return value if isinstance(value, dict) else {}

    async def skills(self, session: aiohttp.ClientSession, session_id: str) -> dict[str, Any]:
        value = await self.rpc(session, "skills/list", {"request": {"sessionId": session_id}})
        return value if isinstance(value, dict) else {}

    async def subagents(self, session: aiohttp.ClientSession, session_id: str) -> dict[str, Any]:
        """List child Sessions of one parent Session.

        DSH 0.2 removed ``subagent.list``; child Sessions are ordinary Session
        rows carrying ``parentSessionId``, so the visible ones are derived from
        ``session/list`` instead of a dedicated catalog.
        """
        entries: list[dict[str, Any]] = []
        for row in await self.list_sessions(session):
            if not isinstance(row, dict):
                continue
            if str(row.get("parentSessionId") or "") != session_id:
                continue
            values = (row.get("projections") or {}).get("values") or {}
            entries.append({
                "id": row.get("sessionId"),
                "kind": "subagent",
                "mode": "",
                "activity": "running" if row.get("running") else "idle",
                "label": str(values.get("title") or ""),
            })
        return {"entries": entries}

    async def interrupt_subagent(
        self, session: aiohttp.ClientSession, parent_session_id: str, child_session_id: str
    ) -> dict[str, Any]:
        value = await self.rpc(
            session,
            "subagents/interruptByParent",
            {
                "parentSessionId": parent_session_id,
                "childSessionId": child_session_id,
                "mode": "continuable",
            },
        )
        return value if isinstance(value, dict) else {}

    # ---------------- Workspace ----------------

    async def create_workspace(self, session: aiohttp.ClientSession, path: str) -> dict[str, Any]:
        value = await self.rpc(session, "workspace/create", {"request": {"path": path}})
        return value if isinstance(value, dict) else {}

    async def rename_workspace(
        self, session: aiohttp.ClientSession, workspace_id: str, title: str
    ) -> dict[str, Any]:
        value = await self.rpc(
            session,
            "workspace/rename",
            {"request": {"workspaceId": workspace_id, "title": title}},
        )
        return value if isinstance(value, dict) else {}

    # ---------------- Goal ----------------

    async def goal(self, session: aiohttp.ClientSession, session_id: str) -> dict[str, Any] | None:
        value = await self.rpc(session, "goals/get", {"agentId": session_id})
        return value if isinstance(value, dict) else None

    async def goal_action(
        self,
        session: aiohttp.ClientSession,
        action: str,
        session_id: str,
        goal: dict[str, Any] | None = None,
        objective: str = "",
        max_rounds: int | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {}
        if action == "create":
            request["objective"] = objective
            if max_rounds is not None:
                request["maxGoalRounds"] = max_rounds
            args: dict[str, Any] = {"agentId": session_id, "request": request}
        else:
            if not goal:
                raise DshError("当前会话没有可操作的 goal。")
            args = {"agentId": session_id, "ref": {"id": goal.get("id"), "revision": goal.get("revision")}}
            if action == "edit":
                if objective:
                    request["objective"] = objective
                if max_rounds is not None:
                    request["maxGoalRounds"] = max_rounds
                args["request"] = request
        value = await self.rpc(session, f"goals/{action}", args)
        return value if isinstance(value, dict) else {}

    # ---------------- 任务执行 ----------------

    async def run_prompt(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        text: str,
        mode: str = "queue",
        client_time_zone: str = "",
    ) -> DshReply:
        if mode == "queue":
            await self._wait_idle(session, session_id)
        baseline = await self.cursor(session, session_id)
        await self.prompt(
            session,
            session_id,
            text,
            mode=mode,
            client_time_zone=client_time_zone,
        )
        return await self._await_reply(session, session_id, baseline)

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
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        baseline: int,
    ) -> DshReply:
        deadline = time.monotonic() + self.timeout
        last_reply = DshReply()
        while time.monotonic() < deadline:
            cursor = await self.cursor(session, session_id)
            if cursor > baseline:
                page = await self.page(session, session_id, cursor, max_messages=100)
                reason = None
                for entry in page.get("records") or []:
                    event = entry.get("event") if isinstance(entry, dict) else None
                    if not isinstance(event, dict) or event_seq(event) <= baseline:
                        continue
                    event_type = event.get("type")
                    if event_type == "assistant/message":
                        # DSH emits one finalized assistant message per provider/tool
                        # step. The last finalized message is the turn result; earlier
                        # messages are intermediate step output and must not be folded
                        # into the final card.
                        last_reply = assistant_reply(event)
                    elif event_type == "turn/end":
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
