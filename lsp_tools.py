import os
import time
import atexit
import threading

# Long-lived LSP process registry, mirroring the bundled-LCM subprocess
# pattern: one pyright-langserver process per workspace root, started
# lazily on first use and reused across sessions/turns rather than
# restarted per query (re-indexing a real codebase from cold is slow, and
# every session working on the same workspace should share one warm
# process the same way multiple editor windows on one project share a
# single language server). An idle sweep tears down processes nobody has
# used in a while, since a self-hosted server that runs for weeks
# shouldn't accumulate one live pyright process per workspace ever touched.
_lsp_clients = {}
_lsp_clients_lock = threading.Lock()
LSP_IDLE_TIMEOUT_SECONDS = 1800


def get_lsp_client(workspace: str):
    from lsp_client import LSPClient
    root = os.path.realpath(workspace)
    now = time.time()
    with _lsp_clients_lock:
        for key, client in list(_lsp_clients.items()):
            if now - client.last_used > LSP_IDLE_TIMEOUT_SECONDS:
                try:
                    client.shutdown()
                except Exception:
                    pass
                del _lsp_clients[key]
        client = _lsp_clients.get(root)
        if client is None:
            client = LSPClient(root)
            _lsp_clients[root] = client
        client.last_used = now
        return client


def _shutdown_lsp_clients():
    with _lsp_clients_lock:
        for client in _lsp_clients.values():
            try:
                client.shutdown()
            except Exception:
                pass
        _lsp_clients.clear()


atexit.register(_shutdown_lsp_clients)


LSP_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "find_definition",
            "description": "Find where a symbol (function, class, variable) is actually defined, using real semantic code analysis (not text search) -- follows imports and scoping the way a real editor's 'go to definition' does. Give the line where the symbol is USED (or defined), and the exact visible text of the symbol itself; no character/column counting needed. Requires an active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, relative to the workspace root."},
                    "line": {"type": "integer", "description": "1-indexed line number where the symbol appears."},
                    "symbol": {"type": "string", "description": "The exact visible text of the symbol, e.g. '_execute_readonly_bash'."},
                },
                "required": ["path", "line", "symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": "Find every real usage of a symbol across the whole workspace, using semantic analysis -- not a text/grep match, so it won't miss aliased imports or false-match unrelated identical names elsewhere. Give the line where the symbol is defined (or any usage of it) and its exact visible text; no character/column counting needed. Requires an active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, relative to the workspace root."},
                    "line": {"type": "integer", "description": "1-indexed line number where the symbol appears."},
                    "symbol": {"type": "string", "description": "The exact visible text of the symbol, e.g. '_execute_readonly_bash'."},
                },
                "required": ["path", "line", "symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_info",
            "description": "Get the real inferred type and signature of a symbol at a specific point in the code, the way hovering in a real editor would show -- more reliable than guessing from context. Give the line where the symbol appears and its exact visible text; no character/column counting needed. Requires an active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, relative to the workspace root."},
                    "line": {"type": "integer", "description": "1-indexed line number where the symbol appears."},
                    "symbol": {"type": "string", "description": "The exact visible text of the symbol, e.g. '_execute_readonly_bash'."},
                },
                "required": ["path", "line", "symbol"],
            },
        },
    },
]
