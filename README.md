# Athena

Athena is a self-hosted AI agent workspace — a chat and coding assistant with persistent memory and a delegation system for sub-agents. It's built as a single FastAPI backend with an Alpine.js + Tailwind frontend, designed to run against any OpenAI-compatible inference backend — including local options like Ollama, llama.cpp, and vLLM — as well as several external API providers. The frontend is built with Vite (see `frontend/`) and its output is committed straight into `static/`, so *running* Athena still needs no Node.js at all — only *editing* the frontend does.

## Core Features

- **Persistent memory (LCM)** — conversations are stored and automatically summarized over time using a lossless context management layer, so long-running sessions don't lose earlier context even as they grow.
- **Coding harness** — read, search, and edit files within a confined workspace directory; run read-only or write-capable shell commands; LSP integration for symbol lookups and diagnostics; a lightweight RAG index for codebase search.
- **Bot delegation** — create specialist sub-agent "bots" with a restricted, read-only toolset. Delegate investigative work to a bot as an isolated, single-topic step; the bot reports its findings back without ever writing files or running commands itself.
- **Skills system** — reusable, on-demand instruction sets the agent can load progressively as needed, rather than keeping everything in the base prompt at all times.
- **Multi-provider support** — works with any OpenAI-compatible inference backend (Ollama, llama.cpp, vLLM, and similar) as well as several external API providers.
- **Authentication** — single-user login with optional TOTP two-factor authentication.

## Requirements

- Python 3.11+
- An OpenAI-compatible inference backend running locally, if you want local models — e.g. [Ollama](https://ollama.com), [llama.cpp](https://github.com/ggml-org/llama.cpp), or [vLLM](https://github.com/vllm-project/vllm). Athena also works against external API providers without any local backend at all.
- Node.js (only if you're modifying the frontend — see `frontend/`; the app itself ships with a pre-built `static/`, so running it needs no Node.js)

## Installation

```bash
git clone https://github.com/OhPorty/Athena-AI-Workspace.git
cd Athena-AI-Workspace
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

On first run, Athena will prompt you to set up a username and password. Two-factor authentication can be enabled afterward from the settings menu.

Model endpoints, workspace paths, and other runtime settings are configured through the app's own settings UI after first launch — see `requirements.txt` and the settings panel for the full set of configurable options.

## Running

```bash
python3 main.py
```

By default the app serves on the port configured in your settings. For always-on use, running it as a systemd service (or an equivalent process manager) behind a reverse proxy for HTTPS is recommended.

## License

MIT License — see [LICENSE](LICENSE) for details.
