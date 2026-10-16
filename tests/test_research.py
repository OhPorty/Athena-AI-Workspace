import hashlib
import re

import numpy as np
import pytest
from fastapi.testclient import TestClient

import research


def fake_embed(texts):
    """Bag-of-words hashing: texts sharing words are similar; unrelated ones are not."""
    out = np.zeros((len(texts), 64))
    for i, t in enumerate(texts):
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            out[i][int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.where(norms == 0, 1, norms)


PAGES = {
    "https://a.example.com/eiffel": "The Eiffel Tower is located in Paris, France. The Eiffel Tower was completed in 1889.",
    "https://b.example.org/tower": "The Eiffel Tower stands in Paris. The Eiffel Tower was completed in 1889. The Eiffel Tower is 330 metres tall.",
    "https://c.example.net/height": "Many sources are wrong. The Eiffel Tower is 300 metres tall.",
}


class Script:
    """Stands in for the model: answers each kind of narrow job from the prompt itself."""
    def __init__(self, bad_quotes=False):
        self.calls = []
        self.bad_quotes = bad_quotes

    def __call__(self, model, prompt, system="", ctx=12288, think="none"):
        self.calls.append(prompt)
        if "Rewrite the follow-up" in prompt:
            return "How many steps does the Eiffel Tower have?"
        if "follow-up questions" in prompt:
            return "1. How much did it cost to build?\n2. Who designed it?\n3. How tall is the Eiffel Tower?\n"
        if "planning web research" in prompt:
            return "1. eiffel tower location\n2. eiffel tower height\n"
        if "List every fact in the text" in prompt:
            text = prompt.split('"""')[1]
            out = []
            for sentence in re.split(r"(?<=\.)\s+", text.strip()):
                if "tall" in sentence or "1889" in sentence or "located" in sentence or "steps" in sentence:
                    quote = "this sentence was never on the page" if self.bad_quotes else sentence
                    out.append(f"CLAIM: {sentence}\nQUOTE: {quote}")
            return "\n".join(out) or "NONE"
        if "A quote" in prompt:
            return "yes"
        if "Statement A" in prompt:
            a, b = re.findall(r"Statement [AB]: (.+)", prompt)
            nums = lambda s: set(re.findall(r"\d{3} metres", s))
            if nums(a) and nums(b) and nums(a) != nums(b):
                return "conflict"
            return "same" if set(re.findall(r"1889|Paris", a)) & set(re.findall(r"1889|Paris", b)) else "different"
        if "Which parts of the question" in prompt:
            return "the exact opening day cost"
        if "Checked claims" in prompt:
            ids = [m[0] for m in re.findall(r"\[(C\d+)\] \([^)]*\) (.*1889.*)", prompt)] or re.findall(r"\[(C\d+)\]", prompt)
            return f"The tower was finished in 1889 [{ids[0]}]. This sentence cites nothing. It is in Paris [{ids[0]}, C999]."
        raise AssertionError("unexpected prompt: " + prompt[:80])


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(research, "_embed", fake_embed)
    monkeypatch.setattr(research, "search", lambda q, n: [{"url": u, "title": u.split("/")[-1]} for u in PAGES][:n])
    monkeypatch.setattr(research, "fetch_text", lambda url: (PAGES[url], False, None))
    script = Script()
    monkeypatch.setattr(research, "_complete", script)
    return script


MODEL = {"model": "fake", "label": "fake"}


@pytest.fixture(autouse=True)
def no_system1(monkeypatch):
    monkeypatch.setattr(research.system1, "available", lambda: False)


def run_to_end(question="How tall is the Eiffel Tower and where is it?", depth="quick", check=None):
    started = research.start_run(question, depth, MODEL, check, wait=True)
    return research.get_run(started["id"])


def test_full_run_keeps_only_checked_cited_claims(world):
    run = run_to_end()
    assert run["status"] == "done", run.get("error")
    assert len([s for s in run["sources"] if s["status"] == "read"]) == 3
    supported = [c for c in run["claims"] if c["supported"]]
    assert supported and all(c["quote_found"] for c in supported)
    report = run["report"]
    assert "finished in 1889." in report and "[[1]](https://a.example.com" in report.replace("\\", "")
    assert "This sentence cites nothing" not in report
    assert "C999" not in report and "[C" not in report
    assert "How this was checked" in report
    assert "## Not established" in report and "opening day cost" in report


def test_conflicting_sites_are_reported_not_averaged(world):
    run = run_to_end()
    assert run["disputes"], run["claims"]
    assert "Where sources disagree" in run["report"]
    verdicts = {c["verdict"] for c in run["claims"] if c["supported"]}
    assert "disputed" in verdicts and "corroborated" in verdicts


def test_claims_whose_quote_is_not_on_the_page_are_dropped(world):
    world.bad_quotes = True
    run = run_to_end()
    assert run["status"] == "done"
    assert run["claims"] and not any(c["supported"] for c in run["claims"])
    assert "nothing reliable" in run["report"].lower()
    assert not any("Checked claims" in p for p in world.calls)          # nothing unverified ever reaches the writer


def test_quote_matching_rules():
    page = "The tower, completed in 1889, is 330 metres tall.  It stands in Paris."
    assert research.quote_in_text("completed in 1889, is 330 metres tall", page)
    assert research.quote_in_text("The tower ... stands in Paris", page)
    assert not research.quote_in_text("completed in 1887", page)
    assert not research.quote_in_text("short", "short")                  # too short to prove anything


def test_same_site_does_not_count_as_corroboration(monkeypatch):
    monkeypatch.setattr(research, "_embed", fake_embed)
    claims = [{"id": f"C{i}", "domain": "same.com", "text": "The tower was completed in 1889 in Paris", "supported": True} for i in (1, 2)]
    research.compare_claims(claims, lambda *a, **k: "same")
    assert {c["verdict"] for c in claims} == {"single-source"}


def test_search_not_configured_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(research, "_complete", Script())
    monkeypatch.setattr(research, "_search_url", lambda: "")
    run = run_to_end()
    assert run["status"] == "error" and "search" in run["error"].lower()


def test_separate_checker_model_is_used_for_checks(monkeypatch, world):
    used = []
    monkeypatch.setattr(research, "_complete", lambda model, prompt, **kw: (used.append((model["model"], prompt[:20])), world(model, prompt))[1])
    run = research.get_run(research.start_run("Where is the Eiffel Tower?", "quick", MODEL, {"model": "checker"}, wait=True)["id"])
    assert run["status"] == "done"
    assert {m for m, p in used if p.startswith("A quote")} == {"checker"}
    assert {m for m, p in used if "planning" in p or p.startswith("Question:")} <= {"fake"}


def test_cancel(world, monkeypatch):
    run_holder = {}
    original = research.search

    def slow_search(q, n):
        research.cancel_run(run_holder["id"])
        return original(q, n)
    monkeypatch.setattr(research, "search", slow_search)
    monkeypatch.setattr(research.threading.Thread, "start", lambda self: None)       # we drive the run ourselves
    started = research.start_run("q?", "quick", MODEL)
    run_holder["id"] = started["id"]
    run = research.get_run(started["id"])
    research._execute(run, {"plan": MODEL, "extract": MODEL, "write": MODEL, "check": MODEL})
    assert research.get_run(started["id"])["status"] == "cancelled"


def test_validation(world):
    assert "error" in research.start_run("  ", "quick", MODEL)
    assert "error" in research.start_run("q", "enormous", MODEL)
    assert "error" in research.start_run("q", "quick", {})


def test_interrupted_runs_are_marked(world):
    run = {"id": "x1", "question": "q", "status": "running", "created_at": 1.0, "events": [], "claims": [], "sources": []}
    research.save_run(run)
    research.mark_interrupted()
    assert research.get_run("x1")["status"] == "error"


def test_routes(world):
    from fastapi import FastAPI
    app = FastAPI(); app.include_router(research.router)
    client = TestClient(app)
    payload = {"question": "Where is the Eiffel Tower?", "depth": "quick", "plan_model": {"model": "fake"}}
    rid = client.post("/api/research", json=payload).json()["id"]
    import time
    for _ in range(100):
        if client.get(f"/api/research/{rid}").json()["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert client.get(f"/api/research/{rid}").json()["status"] == "done"
    listing = client.get("/api/research").json()["runs"]
    assert listing[0]["id"] == rid and listing[0]["verified_claims"] > 0 and "report" not in listing[0]
    assert client.delete(f"/api/research/{rid}").json()["removed"]
    assert client.get("/api/research").json()["runs"] == []


def test_page_markup_cannot_reach_the_report():
    assert "<" not in research._inert("<img src=x onerror=alert(1)>hello")
    assert research.extract_claims("q", "text", lambda *a, **k: "CLAIM: <script>x</script> fact\nQUOTE: some quote here")[0][0].count("<") == 0


def test_footnote_markers_and_table_layout_do_not_break_quotes():
    page = "Height\nArchitectural\n300\nm (984\nft)\n[\n2\n]\nTip\n330\nm (1,083\nft)\nCompletion Date:\n1889\nHeight:\n986 feet"
    assert research.quote_in_text("Architectural 300 m (984 ft); Tip 330 m (1,083 ft)", page)
    assert research.quote_in_text("Height: 986 feet", page)
    assert not research.quote_in_text("Height: 987 feet", page)


def test_sentences_must_match_the_claims_they_cite(monkeypatch):
    monkeypatch.setattr(research, "_embed", fake_embed)
    claims = {"C1": {"id": "C1", "source": "S1", "text": "The tower was completed in 1889.", "verdict": "single-source"}}
    sources = {"S1": {"n": 1, "url": "https://a.example.com"}}
    draft = ("The tower was completed in 1889 [C1]. The tower was completed in 1887 [C1]. "
             "Visitors loved the sparkling lights at night and bought many souvenirs [C1].")
    report, dropped = research.finalize_report(draft, claims, sources)
    assert "1889" in report and "1887" not in report and "souvenirs" not in report and dropped == 2


def test_decision_model_does_the_checks_without_text_generation(world, monkeypatch):
    asked = []
    def decider(model):
        def decide(state, questions):
            asked.append(set(questions))
            if "page_title" in state:
                return {"r": {"noul": 0.9}}
            if "s" in questions:
                return {"s": {"noul": 0.9 if "Eiffel" in state["quote"] or state["claim"] in state["quote"] else 0.1}}
            return {"r": {"probabilities": {"agree": 0.9, "contradict": 0.05, "unrelated": 0.05}}}
        return decide
    monkeypatch.setattr(research, "make_decider", decider)
    run = research.get_run(research.start_run("Where is the Eiffel Tower?", "quick", MODEL, {"model": "tev1:0.8b", "system1": True}, wait=True)["id"])
    assert run["status"] == "done", run.get("error")
    assert {"s"} in asked
    assert not any("A quote" in p or "Statement A" in p for p in world.calls)     # the generative model never judged anything
    assert any(c["supported"] for c in run["claims"])


def test_system_one_is_used_automatically_when_available(world, monkeypatch):
    monkeypatch.setattr(research.system1, "available", lambda: True)
    monkeypatch.setattr(research.system1, "config", lambda: {"model": "tev1:0.8b", "endpoint_url": "http://x"})
    monkeypatch.setattr(research, "make_decider", lambda model: (lambda state, questions: {"s": {"noul": 0.9}, "r": {"noul": 0.9, "probabilities": {"agree": 0.9}}}))
    run = research.get_run(research.start_run("Where is the Eiffel Tower?", "quick", MODEL, wait=True)["id"])
    assert run["status"] == "done" and run["models"]["check"]["system1"] and "System 1" in run["models"]["check"]["label"]
    assert not any("A quote" in p for p in world.calls)


STEPS_PAGE = "https://d.example.com/steps"


def first_run(world):
    return run_to_end()


def follow_up(parent, question="What about the number of steps?", depth="quick"):
    started = research.start_run(question, depth, MODEL, parent_id=parent["id"], wait=True)
    return started, research.get_run(started["id"]) if "id" in started else None


def test_a_finished_run_offers_follow_up_questions(world):
    run = first_run(world)
    assert run["suggestions"] == ["How much did it cost to build?", "Who designed it?", "How tall is the Eiffel Tower?"]


def test_follow_up_starts_from_what_was_already_checked(world, monkeypatch):
    root = first_run(world)
    asked_to_search = []
    pages = dict(PAGES, **{STEPS_PAGE: "The Eiffel Tower has 1,665 steps to the top."})
    monkeypatch.setattr(research, "fetch_text", lambda url: (pages[url], False, None))
    monkeypatch.setattr(research, "search", lambda q, n: (asked_to_search.append(q), [{"url": u, "title": u.split("/")[-1]} for u in pages][:n])[1])
    _, run = follow_up(root)
    assert run["status"] == "done", run.get("error")
    assert run["root_id"] == root["id"] and run["parent_id"] == root["id"]
    assert run["search_question"] == "How many steps does the Eiffel Tower have?" and asked_to_search[0] == run["search_question"]
    urls = [s["url"] for s in run["sources"]]
    assert len(urls) == len(set(urls)) == 4                                    # the three known pages were reused, one is new
    inherited = [c for c in run["claims"] if c.get("inherited")]
    assert inherited and all(c["supported"] for c in inherited)
    new = [c for c in run["claims"] if not c.get("inherited")]
    assert new and any("steps" in c["text"] for c in new)
    assert len({c["id"] for c in run["claims"]}) == len(run["claims"])         # ids stay unique after merging
    assert "How this was checked" in run["report"] and run["suggestions"]


def test_follow_up_does_not_rejudge_pairs_it_already_settled(world, monkeypatch):
    root = first_run(world)
    judged = []
    real = research.judge_relation
    monkeypatch.setattr(research, "judge_relation", lambda a, b, *x, **k: (judged.append((a, b)), real(a, b, *x, **k))[1])
    _, run = follow_up(root)
    carried_texts = {c["text"] for c in run["claims"] if c.get("inherited")}
    assert not any(a in carried_texts and b in carried_texts for a, b in judged)
    assert {c["verdict"] for c in run["claims"] if c.get("inherited")} & {"corroborated", "disputed"}   # earlier findings kept


def test_follow_up_is_refused_while_the_thread_is_running_or_has_nothing_finished(world):
    root = first_run(world)
    research.save_run(dict(root, id="busy", root_id=root["id"], status="running"))
    assert "Wait" in research.start_run("more?", "quick", MODEL, parent_id=root["id"])["error"]
    research.delete_run("busy")
    failed = dict(root, id="failed", status="error", root_id=None)
    research.save_run(failed)
    assert "no finished research" in research.start_run("more?", "quick", MODEL, parent_id="failed")["error"]
    assert "no longer exists" in research.start_run("more?", "quick", MODEL, parent_id="nope")["error"]


def test_the_list_shows_only_first_questions_and_the_thread_has_everything(world):
    root = first_run(world)
    _, followup = follow_up(root)
    listing = research.list_runs()
    assert [r["id"] for r in listing] == [root["id"]] and listing[0]["followups"] == 1
    assert [r["id"] for r in research.thread_of(followup["id"])] == [root["id"], followup["id"]]       # either id finds the thread
    research.save_run(dict(followup, id="live", status="running", detail="Reading pages", created_at=followup["created_at"] + 1))
    assert research.list_runs()[0]["status"] == "running" and research.list_runs()[0]["detail"] == "Reading pages"


def test_deleting_a_first_question_deletes_its_follow_ups_but_not_the_reverse(world):
    root = first_run(world)
    _, f1 = follow_up(root)
    _, f2 = follow_up(f1)
    assert f2["root_id"] == root["id"]
    research.delete_run(f1["id"])
    assert [r["id"] for r in research.thread_of(root["id"])] == [root["id"], f2["id"]]
    research.delete_run(root["id"])
    assert research.thread_of(f2["id"]) == [] and research.list_runs() == []


def test_carried_context_merges_runs_without_duplicates(world):
    root = first_run(world)
    _, f1 = follow_up(root)
    sources, claims = research.carried_context(research.thread_of(root["id"]))
    assert len({s["url"] for s in sources}) == len(sources) and len({(c["source"], c["text"]) for c in claims}) == len(claims)
    ids = {c["id"] for c in claims}
    assert all(set(c["agree"]) | set(c["conflict"]) <= ids for c in claims)             # relations were renumbered with the claims
    assert all(c["source"] in {s["id"] for s in sources} for c in claims)


def test_thread_routes(world):
    from fastapi import FastAPI
    app = FastAPI(); app.include_router(research.router)
    client = TestClient(app)
    root = first_run(world)
    body = {"question": "What about the steps?", "depth": "quick", "plan_model": {"model": "fake"}, "parent_id": root["id"]}
    rid = client.post("/api/research", json=body).json()["id"]
    import time
    for _ in range(100):
        if client.get(f"/api/research/{rid}").json()["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    thread = client.get(f"/api/research/{rid}/thread").json()["runs"]
    assert [r["id"] for r in thread] == [root["id"], rid]
    assert "error" in client.get("/api/research/missing/thread").json()


def test_pages_about_something_else_are_skipped_before_any_claim_is_taken(world, monkeypatch):
    seen_titles = []
    def decider(model):
        def decide(state, questions):
            if "page_title" in state:
                seen_titles.append(state["page_title"])
                return {"r": {"noul": 0.1 if "eiffel" in state["page_title"] else 0.9}}
            if "s" in questions:
                return {"s": {"noul": 0.9}}
            return {"r": {"probabilities": {"agree": 0.9}}}
        return decide
    monkeypatch.setattr(research, "make_decider", decider)
    run = research.get_run(research.start_run("Where is the Eiffel Tower?", "quick", MODEL, {"model": "tev1", "system1": True}, wait=True)["id"])
    skipped = [s for s in run["sources"] if s["status"] == "off_topic"]
    assert [s["title"] for s in skipped] == ["eiffel"] and len(seen_titles) == 3
    assert not any(c["source"] == skipped[0]["id"] for c in run["claims"])
    assert "pages were read" in run["report"] and run["status"] == "done"


def test_indented_bullets_do_not_turn_into_code_blocks(monkeypatch):
    monkeypatch.setattr(research, "_embed", fake_embed)
    claims = {"C1": {"id": "C1", "source": "S1", "text": "The tower was completed in 1889.", "verdict": "single-source"}}
    report, _ = research.finalize_report("Intro [C1].\n\n    *   The tower was completed in 1889 [C1].\n    1. The tower was completed in 1889 [C1].", claims, {"S1": {"n": 1, "url": "https://a.example.com"}})
    assert not any(line.startswith("    ") for line in report.splitlines()) and "* The tower" in report and "1. The tower" in report
