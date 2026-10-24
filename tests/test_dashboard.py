"""Headless rendering tests for the Streamlit dashboard."""

from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")

from gtm_pilot import cli

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")


def test_dashboard_renders_with_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GTM_DB_PATH", str(tmp_path / "dash.db"))
    monkeypatch.setenv("GTM_BRAND", "Databricks")
    cli.main(["demo-data", "--n", "40", "--out", "raw.csv"])
    cli.main(["run", "raw.csv", "--out", "hs.csv"])

    at = st_testing.AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "Clean leads" in labels and "AI answers checked" in labels


def test_dashboard_empty_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GTM_DB_PATH", str(tmp_path / "empty.db"))
    at = st_testing.AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert at.info  # empty-state message
