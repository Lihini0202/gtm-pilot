"""Application settings from environment variables and an optional .env file.

API credentials are read from the environment only and are never logged or persisted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=value pairs from a .env file into os.environ.

    Variables already set in the environment take precedence.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    # Tracked brand (GTM_BRAND). None disables brand-specific reporting.
    brand: str | None = None
    db_path: str = "data/gtm_pilot.db"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    hubspot_token: str | None = None
    # "mock": offline provider; "live": providers with configured API keys.
    mode: str = "mock"
    providers: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        s = cls(
            brand=os.getenv("GTM_BRAND", "").strip() or None,
            db_path=os.getenv("GTM_DB_PATH", "data/gtm_pilot.db"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            hubspot_token=os.getenv("HUBSPOT_TOKEN") or None,
        )
        requested = os.getenv("GTM_MODE", "auto").lower()
        live = [
            name
            for name, key in (
                ("anthropic", s.anthropic_api_key),
                ("openai", s.openai_api_key),
                ("gemini", s.gemini_api_key),
            )
            if key
        ]
        if requested == "mock" or not live:
            s.mode, s.providers = "mock", ["mock"]
        else:
            s.mode, s.providers = "live", live
        return s
