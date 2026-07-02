"""Streamlit dashboard for release download analytics."""
from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

from db import DB_PATH, connect, current_adoption_percentage, downloads_added_between_snapshots, fetch_rows, init_db, milestone_download_totals, snapshots_for_repo, total_downloads_by_release

st.set_page_config(page_title="Release Download Analytics", layout="wide")
st.title("Release Download Analytics")

@st.cache_data(ttl=60)
def repos(db_path: str) -> list[str]:
    with connect(db_path) as conn:
        init_db(conn)
        return [row["full_name"] for row in fetch_rows(conn, "SELECT full_name FROM repos ORDER BY full_name")]

db_path = st.sidebar.text_input("SQLite database", DB_PATH)
repo_names = repos(db_path)
if not repo_names:
    st.info("No data yet. Run collector.py or import_history.py first.")
    st.stop()

repo = st.sidebar.selectbox("Repository", repo_names)
include_prereleases = st.sidebar.toggle("Include prereleases", value=True)
with connect(db_path) as conn:
    asset_names = [r["name"] for r in fetch_rows(conn, """
        SELECT DISTINCT a.name FROM assets a
        JOIN releases r ON r.id = a.release_id
        JOIN repos rp ON rp.id = r.repo_id
        WHERE rp.full_name = ? ORDER BY a.name
    """, [repo])]

default_index = asset_names.index("skylight-calendar-card.js") if "skylight-calendar-card.js" in asset_names else 0
asset_name = st.sidebar.selectbox("Asset", asset_names, index=default_index)
with connect(db_path) as conn:
    rows = snapshots_for_repo(conn, repo, include_prereleases, asset_name)
if not rows:
    st.warning("No snapshots match the selected filters.")
    st.stop()

df = pd.DataFrame(downloads_added_between_snapshots(rows))
df["collected_at"] = pd.to_datetime(df["collected_at"], utc=True)
df["published_at"] = pd.to_datetime(df["published_at"], utc=True)
min_date, max_date = df["collected_at"].min().date(), df["collected_at"].max().date()
start, end = st.sidebar.date_input("Snapshot date range", value=(min_date, max_date), min_value=min_date, max_value=max_date)
df = df[(df["collected_at"].dt.date >= start) & (df["collected_at"].dt.date <= end)]

with connect(db_path) as conn:
    totals = total_downloads_by_release(conn, repo, include_prereleases, asset_name)
latest = totals[0] if totals else {"downloads": 0, "tag": "n/a"}
previous = totals[1] if len(totals) > 1 else {"downloads": 0, "tag": "n/a"}
adoption = current_adoption_percentage(totals)
now = df["collected_at"].max()
last_24h = df[df["collected_at"] >= now - timedelta(hours=24)]["downloads_added"].fillna(0).sum()
last_7d = df[df["collected_at"] >= now - timedelta(days=7)]["downloads_added"].fillna(0).sum()

c1, c2, c3, c4 = st.columns(4)
c1.metric(f"Latest release ({latest['tag']})", f"{latest['downloads']:,}")
c2.metric(f"Previous release ({previous['tag']})", f"{previous['downloads']:,}")
c3.metric("Current adoption", "n/a" if adoption is None else f"{adoption:.1f}%")
c4.metric("Downloads 24h / 7d", f"{int(last_24h):,} / {int(last_7d):,}")

st.subheader("Total downloads by release")
st.bar_chart(pd.DataFrame(totals).set_index("tag")["downloads"] if totals else pd.Series(dtype=int))

st.subheader("Cumulative downloads by release age")
age_df = df.sort_values("release_age_hours")[["tag", "release_age_days", "download_count"]]
st.line_chart(age_df, x="release_age_days", y="download_count", color="tag")

st.subheader("Daily downloads by version")
daily = df.copy()
daily["day"] = daily["collected_at"].dt.date
daily = daily.groupby(["day", "tag"], as_index=False)["downloads_added"].sum()
st.bar_chart(daily, x="day", y="downloads_added", color="tag")

st.subheader("Release comparison milestones")
st.dataframe(pd.DataFrame(milestone_download_totals(rows)), use_container_width=True)

st.subheader("Events / annotations")
with connect(db_path) as conn:
    events = fetch_rows(conn, """
        SELECT e.occurred_at, r.tag, e.title, e.notes, e.url
        FROM events e LEFT JOIN releases r ON r.id = e.release_id
        LEFT JOIN repos rp ON rp.id = e.repo_id
        WHERE rp.full_name = ? OR e.repo_id IS NULL
        ORDER BY e.occurred_at DESC
    """, [repo])
st.dataframe(pd.DataFrame(events), use_container_width=True)
