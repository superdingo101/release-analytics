"""Collect GitHub release asset download snapshots."""
from __future__ import annotations

import argparse
import logging
import os
from typing import Any

import requests
from config import load_env

from db import DB_PATH, connect, init_db, insert_snapshot, upsert_asset, upsert_release, upsert_repo, utc_now_iso

API = "https://api.github.com/repos/{repo}/releases"
log = logging.getLogger(__name__)


def configured_repos() -> list[str]:
    raw = os.getenv("REPOS", "")
    return [repo.strip() for repo in raw.replace("\n", ",").split(",") if repo.strip()]


def fetch_releases(repo: str, token: str | None = None) -> list[dict[str, Any]]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    releases: list[dict[str, Any]] = []
    url: str | None = API.format(repo=repo)
    params: dict[str, int] | None = {"per_page": 100}
    while url:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code >= 400:
            log.error("GitHub API error for %s: %s %s", repo, response.status_code, response.text[:500])
            response.raise_for_status()
        releases.extend(response.json())
        url = response.links.get("next", {}).get("url")
        params = None
    return releases


def collect_repo(repo: str, db_path: str = DB_PATH, include_drafts: bool = False) -> None:
    token = os.getenv("GITHUB_TOKEN")
    collected_at = utc_now_iso()
    releases = fetch_releases(repo, token)
    with connect(db_path) as conn:
        init_db(conn)
        repo_id = upsert_repo(conn, repo)
        for release in releases:
            if release.get("draft") and not include_drafts:
                log.info("Skipping draft release %s %s", repo, release.get("tag_name"))
                continue
            published_at = release.get("published_at") or release.get("created_at")
            existing_release = conn.execute(
                "SELECT id FROM releases WHERE repo_id = ? AND tag = ?",
                (repo_id, release["tag_name"]),
            ).fetchone()
            release_id = upsert_release(
                conn,
                repo_id,
                release["tag_name"],
                published_at,
                bool(release.get("prerelease")),
                bool(release.get("draft")),
                release.get("html_url", ""),
            )
            is_new_release = existing_release is None
            for asset in release.get("assets", []):
                asset_id = upsert_asset(conn, release_id, int(asset["id"]), asset["name"])
                if is_new_release:
                    insert_snapshot(conn, asset_id, published_at, 0)
                insert_snapshot(conn, asset_id, collected_at, int(asset.get("download_count", 0)))
                log.info("Stored %s %s %s=%s", repo, release["tag_name"], asset["name"], asset.get("download_count", 0))
        conn.commit()


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description="Collect GitHub release download snapshots")
    parser.add_argument("--db", default=os.getenv("DB_PATH", DB_PATH))
    parser.add_argument("--repo", action="append", help="owner/name repo; can be repeated")
    parser.add_argument("--include-drafts", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    repos = args.repo or configured_repos()
    if not repos:
        raise SystemExit("No repositories configured. Set REPOS or pass --repo owner/name.")
    for repo in repos:
        try:
            collect_repo(repo, args.db, args.include_drafts)
        except requests.HTTPError:
            log.exception("Failed to collect %s", repo)


if __name__ == "__main__":
    main()
