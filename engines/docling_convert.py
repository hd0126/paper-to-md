#!/usr/bin/env python3
"""
Docling 논문 변환 스크립트 (docling venv에서 실행)

benchmark_mac.py의 run_docling() 로직을 독립 스크립트로 분리.
하이브리드 파이프라인(run_paper_hybrid.py)에서 subprocess로 호출됨.

주요 기능:
  1. 서브패널 중복 추출 제거 (MIN_AREA_RATIO=20%, MIN_ABS_AREA=40×40)
  2. 그림 내부/주변 텍스트 필터링 (패널 레이블 A,B,C 등)
  3. Figure 캡션 그룹핑 (이미지 바로 다음에 배치)
  4. FormulaItem 위치 정보 수집

실행:
  {docling_venv}/bin/python docling_convert.py <pdf_path> <out_json> <asset_dir>

출력 JSON:
  {
    "markdown": "...",
    "formula_items": [...],
    "n_images_raw": 26,
    "n_images_saved": 6,
  }
"""

import json
import re
import ssl
import sys
from pathlib import Path

# 연구실 프록시 자체 서명 인증서 우회 (SSL 패치)
ssl._create_default_https_context = ssl._create_unverified_context
try:
    import requests
    _orig_request = requests.Session.request
    def _patched_request(self, *args, **kwargs):
        kwargs.setdefault("verify", False)
        return _orig_request(self, *args, **kwargs)
    requests.Session.request = _patched_request
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

import pypdfium2 as pdfium

pdf_path  = sys.argv[1]
out_json  = sys.argv[2]
asset_dir = sys.argv[3]

# ─────────────────────────────────────────────────────────────────────────────
# Docling 임포트
# ─────────────────────────────────────────────────────────────────────────────
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling_core.types.doc import (
    PictureItem, FormulaItem, TableItem,
    TextItem, SectionHeaderItem, ListItem, DocItemLabel
)

# ─────────────────────────────────────────────────────────────────────────────
# 변환
# ─────────────────────────────────────────────────────────────────────────────
from docling.datamodel.pipeline_options import (
    AcceleratorOptions, AcceleratorDevice,
    TableFormerMode, TableStructureOptions,
)

pipeline_options = PdfPipelineOptions()
pipeline_options.generate_picture_images = True
pipeline_options.generate_page_images    = False
pipeline_options.images_scale            = 2.5    # ~180 DPI (레이아웃 감지용만, 저장에는 미사용 → 속도 최적화)
# v8.1 속도 최적화: 디지털 PDF는 OCR 불필요, TableFormer FAST 모드
pipeline_options.do_ocr = False                   # born-digital PDF → OCR 비활성화 (~30-50% 속도 향상)
pipeline_options.table_structure_options = TableStructureOptions(
    mode=TableFormerMode.FAST                     # ACCURATE → FAST (~10-30% 향상)
)
pipeline_options.accelerator_options = AcceleratorOptions(
    num_threads=4,
    device=AcceleratorDevice.AUTO,                # CUDA 자동 감지, 없으면 CPU fallback
)
pipeline_options.layout_batch_size = 16           # 기본값 4 → 16 (RTX 3070 8GB VRAM 활용)

# 고해상도 이미지 추출용 pypdfium2 PDF 오픈 (Step 3에서 사용)
_FIGURE_DPI   = 600          # 저장 DPI (300 → 600으로 향상)
_FIGURE_SCALE = _FIGURE_DPI / 72
_FIGURE_PAD   = 5            # bbox 패딩 (PDF points)
_pdf_doc = pdfium.PdfDocument(pdf_path)

converter = DocumentConverter(
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
)
conv_result = converter.convert(pdf_path)
doc = conv_result.document

asset_folder = Path(asset_dir).name
Path(asset_dir).mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: PictureItem 수집 + bbox 수집
# ─────────────────────────────────────────────────────────────────────────────
raw_pics = []
figure_bboxes_by_page: dict = {}  # page_no → list of expanded bboxes

for item, _ in doc.iterate_items():
    if isinstance(item, PictureItem):
        img     = item.image.pil_image if item.image else None
        page_no = item.prov[0].page_no if item.prov else -1
        area_px = (img.size[0] * img.size[1]) if img else 0
        raw_pics.append({
            "item": item,
            "img":  img,
            "page": page_no,
            "area_px": area_px,
            "provs": [(p.page_no, p.bbox) for p in item.prov],
        })
        for prov in item.prov:
            pg = prov.page_no
            b  = prov.bbox
            figure_bboxes_by_page.setdefault(pg, []).append({
                "l": b.l - 50,
                "r": b.r + 50,
                "b": b.b - 10,
                "t": b.t + 100,
            })

total_raw = len(raw_pics)

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: 서브패널 판정 및 필터링
# ─────────────────────────────────────────────────────────────────────────────
MIN_ABS_AREA   = 40 * 40   # 1,600 px²
MIN_AREA_RATIO = 0.20      # 전체 최대 면적의 20%

max_area = max((p["area_px"] for p in raw_pics), default=1)
area_threshold = max(MIN_ABS_AREA, max_area * MIN_AREA_RATIO)

page_max_area: dict = {}
for p in raw_pics:
    pg = p["page"]
    page_max_area[pg] = max(page_max_area.get(pg, 0), p["area_px"])

skip_indices = set()
for i, p in enumerate(raw_pics):
    if p["img"] is None:
        skip_indices.add(i)
        continue
    if p["area_px"] < MIN_ABS_AREA:
        skip_indices.add(i)
        continue
    pg = p["page"]
    page_has_real_figure = (page_max_area[pg] >= area_threshold)
    is_page_max = page_has_real_figure and (p["area_px"] >= page_max_area[pg] * 0.95)
    if not is_page_max and p["area_px"] < area_threshold:
        skip_indices.add(i)

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: 유효 이미지 저장 (pypdfium2 600 DPI 크롭, Docling 렌더 fallback)
# ─────────────────────────────────────────────────────────────────────────────
def _extract_figure_hq(provs):
    """figure bbox를 pypdfium2로 600 DPI 렌더링해서 PIL 이미지 반환.
    실패 시 None 반환 → 호출부에서 Docling 렌더 이미지로 fallback.
    """
    if not provs:
        return None
    pg_no, bbox = provs[0]   # (page_no, Docling BoundingBox)
    try:
        page = _pdf_doc[pg_no - 1]          # pypdfium2는 0-indexed
        pw, ph = page.get_width(), page.get_height()
        # Docling BOTTOMLEFT: l < r, b < t (t가 위)
        # pypdfium2 crop=(left, bottom, right, top) — 동일 좌표계
        # pypdfium2 crop = (left_cutoff, bottom_cutoff, right_cutoff, top_cutoff)
        # 각 변에서 잘라낼 양 = bbox까지의 거리
        crop = (
            max(0, bbox.l - _FIGURE_PAD),        # 왼쪽 여백 제거
            max(0, bbox.b - _FIGURE_PAD),        # 아래쪽 여백 제거
            max(0, pw - bbox.r - _FIGURE_PAD),   # 오른쪽 여백 제거
            max(0, ph - bbox.t - _FIGURE_PAD),   # 위쪽 여백 제거
        )
        bm  = page.render(scale=_FIGURE_SCALE, crop=crop)
        return bm.to_pil().convert("RGB")
    except Exception:
        return None

image_map: dict = {}   # raw_pic index → filename
saved_count = 0
hq_count = 0
for i, p in enumerate(raw_pics):
    if i in skip_indices:
        continue
    saved_count += 1
    img_fn = f"figure_{saved_count}.png"

    hq_img = _extract_figure_hq(p["provs"])
    if hq_img is not None:
        hq_img.save(Path(asset_dir) / img_fn)
        hq_count += 1
    else:
        p["img"].save(Path(asset_dir) / img_fn)   # fallback: Docling 300 DPI
    image_map[i] = img_fn

print(f"[Docling] 원본 {total_raw}개 → 서브패널 {total_raw - saved_count}개 제거 → {saved_count}개 저장 (600DPI:{hq_count}, fallback:{saved_count-hq_count})")

# ─────────────────────────────────────────────────────────────────────────────
# Step 2.5: 보존된 이미지 y-범위 계산 (패널 레이블 필터링용)
# ─────────────────────────────────────────────────────────────────────────────
page_figure_yrange: dict = {}
for i, p in enumerate(raw_pics):
    if i in skip_indices:
        continue
    for pg, bbox in p["provs"]:
        if pg not in page_figure_yrange:
            page_figure_yrange[pg] = (bbox.b, bbox.t)
        else:
            cur_b, cur_t = page_figure_yrange[pg]
            page_figure_yrange[pg] = (min(cur_b, bbox.b), max(cur_t, bbox.t))


def _is_figure_text(chk_item, tree_depth: int) -> bool:
    """그림 내부 텍스트 또는 패널 레이블이면 True. depth >= 2(캡션)는 보존."""
    if tree_depth >= 2:
        return False
    if not getattr(chk_item, 'prov', None):
        return False
    for prov in chk_item.prov:
        pg = prov.page_no
        ib = prov.bbox
        cx = (ib.l + ib.r) / 2
        cy = (ib.b + ib.t) / 2
        if pg in figure_bboxes_by_page:
            for fb in figure_bboxes_by_page[pg]:
                if fb["l"] <= cx <= fb["r"] and fb["b"] <= cy <= fb["t"]:
                    return True
        if isinstance(chk_item, TextItem) and pg in page_figure_yrange:
            item_width = ib.r - ib.l
            if item_width < 80:
                y_bot, y_top = page_figure_yrange[pg]
                if (y_bot - 100) <= cy <= (y_top + 450):
                    return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Step 4: 마크다운 생성 (아이템 순회, 그림 내부 텍스트 제외)
#         캡션 감지: "Figure N." 또는 "Fig. N." 형태의 독립 단락
# ─────────────────────────────────────────────────────────────────────────────
# 캡션 패턴: "Fig. 1 | ..." / "Fig. 1| ..." / "Figure 1. ..." / "Fig. 1: ..."
# Nature 저널: "Fig. 2 | Caption" (숫자 뒤에 공백 있음 → \s* 필요)
_CAPTION_PAT = re.compile(
    r'^(?:Extended\s+Data\s+)?(?:Figure|Fig\.?)\s+(\d+)\s*[\.:|]',
    re.IGNORECASE
)

# 저널 템플릿 상/하단 고정 텍스트 (running header/footer) 패턴
_BOILERPLATE_PATTERNS = [
    # URL 단독 줄: www.advelectronicmat.de, www.advancedsciencenews.com
    re.compile(r'^www\.\S+\.\w{2,}$', re.IGNORECASE),
    # https?:// URL 단독 줄
    re.compile(r'^https?://\S+$', re.IGNORECASE),
    # 저널 인용 줄: "Adv. Electron. Mater. 2024 , 10 , 2400236"
    re.compile(r'^[A-Z][a-z][\w\s\.\-]+\d{4}\s*,\s*\d+\s*,\s*\w+\s*$'),
    # Wiley "SCIENCE NEWS" / Science Advances 저널 헤더
    re.compile(r'^SCIENCE\s+NEWS$', re.IGNORECASE),
    re.compile(r'^SCIENCE\s+ADVANCES', re.IGNORECASE),
    re.compile(r'^R\s+E\s+S\s+E\s+A\s+R\s+C\s+H\s+A\s+R\s+T', re.IGNORECASE),
    # DOI 단독 줄: "DOI: 10.1002/..." or "https://doi.org/..."
    re.compile(r'^DOI:\s*10\.\d{4,}/', re.IGNORECASE),
    # 저작권 줄: "© 2024 Wiley-VCH GmbH"
    re.compile(r'^©\s*\d{4}'),
    # Nature 저널 제목 헤딩 (nature electronics, nature materials 등)
    re.compile(r'^nature\s+\w[\w\s]*$', re.IGNORECASE),
    # 단독 "Article" / "Research Article" 헤딩
    re.compile(r'^(?:Research\s+)?Article$', re.IGNORECASE),
    # 온라인 보기 링크 줄 (AAAS/Science Advances)
    re.compile(r'^View the article online', re.IGNORECASE),
    # "Permissions" 단독 줄
    re.compile(r'^Permissions$', re.IGNORECASE),
    # 저자 소속 줄: "1 Department of ...", "2 School of ...", "3 Institute of ..." (Nature 스타일, 번호 있음)
    re.compile(r'^\d+\s+(?:Department|School|Institute|Center|Laboratory|Division|College|Faculty|Graduate|Program)\b', re.IGNORECASE),
    # 이메일 줄: "e-mail: user@domain.com" / "E-mail: ..."
    re.compile(r'^e-?mail:\s*\S+@', re.IGNORECASE),
    # ── Wiley 저자 소속 (번호 없음) ──────────────────────────────────────────────
    # 한국 소속 끝 표지: "Republic of Korea" 또는 붙여쓴 "RepublicofKorea"
    re.compile(r'Republic\s*of\s*Korea', re.IGNORECASE),
    # 한국 기관명 붙여쓰기 아티팩트 (PDF 추출 시 공백 누락): "KoreaInstitute", "KoreaUniversity"
    re.compile(r'Korea[A-Z]'),
    # 서울 우편번호 줄 (5자리): "Seoul 02841" 또는 "Seoul02792"
    re.compile(r'^Seoul\s*\d{5}', re.IGNORECASE),
    # ORCID 식별번호 줄 (Wiley 저널) — 숫자 ID 포함 시만 삭제 (body text 오삭제 방지)
    re.compile(r'\bORCID\b.*\d'),
    # 저자 이름만 나열 (붙여쓴 이니셜.성 형태): "D.W.Lee,J.G.Son"
    re.compile(r'^(?:[A-Z]\.[A-Z]\.[A-Z]\w+,?)+$'),
    # 저자 이니셜 + 기관명 (Wiley 소속): "J. Oh, S. Lee Department of..."
    # 패턴: 1-2자 이니셜 반복 + 기관 키워드
    re.compile(r'^(?:[A-Z][\w\-\.]{0,3}\.\s*){2,}(?:Department|School|Institute|Center|Laboratory|Division|College|Faculty|Graduate|Program|Research|Advanced|Photovoltaics)\b', re.IGNORECASE),
    # ── 추가 소속 패턴 (2026-03-04) ───────────────────────────────────────────
    # 기관 키워드로 시작하는 줄 (번호 없이): "Department of Electrical Engineering E-mail:..."
    re.compile(r'^(?:Department|School|Institute|Laboratory|Division|Faculty)\s+of\b', re.IGNORECASE),
    # 단독 저자 이름 (이니셜. 성 형식): "J. Cho", "J. Lee"
    re.compile(r'^[A-Z]\.\s+[A-Z][a-z]{2,}\s*$'),
    # 복수 저자 이니셜+성 리스트 (쉼표 구분, 최소 3명 이상):
    # "J.-C. Choi, H. Y . Jeong, S. Chung School of Electrical Engineering Korea University"
    re.compile(r'^[A-Z]\.(?:-[A-Z]\.)?\s+[A-Z]\w+(?:,\s+[A-Z][^\n,]*){2,}$'),
    # Corresponding author 줄: "*corresponding author. email: ..."
    re.compile(r'^\*?\s*[Cc]orresponding\s+(?:author|authors)\b', re.IGNORECASE),
    # 독립 기관명 줄: "Soft Hybrid Materials Research Center"
    re.compile(r'^[\w\s]{5,80}(?:Research\s+Center|Research\s+Institute|Materials\s+(?:Center|Laboratory))\s*$', re.IGNORECASE),
    # "University of Science & Technology (UST)" 등 독립 대학명+약어 줄
    re.compile(r'^University\s+of\s+[\w\s&]+\s*\(\w{2,6}\)\s*$', re.IGNORECASE),
]

# ── 헤더 영역(제목~본문 첫 섹션 직전)에서만 적용할 최소 보일러플레이트 패턴 ────────────
# 저자/소속 정보는 보존하고, URL/DOI/©/저널헤더만 제거
_HEADER_BOILERPLATE_PATTERNS = [
    re.compile(r'^www\.\S+\.\w{2,}$', re.IGNORECASE),
    re.compile(r'^https?://\S+$', re.IGNORECASE),
    re.compile(r'^[A-Z][a-z][\w\s\.\-]+\d{4}\s*,\s*\d+\s*,\s*\w+\s*$'),
    re.compile(r'^SCIENCE\s+NEWS$', re.IGNORECASE),
    re.compile(r'^SCIENCE\s+ADVANCES', re.IGNORECASE),
    re.compile(r'^R\s+E\s+S\s+E\s+A\s+R\s+C\s+H\s+A\s+R\s+T', re.IGNORECASE),
    re.compile(r'^DOI:\s*10\.\d{4,}/', re.IGNORECASE),
    re.compile(r'^©\s*\d{4}'),
    re.compile(r'^nature\s+\w[\w\s]*$', re.IGNORECASE),
    re.compile(r'^(?:Research\s+)?Article$', re.IGNORECASE),
    re.compile(r'^View the article online', re.IGNORECASE),
    re.compile(r'^Permissions$', re.IGNORECASE),
]

# 본문 첫 섹션 헤딩 패턴 (이 헤딩 등장 시 헤더 영역 종료)
_BODY_HEADING_PAT = re.compile(
    r'^(?:\d+\.?\s*)?(?:Introduction|Background|Methods|Results|Discussion|Conclusion|Experimental|'
    r'Materials\s+and\s+Methods|INTRODUCTION|BACKGROUND|METHODS|RESULTS|DISCUSSION|CONCLUSION|'
    r'EXPERIMENTAL|MATERIALS\s+AND\s+METHODS|MATERIALS\s+&\s+METHODS)\b',
    re.IGNORECASE
)


def _is_header_boilerplate(text: str) -> bool:
    """헤더 영역에서만 적용: URL/DOI/© 등만 제거, 저자/소속 보존."""
    t = text.strip()
    for p in _HEADER_BOILERPLATE_PATTERNS:
        if p.search(t):
            return True
    return False


def _is_boilerplate(text: str) -> bool:
    t = text.strip()
    # 짧은 텍스트(≤200자): 모든 패턴 적용 (부분 매칭 포함)
    # 긴 텍스트(>200자): ^-앵커 패턴만 적용 (본문 단락 오삭제 방지)
    short = len(t) <= 200
    for p in _BOILERPLATE_PATTERNS:
        if not short and not p.pattern.startswith('^'):
            continue
        if p.search(t):
            return True
    return False


# 아이템별 (type, content) 목록 생성
items_list = []  # list of {"type": "img"|"text"|"table"|"heading"|"caption", "content": str, "fig_n": int|None}

pic_idx = 0
fig_num = 0
_formula_page_counter: dict = {}   # page_no → 순번 (페이지 내 순서)
_in_header = True   # 첫 본문 섹션 헤딩 전까지 True (저자/소속 보존)

for item, depth in doc.iterate_items():
    if isinstance(item, PictureItem):
        if pic_idx in image_map:
            fig_num += 1
            fn = image_map[pic_idx]
            items_list.append({
                "type":    "img",
                "content": f"![Figure {fig_num}]({asset_folder}/{fn})",
                "fig_n":   fig_num,
            })
        pic_idx += 1

    elif isinstance(item, TableItem):
        items_list.append({
            "type":    "table",
            "content": item.export_to_markdown(),
            "fig_n":   None,
        })

    elif isinstance(item, FormulaItem):
        # display 수식 위치 마커 삽입 — UniMerNet LaTeX 로 나중에 교체됨
        if item.prov:
            p = item.prov[0]
            pg = p.page_no
            _formula_page_counter[pg] = _formula_page_counter.get(pg, 0) + 1
            rank = _formula_page_counter[pg]
            # bbox: l, t, r, b  (Docling BOTTOMLEFT: t > b, t=상단 y)
            if p.bbox:
                bbox_str = f"{p.bbox.l:.1f}:{p.bbox.t:.1f}:{p.bbox.r:.1f}:{p.bbox.b:.1f}"
            else:
                bbox_str = "0:0:0:0"
            items_list.append({
                "type":    "formula_placeholder",
                "content": f"<!-- formula-slot:p{pg}:{rank}:{bbox_str} -->",
                "fig_n":   None,
            })

    elif isinstance(item, SectionHeaderItem):
        if _is_figure_text(item, depth):
            continue
        heading_text = item.text.strip()
        # 헤더 영역 종료 감지: 첫 본문 섹션 헤딩 등장 시 _in_header = False
        if _in_header and _BODY_HEADING_PAT.match(heading_text):
            _in_header = False
        # 보일러플레이트 필터: 헤더 영역은 URL/DOI/© 등만, 본문은 전체 적용
        if _in_header:
            if _is_header_boilerplate(heading_text):
                continue
        else:
            if _is_boilerplate(heading_text):
                continue
        h_level = (getattr(item, 'level', 1) or 1) + 1
        prov_info = None
        if hasattr(item, 'prov') and item.prov:
            _p = item.prov[0]
            if _p.bbox:
                prov_info = {"page": _p.page_no, "bbox": [_p.bbox.l, _p.bbox.t, _p.bbox.r, _p.bbox.b]}
        items_list.append({
            "type":    "heading",
            "content": "#" * h_level + " " + heading_text,
            "fig_n":   None,
            "prov":    prov_info,
        })

    elif hasattr(item, 'text') and item.text:
        if _is_figure_text(item, depth):
            continue
        text = item.text.strip()
        if not text:
            continue

        # 보일러플레이트 필터: 헤더 영역은 URL/DOI/© 등만, 본문은 전체 적용
        if _in_header:
            if _is_header_boilerplate(text):
                continue
        else:
            if _is_boilerplate(text):
                continue

        # prov 정보 추출
        prov_info = None
        if hasattr(item, 'prov') and item.prov:
            _p = item.prov[0]
            if _p.bbox:
                prov_info = {"page": _p.page_no, "bbox": [_p.bbox.l, _p.bbox.t, _p.bbox.r, _p.bbox.b]}

        # 캡션 감지: "Fig. 1 | ..." / "Figure 1. ..." (짧은 라벨이나 "See next page" 제외)
        m = _CAPTION_PAT.match(text)
        if m and len(text) > 20 and 'see next page' not in text.lower():
            fig_n_val = int(m.group(1))
            # "Extended Data Fig." 는 별도 네임스페이스(100+N)
            if text.strip().lower().startswith('extended'):
                fig_n_val += 100
            items_list.append({
                "type":    "caption",
                "content": text,
                "fig_n":   fig_n_val,
                "prov":    prov_info,
            })
        else:
            if isinstance(item, ListItem):
                marker = getattr(item, 'marker', '') or '-'
                items_list.append({
                    "type":    "text",
                    "content": f"{marker} {text}",
                    "fig_n":   None,
                    "prov":    prov_info,
                })
            else:
                items_list.append({
                    "type":    "text",
                    "content": text,
                    "fig_n":   None,
                    "prov":    prov_info,
                })

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: 캡션 그룹핑 — "Figure N." 캡션을 해당 이미지 바로 뒤로 이동
# ─────────────────────────────────────────────────────────────────────────────
# 캡션들을 먼저 수집 (fig_n → caption text)
caption_map: dict = {}
non_caption = []
for it in items_list:
    if it["type"] == "caption" and it["fig_n"] is not None:
        # 더 긴(상세한) 캡션을 우선 보존 — "See next page" 같은 짧은 대체 방지
        existing = caption_map.get(it["fig_n"], "")
        if len(it["content"]) > len(existing):
            caption_map[it["fig_n"]] = it["content"]
    else:
        non_caption.append(it)

# 이미지 직후에 캡션 삽입
final_items = []
for it in non_caption:
    final_items.append(it)
    if it["type"] == "img" and it["fig_n"] in caption_map:
        cap_text = caption_map.pop(it["fig_n"])
        final_items.append({
            "type":    "caption_inline",
            "content": f"*{cap_text}*",
            "fig_n":   it["fig_n"],
        })

# 매칭 안된 캡션은 맨 뒤에 추가
for fig_n in sorted(caption_map.keys()):
    final_items.append({
        "type":    "caption_inline",
        "content": f"*{caption_map[fig_n]}*",
        "fig_n":   fig_n,
    })

# ─────────────────────────────────────────────────────────────────────────────
# Step 5.5: 단락 중간 Figure → 단락 끝으로 이동
#   - img(+caption_inline) 그룹의 앞·뒤가 모두 text이면 단락 중간으로 판단
#   - 뒤이어 오는 text 아이템을 먼저 출력하고 figure 그룹을 그 뒤에 배치
# ─────────────────────────────────────────────────────────────────────────────
def _move_figures_to_para_end(items: list) -> list:
    """연속된 그림 블록이 문장 중간에 있으면 이어지는 본문을 먼저 배치.

    연속 그림(Figure1 + Figure2 등)도 하나의 블록으로 묶어서 판단.
    앞·뒤가 모두 text인 경우에만 이동 (섹션 경계나 끝부분은 이동 안 함).
    """
    result = []
    i = 0
    while i < len(items):
        it = items[i]
        if it["type"] == "img":
            # ① 연속된 img(+caption_inline) 그룹 전체를 수집
            fig_block: list = []
            while i < len(items) and items[i]["type"] in ("img", "caption_inline"):
                if items[i]["type"] == "img":
                    grp = [items[i]]
                    i += 1
                    while i < len(items) and items[i]["type"] == "caption_inline":
                        grp.append(items[i])
                        i += 1
                    fig_block.append(grp)
                else:
                    # 고아 caption_inline (정상적으로는 발생하지 않음)
                    fig_block.append([items[i]])
                    i += 1

            # ② 단락 컨텍스트 판정:
            #   - result를 뒤에서 탐색, img/caption_inline을 건너뛰어 text 찾기
            #   - 이로써 연속 figure 사이에서도 "단락 중간"임을 인식
            prev_is_text = False
            for _k in range(len(result) - 1, -1, -1):
                _t = result[_k]["type"]
                if _t in ("img", "caption_inline"):
                    continue   # 앞의 figure 그룹을 건너뜀
                prev_is_text = (_t == "text")
                break

            next_is_text = i < len(items) and items[i]["type"] == "text"
            if prev_is_text and next_is_text:
                while i < len(items) and items[i]["type"] == "text":
                    result.append(items[i])
                    i += 1

            # ③ 그림 블록 추가
            for grp in fig_block:
                result.extend(grp)
        else:
            result.append(it)
            i += 1
    return result


final_items = _move_figures_to_para_end(final_items)

# ─────────────────────────────────────────────────────────────────────────────
# Step 5.8: text_spans 수집 (inline 수식 삽입용 위치 정보)
# ─────────────────────────────────────────────────────────────────────────────
text_spans = []
# 본문 텍스트 (final_items에서)
for _it in final_items:
    if _it.get("prov") and _it["type"] == "text":
        text_spans.append({
            "text": _it["content"],
            "page": _it["prov"]["page"],
            "bbox": _it["prov"]["bbox"],  # [l, t, r, b] PDF points (BOTTOMLEFT, t > b)
        })
# 캡션 텍스트 (items_list에서 — caption은 final_items에서 caption_inline으로 변환돼 prov 소실)
# 마크다운 상에는 *caption text* 형태로 존재 → is_caption=True 플래그로 검색 방식 구분
for _it in items_list:
    if _it.get("prov") and _it["type"] == "caption":
        text_spans.append({
            "text": _it["content"],
            "page": _it["prov"]["page"],
            "bbox": _it["prov"]["bbox"],
            "is_caption": True,
        })

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: 최종 마크다운 조립
# ─────────────────────────────────────────────────────────────────────────────
md_parts = []
for it in final_items:
    t = it["type"]
    c = it["content"]
    if t == "img":
        md_parts.append(f"\n{c}\n")
    elif t in ("caption_inline",):
        md_parts.append(f"\n{c}\n")
    elif t == "formula_placeholder":
        md_parts.append(f"\n{c}\n")   # HTML 주석 — merge_results()에서 LaTeX로 교체
    elif t == "table":
        md_parts.append(f"\n{c}\n")
    elif t == "heading":
        md_parts.append(f"\n{c}\n")
    else:
        md_parts.append(c)

final_md = "\n\n".join(part.strip() for part in md_parts if part.strip())

# ─────────────────────────────────────────────────────────────────────────────
# Step 7: FormulaItem 수집
# ─────────────────────────────────────────────────────────────────────────────
formula_items = []
for item, _ in doc.iterate_items():
    if isinstance(item, FormulaItem):
        prov_list = []
        for p in item.prov:
            prov_list.append({
                "page": p.page_no,
                "bbox": [p.bbox.l, p.bbox.t, p.bbox.r, p.bbox.b] if p.bbox else None,
            })
        formula_items.append({
            "text": item.text,
            "orig": item.orig,
            "prov": prov_list,
        })

# ─────────────────────────────────────────────────────────────────────────────
# 출력
# ─────────────────────────────────────────────────────────────────────────────
output = {
    "markdown":      final_md,
    "formula_items": formula_items,
    "text_spans":    text_spans,
    "n_images_raw":  total_raw,
    "n_images_saved": saved_count,
}
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"[Docling] 완료: {saved_count}개 이미지, {len(formula_items)}개 수식 아이템")
