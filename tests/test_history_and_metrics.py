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
