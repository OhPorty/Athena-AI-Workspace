"""
Athena's generic MCP client manager -- deliberately knows nothing about
any specific tool or server. An MCP server (HighlightHunter, a Rust
toolchain, a database, anything added later) is just configuration;
its tools are discovered through the standard MCP protocol.

A server is described by a config dict (see normalize_config):
  local program   {"name", "command", "args", "env", "cwd"}
  web address     {"name", "transport": "http" | "sse", "url", "headers"}
plus {"model_access", "enabled_tools", "timeout"} for whether and how
models may use its tools (see mcp_tools.py).

The `mcp` SDK is async-only and the rest of Athena is synchronous, so this
runs its own background thread with an event loop and exposes plain
synchronous methods that bridge onto it. Each server lives in ONE
long-lived task that opens and closes its connection (the SDK requires a
connection to be closed by the task that opened it), so a server can be
disconnected, reconnected or lost without leaking or wedging anything.
"""

import asyncio
import contextlib
import re
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Optional

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import CONNECTION_CLOSED

DEFAULT_TIMEOUT = 60.0
_MIN_TIMEOUT, _MAX_TIMEOUT = 5.0, 600.0
_CONNECT_TIMEOUT = 30.0
_NAME_OK = re.compile(r"^[\w .-]{1,60}$")
_TRANSPORTS = ("stdio", "http", "sse")


# ---------- configuration ----------

def _pairs(value, splitter, what):
    """A dict, or a list of lines like 'KEY=VALUE' / 'Name: value', as a dict of strings."""
    if value in (None, "", [], {}):
        return {}
    if isinstance(value, dict):
        return {str(k).strip(): str(v) for k, v in value.items() if str(k).strip()}
    lines = value.splitlines() if isinstance(value, str) else list(value)
    out = {}
    for line in lines:
        line = str(line).strip()
        if not line or line.startswith("#"):
            continue
        if splitter not in line:
            raise ValueError(f"Each {what} must look like NAME{splitter}value, but got {line!r}.")
        key, val = line.split(splitter, 1)
        if not key.strip():
            raise ValueError(f"A {what} is missing its name: {line!r}.")
        out[key.strip()] = val.strip()
    return out


def normalize_config(raw):
    """-> a complete, validated config. Raises ValueError with a plain message. Old configs that only
    have name/command/args keep working: they are local programs that models may not use."""
    if not isinstance(raw, dict):
        raise ValueError("A server needs a name and either a command or a web address.")
    name = str(raw.get("name") or "").strip()
    if not _NAME_OK.match(name):
        raise ValueError("Give the server a short name (letters, numbers, spaces, dots, dashes).")
    command, url = str(raw.get("command") or "").strip(), str(raw.get("url") or "").strip()
    transport = str(raw.get("transport") or ("http" if url and not command else "stdio")).strip().lower()
    if transport not in _TRANSPORTS:
        raise ValueError(f"transport must be one of: {', '.join(_TRANSPORTS)}.")
    if transport == "stdio" and not command:
        raise ValueError("A local server needs a command to start it (for example python3).")
    if transport != "stdio" and not re.match(r"^https?://[^\s/]+", url):
        raise ValueError("A web server needs a full address starting with http:// or https://.")
    args = raw.get("args") or []
    if isinstance(args, str):
        args = args.split()
    try:
        timeout = float(raw.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    enabled = raw.get("enabled_tools")
    return {
        "name": name, "transport": transport, "command": command, "args": [str(a) for a in args],
        "env": _pairs(raw.get("env"), "=", "environment setting"), "cwd": str(raw.get("cwd") or "").strip(),
        "url": url, "headers": _pairs(raw.get("headers"), ":", "header"),
        "model_access": bool(raw.get("model_access")),
        "enabled_tools": [str(t) for t in enabled] if isinstance(enabled, (list, tuple)) else None,
        "timeout": min(max(timeout, _MIN_TIMEOUT), _MAX_TIMEOUT),
    }


def public_view(config):
    """What the web UI may be shown: never the values of environment settings or headers (they hold secrets)."""
    cfg = normalize_config(config)
    return {**{k: cfg[k] for k in ("name", "transport", "command", "args", "cwd", "url", "model_access", "enabled_tools", "timeout")},
            "env_names": sorted(cfg["env"]), "header_names": sorted(cfg["headers"])}


# ---------- the manager ----------

class _Server:
    def __init__(self, config):
        self.config = config
        self.session = None
        self.tools = []
        self.error = None
        self.status = "connecting"          # connecting | connected | error | stopped
        self.task = None
        self.stop = None


class MCPManager:
    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._servers = {}
        self._lock = threading.Lock()

    def start(self):
        """Starts the background event loop thread. Call once at Athena startup."""
        if self._thread is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coro(self, coro, timeout=30):
        if self._loop is None:
            raise RuntimeError("MCPManager not started -- call start() first")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    # ----- transports -----

    @asynccontextmanager
    async def _open_transport(self, cfg):
        if cfg["transport"] == "stdio":
            params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env=cfg["env"] or None, cwd=cfg["cwd"] or None)
            async with stdio_client(params) as streams:
                yield streams
        elif cfg["transport"] == "sse":
            from mcp.client.sse import sse_client
            async with sse_client(cfg["url"], headers=cfg["headers"] or None) as streams:
                yield streams
        else:
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared._httpx_utils import create_mcp_http_client
            async with create_mcp_http_client(headers=cfg["headers"] or None) as client:
                async with streamable_http_client(cfg["url"], http_client=client) as streams:
                    yield streams

    async def _serve(self, server, ready):
        """One server's whole life, in one task: connect, find its tools, stay up until told to stop."""
        try:
            async with AsyncExitStack() as stack:
                streams = await stack.enter_async_context(self._open_transport(server.config))
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await session.initialize()
                tools = (await session.list_tools()).tools
                with self._lock:
                    server.session, server.tools, server.error, server.status = session, tools, None, "connected"
                if not ready.done():
                    ready.set_result(tools)
                await server.stop.wait()
        except BaseException as e:
            with self._lock:
                server.error, server.status = _describe(e), "error"
            if not ready.done():
                ready.set_exception(RuntimeError(server.error))
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            with self._lock:
                server.session = None
                if server.status == "connected":
                    server.status = "stopped"

    async def _connect_async(self, config):
        name = config["name"]
        old = self._servers.get(name)
        if old is not None:
            await self._stop_async(old)
        server = _Server(config)
        server.stop = asyncio.Event()
        with self._lock:
            self._servers[name] = server
        ready = asyncio.get_running_loop().create_future()
        server.task = asyncio.get_running_loop().create_task(self._serve(server, ready))
        try:
            return await asyncio.wait_for(asyncio.shield(ready), timeout=_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            with self._lock:
                server.error, server.status = f"Did not finish starting within {int(_CONNECT_TIMEOUT)} seconds.", "error"
            await self._stop_async(server)
            raise RuntimeError(server.error)

    async def _stop_async(self, server):
        if server.stop is not None:
            server.stop.set()
        if server.task is not None and not server.task.done():
            try:
                await asyncio.wait_for(server.task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                server.task.cancel()
                with contextlib.suppress(BaseException):
                    await server.task

    # ----- synchronous API -----

    def connect_server(self, config):
        """Connects (or reconnects) one server, discovers its tools, keeps it connected. Blocks until
        ready; raises RuntimeError with a plain message if it cannot be reached."""
        return self._run_coro(self._connect_async(normalize_config(config)), timeout=_CONNECT_TIMEOUT + 15)

    def connect_all(self, configs, on_result=None):
        """Connect several servers without making anyone wait: each in its own thread, results reported
        through on_result(name, tools_or_None, error_or_None)."""
        for raw in configs:
            try:
                cfg = normalize_config(raw)
            except ValueError as e:
                if on_result:
                    on_result(str(raw.get("name") if isinstance(raw, dict) else raw), None, str(e))
                continue
            with self._lock:
                self._servers.setdefault(cfg["name"], _Server(cfg))

            def run(cfg=cfg):
                try:
                    tools = self.connect_server(cfg)
                    result = (cfg["name"], tools, None)
                except Exception as e:
                    result = (cfg["name"], None, _describe(e))
                if on_result:
                    on_result(*result)

            threading.Thread(target=run, daemon=True).start()

    def disconnect_server(self, name):
        server = self._servers.get(name)
        if server is not None:
            self._run_coro(self._stop_async(server), timeout=20)
        with self._lock:
            self._servers.pop(name, None)

    def reconnect(self, name):
        server = self._servers.get(name)
        if server is None:
            raise RuntimeError(f"No MCP server named '{name}'.")
        return self.connect_server(server.config)

    def set_config(self, config):
        """Update the stored config (model access, enabled tools, timeout) without reconnecting."""
        cfg = normalize_config(config)
        server = self._servers.get(cfg["name"])
        if server is not None:
            server.config = cfg

    def get_config(self, name):
        server = self._servers.get(name)
        return server.config if server else None

    def list_tools(self, name):
        server = self._servers.get(name)
        return list(server.tools) if server else []

    def list_servers(self):
        """Every server we know of, connected or not, so the UI can show real status either way."""
        with self._lock:
            return [{"name": n, "connected": s.status == "connected", "status": s.status, "error": s.error,
                     "tool_count": len(s.tools), "transport": s.config["transport"]} for n, s in self._servers.items()]

    def call_tool(self, name, tool_name, arguments, timeout=None):
        """Call one tool and return the SDK's CallToolResult. The connection is checked first and re-made if the
        server has gone away. A call that dies halfway is NOT retried (it may already have done its work): the
        connection is re-made for next time and the error says so. A tool that is merely slow just times out."""
        server = self._servers.get(name)
        if server is None:
            raise RuntimeError(f"MCP server '{name}' is not set up")
        limit = float(timeout or server.config["timeout"])
        return self._run_coro(self._call_async(server, tool_name, arguments or {}, limit), timeout=limit + 2 * _CONNECT_TIMEOUT + 15)

    async def _alive(self, server):
        try:
            await asyncio.wait_for(server.session.send_ping(), timeout=3)
            return True
        except Exception:
            return False

    async def _call_async(self, server, tool_name, arguments, limit):
        name = server.config["name"]
        if server.session is None or not await self._alive(server):
            await self._connect_async(server.config)                   # it went away since last time: bring it back first
            server = self._servers[name]
        try:
            return await asyncio.wait_for(server.session.call_tool(tool_name, arguments), timeout=limit)
        except asyncio.TimeoutError:
            raise TimeoutError(f"'{name}' did not answer within {int(limit)} seconds")
        except Exception as e:
            if not _connection_lost(e):
                raise
            with contextlib.suppress(Exception):
                await self._connect_async(server.config)               # ready for next time, but never re-run this call
            raise ConnectionError(f"The connection to '{name}' dropped during the call, so it may or may not have run. "
                                  "It has been reconnected.")


def _connection_lost(e):
    code = getattr(e, "code", None)
    if code == CONNECTION_CLOSED:
        return True
    return isinstance(e, (ConnectionError, BrokenPipeError, EOFError, OSError)) or type(e).__name__ in (
        "ClosedResourceError", "BrokenResourceError", "EndOfStream", "ClosedResourceError")


def _describe(e):
    """A short, readable reason from an exception (task-group errors wrap the real one)."""
    inner = getattr(e, "exceptions", None)
    if inner:
        return _describe(inner[0])
    text = str(e).strip()
    return text or type(e).__name__


mcp_manager = MCPManager()
