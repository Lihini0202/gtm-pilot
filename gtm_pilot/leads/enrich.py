"""LLM enrichment of leads: seniority, industry and decision-maker flag.

Model output is validated against fixed value sets; invalid values are replaced
with defaults and logged. Results are cached per model in the database.
"""

from __future__ import annotations

import logging
import sqlite3

from gtm_pilot.core.llm import LLMClient, LLMError

log = logging.getLogger(__name__)

SENIORITIES = ["C-Level", "VP", "Director", "Manager", "Individual Contributor"]
INDUSTRIES = ["Banking", "Insurance", "Healthcare", "Telecom", "Retail", "Other"]

SYSTEM = (
    "You classify B2B event leads for a sales team. "
    f"seniority must be one of {SENIORITIES}. industry must be one of {INDUSTRIES}. "
    "is_decision_maker is true for C-Level, VP and Director roles that could buy software."
)


def build_prompt(lead: dict) -> str:
    return (
        f"Job title: {lead.get('job_title') or 'unknown'}\n"
        f"Company: {lead.get('company') or 'unknown'}\n"
        f"Email domain: {lead['email'].split('@')[1]}\n"
        'Return JSON: {"seniority": ..., "industry": ..., "is_decision_maker": true/false}'
    )


def validate(result: dict) -> dict:
    seniority = result.get("seniority")
    industry = result.get("industry")
    dm = result.get("is_decision_maker")
    if seniority not in SENIORITIES:
        log.warning("Model returned unknown seniority %r, using default", seniority)
        seniority = "Individual Contributor"
        dm = None  # re-derive from the defaulted seniority
    if industry not in INDUSTRIES:
        log.warning("Model returned unknown industry %r, using Other", industry)
        industry = "Other"
    if not isinstance(dm, bool):
        dm = seniority in {"C-Level", "VP", "Director"}
    return {"seniority": seniority, "industry": industry, "is_decision_maker": dm}


def _saved_tags(conn: sqlite3.Connection | None, model: str, key: tuple) -> dict | None:
    if conn is None:
        return None
    row = conn.execute(
        "SELECT seniority, industry, is_decision_maker FROM enrich_cache WHERE model = ? AND job_title = ? AND company = ?",
        (model, *key),
    ).fetchone()
    return {"seniority": row[0], "industry": row[1], "is_decision_maker": bool(row[2])} if row else None


def _save_tags(conn: sqlite3.Connection | None, model: str, key: tuple, tags: dict) -> None:
    if conn is not None:
        conn.execute(
            """INSERT OR REPLACE INTO enrich_cache (model, job_title, company, seniority, industry, is_decision_maker)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (model, *key, tags["seniority"], tags["industry"], int(tags["is_decision_maker"])),
        )


def enrich_leads(leads: list[dict], client: LLMClient, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Return leads with enrichment fields added.

    With `conn`, cached results for the same model are reused and new results
    are stored. Failed calls are not cached.
    """
    model = f"{client.provider}/{client.model}"
    out = []
    cache: dict[tuple, dict] = {}  # in-run cache keyed by (job title, company)
    calls = reused = 0
    for lead in leads:
        key = (lead.get("job_title", "").lower(), lead.get("company", "").lower())
        if key not in cache:
            saved = _saved_tags(conn, model, key)
            if saved:
                cache[key], reused = saved, reused + 1
            else:
                calls += 1
                try:
                    cache[key] = validate(client.complete_json(build_prompt(lead), SYSTEM))
                    _save_tags(conn, model, key, cache[key])
                except LLMError as e:
                    log.error("Enrichment failed for %s: %s", lead["email"], e)
                    cache[key] = {"seniority": "", "industry": "", "is_decision_maker": False}
        out.append({**lead, **cache[key]})
    if conn is not None:
        conn.commit()
    log.info("Enriched %d leads with %d model calls (%d reused from earlier runs)", len(out), calls, reused)
    return out
