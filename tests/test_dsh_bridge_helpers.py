import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_service import dotted_path, namespace_map, parse_json_value, read_path
from core.dsh_client import DshHttpClient
from core.session_state import SessionState
from dsh_bridge_helpers import DshReply, assistant_reply, merge_replies, model_rows


class CaptureClient(DshHttpClient):
    def __init__(self):
        super().__init__("http://example.test", 1, 0.1)
        self.calls = []

    async def rpc(self, _session, method, payload):
        self.calls.append((method, payload))
        return {"accepted": True, "sessionId": "child", "revision": 3}


class DshBridgeHelperTests(unittest.TestCase):
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


class DshClientPayloadTests(unittest.IsolatedAsyncioTestCase):
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


class DshBridgeImageTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(request.func_tool.removed, ["dsh_bridge_set_dsh_setting"])


if __name__ == "__main__":
    unittest.main()
