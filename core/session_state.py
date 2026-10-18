"""Durable per-chat state stored through AstrBot's plugin KV interface."""

from __future__ import annotations

from typing import Any

from .session_options import normalize_session_options


class SessionState:
    """Own DSH session bindings and per-chat connector options."""

    def __init__(self) -> None:
        self.session_cache: dict[str, str] = {}
        self.options_cache: dict[str, dict[str, str]] = {}
        self.owners_cache: dict[str, str] | None = None

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
        session_id = self.session_cache.get(chat_key)
        if session_id is None and hasattr(plugin, "get_kv_data"):
            try:
                session_id = str(await plugin.get_kv_data(f"dsh_session:{chat_key}", "") or "") or None
            except Exception:
                session_id = None
        self.session_cache.pop(chat_key, None)
        if session_id:
            await self.forget_owner(plugin, session_id)
        if hasattr(plugin, "delete_kv_data"):
            await plugin.delete_kv_data(f"dsh_session:{chat_key}")

    async def load_owners(self, plugin: Any) -> dict[str, str]:
        """Return the DSH session id to chat-key index used for event routing."""
        if self.owners_cache is not None:
            return dict(self.owners_cache)
        raw = None
        if hasattr(plugin, "get_kv_data"):
            try:
                raw = await plugin.get_kv_data("dsh_session_owners", None)
            except Exception:
                raw = None
        owners: dict[str, str] = {}
        if isinstance(raw, dict):
            owners = {str(key): str(value) for key, value in raw.items() if key and value}
        self.owners_cache = owners
        return dict(owners)

    async def remember_owner(self, plugin: Any, session_id: str, chat_key: str) -> None:
        owners = await self.load_owners(plugin)
        if owners.get(session_id) == chat_key:
            return
        owners[session_id] = chat_key
        self.owners_cache = owners
        if hasattr(plugin, "put_kv_data"):
            await plugin.put_kv_data("dsh_session_owners", owners)

    async def forget_owner(self, plugin: Any, session_id: str) -> None:
        owners = await self.load_owners(plugin)
        if session_id not in owners:
            return
        owners.pop(session_id, None)
        self.owners_cache = owners
        if hasattr(plugin, "put_kv_data"):
            await plugin.put_kv_data("dsh_session_owners", owners)

    async def owner_of(self, plugin: Any, session_id: str) -> str | None:
        owners = await self.load_owners(plugin)
        return owners.get(session_id)

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

    async def load_options(self, plugin: Any, chat_key: str) -> dict[str, str]:
        if chat_key in self.options_cache:
            return dict(self.options_cache[chat_key])
        raw = None
        if hasattr(plugin, "get_kv_data"):
            try:
                raw = await plugin.get_kv_data(f"dsh_session_options:{chat_key}", None)
            except Exception:
                raw = None
        options = normalize_session_options(raw)
        if not raw and hasattr(plugin, "get_kv_data"):
            try:
                options["agent_preset"] = str(
                    await plugin.get_kv_data(f"dsh_option:agent_preset:{chat_key}", "") or ""
                )
            except Exception:
                pass
        self.options_cache[chat_key] = options
        return dict(options)

    async def save_options(self, plugin: Any, chat_key: str, raw: Any) -> dict[str, str]:
        options = normalize_session_options(raw)
        self.options_cache[chat_key] = options
        if hasattr(plugin, "put_kv_data"):
            await plugin.put_kv_data(f"dsh_session_options:{chat_key}", options)
        return dict(options)

    async def update_options(self, plugin: Any, chat_key: str, changes: dict[str, str]) -> dict[str, str]:
        options = await self.load_options(plugin, chat_key)
        options.update(changes)
        return await self.save_options(plugin, chat_key, options)

    async def clear_options(self, plugin: Any, chat_key: str) -> dict[str, str]:
        options = normalize_session_options({})
        self.options_cache[chat_key] = options
        if hasattr(plugin, "put_kv_data"):
            await plugin.put_kv_data(f"dsh_session_options:{chat_key}", options)
        return dict(options)
