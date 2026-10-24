"""HubSpot export tests against an in-memory fake API."""

import csv

import pytest

from gtm_pilot.leads import export


class FakeResponse:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data or {}, headers or {}

    def json(self):
        return self._data


class FakeHubSpot:
    """In-memory stand-in for the HubSpot properties, batch read and batch upsert endpoints."""

    def __init__(self, contacts=None, properties=tuple(export.CUSTOM_PROPERTIES)):
        self.contacts = {e: dict(p, email=e) for e, p in (contacts or {}).items()}
        self.properties = set(properties)
        self.upserts, self.calls = [], []
        self.fail_next: list[int] = []  # status codes returned for the next requests

    def request(self, method, url, headers, json, timeout):
        path = url.removeprefix(export.API)
        self.calls.append((method, path))
        assert headers["Authorization"] == "Bearer tok"
        if self.fail_next:
            return FakeResponse(self.fail_next.pop(0), {"message": "boom"})
        if method == "GET" and path.startswith("/crm/v3/properties/contacts/"):
            return FakeResponse(200 if path.rsplit("/", 1)[1] in self.properties else 404)
        if path == "/crm/v3/properties/contacts":
            self.properties.add(json["name"])
            return FakeResponse(201)
        if path.endswith("/batch/read"):
            found = [{"properties": self.contacts[i["id"]]} for i in json["inputs"] if i["id"] in self.contacts]
            return FakeResponse(207 if len(found) < len(json["inputs"]) else 200, {"results": found})
        if path.endswith("/batch/upsert"):
            self.upserts.append(json["inputs"])
            for item in json["inputs"]:
                self.contacts.setdefault(item["id"], {"email": item["id"]}).update(item["properties"])
            return FakeResponse(200)
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("gtm_pilot.leads.export.time.sleep", lambda s: None)


def lead(email, **kw):
    base = {"email": email, "first_name": "Ann", "last_name": "Lee", "company": "Big Bank", "job_title": "CTO",
            "country": "Sri Lanka", "event": "AI Summit", "seniority": "C-Level", "industry": "Banking",
            "is_decision_maker": True, "email_type": "work", "consent": "yes"}
    return {**base, **kw}


def test_properties_skip_blanks_and_use_hubspot_booleans():
    props = export.hubspot_properties(lead("a@x.com", country="", is_decision_maker=False))
    assert "country" not in props and "email" not in props
    assert props["gtm_decision_maker"] == "false" and props["gtm_event"] == "AI Summit"


def test_failed_enrichment_leaves_existing_tags_alone():
    props = export.hubspot_properties(lead("a@x.com", seniority="", industry="", is_decision_maker=False))
    assert not {"gtm_seniority", "gtm_industry", "gtm_decision_maker"} & props.keys()


def test_csv_uses_lowercase_booleans(tmp_path):
    path = export.write_hubspot_csv([lead("a@x.com")], tmp_path / "hs.csv")
    row = next(csv.DictReader(path.open(newline="")))
    assert row["gtm_decision_maker"] == "true" and row["email"] == "a@x.com"


def test_declined_consent_is_kept_out():
    leads = [lead("a@x.com"), lead("b@x.com", consent="no"), lead("c@x.com", consent="")]
    assert [x["email"] for x in export.consented(leads)] == ["a@x.com", "c@x.com"]


def test_plan_shows_new_changed_and_unchanged():
    hs = FakeHubSpot(contacts={
        "same@x.com": export.hubspot_properties(lead("same@x.com")),
        "old@x.com": {**export.hubspot_properties(lead("old@x.com")), "company": "Old Bank", "jobtitle": "Keep me"},
    })
    plan = export.plan_upload([lead("new@x.com"), lead("same@x.com"), lead("old@x.com", job_title="")], "tok", hs)
    by_email = {p["email"]: p for p in plan}
    assert by_email["new@x.com"]["action"] == "new"
    assert by_email["same@x.com"]["action"] == "unchanged"
    # empty job title is not sent; only company differs
    assert by_email["old@x.com"]["changes"] == {"company": ("Old Bank", "Big Bank")}
    assert hs.upserts == []  # planning is read-only


def test_upload_sends_only_new_contacts_and_changed_fields():
    hs = FakeHubSpot(contacts={"same@x.com": export.hubspot_properties(lead("same@x.com")),
                               "old@x.com": {"company": "Old Bank"}})
    result = export.upload_to_hubspot([lead("new@x.com"), lead("same@x.com"), lead("old@x.com")], "tok", session=hs)
    assert (result.created, result.updated, result.unchanged, result.failed) == (1, 1, 1, [])
    sent = {i["id"]: i["properties"] for i in hs.upserts[0]}
    assert set(sent) == {"new@x.com", "old@x.com"}
    assert sent["old@x.com"]["company"] == "Big Bank" and "gtm_event" in sent["old@x.com"]


def test_upload_stops_before_writing_if_properties_are_missing():
    hs = FakeHubSpot(properties={"gtm_seniority", "gtm_industry", "gtm_decision_maker"})
    with pytest.raises(export.HubSpotError, match="gtm_event, gtm_email_type"):
        export.upload_to_hubspot([lead("a@x.com")], "tok", session=hs)
    assert hs.upserts == []


def test_setup_creates_only_missing_properties():
    hs = FakeHubSpot(properties={"gtm_seniority"})
    created = export.setup_properties("tok", hs)
    assert created == ["gtm_industry", "gtm_decision_maker", "gtm_event", "gtm_email_type"]
    assert export.setup_properties("tok", hs) == []


def test_rate_limit_is_retried():
    hs = FakeHubSpot()
    hs.fail_next = [429]
    assert export.missing_properties("tok", hs) == []
    assert hs.calls[0] == hs.calls[1]  # retried with the same request


def test_failed_batch_does_not_stop_the_rest(monkeypatch):
    hs = FakeHubSpot()
    real = hs.request

    def flaky(method, url, headers, json, timeout):
        if url.endswith("/batch/upsert") and json["inputs"][0]["id"] == "a0@x.com":
            return FakeResponse(400, {"message": "Property values were not valid"})
        return real(method, url, headers, json, timeout)

    monkeypatch.setattr(hs, "request", flaky)
    leads = [lead(f"a{i}@x.com") for i in range(3)]
    result = export.upload_to_hubspot(leads, "tok", batch_size=2, session=hs)
    assert result.failed == ["a0@x.com", "a1@x.com"] and result.created == 1


def test_auth_error_fails_fast_with_hubspot_message():
    hs = FakeHubSpot()
    hs.fail_next = [401]
    with pytest.raises(export.HubSpotError, match="401: boom"):
        export.missing_properties("tok", hs)
    assert len(hs.calls) == 1


def test_reimport_updates_fields_but_never_blanks_them(conn):
    export.save_to_db(conn, [lead("a@x.com", company="Old Bank")])
    export.save_to_db(conn, [lead("a@x.com", first_name="Anne", company="New Bank", country="")])
    row = conn.execute("SELECT first_name, company, country FROM leads").fetchone()
    assert tuple(row) == ("Anne", "New Bank", "Sri Lanka")


def test_reimport_with_failed_tags_keeps_old_tags(conn):
    export.save_to_db(conn, [lead("a@x.com")])
    export.save_to_db(conn, [lead("a@x.com", seniority="", industry="", is_decision_maker=False)])
    row = conn.execute("SELECT seniority, industry, is_decision_maker FROM leads").fetchone()
    assert tuple(row) == ("C-Level", "Banking", 1)
