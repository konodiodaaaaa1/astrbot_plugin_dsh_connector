# -*- coding: utf-8 -*-
"""astrbot_plugin_dsh_connector — DeepSeek Harness 连接器
=====================================================

将 AstrBot 与 DeepSeek Harness（DSH，本客户端平台）连接起来，实现两个平台的桥接：
用户在聊天中发送 ``/dsh <指令>``，插件会把该指令桥接到 DSH 执行，并把回复回传到
聊天中。主要面向官方 QQ 机器人（qq_official），也兼容其它平台。

支持的连接方式（在插件配置面板中选择 ``mode``）：

* ``http``     —— 连接运行中的 DSH Web Host（``dsh web``，默认 http://127.0.0.1:3080）
* ``app``      —— 连接本机正在运行的 DeepSeek Harness 桌面应用（自动发现回环端口）
* ``headless`` —— 通过 DSH 命令行 ``dsh --profile headless "<指令>"`` 一次性执行
* ``auto``     —— 依次尝试 http、app，连接失败时回退到 headless

指令示例：:

    /dsh 帮我写一段快速排序的 Python 代码
    /dsh status   查看与 DSH 的连接状态
    /dsh reset    重置当前会话（忘记多轮上下文，仅 HTTP 模式）
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
import os
import shlex
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.session_waiter import SessionController, session_waiter

try:
    from .dsh_connector_helpers import DshReply, merge_replies, model_rows
    from .core.app_host import discover_app_base_url
    from .core.approval_bridge import DshEventBridge
    from .core.dsh_client import DshConnectionError, DshError, DshHttpClient, DshTimeout, normalize_client_time_zone
    from .core.config_service import compact_json, dotted_path, format_namespaces, namespace_map, parse_json_value, read_path
    from .core.presentation import current_goal, projection_values, session_label
    from .core.session_state import SessionState
    from .core.session_options import (
        SessionSetupWizard, format_session_options, normalize_session_options, resolve_option_field,
    )
    from .core.reply_render import normalize_reply_render_mode, should_render_card, split_markdown_for_cards, split_text_for_message
except ImportError:  # AstrBot also supports loading a plugin main.py as a module.
    from dsh_connector_helpers import DshReply, merge_replies, model_rows
    from core.app_host import discover_app_base_url
    from core.approval_bridge import DshEventBridge
    from core.dsh_client import DshConnectionError, DshError, DshHttpClient, DshTimeout, normalize_client_time_zone
    from core.config_service import compact_json, dotted_path, format_namespaces, namespace_map, parse_json_value, read_path
    from core.presentation import current_goal, projection_values, session_label
    from core.session_state import SessionState
    from core.session_options import SessionSetupWizard, format_session_options, normalize_session_options, resolve_option_field
    from core.reply_render import normalize_reply_render_mode, should_render_card, split_markdown_for_cards, split_text_for_message

HELP_TEXT = """【DeepSeek Harness Connector】
用法：
  /dsh help      显示本帮助和全部可用指令
  /dsh <指令>    将指令发送给 DeepSeek Harness 执行并返回结果
  /dsh status    查看连接状态
  /dsh sessions  查看 DSH 会话列表
  /dsh setup     为当前聊天配置选项并创建新会话
  /dsh config [set <字段> <值>|reset]  管理当前聊天的新会话选项
  /dsh session switch <id> | rename <标题> | fork [seq] | search <关键词>
  /dsh session delete [id]  从 DSH 会话列表删除（需输入 delete 确认）
  /dsh model [服务商/模型] [推理强度]  查看或切换模型
  /dsh providers | global-models  查看 DSH 服务商与全局模型目录
  /dsh effort <值>  设置当前会话推理强度
  /dsh permission <预设>  设置 DSH 权限预设
  /dsh preset [list|select <名称>|read <名称>] 读取或选择 agent 预设
  /dsh settings | setting get <命名空间> [路径] | setting schema <命名空间>
  /dsh setting set <命名空间> <路径> <JSON值> | unset <命名空间> <路径>
  /dsh skills | subagents | workspaces | goal [create|pause|resume|complete|clear]
  /dsh queue <remove|steer|edit> <消息ID> [文本]
  /dsh steer <指令>  插入当前执行中的任务
  /dsh stop      取消当前会话正在执行的任务
  /dsh approve   允许当前 DSH 授权请求（工具调用）
  /dsh deny      拒绝当前 DSH 授权请求
  /dsh approvals 查看本聊天待授权的 DSH 请求
  /dsh history [条数]  查看当前会话最近记录
  /dsh reset     重置当前聊天绑定的 DSH 会话
  /dsh settings  查看 DSH 主机设置命名空间

示例：
  /dsh 帮我写一段 Python 快速排序代码
  /dsh 总结一下我桌面上的 todo.txt
"""


class Main(Star):
    """DSH 桥接插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 会话映射缓存：chat_key -> DSH session_id（HTTP 多轮模式使用）
        self._state = SessionState()
        self._temp_media: set[Path] = set()
        self._pending_approvals: dict[str, dict] = {}
        self._approval_tasks: set[asyncio.Task] = set()
        self._completion_tasks: set[asyncio.Task] = set()
        self._bridge: DshEventBridge | None = None
        self._bridge_http: aiohttp.ClientSession | None = None
        self._bridge_task: asyncio.Task | None = None
        self._bridge_entry = ""
        self._active_entry = ""

    async def initialize(self) -> None:
        """Start the DSH event bridge so approvals reach the bound chat."""
        await self._ensure_event_bridge()

    # ---------------- 配置便捷方法 ----------------

    def _cfg(self, key, default=None):
        value = self.config.get(key)
        return default if value is None else value

    @property
    def mode(self) -> str:
        mode = str(self._cfg("mode", "http")).strip().lower()
        return mode if mode in {"http", "app", "headless", "auto"} else "http"

    @property
    def dsh_command(self) -> str:
        return str(self._cfg("dsh_command", "dsh") or "dsh").strip()

    def _resolve_entry(self, entry: str = "") -> str:
        """Return the transport entry: "http" for the Web Host, "app" for the app."""
        if entry in {"http", "app"}:
            return entry
        if self.mode == "app":
            return "app"
        if self.mode == "auto" and getattr(self, "_active_entry", "") == "app":
            return "app"
        return "http"

    def _app_base_url(self) -> str:
        """Resolve the desktop application's base URL from config or discovery."""
        configured = str(self._cfg("app_base_url", "") or "").strip()
        if configured:
            if not configured.lower().startswith(("http://", "https://")):
                raise DshError(
                    "app_base_url 需要是 http(s) 地址，例如 http://127.0.0.1:10404。"
                )
            return configured.rstrip("/")
        return discover_app_base_url()

    def _http_base_url(self, entry: str = "") -> str:
        """Return the base URL of the selected DSH entry."""
        if self._resolve_entry(entry) == "app":
            return self._app_base_url()
        return str(self._cfg("http_base_url", "http://127.0.0.1:3080")).strip().rstrip("/")

    def _app_entry_description(self) -> str:
        """Describe the app entry for diagnostics without raising."""
        configured = str(self._cfg("app_base_url", "") or "").strip()
        try:
            base_url = self._app_base_url()
        except DshError:
            return "未发现正在运行的桌面应用"
        return base_url if configured else f"{base_url}（自动发现）"

    def _make_http_client(self, entry: str = "") -> DshHttpClient:
        base_url = self._http_base_url(entry)
        timeout = float(self._cfg("command_timeout", 600))
        poll_interval = float(self._cfg("poll_interval", 1.0))
        return DshHttpClient(
            base_url,
            timeout,
            poll_interval,
            dsh_home=str(self._cfg("dsh_home", "") or ""),
            default_cwd=str(self._cfg("default_cwd", "") or ""),
        )

    # ---------------- DSH 事件桥（授权请求） ----------------

    async def _ensure_event_bridge(self, entry: str = "") -> None:
        """Start or retarget the approval bridge for one transport entry."""
        if self.mode == "headless" or not bool(self._cfg("route_approvals", True)):
            return
        target = self._resolve_entry(entry)
        if self._bridge_task is not None and not self._bridge_task.done():
            if getattr(self, "_bridge_entry", "") == target:
                return
            await self._close_event_bridge()
        try:
            client = self._make_http_client(target)
        except DshError as exc:
            logger.warning("DSH 授权事件桥暂未启动：%s", exc)
            return
        if self._bridge_http is None or self._bridge_http.closed:
            self._bridge_http = aiohttp.ClientSession()
        self._bridge = DshEventBridge(
            client,
            self._bridge_http,
            self._on_remote_event,
            logger,
            client_factory=lambda: self._make_http_client(target),
        )
        self._bridge_entry = target
        self._bridge_task = self._bridge.start()

    async def _close_event_bridge(self) -> None:
        """Stop the approval bridge while keeping its HTTP session reusable."""
        if self._bridge is not None:
            await self._bridge.close()
            self._bridge = None
        self._bridge_task = None
        self._bridge_entry = ""

    async def _on_remote_event(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "waterfall" and frame.get("event") == "approval/request":
            task = asyncio.create_task(self._handle_approval(frame))
            self._approval_tasks.add(task)
            task.add_done_callback(self._approval_tasks.discard)
            return
        if kind == "cancel":
            event_id = str(frame.get("eventId") or "")
            record = self._pending_approvals.pop(event_id, None)
            if record is not None and not record["decision"].done():
                record["decision"].set_result(None)
                await self._send_text(
                    record["chat_key"],
                    "刚才的 DSH 授权请求已失效（可能已在 DSH 界面处理，或任务已取消）。",
                )

    async def _handle_approval(self, frame: dict) -> None:
        event_id = str(frame.get("eventId") or "")
        agent_id = str(frame.get("agentId") or "")
        request = frame.get("request") if isinstance(frame.get("request"), dict) else {}
        if not event_id or not agent_id or event_id in self._pending_approvals:
            return
        chat_key = await self._state.owner_of(self, agent_id)
        if not chat_key:
            # Sessions started in the DSH GUI are answered by its own approval UI.
            logger.info("DSH 授权请求 %s 未绑定聊天（session=%s）", event_id, agent_id)
            return
        tool = str(request.get("toolName") or "工具")
        reason = str(request.get("reason") or "").strip()
        display = request.get("displayReason")
        if isinstance(display, dict):
            reason = str(display.get("zh-CN") or display.get("zh") or display.get("en") or reason)
        record = {
            "event_id": event_id,
            "chat_key": chat_key,
            "agent_id": agent_id,
            "tool": tool,
            "reason": reason,
            "decision": asyncio.get_running_loop().create_future(),
        }
        self._pending_approvals[event_id] = record
        lines = ["⚠️ DSH 请求授权", f"工具：{tool}"]
        if reason:
            lines.append(f"原因：{reason}")
        lines.append(f"会话：{agent_id}")
        lines.append("回复 /dsh approve 允许一次，或 /dsh deny 拒绝。")
        await self._send_text(chat_key, "\n".join(lines))
        try:
            decision = await record["decision"]
        finally:
            self._pending_approvals.pop(event_id, None)
        if decision is None or self._bridge is None:
            return
        try:
            await self._bridge.answer(event_id, str(decision))
        except DshError as exc:
            await self._send_text(chat_key, f"⚠️ 授权结果未能送回 DSH：{exc}")

    async def _resolve_approval(self, chat_key: str, decision: str) -> str:
        record = next(
            (
                row for row in self._pending_approvals.values()
                if row.get("chat_key") == chat_key and not row["decision"].done()
            ),
            None,
        )
        if record is None:
            return "当前没有等待授权的 DSH 请求。"
        record["decision"].set_result(decision)
        return "已允许本次工具调用。" if decision == "allowed-once" else "已拒绝本次工具调用。"

    def _approvals_text(self, chat_key: str) -> str:
        rows = [row for row in self._pending_approvals.values() if row.get("chat_key") == chat_key]
        if not rows:
            return "当前没有等待授权的 DSH 请求。"
        return "\n".join(["【待授权】"] + [
            f"- {row['tool']} @ {row['agent_id']}\n  {row['reason'] or '无原因说明'}"
            for row in rows
        ])

    async def _send_text(self, chat_key: str, text: str) -> None:
        await self._send_message(chat_key, [Plain(text)])

    async def _send_message(self, chat_key: str, chain: list) -> None:
        try:
            sent = await self.context.send_message(chat_key, MessageChain(chain))
            if not sent:
                logger.warning("DSH 主动消息未被平台接受：%s", chat_key)
        except Exception as exc:
            logger.warning("DSH 主动消息发送失败：%s", exc)

    # ---------------- 会话映射（尽力持久化） ----------------

    async def _chat_key(self, event: AstrMessageEvent) -> str:
        key = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", None)
        return str(key) if key else str(event.get_sender_id())

    async def _load_session_id(self, chat_key: str):
        return await self._state.load_session(self, chat_key)

    async def _save_session_id(self, chat_key: str, session_id: str) -> None:
        try:
            await self._state.save_session(self, chat_key, session_id)
            await self._state.remember_owner(self, session_id, chat_key)
        except Exception as exc:
            logger.warning(f"保存 DSH 会话映射失败：{exc}")

    async def _clear_session_id(self, chat_key: str) -> None:
        try:
            await self._state.clear_session(self, chat_key)
        except Exception:
            pass

    async def _load_session_options(self, event: AstrMessageEvent) -> dict[str, str]:
        return await self._state.load_options(self, await self._chat_key(event))

    async def _save_session_options(
        self, event: AstrMessageEvent, options: dict[str, str]
    ) -> dict[str, str]:
        return await self._state.save_options(self, await self._chat_key(event), options)

    async def _session_for_chat(
        self,
        event: AstrMessageEvent,
        client: DshHttpClient,
        http_session: aiohttp.ClientSession,
    ) -> str:
        chat_key = await self._chat_key(event)
        session_id = await self._load_session_id(chat_key)
        if session_id:
            return session_id

        options = await self._state.load_options(self, chat_key)
        workspace_id = options.get("workspace_id", "")
        cwd = options["working_directory"] if not workspace_id else ""
        if not workspace_id and not cwd:
            cwd = str((await client.describe(http_session)).get("cwd") or "").strip()
        session_id = await client.create_session(
            http_session,
            cwd=cwd,
            workspace_id=workspace_id,
            agent_preset=options["agent_preset"],
        )
        await self._configure_session_options(client, http_session, session_id, options)
        await self._save_session_id(chat_key, session_id)
        return session_id

    async def _configure_session_options(
        self,
        client: DshHttpClient,
        http_session: aiohttp.ClientSession,
        session_id: str,
        options: dict[str, str],
    ) -> None:
        options = normalize_session_options(options)
        provider = options["provider"]
        model = options["model"]
        effort = options["reasoning_effort"]
        if provider and model:
            await client.select_model(http_session, session_id, provider, model, effort)
        permission = options["permission_preset"]
        if permission:
            await client.execute_command(
                http_session,
                session_id,
                f"/permission {permission}",
            )

    # ---------------- 传输实现 ----------------

    async def _run_http(
        self,
        event: AstrMessageEvent,
        text: str,
        prompt_mode: str = "queue",
        entry: str = "",
    ) -> DshReply:
        actual = self._resolve_entry(entry)
        if actual == "app":
            # The app may start after the plugin loaded; warm the bridge lazily.
            await self._ensure_event_bridge(actual)
        client = self._make_http_client(actual)
        self._active_entry = actual
        chat_key = await self._chat_key(event)
        async with aiohttp.ClientSession() as http_session:
            session_id = await self._session_for_chat(event, client, http_session)
            options = await self._load_session_options(event)
            if prompt_mode == "queue":
                await client.wait_idle(http_session, session_id)
            baseline = await client.cursor(http_session, session_id)
            await client.prompt(
                http_session,
                session_id,
                text,
                mode=prompt_mode,
                client_time_zone=options["client_time_zone"],
            )
            try:
                return await client.await_turn(http_session, session_id, baseline)
            except DshTimeout as exc:
                # The turn keeps running in DSH; keep watching and push the
                # result into this chat when it eventually finishes.
                self._start_completion_watcher(chat_key, session_id, baseline)
                raise DshTimeout(
                    "DSH 仍在后台执行，完成后会把结果主动发到本聊天；"
                    "也可以用 /dsh stop 取消。"
                ) from exc

    def _start_completion_watcher(self, chat_key: str, session_id: str, baseline: int) -> None:
        task = asyncio.create_task(self._watch_completion(chat_key, session_id, baseline))
        self._completion_tasks.add(task)
        task.add_done_callback(self._completion_tasks.discard)

    async def _watch_completion(self, chat_key: str, session_id: str, baseline: int) -> None:
        timeout = float(self._cfg("completion_watch_timeout", 6 * 3600) or 6 * 3600)
        entry = str(getattr(self, "_active_entry", "") or "")
        client = self._make_http_client(entry) if entry else self._make_http_client()
        async with aiohttp.ClientSession() as http_session:
            try:
                reply = await client.await_turn(http_session, session_id, baseline, timeout=timeout)
            except DshError as exc:
                await self._send_text(chat_key, f"❌ DSH 后台任务读取失败：{exc}")
                return
            chain = await self._reply_chain(reply)
        if not chain:
            await self._send_text(chat_key, "（DSH 任务已完成，但没有可呈现的内容）")
            return
        await self._send_message(chat_key, chain)

    async def _run_headless(self, text: str) -> DshReply:
        profile = str(self._cfg("dsh_profile", "headless") or "headless").strip()
        extra_args = str(self._cfg("dsh_extra_args", "") or "").strip()
        timeout = float(self._cfg("command_timeout", 600))

        args = [self.dsh_command, "--profile", profile]
        if extra_args:
            try:
                args += shlex.split(extra_args, posix=(os.name != "nt"))
            except ValueError as exc:
                raise DshError(f"dsh_extra_args 参数格式错误：{exc}") from exc
        args.append(text)

        proc = await self._spawn(args)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            raise DshTimeout(f"DeepSeek Harness 执行超时（{timeout} 秒）")

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise DshError(err or out or f"dsh 退出码 {proc.returncode}")
        return DshReply(text=out)

    async def _spawn(self, args):
        """启动 dsh 子进程，兼容 Windows 下的 .cmd/.exe/.bat 情况。"""
        candidates = [args]
        exe = args[0]
        if os.name == "nt" and not os.path.splitext(exe)[1]:
            for ext in (".cmd", ".exe", ".bat"):
                candidates.append([exe + ext] + args[1:])
        last_err: Optional[Exception] = None
        for cmd in candidates:
            try:
                return await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (FileNotFoundError, OSError) as exc:
                last_err = exc
        if isinstance(last_err, FileNotFoundError):
            raise DshError(
                f"找不到 dsh 可执行文件（{self.dsh_command}）。请在插件配置中把 dsh_command 设置为正确的 DSH 命令行路径。"
            ) from last_err
        raise DshError(f"启动 dsh 失败：{last_err}") from last_err

    async def _run(self, event: AstrMessageEvent, text: str, prompt_mode: str = "queue") -> DshReply:
        mode = self.mode
        if mode == "headless":
            return await self._run_headless(text)
        if mode == "app":
            try:
                return await self._run_http(event, text, prompt_mode=prompt_mode, entry="app")
            except DshConnectionError as exc:
                raise DshError(f"{exc}；请确认 DeepSeek Harness 桌面应用正在运行。") from exc
        try:
            return await self._run_http(event, text, prompt_mode=prompt_mode, entry="http")
        except DshConnectionError:
            if mode != "auto":
                raise
            try:
                self._http_base_url("app")
            except DshError as exc:
                logger.info("App 入口不可用（%s），回退到 headless 模式执行", exc)
                return await self._run_headless(text)
            logger.info("Web 入口连接失败，尝试 DSH 桌面应用入口")
            try:
                return await self._run_http(event, text, prompt_mode=prompt_mode, entry="app")
            except DshConnectionError:
                logger.info("App 入口连接失败，回退到 headless 模式执行")
                return await self._run_headless(text)

    async def _download_image(self, source: str) -> str | None:
        """Return a local image path suitable for AstrBot's ``Image`` component."""
        source = source.strip()
        if not source:
            return None
        if source.startswith("data:image/"):
            try:
                header, encoded = source.split(",", 1)
                mime = header.split(";", 1)[0].split(":", 1)[1]
                blob = base64.b64decode(encoded, validate=True)
                return self._write_temp_image(blob, mime)
            except (ValueError, IndexError, binascii.Error) as exc:
                logger.warning(f"DSH data URL 图片解码失败：{exc}")
                return None

        parsed = urlparse(source)
        if parsed.scheme == "file":
            path = Path(unquote(parsed.path.lstrip("/" if os.name == "nt" else "")))
            return str(path) if path.is_file() else None
        if not parsed.scheme:
            path = Path(source)
            return str(path) if path.is_file() else None
        if parsed.scheme not in {"http", "https"} or not self._cfg("allow_remote_images", True):
            return None

        maximum = max(1, int(self._cfg("max_image_bytes", 5_242_880)))
        timeout = float(self._cfg("image_download_timeout", 20))
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(source) as response:
                    if response.status != 200:
                        logger.warning(f"DSH 图片下载失败：HTTP {response.status} {source[:160]}")
                        return None
                    content_type = response.headers.get("content-type", "image/png").split(";", 1)[0]
                    if not content_type.startswith("image/"):
                        return None
                    blob = await response.content.read(maximum + 1)
            if len(blob) > maximum:
                logger.warning(f"DSH 图片超过 max_image_bytes，已跳过：{source[:160]}")
                return None
            return self._write_temp_image(blob, content_type)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning(f"DSH 图片下载失败：{source[:160]} {exc}")
            return None

    def _write_temp_image(self, blob: bytes, mime: str) -> str | None:
        maximum = max(1, int(self._cfg("max_image_bytes", 5_242_880)))
        if not blob or len(blob) > maximum:
            return None
        suffix = mimetypes.guess_extension(mime) or ".png"
        fd, name = tempfile.mkstemp(prefix="astrbot-dsh-", suffix=suffix)
        with os.fdopen(fd, "wb") as image_file:
            image_file.write(blob)
        path = Path(name)
        self._temp_media.add(path)
        return str(path)

    async def _render_reply_text(self, text: str) -> list:
        """Render DSH Markdown through AstrBot t2i, with a plain-text fallback."""
        mode = normalize_reply_render_mode(self._cfg("reply_render_mode", "text"))
        try:
            minimum = max(1, int(self._cfg("card_min_chars", 120)))
            maximum = max(500, int(self._cfg("card_max_chars", 6000)))
        except (TypeError, ValueError):
            minimum, maximum = 120, 6000
        if not should_render_card(mode, text, minimum):
            return [Plain(text)]

        rendered: list[Image] = []
        try:
            for card_markdown in split_markdown_for_cards(text, maximum):
                image_ref = await self.text_to_image(card_markdown)
                if not image_ref:
                    raise RuntimeError("AstrBot t2i did not return an image")
                image_ref = str(image_ref)
                rendered.append(
                    Image.fromURL(image_ref)
                    if image_ref.startswith(("http://", "https://"))
                    else Image.fromFileSystem(image_ref)
                )
        except Exception as exc:
            logger.warning("DSH reply card rendering failed; falling back to text: %s", exc)
            return [Plain(text)]
        return rendered or [Plain(text)]

    async def _reply_chain(self, reply: DshReply) -> list:
        components = []
        text = self._truncate(reply.text)
        if text:
            components.extend(await self._render_reply_text(text))
        maximum = max(0, int(self._cfg("max_images_per_reply", 4)))
        for source in reply.image_sources[:maximum]:
            image_path = await self._download_image(source)
            if image_path:
                components.append(Image(file=image_path))
        return components

    def _reply_chains(self, chain: list) -> list[list]:
        """Build ordered platform messages, splitting only overlong plain text."""
        text_parts = [part for part in chain if isinstance(part, Plain)]
        if not text_parts:
            return [chain]
        text = "\n".join(part.text for part in text_parts)
        chunks = split_text_for_message(text)
        if len(chunks) == 1:
            return [chain]

        messages = [[Plain(chunk)] for chunk in chunks]
        images = [part for part in chain if isinstance(part, Image)]
        if images:
            messages.append(images)
        return messages

    # ---------------- 指令处理 ----------------

    @filter.command("dsh", alias={"ds"})
    async def dsh(self, event: AstrMessageEvent):
        """调用 DeepSeek Harness 执行指令。例如：/dsh 帮我写一段代码"""
        text = _command_remainder(event)
        if not text:
            yield event.plain_result(HELP_TEXT)
            return

        lowered = text.lower().strip()
        if lowered in ("help", "帮助", "usage", "-h", "--help", "?", "？"):
            yield event.plain_result(HELP_TEXT)
            return
        if lowered in ("approve", "allow", "允许", "批准"):
            yield event.plain_result(await self._resolve_approval(await self._chat_key(event), "allowed-once"))
            return
        if lowered in ("deny", "reject", "拒绝"):
            yield event.plain_result(await self._resolve_approval(await self._chat_key(event), "rejected"))
            return
        if lowered in ("approvals", "pending", "待授权", "授权"):
            yield event.plain_result(self._approvals_text(await self._chat_key(event)))
            return
        if lowered in ("status", "状态", "ping"):
            yield event.plain_result(await self._status_text())
            return
        if lowered in ("reset", "new", "重置", "清空"):
            yield event.plain_result(await self._reset_chat(event))
            return
        if lowered in ("setup", "wizard", "向导", "会话配置"):
            async for result in self._setup_session(event):
                yield result
            return
        if lowered in ("config", "配置") or lowered.startswith(("config ", "配置 ")):
            yield event.plain_result(await self._session_config_command(event, text))
            return
        if lowered in ("settings", "设置"):
            yield event.plain_result(await self._dsh_settings_command(event, "settings"))
            return
        if lowered.startswith(("setting ", "设置 ")):
            yield event.plain_result(await self._dsh_settings_command(event, text))
            return
        if lowered in ("sessions", "session", "会话", "列表"):
            yield event.plain_result(await self._sessions_text())
            return
        if lowered.startswith("session "):
            session_action = lowered.split(None, 2)[1] if len(lowered.split(None, 2)) > 1 else ""
            if session_action in {"delete", "del", "remove", "删除"}:
                async for result in self._delete_session(event, text):
                    yield result
                return
            yield event.plain_result(await self._session_command(event, text))
            return
        if lowered.startswith(("history", "历史")):
            yield event.plain_result(await self._history_text(event, text))
            return
        if lowered in ("stop", "cancel", "停止", "取消"):
            yield event.plain_result(await self._cancel(event))
            return
        if lowered.startswith(("model", "模型")):
            yield event.plain_result(await self._model_command(event, text))
            return
        if lowered.startswith(("effort", "推理")):
            yield event.plain_result(await self._effort_command(event, text))
            return
        if lowered.startswith(("permission", "权限")):
            yield event.plain_result(await self._permission_command(event, text))
            return
        if lowered.startswith(("preset", "预设")):
            yield event.plain_result(await self._preset_command(event, text))
            return
        if lowered in ("providers", "provider", "服务商"):
            yield event.plain_result(await self._providers_text())
            return
        if lowered in ("global-models", "catalog", "模型目录"):
            yield event.plain_result(await self._global_models_text())
            return
        if lowered.startswith(("skills", "skill", "技能")):
            yield event.plain_result(await self._skills_text(event))
            return
        if lowered.startswith(("subagents", "subagent", "子代理")):
            yield event.plain_result(await self._subagents_command(event, text))
            return
        if lowered.startswith(("workspaces", "workspace", "工作区")):
            yield event.plain_result(await self._workspaces_command(event, text))
            return
        if lowered.startswith(("goal", "目标")):
            yield event.plain_result(await self._goal_command(event, text))
            return
        if lowered.startswith(("queue", "队列")):
            yield event.plain_result(await self._queue_command(event, text))
            return

        prompt_mode = "queue"
        if lowered.startswith(("steer ", "插入 ")):
            prompt_mode = "steer"
            text = text.split(None, 1)[1].strip()
            if not text:
                yield event.plain_result("请在 /dsh steer 后提供要插入的指令。")
                return

        yield event.plain_result("🤖 已向 DeepSeek Harness 发送指令，正在执行，请稍候…")
        try:
            reply = await self._run(event, text, prompt_mode=prompt_mode)
        except DshTimeout as exc:
            yield event.plain_result(f"⏱️ {exc}")
            return
        except DshError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception as exc:
            logger.exception("DeepSeek Harness 桥接出现未预期错误")
            yield event.plain_result(f"❌ 插件内部错误：{exc}")
            return

        chain = await self._reply_chain(reply)
        if not chain:
            yield event.plain_result("（DeepSeek Harness 没有返回可呈现的内容）")
            return
        # Send completed text as ordered chains; only platform-overlong text
        # is split, so DSH deltas never become separate QQ messages.
        for message_chain in self._reply_chains(chain):
            yield event.chain_result(message_chain)

    async def _status_text(self) -> str:
        lines = ["【DeepSeek Harness 桥接状态】", f"模式：{self.mode}"]
        if self.mode in ("http", "app", "auto"):
            try:
                entry = self._resolve_entry()
                base_url = self._http_base_url(entry)
                client = self._make_http_client(entry)
                if entry == "app":
                    configured = str(self._cfg("app_base_url", "") or "").strip()
                    lines.append(f"App 入口：{base_url}" + ("" if configured else "（自动发现）"))
                else:
                    lines.append(f"Web 入口：{base_url}")
                async with aiohttp.ClientSession() as http_session:
                    info = await client.describe(http_session)
                lines.append(
                    f"✅ HTTP 已连接：{info.get('provider', '?')} / {info.get('model', '?')} "
                    f"(version {info.get('version', '?')})"
                )
                lines.append(f"工作目录：{info.get('cwd', '?')}")
                connected = bool(self._bridge and self._bridge.connected)
                lines.append(
                    f"事件桥：{'已连接' if connected else '未连接'}；"
                    f"待授权请求：{len(self._pending_approvals)}"
                )
            except DshError as exc:
                lines.append(f"❌ HTTP 连接失败：{exc}")
        if self.mode in ("headless", "auto"):
            profile = str(self._cfg("dsh_profile", "headless") or "headless").strip()
            lines.append(f"命令行：{self.dsh_command} --profile {profile}")
        return "\n".join(lines)

    async def _require_http(self) -> None:
        if self.mode == "headless":
            raise DshError("该操作需要 HTTP 连接。请将 mode 设置为 http、app 或 auto。")

    async def _active_http_session(self, event: AstrMessageEvent):
        await self._require_http()
        client = self._make_http_client()
        http_session = aiohttp.ClientSession()
        try:
            session_id = await self._session_for_chat(event, client, http_session)
            return client, http_session, session_id
        except Exception:
            await http_session.close()
            raise

    async def _sessions_text(self) -> str:
        try:
            await self._require_http()
            client = self._make_http_client()
            async with aiohttp.ClientSession() as http_session:
                sessions = await client.list_sessions(http_session)
        except DshError as exc:
            return f"❌ {exc}"
        if not sessions:
            return "DSH 当前没有会话。"
        lines = ["【DSH 会话】"]
        for index, row in enumerate(sessions[:20], 1):
            if not isinstance(row, dict):
                continue
            values = projection_values(row)
            goal = values.get("goal") or {}
            goal_phase = ((goal.get("goal") or {}).get("phase")) if isinstance(goal, dict) else None
            lines.append(
                f"{index}. {session_label(row)}\n"
                f"   cwd={row.get('cwd', '?')} preset={values.get('agentPreset', '?')} goal={goal_phase or '-'}"
            )
        return "\n".join(lines)

    async def _history_text(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split()
        limit = 30
        if len(parts) > 1:
            try:
                limit = min(max(int(parts[1]), 1), 200)
            except ValueError:
                return "history 条数需要是 1 到 200 的整数。"
        try:
            client, http_session, session_id = await self._active_http_session(event)
            try:
                events = await client.history(http_session, session_id, max_messages=limit)
            finally:
                await http_session.close()
        except DshError as exc:
            return f"❌ {exc}"
        reply = merge_replies(events)
        return self._truncate(reply.text) or "当前历史中没有助手文本。"

    async def _cancel(self, event: AstrMessageEvent) -> str:
        try:
            client, http_session, session_id = await self._active_http_session(event)
            try:
                accepted = await client.cancel(http_session, session_id)
            finally:
                await http_session.close()
            return "已请求取消当前 DSH 任务。" if accepted else "DSH 未接受取消请求。"
        except DshError as exc:
            return f"❌ {exc}"

    async def _models(self, event: AstrMessageEvent) -> tuple[dict, list[dict]]:
        client, http_session, session_id = await self._active_http_session(event)
        try:
            value = await client.models(http_session, session_id)
            return value, model_rows(value)
        finally:
            await http_session.close()

    async def _model_command(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split()
        try:
            if len(parts) == 1:
                value, rows = await self._models(event)
                current = value.get("current") or {}
                lines = [f"当前模型：{current.get('provider', '?')}/{current.get('model', '?')}  推理：{current.get('reasoningEffort', '默认')}", "可用模型："]
                lines.extend(f"- {row['provider']}/{row['model']} ({', '.join(row['efforts']) or '无推理档位'})" for row in rows[:40])
                return "\n".join(lines)
            if "/" not in parts[1]:
                return "模型格式：/dsh model <provider/model> [reasoning_effort]"
            provider, model = parts[1].split("/", 1)
            effort = parts[2] if len(parts) > 2 else ""
            client, http_session, session_id = await self._active_http_session(event)
            try:
                selected = await client.select_model(http_session, session_id, provider, model, effort)
            finally:
                await http_session.close()
            item = selected.get("selected") or selected
            await self._state.update_options(
                self,
                await self._chat_key(event),
                {
                    "provider": str(item.get("provider", provider)),
                    "model": str(item.get("model", model)),
                    "reasoning_effort": str(item.get("reasoningEffort", effort) or ""),
                },
            )
            return f"已切换模型：{item.get('provider', provider)}/{item.get('model', model)}，推理：{item.get('reasoningEffort', effort or '默认')}"
        except DshError as exc:
            return f"❌ {exc}"

    async def _effort_command(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split()
        if len(parts) != 2:
            return "用法：/dsh effort <推理强度>。可先用 /dsh model 查询当前模型支持的档位。"
        try:
            value, _ = await self._models(event)
            current = value.get("current") or {}
            return await self._model_command(event, f"model {current.get('provider', '')}/{current.get('model', '')} {parts[1]}")
        except DshError as exc:
            return f"❌ {exc}"

    async def _permission_command(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            try:
                await self._require_http()
                async with aiohttp.ClientSession() as http_session:
                    presets = await self._make_http_client().permission_presets(http_session)
                return "可用 DSH 权限预设：" + (", ".join(presets) if presets else "DSH 未报告预设，请直接输入名称")
            except DshError as exc:
                return f"❌ {exc}"
        try:
            client, http_session, session_id = await self._active_http_session(event)
            try:
                result = await client.execute_command(
                    http_session,
                    session_id,
                    f"/permission {parts[1].strip()}",
                )
            finally:
                await http_session.close()
            await self._state.update_options(
                self,
                await self._chat_key(event),
                {"permission_preset": parts[1].strip()},
            )
            command_result = result.get("result") or {}
            command_text = str(command_result.get("text") or "").strip()
            suffix = f"（{command_text}）" if command_text else ""
            return f"已应用 DSH 权限预设：{parts[1].strip()}{suffix}"
        except DshError as exc:
            return f"❌ {exc}"

    async def _preset_command(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "list"
        try:
            await self._require_http()
            client = self._make_http_client()
            if action in {"list", "ls"}:
                async with aiohttp.ClientSession() as http_session:
                    value = await client.presets(http_session)
                return "\n".join(["【DSH Agent Presets]"] + [
                    f"- {row.get('id')} default={row.get('isDefault')} order={row.get('order')}"
                    for row in value.get("presets") or []
                ])
            if action == "read":
                if len(parts) < 3:
                    return "用法：/dsh preset read <名称>"
                async with aiohttp.ClientSession() as http_session:
                    value = await client.read_preset(http_session, parts[2].strip())
                return f"【{value.get('agentPreset')}】\n{self._truncate(value.get('content', ''))}"
            if action == "select":
                if len(parts) < 3:
                    return "用法：/dsh preset select <名称>"
                selected_preset = parts[2].strip()
                client, http_session, session_id = await self._active_http_session(event)
                try:
                    value = await client.select_preset(http_session, session_id, selected_preset)
                finally:
                    await http_session.close()
                applied_preset = str(value.get("agentPreset") or selected_preset)
                await self._state.update_options(
                    self,
                    await self._chat_key(event),
                    {"agent_preset": applied_preset},
                )
                return f"当前会话已切换 Agent Preset：{applied_preset}"
            # The concise form stores the preset for this chat's next session.
            chat_key = await self._chat_key(event)
            await self._state.update_options(
                self,
                chat_key,
                {"agent_preset": command.split(maxsplit=1)[1].strip()},
            )
            return f"已设置当前聊天的新会话 Agent Preset：{command.split(maxsplit=1)[1].strip()}。执行 /dsh reset 后生效。"
        except DshError as exc:
            return f"❌ {exc}"

    async def _settings_text(self, event: AstrMessageEvent) -> str:
        options = await self._load_session_options(event)
        return "\n".join([
            "【DSH Connector】",
            f"mode={self.mode}",
            f"web 入口：{self._cfg('http_base_url', 'http://127.0.0.1:3080')}",
            f"app 入口：{self._app_entry_description()}",
            f"图片：max_images_per_reply={self._cfg('max_images_per_reply', 4)}，max_image_bytes={self._cfg('max_image_bytes', 5242880)}",
            "",
            format_session_options(options),
        ])

    async def _session_config_command(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split(maxsplit=3)
        options = await self._load_session_options(event)
        if len(parts) == 1:
            return format_session_options(options)
        action = parts[1].lower()
        if action in {"reset", "clear-all"}:
            cleared = await self._state.clear_options(self, await self._chat_key(event))
            return "已重置当前聊天的新会话选项。\n" + format_session_options(cleared)
        if action not in {"set", "clear", "unset"} or len(parts) < 3:
            return "用法：/dsh config；/dsh config set <workspace|cwd|preset|model|effort|permission|timezone> <值>；/dsh config clear <字段>；/dsh config reset"
        field = resolve_option_field(parts[2])
        if not field:
            return f"未知会话选项：{parts[2]}"
        if action == "set" and len(parts) < 4:
            return f"请提供 {parts[2]} 的值。"
        value = "" if action in {"clear", "unset"} else parts[3].strip()
        changes: dict[str, str]
        if field == "model":
            if value and "/" not in value:
                return "模型格式：provider/model"
            provider, model = value.split("/", 1) if value else ("", "")
            changes = {"provider": provider, "model": model}
            if not value:
                changes["reasoning_effort"] = ""
        elif field == "client_time_zone":
            normalized = normalize_client_time_zone(value)
            if value and normalized == "UTC" and value.upper() != "UTC":
                return "时区需要是 UTC 或有效 IANA Area/Location 名称，例如 Asia/Shanghai。"
            changes = {field: normalized}
        elif field == "workspace_id":
            changes = {field: value, "working_directory": ""} if value else {field: ""}
        elif field == "working_directory":
            changes = {field: value, "workspace_id": ""} if value else {field: ""}
        else:
            changes = {field: value}
        updated = await self._state.update_options(self, await self._chat_key(event), changes)
        return "已更新当前聊天的新会话选项。执行 /dsh session new 应用。\n" + format_session_options(updated)

    async def _create_session_from_options(
        self, event: AstrMessageEvent, options: dict[str, str]
    ) -> str:
        await self._require_http()
        client = self._make_http_client()
        normalized = normalize_session_options(options)
        async with aiohttp.ClientSession() as http_session:
            cwd = normalized["working_directory"]
            workspace_id = normalized.get("workspace_id", "")
            if workspace_id:
                cwd = ""
            elif not cwd:
                cwd = str((await client.describe(http_session)).get("cwd") or "")
            session_id = await client.create_session(
                http_session,
                cwd=cwd,
                workspace_id=workspace_id,
                agent_preset=normalized["agent_preset"],
            )
            await self._configure_session_options(
                client,
                http_session,
                session_id,
                normalized,
            )
        await self._save_session_id(await self._chat_key(event), session_id)
        return session_id

    async def _setup_session(self, event: AstrMessageEvent):
        try:
            await self._require_http()
            client = self._make_http_client()
            async with aiohttp.ClientSession() as http_session:
                host = await client.describe(http_session)
                presets_value = await client.presets(http_session)
                models_value = await client.global_models(http_session)
                workspace_value = await client.workspaces(http_session)
                permission_presets = await client.permission_presets(http_session)
            wizard = SessionSetupWizard(
                str(host.get("cwd") or ""),
                presets_value.get("presets") or [],
                model_rows(models_value),
                await self._load_session_options(event),
                workspace_value.get("items") or [],
                permission_presets,
            )
        except DshError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        yield event.plain_result(wizard.initial_prompt())

        @session_waiter(timeout=120, record_history_chains=False)
        async def setup_waiter(controller: SessionController, incoming: AstrMessageEvent):
            result = wizard.process(incoming.message_str)
            if result.cancelled:
                await incoming.send(incoming.plain_result(result.prompt))
                controller.stop()
                return
            if result.confirmed:
                await incoming.send(incoming.plain_result(result.prompt))
                try:
                    await self._save_session_options(incoming, wizard.options)
                    session_id = await self._create_session_from_options(incoming, wizard.options)
                    await incoming.send(incoming.plain_result(f"已创建并绑定 DSH 会话：{session_id}"))
                except DshError as exc:
                    await incoming.send(incoming.plain_result(f"创建 DSH 会话失败：{exc}"))
                controller.stop()
                return
            await incoming.send(incoming.plain_result(result.prompt))
            controller.keep(timeout=120, reset_timeout=True)

        try:
            await setup_waiter(event)
        except TimeoutError:
            yield event.plain_result("DSH 会话配置超时，已退出。")
        finally:
            event.stop_event()

    async def _dsh_settings_command(self, event: AstrMessageEvent, command: str) -> str:
        """Expose the DSH settings document without maintaining a stale local schema."""
        parts = command.split(maxsplit=4)
        try:
            await self._require_http()
            client = self._make_http_client()
            async with aiohttp.ClientSession() as http_session:
                description = await client.settings(http_session)
                if len(parts) <= 1 or parts[1].lower() in {"list", "ls"}:
                    return format_namespaces(description)
                if parts[1].lower() not in {"get", "schema"} and parts[1] != "读取":
                    if parts[1].lower() not in {"set", "unset"}:
                        return "用法：/dsh setting get <命名空间> [路径]；schema <命名空间>；set <命名空间> <路径> <JSON值>；unset <命名空间> <路径>"
                action = parts[1].lower()
                if action == "schema":
                    if len(parts) < 3:
                        return "用法：/dsh setting schema <命名空间>"
                    namespace = namespace_map(description).get(parts[2])
                    if not namespace:
                        return f"未找到 DSH 设置命名空间：{parts[2]}"
                    return f"【{parts[2]} Schema】\n{compact_json(namespace.get('schema'), 8000)}"
                if action == "get" or action == "读取":
                    if len(parts) < 3:
                        return "用法：/dsh setting get <命名空间> [路径]"
                    namespace = namespace_map(description).get(parts[2])
                    if not namespace:
                        return f"未找到 DSH 设置命名空间：{parts[2]}"
                    if len(parts) < 4:
                        return f"【{parts[2]}】applies={namespace.get('applies')} rev={namespace.get('revision')}\n{compact_json(namespace.get('value'), 6000)}"
                    try:
                        value = read_path(namespace.get("value"), dotted_path(parts[3]))
                    except (KeyError, ValueError):
                        return f"{parts[2]} 中没有路径：{parts[3]}"
                    return f"【{parts[2]}.{parts[3]}】\n{compact_json(value, 6000)}"

                if len(parts) < 4:
                    return f"用法：/dsh setting {action} <命名空间> <路径>" + (" <JSON值>" if action == "set" else "")
                namespace = namespace_map(description).get(parts[2])
                if not namespace:
                    return f"未找到 DSH 设置命名空间：{parts[2]}"
                path = dotted_path(parts[3])
                if action == "set":
                    if len(parts) < 5:
                        return "用法：/dsh setting set <命名空间> <路径> <JSON值>"
                    operation = {"op": "set", "path": path, "value": parse_json_value(parts[4])}
                else:
                    operation = {"op": "unset", "path": path}
                result = await client.mutate_settings(
                    http_session,
                    parts[2],
                    [operation],
                    expected_revision=namespace.get("revision"),
                )
            return f"已更新 {parts[2]}.{parts[3]}；revision={result.get('revision')} applies={result.get('applies')}"
        except (DshError, ValueError) as exc:
            return f"❌ {exc}"

    async def _session_command(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split(maxsplit=2)
        if len(parts) < 2:
            return "用法：/dsh session <list|switch|new|rename|fork|search|delete>"
        action = parts[1].lower()
        try:
            await self._require_http()
            client = self._make_http_client()
            if action in {"list", "ls"}:
                return await self._sessions_text()
            if action == "search":
                if len(parts) < 3 or not parts[2].strip():
                    return "用法：/dsh session search <关键词>"
                async with aiohttp.ClientSession() as http_session:
                    result = await client.search_sessions(http_session, parts[2].strip())
                rows = result.get("items") or []
                return "\n".join(["【DSH 会话搜索】"] + [f"- {row.get('sessionId')} {row.get('snippet', '')}" for row in rows]) or "未找到匹配会话。"
            chat_key = await self._chat_key(event)
            if action == "new":
                options = await self._load_session_options(event)
                session_id = await self._create_session_from_options(event, options)
                return f"已按当前聊天选项创建并绑定 DSH 会话：{session_id}"
            async with aiohttp.ClientSession() as http_session:
                if action == "switch":
                    if len(parts) < 3:
                        return "用法：/dsh session switch <sessionId>"
                    wanted = parts[2].strip()
                    sessions = await client.list_sessions(http_session)
                    session_id = next((str(row.get("sessionId")) for row in sessions if str(row.get("sessionId")) == wanted), "")
                    if not session_id:
                        return f"DSH 中没有会话：{wanted}"
                    await self._save_session_id(chat_key, session_id)
                    return f"当前聊天已绑定 DSH 会话：{session_id}"
                session_id = await self._session_for_chat(event, client, http_session)
                if action == "rename":
                    if len(parts) < 3:
                        return "用法：/dsh session rename <标题>"
                    result = await client.rename_session(http_session, session_id, parts[2].strip())
                    return f"已设置会话标题：{result.get('title')}"
                if action == "fork":
                    at_seq = None
                    if len(parts) == 3 and parts[2].strip():
                        at_seq = int(parts[2].strip())
                    child = await client.fork_session(http_session, session_id, at_seq)
                    await self._save_session_id(chat_key, child)
                    return f"已从当前会话分叉并绑定：{child}"
            return "用法：/dsh session <list|switch|new|rename|fork|search|delete>"
        except (DshError, ValueError) as exc:
            return f"❌ {exc}"

    async def _delete_session(self, event: AstrMessageEvent, command: str):
        """Archive a DSH session after the same explicit confirmation HAPI uses."""
        parts = command.split(maxsplit=2)
        target = parts[2].strip() if len(parts) > 2 else ""
        try:
            await self._require_http()
            client = self._make_http_client()
            chat_key = await self._chat_key(event)
            bound_session_id = await self._load_session_id(chat_key)
            async with aiohttp.ClientSession() as http_session:
                sessions = await client.list_sessions(http_session)
            wanted = target or bound_session_id or ""
            if not wanted:
                yield event.plain_result("当前聊天还没有绑定 DSH 会话。用法：/dsh session delete [sessionId]")
                return
            exact = [str(row.get("sessionId")) for row in sessions if str(row.get("sessionId")) == wanted]
            prefix = [str(row.get("sessionId")) for row in sessions if str(row.get("sessionId", "")).startswith(wanted)]
            matches = exact or prefix
            if not matches:
                yield event.plain_result(f"DSH 中没有会话：{wanted}")
                return
            if len(matches) > 1:
                yield event.plain_result(f"会话 ID 前缀不唯一：{wanted}，请提供完整 ID。")
                return
            session_id = matches[0]
            current = next((row for row in sessions if str(row.get("sessionId")) == session_id), {})
            is_running = bool(current.get("running"))
        except DshError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        state = "该会话正在运行，确认后会先取消当前任务，再从 DSH 会话列表移除。" if is_running else "该会话会从 DSH 会话列表移除。"
        yield event.plain_result(
            f"{state}\n目标：{session_id}\nDSH 会保留历史日志以支持归档恢复。\n输入 delete 确认，其他任意内容取消："
        )

        @session_waiter(timeout=30, record_history_chains=False)
        async def delete_waiter(controller: SessionController, incoming: AstrMessageEvent):
            reply = (incoming.message_str or "").strip()
            if not reply:
                controller.keep(timeout=30, reset_timeout=True)
                return
            if reply != "delete":
                await incoming.send(incoming.plain_result("已取消"))
                controller.stop()
                return
            try:
                async with aiohttp.ClientSession() as http_session:
                    if is_running:
                        await client.cancel(http_session, session_id)
                    result = await client.archive_session(http_session, session_id)
                if session_id == bound_session_id:
                    await self._clear_session_id(chat_key)
                archived = result.get("archivedSessionIds") or []
                await incoming.send(incoming.plain_result(
                    f"DSH 会话已删除：{session_id}\n归档列表已包含该会话：{session_id in archived}"
                ))
            except DshError as exc:
                await incoming.send(incoming.plain_result(f"❌ DSH 删除失败：{exc}"))
            controller.stop()

        try:
            await delete_waiter(event)
        except TimeoutError:
            yield event.plain_result("删除确认超时，已取消。")
        finally:
            event.stop_event()

    async def _providers_text(self) -> str:
        try:
            await self._require_http()
            async with aiohttp.ClientSession() as http_session:
                value = await self._make_http_client().providers(http_session)
            providers = value.get("providers") or []
            lines = ["【DSH LLM 服务商】"]
            lines.extend(
                f"- {row.get('provider')} ({row.get('displayName')}) "
                f"declared={row.get('declared', True)} "
                f"setting={row.get('settingsNs')}.{'.'.join(row.get('settingsPath') or [])}"
                for row in providers
            )
            return "\n".join(lines)
        except DshError as exc:
            return f"❌ {exc}"

    async def _global_models_text(self) -> str:
        try:
            await self._require_http()
            async with aiohttp.ClientSession() as http_session:
                value = await self._make_http_client().global_models(http_session)
            return "\n".join(["【DSH 全局模型目录】"] + [
                f"- {group.get('id')}/{model.get('id')} {model.get('name', '')}"
                for group in value.get("groups") or []
                for model in group.get("models") or []
            ])
        except DshError as exc:
            return f"❌ {exc}"

    async def _skills_text(self, event: AstrMessageEvent) -> str:
        try:
            client, http_session, session_id = await self._active_http_session(event)
            try:
                value = await client.skills(http_session, session_id)
            finally:
                await http_session.close()
            return "\n".join(["【DSH Skills]"] + [
                f"- {row.get('name')}: {row.get('description')} model={row.get('modelInvocable')}"
                for row in value.get("skills") or []
            ])
        except DshError as exc:
            return f"❌ {exc}"

    async def _subagents_command(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split(maxsplit=2)
        try:
            client, http_session, session_id = await self._active_http_session(event)
            try:
                if len(parts) > 2 and parts[1].lower() == "interrupt":
                    result = await client.interrupt_subagent(http_session, session_id, parts[2].strip())
                    return "已请求中断子代理。" if result.get("accepted") else "DSH 未接受中断请求。"
                value = await client.subagents(http_session, session_id)
            finally:
                await http_session.close()
            return "\n".join(["【DSH 子代理】"] + [
                f"- {row.get('id')} {row.get('kind')} {row.get('mode', '')} {row.get('activity', row.get('reason', ''))} {row.get('label', '')}"
                for row in value.get("entries") or []
            ])
        except DshError as exc:
            return f"❌ {exc}"

    async def _workspaces_command(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "list"
        try:
            await self._require_http()
            client = self._make_http_client()
            async with aiohttp.ClientSession() as http_session:
                if action == "create" and len(parts) > 2:
                    value = await client.create_workspace(http_session, parts[2].strip())
                    workspace = value.get("workspace") or {}
                    return f"工作区：{workspace.get('workspaceId')} {workspace.get('title')}"
                if action == "rename" and len(parts) > 2:
                    workspace_id, _, title = parts[2].strip().partition(" ")
                    if not workspace_id or not title:
                        return "用法：/dsh workspace rename <workspaceId> <标题>"
                    value = await client.rename_workspace(http_session, workspace_id, title)
                    return f"已更新工作区：{(value.get('workspace') or {}).get('title')}"
                value = await client.workspaces(http_session)
            return "\n".join(["【DSH 工作区】"] + [
                f"- {row.get('workspaceId')} {row.get('title')} | {row.get('path')} | sessions={len(row.get('sessionIds') or [])}"
                for row in value.get("items") or []
            ])
        except DshError as exc:
            return f"❌ {exc}"

    async def _goal_command(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "show"
        try:
            client, http_session, session_id = await self._active_http_session(event)
            try:
                history = await client.history_value(http_session, session_id, max_messages=1)
                goal = current_goal(history)
                if action == "show":
                    return f"【DSH Goal】\n{compact_json(goal, 4000) if goal else '当前会话没有 goal。'}"
                if action == "create":
                    if len(parts) < 3:
                        return "用法：/dsh goal create <目标>"
                    result = await client.goal_action(http_session, "create", session_id, objective=parts[2].strip())
                elif action in {"pause", "resume", "complete", "clear"}:
                    result = await client.goal_action(http_session, action, session_id, goal=goal)
                else:
                    return "用法：/dsh goal [create <目标>|pause|resume|complete|clear]"
            finally:
                await http_session.close()
            return f"DSH goal {action}：{compact_json(result, 2000)}"
        except DshError as exc:
            return f"❌ {exc}"

    async def _queue_command(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split(maxsplit=3)
        if len(parts) < 3:
            return "用法：/dsh queue <remove|steer|edit> <消息ID> [文本]"
        action = parts[1].lower()
        if action not in {"remove", "steer", "edit"}:
            return "队列操作仅支持 remove、steer 或 edit。"
        if action == "edit" and len(parts) < 4:
            return "用法：/dsh queue edit <消息ID> <文本>"
        try:
            client, http_session, session_id = await self._active_http_session(event)
            try:
                result = await client.update_queue(http_session, session_id, parts[2], action, parts[3] if len(parts) > 3 else "")
            finally:
                await http_session.close()
            return "DSH 已接受队列操作。" if result.get("accepted") else "DSH 未接受队列操作。"
        except DshError as exc:
            return f"❌ {exc}"

    # ---------------- LLM 工具 ----------------

    def _llm_tools_available(self) -> bool:
        return bool(self._cfg("enable_llm_tools", False))

    def _llm_mutations_available(self) -> bool:
        return self._llm_tools_available() and bool(self._cfg("enable_llm_mutation_tools", False))

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request: ProviderRequest):
        """Hide DSH tools completely until the operator enables them."""
        if not getattr(request, "func_tool", None):
            return
        tool_names = {
            "dsh_connector_get_status",
            "dsh_connector_list_sessions",
            "dsh_connector_get_models",
            "dsh_connector_send_prompt",
            "dsh_connector_create_session",
            "dsh_connector_set_model",
            "dsh_connector_set_permission",
            "dsh_connector_stop",
            "dsh_connector_get_config",
            "dsh_connector_search_sessions",
            "dsh_connector_get_dsh_settings",
            "dsh_connector_get_providers",
            "dsh_connector_list_skills",
            "dsh_connector_list_workspaces",
            "dsh_connector_set_dsh_setting",
            "dsh_connector_list_presets",
            "dsh_connector_list_subagents",
            "dsh_connector_get_goal",
            "dsh_connector_manage_session",
        }
        if not self._llm_tools_available():
            for tool_name in tool_names:
                request.func_tool.remove_tool(tool_name)
            return
        if not self._llm_mutations_available():
            request.func_tool.remove_tool("dsh_connector_set_dsh_setting")

    @filter.llm_tool(name="dsh_connector_get_status")
    async def tool_get_status(self, event: AstrMessageEvent):
        """读取 DeepSeek Harness 主机状态、版本、当前模型与工作目录。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._status_text()

    @filter.llm_tool(name="dsh_connector_list_sessions")
    async def tool_list_sessions(self, event: AstrMessageEvent):
        """列出 DSH 已有会话及其运行状态、工作目录、Agent 预设。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._sessions_text()

    @filter.llm_tool(name="dsh_connector_get_models")
    async def tool_get_models(self, event: AstrMessageEvent):
        """查询当前 DSH 会话模型、服务商和可用推理强度。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._model_command(event, "model")

    @filter.llm_tool(name="dsh_connector_send_prompt")
    async def tool_send_prompt(self, event: AstrMessageEvent, prompt: str, steer: bool = False):
        """向当前聊天绑定的 DSH 会话发送任务。

        Args:
            prompt(string): 要交给 DSH 的完整任务。
            steer(boolean): True 时插入正在执行的任务；默认按队列提交。
        """
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        try:
            reply = await self._run(event, prompt, prompt_mode="steer" if steer else "queue")
            result = self._truncate(reply.text) or "（DSH 没有返回文本）"
            if reply.image_sources:
                result += "\n图片：" + "\n".join(reply.image_sources[:int(self._cfg("max_images_per_reply", 4))])
            yield result
        except DshError as exc:
            yield f"DSH 调用失败：{exc}"

    @filter.llm_tool(name="dsh_connector_create_session")
    async def tool_create_session(
        self,
        event: AstrMessageEvent,
        working_directory: str = "",
        agent_preset: str = "",
        workspace_id: str = "",
    ):
        """创建并绑定一个新的 DSH 会话。

        Args:
            working_directory(string): DSH 工作目录；留空沿用当前聊天会话选项。
            agent_preset(string): DSH Agent 预设；留空沿用当前聊天会话选项。
        """
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        try:
            await self._require_http()
            options = await self._load_session_options(event)
            if working_directory.strip():
                options["working_directory"] = working_directory.strip()
            if agent_preset.strip():
                options["agent_preset"] = agent_preset.strip()
            if workspace_id.strip():
                options["workspace_id"] = workspace_id.strip()
                options["working_directory"] = ""
            session_id = await self._create_session_from_options(event, options)
            yield f"已创建并绑定 DSH 会话：{session_id}"
        except DshError as exc:
            yield f"创建 DSH 会话失败：{exc}"

    @filter.llm_tool(name="dsh_connector_set_model")
    async def tool_set_model(self, event: AstrMessageEvent, provider: str, model: str, reasoning_effort: str = ""):
        """切换当前 DSH 会话的 provider、模型和可选推理强度。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._model_command(event, f"model {provider}/{model} {reasoning_effort}".strip())

    @filter.llm_tool(name="dsh_connector_set_permission")
    async def tool_set_permission(self, event: AstrMessageEvent, preset: str):
        """设置当前 DSH 会话权限预设；可用值由 DSH 主机提供。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._permission_command(event, f"permission {preset}")

    @filter.llm_tool(name="dsh_connector_stop")
    async def tool_stop(self, event: AstrMessageEvent):
        """取消当前聊天绑定的 DSH 会话正在执行的任务。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._cancel(event)

    @filter.llm_tool(name="dsh_connector_get_config")
    async def tool_get_config(self, event: AstrMessageEvent):
        """读取 DSH Connector 连接状态和当前聊天的新会话选项。"""
        yield await self._settings_text(event)

    @filter.llm_tool(name="dsh_connector_search_sessions")
    async def tool_search_sessions(self, event: AstrMessageEvent, query: str):
        """在 DSH 全部历史会话中检索标题和消息片段。

        Args:
            query(string): 要搜索的关键词。
        """
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。"
            return
        yield await self._session_command(event, f"session search {query}")

    @filter.llm_tool(name="dsh_connector_get_dsh_settings")
    async def tool_get_dsh_settings(self, event: AstrMessageEvent, namespace: str = "", path: str = "", include_schema: bool = False):
        """读取 DSH 已注册的配置命名空间，或读取一个命名空间中的具体路径。

        Args:
            namespace(string): DSH settings 命名空间；留空列出全部。
            path(string): 点分隔路径，例如 models；留空读取整个命名空间。
            include_schema(boolean): 为 true 时返回命名空间完整 JSON Schema，便于发现可配置项。
        """
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。"
            return
        command = "settings" if not namespace else (
            f"setting schema {namespace}" if include_schema else f"setting get {namespace}" + (f" {path}" if path else "")
        )
        yield await self._dsh_settings_command(event, command)

    @filter.llm_tool(name="dsh_connector_get_providers")
    async def tool_get_providers(self, event: AstrMessageEvent):
        """列出 DSH 当前已配置的 LLM 服务商、配置命名空间和启用状态。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。"
            return
        yield await self._providers_text()

    @filter.llm_tool(name="dsh_connector_list_skills")
    async def tool_list_skills(self, event: AstrMessageEvent):
        """列出当前 DSH 会话可用的 Skills 及模型调用资格。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。"
            return
        yield await self._skills_text(event)

    @filter.llm_tool(name="dsh_connector_list_workspaces")
    async def tool_list_workspaces(self, event: AstrMessageEvent):
        """列出 DSH 已注册的工作区及其会话数量。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。"
            return
        yield await self._workspaces_command(event, "workspaces")

    @filter.llm_tool(name="dsh_connector_set_dsh_setting")
    async def tool_set_dsh_setting(self, event: AstrMessageEvent, namespace: str, path: str, value_json: str):
        """以 DSH 原生 settings.mutate 接口更新一个设置路径。

        Args:
            namespace(string): DSH 配置命名空间。
            path(string): 点分隔配置路径。
            value_json(string): JSON 值；例如 true、42、\"value\" 或对象。
        """
        if not self._llm_mutations_available():
            yield "DSH LLM 设置写入工具当前未启用。请同时启用 enable_llm_tools 与 enable_llm_mutation_tools。"
            return
        yield await self._dsh_settings_command(event, f"setting set {namespace} {path} {value_json}")

    @filter.llm_tool(name="dsh_connector_list_presets")
    async def tool_list_presets(self, event: AstrMessageEvent):
        """列出 DSH 已加载的 Agent Presets、信任级别和默认项。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。"
            return
        yield await self._preset_command(event, "preset list")

    @filter.llm_tool(name="dsh_connector_list_subagents")
    async def tool_list_subagents(self, event: AstrMessageEvent):
        """列出当前 DSH 会话的子代理、运行状态和模式。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。"
            return
        yield await self._subagents_command(event, "subagents")

    @filter.llm_tool(name="dsh_connector_get_goal")
    async def tool_get_goal(self, event: AstrMessageEvent):
        """读取当前 DSH 会话的 Goal id、revision、目标和阶段。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。"
            return
        yield await self._goal_command(event, "goal")

    @filter.llm_tool(name="dsh_connector_manage_session")
    async def tool_manage_session(self, event: AstrMessageEvent, action: str, value: str = ""):
        """切换、重命名或分叉当前聊天绑定的 DSH 会话。

        Args:
            action(string): switch、rename、fork 或 new。
            value(string): switch 的 sessionId、rename 的标题、fork 的可选事件 seq。
        """
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。"
            return
        action = action.strip().lower()
        if action not in {"switch", "rename", "fork", "new"}:
            yield "action 需要是 switch、rename、fork 或 new。"
            return
        yield await self._session_command(event, f"session {action}" + (f" {value}" if value else ""))

    async def _reset_chat(self, event: AstrMessageEvent) -> str:
        chat_key = await self._chat_key(event)
        await self._clear_session_id(chat_key)
        return "已重置当前会话。下一次 /dsh 将开启全新的 DeepSeek Harness 会话。"

    def _truncate(self, reply: str) -> str:
        max_chars = int(self._cfg("max_reply_chars", 4000) or 0)
        if max_chars > 0 and len(reply) > max_chars:
            reply = reply[:max_chars] + "\n\n…（回复过长，已截断）"
        return reply

    async def terminate(self):
        """插件卸载/禁用时调用。"""
        for task in list(self._approval_tasks) + list(self._completion_tasks):
            task.cancel()
        if self._approval_tasks or self._completion_tasks:
            await asyncio.gather(
                *self._approval_tasks, *self._completion_tasks, return_exceptions=True
            )
        self._approval_tasks.clear()
        self._completion_tasks.clear()
        self._pending_approvals.clear()
        await self._close_event_bridge()
        if self._bridge_http is not None:
            await self._bridge_http.close()
            self._bridge_http = None
        self._active_entry = ""
        self._state.session_cache.clear()
        for path in self._temp_media:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self._temp_media.clear()


def _command_remainder(event: AstrMessageEvent) -> str:
    """取出指令名之后的所有文本。

    AstrBot 在唤醒阶段会去掉 wake_prefix（默认 ``/``），因此这里的
    ``event.message_str`` 形如 ``"dsh 帮我 写代码"``，按第一个空白切分即可。
    """
    raw = (event.message_str or "").strip()
    parts = raw.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""
