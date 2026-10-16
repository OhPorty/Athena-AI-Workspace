"""LCM keeps a model's context small on purpose: a few recent messages plus short summaries. These pin down that it stays that
way, and that the details a small summarizer reliably drops (links, file names) survive in the summaries anyway."""
import importlib.util
import pathlib
import sys

import pytest

LCM_DIR = pathlib.Path(__file__).resolve().parent.parent / "lcm"
URL = "https://example.com/shop"


@pytest.fixture
def lcm(tmp_path, monkeypatch):
    monkeypatch.setenv("LCM_DB_PATH", str(tmp_path / "lcm.db"))           # importing the service creates its database in this path
    monkeypatch.syspath_prepend(str(LCM_DIR))
    spec = importlib.util.spec_from_file_location("lcm_server_under_test", LCM_DIR / "server.py")
    server = importlib.util.module_from_spec(spec)
    sys.modules.pop("lcm_core", None)
    spec.loader.exec_module(server)
    server.lcm.summarizer = lambda transcript, *a, **k: "The user and assistant discussed the page."      # a small model's summary: no link in it
    return server


def say(server, session, n, size=800, first=None):
    for i in range(n):
        text = (first if (i == 0 and first) else f"message {i} ") + "x" * size
        server.add_message(server.MessageIn(session_id=session, role="user" if i % 2 == 0 else "assistant", content=text))


def summaries(context):
    return [m["content"] for m in context if m["content"].startswith("[Summary")]


def test_the_context_stays_small_however_long_the_conversation(lcm):
    say(lcm, "s", 60)                                                     # about 12,000 tokens of conversation
    ctx = lcm.get_active_context("s")["context"]
    raw = [m for m in ctx if m["role"] != "system"]
    assert len(raw) == 4 and raw[-1]["content"].startswith("message 59 ")
    assert sum(len(m["content"]) for m in ctx) < 6000                     # a few messages and short summaries, not the chat


def test_a_link_given_long_ago_survives_in_the_summaries_even_when_the_summary_drops_it(lcm):
    say(lcm, "s", 40, first=f"Please restyle {URL} and edit preview-header.html for me. ")
    ctx = lcm.get_active_context("s")["context"]
    joined = " ".join(summaries(ctx))
    assert URL in joined and "preview-header.html" in joined
    assert URL not in " ".join(m["content"] for m in ctx if m["role"] != "system")       # it really was compacted out of the recent messages


def test_links_survive_summaries_of_summaries(lcm):
    say(lcm, "s", 120, first=f"The site is {URL} ")
    ctx = lcm.get_active_context("s")["context"]
    assert lcm.lcm._conn().execute("select max(level) from nodes").fetchone()[0] >= 2          # folding really happened
    assert URL in " ".join(summaries(ctx))
    assert sum(len(m["content"]) for m in ctx) < 8000


def test_what_a_summary_adds_is_a_line_or_two_not_more(lcm):
    out = lcm.exact_details([f"see {URL}, and (https://a.example.com/x?y=1). Edit style.css and src/app.js; version 1.2 is fine"])
    assert URL in out and "https://a.example.com/x?y=1" in out and "style.css" in out and "src/app.js" in out and "1.2" not in out
    assert lcm.exact_details(["nothing special here"]) == ""
    many = lcm.exact_details([" ".join(f"https://example.com/page{i}" for i in range(100))])
    assert many.count("https://") == 12 and len(many) < 1200


def test_the_model_is_told_summaries_are_short_and_where_to_look_things_up(lcm):
    say(lcm, "short", 3)
    assert not any(m["content"].startswith("Earlier parts") for m in lcm.get_active_context("short")["context"])       # nothing summarized, no note
    say(lcm, "long", 30)
    first = lcm.get_active_context("long")["context"][0]
    assert first["role"] == "system" and "lcm_recall_search" in first["content"] and "lcm_recall_expand" in first["content"]
