#!/usr/bin/env python3
"""
PyMuPDF4LLM 변환 + fitz text_spans 추출 헬퍼
run_pymupdf4llm_hybrid.py에서 subprocess로 호출됨 (pymupdf4llm venv)

호출:
    <pymupdf4llm_python> scripts/engines/pymupdf4llm_convert.py \
        <pdf_path> <out_json> <assets_dir>

출력 JSON:
{
  "markdown":     "...",
  "text_spans":   [{"text":"...", "page":1, "bbox":[l,t,r,b]}, ...],
  "page_heights": {"1": 792.0, "2": 792.0, ...}
}

text_spans bbox 형식: [l, t, r, b] BOTTOMLEFT origin (Docling과 동일)
  - t > b, t = 페이지 상단 기준 y (PDF points)
  - 변환: x0,y0,x1,y1(TOP-LEFT) → [x0, ph-y0, x1, ph-y1]
"""

import json
import re
import sys
from pathlib import Path

pdf_path   = sys.argv[1]
out_json   = sys.argv[2]
assets_dir = Path(sys.argv[3])
assets_dir.mkdir(parents=True, exist_ok=True)

asset_folder = assets_dir.name
stem = Path(pdf_path).stem

# ─── 1. PyMuPDF4LLM 변환 ──────────────────────────────────────────────────
import pymupdf4llm

md_text = pymupdf4llm.to_markdown(
    str(pdf_path),
    write_images=True,
    image_path=str(assets_dir),
    image_format="png",
    image_pathdir=f"./{asset_folder}",
    image_size_limit=0.10,   # 페이지 폭·높이의 10% 미만 이미지 제외
)
# 절대경로 → 상대경로 교정
md_text = md_text.replace(str(assets_dir) + "/", asset_folder + "/")
md_text = md_text.replace(str(assets_dir), asset_folder)

# ─── 2. 이미지 필터링 (convert_multi.py와 동일) ───────────────────────────
# 8KB 미만 소형 이미지 제거
_MIN_IMG_BYTES = 8_192
removed_imgs: set = set()
for img_file in list(assets_dir.glob("*.png")):
    if img_file.stat().st_size < _MIN_IMG_BYTES:
        removed_imgs.add(img_file.name)
        img_file.unlink()
if removed_imgs:
    kept = []
    for ln in md_text.split('\n'):
        if ln.strip().startswith('![') and any(
            f'{asset_folder}/{nm}' in ln for nm in removed_imgs
        ):
            continue
        kept.append(ln)
    md_text = '\n'.join(kept)


# 적응형 필터: 12개 초과 시 파일 크기 기준 상위 10개만 유지
_ADAPTIVE_MAX  = 12
_ADAPTIVE_KEEP = 10
md_img_refs = [ln for ln in md_text.split('\n') if ln.strip().startswith('![')]
if len(md_img_refs) > _ADAPTIVE_MAX:
    ref_sizes: dict = {}
    for ln in md_img_refs:
        m = re.search(rf'{re.escape(asset_folder)}/([^)\s"]+)', ln)
        if m:
            nm = m.group(1)
            p = assets_dir / nm
            if p.exists():
                ref_sizes[nm] = p.stat().st_size
    if len(ref_sizes) > _ADAPTIVE_KEEP:
        keep_names = {nm for nm, _ in sorted(
            ref_sizes.items(), key=lambda x: x[1], reverse=True
        )[:_ADAPTIVE_KEEP]}
        adaptive_removed: set = set()
        for nm in ref_sizes:
            if nm not in keep_names:
                p = assets_dir / nm
                if p.exists():
                    p.unlink()
                adaptive_removed.add(nm)
        if adaptive_removed:
            kept = []
            for ln in md_text.split('\n'):
                if ln.strip().startswith('![') and any(
                    f'{asset_folder}/{nm}' in ln for nm in adaptive_removed
                ):
                    continue
                kept.append(ln)
            md_text = '\n'.join(kept)
            print(f"  [PyMuPDF4LLM] 적응형 필터: {len(adaptive_removed)}개 제거 → {_ADAPTIVE_KEEP}개 유지")

# ─── 3. fitz text_spans 추출 ──────────────────────────────────────────────
import fitz  # PyMuPDF (pymupdf4llm venv에 포함)

doc_fitz = fitz.open(str(pdf_path))
text_spans = []
page_heights: dict = {}

for page_no, page in enumerate(doc_fitz, start=1):
    ph = page.rect.height   # PDF points (TOP-LEFT origin의 페이지 높이)
    page_heights[page_no] = ph

    # 텍스트 블록 추출 (type=0: text, type=1: image)
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
    for block in blocks:
        if block.get("type") != 0:
            continue

        # line 단위로 text_span 추출
        # (block 전체 join이 아닌 line별 - PyMuPDF4LLM markdown의 줄별 형식과 일치)
        for line in block.get("lines", []):
            line_text = "".join(sp.get("text", "") for sp in line.get("spans", []))
            line_text = line_text.strip()
            if not line_text or len(line_text) < 3:
                continue

            # line bbox: TOP-LEFT (x0,y0,x1,y1) → BOTTOMLEFT [l, t, r, b] (t > b)
            x0, y0, x1, y1 = line["bbox"]
            bbox = [x0, ph - y0, x1, ph - y1]

            text_spans.append({
                "text": line_text,
                "page": page_no,
                "bbox": bbox,
            })

doc_fitz.close()

img_count = len(list(assets_dir.glob("*.png")))
print(f"[PyMuPDF4LLM] 완료: {img_count}개 이미지, {len(text_spans)}개 text_spans")

# ─── 4. JSON 출력 ─────────────────────────────────────────────────────────
output = {
    "markdown":     md_text,
    "text_spans":   text_spans,
    "page_heights": {str(k): v for k, v in page_heights.items()},
}
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)
