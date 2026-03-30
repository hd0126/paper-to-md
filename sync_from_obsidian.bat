@echo off
:: Obsidian/scripts/ → paper-to-md/ 동기화 스크립트
:: 사용법: sync_from_obsidian.bat
:: 완료 후 git commit/push는 수동으로 실행

set SRC=D:\GitHub\Obsidian\scripts
set DST=D:\GitHub\paper-to-md

echo [sync] Obsidian/scripts/ → paper-to-md/ 동기화 시작...
echo.

:: 메인 스크립트
robocopy "%SRC%" "%DST%" run_paper_hybrid.py run_pymupdf4llm_hybrid.py run_benchmark.py md_to_latex.py benchmark_groundtruth.json journal_profiles.json paper_taxonomy.json /IS /NJH /NJS
echo [OK] 메인 스크립트

:: engines/
robocopy "%SRC%\engines" "%DST%\engines" *.py /IS /NJH /NJS
echo [OK] engines/

:: learning/
robocopy "%SRC%\learning" "%DST%\learning" *.py /IS /NJH /NJS
echo [OK] learning/

:: 문서
robocopy "%SRC%" "%DST%" DEVELOPMENT.md ROADMAP.md /IS /NJH /NJS
echo [OK] 문서 (DEVELOPMENT.md, ROADMAP.md)

echo.
echo [sync] 완료! 변경사항 확인 후 git commit/push 하세요.
echo.
echo   cd D:\GitHub\paper-to-md
echo   git status
echo   git add -A
echo   git commit -m "sync: update from Obsidian scripts"
echo   git push
echo.
pause
