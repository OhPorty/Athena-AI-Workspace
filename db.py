import os
import sqlite3

# Shared connection for the small relational data that used to live as
# four separate files (bots.db, pins.db, ratings.db, tasks.db) -- each
# owning module still defines and creates its own tables (see
# bots._bots_conn, annotations.ratings_conn/_pins_conn,
# task_scheduler._tasks_conn), this just gives them one file and one
# path to agree on instead of each computing its own independently.
#
# Deliberately excludes rag_index.db (fully regenerable by re-indexing --
# merging derived data with authoritative data only grows the blast
# radius of a mistake for no benefit) and lcm/lcm.db (owned by the
# separately-vendored, portable LCM service).
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "athena.db")


def get_conn():
    return sqlite3.connect(DB_PATH)
