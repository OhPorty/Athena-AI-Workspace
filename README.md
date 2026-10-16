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

## Safety and recovery

Athena's tools run commands inside a sandbox container (build it once with `docker build -t athena-sandbox:latest sandbox/`). Anything that deletes files asks you first, in plain language, every time. Before a turn changes a workspace, Athena saves a snapshot of it, and the chat offers **Undo this turn**. Her own settings, passwords, databases and logs are never available to the model's tools, even if you point a workspace at Athena's own folder (which lets her edit herself).

If an edit ever stops Athena from starting, restore the last snapshot without needing her:

```bash
python3 recover.py            # list the latest snapshots of Athena's own folder
python3 recover.py latest     # go back to the newest one
```

## Plans and small models

Before doing anything beyond conversation, Athena writes a short plan and waits for you to approve it. If you decline she asks what to change and plans again. After approval she works one step at a time, in order, and ticks each off as she goes. This is always on for small models (they do far better on small steps) and can be turned on for every model under **Settings → Defaults**. Scheduled and background runs have nobody to approve, so they skip it.

## Accounts

The account you create at first start is the **main** account. In **Settings → Accounts** (visible only to it) it can add more.

- Every account has its own chats, memory, notes, research, tasks, bots, rooms, pins, ratings and push devices, and its own theme, default model and workspace. Local models, MCP servers and skills are shared.
- Passwords you choose (first setup, changing it) need 10+ characters with upper and lower case, a number and a symbol, nothing common like "password" or 12345, and not your username. The same applies to every account.
- A new account always starts with a **temporary password** (typed by you, or generated), which it must change at first sign-in. Unless you tick **Reuse my authenticator**, it must also set up its own authenticator before using anything. Reusing gives it the same authenticator as yours, which is handy for accounts that are all you.
- **Full access to this computer** is a per-account choice. Without it an account can only use workspaces inside its own folder (`user_data/<name>/workspaces/`) and cannot change MCP servers or skills.
- Only the main account adds local models and endpoints. Other accounts can add online models with their own API key, which only they see.
- **One prompt at a time** (for smaller computers): while one account's prompt runs on a model on this machine, anyone who sends another is told and queued, first come first served. Prompts to online models are never held back.
- Removing an account erases its chats, memory, notes, bots and files. **Clear this account's notes and saved browser data** (Settings → Data) only ever touches the account you are signed in as.

## License

MIT License — see [LICENSE](LICENSE) for details.
