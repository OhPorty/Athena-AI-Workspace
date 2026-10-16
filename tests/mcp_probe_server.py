"""A tiny real MCP server for the tests: python tests/mcp_probe_server.py [stdio | streamable-http PORT]."""
import os, sys
from mcp.server.mcpserver import MCPServer
server = MCPServer("probe")

@server.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

@server.tool()
def whoami() -> str:
    """Report the environment variable and working directory this server was started with."""
    return f"{os.environ.get('PROBE_TOKEN', 'unset')}|{os.getcwd()}"

@server.tool()
def crash() -> str:
    """Exit the server process immediately."""
    os._exit(1)

@server.tool()
def slow(seconds: float) -> str:
    """Sleep, then answer."""
    import time; time.sleep(seconds); return "done"

@server.tool()
def info(topic: str) -> dict:
    """Return a small structured record about a topic."""
    return {"topic": topic, "letters": len(topic)}

@server.tool()
def big() -> str:
    """Return a long piece of text."""
    return "lorem " * 2000

@server.tool()
def boom() -> str:
    """Always fails."""
    raise ValueError("this tool always fails")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if mode == "stdio":
        server.run()
    else:
        server.run(transport=mode, host="127.0.0.1", port=int(sys.argv[2]))
