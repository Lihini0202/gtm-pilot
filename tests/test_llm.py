import pytest
import requests

from gtm_pilot.core.llm import LLMClient, LLMError, parse_json_object


class FakeResponse:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data or {}, headers or {}

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, headers, json, timeout):
        self.calls.append({"url": url, "headers": headers, "json": json})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr("gtm_pilot.core.llm.time.sleep", waits.append)
    return waits


def test_rate_limit_waits_as_long_as_gemini_asks(no_sleep):
    limited = FakeResponse(429, {"error": {"details": [{"@type": "RetryInfo", "retryDelay": "17s"}]}})
    s = FakeSession([limited, FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})])
    assert LLMClient("gemini", api_key="k", session=s).complete("hi").text == "ok"
    assert no_sleep == [17.0]


def test_retry_after_header_is_honoured_but_capped(no_sleep):
    s = FakeSession([FakeResponse(429, headers={"Retry-After": "5"}), FakeResponse(503, headers={"Retry-After": "900"}),
                     FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})])
    LLMClient("openai", api_key="k", session=s).complete("hi")
    assert no_sleep == [5.0, 60.0]


def test_parse_json_inside_code_fence():
    assert parse_json_object('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_fails_cleanly():
    with pytest.raises(LLMError):
        parse_json_object("no json here")


def test_anthropic_request_and_parse():
    s = FakeSession([FakeResponse(200, {"content": [{"type": "text", "text": "hello"}]})])
    c = LLMClient("anthropic", api_key="test-key", session=s)
    assert c.complete("hi", system="be nice").text == "hello"
    call = s.calls[0]
    assert call["url"].endswith("/v1/messages")
    assert call["json"]["system"] == "be nice"


def test_retries_on_rate_limit_then_succeeds():
    s = FakeSession([FakeResponse(429), FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})])
    c = LLMClient("openai", api_key="test-key", session=s)
    assert c.complete("hi").text == "ok"
    assert len(s.calls) == 2


def test_no_retry_on_auth_error():
    s = FakeSession([FakeResponse(401), FakeResponse(200)])
    with pytest.raises(LLMError, match="401"):
        LLMClient("openai", api_key="bad", session=s).complete("hi")
    assert len(s.calls) == 1


def test_network_errors_retry_then_fail():
    s = FakeSession([requests.ConnectionError()] * 3)
    with pytest.raises(LLMError, match="network"):
        LLMClient("anthropic", api_key="k", session=s).complete("hi")
    assert len(s.calls) == 3


def test_gemini_key_not_in_url():
    s = FakeSession([FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "x"}]}}]})])
    LLMClient("gemini", api_key="secret-key", session=s).complete("hi")
    assert "secret-key" not in s.calls[0]["url"]


def test_key_never_in_repr():
    assert "secret" not in repr(LLMClient("openai", api_key="secret"))


def test_live_provider_requires_key():
    with pytest.raises(ValueError):
        LLMClient("anthropic")


GROUNDED = {"candidates": [{
    "content": {"parts": [{"text": "Try Clay."}]},
    "groundingMetadata": {"groundingChunks": [
        {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/a", "title": "g2.com"}},
        {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/a", "title": "g2.com"}},
        {"retrievedContext": {"uri": "gs://not-web"}},
    ]},
}]}


def test_gemini_web_search_sends_tool_and_returns_sources():
    s = FakeSession([FakeResponse(200, GROUNDED)])
    r = LLMClient("gemini", api_key="k", session=s).complete("best tools?", web_search=True)
    assert s.calls[0]["json"]["tools"] == [{"google_search": {}}]
    assert r.grounded and r.text == "Try Clay."
    assert r.sources == [{"url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/a", "title": "g2.com"}]


def test_no_tools_sent_without_web_search():
    s = FakeSession([FakeResponse(200, GROUNDED)])
    r = LLMClient("gemini", api_key="k", session=s).complete("hi")
    assert "tools" not in s.calls[0]["json"] and not r.grounded and r.sources == []


def test_unsupported_provider_answers_without_web_search():
    s = FakeSession([FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})])
    r = LLMClient("openai", api_key="k", session=s).complete("hi", web_search=True)
    assert "tools" not in s.calls[0]["json"] and r.grounded is False
