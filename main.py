# -*- coding: utf-8 -*-
"""astrbot_plugin_dsh_bridge — DeepSeek Harness 桥接插件
=====================================================

将 AstrBot 与 DeepSeek Harness（DSH，本客户端平台）连接起来，实现两个平台的桥接：
用户在聊天中发送 ``/dsh <指令>``，插件会把该指令桥接到 DSH 执行，并把回复回传到
聊天中。主要面向官方 QQ 机器人（qq_official），也兼容其它平台。

支持的连接方式（在插件配置面板中选择 ``mode``）：

* ``http``     —— 连接运行中的 DSH Web HTTP API（默认 http://127.0.0.1:3080）
* ``headless`` —— 通过 DSH 命令行 ``dsh --profile headless "<指令>"`` 一次性执行
* ``auto``     —— 优先使用 http，连接失败时自动回退到 headless

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
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:
    from .dsh_bridge_helpers import DshReply, event_seq, merge_replies, model_rows
except ImportError:  # AstrBot also supports loading a plugin main.py as a module.
    from dsh_bridge_helpers import DshReply, event_seq, merge_replies, model_rows

HELP_TEXT = """【DeepSeek Harness 桥接】
用法：
  /dsh <指令>    将指令发送给 DeepSeek Harness 执行并返回结果
  /dsh status    查看连接状态
  /dsh sessions  查看 DSH 会话列表
  /dsh model [服务商/模型] [推理强度]  查看或切换模型
  /dsh effort <值>  设置当前会话推理强度
  /dsh permission <预设>  设置 DSH 权限预设
  /dsh preset <预设>  设置下一次新会话使用的 agent 预设
  /dsh steer <指令>  插入当前执行中的任务
  /dsh stop      取消当前会话正在执行的任务
  /dsh history [条数]  查看当前会话最近记录
  /dsh reset     重置当前聊天绑定的 DSH 会话
  /dsh settings  查看插件与 DSH 运行配置

示例：
  /dsh 帮我写一段 Python 快速排序代码
  /dsh 总结一下我桌面上的 todo.txt
"""


class DshError(Exception):
    """DSH 桥接过程中的可预期错误（会把信息回传给用户）。"""


class DshConnectionError(DshError):
    """无法连接 DSH（HTTP 模式）。auto 模式据此判断是否回退到 headless。"""


class DshTimeout(DshError):
    """等待 DSH 回复超时。"""


class DshHttpClient:
    """DeepSeek Harness 本地 /api 网关的最小 HTTP 客户端。

    DSH 通过 HTTP POST ``/api/<method>`` 暴露一个 Typert RPC：
    请求体为 ``client-request`` 信封，响应体为 ``server-response`` 信封。
    本类覆盖 DSH Web 已公开的会话、模型和取消接口。
    """

    def __init__(self, base_url: str, timeout: float, poll_interval: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval

    async def _rpc(self, session: aiohttp.ClientSession, method: str, payload: dict):
        """发送一个 RPC 请求并解析 server-response 信封。"""
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
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise DshError(f"DSH 返回 HTTP {resp.status}: {body[:300]}")
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise DshConnectionError(
                f"无法连接 DeepSeek Harness（{url}）：{exc}"
            ) from exc

        if not isinstance(data, dict) or data.get("type") != "server-response":
            raise DshError(f"DSH 返回了意外的响应：{str(data)[:300]}")
        result = data.get("result") or {}
        if not result.get("ok"):
            error = result.get("error") or {}
            raise DshError(f"DSH 错误 {error.get('code')}: {error.get('message')}")
        return result.get("value")

    async def describe(self, session: aiohttp.ClientSession) -> dict:
        """host.describe：获取 DSH 版本、模型、工作目录等主机信息。"""
        return await self._rpc(session, "host.describe", {})

    async def list_sessions(self, session: aiohttp.ClientSession) -> list:
        value = await self._rpc(session, "session.list", {})
        return value.get("items", []) if isinstance(value, dict) else []

    async def create_session(
        self,
        session: aiohttp.ClientSession,
        cwd: str = "",
        agent_preset: str = "",
    ) -> str:
        """session.create：新建一个 DSH 会话，返回 sessionId。"""
        payload = {}
        if cwd:
            payload["cwd"] = cwd
        if agent_preset:
            payload["agentPreset"] = agent_preset
        value = await self._rpc(session, "session.create", payload)
        return value.get("sessionId")

    async def prompt(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        text: str,
        mode: str = "queue",
    ) -> None:
        """session.prompt：向指定会话提交一条用户消息。"""
        await self._rpc(
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
        value = await self._rpc(session, "session.cancel", {"sessionId": session_id})
        return bool((value or {}).get("accepted"))

    async def models(self, session: aiohttp.ClientSession, session_id: str) -> dict:
        value = await self._rpc(session, "session.models", {"sessionId": session_id})
        return value if isinstance(value, dict) else {}

    async def select_model(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        provider: str,
        model: str,
        reasoning_effort: str = "",
    ) -> dict:
        payload = {"sessionId": session_id, "provider": provider, "model": model}
        if reasoning_effort:
            payload["reasoningEffort"] = reasoning_effort
        value = await self._rpc(session, "session.selectModel", payload)
        return value if isinstance(value, dict) else {}

    async def history(self, session: aiohttp.ClientSession, session_id: str, max_messages: int = 100) -> list:
        """session.history：读取会话事件流（包含 assistant/message 与 turn/end）。"""
        value = await self._rpc(
            session,
            "session.history",
            {"sessionId": session_id, "maxMessages": max_messages},
        )
        return value.get("events", [])

    async def run_prompt(
        self,
        session: aiohttp.ClientSession,
        session_id: str,
        text: str,
        mode: str = "queue",
    ) -> DshReply:
        """提交指令并轮询历史记录，直到本轮 turn 结束，返回助手文本。"""
        await self._wait_idle(session, session_id)
        baseline = await self._last_seq(session, session_id)
        await self.prompt(session, session_id, text, mode=mode)
        return await self._await_reply(session, session_id, baseline)

    async def _last_seq(self, session: aiohttp.ClientSession, session_id: str) -> int:
        events = await self.history(session, session_id, max_messages=5)
        return max((event_seq(ev) for ev in events), default=0)

    async def _wait_idle(self, session: aiohttp.ClientSession, session_id: str) -> None:
        """等待会话空闲（上一轮 turn 结束），避免把新指令排到进行中的 turn 之后。"""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            events = await self.history(session, session_id, max_messages=50)
            last_start = None
            last_end = None
            for entry in events:
                ev = entry.get("event") if isinstance(entry, dict) else {}
                if ev.get("type") == "turn/start":
                    last_start = event_seq(ev)
                elif ev.get("type") == "turn/end":
                    last_end = event_seq(ev)
            if last_start is None or (last_end is not None and last_end >= last_start):
                return
            await asyncio.sleep(self.poll_interval)

    async def _await_reply(self, session, session_id, baseline) -> DshReply:
        """轮询历史记录直到新的 turn/end 出现，返回本轮富文本输出。"""
        deadline = time.monotonic() + self.timeout
        last_reply = DshReply()
        while time.monotonic() < deadline:
            events = await self.history(session, session_id, max_messages=100)
            reason = None
            for entry in events:
                ev = entry.get("event") if isinstance(entry, dict) else None
                if not ev or event_seq(ev) <= baseline:
                    continue
                etype = ev.get("type")
                if etype == "assistant/message":
                    last_reply = merge_replies(events, after_seq=baseline)
                elif etype == "turn/end":
                    reason = (ev.get("data") or {}).get("reason") or {}
            if reason is not None:
                if reason.get("kind") == "error":
                    err = reason.get("error") or {}
                    raise DshError(
                        f"DeepSeek Harness 任务执行失败：{err.get('code', 'error')} {err.get('message', '')}".strip()
                    )
                return last_reply
            await asyncio.sleep(self.poll_interval)
        if last_reply.text or last_reply.image_sources:
            return DshReply(
                text=(last_reply.text + "\n\n⚠️ 已超时，任务可能仍在后台继续运行。").strip(),
                image_sources=last_reply.image_sources,
            )
        raise DshTimeout(f"等待 DeepSeek Harness 回复超时（{self.timeout} 秒）")


def _chunk_text(text: str, size: int) -> list:
    """把长文本切成若干片，便于发送（size<=0 表示不切分）。"""
    if not size or size <= 0 or len(text) <= size:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]


class Main(Star):
    """DSH 桥接插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 会话映射缓存：chat_key -> DSH session_id（HTTP 多轮模式使用）
        self._session_cache: dict = {}
        self._temp_media: set[Path] = set()

    # ---------------- 配置便捷方法 ----------------

    def _cfg(self, key, default=None):
        value = self.config.get(key)
        return default if value is None else value

    @property
    def mode(self) -> str:
        return str(self._cfg("mode", "http")).lower()

    @property
    def dsh_command(self) -> str:
        return str(self._cfg("dsh_command", "dsh") or "dsh").strip()

    def _make_http_client(self) -> DshHttpClient:
        base_url = str(self._cfg("http_base_url", "http://127.0.0.1:3080"))
        timeout = float(self._cfg("command_timeout", 600))
        poll_interval = float(self._cfg("poll_interval", 1.0))
        return DshHttpClient(base_url, timeout, poll_interval)

    # ---------------- 会话映射（尽力持久化） ----------------

    async def _chat_key(self, event: AstrMessageEvent) -> str:
        key = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", None)
        return str(key) if key else str(event.get_sender_id())

    async def _load_session_id(self, chat_key: str):
        if chat_key in self._session_cache:
            return self._session_cache[chat_key]
        if hasattr(self, "get_kv_data"):
            try:
                sid = await self.get_kv_data(f"dsh_session:{chat_key}", None)
                if sid:
                    self._session_cache[chat_key] = sid
                    return sid
            except Exception:
                pass
        return None

    async def _save_session_id(self, chat_key: str, session_id: str) -> None:
        self._session_cache[chat_key] = session_id
        if hasattr(self, "put_kv_data"):
            try:
                await self.put_kv_data(f"dsh_session:{chat_key}", session_id)
            except Exception as exc:
                logger.warning(f"保存 DSH 会话映射失败：{exc}")

    async def _clear_session_id(self, chat_key: str) -> None:
        self._session_cache.pop(chat_key, None)
        if hasattr(self, "delete_kv_data"):
            try:
                await self.delete_kv_data(f"dsh_session:{chat_key}")
            except Exception:
                pass

    async def _load_chat_option(self, chat_key: str, key: str, default: str = "") -> str:
        if not hasattr(self, "get_kv_data"):
            return default
        try:
            value = await self.get_kv_data(f"dsh_option:{key}:{chat_key}", default)
            return str(value or default)
        except Exception:
            return default

    async def _save_chat_option(self, chat_key: str, key: str, value: str) -> None:
        if hasattr(self, "put_kv_data"):
            try:
                await self.put_kv_data(f"dsh_option:{key}:{chat_key}", value)
            except Exception as exc:
                logger.warning(f"保存 DSH {key} 设置失败：{exc}")

    async def _session_for_chat(
        self,
        event: AstrMessageEvent,
        client: DshHttpClient,
        http_session: aiohttp.ClientSession,
    ) -> str:
        chat_key = await self._chat_key(event)
        if self._cfg("persistent_session", True):
            session_id = await self._load_session_id(chat_key)
            if session_id:
                return session_id

        cwd = str(self._cfg("default_working_directory", "") or "").strip()
        if not cwd:
            cwd = str((await client.describe(http_session)).get("cwd") or "").strip()
        default_preset = str(self._cfg("default_agent_preset", "") or "").strip()
        agent_preset = await self._load_chat_option(chat_key, "agent_preset", default_preset)
        session_id = await client.create_session(http_session, cwd=cwd, agent_preset=agent_preset)
        await self._configure_session_defaults(client, http_session, session_id)
        if self._cfg("persistent_session", True):
            await self._save_session_id(chat_key, session_id)
        return session_id

    async def _configure_session_defaults(
        self,
        client: DshHttpClient,
        http_session: aiohttp.ClientSession,
        session_id: str,
    ) -> None:
        provider = str(self._cfg("default_provider", "") or "").strip()
        model = str(self._cfg("default_model", "") or "").strip()
        effort = str(self._cfg("default_reasoning_effort", "") or "").strip()
        if provider and model:
            await client.select_model(http_session, session_id, provider, model, effort)
        permission = str(self._cfg("default_permission_preset", "") or "").strip()
        if permission:
            await client.prompt(http_session, session_id, f"/permission {permission}")

    # ---------------- 传输实现 ----------------

    async def _run_http(self, event: AstrMessageEvent, text: str, prompt_mode: str = "queue") -> DshReply:
        client = self._make_http_client()
        async with aiohttp.ClientSession() as http_session:
            session_id = await self._session_for_chat(event, client, http_session)
            return await client.run_prompt(http_session, session_id, text, mode=prompt_mode)

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
        try:
            return await self._run_http(event, text, prompt_mode=prompt_mode)
        except DshConnectionError:
            if mode == "auto":
                logger.info("HTTP 连接失败，回退到 headless 模式执行")
                return await self._run_headless(text)
            raise

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

    async def _reply_chain(self, reply: DshReply) -> list:
        components = []
        text = self._truncate(reply.text)
        if text:
            components.append(Plain(text))
        maximum = max(0, int(self._cfg("max_images_per_reply", 4)))
        for source in reply.image_sources[:maximum]:
            image_path = await self._download_image(source)
            if image_path:
                components.append(Image(file=image_path))
        return components

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
        if lowered in ("status", "状态", "ping"):
            yield event.plain_result(await self._status_text())
            return
        if lowered in ("reset", "new", "重置", "清空"):
            yield event.plain_result(await self._reset_chat(event))
            return
        if lowered in ("settings", "config", "配置", "设置"):
            yield event.plain_result(await self._settings_text(event))
            return
        if lowered in ("sessions", "session", "会话", "列表"):
            yield event.plain_result(await self._sessions_text())
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
        text_parts = [part for part in chain if isinstance(part, Plain)]
        image_parts = [part for part in chain if isinstance(part, Image)]
        for part in text_parts:
            for chunk in _chunk_text(part.text, int(self._cfg("reply_chunk_size", 2000) or 0)):
                yield event.plain_result(chunk)
        if image_parts:
            yield event.chain_result(image_parts)

    async def _status_text(self) -> str:
        lines = ["【DeepSeek Harness 桥接状态】", f"模式：{self.mode}"]
        if self.mode in ("http", "auto"):
            client = self._make_http_client()
            try:
                async with aiohttp.ClientSession() as http_session:
                    info = await client.describe(http_session)
                lines.append(
                    f"✅ HTTP 已连接：{info.get('provider', '?')} / {info.get('model', '?')} "
                    f"(version {info.get('version', '?')})"
                )
                lines.append(f"工作目录：{info.get('cwd', '?')}")
            except DshError as exc:
                lines.append(f"❌ HTTP 连接失败：{exc}")
        if self.mode in ("headless", "auto"):
            profile = str(self._cfg("dsh_profile", "headless") or "headless").strip()
            lines.append(f"命令行：{self.dsh_command} --profile {profile}")
        return "\n".join(lines)

    async def _require_http(self) -> None:
        if self.mode == "headless":
            raise DshError("该操作需要 DSH Web HTTP 模式。请将 mode 设置为 http 或 auto。")

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
            values = ((row.get("projections") or {}).get("values") or {})
            title = values.get("title") or "未命名会话"
            lines.append(
                f"{index}. {row.get('sessionId', '?')[:12]}  {'运行中' if row.get('running') else '空闲'}\n"
                f"   {title} | {row.get('cwd', '?')} | preset={row.get('agentPreset', '?')}"
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
            return "用法：/dsh permission <read-only|workspace-write|danger-full-access>"
        try:
            client, http_session, session_id = await self._active_http_session(event)
            try:
                await client.prompt(http_session, session_id, f"/permission {parts[1].strip()}")
            finally:
                await http_session.close()
            return f"已发送 DSH 权限预设：{parts[1].strip()}"
        except DshError as exc:
            return f"❌ {exc}"

    async def _preset_command(self, event: AstrMessageEvent, command: str) -> str:
        parts = command.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            return "用法：/dsh preset <agent preset>。设置会在 reset 后创建的新会话生效。"
        chat_key = await self._chat_key(event)
        preset = parts[1].strip()
        await self._save_chat_option(chat_key, "agent_preset", preset)
        return f"已设置当前聊天的新会话 agent preset：{preset}。执行 /dsh reset 后生效。"

    async def _settings_text(self, event: AstrMessageEvent) -> str:
        chat_key = await self._chat_key(event)
        preset = await self._load_chat_option(chat_key, "agent_preset", str(self._cfg("default_agent_preset", "") or ""))
        return "\n".join([
            "【DSH Bridge 配置】",
            f"mode={self.mode}",
            f"http_base_url={self._cfg('http_base_url', 'http://127.0.0.1:3080')}",
            f"persistent_session={self._cfg('persistent_session', True)}",
            f"working_directory={self._cfg('default_working_directory', '') or 'DSH 默认'}",
            f"agent_preset={preset or 'DSH 默认'}",
            f"default_model={self._cfg('default_provider', '')}/{self._cfg('default_model', '')}",
            f"default_reasoning_effort={self._cfg('default_reasoning_effort', '') or 'DSH 默认'}",
            f"default_permission_preset={self._cfg('default_permission_preset', '') or 'DSH 默认'}",
            f"图片：max_images_per_reply={self._cfg('max_images_per_reply', 4)}，max_image_bytes={self._cfg('max_image_bytes', 5242880)}",
        ])

    # ---------------- LLM 工具 ----------------

    def _llm_tools_available(self) -> bool:
        return bool(self._cfg("enable_llm_tools", False))

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request: ProviderRequest):
        """Hide DSH tools completely until the operator enables them."""
        if self._llm_tools_available() or not getattr(request, "func_tool", None):
            return
        for tool_name in {
            "dsh_bridge_get_status",
            "dsh_bridge_list_sessions",
            "dsh_bridge_get_models",
            "dsh_bridge_send_prompt",
            "dsh_bridge_create_session",
            "dsh_bridge_set_model",
            "dsh_bridge_set_permission",
            "dsh_bridge_stop",
            "dsh_bridge_get_config",
        }:
            request.func_tool.remove_tool(tool_name)

    @filter.llm_tool(name="dsh_bridge_get_status")
    async def tool_get_status(self, event: AstrMessageEvent):
        """读取 DeepSeek Harness 主机状态、版本、当前模型与工作目录。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._status_text()

    @filter.llm_tool(name="dsh_bridge_list_sessions")
    async def tool_list_sessions(self, event: AstrMessageEvent):
        """列出 DSH 已有会话及其运行状态、工作目录、Agent 预设。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._sessions_text()

    @filter.llm_tool(name="dsh_bridge_get_models")
    async def tool_get_models(self, event: AstrMessageEvent):
        """查询当前 DSH 会话模型、服务商和可用推理强度。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._model_command(event, "model")

    @filter.llm_tool(name="dsh_bridge_send_prompt")
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

    @filter.llm_tool(name="dsh_bridge_create_session")
    async def tool_create_session(
        self,
        event: AstrMessageEvent,
        working_directory: str = "",
        agent_preset: str = "",
    ):
        """创建并绑定一个新的 DSH 会话。

        Args:
            working_directory(string): DSH 工作目录；留空沿用插件默认目录。
            agent_preset(string): DSH Agent 预设；留空沿用当前聊天预设。
        """
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        try:
            await self._require_http()
            client = self._make_http_client()
            chat_key = await self._chat_key(event)
            async with aiohttp.ClientSession() as http_session:
                cwd = working_directory.strip() or str(self._cfg("default_working_directory", "") or "").strip()
                if not cwd:
                    cwd = str((await client.describe(http_session)).get("cwd") or "")
                preset = agent_preset.strip() or await self._load_chat_option(
                    chat_key, "agent_preset", str(self._cfg("default_agent_preset", "") or "")
                )
                session_id = await client.create_session(http_session, cwd=cwd, agent_preset=preset)
                await self._configure_session_defaults(client, http_session, session_id)
            await self._save_session_id(chat_key, session_id)
            yield f"已创建并绑定 DSH 会话：{session_id}"
        except DshError as exc:
            yield f"创建 DSH 会话失败：{exc}"

    @filter.llm_tool(name="dsh_bridge_set_model")
    async def tool_set_model(self, event: AstrMessageEvent, provider: str, model: str, reasoning_effort: str = ""):
        """切换当前 DSH 会话的 provider、模型和可选推理强度。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._model_command(event, f"model {provider}/{model} {reasoning_effort}".strip())

    @filter.llm_tool(name="dsh_bridge_set_permission")
    async def tool_set_permission(self, event: AstrMessageEvent, preset: str):
        """设置当前 DSH 会话权限预设，如 read-only、workspace-write、danger-full-access。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._permission_command(event, f"permission {preset}")

    @filter.llm_tool(name="dsh_bridge_stop")
    async def tool_stop(self, event: AstrMessageEvent):
        """取消当前聊天绑定的 DSH 会话正在执行的任务。"""
        if not self._llm_tools_available():
            yield "DSH LLM 工具当前未启用。请在插件配置中设置 enable_llm_tools=true。"
            return
        yield await self._cancel(event)

    @filter.llm_tool(name="dsh_bridge_get_config")
    async def tool_get_config(self, event: AstrMessageEvent):
        """读取 DSH Bridge 的连接、默认会话、模型、权限与图片渲染配置。"""
        yield await self._settings_text(event)

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
        self._session_cache.clear()
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
