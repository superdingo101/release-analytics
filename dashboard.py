"""Streamlit dashboard for release download analytics."""
from __future__ import annotations

from datetime import timedelta
import os

import pandas as pd
import plotly.express as px
import streamlit as st
from config import hidden_release_tags, load_env

from db import DB_PATH, connect, current_adoption_percentage, downloads_added_between_snapshots, fetch_rows, init_db, milestone_download_totals, snapshots_for_repo, total_downloads_by_release

load_env()
DEFAULT_DB_PATH = os.getenv("DB_PATH", DB_PATH)
HIDDEN_RELEASE_TAGS = hidden_release_tags()

st.set_page_config(page_title="Release Download Analytics", layout="wide")
st.title("Release Download Analytics")


def query_param_first(name: str, default: str | None = None) -> str | None:
    values = st.query_params.get_all(name)
    return values[0] if values else default


def set_query_param(name: str, value: str | list[str] | None) -> None:
    if value is None or value == []:
        st.query_params.pop(name, None)
    else:
        st.query_params[name] = value


def bool_query_param(name: str, default: bool) -> bool:
    value = query_param_first(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}

@st.cache_data(ttl=60)
def repos(db_path: str) -> list[str]:
    with connect(db_path) as conn:
        init_db(conn)
        return [row["full_name"] for row in fetch_rows(conn, "SELECT full_name FROM repos ORDER BY full_name")]

db_path = st.sidebar.text_input("SQLite database", DEFAULT_DB_PATH)
repo_names = repos(db_path)
if not repo_names:
    st.info("No data yet. Run collector.py or import_history.py first.")
    st.stop()

repo_param = query_param_first("repo")
repo_index = repo_names.index(repo_param) if repo_param in repo_names else 0
repo = st.sidebar.selectbox("Repository", repo_names, index=repo_index)
set_query_param("repo", repo)
include_prereleases = st.sidebar.toggle("Include prereleases", value=bool_query_param("include_prereleases", True))
set_query_param("include_prereleases", "1" if include_prereleases else "0")
with connect(db_path) as conn:
    asset_names = [r["name"] for r in fetch_rows(conn, """
        SELECT DISTINCT a.name FROM assets a
        JOIN releases r ON r.id = a.release_id
        JOIN repos rp ON rp.id = r.repo_id
        WHERE rp.full_name = ? ORDER BY a.name
    """, [repo])]

asset_param = query_param_first("asset")
default_index = asset_names.index("skylight-calendar-card.js") if "skylight-calendar-card.js" in asset_names else 0
asset_index = asset_names.index(asset_param) if asset_param in asset_names else default_index
asset_name = st.sidebar.selectbox("Asset", asset_names, index=asset_index)
set_query_param("asset", asset_name)
with connect(db_path) as conn:
    rows = snapshots_for_repo(conn, repo, include_prereleases, asset_name)
if HIDDEN_RELEASE_TAGS:
    rows = [row for row in rows if row["tag"] not in HIDDEN_RELEASE_TAGS]
if not rows:
    st.warning("No snapshots match the selected filters after excluding hidden releases.")
    st.stop()

df = pd.DataFrame(downloads_added_between_snapshots(rows))
df["collected_at"] = pd.to_datetime(df["collected_at"], utc=True)
df["published_at"] = pd.to_datetime(df["published_at"], utc=True)
min_date, max_date = df["collected_at"].min().date(), df["collected_at"].max().date()
start_param = pd.to_datetime(query_param_first("start"), errors="coerce")
end_param = pd.to_datetime(query_param_first("end"), errors="coerce")
default_start = start_param.date() if not pd.isna(start_param) and min_date <= start_param.date() <= max_date else min_date
default_end = end_param.date() if not pd.isna(end_param) and min_date <= end_param.date() <= max_date else max_date
if default_start > default_end:
    default_start, default_end = min_date, max_date
start, end = st.sidebar.date_input("Snapshot date range", value=(default_start, default_end), min_value=min_date, max_value=max_date)
set_query_param("start", start.isoformat())
set_query_param("end", end.isoformat())
df = df[(df["collected_at"].dt.date >= start) & (df["collected_at"].dt.date <= end)]

with connect(db_path) as conn:
    totals = [
        total
        for total in total_downloads_by_release(conn, repo, include_prereleases, asset_name)
        if total["tag"] not in HIDDEN_RELEASE_TAGS
    ]
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

available_releases = (
    df[["tag", "published_at"]]
    .drop_duplicates()
    .sort_values(["published_at", "tag"], ascending=[False, True])["tag"]
    .tolist()
)
selected_release_params = st.query_params.get_all("release")
default_releases = [tag for tag in selected_release_params if tag in available_releases] or available_releases
selected_releases = st.multiselect(
    "Releases shown on other charts and tables",
    available_releases,
    default=default_releases,
    help=(
        "Select releases for the total downloads, daily downloads, and milestone sections. "
        "Releases configured in EXCLUDED_RELEASE_TAGS or HIDDEN_RELEASES are omitted from "
        "dashboard metrics, charts, tables, and this selector. "
        "The cumulative release-age chart always includes all non-hidden releases from the repo, asset, "
        "prerelease, and date filters so its Plotly legend can show/hide releases interactively "
        "without rerunning the page. These choices are saved in the browser URL and survive refreshes."
    ),
)
set_query_param("release", selected_releases)
chart_df = df[df["tag"].isin(selected_releases)]
chart_totals = [total for total in totals if total["tag"] in selected_releases]

st.subheader("Total downloads by release")
st.bar_chart(pd.DataFrame(chart_totals).set_index("tag")["downloads"] if chart_totals else pd.Series(dtype=int))

st.subheader("Cumulative downloads by release age")
age_df = df.sort_values("release_age_hours")[["tag", "published_at", "collected_at", "release_age_days", "download_count"]]
release_order = (
    age_df[["tag", "published_at"]]
    .drop_duplicates()
    .sort_values(["published_at", "tag"])
    .reset_index(drop=True)
)
release_order["superseded_at"] = release_order["published_at"].shift(-1)
age_df = age_df.merge(release_order[["tag", "superseded_at"]], on="tag", how="left")

limit_to_before_superseded = st.toggle(
    "Only show releases until superseded",
    value=bool_query_param("limit_to_before_superseded", False),
    help="When enabled, each release line stops at the next release's publication time.",
)
set_query_param("limit_to_before_superseded", "1" if limit_to_before_superseded else "0")
superseded_age_df = age_df[
    age_df["superseded_at"].isna() | (age_df["collected_at"] < age_df["superseded_at"])
]
chart_age_df = superseded_age_df if limit_to_before_superseded else age_df
max_release_age_days = float(chart_age_df["release_age_days"].max()) if not chart_age_df.empty else 0.0

age_limit_param = pd.to_numeric(query_param_first("release_age_days_limit"), errors="coerce")
if "release_age_days_limit" not in st.session_state:
    st.session_state.release_age_days_limit = float(age_limit_param) if not pd.isna(age_limit_param) else max_release_age_days
if "limit_to_before_superseded_previous" not in st.session_state:
    st.session_state.limit_to_before_superseded_previous = limit_to_before_superseded
if st.session_state.limit_to_before_superseded_previous != limit_to_before_superseded:
    st.session_state.release_age_days_limit = max_release_age_days
    st.session_state.limit_to_before_superseded_previous = limit_to_before_superseded

if st.session_state.release_age_days_limit > max_release_age_days:
    st.session_state.release_age_days_limit = max_release_age_days


def reset_release_age_days_limit(max_days: float) -> None:
    st.session_state.release_age_days_limit = max_days


limit_col, reset_col = st.columns([3, 1])
with limit_col:
    release_age_days_limit = st.number_input(
        "Show release age through day",
        min_value=0.0,
        max_value=max_release_age_days,
        value=st.session_state.release_age_days_limit,
        step=1.0,
        format="%.2f",
        key="release_age_days_limit",
        help="Limits the chart view without changing the stored release_age_days values.",
    )
with reset_col:
    st.write("")
    st.write("")
    st.button(
        "Reset view",
        use_container_width=True,
        on_click=reset_release_age_days_limit,
        args=(max_release_age_days,),
    )

set_query_param("release_age_days_limit", f"{release_age_days_limit:.2f}")
filtered_age_df = chart_age_df[chart_age_df["release_age_days"] <= release_age_days_limit]
visible_release_tags = set(filtered_age_df["tag"])
active_release_tags = [tag for tag in release_order["tag"] if tag in visible_release_tags]
age_chart = px.line(
    filtered_age_df,
    x="release_age_days",
    y="download_count",
    color="tag",
    category_orders={"tag": active_release_tags},
    labels={
        "release_age_days": "Release age (days)",
        "download_count": "Downloads",
        "tag": "Release",
    },
    hover_data={
        "release_age_days": ":.2f",
        "download_count": ":,",
    },
)
age_chart.update_layout(
    hovermode="x unified",
    legend_title_text="Release",
)
st.plotly_chart(
    age_chart,
    use_container_width=True,
    config={"displaylogo": False, "responsive": True},
)

st.subheader("Daily downloads by version")
daily = chart_df.copy()
daily["day"] = daily["collected_at"].dt.date
daily = daily.groupby(["day", "tag"], as_index=False)["downloads_added"].sum()
st.bar_chart(daily, x="day", y="downloads_added", color="tag")

st.subheader("Release comparison milestones")
milestone_rows = [row for row in rows if row["tag"] in selected_releases]
st.dataframe(pd.DataFrame(milestone_download_totals(milestone_rows)), use_container_width=True)

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
