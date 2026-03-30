#!/bin/bash
# Obsidian/scripts/ → paper-to-md/ 동기화 스크립트
# 사용법: bash sync_from_obsidian.sh
# 완료 후 git commit/push는 수동으로 실행

SRC="D:/GitHub/Obsidian/scripts"
DST="D:/GitHub/paper-to-md"

echo "[sync] Obsidian/scripts/ → paper-to-md/ 동기화 시작..."

# 메인 스크립트
cp "$SRC/run_paper_hybrid.py"       "$DST/run_paper_hybrid.py"
cp "$SRC/run_pymupdf4llm_hybrid.py" "$DST/run_pymupdf4llm_hybrid.py"
cp "$SRC/run_benchmark.py"          "$DST/run_benchmark.py"
cp "$SRC/md_to_latex.py"            "$DST/md_to_latex.py"
cp "$SRC/benchmark_groundtruth.json" "$DST/benchmark_groundtruth.json"
cp "$SRC/journal_profiles.json"     "$DST/journal_profiles.json"
cp "$SRC/paper_taxonomy.json"       "$DST/paper_taxonomy.json"
echo "[OK] 메인 스크립트"

# engines/
cp "$SRC/engines/postprocess.py"        "$DST/engines/postprocess.py"
cp "$SRC/engines/docling_convert.py"    "$DST/engines/docling_convert.py"
cp "$SRC/engines/text_normalize.py"     "$DST/engines/text_normalize.py"
cp "$SRC/engines/pymupdf4llm_convert.py" "$DST/engines/pymupdf4llm_convert.py"
echo "[OK] engines/"

# learning/
cp "$SRC/learning/correction_tracker.py" "$DST/learning/correction_tracker.py"
cp "$SRC/learning/parameter_store.py"    "$DST/learning/parameter_store.py"
cp "$SRC/learning/regression_guard.py"   "$DST/learning/regression_guard.py"
cp "$SRC/learning/journal_learner.py"    "$DST/learning/journal_learner.py"
echo "[OK] learning/"

# 문서 (ROADMAP.md는 paper-to-md가 원본이므로 제외)
# DEVELOPMENT.md는 양쪽 모두 편집될 수 있으므로 주의
# cp "$SRC/DEVELOPMENT.md" "$DST/DEVELOPMENT.md"
echo "[SKIP] DEVELOPMENT.md — 수동으로 병합할 것"

echo ""
echo "[sync] 완료! 변경사항 확인 후 git commit/push 하세요."
echo ""
echo "  cd D:/GitHub/paper-to-md"
echo "  git status"
echo "  git add -A"
echo "  git commit -m \"sync: update from Obsidian scripts\""
echo "  git push"
