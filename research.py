"""Research: answer a question from the web, and check the answer before presenting it.

The flow is owned by code; the model only does small, separate jobs (propose searches, pull facts with
quotes, judge one claim, write from checked claims). Nothing in the report can come from anywhere but a page
that was actually fetched:

  plan      the model proposes web searches
  gather    search, then fetch and read each page
  extract   the model pulls claims out of the most relevant passages, each with a quote; code drops any claim
            whose quote is not really in the page
  verify    a separate call sees only the claim and its quote and judges whether the quote supports it; claims
            from different sites that say the same thing corroborate each other, and ones that cannot both be
            true are reported as a dispute instead of being averaged away
  write     the model writes from the verified claims only; code removes every sentence without a valid
            citation, turns claim ids into links to the pages, and adds what was not found and how it was checked

What this proves is that sources say a thing and that independent sources agree. It does not prove the thing
is true, and the report says so in its own words.
"""
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

import db
import ownership
import prompt_gate
import host_locks
import settings
import system1
import web_tools
from logging_setup import logger

router = APIRouter()

DEPTHS = {
    "quick": {"queries": 3, "results": 4, "passages": 2},
    "standard": {"queries": 5, "results": 5, "passages": 4},
    "thorough": {"queries": 8, "results": 6, "passages": 6},
}
_WINDOW = 4500
_WINDOW_OVERLAP = 300
_PAGE_TEXT_LIMIT = 60000
_MIN_QUOTE = 12
_PAIR_SIMILARITY = 0.70
_RUNNING = ("queued", "running")
_run_lock = threading.Lock()                 # one run at a time: they all use the same GPU
_cancel_flags = {}


class Cancelled(Exception):
    pass


# ---------- storage ----------

def _conn():
    conn = db.get_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS research_runs (id TEXT PRIMARY KEY, question TEXT, status TEXT, "
                 "created_at REAL, finished_at REAL, data TEXT)")
    ownership.ensure_owner_column(conn, "research_runs")
    return conn


def save_run(run):
    conn = _conn()
    try:
        conn.execute("INSERT OR REPLACE INTO research_runs (id, question, status, created_at, finished_at, data, owner) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (run["id"], run["question"], run["status"], run["created_at"], run.get("finished_at"), json.dumps(run), run.get("owner", "")))
        conn.commit()
    finally:
        conn.close()


def get_run(run_id):
    where, owner_params = ownership.owner_filter()
    conn = _conn()
    try:
        row = conn.execute(f"SELECT data FROM research_runs WHERE id = ? AND {where}", (run_id, *owner_params)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def list_runs():
    runs = _all_runs()
    children = {}
    for r in runs:
        if r.get("root_id"):
            children.setdefault(r["root_id"], []).append(r)
    out = []
    for r in reversed(runs):
        if r.get("root_id"):
            continue
        item = summary(r)
        kids = children.get(r["id"], [])
        item["followups"] = len(kids)
        active = next((k for k in kids if k["status"] in _RUNNING), None)
        if active:
            item.update(status=active["status"], detail=active.get("detail", ""), stage=active.get("stage", ""))
        out.append(item)
    return out


def _all_runs():
    where, owner_params = ownership.owner_filter()
    conn = _conn()
    try:
        rows = conn.execute(f"SELECT data FROM research_runs WHERE {where} ORDER BY created_at ASC", owner_params).fetchall()
    finally:
        conn.close()
    return [json.loads(r[0]) for r in rows]


def thread_of(run_id):
    """The first question and every follow-up under it, oldest first. Any run id in the thread finds the whole thread."""
    runs = _all_runs()
    mine = next((r for r in runs if r["id"] == run_id), None)
    if mine is None:
        return []
    root = mine.get("root_id") or mine["id"]
    return [r for r in runs if r["id"] == root or r.get("root_id") == root]


def delete_run(run_id):
    """Deleting a first question deletes its follow-ups too; deleting one follow-up removes just that one."""
    where, owner_params = ownership.owner_filter()
    conn = _conn()
    try:
        conn.execute(f"DELETE FROM research_runs WHERE id = ? AND {where}", (run_id, *owner_params))
        for r in _all_runs():
            if r.get("root_id") == run_id:
                conn.execute(f"DELETE FROM research_runs WHERE id = ? AND {where}", (r["id"], *owner_params))
        conn.commit()
    finally:
        conn.close()


def mark_interrupted():
    """A run that was going when Athena stopped is not going now."""
    conn = _conn()
    try:
        rows = conn.execute("SELECT data FROM research_runs WHERE status IN ('queued', 'running')").fetchall()
    finally:
        conn.close()
    for (data,) in rows:
        run = json.loads(data)
        run.update(status="error", error="Athena was restarted while this was running.", finished_at=time.time())
        save_run(run)


def summary(run):
    verified = [c for c in run.get("claims", []) if c.get("supported")]
    return {"id": run["id"], "question": run["question"], "status": run["status"], "stage": run.get("stage", ""),
            "detail": run.get("detail", ""), "depth": run.get("depth"), "created_at": run["created_at"],
            "finished_at": run.get("finished_at"), "sources": len([s for s in run.get("sources", []) if s["status"] == "read"]),
            "verified_claims": len(verified), "error": run.get("error")}


def _note(run, message, stage=None, detail=None):
    if stage:
        run["stage"] = stage
    run["detail"] = detail if detail is not None else message
    run["events"].append({"t": time.time(), "message": message})
    save_run(run)


# ---------- talking to a model ----------

def _complete(model, prompt, system="", ctx=12288, think="none"):
    """One narrow model call, returning its text. Runs under the host's lock so it never overlaps a bot or another run."""
    import bots
    from main import _stream_completion, _SUB_AGENT_TIMEOUT
    provider, endpoint_url = model.get("provider") or "", model.get("endpoint_url") or ""
    req = bots._FakeReqForDispatch(provider=provider, model=model["model"], endpoint_url=endpoint_url, api_key=model.get("api_key") or "", think=think)
    target_url = (endpoint_url.rstrip("/") + "/api/chat") if endpoint_url and provider == "" else (host_locks.OLLAMA_URL if provider == "" else "")
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    text = ""
    with host_locks.get_host_lock(host_locks.endpoint_host(endpoint_url)):
        for chunk in _stream_completion(req, target_url, messages, None, ctx, threading.Event(), timeout=_SUB_AGENT_TIMEOUT):
            text += chunk.get("message", {}).get("content", "") or ""
            if chunk.get("done"):
                break
    return text.strip()


def _first_word(text, allowed, default):
    for word in re.findall(r"[a-z]+", (text or "").lower()):
        if word in allowed:
            return word
    return default


# ---------- the steps ----------

def _inert(text):
    """Page-derived text is shown as markdown, so it must never be able to carry markup of its own."""
    return str(text).replace("<", "&lt;").strip()


def _clean_lines(text):
    out = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"').strip()
        if len(line) >= 4 and line.lower() not in ("none", "n/a"):
            out.append(line)
    return out


def plan_queries(question, count, ask, context=""):
    background = f"\nWhat is already established (do not search for this again):\n{context}\n" if context else ""
    prompt = (f"You are planning web research.\nQuestion: {question}\n{background}\n"
              f"Write {count} short, specific web search queries that together would answer it. "
              "One per line. No numbering and no commentary.")
    queries = _clean_lines(ask("plan", prompt))
    seen, unique = set(), []
    for q in [question] + queries:                                  # the question itself is always searched first
        key = q.lower().strip(" ?.")
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique[: count * 2 + 1]


def standalone_question(question, earlier, ask):
    """A follow-up like 'what about its cost?' means nothing to a search engine; restate it with its subject."""
    prompt = (f"A research question was asked: {earlier}\n\nA follow-up question was then asked: {question}\n\n"
              "Rewrite the follow-up as one complete question that makes sense on its own, naming what it is about. "
              "Write only the question.")
    lines = _clean_lines(ask("plan", prompt, ctx=4096))
    return lines[0][:300] if lines else question


def _search_url():
    return (settings.load_settings().get("search_url") or "").rstrip("/")


def search(query, limit):
    base = _search_url()
    if not base:
        raise RuntimeError("No search engine is set up (search_url is empty in Settings), so there is nowhere to research from.")
    resp = httpx.get(f"{base}/search", params={"q": query, "format": "json"}, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"The search engine answered HTTP {resp.status_code}.")
    return [{"url": r["url"], "title": _inert(r.get("title") or "")} for r in resp.json().get("results", [])
            if str(r.get("url", "")).lower().startswith(("http://", "https://"))][:limit]


def canonical_url(url):
    parts = urlparse(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")])
    return urlunparse((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", "", query, ""))


def domain_of(url):
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "org", "gov", "ac", "net"):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def fetch_text(url):
    """-> (text, partly_read, error). Reads the page in chunks up to a limit; the limit is reported, not hidden."""
    text, offset = "", 0
    while True:
        part = web_tools.web_fetch(url, offset)
        if not isinstance(part, dict) or part.get("error") or "content" not in part:
            return text, bool(text), (part.get("error") if isinstance(part, dict) else None) or "the page could not be read"
        text += part["content"]
        if not part.get("has_more"):
            return text, False, None
        if len(text) >= _PAGE_TEXT_LIMIT:
            return text, True, None
        offset = part["next_offset"]


def make_windows(text):
    windows, step = [], _WINDOW - _WINDOW_OVERLAP
    for start in range(0, max(len(text), 1), step):
        piece = text[start:start + _WINDOW]
        if piece.strip():
            windows.append(piece)
        if start + _WINDOW >= len(text):
            break
    return windows


def _embed(texts):
    import rag
    return rag._embed(texts)


def pick_passages(windows, queries, count):
    """The windows most relevant to the searches, in page order, each tagged with the query it matched best."""
    if len(windows) <= count and not queries:
        return [(w, 0) for w in windows]
    try:
        scores = _embed(windows) @ _embed(queries).T
    except Exception:
        words = [set(re.findall(r"[a-z0-9]{4,}", q.lower())) for q in queries]
        scores = [[len(w_words & q_words) / (len(q_words) or 1) for q_words in words] for w_words in (set(re.findall(r"[a-z0-9]{4,}", w.lower())) for w in windows)]
    best = []
    for i in range(len(windows)):
        row = list(scores[i])
        j = max(range(len(row)), key=lambda k: row[k])
        best.append((row[j], i, j))
    chosen = sorted(sorted(best, reverse=True)[:count], key=lambda t: t[1])
    return [(windows[i], j) for _, i, j in chosen]


_CLAIM = re.compile(r"CLAIM:\s*(.+?)\s*\n\s*QUOTE:\s*(.+?)(?=\n\s*CLAIM:|\Z)", re.S | re.I)


def extract_claims(question, window, ask):
    prompt = (f"Question: {question}\n\nText:\n\"\"\"\n{window}\n\"\"\"\n\n"
              "List every fact in the text that helps answer the question. For each one, write exactly:\n"
              "CLAIM: <the fact, as one plain sentence>\n"
              "QUOTE: <the whole sentence or phrase from the text that states it, copied character for character>\n\n"
              "The claim may say only what its quote says: no figures, names or dates that are not in the quote, and nothing from the rest of the text. "
              "Only list facts that help answer the question. If the text has nothing relevant, write: NONE")
    reply = ask("extract", prompt, ctx=8192)
    if reply.strip().upper().startswith("NONE"):
        return []
    found = [(_inert(m.group(1)), m.group(2).strip().strip('"').strip()) for m in _CLAIM.finditer(reply)]
    return [(c, q) for c, q in found if q.upper() != "NONE" and not c.lower().startswith("the text does not")]


_MARKERS = re.compile(r"\[\s*(?:\d{1,3}|[a-z]|edit|citation needed|note \d+)\s*\]", re.I)


def _norm(text):
    text = _MARKERS.sub(" ", text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"'))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%$.,'\"-]", " ", text)).strip()


def quote_in_text(quote, text):
    """True only if the quote's words really appear in the page (an ellipsis may join separate pieces).
    Footnote markers and layout (tables, line breaks) are ignored on both sides."""
    pieces = [p for p in re.split(r"\.\.\.|…", quote) if len(p.strip()) >= 6]
    whole = _norm(quote)
    if not pieces or len(whole) < _MIN_QUOTE or len(whole.split()) < 3:
        return False
    haystack = _norm(text)
    return all(_norm(p) in haystack for p in pieces)


_DECISION_YES = 0.7
_DECISION_CONFLICT = 0.8


def is_decision_model(model):
    return bool(model.get("system1"))


def make_decider(model):
    return lambda state, questions: system1.decide(state, questions, {"model": model["model"], "endpoint_url": model["endpoint_url"]})


_PAGE_ON_TOPIC = 0.6


def page_on_topic(question, title, text, decide):
    """Search engines return look-alikes (another tower, another tower's year). Claims pulled from those would
    be perfectly quotable and still be about the wrong thing, so the page itself is checked first."""
    q = {"r": {"type": "noul", "instructions": "Is this page about the specific thing that `question` asks about, not about something else?",
               "criteria": {"true": "The page is about the subject of the question.", "false": "The page is about a different subject than the question."}}}
    return decide({"question": question, "page_title": title, "page_start": text[:500]}, q)["r"]["noul"] >= _PAGE_ON_TOPIC


def judge_support(claim, quote, ask, title="", decide=None):
    if decide:
        q = {"s": {"type": "noul", "instructions": "Does `quote` state everything that `claim` says, with the same facts and numbers and nothing added?",
                   "criteria": {"true": "The quote fully supports the claim.", "false": "The claim says something the quote does not say, or the quote says something different."}}}
        return "yes" if decide({"quote": quote, "claim": claim}, q)["s"]["noul"] >= _DECISION_YES else "no"
    where = f" from a page titled \"{title}\"" if title else ""
    prompt = (f"A quote{where}:\n\"{quote}\"\n\nA claim: {claim}\n\n"
              "The quote may not repeat what it is about; the page title says that. "
              "Do the quote's own words state what the claim says (the same facts and numbers, nothing added)? "
              "Answer with one word: yes, partly, or no.")
    return _first_word(ask("check", prompt, ctx=4096), ("yes", "partly", "no"), "no")


def judge_relation(a, b, ask, decide=None):
    if decide:
        q = {"r": {"type": "choice", "instructions": "How do `a` and `b` relate?",
                   "criteria": {"agree": "They state the same fact.", "contradict": "They state different values for the same thing, so they cannot both be true.",
                                "unrelated": "They are about different things."}}}
        p = decide({"a": a, "b": b}, q)["r"]["probabilities"]
        return "conflict" if p.get("contradict", 0) >= _DECISION_CONFLICT else "same" if p.get("agree", 0) >= _DECISION_YES else "different"
    prompt = (f"Statement A: {a}\nStatement B: {b}\n\n"
              "Do they say the same thing, contradict each other (they cannot both be true), or concern different facts? "
              "Answer with one word: same, conflict, or different.")
    return _first_word(ask("check", prompt, ctx=4096), ("same", "conflict", "different"), "different")


def compare_claims(claims, ask, on_pair=None, decide=None):
    """Mark each supported claim corroborated, single-source or disputed, and return the disputes found.
    Claims carried over from earlier in a thread keep the agreements already found between them; only pairs
    that involve a claim from this run are judged."""
    supported = [c for c in claims if c["supported"]]
    for c in supported:
        if c.get("inherited"):
            c.setdefault("agree", []); c.setdefault("conflict", [])
        else:
            c.update(verdict="single-source", agree=[], conflict=[])
    if len(supported) >= 2:
        try:
            vectors = _embed([c["text"] for c in supported])
            sims = vectors @ vectors.T
        except Exception:
            sims = None
        for i in range(len(supported) if sims is not None else 0):
            for j in range(i + 1, len(supported)):
                a, b = supported[i], supported[j]
                if (a.get("inherited") and b.get("inherited")) or a["domain"] == b["domain"] or float(sims[i][j]) < _PAIR_SIMILARITY:
                    continue
                relation = judge_relation(a["text"], b["text"], ask, decide)
                if on_pair:
                    on_pair()
                if relation == "same":
                    a["agree"].append(b["id"]); b["agree"].append(a["id"])
                elif relation == "conflict":
                    a["conflict"].append(b["id"]); b["conflict"].append(a["id"])
    by_id = {c["id"]: c for c in supported}
    disputes, seen_pairs = [], set()
    for c in supported:
        c["agree"] = [i for i in dict.fromkeys(c["agree"]) if i in by_id]
        c["conflict"] = [i for i in dict.fromkeys(c["conflict"]) if i in by_id]
        sites = {c["domain"]} | {by_id[i]["domain"] for i in c["agree"]}
        c["sites"] = len(sites)
        c["verdict"] = "disputed" if c["conflict"] else "corroborated" if len(sites) >= 2 else "single-source"
        for other in c["conflict"]:
            pair = tuple(sorted((c["id"], other), key=lambda x: int(x[1:])))
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                disputes.append({"claims": list(pair)})
    return disputes


# ---------- writing and checking the report ----------

_CITE = re.compile(r"\[(C\d+(?:\s*[,;]\s*C?\d+)*)\]", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[\"(*])")


def _claim_ids(group):
    return ["C" + n for n in re.findall(r"\d+", group)]


def find_gaps(question, claims, ask):
    facts = "\n".join("- " + c["text"] for c in claims)
    prompt = (f"Question: {question}\n\nChecked facts:\n{facts}\n\n"
              "Which parts of the question do these facts NOT answer? One short phrase per line. "
              "If they answer every part, write only: NONE")
    return [g for g in _clean_lines(ask("write", prompt, ctx=8192)) if not g.lower().startswith("none")][:5]


def suggest_followups(question, claims, gaps, ask):
    facts = "\n".join("- " + c["text"] for c in claims[:12])
    open_ends = "\n".join("- " + g for g in gaps)
    prompt = (f"Question: {question}\n\nWhat was found:\n{facts}\n" + (f"\nWhat was not found:\n{open_ends}\n" if open_ends else "") +
              "\nWrite 3 short follow-up questions the reader is likely to want answered next, each answerable by searching the web. "
              "One per line, no numbering, no commentary.")
    out = [_inert(q) for q in _clean_lines(ask("write", prompt, ctx=8192)) if q.rstrip(" ?.").lower() != question.rstrip(" ?.").lower()]
    return [q if q.endswith("?") else q + "?" for q in out][:3]


def relevant_carried(question, claims, limit=12):
    """Earlier checked claims worth showing the writer: the ones that say something about the new question."""
    if not claims:
        return []
    try:
        vectors = _embed([question] + [c["text"] for c in claims])
    except Exception:
        return []
    scored = sorted(zip((float(x) for x in vectors[1:] @ vectors[0]), range(len(claims))), reverse=True)
    return [claims[i] for score, i in scored[:limit] if score >= 0.45]


def carried_context(thread):
    """Sources and checked claims from the finished runs of a thread, merged and renumbered so a new run can start from them."""
    sources, by_url, claims, by_sig = [], {}, [], {}
    for prior in thread:
        if prior["status"] != "done":
            continue
        source_map = {}
        for src in prior["sources"]:
            if src["status"] != "read":
                continue
            key = canonical_url(src["url"])
            if key not in by_url:
                n = len(sources) + 1
                by_url[key] = {k: src.get(k) for k in ("url", "title", "domain", "status", "chars", "partly_read", "fetched_at")} | {"id": f"S{n}", "n": n, "queries": [], "inherited": True, "inherited_only": True}
                sources.append(by_url[key])
            source_map[src["id"]] = by_url[key]["id"]
        id_map, fresh = {}, []
        for c in prior["claims"]:
            if not c.get("supported") or c["source"] not in source_map:
                continue
            sig = (source_map[c["source"]], _norm(c["text"]))
            if sig in by_sig:
                id_map[c["id"]] = by_sig[sig]["id"]
            else:
                new = {k: c.get(k) for k in ("domain", "text", "quote", "judgement", "verdict", "sites")} | {
                    "id": f"C{len(claims) + 1}", "source": source_map[c["source"]], "sub": -1, "quote_found": True, "supported": True,
                    "inherited": True, "agree": [], "conflict": []}
                by_sig[sig] = new
                claims.append(new)
                id_map[c["id"]] = new["id"]
                fresh.append((new, c))
        for new, old in fresh:
            new["agree"] = [id_map[i] for i in old.get("agree", []) if i in id_map and id_map[i] != new["id"]]
            new["conflict"] = [id_map[i] for i in old.get("conflict", []) if i in id_map and id_map[i] != new["id"]]
    return sources, claims


def writer_prompt(question, claims, sub_questions):
    lines = []
    for index, sub in enumerate(sub_questions):
        mine = [c for c in claims if c["sub"] == index]
        if not mine:
            continue
        lines.append(f"\nAbout: {sub}")
        for c in mine:
            note = {"corroborated": f"{c['sites']} sites agree", "disputed": "DISPUTED by another site", "single-source": "one site only"}[c["verdict"]]
            lines.append(f"[{c['id']}] ({note}) {c['text']}")
    background = [c for c in claims if not 0 <= c["sub"] < len(sub_questions)]
    if background:
        lines.append("\nAlready established earlier in this research:")
        for c in background:
            note = {"corroborated": f"{c['sites']} sites agree", "disputed": "DISPUTED by another site", "single-source": "one site only"}[c["verdict"]]
            lines.append(f"[{c['id']}] ({note}) {c['text']}")
    return (f"Question: {question}\n\nChecked claims, each with its id:\n" + "\n".join(lines) + "\n\n"
            "Write a clear, concise answer to the question in markdown, using short paragraphs or bullets and, if it helps, a few plain headings. "
            "Use only the claims above and add nothing else. End every sentence that states a fact with the ids of the claims it rests on, "
            "like [C3] or [C3, C7]. Where a claim is from one site only or is disputed, say so in the sentence.")


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_FAITHFUL = 0.55


def _numbers(text):
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(text)}


def sentence_matches_claims(text, cited):
    """A sentence may only use figures that appear in the claims it cites, and must say roughly what one of them says."""
    allowed = set().union(*(_numbers(c["text"]) for c in cited))
    if not _numbers(text) <= allowed:
        return False
    try:
        vectors = _embed([text] + [c["text"] for c in cited])
    except Exception:
        return True
    return float((vectors[1:] @ vectors[0]).max()) >= _FAITHFUL


def finalize_report(draft, claims_by_id, sources_by_id):
    """Keep only sentences that cite real, supported claims and match them; turn the ids into links to the pages read."""
    out, dropped = [], 0
    for line in draft.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("|") or set(stripped) <= set("-*_ "):
            out.append(line)
            continue
        marker = re.match(r"^\s*((?:[-*•]|\d+[.)])\s+)?", line)
        prefix = (marker.group(1).strip() + " ") if marker.group(1) else ""      # an indented bullet would render as a code block
        body = line[marker.end():]
        kept = []
        for sentence in _SENTENCE_SPLIT.split(body):
            ids = []
            for group in _CITE.findall(sentence):
                ids += [i for i in _claim_ids(group) if i in claims_by_id]
            if not ids:
                dropped += 1
                continue
            text = re.sub(r"\s+([.,;:!?])", r"\1", _CITE.sub("", sentence)).rstrip()
            if not sentence_matches_claims(text, [claims_by_id[i] for i in ids]):
                dropped += 1
                continue
            sources = sorted({sources_by_id[claims_by_id[i]["source"]]["n"] for i in ids})
            links = "".join(f"[\\[{n}\\]]({next(s['url'] for s in sources_by_id.values() if s['n'] == n)})" for n in sources)
            verdicts = {claims_by_id[i]["verdict"] for i in ids}
            tag = " *(disputed)*" if "disputed" in verdicts else " *(single source)*" if verdicts == {"single-source"} else ""
            kept.append(f"{text}{tag} {links}")
        if kept:
            out.append(prefix + " ".join(kept))
    return "\n".join(out).strip(), dropped


def assemble(run, draft):
    used = set(run.get("used_claim_ids") or [c["id"] for c in run["claims"] if c["supported"]])
    claims = {c["id"]: c for c in run["claims"] if c["supported"] and c["id"] in used}
    sources = {s["id"]: s for s in run["sources"]}
    body, dropped = finalize_report(_inert(draft), claims, sources) if draft else ("", 0)
    parts = [body] if body else ["No claim survived the checks, so there is nothing reliable to report."]
    disputes = [d for d in run["disputes"] if all(i in claims for i in d["claims"])]
    if disputes:
        parts.append("## Where sources disagree")
        for d in disputes:
            a, b = (claims[i] for i in d["claims"])
            parts.append(f"- {a['text']} ([{sources[a['source']]['n']}]({sources[a['source']]['url']})) **but** {b['text']} ([{sources[b['source']]['n']}]({sources[b['source']]['url']}))")
    if run.get("gaps"):
        parts.append("## Not established")
        parts.append("The sources that passed the checks did not answer:")
        parts += [f"- {_inert(g)}" for g in run["gaps"]]
    read = [s for s in run["sources"] if s["status"] == "read" and not s.get("inherited_only")]
    fresh = [c for c in run["claims"] if not c.get("inherited")]
    verified = [c for c in fresh if c["supported"]]
    carried = [c for c in claims.values() if c.get("inherited")]
    sites = len({c["domain"] for c in claims.values()})
    parts.append("## How this was checked")
    parts.append(f"{len(read)} pages were read. {len(fresh)} claims were pulled out of them with a quote each; {len(verified)} of those had a quote that "
                 f"really appears on the page and was judged to support the claim. "
                 + (f"{len(carried)} claims already checked earlier in this research were also used. " if carried else "")
                 + f"The answer rests on {len(claims)} claims from {sites} site(s); "
                 f"{sum(1 for c in claims.values() if c['verdict'] == 'corroborated')} were confirmed by a second site. "
                 "This shows what the sources say and whether independent sources agree; it does not prove any of it is true."
                 + (f" {dropped} sentence(s) were removed for lacking a valid citation." if dropped else ""))
    return "\n\n".join(parts)


# ---------- the run ----------

def _pipeline(run, models, thread=()):
    depth = DEPTHS[run["depth"]]

    def ask(role, prompt, system="", ctx=12288, think="none"):
        if _cancel_flags[run["id"]].is_set():
            raise Cancelled()
        return _complete(models[role], prompt, system=system, ctx=ctx, think=think)

    plan_ask = lambda role, prompt, **kw: ask("plan", prompt, **kw)
    check_ask = lambda role, prompt, **kw: ask("check", prompt, **kw)
    decide = make_decider(models["check"]) if is_decision_model(models["check"]) else None
    run["status"] = "running"

    inherited_sources, inherited_claims = carried_context(thread)
    question = run["question"]
    context = ""
    if inherited_claims:
        _note(run, "Looking at what the earlier research already established", stage="planning")
        question = standalone_question(run["question"], thread[0]["question"], plan_ask)
        run["search_question"] = question
        context = "\n".join("- " + c["text"] for c in relevant_carried(question, inherited_claims, 6))
    _note(run, "Planning the searches", stage="planning")
    run["plan"] = plan_queries(question, depth["queries"], plan_ask, context)
    _note(run, f"Planned {len(run['plan'])} searches", detail="Searching the web")

    _note(run, "Searching and reading pages", stage="gathering")
    seen = {canonical_url(s["url"]): s for s in inherited_sources}
    sources = list(inherited_sources)
    run["sources"], run["claims"] = sources, inherited_claims
    to_read = []
    for index, query in enumerate(run["plan"]):
        if _cancel_flags[run["id"]].is_set():
            raise Cancelled()
        try:
            results = search(query, depth["results"])
        except RuntimeError as e:
            if not to_read and index == len(run["plan"]) - 1 and not inherited_claims:
                raise
            run["events"].append({"t": time.time(), "message": f"Search failed for '{query}': {e}"})
            continue
        for r in results:
            key = canonical_url(r["url"])
            if key not in seen:
                seen[key] = {"id": f"S{len(sources) + 1}", "n": len(sources) + 1, "url": r["url"], "title": r["title"] or r["url"],
                             "domain": domain_of(r["url"]), "queries": [], "status": "pending", "fetched_at": None, "chars": 0}
                sources.append(seen[key])
            if seen[key] not in to_read:
                to_read.append(seen[key])
            seen[key].setdefault("queries", []).append(index)
    _note(run, f"Found {len(to_read)} pages", detail=f"Reading {len(to_read)} pages")
    texts = {}

    def read(source):
        text, partly, error = fetch_text(source["url"])
        if error and not text:
            if not source.get("inherited"):
                source["status"], source["error"] = "failed", error
                source["fetched_at"] = time.time()
        else:
            source.update(status="read", chars=len(text), partly_read=partly, fetched_at=time.time(), inherited_only=False)
            texts[source["id"]] = text

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(read, to_read))
    if decide:
        off_topic = [src for src in to_read if src["id"] in texts and not src.get("inherited") and not page_on_topic(question, src["title"], texts[src["id"]], decide)]
        for src in off_topic:
            src["status"] = "off_topic"
            texts.pop(src["id"])
        if off_topic:
            _note(run, f"Skipped {len(off_topic)} pages that are about something else", detail="Reading the relevant pages")
    save_run(run)
    if not texts and not inherited_claims:
        raise RuntimeError("None of the pages found could be read, or none of them were about the question, so there is nothing to check.")

    _note(run, "Pulling claims out of the most relevant passages", stage="extracting")
    known = {(c["source"], _norm(c["text"])) for c in run["claims"]}
    for source in [s for s in to_read if s["id"] in texts]:
        windows = make_windows(texts[source["id"]])
        for window, query_index in pick_passages(windows, run["plan"], depth["passages"]):
            if _cancel_flags[run["id"]].is_set():
                raise Cancelled()
            for claim, quote in extract_claims(question, window, ask):
                if (source["id"], _norm(claim)) in known:
                    continue
                known.add((source["id"], _norm(claim)))
                run["claims"].append({"id": f"C{len(run['claims']) + 1}", "source": source["id"], "domain": source["domain"], "sub": query_index,
                                      "text": claim, "quote": quote, "quote_found": quote_in_text(quote, window), "supported": False})
        _note(run, f"Read {source['title'][:60]}", detail=f"Extracted {sum(1 for c in run['claims'] if not c.get('inherited'))} claims so far")

    _note(run, "Checking each claim", stage="verifying")
    titles = {src["id"]: src["title"] for src in run["sources"]}
    candidates = [c for c in run["claims"] if not c.get("inherited") and c["quote_found"]]
    for n, claim in enumerate(candidates, 1):
        claim["judgement"] = judge_support(claim["text"], claim["quote"], check_ask, titles.get(claim["source"], ""), decide)
        claim["supported"] = claim["judgement"] in ("yes", "partly")
        if n % 5 == 0 or n == len(candidates):
            _note(run, f"Checked {n} of {len(candidates)} claims", detail=f"Checked {n} of {len(candidates)} claims")
    run["disputes"] = compare_claims(run["claims"], check_ask, decide=decide)
    _note(run, "Comparing sources against each other", detail=f"{sum(1 for c in run['claims'] if c['supported'])} claims passed the checks")

    _note(run, "Writing the report", stage="writing")
    new_supported = [c for c in run["claims"] if c["supported"] and not c.get("inherited")]
    carried = relevant_carried(question, [c for c in run["claims"] if c.get("inherited")])
    supported = new_supported + carried
    run["used_claim_ids"] = [c["id"] for c in supported]
    run["gaps"] = find_gaps(question, supported, ask) if supported else []
    draft = ask("write", writer_prompt(question, supported, run["plan"]), ctx=16384) if supported else ""
    run["report"] = assemble(run, draft)
    try:
        run["suggestions"] = suggest_followups(question, supported, run["gaps"], ask) if supported else []
    except Cancelled:
        raise
    except Exception as e:
        logger.warning(f"research {run['id']}: could not suggest follow-ups: {e!r}")
        run["suggestions"] = []
    run.update(status="done", stage="done", finished_at=time.time())
    _note(run, "Done", detail="Finished")


def _execute(run, models, thread=()):
    slot = None
    plan_model = models["plan"]
    if prompt_gate.applies(plan_model.get("provider") or "", plan_model.get("endpoint_url") or ""):
        run.update(detail="Waiting for another prompt to finish")
        save_run(run)
        slot = prompt_gate.blocking_turn(run.get("owner", ""), f"research:{run['id']}", _cancel_flags.get(run["id"]) or threading.Event())
        if slot is None:
            run.update(status="cancelled", stage="cancelled", finished_at=time.time())
            _note(run, "Cancelled", detail="Cancelled")
            _cancel_flags.pop(run["id"], None)
            return
    try:
        _execute_in_turn(run, models, thread)
    finally:
        if slot is not None:
            prompt_gate.release(slot)


def _execute_in_turn(run, models, thread=()):
    with _run_lock:
        try:
            _pipeline(run, models, thread)
        except Cancelled:
            run.update(status="cancelled", stage="cancelled", finished_at=time.time())
            _note(run, "Cancelled", detail="Cancelled")
        except Exception as e:
            logger.error(f"research run {run['id']} failed: {e!r}")
            run.update(status="error", error=str(e) or type(e).__name__, finished_at=time.time())
            _note(run, f"Stopped: {run['error']}", detail=run["error"])
        finally:
            _cancel_flags.pop(run["id"], None)


def _public_model(cfg):
    return {"model": cfg["model"], "label": cfg.get("label") or cfg["model"], "provider": cfg.get("provider") or "", "endpoint_url": cfg.get("endpoint_url") or "", "system1": bool(cfg.get("system1"))}


def start_run(question, depth, plan_model, check_model=None, wait=False, parent_id=None):
    question = (question or "").strip()
    if not question:
        return {"error": "Type the question you want researched."}
    if depth not in DEPTHS:
        return {"error": f"depth must be one of: {', '.join(DEPTHS)}."}
    if not plan_model or not plan_model.get("model"):
        return {"error": "Choose a model for the research."}
    if check_model and check_model.get("model"):
        checker = check_model
    elif system1.available():
        cfg = system1.config()
        checker = {"model": cfg["model"], "label": f"System 1 ({cfg['model']})", "endpoint_url": cfg["endpoint_url"], "provider": "", "system1": True}
    else:
        checker = plan_model
    models = {"plan": plan_model, "extract": plan_model, "write": plan_model, "check": checker}
    thread = []
    if parent_id:
        thread = thread_of(parent_id)
        if not thread:
            return {"error": "That research no longer exists."}
        if any(r["status"] in _RUNNING for r in thread):
            return {"error": "Wait for the current research in this thread to finish before asking another follow-up."}
        if not any(r["status"] == "done" for r in thread):
            return {"error": "There is no finished research here to follow up on."}
    run = {"id": uuid.uuid4().hex[:12], "question": question[:2000], "depth": depth, "status": "queued", "stage": "queued", "detail": "Waiting to start",
           "created_at": time.time(), "finished_at": None, "models": {k: _public_model(v) for k, v in models.items()},
           "plan": [], "sources": [], "claims": [], "disputes": [], "report": "", "events": [], "error": None,
           "root_id": (thread[0].get("root_id") or thread[0]["id"]) if thread else None, "parent_id": parent_id or None,
           "owner": ownership.stored_owner()}
    save_run(run)
    _cancel_flags[run["id"]] = threading.Event()
    worker = threading.Thread(target=_execute, args=(run, models, thread), daemon=True)
    worker.start()
    if wait:
        worker.join()
    return {"id": run["id"]}


def cancel_run(run_id):
    flag = _cancel_flags.get(run_id)
    if flag is None:
        return False
    flag.set()
    return True


# ---------- routes ----------

class ModelChoice(BaseModel):
    model: str
    label: str = ""
    provider: str = ""
    endpoint_url: str = ""
    api_key: str = ""


class ResearchIn(BaseModel):
    question: str
    depth: str = "standard"
    plan_model: ModelChoice
    check_model: Optional[ModelChoice] = None
    parent_id: Optional[str] = None


@router.post("/api/research")
def api_start(req: ResearchIn):
    for choice in (req.plan_model, req.check_model):
        if choice and ownership.endpoint_kind(choice.provider, choice.endpoint_url) is None:
            return {"error": "This account cannot use that model endpoint."}
    return start_run(req.question, req.depth, req.plan_model.model_dump(), req.check_model.model_dump() if req.check_model else None, parent_id=req.parent_id)


@router.get("/api/research")
def api_list():
    return {"runs": list_runs()}


@router.get("/api/research/{run_id}")
def api_get(run_id: str):
    run = get_run(run_id)
    return run if run else {"error": "No such research run."}


@router.get("/api/research/{run_id}/thread")
def api_thread(run_id: str):
    runs = thread_of(run_id)
    return {"runs": runs} if runs else {"error": "No such research run."}


@router.post("/api/research/{run_id}/cancel")
def api_cancel(run_id: str):
    return {"cancelled": cancel_run(run_id)}


@router.delete("/api/research/{run_id}")
def api_delete(run_id: str):
    for r in thread_of(run_id):
        if r["id"] == run_id or r.get("root_id") == run_id:
            cancel_run(r["id"])
    delete_run(run_id)
    return {"removed": True}
