# Sandbox image for bash_exec: the tools its allowlist permits (BASH_EXEC_ALLOWED_COMMANDS in
# bash_tools.py) plus the libraries for producing common document and image formats.
FROM node:20-slim

# node:20-slim ships its own corepack-managed yarn stub, which checks
# the registry for "the latest" on every invocation with no local
# project pin -- removed here so a real, standalone yarn install can
# take its place instead, with no live network dependency at runtime.
RUN rm -f /usr/local/bin/yarn /usr/local/bin/yarnpkg

# Everything a model needs to produce common file formats from a script: Office documents,
# spreadsheets, slide decks, PDFs, images, charts, archives. They are preinstalled because every
# bash_exec call is a fresh container, so a `pip install` in one call would be gone by the next.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    git \
    make \
    zip \
    unzip \
    libcairo2 \
    fonts-dejavu-core \
    && npm install -g yarn \
    && pip3 install --break-system-packages --no-cache-dir \
        pytest \
        python-docx \
        openpyxl \
        XlsxWriter \
        python-pptx \
        reportlab \
        pypdf \
        pillow \
        matplotlib \
        cairosvg \
        qrcode \
        lxml \
        pyyaml \
        jinja2 \
        markdown \
    && rm -rf /var/lib/apt/lists/*

# Anything beyond that is installed on demand into the workspace (see bash_tools: PYTHONUSERBASE),
# where it persists between calls; the root filesystem itself stays read-only.
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PIP_USER=1 MPLCONFIGDIR=/tmp/mpl

# node:20-slim already ships a non-root "node" user -- reused here
# rather than creating a new one, so a compromised process inside the
# sandbox isn't root even locally, without fighting an existing UID.
USER node
WORKDIR /workspace
