"""Management screens using Streamlit's existing widgets."""
from __future__ import annotations

import os
from contextlib import closing

import streamlit as st

from db import connect, init_db, fetch_rows, utc_now_iso
from import_history import import_history, parse_history
from management import (
    Settings, read_settings, save_settings, parse_repos, parse_tags,
    request_collection, collection_status, save_annotation, delete_annotation,
)


def render_collection_status(db_path: str, repo: str) -> None:
    with closing(connect(db_path)) as conn:
        settings = read_settings(conn)
        status = collection_status(conn, repo)
    st.sidebar.subheader("Collection status")
    tracked = repo in settings.repos
    if not tracked:
        st.sidebar.caption("Not currently tracked; historical data is retained.")
    else:
        state = "Collecting" if status.get("running") else "Queued" if status.get("pending") else "Idle"
        st.sidebar.caption(state)
    st.sidebar.caption("Last successful collection (UTC): " + (status.get("last_success") or "Not recorded"))
    st.sidebar.caption("Next scheduled collection (UTC): " + ((status.get("next_due") if tracked else None) or "Not scheduled"))
    if status.get("error"):
        st.sidebar.warning(status["error"])
    if st.sidebar.button("Collect now", disabled=not tracked or bool(status.get("pending") or status.get("running"))):
        with closing(connect(db_path)) as conn:
            request_collection(conn, repo)
        st.rerun()
    st.sidebar.button("Refresh collection status")


def render_settings(db_path: str) -> None:
    st.header("Settings")
    st.caption("Settings are saved in the persistent database. The running scheduler picks up changes automatically.")
    with closing(connect(db_path)) as conn:
        init_db(conn)
        settings = read_settings(conn)
        known = [row["full_name"] for row in fetch_rows(conn, "SELECT full_name FROM repos ORDER BY full_name")]
    with st.form("management_settings"):
        repos = st.text_area("Tracked repositories", "\n".join(settings.repos),
                             help="One owner/repo per line. Removing a repository stops collection but retains its history.")
        excluded = st.text_area("Excluded release tags (all repositories)", "\n".join(settings.excluded_tags),
                                help="One exact release tag per line, or comma-separated. Hidden from all dashboard metrics and charts.")
        interval = st.number_input("Collection interval (seconds)", min_value=60, max_value=604800,
                                   value=settings.interval, step=60, help="21600 seconds = 6 hours. Release-aligned scheduling is preserved.")
        startup = st.checkbox("Collect on startup", value=settings.collect_on_start)
        if st.form_submit_button("Save settings"):
            try:
                with closing(connect(db_path)) as conn:
                    save_settings(conn, Settings(parse_repos(repos), parse_tags(excluded), int(interval), startup))
                st.success("Settings saved. New repositories are queued automatically.")
                st.cache_data.clear()
            except ValueError as exc:
                st.error(str(exc))
    st.caption("GitHub token: " + ("configured" if os.getenv("GITHUB_TOKEN") else "not configured (public API only)"))
    st.caption("Credentials, logging, server address and port remain deployment configuration. Only expose this unauthenticated app to trusted users.")
    with closing(connect(db_path)) as conn:
        settings = read_settings(conn)
    if settings.repos:
        repo = st.selectbox("Collection status for", settings.repos, key="settings_status_repo")
        render_collection_status(db_path, repo)

    st.subheader("Import historical snapshots")
    all_repos = list(dict.fromkeys([*settings.repos, *known]))
    with st.form("history_import"):
        import_repo = st.text_input("Repository (owner/repo)", value=all_repos[0] if all_repos else "")
        history = st.text_area("Historical snapshot text", placeholder="Current UTC time: 2026-07-02T05:11:27Z\nv1 2026-07-01T00:00:00Z app.js=188")
        if st.form_submit_button("Import snapshots"):
            try:
                names = parse_repos(import_repo)
                if len(names) != 1:
                    raise ValueError("Enter exactly one repository.")
                _, snapshots = parse_history(history)
                if not snapshots:
                    raise ValueError("No snapshot rows found.")
                count = import_history(names[0], history, db_path)
                st.cache_data.clear()
                st.success(f"Processed {count} snapshot rows. Existing observed timestamps are not overwritten.")
            except ValueError as exc:
                st.error(str(exc))

    st.subheader("Manage annotations")
    with closing(connect(db_path)) as conn:
        known = [row["full_name"] for row in fetch_rows(conn, "SELECT full_name FROM repos ORDER BY full_name")]
    all_repos = list(dict.fromkeys([*settings.repos, *known]))
    if not all_repos:
        st.info("Add a repository before creating annotations.")
        return
    repo = st.selectbox("Annotation repository", all_repos)
    with closing(connect(db_path)) as conn:
        events = fetch_rows(conn, """SELECT e.id,e.title,e.occurred_at,e.notes,e.url,r.tag FROM events e
            JOIN repos rp ON rp.id=e.repo_id LEFT JOIN releases r ON r.id=e.release_id
            WHERE rp.full_name=? ORDER BY e.occurred_at DESC""", [repo])
        tags = [""] + [row["tag"] for row in fetch_rows(conn, """SELECT r.tag FROM releases r
            JOIN repos rp ON rp.id=r.repo_id WHERE rp.full_name=? ORDER BY r.published_at DESC""", [repo])]
    options = {event["id"]: event for event in events}
    chosen = st.selectbox("Annotation", [None, *options], format_func=lambda key: "New annotation" if key is None else f"{options[key]['title']} ({options[key]['occurred_at']})")
    event = options.get(chosen, {})
    with st.form(f"annotation_{repo}_{chosen}"):
        title = st.text_input("Title", value=event.get("title", ""))
        occurred = st.text_input("Date and time (ISO 8601 with timezone)", value=event.get("occurred_at", utc_now_iso()))
        tag = st.selectbox("Release (optional)", tags, index=tags.index(event.get("tag") or "") if (event.get("tag") or "") in tags else 0)
        notes = st.text_area("Notes", value=event.get("notes") or "")
        url = st.text_input("URL (optional)", value=event.get("url") or "")
        if st.form_submit_button("Save annotation"):
            try:
                with closing(connect(db_path)) as conn:
                    save_annotation(conn, repo, occurred, title, notes, url, tag, chosen)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    if chosen is not None:
        confirm = st.checkbox("Confirm deletion of this annotation", key=f"delete_{chosen}")
        if st.button("Delete annotation", disabled=not confirm):
            with closing(connect(db_path)) as conn:
                delete_annotation(conn, repo, chosen)
            st.rerun()
