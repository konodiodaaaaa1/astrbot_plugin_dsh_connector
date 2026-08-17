"""Markdown-aware selection and chunking for AstrBot text-to-image replies."""

from __future__ import annotations

import re


RENDER_MODES = ("text", "auto", "card")
_RICH_MARKDOWN = re.compile(
    r"(^#{1,6}\s|^\s*(?:[-*+]\s|\d+[.)]\s)|^>\s|^\|.+\|\s*$|```|~~~|\$\$|!\[[^\]]*\]\()",
    re.MULTILINE,
)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


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
