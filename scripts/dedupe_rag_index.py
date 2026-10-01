"""One-off cleanup for rag_index.db's duplicate-path indexing bug (fixed in
rag.py's index_codebase, which now canonicalizes every filepath). Existing
rows indexed before that fix are stuck under their old, non-canonical
filepath spelling forever, since chunk ids are derived from filepath.

Every duplicate pair observed in production has exactly one variant that is
already its own os.path.realpath() form, so the safe cleanup is: delete any
row whose filepath is NOT already canonical. The canonical sibling row is
left untouched -- no data loss, no re-embedding needed. If some future
duplicate ever has neither variant canonical, this deletes both; the next
index_codebase() run simply re-embeds that file fresh under its canonical
path.

Usage: python3 scripts/dedupe_rag_index.py [path/to/rag_index.db]
"""
import os
import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ATHENA_RAG_DB", "rag_index.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT filepath), COUNT(*) FROM chunks")
    before = cur.fetchone()
    print(f"before: {before[0]} distinct filepath(s), {before[1]} chunk row(s)")

    cur.execute("SELECT DISTINCT filepath FROM chunks")
    paths = [r[0] for r in cur.fetchall()]
    deleted_groups = 0
    for filepath in paths:
        canonical = os.path.realpath(filepath)
        if filepath != canonical:
            cur.execute("DELETE FROM chunks WHERE filepath = ?", (filepath,))
            deleted_groups += 1
    conn.commit()

    cur.execute("SELECT COUNT(DISTINCT filepath), COUNT(*) FROM chunks")
    after = cur.fetchone()
    print(f"removed {deleted_groups} non-canonical filepath group(s)")
    print(f"after: {after[0]} distinct filepath(s), {after[1]} chunk row(s)")
    conn.close()


if __name__ == "__main__":
    main()
