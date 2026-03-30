"""regression_guard.py — Regression guard for learned journal parameters.

Usage:
  python regression_guard.py --freeze              # Freeze current scores as baseline
  python regression_guard.py --check <journal_id>  # Check regression for a journal
  python regression_guard.py --status              # Show baseline status
"""
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime
from typing import Any

_SCRIPTS_DIR   = pathlib.Path(__file__).parent.parent
_VAULT_ROOT    = _SCRIPTS_DIR.parent
_SCORES_FILE   = _VAULT_ROOT / "_Inbox-Papers" / "converted" / "benchmark_scores.json"
_BASELINE_FILE = _SCRIPTS_DIR / "learning_data" / "regression_baseline.json"
_PROFILES_FILE = _SCRIPTS_DIR / "journal_profiles.json"


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _best_total(entries: dict[str, Any]) -> float | None:
    totals = [v["total"] for v in entries.values() if isinstance(v, dict) and v.get("total") is not None]
    return max(totals) if totals else None


def freeze_baseline(scores_file: pathlib.Path | None = None) -> dict:
    """Read benchmark_scores.json and save as regression_baseline.json. Returns snapshot."""
    scores = _load(scores_file if scores_file is not None else _SCORES_FILE)
    snapshot = {
        "_frozen_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_source": str(scores_file or _SCORES_FILE),
        "papers": {
            key: _best_total(entries)
            for key, entries in scores.items()
            if not key.startswith("_")
        },
    }
    _save(_BASELINE_FILE, snapshot)
    return snapshot


def get_baseline() -> dict:
    """Return contents of regression_baseline.json."""
    return _load(_BASELINE_FILE)


def check_regression(
    journal_profile_id: str,
    tolerance: float = -0.5,
) -> tuple[bool, list[dict]]:
    """Compare current scores vs baseline for all known_papers of a journal profile.

    Returns (passed, details). passed=False when any paper drops beyond tolerance.
    """
    profiles = _load(_PROFILES_FILE)
    profile = profiles.get("profiles", {}).get(journal_profile_id)
    if profile is None:
        raise ValueError(f"Journal profile not found: {journal_profile_id!r}")

    baseline_papers: dict[str, Any] = get_baseline().get("papers", {})
    current_scores = _load(_SCORES_FILE)
    passed = True
    details: list[dict] = []

    for paper_key in profile.get("known_papers", []):
        baseline_score = baseline_papers.get(paper_key)
        current_score  = _best_total(current_scores.get(paper_key, {}))

        if baseline_score is None or current_score is None:
            status, delta, regressed = "no_baseline" if baseline_score is None else "no_current_score", None, False
        else:
            delta     = round(current_score - baseline_score, 3)
            regressed = delta < tolerance
            status    = "regressed" if regressed else "ok"

        if regressed:
            passed = False

        details.append({"paper_key": paper_key, "baseline": baseline_score,
                         "current": current_score, "delta": delta, "status": status})

    return (passed, details)


def _cmd_freeze() -> None:
    snap = freeze_baseline()
    print(f"Baseline frozen: {len(snap['papers'])} papers at {snap['_frozen_at']}")
    print(f"Saved to: {_BASELINE_FILE}")


def _cmd_check(journal_id: str) -> None:
    try:
        passed, details = check_regression(journal_id)
    except ValueError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)

    print(f"Regression check: {journal_id}\n")
    print(f"{'Paper':<48} {'Baseline':>9} {'Current':>9} {'Delta':>7}  Status")
    print("-" * 80)
    for d in details:
        b = f"{d['baseline']:.2f}" if d["baseline"] is not None else "—"
        c = f"{d['current']:.2f}"  if d["current"]  is not None else "—"
        dd = f"{d['delta']:+.3f}" if d["delta"] is not None else "—"
        print(f"{d['paper_key']:<48} {b:>9} {c:>9} {dd:>7}  {d['status']}")

    print(f"\nResult: {'PASSED' if passed else 'FAILED (regression detected)'}")
    raise SystemExit(0 if passed else 1)


def _cmd_status() -> None:
    baseline = get_baseline()
    if not baseline:
        print("No baseline frozen yet. Run --freeze first.")
        return
    papers = baseline.get("papers", {})
    print(f"Frozen at : {baseline.get('_frozen_at', 'unknown')}")
    print(f"Source    : {baseline.get('_source', 'unknown')}")
    print(f"Papers    : {len(papers)}")
    for key, score in sorted(papers.items()):
        print(f"  {key:<50} {f'{score:.2f}' if score is not None else '—':>9}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Regression guard for learned journal parameters.")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--freeze", action="store_true", help="Freeze current scores as baseline")
    grp.add_argument("--check",  metavar="JOURNAL_ID", help="Check regression for a journal profile")
    grp.add_argument("--status", action="store_true",  help="Show baseline status")
    args = parser.parse_args()

    if args.freeze:
        _cmd_freeze()
    elif args.check:
        _cmd_check(args.check)
    elif args.status:
        _cmd_status()


if __name__ == "__main__":
    main()
