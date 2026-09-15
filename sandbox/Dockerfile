# Minimal sandbox image -- only the tools bash_exec's own allowlist
# actually permits (BASH_EXEC_ALLOWED_COMMANDS in main.py). Nothing
# extra, matching the same "don't carry what you don't need" principle
# used for tool availability elsewhere in this codebase.
FROM node:20-slim

# node:20-slim ships its own corepack-managed yarn stub, which checks
# the registry for "the latest" on every invocation with no local
# project pin -- removed here so a real, standalone yarn install can
# take its place instead, with no live network dependency at runtime.
RUN rm -f /usr/local/bin/yarn /usr/local/bin/yarnpkg

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    git \
    make \
    && npm install -g yarn \
    && pip3 install --break-system-packages --no-cache-dir pytest \
    && rm -rf /var/lib/apt/lists/*

# node:20-slim already ships a non-root "node" user -- reused here
# rather than creating a new one, so a compromised process inside the
# sandbox isn't root even locally, without fighting an existing UID.
USER node
WORKDIR /workspace
