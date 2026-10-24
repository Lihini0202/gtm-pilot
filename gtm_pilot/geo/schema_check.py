"""Audit schema.org JSON-LD structured data in an HTML page.

Reports missing types and recommended properties for B2B sites, plus the
page title and meta description.
"""

from __future__ import annotations

import json
import re

# schema.org type -> recommended properties
RECOMMENDED = {
    "Organization": ["name", "url", "logo", "sameAs", "description"],
    "WebSite": ["name", "url"],
    "Article": ["headline", "author", "datePublished", "image"],
    "SoftwareApplication": ["name", "applicationCategory", "offers"],
    "FAQPage": ["mainEntity"],
}

JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)


def extract_jsonld(html: str) -> tuple[list[dict], list[str]]:
    items, errors = [], []
    for i, block in enumerate(JSONLD_RE.findall(html), 1):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError as e:
            errors.append(f"JSON-LD block {i} is not valid JSON ({e.msg})")
            continue
        if isinstance(data, dict) and "@graph" in data:
            data = data["@graph"]
        items.extend(d for d in (data if isinstance(data, list) else [data]) if isinstance(d, dict))
    return items, errors


def check_html(html: str, page_kind: str = "home") -> dict:
    items, errors = extract_jsonld(html)
    found: dict[str, dict] = {}
    for item in items:
        types = item.get("@type", [])
        for t in [types] if isinstance(types, str) else types:
            found.setdefault(t, item)

    issues = list(errors)
    expected = ["Organization", "WebSite"] if page_kind == "home" else ["Article"]
    for t in expected:
        if t not in found:
            issues.append(f"Missing {t} structured data")
    for t, item in found.items():
        missing = [f for f in RECOMMENDED.get(t, []) if not item.get(f)]
        if missing:
            issues.append(f"{t} is missing: {', '.join(missing)}")

    has_title = bool(re.search(r"<title>\s*\S", html, re.IGNORECASE))
    has_desc = bool(re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']\s*\S', html, re.IGNORECASE))
    if not has_title:
        issues.append("Missing <title>")
    if not has_desc:
        issues.append("Missing meta description")

    return {"types_found": sorted(found), "issues": issues, "score": max(0, 100 - 15 * len(issues))}
