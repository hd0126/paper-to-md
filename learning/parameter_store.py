"""
parameter_store.py — Per-journal learned parameter store.

Reads and writes scripts/learning_data/journal_params.json.
All mutations return new dicts (immutable pattern).
"""

from __future__ import annotations

import json
import pathlib
from datetime import date
from typing import Any

_PARAMS_PATH = pathlib.Path(__file__).parent.parent / "learning_data" / "journal_params.json"

_DEFAULT_PARAMS: dict[str, Any] = {
    "mfd_conf_override": None,          # float or None (use dynamic default)
    "extra_boilerplate_patterns": [],
    "heading_remap": {},
    "postprocess_extras": [],
    "history": [],
}

_META_KEYS = {"_version", "_auto_generated", "_last_updated", "_description"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load() -> dict:
    if not _PARAMS_PATH.exists():
        return {}
    with _PARAMS_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(data: dict) -> None:
    _PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PARAMS_PATH.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def _today() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_default_params() -> dict:
    """Return a fresh copy of the default parameter template."""
    return {
        **_DEFAULT_PARAMS,
        "extra_boilerplate_patterns": list(_DEFAULT_PARAMS["extra_boilerplate_patterns"]),
        "heading_remap": dict(_DEFAULT_PARAMS["heading_remap"]),
        "postprocess_extras": list(_DEFAULT_PARAMS["postprocess_extras"]),
        "history": [],
    }


def get_journal_params(journal_id: str) -> dict | None:
    """Return params for *journal_id*, or None if not present."""
    data = _load()
    return data.get(journal_id)


def update_journal_param(
    journal_id: str,
    param: str,
    value: Any,
    reason: str,
    papers: list[str],
) -> None:
    """Update *param* for *journal_id*, appending a history entry.

    Creates the journal entry with default params if it does not exist yet.
    """
    data = _load()

    existing = data.get(journal_id, get_default_params())
    old_value = existing.get(param)

    history_entry = {
        "date": _today(),
        "param": param,
        "old": old_value,
        "new": value,
        "reason": reason,
        "papers": list(papers),
    }

    updated_journal = {
        **existing,
        param: value,
        "history": [*existing.get("history", []), history_entry],
    }

    new_data = {
        **data,
        journal_id: updated_journal,
        "_last_updated": _today(),
    }

    _save(new_data)


def list_overrides() -> dict:
    """Return all journal overrides (excludes metadata keys)."""
    data = _load()
    return {k: v for k, v in data.items() if k not in _META_KEYS}
