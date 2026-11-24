"""Shared SQLite settings and collection coordination for the web UI and scheduler."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from config import comma_separated_env, hidden_release_tags
from db import utc_now_iso, upsert_repo

DEFAULT_INTERVAL = 21600
REPO_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Settings:
    repos: tuple[str, ...]
    excluded_tags: tuple[str, ...]
    interval: int
    collect_on_start: bool


def parse_repos(raw: str) -> tuple[str, ...]:
    repos = {}
    for value in raw.replace("\n", ",").split(","):
        name = value.strip()
        if not name:
            continue
        if not REPO_PATTERN.fullmatch(name) or name.split("/")[1] in {".", ".."}:
            raise ValueError(f"Invalid repository: {name}. Use owner/repo.")
        repos.setdefault(name.lower(), name)
    return tuple(repos.values())


def parse_tags(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(tag.strip() for tag in raw.replace("\n", ",").split(",") if tag.strip()))


def read_settings(conn: sqlite3.Connection) -> Settings:
    values = {row["key"]: json.loads(row["value"]) for row in conn.execute("SELECT key, value FROM app_settings")}
    return Settings(
        tuple(values["repos"]) if "repos" in values else tuple(comma_separated_env("REPOS")),
        tuple(values["excluded_tags"]) if "excluded_tags" in values else tuple(sorted(hidden_release_tags())),
        int(values["interval"]) if "interval" in values else int(os.getenv("RUN_INTERVAL_SECONDS", str(DEFAULT_INTERVAL))),
        bool(values["collect_on_start"]) if "collect_on_start" in values else os.getenv("COLLECT_ON_START", "true").lower() == "true",
    )


def save_settings(conn: sqlite3.Connection, settings: Settings) -> None:
    if not 60 <= settings.interval <= 7 * 86400:
        raise ValueError("Collection interval must be between 60 seconds and 7 days.")
    repos = parse_repos(",".join(settings.repos))
    tags = parse_tags(",".join(settings.excluded_tags))
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        old = read_settings(conn)
        # Retain stored history when monitoring is disabled; only cancel queued work.
        removed = set(old.repos) - set(repos)
        conn.executemany("UPDATE collection_status SET pending=0, next_due=NULL WHERE repo=?", [(r,) for r in removed])
        conn.executemany(
            "INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, json.dumps(value)) for key, value in {
                "repos": repos, "excluded_tags": tags, "interval": settings.interval,
                "collect_on_start": settings.collect_on_start,
            }.items()],
        )
        for repo in set(repos) - set(old.repos):
            _queue(conn, repo)


def _queue(conn: sqlite3.Connection, repo: str) -> None:
    conn.execute(
        """INSERT INTO collection_status(repo,pending) VALUES (?,1)
        ON CONFLICT(repo) DO UPDATE SET pending=1 WHERE running=0""", (repo,)
    )


def request_collection(conn: sqlite3.Connection, repo: str) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if repo not in read_settings(conn).repos:
            raise ValueError("Repository is not tracked.")
        _queue(conn, repo)


def collection_status(conn: sqlite3.Connection, repo: str) -> dict:
    row = conn.execute("SELECT * FROM collection_status WHERE repo=?", (repo,)).fetchone()
    return dict(row) if row else {}


def recover_interrupted(conn: sqlite3.Connection) -> None:
    """Called by the single scheduler on startup; interrupted work is retried."""
    with conn:
        conn.execute("UPDATE collection_status SET running=0, pending=1 WHERE running=1")


def pending_repos(conn: sqlite3.Connection, repos: Iterable[str]) -> set[str]:
    tracked = set(repos)
    return {row["repo"] for row in conn.execute("SELECT repo FROM collection_status WHERE pending=1 AND running=0")} & tracked


def start_collection(conn: sqlite3.Connection, repo: str) -> bool:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if repo not in read_settings(conn).repos:
            return False
        result = conn.execute(
            """INSERT INTO collection_status(repo,running,last_attempt) VALUES (?,1,?)
            ON CONFLICT(repo) DO UPDATE SET running=1,pending=0,last_attempt=excluded.last_attempt,error=NULL
            WHERE running=0""", (repo, utc_now_iso())
        )
        return result.rowcount == 1


def finish_collection(conn: sqlite3.Connection, repo: str, success: bool) -> None:
    with conn:
        conn.execute(
            """UPDATE collection_status SET running=0,
            last_success=CASE WHEN ? THEN ? ELSE last_success END,
            error=CASE WHEN ? THEN NULL ELSE 'Collection failed; see container logs.' END
            WHERE repo=?""", (success, utc_now_iso(), success, repo)
        )


def publish_due(conn: sqlite3.Connection, due: dict[str, tuple[datetime, datetime]]) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        tracked = set(read_settings(conn).repos)
        conn.executemany(
            """INSERT INTO collection_status(repo,next_due) VALUES (?,?)
            ON CONFLICT(repo) DO UPDATE SET next_due=excluded.next_due
            WHERE next_due IS NOT excluded.next_due""",
            [(repo, when[0].isoformat()) for repo, when in due.items() if repo in tracked],
        )


def save_annotation(conn: sqlite3.Connection, repo: str, occurred_at: str, title: str,
                    notes: str = "", url: str = "", tag: str = "", event_id: int | None = None) -> None:
    title = title.strip()
    if not title:
        raise ValueError("Enter an annotation title.")
    occurred = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    if occurred.tzinfo is None:
        raise ValueError("Use a timestamp with a timezone (e.g. 2026-07-01T12:00:00Z).")
    occurred_at = occurred.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if url and not url.startswith(("https://", "http://")):
        raise ValueError("Annotation URLs must start with http:// or https://.")
    with conn:
        repo_id = upsert_repo(conn, repo)
        release_id = None
        if tag:
            row = conn.execute("SELECT id FROM releases WHERE repo_id=? AND tag=?", (repo_id, tag)).fetchone()
            if not row:
                raise ValueError("Release does not belong to this repository.")
            release_id = row["id"]
        if event_id is None:
            conn.execute(
                "INSERT INTO events(repo_id,release_id,occurred_at,title,notes,url,created_at) VALUES (?,?,?,?,?,?,?)",
                (repo_id, release_id, occurred_at, title, notes, url, utc_now_iso()),
            )
        else:
            result = conn.execute(
                "UPDATE events SET release_id=?,occurred_at=?,title=?,notes=?,url=? WHERE id=? AND repo_id=?",
                (release_id, occurred_at, title, notes, url, event_id, repo_id),
            )
            if not result.rowcount:
                raise ValueError("Annotation no longer exists for this repository.")


def delete_annotation(conn: sqlite3.Connection, repo: str, event_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM events WHERE id=? AND repo_id=(SELECT id FROM repos WHERE full_name=?)", (event_id, repo))
