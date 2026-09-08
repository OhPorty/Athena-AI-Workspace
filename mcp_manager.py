"""
Athena's generic MCP client manager -- deliberately knows nothing about
any specific tool or server. Any MCP server (HighlightHunter,
Streamer.bot, or anything added later) is just configuration: a name
and a launch command. Tools are discovered dynamically via the
standard MCP protocol, not hardcoded here.

The entire `mcp` SDK is async-only, but the rest of Athena is
synchronous (plain httpx calls, not async def). Rather than convert
the whole app to async, this runs its own persistent background
thread with a dedicated asyncio event loop, and exposes plain
synchronous methods that bridge onto it via
asyncio.run_coroutine_threadsafe(...).result() -- the same "sync app,
async subsystem in its own thread" pattern already used for the
bundled LCM subprocess and the memory-scan loop.
"""

import asyncio
import threading
from contextlib import AsyncExitStack
from typing import Optional

from mcp import ClientSession, StdioServerParameters, stdio_client


class MCPManager:
    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._sessions = {}       # server_name -> ClientSession
        self._exit_stacks = {}    # server_name -> AsyncExitStack (keeps the connection alive)
        self._tools_cache = {}    # server_name -> list[mcp.Tool]
        self._errors = {}         # server_name -> last connection error, if any
        self._lock = threading.Lock()

    def start(self):
        """Starts the background event loop thread. Call once at
        Athena startup, same as the bundled LCM subprocess."""
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
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def connect_server(self, name: str, command: str, args: list):
        """Connects to one MCP server by name + launch command,
        discovers its tools, and keeps the connection alive
        indefinitely. Returns the list of discovered tools."""
        return self._run_coro(self._connect_server_async(name, command, args))

    async def _connect_server_async(self, name, command, args):
        params = StdioServerParameters(command=command, args=args)
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools_result = await session.list_tools()
        except Exception as e:
            await stack.aclose()
            with self._lock:
                self._errors[name] = str(e)
            raise

        with self._lock:
            self._sessions[name] = session
            self._exit_stacks[name] = stack
            self._tools_cache[name] = tools_result.tools
            self._errors.pop(name, None)
        return tools_result.tools

    def disconnect_server(self, name: str):
        stack = self._exit_stacks.get(name)
        if stack:
            self._run_coro(stack.aclose())
        with self._lock:
            self._sessions.pop(name, None)
            self._exit_stacks.pop(name, None)
            self._tools_cache.pop(name, None)
            self._errors.pop(name, None)

    def list_tools(self, name: str):
        return self._tools_cache.get(name, [])

    def list_servers(self):
        """Every server we've attempted to connect, whether currently
        connected or failed -- so the UI can show real status either
        way, not just successes."""
        with self._lock:
            names = set(self._sessions.keys()) | set(self._errors.keys())
            return [
                {
                    "name": n,
                    "connected": n in self._sessions,
                    "error": self._errors.get(n),
                    "tool_count": len(self._tools_cache.get(n, [])),
                }
                for n in names
            ]

    def call_tool(self, name: str, tool_name: str, arguments: dict, timeout: float = 30):
        return self._run_coro(self._call_tool_async(name, tool_name, arguments), timeout=timeout)

    async def _call_tool_async(self, name, tool_name, arguments):
        session = self._sessions.get(name)
        if not session:
            raise RuntimeError(f"MCP server '{name}' is not connected")
        return await session.call_tool(tool_name, arguments)


mcp_manager = MCPManager()
