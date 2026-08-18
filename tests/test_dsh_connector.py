import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_service import dotted_path, namespace_map, parse_json_value, read_path
from core.dsh_client import DshHttpClient, normalize_client_time_zone
from core.session_options import SessionSetupWizard, format_session_options, normalize_session_options
from core.session_state import SessionState
from core.reply_render import should_render_card, split_markdown_for_cards
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
        session = Session()
        result = await client.execute_command(session, "session-a", "/permission read-only")
        self.assertEqual(session.url, "http://example.test/api/commands/execute")
        self.assertEqual(session.request["method"], "commands/execute")
        self.assertEqual(session.request["payload"], {
            "args": {"agentId": "session-a", "line": "/permission read-only"},
        })
        self.assertEqual(result["result"]["text"], "preset read-only")

    async def test_permission_presets_are_read_from_live_schema_shape(self):
        class SettingsClient(DshHttpClient):
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

        client = SettingsClient("http://example.test", 1, 0.1)
        self.assertEqual(await client.permission_presets(None), ["sandboxed", "trusted"])

    async def test_create_session_prefers_workspace_id(self):
        client = CaptureClient()
        await client.create_session(None, cwd="D:/ignored", workspace_id="workspace-live")
        self.assertEqual(client.calls, [(
            "session.create",
            {"workspaceId": "workspace-live"},
        )])

    async def test_archive_session_uses_dsh_workspace_api(self):
        client = CaptureClient()
        await client.archive_session(None, "session-a")
        self.assertEqual(client.calls, [("workspace.archiveSession", {"sessionId": "session-a"})])

    async def test_history_streams_each_new_text_delta_once(self):
        chunk_one = {"event": {"seq": 1, "type": "assistant/chunk", "data": {
            "chunk": {"type": "text-delta", "text": "hel"},
        }}}
        chunk_two = {"event": {"seq": 2, "type": "assistant/chunk", "data": {
            "chunk": {"type": "text-delta", "text": "lo"},
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

            async def history(self, _session, _session_id, max_messages=100):
                self.round += 1
                return [chunk_one] if self.round == 1 else [chunk_one, chunk_two, message, end]

        streamed = []
        reply = await StreamingClient()._await_reply(None, "session-a", 0, on_chunk=streamed.append)
        self.assertEqual(streamed, ["hel", "lo"])
        self.assertEqual(reply.text, "hello")

    async def test_history_returns_only_last_assistant_message_from_multi_step_turn(self):
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
                self.round = 0

            async def history(self, _session, _session_id, max_messages=100):
                self.round += 1
                return [step_one] if self.round == 1 else [step_one, step_two, end]

        reply = await MultiStepClient()._await_reply(None, "session-a", 0)
        self.assertEqual(reply.text, "final answer")

    async def test_prompt_uses_dsh_compatible_iana_time_zone(self):
        client = CaptureClient()
        await client.prompt(None, "session-a", "hello", client_time_zone="Asia/Shanghai")
        payload = client.calls[0][1]
        self.assertEqual(payload["clientTimeZone"], "Asia/Shanghai")

    async def test_settings_mutation_uses_revision_and_path_operation(self):
        client = CaptureClient()
        await client.mutate_settings(None, "llm-deepseek", [{"op": "set", "path": ["maxTokens"], "value": 4096}], 7)
        self.assertEqual(client.calls, [("settings.mutate", {
            "ns": "llm-deepseek",
            "ops": [{"op": "set", "path": ["maxTokens"], "value": 4096}],
            "expectedRevision": 7,
        })])

    async def test_queue_and_goal_payloads_follow_dsh_contract(self):
        client = CaptureClient()
        await client.update_queue(None, "session-a", "message-a", "edit", "revised")
        await client.goal_action(None, "pause", "session-a", {"id": "goal-a", "revision": 2})
        self.assertEqual(client.calls[0], ("session.updateQueue", {
            "sessionId": "session-a",
            "itemId": "message-a",
            "action": {"kind": "edit", "content": [{"type": "text", "text": "revised"}]},
        }))
        self.assertEqual(client.calls[1], ("goal.pause", {
            "sessionId": "session-a", "ref": {"id": "goal-a", "revision": 2},
        }))

    async def test_attachment_payload_uses_owning_session(self):
        client = CaptureClient()
        await client.attachment(None, "session-a", "sha256:abc")
        self.assertEqual(client.calls, [("session.attachment", {
            "sessionId": "session-a", "attachmentId": "sha256:abc",
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


class DshConnectorImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_mode_uses_only_completed_reply_path(self):
        from main import Main

        plugin = object.__new__(Main)
        plugin.config = {
            "mode": "http",
            "stream_replies": True,
            "reply_render_mode": "card",
        }
        self.assertFalse(plugin._stream_replies_enabled())

        plugin.config["reply_render_mode"] = "text"
        self.assertTrue(plugin._stream_replies_enabled())

    async def test_stream_reply_forwards_dsh_chunks_to_astrbot(self):
        from main import Main
        from astrbot.api.message_components import Plain

        plugin = object.__new__(Main)

        async def run(_event, _text, prompt_mode="queue", on_chunk=None):
            await on_chunk("hello ")
            await on_chunk("world")
            return DshReply(text="hello world")

        plugin._run = run

        class Event:
            def __init__(self):
                self.chunks = []

            async def send_streaming(self, generator, use_fallback=False):
                self.assertTrue(use_fallback)
                async for chain in generator:
                    self.chunks.extend(part.text for part in chain.chain if isinstance(part, Plain))

            def assertTrue(self, value):
                if not value:
                    raise AssertionError("expected true")

        event = Event()
        reply, streamed = await plugin._stream_reply(event, "prompt", "queue")
        self.assertEqual(event.chunks, ["hello ", "world"])
        self.assertEqual(streamed, "hello world")
        self.assertEqual(reply.text, "hello world")

    async def test_streamed_final_text_is_not_sent_twice(self):
        from main import Main

        self.assertEqual(Main._unstreamed_text("hello", "hello"), "")
        self.assertEqual(Main._unstreamed_text("hello\nnext", "hello"), "next")
        self.assertEqual(Main._unstreamed_text("final", "partial"), "final")

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


if __name__ == "__main__":
    unittest.main()
