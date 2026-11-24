"""Management screens using Streamlit's existing widgets."""
from __future__ import annotations

import os
from contextlib import closing

import streamlit as st

from db import connect, init_db, fetch_rows, utc_now_iso
from import_history import import_history, parse_history
from management import (
    Settings, read_settings, save_settings, parse_repos, parse_tags,
    request_collection, collection_status, save_annotation, delete_annotation, annotation_rows,
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
        try:
            with closing(connect(db_path)) as conn:
                request_collection(conn, repo)
            st.rerun()
        except ValueError as exc:
            st.sidebar.error(str(exc))
    st.sidebar.button("Refresh collection status", on_click=st.cache_data.clear)


def _reload_annotations() -> None:
    st.session_state.annotation_catalog = None
    st.session_state.annotation_context = None


def render_settings(db_path: str) -> None:
    st.header("Settings")
    st.caption("Settings are saved in the persistent database. The running scheduler picks up changes automatically.")
    with closing(connect(db_path)) as conn:
        init_db(conn)
        settings = read_settings(conn)
        known = [row["full_name"] for row in fetch_rows(conn, "SELECT full_name FROM repos ORDER BY full_name")]
    if st.session_state.get("settings_defaults") is None:
        st.session_state.settings_defaults = settings
        st.session_state.settings_generation = st.session_state.get("settings_generation", 0) + 1
    defaults = st.session_state.settings_defaults
    generation = st.session_state.settings_generation
    if st.session_state.pop("settings_saved", False):
        st.success("Settings saved. New repositories are queued automatically.")
    if defaults != settings:
        st.warning("Settings changed in another session. Reload before saving.")
        if st.button("Reload settings", help="Discard unsaved settings changes."):
            st.session_state.settings_defaults = None
            st.rerun()
    with st.form(f"management_settings_{generation}"):
        repos = st.text_area("Tracked repositories", "\n".join(defaults.repos),
                             help="One owner/repo per line. Removing a repository stops collection but retains its history.")
        excluded = st.text_area("Excluded release tags (all repositories)", "\n".join(defaults.excluded_tags),
                                help="One exact release tag per line, or comma-separated. Hidden from all dashboard metrics and charts.")
        interval = st.number_input("Collection interval (seconds)", min_value=60, max_value=604800,
                                   value=max(60, min(604800, defaults.interval)), step=60, help="21600 seconds = 6 hours. Release-aligned scheduling is preserved.")
        startup = st.checkbox("Collect on startup", value=defaults.collect_on_start)
        if st.form_submit_button("Save settings"):
            try:
                with closing(connect(db_path)) as conn:
                    save_settings(conn, Settings(parse_repos(repos), parse_tags(excluded), int(interval), startup), expected=defaults)
                st.session_state.settings_defaults = None
                st.session_state.settings_saved = True
                _reload_annotations()
                st.session_state.pop("history_repo_default", None)
                st.cache_data.clear()
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    st.caption("GitHub token: " + ("configured" if os.getenv("GITHUB_TOKEN") else "not configured (public API only)"))
    st.caption("Credentials, logging, server address and port remain deployment configuration. Only expose this unauthenticated app to trusted users.")
    if settings.repos:
        repo = st.selectbox("Collection status for", settings.repos, key="settings_status_repo")
        render_collection_status(db_path, repo)

    st.subheader("Import historical snapshots")
    all_repos = list(dict.fromkeys([*settings.repos, *known]))
    if "history_repo_default" not in st.session_state:
        st.session_state.history_repo_default = all_repos[0] if all_repos else ""
    with st.form("history_import"):
        import_repo = st.text_input("Repository (owner/repo)", value=st.session_state.history_repo_default)
        history = st.text_area("Historical snapshot text", placeholder="Current UTC time: 2026-07-02T05:11:27Z\nv1 2026-06-27T17:58:49Z app.js=188")
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
    # Freeze selector choices AND labels (old Streamlit versions use both
    # in widget identity). Refresh only on an explicit action or successful
    # save, preserving the selected repository and event by ID.
    if st.session_state.get("annotation_catalog") is None:
        st.session_state.annotation_catalog = all_repos
        st.session_state.annotation_picker_generation = st.session_state.get("annotation_picker_generation", 0) + 1
        st.session_state.annotation_picker_repo = None
        last_repo = st.session_state.get("annotation_selected_repo")
        st.session_state.annotation_repo_default = last_repo if last_repo in all_repos else all_repos[0]
    catalog = st.session_state.annotation_catalog
    picker_generation = st.session_state.annotation_picker_generation
    last_repo = st.session_state.get("annotation_selected_repo")
    repo = st.selectbox("Annotation repository", catalog,
                        index=catalog.index(st.session_state.annotation_repo_default),
                        key=f"annotation_repo_{picker_generation}")
    # Keep the widget default stable across selection reruns as well.
    with closing(connect(db_path)) as conn:
        events = annotation_rows(conn, repo)
        tags = [""] + [row["tag"] for row in fetch_rows(conn, """SELECT r.tag FROM releases r
            JOIN repos rp ON rp.id=r.repo_id WHERE rp.full_name=? ORDER BY r.published_at DESC,r.tag""", [repo])]
    live = {event["id"]: event for event in events}
    if st.session_state.get("annotation_picker_repo") != repo:
        st.session_state.annotation_picker_repo = repo
        st.session_state.annotation_event_options = live
        last_event = st.session_state.get("annotation_selected_event") if last_repo == repo else None
        st.session_state.annotation_picker_default = last_event if last_event in live else None
        st.session_state.annotation_event_generation = st.session_state.get("annotation_event_generation", 0) + 1
    options = st.session_state.annotation_event_options
    ids = [None, *options]
    chosen = st.selectbox("Annotation", ids,
                          index=ids.index(st.session_state.annotation_picker_default),
                          key=f"annotation_event_{st.session_state.annotation_event_generation}",
                          format_func=lambda key: "New annotation" if key is None else f"{options[key]['title']} ({options[key]['occurred_at']})")
    st.session_state.annotation_selected_repo = repo
    st.session_state.annotation_selected_event = chosen
    context = (repo, chosen)
    if st.session_state.get("annotation_context") != context:
        st.session_state.annotation_context = context
        st.session_state.annotation_event_default = options.get(chosen, {})
        st.session_state.annotation_default_time = options.get(chosen, {}).get("occurred_at") or utc_now_iso()
        st.session_state.annotation_tags = tags
        st.session_state.annotation_generation = st.session_state.get("annotation_generation", 0) + 1
    event = st.session_state.annotation_event_default
    generation = st.session_state.annotation_generation
    form_tags = st.session_state.annotation_tags
    default_tag = event.get("tag") or ""
    if chosen is not None and chosen not in live:
        st.warning("This annotation was deleted in another session. Refresh the annotation form before continuing.")
    elif chosen is not None and live[chosen] != event:
        st.warning("This annotation changed in another session. Refresh the annotation form before saving.")
    if catalog != all_repos or options != live or form_tags != tags:
        st.caption("Available annotations or releases changed. Your current form is preserved; refresh to reload.")
        if st.button("Refresh annotation form", help="Discard unsaved annotation changes and reload available annotations and releases."):
            _reload_annotations()
            st.rerun()
    with st.form(f"annotation_{repo}_{chosen}_{generation}"):
        title = st.text_input("Title", value=event.get("title", ""))
        occurred = st.text_input("Date and time (ISO 8601 with timezone)",
                                 value=st.session_state.annotation_default_time,
                                 key=f"annotation_time_{generation}")
        tag = st.selectbox("Release (optional)", form_tags,
                           index=form_tags.index(default_tag) if default_tag in form_tags else 0,
                           key=f"annotation_release_{generation}")
        notes = st.text_area("Notes", value=event.get("notes") or "")
        url = st.text_input("URL (optional)", value=event.get("url") or "")
        if st.form_submit_button("Save annotation"):
            try:
                with closing(connect(db_path)) as conn:
                    save_annotation(conn, repo, occurred, title, notes, url, tag, chosen, expected=event if chosen is not None else None)
                _reload_annotations()
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    if chosen is not None:
        confirm = st.checkbox("Confirm deletion of this annotation", key=f"delete_{generation}")
        if st.button("Delete annotation", disabled=not confirm):
            try:
                with closing(connect(db_path)) as conn:
                    delete_annotation(conn, repo, chosen, expected=event)
                st.session_state.annotation_selected_event = None
                _reload_annotations()
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
