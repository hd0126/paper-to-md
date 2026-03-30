# paper-to-md

학술논문 PDF를 고품질 Markdown으로 변환하는 파이프라인.

## 현재 버전: hybrid v9.0

- **엔진**: Docling + MinerU UniMerNet 하이브리드
- **성능**: 94.6/100 (7카테고리), 평균 32초/논문 (RTX 3070)
- **지원 저널**: Wiley, Nature, Science Advances, ACS, npj 등

## 빠른 시작

```bash
# D:\GitHub\Obsidian\ 에서 실행
envs/mineru/.venv/Scripts/python.exe run_paper_hybrid.py <pdf_path>
envs/mineru/.venv/Scripts/python.exe run_paper_hybrid.py <pdf_path> --out-dir <output_dir>
```

## 프로젝트 구조

```
paper-to-md/
├── run_paper_hybrid.py      # 메인 실행 스크립트 (hybrid v9)
├── md_to_latex.py           # MD → LaTeX 변환
├── run_benchmark.py         # 벤치마크 실행
├── engines/                 # 변환 엔진
│   ├── docling_convert.py   # Docling 변환 (GPU 최적화)
│   ├── postprocess.py       # 후처리
│   └── text_normalize.py    # 텍스트 정규화 (공유 모듈)
├── learning/                # 자기발전 시스템
│   ├── correction_tracker.py  # Phase 1: 교정 diff 캡처
│   ├── parameter_store.py     # Phase 2: 저널별 파라미터
│   ├── regression_guard.py    # Phase 3: 회귀 방지
│   └── journal_learner.py     # Phase 4: 자동 학습
├── learning_data/           # 학습 데이터 누적
├── benchmark/               # 벤치마크 논문 (A1~F2)
├── envs/                    # 가상환경 (symlink → Obsidian/scripts/envs)
├── docs/                    # 추가 문서
├── DEVELOPMENT.md           # 버전 이력 및 벤치마크
└── ROADMAP.md               # 미구현 아이디어 및 장기 설계
```

## 벤치마크 결과

| 저널 계열 | 논문 수 | 평균 점수 | 평균 시간 |
|-----------|--------|-----------|-----------|
| Wiley/Nature/AAAS (기존) | 8편 | 94.6/100 | 32s |
| ACS Nano | 1편 | 87/100 | 57s (37p 리뷰) |
| npj 2D Materials | 2편 | 86.5/100 | 26s |

## 개발 이력

→ [DEVELOPMENT.md](DEVELOPMENT.md)

## 장기 로드맵

→ [ROADMAP.md](ROADMAP.md)
