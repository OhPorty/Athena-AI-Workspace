import os
import ast
import glob
import sqlite3
import hashlib
import threading
from typing import List, Dict, Any, Tuple

import numpy as np

from logging_setup import logger

# CPU-only ONNX embeddings via fastembed -- chosen over calling Ollama's own
# /api/embeddings endpoint because onnxruntime is already a hard dependency
# here (piper-tts/faster-whisper), so this adds no new dependency category,
# just one ~130MB model file (downloaded once from HuggingFace on first use,
# cached locally by fastembed itself), and never depends on an external
# service being reachable at index time.
EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

# Line-window chunking knobs, used both for non-Python files and as the
# fallback/overflow path for Python chunking below. Overlap exists so a
# concept split across a window boundary still appears whole in at least
# one chunk, instead of every window edge being a hard, context-free cut.
CHUNK_SIZE = 50
CHUNK_OVERLAP = 10

# A top-level function/class chunk bigger than this (e.g. main.py's own
# chat_stream, ~600 lines) still gets split further via the same line-window
# fallback -- one chunk per function is the goal, not one chunk per function
# no matter how large, since an oversized chunk is exactly as useless for
# embedding similarity as an arbitrary slice was.
MAX_CHUNK_LINES = 200

# A class bigger than this gets chunked per-method instead of as one whole-
# class chunk -- a large class embedded as a single blob dilutes the vector
# across every method it contains, so a query about one specific method
# stops matching well once the class around it gets big enough.
CLASS_SIZE_CAP = 150

_embedder = None
_embedder_lock = threading.Lock()
_load_failed_reason = None  # cache a load failure so we don't re-attempt a slow/stuck download on every call


def _get_embedder():
    """Lazy singleton, same shape as laya_gate.py's _get_agent, including
    caching a load failure rather than retrying it -- the first real use
    downloads the model from HuggingFace (~130MB), which can legitimately
    take a while, but if it fails (or gets stuck on a bad network path),
    every call after that would otherwise re-attempt the same slow
    operation from scratch instead of failing fast. A cached failure only
    clears on process restart, same as Laya's own precedent."""
    global _embedder, _load_failed_reason
    if _embedder is not None or _load_failed_reason is not None:
        return _embedder
    with _embedder_lock:
        if _embedder is None and _load_failed_reason is None:
            logger.info(f"loading embedding model {EMBEDDING_MODEL_NAME} (first run downloads it from HuggingFace, ~130MB, may take a while)...")
            try:
                from fastembed import TextEmbedding
                _embedder = TextEmbedding(model_name=EMBEDDING_MODEL_NAME)
                logger.info("embedding model ready.")
            except Exception as e:
                _load_failed_reason = repr(e)
                logger.error(f"embedding model failed to load, search_codebase will be unavailable until Athena restarts: {e!r}")
    return _embedder


def warm_up():
    """Explicitly triggers the (potentially slow) model load, so it happens
    at a known point with a clear log message instead of implicitly
    whichever thread -- a background reindex or a live chat request -- first
    happens to touch _embed(). Safe to call redundantly; _get_embedder is
    itself idempotent."""
    _get_embedder()


def _embed(texts: List[str]) -> np.ndarray:
    """Returns an (N, EMBEDDING_DIM) float32 matrix, each row L2-normalized
    so a plain dot product at query time is a cosine similarity -- normalized
    explicitly rather than trusting the model's own output stays normalized
    forever across model/library versions. Raises RuntimeError (cheaply,
    using the cached failure reason) rather than re-attempting a load that's
    already known to fail."""
    if not texts:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    embedder = _get_embedder()
    if embedder is None:
        raise RuntimeError(f"embedding model unavailable: {_load_failed_reason}")
    vectors = np.array(list(embedder.embed(texts)), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def _line_windows(lines: List[str], start_offset: int, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[Tuple[int, str]]:
    """Sliding-window chunker over an arbitrary slice of source lines.
    start_offset is the 0-indexed line number `lines[0]` corresponds to in
    the real file, so returned start_line values are real, absolute file
    line numbers regardless of which slice of the file this was called on
    (a whole file, or the leftover/overflow portion of one). Returns
    (start_line, text) so callers can build a stable, collision-free chunk
    id from the real line number instead of an arbitrary loop index."""
    stride = max(1, chunk_size - overlap)
    chunks = []
    for i in range(0, len(lines), stride):
        window = lines[i:i + chunk_size]
        text = "\n".join(window)
        if text.strip():
            chunks.append((start_offset + i + 1, text))
        if i + chunk_size >= len(lines):
            break
    return chunks


def _split_if_too_big(lines: List[str], start_offset: int, label: str) -> List[Tuple[int, str]]:
    """A top-level def/class (or one method of an oversized class) that fits
    under MAX_CHUNK_LINES becomes exactly one chunk; one that doesn't falls
    back to the same overlapping line-window chunking used for non-Python
    files, with each window prefixed by the enclosing function/class name
    so the embedding still has that context even once split apart."""
    if len(lines) <= MAX_CHUNK_LINES:
        text = "\n".join(lines)
        return [(start_offset + 1, text)] if text.strip() else []
    windows = _line_windows(lines, start_offset)
    return [(start, f"# {label} (continued)\n{text}") for start, text in windows]


def _chunk_python(content: str) -> List[Tuple[int, str]]:
    """AST-based chunking: one chunk per top-level function/class instead of
    an arbitrary 50-line window that can cut a function in half. A class
    over CLASS_SIZE_CAP is chunked per-method instead of as one whole-class
    blob. Anything not covered by a top-level def/class (imports, module-
    level constants, top-of-file comments) falls back to line-window
    chunking over just those leftover line ranges. Raises SyntaxError on
    unparseable content -- callers fall back to plain line chunking for
    that file rather than failing indexing entirely."""
    tree = ast.parse(content)
    lines = content.splitlines()
    covered = [False] * (len(lines) + 1)  # 1-indexed
    chunks: List[Tuple[int, str]] = []

    def mark_covered(start, end):
        for ln in range(start, min(end, len(lines)) + 1):
            covered[ln] = True

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
        mark_covered(start, end)
        node_lines = lines[start - 1:end]

        if isinstance(node, ast.ClassDef) and (end - start + 1) > CLASS_SIZE_CAP:
            method_chunks = []
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cstart, cend = child.lineno, getattr(child, "end_lineno", child.lineno)
                    method_chunks.extend(_split_if_too_big(lines[cstart - 1:cend], cstart - 1, f"class {node.name}"))
            # A class over the size cap with no methods at all (rare -- a
            # huge block of class-level attributes) has nothing to chunk
            # per-method, so fall back to the whole-class chunk rather than
            # silently dropping it from the index.
            chunks.extend(method_chunks if method_chunks else _split_if_too_big(node_lines, start - 1, node.name))
        else:
            chunks.extend(_split_if_too_big(node_lines, start - 1, node.name))

    leftover_start = None
    for ln in range(1, len(lines) + 1):
        if not covered[ln] and leftover_start is None:
            leftover_start = ln
        elif covered[ln] and leftover_start is not None:
            chunks.extend(_line_windows(lines[leftover_start - 1:ln - 1], leftover_start - 1))
            leftover_start = None
    if leftover_start is not None:
        chunks.extend(_line_windows(lines[leftover_start - 1:], leftover_start - 1))

    return chunks


class SimpleCodeRAG:
    def __init__(self, db_path: str = "rag_index.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                filepath TEXT,
                content TEXT,
                file_hash TEXT
            )
        ''')
        # Migration for pre-embedding-era index files: idempotent, safe to
        # run on both a brand-new DB (embedding already in CREATE TABLE
        # scope conceptually, but ALTER still runs harmlessly) and an
        # existing one. Rows left over from before this column existed get
        # embedding=NULL, which is exactly the signal index_codebase's
        # skip-unchanged check needs to know they still need embedding.
        existing_columns = {row[1] for row in cursor.execute("PRAGMA table_info(chunks)").fetchall()}
        if "embedding" not in existing_columns:
            cursor.execute("ALTER TABLE chunks ADD COLUMN embedding BLOB")
        conn.commit()
        conn.close()

    def index_codebase(self, root_dir: str = "."):
        exclude_dirs = {".git", "venv", "node_modules", "file_backups", "__pycache__"}
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        visited_filepaths = set()
        reindexed_count = 0

        for filepath in glob.glob(os.path.join(root_dir, "**/*.*"), recursive=True):
            parts = filepath.split(os.sep)
            if any(d in parts for d in exclude_dirs):
                continue
            if not filepath.endswith((".py", ".md", ".json", ".js", ".html", ".css")):
                continue

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue

            visited_filepaths.add(filepath)
            file_hash = hashlib.md5(content.encode("utf-8")).hexdigest()

            # Skip re-chunking/re-embedding a file whose content hasn't
            # changed since it was last indexed -- without this,
            # index_codebase(workspace) (called after every single
            # edit_file/replace_lines/write_file in main.py) would re-embed
            # the ENTIRE codebase on every individual file edit, which is
            # far too slow to sit inline in the tool-call path. The
            # `embedding IS NOT NULL` half of this check is what makes a
            # freshly-migrated pre-embedding DB re-embed everything exactly
            # once on the next reindex, with no separate migration step:
            # old rows match on file_hash but have a NULL embedding, so
            # they correctly fail this check and get re-chunked below.
            cursor.execute(
                "SELECT 1 FROM chunks WHERE filepath = ? AND file_hash = ? AND embedding IS NOT NULL LIMIT 1",
                (filepath, file_hash),
            )
            if cursor.fetchone() is not None:
                continue

            if filepath.endswith(".py"):
                try:
                    chunk_specs = _chunk_python(content)
                except SyntaxError:
                    chunk_specs = _line_windows(content.splitlines(), 0)
            else:
                chunk_specs = _line_windows(content.splitlines(), 0)

            if not chunk_specs:
                continue

            try:
                embeddings = _embed([text for _, text in chunk_specs])
            except RuntimeError as e:
                # The embedding model itself is unavailable (failed to load,
                # or the load is cached as permanently failed -- see
                # _get_embedder) -- every remaining file would fail the
                # exact same way, so stop here rather than re-raising the
                # same error once per remaining file in the tree. Whatever
                # was already committed for earlier files in this pass
                # stays valid; this file and the rest are simply left as
                # not-yet-(re)indexed for the next successful reindex.
                logger.warning(f"stopping index_codebase early -- {e}")
                break

            # Clear this file's existing chunks first -- otherwise a file
            # that shrinks or gets restructured leaves stale chunk rows
            # behind forever, since chunk ids are derived from filepath and
            # start line, and a restructured file's chunk boundaries rarely
            # line up with the previous version's.
            cursor.execute('DELETE FROM chunks WHERE filepath = ?', (filepath,))
            for (start_line, chunk_text), vector in zip(chunk_specs, embeddings):
                chunk_id = hashlib.md5(f"{filepath}:{start_line}".encode("utf-8")).hexdigest()
                cursor.execute('''
                    INSERT OR REPLACE INTO chunks (id, filepath, content, file_hash, embedding)
                    VALUES (?, ?, ?, ?, ?)
                ''', (chunk_id, filepath, chunk_text, file_hash, vector.astype(np.float32).tobytes()))
            # Committed per-file rather than once for the whole tree -- a
            # first-time reindex of a real codebase can take a while (every
            # chunk needs a real embedding call), and holding one giant
            # transaction open for all of it means an interrupted run loses
            # ALL progress instead of just whatever file was mid-flight.
            conn.commit()
            reindexed_count += 1
            if reindexed_count % 25 == 0:
                logger.info(f"indexed {reindexed_count} changed file(s) so far...")

        # Orphaned-chunk cleanup: a file that no longer exists on disk (
        # renamed, deleted) never gets visited by the glob above, so its old
        # chunks would otherwise keep surfacing in search results forever.
        # Checked against the real filesystem rather than "was this file in
        # this pass's glob results", so reindexing a narrower workspace
        # subtree never wrongly deletes chunks belonging to files outside
        # that subtree.
        for (filepath,) in cursor.execute("SELECT DISTINCT filepath FROM chunks").fetchall():
            if filepath not in visited_filepaths and not os.path.isfile(filepath):
                cursor.execute("DELETE FROM chunks WHERE filepath = ?", (filepath,))

        conn.commit()
        conn.close()
        if reindexed_count:
            logger.info(f"index_codebase done: {reindexed_count} file(s) (re)embedded.")

    def search(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        if not query or not query.strip():
            return []

        query_vec = _embed([query])[0]

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT filepath, content, embedding FROM chunks WHERE embedding IS NOT NULL")
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return []

        vectors = np.zeros((len(rows), query_vec.shape[0]), dtype=np.float32)
        for i, (_, _, blob) in enumerate(rows):
            vectors[i] = np.frombuffer(blob, dtype=np.float32)

        # Rows are already L2-normalized at embed time (see _embed), so this
        # dot product against the (also normalized) query vector is cosine
        # similarity directly -- no separate normalization step needed here.
        scores = vectors @ query_vec
        order = np.argsort(-scores)[:max(1, limit)]

        return [
            {"filepath": rows[i][0], "content": rows[i][1], "score": float(scores[i])}
            for i in order
        ]


# Module-level default instance + convenience functions, so every consumer
# (main.py, file_tools.py, backup_tools.py, and any future module that needs
# to trigger a reindex after a write) just does `import rag` and calls
# `rag.index_codebase(...)`/`rag.search(...)` directly, rather than each one
# separately instantiating SimpleCodeRAG or main.py handing its own instance
# around to every other module that needs it.
RAG_DB_PATH = os.environ.get("ATHENA_RAG_DB", "rag_index.db")
_default_instance = None


def _get_default_instance():
    global _default_instance
    if _default_instance is None:
        _default_instance = SimpleCodeRAG(RAG_DB_PATH)
    return _default_instance


def index_codebase(root_dir: str = "."):
    _get_default_instance().index_codebase(root_dir)


def search(query: str, limit: int = 3) -> List[Dict[str, Any]]:
    return _get_default_instance().search(query, limit=limit)
