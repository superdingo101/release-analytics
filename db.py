"""SQLite helpers for release download analytics."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DB_PATH = "release_analytics.sqlite3"
MILESTONE_HOURS = (24, 72, 168, 336)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string ending in Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """Parse a UTC ISO-8601 timestamp."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the database schema and indexes if needed."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS repos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            published_at TEXT NOT NULL,
            prerelease INTEGER NOT NULL DEFAULT 0,
            draft INTEGER NOT NULL DEFAULT 0,
            html_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(repo_id, tag)
        );

        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
            github_asset_id INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS download_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            collected_at TEXT NOT NULL,
            download_count INTEGER NOT NULL CHECK(download_count >= 0),
            created_at TEXT NOT NULL,
            UNIQUE(asset_id, collected_at)
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER REFERENCES repos(id) ON DELETE CASCADE,
            release_id INTEGER REFERENCES releases(id) ON DELETE SET NULL,
            occurred_at TEXT NOT NULL,
            title TEXT NOT NULL,
            notes TEXT,
            url TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_releases_repo_tag ON releases(repo_id, tag);
        CREATE INDEX IF NOT EXISTS idx_releases_published_at ON releases(published_at);
        CREATE INDEX IF NOT EXISTS idx_snapshots_collected_at ON download_snapshots(collected_at);
        CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);
        """
    )
    _dedupe_assets(conn)
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_github_asset_id ON assets(github_asset_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_release_name ON assets(release_id, name);
        """
    )
    conn.commit()


def _dedupe_assets(conn: sqlite3.Connection) -> None:
    """Merge duplicate asset rows for the same release/name before adding constraints."""
    duplicate_groups = conn.execute(
        """
        SELECT release_id, name, MIN(id) AS keep_id, GROUP_CONCAT(id) AS asset_ids
        FROM assets
        GROUP BY release_id, name
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for group in duplicate_groups:
        keep_id = int(group["keep_id"])
        asset_ids = [int(asset_id) for asset_id in str(group["asset_ids"]).split(",")]
        discard_ids = [asset_id for asset_id in asset_ids if asset_id != keep_id]
        for discard_id in discard_ids:
            conn.execute(
                """
                DELETE FROM download_snapshots
                WHERE asset_id = ?
                  AND collected_at IN (
                      SELECT collected_at FROM download_snapshots WHERE asset_id = ?
                  )
                """,
                (discard_id, keep_id),
            )
            conn.execute("UPDATE download_snapshots SET asset_id = ? WHERE asset_id = ?", (keep_id, discard_id))
            conn.execute("DELETE FROM assets WHERE id = ?", (discard_id,))


def upsert_repo(conn: sqlite3.Connection, full_name: str, now: str | None = None) -> int:
    now = now or utc_now_iso()
    conn.execute("INSERT OR IGNORE INTO repos(full_name, created_at) VALUES (?, ?)", (full_name, now))
    return int(conn.execute("SELECT id FROM repos WHERE full_name = ?", (full_name,)).fetchone()["id"])


def upsert_release(conn: sqlite3.Connection, repo_id: int, tag: str, published_at: str, prerelease: bool, draft: bool, html_url: str) -> int:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO releases(repo_id, tag, published_at, prerelease, draft, html_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_id, tag) DO UPDATE SET
            published_at=excluded.published_at,
            prerelease=excluded.prerelease,
            draft=excluded.draft,
            html_url=excluded.html_url,
            updated_at=excluded.updated_at
        """,
        (repo_id, tag, published_at, int(prerelease), int(draft), html_url, now, now),
    )
    return int(conn.execute("SELECT id FROM releases WHERE repo_id = ? AND tag = ?", (repo_id, tag)).fetchone()["id"])


def upsert_asset(conn: sqlite3.Connection, release_id: int, github_asset_id: int, name: str) -> int:
    now = utc_now_iso()
    existing_by_name = conn.execute(
        "SELECT id, github_asset_id FROM assets WHERE release_id = ? AND name = ?",
        (release_id, name),
    ).fetchone()
    if existing_by_name:
        conn.execute(
            """
            UPDATE assets
            SET github_asset_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (github_asset_id, now, int(existing_by_name["id"])),
        )
        return int(existing_by_name["id"])
    conn.execute(
        """
        INSERT INTO assets(release_id, github_asset_id, name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(github_asset_id) DO UPDATE SET
            release_id=excluded.release_id,
            name=excluded.name,
            updated_at=excluded.updated_at
        """,
        (release_id, github_asset_id, name, now, now),
    )
    return int(conn.execute("SELECT id FROM assets WHERE github_asset_id = ?", (github_asset_id,)).fetchone()["id"])


def insert_snapshot(conn: sqlite3.Connection, asset_id: int, collected_at: str, download_count: int) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO download_snapshots(asset_id, collected_at, download_count, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(asset_id, collected_at) DO NOTHING
        """,
        (asset_id, collected_at, int(download_count), now),
    )


def fetch_rows(conn: sqlite3.Connection, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]


def snapshots_for_repo(conn: sqlite3.Connection, repo_full_name: str, include_prereleases: bool = True, asset_name: str | None = None) -> list[dict[str, Any]]:
    params: list[Any] = [repo_full_name]
    filters = ["rp.full_name = ?", "r.draft = 0"]
    if not include_prereleases:
        filters.append("r.prerelease = 0")
    if asset_name:
        filters.append("a.name = ?")
        params.append(asset_name)
    return fetch_rows(conn, f"""
        SELECT rp.full_name AS repo, r.id AS release_id, r.tag, r.published_at, r.prerelease, r.html_url,
               a.id AS asset_id, a.github_asset_id, a.name AS asset_name,
               s.collected_at, s.download_count
        FROM download_snapshots s
        JOIN assets a ON a.id = s.asset_id
        JOIN releases r ON r.id = a.release_id
        JOIN repos rp ON rp.id = r.repo_id
        WHERE {' AND '.join(filters)}
        ORDER BY r.published_at, s.collected_at
    """, params)


def latest_snapshot_summary(conn: sqlite3.Connection, repo_full_name: str, include_prereleases: bool = False, asset_name: str | None = None) -> list[dict[str, Any]]:
    params: list[Any] = [repo_full_name]
    asset_filter = ""
    if asset_name:
        asset_filter = " AND a.name = ?"
        params.append(asset_name)
    prerelease_filter = "" if include_prereleases else " AND r.prerelease = 0"
    return fetch_rows(conn, f"""
        WITH latest AS (
            SELECT a.id AS asset_id, MAX(s.collected_at) AS collected_at
            FROM assets a
            JOIN releases r ON r.id = a.release_id
            JOIN repos rp ON rp.id = r.repo_id
            JOIN download_snapshots s ON s.asset_id = a.id
            WHERE rp.full_name = ? AND r.draft = 0 {prerelease_filter} {asset_filter}
            GROUP BY a.id
        )
        SELECT r.tag, r.published_at, r.prerelease, a.name AS asset_name, s.collected_at, s.download_count
        FROM latest l
        JOIN download_snapshots s ON s.asset_id = l.asset_id AND s.collected_at = l.collected_at
        JOIN assets a ON a.id = s.asset_id
        JOIN releases r ON r.id = a.release_id
        ORDER BY r.published_at DESC
    """, params)


def total_downloads_by_release(conn: sqlite3.Connection, repo_full_name: str, include_prereleases: bool = False, asset_name: str | None = None) -> list[dict[str, Any]]:
    rows = latest_snapshot_summary(conn, repo_full_name, include_prereleases, asset_name)
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = totals.setdefault(row["tag"], {"tag": row["tag"], "published_at": row["published_at"], "downloads": 0})
        item["downloads"] += row["download_count"]
    return sorted(totals.values(), key=lambda r: r["published_at"], reverse=True)


def downloads_added_between_snapshots(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous: dict[int, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: (r["asset_id"], r["collected_at"])):
        prior = previous.get(row["asset_id"])
        delta = None if prior is None else row["download_count"] - prior["download_count"]
        hours = None if prior is None else (parse_utc(row["collected_at"]) - parse_utc(prior["collected_at"])).total_seconds() / 3600
        enriched = dict(row)
        enriched["downloads_added"] = delta
        enriched["downloads_per_day"] = None if delta is None or not hours or hours <= 0 else delta / (hours / 24)
        enriched["release_age_hours"] = (parse_utc(row["collected_at"]) - parse_utc(row["published_at"])).total_seconds() / 3600
        enriched["release_age_days"] = enriched["release_age_hours"] / 24
        out.append(enriched)
        previous[row["asset_id"]] = row
    return out


def milestone_download_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["tag"], []).append(row)
    result = []
    for tag, items in grouped.items():
        item = {"tag": tag}
        published = parse_utc(items[0]["published_at"])
        for hours in MILESTONE_HOURS:
            cutoff = published.timestamp() + hours * 3600
            eligible = [r for r in items if parse_utc(r["collected_at"]).timestamp() <= cutoff]
            label = {24: "24h", 72: "72h", 168: "7d", 336: "14d"}[hours]
            item[label] = max((r["download_count"] for r in eligible), default=None)
        result.append(item)
    return result


def current_adoption_percentage(totals: list[dict[str, Any]]) -> float | None:
    stable = totals[:2]
    if len(stable) < 2 or stable[1]["downloads"] == 0:
        return None
    return stable[0]["downloads"] / stable[1]["downloads"] * 100
