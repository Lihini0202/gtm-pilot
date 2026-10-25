"""Lead export to SQLite, a HubSpot import CSV and the HubSpot CRM API.

HubSpot sync behaviour:
    * Empty values are never sent, so existing CRM data is not overwritten.
    * Only new contacts and changed properties are written.
    * Only contacts with consent == 'yes' are included; a blank answer is not consent.
    * `plan_upload` reports all changes without writing.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from gtm_pilot.core.http import RETRYABLE_STATUS, server_retry_wait

log = logging.getLogger(__name__)

API = "https://api.hubapi.com"

# Lead field -> HubSpot contact property. gtm_* are custom properties,
# created by `gtm-pilot hubspot --setup`.
HUBSPOT_FIELDS = {
    "email": "email",
    "first_name": "firstname",
    "last_name": "lastname",
    "company": "company",
    "job_title": "jobtitle",
    "country": "country",
    "seniority": "gtm_seniority",
    "industry": "gtm_industry",
    "is_decision_maker": "gtm_decision_maker",
    "event": "gtm_event",
    "email_type": "gtm_email_type",
}

# Custom property name -> (label, type, fieldType).
CUSTOM_PROPERTIES = {
    "gtm_seniority": ("GTM seniority", "string", "text"),
    "gtm_industry": ("GTM industry", "string", "text"),
    "gtm_decision_maker": ("GTM decision maker", "bool", "booleancheckbox"),
    "gtm_event": ("GTM event", "string", "text"),
    "gtm_email_type": ("GTM email type", "string", "text"),
}

# Enrichment fields; omitted from uploads when enrichment failed.
TAG_FIELDS = ("seniority", "industry", "is_decision_maker")


class HubSpotError(RuntimeError):
    """A HubSpot request failed, or the portal lacks required properties."""


# Local storage

def _keep(col: str) -> str:
    """Return an upsert SET clause that keeps the stored value when the new one is empty."""
    return f"{col} = COALESCE(NULLIF(excluded.{col}, ''), leads.{col})"


def save_to_db(conn: sqlite3.Connection, leads: list[dict]) -> None:
    """Upsert leads by email.

    Non-empty values replace stored ones; empty values leave them unchanged.
    If enrichment failed (empty seniority), stored enrichment fields are kept.
    """
    keep = _keep
    tags_failed = "excluded.seniority = ''"
    conn.executemany(
        f"""INSERT INTO leads (email, first_name, last_name, company, job_title, country, event,
                               seniority, industry, is_decision_maker, email_type, consent)
            VALUES (:email, :first_name, :last_name, :company, :job_title, :country, :event,
                    :seniority, :industry, :is_decision_maker, :email_type, :consent)
            ON CONFLICT(email) DO UPDATE SET
                {keep("first_name")}, {keep("last_name")}, {keep("company")}, {keep("job_title")},
                {keep("country")}, {keep("event")}, {keep("email_type")}, {keep("consent")},
                {keep("seniority")}, {keep("industry")},
                is_decision_maker = CASE WHEN {tags_failed} THEN leads.is_decision_maker
                                         ELSE excluded.is_decision_maker END""",
        [
            {"email_type": "", "consent": "", **lead, "is_decision_maker": int(bool(lead.get("is_decision_maker")))}
            for lead in leads
        ],
    )
    conn.commit()


def load_leads(conn: sqlite3.Connection) -> list[dict]:
    """Return all stored leads, ordered by email."""
    rows = conn.execute("SELECT * FROM leads ORDER BY email").fetchall()
    return [{**dict(r), "is_decision_maker": bool(r["is_decision_maker"])} for r in rows]


def consented(leads: list[dict]) -> list[dict]:
    """Return leads eligible for CRM upload (consent is 'yes'; blank or 'no' is excluded)."""
    return [lead for lead in leads if lead.get("consent") == "yes"]


def hubspot_properties(lead: dict) -> dict[str, str]:
    """Return the lead as HubSpot property values.

    Empty values are omitted, booleans are encoded as 'true'/'false', and
    enrichment fields are omitted when enrichment failed.
    """
    tags_ok = bool(lead.get("seniority"))
    props = {}
    for ours, hs in HUBSPOT_FIELDS.items():
        if ours == "email" or (ours in TAG_FIELDS and not tags_ok):
            continue
        value = lead.get(ours)
        if isinstance(value, bool):
            value = "true" if value else "false"  # booleancheckbox encoding
        if value not in (None, ""):
            props[hs] = str(value)
    return props


def write_hubspot_csv(leads: list[dict], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(HUBSPOT_FIELDS.values()))
        w.writeheader()
        for lead in leads:
            w.writerow({"email": lead["email"], **hubspot_properties(lead)})
    return p


# HubSpot API

def _call(session, method: str, path: str, token: str, body: dict | None = None,
          allow: tuple[int, ...] = (), retries: int = 3) -> requests.Response:
    """Send a HubSpot API request with retries.

    Responses with a status in `allow` are returned instead of raising.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    last = ""
    for attempt in range(1, retries + 1):
        wait = 2 ** (attempt - 1)
        try:
            r = session.request(method, API + path, headers=headers, json=body, timeout=30)
        except requests.RequestException as e:
            last = f"network error: {type(e).__name__}"
        else:
            if r.status_code < 300 or r.status_code in allow:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code not in RETRYABLE_STATUS:
                try:
                    last += f": {r.json().get('message', '')}"[:300]
                except ValueError:
                    pass
                break  # non-retryable client error
            wait = server_retry_wait(r) or wait
        log.warning("HubSpot %s %s failed (%s), attempt %d/%d", method, path, last, attempt, retries)
        if attempt < retries:
            time.sleep(wait)
    raise HubSpotError(f"HubSpot {method} {path} failed: {last}")


def missing_properties(token: str, session=None) -> list[str]:
    """Return required custom properties that do not exist in the portal."""
    session = session or requests.Session()
    return [name for name in CUSTOM_PROPERTIES
            if _call(session, "GET", f"/crm/v3/properties/contacts/{name}", token, allow=(404,)).status_code == 404]


def setup_properties(token: str, session=None) -> list[str]:
    """Create missing custom properties. Requires the crm.schemas.contacts.write scope."""
    session = session or requests.Session()
    created = []
    for name in missing_properties(token, session):
        label, type_, field_type = CUSTOM_PROPERTIES[name]
        _call(session, "POST", "/crm/v3/properties/contacts", token, {
            "name": name, "label": label, "type": type_, "fieldType": field_type, "groupName": "contactinformation",
        })
        created.append(name)
    return created


def fetch_existing(emails: list[str], token: str, session=None, batch_size: int = 100) -> dict[str, dict]:
    """Return current HubSpot properties, keyed by email, for contacts that exist."""
    session = session or requests.Session()
    found = {}
    for i in range(0, len(emails), batch_size):
        r = _call(session, "POST", "/crm/v3/objects/contacts/batch/read", token, {
            "idProperty": "email",
            "properties": list(HUBSPOT_FIELDS.values()),
            "inputs": [{"id": e} for e in emails[i : i + batch_size]],
        })
        for contact in r.json().get("results", []):
            props = contact.get("properties") or {}
            if props.get("email"):
                found[props["email"].lower()] = props
    return found


def plan_upload(leads: list[dict], token: str, session=None) -> list[dict]:
    """Classify each lead as new, update (with changed properties) or unchanged."""
    existing = fetch_existing([lead["email"] for lead in leads], token, session)
    plan = []
    for lead in leads:
        props = hubspot_properties(lead)
        current = existing.get(lead["email"])
        if current is None:
            plan.append({"email": lead["email"], "action": "new", "properties": props, "changes": {}})
            continue
        changes = {k: (current.get(k) or "", v) for k, v in props.items() if (current.get(k) or "") != v}
        plan.append({"email": lead["email"], "action": "update" if changes else "unchanged",
                     "properties": {k: v for k, (_, v) in changes.items()}, "changes": changes})
    return plan


def write_plan_csv(plan: list[dict], path: str | Path) -> Path:
    """Write the upload plan as one row per property change."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["email", "action", "property", "hubspot_now", "after_upload"])
        for item in plan:
            if item["action"] == "new":
                for k, v in item["properties"].items():
                    w.writerow([item["email"], "new", k, "", v])
            for k, (old, new) in item["changes"].items():
                w.writerow([item["email"], "update", k, old, new])
    return p


def _rejected_emails(r: requests.Response, emails: list[str]) -> set[str]:
    """Return the emails HubSpot rejected in a batch response.

    A 207 response means some inputs failed; its `errors` list names them in
    `context.ids`. If errors cannot be matched to emails, the whole batch is
    treated as failed so nothing is reported as uploaded by mistake.
    """
    if r.status_code != 207:
        return set()
    try:
        errors = r.json().get("errors") or []
    except ValueError:
        return set(emails)
    if not errors:
        return set()
    wanted = {e.lower(): e for e in emails}
    ids = {str(i).lower() for err in errors for i in (err.get("context") or {}).get("ids", [])}
    rejected = {wanted[i] for i in ids if i in wanted}
    for err in errors:
        log.warning("HubSpot rejected contacts: %s", str(err.get("message", ""))[:300])
    return rejected or set(emails)


@dataclass
class UploadResult:
    """Outcome of upload_to_hubspot."""
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: list[str] = field(default_factory=list)  # emails from failed batches


def upload_to_hubspot(leads: list[dict], token: str, batch_size: int = 100, session=None) -> UploadResult:
    """Upsert new and changed contacts by email.

    Raises HubSpotError if required properties are missing. Failed batches are
    logged and listed in the result; remaining batches continue.
    """
    session = session or requests.Session()
    missing = missing_properties(token, session)
    if missing:
        raise HubSpotError(f"HubSpot is missing contact properties: {', '.join(missing)}. "
                           "Run `gtm-pilot hubspot --setup` or create them in Settings -> Properties.")
    plan = plan_upload(leads, token, session)
    result = UploadResult(unchanged=sum(1 for p in plan if p["action"] == "unchanged"))
    todo = [p for p in plan if p["action"] != "unchanged"]
    for i in range(0, len(todo), batch_size):
        batch = todo[i : i + batch_size]
        body = {"inputs": [{"idProperty": "email", "id": p["email"], "properties": p["properties"]} for p in batch]}
        try:
            r = _call(session, "POST", "/crm/v3/objects/contacts/batch/upsert", token, body)
        except HubSpotError as e:
            log.error("HubSpot batch %d skipped: %s", i // batch_size + 1, e)
            result.failed += [p["email"] for p in batch]
            continue
        rejected = _rejected_emails(r, [p["email"] for p in batch])
        if rejected:
            log.error("HubSpot batch %d: %d contacts rejected", i // batch_size + 1, len(rejected))
            result.failed += [p["email"] for p in batch if p["email"] in rejected]
        done = [p for p in batch if p["email"] not in rejected]
        result.created += sum(1 for p in done if p["action"] == "new")
        result.updated += sum(1 for p in done if p["action"] == "update")
    log.info("HubSpot: %d created, %d updated, %d unchanged, %d failed",
             result.created, result.updated, result.unchanged, len(result.failed))
    return result
