"""Command-line interface.

Examples:
    gtm-pilot demo-data                 # generate sample lead data
    gtm-pilot leads data/raw_leads.csv  # clean, enrich and export leads
    gtm-pilot geo                       # run the AI visibility tracker
    gtm-pilot hubspot --dry-run         # preview HubSpot changes for stored leads
    gtm-pilot schema page.html          # audit structured data on a page
    gtm-pilot run data/raw_leads.csv    # leads pipeline, then the tracker
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from gtm_pilot.core import db
from gtm_pilot.core.config import Settings
from gtm_pilot.core.llm import make_clients
from gtm_pilot.core.logging_setup import setup_logging
from gtm_pilot.geo import schema_check, track
from gtm_pilot.leads import clean, enrich, export, generate

log = logging.getLogger("gtm_pilot")


def cmd_demo_data(args, settings) -> None:
    path = generate.write_csv(generate.generate_leads(args.n), args.out)
    print(f"Wrote {args.n}+ messy sample leads to {path}")


def cmd_leads(args, settings) -> None:
    raw = clean.read_csv(args.csv)
    leads, report = clean.clean_rows(raw)
    print(report.summary())

    conn = db.connect(settings.db_path)
    client = make_clients(settings)[0]  # enrichment uses the first configured provider
    leads = enrich.enrich_leads(leads, client, conn)

    export.save_to_db(conn, leads)
    crm_leads = export.consented(leads)
    out = export.write_hubspot_csv(crm_leads, args.out)
    if report.rejected:
        rej = Path(args.out).with_name("rejected_leads.csv")
        generate.write_csv(report.rejected, rej)
        print(f"Rejected rows saved to {rej} for a human to check")

    dms = sum(1 for lead in leads if lead.get("is_decision_maker"))
    print(f"{len(leads)} leads saved ({dms} decision makers). HubSpot import file: {out}")
    if len(crm_leads) < len(leads):
        declined = sum(1 for lead in leads if lead.get("consent") == "no")
        print(f"{len(leads) - len(crm_leads)} leads are kept out of HubSpot without marketing consent "
              f"({declined} declined, {len(leads) - len(crm_leads) - declined} gave no answer)")

    if args.upload or args.dry_run:
        sync_hubspot(crm_leads, settings, args.dry_run, Path(args.out).with_name("hubspot_dry_run.csv"))


def sync_hubspot(leads: list[dict], settings, dry_run: bool, plan_path: Path) -> None:
    if not settings.hubspot_token:
        sys.exit("Set HUBSPOT_TOKEN in .env to use HubSpot")
    try:
        if dry_run:
            plan = export.plan_upload(leads, settings.hubspot_token)
            counts = {a: sum(1 for p in plan if p["action"] == a) for a in ("new", "update", "unchanged")}
            changed = sorted({k for p in plan for k in p["changes"]})
            print(f"Dry run, nothing written: {counts['new']} new, {counts['update']} would update, "
                  f"{counts['unchanged']} unchanged")
            if changed:
                print(f"Fields that would change: {', '.join(changed)}")
            missing = export.missing_properties(settings.hubspot_token)
            if missing:
                print(f"Upload would stop: HubSpot is missing {', '.join(missing)} (run `gtm-pilot hubspot --setup`)")
            print(f"Every change, field by field: {export.write_plan_csv(plan, plan_path)}")
            return
        result = export.upload_to_hubspot(leads, settings.hubspot_token)
    except export.HubSpotError as e:
        sys.exit(str(e))
    print(f"HubSpot: {result.created} created, {result.updated} updated, {result.unchanged} unchanged")
    if result.failed:
        failed = plan_path.with_name("hubspot_failed.csv")
        generate.write_csv([{"email": e} for e in result.failed], failed)
        sys.exit(f"{len(result.failed)} contacts failed to upload; listed in {failed}. Run it again to retry them.")


def cmd_hubspot(args, settings) -> None:
    if not settings.hubspot_token:
        sys.exit("Set HUBSPOT_TOKEN in .env to use HubSpot")
    if args.setup:
        try:
            created = export.setup_properties(settings.hubspot_token)
        except export.HubSpotError as e:
            sys.exit(f"{e} (the key needs the crm.schemas.contacts.write scope to create properties)")
        print(f"Created: {', '.join(created)}" if created else "All gtm_* properties already exist")
        return
    leads = export.consented(export.load_leads(db.connect(settings.db_path)))
    if not leads:
        sys.exit("No saved leads yet. Run `gtm-pilot leads <csv>` first.")
    sync_hubspot(leads, settings, args.dry_run, Path(args.out).with_name("hubspot_dry_run.csv"))


def cmd_geo(args, settings) -> None:
    conn = db.connect(settings.db_path)
    config = track.load_config(args.config) if args.config else track.load_config()
    industries = args.industry or track.top_lead_industries(conn)
    if not industries:
        print("No lead industries found yet, checking general prompts only")
    else:
        print(f"Checking AI visibility for lead industries: {', '.join(industries)}")
    if not settings.brand:
        print("GTM_BRAND is not set, so no brand is highlighted. Set it in .env to track your own brand.")
    track.run_tracker(conn, make_clients(settings), config, industries, settings.brand,
                      samples=getattr(args, "samples", None), web_search=getattr(args, "web_search", None))
    report = track.visibility_report(conn, settings.brand)
    print_report(report)


def cmd_schema(args, settings) -> None:
    html = Path(args.html).read_text(encoding="utf-8", errors="replace")
    print(json.dumps(schema_check.check_html(html, args.kind), indent=2))


def cmd_run(args, settings) -> None:
    cmd_leads(args, settings)
    args.industry, args.config = None, None
    cmd_geo(args, settings)


def print_report(report: dict) -> None:
    brand, batch = report["brand"], report["batch"]
    if not batch:
        print("\nNo AI answers saved yet.")
        return
    search = f"web search on ({batch['grounded']} grounded)" if batch["web_search"] else "web search off"
    print(f"\nAI visibility, batch {batch['id']} ({batch['started_at']} UTC, {batch['providers']}): "
          f"{report['total_answers']} answers, {batch['samples']} sample(s) per question, {search}")
    if "mock" in batch["providers"].split(","):
        print("  (mock mode: these answers are fake, for trying the tool out)")
    for row in report["share_of_voice"]:
        marker = "  <-- us" if row["brand"] == brand else ""
        rec = f", recommended in {row['recommended_pct']:>5}%" if row["recommended_pct"] is not None else ""
        print(f"  {row['brand']:<24} in {row['pct']:>5}% of answers{rec}, avg position {row['avg_position']}{marker}")
    if report["top_sources"]:
        print("\nWebsites the AI cited most:")
        for row in report["top_sources"][:8]:
            print(f"  {row['domain']:<32} in {row['pct']:>5}% of searched answers")
    if not brand:
        return
    print(f"\n{brand} by industry:")
    for row in report["by_industry"]:
        rec = f", recommended in {row['recommended']}" if report["judged"] else ""
        print(f"  {row['industry']:<12} mentioned in {row['mentioned']}/{row['answers']} answers{rec}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="gtm-pilot", description="AI tools for go-to-market teams")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo-data", help="generate a messy sample lead CSV")
    d.add_argument("--n", type=int, default=200)
    d.add_argument("--out", default="data/raw_leads.csv")
    d.set_defaults(func=cmd_demo_data)

    for name, fn, helptext in (("leads", cmd_leads, "clean, enrich and export leads"),
                               ("run", cmd_run, "leads pipeline, then GEO tracker")):
        lp = sub.add_parser(name, help=helptext)
        lp.add_argument("csv")
        lp.add_argument("--out", default="data/hubspot_import.csv")
        lp.add_argument("--upload", action="store_true", help="also upsert to HubSpot")
        lp.add_argument("--dry-run", action="store_true", help="show what a HubSpot upload would change, write nothing")
        lp.set_defaults(func=fn)

    h = sub.add_parser("hubspot", help="upload saved leads to HubSpot")
    h.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    h.add_argument("--setup", action="store_true", help="create the gtm_* contact properties in HubSpot")
    h.add_argument("--out", default="data/hubspot_import.csv", help=argparse.SUPPRESS)
    h.set_defaults(func=cmd_hubspot)

    g = sub.add_parser("geo", help="track brand mentions in AI answers")
    g.add_argument("--industry", action="append", help="override industries (repeatable)")
    g.add_argument("--config", help="custom prompts.yaml")
    g.add_argument("--samples", type=int, help="times to ask each question (default: from prompts.yaml)")
    g.add_argument("--web-search", action=argparse.BooleanOptionalAction, default=None,
                   help="search the web before answering and record cited sites (Gemini only)")
    g.set_defaults(func=cmd_geo)

    s = sub.add_parser("schema", help="check a page's structured data")
    s.add_argument("html")
    s.add_argument("--kind", choices=["home", "article"], default="home")
    s.set_defaults(func=cmd_schema)

    args = p.parse_args(argv)
    setup_logging(args.verbose)
    settings = Settings.from_env()
    log.info("Mode: %s (providers: %s)", settings.mode, ", ".join(settings.providers))
    args.func(args, settings)


if __name__ == "__main__":
    main()
