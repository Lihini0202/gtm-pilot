"""Streamlit dashboard for lead and AI-visibility reporting.

Usage: streamlit run dashboard/app.py
"""

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gtm_pilot.core import db
from gtm_pilot.core.config import Settings
from gtm_pilot.geo.track import list_batches, visibility_report

settings = Settings.from_env()
conn = db.connect(settings.db_path)

st.set_page_config(page_title="GTM Pilot", layout="wide")
st.title("GTM Pilot")
brand = settings.brand
st.caption(f"Event leads and AI search visibility for {brand or 'no brand set'} · mode: {settings.mode}")
if not brand:
    st.warning("GTM_BRAND is not set. Add it to .env to see your own brand's visibility.")

leads = pd.read_sql("SELECT * FROM leads", conn)
batches = list_batches(conn)

if leads.empty and not batches:
    st.info("No data yet. Run `python -m gtm_pilot.cli demo-data` then `python -m gtm_pilot.cli run data/raw_leads.csv`.")
    st.stop()


def batch_label(b: dict) -> str:
    search = "web search" if b["web_search"] else "no web search"
    return f"{b['started_at']} UTC · {b['providers']} · {b['answers']} answers · {search}"


batch = None
if batches:
    batch = st.selectbox("AI visibility run", batches, format_func=batch_label,
                         help="Each tracker run is kept separately, so results are never mixed.")
report = visibility_report(conn, brand, batch["id"] if batch else None)

# Summary metrics
ours = next((r for r in report["share_of_voice"] if r["brand"] == brand), None)
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Clean leads", len(leads))
c2.metric("Decision makers", int(leads["is_decision_maker"].sum()) if not leads.empty else 0)
c3.metric("AI answers checked", report["total_answers"])
c4.metric(f"{brand or 'Your brand'} mentioned in", f"{ours['pct']}%" if ours else ("0%" if brand else "n/a"))
if report["judged"]:
    c5.metric(f"{brand or 'Your brand'} recommended in",
              f"{ours['recommended_pct']}%" if ours else ("0%" if brand else "n/a"))

# Leads
st.subheader("Leads by industry")
if not leads.empty:
    by_ind = (leads.groupby("industry")
                   .agg(leads=("email", "count"), decision_makers=("is_decision_maker", "sum"))
                   .sort_values("decision_makers", ascending=False))
    left, right = st.columns([2, 1])
    left.bar_chart(by_ind, stack=False)
    right.dataframe(by_ind, width="stretch")

# AI visibility
if batch:
    if "mock" in batch["providers"].split(","):
        st.warning("This run used mock mode: the AI answers are fake. Add an API key to .env for real results.")
    st.caption(f"Each question was asked {batch['samples']} time(s); percentages are over all answers.")

st.subheader("Which brands do AI assistants recommend?")
sov = pd.DataFrame(report["share_of_voice"])
if not sov.empty:
    chart_cols = ["pct", "recommended_pct"] if report["judged"] else ["pct"]
    left, right = st.columns([3, 2])
    # Rows arrive ordered by mention count; sort=False keeps that order.
    left.bar_chart(sov.set_index("brand")[chart_cols].rename(
        columns={"pct": "mentioned %", "recommended_pct": "recommended %"}),
        horizontal=True, sort=False, stack=False, height=520)
    table = sov[["brand", "pct", "recommended_pct", "negative", "avg_position"]] if report["judged"] \
        else sov[["brand", "pct", "avg_position"]]
    right.dataframe(table.rename(columns={"pct": "mentioned %", "recommended_pct": "recommended %",
                                          "negative": "negative", "avg_position": "avg rank"}),
                    hide_index=True, width="stretch", height=min(520, 35 * (len(table) + 1) + 3))

by_industry = pd.DataFrame(report["by_industry"])
if not by_industry.empty:
    st.subheader(f"{brand} visibility in our lead industries")
    by_industry["mention rate %"] = (100 * by_industry["mentioned"] / by_industry["answers"]).round(1)
    if not report["judged"]:
        by_industry = by_industry.drop(columns="recommended")
    st.dataframe(by_industry, hide_index=True, width="stretch")
    gaps = by_industry[(by_industry["mention rate %"] < 50) & (by_industry["industry"] != "General")]
    for _, row in gaps.iterrows():
        share = ("does not appear in any" if row["mentioned"] == 0
                 else f"appears in only {row['mention rate %']}% of")
        st.warning(f"Gap: {brand} {share} AI answers for {row['industry']}, "
                   "an industry our event leads come from.")

by_question = pd.DataFrame(report["by_question"])
if not by_question.empty:
    with st.expander(f"{brand} by question"):
        if not report["judged"]:
            by_question = by_question.drop(columns="recommended")
        st.dataframe(by_question, hide_index=True, width="stretch")

st.subheader("Websites the AI cites")
sources = pd.DataFrame(report["top_sources"])
if not sources.empty:
    st.caption("Getting your brand onto these sites is the most direct way to show up in AI answers.")
    st.dataframe(sources.rename(columns={"answers": "answers citing it", "pct": "% of searched answers"}),
                 hide_index=True, width="stretch")
else:
    st.info("No sources for this run. Sources are only recorded when web search is on "
            "(`web_search: true` in prompts.yaml, or `gtm-pilot geo --web-search`).")

if batch:
    with st.expander("Raw AI answers"):
        st.dataframe(pd.read_sql("""SELECT run_at, provider, model, industry, sample, grounded, prompt, answer
                                    FROM geo_runs WHERE batch_id = ? ORDER BY id DESC""", conn, params=(batch["id"],)),
                     hide_index=True, width="stretch")
