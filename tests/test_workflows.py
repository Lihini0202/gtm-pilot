"""Checks on the GitHub Actions workflows."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def test_weekly_geo_passes_the_tracked_brand():
    jobs = yaml.safe_load((WORKFLOWS / "weekly-geo.yml").read_text(encoding="utf-8"))["jobs"]
    tracker = next(s for s in jobs["geo"]["steps"] if s.get("name") == "Run tracker")
    assert tracker["env"]["GTM_BRAND"] == "${{ vars.GTM_BRAND }}"
