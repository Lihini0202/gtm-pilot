"""GEO (generative engine optimisation) tracker.

Queries AI assistants with buyer-intent prompts and records, per answer, the
brands mentioned, their order and their stance (recommended, mentioned,
negative). Industry prompts are generated from the industries in the leads table.

Each prompt is sampled `samples` times per provider. A tracker run is stored as
one batch, and reports are computed per batch.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import yaml

from gtm_pilot.core.llm import LLMClient, LLMError, LLMResponse

log = logging.getLogger(__name__)

DEFAULT_CONFIG = Path(__file__).with_name("prompts.yaml")

# Output token limit for answers; sized to avoid truncated recommendation lists.
ANSWER_TOKENS = 2000

STANCES = ["recommended", "mentioned", "negative"]
STANCE_SYSTEM = (
    "You analyse how an AI assistant's answer talks about brands. For each brand listed, the stance must be "
    "one of: recommended (presented as a good choice for the question), mentioned (named without being "
    "recommended), negative (criticised or advised against)."
)

# Citation redirect hosts; for these, the source title holds the domain.
REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com",)


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    with Path(path).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def top_lead_industries(conn: sqlite3.Connection, limit: int = 3) -> list[str]:
    """Return industries ranked by decision-maker count, excluding 'Other'."""
    rows = conn.execute(
        """SELECT industry, SUM(is_decision_maker) AS dms, COUNT(*) AS n
           FROM leads WHERE industry NOT IN ('', 'Other')
           GROUP BY industry ORDER BY dms DESC, n DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [r["industry"] for r in rows]


def build_prompts(config: dict, industries: list[str]) -> list[tuple[str | None, str]]:
    prompts = [(ind, t.format(industry=ind.lower())) for ind in industries for t in config["templates"]]
    prompts += [(None, q) for q in config.get("general", [])]
    return prompts


def _brand_spec(name: str, spec) -> tuple[list[str], bool, list[str]]:
    """Normalise a brand entry to (aliases, case_sensitive, ignore).

    An entry is a list of aliases, or a mapping with `aliases` and optional
    `case_sensitive` and `ignore` (phrases starting with an alias that are not
    a mention, e.g. "Make sure").
    """
    if isinstance(spec, dict):
        return list(spec.get("aliases") or [name]), bool(spec.get("case_sensitive")), list(spec.get("ignore") or [])
    return list(spec or [name]), False, []


def find_mentions(answer: str, brands: dict) -> list[tuple[str, int]]:
    """Return [(brand, position)] in order of first appearance.

    Aliases match on word boundaries. Each brand is counted once, at its
    earliest match.
    """
    first_seen: dict[str, int] = {}
    for brand, spec in brands.items():
        aliases, case_sensitive, ignore = _brand_spec(brand, spec)
        flags = 0 if case_sensitive else re.IGNORECASE
        skip = [re.compile(rf"{re.escape(p)}(?![\w])", flags) for p in ignore]
        for alias in sorted(aliases, key=len, reverse=True):
            for m in re.finditer(rf"(?<![\w.]){re.escape(alias)}(?![\w])", answer, flags):
                if any(s.match(answer, m.start()) for s in skip):
                    continue
                if brand not in first_seen or m.start() < first_seen[brand]:
                    first_seen[brand] = m.start()
                break
    ordered = sorted(first_seen.items(), key=lambda kv: kv[1])
    return [(brand, i + 1) for i, (brand, _) in enumerate(ordered)]


def brands_with(brands: dict | None, brand: str | None) -> dict:
    """Return the configured brands with `brand` added if absent."""
    out = dict(brands or {})
    if brand and brand not in out:
        out[brand] = [brand]
    return out


def source_domain(url: str, title: str | None = None) -> str:
    host = (urlparse(url).hostname or "").lower()
    if not host or host.endswith(REDIRECT_HOSTS):
        host = (title or "").strip().lower()
    return host.removeprefix("www.")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _stance_for(result: dict, brand: str):
    """Return the stance for `brand`, matching keys case- and punctuation-insensitively."""
    if brand in result:
        return result[brand]
    want = _norm(brand)
    for key, value in result.items():
        got = _norm(str(key))
        if got and (got == want or got in want or want in got):
            return value
    return None


def validate_stances(result: dict, brands: list[str]) -> dict[str, str | None]:
    """Restrict stance values to STANCES.

    Brands missing from `result` map to None; unrecognised values map to
    'mentioned'.
    """
    out = {}
    for b in brands:
        v = _stance_for(result, b) if isinstance(result, dict) else None
        v = v.strip().lower() if isinstance(v, str) else v
        if v is not None and v not in STANCES:
            log.warning("Model returned stance %r for %s, using 'mentioned'", v, b)
            v = "mentioned"
        out[b] = v
    return out


def judge_stances(client: LLMClient, question: str, answer: str, brands: list[str]) -> dict[str, str | None]:
    """Classify the stance of each brand in an answer.

    Brands omitted from the first response are requested once more; any still
    missing are returned as None.
    """
    out: dict[str, str | None] = {b: None for b in brands}
    todo = list(brands)
    for _ in range(2):
        if not todo:
            break
        prompt = (
            f"Question: {question}\n\nAnswer:\n{answer}\n\n"
            f"Brands: {', '.join(todo)}\n"
            "Return JSON with exactly these brand names as keys, each mapped to its stance, "
            'for example {"BrandA": "recommended"}'
        )
        try:
            out.update({b: v for b, v in validate_stances(client.complete_json(prompt, STANCE_SYSTEM), todo).items()
                        if v is not None})
        except LLMError as e:
            log.warning("Couldn't judge stances on %s: %s", client.provider, e)
        todo = [b for b in brands if out[b] is None]
    if todo:
        log.warning("No stance for %s on %s", ", ".join(todo), client.provider)
    return out


def _save_answer(conn, batch_id, industry, prompt, sample, resp: LLMResponse, brands, judge: LLMClient | None):
    run_id = conn.execute(
        """INSERT INTO geo_runs (provider, model, industry, prompt, answer, batch_id, sample, grounded)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (resp.provider, resp.model, industry, prompt, resp.text, batch_id, sample, int(resp.grounded)),
    ).lastrowid
    mentions = find_mentions(resp.text, brands)
    stances = judge_stances(judge, prompt, resp.text, [b for b, _ in mentions]) if judge else {}
    conn.executemany(
        "INSERT INTO geo_mentions (run_id, brand, position, stance) VALUES (?, ?, ?, ?)",
        [(run_id, b, pos, stances.get(b)) for b, pos in mentions],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO geo_sources (run_id, url, title, domain) VALUES (?, ?, ?, ?)",
        [
            (run_id, s["url"], s.get("title") or "", source_domain(s["url"], s.get("title")))
            for s in resp.sources
            if isinstance(s.get("url"), str) and s["url"].startswith(("http://", "https://"))
        ],
    )


def run_tracker(
    conn: sqlite3.Connection,
    clients: list[LLMClient],
    config: dict,
    industries: list[str],
    brand: str | None = None,
    samples: int | None = None,
    web_search: bool | None = None,
) -> int:
    """Run every prompt on every client and store the answers as one batch.

    `samples` and `web_search` default to the values in `config`. Returns the
    number of answers saved.
    """
    prompts = build_prompts(config, industries)
    brands = brands_with(config.get("brands"), brand)
    samples = max(1, int(samples or config.get("samples", 1)))
    web_search = bool(config.get("web_search", False) if web_search is None else web_search)
    judge = bool(config.get("judge_stance", False))
    batch_id = conn.execute(
        "INSERT INTO geo_batches (providers, samples, web_search) VALUES (?, ?, ?)",
        (",".join(c.provider for c in clients), samples, int(web_search)),
    ).lastrowid
    saved = 0
    for client in clients:
        for industry, prompt in prompts:
            for sample in range(1, samples + 1):
                try:
                    resp = client.complete(prompt, max_tokens=ANSWER_TOKENS, web_search=web_search)
                except LLMError as e:
                    log.error("Skipping prompt on %s: %s", client.provider, e)
                    continue
                if not resp.text.strip():
                    log.warning("Empty answer from %s, not counted: %s", client.provider, prompt)
                    continue
                _save_answer(conn, batch_id, industry, prompt, sample, resp, brands, client if judge else None)
                saved += 1
        conn.commit()
    log.info("Saved %d AI answers across %d provider(s) in batch %d", saved, len(clients), batch_id)
    return saved


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    """Return batches with at least one answer, newest first."""
    rows = conn.execute(
        """SELECT b.id, b.started_at, b.providers, b.samples, b.web_search,
                  COUNT(r.id) AS answers, SUM(r.grounded) AS grounded
           FROM geo_batches b JOIN geo_runs r ON r.batch_id = b.id
           GROUP BY b.id ORDER BY b.id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def visibility_report(conn: sqlite3.Connection, brand: str | None, batch_id: int | None = None) -> dict:
    """Build the visibility report for a batch (default: the latest).

    Includes per-brand mention and recommendation rates, the tracked brand's
    results by industry and by prompt, and cited domains for grounded answers.
    """
    batches = list_batches(conn)
    batch = next((b for b in batches if b["id"] == batch_id), None) if batch_id else (batches[0] if batches else None)
    report = {"brand": brand, "batch": batch, "total_answers": 0, "judged": False,
              "share_of_voice": [], "by_industry": [], "by_question": [], "top_sources": []}
    if not batch:
        return report
    bid, total = batch["id"], batch["answers"]
    report["total_answers"] = total

    share = [dict(r) for r in conn.execute(
        """SELECT m.brand, COUNT(*) AS answers, ROUND(100.0 * COUNT(*) / ?, 1) AS pct,
                  ROUND(AVG(m.position), 1) AS avg_position,
                  SUM(CASE WHEN m.stance = 'recommended' THEN 1 ELSE 0 END) AS recommended,
                  SUM(CASE WHEN m.stance = 'negative' THEN 1 ELSE 0 END) AS negative,
                  SUM(CASE WHEN m.stance IS NOT NULL THEN 1 ELSE 0 END) AS judged
           FROM geo_mentions m JOIN geo_runs r ON r.id = m.run_id
           WHERE r.batch_id = ? GROUP BY m.brand ORDER BY answers DESC, avg_position""",
        (total, bid),
    )]
    report["judged"] = any(r["judged"] for r in share)
    for r in share:
        r["recommended_pct"] = round(100.0 * r["recommended"] / total, 1) if report["judged"] else None
    report["share_of_voice"] = share

    if brand:
        per_answer = """FROM geo_runs r LEFT JOIN geo_mentions m ON m.run_id = r.id AND m.brand = ?
                        WHERE r.batch_id = ?"""
        counts = """COUNT(*) AS answers,
                    SUM(CASE WHEN m.brand IS NOT NULL THEN 1 ELSE 0 END) AS mentioned,
                    SUM(CASE WHEN m.stance = 'recommended' THEN 1 ELSE 0 END) AS recommended"""
        report["by_industry"] = [dict(r) for r in conn.execute(
            f"SELECT COALESCE(r.industry, 'General') AS industry, {counts} {per_answer} "
            "GROUP BY COALESCE(r.industry, 'General')",
            (brand, bid),
        )]
        report["by_question"] = [dict(r) for r in conn.execute(
            f"SELECT COALESCE(r.industry, 'General') AS industry, r.prompt, {counts} {per_answer} "
            "GROUP BY r.prompt ORDER BY mentioned, r.prompt",
            (brand, bid),
        )]

    if batch["grounded"]:
        report["top_sources"] = [dict(r) for r in conn.execute(
            """SELECT s.domain, COUNT(DISTINCT s.run_id) AS answers,
                      ROUND(100.0 * COUNT(DISTINCT s.run_id) / ?, 1) AS pct
               FROM geo_sources s JOIN geo_runs r ON r.id = s.run_id
               WHERE r.batch_id = ? GROUP BY s.domain ORDER BY answers DESC, s.domain LIMIT 15""",
            (batch["grounded"], bid),
        )]
    return report
