# paper-to-md 프로젝트 컨텍스트

## 프로젝트 개요
학술논문 PDF → 고품질 Markdown 변환 파이프라인 (hybrid v9).
Docling + MinerU UniMerNet 하이브리드 구조.

## 실행 환경
- **PC**: Simulation Notebook1 (Windows 11, RTX 3070 Laptop GPU)
- **작업 디렉토리**: `D:\GitHub\Obsidian\` (run_paper_hybrid.py가 상대경로 사용)
- **Python**: `envs/mineru/.venv/Scripts/python.exe`
- **GPU**: CUDA 12.4, torch 2.6.0+cu124, onnxruntime-gpu

## 실행 방법
```bash
# D:\GitHub\Obsidian\ 에서 실행할 것
cd D:\GitHub\Obsidian
"D:/GitHub/paper-to-md/envs/mineru/.venv/Scripts/python.exe" \
  "D:/GitHub/paper-to-md/run_paper_hybrid.py" <pdf_path>
```

## 핵심 파일
- `run_paper_hybrid.py` — 메인 파이프라인 (hybrid v9)
- `engines/docling_convert.py` — Docling GPU 최적화 (OCR끔, FAST, batch16)
- `engines/postprocess.py` — 후처리 (PUA, 리가처, 워터마크)
- `engines/text_normalize.py` — 공유 정규화 모듈
- `learning/` — 자기발전 시스템 Phase 1~4
- `DEVELOPMENT.md` — 버전 이력, 벤치마크 기록
- `ROADMAP.md` — 미구현 아이디어, 장기 설계

## 알려진 이슈 (v9.1 수정 예정)
- npj 저널: `fi`/`fl` 리가처 미처리
- ACS 저널: 참고문헌 `(N)` 스타일 미파싱
- npj 저널: YAML first_author에 figure 오삽입
- ACS (preprint 출처): Chalmers/PMC 워터마크 YAML 오염

## 장기 목표
orchestrator 패턴으로 Judge + Fixer + Analyst + Classifier agent 분리 구현.
상세 설계: ROADMAP.md 참고.
