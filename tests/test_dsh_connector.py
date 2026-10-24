import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import app_host
from core.app_host import discover_app_base_url, reset_app_host_cache
from core.approval_bridge import DshEventBridge
from core.config_service import dotted_path, namespace_map, parse_json_value, read_path
from core.dsh_client import (
    DshConnectionError,
    DshError,
    DshHttpClient,
    DshTimeout,
    build_auth_cookie,
    extract_browser_session_secret,
    normalize_client_time_zone,
)
from core.session_options import SessionSetupWizard, format_session_options, normalize_session_options
from core.session_state import SessionState
from core.reply_render import should_render_card, split_markdown_for_cards, split_text_for_message
from dsh_connector_helpers import DshReply, assistant_reply, merge_replies, model_rows


class CaptureClient(DshHttpClient):
    def __init__(self):
        super().__init__("http://example.test", 1, 0.1)
        self.calls = []

    async def rpc(self, _session, method, payload):
        self.calls.append((method, payload))
        return {"accepted": True, "sessionId": "child", "revision": 3}


class DshConnectorHelperTests(unittest.TestCase):
    def test_help_lists_session_delete_command(self):
        from main import HELP_TEXT

        self.assertIn("/dsh help", HELP_TEXT)
        self.assertIn("/dsh session delete [id]", HELP_TEXT)
        self.assertIn("/dsh approve", HELP_TEXT)
        self.assertIn("/dsh deny", HELP_TEXT)

    def test_assistant_reply_keeps_text_and_distinct_images(self):
        reply = assistant_reply({
            "data": {"message": {"content": [
                {"type": "text", "text": "图表：![chart](https://example.test/chart.png)"},
                {"type": "image", "url": "data:image/png;base64,AA=="},
                {"type": "image", "url": "https://example.test/chart.png"},
            ]}}
        })
        self.assertEqual(reply.text, "图表：![chart](https://example.test/chart.png)")
        self.assertEqual(reply.image_sources, ["https://example.test/chart.png", "data:image/png;base64,AA=="])

    def test_merge_replies_only_reads_new_assistant_events(self):
        events = [
            {"event": {"seq": 4, "type": "assistant/message", "data": {"message": {"content": [{"type": "text", "text": "old"}]}}}},
            {"event": {"seq": 5, "type": "user/message", "data": {}}},
            {"event": {"seq": 6, "type": "assistant/message", "data": {"message": {"content": [{"type": "text", "text": "new"}]}}}},
        ]
        self.assertEqual(merge_replies(events, after_seq=4).text, "new")

    def test_assistant_reply_tags_native_dsh_attachments(self):
        reply = assistant_reply({"data": {"message": {"content": [{
            "type": "image",
            "attachment": {"attachmentId": "sha256:abc", "mediaType": "image/png", "bytes": 3, "width": 1, "height": 1},
        }]}}})
        self.assertEqual(reply.image_sources, ["dsh-attachment:sha256:abc"])

    def test_model_rows_include_reasoning_efforts(self):
        rows = model_rows({"groups": [{"id": "deepseek", "name": "DeepSeek", "models": [{"id": "v4", "reasoning": {"efforts": [{"id": "low"}, {"id": "high"}]}}]}]})
        self.assertEqual(rows, [{"provider": "deepseek", "provider_name": "DeepSeek", "model": "v4", "name": "v4", "efforts": ["low", "high"]}])

    def test_settings_path_helpers_keep_json_types(self):
        description = {"namespaces": [{"ns": "llm-deepseek", "value": {"models": [{"id": "v4"}]}}]}
        self.assertEqual(namespace_map(description)["llm-deepseek"]["value"]["models"][0]["id"], "v4")
        self.assertEqual(dotted_path("models.default"), ["models", "default"])
        self.assertEqual(parse_json_value('{"enabled":true}'), {"enabled": True})
        self.assertEqual(parse_json_value("raw-text"), "raw-text")
        self.assertEqual(read_path({"a": {"b": 1}}, ["a", "b"]), 1)

    def test_session_setup_wizard_builds_per_chat_options(self):
        wizard = SessionSetupWizard(
            "D:/AI/workspace",
            [{"id": "standard", "name": "Standard"}],
            [{"provider": "deepseek", "model": "v4", "efforts": ["low", "high"]}],
            permission_presets=["read-only", "workspace-write", "danger-full-access"],
        )
        wizard.process("D:/project")
        wizard.process("1")
        wizard.process("1")
        wizard.process("2")
        wizard.process("2")
        wizard.process("1")
        result = wizard.process("y")
        self.assertTrue(result.confirmed)
        self.assertEqual(wizard.options, {
            "workspace_id": "",
            "working_directory": "D:/project",
            "agent_preset": "standard",
            "provider": "deepseek",
            "model": "v4",
            "reasoning_effort": "high",
            "permission_preset": "workspace-write",
            "client_time_zone": "Asia/Shanghai",
        })
        self.assertIn("当前聊天", format_session_options(wizard.options))

    def test_session_setup_lists_and_selects_live_workspace_and_permissions(self):
        wizard = SessionSetupWizard(
            "D:/AI/workspace",
            [{"id": "standard", "name": "Standard"}],
            [{"provider": "deepseek", "model": "v4", "efforts": ["low"]}],
            workspaces=[{
                "workspaceId": "workspace-live",
                "title": "Live Workspace",
                "path": "D:/live",
            }],
            permission_presets=["sandboxed", "trusted"],
        )
        self.assertIn("Live Workspace", wizard.initial_prompt())
        wizard.process("1")
        wizard.process("0")
        wizard.process("0")
        wizard.process("0")
        permission_prompt = wizard.process("0")
        self.assertIn("sandboxed", permission_prompt.prompt)
        self.assertIn("trusted", permission_prompt.prompt)
        wizard.process("2")
        wizard.process("1")
        result = wizard.process("y")
        self.assertTrue(result.confirmed)
        self.assertEqual(wizard.options["workspace_id"], "workspace-live")
        self.assertEqual(wizard.options["working_directory"], "")
        self.assertEqual(wizard.options["permission_preset"], "trusted")

    def test_directory_and_workspace_options_are_mutually_exclusive(self):
        options = normalize_session_options({
            "workspace_id": "workspace-live",
            "working_directory": "D:/stale",
        })
        self.assertEqual(options["workspace_id"], "workspace-live")
        self.assertEqual(options["working_directory"], "")

    def test_invalid_saved_timezone_falls_back_to_utc(self):
        options = normalize_session_options({"client_time_zone": "中国标准时间"})
        self.assertEqual(options["client_time_zone"], "Asia/Shanghai")

    def test_explicit_iana_timezone_is_preserved(self):
        self.assertEqual(normalize_client_time_zone("Asia/Shanghai"), "Asia/Shanghai")
        self.assertEqual(normalize_client_time_zone("UTC"), "UTC")

    def test_card_split_preserves_fenced_code_blocks(self):
        markdown = "# Result\n\n```python\n" + "print('line')\n" * 160 + "```\n"
        cards = split_markdown_for_cards(markdown, 600)
        self.assertGreater(len(cards), 1)
        self.assertTrue(all(card.count("```") % 2 == 0 for card in cards))
        self.assertTrue(should_render_card("auto", "```python\nprint('ok')\n```", 120))
        self.assertFalse(should_render_card("text", markdown, 1))

    def test_message_split_uses_line_boundaries_and_fixed_limit(self):
        text = "\n".join(f"line-{index}" for index in range(900))
        chunks = split_text_for_message(text, 100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))
        self.assertEqual("\n".join(chunks), text)


class DshClientPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_command_uses_typert_remote_wire(self):
        class Response:
            status = 200

            def __init__(self, request):
                self.request = request

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return {
                    "type": "server-response",
                    "rpcId": self.request["rpcId"],
                    "result": {"ok": True, "value": {
                        "commandId": "cmd-test",
                        "result": {"kind": "success", "text": "preset read-only"},
                    }},
                }

            async def text(self):
                return ""

        class Session:
            def __init__(self):
                self.url = None
                self.request = None

            def post(self, url, **kwargs):
                self.url = url
                self.request = kwargs["json"]
                return Response(self.request)

        client = DshHttpClient("http://example.test", 1, 0.1)
        client._secret = b"unit-test-secret-0123456789abcd"
        session = Session()
        result = await client.execute_command(session, "session-a", "/permission read-only")
        self.assertEqual(session.url, "http://example.test/api/commands/execute")
        self.assertEqual(session.request["method"], "commands/execute")
        self.assertEqual(session.request["payload"], {
            "args": {
                "agentId": "session-a",
                "line": "/permission read-only",
                "submittedAttachments": [],
            },
        })
        self.assertEqual(result["result"]["text"], "preset read-only")

    async def test_permission_presets_are_read_from_live_schema_shape(self):
        class SettingsClient(DshHttpClient):
            def __init__(self):
                super().__init__("http://example.test", 1, 0)

            async def rpc(self, _session, method, args=None):
                raise DshError("permission catalog unavailable")

            async def settings(self, _session):
                return {"namespaces": [{
                    "ns": "permission",
                    "schema": {
                        "uid": 14,
                        "refs": {
                            "11": {"type": "const", "value": "sandboxed"},
                            "12": {"type": "const", "value": "trusted"},
                            "13": {"type": "union", "list": [11, 12]},
                            "14": {"type": "object", "dict": {"defaultPreset": 13}},
                        },
                    },
                }]}

        client = SettingsClient()
        self.assertEqual(await client.permission_presets(None), ["sandboxed", "trusted"])

    async def test_create_session_prefers_workspace_id(self):
        client = CaptureClient()
        await client.create_session(None, cwd="D:/ignored", workspace_id="workspace-live")
        self.assertEqual(client.calls, [(
            "session/create",
            {"request": {"workspaceId": "workspace-live"}},
        )])

    async def test_archive_session_uses_dsh_workspace_api(self):
        client = CaptureClient()
        await client.archive_session(None, "session-a")
        self.assertEqual(client.calls, [(
            "workspace/archiveSession",
            {"request": {"sessionId": "session-a"}},
        )])

    async def test_await_reply_waits_for_turn_end_after_intermediate_messages(self):
        partial = {"event": {"seq": 1, "type": "assistant/message", "data": {
            "message": {"content": [{"type": "text", "text": "hel"}]},
        }}}
        message = {"event": {"seq": 3, "type": "assistant/message", "data": {
            "message": {"content": [{"type": "text", "text": "hello"}]},
        }}}
        end = {"event": {"seq": 4, "type": "turn/end", "data": {
            "reason": {"kind": "completed"},
        }}}

        class StreamingClient(DshHttpClient):
            def __init__(self):
                super().__init__("http://example.test", 1, 0)
                self.round = 0

            async def cursor(self, _session, _session_id):
                return 2 if self.round == 0 else 4

            async def page(self, _session, _session_id, through_seq, max_messages=100, before_seq=None):
                if self.round == 0:
                    self.round = 1
                    return {"records": [partial]}
                return {"records": [partial, message, end]}

        reply = await StreamingClient()._await_reply(None, "session-a", 0)
        self.assertEqual(reply.text, "hello")

    async def test_await_reply_returns_only_last_assistant_message_from_multi_step_turn(self):
        step_one = {"event": {"seq": 1, "type": "assistant/message", "data": {
            "turn": 1,
            "step": 1,
            "message": {"content": [{"type": "text", "text": "step progress"}]},
        }}}
        step_two = {"event": {"seq": 2, "type": "assistant/message", "data": {
            "turn": 1,
            "step": 2,
            "message": {"content": [{"type": "text", "text": "final answer"}]},
        }}}
        end = {"event": {"seq": 3, "type": "turn/end", "data": {
            "turn": 1,
            "reason": {"kind": "completed"},
        }}}

        class MultiStepClient(DshHttpClient):
            def __init__(self):
                super().__init__("http://example.test", 1, 0)

            async def cursor(self, _session, _session_id):
                return 3

            async def page(self, _session, _session_id, through_seq, max_messages=100, before_seq=None):
                return {"records": [step_one, step_two, end]}

        reply = await MultiStepClient()._await_reply(None, "session-a", 0)
        self.assertEqual(reply.text, "final answer")

    async def test_prompt_uses_dsh_compatible_iana_time_zone(self):
        client = CaptureClient()
        await client.prompt(None, "session-a", "hello", client_time_zone="Asia/Shanghai")
        payload = client.calls[0][1]["request"]
        self.assertEqual(payload["clientTimeZone"], "Asia/Shanghai")
        self.assertEqual(payload["sessionId"], "session-a")
        self.assertEqual(payload["mode"], "queue")
        self.assertTrue(payload["requestId"])

    async def test_settings_mutation_uses_revision_and_path_operation(self):
        client = CaptureClient()
        await client.mutate_settings(None, "llm-deepseek", [{"op": "set", "path": ["maxTokens"], "value": 4096}], 7)
        self.assertEqual(client.calls, [("settings/mutate", {
            "ns": "llm-deepseek",
            "ops": [{"op": "set", "path": ["maxTokens"], "value": 4096}],
            "expectedRevision": 7,
        })])

    async def test_queue_and_goal_payloads_follow_dsh_contract(self):
        client = CaptureClient()
        await client.update_queue(None, "session-a", "message-a", "edit", "revised")
        await client.goal_action(None, "pause", "session-a", {"id": "goal-a", "revision": 2})
        self.assertEqual(client.calls[0], ("session/updateQueue", {
            "request": {
                "sessionId": "session-a",
                "itemId": "message-a",
                "action": {"kind": "edit", "content": [{"type": "text", "text": "revised"}]},
            },
        }))
        self.assertEqual(client.calls[1], ("goals/pause", {
            "agentId": "session-a", "ref": {"id": "goal-a", "revision": 2},
        }))

    async def test_attachment_payload_uses_owning_session(self):
        client = CaptureClient()
        await client.attachment(None, "session-a", "sha256:abc")
        self.assertEqual(client.calls, [("session/attachment", {
            "request": {"sessionId": "session-a", "attachmentId": "sha256:abc"},
        })])

    async def test_native_attachment_resolves_to_renderable_data_url(self):
        class AttachmentClient(CaptureClient):
            async def attachment(self, _session, session_id, attachment_id):
                self.calls.append((session_id, attachment_id))
                return {"attachment": {"mediaType": "image/png"}, "data": "AA=="}

        client = AttachmentClient()
        reply = await client.resolve_reply_attachments(
            None, "session-a", DshReply(text="image", image_sources=["dsh-attachment:sha256:abc"])
        )
        self.assertEqual(reply.image_sources, ["data:image/png;base64,AA=="])
        self.assertEqual(client.calls, [("session-a", "sha256:abc")])

    def test_credentials_secret_is_read_without_yaml_dependency(self):
        secret = bytes(range(32))
        encoded = base64.b64encode(secret).decode().replace("+", "-").replace("/", "_").rstrip("=")
        text = (
            "version: 1\n"
            "refs:\n"
            "  DEEPSEEK_API_KEY: value\n"
            "records:\n"
            "  client-connection/browser-session:\n"
            "    kind: grant\n"
            "    payload:\n"
            "      version: 1\n"
            f"      secret: {encoded}\n"
        )
        self.assertEqual(extract_browser_session_secret(text), secret)
        self.assertIsNone(extract_browser_session_secret("records: {}\n"))

    async def test_rpc_signs_the_browser_session_cookie_for_its_authority(self):
        secret = bytes(range(32))

        class Response:
            status = 200

            def __init__(self, request):
                self.request = request

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return {
                    "type": "server-response",
                    "rpcId": self.request["rpcId"],
                    "result": {"ok": True, "value": {"items": []}},
                }

            async def text(self):
                return ""

        class Session:
            def __init__(self):
                self.headers = None

            def post(self, url, **kwargs):
                self.headers = kwargs["headers"]
                return Response(kwargs["json"])

        client = DshHttpClient("http://127.0.0.1:3080", 1, 0.1)
        client._secret = secret
        session = Session()
        await client.rpc(session, "session/list", {"_request": {}})

        self.assertEqual(session.headers["content-type"], "application/json")
        name, _, value = session.headers["cookie"].partition("=")
        expected_name = "dsh-auth-" + base64.b64encode(
            hashlib.sha256(b"127.0.0.1:3080").digest()
        ).decode().replace("+", "-").replace("/", "_").rstrip("=")
        self.assertEqual(name, expected_name)

        version, body, signature = value.split(".")
        self.assertEqual(version, "v1")
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * ((4 - len(body) % 4) % 4)))
        self.assertEqual(payload["authority"], "127.0.0.1:3080")
        self.assertLess(payload["issuedAt"], payload["expiresAt"])
        expected_signature = base64.b64encode(
            hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
        ).decode().replace("+", "-").replace("/", "_").rstrip("=")
        self.assertEqual(signature, expected_signature)

    async def test_missing_credentials_raise_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            client = DshHttpClient("http://127.0.0.1:3080", 1, 0.1, dsh_home=directory)
            with self.assertRaises(DshError) as caught:
                await client.list_sessions(None)
        self.assertIn(".credentials.yaml", str(caught.exception))

    async def test_history_value_reads_page_through_the_projection_cursor(self):
        class PageClient(DshHttpClient):
            def __init__(self):
                super().__init__("http://example.test", 1, 0)
                self.pages = []

            async def projections(self, _session, _session_id):
                return {"asOfSeq": 42, "values": {"goal": {"goal": {"id": "goal-a", "revision": 2}}}}

            async def page(self, _session, session_id, through_seq, max_messages=100, before_seq=None):
                self.pages.append((session_id, through_seq, max_messages))
                return {
                    "records": [{"event": {"seq": 43, "type": "assistant/message", "data": {}}}],
                    "hasMore": False,
                }

        client = PageClient()
        value = await client.history_value(None, "session-a", 30)
        self.assertEqual(client.pages, [("session-a", 42, 30)])
        self.assertEqual(value["cursor"], 42)
        self.assertEqual(len(value["events"]), 1)
        self.assertIn("goal", value["projections"]["values"])

    async def test_permission_presets_prefer_the_catalog_endpoint(self):
        class CatalogClient(DshHttpClient):
            def __init__(self):
                super().__init__("http://example.test", 1, 0)
                self.calls = []

            async def rpc(self, _session, method, args=None):
                self.calls.append((method, args))
                return {"options": [{"value": "read-only"}, {"value": "workspace-write"}]}

        client = CatalogClient()
        self.assertEqual(await client.permission_presets(None), ["read-only", "workspace-write"])
        self.assertEqual(client.calls, [("permissionPresets/catalog", {})])


class DshApprovalBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_dispatches_approval_and_answers_via_event_result(self):
        seen = []
        answers = []

        class Client:
            def auth_cookie(self):
                return "dsh-auth-test=value"

            def websocket_url(self, path):
                return "ws://example.test" + path

            async def rpc(self, _session, method, args=None):
                answers.append((method, args))
                return None

        async def on_frame(frame):
            seen.append(frame)

        bridge = DshEventBridge(Client(), object(), on_frame, logging.getLogger("dsh-connector-test"))
        bridge._receive(json.dumps({"type": "item", "value": {"type": "ready", "clientId": "client-a"}}))
        bridge._receive(json.dumps({"type": "item", "value": {
            "type": "waterfall",
            "event": "approval/request",
            "eventId": "event-a",
            "agentId": "session-a",
            "request": {"toolName": "pwsh"},
        }}))
        await asyncio.sleep(0)

        self.assertEqual(bridge.client_id, "client-a")
        self.assertTrue(bridge.connected)
        self.assertEqual(seen[0]["eventId"], "event-a")

        await bridge.answer("event-a", "allowed-once")
        self.assertEqual(answers, [("$events/result", {
            "clientId": "client-a",
            "eventId": "event-a",
            "outcome": {"kind": "result", "value": "allowed-once"},
        })])

    async def test_bridge_refuses_to_answer_before_its_ready_frame(self):
        class Client:
            def auth_cookie(self):
                return ""

            def websocket_url(self, path):
                return "ws://example.test" + path

            async def rpc(self, *_args, **_kwargs):
                raise AssertionError("must not post before ready")

        bridge = DshEventBridge(Client(), object(), lambda _frame: None, logging.getLogger("test"))
        with self.assertRaises(DshError):
            await bridge.answer("event-a", "rejected")


class DshApprovalRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_is_forwarded_to_the_bound_chat_and_answered(self):
        from main import Main
        from core.session_state import SessionState

        plugin = object.__new__(Main)
        plugin.config = {"route_approvals": True}
        plugin._state = SessionState()
        plugin._state.owners_cache = {"session-a": "chat-a"}
        plugin._pending_approvals = {}
        plugin._approval_tasks = set()
        sent = []
        answered = []

        async def send_text(chat_key, text):
            sent.append((chat_key, text))

        plugin._send_text = send_text

        class Bridge:
            async def answer(self, event_id, outcome):
                answered.append((event_id, outcome))

        plugin._bridge = Bridge()
        frame = {
            "type": "waterfall",
            "event": "approval/request",
            "eventId": "event-a",
            "agentId": "session-a",
            "request": {"toolName": "pwsh", "reason": "需要写入工作区外文件"},
        }
        task = asyncio.create_task(plugin._handle_approval(frame))
        await asyncio.sleep(0)

        self.assertEqual(sent[0][0], "chat-a")
        self.assertIn("DSH 请求授权", sent[0][1])
        self.assertIn("pwsh", sent[0][1])
        self.assertEqual(
            await plugin._resolve_approval("chat-a", "allowed-once"),
            "已允许本次工具调用。",
        )
        await task
        self.assertEqual(answered, [("event-a", "allowed-once")])

    async def test_approval_without_a_bound_chat_is_left_to_the_dsh_gui(self):
        from main import Main
        from core.session_state import SessionState

        plugin = object.__new__(Main)
        plugin.config = {"route_approvals": True}
        plugin._state = SessionState()
        plugin._state.owners_cache = {}
        plugin._pending_approvals = {}
        sent = []

        async def send_text(chat_key, text):
            sent.append((chat_key, text))

        plugin._send_text = send_text
        frame = {
            "type": "waterfall",
            "event": "approval/request",
            "eventId": "event-b",
            "agentId": "session-gui",
            "request": {"toolName": "pwsh"},
        }
        await plugin._handle_approval(frame)
        self.assertEqual(sent, [])
        self.assertEqual(plugin._pending_approvals, {})


class DshCompletionWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_watcher_pushes_a_finished_turn_to_the_chat(self):
        from main import Main

        plugin = object.__new__(Main)
        plugin.config = {
            "completion_watch_timeout": 60,
            "reply_render_mode": "text",
            "max_reply_chars": 0,
            "max_images_per_reply": 0,
        }
        sent = []

        async def send_message(chat_key, chain):
            sent.append((chat_key, [part.text for part in chain]))

        plugin._send_message = send_message

        class Client:
            async def await_turn(self, _session, session_id, baseline, timeout=None):
                return DshReply(text="后台完成")

        plugin._make_http_client = lambda *_args, **_kwargs: Client()
        await plugin._watch_completion("chat-a", "session-a", 0)
        self.assertEqual(sent, [("chat-a", ["后台完成"])])

    async def test_timed_out_turn_starts_a_background_watcher(self):
        from main import Main
        from core.session_options import normalize_session_options

        plugin = object.__new__(Main)
        plugin.config = {
            "command_timeout": 1,
            "poll_interval": 0.05,
            "reply_render_mode": "text",
            "max_reply_chars": 0,
            "max_images_per_reply": 0,
            "completion_watch_timeout": 60,
        }
        plugin._state = SessionState()
        plugin._state.session_cache["chat-a"] = "session-a"
        plugin._state.options_cache["chat-a"] = normalize_session_options({})
        plugin._completion_tasks = set()
        clients = []
        sent = []

        class Client:
            def __init__(self, fail):
                self.fail = fail

            async def wait_idle(self, *_args):
                return None

            async def cursor(self, *_args):
                return 5

            async def prompt(self, *_args, **_kwargs):
                return {"accepted": True}

            async def await_turn(self, _session, session_id, baseline, timeout=None):
                if self.fail:
                    raise DshTimeout("等待超时")
                return DshReply(text="后台结果")

        def make_client(*_args, **_kwargs):
            client = Client(fail=not clients)
            clients.append(client)
            return client

        async def send_message(chat_key, chain):
            sent.append((chat_key, [part.text for part in chain]))

        plugin._make_http_client = make_client
        plugin._send_message = send_message

        class Event:
            unified_msg_origin = "chat-a"

            def get_sender_id(self):
                return "1"

        with self.assertRaises(DshTimeout) as caught:
            await plugin._run_http(Event(), "long task")
        self.assertIn("后台", str(caught.exception))

        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(sent, [("chat-a", ["后台结果"])])


class DshSessionStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_state_persists_and_reuses_chat_binding(self):
        class Plugin:
            def __init__(self):
                self.store = {}

            async def get_kv_data(self, key, default):
                return self.store.get(key, default)

            async def put_kv_data(self, key, value):
                self.store[key] = value

            async def delete_kv_data(self, key):
                self.store.pop(key, None)

        plugin = Plugin()
        state = SessionState()
        await state.save_session(plugin, "chat-a", "session-a")
        self.assertEqual(await state.load_session(plugin, "chat-a"), "session-a")
        await state.clear_session(plugin, "chat-a")
        self.assertIsNone(await state.load_session(plugin, "chat-a"))

    async def test_session_options_are_isolated_by_chat(self):
        class Plugin:
            def __init__(self):
                self.store = {}

            async def get_kv_data(self, key, default):
                return self.store.get(key, default)

            async def put_kv_data(self, key, value):
                self.store[key] = value

        plugin = Plugin()
        state = SessionState()
        await state.update_options(plugin, "chat-a", {"working_directory": "D:/A"})
        await state.update_options(plugin, "chat-b", {"working_directory": "D:/B"})
        self.assertEqual((await state.load_options(plugin, "chat-a"))["working_directory"], "D:/A")
        self.assertEqual((await state.load_options(plugin, "chat-b"))["working_directory"], "D:/B")

    async def test_session_owner_index_routes_events_back_to_the_chat(self):
        class Plugin:
            def __init__(self):
                self.store = {}

            async def get_kv_data(self, key, default):
                return self.store.get(key, default)

            async def put_kv_data(self, key, value):
                self.store[key] = value

            async def delete_kv_data(self, key):
                self.store.pop(key, None)

        plugin = Plugin()
        state = SessionState()
        await state.remember_owner(plugin, "session-a", "chat-a")
        self.assertEqual(await state.owner_of(plugin, "session-a"), "chat-a")
        self.assertEqual(await SessionState().owner_of(plugin, "session-a"), "chat-a")

        await state.forget_owner(plugin, "session-a")
        self.assertIsNone(await SessionState().owner_of(plugin, "session-a"))


class DshConnectorImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_reply_is_sent_as_one_message_chain(self):
        from main import Main
        from astrbot.api.message_components import Plain

        plugin = object.__new__(Main)
        plugin.config = {
            "max_reply_chars": 0,
            "max_images_per_reply": 0,
            "reply_render_mode": "text",
        }

        async def run(_event, _text, prompt_mode="queue"):
            return DshReply(text="paragraph one\n\nparagraph two")

        plugin._run = run

        class Event:
            def __init__(self):
                self.message_str = "dsh prompt"
                self.results = []

            def plain_result(self, text):
                result = ("plain", text)
                self.results.append(result)
                return result

            def chain_result(self, chain):
                result = ("chain", chain)
                self.results.append(result)
                return result

        event = Event()
        results = [result async for result in plugin.dsh(event)]
        self.assertEqual([kind for kind, _value in results], ["plain", "chain"])
        self.assertEqual(len(event.results[1][1]), 1)
        self.assertIsInstance(event.results[1][1][0], Plain)
        self.assertIn("paragraph one", event.results[1][1][0].text)

    async def test_qq_adapter_sends_long_reply_in_ordered_private_messages(self):
        from unittest.mock import AsyncMock

        from main import Main
        from astrbot.api.message_components import Plain
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

        plugin = object.__new__(Main)
        plugin.config = {
            "max_reply_chars": 0,
            "max_images_per_reply": 0,
            "reply_render_mode": "text",
        }
        expected_text = "我要复活: 08-18 15:53:16\n\n" + "正文段落。\n" * 1500

        async def run(_event, _text, prompt_mode="queue"):
            return DshReply(text=expected_text)

        plugin._run = run

        class Event:
            message_str = "dsh prompt"

            def plain_result(self, text):
                return ("plain", text)

            def chain_result(self, chain):
                return ("chain", chain)

        results = [result async for result in plugin.dsh(Event())]
        chains = [value for kind, value in results if kind == "chain"]
        self.assertGreater(len(chains), 1)
        self.assertTrue(all(len(chain) == 1 and isinstance(chain[0], Plain) for chain in chains))

        bot = AsyncMock()
        for chain in chains:
            await AiocqhttpMessageEvent.send_message(
                bot=bot,
                message_chain=MessageChain(chain),
                event=None,
                is_group=False,
                session_id="987654",
            )
        self.assertEqual(bot.send_private_msg.await_count, len(chains))
        payload_texts = []
        for call in bot.send_private_msg.await_args_list:
            payload = call.kwargs["message"]
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["type"], "text")
            self.assertLessEqual(len(payload[0]["data"]["text"]), 4200)
            payload_texts.append(payload[0]["data"]["text"])
        self.assertEqual("\n".join(payload_texts), expected_text.rstrip("\n"))

    async def test_card_mode_uses_astrbot_t2i_and_keeps_image_component(self):
        from main import Main

        plugin = object.__new__(Main)
        plugin.config = {"reply_render_mode": "card", "card_max_chars": 6000}
        rendered = []

        async def text_to_image(markdown):
            rendered.append(markdown)
            return "https://example.test/dsh-card.png"

        plugin.text_to_image = text_to_image
        components = await plugin._render_reply_text("```python\nprint('ok')\n```")
        self.assertEqual(rendered, ["```python\nprint('ok')\n```"])
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0].file, "https://example.test/dsh-card.png")

    async def test_card_mode_falls_back_to_plain_text_when_t2i_fails(self):
        from main import Main

        plugin = object.__new__(Main)
        plugin.config = {"reply_render_mode": "card"}

        async def text_to_image(_markdown):
            raise RuntimeError("renderer unavailable")

        plugin.text_to_image = text_to_image
        components = await plugin._render_reply_text("# fallback")
        self.assertEqual(components[0].text, "# fallback")

    async def test_data_url_becomes_an_astrbot_image_component(self):
        from main import Main

        plugin = object.__new__(Main)
        plugin.config = {"max_reply_chars": 4000, "max_images_per_reply": 4, "max_image_bytes": 1024}
        plugin._temp_media = set()
        chain = await plugin._reply_chain(assistant_reply({
            "data": {"message": {"content": [
                {"type": "text", "text": "render this"},
                {"type": "image", "url": "data:image/png;base64,iVBORw0KGgo="},
            ]}}
        }))
        self.assertEqual(chain[0].text, "render this")
        self.assertEqual(len(chain), 2)
        image_path = Path(chain[1].file)
        self.assertTrue(image_path.is_file())
        for path in plugin._temp_media:
            path.unlink(missing_ok=True)

    async def test_llm_setting_mutation_tool_has_separate_gate(self):
        from main import Main

        class FunctionTools:
            def __init__(self):
                self.removed = []

            def remove_tool(self, name):
                self.removed.append(name)

        plugin = object.__new__(Main)
        plugin.config = {"enable_llm_tools": True, "enable_llm_mutation_tools": False}
        request = type("Request", (), {"func_tool": FunctionTools()})()
        await plugin.on_llm_request(None, request)
        self.assertEqual(request.func_tool.removed, ["dsh_connector_set_dsh_setting"])


class DshDesktopAppDiscoveryTests(unittest.TestCase):
    def setUp(self):
        reset_app_host_cache()

    def tearDown(self):
        reset_app_host_cache()

    def test_process_matching_accepts_desktop_host_and_app_executable(self):
        self.assertTrue(app_host._is_dsh_desktop_host(
            "electron.exe",
            "C:/electron.exe",
            "electron app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/index.js",
        ))
        self.assertTrue(app_host._is_dsh_desktop_host(
            "DeepSeek Harness.exe",
            "D:/deepseek harness/app/DeepSeek Harness.exe",
            "",
        ))
        self.assertFalse(app_host._is_dsh_desktop_host(
            "node.exe",
            "C:/node/node.exe",
            "node dsh web --port 3080",
        ))
        self.assertFalse(app_host._is_dsh_desktop_host(
            "DeepSeek Harness.exe",
            "D:/deepseek harness/app/DeepSeek Harness.exe",
            "app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js web --port 3080",
        ))

    def test_discovery_returns_loopback_base_url_of_desktop_host(self):
        identities = {
            42: ("DeepSeek Harness.exe", "D:/deepseek harness/app/DeepSeek Harness.exe", "dsh-desktop-host/lib/index.js"),
            7: ("node.exe", "C:/node/node.exe", "dsh web --port 3080"),
        }
        with mock.patch.object(app_host, "_listening_loopback_ports", return_value={42: [10404], 7: [3080]}), \
                mock.patch.object(app_host, "_process_identity", side_effect=identities.get):
            self.assertEqual(discover_app_base_url(), "http://127.0.0.1:10404")

    def test_discovery_ignores_web_host_and_reports_actionable_error(self):
        identity = ("node.exe", "C:/node/node.exe", "dsh web --port 3080")
        with mock.patch.object(app_host, "_listening_loopback_ports", return_value={7: [3080]}), \
                mock.patch.object(app_host, "_process_identity", return_value=identity):
            with self.assertRaises(DshError) as caught:
                discover_app_base_url()
        self.assertIn("桌面应用", str(caught.exception))
        self.assertIn("app_base_url", str(caught.exception))

    def test_discovery_chooses_lowest_candidate_port(self):
        identities = {
            42: ("DeepSeek Harness.exe", "DeepSeek Harness.exe", "dsh-desktop-host"),
            43: ("DeepSeek Harness.exe", "DeepSeek Harness.exe", "dsh-desktop-host"),
        }
        with mock.patch.object(app_host, "_listening_loopback_ports", return_value={42: [10405], 43: [10404]}), \
                mock.patch.object(app_host, "_process_identity", side_effect=identities.get):
            self.assertEqual(discover_app_base_url(), "http://127.0.0.1:10404")

    def test_discovery_caches_success_until_forced(self):
        calls = []

        def listening_ports():
            calls.append(1)
            return {42: [10404]}

        identity = ("DeepSeek Harness.exe", "DeepSeek Harness.exe", "dsh-desktop-host")
        with mock.patch.object(app_host, "_listening_loopback_ports", side_effect=listening_ports), \
                mock.patch.object(app_host, "_process_identity", return_value=identity):
            discover_app_base_url()
            discover_app_base_url()
            self.assertEqual(len(calls), 1)
            discover_app_base_url(force=True)
            self.assertEqual(len(calls), 2)

    def test_discovery_reports_missing_psutil(self):
        with mock.patch.object(app_host, "psutil", None):
            with self.assertRaises(DshError) as caught:
                discover_app_base_url(force=True)
        self.assertIn("psutil", str(caught.exception))


class DshAppEntryModeTests(unittest.TestCase):
    def _plugin(self, **config):
        from main import Main

        plugin = object.__new__(Main)
        plugin.config = config
        plugin._active_entry = ""
        return plugin

    def test_app_mode_prefers_configured_app_base_url(self):
        plugin = self._plugin(mode="app", app_base_url="http://127.0.0.1:1234/")
        self.assertEqual(plugin._http_base_url(), "http://127.0.0.1:1234")
        self.assertEqual(plugin._make_http_client().base_url, "http://127.0.0.1:1234")

    def test_app_mode_discovers_entry_when_url_is_blank(self):
        plugin = self._plugin(mode="app")
        with mock.patch("main.discover_app_base_url", return_value="http://127.0.0.1:10404"):
            self.assertEqual(plugin._http_base_url("app"), "http://127.0.0.1:10404")
            self.assertEqual(plugin._make_http_client("app").base_url, "http://127.0.0.1:10404")

    def test_http_mode_keeps_web_base_url(self):
        plugin = self._plugin(mode="http", http_base_url="http://127.0.0.1:3080/")
        self.assertEqual(plugin._http_base_url(), "http://127.0.0.1:3080")

    def test_unknown_mode_falls_back_to_web_entry(self):
        plugin = self._plugin(mode="desktop")
        self.assertEqual(plugin.mode, "http")

    def test_app_mode_rejects_url_without_scheme(self):
        plugin = self._plugin(mode="app", app_base_url="127.0.0.1:10404")
        with self.assertRaises(DshError) as caught:
            plugin._http_base_url()
        self.assertIn("app_base_url", str(caught.exception))


class DshAppEntryStatusTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, **config):
        from main import Main

        plugin = object.__new__(Main)
        plugin.config = config
        plugin._active_entry = ""
        plugin._pending_approvals = {}
        plugin._bridge = None
        return plugin

    async def test_status_shows_discovered_app_entry(self):
        plugin = self._plugin(mode="app")

        class Client:
            async def describe(self, _session):
                return {
                    "provider": "deepseek-official",
                    "model": "deepseek-flash",
                    "version": "0.2.1",
                    "cwd": "D:/AI/workspace",
                }

        plugin._make_http_client = lambda *_args, **_kwargs: Client()
        with mock.patch("main.discover_app_base_url", return_value="http://127.0.0.1:10404"):
            text = await plugin._status_text()
        self.assertIn("App 入口：http://127.0.0.1:10404（自动发现）", text)
        self.assertIn("✅", text)

    async def test_status_reports_missing_app_entry_without_raising(self):
        plugin = self._plugin(mode="app")
        with mock.patch(
            "main.discover_app_base_url",
            side_effect=DshError("未发现正在运行的 DeepSeek Harness 桌面应用。"),
        ):
            text = await plugin._status_text()
        self.assertIn("❌", text)
        self.assertIn("桌面应用", text)


class DshAppEntryRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_mode_tries_app_entry_before_headless(self):
        from main import Main

        plugin = object.__new__(Main)
        plugin.config = {"mode": "auto"}
        plugin._active_entry = ""
        calls = []

        async def fake_run_http(event, text, prompt_mode="queue", entry=""):
            calls.append(entry)
            if entry == "http":
                raise DshConnectionError("web down")
            return DshReply(text="来自桌面应用")

        async def fail_headless(_text):
            raise AssertionError("headless must not run when the app entry works")

        plugin._run_http = fake_run_http
        plugin._run_headless = fail_headless
        plugin._http_base_url = lambda entry="": "http://127.0.0.1:10404"
        reply = await plugin._run(object(), "hi")
        self.assertEqual(reply.text, "来自桌面应用")
        self.assertEqual(calls, ["http", "app"])

    async def test_auto_mode_falls_back_to_headless_when_app_is_missing(self):
        from main import Main

        plugin = object.__new__(Main)
        plugin.config = {"mode": "auto"}
        plugin._active_entry = ""
        calls = []

        async def fake_run_http(event, text, prompt_mode="queue", entry=""):
            calls.append(entry)
            raise DshConnectionError("web down")

        async def fake_headless(_text):
            return DshReply(text="来自命令行")

        def fake_base_url(entry=""):
            if entry == "app":
                raise DshError("未发现正在运行的 DeepSeek Harness 桌面应用。")
            return "http://127.0.0.1:3080"

        plugin._run_http = fake_run_http
        plugin._run_headless = fake_headless
        plugin._http_base_url = fake_base_url
        reply = await plugin._run(object(), "hi")
        self.assertEqual(reply.text, "来自命令行")
        self.assertEqual(calls, ["http"])

    async def test_app_mode_connection_error_mentions_desktop_app(self):
        from main import Main

        plugin = object.__new__(Main)
        plugin.config = {"mode": "app"}
        plugin._active_entry = ""
        calls = []

        async def fake_run_http(event, text, prompt_mode="queue", entry=""):
            calls.append(entry)
            raise DshConnectionError("连接 DeepSeek Harness 失败（http://127.0.0.1:10404）")

        plugin._run_http = fake_run_http
        plugin._http_base_url = lambda entry="": "http://127.0.0.1:10404"
        with self.assertRaises(DshError) as caught:
            await plugin._run(object(), "hi")
        self.assertEqual(calls, ["app"])
        self.assertIn("桌面应用", str(caught.exception))


class DshApprovalBridgeClientFactoryTests(unittest.TestCase):
    def test_bridge_refreshes_client_before_connecting(self):
        refreshed = []

        class Client:
            def __init__(self, name):
                self.name = name

        def factory():
            client = Client(f"client-{len(refreshed)}")
            refreshed.append(client)
            return client

        bridge = DshEventBridge(
            Client("initial"),
            object(),
            lambda _frame: None,
            logging.getLogger("test"),
            client_factory=factory,
        )
        self.assertIs(bridge._refresh_client(), refreshed[0])
        self.assertIs(bridge.client, refreshed[0])


@unittest.skipUnless(
    os.environ.get("DSH_CONNECTOR_LIVE_APP") == "1",
    "set DSH_CONNECTOR_LIVE_APP=1 to probe the running DSH desktop app",
)
class DshDesktopAppLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_app_answers_an_authenticated_rpc(self):
        import aiohttp

        base_url = discover_app_base_url(force=True)
        client = DshHttpClient(base_url, 10, 0.5)
        async with aiohttp.ClientSession() as http_session:
            value = await client.rpc(http_session, "settings/describe", {})
        self.assertIn("namespaces", value)


if __name__ == "__main__":
    unittest.main()
