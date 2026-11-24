"""Release-time-aligned collector scheduling."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable


from collector import collect_repo
from config import load_env
from db import DB_PATH, connect, init_db, parse_utc
from contextlib import closing
from management import (read_settings, recover_interrupted, pending_repos,
                        start_collection, finish_collection, publish_due)

log = logging.getLogger(__name__)
DEFAULT_INTERVAL_SECONDS = 21600
DEFAULT_DUE_WINDOW_SECONDS = 300
MIN_SLEEP_SECONDS = 1
MAX_SLEEP_SECONDS = 24 * 3600


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def next_aligned_slot(anchor: datetime, now: datetime, interval_seconds: int, due_window_seconds: int = 0) -> datetime:
    """Return the release-aligned slot to collect for.

    Slots are ``anchor + N * interval_seconds``. A slot that is just due is
    returned as ``now`` so scheduler wake-up jitter does not skip it.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    anchor = _utc(anchor)
    now = _utc(now)
    elapsed = (now - anchor).total_seconds()
    if elapsed < 0:
        return anchor
    remainder = elapsed % interval_seconds
    if remainder == 0 or 0 < remainder <= due_window_seconds:
        return now
    return now + timedelta(seconds=(interval_seconds - remainder))


def next_aligned_slot_with_nominal(anchor: datetime, now: datetime, interval_seconds: int, due_window_seconds: int = 0) -> tuple[datetime, datetime]:
    """Return the actual run time and nominal aligned slot."""
    anchor = _utc(anchor)
    now = _utc(now)
    run_at = next_aligned_slot(anchor, now, interval_seconds, due_window_seconds)
    if run_at == now:
        elapsed = max(0, (now - anchor).total_seconds())
        n = int(elapsed // interval_seconds)
        nominal = anchor + timedelta(seconds=n * interval_seconds)
    else:
        nominal = run_at
    return run_at, nominal


def next_run_time(anchors: Iterable[datetime], now: datetime, interval_seconds: int, fallback_seconds: int | None = None, due_window_seconds: int = 0) -> datetime:
    """Choose the earliest upcoming aligned slot, or fixed fallback delay."""
    anchors = list(anchors)
    now = _utc(now)
    if not anchors:
        return now + timedelta(seconds=fallback_seconds or interval_seconds)
    return min(next_aligned_slot(anchor, now, interval_seconds, due_window_seconds) for anchor in anchors)


def next_due_by_repo(
    repos: Iterable[str],
    anchors_by_repo: dict[str, datetime],
    now: datetime,
    interval_seconds: int,
    fallback_due_by_repo: dict[str, datetime] | None = None,
    last_nominal_by_repo: dict[str, datetime] | None = None,
    due_window_seconds: int = 0,
) -> dict[str, tuple[datetime, datetime]]:
    """Calculate each repo's next due time independently.

    The returned mapping is ``repo -> (due_at, nominal_slot)``. Repos with no
    release anchor use their existing fallback due time, or ``now + interval``
    when no fallback has been scheduled yet.
    """
    now = _utc(now)
    fallback_due_by_repo = fallback_due_by_repo or {}
    last_nominal_by_repo = last_nominal_by_repo or {}
    due: dict[str, tuple[datetime, datetime]] = {}
    for repo in [repo for repo in repos if repo.strip()]:
        anchor = anchors_by_repo.get(repo)
        if anchor is None:
            fallback_due = fallback_due_by_repo.get(repo, now + timedelta(seconds=interval_seconds))
            fallback_due = _utc(fallback_due)
            due[repo] = (fallback_due, fallback_due)
            continue

        due_at, nominal = next_aligned_slot_with_nominal(anchor, now, interval_seconds, due_window_seconds)
        last_nominal = last_nominal_by_repo.get(repo)
        if last_nominal is not None and nominal <= last_nominal and due_at <= now:
            due_at = next_aligned_slot(anchor, last_nominal + timedelta(seconds=1), interval_seconds, due_window_seconds=0)
            nominal = due_at
        due[repo] = (due_at, nominal)
    return due


def due_repos(next_due: dict[str, tuple[datetime, datetime]], now: datetime, due_window_seconds: int) -> list[str]:
    """Return repos whose independent due time is ready in this wake cycle."""
    ready_at = _utc(now) + timedelta(seconds=due_window_seconds)
    return [repo for repo, (due_at, _) in next_due.items() if due_at <= ready_at]


def latest_release_anchors(db_path: str, repos: Iterable[str], *, initialize: bool = True) -> dict[str, datetime]:
    """Load the latest non-draft release published_at for each configured repo."""
    repo_names = [repo.strip() for repo in repos if repo.strip()]
    if not repo_names:
        return {}
    with closing(connect(db_path)) as conn:
        if initialize:
            init_db(conn)
        rows = conn.execute(
            f"""
            SELECT rp.full_name, MAX(r.published_at) AS published_at
            FROM repos rp
            JOIN releases r ON r.repo_id = rp.id
            WHERE r.draft = 0 AND rp.full_name IN ({','.join('?' for _ in repo_names)})
            GROUP BY rp.full_name
            """,
            repo_names,
        ).fetchall()
    return {row["full_name"]: parse_utc(row["published_at"]) for row in rows if row["published_at"]}


def collect_repos(repos: Iterable[str], db_path: str) -> None:
    for repo in repos:
        try:
            collect_repo(repo, db_path)
        except Exception:
            log.exception("Failed to collect %s", repo)


def collect_managed_repo(conn, repo: str, db_path: str) -> bool:
    """Run one collection through the scheduler, never through a web request."""
    if not start_collection(conn, repo):
        return False
    success = False
    try:
        collect_repo(repo, db_path)
        success = True
    except Exception:
        log.exception("Failed to collect %s", repo)
    finally:
        finish_collection(conn, repo, success)
    return True


SETTINGS_POLL_SECONDS = 5


def scheduler_tick(conn, db_path, now, settings, fallback, last_nominal, previous_settings, outstanding):
    """One scheduler iteration; return the current settings for the next tick."""
    interval = settings.interval
    repos = settings.repos
    if previous_settings is not None and previous_settings.interval != interval:
        fallback.clear()
        last_nominal.clear()
        outstanding.clear()
    for repo in set(fallback) - set(repos):
        fallback.pop(repo, None)
    for repo in set(last_nominal) - set(repos):
        last_nominal.pop(repo, None)
    anchors = latest_release_anchors(db_path, repos, initialize=False)
    for repo in list(outstanding):
        if repo not in repos or outstanding[repo][0] != anchors.get(repo):
            outstanding.pop(repo)
    for repo in repos:
        if repo not in anchors:
            fallback.setdefault(repo, now + timedelta(seconds=interval))
    due_window = min(int(os.getenv("SCHEDULE_DUE_WINDOW_SECONDS", str(DEFAULT_DUE_WINDOW_SECONDS))), interval - 1)
    next_due = next_due_by_repo(repos, anchors, now, interval, fallback, last_nominal, due_window)
    # Keep the previously announced slot until it is processed, even if a
    # polling tick or a slow manual collection crosses the due window.
    for repo, slot in next_due.items():
        if repo in outstanding:
            next_due[repo] = outstanding[repo][1]
        else:
            outstanding[repo] = (anchors.get(repo), slot)
    publish_due(conn, next_due)
    scheduled = set(due_repos(next_due, now, 0))
    requested = pending_repos(conn, repos)
    if previous_settings is None and settings.collect_on_start:
        requested.update(repos)
    for repo in repos:
        if repo not in scheduled and repo not in requested:
            continue
        if collect_managed_repo(conn, repo, db_path):
            if repo in scheduled:
                last_nominal[repo] = next_due[repo][1]
                outstanding.pop(repo, None)
            if repo not in anchors and repo in scheduled:
                fallback[repo] = now + timedelta(seconds=interval)
    return settings


def run_collector_loop() -> None:
    load_env()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    db_path = os.getenv("DB_PATH", DB_PATH)
    fallback: dict[str, datetime] = {}
    last_nominal: dict[str, datetime] = {}
    previous_settings = None
    outstanding: dict[str, tuple[datetime | None, tuple[datetime, datetime]]] = {}
    with closing(connect(db_path)) as conn:
        init_db(conn)
        recover_interrupted(conn)
        while True:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            settings = read_settings(conn)
            previous_settings = scheduler_tick(conn, db_path, now, settings, fallback, last_nominal, previous_settings, outstanding)
            time.sleep(SETTINGS_POLL_SECONDS)


if __name__ == "__main__":
    run_collector_loop()
