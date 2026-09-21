import os
import threading
from urllib.parse import urlparse

OLLAMA_URL = os.environ.get("ATHENA_OLLAMA_URL", "http://localhost:11434/api/chat")

_bot_host_locks = {}
_bot_host_locks_guard = threading.Lock()


def endpoint_host(endpoint_url):
    """Extract just host:port (no scheme, no path) for lock-keying --
    two bots pointing at the same host, even via different frameworks
    (Ollama vs a local OpenAI-compatible server), must never generate
    concurrently. Falls back to OLLAMA_URL's own host when a bot has
    no explicit endpoint_url, since that's genuinely the same shared
    machine every other Ollama-native bot (and Athena's own default
    chat) also targets."""
    url = endpoint_url or OLLAMA_URL
    parsed = urlparse(url)
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else (parsed.hostname or url)


def get_host_lock(host_key):
    with _bot_host_locks_guard:
        if host_key not in _bot_host_locks:
            _bot_host_locks[host_key] = threading.Lock()
        return _bot_host_locks[host_key]
