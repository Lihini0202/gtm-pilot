"""Deterministic cleaning of raw event-lead exports.

Covers header normalisation, email validation and typo correction, name and
country normalisation, consent parsing and de-duplication. Classification that
needs judgement is handled by `enrich`.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

# Canonical field -> accepted source column names (case-insensitive).
COLUMN_ALIASES = {
    "first_name": ["first name", "firstname", "first", "given name"],
    "last_name": ["last name", "lastname", "last", "surname", "family name"],
    "email": ["email", "email address", "e-mail", "work email"],
    "company": ["company", "company name", "organization", "organisation"],
    "job_title": ["job title", "title", "position", "role"],
    "country": ["country", "country/region"],
    "event": ["event", "event name", "source"],
    "consent": ["consent", "marketing consent", "gdpr consent", "opt in", "opt-in", "email opt-in",
                "email opt in", "marketing opt-in", "marketing opt in", "subscribed"],
}

# Accepted consent values, compared lowercased with whitespace collapsed.
CONSENT_YES = {"yes", "y", "true", "1", "opted in", "opt-in", "opt in", "subscribed", "agreed", "accepted"}
CONSENT_NO = {"no", "n", "false", "0", "opted out", "opt-out", "opt out", "unsubscribed", "declined", "refused"}

DOMAIN_FIXES = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gmail.con": "gmail.com", "gmail.co": "gmail.com",
    "yaho.com": "yahoo.com", "yahoo.con": "yahoo.com",
    "outlok.com": "outlook.com", "hotmial.com": "hotmail.com",
}

COUNTRY_FIXES = {
    "lk": "Sri Lanka", "sri lanka": "Sri Lanka", "srilanka": "Sri Lanka",
    "usa": "United States", "us": "United States", "united states": "United States",
    "uk": "United Kingdom", "india": "India", "singapore": "Singapore",
}

PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"}

EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")


@dataclass
class CleanReport:
    rows_in: int = 0
    rows_out: int = 0
    duplicates_removed: int = 0
    emails_fixed: int = 0
    rejected: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.rows_in} rows in -> {self.rows_out} clean leads | "
            f"{self.duplicates_removed} duplicates removed, {self.emails_fixed} email typos fixed, "
            f"{len(self.rejected)} rejected"
        )


def normalize_header(header: str) -> str | None:
    h = header.strip().lower()
    for canonical, aliases in COLUMN_ALIASES.items():
        if h == canonical or h in aliases:
            return canonical
    return None


def clean_email(raw: str) -> tuple[str | None, bool]:
    """Return (normalised email, or None if invalid; whether a domain typo was corrected)."""
    email = (raw or "").strip().lower()
    if "@" not in email:
        return None, False
    local, _, domain = email.rpartition("@")
    fixed = DOMAIN_FIXES.get(domain)
    if fixed:
        domain = fixed
    email = f"{local}@{domain}"
    return (email if EMAIL_RE.match(email) else None), bool(fixed)


def _cap(part: str) -> str:
    return part[:1].upper() + part[1:].lower()


def clean_name(raw: str) -> str:
    """Normalise whitespace and capitalisation, e.g. 'MARY-JANE' -> 'Mary-Jane', "o'neil" -> "O'Neil"."""
    words = " ".join((raw or "").split()).split(" ")
    return " ".join(
        "-".join("'".join(_cap(p) for p in piece.split("'")) for piece in w.split("-")) for w in words
    )


def clean_country(raw: str) -> str:
    c = " ".join((raw or "").split())
    return COUNTRY_FIXES.get(c.lower(), c)


def clean_consent(raw: str) -> str:
    """Map a consent value to 'yes', 'no' or '' (unspecified)."""
    v = " ".join((raw or "").lower().split())
    return "yes" if v in CONSENT_YES else "no" if v in CONSENT_NO else ""


def clean_rows(rows: list[dict]) -> tuple[list[dict], CleanReport]:
    report = CleanReport(rows_in=len(rows))
    seen: dict[str, dict] = {}
    for raw in rows:
        row = {}
        for k, v in raw.items():
            key = normalize_header(k or "")
            if key:
                row[key] = (v or "").strip()

        email, was_fixed = clean_email(row.get("email", ""))
        if not email:
            report.rejected.append({**raw, "reason": "invalid email"})
            continue
        report.emails_fixed += was_fixed

        lead = {
            "email": email,
            "first_name": clean_name(row.get("first_name", "")),
            "last_name": clean_name(row.get("last_name", "")),
            "company": " ".join(row.get("company", "").split()),
            "job_title": " ".join(row.get("job_title", "").split()),
            "country": clean_country(row.get("country", "")),
            "event": row.get("event", ""),
            "email_type": "personal" if email.split("@")[1] in PERSONAL_DOMAINS else "work",
            "consent": clean_consent(row.get("consent", "")),
        }

        if email in seen:
            report.duplicates_removed += 1
            # Keep the first record; fill its empty fields from the duplicate.
            for k, v in lead.items():
                if v and not seen[email].get(k):
                    seen[email][k] = v
            if lead["consent"] == "no":  # an opt-out on any duplicate takes precedence
                seen[email]["consent"] = "no"
            continue
        seen[email] = lead

    clean = list(seen.values())
    report.rows_out = len(clean)
    return clean, report


def read_csv(path: str | Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))
