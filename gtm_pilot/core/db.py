"""SQLite schema, migrations and connection handling."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    email          TEXT PRIMARY KEY,
    first_name     TEXT,
    last_name      TEXT,
    company        TEXT,
    job_title      TEXT,
    country        TEXT,
    event          TEXT,
    seniority      TEXT,
    industry       TEXT,
    is_decision_maker INTEGER,
    loaded_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    email_type     TEXT,            -- work | personal
    consent        TEXT             -- yes | no | '' (unspecified)
);

-- Enrichment results keyed by model, job title and company.
CREATE TABLE IF NOT EXISTS enrich_cache (
    model      TEXT NOT NULL,       -- provider/model
    job_title  TEXT NOT NULL,       -- lowercased
    company    TEXT NOT NULL,       -- lowercased
    seniority  TEXT NOT NULL,
    industry   TEXT NOT NULL,
    is_decision_maker INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (model, job_title, company)
);

-- One row per tracker run; reports are scoped to a single batch.
CREATE TABLE IF NOT EXISTS geo_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    providers   TEXT NOT NULL,
    samples     INTEGER NOT NULL DEFAULT 1,
    web_search  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS geo_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    industry   TEXT,
    prompt     TEXT NOT NULL,
    answer     TEXT NOT NULL,
    batch_id   INTEGER REFERENCES geo_batches(id),
    sample     INTEGER NOT NULL DEFAULT 1,
    grounded   INTEGER NOT NULL DEFAULT 0   -- 1 if the answer used web search grounding
);

CREATE TABLE IF NOT EXISTS geo_mentions (
    run_id    INTEGER NOT NULL REFERENCES geo_runs(id),
    brand     TEXT NOT NULL,
    position  INTEGER,          -- 1-based order of first mention
    stance    TEXT,             -- recommended | mentioned | negative; NULL if not classified
    PRIMARY KEY (run_id, brand)
);

-- Citations returned with grounded answers.
CREATE TABLE IF NOT EXISTS geo_sources (
    run_id    INTEGER NOT NULL REFERENCES geo_runs(id),
    url       TEXT NOT NULL,
    title     TEXT,
    domain    TEXT,
    PRIMARY KEY (run_id, url)
);
"""

# Columns added to existing tables; applied by _migrate() on connect.
ADDED_COLUMNS = {
    "leads": {"email_type": "TEXT", "consent": "TEXT"},
    "geo_runs": {
        "batch_id": "INTEGER REFERENCES geo_batches(id)",
        "sample": "INTEGER NOT NULL DEFAULT 1",
        "grounded": "INTEGER NOT NULL DEFAULT 0",
    },
    "geo_mentions": {"stance": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    # Assign answers without a batch to a single backfill batch.
    old = conn.execute(
        "SELECT MIN(run_at), GROUP_CONCAT(DISTINCT provider) FROM geo_runs WHERE batch_id IS NULL"
    ).fetchone()
    if old[0]:
        cur = conn.execute("INSERT INTO geo_batches (started_at, providers) VALUES (?, ?)", (old[0], old[1]))
        conn.execute("UPDATE geo_runs SET batch_id = ? WHERE batch_id IS NULL", (cur.lastrowid,))
    conn.execute("CREATE INDEX IF NOT EXISTS geo_runs_batch ON geo_runs(batch_id)")
    conn.commit()


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
