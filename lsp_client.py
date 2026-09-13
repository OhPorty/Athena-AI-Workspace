"""
Minimal JSON-RPC-over-stdio client for a Language Server Protocol
process (pyright-langserver), giving Athena's coding harness real
semantic code intelligence -- go-to-definition, find-references,
hover/type-info -- instead of only grep/sed text search.

Not a general LSP library: just enough protocol for the three
operations the tools below need. Requests are correlated by id via a
background reader thread, since LSP's stdio framing (Content-Length
-prefixed JSON messages) is asynchronous -- the server can send
notifications at any time, not only in direct reply to a request.

Deliberately does not require the model to supply an exact
character/column offset: that's a fragile, easy-to-get-subtly-wrong
input for any model, especially a smaller one. Instead each lookup
takes a 1-indexed line number and the visible symbol text, and this
client finds the exact column itself before querying pyright.
"""

import json
import os
import subprocess
import sys
import threading
import time
import signal


def _set_pdeathsig():
    # Ensure the language server dies if this Athena process dies --
    # mirrors the guarantee _start_bundled_lcm already relies on for
    # the LCM subprocess, so a killed/crashed Athena doesn't leak
    # orphaned pyright processes indefinitely.
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


class LSPClient:
    """One pyright-langserver process bound to a single workspace root."""

    def __init__(self, workspace_root: str):
        self.workspace_root = os.path.realpath(workspace_root)
        self._proc = subprocess.Popen(
            [os.path.join(os.path.dirname(sys.executable), "pyright-langserver"), "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self.workspace_root,
            preexec_fn=_set_pdeathsig,
        )
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending = {}
        self._opened_files = set()
        self.last_used = time.time()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        self._initialize()

    def _write(self, obj):
        body = json.dumps(obj).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + body)
        self._proc.stdin.flush()

    def _read_loop(self):
        stdout = self._proc.stdout
        while True:
            line = stdout.readline()
            if not line:
                return
            if not line.lower().startswith(b"content-length"):
                continue
            length = int(line.split(b":")[1].strip())
            while True:
                sep = stdout.readline()
                if sep.strip() == b"":
                    break
            body = stdout.read(length)
            try:
                msg = json.loads(body)
            except Exception:
                continue
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._pending:
                entry = self._pending[msg_id]
                entry["result"] = msg.get("result")
                entry["error"] = msg.get("error")
                entry["event"].set()

    def _request(self, method, params, timeout=20):
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            event = threading.Event()
            self._pending[req_id] = {"event": event, "result": None, "error": None}
        self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        got = event.wait(timeout)
        entry = self._pending.pop(req_id, None)
        if not got:
            return None, {"message": f"Timed out waiting for {method}"}
        return entry["result"], entry["error"]

    def _notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _initialize(self):
        result, error = self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": "file://" + self.workspace_root,
            "capabilities": {},
        }, timeout=30)
        self._notify("initialized", {})
        return result, error

    def _uri_for(self, rel_path):
        return "file://" + os.path.realpath(os.path.join(self.workspace_root, rel_path))

    def _ensure_open(self, rel_path):
        if rel_path in self._opened_files:
            return None
        abs_path = os.path.realpath(os.path.join(self.workspace_root, rel_path))
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            return {"error": f"Could not read '{rel_path}': {e}"}
        self._notify("textDocument/didOpen", {
            "textDocument": {"uri": self._uri_for(rel_path), "languageId": "python", "version": 1, "text": text}
        })
        self._opened_files.add(rel_path)
        return None

    def _find_character(self, rel_path, line_1indexed, symbol):
        abs_path = os.path.realpath(os.path.join(self.workspace_root, rel_path))
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                lines = f.read().split("\n")
        except Exception as e:
            return None, {"error": f"Could not read '{rel_path}': {e}"}
        idx = line_1indexed - 1
        if idx < 0 or idx >= len(lines):
            return None, {"error": f"Line {line_1indexed} is out of range for '{rel_path}' ({len(lines)} lines)."}
        char = lines[idx].find(symbol)
        if char == -1:
            return None, {"error": f"'{symbol}' not found on line {line_1indexed} of '{rel_path}'."}
        return char, None

    def _rel_from_uri(self, uri):
        path = uri[len("file://"):] if uri.startswith("file://") else uri
        try:
            return os.path.relpath(path, self.workspace_root)
        except Exception:
            return path

    def _flatten_locations(self, result):
        if not result:
            return []
        if isinstance(result, dict):
            result = [result]
        out = []
        for item in result:
            uri = item.get("uri") or item.get("targetUri")
            rng = item.get("range") or item.get("targetSelectionRange") or item.get("targetRange")
            if not uri or not rng:
                continue
            out.append({"path": self._rel_from_uri(uri), "line": rng["start"]["line"] + 1})
        return out

    def find_definition(self, rel_path, line_1indexed, symbol):
        err = self._ensure_open(rel_path)
        if err:
            return err
        char, err = self._find_character(rel_path, line_1indexed, symbol)
        if err:
            return err
        result, error = self._request("textDocument/definition", {
            "textDocument": {"uri": self._uri_for(rel_path)},
            "position": {"line": line_1indexed - 1, "character": char},
        })
        if error:
            return {"error": error.get("message", "definition lookup failed")}
        locations = self._flatten_locations(result)
        if not locations:
            return {"error": f"No definition found for '{symbol}'."}
        return {"definitions": locations, "count": len(locations)}

    def find_references(self, rel_path, line_1indexed, symbol):
        err = self._ensure_open(rel_path)
        if err:
            return err
        char, err = self._find_character(rel_path, line_1indexed, symbol)
        if err:
            return err
        result, error = self._request("textDocument/references", {
            "textDocument": {"uri": self._uri_for(rel_path)},
            "position": {"line": line_1indexed - 1, "character": char},
            "context": {"includeDeclaration": True},
        })
        if error:
            return {"error": error.get("message", "references lookup failed")}
        locations = self._flatten_locations(result)
        return {"references": locations, "count": len(locations)}

    def type_info(self, rel_path, line_1indexed, symbol):
        err = self._ensure_open(rel_path)
        if err:
            return err
        char, err = self._find_character(rel_path, line_1indexed, symbol)
        if err:
            return err
        result, error = self._request("textDocument/hover", {
            "textDocument": {"uri": self._uri_for(rel_path)},
            "position": {"line": line_1indexed - 1, "character": char},
        })
        if error:
            return {"error": error.get("message", "hover lookup failed")}
        if not result or not result.get("contents"):
            return {"error": f"No type info available for '{symbol}'."}
        contents = result["contents"]
        if isinstance(contents, dict):
            text = contents.get("value", "")
        elif isinstance(contents, list):
            text = "\n".join(c.get("value", c) if isinstance(c, dict) else c for c in contents)
        else:
            text = str(contents)
        text = text.replace("```python", "").replace("```", "").strip()
        return {"symbol": symbol, "info": text}

    def shutdown(self):
        try:
            self._request("shutdown", {}, timeout=5)
            self._notify("exit", {})
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
