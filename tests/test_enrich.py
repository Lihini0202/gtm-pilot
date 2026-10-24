from gtm_pilot.core.llm import LLMClient, LLMError
from gtm_pilot.leads.enrich import enrich_leads, validate


def test_validate_rejects_made_up_values():
    out = validate({"seniority": "Supreme Leader", "industry": "Space Mining", "is_decision_maker": "yes"})
    assert out == {"seniority": "Individual Contributor", "industry": "Other", "is_decision_maker": False}


def test_validate_infers_decision_maker_when_missing():
    assert validate({"seniority": "VP", "industry": "Banking"})["is_decision_maker"] is True


def test_mock_enrichment_tags_leads():
    leads = [
        {"email": "a@combank.lk", "job_title": "Chief Data Officer", "company": "Commercial Bank of Ceylon"},
        {"email": "b@asiri.com", "job_title": "Data Scientist", "company": "Asiri Health"},
    ]
    out = enrich_leads(leads, LLMClient("mock"))
    assert out[0]["seniority"] == "C-Level" and out[0]["industry"] == "Banking" and out[0]["is_decision_maker"]
    assert out[1]["industry"] == "Healthcare" and not out[1]["is_decision_maker"]


def test_same_title_and_company_only_calls_model_once():
    calls = []

    class Counting(LLMClient):
        def complete_json(self, prompt, system=None):
            calls.append(prompt)
            return {"seniority": "VP", "industry": "Retail", "is_decision_maker": True}

    leads = [{"email": f"p{i}@keells.com", "job_title": "VP Sales", "company": "Keells"} for i in range(5)]
    enrich_leads(leads, Counting("mock"))
    assert len(calls) == 1


def test_model_failure_does_not_crash_pipeline():
    class Broken(LLMClient):
        def complete_json(self, prompt, system=None):
            raise LLMError("boom")

    out = enrich_leads([{"email": "a@x.com", "job_title": "CTO", "company": "X"}], Broken("mock"))
    assert out[0]["industry"] == "" and out[0]["is_decision_maker"] is False


def test_invalid_seniority_discards_the_models_decision_maker_answer():
    out = validate({"seniority": "Supreme Leader", "industry": "Banking", "is_decision_maker": True})
    assert out == {"seniority": "Individual Contributor", "industry": "Banking", "is_decision_maker": False}


def test_saved_tags_are_reused_on_the_next_run(conn):
    calls = []

    class Counting(LLMClient):
        def complete_json(self, prompt, system=None):
            calls.append(prompt)
            return {"seniority": "VP", "industry": "Retail", "is_decision_maker": True}

    leads = [{"email": "a@keells.com", "job_title": "VP Sales", "company": "Keells"}]
    enrich_leads(leads, Counting("mock"), conn)
    out = enrich_leads(leads, Counting("mock"), conn)
    assert len(calls) == 1 and out[0]["seniority"] == "VP"


def test_saved_tags_are_per_model(conn):
    leads = [{"email": "a@keells.com", "job_title": "VP Sales", "company": "Keells"}]
    enrich_leads(leads, LLMClient("mock"), conn)
    assert conn.execute("SELECT model FROM enrich_cache").fetchone()[0] == "mock/mock-1"
    other = LLMClient("mock", model="mock-2")
    enrich_leads(leads, other, conn)
    assert conn.execute("SELECT COUNT(*) FROM enrich_cache").fetchone()[0] == 2


def test_failed_tags_are_not_saved(conn):
    class Broken(LLMClient):
        def complete_json(self, prompt, system=None):
            raise LLMError("boom")

    enrich_leads([{"email": "a@x.com", "job_title": "CTO", "company": "X"}], Broken("mock"), conn)
    assert conn.execute("SELECT COUNT(*) FROM enrich_cache").fetchone()[0] == 0
