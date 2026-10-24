import sqlite3

from gtm_pilot.core import db

OLD_SCHEMA = """
CREATE TABLE geo_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT DEFAULT CURRENT_TIMESTAMP,
    provider TEXT NOT NULL, model TEXT NOT NULL, industry TEXT, prompt TEXT NOT NULL, answer TEXT NOT NULL);
CREATE TABLE geo_mentions (run_id INTEGER NOT NULL REFERENCES geo_runs(id), brand TEXT NOT NULL,
    position INTEGER, PRIMARY KEY (run_id, brand));
INSERT INTO geo_runs (provider, model, prompt, answer) VALUES ('gemini', 'g', 'q', 'a'), ('gemini', 'g', 'q', 'b');
INSERT INTO geo_mentions VALUES (1, 'Databricks', 1);
"""


def test_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.executescript(OLD_SCHEMA)

    conn = db.connect(path)
    batches = conn.execute("SELECT id, providers FROM geo_batches").fetchall()
    assert [tuple(b) for b in batches] == [(1, "gemini")]
    assert {r["batch_id"] for r in conn.execute("SELECT batch_id FROM geo_runs")} == {1}
    assert conn.execute("SELECT stance FROM geo_mentions").fetchone()["stance"] is None
    conn.close()

    # migration is idempotent
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM geo_batches").fetchone()[0] == 1
    conn.close()
