"""Synthetic event-lead data for demos and tests.

Includes common export defects: duplicates with inconsistent casing, email
domain typos, stray whitespace, missing fields and free-text job titles.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

FIRST = ["Nimal", "Ayesha", "Kasun", "Priya", "James", "Sarah", "Ravi", "Emily", "Dilan", "Fatima",
         "Michael", "Chen", "Tharushi", "David", "Olivia", "Arjun", "Sofia", "Lahiru", "Grace", "Omar"]
LAST = ["Perera", "Fernando", "Silva", "Smith", "Johnson", "Wijesinghe", "Khan", "Lee", "Brown",
        "Jayasuriya", "Patel", "Garcia", "Dissanayake", "Wong", "Taylor", "Bandara"]

COMPANIES = [
    ("Commercial Bank of Ceylon", "combank.lk"), ("First Capital Holdings", "firstcapital.lk"),
    ("Union Assurance", "unionassurance.com"), ("Allianz Life", "allianz.com"),
    ("Asiri Health", "asirihealth.com"), ("Nawaloka Hospitals", "nawaloka.com"),
    ("Dialog Axiata Telecom", "dialog.lk"), ("Keells Retail", "keells.com"),
    ("Cargills Food City Retail", "cargillsceylon.com"), ("Northbridge Credit Union", "northbridgecu.org"),
    ("Summit Mutual Insurance", "summitmutual.com"), ("MetroCare Medical", "metrocare.org"),
]

TITLES = [
    "Chief Data Officer", "Head of Analytics", "VP, Data Science", "Data Scientist",
    "Senior ML Engineer", "Director of Risk Analytics", "CTO", "Analytics Manager",
    "Business Analyst", "Head of Digital Transformation", "Data Engineer", "CIO",
    "Principal Data Scientist", "Marketing Analyst", "SVP Technology",
]

EMAIL_TYPOS = {"gmail.com": ["gmial.com", "gmail.con", "gmai.com"], "yahoo.com": ["yaho.com"], "outlook.com": ["outlok.com"]}

COUNTRIES = ["Sri Lanka", "sri lanka", "LK", "USA", "United States", "India", "Singapore", ""]


def _messy_name(name: str, rng: random.Random) -> str:
    style = rng.random()
    if style < 0.15:
        return name.upper()
    if style < 0.30:
        return name.lower()
    if style < 0.40:
        return f"  {name} "
    return name


def generate_leads(n: int = 200, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    rows: list[dict] = []
    for _ in range(n):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        company, domain = rng.choice(COMPANIES)
        if rng.random() < 0.2:  # personal email domains, some with typos
            domain = rng.choice(list(EMAIL_TYPOS))
            if rng.random() < 0.5:
                domain = rng.choice(EMAIL_TYPOS[domain])
        email = f"{first}.{last}{rng.randint(1, 99)}@{domain}".lower()
        if rng.random() < 0.1:
            email = email.upper()
        if rng.random() < 0.03:
            email = email.replace("@", " at ")  # invalid address; rejected by cleaning
        rows.append({
            "First Name": _messy_name(first, rng),
            "Last Name": _messy_name(last, rng),
            "Email": email,
            "Company": company if rng.random() > 0.05 else "",
            "Job Title": rng.choice(TITLES) if rng.random() > 0.05 else "",
            "Country": rng.choice(COUNTRIES),
            "Event": "AI Summit Colombo 2026",
        })
    # Marketing consent. A separate RNG keeps the base rows identical for a given seed.
    consent_rng = random.Random(seed + 1)
    for row in rows:
        roll = consent_rng.random()
        row["Marketing Consent"] = "Yes" if roll < 0.8 else "" if roll < 0.92 else "No"
    # Duplicate scans, some with different casing.
    for row in rng.sample(rows, k=max(1, n // 10)):
        dup = dict(row)
        dup["Email"] = dup["Email"].upper() if rng.random() < 0.5 else dup["Email"]
        dup["First Name"] = dup["First Name"].strip().lower()
        rows.append(dup)
    rng.shuffle(rows)
    return rows


def write_csv(rows: list[dict], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return p
