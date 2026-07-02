"""Import pasted historical release download snapshots."""
from __future__ import annotations

import argparse
import logging
import hashlib
import re
import sys

from db import DB_PATH, connect, init_db, insert_snapshot, upsert_asset, upsert_release, upsert_repo

log = logging.getLogger(__name__)
TIME_RE = re.compile(r"Current UTC time:\s*(?P<time>\S+)")
LINE_RE = re.compile(r"^(?P<tag>\S+)\s+(?P<published>\S+)\s+(?P<assets>.+)$")
ASSET_RE = re.compile(r"(?P<name>[\w.\-+]+)=(?P<count>\d+)")


def synthetic_asset_id(repo: str, tag: str, name: str) -> int:
    digest = hashlib.sha256(f"{repo}:{tag}:{name}".encode()).hexdigest()
    return int(digest[:12], 16)


def parse_history(text: str) -> tuple[str, list[dict[str, object]]]:
    collected_at: str | None = None
    snapshots: list[dict[str, object]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("`")
        if not line:
            continue
        time_match = TIME_RE.search(line)
        if time_match:
            collected_at = time_match.group("time")
            continue
        match = LINE_RE.match(line)
        if not match:
            continue
        for asset in ASSET_RE.finditer(match.group("assets")):
            snapshots.append(
                {
                    "tag": match.group("tag"),
                    "published_at": match.group("published"),
                    "asset_name": asset.group("name"),
                    "download_count": int(asset.group("count")),
                }
            )
    if not collected_at:
        raise ValueError("Could not find 'Current UTC time: ...' line")
    return collected_at, snapshots


def import_history(repo: str, text: str, db_path: str = DB_PATH) -> int:
    collected_at, snapshots = parse_history(text)
    with connect(db_path) as conn:
        init_db(conn)
        repo_id = upsert_repo(conn, repo)
        for snap in snapshots:
            release_id = upsert_release(conn, repo_id, str(snap["tag"]), str(snap["published_at"]), False, False, "")
            asset_id = upsert_asset(conn, release_id, synthetic_asset_id(repo, str(snap["tag"]), str(snap["asset_name"])), str(snap["asset_name"]))
            insert_snapshot(conn, asset_id, collected_at, int(snap["download_count"]))
        conn.commit()
    return len(snapshots)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import pasted historical snapshots from stdin")
    parser.add_argument("--repo", required=True, help="owner/name repo for the pasted data")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    count = import_history(args.repo, sys.stdin.read(), args.db)
    log.info("Imported %s snapshot rows", count)


if __name__ == "__main__":
    main()
