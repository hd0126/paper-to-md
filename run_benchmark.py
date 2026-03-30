#!/usr/bin/env python3
"""
Docling Hybrid 파이프라인 객관적 벤치마크

100점 만점의 완전 자동화 평가 — 주관적 판단 없음.

점수 구성 (100점):
  수식 처리   30점  — MFD 감지 대비 삽입률, LaTeX 유효성, not-decoded 페널티
  이미지 정확도 20점  — Figure 수 정확도, 캡션 커버리지
  텍스트 품질  20점  — 아티팩트 없음 (Unicode, 리가처, PUA, 보일러플레이트)
  문서 구조   15점  — 필수 헤딩, 캡션 위치, 미매핑 수식 섹션
  처리 속도   15점  — 벽시계 시간 기준 Step 함수

실행:
  # Ground Truth 기반 점수만 (속도 제외):
  python scripts/run_benchmark.py <paper_key> <md_file>

  # 속도 포함 (직접 타이밍 입력):
  python scripts/run_benchmark.py <paper_key> <md_file> --elapsed 22.6

  # 전체 실행 + 채점 (파이프라인 직접 실행):
  python scripts/run_benchmark.py <paper_key> <pdf_file> --run-pipeline

예시:
  python scripts/run_benchmark.py A1_Adv_Funct_Mater_2024_Zero_Poisson \\
      _Inbox-Papers/hybrid_test/v4_inline/A1_Adv_Funct_Mater_2024_Zero_Poisson_Hybrid_Full.md \\
      --elapsed 22.6

사용 환경: mineru venv (또는 기본 Python 3.11+)
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# 경로 설정
# ─────────────────────────────────────────────────────────────────────────────
VAULT_ROOT   = Path(__file__).parent.parent
GT_FILE      = Path(__file__).parent / "benchmark_groundtruth.json"
SCORES_FILE  = VAULT_ROOT / "_Inbox-Papers" / "converted" / "benchmark_scores.json"


# ─────────────────────────────────────────────────────────────────────────────
# LaTeX 유효성 검사 (경량, 컴파일 없이)
# ─────────────────────────────────────────────────────────────────────────────
def _latex_valid(latex: str) -> bool:
    """LaTeX 문법 유효성 경량 검사: 중괄호 균형 + 환경 짝"""
    # \begin{X} → {, \end{X} → } 로 치환해서 균형 확인
    s = re.sub(r'\\begin\{[^}]+\}', '{', latex)
    s = re.sub(r'\\end\{[^}]+\}', '}', s)
    depth = 0
    for c in s:
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
        if depth < 0:
            return False
    return depth == 0


# ─────────────────────────────────────────────────────────────────────────────
# 마크다운 파싱 유틸
# ─────────────────────────────────────────────────────────────────────────────
def parse_md(md: str) -> dict:
    """마크다운에서 벤치마크 측정에 필요한 요소 추출."""
    result = {}

    # ── 수식 ──────────────────────────────────────────────────────────────────
    # display ($$...$$)
    display_formulas = re.findall(r'\$\$([\s\S]+?)\$\$', md)
    # inline ($...$, $$는 제외)
    inline_formulas  = re.findall(r'(?<!\$)\$(?!\$)([^$\n]+?)(?<!\$)\$(?!\$)', md)
    not_decoded      = md.count('formula-not-decoded')

    # 미매핑 수식 fallback 섹션 (display)
    fallback_section = '📐 수식 (위치 미매핑)' in md
    fallback_pages   = re.findall(r'^### Page \d+', md, re.MULTILINE)

    # LaTeX 유효성
    display_valid = sum(1 for l in display_formulas if _latex_valid(l))
    inline_valid  = sum(1 for l in inline_formulas  if _latex_valid(l))

    # 수식 위치 품질: 앞뒤가 단어 중간이 아닌 경우 (공백/구두점 경계)
    good_ctx = 0
    for m in re.finditer(r'(?<!\$)\$(?!\$)[^$\n]+?(?<!\$)\$(?!\$)', md):
        s, e = m.start(), m.end()
        before = md[s-1] if s > 0 else ' '
        after  = md[e]   if e < len(md) else ' '
        if before in ' \n,.(;:[' and after in ' \n,.)%;:]':
            good_ctx += 1

    result['formula'] = {
        'n_display':      len(display_formulas),
        'n_inline':       len(inline_formulas),
        'not_decoded':    not_decoded,
        'display_valid':  display_valid,
        'inline_valid':   inline_valid,
        'good_ctx':       good_ctx,
        'fallback_section': fallback_section,
        'n_fallback_display': len(fallback_pages),
    }

    # ── 이미지 ────────────────────────────────────────────────────────────────
    n_images   = len(re.findall(r'!\[.*?\]\(', md))
    # 캡션: 이미지 다음 단락에 * (italic) 또는 > (blockquote) 시작 (Fig/Figure)
    cap_blocks = re.findall(
        r'!\[.*?\]\([^)]+\)\n{1,3}(?:[*>])\s*((?:Fig|Figure|Extended Data)[^\n]+)',
        md, re.IGNORECASE
    )
    result['image'] = {
        'n_images':        n_images,
        'n_captions_near': len(cap_blocks),
    }

    # ── 텍스트 품질 아티팩트 ───────────────────────────────────────────────────
    artifacts = {
        'u_escape':  len(re.findall(r'/u[0-9A-Fa-f]{5}', md))
                   + len(re.findall(r'/u[0-9A-Fa-f]{4}', md)),
        'ligature':  len(re.findall(r'/uniFB[0-9a-f]+', md)),
        'pua':       sum(1 for c in md if '\uf000' <= c <= '\uf0ff'),
        'um_space':  len(re.findall(r'µ [mMlLsSnNgGΩ](?=[^a-zA-Z])', md)),
    }
    result['text'] = artifacts

    # ── 구조 ──────────────────────────────────────────────────────────────────
    headings_h2plus = re.findall(r'^#{2,6} (.+)', md, re.MULTILINE)
    result['structure'] = {
        'n_h2plus':      len(headings_h2plus),
        'heading_texts': [h.strip() for h in headings_h2plus],
        'fallback_section': fallback_section,
        'n_fallback_display': len(fallback_pages),
    }

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 개별 항목 채점
# ─────────────────────────────────────────────────────────────────────────────
def score_formula(parsed: dict, gt: dict) -> tuple[float, dict]:
    """수식 처리 점수 (30점)."""
    f    = parsed['formula']
    mfd  = gt['formulas']
    details = {}

    # ① Inline 삽입률 (12점)
    # mfd_inline_insertable: Figure 내부 수식 제외한 실제 삽입 가능한 최대치
    # (없으면 구버전 호환을 위해 mfd_inline 사용)
    exp_inline = mfd.get('mfd_inline_insertable') or mfd['mfd_inline']
    exp_inline_raw = mfd['mfd_inline']
    if exp_inline > 0:
        inline_ratio = min(f['n_inline'] / exp_inline, 1.0)
    else:
        inline_ratio = 1.0  # 수식 없는 논문 → 만점
    s_inline = round(inline_ratio * 12, 2)
    insertable_note = (
        f" (삽입가능기준={exp_inline}, 전체MFD={exp_inline_raw})"
        if 'mfd_inline_insertable' in mfd else ""
    )
    details['inline_coverage'] = {
        'score': s_inline, 'max': 12,
        'detail': f"{f['n_inline']}/{exp_inline} = {inline_ratio:.1%}{insertable_note}"
    }

    # ② Display 삽입률 (8점)
    exp_display = mfd['mfd_display']
    if exp_display > 0:
        display_ratio = min(f['n_display'] / exp_display, 1.0)
    else:
        display_ratio = 1.0  # display 수식 없는 논문 → 만점
    s_display = round(display_ratio * 8, 2)
    details['display_coverage'] = {
        'score': s_display, 'max': 8,
        'detail': f"{f['n_display']}/{exp_display} = {display_ratio:.1%}"
    }

    # ③ LaTeX 문법 유효성 (6점)
    total_f   = f['n_inline'] + f['n_display']
    total_valid = f['inline_valid'] + f['display_valid']
    validity_ratio = total_valid / total_f if total_f > 0 else 1.0
    s_valid = round(validity_ratio * 6, 2)
    details['latex_validity'] = {
        'score': s_valid, 'max': 6,
        'detail': f"{total_valid}/{total_f} = {validity_ratio:.1%}"
    }

    # ④ not-decoded 페널티 (4점)
    s_notdecoded = max(0.0, 4.0 - f['not_decoded'] * 1.0)
    details['not_decoded_penalty'] = {
        'score': s_notdecoded, 'max': 4,
        'detail': f"formula-not-decoded: {f['not_decoded']}개"
    }

    total = s_inline + s_display + s_valid + s_notdecoded
    return round(total, 2), details


def score_image(parsed: dict, gt: dict) -> tuple[float, dict]:
    """이미지 정확도 점수 (20점)."""
    img     = parsed['image']
    exp_fig = gt['figures']['expected_count']
    exp_cap = gt['captions']['expected_count']
    details = {}

    # ① Figure 수 정확도 (14점)
    diff = abs(img['n_images'] - exp_fig)
    if diff == 0:
        s_fig = 14.0
    elif diff == 1:
        s_fig = 10.0
    elif diff == 2:
        s_fig = 6.0
    else:
        s_fig = max(0.0, 14.0 - diff * 3.0)
    details['figure_count'] = {
        'score': s_fig, 'max': 14,
        'detail': f"found={img['n_images']}, expected={exp_fig}, diff={diff}"
    }

    # ② 캡션 커버리지 (6점)
    cap_ratio = min(img['n_captions_near'] / max(exp_cap, 1), 1.0)
    s_cap = round(cap_ratio * 6, 2)
    details['caption_coverage'] = {
        'score': s_cap, 'max': 6,
        'detail': f"captions_near_figure={img['n_captions_near']}/{exp_cap}"
    }

    total = s_fig + s_cap
    return round(total, 2), details


def score_text(parsed: dict, gt: dict, md: str) -> tuple[float, dict]:
    """텍스트 품질 점수 (20점)."""
    art     = parsed['text']
    details = {}
    base    = 20.0

    # ① Unicode 이스케이프 잔존 (기여: 5점)
    s_uesc = max(0.0, 5.0 - art['u_escape'] * 1.0)
    details['unicode_escape'] = {
        'score': s_uesc, 'max': 5,
        'detail': f"잔존 /uXXXX: {art['u_escape']}개"
    }

    # ② 리가처 잔존 (기여: 4점)
    s_lig = max(0.0, 4.0 - art['ligature'] * 1.0)
    details['ligature'] = {
        'score': s_lig, 'max': 4,
        'detail': f"잔존 /uniFBxx: {art['ligature']}개"
    }

    # ③ Symbol PUA 잔존 (기여: 3점)
    s_pua = max(0.0, 3.0 - art['pua'] * 0.5)
    details['symbol_pua'] = {
        'score': s_pua, 'max': 3,
        'detail': f"잔존 PUA 문자: {art['pua']}개"
    }

    # ④ 보일러플레이트 잔존 (기여: 5점)
    boiler_count = 0
    boiler_found = []
    for pat in gt['text_quality']['forbidden_patterns']:
        m = re.findall(pat, md, re.IGNORECASE | re.MULTILINE)
        if m:
            boiler_count += len(m)
            boiler_found.extend(m[:2])
    s_boil = max(0.0, 5.0 - boiler_count * 1.0)
    details['boilerplate'] = {
        'score': s_boil, 'max': 5,
        'detail': f"잔존 패턴: {boiler_count}개 {boiler_found[:3]}"
    }

    # ⑤ µm 스페이싱 오류 (기여: 3점)
    s_um = max(0.0, 3.0 - art['um_space'] * 0.5)
    details['um_spacing'] = {
        'score': s_um, 'max': 3,
        'detail': f"'µ X' 스페이싱 오류: {art['um_space']}개"
    }

    total = s_uesc + s_lig + s_pua + s_boil + s_um
    return round(total, 2), details


def score_structure(parsed: dict, gt: dict) -> tuple[float, dict]:
    """문서 구조 점수 (15점)."""
    st      = parsed['structure']
    details = {}

    # ① 필수 헤딩 존재 (6점)
    req_headings = gt['structure']['required_headings']
    found_req = sum(
        1 for rh in req_headings
        if any(rh.lower() in h.lower() for h in st['heading_texts'])
    )
    req_ratio = found_req / len(req_headings) if req_headings else 1.0
    s_req = round(req_ratio * 6, 2)
    details['required_headings'] = {
        'score': s_req, 'max': 6,
        'detail': f"필수 헤딩 {found_req}/{len(req_headings)}: {req_headings}"
    }

    # ② 헤딩 밀도 (4점): min_h2_count 이상이면 4점, 부족하면 비례 감점
    min_h2 = gt['structure']['min_h2_count']
    h2_ratio = min(st['n_h2plus'] / min_h2, 1.0) if min_h2 > 0 else 1.0
    s_h2 = round(h2_ratio * 4, 2)
    details['heading_density'] = {
        'score': s_h2, 'max': 4,
        'detail': f"h2+ 헤딩: {st['n_h2plus']}개 (최소 {min_h2}개 기준)"
    }

    # ③ 미매핑 display 수식 섹션 없음 (5점): fallback_display 0이면 5점
    n_fb = st['n_fallback_display']
    s_fallback = max(0.0, 5.0 - n_fb * 1.5)
    details['no_fallback_formulas'] = {
        'score': s_fallback, 'max': 5,
        'detail': f"📐 미매핑 display 수식: {n_fb}개 (Page 섹션 기준)"
    }

    total = s_req + s_h2 + s_fallback
    return round(total, 2), details


def score_speed(elapsed_s) -> tuple:
    """처리 속도 점수 (15점). elapsed_s=None이면 점수 미산정."""
    if elapsed_s is None:
        return None, {'detail': '타이밍 미입력 (--elapsed 옵션 사용)'}

    # Step function
    if elapsed_s < 12:
        s = 15.0
    elif elapsed_s < 17:
        s = 12.0
    elif elapsed_s < 22:
        s = 9.0
    elif elapsed_s < 27:
        s = 6.0
    elif elapsed_s < 35:
        s = 3.0
    else:
        s = 1.0

    detail = f"{elapsed_s:.1f}s"
    thresholds = "<12s→15, <17s→12, <22s→9, <27s→6, <35s→3, else→1"
    return s, {'score': s, 'max': 15, 'detail': detail, 'thresholds': thresholds}


# ─────────────────────────────────────────────────────────────────────────────
# 통합 7카테고리 채점 (100점)
# 수식25 + 표10 + 이미지15 + 텍스트20 + 구조15 + 참고문헌10 + 속도5 = 100
# GT 있으면 정밀, 없으면 heuristic (카테고리 구조는 동일)
# ─────────────────────────────────────────────────────────────────────────────

def _score_formula_unified(parsed: dict, gt: dict = None) -> tuple:
    """수식 25점. GT 있으면 삽입률 기반, 없으면 count 기반."""
    f = parsed['formula']
    details = {}

    if gt and 'formulas' in gt:
        mfd = gt['formulas']
        exp_inline = mfd.get('mfd_inline_insertable') or mfd['mfd_inline']
        exp_display = mfd['mfd_display']

        inline_ratio  = min(f['n_inline'] / exp_inline,  1.0) if exp_inline  > 0 else 1.0
        display_ratio = min(f['n_display'] / exp_display, 1.0) if exp_display > 0 else 1.0
        total_f = f['n_inline'] + f['n_display']
        total_valid = f['inline_valid'] + f['display_valid']
        validity_ratio = total_valid / total_f if total_f > 0 else 1.0

        s_inline  = round(inline_ratio * 10, 2)   # 10점
        s_display = round(display_ratio * 6, 2)    # 6점
        s_valid   = round(validity_ratio * 5, 2)   # 5점
        s_nd      = max(0.0, 4.0 - f['not_decoded'])  # 4점

        details['inline_coverage']      = {'score': s_inline,  'max': 10, 'detail': f"{f['n_inline']}/{exp_inline} = {inline_ratio:.1%}"}
        details['display_coverage']     = {'score': s_display, 'max': 6,  'detail': f"{f['n_display']}/{exp_display} = {display_ratio:.1%}"}
        details['latex_validity']       = {'score': s_valid,   'max': 5,  'detail': f"{total_valid}/{total_f} = {validity_ratio:.1%}"}
        details['not_decoded_penalty']  = {'score': s_nd,      'max': 4,  'detail': f"not-decoded: {f['not_decoded']}개"}
        total = s_inline + s_display + s_valid + s_nd
    else:
        total_f = f['n_inline'] + f['n_display']
        if   total_f >= 150: s = 23.0
        elif total_f >= 100: s = 22.0
        elif total_f >= 70:  s = 21.0
        elif total_f >= 50:  s = 20.0
        elif total_f >= 30:  s = 18.0
        elif total_f >= 15:  s = 14.0
        elif total_f >= 5:   s = 9.0
        elif total_f >= 1:   s = 5.0
        else:                s = 2.0
        s = max(0.0, s - min(f['not_decoded'] * 2, 6))
        # validity bonus: 유효 수식 비율에 따라 최대 +2점
        if total_f > 0:
            total_valid = f['inline_valid'] + f['display_valid']
            validity_ratio = total_valid / total_f
            s = min(25.0, s + round(validity_ratio * 2, 2))
        total = round(s, 2)
        details['count_heuristic'] = {'score': total, 'max': 25, 'detail': f"inline={f['n_inline']}, display={f['n_display']}, not-decoded={f['not_decoded']}, tier_base={s:.1f}"}

    return round(total, 2), details


def _count_real_table_rows(md: str) -> tuple[int, int]:
    """실제 데이터 표 행 수 반환. Figure 라벨 오인식(Col1/Col2, <br> 과다) 필터링."""
    all_rows = re.findall(r'^\|.+\|', md, re.MULTILINE)
    all_headers = re.findall(r'^\|[-:| ]+\|', md, re.MULTILINE)
    if not all_rows:
        return 0, 0
    # 각 표 블록을 분리하여 가짜 표 여부 판단
    real_rows = 0
    real_tables = 0
    # 줄 단위로 표 블록 추출
    lines = md.split('\n')
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith('|') and '|' in lines[i][1:]:
            # 표 블록 수집
            block = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                block.append(lines[i].strip())
                i += 1
            if len(block) < 2:
                continue
            header = block[0]
            cells = [c.strip() for c in header.split('|') if c.strip()]
            # 가짜 표 판단: Col숫자 헤더가 하나라도 있거나, <br> 비율이 40% 이상
            br_count = sum(b.count('<br>') for b in block)
            any_col = any(re.match(r'^Col\d+$', c) for c in cells)
            br_heavy = br_count >= len(block) * 0.4
            if any_col or br_heavy:
                continue
            real_tables += 1
            real_rows += sum(1 for b in block if not re.match(r'^\|[-:| ]+\|', b))
        else:
            i += 1
    return real_rows, real_tables


def _score_table_unified(md: str, gt: dict = None) -> tuple:
    """표 10점. GT 있으면 예상 표 수 비교, 없으면 실제 표 감지 heuristic."""
    real_rows, real_tables = _count_real_table_rows(md)
    all_rows = len(re.findall(r'^\|.+\|', md, re.MULTILINE))
    fake_rows = all_rows - real_rows

    if gt and 'tables' in gt:
        exp = gt['tables']['expected_count']
        if exp == 0:
            # 표 없는 논문: 가짜 표 감지 시 페널티, 실제 표 없으면 만점
            if real_rows == 0:
                s = 10.0
                detail = f"올바름: 실제 표 없음 (가짜 {fake_rows}행 필터됨)"
            else:
                s = max(0.0, 10.0 - real_rows * 1.0)
                detail = f"오감지: 실제표 {real_tables}개 {real_rows}행 (예상 0)"
        else:
            # 표 있는 논문: 감지 비율 기반
            ratio = min(real_rows / max(exp * 5, 1), 1.0)
            s = round(ratio * 10, 2)
            detail = f"실제표 {real_tables}개 {real_rows}행 / 예상 {exp}개"
    else:
        # GT 없음: 실제 표만 채점
        if   real_rows >= 30: s = 10.0
        elif real_rows >= 15: s = 8.0
        elif real_rows >= 5:  s = 6.0
        elif real_rows >= 1:  s = 3.0
        else:                 s = 7.0  # 표 없음: 중립 (GT 없이는 판단 불가)
        detail = f"실제표 {real_tables}개 {real_rows}행 (가짜 {fake_rows}행 제외)"
    return s, {'table_count': {'score': s, 'max': 10, 'detail': detail}}


def _score_image_unified(parsed: dict, gt: dict = None) -> tuple:
    """이미지 15점. GT 있으면 정확도 기반, 없으면 count 기반."""
    img = parsed['image']
    details = {}

    if gt and 'figures' in gt and 'captions' in gt:
        exp_fig = gt['figures']['expected_count']
        exp_cap = gt['captions']['expected_count']
        found = img['n_images']
        # 비율 기반 점수: 서브패널 추출 엔진(pymupdf4llm)의 과잉 감지에도 partial credit
        ratio = found / max(exp_fig, 1)
        if ratio <= 1.0:  # 정확하거나 부족
            s_fig = round(10.0 * ratio, 2)  # 부족분 비례 감점
            if found == exp_fig: s_fig = 10.0  # 정확히 맞으면 만점
        elif ratio <= 2.0:  # 최대 2배까지 완만한 감점
            s_fig = round(max(0.0, 10.0 - (ratio - 1.0) * 5), 2)
        else:  # 2배 초과 → 급격한 감점
            s_fig = round(max(0.0, 10.0 - 5.0 - (ratio - 2.0) * 5), 2)
        cap_ratio = min(img['n_captions_near'] / max(exp_cap, 1), 1.0)
        s_cap = round(cap_ratio * 5, 2)
        details['figure_count']    = {'score': s_fig, 'max': 10, 'detail': f"found={found}, expected={exp_fig}, ratio={ratio:.1f}x"}
        details['caption_coverage']= {'score': s_cap, 'max': 5,  'detail': f"캡션매칭 {img['n_captions_near']}/{exp_cap}"}
        total = round(s_fig + s_cap, 2)
    else:
        n = img['n_images']
        if   n >= 6: s_fig = 10.0
        elif n >= 5: s_fig = 8.5
        elif n >= 4: s_fig = 7.0
        elif n >= 3: s_fig = 5.0
        elif n >= 2: s_fig = 3.0
        elif n >= 1: s_fig = 1.5
        else:        s_fig = 0.0
        cap_ratio = img['n_captions_near'] / max(n, 1) if n > 0 else 0
        s_cap = round(cap_ratio * 5, 2)
        details['image_count']     = {'score': s_fig, 'max': 10, 'detail': f"이미지 {n}개"}
        details['caption_coverage']= {'score': s_cap, 'max': 5,  'detail': f"캡션매칭 {img['n_captions_near']}개"}
        total = round(min(s_fig + s_cap, 15.0), 2)

    return total, details


def _score_text_unified(parsed: dict, md: str, gt: dict = None) -> tuple:
    """텍스트 20점. 아티팩트 감지 (보일러플레이트는 GT 있을 때 정밀)."""
    art = parsed['text']
    details = {}

    s_uesc = max(0.0, 5.0 - art['u_escape'])
    s_lig  = max(0.0, 4.0 - art['ligature'])
    s_pua  = max(0.0, 3.0 - art['pua'] * 0.5)
    s_um   = max(0.0, 3.0 - art['um_space'] * 0.5)
    details['unicode_escape'] = {'score': s_uesc, 'max': 5, 'detail': f"잔존 /uXXXX: {art['u_escape']}개"}
    details['ligature']       = {'score': s_lig,  'max': 4, 'detail': f"잔존 /uniFBxx: {art['ligature']}개"}
    details['symbol_pua']     = {'score': s_pua,  'max': 3, 'detail': f"잔존 PUA: {art['pua']}개"}
    details['um_spacing']     = {'score': s_um,   'max': 3, 'detail': f"µ 스페이싱 오류: {art['um_space']}개"}

    if gt and 'text_quality' in gt:
        boiler_count = 0
        boiler_found = []
        for pat in gt['text_quality']['forbidden_patterns']:
            ms = re.findall(pat, md, re.IGNORECASE | re.MULTILINE)
            boiler_count += len(ms)
            boiler_found.extend(ms[:2])
        s_boil = max(0.0, 5.0 - boiler_count)
        details['boilerplate'] = {'score': s_boil, 'max': 5, 'detail': f"잔존 패턴: {boiler_count}개 {boiler_found[:3]}"}
    else:
        generic_boilers = [r'formula-not-decoded', r'/u[0-9A-Fa-f]{4,5}', r'/uniFB[0-9a-f]+']
        bc = sum(len(re.findall(p, md)) for p in generic_boilers)
        s_boil = max(0.0, 5.0 - bc)
        details['boilerplate'] = {'score': s_boil, 'max': 5, 'detail': f"공통 아티팩트: {bc}개"}

    return round(s_uesc + s_lig + s_pua + s_um + s_boil, 2), details


def _score_structure_unified(parsed: dict, gt: dict = None) -> tuple:
    """구조 15점. GT 있으면 필수헤딩 정밀, 없으면 heuristic."""
    st = parsed['structure']
    details = {}

    if gt and 'structure' in gt:
        req = gt['structure']['required_headings']
        found = sum(1 for r in req if any(r.lower() in h.lower() for h in st['heading_texts']))
        s_req = round(found / len(req) * 6, 2) if req else 6.0
        min_h2 = gt['structure']['min_h2_count']
        s_h2  = round(min(st['n_h2plus'] / min_h2, 1.0) * 4, 2) if min_h2 > 0 else 4.0
        details['required_headings'] = {'score': s_req, 'max': 6, 'detail': f"필수헤딩 {found}/{len(req)}: {req}"}
        details['heading_density']   = {'score': s_h2,  'max': 4, 'detail': f"h2+ {st['n_h2plus']}개 (최소 {min_h2})"}
    else:
        section_kws = [
            r'(?i)a[\s\-]*b[\s\-]*s[\s\-]*t[\s\-]*r[\s\-]*a[\s\-]*c[\s\-]*t',  # abstract (spaced 포함)
            r'(?i)(introduction|1\.\s+intro)',
            r'(?i)(conclusion|discussion|summary|overview|background|scope)',
            r'(?i)(materials.and.methods|experimental|\bmethods?\b|results?\b)',
        ]
        # 각 헤딩을 개별 검색 (join 시 단어경계 소실 방지)
        found_secs = sum(1 for kw in section_kws if any(re.search(kw, h) for h in st['heading_texts']))
        # 3개 이상 섹션 발견 시 만점 인정 (섹션 명칭 다양성 반영)
        if found_secs >= 3:
            s_req = 6.0
        else:
            s_req = round(found_secs / 4 * 6, 2)
        s_h2  = round(min(st['n_h2plus'] / 6, 1.0) * 4, 2)
        details['section_coverage']  = {'score': s_req, 'max': 6, 'detail': f"핵심섹션 {found_secs}/4개"}
        details['heading_density']   = {'score': s_h2,  'max': 4, 'detail': f"h2+ {st['n_h2plus']}개"}

    n_fb  = st['n_fallback_display']
    s_nfb = max(0.0, 5.0 - n_fb * 1.5)
    details['no_fallback_formulas'] = {'score': s_nfb, 'max': 5, 'detail': f"미매핑 display: {n_fb}개"}

    return round((details.get('required_headings', details.get('section_coverage'))['score']
                  + details['heading_density']['score'] + s_nfb), 2), details


def _score_reference_unified(md: str) -> tuple:
    """참고문헌 10점. 항상 heuristic."""
    ref_count = max(
        len(re.findall(r'^\[\d+\]',           md, re.MULTILINE)),  # [1] 형식
        len(re.findall(r'^-\s+\[\d+\]',       md, re.MULTILINE)),  # - [1] 형식 (hybrid postprocess)
        len(re.findall(r'^\d+\.\s+[A-Z]',     md, re.MULTILINE)),  # 1. Author 형식
        len(re.findall(r'^- [A-Z][a-z]+,',    md, re.MULTILINE)),  # - Author, 형식
        len(re.findall(r'^-\s+\d+\.\s+\S',    md, re.MULTILINE)),  # - 1. Author 형식 (Marker/hybrid)
    )
    if   ref_count >= 25: s = 10.0
    elif ref_count >= 15: s = 8.0
    elif ref_count >= 10: s = 7.0
    elif ref_count >= 8:  s = 6.0
    elif ref_count >= 5:  s = 4.0
    elif ref_count >= 3:  s = 3.0
    elif ref_count >= 1:  s = 1.0
    else:                 s = 0.0
    return s, {'ref_count': {'score': s, 'max': 10, 'detail': f"참고문헌 {ref_count}개"}}


def _score_speed_unified(elapsed_s) -> tuple:
    """속도 5점."""
    if elapsed_s is None:
        return None, {}
    if   elapsed_s < 12: s = 5.0
    elif elapsed_s < 20: s = 4.0
    elif elapsed_s < 25: s = 3.0
    elif elapsed_s < 30: s = 2.0
    elif elapsed_s < 40: s = 1.0
    else:                s = 0.5
    return s, {'elapsed': {'score': s, 'max': 5, 'detail': f"{elapsed_s:.1f}s"}}


def score_unified(md_path: Path, gt: dict = None, elapsed=None) -> dict:
    """7카테고리 통합 채점. gt=None이면 전 카테고리 heuristic."""
    md = md_path.read_text(encoding='utf-8', errors='ignore')
    parsed = parse_md(md)

    f_score, f_det = _score_formula_unified(parsed, gt)
    t_score, t_det = _score_table_unified(md, gt)
    i_score, i_det = _score_image_unified(parsed, gt)
    tx_score, tx_det = _score_text_unified(parsed, md, gt)
    st_score, st_det = _score_structure_unified(parsed, gt)
    r_score, r_det = _score_reference_unified(md)
    sp_score, sp_det = _score_speed_unified(elapsed)

    base = f_score + t_score + i_score + tx_score + st_score + r_score
    total = base + (sp_score or 0)
    mode = 'gt' if gt else 'heuristic'

    return {
        'mode':      mode,
        'formula':   {'total': f_score,  'max': 25, 'details': f_det},
        'table':     {'total': t_score,  'max': 10, 'details': t_det},
        'image':     {'total': i_score,  'max': 15, 'details': i_det},
        'text':      {'total': tx_score, 'max': 20, 'details': tx_det},
        'structure': {'total': st_score, 'max': 15, 'details': st_det},
        'reference': {'total': r_score,  'max': 10, 'details': r_det},
        'speed':     {'total': sp_score, 'max': 5,  'details': sp_det},
        'subtotal':  round(base, 2),
        'total':     round(total, 2),
    }


_ANSI_GREEN  = '\033[92m'
_ANSI_YELLOW = '\033[93m'
_ANSI_RED    = '\033[91m'
_ANSI_BOLD   = '\033[1m'
_ANSI_RESET  = '\033[0m'


def _score_color(score: float, max_score: float = 100) -> str:
    """점수 비율에 따른 ANSI 색상 코드 반환."""
    ratio = score / max_score
    if ratio >= 0.80: return _ANSI_GREEN
    if ratio >= 0.60: return _ANSI_YELLOW
    return _ANSI_RED


def _score_grade(score: float) -> str:
    """100점 기준 등급 뱃지 반환."""
    if score >= 90: return "🏆 S"
    if score >= 80: return "🟢 A"
    if score >= 70: return "🟡 B"
    if score >= 60: return "🟠 C"
    return "🔴 D"


def print_unified_report(paper_label: str, scores: dict) -> None:
    """7카테고리 통합 채점 결과 출력 (이모지·색상·100점 만점)."""
    mode_tag = "[GT]" if scores['mode'] == 'gt' else "[휴리스틱]"
    total = scores['total']
    subtotal = scores['subtotal']
    sp = scores.get('speed', {}).get('total')

    color = _score_color(total if sp is not None else subtotal * 100 / 95)
    grade = _score_grade(total if sp is not None else subtotal * 100 / 95)

    print()
    print(_ANSI_BOLD + "=" * 72 + _ANSI_RESET)
    print(f"  📄 {paper_label}  {mode_tag}")
    print(_ANSI_BOLD + "=" * 72 + _ANSI_RESET)
    print(f"  {'항목':<8} {'배점':>4}  {'바':20}  {'점수':>8}  {'세부'}")
    print(f"  {'─'*8} {'─'*4}  {'─'*20}  {'─'*8}  {'─'*28}")

    cats = [
        ("🔢 수식",   "formula",   25),
        ("📊 표",     "table",     10),
        ("🖼️ 이미지", "image",     15),
        ("📝 텍스트", "text",      20),
        ("🏗️ 구조",  "structure", 15),
        ("📚 참고",   "reference", 10),
        ("⚡ 속도",   "speed",      5),
    ]
    for name, key, max_v in cats:
        cat = scores.get(key, {})
        s = cat.get('total')
        if s is None:
            print(f"  {name:<8} {max_v:>4}  {'—'*20}  {'미입력':>8}")
            continue
        filled = int(s / max_v * 20)
        bar = "█" * filled + "░" * (20 - filled)
        cat_color = _score_color(s, max_v)
        score_str = f"{s:.1f}/{max_v}"
        print(f"  {name:<8} {max_v:>4}  {cat_color}{bar}{_ANSI_RESET}  {cat_color}{score_str:>8}{_ANSI_RESET}")
        for sk, sv in cat.get('details', {}).items():
            if isinstance(sv, dict):
                sc = sv.get('score')
                mx = sv.get('max', '?')
                sc_str = f"{sc:.1f}" if sc is not None else "?"
                detail = sv.get('detail', '')[:45]
                print(f"    {'·'} {sk:<28} {sc_str:>4}/{mx}  {detail}")

    print(f"  {'─'*70}")
    if sp is not None:
        norm = total
        print(f"  {color}{_ANSI_BOLD}합계:  {norm:.1f} / 100  {grade}{_ANSI_RESET}")
    else:
        norm = round(subtotal * 100 / 95, 1)
        print(f"  {color}{_ANSI_BOLD}합계(속도 제외):  {subtotal:.1f}/95점  ({norm:.1f}/100 환산)  {grade}{_ANSI_RESET}")
    print("=" * 72)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# 보고서 출력
# ─────────────────────────────────────────────────────────────────────────────
def print_report(paper_key: str, gt: dict, scores: dict, total: float, elapsed):
    print()
    print("=" * 70)
    print(f"  벤치마크 결과: {gt.get('display_name', paper_key)}")
    print("=" * 70)

    categories = [
        ("수식 처리",    "formula",   30),
        ("이미지 정확도", "image",    20),
        ("텍스트 품질",  "text",      20),
        ("문서 구조",    "structure", 15),
        ("처리 속도",    "speed",     15),
    ]

    for cat_name, cat_key, cat_max in categories:
        cat = scores.get(cat_key, {})
        cat_score = cat.get('total', None)
        if cat_score is None and cat_key == 'speed':
            print(f"\n{'─'*60}")
            print(f"  {cat_name:12s}  [{cat_max}점] — 미입력")
            continue

        bar_len = int(cat_score / cat_max * 20) if cat_score is not None else 0
        bar = "█" * bar_len + "░" * (20 - bar_len)
        print(f"\n{'─'*60}")
        print(f"  {cat_name:12s}  [{bar}]  {cat_score:.1f}/{cat_max}")

        for sub_key, sub in cat.get('details', {}).items():
            if isinstance(sub, dict):
                score_str = f"{sub.get('score', '?'):.1f}" if sub.get('score') is not None else "?"
                max_str   = f"{sub.get('max', '?')}"
                detail    = sub.get('detail', '')
                print(f"    {'•'} {sub_key:<30s} {score_str:>5s}/{max_str:>3s}  {detail}")

    # 합계
    speed_s = scores.get('speed', {}).get('total', None)
    sub_total = sum(
        scores[k]['total'] for k in ('formula', 'image', 'text', 'structure')
        if k in scores and scores[k].get('total') is not None
    )
    print(f"\n{'='*70}")
    if speed_s is not None:
        full_total = sub_total + speed_s
        print(f"  총점:  {full_total:.1f} / 100")
    else:
        print(f"  총점 (속도 제외):  {sub_total:.1f} / 85")
    print("=" * 70)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# 채점 결과 누적 저장 (benchmark_scores.json)
# ─────────────────────────────────────────────────────────────────────────────

def _read_yaml_frontmatter(md_path: Path) -> dict:
    """MD 파일 상단 YAML frontmatter 파싱 (--- 블록). 없으면 {}."""
    try:
        text = md_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end].strip()
    meta = {}
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"')
    return meta


def save_score(
    paper_key: str,
    md_path: Path,
    scores: dict,
    total: float,
    elapsed,
    mode: str = "gt",
) -> None:
    """채점 결과를 benchmark_scores.json에 누적 저장.

    구조:
      {
        "A1": {
          "A1_Hybrid_Full": {
            "engine": "hybrid_v4_speed",
            "postprocess_rules": "...",
            "total": 90.4,
            "formula": 25.2,
            "image": 18.0,
            "text": 19.0,
            "structure": 14.0,
            "speed": 9.0,
            "elapsed_s": 21.9,
            "mode": "gt",
            "timestamp": "2026-03-04 12:00:00",
            "md_file": "relative/path.md"
          }
        }
      }
    """
    # 기존 파일 로드
    if SCORES_FILE.exists():
        try:
            db = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
        except Exception:
            db = {}
    else:
        db = {}

    # YAML frontmatter에서 엔진·후처리 정보 추출
    meta = _read_yaml_frontmatter(md_path)
    engine = meta.get("engine", "unknown")
    pp_rules = meta.get("postprocess_rules", "")

    # 점수 항목 평탄화
    entry = {
        "engine":           engine,
        "postprocess_rules": pp_rules,
        "total":            round(total, 2),
        "elapsed_s":        elapsed,
        "mode":             mode,
        "timestamp":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "md_file":          str(md_path.relative_to(VAULT_ROOT)) if md_path.is_relative_to(VAULT_ROOT) else str(md_path),
    }
    # 카테고리별 점수 추가
    for cat in ("formula", "image", "text", "structure", "speed"):
        cat_data = scores.get(cat, {})
        entry[cat] = cat_data.get("total")

    # 복합키: "엔진/파일스템" — 동일 논문의 다른 엔진 결과를 구분
    # YAML engine 값 우선, 없으면 부모 폴더명
    engine_key = engine if engine and engine != "unknown" else md_path.parent.name
    key_name = f"{engine_key}/{md_path.stem}"
    db.setdefault(paper_key, {})[key_name] = entry

    SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCORES_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_unified_score(paper_key: str, key_name: str, engine: str, scores: dict, md_path: Path) -> None:
    """통합 채점 결과를 benchmark_scores.json에 저장."""
    if SCORES_FILE.exists():
        try:
            db = json.loads(SCORES_FILE.read_text(encoding='utf-8'))
        except Exception:
            db = {}
    else:
        db = {}

    meta = _read_yaml_frontmatter(md_path)
    entry = {
        'engine':           engine,
        'postprocess_rules': meta.get('postprocess_rules', ''),
        'mode':             scores['mode'],
        'total':            scores['total'],
        'subtotal':         scores['subtotal'],
        'formula':          scores['formula']['total'],
        'table':            scores['table']['total'],
        'image':            scores['image']['total'],
        'text':             scores['text']['total'],
        'structure':        scores['structure']['total'],
        'reference':        scores['reference']['total'],
        'speed':            scores['speed']['total'],
        'timestamp':        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'md_file':          str(md_path.relative_to(VAULT_ROOT)) if md_path.is_relative_to(VAULT_ROOT) else str(md_path),
    }
    db.setdefault(paper_key, {})[key_name] = entry
    SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCORES_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding='utf-8')


def print_scores_table(paper_key=None) -> None:
    """benchmark_scores.json 내용을 비교 테이블로 출력."""
    if not SCORES_FILE.exists():
        print("  (저장된 채점 결과 없음)")
        return

    db = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
    keys = [paper_key] if paper_key and paper_key in db else sorted(db.keys())

    for pk in keys:
        entries = db[pk]
        print(f"\n  [{pk}]  ({len(entries)}개 결과)")
        print(f"  {'엔진':28} {'수식':>5} {'이미지':>5} {'텍스트':>5} {'구조':>5} {'속도':>5} {'합계':>6}  {'시각'}")
        print(f"  {'─'*28} {'─'*5} {'─'*5} {'─'*5} {'─'*5} {'─'*5} {'─'*6}  {'─'*16}")
        for stem, e in sorted(entries.items(), key=lambda x: -(x[1].get("total") or 0)):
            def fmt(v):
                return f"{v:.1f}" if v is not None else "  —"
            # "엔진폴더/파일스템" 형태 → 엔진 폴더만 표시
            display_key = stem.split("/")[0] if "/" in stem else stem
            print(
                f"  {display_key:<28} "
                f"{fmt(e.get('formula')):>5} {fmt(e.get('image')):>5} "
                f"{fmt(e.get('text')):>5} {fmt(e.get('structure')):>5} "
                f"{fmt(e.get('speed')):>5} {fmt(e.get('total')):>6}  "
                f"{e.get('timestamp','')[:16]}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# journal_profiles.json 연동
# ─────────────────────────────────────────────────────────────────────────────

PROFILES_FILE = Path(__file__).parent / "journal_profiles.json"


def _load_profiles() -> dict:
    if not PROFILES_FILE.exists():
        return {}
    return json.loads(PROFILES_FILE.read_text(encoding="utf-8"))


def match_journal(query: str) -> tuple:
    """저널명/파일명 키워드로 프로파일 매칭.

    Returns: (profile_id, profile_dict) 또는 (None, None)
    """
    data = _load_profiles()
    q = query.lower().replace("-", "_").replace(" ", "_")

    for pid, prof in data.get("profiles", {}).items():
        match = prof.get("match", {})
        for kw in match.get("filename_keywords", []):
            if kw.lower().replace("-", "_") in q:
                return pid, prof
        for kw in match.get("journal_keywords", []):
            if kw.lower() in query.lower():
                return pid, prof

    return None, None


def update_profile(paper_key: str, engine: str, total: float) -> None:
    """benchmark_scores.json 결과가 더 좋으면 journal_profiles.json 추천을 자동 갱신.

    paper_key가 known_papers에 등록된 프로파일을 찾아 score를 비교한다.
    더 높은 score이면 recommended 항목을 업데이트.
    """
    data = _load_profiles()
    found_pid  = None
    was_updated = False

    for pid, prof in data.get("profiles", {}).items():
        if paper_key in prof.get("known_papers", []):
            found_pid = pid
            rec = prof.setdefault("recommended", {})
            current_score = rec.get("score") or 0.0
            if total > current_score:
                rec["engine"]         = engine
                rec["score"]          = round(total, 2)
                rec["evidence_paper"] = paper_key
                rec["updated"]        = datetime.now().strftime("%Y-%m-%d")
                if "_note" in rec:
                    del rec["_note"]
                was_updated = True
                print(f"  → 프로파일 [{pid}] 추천 갱신: {engine} ({total:.1f}점)")
            else:
                print(f"  → 프로파일 [{pid}] 유지: 기존 추천({rec.get('engine')}, {current_score:.1f}점) ≥ 새 결과({total:.1f}점)")
            break

    if found_pid is None:
        print(f"  → [{paper_key}] 가 등록된 프로파일 없음 (journal_profiles.json known_papers 추가 필요)")
        return

    if was_updated:
        PROFILES_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def print_profiles() -> None:
    """journal_profiles.json 현황 테이블 출력."""
    data = _load_profiles()
    profs = data.get("profiles", {})
    if not profs:
        print("  (프로파일 없음)")
        return

    print(f"\n  {'프로파일 ID':<30} {'권장 엔진':<22} {'점수':>5}  {'증거 논문'}")
    print(f"  {'─'*30} {'─'*22} {'─'*5}  {'─'*40}")
    for pid, prof in profs.items():
        rec = prof.get("recommended", {})
        eng   = rec.get("engine", "—")
        score = rec.get("score")
        evid  = rec.get("evidence_paper", "—")
        score_str = f"{score:.1f}" if score is not None else "  —"
        print(f"  {pid:<30} {eng:<22} {score_str:>5}  {evid}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# GT 없는 기본 채점 (새 문서 유형 등 GT 미보유 시)
# ─────────────────────────────────────────────────────────────────────────────

def analyze_quality(md_path: Path) -> dict:
    """Ground Truth 없이 MD 파일을 정적 분석으로 기본 채점 (100점 만점).

    수식·표·이미지·구조·참고문헌 각 20점. GT 기반 채점보다 정밀도는 낮으나
    GT가 없는 새 문서 유형에도 적용 가능.
    """
    if md_path is None or not md_path.exists():
        return {"total": 0, "details": {"error": "파일 없음"}}

    text = md_path.read_text(encoding="utf-8", errors="ignore")
    details = {}

    # 수식 (20점)
    inline_math  = len(re.findall(r'\$[^$\n]+\$', text))
    display_math = len(re.findall(r'\$\$[\s\S]+?\$\$', text))
    broken_math  = len(re.findall(r'(?<!\$)\$(?!\$)(?:[^$\n]{0,3})$', text, re.MULTILINE))
    math_total   = inline_math + display_math
    details["inline_math"]  = inline_math
    details["display_math"] = display_math
    details["broken_math"]  = broken_math
    if   math_total >= 30: formula_score = 20
    elif math_total >= 20: formula_score = 17
    elif math_total >= 10: formula_score = 14
    elif math_total >= 5:  formula_score = 10
    elif math_total >= 1:  formula_score = 6
    else:                  formula_score = 2
    formula_score = max(0, formula_score - min(broken_math * 2, 6))

    # 표 (20점)
    table_rows    = len(re.findall(r'^\|.+\|', text, re.MULTILINE))
    table_headers = len(re.findall(r'^\|[-:| ]+\|', text, re.MULTILINE))
    details["table_rows"]  = table_rows
    details["table_count"] = table_headers
    if   table_rows >= 30: table_score = 20
    elif table_rows >= 15: table_score = 16
    elif table_rows >= 5:  table_score = 12
    elif table_rows >= 1:  table_score = 7
    else:                  table_score = 0

    # 이미지 (20점)
    image_refs  = re.findall(r'!\[([^\]]*)\]\(([^)]+)\)', text)
    image_count = len(image_refs)
    details["image_count"] = image_count
    if   image_count >= 5: image_score = 20
    elif image_count >= 4: image_score = 16
    elif image_count >= 3: image_score = 12
    elif image_count >= 2: image_score = 8
    elif image_count >= 1: image_score = 4
    else:                  image_score = 0

    # 구조 (20점)
    h2 = len(re.findall(r'^## .+', text, re.MULTILINE))
    h3 = len(re.findall(r'^### .+', text, re.MULTILINE))
    section_count = sum([
        bool(re.search(r'(?i)abstract', text)),
        bool(re.search(r'(?i)(introduction|1\.\s+intro)', text)),
        bool(re.search(r'(?i)(conclusion|discussion)', text)),
        bool(re.search(r'(?i)(references|bibliography)', text)),
    ])
    details["h2"] = h2
    details["h3"] = h3
    details["sections_found"] = section_count
    structure_score = min(20, h2 * 2 + h3 + section_count * 3)

    # 참고문헌 (20점)
    ref_count = max(
        len(re.findall(r'^\[\d+\]',           text, re.MULTILINE)),  # [1] 형식
        len(re.findall(r'^\d+\.\s+[A-Z]',     text, re.MULTILINE)),  # 1. Author 형식
        len(re.findall(r'^- [A-Z][a-z]+,\s',  text, re.MULTILINE)),  # - Author, 형식
        len(re.findall(r'^-\s+\d+\.\s+\S',    text, re.MULTILINE)),  # - 1. Author 형식 (Marker)
    )
    details["reference_count"] = ref_count
    details["char_count"] = len(text)
    if   ref_count >= 30: reference_score = 20
    elif ref_count >= 20: reference_score = 17
    elif ref_count >= 10: reference_score = 13
    elif ref_count >= 5:  reference_score = 9
    elif ref_count >= 1:  reference_score = 5
    else:                 reference_score = 0

    total = formula_score + table_score + image_score + structure_score + reference_score
    return {
        "formula_score":   formula_score,
        "table_score":     table_score,
        "image_score":     image_score,
        "structure_score": structure_score,
        "reference_score": reference_score,
        "total":           total,
        "details":         details,
    }


def _print_quick_report(md_path: Path, result: dict):
    print()
    print("=" * 60)
    print(f"  기본 채점 (GT 없음): {md_path.name}")
    print("=" * 60)
    cats = [
        ("수식",    "formula_score",   20),
        ("표",      "table_score",     20),
        ("이미지",  "image_score",     20),
        ("구조",    "structure_score", 20),
        ("참고문헌","reference_score", 20),
    ]
    for name, key, max_v in cats:
        s = result.get(key, 0)
        bar = "█" * int(s / max_v * 20) + "░" * (20 - int(s / max_v * 20))
        print(f"  {name:6s}  [{bar}]  {s}/{max_v}")
    print(f"{'='*60}")
    print(f"  총점: {result['total']} / 100")
    print(f"{'='*60}\n")
    d = result.get("details", {})
    print(f"  수식: inline={d.get('inline_math',0)}, display={d.get('display_math',0)}, broken={d.get('broken_math',0)}")
    print(f"  표: {d.get('table_rows',0)}행  이미지: {d.get('image_count',0)}개")
    print(f"  섹션: {d.get('sections_found',0)}/4  참고문헌: {d.get('reference_count',0)}개")
    print()


def main():
    parser = argparse.ArgumentParser(description="Markdown 파일 벤치마크 채점")
    parser.add_argument("paper_key", nargs="?",
                        help="Ground truth 키. 생략 시 --quick 기본 채점 모드")
    parser.add_argument("md_file",   nargs="?", default=None,
                        help="평가할 마크다운 파일 경로 (--scores 단독 사용 시 생략 가능)")
    parser.add_argument("--elapsed", type=float, default=None,
                        help="처리 시간(초). 미입력 시 속도 점수 제외")
    parser.add_argument("--quick",   action="store_true",
                        help="GT 없는 기본 채점 (paper_key 생략과 동일)")
    parser.add_argument("--json",    action="store_true", help="JSON 형태로 출력")
    parser.add_argument("--unified", action="store_true",
                        help="7카테고리 통합 채점 (수식/표/이미지/텍스트/구조/참고문헌/속도)")
    parser.add_argument("--no-save", action="store_true",
                        help="채점 결과를 benchmark_scores.json에 저장하지 않음")
    parser.add_argument("--scores",  action="store_true",
                        help="benchmark_scores.json 비교 테이블 출력 (채점 없이)")
    parser.add_argument("--profiles", action="store_true",
                        help="journal_profiles.json 현황 출력 (채점 없이)")
    parser.add_argument("--update-profile", action="store_true",
                        help="채점 후 journal_profiles.json 추천을 자동 갱신")
    parser.add_argument("--match", metavar="KEYWORD",
                        help="저널명/파일명 키워드로 권장 엔진 조회")
    args = parser.parse_args()

    # ── 프로파일 현황 출력 ────────────────────────────────────────────────────
    if args.profiles:
        print(f"\n{'='*80}")
        print(f"  저널 프로파일 현황")
        print(f"  파일: {PROFILES_FILE.relative_to(VAULT_ROOT)}")
        print(f"{'='*80}")
        print_profiles()
        return

    # ── 저널 매칭 조회 ────────────────────────────────────────────────────────
    if args.match:
        pid, prof = match_journal(args.match)
        print(f"\n  키워드: '{args.match}'")
        if prof:
            rec = prof.get("recommended", {})
            print(f"  매칭 프로파일: {pid} ({prof.get('display_name', '')})")
            print(f"  권장 엔진: {rec.get('engine', '—')}")
            print(f"  근거 점수: {rec.get('score', '—')} (논문: {rec.get('evidence_paper', '—')})")
        else:
            print(f"  매칭 프로파일 없음 — hybrid_v4_speed 사용 권장 (기본값)")
        print()
        return

    # ── 점수 테이블 출력 모드 ──────────────────────────────────────────────
    if args.scores:
        pk = args.paper_key  # None이면 전체 출력
        print(f"\n{'='*80}")
        print(f"  누적 벤치마크 결과 {'[' + pk + ']' if pk else '(전체)'}")
        print(f"  저장 파일: {SCORES_FILE.relative_to(VAULT_ROOT)}")
        print(f"{'='*80}")
        print_scores_table(pk)
        print()
        return

    if not args.md_file:
        parser.error("md_file 은 필수입니다 (--scores 단독 사용 시는 제외)")

    md_path = Path(args.md_file)
    if not md_path.exists():
        print(f"[ERROR] 파일 없음: {md_path}", file=sys.stderr)
        sys.exit(1)

    # ── 7카테고리 통합 채점 ───────────────────────────────────────────────────
    if args.unified:
        gt = None
        if args.paper_key and GT_FILE.exists():
            all_gt = json.loads(GT_FILE.read_text(encoding='utf-8'))
            gt = all_gt['papers'].get(args.paper_key)
        label = args.paper_key or md_path.stem
        scores = score_unified(md_path, gt=gt, elapsed=args.elapsed)
        if args.json:
            print(json.dumps(scores, ensure_ascii=False, indent=2))
        else:
            print_unified_report(label, scores)
        if not args.no_save and args.paper_key:
            meta = _read_yaml_frontmatter(md_path)
            engine = meta.get('engine', md_path.parent.name)
            key_name = f"{engine}/{md_path.stem}"
            _save_unified_score(args.paper_key, key_name, engine, scores, md_path)
        return

    # GT 없는 기본 채점
    if args.quick or not args.paper_key:
        result = analyze_quality(md_path)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_quick_report(md_path, result)
        return

    # GT 기반 정밀 채점
    if not GT_FILE.exists():
        print(f"[ERROR] Ground truth 파일 없음: {GT_FILE}", file=sys.stderr)
        sys.exit(1)
    with open(GT_FILE, encoding="utf-8") as f:
        all_gt = json.load(f)

    gt = all_gt["papers"].get(args.paper_key)
    if gt is None:
        available = list(all_gt["papers"].keys())
        print(f"[ERROR] paper_key '{args.paper_key}' 없음. 사용 가능: {available}", file=sys.stderr)
        sys.exit(1)

    md = md_path.read_text(encoding="utf-8")
    parsed = parse_md(md)

    formula_score, formula_det = score_formula(parsed, gt)
    image_score,   image_det   = score_image(parsed, gt)
    text_score,    text_det    = score_text(parsed, gt, md)
    struct_score,  struct_det  = score_structure(parsed, gt)
    speed_score,   speed_det   = score_speed(args.elapsed)

    scores = {
        'formula':   {'total': formula_score, 'max': 30, 'details': formula_det},
        'image':     {'total': image_score,   'max': 20, 'details': image_det},
        'text':      {'total': text_score,    'max': 20, 'details': text_det},
        'structure': {'total': struct_score,  'max': 15, 'details': struct_det},
        'speed':     {'total': speed_score,   'max': 15, 'details': speed_det},
    }

    sub_total = formula_score + image_score + text_score + struct_score
    total = sub_total + (speed_score or 0)

    if args.json:
        output = {
            'paper_key': args.paper_key,
            'md_file': str(md_path),
            'elapsed_s': args.elapsed,
            'scores': scores,
            'subtotal_no_speed': sub_total,
            'total': total,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print_report(args.paper_key, gt, scores, total, args.elapsed)

    # ── benchmark_scores.json 자동 저장 ──────────────────────────────────────
    if not args.no_save:
        save_score(args.paper_key, md_path, scores, total, args.elapsed, mode="gt")
        if not args.json:
            print(f"  → 결과 저장: {SCORES_FILE.relative_to(VAULT_ROOT)}")

    # ── journal_profiles.json 추천 갱신 ──────────────────────────────────────
    if args.update_profile and not args.no_save:
        meta   = _read_yaml_frontmatter(md_path)
        engine = meta.get("engine", "unknown")
        update_profile(args.paper_key, engine, total)

    if not args.json:
        print()


if __name__ == "__main__":
    main()
