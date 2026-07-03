from __future__ import annotations

import os
import sqlite3
import sys
import types

import pytest

from collector import collect_repo, configured_repos, fetch_releases
from config import load_env
from db import (
    connect,
    current_adoption_percentage,
    downloads_added_between_snapshots,
    fetch_rows,
    init_db,
    insert_snapshot,
    latest_snapshot_summary,
    milestone_download_totals,
    snapshots_for_repo,
    total_downloads_by_release,
    upsert_asset,
    upsert_release,
    upsert_repo,
)
from import_history import import_history, parse_history, synthetic_asset_id


def seed_release(conn: sqlite3.Connection, repo="owner/repo", tag="v1", published="2026-07-01T00:00:00Z", *, prerelease=False, draft=False, asset="asset.js", count=10, collected="2026-07-02T00:00:00Z"):
    repo_id = upsert_repo(conn, repo, now="2026-07-01T00:00:00Z")
    release_id = upsert_release(conn, repo_id, tag, published, prerelease, draft, f"https://example.test/{tag}")
    asset_id = upsert_asset(conn, release_id, synthetic_asset_id(repo, tag, asset), asset)
    insert_snapshot(conn, asset_id, collected, count)
    return repo_id, release_id, asset_id


def test_load_env_ignores_comments_and_preserves_existing_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("""
    # comment
    REPOS=owner/repo,other/repo
    QUOTED="hello world"
    EXISTING=from-file
    INVALID_LINE
    """)
    monkeypatch.delenv("REPOS", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    monkeypatch.setenv("EXISTING", "already-set")

    load_env(env_file)

    assert configured_repos() == ["owner/repo", "other/repo"]
    assert os.environ["QUOTED"] == "hello world"
    assert os.environ["EXISTING"] == "already-set"


def test_configured_repos_splits_commas_and_newlines(monkeypatch):
    monkeypatch.setenv("REPOS", " owner/repo,\nsecond/repo ,, third/repo ")
    assert configured_repos() == ["owner/repo", "second/repo", "third/repo"]


def test_parse_history_supports_multiple_assets_and_backticks():
    text = """
    `Current UTC time: 2026-07-02T05:11:27Z`
    v4.7.0  2026-06-27T17:58:49Z    app.js=188 app.css=7 ignored=nope
    not a snapshot line
    """
    collected_at, rows = parse_history(text)

    assert collected_at == "2026-07-02T05:11:27Z"
    assert rows == [
        {"tag": "v4.7.0", "published_at": "2026-06-27T17:58:49Z", "asset_name": "app.js", "download_count": 188},
        {"tag": "v4.7.0", "published_at": "2026-06-27T17:58:49Z", "asset_name": "app.css", "download_count": 7},
    ]


def test_parse_history_requires_collection_time():
    with pytest.raises(ValueError, match="Current UTC time"):
        parse_history("v1 2026-07-01T00:00:00Z app.js=1")


def test_import_history_inserts_rows_and_is_idempotent_for_same_timestamp(tmp_path):
    db_file = tmp_path / "analytics.sqlite3"
    text = """
    Current UTC time: 2026-07-02T05:11:27Z
    v1 2026-07-01T00:00:00Z app.js=3 app.css=4
    """

    assert import_history("owner/repo", text, str(db_file)) == 2
    assert import_history("owner/repo", text, str(db_file)) == 2

    with connect(db_file) as conn:
        rows = fetch_rows(conn, "SELECT download_count FROM download_snapshots ORDER BY download_count")
    assert [row["download_count"] for row in rows] == [3, 4]


def test_schema_enforces_foreign_keys_and_non_negative_snapshots(tmp_path):
    with connect(tmp_path / "analytics.sqlite3") as conn:
        init_db(conn)
        with pytest.raises(sqlite3.IntegrityError):
            insert_snapshot(conn, 999, "2026-07-02T00:00:00Z", 1)
        _, _, asset_id = seed_release(conn)
        with pytest.raises(sqlite3.IntegrityError):
            insert_snapshot(conn, asset_id, "2026-07-03T00:00:00Z", -1)


def test_init_db_deduplicates_legacy_duplicate_assets(tmp_path):
    db_file = tmp_path / "analytics.sqlite3"
    with sqlite3.connect(db_file) as raw:
        raw.row_factory = sqlite3.Row
        init_db(raw)
        repo_id = upsert_repo(raw, "owner/repo")
        release_id = upsert_release(raw, repo_id, "v1", "2026-07-01T00:00:00Z", False, False, "")
        raw.executescript("DROP INDEX idx_assets_release_name; DROP INDEX idx_assets_github_asset_id;")
        raw.execute("INSERT INTO assets(release_id, github_asset_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (release_id, 1, "asset.js", "now", "now"))
        keep_id = raw.execute("SELECT id FROM assets WHERE github_asset_id = 1").fetchone()["id"]
        raw.execute("INSERT INTO assets(release_id, github_asset_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (release_id, 2, "asset.js", "now", "now"))
        discard_id = raw.execute("SELECT id FROM assets WHERE github_asset_id = 2").fetchone()["id"]
        raw.execute("INSERT INTO download_snapshots(asset_id, collected_at, download_count, created_at) VALUES (?, ?, ?, ?)", (keep_id, "2026-07-02T00:00:00Z", 5, "now"))
        raw.execute("INSERT INTO download_snapshots(asset_id, collected_at, download_count, created_at) VALUES (?, ?, ?, ?)", (discard_id, "2026-07-02T00:00:00Z", 9, "now"))
        raw.execute("INSERT INTO download_snapshots(asset_id, collected_at, download_count, created_at) VALUES (?, ?, ?, ?)", (discard_id, "2026-07-03T00:00:00Z", 10, "now"))
        raw.commit()
        init_db(raw)
        assets = raw.execute("SELECT id, name FROM assets").fetchall()
        snapshots = raw.execute("SELECT asset_id, collected_at, download_count FROM download_snapshots ORDER BY collected_at").fetchall()

    assert len(assets) == 1
    assert [row["download_count"] for row in snapshots] == [5, 10]


def test_snapshot_queries_filter_prerelease_drafts_and_assets(tmp_path):
    with connect(tmp_path / "analytics.sqlite3") as conn:
        init_db(conn)
        seed_release(conn, tag="v1", published="2026-07-01T00:00:00Z", count=10)
        seed_release(conn, tag="v2-beta", published="2026-07-02T00:00:00Z", prerelease=True, count=20)
        seed_release(conn, tag="v3-draft", published="2026-07-03T00:00:00Z", draft=True, count=30)
        seed_release(conn, tag="v1", asset="other.js", count=5)

        stable_rows = snapshots_for_repo(conn, "owner/repo", include_prereleases=False, asset_name="asset.js")
        all_rows = snapshots_for_repo(conn, "owner/repo", include_prereleases=True, asset_name="asset.js")
        latest = latest_snapshot_summary(conn, "owner/repo", include_prereleases=True, asset_name="asset.js")
        totals = total_downloads_by_release(conn, "owner/repo", include_prereleases=False)

    assert [row["tag"] for row in stable_rows] == ["v1"]
    assert [row["tag"] for row in all_rows] == ["v1", "v2-beta"]
    assert [row["tag"] for row in latest] == ["v2-beta", "v1"]
    assert totals == [{"tag": "v1", "published_at": "2026-07-01T00:00:00Z", "downloads": 15}]


def test_metric_helpers_handle_negative_deltas_zero_intervals_and_adoption():
    rows = [
        {"asset_id": 1, "tag": "v1", "published_at": "2026-07-01T00:00:00Z", "collected_at": "2026-07-02T00:00:00Z", "download_count": 10},
        {"asset_id": 1, "tag": "v1", "published_at": "2026-07-01T00:00:00Z", "collected_at": "2026-07-02T00:00:00Z", "download_count": 8},
    ]
    enriched = downloads_added_between_snapshots(rows)
    assert enriched[1]["downloads_added"] == -2
    assert enriched[1]["downloads_per_day"] is None
    assert current_adoption_percentage([{"downloads": 10}, {"downloads": 0}]) is None
    assert current_adoption_percentage([{"downloads": 25}, {"downloads": 100}]) == 25


def test_milestone_download_totals_uses_latest_snapshot_before_cutoff():
    rows = [
        {"tag": "v1", "published_at": "2026-07-01T00:00:00Z", "collected_at": "2026-07-01T12:00:00Z", "download_count": 4},
        {"tag": "v1", "published_at": "2026-07-01T00:00:00Z", "collected_at": "2026-07-02T00:00:00Z", "download_count": 9},
        {"tag": "v1", "published_at": "2026-07-01T00:00:00Z", "collected_at": "2026-07-04T01:00:00Z", "download_count": 20},
    ]
    assert milestone_download_totals(rows) == [{"tag": "v1", "24h": 9, "72h": 9, "7d": 20, "14d": 20}]


def test_milestone_download_totals_allows_small_late_release_aligned_sample():
    rows = [
        {"tag": "v1", "published_at": "2026-07-01T02:17:00Z", "collected_at": "2026-07-02T02:20:00Z", "download_count": 12},
        {"tag": "v1", "published_at": "2026-07-01T02:17:00Z", "collected_at": "2026-07-02T03:00:00Z", "download_count": 18},
    ]
    assert milestone_download_totals(rows)[0]["24h"] == 12


def test_fetch_releases_sends_token_and_raises_http_errors(monkeypatch):
    class Response:
        status_code = 403
        text = "rate limited"
        links = {}

        def json(self):
            return []

        def raise_for_status(self):
            raise RuntimeError("boom")

    seen = {}

    def fake_get(url, headers, params=None, timeout=30):
        seen.update(headers=headers, timeout=timeout, params=params, url=url)
        return Response()

    fake_requests = types.SimpleNamespace(get=fake_get, HTTPError=RuntimeError)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setattr("collector.requests", fake_requests)

    with pytest.raises(RuntimeError, match="boom"):
        fetch_releases("owner/repo", token="secret")
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert seen["timeout"] == 30


def test_collect_repo_persists_releases_and_skips_drafts(tmp_path, monkeypatch):
    payload = [
        {"tag_name": "v1", "published_at": "2026-07-01T00:00:00Z", "created_at": "2026-07-01T00:00:00Z", "prerelease": False, "draft": False, "html_url": "https://example.test/v1", "assets": [{"id": 1, "name": "app.js", "download_count": 11}]},
        {"tag_name": "v2", "published_at": "2026-07-02T00:00:00Z", "created_at": "2026-07-02T00:00:00Z", "prerelease": False, "draft": True, "html_url": "https://example.test/v2", "assets": [{"id": 2, "name": "app.js", "download_count": 22}]},
    ]
    monkeypatch.setattr("collector.fetch_releases", lambda repo, token=None: payload)
    monkeypatch.setattr("collector.utc_now_iso", lambda: "2026-07-03T00:00:00Z")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    collect_repo("owner/repo", str(tmp_path / "analytics.sqlite3"))

    with connect(tmp_path / "analytics.sqlite3") as conn:
        rows = snapshots_for_repo(conn, "owner/repo", include_prereleases=True, asset_name="app.js")
    assert [(row["tag"], row["download_count"]) for row in rows] == [("v1", 11)]
