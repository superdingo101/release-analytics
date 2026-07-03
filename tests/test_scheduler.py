from __future__ import annotations

from datetime import datetime, timezone

from db import connect, init_db, upsert_release, upsert_repo
from scheduler import due_repos, latest_release_anchors, next_due_by_repo, next_run_time


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_release_at_0217_with_6h_interval_gives_0817_when_now_is_0300():
    assert next_run_time([dt("2026-07-03T02:17:00Z")], dt("2026-07-03T03:00:00Z"), 21600) == dt("2026-07-03T08:17:00Z")


def test_release_at_0217_with_6h_interval_gives_1417_when_now_is_0818():
    assert next_run_time([dt("2026-07-03T02:17:00Z")], dt("2026-07-03T08:18:00Z"), 21600) == dt("2026-07-03T14:17:00Z")


def test_exact_slot_runs_immediately():
    now = dt("2026-07-03T08:17:00Z")
    assert next_run_time([dt("2026-07-03T02:17:00Z")], now, 21600) == now


def test_multiple_repo_anchors_choose_earliest_upcoming_slot():
    now = dt("2026-07-03T03:00:00Z")
    anchors = [dt("2026-07-03T02:17:00Z"), dt("2026-07-03T01:30:00Z")]
    assert next_run_time(anchors, now, 21600) == dt("2026-07-03T07:30:00Z")


def test_no_release_rows_falls_back_to_interval():
    now = dt("2026-07-03T03:00:00Z")
    assert next_run_time([], now, 21600) == dt("2026-07-03T09:00:00Z")


def test_latest_release_anchors_use_latest_non_draft_per_repo(tmp_path):
    db_file = tmp_path / "analytics.sqlite3"
    with connect(db_file) as conn:
        init_db(conn)
        repo_id = upsert_repo(conn, "owner/repo")
        upsert_release(conn, repo_id, "v1", "2026-07-01T00:00:00Z", False, False, "")
        upsert_release(conn, repo_id, "v2-beta", "2026-07-02T00:00:00Z", True, False, "")
        upsert_release(conn, repo_id, "v3-draft", "2026-07-03T00:00:00Z", False, True, "")
    assert latest_release_anchors(str(db_file), ["owner/repo"]) == {"owner/repo": dt("2026-07-02T00:00:00Z")}


def test_each_repo_keeps_its_own_release_aligned_schedule():
    now = dt("2026-07-03T08:17:00Z")
    next_due = next_due_by_repo(
        ["owner/repo-a", "owner/repo-b"],
        {
            "owner/repo-a": dt("2026-07-03T02:17:00Z"),
            "owner/repo-b": dt("2026-07-03T04:43:00Z"),
        },
        now,
        21600,
    )

    assert next_due["owner/repo-a"] == (dt("2026-07-03T08:17:00Z"), dt("2026-07-03T08:17:00Z"))
    assert next_due["owner/repo-b"] == (dt("2026-07-03T10:43:00Z"), dt("2026-07-03T10:43:00Z"))
    assert due_repos(next_due, now, due_window_seconds=0) == ["owner/repo-a"]


def test_repos_with_no_anchor_use_independent_fallback_due_time():
    now = dt("2026-07-03T08:17:00Z")
    fallback = {"owner/repo-b": dt("2026-07-03T09:00:00Z")}

    next_due = next_due_by_repo(
        ["owner/repo-a", "owner/repo-b"],
        {"owner/repo-a": dt("2026-07-03T02:17:00Z")},
        now,
        21600,
        fallback_due_by_repo=fallback,
    )

    assert next_due["owner/repo-a"][0] == dt("2026-07-03T08:17:00Z")
    assert next_due["owner/repo-b"] == (dt("2026-07-03T09:00:00Z"), dt("2026-07-03T09:00:00Z"))
