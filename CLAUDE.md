# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this is
GTM Pilot: a Python CLI and Streamlit dashboard for a go-to-market team.
- `gtm_pilot/leads/`: clean, enrich (LLM) and export event leads
- `gtm_pilot/geo/`: track brand mentions in AI assistant answers for lead industries
- `gtm_pilot/core/`: shared LLM client, SQLite, config

## Commands
- Install: `pip install -e ".[dev,dashboard]"`
- Test: `pytest -q`
- Lint: `ruff check .` (fix with `ruff check . --fix`)
- Full offline run: `gtm-pilot demo-data && gtm-pilot run data/raw_leads.csv`
- Dashboard: `streamlit run dashboard/app.py`

## Rules
- Every change keeps `pytest` and `ruff` green. New behaviour needs a test.
- Tests must never call real APIs. Use `LLMClient("mock")` or the `FakeSession` in `tests/test_llm.py`.
- Never log, print or put API keys in URLs. Keys come only from env vars via `core/config.py`.
- Validate anything an LLM returns before storing it (see `leads/enrich.py::validate`).
- Deterministic cleaning belongs in `leads/clean.py` as plain code; use the LLM only for fuzzy judgement.
- Keep dependencies minimal: `requests` and `PyYAML` at runtime. Ask before adding more.
- Work on a branch and open a PR. Don't push to `main`.
