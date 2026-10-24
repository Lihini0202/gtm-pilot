import re

from gtm_pilot.core.llm import LLMClient, LLMResponse
from gtm_pilot.geo.schema_check import check_html
from gtm_pilot.geo.track import (
    STANCES,
    brands_with,
    build_prompts,
    find_mentions,
    judge_stances,
    list_batches,
    load_config,
    run_tracker,
    source_domain,
    top_lead_industries,
    validate_stances,
    visibility_report,
)
from gtm_pilot.leads.export import save_to_db

BRANDS = {"Acme AI": ["Acme.ai", "Acme"], "AWS SageMaker": ["SageMaker", "AWS"], "Databricks": ["Databricks"]}


def test_mentions_in_order():
    text = "Try Databricks first, then Acme.ai, or SageMaker."
    assert find_mentions(text, BRANDS) == [("Databricks", 1), ("Acme AI", 2), ("AWS SageMaker", 3)]


def test_brand_counted_once_and_no_partial_word_matches():
    text = "Acme.ai is great. Acme again. New laws apply."  # 'laws' must not match AWS
    assert find_mentions(text, BRANDS) == [("Acme AI", 1)]


def test_prompts_built_from_industries():
    cfg = {"templates": ["best AI for {industry}?"], "general": ["general q"]}
    assert build_prompts(cfg, ["Banking"]) == [("Banking", "best AI for banking?"), (None, "general q")]


def _lead(email, industry, dm):
    return {"email": email, "first_name": "", "last_name": "", "company": "", "job_title": "",
            "country": "", "event": "", "seniority": "", "industry": industry, "is_decision_maker": dm}


def test_top_industries_come_from_decision_makers(conn):
    save_to_db(conn, [
        _lead("a@x", "Retail", False), _lead("b@x", "Retail", False), _lead("c@x", "Retail", False),
        _lead("d@x", "Banking", True), _lead("e@x", "Banking", True),
        _lead("f@x", "Other", True),
    ])
    assert top_lead_industries(conn) == ["Banking", "Retail"]


def test_full_geo_run_with_mock(conn):
    cfg = load_config()
    n = run_tracker(conn, [LLMClient("mock")], cfg, ["Banking"], "Acme AI")
    assert n == (len(cfg["templates"]) + len(cfg["general"])) * cfg["samples"]
    report = visibility_report(conn, "Acme AI")
    assert report["total_answers"] == n
    assert all(0 < r["pct"] <= 100 for r in report["share_of_voice"])
    assert {r["industry"] for r in report["by_industry"]} == {"Banking", "General"}


def test_failed_provider_is_skipped(conn):
    class Flaky(LLMClient):
        def complete(self, prompt, system=None, max_tokens=800, web_search=False):
            from gtm_pilot.core.llm import LLMError
            if "banking" in prompt:
                raise LLMError("down")
            return LLMResponse("mock", "m", "Acme.ai")

    cfg = {"templates": ["x {industry}"], "general": ["y"], "brands": BRANDS}
    assert run_tracker(conn, [Flaky("mock")], cfg, ["Banking"]) == 1


def test_own_brand_is_tracked_without_config_entry(conn):
    class Answers(LLMClient):
        def complete(self, prompt, system=None, max_tokens=800, web_search=False):
            return LLMResponse("mock", "m", "Try Databricks or Beta Corp.")

    cfg = {"templates": [], "general": ["q"], "brands": {"Databricks": ["Databricks"]}}
    run_tracker(conn, [Answers("mock")], cfg, [], "Beta Corp")
    report = visibility_report(conn, "Beta Corp")
    assert {r["brand"] for r in report["share_of_voice"]} == {"Databricks", "Beta Corp"}
    assert report["by_industry"] == [{"industry": "General", "answers": 1, "mentioned": 1, "recommended": 0}]


def test_brands_with_keeps_configured_aliases():
    assert brands_with(BRANDS, "Acme AI") == BRANDS
    assert brands_with(None, None) == {}


def test_report_without_brand_has_no_brand_section(conn):
    run_tracker(conn, [LLMClient("mock")], load_config(), ["Banking"])
    report = visibility_report(conn, None)
    assert report["share_of_voice"] and report["by_industry"] == []


def test_schema_check_finds_gaps():
    html = """<html><head><title>Acme</title>
    <script type="application/ld+json">{"@type": "Organization", "name": "Acme", "url": "https://acme.test"}</script>
    </head></html>"""
    result = check_html(html)
    assert result["types_found"] == ["Organization"]
    assert "Missing WebSite structured data" in result["issues"]
    assert any("logo" in i for i in result["issues"])
    assert "Missing meta description" in result["issues"]


def test_schema_check_reports_broken_json():
    html = '<script type="application/ld+json">{"@type": "Organization",}</script>'
    assert any("not valid JSON" in i for i in check_html(html)["issues"])


# Sampling, batches, stance classification, citations and alias matching

def test_each_question_is_asked_samples_times(conn):
    cfg = {"templates": [], "general": ["q1", "q2"], "brands": BRANDS}
    assert run_tracker(conn, [LLMClient("mock")], cfg, [], samples=3) == 6
    assert [r["sample"] for r in conn.execute("SELECT sample FROM geo_runs WHERE prompt = 'q1'")] == [1, 2, 3]


def test_report_covers_one_batch_and_defaults_to_latest(conn):
    class Says(LLMClient):
        def __init__(self, text):
            super().__init__("mock")
            self.text = text

        def complete(self, prompt, system=None, max_tokens=800, web_search=False):
            return LLMResponse("mock", "m", self.text)

    cfg = {"templates": [], "general": ["q"], "brands": BRANDS}
    run_tracker(conn, [Says("Databricks")], cfg, [])
    run_tracker(conn, [Says("Acme.ai")], cfg, [])
    latest = visibility_report(conn, "Acme AI")
    assert [r["brand"] for r in latest["share_of_voice"]] == ["Acme AI"]
    first = visibility_report(conn, "Acme AI", list_batches(conn)[-1]["id"])
    assert [r["brand"] for r in first["share_of_voice"]] == ["Databricks"]


def test_empty_answers_are_not_counted(conn):
    class Blank(LLMClient):
        def complete(self, prompt, system=None, max_tokens=800, web_search=False):
            return LLMResponse("mock", "m", "   ")

    assert run_tracker(conn, [Blank("mock")], {"templates": [], "general": ["q"], "brands": BRANDS}, []) == 0
    assert visibility_report(conn, "Acme AI")["total_answers"] == 0


def test_stances_are_validated():
    assert validate_stances({"A": "Recommended", "B": "loves it"}, ["A", "B", "C"]) == {
        "A": "recommended", "B": "mentioned", "C": None}


def test_stance_keys_may_be_renamed_by_the_model():
    result = {"Vertex AI": "negative", "amazon bedrock": "recommended"}
    assert validate_stances(result, ["Google Vertex AI", "Amazon Bedrock"]) == {
        "Google Vertex AI": "negative", "Amazon Bedrock": "recommended"}


def test_brands_the_judge_skips_are_asked_again():
    asked = []

    class Forgetful(LLMClient):
        def complete_json(self, prompt, system=None):
            brands = next(ln for ln in prompt.splitlines() if ln.startswith("Brands:"))
            asked.append(brands)
            return {"A": "recommended"} if len(asked) == 1 else {"B": "negative"}

    out = judge_stances(Forgetful("mock"), "q", "A and B", ["A", "B"])
    assert out == {"A": "recommended", "B": "negative"}
    assert asked == ["Brands: A, B", "Brands: B"]


def test_judged_run_reports_recommendation_rate(conn):
    cfg = {**load_config(), "judge_stance": True}
    run_tracker(conn, [LLMClient("mock")], cfg, ["Banking"], samples=1)
    report = visibility_report(conn, None)
    assert report["judged"]
    stances = {r["stance"] for r in conn.execute("SELECT stance FROM geo_mentions")}
    assert stances <= set(STANCES) and stances
    for r in report["share_of_voice"]:
        assert r["recommended_pct"] <= r["pct"]


def test_judge_failure_keeps_the_mention(conn):
    class NoJudge(LLMClient):
        def complete(self, prompt, system=None, max_tokens=800, web_search=False):
            return LLMResponse("mock", "m", "not json" if system else "Try Databricks.")

    cfg = {"templates": [], "general": ["q"], "brands": BRANDS, "judge_stance": True}
    run_tracker(conn, [NoJudge("mock")], cfg, [])
    assert [tuple(r) for r in conn.execute("SELECT brand, stance FROM geo_mentions")] == [("Databricks", None)]


def test_web_search_sources_are_stored_and_ranked(conn):
    cfg = {"templates": [], "general": ["q1", "q2", "q3"], "brands": BRANDS}
    run_tracker(conn, [LLMClient("mock")], cfg, [], web_search=True)
    report = visibility_report(conn, None)
    assert report["batch"]["web_search"] == 1 and report["batch"]["grounded"] == 3
    assert report["top_sources"] and all(0 < r["pct"] <= 100 for r in report["top_sources"])
    assert all("www." not in r["domain"] for r in report["top_sources"])


def test_no_sources_section_without_web_search(conn):
    run_tracker(conn, [LLMClient("mock")], {"templates": [], "general": ["q"], "brands": BRANDS}, [])
    assert visibility_report(conn, None)["top_sources"] == []


def test_source_domain_handles_gemini_redirects():
    assert source_domain("https://www.g2.com/categories/x") == "g2.com"
    assert source_domain("https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc", "G2.com") == "g2.com"


def test_case_sensitive_brand_ignores_ordinary_word():
    brands = {"Snowflake": {"aliases": ["Snowflake"], "case_sensitive": True}}
    assert find_mentions("Every snowflake is unique.", brands) == []
    assert find_mentions("Snowflake Cortex is popular.", brands) == [("Snowflake", 1)]


def test_shipped_config_ignores_ordinary_words():
    text = "Work the clay slowly. A profound change, as big as the apollo program."
    assert find_mentions(text, load_config()["brands"]) == []
    assert [b for b, _ in find_mentions("Clay, then Apollo.io and Profound.", load_config()["brands"])] == [
        "Clay", "Apollo.io", "Profound"]


def test_ignored_phrases_are_not_the_brand():
    brands = load_config()["brands"]
    assert find_mentions("Make sure the data is clean. Make your list.", brands) == []
    assert find_mentions("Make sure to compare Zapier or Make for this.", brands) == [("Zapier", 1), ("Make", 2)]


def test_shipped_prompts_read_correctly_for_every_industry():
    from gtm_pilot.leads.enrich import INDUSTRIES

    for _, prompt in build_prompts(load_config(), INDUSTRIES):
        assert not re.search(r"\ba [aeiou]", prompt, re.IGNORECASE), prompt
