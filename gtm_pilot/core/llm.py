"""Minimal multi-provider LLM client built on `requests`.

Providers:
    anthropic: Messages API
    openai: Chat Completions API
    gemini: generateContent API
    mock: deterministic offline responses for tests and demos
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field

import requests

from gtm_pilot.core.http import RETRYABLE_STATUS, server_retry_wait

log = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-5-mini",
    # Lite tier: available to new API keys and responsive under free-tier limits.
    "gemini": "gemini-flash-lite-latest",
    "mock": "mock-1",
}



class LLMError(RuntimeError):
    """Raised when a provider call fails after all retries."""


@dataclass
class LLMResponse:
    provider: str
    model: str
    text: str
    grounded: bool = False  # answer used web search grounding
    sources: list[dict] = field(default_factory=list)  # cited pages: [{"url": ..., "title": ...}]

# Providers with web search support (Gemini: Google Search grounding).
WEB_SEARCH_PROVIDERS = {"gemini", "mock"}


class LLMClient:
    def __init__(
        self,
        provider: str,
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        if provider not in DEFAULT_MODELS:
            raise ValueError(f"Unknown provider: {provider}")
        if provider != "mock" and not api_key:
            raise ValueError(f"{provider} needs an API key")
        self.provider = provider
        self.model = model or DEFAULT_MODELS[provider]
        self._key = api_key
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = session or requests.Session()

    def __repr__(self) -> str:  # excludes the API key
        return f"LLMClient(provider={self.provider!r}, model={self.model!r})"

    # Public API

    def complete(
        self, prompt: str, system: str | None = None, max_tokens: int = 800, web_search: bool = False
    ) -> LLMResponse:
        """Return a completion for `prompt`.

        With `web_search`, supported providers ground the answer in web search and
        return the cited sources. Other providers answer without search and the
        response has grounded=False.
        """
        if web_search and self.provider not in WEB_SEARCH_PROVIDERS:
            log.warning("Web search isn't supported for %s yet; answering without it", self.provider)
            web_search = False
        if self.provider == "mock":
            sources = mock_sources(prompt) if web_search else []
            return LLMResponse("mock", self.model, mock_answer(prompt, system), web_search, sources)

        url, headers, body = self._build_request(prompt, system, max_tokens)
        if web_search:
            body["tools"] = [{"google_search": {}}]
        data = self._post_with_retries(url, headers, body)
        sources = self._extract_sources(data) if web_search else []
        return LLMResponse(self.provider, self.model, self._extract_text(data), web_search, sources)

    def complete_json(self, prompt: str, system: str | None = None) -> dict:
        """Return the first JSON object in the reply, parsed.

        Surrounding prose and code fences are ignored.
        """
        sys_msg = (system or "") + "\nRespond with a single JSON object and nothing else."
        text = self.complete(prompt, sys_msg.strip()).text
        return parse_json_object(text)

    # Internals

    def _build_request(self, prompt: str, system: str | None, max_tokens: int):
        if self.provider == "anthropic":
            body = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                body["system"] = system
            headers = {
                "x-api-key": self._key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            return "https://api.anthropic.com/v1/messages", headers, body

        if self.provider == "openai":
            messages = ([{"role": "system", "content": system}] if system else []) + [
                {"role": "user", "content": prompt}
            ]
            body = {"model": self.model, "messages": messages, "max_completion_tokens": max_tokens}
            headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
            return "https://api.openai.com/v1/chat/completions", headers, body

        # Gemini: API key in a header, not the query string, to keep it out of URL logs.
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        headers = {"x-goog-api-key": self._key, "Content-Type": "application/json"}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        return url, headers, body

    def _post_with_retries(self, url: str, headers: dict, body: dict) -> dict:
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            wait = 2 ** (attempt - 1)
            try:
                r = self.session.post(url, headers=headers, json=body, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = f"network error: {type(e).__name__}"
            else:
                if r.status_code == 200:
                    return r.json()
                last_error = f"HTTP {r.status_code}"
                if r.status_code not in RETRYABLE_STATUS:
                    break  # non-retryable client error
                wait = server_retry_wait(r) or wait
            log.warning("%s call failed (%s), attempt %d/%d", self.provider, last_error, attempt, self.max_retries)
            if attempt < self.max_retries:
                time.sleep(wait)
        raise LLMError(f"{self.provider} failed: {last_error}")

    def _extract_text(self, data: dict) -> str:
        try:
            if self.provider == "anthropic":
                return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            if self.provider == "openai":
                return data["choices"][0]["message"]["content"] or ""
            return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Unexpected {self.provider} response shape") from e

    def _extract_sources(self, data: dict) -> list[dict]:
        """Return unique web citations from Gemini grounding metadata."""
        try:
            chunks = data["candidates"][0].get("groundingMetadata", {}).get("groundingChunks", [])
        except (KeyError, IndexError, TypeError, AttributeError):
            return []
        seen: dict[str, dict] = {}
        for chunk in chunks:
            web = chunk.get("web") if isinstance(chunk, dict) else None
            if isinstance(web, dict) and web.get("uri"):
                seen.setdefault(web["uri"], {"url": web["uri"], "title": web.get("title") or ""})
        return list(seen.values())


def parse_json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise LLMError("No JSON object in model output")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise LLMError("Model returned invalid JSON") from e


def make_clients(settings) -> list[LLMClient]:
    keys = {
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "gemini": settings.gemini_api_key,
        "mock": None,
    }
    return [LLMClient(p, keys[p]) for p in settings.providers]


# Mock provider: deterministic output derived from a hash of the prompt.

# Mirrors the brands in geo/prompts.yaml.
MOCK_BRANDS = [
    "Clay", "Apollo.io", "ZoomInfo", "Cognism", "Lusha", "Cvent", "Profound", "Otterly.AI", "Semrush",
]


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest(), 16)


MOCK_SOURCES = ["g2.com", "gartner.com", "forrester.com", "techradar.com", "reddit.com", "capterra.com"]


def mock_sources(prompt: str) -> list[dict]:
    seed = _seed(prompt)
    picks = dict.fromkeys(MOCK_SOURCES[(seed + i * 5) % len(MOCK_SOURCES)] for i in range(2 + seed % 3))
    return [{"url": f"https://www.{d}/mock-review", "title": d} for d in picks]


def _mock_stances(prompt: str) -> dict:
    line = next((ln for ln in prompt.splitlines() if ln.startswith("Brands:")), "Brands:")
    brands = [b.strip() for b in line.removeprefix("Brands:").split(",") if b.strip()]
    labels = ["recommended", "recommended", "mentioned", "recommended", "negative"]
    return {b: labels[_seed(b + prompt[:80]) % len(labels)] for b in brands}


def mock_answer(prompt: str, system: str | None = None) -> str:
    if system and "JSON" in system and "stance" in system.lower():
        return json.dumps(_mock_stances(prompt))
    if system and "JSON" in system and "job title" in prompt.lower():
        return json.dumps(_mock_classify(prompt))
    seed = _seed(prompt)
    count = 4 + seed % 3
    start = seed % len(MOCK_BRANDS)
    picks = [MOCK_BRANDS[(start + i * 2) % len(MOCK_BRANDS)] for i in range(count)]
    lines = [f"{i + 1}. {b} - a solid option for this use case." for i, b in enumerate(dict.fromkeys(picks))]
    return "Here are some platforms worth considering:\n" + "\n".join(lines)


_SENIORITY_RULES = [
    ("C-Level", r"\b(chief|ceo|cto|cio|cdo|cfo|coo|founder|president)\b"),
    ("VP", r"\b(vp|vice president|svp|evp)\b"),
    ("Director", r"\b(director|head of)\b"),
    ("Manager", r"\b(manager|lead|principal)\b"),
]
_INDUSTRY_RULES = [
    ("Banking", r"bank|capital|credit|financial|finance|lending"),
    ("Insurance", r"insur|assurance|underwrit|mutual|life\b"),
    ("Healthcare", r"health|hospital|medical|pharma|clinic|bio"),
    ("Telecom", r"telecom|mobile|wireless|network|dialog|tel\b"),
    ("Retail", r"retail|store|mart|shop|commerce|consumer"),
]


def _mock_classify(prompt: str) -> dict:
    low = prompt.lower()
    seniority = next((s for s, pat in _SENIORITY_RULES if re.search(pat, low)), "Individual Contributor")
    industry = next((i for i, pat in _INDUSTRY_RULES if re.search(pat, low)), "Other")
    return {"seniority": seniority, "industry": industry, "is_decision_maker": seniority in {"C-Level", "VP", "Director"}}
