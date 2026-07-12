"""Configuration helpers for release analytics."""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE pairs from a .env file without overriding the environment."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def comma_separated_env(name: str, fallback_name: str | None = None) -> list[str]:
    """Return a comma/newline-separated environment value as a clean list."""
    raw_value = os.getenv(name)
    if (raw_value is None or not raw_value.strip()) and fallback_name:
        raw_value = os.getenv(fallback_name)
    if not raw_value:
        return []
    return [item.strip() for chunk in raw_value.splitlines() for item in chunk.split(",") if item.strip()]


def hidden_release_tags() -> set[str]:
    """Release tags that should be excluded from dashboard metrics and charts."""
    return set(comma_separated_env("EXCLUDED_RELEASE_TAGS", fallback_name="HIDDEN_RELEASES"))
