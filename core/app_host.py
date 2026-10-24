"""Discovery of the running DeepSeek Harness desktop application.

The desktop application boots the same Web host as "dsh web" but binds it to a
random loopback port (--host 127.0.0.1 --port 0) and never writes that port to
disk.  This module finds the running host through the OS process table and
returns the loopback base URL that the connector's Web client can talk to.
"""

from __future__ import annotations

import time
from typing import Any

try:
    import psutil
except ImportError:  # AstrBot ships psutil; keep the plugin importable without it.
    psutil = None

try:
    from .dsh_client import DshError
except ImportError:  # AstrBot also loads a plugin main.py as a module.
    from dsh_client import DshError


APP_HOST_MARKER = "dsh-desktop-host"
APP_EXECUTABLE_HINTS = ("deepseek harness", "deepseek-harness", "dsh-desktop")
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "localhost", "::1"})
DISCOVERY_CACHE_SECONDS = 10.0
APP_ENTRY_HINT = (
    "请先启动 DeepSeek Harness 桌面应用；如果应用已经启动但仍找不到，"
    "请把插件配置 app_base_url 设为应用 Web Host 的地址（例如 http://127.0.0.1:10404）。"
)

_cache: dict[str, Any] = {"base_url": "", "expires_at": 0.0}


def reset_app_host_cache() -> None:
    """Drop the cached discovery result so the next call probes again."""
    _cache["base_url"] = ""
    _cache["expires_at"] = 0.0


def discover_app_base_url(
    force: bool = False,
    cache_seconds: float = DISCOVERY_CACHE_SECONDS,
) -> str:
    """Return the loopback base URL of the running desktop application host.

    Args:
        force: Ignore a cached result and scan the process table again.
        cache_seconds: How long a successful discovery stays cached.

    Returns:
        Base URL such as http://127.0.0.1:10404.

    Raises:
        DshError: psutil is unavailable, or no desktop host is running.
    """
    now = time.monotonic()
    cached = str(_cache.get("base_url") or "")
    if cached and not force and float(_cache.get("expires_at") or 0.0) > now:
        return cached
    if psutil is None:
        raise DshError(
            "未安装 psutil，无法自动发现 DeepSeek Harness 桌面应用的入口。"
            "请重新安装 AstrBot 依赖，或在插件配置中填写 app_base_url。"
        )
    port = _discover_port()
    if port is None:
        raise DshError(f"未发现正在运行的 DeepSeek Harness 桌面应用。{APP_ENTRY_HINT}")
    base_url = f"http://127.0.0.1:{port}"
    _cache["base_url"] = base_url
    _cache["expires_at"] = now + max(float(cache_seconds), 0.0)
    return base_url


def _discover_port() -> int | None:
    """Return the smallest loopback port owned by a desktop host process."""
    candidates: list[int] = []
    for pid, ports in _listening_loopback_ports().items():
        identity = _process_identity(pid)
        if identity is None or not _is_dsh_desktop_host(*identity):
            continue
        candidates.extend(ports)
    return min(candidates) if candidates else None


def _listening_loopback_ports() -> dict[int, list[int]]:
    """Map listening PIDs to their loopback ports."""
    found: dict[int, list[int]] = {}
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.Error, OSError):
        return found
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.pid:
            continue
        address = connection.laddr[0]
        if str(address) not in LOOPBACK_ADDRESSES:
            continue
        port = int(connection.laddr[1] or 0)
        if port <= 0:
            continue
        ports = found.setdefault(int(connection.pid), [])
        if port not in ports:
            ports.append(port)
    return found


def _process_identity(pid: int) -> tuple[str, str, str] | None:
    """Return (name, executable, command line) for one PID, or None."""
    try:
        process = psutil.Process(pid)
        name = str(process.name() or "")
        try:
            executable = str(process.exe() or "")
        except (psutil.Error, OSError):
            executable = ""
        try:
            command_line = " ".join(process.cmdline() or [])
        except (psutil.Error, OSError):
            command_line = ""
    except (psutil.Error, OSError):
        return None
    return name, executable, command_line


def _is_dsh_desktop_host(name: str, executable: str, command_line: str) -> bool:
    """Whether one process is the Web host inside the desktop application."""
    lowered = f"{name} {executable} {command_line}".lower()
    if "cli.js" in lowered and "index.js" not in lowered:
        # The desktop-bundled CLI (dsh web/headless) stays on the Web entry.
        return False
    if APP_HOST_MARKER in lowered:
        return True
    return any(hint in lowered for hint in APP_EXECUTABLE_HINTS)
