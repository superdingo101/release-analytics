from datetime import datetime, timezone

import pytest

from db import connect, init_db, fetch_rows
from management import (Settings, read_settings, save_settings, parse_repos, request_collection,
                        collection_status, pending_repos, start_collection, finish_collection,
                        recover_interrupted, save_annotation, delete_annotation)
from scheduler import scheduler_tick


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOS", "owner/old")
    monkeypatch.setenv("RUN_INTERVAL_SECONDS", "21600")
    monkeypatch.setenv("EXCLUDED_RELEASE_TAGS", "v0")
    monkeypatch.setenv("COLLECT_ON_START", "false")
    path = str(tmp_path / "test.sqlite3")
    with connect(path) as conn:
        init_db(conn)
        yield path, conn


def test_settings_persist_and_empty_list_overrides_environment(database):
    path, conn = database
    assert read_settings(conn) == Settings(("owner/old",), ("v0",), 21600, False)
    save_settings(conn, Settings(("owner/new",), (), 3600, True))
    with connect(path) as other:
        assert read_settings(other) == Settings(("owner/new",), (), 3600, True)
        assert pending_repos(other, ["owner/new"]) == {"owner/new"}
        save_settings(other, Settings((), (), 3600, False))
    assert read_settings(conn).repos == ()
    assert not collection_status(conn, "owner/new")["pending"]


@pytest.mark.parametrize("value", ["https://github.com/owner/repo", "owner/..", "owner/repo?x=1", "owner/repo/extra"])
def test_invalid_repository(value):
    with pytest.raises(ValueError):
        parse_repos(value)


def test_collection_requests_coalesce_and_failures_preserve_success(database):
    _, conn = database
    repo = "owner/old"
    request_collection(conn, repo)
    request_collection(conn, repo)
    assert pending_repos(conn, [repo]) == {repo}
    assert start_collection(conn, repo)
    request_collection(conn, repo)
    assert not start_collection(conn, repo)
    assert pending_repos(conn, [repo]) == set()
    finish_collection(conn, repo, True)
    success = collection_status(conn, repo)["last_success"]
    assert success
    assert start_collection(conn, repo)
    finish_collection(conn, repo, False)
    assert collection_status(conn, repo)["last_success"] == success
    assert collection_status(conn, repo)["error"]
    assert start_collection(conn, repo)
    recover_interrupted(conn)
    assert pending_repos(conn, [repo]) == {repo}
    with pytest.raises(ValueError):
        request_collection(conn, "owner/untracked")


def test_scheduler_reads_updates_and_runs_manual_requests_once(database, monkeypatch):
    path, conn = database
    calls = []
    monkeypatch.setattr("scheduler.collect_repo", lambda repo, db_path: calls.append(repo))
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    fallback, nominal, outstanding = {}, {}, {}
    settings = read_settings(conn)
    previous = scheduler_tick(conn, path, now, settings, fallback, nominal, None, outstanding)
    assert calls == []
    due_before = collection_status(conn, "owner/old")["next_due"]
    scheduler_tick(conn, path, now, settings, fallback, nominal, previous, outstanding)
    assert collection_status(conn, "owner/old")["next_due"] == due_before
    request_collection(conn, "owner/old")
    scheduler_tick(conn, path, now, settings, fallback, nominal, previous, outstanding)
    scheduler_tick(conn, path, now, settings, fallback, nominal, previous, outstanding)
    assert calls == ["owner/old"]
    save_settings(conn, Settings(("owner/new",), (), 3600, False))
    previous = scheduler_tick(conn, path, now, read_settings(conn), fallback, nominal, previous, outstanding)
    assert calls == ["owner/old", "owner/new"]
    assert "owner/old" not in fallback
    assert collection_status(conn, "owner/old")["next_due"] is None
    scheduler_tick(conn, path, now, read_settings(conn), fallback, nominal, previous, outstanding)
    assert calls == ["owner/old", "owner/new"]


def test_annotations_are_repository_scoped(database):
    _, conn = database
    timestamp = "2026-07-01T12:00:00-07:00"
    save_annotation(conn, "owner/old", timestamp, "Launch", "notes", "https://example.com")
    event = fetch_rows(conn, "SELECT * FROM events")[0]
    assert event["occurred_at"] == "2026-07-01T19:00:00Z"
    save_annotation(conn, "owner/old", timestamp, "Updated", event_id=event["id"])
    assert fetch_rows(conn, "SELECT title FROM events") == [{"title": "Updated"}]
    with pytest.raises(ValueError):
        save_annotation(conn, "owner/new", timestamp, "Wrong", event_id=event["id"])
    delete_annotation(conn, "owner/new", event["id"])
    assert len(fetch_rows(conn, "SELECT * FROM events")) == 1
    delete_annotation(conn, "owner/old", event["id"])
    assert not fetch_rows(conn, "SELECT * FROM events")


def test_manual_collection_does_not_shift_existing_fallback(database, monkeypatch):
    from datetime import timedelta
    path, conn = database
    monkeypatch.setattr("scheduler.collect_repo", lambda *args: None)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    fallback, nominal, outstanding = {}, {}, {}
    settings = read_settings(conn)
    scheduler_tick(conn, path, now, settings, fallback, nominal, None, outstanding)
    due = fallback["owner/old"]
    request_collection(conn, "owner/old")
    scheduler_tick(conn, path, now + timedelta(minutes=5), settings, fallback, nominal, settings, outstanding)
    assert fallback["owner/old"] == due


def test_history_import_preserves_live_release_and_asset_metadata(database):
    from db import upsert_repo, upsert_release, upsert_asset, snapshots_for_repo
    from import_history import import_history
    path, conn = database
    repo_id = upsert_repo(conn, "owner/old")
    release_id = upsert_release(conn, repo_id, "v1", "2026-07-01T00:00:00Z", True, False, "https://example.com/v1")
    asset_id = upsert_asset(conn, release_id, 123, "app.js")
    conn.commit()
    assert import_history("owner/old", "Current UTC time: 2026-07-02T00:00:00Z\nv1 2026-07-01T00:00:00Z app.js=8", path) == 1
    row = snapshots_for_repo(conn, "owner/old")[0]
    assert (row["asset_id"], row["github_asset_id"], row["prerelease"], row["html_url"]) == (asset_id, 123, 1, "https://example.com/v1")
    with pytest.raises(ValueError, match="precede"):
        import_history("owner/old", "Current UTC time: 2026-06-30T00:00:00Z\nv1 2026-07-01T00:00:00Z app.js=8", path)
    assert len(snapshots_for_repo(conn, "owner/old")) == 1


def test_scheduled_and_manual_requests_share_one_collection(database, monkeypatch):
    from datetime import timedelta
    path, conn = database
    calls = []
    monkeypatch.setattr("scheduler.collect_repo", lambda repo, db_path: calls.append(repo))
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    fallback, nominal, outstanding = {}, {}, {}
    settings = read_settings(conn)
    scheduler_tick(conn, path, now, settings, fallback, nominal, None, outstanding)
    due = now + timedelta(seconds=settings.interval)
    request_collection(conn, "owner/old")
    scheduler_tick(conn, path, due, settings, fallback, nominal, settings, outstanding)
    scheduler_tick(conn, path, due, settings, fallback, nominal, settings, outstanding)
    assert calls == ["owner/old"]
    assert fallback["owner/old"] == due + timedelta(seconds=settings.interval)


def test_settings_accessible_before_first_collection(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    path = str(tmp_path / "empty.sqlite3")
    monkeypatch.setenv("DB_PATH", path)
    monkeypatch.setenv("REPOS", "")
    monkeypatch.setenv("RUN_INTERVAL_SECONDS", "21600")
    monkeypatch.setenv("COLLECT_ON_START", "false")
    monkeypatch.setenv("EXCLUDED_RELEASE_TAGS", "")
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard.py"))
    app.run()
    assert not app.exception
    app.sidebar.radio[0].set_value("Settings").run()
    assert not app.exception
    next(widget for widget in app.text_area if widget.label == "Tracked repositories").set_value("owner/new")
    next(widget for widget in app.button if widget.label == "Save settings").click().run()
    assert not app.exception
    with connect(path) as conn:
        assert read_settings(conn).repos == ("owner/new",)
        assert pending_repos(conn, ["owner/new"]) == {"owner/new"}
    app.sidebar.radio[0].set_value("Dashboard").run()
    assert not app.exception
    assert app.sidebar.selectbox[0].value == "owner/new"
    assert app.sidebar.toggle[0].label == "Include prereleases"


@pytest.mark.parametrize("window,delay", [(0, 3), (300, 600), (0, 21603), (300, 21603)])
def test_aligned_slot_survives_delayed_poll(database, monkeypatch, window, delay):
    from datetime import timedelta
    from db import upsert_repo, upsert_release
    path, conn = database
    repo = "owner/old"
    upsert_release(conn, upsert_repo(conn, repo), "v1", "2026-07-01T00:00:00Z", False, False, "")
    conn.commit()
    monkeypatch.setenv("SCHEDULE_DUE_WINDOW_SECONDS", str(window))
    calls = []
    monkeypatch.setattr("scheduler.collect_repo", lambda repo, db_path: calls.append(repo))
    due = datetime(2026, 7, 1, 6, tzinfo=timezone.utc)
    now = due - timedelta(seconds=2)
    fallback, nominal, outstanding = {}, {}, {}
    settings = read_settings(conn)
    previous = scheduler_tick(conn, path, now, settings, fallback, nominal, None, outstanding)
    assert calls == []
    assert outstanding[repo][1] == (due, due)
    late = due + timedelta(seconds=delay)
    scheduler_tick(conn, path, late, settings, fallback, nominal, previous, outstanding)
    assert calls == [repo]
    assert nominal[repo] == due + timedelta(seconds=(delay // settings.interval) * settings.interval)
    # A long delay coalesces missed observations rather than inventing history.
    scheduler_tick(conn, path, late, settings, fallback, nominal, previous, outstanding)
    assert calls == [repo]
    assert outstanding[repo][1][0] > late


def test_outstanding_slot_replans_on_anchor_and_interval_change(database, monkeypatch):
    from datetime import timedelta
    from db import upsert_repo, upsert_release
    path, conn = database
    repo = "owner/old"
    repo_id = upsert_repo(conn, repo)
    upsert_release(conn, repo_id, "v1", "2026-07-01T00:00:00Z", False, False, "")
    conn.commit()
    monkeypatch.setenv("SCHEDULE_DUE_WINDOW_SECONDS", "0")
    calls = []
    monkeypatch.setattr("scheduler.collect_repo", lambda *args: calls.append(args))
    now = datetime(2026, 7, 1, 5, 59, 58, tzinfo=timezone.utc)
    fallback, nominal, outstanding = {}, {}, {}
    settings = read_settings(conn)
    previous = scheduler_tick(conn, path, now, settings, fallback, nominal, None, outstanding)
    upsert_release(conn, repo_id, "v2", "2026-07-01T01:00:00Z", False, False, "")
    conn.commit()
    later = now + timedelta(seconds=5)
    scheduler_tick(conn, path, later, settings, fallback, nominal, previous, outstanding)
    assert calls == []
    assert outstanding[repo][1][0].hour == 7
    save_settings(conn, Settings((repo,), (), 14400, False))
    scheduler_tick(conn, path, later, read_settings(conn), fallback, nominal, previous, outstanding)
    assert calls == []
    assert outstanding[repo][1][0].hour == 9
    assert collection_status(conn, repo)["next_due"] == "2026-07-01T09:00:00+00:00"


def test_annotation_keeps_user_timestamp_across_reruns(database, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    path, conn = database
    monkeypatch.setenv("DB_PATH", path)
    clock = ["2026-07-01T00:00:00Z"]
    monkeypatch.setattr("settings_ui.utc_now_iso", lambda: clock[0])
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard.py")).run()
    app.sidebar.radio[0].set_value("Settings").run()
    assert not app.exception
    next(w for w in app.text_input if w.label == "Title").set_value("Old launch")
    next(w for w in app.text_input if w.label.startswith("Date and time")).set_value("2020-01-01T00:00:00Z")
    clock[0] = "2026-07-01T00:00:10Z"
    next(w for w in app.button if w.label == "Save annotation").click().run()
    assert not app.exception
    assert fetch_rows(conn, "SELECT title, occurred_at FROM events") == [
        {"title": "Old launch", "occurred_at": "2020-01-01T00:00:00Z"}
    ]
    assert next(w for w in app.text_input if w.label.startswith("Date and time")).value == clock[0]


@pytest.mark.parametrize("editing", [False, True])
def test_annotation_release_survives_background_discovery(database, monkeypatch, editing):
    from pathlib import Path
    from db import upsert_repo, upsert_release
    from streamlit.testing.v1 import AppTest

    path, conn = database
    repo = "owner/old"
    monkeypatch.setenv("DB_PATH", path)
    repo_id = upsert_repo(conn, repo)
    original_release = upsert_release(conn, repo_id, "v1", "2026-07-01T00:00:00Z", False, False, "")
    conn.commit()
    if editing:
        save_annotation(conn, repo, "2026-07-01T12:00:00Z", "Original annotation")
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard.py")).run()
    app.sidebar.radio[0].set_value("Settings").run()
    if editing:
        event_id = fetch_rows(conn, "SELECT id FROM events")[0]["id"]
        next(w for w in app.selectbox if w.label == "Annotation").set_value(event_id).run()
    assert not app.exception
    next(w for w in app.text_input if w.label == "Title").set_value("Linked annotation")
    next(w for w in app.selectbox if w.label == "Release (optional)").set_value("v1")
    # A new release sorts ahead of v1 and changes its index in the live list.
    upsert_release(conn, repo_id, "v2", "2026-07-02T00:00:00Z", False, False, "")
    conn.commit()
    next(w for w in app.button if w.label == "Save annotation").click().run()
    assert not app.exception
    assert fetch_rows(conn, "SELECT title, release_id FROM events") == [
        {"title": "Linked annotation", "release_id": original_release}
    ]
    assert next(w for w in app.selectbox if w.label == "Release (optional)").options == ["", "v2", "v1"]


def test_annotation_form_can_reload_new_release_options(database, monkeypatch):
    from pathlib import Path
    from db import upsert_repo, upsert_release
    from streamlit.testing.v1 import AppTest

    path, conn = database
    monkeypatch.setenv("DB_PATH", path)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard.py")).run()
    app.sidebar.radio[0].set_value("Settings").run()
    assert next(w for w in app.selectbox if w.label == "Release (optional)").options == [""]
    upsert_release(conn, upsert_repo(conn, "owner/old"), "v1", "2026-07-01T00:00:00Z", False, False, "")
    conn.commit()
    app.run()
    assert not app.exception
    assert next(w for w in app.selectbox if w.label == "Release (optional)").options == [""]
    next(w for w in app.button if w.label == "Refresh annotation form").click().run()
    assert not app.exception
    assert next(w for w in app.selectbox if w.label == "Release (optional)").options == ["", "v1"]


def test_consecutive_settings_saves(database, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    path, conn = database
    monkeypatch.setenv("DB_PATH", path)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard.py")).run()
    app.sidebar.radio[0].set_value("Settings").run()
    for repo in ("owner/one", "owner/two"):
        next(w for w in app.text_area if w.label == "Tracked repositories").set_value(repo)
        next(w for w in app.button if w.label == "Save settings").click().run()
        assert not app.exception
        assert read_settings(conn).repos == (repo,)


@pytest.mark.parametrize("change", ["insert", "update", "delete"])
def test_annotation_concurrent_changes_do_not_discard_or_overwrite_edits(database, monkeypatch, change):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    path, conn = database
    repo = "owner/old"
    monkeypatch.setenv("DB_PATH", path)
    stamp = "2026-07-01T00:00:00Z"
    save_annotation(conn, repo, stamp, "Original")
    event_id = fetch_rows(conn, "SELECT id FROM events")[0]["id"]
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard.py")).run()
    app.sidebar.radio[0].set_value("Settings").run()
    next(w for w in app.selectbox if w.label == "Annotation").set_value(event_id).run()
    next(w for w in app.text_input if w.label == "Title").set_value("My edit")
    if change == "insert":
        save_annotation(conn, repo, "2026-07-02T00:00:00Z", "Other")
    elif change == "update":
        save_annotation(conn, repo, stamp, "Other edit", event_id=event_id)
    else:
        delete_annotation(conn, repo, event_id)
    next(w for w in app.button if w.label == "Save annotation").click().run()
    assert not app.exception
    rows = fetch_rows(conn, "SELECT id,title FROM events ORDER BY id")
    if change == "insert":
        assert [row["title"] for row in rows] == ["My edit", "Other"]
    elif change == "update":
        assert [row["title"] for row in rows] == ["Other edit"]
        assert app.error
    else:
        assert rows == []
        assert app.error


def test_settings_conflict_is_rejected_and_case_only_changes_preserve_history(database):
    _, conn = database
    old = read_settings(conn)
    save_settings(conn, Settings(("OWNER/OLD",), (), 3600, True), expected=old)
    assert read_settings(conn).repos == ("owner/old",)
    with pytest.raises(ValueError, match="another session"):
        save_settings(conn, Settings(("owner/new",), (), 7200, False), expected=old)
    assert read_settings(conn).interval == 3600


def test_history_rejects_multiple_timestamps_and_impossible_counts(database):
    from import_history import import_history
    path, conn = database
    first = "Current UTC time: 2026-07-02T00:00:00Z\nv1 2026-07-01T00:00:00Z app.js=8"
    with pytest.raises(ValueError, match="one snapshot"):
        import_history("owner/old", first + "\nCurrent UTC time: 2026-07-03T00:00:00Z\nv1 2026-07-01T00:00:00Z app.js=9", path)
    with pytest.raises(ValueError, match="integer range"):
        import_history("owner/old", first.replace("app.js=8", "app.js=9223372036854775808"), path)
    assert not fetch_rows(conn, "SELECT * FROM download_snapshots")


def test_history_rejects_timestamp_before_existing_release(database):
    from db import upsert_repo, upsert_release
    from import_history import import_history
    path, conn = database
    upsert_release(conn, upsert_repo(conn, "owner/old"), "v1", "2026-07-03T00:00:00Z", False, False, "")
    conn.commit()
    with pytest.raises(ValueError, match="stored release"):
        import_history("OWNER/OLD", "Current UTC time: 2026-07-02T00:00:00Z\nv1 2026-07-01T00:00:00Z app.js=8", path)
    assert not fetch_rows(conn, "SELECT * FROM download_snapshots")
    assert len(fetch_rows(conn, "SELECT * FROM repos")) == 1
