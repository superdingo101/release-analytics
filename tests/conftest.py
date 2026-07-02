"""Pytest configuration for local module imports and optional dependency shims."""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    def _missing_get(*args, **kwargs):
        raise RuntimeError("requests is not installed; tests should monkeypatch requests.get")

    sys.modules["requests"] = types.SimpleNamespace(get=_missing_get, HTTPError=RuntimeError)
