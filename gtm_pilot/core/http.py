"""Retry policy shared by outbound HTTP clients."""

from __future__ import annotations

RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}
MAX_RETRY_WAIT = 60.0  # seconds


def server_retry_wait(r) -> float | None:
    """Return the server-requested retry delay in seconds, or None.

    Reads the Retry-After header, then the retryDelay field in Gemini error
    details. The result is capped at MAX_RETRY_WAIT.
    """
    hint = (r.headers or {}).get("Retry-After")
    if not hint:
        try:
            details = r.json().get("error", {}).get("details", [])
        except (ValueError, AttributeError):
            details = []
        hint = next((d["retryDelay"].rstrip("s") for d in details if isinstance(d, dict) and "retryDelay" in d), None)
    try:
        return min(float(hint), MAX_RETRY_WAIT) if hint else None
    except ValueError:
        return None  # HTTP-date form is not supported; the caller falls back to backoff
