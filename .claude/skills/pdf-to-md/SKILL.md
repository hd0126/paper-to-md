---
name: pdf-to-md
description: This skill should be used when the user asks to "convert PDF to markdown", "pdf를 md로 변환", "논문 변환", "run hybrid pipeline", "hybrid로 변환", "pdf-to-md", or wants to convert an academic paper PDF to high-quality Markdown using the hybrid v9 pipeline (Docling + MinerU).
---

# PDF → Markdown 변환 (hybrid v9 파이프라인)

## 개요

hybrid v9 파이프라인(Docling + MinerU UniMerNet)을 사용해 학술논문 PDF를 고품질 Markdown으로 변환한다.
평균 94.6/100점, 32초/논문 (RTX 3070 Laptop GPU 기준).

- 스크립트: `D:\GitHub\Obsidian\scripts\run_paper_hybrid.py`
- 개발 이력: `D:\GitHub\Obsidian\scripts\DEVELOPMENT.md`
- 작업 PC: Simulation Notebook1 / Windows Terminal

## 실행 방법

```bash
# 기본 실행 (출력: PDF와 같은 디렉토리)
scripts/envs/mineru/.venv/Scripts/python.exe scripts/run_paper_hybrid.py <pdf_path>

# 출력 디렉토리 지정
scripts/envs/mineru/.venv/Scripts/python.exe scripts/run_paper_hybrid.py <pdf_path> --out-dir <output_dir>
```

**작업 디렉토리**: 반드시 `D:\GitHub\Obsidian`에서 실행할 것.

## 저널별 권장 도구

| 저널 계열 | 권장 도구 | 비고 |
|-----------|-----------|------|
| Wiley (Adv. Mater., Small Structures 등) | hybrid v9 | 기본 |
| Nature 계열 | hybrid v9 | 기본 |
| Science Advances | hybrid v9 | 기본 |
| 모든 디지털 PDF | hybrid v9 | 스캔본 제외 |

## 출력 결과 확인

변환 완료 후 확인할 항목:
- [ ] YAML frontmatter (title, authors, journal, keywords, 분류체계)
- [ ] 수식 정상 렌더링 (`$...$`, `$$...$$`)
- [ ] Figure 캡션 blockquote 형식 (`> Figure N.`)
- [ ] 참고문헌 목록 완전
- [ ] 섹션 헤딩 계층 구조 (H1~H3)
- [ ] 표 구조 유지

## 교정 추적 (선택)

변환 후 수동 교정 시 교정 내용을 학습 데이터로 캡처:

```bash
python scripts/learning/correction_tracker.py original.md corrected.md \
    --paper-key <논문ID>
```

## 버전 정보

- 현재: **hybrid v9.0** (2026-03-28)
- 점수: 94.6/100 (속도 포함), 98.5/100 (속도 제외)
- 자기발전 시스템: Phase 1~4 완료 (correction_tracker → regression_guard → journal_learner)
- 상세 이력: `D:\GitHub\Obsidian\scripts\DEVELOPMENT.md`

## 향후 로드맵 (v9.1+)

- v9.1: Elsevier/ACS 지원, MinerU 2.7 평가, MonkeyOCR/FireRed-OCR 벤치마크
- v9.2: Docling 영속 서버 모드 (목표 15초/논문), 수식 의미 정확도 벤치마크
