"""End-to-end pipeline test in mock mode (no network)."""

import csv

from gtm_pilot import cli


def test_full_run_in_mock_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GTM_DB_PATH", str(tmp_path / "test.db"))

    cli.main(["demo-data", "--n", "60", "--out", "raw.csv"])
    cli.main(["run", "raw.csv", "--out", "hubspot.csv"])

    with open("hubspot.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    emails = [r["email"] for r in rows]
    assert rows and len(emails) == len(set(emails))  # no duplicates
    assert all(e == e.lower() and "@" in e for e in emails)
    assert not any("gmial" in e for e in emails)
    assert {"gtm_industry", "gtm_seniority", "gtm_decision_maker", "gtm_event"} <= rows[0].keys()

    with open("raw.csv", newline="") as f:
        declined = {r["Email"].strip().lower() for r in csv.DictReader(f) if r["Marketing Consent"] == "No"}
    assert declined and not declined & set(emails)  # opted-out contacts are excluded from the CRM file


def test_brand_comes_only_from_env(monkeypatch, tmp_path):
    from gtm_pilot.core.config import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GTM_BRAND", raising=False)
    assert Settings.from_env().brand is None
    monkeypatch.setenv("GTM_BRAND", " Beta Corp ")
    assert Settings.from_env().brand == "Beta Corp"
