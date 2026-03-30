---
date: 2026-03-31
purpose: "hybrid 파이프라인 미구현 아이디어 및 장기 설계 논의 기록"
author: "HDK"
agent: "Claude Code"
type: roadmap
---

# hybrid 파이프라인 장기 로드맵 (미구현 아이디어)

> 실제 구현된 내용은 `DEVELOPMENT.md` 참고.
> 이 파일은 논의했지만 구현하지 않은 설계 아이디어와 방향성을 기록한다.

---

## 1. 자동 품질 검증 + 자기개선 파이프라인

### 배경 (2026-03-31 논의)
현재 자기발전 시스템(Phase 1~4)은 구현 완료됐지만 `corrections_log.jsonl`이 0건.
사용자가 수동으로 MD를 교정하고 `correction_tracker.py`를 실행해야 학습 데이터가 쌓이는 구조 → 너무 번거로움.

### 아이디어: GAN 스타일 Judge-Fixer 루프

```
PDF → hybrid v9 → MD
                    ↓
              [Judge Agent]  ← 규칙 기반 + 원본 PDF 메타데이터 비교
              수식/섹션/참고문헌/이미지 수 검증
              리가처, 워터마크, YAML 오염 감지
              → 점수 + 교정 지시 자동 생성
                    ↓
              [Fixer Agent]  ← 교정 지시 받아 MD 자동 수정
                    ↓
              corrections_log.jsonl 자동 누적
              → journal_learner.py 자동 학습
```

### 설계 원칙
- Judge는 **경량 규칙 기반**으로 유지 (LaTeX 재컴파일 비교는 너무 느림)
- 원본 PDF에서 pdfminer/pymupdf로 수식/섹션/참고문헌 수 추출 → MD와 비교
- 리가처(`fi`/`fl`), 워터마크, YAML 오염은 패턴 매칭으로 즉시 감지 가능

### 보류 이유
- Judge 규칙셋 설계에 시간 필요
- 현재 테스트셋(8+3편)으로는 규칙 검증이 부족

---

## 2. 논문 분석 파이프라인 (Analyst)

### 아이디어
변환 완료된 MD를 자동으로 분석해서 연구에 활용 가능한 형태로 가공.

```
MD (변환 완료)
    ↓
[Analyst Agent]
    - Abstract → YAML summary 자동 추출 (규칙 기반, 무료)
    - 키워드 → PDF 키워드 섹션 + 빈도 기반 추출 (규칙 기반)
    - 분류 → paper_taxonomy.json 매칭 (규칙 기반)
    - 내 연구와의 관련성 / insight → LLM 필요 (선택)
    ↓
YAML frontmatter 자동 업데이트
Obsidian 노트로 자동 이동 + 분류
```

### 비용 방침
- **자동화 구간**: 규칙 기반으로 무료 처리 (요약, 키워드, 분류)
- **심층 분석**: Claude Code에서 수동 요청 시 처리 ("이 논문 내 연구와 관련성 분석해줘")
- Claude API 비용 지불 의향 없음 → API 호출 자동화 없음

### LLM 옵션 (나중에 맥북 도착 시 검토)
- MacBook Pro M5 Max (128GB) + Ollama로 로컬 LLM 실행 가능
- Qwen2.5 72B / Gemma3 27B 수준이면 analyst 용도로 충분
- **단, converter(MinerU+Docling)는 CUDA 최적화가 핵심 → Windows RTX 3070이 당분간 더 빠름**
- 최적 분산 구조: 변환(Windows) + 분석(MacBook Ollama)

---

## 3. 권장 orchestrator 아키텍처

### 설계 (Agent 분리 패턴)

```
orchestrator.py  ← 단일 진입점
    │
    ├─ converter     현재 run_paper_hybrid.py     ~30s
    ├─ judge         규칙 기반 경량               ~3s
    ├─ fixer         judge 결과 받아 자동 수정    ~5s
    ├─ analyst       규칙 기반 (자동) + LLM(선택) ~2s+
    └─ classifier    paper_taxonomy.json 매칭     ~1s
```

### 선택 이유 (통합 vs 분리 검토 결과)
- 통합 단일 스크립트: 단순하지만 일부만 재실행 불가, 기능 추가 시 비대화
- **Agent 분리**: 단계별 독립 실행, 캐싱, 병렬화 가능 → 채택
- analyst는 백그라운드 실행 → 체감 속도 거의 동일

### 단계별 캐싱 구조
```
_Inbox-Papers/
  {paper_key}.pdf
  {paper_key}_Hybrid_Full.md      ← converter 출력 (캐시)
  {paper_key}_judge.json          ← judge 결과 (캐시)
  {paper_key}_analysis.json       ← analyst 결과 (캐시)
```

### 예상 총 소요시간
| 모드 | 시간 | 용도 |
|------|------|------|
| 변환만 | 26~57s | 빠른 확인 |
| 변환+Judge+Fixer | 35~70s | 기본 모드 |
| 전체 (analyst 백그라운드) | 50~90s | 권장 모드 |

---

## 4. 최종 비전: PDF → 연구 활용까지 자동화

```
PDF 드롭
    ↓
변환 (hybrid v9)
    ↓
Judge + Fixer (품질 자동 보정)
    ↓
Analyst (요약, 키워드, 분류 자동)
    ↓
Obsidian 노트 생성 + 폴더 분류
    ↓
내 연구 노트와 자동 링크
    ↓
(선택) Claude Code에 "이 논문 분석해줘" → insight, 실험 아이디어
```

### 보류 이유
- orchestrator + judge + fixer + analyst + classifier 순차 구현 필요
- 현재 우선순위: v9.1 신규 저널 포맷 수정 (리가처, ACS 참고문헌 파서) 먼저

---

## 구현 우선순위 (현재 판단 기준)

| 순위 | 항목 | 이유 |
|------|------|------|
| 1 | npj 리가처(`fi`/`fl`) 자동 수정 | 명확한 버그, 빠른 수정 가능 |
| 2 | ACS 참고문헌 `(N)` 파서 | E1 점수 개선 |
| 3 | Judge Agent (경량) | 자동 품질 검증 기반 |
| 4 | Analyst Agent (규칙 기반) | 연구 활용 가치 |
| 5 | orchestrator.py | 위 4개 완성 후 통합 |
| 6 | Ollama analyst 연동 | 맥북 도착 후 검토 |
