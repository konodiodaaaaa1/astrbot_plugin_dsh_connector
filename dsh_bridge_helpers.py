"""Pure helpers used by the DSH bridge and its regression tests."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)")


@dataclass
class DshReply:
    """Assistant output collected from one completed DSH turn."""

    text: str = ""
    image_sources: list[str] = field(default_factory=list)


def event_seq(entry: Any) -> int:
    """Return a history event sequence number without trusting its shape."""
    try:
        return int(entry.get("seq", 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def assistant_reply(event: dict[str, Any]) -> DshReply:
    """Extract text and image references from a DSH ``assistant/message`` event.

    DSH content blocks are extensible. The parser deliberately accepts the
    documented text blocks plus the common image/attachment field variants so
    newer DSH renderers can remain backwards compatible with this bridge.
    """
    data = event.get("data") or {}
    message = data.get("message") or {}
    content = message.get("content") or data.get("content") or []
    if not isinstance(content, list):
        content = [content]

    text_parts: list[str] = []
    images: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type in {"text", "markdown"} and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
            for source in MARKDOWN_IMAGE_RE.findall(block["text"]):
                _append_unique(images, source)
        _collect_image_sources(block, images, image_hint=block_type in {"image", "image_url", "attachment"})

    text = "".join(text_parts).strip()
    for source in MARKDOWN_IMAGE_RE.findall(text):
        _append_unique(images, source)
    return DshReply(text=text, image_sources=images)


def merge_replies(events: list[dict[str, Any]], after_seq: int = 0) -> DshReply:
    """Merge all assistant messages created after ``after_seq`` in a turn."""
    text_parts: list[str] = []
    images: list[str] = []
    for entry in events:
        event = entry.get("event") if isinstance(entry, dict) else None
        if not isinstance(event, dict) or event_seq(event) <= after_seq:
            continue
        if event.get("type") != "assistant/message":
            continue
        reply = assistant_reply(event)
        if reply.text:
            text_parts.append(reply.text)
        for source in reply.image_sources:
            _append_unique(images, source)
    return DshReply(text="\n".join(text_parts), image_sources=images)


def model_rows(value: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize DSH ``session.models`` provider groups for commands/tools."""
    rows: list[dict[str, Any]] = []
    for group in value.get("groups") or []:
        if not isinstance(group, dict):
            continue
        provider = group.get("id")
        for model in group.get("models") or []:
            if not provider or not isinstance(model, dict) or not model.get("id"):
                continue
            reasoning = model.get("reasoning") or {}
            efforts = [entry.get("id") for entry in reasoning.get("efforts") or [] if isinstance(entry, dict) and entry.get("id")]
            rows.append({
                "provider": str(provider),
                "provider_name": str(group.get("name") or provider),
                "model": str(model["id"]),
                "name": str(model.get("name") or model["id"]),
                "efforts": efforts,
            })
    return rows


def _collect_image_sources(value: Any, images: list[str], image_hint: bool = False) -> None:
    if isinstance(value, str):
        if image_hint or value.startswith(("data:image/", "http://", "https://", "file://")):
            _append_unique(images, value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_image_sources(item, images, image_hint=image_hint)
        return
    if not isinstance(value, dict):
        return
    attachment_id = value.get("attachmentId")
    if isinstance(attachment_id, str) and attachment_id.strip():
        _append_unique(images, f"dsh-attachment:{attachment_id.strip()}")
        return
    for key, item in value.items():
        if key == "type":
            continue
        key_hint = key.lower() in {"url", "uri", "src", "source", "image", "image_url", "file", "path", "attachment", "attachments"}
        _collect_image_sources(item, images, image_hint=image_hint or key_hint)


def _append_unique(items: list[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    value = value.strip().strip("<>")
    if value and value not in items:
        items.append(value)
