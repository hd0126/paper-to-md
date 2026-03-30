---
date: 2026-03-28 14:00:00
modified: 2026-03-28 16:30:00
purpose: "hybrid_v8 파이프라인 개발 버전 관리 및 진행 상황 추적"
author: "HDK"
agent: "Claude Code"
type: plan
---

# hybrid 파이프라인 개발 버전 관리

## 현재 버전: v9.0 (2026-03-28)
- 기반: hybrid_v8 → v8.1 → v9.0
- 목표: 자기발전 시스템 완성 + 속도 최적화 + 파이프라인 연동
- 결과: **94.6/100 (속도 포함), 98.5/100 (속도 제외), 평균 32초/논문, 전체 S등급**

## 버전 이력

### v8.0 (2026-03-12) — Baseline
- Docling + MinerU MFD+UniMerNet 하이브리드
- 벤치마크: 98.1/100 (8논문 평균, 5카테고리)
- md_to_latex.py: 전체 8편 에러/Overfull/Float 0
- 테스트셋: A1~D1 (Wiley 4, AAAS 2, Springer Nature 2)

### v8.1 (2026-03-28) — Bug Fix + Self-Improving Foundation
#### 완료 항목
- [x] BUG: PUA 심볼 맵핑 불일치 수정 (postprocess.py `\uF076`~`\uF07A` 정렬)
- [x] BUG: 정규화 로직 중복 제거 → `engines/text_normalize.py` 공유 모듈 추출
- [x] FEAT: `learning/correction_tracker.py` (교정 diff 캡처 → JSONL 누적)
- [x] FEAT: `learning/parameter_store.py` + `learning_data/journal_params.json` (저널별 파라미터)
- [x] BENCH: 7카테고리 통합 벤치마크 전체 재측정

#### v8.1에서 미완료 → v9.0으로 이관 완료
- [x] FEAT: `learning/regression_guard.py` (회귀 방지) → v9.0에서 완료
- [x] FEAT: `learning/journal_learner.py` (자동 학습) → v9.0에서 완료
- [x] INTEG: `run_paper_hybrid.py`에서 `journal_params.json` 읽기 연동 → v9.0에서 완료
- [ ] BENCH: 테스트셋 확장 (Elsevier/ACS 추가 → 10편+) → v9.1
- [ ] EVAL: Marker 1.10 `--use_llm` 비교 벤치마크 → v9.1

#### 변경 로그
| 날짜 | 작업 | 파일 | 상태 |
|------|------|------|:----:|
| 2026-03-28 | PUA 심볼 맵핑 버그 수정 | `engines/postprocess.py` | 완료 |
| 2026-03-28 | 정규화 공유 모듈 추출 | `engines/text_normalize.py` (신규), `run_paper_hybrid.py` | 완료 |
| 2026-03-28 | 교정 추적기 구현 | `learning/correction_tracker.py` (신규) | 완료 |
| 2026-03-28 | 파라미터 스토어 구현 | `learning/parameter_store.py` (신규), `learning_data/journal_params.json` (신규) | 완료 |
| 2026-03-28 | md_to_latex BUG-26~30 수정 | `md_to_latex.py` | 완료 |
| 2026-03-28 | 7카테고리 통합 벤치마크 전체 재측정 | 8편 전체 | 완료 |

## 벤치마크 기록

### v8.1 — 7카테고리 통합 채점 + 속도 포함 최종 (2026-03-28, RTX 3070 Laptop GPU)
| 논문 | 저널 | Docling | UniMerNet | 전체 | **점수/100** |
|------|------|:------:|:--------:|:----:|:----------:|
| A1 | Adv. Funct. Mater. | 26s | 20s | 29s | **93.0** |
| A2 | Small Structures | 23s | 15s | 27s | **95.6** |
| A3 | Adv. Mater. | 27s | 18s | 31s | **93.5** |
| B1 | Nature Materials | 35s | 18s | 39s | **95.4** |
| B2 | Science Advances | 37s | 21s | 42s | **95.2** |
| C1 | Nature Electronics | 37s | 25s | 42s | **92.8** |
| C2 | Science Advances | 27s | 18s | 31s | **95.1** |
| D1 | Adv. Electron. Mater. | 30s | 15s | 34s | **96.0** |
| **평균** | | **30s** | **19s** | **34s** | **94.6** |

**전체 8편 S등급!**

#### 속도 최적화 이력 (A1 기준)
| 단계 | 시간 | 배수 | 변경 |
|------|:----:|:----:|------|
| v8.0 CPU 이전 | 160s | 1.0x | baseline |
| + GPU torch CUDA | 44s | 3.6x | `torch 2.6.0+cu124` + `onnxruntime-gpu` |
| + OCR 끔 + FAST | 44s | 3.6x | `do_ocr=False`, `TableFormerMode.FAST` |
| **+ batch_size=16** | **29s** | **5.5x** | `layout_batch_size=16` |

#### 속도 최적화 전체 내역
1. Docling venv에 `torch 2.6.0+cu124` + `onnxruntime-gpu` 설치
2. `docling_convert.py`에 `do_ocr=False` (디지털 PDF OCR 비활성화)
3. `TableFormerMode.FAST` (ACCURATE → FAST 전환)
4. `AcceleratorOptions(device=AUTO)` 명시 설정
5. `layout_batch_size=16` (기본 4 → 16, RTX 3070 8GB VRAM 활용)

#### Pix2Text 비교 결과 (탈락)
- Pix2Text 1.1.6: A1에서 9개 수식, 79초 — UniMerNet(256개, 20초) 대비 부적합

#### 카테고리별 분석
- **수식 (23.8/25)**: 전체 수식 인식 정상 (평균 202개/논문)
- **텍스트 (20/20)**: PUA 수정 후 전체 만점
- **표 (10/10)**: 전체 만점
- **이미지 (15/15)**: 전체 만점
- **구조 (14.6/15)**: A1/C1에서 미매핑 display formula 각 1개
- **참고 (10/10)**: 전체 만점
- **속도**: 평균 34초/논문 (벤치마크 만점 기준 10초 이하)

#### LaTeX 컴파일 결과
- 전체 8편: **에러 0, Overfull 0, Float too large 0**

#### 속도 제외 환산 점수
- 속도 제외 기준: **93.6/95 → 98.5/100** (v8.0 대비 +0.4)

### v8.0 Baseline (2026-03-12, 참고)
| 논문 | 저널 | v8.0 점수 |
|------|------|:---------:|
| 평균 | | **98.1** |
(v8.0은 5카테고리 기준으로 측정. v8.1에서 7카테고리로 확장하여 재측정)

### v9.0 (2026-03-28) — Self-Improving System Complete + Speed Optimization
#### 완료 항목
- [x] FEAT: `learning/regression_guard.py` — 회귀 방지 (Phase 3)
- [x] FEAT: `learning/journal_learner.py` — 자동 학습 규칙 엔진 (Phase 4)
- [x] INTEG: `run_paper_hybrid.py` ↔ `journal_params.json` 런타임 연동
- [x] PERF: Docling `do_ocr=False` + `TableFormerMode.FAST` + `layout_batch_size=16`
- [x] PERF: Docling venv `onnxruntime-gpu` + `torch 2.6.0+cu124`
- [x] PERF: `AcceleratorOptions(device=AUTO)` 명시 설정
- [x] EVAL: Pix2Text 1.1.6 벤치마크 (탈락: 9수식/79초 vs UniMerNet 256수식/20초)

#### 변경 로그
| 날짜 | 작업 | 파일 | 상태 |
|------|------|------|:----:|
| 2026-03-28 | Phase 3 회귀 방지 구현 | `learning/regression_guard.py` (신규) | 완료 |
| 2026-03-28 | Phase 4 자동 학습 구현 | `learning/journal_learner.py` (신규) | 완료 |
| 2026-03-28 | 파이프라인 연동 | `run_paper_hybrid.py` (+38줄) | 완료 |
| 2026-03-28 | Docling 속도 최적화 | `engines/docling_convert.py` | 완료 |
| 2026-03-28 | Pix2Text 벤치마크 | `_test_pix2text_a1.ps1` | 완료(탈락) |

### v9.0 최종 벤치마크 (2026-03-28, RTX 3070 Laptop GPU)
| 논문 | 저널 | Docling | UniMerNet | 전체 | 수식 | **점수/100** |
|------|------|:------:|:--------:|:----:|:----:|:----------:|
| A1 | Adv. Funct. Mater. | 26s | 20s | 33s | 256 | **92.0** |
| A2 | Small Structures | 22s | 15s | 26s | 154 | **95.6** |
| A3 | Adv. Mater. | 27s | 18s | 31s | 226 | **93.5** |
| B1 | Nature Materials | 29s | 15s | 33s | 211 | **95.3** |
| B2 | Science Advances | 32s | 17s | 36s | 160 | **95.7** |
| C1 | Nature Electronics | 30s | 21s | 34s | 255 | **93.3** |
| C2 | Science Advances | 28s | 20s | 32s | 256 | **95.1** |
| D1 | Adv. Electron. Mater. | 29s | 16s | 33s | 94 | **96.0** |
| **평균** | | **28s** | **18s** | **32s** | **202** | **94.6** |

**전체 8편 S등급!**

#### 속도 최적화 이력
| 단계 | 평균 시간 | 배수 |
|------|:--------:|:----:|
| v8.0 CPU | 140s | 1.0x |
| + GPU torch CUDA | 44s | 3.2x |
| + OCR끔 + FAST | 36s | 3.9x |
| **v9.0 + batch16** | **32s** | **4.4x** |

## 자기발전 시스템 진행 상황

| Phase | 구성요소 | 상태 | 비고 |
|:-----:|---------|:----:|------|
| 1 | `learning/correction_tracker.py` | **완료** | 교정 diff 캡처 → JSONL |
| 2 | `learning/parameter_store.py` | **완료** | 저널별 파라미터 오버라이드 |
| 3 | `learning/regression_guard.py` | **완료** | 회귀 방지 |
| 4 | `learning/journal_learner.py` | **완료** | 자동 학습 규칙 엔진 |

### 사용법
```bash
# 교정 추적 (원본 vs 교정본 diff 캡처)
python scripts/learning/correction_tracker.py original.md corrected.md \
    --paper-key A1_Adv_Funct_Mater_2024_Zero_Poisson

# 저널별 파라미터 조회
python -c "from learning.parameter_store import list_overrides; print(list_overrides())"
```

## 파일 구조 (v9.0)
```
scripts/
  engines/
    text_normalize.py          # v8.1: 공유 정규화 모듈 (PUA, 리가처, 유니코드)
    postprocess.py             # v8.1: PUA 맵핑 수정
    docling_convert.py         # v9.0: GPU최적화 (OCR끔, FAST, batch16, AcceleratorOptions)
  learning/
    __init__.py                # v8.1
    correction_tracker.py      # v8.1: Phase 1 — 교정 diff 캡처
    parameter_store.py         # v8.1: Phase 2 — 저널별 파라미터 관리
    regression_guard.py        # v9.0: Phase 3 — 회귀 방지
    journal_learner.py         # v9.0: Phase 4 — 자동 학습 규칙 엔진
  learning_data/
    corrections_log.jsonl      # 교정 누적 로그 (JSONL)
    journal_params.json        # 학습된 저널별 파라미터
    regression_baseline.json   # v9.0: 회귀 방지 기준선
  run_paper_hybrid.py          # v9.0: 정규화 위임 + journal_params 연동
  DEVELOPMENT.md               # 이 문서
```

## 신규 저널 벤치마크 (2026-03-30)

작업 위치: Simulation Notebook1 / `D:\GitHub\Obsidian\scripts\` / Windows Terminal

### 테스트 논문 (graphene/2D transfer 주제, 모두 CC-BY 오픈 액세스)

| ID | 저널 | 포맷 | 페이지 | 시간 | 점수 |
|----|------|------|--------|------|------|
| E1 | ACS Nano 18(23) 2024 | **ACS** | 37p (리뷰) | 56.6s | **87/100** |
| F1 | npj 2D Mater. Appl. 2024 | **npj** | 9p | 26.0s | **87/100** |
| F2 | npj 2D Mater. Appl. 2025 | **npj** | 14p | 25.9s | **86/100** |

- E1: "Transfer of 2D Films: From Imperfection to Perfection" (DOI: 10.1021/acsnano.4c00590)
- F1: "Automated and parallel transfer of arrays of oriented graphene ribbons" (DOI: 10.1038/s41699-024-00491-8)
- F2: "Thermal engineering of interface adhesion for efficient transfer of CVD-grown TMDs" (DOI: 10.1038/s41699-025-00594-w)

### 신규 저널 포맷 이슈 (v9.1 개선 대상)

| 문제 | 영향 저널 | 개선 방법 |
|------|-----------|-----------|
| `fi`/`fl` 리가처 미처리 | npj (F1, F2) | postprocess에 리가처 → 일반문자 변환 추가 |
| ACS 참고문헌 `(N)` 스타일 미파싱 | ACS Nano (E1) | 참고문헌 파서 확장 |
| YAML `first_author`에 figure 오삽입 | npj (F2) | 저자 블록 감지 로직 강화 |
| Chalmers/PMC 워터마크 YAML 오염 | ACS Nano (E1) | preprint 저장소 워터마크 필터 추가 |

### 자기발전 시스템 현황
- `corrections_log.jsonl`: 0건 — 교정 데이터 미누적 (수동 교정 후 correction_tracker.py 실행 필요)
- `/pdf-to-md` 스킬 생성 완료 (Claude Code 플러그인, v9.2 로드맵 항목 조기 완료)
- Python 실행 경로 확정: `scripts/envs/mineru/.venv/Scripts/python.exe` (Windows)

---

## 향후 로드맵

### v9.1 — 테스트셋 확장 + 신규 포맷 수정
- [x] ACS 논문 벤치마크 (E1 완료)
- [x] npj 2D Materials 벤치마크 (F1, F2 완료)
- [ ] ACS 참고문헌 `(N)` 스타일 파서 추가
- [ ] npj `fi`/`fl` 리가처 postprocess 처리
- [ ] YAML 저자 블록 감지 로직 강화
- [ ] Marker 1.10 `--use_llm` 비교 벤치마크
- [ ] MinerU 2.7 hybrid 백엔드 교체 평가
- [ ] MonkeyOCR / FireRed-OCR 벤치마크

### v9.2 — 플러그인화 + 고급 기능
- [x] `/pdf-to-md` 스킬 생성 (Claude Code 플러그인)
- [ ] Docling 영속 서버 모드 (cold start 제거 → 15초/논문 목표)
- [ ] 벤치마크 수식 의미 정확도 추가
- [ ] IEEE/APS 2-column + arXiv preprint 지원
