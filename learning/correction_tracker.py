#!/usr/bin/env python3
"""
correction_tracker.py — pipeline MD 출력과 사용자 수정본 간의 시맨틱 diff 캡처 도구

사용법:
    python correction_tracker.py <original_md> <corrected_md> --paper-key <key>

예시:
    python correction_tracker.py original.md corrected.md \\
        --paper-key A1_Adv_Funct_Mater_2024_Zero_Poisson
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# 경로 설정
# ─────────────────────────────────────────────────────────────────────────────
SCRIPTS_DIR      = Path(__file__).parent.parent
JOURNAL_PROFILES = SCRIPTS_DIR / "journal_profiles.json"
LOG_FILE         = SCRIPTS_DIR.parent / "scripts" / "learning_data" / "corrections_log.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# 저널 프로필 감지
# ─────────────────────────────────────────────────────────────────────────────
def load_journal_profiles() -> dict:
    """journal_profiles.json을 읽어 반환. 실패 시 빈 dict."""
    if not JOURNAL_PROFILES.exists():
        return {}
    with JOURNAL_PROFILES.open(encoding="utf-8") as f:
        data = json.load(f)
    return data.get("profiles", {})


def detect_journal_profile(paper_key: str, profiles: dict) -> str:
    """
    paper_key를 소문자로 변환해 각 프로필의 filename_keywords 및 known_papers와 매칭.
    매칭 없으면 'unknown' 반환.
    """
    key_lower = paper_key.lower()

    # known_papers 정확 매칭 우선
    for profile_id, profile in profiles.items():
        known = profile.get("known_papers", [])
        if paper_key in known:
            return profile_id

    # filename_keywords 부분 매칭
    for profile_id, profile in profiles.items():
        keywords = profile.get("match", {}).get("filename_keywords", [])
        for kw in keywords:
            if kw.lower() in key_lower:
                return profile_id

    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# diff 분류 헬퍼
# ─────────────────────────────────────────────────────────────────────────────
_FORMULA_RE  = re.compile(r'\$\$?.+?\$?\$', re.DOTALL)
_HEADING_RE  = re.compile(r'^#{1,6}\s')
_IMAGE_RE    = re.compile(r'!\[.*?\]\(.*?\)')


def _has_formula(line: str) -> bool:
    return bool(_FORMULA_RE.search(line))


def _is_heading(line: str) -> bool:
    return bool(_HEADING_RE.match(line))


def _has_image(line: str) -> bool:
    return bool(_IMAGE_RE.search(line))


def _classify_change(before: str, after: str | None) -> str:
    """
    단일 변경을 분류:
      - formula_fix       : 수식 포함 라인의 내용 변경
      - heading_change    : 헤딩 라인 변경
      - boilerplate_removal: after가 None (삭제된 라인)
      - image_reorder     : 이미지 참조 포함 라인 이동/변경
      - text_fix          : 기타 텍스트 변경
    """
    if after is None:
        return "boilerplate_removal"
    if _has_formula(before) or _has_formula(after):
        return "formula_fix"
    if _is_heading(before) or _is_heading(after):
        return "heading_change"
    if _has_image(before) or _has_image(after):
        return "image_reorder"
    return "text_fix"


# ─────────────────────────────────────────────────────────────────────────────
# 이미지 재순서 감지
# ─────────────────────────────────────────────────────────────────────────────
def _extract_image_refs(lines: list[str]) -> list[tuple[int, str]]:
    """(line_number, image_ref) 목록 반환."""
    return [
        (i + 1, match.group())
        for i, line in enumerate(lines)
        for match in [_IMAGE_RE.search(line)]
        if match
    ]


def _detect_image_reorders(
    orig_lines: list[str],
    corr_lines: list[str],
) -> list[dict]:
    """
    이미지 참조 순서가 바뀐 경우를 감지해 correction 항목 목록 반환.
    순서가 동일하면 빈 리스트.
    """
    orig_refs = [ref for _, ref in _extract_image_refs(orig_lines)]
    corr_refs = [ref for _, ref in _extract_image_refs(corr_lines)]

    if orig_refs == corr_refs:
        return []

    # 실제로 위치가 바뀐 공통 참조만 기록
    orig_positions = {ref: idx for idx, ref in enumerate(orig_refs)}
    corr_positions = {ref: idx for idx, ref in enumerate(corr_refs)}

    reorders = []
    for ref in set(orig_refs) & set(corr_positions):
        if orig_positions[ref] != corr_positions[ref]:
            reorders = reorders + [{
                "type": "image_reorder",
                "line": corr_positions[ref] + 1,
                "before": f"position {orig_positions[ref] + 1}: {ref}",
                "after": f"position {corr_positions[ref] + 1}: {ref}",
            }]

    return reorders


# ─────────────────────────────────────────────────────────────────────────────
# 메인 diff 계산
# ─────────────────────────────────────────────────────────────────────────────
def compute_diff(orig_lines: list[str], corr_lines: list[str]) -> list[dict]:
    """
    LCS 기반 라인 diff를 수행해 변경 항목 목록을 반환.
    불변 패턴: 새 리스트만 생성, 기존 데이터 변형 없음.
    """
    # DP 테이블 구축 (LCS)
    n, m = len(orig_lines), len(corr_lines)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if orig_lines[i - 1] == corr_lines[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    # 역추적으로 변경 추출
    changes: list[dict] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and orig_lines[i - 1] == corr_lines[j - 1]:
            i -= 1
            j -= 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            # 추가된 라인 (before 없음 → text_fix 또는 formula_fix)
            after_line = corr_lines[j - 1].strip()
            change_type = _classify_change(after_line, after_line)
            changes = [{
                "type": change_type,
                "line": j,
                "before": "",
                "after": after_line,
            }] + changes
            j -= 1
        else:
            # 삭제되거나 변경된 라인
            before_line = orig_lines[i - 1].strip()
            # 다음 corr 라인이 수정본이라면 pair로 묶기
            if j > 0 and orig_lines[i - 1].strip() != corr_lines[j - 1].strip():
                after_line = corr_lines[j - 1].strip()
                change_type = _classify_change(before_line, after_line)
                changes = [{
                    "type": change_type,
                    "line": i,
                    "before": before_line,
                    "after": after_line,
                }] + changes
                i -= 1
                j -= 1
            else:
                change_type = _classify_change(before_line, None)
                changes = [{
                    "type": change_type,
                    "line": i,
                    "before": before_line,
                    "after": "",
                }] + changes
                i -= 1

    # image_reorder는 별도 감지로 교체 (위 LCS가 놓친 순서 변경 보완)
    image_reorders = _detect_image_reorders(orig_lines, corr_lines)
    non_image = [c for c in changes if c["type"] != "image_reorder"]
    return non_image + image_reorders


# ─────────────────────────────────────────────────────────────────────────────
# 카운트 집계
# ─────────────────────────────────────────────────────────────────────────────
CORRECTION_TYPES = (
    "formula_fix",
    "heading_change",
    "boilerplate_removal",
    "image_reorder",
    "text_fix",
)


def count_corrections(corrections: list[dict]) -> dict:
    """각 타입별 카운트를 반환 (불변 dict 생성)."""
    base = {t: 0 for t in CORRECTION_TYPES}
    counts = dict(base)
    for c in corrections:
        t = c["type"]
        if t in counts:
            counts = {**counts, t: counts[t] + 1}
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# JSONL 기록
# ─────────────────────────────────────────────────────────────────────────────
def append_log_entry(entry: dict) -> None:
    """corrections_log.jsonl에 한 줄 추가."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def build_log_entry(
    paper_key: str,
    journal_profile: str,
    corrections: list[dict],
    counts: dict,
) -> dict:
    """로그 항목 dict를 새로 생성해 반환 (불변 패턴)."""
    return {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "paper_key": paper_key,
        "journal_profile": journal_profile,
        "corrections": corrections,
        "correction_counts": counts,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 요약 출력
# ─────────────────────────────────────────────────────────────────────────────
def print_summary(paper_key: str, journal_profile: str, counts: dict) -> None:
    total = sum(counts.values())
    print(f"\n=== Correction Summary ===")
    print(f"  paper_key      : {paper_key}")
    print(f"  journal_profile: {journal_profile}")
    print(f"  total changes  : {total}")
    print()
    for t in CORRECTION_TYPES:
        n = counts[t]
        if n > 0:
            print(f"  {t:<24}: {n}")
    print(f"\n  logged to: {LOG_FILE}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI 진입점
# ─────────────────────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="pipeline MD 출력과 사용자 수정본 간의 시맨틱 diff 캡처"
    )
    parser.add_argument("original_md",  type=Path, help="원본 MD 파일 경로")
    parser.add_argument("corrected_md", type=Path, help="수정된 MD 파일 경로")
    parser.add_argument("--paper-key",  required=True, help="논문 키 (예: A1_Adv_Funct_Mater_2024_Zero_Poisson)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.original_md.exists():
        print(f"오류: 원본 파일을 찾을 수 없음: {args.original_md}", file=sys.stderr)
        return 1
    if not args.corrected_md.exists():
        print(f"오류: 수정 파일을 찾을 수 없음: {args.corrected_md}", file=sys.stderr)
        return 1

    orig_lines = args.original_md.read_text(encoding="utf-8").splitlines()
    corr_lines = args.corrected_md.read_text(encoding="utf-8").splitlines()

    profiles        = load_journal_profiles()
    journal_profile = detect_journal_profile(args.paper_key, profiles)
    corrections     = compute_diff(orig_lines, corr_lines)
    counts          = count_corrections(corrections)
    entry           = build_log_entry(args.paper_key, journal_profile, corrections, counts)

    append_log_entry(entry)
    print_summary(args.paper_key, journal_profile, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
