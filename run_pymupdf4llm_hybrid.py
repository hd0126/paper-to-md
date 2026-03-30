#!/usr/bin/env python3
"""
PyMuPDF4LLM + MFD+UniMerNet 하이브리드 논문 변환 파이프라인 (FastHybrid)

- PyMuPDF4LLM (pymupdf4llm venv, subprocess): 문서 텍스트 + 이미지
- MFD + UniMerNet (mineru venv, current): 수식 감지 및 LaTeX 변환
- 두 파이프라인을 병렬 실행 후 결과 병합

목표: 15초 이내 논문 Markdown (Hybrid v4_speed보다 빠르게)

실행:
    ~/dotfiles/uv/mineru/.venv/bin/python scripts/run_pymupdf4llm_hybrid.py <pdf>
    ~/dotfiles/uv/mineru/.venv/bin/python scripts/run_pymupdf4llm_hybrid.py <pdf> --out-dir <dir>
"""

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# ─── 경로 설정 ────────────────────────────────────────────────────────────
VAULT_ROOT   = Path(__file__).parent.parent
SCRIPTS_DIR  = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

from env_paths import get_python
PYMUPDF4LLM_PY = get_python("pymupdf4llm")
PYMUPDF4LLM_CONVERT_SCRIPT = SCRIPTS_DIR / "engines" / "pymupdf4llm_convert.py"

# ─── run_paper_hybrid.py 공유 함수 임포트 ────────────────────────────────
# run_paper_hybrid.py에서 수식 처리 함수들을 재사용
import run_paper_hybrid as _rph

_normalize_span_text    = _rph._normalize_span_text
normalize_latex         = _rph.normalize_latex
merge_results           = _rph.merge_results
_latex_to_search_text   = _rph._latex_to_search_text
_build_flex_pat         = _rph._build_flex_pat

# MFD/MFR 파라미터 (run_paper_hybrid와 동일)
DPI          = _rph.DPI
DEVICE       = _rph.DEVICE
MFD_WEIGHT   = _rph.MFD_WEIGHT
MFR_PATH     = _rph.MFR_PATH
MFD_CONF_THR = _rph.MFD_CONF_THR
MFR_BATCH    = _rph.MFR_BATCH

# Symbol PUA 문자 맵 (정규화용)
_SYMBOL_PUA = _rph._SYMBOL_PUA

# Unicode 리가처 (fitz가 그대로 출력하는 실제 Unicode 문자)
_LIGATURES_UNICODE = {
    '\uFB00': 'ff',
    '\uFB01': 'fi',
    '\uFB02': 'fl',
    '\uFB03': 'ffi',
    '\uFB04': 'ffl',
}


# ─── PyMuPDF4LLM 변환 (subprocess) ────────────────────────────────────────

def run_pymupdf4llm(pdf_path: Path, out_dir: Path, pdf_stem: str):
    """engines/pymupdf4llm_convert.py를 pymupdf4llm venv로 실행.
    (markdown, text_spans, page_heights) 반환.
    """
    asset_dir = out_dir / f"{pdf_stem}_PyMuPDF4LLM_Hybrid_assets"
    json_path = out_dir / f"{pdf_stem}_pymupdf4llm_temp.json"

    proc = subprocess.run(
        [str(PYMUPDF4LLM_PY), str(PYMUPDF4LLM_CONVERT_SCRIPT),
         str(pdf_path), str(json_path), str(asset_dir)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"[PyMuPDF4LLM] Error:\n{proc.stderr[-800:]}")
        return None, None, None
    print(proc.stdout.strip())
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    finally:
        if json_path.exists():
            json_path.unlink()

    # page_heights: JSON에서 string key → int key 변환
    page_heights = {int(k): v for k, v in data.get("page_heights", {}).items()}
    return data["markdown"], data.get("text_spans", []), page_heights


# ─── MFD + UniMerNet (run_paper_hybrid.py와 동일) ─────────────────────────

def run_formula_pipeline(pdf_path: Path):
    """MFD + UniMerNet으로 전체 수식 인식. (formulas, page_heights) 반환"""
    return _rph.run_formula_pipeline(pdf_path)


# ─── PyMuPDF4LLM 마크다운 정규화 ──────────────────────────────────────────

def normalize_pymupdf4llm_md(text: str) -> str:
    """PyMuPDF4LLM 마크다운 후처리.

    normalize_docling_md()와 달리:
    - 리가처: /uniFBxx 문자열 형식 없음 → 실제 Unicode 문자만 처리
    - /uXXXX 이스케이프 없음 (fitz가 직접 Unicode 출력)
    - /Cxx 이스케이프 없음
    - 단락 병합 없음 (PyMuPDF4LLM이 이미 컬럼 처리)
    """
    # 1. Unicode 리가처 → ASCII
    for lig, rep in _LIGATURES_UNICODE.items():
        text = text.replace(lig, rep)

    # 2. NO-BREAK SPACE / thin space → 일반 공백
    text = text.replace('\u00A0', ' ')
    text = text.replace('\u2009', ' ')   # thin space
    text = text.replace('\u200A', ' ')   # hair space

    # 3. Symbol 폰트 PUA 문자 → Unicode 복원
    for pua, uni in _SYMBOL_PUA.items():
        text = text.replace(pua, uni)

    # 4. 이중 공백 → 단일 공백 (표/코드 제외)
    in_code = False
    lines = []
    for line in text.split('\n'):
        if line.startswith('```'):
            in_code = not in_code
        if not in_code and not line.startswith('|'):
            line = re.sub(r' {2,}', ' ', line)
        lines.append(line)
    text = '\n'.join(lines)

    # 5. µm 스페이싱 정규화
    text = re.sub(r'µ ([mMlLsSnNgGΩ])([a-zA-Z])', r'µ\1 \2', text)
    text = re.sub(r'µ ([mMlLsSnNgGΩ])(?=[^a-zA-Z])', r'µ\1', text)

    # 6. Soft hyphen 제거 (U+00AD) — PyMuPDF4LLM가 line-break 지점에 삽입
    #    fitz raw text에는 없으므로 span 매칭을 위해 제거 필요
    text = text.replace('\u00AD', '')

    # 7. inline bold 분할 정규화: "text** **moretext" → "text moretext"
    #    Science Advances 등에서 superscript/subscript가 별도 bold span으로 분리될 때
    text = re.sub(r'\*\* \*\*', ' ', text)
    text = re.sub(r'\*\*\*\*', '', text)  # 빈 bold span 제거

    # 8. 인용번호 이탤릭 정규화: ( _N_, _M_ ) → (N, M), ( _N_ - _M_ ) → (N–M)
    #    PyMuPDF4LLM이 인라인 인용번호를 _N_ (italic)으로 렌더링 → fitz plain text와 일치
    def _deitalic_cites(m):
        inner = m.group(1)
        inner = re.sub(r'_(\w+)_', r'\1', inner).strip()
        inner = re.sub(r'\s*[-–]\s*', '–', inner)   # " - " → en dash (fitz 형식)
        inner = re.sub(r'\s*,\s*', ', ', inner)      # 쉼표 뒤 공백 정규화
        return f'({inner})'
    text = re.sub(r'\(\s*((?:_\w+_\s*[,–\-]?\s*)+)\)', _deitalic_cites, text)

    return text


# ─── PyMuPDF4LLM 전용 inline 수식 삽입 ───────────────────────────────────

def _insert_inline_formulas_pymupdf(
    md: str,
    text_spans: list,
    page_heights: dict,
    mfr_formulas: list,
) -> str:
    """PyMuPDF4LLM 전용 inline 수식 삽입.

    run_paper_hybrid._insert_inline_formulas()와 Steps 1, 3-5는 동일.
    Step 2만 다름:
    - 페이지별 단조증가 pos 커서 (전역 커서 대신): footer 중복 등 순서 오류 방지
    - 줄바꿈 하이픈 처리: fitz 줄 끝 'How-' → md의 'However,' 매칭
    - 이미 ts['normed']가 설정된 것을 재사용 (재정규화 없음)
    """
    from collections import defaultdict

    inline_formulas = [
        f for f in mfr_formulas
        if f["type"] == "inline" and f.get("latex")
        and f.get("score", 0) >= 0.5
    ]
    if not inline_formulas or not text_spans:
        return md

    scale = DPI / 72

    # ── Step 1: 수식 poly(픽셀) → PDF-point bbox 변환 ──────────────────────
    for f in inline_formulas:
        poly = f["poly"]
        xs = [poly[i] for i in range(0, len(poly), 2)]
        ys = [poly[i] for i in range(1, len(poly), 2)]
        ph = page_heights.get(f["page"], 792)
        f["_bbox"] = {
            "l": min(xs) / scale,
            "r": max(xs) / scale,
            "t": ph - min(ys) / scale,
            "b": ph - max(ys) / scale,
        }

    # ── Step 2 (개선): 페이지별 단조증가 pos + 줄바꿈 하이픈 처리 ───────────
    # ts["normed"]는 이미 main()에서 설정됨 (soft hyphen 제거 포함)
    #
    # 핵심 아이디어: 페이지별 pos 커서로 footer/header 중복 문제 해결
    # - global_pos: 이전 페이지 종료 위치. 75th-percentile(max 대신) 사용:
    #   max()는 outlier(각주/참고문헌에 반복되는 텍스트)에 의해 과도하게 전진할 수 있음
    #   75th-percentile은 대부분의 페이지 본문 span 위치를 커버하면서 안정적
    # - 줄바꿈 하이픈 처리: fitz 줄 끝 'How-' → md의 'However,' 매칭
    page_span_groups: dict = defaultdict(list)
    for ts in text_spans:
        page_span_groups[ts["page"]].append(ts)

    global_pos = 0

    def _find_span(normed: str, start: int) -> tuple:
        """normed 텍스트를 start 이후에서 탐색. (idx, end_idx) 반환. 미발견 시 (-1,-1)."""
        if not normed:
            return -1, -1
        idx = md.find(normed, start)
        if idx >= 0:
            return idx, idx + len(normed)
        # 줄바꿈 하이픈 처리: fitz 줄 끝의 'Word-' → md의 'Word...' (이어진 단어)
        if normed.endswith('-') and len(normed) > 1:
            stripped = normed[:-1]
            idx = md.find(stripped, start)
            if idx >= 0:
                return idx, idx + len(stripped)
        return -1, -1

    for page_num in sorted(page_span_groups.keys()):
        page_spans = page_span_groups[page_num]

        # 페이지 내 presort: global_pos 이후에서의 첫 등장 순서 기준
        tagged = []
        for ts in page_spans:
            normed = ts.get("normed", "")
            idx, _ = _find_span(normed, global_pos)
            if idx < 0:
                idx, _ = _find_span(normed, 0)  # fallback: 전체 탐색
            tagged.append((idx if idx >= 0 else len(md), ts))
        tagged.sort(key=lambda x: x[0])

        page_pos = global_pos
        for _, ts in tagged:
            normed = ts.get("normed", "")
            idx, end_idx = _find_span(normed, page_pos)
            if idx >= 0:
                ts["_md_start"] = idx
                ts["_md_end"]   = end_idx
                page_pos = idx + 1
            else:
                ts["_md_start"] = -1
                ts["_md_end"]   = -1

        matched = [ts for ts in page_spans if ts.get("_md_start", -1) >= 0]
        if matched:
            ends = sorted(ts["_md_end"] for ts in matched)
            # 75th percentile: max() 대신 사용 → outlier(각주/참고문헌 반복 텍스트)에
            # 의한 global_pos 과도 전진 방지
            p75_idx = max(0, int(len(ends) * 0.75) - 1)
            global_pos = ends[p75_idx]

    # ── Step 3: 수식 → text_span 공간 매칭 ────────────────────────────────
    for f in inline_formulas:
        fb = f["_bbox"]
        f_page = f["page"]
        f_cy = (fb["t"] + fb["b"]) / 2
        best, best_dist = None, float("inf")
        for ts in text_spans:
            if ts["_md_start"] < 0 or ts["page"] != f_page:
                continue
            tb = ts["bbox"]
            if not (tb[3] - 15 <= f_cy <= tb[1] + 15):
                continue
            if min(fb["r"], tb[2]) - max(fb["l"], tb[0]) <= 0:
                continue
            dist = abs(f_cy - (tb[1] + tb[3]) / 2)
            if dist < best_dist:
                best, best_dist = ts, dist
        f["_span"] = best

    # ── Step 4: span별 수식 그룹 → 치환 목록 ─────────────────────────────
    span_groups: dict = defaultdict(list)
    for f in inline_formulas:
        if f.get("_span"):
            span_groups[id(f["_span"])].append(f)

    replacements = []

    for span_id, formulas in span_groups.items():
        ts = formulas[0]["_span"]
        ts_text  = ts["normed"]
        ts_start = ts["_md_start"]
        ts_end   = ts["_md_end"]

        formulas.sort(key=lambda _f: -_f["_bbox"]["l"])

        for f in formulas:
            latex = normalize_latex(f["latex"])
            if not latex:
                continue
            search_text = _latex_to_search_text(latex)
            if len(search_text) == 0:
                continue
            if len(search_text) == 1 and search_text.isascii():
                continue

            flex_pat = _build_flex_pat(search_text)
            region = md[ts_start:ts_end]
            m_obj = re.search(flex_pat, region) if len(search_text) >= 2 else None
            if m_obj:
                abs_start = ts_start + m_obj.start()
                abs_end   = ts_start + m_obj.end()
                replacements.append((abs_start, abs_end, f"${latex}$"))
            else:
                fb = f["_bbox"]
                tb = ts["bbox"]
                span_width = tb[2] - tb[0]
                if span_width > 0:
                    ratio = max(0.0, min(1.0, (fb["l"] - tb[0]) / span_width))
                    insert_pos = ts_start + int(len(ts_text) * ratio)
                    end_limit = min(ts_end, ts_start + len(ts_text))
                    while insert_pos < end_limit and md[insert_pos] not in ' \n':
                        insert_pos += 1
                    replacements.append((insert_pos, insert_pos, f" ${latex}$ "))

    # ── Step 5: 겹침 제거 + 뒤에서부터 적용 ──────────────────────────────
    replacements.sort(key=lambda r: r[0])
    filtered = []
    last_end = -1
    for start, end, text in replacements:
        if start >= last_end:
            filtered.append((start, end, text))
            last_end = max(end, start + 1)

    for start, end, text in reversed(filtered):
        md = md[:start] + text + md[end:]

    n_inserted = len(filtered)
    n_match = sum(1 for s, e, _ in filtered if e > s)
    n_fallback = n_inserted - n_match
    print(f"[InlineFormula] 삽입: {n_inserted}개 (패턴매칭={n_match}, fallback={n_fallback})")
    return md


# ─── 메인 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PyMuPDF4LLM + MFD+UniMerNet 하이브리드 논문 변환 (FastHybrid)"
    )
    parser.add_argument("pdf", help="PDF 파일 경로")
    parser.add_argument("--out-dir", help="출력 디렉토리 (기본: PDF와 같은 폴더)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    out_dir  = Path(args.out_dir).resolve() if args.out_dir else pdf_path.parent
    pdf_stem = pdf_path.stem

    if not pdf_path.exists():
        print(f"오류: PDF 파일 없음: {pdf_path}")
        sys.exit(1)
    if not PYMUPDF4LLM_PY.exists():
        print(f"오류: PyMuPDF4LLM Python 없음: {PYMUPDF4LLM_PY}")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"FastHybrid 변환: {pdf_path.name}")
    print(f"  디바이스: {DEVICE}, DPI: {DPI}")
    print("=" * 60)

    t0 = time.time()

    # ── 병렬 실행 ─────────────────────────────────────────────────────────
    pymupdf_result = [None, None, None]   # md, text_spans, page_heights
    mfr_result     = [None, None]         # formulas, page_heights

    def _pymupdf4llm():
        t = time.time()
        md, spans, heights = run_pymupdf4llm(pdf_path, out_dir, pdf_stem)
        pymupdf_result[0] = md
        pymupdf_result[1] = spans or []
        pymupdf_result[2] = heights or {}
        print(f"[PyMuPDF4LLM] {time.time()-t:.1f}s 완료")

    def _unimernet():
        t = time.time()
        formulas, page_heights = run_formula_pipeline(pdf_path)
        mfr_result[0] = formulas
        mfr_result[1] = page_heights
        print(f"[UniMerNet] {time.time()-t:.1f}s 완료")

    t1 = threading.Thread(target=_pymupdf4llm, daemon=True)
    t2 = threading.Thread(target=_unimernet,   daemon=True)

    print("\n[병렬 실행 시작]")
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    elapsed_parallel = time.time() - t0
    print(f"\n[병렬 완료] {elapsed_parallel:.1f}s")

    # ── 결과 확인 ─────────────────────────────────────────────────────────
    pymupdf_md = pymupdf_result[0]
    if pymupdf_md is None:
        print("오류: PyMuPDF4LLM 변환 실패")
        sys.exit(1)

    text_spans   = pymupdf_result[1] or []
    # page_heights: PyMuPDF4LLM에서 추출한 것 우선, fallback은 MFR의 것
    page_heights = pymupdf_result[2] or mfr_result[1] or {}
    mfr_formulas = mfr_result[0] or []

    # ── 텍스트 정규화 ─────────────────────────────────────────────────────
    pymupdf_md = normalize_pymupdf4llm_md(pymupdf_md)

    # ── text_spans 정규화 ────────────────────────────────────────────────
    # Docling 전용 스텝들(리가처 문자열, /Cxx, /uXXXX)은 PyMuPDF에서 no-op
    # 실제 Unicode 리가처, NBSP, Symbol PUA, 이중공백, µm 처리는 동일하게 적용
    for ts in text_spans:
        ts["normed"] = _normalize_span_text(ts["text"])
        # soft hyphen 제거 (markdown 정규화와 일치; fitz raw에 없지만 일부 PDF에 있음)
        ts["normed"] = ts["normed"].replace('\u00AD', '')

    # ── inline 수식 삽입 (PyMuPDF4LLM 전용 함수) ──────────────────────────
    # _insert_inline_formulas_pymupdf()는 페이지별 단조증가 pos 커서를 사용:
    # - 전역 커서 대신 페이지별 커서 → footer 중복 등 순서 오류 방지
    # - 줄바꿈 하이픈 처리: 'How-' → 'However,' 매칭
    pymupdf_md = _insert_inline_formulas_pymupdf(pymupdf_md, text_spans, page_heights, mfr_formulas)

    # ── display 수식 합산 (formula slot 없음 → 모두 문서 끝에 배치) ─────────
    # merge_results()에 formula_slots가 없는 md를 전달하면
    # 모든 display 수식이 unmatched → "## 📐 수식 (위치 미매핑)" 섹션으로 추가됨
    merged_md = merge_results(pymupdf_md, [], mfr_formulas)

    # ── 저장 ──────────────────────────────────────────────────────────────
    full_path = out_dir / f"{pdf_stem}_PyMuPDF4LLM_Hybrid_Full.md"
    full_path.write_text(merged_md, encoding="utf-8")

    # 수식 JSON 저장
    formula_json_path = out_dir / f"{pdf_stem}_formulas_pymupdf.json"
    formula_json_path.write_text(
        json.dumps(mfr_formulas, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ── 통계 ──────────────────────────────────────────────────────────────
    total_elapsed = time.time() - t0
    n_display = sum(1 for f in mfr_formulas if f["type"] == "display")
    n_inline  = sum(1 for f in mfr_formulas if f["type"] == "inline")
    n_display_in_md = len(re.findall(r'^\$\$\s*$', merged_md, re.MULTILINE))
    n_inline_in_md  = len(re.findall(r'(?<!\$)\$(?!\$)[^$\n]+(?<!\$)\$(?!\$)', merged_md))

    print("\n" + "=" * 60)
    print(f"FastHybrid 완료! 총 {total_elapsed:.1f}s")
    print(f"  PyMuPDF4LLM 문자: {len(pymupdf_md):,}자")
    print(f"  병합 후 문자:     {len(merged_md):,}자")
    print(f"  수식 인식:        {len(mfr_formulas)}개 (display={n_display}, inline={n_inline})")
    print(f"  display 삽입:     {n_display_in_md}개 ($$...$$, 문서 끝)")
    print(f"  inline 삽입:      {n_inline_in_md}개 ($...$)")
    print(f"  출력:             {full_path.name}")
    print("=" * 60)


if __name__ == "__main__":
    main()
