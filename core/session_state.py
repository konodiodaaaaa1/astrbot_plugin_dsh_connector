"""Durable per-chat state stored through AstrBot's plugin KV interface."""

from __future__ import annotations

from typing import Any


class SessionState:
    """Own DSH session bindings and per-chat connector options."""

    def __init__(self) -> None:
        self.session_cache: dict[str, str] = {}

    async def load_session(self, plugin: Any, chat_key: str) -> str | None:
        if chat_key in self.session_cache:
            return self.session_cache[chat_key]
        if not hasattr(plugin, "get_kv_data"):
            return None
        try:
            session_id = await plugin.get_kv_data(f"dsh_session:{chat_key}", None)
        except Exception:
            return None
        if session_id:
            session_id = str(session_id)
            self.session_cache[chat_key] = session_id
            return session_id
        return None

    async def save_session(self, plugin: Any, chat_key: str, session_id: str) -> None:
        self.session_cache[chat_key] = session_id
        if hasattr(plugin, "put_kv_data"):
            await plugin.put_kv_data(f"dsh_session:{chat_key}", session_id)

    async def clear_session(self, plugin: Any, chat_key: str) -> None:
        self.session_cache.pop(chat_key, None)
        if hasattr(plugin, "delete_kv_data"):
            await plugin.delete_kv_data(f"dsh_session:{chat_key}")

    async def load_option(self, plugin: Any, chat_key: str, key: str, default: str = "") -> str:
        if not hasattr(plugin, "get_kv_data"):
            return default
        try:
            value = await plugin.get_kv_data(f"dsh_option:{key}:{chat_key}", default)
        except Exception:
            return default
        return str(value or default)

    async def save_option(self, plugin: Any, chat_key: str, key: str, value: str) -> None:
        if hasattr(plugin, "put_kv_data"):
            await plugin.put_kv_data(f"dsh_option:{key}:{chat_key}", value)
