#!/usr/bin/env python3
"""
Docling + UniMerNet 하이브리드 논문 변환 파이프라인  [hybrid_v8]

- Docling (docling venv, subprocess): 문서 구조 + 텍스트 + 표 + 그림
- MFD + UniMerNet (mineru venv, current): 수식 감지 및 LaTeX 변환
- 두 파이프라인을 병렬 실행 후 결과 병합

목표: 20초 이내 고품질 논문 Markdown 생성
벤치마크 (8논문 평균, 2026-03-05): 98.1/100

v8 변경사항 (vs v7):
  - 단일 ASCII 변수 수식 삽입 개선: span 컨텍스트 분석으로 수식 환경(수식 인접) 판단
    (이탤릭 여부는 Docling에서 미노출 → 인접 수식/변수 패턴 + 위치 기반 휴리스틱)
  - MFD 동적 신뢰도 임계값: 논문 수식 밀도에 따라 0.3~0.55 자동 조정
    (밀도 < 0.5/page → 보수적 0.55, 밀도 > 3/page → 적극적 0.3)
  - inline 수식 중복 삽입 방지: 이미 마크다운에 동일 LaTeX가 있는 span은 재삽입 건너뜀
  - 수식 LaTeX 정규화 강화: normalize_latex() 단수/복수 공백 패턴 추가 처리
  - YAML frontmatter engine 필드: "hybrid_v7" → "hybrid_v8"

v7 변경사항 (vs v6):
  - YAML frontmatter 추가: title, authors, 교신저자, 소속, email, 날짜, keyword, 대/중/소분류
  - 소속(affiliation) 본문 제거 → YAML로 이동
  - paper_taxonomy.json: 자동 분류 체계 (점진적 업데이트)
  - 아래첨자 수식 통합: $formula$ x → $formula_x$, $f$ $γ$ y → $f_y$
  - single 비ASCII 그리스 문자 fallback 방지 (span 내 미발견 시 skip)

v6 변경사항 (vs v5_speed):
  - 저자/소속(affiliation) 헤더 영역 보존 (Title→Authors→Affiliations→Body 구조)
  - merge_results: subformula deduplication (A1 중복 εx·εy 제거)
  - \textcircled{N} 수식 삽입 필터링 (B1 저자란 오삽입)
  - Figure $formula$ N. 캡션 정규화 (A2 γ 기호 오삽입)
  - "University of X (abbrev)" 보일러플레이트 패턴 추가 (D1 UST 제거)

v5_speed 변경사항 (vs v4_speed):
  - 캡션 스팬 pos 버그 수정 (캡션 내 수식 삽입 가능)
  - dynamic threshold display 보존 (cls=1, MFD 실제 클래스)
  - _normalize_span_text Step 5.5 동기화 (/hairspace)
  - x-tolerance ±10pt (컬럼 경계 수식 복구)
  - 저자/소속 boilerplate 패턴 강화 (docling_convert.py)
  - /hairspace 글리프 제거 (normalize_docling_md Step 5.5)

실행:
    scripts/envs/mineru/.venv/bin/python scripts/run_paper_hybrid.py <pdf_path>
    scripts/envs/mineru/.venv/bin/python scripts/run_paper_hybrid.py <pdf_path> --out-dir <output_dir>
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# ─── 경로 설정 ─────────────────────────────────────────────────────────────
VAULT_ROOT   = Path(__file__).parent.parent
SCRIPTS_DIR  = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))
from env_paths import get_python
from engines.postprocess import postprocess as apply_postprocess
from engines.text_normalize import normalize_text as _normalize_text
DOCLING_PY   = get_python("docling")

# v8.1: 저널별 학습 파라미터 (없으면 None으로 대체하여 파이프라인 정상 동작)
try:
    from learning.parameter_store import get_journal_params as _get_journal_params
except ImportError:
    _get_journal_params = None  # type: ignore[assignment]

# HuggingFace 캐시: 환경변수 우선, 없으면 기본값 (~/.cache/huggingface)
_HF_CACHE = Path(
    os.environ.get("HF_HOME")
    or os.environ.get("HUGGINGFACE_HUB_CACHE")
    or Path.home() / ".cache" / "huggingface"
) / "hub"
_PDFKIT_SNAP = (
    _HF_CACHE
    / "models--opendatalab--PDF-Extract-Kit-1.0"
    / "snapshots"
    / "1d9a3cd772329d0f83d84638a789296863f940f9"
)
MFD_WEIGHT = str(_PDFKIT_SNAP / "models" / "MFD" / "YOLO" / "yolo_v8_ft.pt")
MFR_PATH   = str(_PDFKIT_SNAP / "models" / "MFR" / "unimernet_hf_small_2503")

# 디바이스 자동 감지: CUDA(Windows GPU) > MPS(Apple Silicon) > CPU
def _detect_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"

DPI           = 144
DEVICE        = os.environ.get("HYBRID_DEVICE") or _detect_device()
MFD_CONF_THR      = 0.5   # MFD 기본 신뢰도 임계값 (동적 조정의 baseline; v8에서 밀도 기반 자동 튜닝)
MFR_BATCH         = 128   # UniMerNet batch size — MPS 최적값 (실측: 512→~210s/A1, 128→~24s)
_MFR_SINGLE_BATCH = 256   # UniMerNet 내부 최대 배치 크기 (상위 256개로 제한)
                          # 수식 > 256이면 3+ 배치 처리로 partial batch → trim하여 2배치 유지
_DEBUG_TIMING     = bool(os.environ.get("HYBRID_DEBUG_TIMING"))  # 세부 타이밍 출력

# v8: MFD 동적 신뢰도 임계값 범위
_MFD_CONF_MIN = 0.30   # 수식 밀도 높은 논문 (3+/page): 더 적극적으로 탐지
_MFD_CONF_MAX = 0.55   # 수식 밀도 낮은 논문 (0.5-/page): 보수적으로 탐지

# v8.1: journal_profiles.json 경로 (저널 매칭용)
_JOURNAL_PROFILES_PATH = SCRIPTS_DIR / "journal_profiles.json"


def _detect_journal_id(pdf_path: Path) -> str | None:
    """PDF 파일명 키워드를 journal_profiles.json의 filename_keywords와 매칭하여 journal_id 반환.

    매칭 실패 시 None 반환.
    """
    if not _JOURNAL_PROFILES_PATH.exists():
        return None
    try:
        with _JOURNAL_PROFILES_PATH.open("r", encoding="utf-8") as fh:
            profiles = json.load(fh).get("profiles", {})
    except Exception:
        return None

    stem_lower = pdf_path.stem.lower()
    for journal_id, profile in profiles.items():
        keywords = profile.get("match", {}).get("filename_keywords", [])
        if any(kw.lower() in stem_lower for kw in keywords):
            return journal_id
    return None


def _compute_dynamic_mfd_threshold(n_boxes: int, n_pages: int) -> float:
    """논문의 수식 밀도에 따라 MFD 신뢰도 임계값을 동적으로 조정.

    밀도 = n_boxes / n_pages
      < 0.5/page → 보수적 (0.55): false positive 최소화
      0.5 ~ 3/page → 선형 보간 (0.55 ~ 0.30)
      > 3/page    → 적극적 (0.30): 더 많은 수식 포착
    """
    if n_pages == 0:
        return MFD_CONF_THR
    density = n_boxes / n_pages
    if density <= 0.5:
        return _MFD_CONF_MAX
    if density >= 3.0:
        return _MFD_CONF_MIN
    # 선형 보간
    ratio = (density - 0.5) / (3.0 - 0.5)
    return _MFD_CONF_MAX - ratio * (_MFD_CONF_MAX - _MFD_CONF_MIN)


# ─── 텍스트 정규화 ─────────────────────────────────────────────────────────
# 정규화 로직은 engines.text_normalize 모듈로 추출됨 (위 import 참조)


def normalize_docling_md(text: str) -> str:
    """Docling 마크다운 후처리: 리가처, 특수문자, 인용 공백, 단락 병합"""
    return _normalize_text(text, merge_paragraphs=True)


# ─────────────────────────────────────────────────────────────────────────────
# Docling 변환 (subprocess → engines/docling_convert.py)
# ─────────────────────────────────────────────────────────────────────────────
DOCLING_CONVERT_SCRIPT = SCRIPTS_DIR / "engines" / "docling_convert.py"


def run_docling(pdf_path: Path, out_dir: Path, pdf_stem: str):
    """engines/docling_convert.py를 docling venv로 실행하여 마크다운 + 수식 위치 + text_spans 반환"""
    asset_dir = out_dir / f"{pdf_stem}_Hybrid_assets"
    json_path = out_dir / f"{pdf_stem}_docling_temp.json"

    # 연구실 프록시 자체 서명 인증서 우회용 환경변수 주입
    _ssl_env = os.environ.copy()
    _ssl_env["CURL_CA_BUNDLE"] = ""
    _ssl_env["REQUESTS_CA_BUNDLE"] = ""
    _ssl_env["HF_DATASETS_OFFLINE"] = "1"
    _ssl_env["PYTHONUTF8"] = "1"  # 한글 경로 인코딩 보장

    proc = subprocess.run(
        [str(DOCLING_PY), str(DOCLING_CONVERT_SCRIPT),
         str(pdf_path), str(json_path), str(asset_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_ssl_env,
    )
    if proc.returncode != 0:
        print(f"[Docling] Error:\n{proc.stderr[-800:]}")
        return None, None, None
    print(proc.stdout.strip())
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    finally:
        if json_path.exists():
            json_path.unlink()
    return data["markdown"], data.get("formula_items", []), data.get("text_spans", [])


# ─────────────────────────────────────────────────────────────────────────────
# MFD + UniMerNet 파이프라인
# ─────────────────────────────────────────────────────────────────────────────

def run_formula_pipeline(pdf_path: Path):
    """MFD + UniMerNet으로 전체 수식 인식. (formulas, page_heights) 반환"""
    import numpy as np
    import pypdfium2 as pdfium

    if _DEBUG_TIMING:
        _t = time.time()

    # 페이지 렌더링 + 높이 수집 (좌표 변환용)
    doc = pdfium.PdfDocument(str(pdf_path))
    pages_np = []
    page_heights = {}  # page_no (1-based) → height in PDF points
    scale = DPI / 72
    for i, page in enumerate(doc):
        page_heights[i + 1] = page.get_height()
        bm = page.render(scale=scale, rotation=0)
        pages_np.append(np.array(bm.to_pil().convert("RGB")))
    if _DEBUG_TIMING:
        print(f"[Timing/MFR] PDF렌더링 {len(pages_np)}페이지: {time.time()-_t:.1f}s"); _t = time.time()

    # MFD
    from mineru.model.mfd.yolo_v8 import YOLOv8MFDModel
    mfd_model = YOLOv8MFDModel(weight=MFD_WEIGHT, device=DEVICE)
    if _DEBUG_TIMING:
        print(f"[Timing/MFR] MFD 모델 로드: {time.time()-_t:.1f}s"); _t = time.time()
    mfd_results = [mfd_model.predict(p) for p in pages_np]
    total_boxes = sum(len(r.boxes) if r.boxes else 0 for r in mfd_results)
    if _DEBUG_TIMING:
        print(f"[Timing/MFR] MFD 추론({len(pages_np)}pg): {time.time()-_t:.1f}s"); _t = time.time()
    print(f"[UniMerNet] 수식 감지: {total_boxes}개")

    if total_boxes == 0:
        return [], page_heights

    # v8: MFD 동적 신뢰도 임계값 계산 (밀도 기반)
    n_pages = len(pages_np)
    dyn_thr = _compute_dynamic_mfd_threshold(total_boxes, n_pages)
    print(f"[UniMerNet] MFD 동적 임계값: {dyn_thr:.3f} (밀도={total_boxes/max(n_pages,1):.1f}/page)")

    # v8.1: 저널별 학습 파라미터 오버라이드
    if _get_journal_params is not None:
        j_params = _get_journal_params(_detect_journal_id(pdf_path))
        if j_params and j_params.get("mfd_conf_override") is not None:
            dyn_thr = j_params["mfd_conf_override"]
            print(f"  [학습] 저널 MFD 임계값 오버라이드: {dyn_thr}")

    # MFD 신뢰도 필터: score < dyn_thr 제거 → UniMerNet 처리량 감소
    filtered_mfd = []
    for r in mfd_results:
        if r.boxes and r.boxes.conf is not None and len(r.boxes) > 0:
            mask = r.boxes.conf >= dyn_thr
            filtered_mfd.append(r[mask])
        else:
            filtered_mfd.append(r)
    kept = sum(len(r.boxes) if r.boxes else 0 for r in filtered_mfd)
    print(f"[UniMerNet] MFD 필터(≥{dyn_thr:.3f}) 후: {kept}개")

    # 단일 배치 최적화: UniMerNet 내부 배치크기 = 2^floor(log2(n))
    # n > 256이면 2개 배치 처리 → 마지막 partial batch 느림 (MPS overhead)
    # MFD 클래스: cls=0('embedding'/inline), cls=1('isolated'/display)
    # display 수식(cls=1)은 항상 보존, inline(cls=0)만 트리밍
    _DISPLAY_CLS = 1  # YOLOv8 MFD: 0=embedding(inline), 1=isolated(display)
    if kept > _MFR_SINGLE_BATCH:
        n_display = sum(
            int((r.boxes.cls == _DISPLAY_CLS).sum())
            if r.boxes and r.boxes.cls is not None and len(r.boxes) > 0 else 0
            for r in filtered_mfd
        )
        inline_budget = max(0, _MFR_SINGLE_BATCH - n_display)
        inline_confs = []
        for r in filtered_mfd:
            if r.boxes and r.boxes.conf is not None and r.boxes.cls is not None and len(r.boxes) > 0:
                for cls_val, conf_val in zip(r.boxes.cls.tolist(), r.boxes.conf.tolist()):
                    if int(cls_val) != _DISPLAY_CLS:  # inline
                        inline_confs.append(conf_val)
        if len(inline_confs) > inline_budget > 0:
            cutoff = float(sorted(inline_confs, reverse=True)[inline_budget - 1])
            trimmed_mfd = []
            for r in filtered_mfd:
                if r.boxes and r.boxes.conf is not None and r.boxes.cls is not None and len(r.boxes) > 0:
                    # display(cls=1) 항상 보존, inline은 cutoff 이상만
                    mask = (r.boxes.cls == _DISPLAY_CLS) | (r.boxes.conf >= cutoff)
                    trimmed_mfd.append(r[mask])
                else:
                    trimmed_mfd.append(r)
            kept2 = sum(len(r.boxes) if r.boxes else 0 for r in trimmed_mfd)
            if kept2 <= _MFR_SINGLE_BATCH:
                filtered_mfd = trimmed_mfd
                print(f"[UniMerNet] 단일배치 최적화: {kept}→{kept2}개 (display={n_display} 보존, inline 컷오프 {cutoff:.3f})")
                kept = kept2

    # UniMerNet
    from mineru.model.mfr.unimernet.Unimernet import UnimernetModel
    mfr_model = UnimernetModel(weight_dir=MFR_PATH, _device_=DEVICE)
    if _DEBUG_TIMING:
        print(f"[Timing/MFR] UniMerNet 모델 로드: {time.time()-_t:.1f}s"); _t = time.time()
    formula_lists = mfr_model.batch_predict(filtered_mfd, pages_np, batch_size=MFR_BATCH)
    if _DEBUG_TIMING:
        print(f"[Timing/MFR] UniMerNet 추론({kept}식): {time.time()-_t:.1f}s")

    # 결과 정리
    formulas = []
    for page_idx, fl in enumerate(formula_lists):
        for f in fl:
            cat_id = f.get("category_id", 13)
            formulas.append({
                "page": page_idx + 1,
                "type": "display" if cat_id == 14 else "inline",
                "score": f.get("score", 0),
                "poly":  f.get("poly", []),
                "latex": f.get("latex", ""),
            })

    print(f"[UniMerNet] LaTeX 변환 완료: {len(formulas)}개")
    return formulas, page_heights


# ─────────────────────────────────────────────────────────────────────────────
# Inline 수식 삽입 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

# Math-italic / bold-italic Unicode 변형 → regex 문자 클래스 매핑
# PDF에서 Docling이 math-italic 유니코드(𝜈=U+1D708)를 추출하는 반면
# _latex_to_search_text()는 일반 그리스 문자(ν=U+03BD)를 반환하므로
# flex_pat 생성 시 두 코드포인트를 모두 매칭시켜야 함
_MATH_VARIANTS: dict = {
    # 일반 그리스 → [일반, math-italic(1D6FC+), math-bold-italic(1D736+), math-ss-bold-italic(1D770+)]
    'α': '(?:α|\U0001D6FC|\U0001D736|\U0001D770)',
    'β': '(?:β|\U0001D6FD|\U0001D737|\U0001D771)',
    'γ': '(?:γ|\U0001D6FE|\U0001D738|\U0001D772)',
    'δ': '(?:δ|\U0001D6FF|\U0001D739|\U0001D773)',
    'ε': '(?:ε|\U0001D700|\U0001D73A|\U0001D774)',
    'ζ': '(?:ζ|\U0001D701|\U0001D73B|\U0001D775)',
    'η': '(?:η|\U0001D702|\U0001D73C|\U0001D776)',
    'θ': '(?:θ|\U0001D703|\U0001D73D|\U0001D777)',
    'ι': '(?:ι|\U0001D704|\U0001D73E|\U0001D778)',
    'κ': '(?:κ|\U0001D705|\U0001D73F|\U0001D779)',
    'λ': '(?:λ|\U0001D706|\U0001D740|\U0001D77A)',
    'µ': '(?:µ|μ|\U0001D707|\U0001D741|\U0001D77B)',
    'ν': '(?:ν|\U0001D708|\U0001D742|\U0001D77C)',
    'ξ': '(?:ξ|\U0001D709|\U0001D743|\U0001D77D)',
    'π': '(?:π|\U0001D70B|\U0001D745|\U0001D77F)',
    'ρ': '(?:ρ|\U0001D70C|\U0001D746|\U0001D780)',
    'σ': '(?:σ|\U0001D70E|\U0001D748|\U0001D782)',
    'τ': '(?:τ|\U0001D70F|\U0001D749|\U0001D783)',
    'υ': '(?:υ|\U0001D710|\U0001D74A|\U0001D784)',
    'φ': '(?:φ|ϕ|\U0001D711|\U0001D74B|\U0001D785)',
    'χ': '(?:χ|\U0001D712|\U0001D74C|\U0001D786)',
    'ψ': '(?:ψ|\U0001D713|\U0001D74D|\U0001D787)',
    'ω': '(?:ω|\U0001D714|\U0001D74E|\U0001D788)',
}


def _build_flex_pat(search_text: str) -> str:
    """search_text의 각 문자 사이 \\s* 허용 + 그리스 문자는 math-italic 변형도 매칭."""
    parts = []
    for c in search_text:
        if c in _MATH_VARIANTS:
            parts.append(_MATH_VARIANTS[c])
        else:
            parts.append(re.escape(c))
    return r'\s*'.join(parts)


# v8: 단일 ASCII 변수 수식 삽입 허용 패턴
# 수식 환경 컨텍스트: 이미 삽입된 $...$ 수식 앞/뒤에 단일 변수가 오는 경우
# 예: "$\sigma$ = E x" → x를 $x$로 삽입 허용
# 예: "f = k w" → 맥락 없는 단독 문자 → 삽입 금지
_MATH_CONTEXT_PRE  = re.compile(r'\$[^$\n]+\$\s*[\w\+\-\*/=≈≤≥<>±~∝]?\s*$')
_MATH_CONTEXT_POST = re.compile(r'^\s*[\w\+\-\*/=≈≤≥<>±~∝]?\s*\$[^$\n]+\$')
# 단일 ASCII 변수가 수식 컨텍스트 내에 있는지 판단
# span_region: 해당 span의 마크다운 텍스트, char_start: 해당 문자의 span 내 위치
def _is_in_math_context(span_region: str, char_start: int, char: str) -> bool:
    """단일 ASCII 문자 char가 span_region[char_start] 위치에서 수식 컨텍스트에 있는지 판단.

    기준:
    1. 이미 span 내에 $...$ 수식이 하나 이상 있음 (수식이 많은 span)
    2. 직전/직후 비공백 문자가 수식 관련 기호 또는 $...$ 수식
    3. span이 짧고(< 60자) 이미 $...$가 있는 경우 (수식 위주 텍스트)
    """
    # 기준 1: span에 이미 수식이 있는가
    has_math_in_span = bool(re.search(r'\$[^$\n]+\$', span_region))

    # 기준 2: 직전/직후 컨텍스트
    pre  = span_region[:char_start].rstrip()
    post = span_region[char_start + len(char):].lstrip()

    pre_has_math  = bool(_MATH_CONTEXT_PRE.search(pre))
    post_has_math = bool(_MATH_CONTEXT_POST.match(post))

    # 직전/직후 문자가 수식 기호인가 (=, +, -, ×, ≤, ≥ 등)
    _MATH_OPS = set('=+-*/×÷≈≤≥<>±~∝∂∑∏√∫')
    pre_char  = pre[-1] if pre else ''
    post_char = post[0] if post else ''
    pre_is_op  = pre_char in _MATH_OPS
    post_is_op = post_char in _MATH_OPS

    # 기준 3: 짧은 수식 위주 span
    is_short_math_span = len(span_region) < 80 and has_math_in_span

    return (has_math_in_span and (pre_has_math or post_has_math or pre_is_op or post_is_op)) or is_short_math_span

def _normalize_span_text(text: str) -> str:
    """text_span 텍스트에 문자 수준 정규화 적용.

    normalize_docling_md()의 1~8.5단계를 모두 적용 (Step 9~10 단락 병합 제외).
    마크다운에서 md.find()로 span을 찾기 위해 동일한 변환이 필요.
    """
    return _normalize_text(text, merge_paragraphs=False)


def _latex_to_search_text(latex: str) -> str:
    """LaTeX → Docling이 추출했을 예상 Unicode 텍스트 (공백 제거, 패턴 매칭용)"""
    s = latex.strip().strip('$')
    # Greek commands → Unicode
    _GREEK_MAP = {
        r'\alpha': 'α', r'\beta': 'β', r'\gamma': 'γ', r'\delta': 'δ',
        r'\epsilon': 'ε', r'\varepsilon': 'ε', r'\zeta': 'ζ', r'\eta': 'η',
        r'\theta': 'θ', r'\vartheta': 'θ', r'\iota': 'ι', r'\kappa': 'κ',
        r'\lambda': 'λ', r'\mu': 'µ', r'\nu': 'ν', r'\xi': 'ξ',
        r'\pi': 'π', r'\varpi': 'π', r'\rho': 'ρ', r'\varrho': 'ρ',
        r'\sigma': 'σ', r'\varsigma': 'σ', r'\tau': 'τ', r'\upsilon': 'υ',
        r'\phi': 'φ', r'\varphi': 'φ', r'\chi': 'χ', r'\psi': 'ψ', r'\omega': 'ω',
        r'\Gamma': 'Γ', r'\Delta': 'Δ', r'\Theta': 'Θ', r'\Lambda': 'Λ',
        r'\Xi': 'Ξ', r'\Pi': 'Π', r'\Sigma': 'Σ', r'\Upsilon': 'Υ',
        r'\Phi': 'Φ', r'\Psi': 'Ψ', r'\Omega': 'Ω',
    }
    for cmd, char in sorted(_GREEK_MAP.items(), key=lambda x: -len(x[0])):
        s = s.replace(cmd, char)
    # \text{}, \mathrm{} 등 텍스트 명령 → 내용만
    s = re.sub(r'\\(?:text|mathrm|mathit|mathbf|mathsf|boldsymbol)\{([^}]*)\}', r'\1', s)
    # \frac{a}{b}, \tfrac{a}{b} → a/b
    s = re.sub(r'\\(?:frac|tfrac)\{([^}]*)\}\{([^}]*)\}', r'\1/\2', s)
    # 특수 기호
    _SYM_MAP = [
        (r'\times', '×'), (r'\pm', '±'), (r'\mp', '∓'), (r'\cdot', '·'),
        (r'\%', '%'), (r'\sim', '~'), (r'\approx', '≈'), (r'\simeq', '≃'),
        (r'\leq', '≤'), (r'\geq', '≥'), (r'\le', '≤'), (r'\ge', '≥'),
        (r'\neq', '≠'), (r'\ne', '≠'), (r'\circ', '°'), (r'\infty', '∞'),
        (r'\rightarrow', '→'), (r'\leftarrow', '←'), (r'\Rightarrow', '⇒'),
        (r'\propto', '∝'), (r'\partial', '∂'), (r'\nabla', '∇'),
        (r'\sqrt', '√'), (r'\degree', '°'),
    ]
    for cmd, char in _SYM_MAP:
        s = s.replace(cmd, char)
    # 나머지 LaTeX 명령 제거
    s = re.sub(r'\\[a-zA-Z]+', '', s)
    # 서식 문자 제거 (백슬래시, 중괄호, 상/하첨자)
    s = re.sub(r'[\\{}^_]', '', s)
    # 공백 제거 (문자 비교를 위해)
    s = s.replace(' ', '')
    return s


def _insert_inline_formulas(
    md: str,
    text_spans: list,
    page_heights: dict,
    mfr_formulas: list,
) -> str:
    """inline 수식을 마크다운 본문에 $latex$ 형태로 삽입"""
    from collections import defaultdict

    inline_formulas = [
        f for f in mfr_formulas
        if f["type"] == "inline" and f.get("latex")
        and f.get("score", 0) >= 0.5
    ]
    if not inline_formulas or not text_spans:
        return md

    scale = DPI / 72  # pixel → PDF point 변환 계수

    # ── Step 1: 수식 poly(픽셀) → PDF-point bbox 변환 ──────────────────────────
    for f in inline_formulas:
        poly = f["poly"]
        xs = [poly[i] for i in range(0, len(poly), 2)]
        ys = [poly[i] for i in range(1, len(poly), 2)]
        ph = page_heights.get(f["page"], 792)  # 기본 792pt (letter)
        f["_bbox"] = {
            "l": min(xs) / scale,
            "r": max(xs) / scale,
            "t": ph - min(ys) / scale,   # 픽셀 최소y → PDF 상단y
            "b": ph - max(ys) / scale,   # 픽셀 최대y → PDF 하단y
        }

    # ── Step 2: text_span 텍스트 정규화 + 마크다운 내 위치 탐색 ───────────────
    for ts in text_spans:
        ts["normed"] = _normalize_span_text(ts["text"])

    pos = 0
    for ts in text_spans:
        normed = ts["normed"]
        if ts.get("is_caption"):
            # 캡션은 마크다운에서 *...* 형태
            # 캡션 스팬은 items_list 끝에 추가되므로 pos 무관하게 처음부터 검색
            search_str = "*" + normed + "*"
            idx = md.find(search_str)
            if idx >= 0:
                ts["_md_start"] = idx + 1          # * 이후
                ts["_md_end"]   = idx + 1 + len(normed)
            else:
                ts["_md_start"] = -1
                ts["_md_end"]   = -1
        else:
            idx = md.find(normed, pos)
            if idx >= 0:
                ts["_md_start"] = idx
                ts["_md_end"]   = idx + len(normed)
                pos = idx + 1      # 다음 span 탐색 시작점 전진
            else:
                ts["_md_start"] = -1
                ts["_md_end"]   = -1

    # 진단: span 탐색 성공률
    n_spans_total = len(text_spans)
    n_spans_found = sum(1 for ts in text_spans if ts["_md_start"] >= 0)
    print(f"[InlineFormula] span 탐색: {n_spans_found}/{n_spans_total} 개 MD에서 발견")

    # ── Step 3: 수식 → text_span 공간 매칭 ────────────────────────────────────
    for f in inline_formulas:
        fb = f["_bbox"]
        f_page = f["page"]
        f_cy = (fb["t"] + fb["b"]) / 2   # 수식 중심 y (PDF points)
        best, best_dist = None, float("inf")
        for ts in text_spans:
            if ts["_md_start"] < 0 or ts["page"] != f_page:
                continue
            tb = ts["bbox"]  # [l, t, r, b] — t > b (BOTTOMLEFT origin)
            # y overlap: 수식 중심이 텍스트 span y 범위 내 (±30pt 허용)
            if not (tb[3] - 30 <= f_cy <= tb[1] + 30):
                continue
            # x overlap (±10pt 허용 — 컬럼 경계 수식 복구)
            if min(fb["r"], tb[2] + 10) - max(fb["l"] - 10, tb[0]) <= 0:
                continue
            dist = abs(f_cy - (tb[1] + tb[3]) / 2)
            if dist < best_dist:
                best, best_dist = ts, dist
        f["_span"] = best

    n_no_span = sum(1 for f in inline_formulas if f["_span"] is None)
    print(f"[InlineFormula] span 매칭: {len(inline_formulas)-n_no_span}/{len(inline_formulas)} 개 (span 없음: {n_no_span})")
    # 진단: no-span 수식 원인 (y-fail vs x-fail)
    n_y_fail = n_x_fail = 0
    for f in inline_formulas:
        if f["_span"] is not None:
            continue
        fb = f["_bbox"]
        f_cy = (fb["t"] + fb["b"]) / 2
        has_y = False
        for ts in text_spans:
            if ts["_md_start"] < 0 or ts["page"] != f["page"]:
                continue
            tb = ts["bbox"]
            if tb[3] - 30 <= f_cy <= tb[1] + 30:
                has_y = True
                break
        if has_y:
            n_x_fail += 1
        else:
            n_y_fail += 1
    print(f"[InlineFormula] no-span 원인: y범위밖={n_y_fail}, x겹침없음={n_x_fail}")

    # ── Step 4: span별 수식 그룹 → 우→좌 순서로 치환 목록 생성 ───────────────
    span_groups = defaultdict(list)
    for f in inline_formulas:
        if f.get("_span"):
            span_groups[id(f["_span"])].append(f)

    replacements = []  # (md_start, md_end, replacement_text)

    # v8: 전체 마크다운에서 이미 삽입된 inline 수식 집합 수집 (중복 삽입 방지)
    _existing_inline_latex: set = set()
    for _m in re.finditer(r'(?<!\$)\$(?!\$)([^$\n]+?)(?<!\$)\$(?!\$)', md):
        _existing_inline_latex.add(normalize_latex(_m.group(1).strip()))

    for span_id, formulas in span_groups.items():
        ts = formulas[0]["_span"]
        ts_text  = ts["normed"]
        ts_start = ts["_md_start"]
        ts_end   = ts["_md_end"]

        # x 위치 기준 우→좌 정렬 (뒤에서 앞으로 치환해 오프셋 유지)
        formulas.sort(key=lambda _f: -_f["_bbox"]["l"])

        for f in formulas:
            latex = normalize_latex(f["latex"])
            if not latex:
                continue
            # \textcircled{} 는 저자/소속 superscript 번호 (수학 수식 아님) → 건너뜀
            if r'\textcircled' in latex:
                continue
            # v8: 이미 마크다운에 동일 수식이 있으면 재삽입 건너뜀
            if normalize_latex(latex) in _existing_inline_latex:
                continue

            search_text = _latex_to_search_text(latex)
            # 단일 ASCII 문자(x, h, t 등): v8에서 수식 컨텍스트가 확인되면 삽입 허용
            # 단일 비ASCII 문자(ν, ε 등 그리스)는 fallback으로 처리
            if len(search_text) == 0:
                continue

            region = md[ts_start:ts_end]

            if len(search_text) == 1 and search_text.isascii():
                # v8: 수식 컨텍스트 확인 후 삽입 결정
                # 먼저 regex로 위치 찾기 (단어 경계 체크 포함)
                flex_pat = _build_flex_pat(search_text)
                # 단일 알파벳: 단어 경계(\b)로 검색해서 부분 매칭 방지
                bounded_pat = r'\b' + flex_pat + r'\b'
                m_ascii = re.search(bounded_pat, region)
                if m_ascii:
                    rel_pos = m_ascii.start()
                    if _is_in_math_context(region, rel_pos, search_text):
                        abs_start = ts_start + m_ascii.start()
                        abs_end   = ts_start + m_ascii.end()
                        replacements.append((abs_start, abs_end, f"${latex}$"))
                continue  # 컨텍스트 없거나 매칭 없는 경우 건너뜀

            # flexible regex: 문자 사이 공백 허용 + math-italic 변형 매칭
            flex_pat = _build_flex_pat(search_text)
            # 단일 문자도 regex 시도 (단일 비ASCII는 span 내 존재 여부로 fallback 제어)
            m = re.search(flex_pat, region) if len(search_text) >= 1 else None
            if m:
                abs_start = ts_start + m.start()
                abs_end   = ts_start + m.end()
                replacements.append((abs_start, abs_end, f"${latex}$"))
            else:
                # 단일 비ASCII(그리스 문자 등): span에서 발견 안 되면 false positive → 삽입 건너뜀
                # 예: subscript y를 γ로 오탐지 → span에 γ 없음 → 삽입 방지
                if len(search_text) == 1 and not search_text.isascii():
                    continue
                # Fallback: x 비율로 삽입 위치 추정
                fb = f["_bbox"]
                tb = ts["bbox"]
                span_width = tb[2] - tb[0]
                if span_width > 0:
                    ratio = max(0.0, min(1.0, (fb["l"] - tb[0]) / span_width))
                    insert_pos = ts_start + int(len(ts_text) * ratio)
                    # 가장 가까운 공백 경계로 이동
                    end_limit = min(ts_end, ts_start + len(ts_text))
                    while insert_pos < end_limit and md[insert_pos] not in ' \n':
                        insert_pos += 1
                    replacements.append((insert_pos, insert_pos, f" ${latex}$ "))

    # ── Step 5: 겹침 제거 + 뒤에서부터 적용 ───────────────────────────────────
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
    n_overlap_rejected = len(replacements) - n_inserted
    print(f"[InlineFormula] 삽입: {n_inserted}개 (패턴매칭={n_match}, fallback={n_fallback}, overlap거부={n_overlap_rejected})")
    return md


# ─────────────────────────────────────────────────────────────────────────────
# 결과 병합
# ─────────────────────────────────────────────────────────────────────────────

def normalize_latex(s: str) -> str:
    """LaTeX 공백 정규화 (UniMerNet 출력 특성)"""
    # "3 2 \mu" → "32\mu", "9 9 . 7 5 \%" → "99.75\%"
    # v8: 추가 패턴 처리
    # 숫자 사이 공백 제거 (3회 적용으로 긴 숫자열 처리)
    s = re.sub(r'(\d) (\d)', r'\1\2', s)
    s = re.sub(r'(\d) (\d)', r'\1\2', s)  # 두번 적용 (3 2 4 → 324)
    s = re.sub(r'(\d) (\d)', r'\1\2', s)  # 세번 적용 (긴 숫자열)
    # 소수점 공백 제거
    s = re.sub(r'(\d) \. (\d)', r'\1.\2', s)
    # v8: "\mathrm { cm }" → "\mathrm{cm}" 과도한 공백 정규화
    s = re.sub(r'(\\[a-zA-Z]+)\s*\{\s*([^}]*?)\s*\}', lambda m: m.group(1) + '{' + re.sub(r'\s+', ' ', m.group(2)).strip() + '}', s)
    # v8: "10 ^ { - 3 }" → "10^{-3}" 지수 공백 정규화
    s = re.sub(r'\^\s*\{\s*([^}]*?)\s*\}', lambda m: '^{' + re.sub(r'\s+', '', m.group(1)) + '}', s)
    s = re.sub(r'_\s*\{\s*([^}]*?)\s*\}', lambda m: '_{' + re.sub(r'\s+', '', m.group(1)) + '}', s)
    return s.strip()


def merge_results(docling_md: str, docling_formulas: list, mfr_formulas: list) -> str:
    """Docling 마크다운 + UniMerNet display 수식 병합 (본문 위치에 삽입)

    Docling이 FormulaItem 위치에 삽입한 <!-- formula-slot:p{page}:{rank}:... --> 마커를
    UniMerNet display 수식의 LaTeX로 교체한다.

    매핑 전략:
      - 페이지별로 묶어서, Docling 마커 순서(≈ 문서 읽기 순서) ↔
        UniMerNet display 수식 y-상단 순(위→아래) 을 순서대로 1:1 대응
      - 마커보다 수식이 많으면(= Docling이 감지 못한 수식) 문서 끝에 fallback 추가
      - 수식보다 마커가 많으면 해당 마커를 빈 줄로 제거
    """
    from collections import defaultdict

    # display 수식만 사용 (inline은 본문에 자연 삽입 불가)
    display_formulas = [f for f in mfr_formulas if f["type"] == "display" and f["latex"]]

    if not display_formulas:
        # 마커만 제거하고 반환
        result = re.sub(r'\n?<!-- formula-slot:[^\n]+ -->\n?', '\n', docling_md)
        return result

    # ── 중복(subformula) 제거: A 수식의 LaTeX가 B 수식에 부분 포함이면 A 제거 ───
    def _remove_subformulas(formulas: list) -> list:
        """같은 페이지 내, LaTeX 내용이 다른 수식에 완전 포함된 수식 제거.
        예: '\\varepsilon_x = ...' 가 '\\begin{array}...\\varepsilon_x = ...\\end{array}' 안에 있으면 제거.
        최소 20자 이상 수식에만 적용 (짧은 수식 오삭제 방지).
        """
        def _ws_strip(s):
            s = re.sub(r'\s+', '', s)
            # \mathrm{x} / \text{x} → x (포맷 차이 무시)
            s = re.sub(r'\\(?:mathrm|text|mathit|mathbf)\{(\w+)\}', r'\1', s)
            return s
        norms = [_ws_strip(f["latex"]) for f in formulas]
        keep = [True] * len(formulas)
        for i in range(len(formulas)):
            if len(norms[i]) < 20:
                continue
            for j in range(len(formulas)):
                if i == j or not keep[j] or len(norms[j]) <= len(norms[i]):
                    continue
                if norms[i] in norms[j]:
                    keep[i] = False
                    print(f"[merge] 중복 수식 제거 (subformula): {formulas[i]['latex'][:60]}")
                    break
        return [f for f, k in zip(formulas, keep) if k]

    # ── UniMerNet display 수식을 페이지별 y-상단 순으로 정렬 ─────────────────
    def _mfr_y_top(f):
        """UniMerNet poly (픽셀좌표) 에서 최소 y = 가장 위쪽 위치"""
        poly = f.get("poly", [])
        ys = [poly[i] for i in range(1, len(poly), 2)]
        return min(ys) if ys else 0

    mfr_by_page: dict = defaultdict(list)
    for f in display_formulas:
        mfr_by_page[f["page"]].append(f)
    for page_no in mfr_by_page:
        mfr_by_page[page_no] = _remove_subformulas(mfr_by_page[page_no])
        mfr_by_page[page_no].sort(key=_mfr_y_top)

    # ── Docling 마커를 페이지별로 수집 (마크다운 등장 순서 유지) ──────────────
    SLOT_PAT = re.compile(r'<!-- formula-slot:p(\d+):(\d+):[^>]+ -->')
    slot_by_page: dict = defaultdict(list)
    for m in SLOT_PAT.finditer(docling_md):
        page = int(m.group(1))
        slot_by_page[page].append(m.group(0))   # 전체 마커 문자열

    # ── 페이지별 매핑 및 치환 ─────────────────────────────────────────────────
    result = docling_md
    unmatched_formulas = []

    for page_no in sorted(set(list(mfr_by_page.keys()) + list(slot_by_page.keys()))):
        slots    = slot_by_page.get(page_no, [])
        formulas = mfr_by_page.get(page_no, [])
        n_match  = min(len(slots), len(formulas))

        # 1:1 치환
        for i in range(n_match):
            latex = normalize_latex(formulas[i]["latex"])
            replacement = f"$$\n{latex}\n$$" if latex else ""
            result = result.replace(slots[i], replacement, 1)

        # 미매핑 마커 → 제거
        for i in range(n_match, len(slots)):
            result = result.replace(slots[i], "", 1)

        # 미매핑 수식 → fallback (이미 본문에 있는 LaTeX는 제외)
        for f in formulas[n_match:]:
            latex = normalize_latex(f["latex"])
            if latex and latex in result:
                print(f"[merge] fallback 제외 (이미 본문에 존재): {latex[:60]}")
                continue
            unmatched_formulas.append(f)

    # ── 남은 마커 일괄 제거 ───────────────────────────────────────────────────
    result = SLOT_PAT.sub("", result)

    # ── fallback: 매핑 못한 display 수식을 문서 끝에 추가 ────────────────────
    if unmatched_formulas:
        by_page_extra: dict = defaultdict(list)
        for f in unmatched_formulas:
            by_page_extra[f["page"]].append(f)
        extra_md = "\n\n---\n\n## 📐 수식 (위치 미매핑)\n"
        for pg in sorted(by_page_extra.keys()):
            extra_md += f"\n### Page {pg}\n\n"
            for f in by_page_extra[pg]:
                latex = normalize_latex(f["latex"])
                if latex:
                    extra_md += f"$$\n{latex}\n$$\n\n"
        result += extra_md

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 수식 아래첨자 후처리
# ─────────────────────────────────────────────────────────────────────────────

def _fix_formula_subscripts(md: str) -> str:
    """inline 수식 삽입 후 분리된 아래첨자를 LaTeX 표기로 통합.

    처리 대상:
      1. $\\greek$ N   → $\\greek_N$   (bare 그리스 문자 + digit subscript)
      2. $\\greek$ x   → $\\greek_x$   (bare 그리스 문자 + single-letter subscript)
      3. $\\greek1$ $\\greek2$ x → $\\greek1_x$  (spurious 그리스 문자 제거)
      4. l 0, h 0 (일반 텍스트) → $l_0$, $h_0$

    주의: 복잡한 수식($\\varepsilon_{axial}$, $\\gamma\\cdot$ 등)은 건드리지 않음.
          bare 그리스 문자 단독($\\alpha$, $\\theta$ 등)에만 적용.
    """
    # bare 그리스 문자 수식 패턴: $\greek$ (수식 내 _ { } 등 없어야 함)
    _BARE_GREEK = (
        r'\$\s*\\(?:alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|'
        r'iota|kappa|lambda|mu|nu|xi|pi|varpi|rho|sigma|tau|upsilon|phi|varphi|'
        r'chi|psi|omega)\s*\$'
    )
    _SINGLE_GREEK = _BARE_GREEK  # alias (Case 3용)

    # ── Case 3: $bare_greek1$ $bare_greek2$ letter → $bare_greek1_{letter}$
    #   예: $\varepsilon$ $\gamma$ y → $\varepsilon_y$
    #   (MFD가 subscript y 를 γ 로 오탐지한 경우)
    md = re.sub(
        rf'({_BARE_GREEK})\s+{_SINGLE_GREEK}\s+([a-zA-Z0-9])(?![a-zA-Z0-9])',
        lambda m: f'${m.group(1)[1:-1]}_{{{m.group(2)}}}$',
        md,
        flags=re.IGNORECASE,
    )

    # ── Case 1 & 2: $bare_greek$ char → $bare_greek_{char}$
    #   bare 그리스 문자(내부에 _ { } \ 없는 것)에만 적용
    #   예: $\theta$ 0 → $\theta_0$  /  $\varepsilon$ x → $\varepsilon_x$
    #   예외: $\varepsilon_{axial}$ l → 미적용 (이미 subscript 있음)
    #         $\gamma\cdot$ e → 미적용 (modifier 있음)
    md = re.sub(
        rf'({_BARE_GREEK})\s+([a-zA-Z0-9])(?![a-zA-Z0-9])',
        lambda m: f'${m.group(1)[1:-1]}_{{{m.group(2)}}}$',
        md,
        flags=re.IGNORECASE,
    )

    # ── 일반 텍스트 단변수 아래첨자: l 0, h 0 → $l_0$, $h_0$
    #   조건: 소문자 단독(앞에 영문자/$없음) + 공백 + 숫자 + (문장부호/공백 뒤, 소수점 아님)
    md = re.sub(
        r'(?<![a-zA-Z$\d])([a-z]) ([0-9])(?!\.[0-9])(?=\s*[,\.;\)\s]|$)',
        r'$\1_{\2}$',
        md,
    )

    return md


# ─────────────────────────────────────────────────────────────────────────────
# 논문 메타데이터 추출 + YAML 생성
# ─────────────────────────────────────────────────────────────────────────────

import datetime as _datetime
import json as _json

_TAXONOMY_PATH = SCRIPTS_DIR / "paper_taxonomy.json"

_AFFIL_KW = re.compile(
    r'(?:University|Institute|College|School|Department|Center|Centre|Laboratory|'
    r'Lab\b|Hospital|Foundation|Universit[éä]|Institut\b|Research)',
    re.IGNORECASE,
)
_EMAIL_PAT  = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z]{2,}\b', re.IGNORECASE)
_EMAIL_LINE = re.compile(r'^E-?mail[:\s]+\S+', re.IGNORECASE | re.MULTILINE)

# 첫 본문 섹션 헤딩 패턴
_BODY_SECT_PAT = re.compile(
    r'^#{1,3}\s+(?:\d+\.?\s+)?'
    r'(?:Introduction|Abstract|Methods?|Results?|Discussion|Conclusion|'
    r'Experimental|Materials|Background|INTRODUCTION|METHODS?|RESULTS?|'
    r'DISCUSSION|CONCLUSION|EXPERIMENTAL|MATERIALS)',
    re.MULTILINE | re.IGNORECASE,
)


def _extract_paper_metadata(md: str) -> dict:
    """마크다운 본문에서 논문 메타데이터 추출."""
    meta = {
        "title": "",
        "first_author": "",
        "authors": [],
        "corresponding_authors": [],
        "affiliations": [],
        "corresponding_affiliation": "",
        "email": "",
        "submission_date": "",
        "publication_date": "",
        "keywords": [],
        "category": {"major": "", "middle": "", "minor": ""},
    }

    lines = md.split('\n')

    # ── 제목: 첫 번째 ## 헤딩 (숫자 섹션 제외)
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('## ') and not re.match(r'^##\s+\d+[\.\s]', stripped):
            meta["title"] = stripped[3:].strip()
            break

    # ── 저자줄: 제목 다음 첫 비어있지 않은 줄
    author_line = ""
    found_title = False
    for line in lines:
        stripped = line.strip()
        if not found_title:
            if meta["title"] and stripped.endswith(meta["title"]):
                found_title = True
            continue
        if stripped:
            author_line = stripped
            break

    # ── 저자 파싱 (*, ✉, † 교신저자 마커)
    if author_line:
        # "Name,*" → "Name*," 정규화 (콤마 뒤에 마커가 오는 경우)
        clean = re.sub(r'(\w),\s*([*✉†]+)', r'\1\2,', author_line)
        clean = re.sub(r'\s+\d+(?:,\d+)*\b', '', clean)   # 소속번호 제거 "1,7"
        parts = re.split(r',\s*|\s+and\s+', clean)
        authors = []
        corresponding = []
        for name in parts:
            name = name.strip()
            # 분리 잔류 "and " 접두어 제거
            name = re.sub(r'^and\s+', '', name, flags=re.IGNORECASE)
            if not name:
                continue
            is_corr = bool(re.search(r'[*✉†]', name))
            name_clean = re.sub(r'[*✉†]+', '', name).strip()
            if name_clean:
                authors.append(name_clean)
                if is_corr:
                    corresponding.append(name_clean)
        meta["authors"] = authors
        meta["corresponding_authors"] = corresponding
        if authors:
            meta["first_author"] = authors[0]

    # ── 소속 블록 감지
    # 소속존 = 저자줄 이후 ~ Abstract 첫 문장 이전 (짧은 헤더 영역만)
    # Nature 등 Abstract 헤딩 없는 저널: 첫 긴 단락(>200자)이 Abstract 시작
    body_m = _BODY_SECT_PAT.search(md)
    pre_body = md[:body_m.start()] if body_m else md

    # 단락 분리
    paras = [p.strip() for p in re.split(r'\n{2,}', pre_body) if p.strip()]

    # Abstract 시작 인덱스 탐색 (첫 200자+ 단락 = Abstract 본문)
    abstract_idx = len(paras)  # 기본: 모든 단락이 헤더존
    for i, p in enumerate(paras):
        if len(p) > 200 and not p.startswith('#') and not _AFFIL_KW.search(p):
            abstract_idx = i
            break

    # 소속존: title/author 이후 ~ abstract 이전 단락만
    header_paras = paras[:abstract_idx]
    # 키워드는 abstract 이후에도 나올 수 있으므로 전체 pre_body 스캔
    all_pre_paras = paras

    affil_paras = []
    email_found = ""

    for para in header_paras:
        # 제목/저자줄/헤딩 제외
        if para.startswith('#') or (meta["title"] and meta["title"] in para):
            continue
        if author_line and author_line in para:
            continue
        # E-mail 단독 줄
        if _EMAIL_LINE.match(para) and len(para) < 80:
            em = _EMAIL_PAT.search(para)
            if em and not email_found:
                email_found = em.group(0)
            continue
        # 소속 감지: 기관명 키워드 OR 이메일 OR 짧은 단락(<200자, 헤더존 내)
        is_affil = (
            _AFFIL_KW.search(para)
            or _EMAIL_PAT.search(para)
            or len(para) <= 200  # 소속존의 짧은 단락 (도시/국가, 약칭 저자 등)
        )
        if is_affil:
            affil_paras.append(para)
            em = _EMAIL_PAT.search(para)
            if em and not email_found:
                email_found = em.group(0)

    # 키워드: pre_body 전체 스캔 (소속존 밖에도 위치 가능)
    for para in all_pre_paras:
        kw_m = re.match(r'^Key\s*words?[:\s]+(.+)', para, re.IGNORECASE)
        if kw_m:
            kws = [k.strip().rstrip(';,') for k in re.split(r'[;,]', kw_m.group(1)) if k.strip()]
            if kws:
                meta["keywords"] = kws[:5]
            break

    meta["affiliations"] = affil_paras
    meta["email"] = email_found

    # 교신저자 소속: 이메일 포함 블록 (이메일 제거 후)
    for ap in affil_paras:
        if _EMAIL_PAT.search(ap):
            meta["corresponding_affiliation"] = re.sub(
                r'\s*E-?mail[:\s]+\S+', '', ap, flags=re.IGNORECASE
            ).strip()
            break

    # 자동 분류
    abstract_text = " ".join(p for p in paras if len(p) > 200)
    meta["category"] = _classify_paper(meta["title"], abstract_text)

    return meta


def _classify_paper(title: str, abstract: str) -> dict:
    """제목+초록 키워드 매칭으로 분류 자동 추정."""
    if not _TAXONOMY_PATH.exists():
        return {"major": "", "middle": "", "minor": ""}

    try:
        taxonomy = _json.loads(_TAXONOMY_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {"major": "", "middle": "", "minor": ""}

    text_lower = (title + " " + abstract).lower()
    kw_rules = taxonomy.get("auto_classify_keywords", {})

    best_major, best_middle, best_score = "", "", 0
    for major, middles in kw_rules.items():
        for middle, kws in middles.items():
            score = sum(1 for kw in kws if kw.lower() in text_lower)
            if score > best_score:
                best_score, best_major, best_middle = score, major, middle

    minor_rules = taxonomy.get("auto_classify_minor", {})
    best_minor, best_minor_score = "", 0
    for mn, kws in minor_rules.items():
        s = sum(1 for kw in kws if kw.lower() in text_lower)
        if s > best_minor_score:
            best_minor_score, best_minor = s, mn

    return {"major": best_major, "middle": best_middle, "minor": best_minor}


def _remove_affiliations_from_body(md: str, affiliations: list) -> str:
    """본문에서 소속 블록 및 E-mail 줄 제거."""
    for aff in affiliations:
        escaped = re.escape(aff)
        # 빈 줄로 둘러싸인 블록 제거
        md = re.sub(r'\n\n' + escaped + r'\n\n', '\n\n', md)
        md = re.sub(r'\n\n' + escaped + r'\n',   '\n\n', md)
        md = re.sub(r'\n'   + escaped + r'\n\n', '\n',   md)
        md = md.replace('\n\n' + aff + '\n\n', '\n\n')
        md = md.replace('\n\n' + aff + '\n',   '\n\n')

    # 남은 E-mail: 단독 줄 제거
    md = re.sub(r'\n\nE-?mail[:\s]+\S+[ \t]*\n', '\n\n', md, flags=re.IGNORECASE)
    md = re.sub(r'\nE-?mail[:\s]+\S+[ \t]*\n',   '\n',   md, flags=re.IGNORECASE)

    # 연속 빈 줄 정리
    md = re.sub(r'\n{3,}', '\n\n', md)
    return md


def _build_yaml_frontmatter(meta: dict, paper_key: str = "") -> str:
    """Obsidian YAML frontmatter 생성."""
    today = _datetime.date.today().isoformat()

    def _yl(items: list, indent: int = 2) -> str:
        """YAML 리스트 포맷 (빈 리스트는 [] 반환)."""
        if not items:
            return "[]"
        sp = ' ' * indent
        return '\n' + '\n'.join(f'{sp}- "{_yaml_esc(x)}"' for x in items)

    def _yaml_esc(s: str) -> str:
        return s.replace('"', '\\"')

    cat = meta.get("category", {})
    kw_list = meta.get("keywords", [])
    kw_yaml = _yl(kw_list) if kw_list else "[]"

    lines = [
        "---",
        f'title: "{_yaml_esc(meta["title"])}"',
        f'paper_key: "{paper_key}"',
        "",
        f'first_author: "{_yaml_esc(meta["first_author"])}"',
        f'authors:{_yl(meta["authors"])}',
        f'corresponding_authors:{_yl(meta["corresponding_authors"])}',
        f'corresponding_affiliation: "{_yaml_esc(meta["corresponding_affiliation"])}"',
        f'email: "{meta["email"]}"',
        f'affiliations:{_yl(meta["affiliations"])}',
        "",
        f'submission_date: "{meta["submission_date"]}"',
        f'publication_date: "{meta["publication_date"]}"',
        f'keywords: {kw_yaml}',
        "",
        f'category_major: "{cat.get("major", "")}"',
        f'category_middle: "{cat.get("middle", "")}"',
        f'category_minor: "{cat.get("minor", "")}"',
        "",
        f'engine: "hybrid_v8"',
        f'converted_date: "{today}"',
        "---",
        "",
    ]
    return '\n'.join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Docling + UniMerNet 하이브리드 논문 변환")
    parser.add_argument("pdf", help="PDF 파일 경로")
    parser.add_argument("--out-dir", help="출력 디렉토리 (기본: PDF와 같은 폴더)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    out_dir  = Path(args.out_dir).resolve() if args.out_dir else pdf_path.parent
    pdf_stem = pdf_path.stem

    if not pdf_path.exists():
        print(f"오류: PDF 파일 없음: {pdf_path}")
        sys.exit(1)
    if not DOCLING_PY.exists():
        print(f"오류: Docling Python 없음: {DOCLING_PY}")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"하이브리드 변환 [hybrid_v8]: {pdf_path.name}")
    print("=" * 60)

    t0 = time.time()

    # ── 병렬 실행 ──────────────────────────────────────────────────────────────
    docling_result = [None, None, None]  # md, formulas, text_spans
    mfr_result     = [None, None]        # formulas, page_heights

    def _docling():
        t = time.time()
        md, forms, spans = run_docling(pdf_path, out_dir, pdf_stem)
        docling_result[0] = md
        docling_result[1] = forms or []
        docling_result[2] = spans or []
        print(f"[Docling] {time.time()-t:.1f}s 완료")

    def _unimernet():
        t = time.time()
        formulas, page_heights = run_formula_pipeline(pdf_path)
        mfr_result[0] = formulas
        mfr_result[1] = page_heights
        print(f"[UniMerNet] {time.time()-t:.1f}s 완료")

    t1 = threading.Thread(target=_docling, daemon=True)
    t2 = threading.Thread(target=_unimernet, daemon=True)

    print("\n[병렬 실행 시작]")
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    elapsed_parallel = time.time() - t0
    print(f"\n[병렬 완료] {elapsed_parallel:.1f}s")

    # ── 결과 확인 ──────────────────────────────────────────────────────────────
    docling_md = docling_result[0]
    if docling_md is None:
        print("오류: Docling 변환 실패")
        sys.exit(1)

    # ── 텍스트 정규화 ──────────────────────────────────────────────────────────
    if _DEBUG_TIMING:
        _tp = time.time()
    docling_md = normalize_docling_md(docling_md)
    if _DEBUG_TIMING:
        print(f"[Timing] normalize: {time.time()-_tp:.2f}s"); _tp = time.time()

    mfr_formulas = mfr_result[0] or []
    text_spans   = docling_result[2] or []
    page_heights = mfr_result[1] or {}

    # ── inline 수식 삽입 (정규화 후, display 병합 전) ──────────────────────────
    docling_md = _insert_inline_formulas(docling_md, text_spans, page_heights, mfr_formulas)
    if _DEBUG_TIMING:
        print(f"[Timing] insert_inline: {time.time()-_tp:.2f}s"); _tp = time.time()

    # ── caption 내 Figure 번호 오염 수정 ───────────────────────────────────────
    # "Figure $\gamma \mathrm{.}$ 1." → "Figure 1."  (fallback 삽입이 그림번호 앞에 위치한 경우)
    docling_md = re.sub(
        r'((?:Figure|Fig\.?)\s+)\$[^$\n]{1,80}\$[ \t]*(\d)',
        r'\1\2',
        docling_md,
    )

    # ── 아래첨자 수식 통합: $formula$ x → $formula_x$ ─────────────────────────
    docling_md = _fix_formula_subscripts(docling_md)

    # ── display 수식 병합 ──────────────────────────────────────────────────────
    merged_md = merge_results(docling_md, docling_result[1], mfr_formulas)
    if _DEBUG_TIMING:
        print(f"[Timing] merge_results: {time.time()-_tp:.2f}s"); _tp = time.time()

    # ── 통합 후처리 (Layer 1 + Layer 2 journal_paper, hybrid 모드) ─────────────
    merged_md = apply_postprocess(merged_md, engine="hybrid", doc_type="journal_paper")
    if _DEBUG_TIMING:
        print(f"[Timing] postprocess: {time.time()-_tp:.2f}s"); _tp = time.time()

    # ── 아래첨자 수식 통합: merge/postprocess 이후 잔류 패턴 재처리 ─────────────
    merged_md = _fix_formula_subscripts(merged_md)

    # ── 메타데이터 추출 + 소속 제거 + YAML 생성 ───────────────────────────────
    meta = _extract_paper_metadata(merged_md)
    merged_md = _remove_affiliations_from_body(merged_md, meta["affiliations"])
    if _DEBUG_TIMING:
        print(f"[Timing] metadata+affil: {time.time()-_tp:.2f}s")

    # paper_key: PDF 파일명이 규칙에 맞으면 그대로 사용
    paper_key = pdf_stem
    yaml_header = _build_yaml_frontmatter(meta, paper_key=paper_key)
    merged_md = yaml_header + merged_md

    print(f"\n[메타데이터] 제목: {meta['title'][:60]}...")
    print(f"[메타데이터] 저자: {len(meta['authors'])}명, 교신저자: {meta['corresponding_authors']}")
    print(f"[메타데이터] 소속 {len(meta['affiliations'])}블록 추출 → YAML 이동")
    print(f"[메타데이터] 분류: {meta['category']}")

    # ── 저장 ──────────────────────────────────────────────────────────────────
    full_path = out_dir / f"{pdf_stem}_Hybrid_Full.md"
    full_path.write_text(merged_md, encoding="utf-8")

    # 수식 JSON 저장
    formula_json_path = out_dir / f"{pdf_stem}_formulas.json"
    formula_json_path.write_text(
        json.dumps(mfr_formulas, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ── 통계 ──────────────────────────────────────────────────────────────────
    total_elapsed = time.time() - t0
    n_display = sum(1 for f in mfr_formulas if f["type"] == "display")
    n_inline  = sum(1 for f in mfr_formulas if f["type"] == "inline")
    # 삽입된 display 수식 블록 수
    n_display_inserted = len(re.findall(r'^\$\$\s*$', merged_md, re.MULTILINE))
    # 삽입된 inline 수식 수 ($...$, $$...$$ 제외)
    n_inline_inserted = len(re.findall(r'(?<!\$)\$(?!\$)[^$\n]+(?<!\$)\$(?!\$)', merged_md))

    print("\n" + "=" * 60)
    print(f"완료! 총 {total_elapsed:.1f}s")
    print(f"  Docling 문자:    {len(docling_md):,}자")
    print(f"  병합 후 문자:    {len(merged_md):,}자")
    print(f"  수식 인식:       {len(mfr_formulas)}개 (display={n_display}, inline={n_inline})")
    print(f"  display 삽입:    {n_display_inserted}개 ($$...$$)")
    print(f"  inline 삽입:     {n_inline_inserted}개 ($...$)")
    print(f"  출력:            {full_path.name}")
    print(f"  수식 JSON:       {formula_json_path.name}")
    print("=" * 60)


if __name__ == "__main__":
    main()
