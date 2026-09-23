"""One-time migration: copies bots.db/pins.db/ratings.db/tasks.db into
the single shared athena.db (see db.py). Never touches or deletes the
old files -- they stay on disk as an untouched safety net. Safe to run
more than once: skips any table that already has rows in athena.db
rather than duplicating data.

Run once, before starting Athena against the new code:
    python migrate_to_athena_db.py
"""
import os
import sqlite3

import db
import bots
import annotations
import task_scheduler

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# (old file path, [table names in that file])
_SOURCES = [
    (os.path.join(_REPO_ROOT, "bots.db"), ["bots", "rooms", "room_messages"]),
    (os.path.join(_REPO_ROOT, "pins.db"), ["pinned_messages"]),
    (os.path.join(_REPO_ROOT, "ratings.db"), ["ratings"]),
    (os.path.join(_REPO_ROOT, "tasks.db"), ["tasks"]),
]


def _table_columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def main():
    # Creates athena.db (if not already there) with every table, via the
    # exact CREATE TABLE IF NOT EXISTS statements already in source --
    # no schema duplicated in this script.
    bots._bots_conn().close()
    annotations.ratings_conn().close()
    annotations._pins_conn().close()
    task_scheduler._tasks_conn().close()

    new_conn = db.get_conn()
    print(f"Target: {db.DB_PATH}")

    for old_path, tables in _SOURCES:
        if not os.path.exists(old_path):
            print(f"-- {old_path}: not found, skipping")
            continue
        old_conn = sqlite3.connect(f"file:{old_path}?mode=ro", uri=True)
        for table in tables:
            existing = new_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if existing:
                print(f"-- {table}: athena.db already has {existing} row(s), skipping (already migrated)")
                continue
            cols = _table_columns(old_conn, table)
            col_list = ", ".join(cols)
            rows = old_conn.execute(f"SELECT {col_list} FROM {table}").fetchall()
            if rows:
                placeholders = ", ".join("?" for _ in cols)
                new_conn.executemany(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", rows)
                new_conn.commit()
            after = new_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            status = "OK" if after == len(rows) else "MISMATCH"
            print(f"-- {table}: {len(rows)} row(s) in {old_path} -> {after} row(s) in athena.db [{status}]")
        old_conn.close()

    new_conn.close()
    print("Done. Old files were not modified or deleted.")


if __name__ == "__main__":
    main()
