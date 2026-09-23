"""
Athena — a lean, LCM-backed chat/agent workspace with voice support.
"""
import os
import re
import subprocess
import sqlite3
import json
import sys
import time
import threading
import hashlib
import queue
from typing import Optional, List
import uuid
import httpx
import psutil
import argparse
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from rag import SimpleCodeRAG
import rag
import settings
import task_context
import activity
import generation_streaming
import lcm_client
import voice
import lsp_tools
import skills
import annotations
import auth_routes
import tool_output
import bash_tools
import backup_tools
import file_tools
import web_tools
import host_locks
import bots
import rooms
import delegation_jobs
import task_scheduler
from logging_setup import logger, log_trace_event
from host_locks import OLLAMA_URL
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# A flat timeout=180 applies that value to connect/read/write/pool alike,
# which was too short on its read side: prompt evaluation on a large model
# with a large, accumulated context (the common case in a long
# athena_delegation turn) can genuinely take longer than 180s on its own,
# with zero bytes sent back until it's done -- httpx's read timeout has no
# way to distinguish that from a truly dead connection, so it fires either
# way. Split so a dead/unreachable server is still caught fast (short
# connect timeout) while a slow-but-alive one gets real patience on the
# read side -- these are different failure modes and shouldn't share one
# number. Two separate read budgets, not one shared value: a bot (sub
# agent) round is one bounded, single-topic investigation step, while
# Athena's own turn can legitimately run long chains of rounds on old/
# slow hardware -- she needs real patience, a bot needs a real ceiling.
_SUB_AGENT_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)      # 10 minutes -- one bot round
_MAIN_AGENT_TIMEOUT = httpx.Timeout(connect=10.0, read=3600.0, write=30.0, pool=10.0)    # 1 hour -- Athena's own turn
DEFAULT_MODEL = os.environ.get("ATHENA_DEFAULT_MODEL", "gpt-oss-20b-32k:latest")

app = FastAPI(title="Athena")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(generation_streaming.router)
app.include_router(voice.router)
app.include_router(skills.router)
app.include_router(annotations.router)
app.include_router(auth_routes.router)
app.include_router(file_tools.router)
app.include_router(bots.router)
app.include_router(rooms.router)
app.include_router(task_scheduler.router)
app.add_middleware(auth_routes.AuthMiddleware)

BASE_SYSTEM_PROMPT = (
    "You are Athena, a helpful assistant. Be direct and concise."
)

# Casual-chat system-prompt suffix, built the same named-section way as
# CODING_HARNESS_SECTIONS below -- its own independent registry, its own
# wording. Deliberately NOT shared text with the coding-harness sections:
# casual and coding-harness mode are separate layers by design (DeepSeek-
# Harness-style -- a stable per-mode prompt, not one prompt with mode-
# conditional bits spliced together), so each layer gets tuned on its own
# terms rather than collapsed into a single shared block. This used to be
# one 80-line unbroken paragraph mixing everything below into a single
# run-on block with no structure between concerns -- splitting it out
# doesn't change what it says, just how legible it is to edit later.
CASUAL_AGENT_SECTIONS = [
    ("casual_tool_use",
     "You have access to tools listed below when relevant -- use them rather than guessing "
     "when you need real information."),

    ("plan_first",
     "## When a question needs more than a couple of lookups\n"
     "If answering something is genuinely going to take more than a couple of tool calls -- "
     "several searches, a fetch followed by more fetches to track down the right source, "
     "cross-checking more than one thing -- call write_plan first with the short ordered list "
     "of what you're actually going to check, before diving in. This is a real requirement, "
     "not a suggestion: if you make several tool calls in a row without ever calling "
     "write_plan, every tool except write_plan disappears until you call it. It does not cap "
     "how much you can look up afterward -- once you've written a plan, your full tool access "
     "returns and stays available. A simple one-lookup question never needs this at all."),

    ("formatting",
     "## Formatting structured data\n"
     "When presenting structured or tabular data (schedules, forecasts, comparisons, lists "
     "of items with multiple fields each), format it as a real markdown table with | column | "
     "headers | -- not a run-on list of colon-separated values."),

    ("research_discipline",
     "## Researching something real\n"
     "When you are asked to research a specific, named software library, SDK, or API -- not "
     "a general topic -- do not rely on a broad web search alone: that returns scattered blog "
     "posts and tutorials of unknown age that get blended together into answers matching no "
     "real version. Instead, fetch that project's own official repository or documentation "
     "site directly (e.g. its GitHub README, its docs site) and base your answer on that "
     "source. If the library has multiple language SDKs for the same protocol or spec (e.g. "
     "Python, TypeScript, Go), never mix a convention from one language's SDK into another "
     "language's example -- python-style decorators on bare functions are not valid syntax in "
     "JavaScript or TypeScript, and vice versa; verify each language's example separately "
     "against that language's own SDK docs. For every specific API claim you state as fact -- "
     "an import path, a class name, a function signature -- name which source you got it "
     "from, so a person reading your answer can verify it themselves rather than trusting it "
     "blindly."),

    ("dependency_pinning",
     "## Pinning dependency versions\n"
     "When you generate a dependency declaration for an external library -- requirements.txt, "
     "pyproject.toml, package.json, go.mod, or any similar file -- always pin to a specific "
     "version or a bounded range (e.g. mcp>=1.28,<2), never an open-ended minimum like "
     "mcp>=1.0.0. An unbounded dependency silently resolves to whatever is newest at install "
     "time, which may be a different major version than whatever API you actually researched "
     "and wrote code against -- exactly the kind of breaking change that makes generated code "
     "fail long after you wrote it. When you research a library's current API, note which "
     "version that API belongs to, and pin your generated dependency file to that version."),

    ("explicit_write_calls",
     "## Naming the actual write, not just describing it\n"
     "When research is about building something that creates, writes, or generates files or "
     "other output, it must explicitly name the specific API, method, or function that "
     "performs that write (for example fs.writeFile, or a library's own save/create method) "
     "and show it actually being called in at least one real example -- never leave the "
     "actual creation step implied, described only in prose, or absent from every example "
     "while only reading, analyzing, or reacting to things gets demonstrated."),

    ("uncertainty",
     "## Naming uncertainty plainly\n"
     "When you are not certain whether a capability is available or restricted in some "
     "environment or API, say that uncertainty plainly -- never state a confident-sounding "
     "restriction you have not actually confirmed from a real source, since a plausible but "
     "invented rule is often followed just as strictly as a true one, and is much harder to "
     "catch."),

    ("source_fidelity",
     "## Using the real source, not a summary of it\n"
     "When a task asks you to review, verify, check, or update something against 'the "
     "documentation' or another external source, and you notice you only have a summary, a "
     "compacted account, or a secondhand description of that source in your current context "
     "-- not the source itself -- you must actually fetch the real source before proceeding. "
     "Noticing that gap in your own reasoning and then continuing anyway with the lossy "
     "version defeats the entire point of checking. This applies even when the summary comes "
     "from earlier in this same conversation, including your own memory system's compacted "
     "context, since compaction can silently drop the exact specific detail that turns out to "
     "matter most."),

    ("generated_artifacts",
     "## Artifacts built as strings\n"
     "When something is built as a string or text blob representing some OTHER artifact -- "
     "generated code in a different language, a JSON or YAML config assembled via string "
     "concatenation, an HTML template, a SQL query built by interpolation, a shell script "
     "written out as text, anything where the actual product lives inside a string rather "
     "than being written directly as its own native syntax that gets parsed and checked as "
     "such -- the correctness of the code doing the building is a separate question from the "
     "correctness of what it actually produces. Validate both independently. Code that "
     "assembles such an artifact can be flawless in its own language while the artifact it "
     "produces is still broken, since that artifact lives inside strings, invisible to normal "
     "review, rather than as code your own tooling would ever check directly."),
]
# Note: there is deliberately no dead-tool STRICT RULE here about grep -n
# vs. read_file for line-location questions -- casual mode's tool list
# never includes bash or read_file (see _get_mode_tools; only workspace/
# athena_delegation modes get bash_tools.BASH_TOOL_SCHEMAS/file_tools.FILE_TOOL_SCHEMAS), so an
# instruction to use them here was always unreachable. That guidance
# already lives, correctly scoped, in CODING_HARNESS_SECTIONS'
# tool_selection section below, where those tools actually exist.

# Coding-harness system prompt, built as named, independently
# addressable sections assembled in order at request time -- not one
# flat string. A future mode can select a different subset or order
# without touching the sections it doesn't use, and a mode can splice
# in its own extra section (see _assemble_prompt_sections) the same
# way a workspace-bound delegation session layers BOT_DELEGATION
# on top of the same base sections rather than duplicating them.
CODING_HARNESS_SECTIONS = [
    ("identity",
     "You are Athena, in agentic coding mode. A real workspace is bound to this session: "
     "you can read, search, and modify real files, and run real commands. Relevant memory "
     "has already been retrieved and provided as context below -- treat it as established "
     "fact about this person and their work, not something to re-ask for or re-derive."),

    ("plan_before_acting",
     "## Before you act\n"
     "For anything touching more than one file, or more than a couple of lines, or requiring "
     "several tool calls to even figure out the shape of the problem: call write_plan first "
     "with the short ordered list of what you're actually going to do. A trivial one-line fix "
     "doesn't need one. This is enforced, not just advised: if you make several tool calls in "
     "a row without ever calling write_plan, every other tool disappears until you call it -- "
     "so don't be surprised if bash/read_file/etc. suddenly aren't in your tool list anymore; "
     "that's this gate, not an error. It never reduces how much you can actually do -- once "
     "you've written a plan, full tool access returns and stays available for the rest of the "
     "turn. A plan can be wrong once you see real file contents -- call update_plan_step to "
     "mark a step done, failed, or in progress as you go, and call write_plan again if the "
     "whole approach needs to change -- but skipping it and improvising edit-by-edit is how "
     "partial, inconsistent changes happen."),

    ("grounded_claims",
     "## Ground every claim in a real tool result\n"
     "Only a completed tool call establishes a fact about this codebase. Never describe a "
     "file as read, checked, or changed unless a real tool call for that action is actually "
     "present in this conversation. If you're not certain, say so and make the call -- "
     "don't narrate an action you haven't taken."),

    ("loop_signals",
     "## If you see a blocked or interrupted message\n"
     "This harness enforces two things structurally, not just by asking: it will refuse to "
     "repeat an identical tool call, and it will cut off detected repetition in your own "
     "output. Neither is an error to route around. Both mean the same thing: stop, and "
     "either state your actual conclusion from what you already have, or say plainly that "
     "you don't have one yet."),

    ("tool_selection",
     "## Picking the right tool\n"
     "Use grep/sed to locate something by line -- never read_file for that, since it "
     "returns no line numbers. Before reading or grepping any file yourself for a "
     "codebase question, call search_codebase first -- this is a default, not an optional "
     "step for unfamiliar territory. Skipping straight to read_file/bash on a guess wastes "
     "context searching by hand for something search_codebase would have pointed to in one "
     "call. Treat its results as a pointer, not ground truth -- read the real file before "
     "editing, but let search_codebase tell you which file and roughly where first. Use "
     "find_definition/find_references/type_info for real semantic questions about a symbol, "
     "not text search. Use backup_file before an edit whose current state matters and isn't "
     "already in version control this turn -- never a hand-rolled copy."),

    ("research_discipline",
     "## Researching something real\n"
     "For a specific named library, SDK, or API: don't rely on general web search alone -- "
     "fetch that project's own docs or repo directly. If it has SDKs for multiple languages, "
     "verify each language's example against that language's own docs rather than carrying a "
     "convention from one language's SDK into another's syntax. State which source backs any "
     "specific claim (an import path, a function signature). Pin dependency versions "
     "explicitly rather than leaving them open-ended.\n\n"
     "Use tools for real information. State uncertainty plainly rather than presenting a "
     "guess as settled fact."),
]


def _assemble_prompt_sections(sections, section_ids, extra_sections=None):
    """Build a system prompt from named sections, in the order given by
    section_ids, looked up from the given sections registry (a list of
    (id, text) tuples -- CODING_HARNESS_SECTIONS or CASUAL_AGENT_SECTIONS
    today). extra_sections lets a caller splice in one-off section text
    (keyed by id) without it needing to live in either registry above."""
    lookup = dict(sections)
    if extra_sections:
        lookup.update(extra_sections)
    return "\n\n".join(lookup[sid] for sid in section_ids if sid in lookup)


# Default: every section, in registry order -- kept under this same
# name so every existing reference to the coding-harness prompt keeps
# working unchanged while the underlying mechanism stays genuinely
# modular rather than one flat string.
CODING_HARNESS_SYSTEM_PROMPT = _assemble_prompt_sections(CODING_HARNESS_SECTIONS, [sid for sid, _ in CODING_HARNESS_SECTIONS])

# Same mechanism, casual mode's own independent registry (see
# CASUAL_AGENT_SECTIONS above). Leading "\n\n" so BASE_SYSTEM_PROMPT +
# AGENT_SYSTEM_SUFFIX (no separator at that call site, in
# _get_mode_system_prompt) still reads as two cleanly separated blocks,
# the same way _get_bot_delegation_prompt_section already leads with its
# own "\n\n" when layered onto CODING_HARNESS_SYSTEM_PROMPT.
AGENT_SYSTEM_SUFFIX = "\n\n" + _assemble_prompt_sections(CASUAL_AGENT_SECTIONS, [sid for sid, _ in CASUAL_AGENT_SECTIONS])


_SEARCH_STOPWORDS = {"the", "a", "an", "of", "for", "and", "or", "in", "on", "to", "is", "are", "what", "how", "does", "do", "with", "vs", "current"}


def _light_stem(word):
    """Strip common suffixes so morphological variants of the same
    root word (generation/generating, models/model) compare equal --
    deliberately crude, not a real stemmer, just enough to stop
    obvious variants from being treated as unrelated words."""
    for suffix in ("ions", "ion", "ing", "ies", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _search_query_similarity(a, b):
    """Word-overlap similarity between two search queries, ignoring
    common stopwords and light stemming -- catches a search that's
    been reworded but is still on the same topic, which exact
    tool-call fingerprint matching misses entirely since a model can
    dodge that block by just rephrasing the query."""
    words_a = set(_light_stem(w) for w in re.findall(r"[a-z0-9]+", a.lower()) if w not in _SEARCH_STOPWORDS)
    words_b = set(_light_stem(w) for w in re.findall(r"[a-z0-9]+", b.lower()) if w not in _SEARCH_STOPWORDS)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


def _resolve_mode_and_workspace(req):
    """Explicit, named mode resolution -- not inferred piecemeal from
    workspace truthiness scattered across several call sites. Athena's
    own delegation session (bots.ATHENA_BOTS_SESSION_ID) always runs in
    real coding-harness mode: she was designed as full workspace
    capability with delegation layered on top, never a lesser casual-
    chat base, so her own request's workspace field (which her
    frontend never actually populates) falls back to the configured
    default workspace instead of silently downgrading her to casual
    chat -- which is what was actually happening before this existed."""
    if req.session_id == bots.ATHENA_BOTS_SESSION_ID:
        workspace = req.workspace or settings.load_settings().get("workspace") or ""
        return "athena_delegation", workspace
    if req.workspace:
        return "workspace", req.workspace
    return "casual", ""


def _get_mode_system_prompt(mode, req):
    """The stable identity and rules only -- memory, skills, and
    pinned-message context are deliberately NOT concatenated in here.
    They're dynamic, situational material that changes every request,
    kept as a separate context message instead (see
    _get_dynamic_context_message) rather than fused into the same
    block as the model's actual operating instructions, the same
    distinction DeepSeek Harness draws between a stable system prompt
    and dynamically-sourced runtime context."""
    if mode in ("workspace", "athena_delegation"):
        base = CODING_HARNESS_SYSTEM_PROMPT
    else:
        base = BASE_SYSTEM_PROMPT + AGENT_SYSTEM_SUFFIX
    if mode == "athena_delegation":
        base = base + bots._get_bot_delegation_prompt_section(req.workspace, async_enabled=bool(settings.load_settings().get("delegation_async_enabled")), laya_gating_enabled=bool(settings.load_settings().get("laya_gating_enabled")))
    return base


def _get_dynamic_context_message(req):
    """Memory, skills, and pinned-message context, assembled as their
    own separate message rather than string-concatenated onto the
    system prompt. Returns None when there's genuinely nothing to
    include, so the caller can skip adding an empty message."""
    content = _get_memory_context() + skills.get_skills_context(req.allowed_skills) + annotations.get_pinned_context(req.session_id)
    if not content.strip():
        return None
    return {"role": "user", "content": content}


# Blind-exploration tools that require search_codebase to have been
# called at least once this turn first -- the same "orient before you
# dig" discipline already asked for in prose (CODING_HARNESS_SECTIONS'
# tool_selection section) but enforced structurally: a model can drift
# off a prompt instruction under pressure, it can't call a tool that
# isn't in its tool list.
_SEARCH_GATED_TOOL_NAMES = {"bash", "read_file"}

# The full set of self-investigation tools, cut off once a turn has
# leaned on them past the threshold below AND no delegation has
# happened yet this turn -- narrowing what's reachable down to
# delegation (plus whatever write tools were already available) rather
# than asking the model to notice it's over-exploring and delegate on
# its own judgment. Once she's delegated at least once this turn, the
# cutoff no longer applies: initial research should generally go
# through sub-agents (this still forces that), but she isn't locked out
# of her own follow-up digging on top of real delegation findings --
# e.g. confirming something a bot's scratch note pointed to.
_EXPLORATION_TOOL_NAMES = {
    "bash", "read_file", "list_files", "search_codebase",
    "find_definition", "find_references", "type_info",
}
_DELEGATION_TOOL_NAMES = {"run_delegation_step", "plan_delegation"}
_SELF_INVESTIGATION_TOOL_THRESHOLD = 6

# Below this many total tool calls in one turn (any tool, any mode), a
# turn is small enough not to need an explicit plan. At or past it,
# _get_mode_tools narrows the tool list down to write_plan alone until
# it's called -- a real structural checkpoint, not a prose reminder,
# replacing what used to be enforced only by CODING_HARNESS_SECTIONS'
# plan_before_acting sentence. Deliberately mode-independent (unlike
# _SELF_INVESTIGATION_TOOL_THRESHOLD, which only applies in
# athena_delegation): the observed failure mode -- 23 tool calls
# rediscovering a fixed weather API endpoint -- happened in plain casual
# chat, which had no per-turn structure of any kind before this existed.
# This is NOT a cap on how much work can happen -- once write_plan has
# been called, the gate never re-applies for the rest of the turn, so
# total tool-call volume is unaffected; it only forces one checkpoint.
_PLAN_REQUIRED_THRESHOLD = 3


def _check_tool_gate(mode, name, pre_round_called, pre_round_exploration_count, pre_round_total_count):
    """The four structural tool-availability gates, checked against a
    snapshot of state as of the START of the current round (not the
    live-updating turn totals) -- so a tool unlocked by a call earlier
    THIS round (e.g. search_codebase and bash requested in the same
    parallel tool-call batch) still can't be used until the round after,
    matching the sequencing the old schema-hiding approach enforced.
    Returns an error dict if `name` is currently gated, else None. A
    standalone function (not inlined in chat_stream's _execute_tool_call)
    so it's independently testable without spinning up a real turn."""
    if mode in ("workspace", "athena_delegation") and name in _SEARCH_GATED_TOOL_NAMES and "search_codebase" not in pre_round_called:
        return {"error": f"BLOCKED: '{name}' is not available yet this turn -- call search_codebase first before using bash or read_file directly, so you're working from search results rather than blind exploration."}
    if (mode == "athena_delegation" and name in _EXPLORATION_TOOL_NAMES
            and pre_round_exploration_count >= _SELF_INVESTIGATION_TOOL_THRESHOLD
            and not (pre_round_called & _DELEGATION_TOOL_NAMES)):
        return {"error": (
            "BLOCKED: you've made enough self-investigation tool calls this turn that direct exploration "
            "(bash, read_file, list_files, search_codebase, find_definition, find_references, type_info) is "
            "no longer available for the rest of this turn -- only delegation (run_delegation_step/"
            "plan_delegation), any write tools, and your existing findings so far remain. Use plan_delegation "
            "or run_delegation_step to get any further investigation done instead of continuing to look yourself."
        )}
    if mode == "athena_delegation" and name == "run_delegation_step" and "plan_delegation" in pre_round_called:
        return {"error": (
            "BLOCKED: you called plan_delegation this turn, so run_delegation_step is no longer available for "
            "the rest of this turn -- that commitment is permanent once made. If a step was rejected or came "
            "back incomplete, do not work around it with a one-off run_delegation_step call: fix the step (or "
            "steps) and call plan_delegation again with the corrected list."
        )}
    if pre_round_total_count >= _PLAN_REQUIRED_THRESHOLD and "write_plan" not in pre_round_called and name != "write_plan":
        return {"error": (
            "BLOCKED: you've made several tool calls this turn without writing a plan. Every tool except "
            "write_plan is now unavailable until you call it. This does not limit how much you can do for the "
            "rest of this turn -- once you call write_plan with the ordered steps you're actually going to "
            "take, full tool access returns and stays available. Call write_plan now."
        )}
    return None


def _get_mode_tools(mode, req, called_tool_names=None, exploration_call_count=0, total_tool_call_count=0):
    """Casual chat no longer carries bash/backup_file -- a mode only
    gets tools it actually needs, matching the same principle behind
    bots never getting write tools at the registration level rather
    than being told not to use them.

    called_tool_names/exploration_call_count/total_tool_call_count let a
    caller recompute this per round within one turn (see chat_stream's
    generation loop) so tool availability can shift as the model's own
    actions this turn accumulate, instead of being fixed once at request
    start."""
    tools = lcm_client.get_lcm_tools() + PLAN_TOOL_SCHEMAS
    if req.search_url:
        tools = tools + web_tools.WEB_TOOL_SCHEMAS
    if mode in ("workspace", "athena_delegation"):
        tools = tools + bash_tools.BASH_TOOL_SCHEMAS + backup_tools.BACKUP_TOOL_SCHEMAS + RAG_TOOL_SCHEMAS + file_tools.FILE_TOOL_SCHEMAS + lsp_tools.LSP_TOOL_SCHEMAS + tool_output.CONTEXT_TOOL_SCHEMAS
    if skills.scan_skills():
        tools = tools + skills.SKILL_TOOL_SCHEMAS
    if mode == "athena_delegation":
        tools = tools + bots._bot_delegation_tool_schemas_for_mode(bool(settings.load_settings().get("delegation_async_enabled")))
    called = called_tool_names or set()
    if mode in ("workspace", "athena_delegation"):
        if "search_codebase" not in called:
            tools = [t for t in tools if t.get("function", {}).get("name") not in _SEARCH_GATED_TOOL_NAMES]
        if (mode == "athena_delegation" and exploration_call_count >= _SELF_INVESTIGATION_TOOL_THRESHOLD
                and not (called & _DELEGATION_TOOL_NAMES)):
            tools = [t for t in tools if t.get("function", {}).get("name") not in _EXPLORATION_TOOL_NAMES]
        if mode == "athena_delegation" and "plan_delegation" in called:
            # Hard commitment: once a plan has been submitted, any
            # correction (a rejected step, an incomplete result) must go
            # through resubmitting plan_delegation, never an ad hoc
            # one-off run_delegation_step call. This is the structural
            # fix for the observed failure mode where a rejected
            # plan_delegation step led to abandoning the plan entirely
            # in favor of reworded, one-at-a-time retries.
            tools = [t for t in tools if t.get("function", {}).get("name") != "run_delegation_step"]
    if total_tool_call_count >= _PLAN_REQUIRED_THRESHOLD and "write_plan" not in called:
        tools = [t for t in tools if t.get("function", {}).get("name") == "write_plan"]
    if req.allowed_tools is not None:
        _allowed = set(req.allowed_tools)
        tools = [t for t in tools if t.get("function", {}).get("name") in _allowed]
    return tools


# Direct port of Odysseus's proven _pick_dynamic_ctx formula and step
# list (src/llm_core.py) -- same steps, same len(text)//3 + 16000
# headroom, same ceiling default. Not reinvented -- Odysseus's version
# is confirmed working in production; this mirrors it exactly rather
# than trusting a from-scratch reimplementation that kept landing at
# the wrong number in testing.
_CTX_STEPS = [4096, 16384, 32768, 65536, 131072]
MAX_CTX_DEFAULT = int(os.environ.get("ATHENA_MAX_CTX", "65536"))  # matches Odysseus's OLLAMA_NUM_CTX_OVERRIDE

def estimate_tokens(messages: list, tools: list = None) -> int:
    total_chars = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            total_chars += len(content)
    if tools:
        total_chars += len(json.dumps(tools))
    return max(1, total_chars // 4)

def pick_dynamic_ctx(messages: list, tools: list = None, max_ctx: int = MAX_CTX_DEFAULT) -> int:
    combined_text = ""
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            combined_text += content
    if tools:
        combined_text += json.dumps(tools)
    needed = len(combined_text) // 3 + 48000
    for step in _CTX_STEPS:
        if step >= needed and step <= max_ctx:
            return step
    return max_ctx


class ChatIn(BaseModel):
    session_id: str
    message: str
    model: str = DEFAULT_MODEL
    max_ctx: int = 0  # 0 = dynamic sizing; any other value is an explicit override
    workspace: str = ""  # empty = no workspace bound; file tools stay disabled
    endpoint_url: str = ""  # empty = use the default OLLAMA_URL
    search_url: str = ""  # SearXNG base URL; empty = web_search/web_fetch tools unavailable
    images: list = []  # base64-encoded image strings (no data: prefix), passed through to vision-capable models
    attachments: list = []  # [{"name": str, "content": str}] text-file attachments, inlined into the prompt
    allowed_tools: Optional[List[str]] = None  # None = no restriction (normal chat); a task run passes its own explicit allowlist
    allowed_skills: Optional[List[str]] = None  # same, for which skills load_skill can actually load
    provider: str = ""  # "" = Ollama-native (default); "openai"/"openrouter"/"custom"/"anthropic"/"google" route through an adapter in _stream_completion
    api_key: str = ""  # only used when provider is set
    think: str = ""  # ""=don't ask (today's behavior); "none"/"low"/"medium"/"high"/"max" -- Ollama-native only for now, see _stream_completion


def _search_codebase(query: str, limit: int = 3):
    """Tool-callable RAG search over the indexed codebase, returning
    structured results the model can inspect and, if needed, refine
    with a follow-up query -- rather than a fixed block of context
    passively injected every single turn regardless of whether it's
    actually needed or matches what the model is currently after."""
    if not query:
        return {"error": "query is required"}
    try:
        limit = max(1, min(int(limit), 10))
    except (TypeError, ValueError):
        limit = 3
    try:
        results = rag.search(query, limit=limit)
    except RuntimeError as e:
        # The embedding model failed to load (or is still loading for the
        # first time and hasn't finished) -- surface a clear, structured
        # error instead of letting this propagate up out of tool dispatch.
        return {"error": f"search_codebase is temporarily unavailable: {e}"}
    if not results:
        return {"query": query, "results": [], "note": "No matches found -- try different or broader search terms."}
    return {
        "query": query,
        "results": [
            {"filepath": r["filepath"], "content": r["content"][:3000], "score": r["score"]}
            for r in results
        ],
    }


# The bot-delegation tool cluster used to be gated only inside chat_stream's
# own closure-based dispatcher -- bots.py's and rooms.py's dispatchers reach
# several of these same names too, but without the ATHENA_BOTS_SESSION_ID
# restriction, since that restriction only ever meant "a regular user chat
# session can't use these directly," not "no non-Athena-chat caller ever
# can." ToolContext.require_athena_bots_session below is what preserves
# that distinction with one shared table instead of three separate ones.
_BOT_DELEGATION_CLUSTER_NAMES = {
    "list_bots", "draft_bot_prompt", "message_bot", "list_rooms", "read_room_messages",
    "message_room", "create_bot", "update_bot", "run_delegation_step", "read_scratch_file",
    "read_tool_call_history", "plan_delegation",
}


class ToolContext:
    """Shared per-call context for dispatch_tool below, built differently
    by each of the three call sites (chat_stream's own dispatch, bot
    delegation, Athena's room-turn loop) from whatever fields that caller
    actually has. A plain class, not pydantic -- this is internal,
    per-tool-call hot-path state, never parsed from external input, so
    validation overhead isn't worth paying on every single tool call."""
    def __init__(self, session_id="", workspace="", search_url="", bot=None, session_key=None,
                 req=None, plan_state=None, passthrough_session_id=None,
                 require_athena_bots_session=False,
                 unknown_tool_message_template="'{name}' is not a tool available in this context."):
        self.session_id = session_id
        self.workspace = workspace
        self.search_url = search_url
        self.bot = bot
        self.session_key = session_key
        self.req = req
        self.plan_state = plan_state  # None unless the caller supports write_plan (only chat_stream does)
        self.passthrough_session_id = passthrough_session_id  # None disables the generic LCM tools/call fallback entirely
        self.require_athena_bots_session = require_athena_bots_session
        self.unknown_tool_message_template = unknown_tool_message_template


def _render_plan_state(plan_state):
    if not plan_state:
        return {"plan": [], "note": "No plan recorded yet -- call write_plan first."}
    lines = []
    for i, step in enumerate(plan_state):
        marker = {"done": "[x]", "failed": "[!]", "in_progress": "[~]"}.get(step["status"], "[ ]")
        note = f" -- {step['note']}" if step.get("note") else ""
        lines.append(f"{i}. {marker} {step['text']}{note}")
    return {"plan": "\n".join(lines)}


def dispatch_tool(name, args, ctx, allowed_names=None):
    """Single shared tool dispatcher -- replaces what used to be three
    independently hand-written copies (chat_stream's own closure,
    bots._execute_bot_tool_call, rooms._dispatch_athena_room_tool_call),
    which had already started drifting apart (the room one covered a
    narrower, separately-maintained set). allowed_names is the calling
    loop's own permission scope: None means the full table, used by
    chat_stream since _get_mode_tools already controls what the model can
    even see -- this is a defensive second layer there, not the primary
    gate. bots.py and rooms.py pass their own real allow-sets, since this
    IS the primary enforcement for them."""
    if allowed_names is not None and name not in allowed_names:
        return {"error": ctx.unknown_tool_message_template.format(name=name)}

    if name == "web_search":
        if not ctx.search_url:
            return {"error": "No search engine is configured (search_url is empty in Athena's settings) -- web_search is unavailable until one is set."}
        return web_tools.web_search(ctx.search_url, args.get("query", ""))
    if name == "web_fetch":
        return web_tools.web_fetch(args.get("url", ""), args.get("offset", 0))
    if name == "load_skill":
        return skills.load_skill(args.get("name", ""))
    if name == "bash":
        return bash_tools.execute_readonly_bash(args.get("command", ""), args.get("args", []), ctx.workspace)
    if name == "bash_exec":
        return bash_tools.execute_write_bash(args.get("command", ""), args.get("args", []), ctx.workspace)
    if name == "bash_exec_start":
        return bash_tools.start_background_bash(args.get("command", ""), args.get("args", []), ctx.workspace)
    if name == "bash_exec_check":
        return bash_tools.check_background_bash(args.get("process_id", ""))
    if name == "bash_exec_stop":
        return bash_tools.stop_background_bash(args.get("process_id", ""), args.get("force", False))
    if name == "get_full_tool_output":
        return tool_output.get_full_tool_output_tool(args.get("output_id", ""))
    if name == "find_definition":
        return lsp_tools.get_lsp_client(ctx.workspace).find_definition(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
    if name == "find_references":
        return lsp_tools.get_lsp_client(ctx.workspace).find_references(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
    if name == "type_info":
        return lsp_tools.get_lsp_client(ctx.workspace).type_info(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
    if name == "list_files":
        return file_tools.list_files(ctx.workspace, args.get("path", "."))
    if name == "read_file":
        return file_tools.read_file(ctx.workspace, args.get("path", ""), args.get("offset", 0))
    if name == "write_file":
        return file_tools.write_file(ctx.workspace, args.get("path", ""), args.get("content", ""))
    if name == "edit_file":
        return file_tools.edit_file(ctx.workspace, args.get("path", ""), args.get("old_text", ""), args.get("new_text", ""))
    if name == "replace_lines":
        return file_tools.replace_lines(ctx.workspace, args.get("path", ""), args.get("start_line"), args.get("end_line"), args.get("new_content", ""), args.get("expected_content", ""))
    if name == "backup_file":
        return backup_tools.backup_file(args.get("path", ""))
    if name == "restore_file":
        return backup_tools.restore_file(args.get("path", ""), args.get("snapshot_id", ""), args.get("restore_hash", ""), args.get("restore_timestamp"))

    if name in _BOT_DELEGATION_CLUSTER_NAMES:
        if ctx.require_athena_bots_session and ctx.session_id != bots.ATHENA_BOTS_SESSION_ID:
            return {"error": "Bot delegation tools are only available in the dedicated Athena Bots session."}
        if name == "list_bots":
            return bots._list_bots_tool()
        if name == "draft_bot_prompt":
            return bots._draft_bot_prompt(args.get("name", ""), args.get("job_scope", ""), args.get("tools", []), args.get("additional_constraints"))
        if name == "message_bot":
            return bots._message_bot_tool(args.get("bot_id"), args.get("content", ""))
        if name == "list_rooms":
            return rooms._list_rooms_tool()
        if name == "read_room_messages":
            return rooms._read_room_messages_tool(args.get("room_id"))
        if name == "message_room":
            return rooms._message_room_tool(args.get("room_id"), args.get("content", ""))
        if name == "create_bot":
            return bots._create_bot_tool(args.get("name", ""), args.get("description", ""), args.get("allowed_tools", []))
        if name == "update_bot":
            return bots._update_bot_tool(args.get("bot_id"), args.get("description"), args.get("allowed_tools"))
        if name == "run_delegation_step":
            task_context.get_task_hash(ctx.session_id)
            if ctx.req is not None and settings.load_settings().get("delegation_async_enabled"):
                return delegation_jobs._run_delegation_step_async_tool(ctx.req, args.get("bot_id"), args.get("instruction", ""))
            return bots._run_delegation_step_tool(args.get("bot_id"), args.get("instruction", ""))
        if name == "read_scratch_file":
            return bots._read_scratch_file(args.get("bot_name", ""))
        if name == "read_tool_call_history":
            return bots._read_tool_call_history(args.get("bot_name", ""))
        if name == "plan_delegation":
            task_context.get_task_hash(ctx.session_id)
            if ctx.req is not None and settings.load_settings().get("delegation_async_enabled"):
                return delegation_jobs._plan_delegation_async_tool(ctx.req, args.get("steps", []))
            return bots._plan_delegation_tool(args.get("steps", []))

    if name == "search_codebase":
        return _search_codebase(args.get("query", ""), args.get("limit", 3))

    if name == "write_plan":
        if ctx.plan_state is None:
            return {"error": "Planning is not available in this context."}
        steps = args.get("steps", [])
        if not steps or not isinstance(steps, list) or not all(isinstance(s, str) and s.strip() for s in steps):
            return {"error": "steps must be a non-empty list of non-empty strings."}
        ctx.plan_state.clear()
        ctx.plan_state.extend({"text": s, "status": "pending", "note": ""} for s in steps)
        return _render_plan_state(ctx.plan_state)
    if name == "update_plan_step":
        if ctx.plan_state is None:
            return {"error": "Planning is not available in this context."}
        if not ctx.plan_state:
            return {"error": "No plan recorded yet -- call write_plan first."}
        index = args.get("index")
        status = args.get("status", "")
        if not isinstance(index, int) or not (0 <= index < len(ctx.plan_state)):
            return {"error": f"index must be between 0 and {len(ctx.plan_state) - 1}, got {index!r}."}
        if status not in ("pending", "in_progress", "done", "failed"):
            return {"error": f"status must be one of pending/in_progress/done/failed, got {status!r}."}
        ctx.plan_state[index]["status"] = status
        ctx.plan_state[index]["note"] = args.get("note", "")
        return _render_plan_state(ctx.plan_state)

    if ctx.passthrough_session_id is not None:
        args = dict(args)
        args["session_id"] = ctx.passthrough_session_id
        try:
            resp = httpx.post(f"{lcm_client.LCM_URL}/tools/call", json={"name": name, "arguments": args}, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("result")
            return {"error": f"Tool call failed: HTTP {resp.status_code}"}
        except Exception as e:
            return {"error": f"Tool call failed: {e}"}
    return {"error": ctx.unknown_tool_message_template.format(name=name)}


class RAGIndexIn(BaseModel):
    workspace: str = "."

@app.post("/api/rag/index")
def index_codebase(req: RAGIndexIn):
    """Manually triggers RAG indexing for a workspace directory.
    Call this after codebase changes to keep the index up to date."""
    workspace = req.workspace or "."
    try:
        rag.index_codebase(workspace)
        return {"ok": True}
    except Exception as e:
        return {"error": f"Indexing failed: {e}"}

@app.get("/api/model-stats")

def get_model_stats():
    """Aggregate up/down counts per model, for showing real quality
    signal on the Models page -- computed fresh from current rows every
    call, so it's always correct even after votes change or get removed."""
    conn = annotations.ratings_conn()
    try:
        rows = conn.execute("""
            SELECT model,
                   SUM(CASE WHEN rating = 'up' THEN 1 ELSE 0 END) as up_count,
                   SUM(CASE WHEN rating = 'down' THEN 1 ELSE 0 END) as down_count
            FROM ratings
            GROUP BY model
        """).fetchall()
        return {r[0]: {"up": r[1], "down": r[2]} for r in rows}
    finally:
        conn.close()




@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html") as f:
        return f.read()


class DetectModelsIn(BaseModel):
    url: str = ""
    type: str = ""
    provider: str = ""
    api_key: str = ""

@app.post("/api/detect-models")
def detect_models(req: DetectModelsIn):
    """Live model listing -- local Ollama endpoints via /api/tags as
    before; online providers via each one's own models endpoint, all
    of which now exist (OpenAI, Anthropic, and OpenRouter all expose
    one; Google's is prefixed differently and handled separately
    below). Returns plain model-id strings in every case, so the
    frontend's enabledModels/detectedModels handling stays identical
    regardless of which kind of endpoint this was called for."""
    try:
        if req.type == "local":
            base = req.url.rstrip("/")
            resp = httpx.get(f"{base}/api/tags", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                names = [m.get("name") for m in data.get("models", []) if m.get("name")]
                return {"models": names}
            return {"error": f"Endpoint responded with HTTP {resp.status_code}"}
        if req.provider == "openai" or req.provider == "custom":
            if not req.api_key:
                return {"error": "An API key is required for OpenAI-compatible model detection."}
            base = (req.url or "https://api.openai.com/v1").rstrip("/")
            resp = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {req.api_key}"}, timeout=10)
            if resp.status_code == 200:
                names = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
                return {"models": sorted(names)}
            return {"error": f"Endpoint responded with HTTP {resp.status_code}: {resp.text[:200]}"}

        if req.provider == "openrouter":
            resp = httpx.get("https://openrouter.ai/api/v1/models", timeout=10)
            if resp.status_code == 200:
                names = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
                return {"models": sorted(names)}
            return {"error": f"OpenRouter responded with HTTP {resp.status_code}"}
        if req.provider == "anthropic":
            resp = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": req.api_key, "anthropic-version": "2023-06-01"},
                timeout=10,
            )
            if resp.status_code == 200:
                names = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
                return {"models": sorted(names)}
            return {"error": f"Endpoint responded with HTTP {resp.status_code}: {resp.text[:200]}"}
        if req.provider == "google":
            resp = httpx.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={req.api_key}",
                timeout=10,
            )
            if resp.status_code == 200:
                names = [m["name"].split("/", 1)[-1] for m in resp.json().get("models", []) if m.get("name")]
                return {"models": sorted(names)}
            return {"error": f"Endpoint responded with HTTP {resp.status_code}: {resp.text[:200]}"}
        base = req.url.rstrip("/")
        resp = httpx.get(f"{base}/api/tags", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            names = [m.get("name") for m in data.get("models", []) if m.get("name")]
            return {"models": names}
        return {"error": f"Endpoint responded with HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"Could not reach endpoint: {e}"}


@app.get("/api/workspace/browse")
def browse_workspace(path: str = ""):
    """List subdirectories AND files at a given path, for both the
    workspace picker (directories only matter there, workspace roots
    can't be files) and the save-as-file picker (files are shown too,
    read-only, so the user can see what's already there and avoid an
    accidental overwrite). Defaults to the user's home directory."""
    target = os.path.realpath(path) if path else os.path.expanduser("~")
    if not os.path.isdir(target):
        return {"error": f"Not a directory: {target}"}
    try:
        entries = os.listdir(target)
        dirs = sorted([
            name for name in entries
            if os.path.isdir(os.path.join(target, name)) and not name.startswith(".")
        ], key=str.lower)
        files = sorted([
            name for name in entries
            if os.path.isfile(os.path.join(target, name)) and not name.startswith(".")
        ], key=str.lower)
    except PermissionError:
        return {"error": f"Permission denied: {target}"}
    parent = os.path.dirname(target) if target != "/" else None
    return {"path": target, "parent": parent, "directories": dirs, "files": files}


class MkdirIn(BaseModel):
    path: str
    name: str

class SaveNoteFileIn(BaseModel):
    path: str
    filename: str
    content: str

@app.post("/api/notes/save-to-file")
def save_note_to_file(req: SaveNoteFileIn):
    """Writes note content to an arbitrary filesystem location chosen
    via the same directory browser used for workspace selection --
    a general 'Save As', not scoped to any pre-configured workspace."""
    if not req.filename or "/" in req.filename or req.filename in (".", ".."):
        return {"error": "Invalid filename."}
    base = os.path.realpath(req.path)
    if not os.path.isdir(base):
        return {"error": f"Not a directory: {base}"}
    target = os.path.join(base, req.filename)
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"ok": True, "path": target}
    except Exception as e:
        return {"error": f"Could not save file: {e}"}

@app.post("/api/workspace/mkdir")
def mkdir_workspace(req: MkdirIn):
    """Create a new directory inside the given path, for the workspace
    picker's 'New Folder' action."""
    if not req.name or "/" in req.name or req.name in (".", ".."):
        return {"error": "Invalid folder name."}
    base = os.path.realpath(req.path)
    if not os.path.isdir(base):
        return {"error": f"Not a directory: {base}"}
    new_dir = os.path.join(base, req.name)
    try:
        os.makedirs(new_dir, exist_ok=False)
        return {"path": new_dir}
    except FileExistsError:
        return {"error": "A folder with that name already exists."}
    except Exception as e:
        return {"error": f"Could not create folder: {e}"}



# Mode-independent -- available in casual, workspace, and athena_delegation
# alike (see _get_mode_tools), unlike every other schema list here which is
# gated to specific modes. Backs the plan-required structural gate in
# chat_stream's generate() loop: once a turn has made several tool calls
# without writing a plan, every tool except write_plan disappears until it's
# called. This replaces relying on the prose-only "state a short plan
# first" sentence that used to be the entire mechanism (see
# CODING_HARNESS_SECTIONS' plan_before_acting section) -- prompt-only
# guardrails here have repeatedly been observed to degrade under pressure;
# see _SELF_INVESTIGATION_TOOL_THRESHOLD above for the same lesson applied
# to blind self-investigation.
PLAN_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "write_plan",
            "description": "Record (or completely replace) the ordered list of steps for what you're about to do this turn. Call this once you can see a task is going to take more than a couple of tool calls, before diving into the work -- not after. This does not limit how many tool calls you can make afterward; it only requires deciding on a structure first. Calling this again replaces the previous plan entirely (use update_plan_step instead if you just want to mark progress on the existing plan).",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of short, concrete step descriptions, e.g. ['Find where X is defined', 'Check how Y calls it', 'Make the fix', 'Verify it']."
                    },
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan_step",
            "description": "Mark progress on one step of the plan already recorded via write_plan. Returns the full plan re-rendered with the update applied. Fails with a clear error if write_plan hasn't been called yet this turn, or if the index is out of range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Zero-based index of the step to update, matching its position in the list passed to write_plan."},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "done", "failed"], "description": "New status for this step."},
                    "note": {"type": "string", "description": "Optional short note about this step's outcome (e.g. what was found, or why it failed)."},
                },
                "required": ["index", "status"],
            },
        },
    },
]

RAG_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_codebase",
            "description": "Semantic search over the indexed codebase (built ahead of time, separately from this conversation) -- embeds your query and ranks chunks of real code by meaning, not by literal word overlap, so a query phrased differently from the code's own vocabulary can still find it. Each Python function/class is its own chunk (a large class is split per-method); other file types are chunked by section. Use this for orientation questions (where does X live, what does this area of the codebase roughly do) before falling back to list_files or grep to explore blind. Results are a starting point, not a source of truth: a chunk can still be stale relative to changes made earlier this session, and an oversized function may be split across more than one result -- once you're actually about to make an edit, read the real file directly first. If results aren't useful, try a different, more specific query rather than giving up on the tool entirely.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language or keyword search query describing what you're looking for."},
                    "limit": {"type": "integer", "description": "Maximum number of result chunks to return (default 3, max 10)."},
                },
                "required": ["query"],
            },
        },
    },
]

# ==================== Online provider adapters ====================
# generate()'s round loop, loop detection, tool-call execution, and
# doom-loop defenses are all built around Ollama's own streaming chunk
# shape: {"message": {"content": str, "tool_calls": [...], "thinking":
# str}, "done": bool, "eval_count": int, "eval_duration": int}.
# _stream_completion is the one seam where a request's actual
# provider matters -- everything downstream of it reads that exact
# shape regardless of which provider actually served the request, so
# none of the carefully-tested logic built around it needs to change
# at all to support a new provider; only a new adapter here does.

def _translate_messages_to_openai(messages):
    """Ollama's tool-call convention has no persistent IDs -- a call
    and its result are matched by position/order alone. OpenAI's
    format requires an explicit id on each tool_call and a matching
    tool_call_id on the message carrying its result, so this assigns
    synthetic, per-turn IDs when translating, tracking them just long
    enough to match each following role="tool" message to the specific
    call it's actually responding to."""
    out = []
    pending_tool_call_ids = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            tool_call_id = pending_tool_call_ids.pop(0) if pending_tool_call_ids else f"call_{uuid.uuid4().hex[:8]}"
            out.append({"role": "tool", "tool_call_id": tool_call_id, "content": m.get("content", "")})
            continue
        entry = {"role": role}
        images = m.get("images")
        content = m.get("content", "")
        if images:
            parts = [{"type": "text", "text": content}] if content else []
            for img in images:
                parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}})
            entry["content"] = parts
        else:
            entry["content"] = content
        tool_calls = m.get("tool_calls")
        if tool_calls:
            openai_calls = []
            pending_tool_call_ids = []
            for tc in tool_calls:
                call_id = f"call_{uuid.uuid4().hex[:8]}"
                pending_tool_call_ids.append(call_id)
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                if not isinstance(args, str):
                    args = json.dumps(args)
                openai_calls.append({"id": call_id, "type": "function", "function": {"name": fn.get("name", ""), "arguments": args}})
            entry["tool_calls"] = openai_calls
        out.append(entry)
    return out

def _iter_sse_lines(resp):
    """Manually buffers raw response bytes and only yields a line once
    it has been fully received, rather than trusting a text/line
    iterator to always get network chunk boundaries right. This is a
    real, independently-documented failure mode for exactly this kind
    of SSE parsing -- a single logical line (including a multi-byte
    UTF-8 character within it) can land split across two separate
    reads, corrupting or dropping fragments of the decoded text
    without ever raising a visible error. Buffering raw bytes and only
    decoding once a complete \n-terminated line has actually arrived
    avoids that regardless of exactly how the underlying chunks split."""
    buffer = b""
    for raw_chunk in resp.iter_raw():
        buffer += raw_chunk
        while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            yield line_bytes.decode("utf-8", errors="ignore").rstrip("\r")

def _stream_openai_compatible(req, messages, tools, cancel_flag, timeout=_MAIN_AGENT_TIMEOUT):
    """Covers OpenAI itself, OpenRouter, and any custom OpenAI-
    compatible endpoint -- all three speak the same /chat/completions
    wire format. Tool schemas need no translation at all: Ollama
    adopted OpenAI's own function-calling convention, so the exact
    same tools list built for a native Ollama request is valid here
    unchanged."""
    if req.endpoint_url:
        base_url = req.endpoint_url.rstrip("/")
    elif req.provider == "openrouter":
        base_url = "https://openrouter.ai/api/v1"
    else:
        base_url = "https://api.openai.com/v1"
    url = base_url + "/chat/completions"
    headers = {"Authorization": f"Bearer {req.api_key}", "Content-Type": "application/json"}
    payload = {
        "model": req.model,
        "messages": _translate_messages_to_openai(messages),
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    accumulated_tool_calls = {}
    try:
        with httpx.stream("POST", url, headers=headers, json=payload, timeout=timeout) as resp:
            if resp.status_code != 200:
                error_text = resp.read().decode(errors="replace")
                try:
                    error_detail = json.loads(error_text).get("error", {}).get("message") or error_text[:300]
                except (json.JSONDecodeError, AttributeError):
                    error_detail = error_text[:300]
                if resp.status_code == 401:
                    friendly = f"Authentication failed -- check the API key for this endpoint.\n\n{error_detail}"
                elif resp.status_code == 429:
                    friendly = f"Rate limit or usage quota exceeded for this provider.\n\n{error_detail}"
                elif resp.status_code == 400:
                    friendly = f"The request was rejected (bad request) -- this can mean an unsupported parameter or a model name the provider doesn't recognize.\n\n{error_detail}"
                elif resp.status_code >= 500:
                    friendly = f"The provider's own servers returned an error (HTTP {resp.status_code}) -- this is on their end, not a local problem.\n\n{error_detail}"
                else:
                    friendly = f"Request failed (HTTP {resp.status_code}).\n\n{error_detail}"
                yield {"message": {"content": f"[Online provider error] {friendly}"}, "done": True}
                return
            for line in _iter_sse_lines(resp):
                if cancel_flag.is_set():
                    return
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str.strip() == "[DONE]":
                    yield {"message": {"content": ""}, "done": True}
                    return
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choice = (event.get("choices") or [{}])[0]
                delta = choice.get("delta", {})
                out_msg = {}
                if delta.get("content"):
                    out_msg["content"] = delta["content"]
                if delta.get("reasoning"):
                    # OpenRouter's own extension for reasoning-capable
                    # models -- separate from "content", and previously
                    # not read at all here, so a model that reasons
                    # before calling a tool (very common) had its
                    # entire visible output silently dropped: nothing
                    # in the thinking panel, nothing in the reply, only
                    # the tool call itself showed up.
                    out_msg["thinking"] = delta["reasoning"]
                if delta.get("tool_calls"):
                    for tc_delta in delta["tool_calls"]:
                        idx = tc_delta.get("index", 0)
                        if idx not in accumulated_tool_calls:
                            accumulated_tool_calls[idx] = {"id": tc_delta.get("id", ""), "name": "", "arguments": ""}
                        if tc_delta.get("id"):
                            accumulated_tool_calls[idx]["id"] = tc_delta["id"]
                        fn_delta = tc_delta.get("function", {})
                        if fn_delta.get("name"):
                            accumulated_tool_calls[idx]["name"] += fn_delta["name"]
                        if fn_delta.get("arguments"):
                            accumulated_tool_calls[idx]["arguments"] += fn_delta["arguments"]
                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    # Tool calls arrive incrementally across many chunks
                    # and are only complete once finish_reason shows up
                    # -- unlike Ollama, which always sends a tool call
                    # whole, never as deltas, so this is the point to
                    # finally emit them as complete objects matching
                    # what the rest of the loop actually expects.
                    if accumulated_tool_calls:
                        out_msg["tool_calls"] = []
                        for idx in sorted(accumulated_tool_calls.keys()):
                            tc = accumulated_tool_calls[idx]
                            try:
                                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                            except json.JSONDecodeError:
                                args = {}
                            out_msg["tool_calls"].append({"function": {"name": tc["name"], "arguments": args}})
                    usage = event.get("usage") or {}
                    yield {
                        "message": out_msg,
                        "done": True,
                        "eval_count": usage.get("completion_tokens"),
                        "eval_duration": None,  # not reported by these APIs -- tokens/sec just won't show for online-provider responses
                    }
                    return
                if out_msg:
                    yield {"message": out_msg, "done": False}
    except httpx.RequestError as e:
        # Connection-level failure (DNS, timeout, refused, etc.) --
        # without this, the exception would propagate silently up
        # through the background thread and generation would just
        # stop with no reply at all, rather than a visible explanation.
        yield {"message": {"content": f"[Online provider error] Could not reach the provider: {e}"}, "done": True}
        return

def _translate_messages_to_anthropic(messages):
    """Anthropic's format differs from Ollama's/OpenAI's in several
    real ways: the system prompt is a separate top-level field, not a
    message; tool calls live inside an assistant message's content
    array as tool_use blocks (each with its own id); and tool results
    must be sent back as a user-role message containing tool_result
    blocks -- there's no separate "tool" role at all. This walks
    Ollama's message list and produces both the extracted system text
    and Anthropic-shaped messages, tracking each tool_use id so the
    result(s) that follow reference the correct one, and batching
    consecutive tool results into one user message the way Anthropic
    expects rather than one message per result."""
    system_parts = []
    out = []
    pending_tool_use_ids = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(m["content"])
            continue
        if role == "tool":
            tool_use_id = pending_tool_use_ids.pop(0) if pending_tool_use_ids else f"toolu_{uuid.uuid4().hex[:8]}"
            result_block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": m.get("content", "")}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) and all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                out[-1]["content"].append(result_block)
            else:
                out.append({"role": "user", "content": [result_block]})
            continue
        content = m.get("content", "")
        images = m.get("images")
        tool_calls = m.get("tool_calls")
        blocks = []
        if content:
            blocks.append({"type": "text", "text": content})
        if images:
            for img in images:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}})
        if tool_calls:
            pending_tool_use_ids = []
            for tc in tool_calls:
                tool_id = f"toolu_{uuid.uuid4().hex[:8]}"
                pending_tool_use_ids.append(tool_id)
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                blocks.append({"type": "tool_use", "id": tool_id, "name": fn.get("name", ""), "input": args})
        out.append({"role": role, "content": blocks if blocks else content})
    return chr(10).join(system_parts), out

def _translate_tools_to_anthropic(tools):
    out = []
    for t in (tools or []):
        fn = t.get("function", {})
        out.append({"name": fn.get("name", ""), "description": fn.get("description", ""), "input_schema": fn.get("parameters", {"type": "object", "properties": {}})})
    return out

def _anthropic_error_message(status_code, error_detail):
    if status_code == 401:
        return f"Authentication failed -- check the API key for this endpoint.\n\n{error_detail}"
    if status_code == 429:
        return f"Rate limit or usage quota exceeded for this provider.\n\n{error_detail}"
    if status_code == 400:
        return f"The request was rejected (bad request) -- this can mean an unsupported parameter or a model name the provider doesn't recognize.\n\n{error_detail}"
    if status_code >= 500:
        return f"The provider's own servers returned an error (HTTP {status_code}) -- this is on their end, not a local problem.\n\n{error_detail}"
    return f"Request failed (HTTP {status_code}).\n\n{error_detail}"

def _stream_anthropic(req, messages, tools, cancel_flag, timeout=_MAIN_AGENT_TIMEOUT):
    """Anthropic's own /v1/messages format -- separate system field,
    tool_use/tool_result content blocks instead of Ollama's/OpenAI's
    role="tool" convention, and a named-SSE-event streaming protocol
    (content_block_start/delta/stop, message_delta, message_stop)
    rather than one uniform delta shape."""
    system_text, anthropic_messages = _translate_messages_to_anthropic(messages)
    url = (req.endpoint_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": req.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": req.model,
        # max_tokens must exceed thinking's budget_tokens, since the
        # thinking budget counts against the same output allowance --
        # raised from a flat 8192 to leave real room for the actual
        # reply on top of it.
        "max_tokens": 16000,
        "thinking": {"type": "enabled", "budget_tokens": 8000},
        "messages": anthropic_messages,
        "stream": True,
    }
    if system_text:
        payload["system"] = system_text
    if tools:
        payload["tools"] = _translate_tools_to_anthropic(tools)
    current_blocks = {}
    try:
        with httpx.stream("POST", url, headers=headers, json=payload, timeout=timeout) as resp:
            if resp.status_code != 200:
                error_text = resp.read().decode(errors="replace")
                try:
                    error_detail = json.loads(error_text).get("error", {}).get("message") or error_text[:300]
                except (json.JSONDecodeError, AttributeError):
                    error_detail = error_text[:300]
                friendly = _anthropic_error_message(resp.status_code, error_detail)
                yield {"message": {"content": f"[Online provider error] {friendly}"}, "done": True}
                return
            output_tokens = None
            for line in _iter_sse_lines(resp):
                if cancel_flag.is_set():
                    return
                if not line or not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "content_block_start":
                    idx = event.get("index", 0)
                    block = event.get("content_block", {})
                    block_type = block.get("type")
                    if block_type == "tool_use":
                        current_blocks[idx] = {"type": "tool_use", "id": block.get("id", ""), "name": block.get("name", ""), "json": ""}
                    elif block_type == "thinking":
                        current_blocks[idx] = {"type": "thinking", "text": ""}
                    else:
                        current_blocks[idx] = {"type": "text", "text": ""}
                elif etype == "content_block_delta":
                    idx = event.get("index", 0)
                    delta = event.get("delta", {})
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if idx in current_blocks:
                            current_blocks[idx]["text"] = current_blocks[idx].get("text", "") + text
                        if text:
                            yield {"message": {"content": text}, "done": False}
                    elif delta_type == "thinking_delta":
                        # Separate field name from a regular text delta
                        # (Anthropic's own convention, mirroring how
                        # Ollama's "thinking" is kept apart from
                        # "content") -- mapped the same way here so it
                        # renders in the same thinking panel.
                        thinking_text = delta.get("thinking", "")
                        if idx in current_blocks:
                            current_blocks[idx]["text"] = current_blocks[idx].get("text", "") + thinking_text
                        if thinking_text:
                            yield {"message": {"thinking": thinking_text}, "done": False}
                    elif delta_type == "input_json_delta":
                        if idx in current_blocks:
                            current_blocks[idx]["json"] = current_blocks[idx].get("json", "") + delta.get("partial_json", "")
                elif etype == "message_delta":
                    usage = event.get("usage") or {}
                    if usage.get("output_tokens") is not None:
                        output_tokens = usage["output_tokens"]
                elif etype == "message_stop":
                    tool_calls = []
                    for idx in sorted(current_blocks.keys()):
                        b = current_blocks[idx]
                        if b.get("type") == "tool_use":
                            try:
                                args = json.loads(b["json"]) if b["json"] else {}
                            except json.JSONDecodeError:
                                args = {}
                            tool_calls.append({"function": {"name": b.get("name", ""), "arguments": args}})
                    out_msg = {}
                    if tool_calls:
                        out_msg["tool_calls"] = tool_calls
                    yield {"message": out_msg, "done": True, "eval_count": output_tokens, "eval_duration": None}
                    return
    except httpx.RequestError as e:
        yield {"message": {"content": f"[Online provider error] Could not reach the provider: {e}"}, "done": True}
        return

def _translate_messages_to_google(messages):
    """Google's format uses "model" instead of "assistant" for the
    role name, always represents content as an array of "parts"
    rather than a plain string, and matches tool calls/results by
    function name and position rather than an explicit id the way
    OpenAI and Anthropic both require -- simpler in that one respect,
    but the content shape itself differs from every other provider."""
    system_parts = []
    out = []
    pending_tool_names = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(m["content"])
            continue
        if role == "tool":
            name = pending_tool_names.pop(0) if pending_tool_names else ""
            try:
                response_obj = json.loads(m.get("content", "{}"))
                if not isinstance(response_obj, dict):
                    response_obj = {"result": response_obj}
            except json.JSONDecodeError:
                response_obj = {"result": m.get("content", "")}
            part = {"functionResponse": {"name": name, "response": response_obj}}
            if out and out[-1]["role"] == "user" and all("functionResponse" in p for p in out[-1]["parts"]):
                out[-1]["parts"].append(part)
            else:
                out.append({"role": "user", "parts": [part]})
            continue
        google_role = "model" if role == "assistant" else "user"
        parts = []
        content = m.get("content", "")
        if content:
            parts.append({"text": content})
        images = m.get("images")
        if images:
            for img in images:
                parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img}})
        tool_calls = m.get("tool_calls")
        if tool_calls:
            pending_tool_names = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                pending_tool_names.append(name)
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                parts.append({"functionCall": {"name": name, "args": args}})
        if parts:
            out.append({"role": google_role, "parts": parts})
    return chr(10).join(system_parts), out

def _translate_tools_to_google(tools):
    declarations = []
    for t in (tools or []):
        fn = t.get("function", {})
        declarations.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return [{"function_declarations": declarations}] if declarations else None

def _stream_google(req, messages, tools, cancel_flag, timeout=_MAIN_AGENT_TIMEOUT):
    """Google Gemini's generateContent streaming API -- uses ?alt=sse
    to get proper SSE framing rather than Google's default
    single-JSON-array response, since the whole _stream_completion
    abstraction is built around consuming an event-by-event stream."""
    system_text, google_messages = _translate_messages_to_google(messages)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{req.model}:streamGenerateContent?key={req.api_key}&alt=sse"
    payload = {
        "contents": google_messages,
        # Off by default -- without this, thinking-capable Gemini
        # models (2.5+) still reason internally but never surface any
        # of it, same underlying gap as Anthropic needing its own
        # thinking parameter explicitly set.
        "generationConfig": {"thinkingConfig": {"includeThoughts": True}},
    }
    if system_text:
        payload["system_instruction"] = {"parts": [{"text": system_text}]}
    google_tools = _translate_tools_to_google(tools)
    if google_tools:
        payload["tools"] = google_tools
    try:
        with httpx.stream("POST", url, json=payload, timeout=timeout) as resp:
            if resp.status_code != 200:
                error_text = resp.read().decode(errors="replace")
                try:
                    error_detail = json.loads(error_text).get("error", {}).get("message") or error_text[:300]
                except (json.JSONDecodeError, AttributeError, TypeError):
                    error_detail = error_text[:300]
                friendly = _anthropic_error_message(resp.status_code, error_detail)  # same generic 401/429/400/5xx framing applies here too
                yield {"message": {"content": f"[Online provider error] {friendly}"}, "done": True}
                return
            output_tokens = None
            for line in _iter_sse_lines(resp):
                if cancel_flag.is_set():
                    return
                if not line or not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                candidates = event.get("candidates") or []
                if not candidates:
                    continue
                candidate = candidates[0]
                content = candidate.get("content", {})
                parts = content.get("parts", [])
                out_msg = {}
                text_out = ""
                thinking_out = ""
                tool_calls = []
                for part in parts:
                    if "text" in part:
                        # Thinking and regular answer text arrive mixed
                        # together in the same parts array, distinguished
                        # only by this boolean flag -- not a separate
                        # part type or field name the way Anthropic and
                        # OpenRouter both do it.
                        if part.get("thought"):
                            thinking_out += part["text"]
                        else:
                            text_out += part["text"]
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        tool_calls.append({"function": {"name": fc.get("name", ""), "arguments": fc.get("args", {})}})
                if text_out:
                    out_msg["content"] = text_out
                if thinking_out:
                    out_msg["thinking"] = thinking_out
                if tool_calls:
                    out_msg["tool_calls"] = tool_calls
                usage = event.get("usageMetadata") or {}
                if usage.get("candidatesTokenCount") is not None:
                    output_tokens = usage["candidatesTokenCount"]
                finish_reason = candidate.get("finishReason")
                if finish_reason:
                    yield {"message": out_msg, "done": True, "eval_count": output_tokens, "eval_duration": None}
                    return
                if out_msg:
                    yield {"message": out_msg, "done": False}
    except httpx.RequestError as e:
        yield {"message": {"content": f"[Online provider error] Could not reach the provider: {e}"}, "done": True}
        return

def _stream_completion(req, target_url, messages, tools, ctx_size, cancel_flag, timeout=_MAIN_AGENT_TIMEOUT):
    """Single entry point for actually running one round of generation
    -- yields Ollama-shaped chunks no matter which provider a request
    targets. Ollama-native requests (provider == "") pass straight
    through to Ollama's own /api/chat, completely unchanged from
    before online providers existed at all.

    timeout defaults to _MAIN_AGENT_TIMEOUT (Athena's own turn, real
    patience on old/slow hardware) -- _call_bot_endpoint explicitly
    passes _SUB_AGENT_TIMEOUT instead, since a bot round is one bounded,
    single-topic investigation step that should hit a real ceiling
    rather than running indefinitely."""
    if req.provider in ("openai", "openrouter", "custom"):
        yield from _stream_openai_compatible(req, messages, tools, cancel_flag, timeout=timeout)
        return
    if req.provider == "anthropic":
        yield from _stream_anthropic(req, messages, tools, cancel_flag, timeout=timeout)
        return
    if req.provider == "google":
        yield from _stream_google(req, messages, tools, cancel_flag, timeout=timeout)
        return
    payload = {
        "model": req.model,
        "messages": messages,
        "tools": tools if tools else None,
        "options": {"num_ctx": ctx_size},
        "stream": True,
    }
    if req.think:
        # Ollama's own accepted shape: a boolean, or one of the exact
        # strings "low"/"medium"/"high"/"max" -- "none" (Athena's own
        # "force thinking off" option) is the one value that needs
        # translating, to the boolean False.
        payload["think"] = False if req.think == "none" else req.think
    with httpx.stream("POST", target_url, json=payload, timeout=timeout) as resp:
        if resp.status_code != 200:
            # Ollama returning a non-200 (most often 500 -- the model
            # failed to load, typically not enough RAM/VRAM for it, or an
            # unsupported quantization on this device) used to be
            # silently mistaken for a normal empty streaming chunk here:
            # httpx.stream doesn't raise on its own for a bad status
            # code, so the error body's JSON just got parsed as if it
            # were a real chat chunk, found no "message"/"done" fields
            # it recognized, and the round quietly finished with nothing
            # -- no error shown at all. Surface it explicitly instead,
            # mirroring the same friendly-error pattern already used for
            # the online-provider adapters below.
            error_text = resp.read().decode(errors="replace")
            try:
                error_detail = json.loads(error_text).get("error") or error_text[:300]
            except (json.JSONDecodeError, AttributeError):
                error_detail = error_text[:300]
            if "unknown model architecture" in str(error_detail).lower():
                friendly = (
                    "This model's architecture isn't supported by llama.cpp (the engine Ollama runs models with) -- "
                    "not a RAM/VRAM/quantization issue, and not fixable by retrying or picking a different endpoint. "
                    "llama.cpp only understands a specific, hardcoded list of architectures (llama, qwen2/3, gemma, "
                    "mistral, etc.); this GGUF was converted from one that isn't in that list yet, so it fails at the "
                    "exact same point on any device until llama.cpp adds support for it upstream. Look for a "
                    f"differently-converted GGUF of the same model using a supported architecture, or use a different "
                    f"model.\n\n{error_detail}"
                )
            elif resp.status_code >= 500:
                friendly = f"Ollama itself returned an error (HTTP {resp.status_code}) -- this usually means the model failed to load, often because there isn't enough RAM/VRAM for it, or this device doesn't support its quantization format.\n\n{error_detail}"
            elif resp.status_code == 404:
                friendly = f"Ollama doesn't have this model pulled, or the endpoint URL is wrong.\n\n{error_detail}"
            else:
                friendly = f"Ollama request failed (HTTP {resp.status_code}).\n\n{error_detail}"
            yield {"message": {"content": f"[Ollama error] {friendly}"}, "done": True}
            return
        for line in resp.iter_lines():
            if cancel_flag.is_set():
                return
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


@app.post("/api/chat")
def chat_stream(req: ChatIn):
    activity.touch()
    logger.debug(f"model={req.model!r} endpoint_url={req.endpoint_url!r}")
    effective_message = req.message
    for att in req.attachments:
        att_content = (att.get("content") or "")[:20000]
        effective_message += f"\n\n[Attached file: {att.get('name', 'file')}]\n```\n{att_content}\n```"
    _user_msg_id = lcm_client.send_to_lcm(req.session_id, "user", effective_message, has_image=bool(req.images))
    if _user_msg_id is None:
        # Don't silently generate a response for a message that was
        # never actually persisted -- that's exactly the confusing
        # failure mode where the user gets a real-looking reply in the
        # moment, then it vanishes on the next reload with no
        # indication anything went wrong.
        def _save_failed_error():
            yield f"data: {json.dumps({'delta': 'Your message could not be saved -- Athena\'s memory service is unreachable right now. Please try again in a moment.'})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        return StreamingResponse(_save_failed_error(), media_type="text/event-stream")

    context = lcm_client.get_lcm_context(req.session_id)

    # Perpetual agent mode -- one tool set, always available. LCM's recall
    # tools and the read-only bash tool are always on; file write/edit
    # tools are only included here when req.workspace is non-empty, so
    # the model can never edit files on a session with no workspace
    # bound to it, regardless of how it reads an ambiguous prompt.
    mode, effective_workspace = _resolve_mode_and_workspace(req)
    req.workspace = effective_workspace
    system_prompt = _get_mode_system_prompt(mode, req)
    # The maximum tool set this turn could ever reach (as if
    # search_codebase were already called) -- used both for ctx_size
    # estimation below AND as the actual tools payload sent every round
    # (see generate()'s round loop). Keeping this one stable list for the
    # whole turn, instead of recomputing a narrower one as gates close,
    # is what keeps the tool schema an unchanging prefix across rounds --
    # gate enforcement itself now happens in _execute_tool_call at the
    # moment a call is attempted, not by hiding tools from the schema.
    tools = _get_mode_tools(mode, req, called_tool_names={"search_codebase"})
    logger.debug(f"tools for this turn: {[t.get('function', {}).get('name') for t in tools]}")

    _dynamic_context_msg = _get_dynamic_context_message(req)
    _raw_messages = [{"role": "system", "content": system_prompt}] + ([_dynamic_context_msg] if _dynamic_context_msg else []) + context
    # Some chat templates (e.g. qwen3.5's) require every system-role
    # message to be grouped at the very start of the conversation --
    # LCM returns one separate system message per summary node, which
    # otherwise ends up interleaved with real user/assistant turns and
    # breaks that requirement. Merge them into a single leading system
    # message, preserving order, then keep everything else as-is.
    _system_parts = [m["content"] for m in _raw_messages if m.get("role") == "system"]
    _non_system = [m for m in _raw_messages if m.get("role") != "system"]
    messages = [{"role": "system", "content": "\n\n".join(_system_parts)}] + _non_system
    if req.images:
        for _m in reversed(messages):
            if _m.get("role") == "user":
                _m["images"] = req.images
                break
    effective_max = req.max_ctx if req.max_ctx > 0 else MAX_CTX_DEFAULT
    ctx_size = pick_dynamic_ctx(messages, tools, max_ctx=effective_max)
    if req.max_ctx > 0:
        ctx_size = req.max_ctx
    prompt_tokens = estimate_tokens(messages, tools)

    # Per-turn plan state, mutated in place (never rebound) via
    # ToolContext.plan_state -- see dispatch_tool. Each entry:
    # {"text": str, "status": str, "note": str}.
    _current_plan = []

    def _execute_tool_call(tool_call, pre_round_called, pre_round_exploration_count, pre_round_total_count):
        """Thin per-turn wrapper around the shared dispatch_tool: build a
        ToolContext from this request/turn's own state, then dispatch.
        allowed_names=None (the default) since _get_mode_tools already
        controls what the model can even see this turn -- this call site
        doesn't need a second restrictive allow-list on top of that.

        Gate enforcement (see _check_tool_gate) used to happen by omitting
        a gated tool from the schema sent to the model each round -- but
        the schema is now the same stable, maximal list for the whole turn
        (see the `tools` computed once before the round loop), so gating
        happens here instead, at the moment a call is actually attempted,
        which keeps the schema itself cache-friendly across every round."""
        fn = tool_call.get("function", {})
        name = fn.get("name", "")
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        gate_error = _check_tool_gate(mode, name, pre_round_called, pre_round_exploration_count, pre_round_total_count)
        if gate_error is not None:
            return gate_error
        ctx = ToolContext(
            session_id=req.session_id,
            workspace=req.workspace,
            search_url=req.search_url,
            req=req,
            plan_state=_current_plan,
            passthrough_session_id=req.session_id,
            require_athena_bots_session=True,
        )
        return dispatch_tool(name, args, ctx)

    def generate():
        cancel_flag = threading.Event()
        generation_streaming.cancel_flags[req.session_id] = cancel_flag
        # Doom-loop detection: fingerprint each tool call (name + exact
        # arguments) and track the last 20 in this turn. If the same
        # fingerprint would be executed a 3rd time, refuse to run it
        # again and tell the model directly instead -- this is a real,
        # structural block, not just a text suggestion, since a model
        # stuck re-gathering the same already-known information can
        # simply ignore an injected warning but cannot bypass an
        # actual execution refusal. Modeled on a documented pattern
        # used by other production coding-agent harnesses for exactly
        # this failure mode.
        _tool_call_fingerprints = []
        _recent_search_queries = []
        def _tool_call_fingerprint(tc):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            try:
                args_str = json.dumps(args, sort_keys=True)
            except TypeError:
                args_str = str(args)
            raw = fn.get("name", "") + "|" + args_str
            return hashlib.md5(raw.encode()).hexdigest()
        def _detect_text_loop(text):
            """Model-agnostic loop detector for plain-text generation.
            Catches a model stuck repeating itself -- restated reasoning,
            a hallucinated fake tool call written as plain text instead
            of a real structured one, anything -- by watching for actual
            repeated content, not by pattern-matching any one model's
            own tool-call syntax. Syntax-matching would only ever work
            for the specific model it was written against, which is the
            opposite of what this harness needs: it has to hold up for
            whatever model someone points it at, including small,
            resource-constrained models on edge-case hardware that show
            this failure mode worst.

            Layered rather than one fixed check: a longer window (40
            chars, 3 repeats) catches a whole sentence or reasoning
            fragment being restated; a shorter window (18 chars, 5
            repeats) catches a shorter recurring phrase that doesn't
            happen to line up as an exact 40-char match -- interspersed
            with slightly different surrounding text each time, which
            the single long-window check alone would miss entirely."""
            def _tail_repeats(window, min_repeats):
                if len(text) < window * min_repeats:
                    return False
                tail = text[-window:]
                if len(tail.strip()) < window * 0.5:
                    return False  # mostly whitespace -- not a meaningful signal
                return text.count(tail) >= min_repeats
            return _tail_repeats(40, 3) or _tail_repeats(18, 5)

        def _log_turn_end(outcome, **extra):
            log_trace_event(
                req.session_id, "turn_end", mode=mode, round=_round,
                total_tool_calls=_total_tool_call_count, outcome=outcome, **extra,
            )

        log_trace_event(req.session_id, "turn_start", mode=mode, model=req.model, workspace=req.workspace, endpoint_url=req.endpoint_url, think=req.think)
        yield f"data: {json.dumps({'user_message_id': _user_msg_id})}\n\n"
        full_reply = ""
        full_thinking = ""
        full_tool_calls = []
        _target_url = (req.endpoint_url.rstrip("/") + "/api/chat") if req.endpoint_url else OLLAMA_URL
        _messages = list(messages)
        logger.debug(f"_messages roles={[m.get('role') for m in _messages]!r}")
        MAX_ROUNDS = 1000  # effectively unbounded; the stop button is the real safety net now
        last_eval_count = None
        last_eval_duration = None
        consecutive_loop_detections = 0
        consecutive_ungrounded_claims = 0
        consecutive_empty_final_answers = 0
        made_any_tool_calls_this_turn = False
        # Tool availability is recomputed every round from these two,
        # not fixed once at request start -- see _get_mode_tools. This is
        # what makes read_file/bash gated behind search_codebase, and
        # blind self-investigation gated behind delegation, structural
        # instead of one more prompt instruction to drift off under
        # pressure.
        _called_tool_names = set()
        _exploration_call_count = 0
        _investigation_gate_notified = False
        _total_tool_call_count = 0
        _plan_gate_notified = False
        _delegation_commitment_notified = False

        for _round in range(MAX_ROUNDS):
            round_reply = ""
            round_tool_calls = []
            loop_detected = False

            # Same per-host lock bot delegation calls already use
            # (_get_host_lock / _run_delegation_step_tool) -- without it,
            # Athena's own generation here and a concurrent bot call could
            # land on the same physical Ollama host at the same time (the
            # common case -- see _endpoint_host's own docstring), forcing
            # it to juggle two different models on hardware that can't
            # hold both in VRAM at once. That contention is the leading
            # suspect behind recurring RemoteProtocolError("incomplete
            # chunked read")/ReadTimeout failures observed specifically
            # in the athena-bots-agent session. Scoped to one round's
            # streaming call, not the whole multi-round turn, so a queued
            # bot call still gets a fair turn between rounds.
            try:
                with host_locks.get_host_lock(host_locks.endpoint_host(req.endpoint_url)):
                    for chunk in _stream_completion(req, _target_url, _messages, tools, ctx_size, cancel_flag):
                        if cancel_flag.is_set():
                            break
                        # Refresh on every real chunk received, not just once
                        # when the request first arrives -- otherwise a single
                        # generation running longer than the idle threshold
                        # (a large tool-call argument buffered by Ollama, a
                        # big context, a slow model) gets misread as an idle
                        # window partway through, and the memory scan can
                        # fire a second, competing Ollama request while this
                        # one is still actively streaming.
                        activity.touch()
                        msg = chunk.get("message", {})
                        logger.debug(f"chunk={chunk}")
                        thinking_delta = msg.get("thinking", "")
                        if thinking_delta:
                            full_thinking += thinking_delta
                            yield f"data: {json.dumps({'thinking': thinking_delta})}\n\n"
                        delta = msg.get("content", "")
                        if delta:
                            round_reply += delta
                            full_reply += delta
                            yield f"data: {json.dumps({'delta': delta})}\n\n"
                            if _detect_text_loop(round_reply):
                                loop_detected = True
                                break
                        if msg.get("tool_calls"):
                            round_tool_calls.extend(msg["tool_calls"])
                        if chunk.get("done"):
                            last_eval_count = chunk.get("eval_count")
                            last_eval_duration = chunk.get("eval_duration")
                            break
            except httpx.RequestError as e:
                # The model backend became unreachable or dropped the
                # connection mid-response (server restarted/crashed, network
                # blip, or -- in athena_delegation mode -- resource
                # contention with a concurrent bot call on the same host).
                # Previously this surfaced nowhere: the exception propagated
                # up to generation_streaming.drain_generator_to_queue, which just logged it and
                # quietly cleaned up, so the turn appeared to silently stop
                # with zero explanation. Persist and show a real message
                # instead, reusing whatever partial reply already streamed.
                logger.error(f"generation for session {req.session_id!r} raised: {e!r}")
                error_msg = f"Generation failed partway through: {e!r}. The model backend likely became unreachable or dropped the connection -- try again in a moment."
                _had_prior_content = bool(full_reply)
                full_reply += ("\n\n" if _had_prior_content else "") + error_msg
                _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                yield f"data: {json.dumps({'delta': ('\n\n' if _had_prior_content else '') + error_msg})}\n\n"
                _log_turn_end("generation_error", error=repr(e))
                yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'assistant_message_id': _msg_id, 'generation_error': True})}\n\n"
                return

            if cancel_flag.is_set():
                if full_reply:
                    lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                _log_turn_end("cancelled")
                yield f"data: {json.dumps({'done': True, 'cancelled': True})}\n\n"
                generation_streaming.cancel_flags.pop(req.session_id, None)
                return

            if loop_detected:
                # Model-agnostic recovery: stop this round, drop the
                # repeated tail from what actually gets saved/fed back
                # (keep one copy of the repeated text, not the pile of
                # duplicates), then redirect rather than either silently
                # cutting the response short or forcing a premature
                # conclusion out of nothing -- same principle as the
                # tool-call fingerprint block, applied to plain text.
                consecutive_loop_detections += 1
                tail = round_reply[-40:]
                first_idx = round_reply.find(tail)
                trimmed_reply = round_reply[:first_idx + len(tail)] if first_idx != -1 else round_reply
                if trimmed_reply.strip():
                    _messages.append({"role": "assistant", "content": trimmed_reply})
                if consecutive_loop_detections >= 3:
                    # Redirecting hasn't helped after repeated tries --
                    # stop honestly rather than silently burning through
                    # the rest of MAX_ROUNDS on a model that can't recover.
                    failure_msg = "I got stuck repeating myself and wasn't able to recover after a few attempts -- stopping here rather than continuing to loop."
                    full_reply = (full_reply.rsplit(round_reply, 1)[0] if round_reply in full_reply else full_reply) + ("\n\n" + failure_msg if full_reply else failure_msg)
                    _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                    yield f"data: {json.dumps({'delta': ('\n\n' if trimmed_reply.strip() else '') + failure_msg})}\n\n"
                    _log_turn_end("loop_detected_giveup")
                    yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'assistant_message_id': _msg_id})}\n\n"
                    return
                _messages.append({"role": "user", "content": (
                    "You were repeating the same text over and over without making progress, and "
                    "generation was stopped automatically. Do not restart the same line of reasoning. "
                    "Review what you already wrote above: if you reached a real conclusion before the "
                    "repetition started, state it directly and concisely. If you had not reached one, "
                    "say so plainly instead of restating the same points again."
                )})
                continue

            consecutive_loop_detections = 0

            if not round_tool_calls:
                # A substantial, tool-free round arriving after this same
                # turn already made real tool calls is the exact pattern
                # behind a real, observed failure: the model made a few
                # genuine tool calls, then started fabricating further
                # "I read/found/fixed X" narration with no real tool
                # activity behind it at all. This check is model-agnostic
                # by design -- it never parses the text itself for
                # language patterns (which would only ever catch one
                # model's specific phrasing), it only checks the one
                # verifiable fact available: whether a real, structured
                # tool call actually happened. Only fires in workspace
                # (coding-harness) sessions, where tool use is expected,
                # and only once real tool use has already started this
                # turn, so a genuine short question or a real final
                # answer that never needed tools at all is unaffected.
                if made_any_tool_calls_this_turn and req.workspace and len(round_reply) > 400:
                    consecutive_ungrounded_claims += 1
                    if consecutive_ungrounded_claims >= 3:
                        failure_msg = "I started describing further progress without actually making the tool calls to back it up, and wasn't able to correct that after a few attempts -- stopping here rather than continuing to report unverified work."
                        full_reply += ("\n\n" + failure_msg if full_reply else failure_msg)
                        _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                        yield f"data: {json.dumps({'delta': '\n\n' + failure_msg})}\n\n"
                        _log_turn_end("ungrounded_claims_giveup")
                        yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'assistant_message_id': _msg_id})}\n\n"
                        return
                    _messages.append({"role": "assistant", "content": round_reply})
                    _messages.append({"role": "user", "content": (
                        "You wrote a substantial amount of text describing further progress -- reading, "
                        "checking, or changing something -- but did not actually make a real tool call this "
                        "round to back any of it up. Do not treat anything you just described as having "
                        "actually happened; it did not. If you genuinely already have everything you need "
                        "from real tool results earlier in this conversation, state your actual final answer "
                        "now, directly and concisely, without describing further actions you have not taken. "
                        "Otherwise, make a real tool call right now for whatever you still need, instead of "
                        "describing what it would show."
                    )})
                    continue
                consecutive_ungrounded_claims = 0

                # The model did real tool-call work this turn, then ended
                # with a completely blank round -- no content, no tool
                # calls, done:true. Nothing else here catches this: it's
                # not a loop (no repeated text), not an ungrounded claim
                # (no text was written at all), so without this it sails
                # through as an ordinary successful completion and the
                # turn just ends with nothing to show for the work done.
                # Nudge for an actual answer a couple of times before
                # giving up honestly, same shape as the other two
                # recovery paths above.
                if made_any_tool_calls_this_turn and not full_reply.strip():
                    consecutive_empty_final_answers += 1
                    if consecutive_empty_final_answers >= 3:
                        failure_msg = "I did some real investigation this turn but wasn't able to put together an actual answer from it after a few attempts -- stopping here rather than ending with nothing. Check the tool results above for what was actually found."
                        full_reply = failure_msg
                        _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                        yield f"data: {json.dumps({'delta': failure_msg})}\n\n"
                        _log_turn_end("empty_final_answer_giveup")
                        yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'assistant_message_id': _msg_id})}\n\n"
                        return
                    _messages.append({"role": "user", "content": (
                        "You finished a round of tool calls but ended with a completely blank reply -- no "
                        "answer, no further tool call. Based on what you've actually found so far this turn, "
                        "state your real conclusion or findings now, directly and concisely. If you're genuinely "
                        "stuck, say so plainly instead of ending with nothing."
                    )})
                    continue
                consecutive_empty_final_answers = 0

                _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                tokens_per_sec = round(last_eval_count / (last_eval_duration / 1e9), 1) if last_eval_count and last_eval_duration else None
                _log_turn_end("completed")
                yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'tokens_per_sec': tokens_per_sec, 'assistant_message_id': _msg_id})}\n\n"
                return

            made_any_tool_calls_this_turn = True
            consecutive_ungrounded_claims = 0

            _gate_was_open = _exploration_call_count < _SELF_INVESTIGATION_TOOL_THRESHOLD
            _plan_gate_was_open = _total_tool_call_count < _PLAN_REQUIRED_THRESHOLD
            _delegation_commitment_was_open = "plan_delegation" not in _called_tool_names
            # Snapshot of gate state as of the START of this round, before
            # this round's own calls get folded into the running totals
            # below -- passed into _execute_tool_call so a tool unlocked by
            # a call earlier THIS round can't be used until the round
            # after, matching what schema-hiding used to enforce.
            _pre_round_called = set(_called_tool_names)
            _pre_round_exploration_count = _exploration_call_count
            _pre_round_total_count = _total_tool_call_count
            for tc in round_tool_calls:
                _tc_name = tc.get("function", {}).get("name", "")
                _called_tool_names.add(_tc_name)
                if _tc_name in _EXPLORATION_TOOL_NAMES:
                    _exploration_call_count += 1
                _total_tool_call_count += 1

            # Model wants to call tool(s) -- execute each, tell the
            # frontend what's happening, then loop back with results
            # appended so the model can use them for its next turn.
            _messages.append({"role": "assistant", "content": round_reply, "tool_calls": round_tool_calls})
            for tc in round_tool_calls:
                tool_name = tc.get("function", {}).get("name", "unknown")
                log_trace_event(req.session_id, "tool_call", round=_round, tool=tool_name, args=tc.get("function", {}).get("arguments"))
                yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name})}\n\n"
                fingerprint = _tool_call_fingerprint(tc)
                repeat_count = _tool_call_fingerprints.count(fingerprint)
                similar_query = None
                if repeat_count < 2 and tool_name == "web_search":
                    fn_args = tc.get("function", {}).get("arguments", {})
                    if isinstance(fn_args, str):
                        try:
                            fn_args = json.loads(fn_args)
                        except json.JSONDecodeError:
                            fn_args = {}
                    query = fn_args.get("query", "") if isinstance(fn_args, dict) else ""
                    if query:
                        similar_query = next((q for q in _recent_search_queries if _search_query_similarity(query, q) >= 0.7), None)
                if repeat_count >= 2:
                    result = {
                        "error": f"BLOCKED: this exact {tool_name} call (same tool, same arguments) has already "
                        f"been made {repeat_count} times this turn. It will not be run again -- you already have "
                        f"its result from earlier in this conversation. Do not repeat it a third time; instead, "
                        f"either use the information you already gathered to reach a conclusion, or make a "
                        f"genuinely different call (different tool, or different arguments) if you actually need "
                        f"new information."
                    }
                elif similar_query:
                    result = {
                        "error": f"BLOCKED: this search is on essentially the same topic as an earlier query this "
                        f"turn ('{similar_query}'), just reworded. Reformulating a search doesn't give you new "
                        f"information if the underlying question is the same -- use what that earlier search "
                        f"already returned, or search for something genuinely different if you actually need it."
                    }
                else:
                    result = _execute_tool_call(tc, _pre_round_called, _pre_round_exploration_count, _pre_round_total_count)
                    _tool_call_fingerprints.append(fingerprint)
                    if len(_tool_call_fingerprints) > 20:
                        _tool_call_fingerprints.pop(0)
                    if tool_name == "web_search":
                        fn_args = tc.get("function", {}).get("arguments", {})
                        if isinstance(fn_args, str):
                            try:
                                fn_args = json.loads(fn_args)
                            except json.JSONDecodeError:
                                fn_args = {}
                        query = fn_args.get("query", "") if isinstance(fn_args, dict) else ""
                        if query:
                            _recent_search_queries.append(query)
                            if len(_recent_search_queries) > 10:
                                _recent_search_queries.pop(0)
                _result_error = result.get("error") if isinstance(result, dict) else None
                log_trace_event(
                    req.session_id, "tool_blocked" if isinstance(_result_error, str) and _result_error.startswith("BLOCKED:") else "tool_result",
                    round=_round, tool=tool_name, success=_result_error is None, output=result,
                )
                yield f"data: {json.dumps({'type': 'tool_output', 'tool': tool_name, 'output': result})}\n\n"
                # Saved in the same shape the frontend's own live tool-call
                # display already uses (tool/status/output) -- NOT the raw
                # LLM tool_calls format, which has no result field at all
                # and uses function.name instead of a plain tool name, so
                # a reloaded history entry actually matches what streaming
                # already shows instead of rendering empty.
                full_tool_calls.append({"tool": tool_name, "status": "done", "output": result})
                # The person always sees the real, full result above (nothing hidden) --
                # only what actually goes back into the model's own context gets compressed,
                # since that's the thing burning tokens, not what's shown in the UI.
                _result_for_context = result if tool_name in tool_output.UNCOMPRESSED_TOOL_NAMES else tool_output.compress_tool_result(result)
                _messages.append({"role": "tool", "content": json.dumps(_result_for_context)})

            if (_gate_was_open and _exploration_call_count >= _SELF_INVESTIGATION_TOOL_THRESHOLD
                    and mode == "athena_delegation" and not _investigation_gate_notified
                    and not (_called_tool_names & _DELEGATION_TOOL_NAMES)):
                _investigation_gate_notified = True
                _messages.append({"role": "user", "content": (
                    "You've made enough self-investigation tool calls this turn that direct exploration "
                    "(bash, read_file, list_files, search_codebase, find_definition, find_references, "
                    "type_info) is no longer available for the rest of this turn -- only delegation "
                    "(run_delegation_step/plan_delegation), any write tools, and your existing findings "
                    "so far remain. Use plan_delegation or run_delegation_step to get any further "
                    "investigation done instead of continuing to look yourself."
                )})

            if (_plan_gate_was_open and _total_tool_call_count >= _PLAN_REQUIRED_THRESHOLD
                    and not _plan_gate_notified and "write_plan" not in _called_tool_names):
                _plan_gate_notified = True
                _messages.append({"role": "user", "content": (
                    "You've made several tool calls this turn without writing a plan. Every tool except "
                    "write_plan is now unavailable until you call it. This does not limit how much you can "
                    "do for the rest of this turn -- once you call write_plan with the ordered steps you're "
                    "actually going to take, full tool access returns and stays available. Call write_plan now."
                )})

            if (mode == "athena_delegation" and _delegation_commitment_was_open
                    and "plan_delegation" in _called_tool_names and not _delegation_commitment_notified):
                _delegation_commitment_notified = True
                _messages.append({"role": "user", "content": (
                    "You called plan_delegation this turn, so run_delegation_step is no longer available for "
                    "the rest of this turn -- that commitment is permanent once made. If a step was rejected "
                    "or came back incomplete, do not work around it with a one-off run_delegation_step call: "
                    "fix the step (or steps) and call plan_delegation again with the corrected list."
                )})
        else:
            fallback_msg = "I wasn't able to settle on an answer after several tool calls -- the search results may be inconsistent or the page I need isn't easily fetchable. Try rephrasing, or ask me to check a specific source directly."
            full_reply = fallback_msg
            yield f"data: {json.dumps({'delta': fallback_msg})}\n\n"
            _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
            tokens_per_sec = round(last_eval_count / (last_eval_duration / 1e9), 1) if last_eval_count and last_eval_duration else None
            _log_turn_end("max_rounds_reached")
            yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'tokens_per_sec': tokens_per_sec, 'note': 'max tool rounds reached', 'assistant_message_id': _msg_id})}\n\n"

    # Run the generation loop above in a background thread, draining it
    # into a queue rather than handing it directly to this response --
    # this is the whole mechanism that lets it survive a closed tab or
    # navigation away. generate() itself is completely unmodified.
    gen = generate()
    event_queue = queue.Queue()
    generation_streaming.active_generations[req.session_id] = event_queue
    generation_streaming.generation_snapshots[req.session_id] = {"thinking": "", "content": "", "tool_calls": []}
    threading.Thread(target=generation_streaming.drain_generator_to_queue, args=(gen, event_queue, req.session_id), daemon=True).start()

    def _stream_from_queue():
        while True:
            chunk = event_queue.get()
            if chunk is None:
                break
            yield chunk

    return StreamingResponse(_stream_from_queue(), media_type="text/event-stream")



@app.get("/api/history/{session_id}")
def get_history(session_id: str):
    try:
        resp = httpx.get(f"{lcm_client.LCM_URL}/messages/{session_id}", timeout=3)
        if resp.status_code == 200:
            msgs = resp.json()
            from datetime import datetime
            result = []
            for m in msgs:
                ca = m.get("created_at")
                if isinstance(ca, str):
                    try:
                        dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
                        ca = int(dt.timestamp() * 1000)
                    except Exception:
                        ca = None
                result.append({
                    "role": m["role"], "content": m["content"], "messageId": m["id"], "model": m.get("model"),
                    "hasImage": m.get("has_image", False), "thinking": m.get("thinking"), "toolCalls": m.get("tool_calls"),
                    "created_at": ca,
                })
            return result
    except Exception:
        pass
    return []

class MemoryIn(BaseModel):
    content: str

@app.get("/api/memory")
def list_memory():
    try:
        resp = httpx.get(f"{lcm_client.LCM_URL}/facts", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.post("/api/memory")
def add_memory(req: MemoryIn):
    try:
        resp = httpx.post(f"{lcm_client.LCM_URL}/facts", json={"content": req.content, "manual": True}, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.delete("/api/memory/{fact_id}")
def delete_memory(fact_id: int):
    try:
        resp = httpx.delete(f"{lcm_client.LCM_URL}/facts/{fact_id}", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.delete("/api/messages/{message_id}")
def delete_message(message_id: int):
    try:
        resp = httpx.delete(f"{lcm_client.LCM_URL}/messages/{message_id}", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

class SessionMetaIn(BaseModel):
    id: str
    label: str
    pinned: bool = False
    created_at: float
    last_active: Optional[float] = None

@app.get("/api/sessions")
def list_sessions_proxy():
    try:
        resp = httpx.get(f"{lcm_client.LCM_URL}/sessions", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.post("/api/sessions")
def upsert_session_proxy(req: SessionMetaIn):
    try:
        resp = httpx.post(f"{lcm_client.LCM_URL}/sessions", json=req.model_dump(), timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

class ForkSessionProxyIn(BaseModel):
    message_id: int
    label: str

@app.post("/api/sessions/{session_id}/fork")
def fork_session_proxy(session_id: str, req: ForkSessionProxyIn):
    """Proxy to LCM's fork endpoint -- creates a new, independent
    session containing everything up to and including message_id,
    letting the user rewind to a point in a conversation and continue
    down a different path without losing or altering the original."""
    try:
        resp = httpx.post(f"{lcm_client.LCM_URL}/sessions/{session_id}/fork", json=req.model_dump(), timeout=15)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.delete("/api/history/{session_id}")
def delete_history(session_id: str):
    """Delete a chat session's messages and summary nodes from LCM --
    real data deletion, not just removing it from the sidebar list."""
    try:
        resp = httpx.delete(f"{lcm_client.LCM_URL}/session/{session_id}", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM delete failed: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM delete failed: {e}"}

def _get_gpu_stats():
    """Reads GPU utilization/memory straight from the driver via
    nvidia-smi -- deliberately NOT from any inference engine's own API,
    so this works the same whether the backend serving models is
    Ollama, llama.cpp, vLLM, or anything else. Returns None (not an
    error) on any non-NVIDIA machine so the UI can show 'No GPU
    detected' instead of breaking."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return None
        gpus = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 3:
                gpus.append({
                    "utilization_percent": float(parts[0]),
                    "memory_used_mb": float(parts[1]),
                    "memory_total_mb": float(parts[2]),
                })
        return gpus if gpus else None
    except Exception:
        return None

@app.get("/api/system/stats")
def system_stats():
    """Host-level resource stats -- RAM/CPU via psutil, GPU via
    nvidia-smi -- deliberately engine-agnostic. Which model/session is
    active is tracked client-side by Athena itself, not read from any
    particular inference engine's API."""
    vm = psutil.virtual_memory()
    cpu_percent = psutil.cpu_percent(interval=0.3)
    gpus = _get_gpu_stats()
    return {
        "ram": {"used_gb": round(vm.used / (1024 ** 3), 2), "total_gb": round(vm.total / (1024 ** 3), 2), "percent": vm.percent},
        "cpu_percent": cpu_percent,
        "gpus": gpus,
    }

class NotesIn(BaseModel):
    notes: list

@app.get("/api/notes")
def get_notes():
    with settings.notes_lock:
        return {"notes": settings.load_notes()}

@app.post("/api/notes")
def save_notes_endpoint(req: NotesIn):
    with settings.notes_lock:
        settings.save_notes(req.notes)
    return {"ok": True}

class MemoryModelIn(BaseModel):
    model: Optional[str] = None

class SettingsIn(BaseModel):
    workspace: Optional[str] = None
    endpoints: Optional[list] = None
    search_url: Optional[str] = None
    default_model: Optional[dict] = None
    theme: Optional[dict] = None
    model_aliases: Optional[dict] = None
    bot_creation_defaults: Optional[dict] = None
    athena_agent_model: Optional[dict] = None
    delegation_concurrency: Optional[int] = None
    bot_ptc_enabled: Optional[bool] = None
    delegation_async_enabled: Optional[bool] = None
    laya_gating_enabled: Optional[bool] = None

class MCPServerIn(BaseModel):
    name: str
    command: str
    args: Optional[list] = None

@app.get("/api/mcp/servers")
def list_mcp_servers():
    from mcp_manager import mcp_manager
    servers = mcp_manager.list_servers()
    for s in servers:
        tools = mcp_manager.list_tools(s["name"])
        s["tools"] = [{"name": t.name, "description": t.description, "inputSchema": t.input_schema} for t in tools]
    return {"servers": servers}

@app.post("/api/mcp/servers")
def add_mcp_server(req: MCPServerIn):
    """Adds a server to persisted config AND connects immediately,
    so the person sees real success/failure right away instead of
    only finding out on the next restart."""
    from mcp_manager import mcp_manager
    with settings.settings_lock:
        current_settings = settings.load_settings()
        servers = current_settings.setdefault("mcp_servers", [])
        servers = [s for s in servers if s["name"] != req.name]
        servers.append({"name": req.name, "command": req.command, "args": req.args or []})
        current_settings["mcp_servers"] = servers
        settings.save_settings(current_settings)
    try:
        tools = mcp_manager.connect_server(req.name, req.command, req.args or [])
        return {"connected": True, "tool_count": len(tools)}
    except Exception as e:
        return {"connected": False, "error": str(e)}

@app.delete("/api/mcp/servers/{name}")
def remove_mcp_server(name: str):
    from mcp_manager import mcp_manager
    mcp_manager.disconnect_server(name)
    with settings.settings_lock:
        current_settings = settings.load_settings()
        current_settings["mcp_servers"] = [s for s in current_settings.get("mcp_servers", []) if s["name"] != name]
        settings.save_settings(current_settings)
    return {"removed": True}

class MCPCallIn(BaseModel):
    server: str
    tool: str
    arguments: Optional[dict] = None

@app.post("/api/mcp/call")
def call_mcp_tool(req: MCPCallIn):
    """Generic tool-call passthrough -- Athena has no idea what any
    given tool does, it just forwards the call and hands back the raw
    result. If the result's text content happens to parse as JSON,
    that's included too, for the frontend's shape-sniffing renderer to
    work with; the raw text is always included as a safe fallback."""
    from mcp_manager import mcp_manager
    try:
        result = mcp_manager.call_tool(req.server, req.tool, req.arguments or {})
    except Exception as e:
        return {"error": str(e)}

    text_parts = [item.text for item in result.content if hasattr(item, "text")]
    raw_text = "\n".join(text_parts)

    parsed = None
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        pass

    return {"isError": getattr(result, "isError", False), "raw_text": raw_text, "parsed": parsed}

MCP_UIS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_uis")

def _serve_mcp_ui_file(server_name: str, path: str):
    """Serves a custom, hand-built UI for one MCP server, if the
    person has created one -- entirely optional plugin folders,
    gitignored by default, so a public release of Athena never ships
    anyone's bespoke tool-specific UI. Falls back to a short, friendly
    explainer (not a bare 404) when no folder exists, so this doubles
    as living documentation for building one."""
    if ".." in server_name or ".." in path:
        return HTMLResponse("Invalid path", status_code=400)

    server_dir = os.path.join(MCP_UIS_DIR, server_name)
    if not os.path.isdir(server_dir):
        return HTMLResponse(f"""
        <html><body style="font-family: sans-serif; padding: 2rem; color: #666; max-width: 640px; margin: 0 auto;">
        <h2>No custom UI for &#39;{server_name}&#39; yet</h2>
        <p>To build one, create a folder at <code>mcp_uis/{server_name}/</code> containing an <code>index.html</code> (plus any JS/CSS/images it needs). It loads in an isolated iframe, so it can be styled however you like.</p>
        <p>It can call Athena&#39;s existing generic endpoints to interact with this server:</p>
        <ul>
            <li><code>GET /api/mcp/servers</code> &mdash; list connected servers and their discovered tools</li>
            <li><code>POST /api/mcp/call</code> &mdash; call a tool: <code>{{"server": "{server_name}", "tool": "...", "arguments": {{...}}}}</code></li>
        </ul>
        </body></html>
        """)

    if not path:
        path = "index.html"
    file_path = os.path.join(server_dir, path)
    real_server_dir = os.path.realpath(server_dir)
    real_file_path = os.path.realpath(file_path)
    if not real_file_path.startswith(real_server_dir):
        return HTMLResponse("Invalid path", status_code=400)
    if not os.path.isfile(file_path):
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(file_path)

def _load_mcp_ui_backends():
    """Generic plugin mechanism: any mcp_uis/<name>/backend.py that
    defines a FastAPI APIRouter named `router` gets mounted under
    /mcp-ui-api/<name>/. Athena's core has zero knowledge of what's
    inside a plugin's backend -- this is the one place tool-specific
    server-side logic is allowed to exist at all, deliberately
    isolated to plugin folders that are gitignored by default, never
    touching Athena's own codebase."""
    import importlib.util
    if not os.path.isdir(MCP_UIS_DIR):
        return
    for name in os.listdir(MCP_UIS_DIR):
        backend_path = os.path.join(MCP_UIS_DIR, name, "backend.py")
        if os.path.isfile(backend_path):
            try:
                spec = importlib.util.spec_from_file_location(f"mcp_ui_backend_{name}", backend_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "router"):
                    app.include_router(module.router, prefix=f"/mcp-ui-api/{name}")
                    logger.info(f"Loaded custom backend for MCP UI plugin '{name}'")
            except Exception as e:
                logger.error(f"Failed to load custom backend for MCP UI plugin '{name}': {e}")

_load_mcp_ui_backends()

@app.get("/mcp-ui/{server_name}")
def serve_mcp_ui_root(server_name: str):
    return _serve_mcp_ui_file(server_name, "index.html")

@app.get("/mcp-ui/{server_name}/{path:path}")
def serve_mcp_ui_file(server_name: str, path: str):
    return _serve_mcp_ui_file(server_name, path)

@app.get("/api/settings")
def get_settings():
    return settings.load_settings()

@app.post("/api/settings")
def update_settings(req: SettingsIn):
    """Partial update -- only fields actually present in the request
    body get merged in, so one device saving just its workspace change
    can never accidentally wipe out another device's endpoints list or
    default model. Lock-guarded end to end (read, merge, write) so two
    devices saving at nearly the same moment serialize safely instead
    of one's update silently clobbering the other's."""
    with settings.settings_lock:
        current_settings = settings.load_settings()
        current_settings.update(req.model_dump(exclude_unset=True))
        settings.save_settings(current_settings)
        return current_settings

@app.get("/api/settings/memory-model")
def get_memory_model():
    return {"model": settings.load_settings().get("memory_extraction_model")}

@app.post("/api/settings/memory-model")
def set_memory_model(req: MemoryModelIn):
    current_settings = settings.load_settings()
    current_settings["memory_extraction_model"] = req.model
    settings.save_settings(current_settings)
    return {"model": req.model}

def _get_memory_context() -> str:
    """Formats current durable facts (from every session, not just this
    one) into a system-prompt block. This is the actual payoff of the
    whole memory-extraction pipeline -- without this, facts would just
    sit in LCM's database having zero effect on any conversation.
    Returns an empty string when there are no facts yet, so a fresh
    install with nothing learned behaves identically to before this
    feature existed."""
    try:
        resp = httpx.get(f"{lcm_client.LCM_URL}/facts", timeout=5)
        if resp.status_code != 200:
            return ""
        facts = resp.json()
    except Exception:
        return ""
    if not facts:
        return ""
    facts_list = "\n".join(f"- {f['content']}" for f in facts)
    return (
        "\n\nWhat you know about the user from PAST conversations "
        "(not just this one) -- treat this as settled background, not "
        "something to re-derive, question, or bring up unprompted "
        "unless it's actually relevant to what they're asking right now:\n"
        f"{facts_list}"
    )

_MEMORY_SCAN_INTERVAL_SECONDS = 60
_MEMORY_IDLE_THRESHOLD_SECONDS = 600  # 10 minutes

_MEMORY_EXTRACTION_SYSTEM_PROMPT = """You are a careful memory-extraction assistant. You are shown a chunk of recent conversation transcript, plus a list of facts already known about the user. Your ONLY job is to identify NEW, durable, genuinely important facts about the user that are NOT already covered by the existing facts list -- even if worded differently.

Rules:
- Only extract facts the user stated directly about themselves: identity, stated preferences, ongoing projects, explicit decisions. Never the assistant's own suggestions, and never a guess or inference the user didn't actually state.
- Skip anything trivial, one-off, or already covered by an existing fact in meaning, not just exact wording.
- When in doubt, extract NOTHING. A missed fact is cheap to catch later; a wrong or duplicate one is expensive since it pollutes every future conversation until someone notices and deletes it.
- Never invent facts not actually present in the transcript.

Respond with ONLY a JSON array of new fact strings, e.g. ["Works as a mechanical engineer", "Prefers dark roast coffee"]. If there is nothing new and genuinely worth keeping, respond with exactly: []"""

def _run_memory_scan(model: str):
    scan_state = httpx.get(f"{lcm_client.LCM_URL}/scan-state", timeout=10).json()
    last_id = scan_state.get("last_scanned_message_id", 0)

    new_messages = httpx.get(f"{lcm_client.LCM_URL}/messages/since/{last_id}", timeout=10).json()
    if not new_messages:
        return  # nothing new -- also naturally prevents re-scanning during a long idle stretch

    existing_facts = httpx.get(f"{lcm_client.LCM_URL}/facts", timeout=10).json()
    existing_facts_text = "\n".join(f"- {f['content']}" for f in existing_facts) or "(none yet)"
    transcript_text = "\n".join(f"[{m['role']}] {m['content']}" for m in new_messages)

    max_id_seen = max(m["id"] for m in new_messages)
    last_session_id = new_messages[-1]["session_id"]

    host_key = host_locks.endpoint_host(OLLAMA_URL)
    lock = host_locks.get_host_lock(host_key)
    try:
        with lock:
            resp = httpx.post(OLLAMA_URL, json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _MEMORY_EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Existing facts already known:\n{existing_facts_text}\n\nNew conversation since last scan:\n{transcript_text}"},
                ],
                "options": {"num_ctx": 32768},
                "stream": False,
                "think": False,
            }, timeout=120)
        raw = resp.json()["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.strip("`").lstrip("json").strip()
        new_facts = json.loads(raw)
    except Exception as e:
        logger.warning(f"Memory scan: extraction failed, will retry next cycle: {e}")
        return  # deliberately do NOT advance scan-state, so this batch gets retried

    for fact_text in new_facts:
        if isinstance(fact_text, str) and fact_text.strip():
            httpx.post(f"{lcm_client.LCM_URL}/facts", json={
                "content": fact_text.strip(),
                "source_session_id": last_session_id,
                "source_message_id": max_id_seen,
                "manual": False,
            }, timeout=10)

    if new_facts:
        logger.info(f"Memory scan: added {len(new_facts)} new fact(s)")
    httpx.post(f"{lcm_client.LCM_URL}/scan-state", json={"last_scanned_message_id": max_id_seen}, timeout=10)

def _memory_scan_loop():
    logger.info("Memory scan loop started (checks every 60s)")
    while True:
        time.sleep(_MEMORY_SCAN_INTERVAL_SECONDS)
        try:
            model = settings.load_settings().get("memory_extraction_model")
            if not model:
                logger.debug("Memory scan check: no model configured, skipping")
                continue  # no model configured -- do nothing, no error
            idle_seconds = time.time() - activity.last_activity_ts
            if idle_seconds < _MEMORY_IDLE_THRESHOLD_SECONDS:
                remaining = int(_MEMORY_IDLE_THRESHOLD_SECONDS - idle_seconds)
                logger.debug(f"Memory scan check: still active, {remaining}s until idle threshold")
                continue  # still active, wait for a real idle window
            logger.info(f"Memory scan check: idle threshold reached, scanning with {model}")
            _run_memory_scan(model)
        except Exception as e:
            logger.warning(f"Memory scan loop error (will retry next cycle): {e}")

@app.get("/health")
def health():
    return {"status": "ok"}


def _check_requirements():
    """Verify every package pinned in requirements.txt is actually
    installed before Athena tries to boot, so a missing dependency (e.g.
    someone cloning this fresh without running pip install -r
    requirements.txt) surfaces as one clear message here instead of a
    confusing traceback the first time some unrelated route imports it."""
    import importlib.metadata as _im
    req_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    if not os.path.exists(req_path):
        return
    missing = []
    mismatched = []
    with open(req_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "==" not in line:
                continue
            name, pinned_version = line.split("==", 1)
            try:
                installed_version = _im.version(name)
                if installed_version != pinned_version:
                    mismatched.append(f"{name} (have {installed_version}, requirements.txt has {pinned_version})")
            except _im.PackageNotFoundError:
                missing.append(name)
    if missing:
        logger.error(f"Missing required packages: {', '.join(missing)}. Run: pip install -r requirements.txt")
        sys.exit(1)
    if mismatched:
        logger.warning("Version mismatch (may still work fine): " + "; ".join(mismatched))

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Athena -- chat/agent workspace")
    parser.add_argument("--reindex", action="store_true", help="Re-index the codebase for RAG and exit")
    args = parser.parse_args()

    if args.reindex:
        logger.info("Re-indexing codebase for RAG...")
        db_path = os.environ.get("ATHENA_RAG_DB", "rag_index.db")
        # Clear existing index by deleting the old DB file
        if os.path.exists(db_path):
            os.remove(db_path)
            logger.info(f"Removed existing RAG index: {db_path}")
        # Create fresh index
        fresh_rag = SimpleCodeRAG(db_path)
        fresh_rag.index_codebase(root_dir=".")
        # Count indexed chunks
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM chunks")
        count = cursor.fetchone()[0]
        conn.close()
        logger.info(f"Re-index complete: {count} chunks indexed in {db_path}")
        sys.exit(0)

    _check_requirements()
    lcm_client.start_bundled_lcm()

    def _startup_reindex():
        # Warm the embedding model here, explicitly, before touching any
        # files -- so the potentially slow first-run model load (and its
        # own [RAG] log lines, see rag._get_embedder) happens at a known
        # point in the background at boot, decoupled from whichever thread
        # (this one or a live chat request's search_codebase call) would
        # otherwise have triggered it incidentally first.
        rag.warm_up()
        logger.info("Re-indexing codebase for RAG...")
        try:
            rag.index_codebase(".")
        except Exception as e:
            logger.error(f"startup reindex failed: {e!r}")
            return
        logger.info("RAG re-index complete.")
    threading.Thread(target=_startup_reindex, daemon=True).start()

    threading.Thread(target=_memory_scan_loop, daemon=True).start()
    threading.Thread(target=task_scheduler._task_scheduler_loop, daemon=True).start()

    from mcp_manager import mcp_manager
    mcp_manager.start()
    for server_cfg in settings.load_settings().get("mcp_servers", []):
        try:
            tools = mcp_manager.connect_server(server_cfg["name"], server_cfg["command"], server_cfg.get("args", []))
            logger.info(f"Connected to MCP server '{server_cfg['name']}' -- {len(tools)} tools discovered")
        except Exception as e:
            logger.error(f"Failed to connect to MCP server '{server_cfg['name']}': {e}")

    port = int(os.environ.get("ATHENA_PORT", "9500"))
    uvicorn.run(app, host="0.0.0.0", port=port)
