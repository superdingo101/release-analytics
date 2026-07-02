from import_history import parse_history, synthetic_asset_id
from db import downloads_added_between_snapshots


def test_parse_history_snapshot_text():
    text = """
    Current UTC time: 2026-07-02T05:11:27Z
    v4.7.0  2026-06-27T17:58:49Z    skylight-calendar-card.js=188
    """
    collected_at, rows = parse_history(text)
    assert collected_at == "2026-07-02T05:11:27Z"
    assert rows == [
        {
            "tag": "v4.7.0",
            "published_at": "2026-06-27T17:58:49Z",
            "asset_name": "skylight-calendar-card.js",
            "download_count": 188,
        }
    ]


def test_synthetic_asset_id_is_stable():
    assert synthetic_asset_id("owner/repo", "v1", "asset.js") == synthetic_asset_id("owner/repo", "v1", "asset.js")


def test_download_deltas_and_rates():
    rows = [
        {"asset_id": 1, "tag": "v1", "published_at": "2026-07-01T00:00:00Z", "collected_at": "2026-07-01T12:00:00Z", "download_count": 10},
        {"asset_id": 1, "tag": "v1", "published_at": "2026-07-01T00:00:00Z", "collected_at": "2026-07-02T12:00:00Z", "download_count": 34},
    ]
    enriched = downloads_added_between_snapshots(rows)
    assert enriched[0]["downloads_added"] is None
    assert enriched[1]["downloads_added"] == 24
    assert enriched[1]["downloads_per_day"] == 24
    assert enriched[1]["release_age_days"] == 1.5

from db import connect, init_db, insert_snapshot, total_downloads_by_release, upsert_asset, upsert_release, upsert_repo


def test_upsert_asset_reconciles_imported_asset_with_live_github_id(tmp_path):
    db_file = tmp_path / "analytics.sqlite3"
    with connect(db_file) as conn:
        init_db(conn)
        repo_id = upsert_repo(conn, "owner/repo")
        release_id = upsert_release(conn, repo_id, "v1", "2026-07-01T00:00:00Z", False, False, "")
        imported_asset_id = upsert_asset(conn, release_id, synthetic_asset_id("owner/repo", "v1", "asset.js"), "asset.js")
        insert_snapshot(conn, imported_asset_id, "2026-07-02T00:00:00Z", 10)

        live_asset_id = upsert_asset(conn, release_id, 123456, "asset.js")
        insert_snapshot(conn, live_asset_id, "2026-07-03T00:00:00Z", 15)

        assets = conn.execute("SELECT id, github_asset_id, name FROM assets").fetchall()
        totals = total_downloads_by_release(conn, "owner/repo", True, "asset.js")

    assert live_asset_id == imported_asset_id
    assert len(assets) == 1
    assert assets[0]["github_asset_id"] == 123456
    assert totals == [{"tag": "v1", "published_at": "2026-07-01T00:00:00Z", "downloads": 15}]


def test_fetch_releases_follows_next_link(monkeypatch):
    import importlib
    import sys
    import types

    calls = []

    class Response:
        def __init__(self, payload, links=None):
            self.status_code = 200
            self._payload = payload
            self.links = links or {}
            self.text = ""

        def json(self):
            return self._payload

        def raise_for_status(self):
            raise AssertionError("unexpected raise_for_status")

    def fake_get(url, headers, params=None, timeout=30):
        calls.append((url, params))
        if len(calls) == 1:
            return Response([{"tag_name": "v2"}], {"next": {"url": "https://api.github.com/page/2"}})
        return Response([{"tag_name": "v1"}])

    fake_requests = types.SimpleNamespace(get=fake_get, HTTPError=RuntimeError)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    collector = importlib.import_module("collector")
    monkeypatch.setattr(collector, "requests", fake_requests)

    assert collector.fetch_releases("owner/repo") == [{"tag_name": "v2"}, {"tag_name": "v1"}]
    assert calls == [
        ("https://api.github.com/repos/owner/repo/releases", {"per_page": 100}),
        ("https://api.github.com/page/2", None),
    ]
