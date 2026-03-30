#!/usr/bin/env python3
"""
journal_learner.py — corrections_log.jsonl 분석 후 저널별 파라미터 변경 제안 도구

사용법:
    python journal_learner.py --analyze   # 분석 + 제안 출력
    python journal_learner.py --apply     # 분석 + 파라미터 적용
    python journal_learner.py --dry-run   # 변경 내용 미리 보기 (적용 안 함)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# 경로 설정
# ─────────────────────────────────────────────────────────────────────────────
_LEARNING_DIR = Path(__file__).parent
_SCRIPTS_DIR  = _LEARNING_DIR.parent
_LOG_FILE     = _SCRIPTS_DIR / "learning_data" / "corrections_log.jsonl"

# parameter_store를 상대 import 없이 직접 사용하기 위해 경로 추가
sys.path.insert(0, str(_LEARNING_DIR))
import parameter_store as ps  # noqa: E402  (경로 삽입 후 import)


# ─────────────────────────────────────────────────────────────────────────────
# 1. JSONL 로드
# ─────────────────────────────────────────────────────────────────────────────
def load_corrections(log_path: Path | None = None) -> list[dict]:
    """corrections_log.jsonl의 모든 항목을 읽어 반환. 파일 없거나 비어 있으면 []."""
    path = log_path or _LOG_FILE
    if not path.exists():
        return []
    entries: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    entries = [*entries, json.loads(line)]
                except json.JSONDecodeError:
                    pass
    return entries


# ─────────────────────────────────────────────────────────────────────────────
# 2. 저널별 그룹화
# ─────────────────────────────────────────────────────────────────────────────
def group_by_journal(corrections: list[dict]) -> dict[str, list[dict]]:
    """journal_profile 키로 corrections를 그룹화해 반환."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for entry in corrections:
        jid = entry.get("journal_profile", "unknown")
        groups[jid] = [*groups[jid], entry]
    return dict(groups)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 규칙 엔진 헬퍼
# ─────────────────────────────────────────────────────────────────────────────
_FORMULA_RE = re.compile(r'\$\$?.+?\$?\$', re.DOTALL)
_CHAR_SUBST_RE = re.compile(r'^(.)$')  # 단일 문자 치환 감지용


def _flatten_corrections(entries: list[dict]) -> list[dict]:
    """각 로그 항목의 corrections 리스트를 하나로 병합."""
    result: list[dict] = []
    for entry in entries:
        paper_key = entry.get("paper_key", "")
        for c in entry.get("corrections", []):
            result = [*result, {**c, "_paper_key": paper_key}]
    return result


def _user_added_formula(c: dict) -> bool:
    """사용자가 수식을 추가한 formula_fix 항목인지 판단."""
    return (
        c.get("type") == "formula_fix"
        and bool(_FORMULA_RE.search(c.get("after", "")))
        and not bool(_FORMULA_RE.search(c.get("before", "")))
    )


def _user_removed_formula(c: dict) -> bool:
    """사용자가 수식을 제거한 formula_fix 항목인지 판단."""
    return (
        c.get("type") == "formula_fix"
        and bool(_FORMULA_RE.search(c.get("before", "")))
        and not bool(_FORMULA_RE.search(c.get("after", "")))
    )


def _extract_char_subst(c: dict) -> tuple[str, str] | None:
    """text_fix 항목에서 단일 문자 치환 패턴 추출. 없으면 None."""
    if c.get("type") != "text_fix":
        return None
    before, after = c.get("before", ""), c.get("after", "")
    if len(before) != len(after) or before == after:
        return None
    diffs = [(b, a) for b, a in zip(before, after) if b != a]
    if len(diffs) == 1:
        return diffs[0]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 4. 분석: 저널 단위 규칙 적용
# ─────────────────────────────────────────────────────────────────────────────
_THRESHOLD = 3  # 제안 발동 최소 횟수


def analyze_journal(journal_id: str, entries: list[dict]) -> list[dict]:
    """
    저널별 수정 항목에 규칙 엔진을 적용해 변경 제안 목록을 반환.
    반환 형식: [{"param": str, "action": str, "value": Any, "reason": str, "papers": list[str]}]
    """
    flat = _flatten_corrections(entries)
    proposals: list[dict] = []
    papers_involved = sorted({e.get("paper_key", "") for e in entries})

    # ── Rule 1: formula_miss — 사용자가 수식을 3번 이상 추가 → mfd_conf_override 낮추기
    formula_add_count = sum(1 for c in flat if _user_added_formula(c))
    if formula_add_count >= _THRESHOLD:
        current = (ps.get_journal_params(journal_id) or {}).get("mfd_conf_override")
        base = current if isinstance(current, float) else 0.45
        new_val = round(base - 0.05, 4)
        proposals = [*proposals, {
            "param": "mfd_conf_override",
            "action": "lower",
            "value": new_val,
            "reason": f"formula_miss: 사용자가 수식을 {formula_add_count}회 추가 → 신뢰도 임계값 낮춤 ({base} → {new_val})",
            "papers": papers_involved,
        }]

    # ── Rule 2: formula_false — 사용자가 수식을 3번 이상 제거 → mfd_conf_override 높이기
    formula_rm_count = sum(1 for c in flat if _user_removed_formula(c))
    if formula_rm_count >= _THRESHOLD:
        current = (ps.get_journal_params(journal_id) or {}).get("mfd_conf_override")
        base = current if isinstance(current, float) else 0.45
        new_val = round(base + 0.05, 4)
        proposals = [*proposals, {
            "param": "mfd_conf_override",
            "action": "raise",
            "value": new_val,
            "reason": f"formula_false: 사용자가 수식을 {formula_rm_count}회 제거 → 신뢰도 임계값 높임 ({base} → {new_val})",
            "papers": papers_involved,
        }]

    # ── Rule 3: boilerplate — 동일 텍스트 3번 이상 삭제 → extra_boilerplate_patterns 추가
    bp_texts: dict[str, int] = defaultdict(int)
    for c in flat:
        if c.get("type") == "boilerplate_removal" and c.get("before", "").strip():
            text = c["before"].strip()
            bp_texts[text] = bp_texts[text] + 1

    new_patterns: list[str] = []
    for text, cnt in bp_texts.items():
        if cnt >= _THRESHOLD:
            existing_params = ps.get_journal_params(journal_id) or {}
            existing_bp = existing_params.get("extra_boilerplate_patterns", [])
            if text not in existing_bp:
                new_patterns = [*new_patterns, text]

    if new_patterns:
        existing_params = ps.get_journal_params(journal_id) or {}
        existing_bp = existing_params.get("extra_boilerplate_patterns", [])
        merged = list({*existing_bp, *new_patterns})
        proposals = [*proposals, {
            "param": "extra_boilerplate_patterns",
            "action": "add",
            "value": merged,
            "reason": f"boilerplate: {len(new_patterns)}개 패턴이 {_THRESHOLD}회 이상 반복 삭제됨",
            "papers": papers_involved,
        }]

    # ── Rule 4: heading — 일관된 헤딩 변경 패턴 감지 → heading_remap 추가
    heading_map: dict[tuple[str, str], int] = defaultdict(int)
    for c in flat:
        if c.get("type") == "heading_change":
            before = c.get("before", "").strip()
            after  = c.get("after",  "").strip()
            if before and after and before != after:
                heading_map[(before, after)] = heading_map[(before, after)] + 1

    new_remap: dict[str, str] = {}
    for (before, after), cnt in heading_map.items():
        if cnt >= _THRESHOLD:
            existing_params = ps.get_journal_params(journal_id) or {}
            existing_remap = existing_params.get("heading_remap", {})
            if existing_remap.get(before) != after:
                new_remap = {**new_remap, before: after}

    if new_remap:
        existing_params = ps.get_journal_params(journal_id) or {}
        existing_remap  = existing_params.get("heading_remap", {})
        merged_remap    = {**existing_remap, **new_remap}
        proposals = [*proposals, {
            "param": "heading_remap",
            "action": "add",
            "value": merged_remap,
            "reason": f"heading: {len(new_remap)}개 헤딩 변경 패턴이 {_THRESHOLD}회 이상 반복됨",
            "papers": papers_involved,
        }]

    # ── Rule 5: text_char — 반복 단일 문자 치환 → postprocess_extras 추가
    char_subst: dict[tuple[str, str], int] = defaultdict(int)
    for c in flat:
        pair = _extract_char_subst(c)
        if pair:
            char_subst[pair] = char_subst[pair] + 1

    new_extras: list[dict] = []
    for (frm, to), cnt in char_subst.items():
        if cnt >= _THRESHOLD:
            rule = {"replace": frm, "with": to}
            existing_params = ps.get_journal_params(journal_id) or {}
            existing_extras = existing_params.get("postprocess_extras", [])
            if rule not in existing_extras:
                new_extras = [*new_extras, rule]

    if new_extras:
        existing_params = ps.get_journal_params(journal_id) or {}
        existing_extras = existing_params.get("postprocess_extras", [])
        merged_extras   = [*existing_extras, *new_extras]
        proposals = [*proposals, {
            "param": "postprocess_extras",
            "action": "add",
            "value": merged_extras,
            "reason": f"text_char: {len(new_extras)}개 문자 치환 규칙이 {_THRESHOLD}회 이상 반복됨",
            "papers": papers_involved,
        }]

    return proposals


# ─────────────────────────────────────────────────────────────────────────────
# 5. propose_changes (퍼블릭 API, journal_id 단위)
# ─────────────────────────────────────────────────────────────────────────────
def propose_changes(journal_id: str, entries: list[dict]) -> list[dict]:
    """저널 항목에서 파라미터 변경 제안 목록을 반환."""
    return analyze_journal(journal_id, entries)


# ─────────────────────────────────────────────────────────────────────────────
# 6. apply_changes
# ─────────────────────────────────────────────────────────────────────────────
def apply_changes(proposals: list[dict], dry_run: bool = True) -> None:
    """
    제안된 변경을 parameter_store에 적용.
    dry_run=True 이면 출력만 하고 실제 저장은 하지 않음.
    """
    if not proposals:
        print("  (변경 사항 없음)")
        return

    for p in proposals:
        journal_id = p.get("journal_id", "unknown")
        param      = p["param"]
        value      = p["value"]
        reason     = p["reason"]
        papers     = p.get("papers", [])

        if dry_run:
            print(f"  [dry-run] {journal_id}.{param} → {json.dumps(value, ensure_ascii=False)}")
            print(f"            이유: {reason}")
        else:
            ps.update_journal_param(
                journal_id=journal_id,
                param=param,
                value=value,
                reason=reason,
                papers=papers,
            )
            print(f"  적용됨: {journal_id}.{param} → {json.dumps(value, ensure_ascii=False)}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI 헬퍼
# ─────────────────────────────────────────────────────────────────────────────
def _run_analysis(corrections: list[dict]) -> list[dict]:
    """모든 저널에 대해 분석 실행, journal_id 포함된 전체 제안 목록 반환."""
    groups = group_by_journal(corrections)
    all_proposals: list[dict] = []
    for journal_id, entries in sorted(groups.items()):
        proposals = propose_changes(journal_id, entries)
        tagged = [{**p, "journal_id": journal_id} for p in proposals]
        all_proposals = [*all_proposals, *tagged]
    return all_proposals


def _print_proposals(all_proposals: list[dict]) -> None:
    if not all_proposals:
        print("제안된 변경 사항이 없습니다.")
        return
    by_journal: dict[str, list[dict]] = defaultdict(list)
    for p in all_proposals:
        by_journal[p["journal_id"]].append(p)
    for journal_id, props in sorted(by_journal.items()):
        print(f"\n[{journal_id}] — {len(props)}개 제안")
        for p in props:
            print(f"  • {p['param']} ({p['action']})")
            print(f"    이유: {p['reason']}")
            print(f"    값:   {json.dumps(p['value'], ensure_ascii=False)}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────────────────────
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="corrections_log.jsonl 분석 후 저널별 파라미터 변경 제안"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--analyze",  action="store_true", help="분석 결과 및 제안 출력")
    mode.add_argument("--apply",    action="store_true", help="분석 + 파라미터 실제 적용")
    mode.add_argument("--dry-run",  action="store_true", help="변경 내용 미리 보기 (적용 안 함)")
    parser.add_argument("--log", type=Path, default=None, help="corrections_log.jsonl 경로 (기본: 자동 탐색)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    corrections = load_corrections(args.log)
    if not corrections:
        print("수정 로그가 비어 있거나 파일을 찾을 수 없습니다.")
        print(f"  경로: {args.log or _LOG_FILE}")
        return 0

    print(f"로그 항목 수: {len(corrections)}")
    groups = group_by_journal(corrections)
    print(f"저널 프로필 수: {len(groups)}  ({', '.join(sorted(groups))})")

    all_proposals = _run_analysis(corrections)

    if args.analyze or args.dry_run:
        print("\n=== 파라미터 변경 제안 ===")
        _print_proposals(all_proposals)
        if args.dry_run:
            print("\n[dry-run 모드] 실제 변경은 적용되지 않았습니다.")

    elif args.apply:
        print("\n=== 파라미터 변경 제안 ===")
        _print_proposals(all_proposals)
        if all_proposals:
            print("\n=== 파라미터 적용 중 ===")
            apply_changes(all_proposals, dry_run=False)
            print("완료.")
        else:
            print("적용할 변경 사항이 없습니다.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
