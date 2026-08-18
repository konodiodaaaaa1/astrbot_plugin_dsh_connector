"""Markdown-aware selection and chunking for AstrBot text-to-image replies."""

from __future__ import annotations

import re


RENDER_MODES = ("text", "auto", "card")
_RICH_MARKDOWN = re.compile(
    r"(^#{1,6}\s|^\s*(?:[-*+]\s|\d+[.)]\s)|^>\s|^\|.+\|\s*$|```|~~~|\$\$|!\[[^\]]*\]\()",
    re.MULTILINE,
)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
DEFAULT_MESSAGE_MAX_CHARS = 4200


def normalize_reply_render_mode(value: object) -> str:
    mode = str(value or "text").strip().lower()
    return mode if mode in RENDER_MODES else "text"


def should_render_card(mode: object, markdown: str, minimum_chars: int = 120) -> bool:
    normalized = normalize_reply_render_mode(mode)
    if normalized == "card":
        return bool(markdown.strip())
    if normalized != "auto":
        return False
    return len(markdown.strip()) >= max(1, minimum_chars) or bool(_RICH_MARKDOWN.search(markdown))


def split_markdown_for_cards(markdown: str, maximum_chars: int) -> list[str]:
    """Split long Markdown while keeping every generated fenced-code chunk valid."""
    text = str(markdown or "").strip()
    if not text:
        return []
    limit = max(500, int(maximum_chars or 6000))
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buffer = ""
    opened_fence = ""
    close_fence = ""

    for line in text.splitlines(keepends=True):
        if buffer and len(buffer) + len(line) > limit:
            if opened_fence:
                chunks.append((buffer.rstrip() + "\n" + close_fence).strip())
                buffer = opened_fence + "\n"
            else:
                chunks.append(buffer.strip())
                buffer = ""

        buffer += line
        match = _FENCE.match(line)
        if not match:
            continue
        token = match.group(1)
        if not opened_fence:
            opened_fence = line.rstrip()
            close_fence = token[0] * len(token)
        elif token[0] == close_fence[0] and len(token) >= len(close_fence):
            opened_fence = ""
            close_fence = ""

    if buffer.strip():
        if opened_fence:
            buffer = buffer.rstrip() + "\n" + close_fence
        chunks.append(buffer.strip())
    return chunks


def split_text_for_message(text: str, maximum_chars: int = DEFAULT_MESSAGE_MAX_CHARS) -> list[str]:
    """Split long plain text on line boundaries for platform message limits."""
    value = str(text or "")
    limit = max(1, int(maximum_chars or DEFAULT_MESSAGE_MAX_CHARS))
    if len(value) <= limit:
        return [value]

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in value.splitlines():
        if len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
            chunks.extend(line[index : index + limit] for index in range(0, len(line), limit))
            continue
        extra = len(line) if not current else len(line) + 1
        if current and current_length + extra > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) if len(current) == 1 else len(line) + 1

    if current:
        chunks.append("\n".join(current))
    return chunks or [value[index : index + limit] for index in range(0, len(value), limit)]
