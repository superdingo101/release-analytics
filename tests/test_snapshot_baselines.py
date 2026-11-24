"""Regression tests for initial collection and legacy publication anchors."""
from datetime import timedelta

import pytest

from collector import collect_repo
from db import (
    connect,
    downloads_added_between_snapshots,
    init_db,
    insert_snapshot,
    milestone_download_totals,
    parse_utc,
    snapshots_for_repo,
    total_downloads_by_release,
    upsert_asset,
    upsert_release,
    upsert_repo,
)
from import_history import import_history

REPO = "owner/repo"
PUBLISHED = "2025-06-01T00:00:00Z"
FIRST = "2026-07-01T00:00:00Z"
SECOND = "2026-07-01T06:00:00Z"


def _asset(conn, tag, github_id):
    repo_id = upsert_repo(conn, REPO)
    release_id = upsert_release(conn, repo_id, tag, PUBLISHED, False, False, "")
    return upsert_asset(conn, release_id, github_id, "app.js")


def _recent_deltas(rows):
    enriched = downloads_added_between_snapshots(rows)
    now = max(parse_utc(row["collected_at"]) for row in enriched)
    return [
        sum(row["downloads_added"] or 0 for row in enriched
            if parse_utc(row["collected_at"]) >= now - timedelta(hours=hours))
        for hours in (24, 168)
    ]


def test_initial_collection_does_not_inflate_recent_metrics(tmp_path, monkeypatch):
    db_path = tmp_path / "analytics.sqlite3"
    release = {
        "tag_name": "v1", "published_at": PUBLISHED,
        "assets": [{"id": 1, "name": "app.js", "download_count": 41094}],
    }
    monkeypatch.setattr("collector.fetch_releases", lambda *args: [release])
    times = iter([FIRST, SECOND])
    monkeypatch.setattr("collector.utc_now_iso", lambda: next(times))
    collect_repo(REPO, str(db_path))
    with connect(db_path) as conn:
        rows = snapshots_for_repo(conn, REPO)
        assert _recent_deltas(rows) == [0, 0]
        assert total_downloads_by_release(conn, REPO)[0]["downloads"] == 41094
        assert milestone_download_totals(rows) == [
            {"tag": "v1", "24h": None, "72h": None, "7d": None, "14d": None}
        ]
    release["assets"][0]["download_count"] = 41194
    collect_repo(REPO, str(db_path))
    with connect(db_path) as conn:
        rows = snapshots_for_repo(conn, REPO)
        assert _recent_deltas(rows) == [100, 100]
        assert total_downloads_by_release(conn, REPO)[0]["downloads"] == 41194


def test_upgrade_marks_legacy_anchors_without_deleting_observations(tmp_path):
    db_path = tmp_path / "analytics.sqlite3"
    with connect(db_path) as conn:
        init_db(conn)
        # Reconstruct the old schema and populate it using the legacy SQL.
        conn.execute("ALTER TABLE download_snapshots DROP COLUMN is_synthetic")
        old = _asset(conn, "v1", 1)
        real_zero = _asset(conn, "v2", 2)
        imported = _asset(conn, "v3", 3)
        nonzero_at_publication = _asset(conn, "v4", 4)
        conn.executemany(
            "INSERT INTO download_snapshots(asset_id, collected_at, download_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                (old, PUBLISHED, 0, FIRST),
                (old, FIRST, 41094, FIRST),
                (old, SECOND, 41194, SECOND),
                (real_zero, PUBLISHED, 0, PUBLISHED),
                (real_zero, FIRST, 5, FIRST),
                (imported, "2025-06-02T00:00:00Z", 0, FIRST),
                (imported, FIRST, 7, FIRST),
                (nonzero_at_publication, PUBLISHED, 3, FIRST),
            ],
        )
        conn.commit()
        init_db(conn)
        init_db(conn)
        raw = conn.execute(
            "SELECT asset_id, collected_at, is_synthetic FROM download_snapshots "
            "WHERE is_synthetic = 1"
        ).fetchall()
        assert [tuple(row) for row in raw] == [(old, PUBLISHED, 1)]
        assert conn.execute("SELECT COUNT(*) FROM download_snapshots").fetchone()[0] == 8
        rows = snapshots_for_repo(conn, REPO)
        by_asset = {
            asset: [row for row in rows if row["asset_id"] == asset]
            for asset in (old, real_zero, imported, nonzero_at_publication)
        }
        assert _recent_deltas(by_asset[old]) == [100, 100]
        assert _recent_deltas(by_asset[real_zero]) == [5, 5]
        assert _recent_deltas(by_asset[imported]) == [7, 7]
        assert by_asset[nonzero_at_publication][0]["download_count"] == 3
        assert {row["tag"]: row["downloads"] for row in total_downloads_by_release(conn, REPO)} == {
            "v1": 41194, "v2": 5, "v3": 7, "v4": 3,
        }
        # A real observation can replace a marked legacy anchor; subsequent
        # duplicate imports still must not overwrite observed data.
        insert_snapshot(conn, old, PUBLISHED, 0)
        insert_snapshot(conn, old, PUBLISHED, 99)
        conn.commit()
        init_db(conn)
        rows = [row for row in snapshots_for_repo(conn, REPO) if row["asset_id"] == old]
        assert [row["download_count"] for row in rows] == [0, 41094, 41194]
        assert not conn.execute("SELECT 1 FROM download_snapshots WHERE is_synthetic = 1").fetchone()


@pytest.mark.parametrize("count", [0, 8])
def test_new_history_at_publication_is_observed(tmp_path, count):
    db_path = tmp_path / "analytics.sqlite3"
    import_history(REPO, f"Current UTC time: {PUBLISHED}\nv1 {PUBLISHED} app.js={count}", str(db_path))
    with connect(db_path) as conn:
        init_db(conn)
        rows = snapshots_for_repo(conn, REPO)
        assert [(row["collected_at"], row["download_count"]) for row in rows] == [(PUBLISHED, count)]


def test_collection_at_publication_keeps_actual_count(tmp_path, monkeypatch):
    db_path = tmp_path / "analytics.sqlite3"
    monkeypatch.setattr("collector.utc_now_iso", lambda: PUBLISHED)
    monkeypatch.setattr("collector.fetch_releases", lambda *args: [{
        "tag_name": "v1", "published_at": PUBLISHED,
        "assets": [{"id": 1, "name": "app.js", "download_count": 12}],
    }])
    collect_repo(REPO, str(db_path))
    with connect(db_path) as conn:
        rows = snapshots_for_repo(conn, REPO)
        assert [row["download_count"] for row in rows] == [12]
        assert _recent_deltas(rows) == [0, 0]
