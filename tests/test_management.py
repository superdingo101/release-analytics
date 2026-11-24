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
    fallback, nominal = {}, {}
    settings = read_settings(conn)
    previous = scheduler_tick(conn, path, now, settings, fallback, nominal, None)
    assert calls == []
    due_before = collection_status(conn, "owner/old")["next_due"]
    scheduler_tick(conn, path, now, settings, fallback, nominal, previous)
    assert collection_status(conn, "owner/old")["next_due"] == due_before
    request_collection(conn, "owner/old")
    scheduler_tick(conn, path, now, settings, fallback, nominal, previous)
    scheduler_tick(conn, path, now, settings, fallback, nominal, previous)
    assert calls == ["owner/old"]
    save_settings(conn, Settings(("owner/new",), (), 3600, False))
    previous = scheduler_tick(conn, path, now, read_settings(conn), fallback, nominal, previous)
    assert calls == ["owner/old", "owner/new"]
    assert "owner/old" not in fallback
    assert collection_status(conn, "owner/old")["next_due"] is None
    scheduler_tick(conn, path, now, read_settings(conn), fallback, nominal, previous)
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
