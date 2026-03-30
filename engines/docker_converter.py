"""
Docker PDF Converter Client
pdf-processor:api 컨테이너의 REST API를 호출하여 PDF → Markdown 변환

사용법:
    python docker_converter.py paper.pdf
    python docker_converter.py paper.pdf --engine marker
    python docker_converter.py paper.pdf --output-dir /path/to/output
"""

import sys
import time
import shutil
import argparse
import requests
from pathlib import Path

# ── 설정 ──────────────────────────────────────────────────
API_URL     = "http://localhost:8080"
POLL_INTERVAL = 3   # 초
TIMEOUT       = 300  # 최대 대기 초 (5분)


def check_server() -> bool:
    """API 서버 가동 여부 확인"""
    try:
        r = requests.get(f"{API_URL}/status", timeout=5)
        return r.status_code == 200
    except requests.ConnectionError:
        return False


def convert_pdf(
    pdf_path: Path,
    engine: str = "docling",
    output_dir: Path | None = None,
    sync: bool = False,
) -> Path | None:
    """
    PDF를 Docker API 서버를 통해 Markdown으로 변환합니다.

    Returns:
        변환된 .md 파일 경로 (output_dir 아래에 저장됨), 실패 시 None
    """
    if not check_server():
        print(
            "[docker_converter] API 서버에 연결할 수 없습니다.\n"
            "아래 명령으로 컨테이너를 먼저 시작하세요:\n"
            "  docker run -d -p 8080:8080 --name pdf-api pdf-processor:api"
        )
        return None

    if output_dir is None:
        output_dir = pdf_path.parent

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[docker_converter] 변환 시작: {pdf_path.name} (엔진: {engine})")

    # ── 파일 업로드 ───────────────────────────────────────
    with pdf_path.open("rb") as f:
        resp = requests.post(
            f"{API_URL}/convert",
            files={"file": (pdf_path.name, f, "application/pdf")},
            data={"engine": engine, "sync": "false"},
            timeout=30,
        )

    if resp.status_code not in (200, 202):
        print(f"[docker_converter] 업로드 실패: {resp.status_code} {resp.text}")
        return None

    result = resp.json()
    job_id   = result["job_id"]
    poll_url = f"{API_URL}{result['poll_url']}"
    print(f"[docker_converter] 작업 ID: {job_id}  폴링 중...")

    # ── 완료 대기 ─────────────────────────────────────────
    waited = 0
    while waited < TIMEOUT:
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

        status_resp = requests.get(poll_url, timeout=10)
        if status_resp.status_code != 200:
            print(f"[docker_converter] 상태 조회 실패: {status_resp.status_code}")
            return None

        job = status_resp.json()
        status = job["status"]

        if status == "done":
            break
        elif status == "error":
            print(f"[docker_converter] 변환 오류: {job.get('error')}")
            return None
        else:
            print(f"  ... {status} ({waited}s)")

    else:
        print(f"[docker_converter] 타임아웃 ({TIMEOUT}s 초과)")
        return None

    # ── 결과 다운로드 ─────────────────────────────────────
    download_url = f"{API_URL}/jobs/{job_id}/download"
    dl_resp = requests.get(download_url, timeout=60, stream=True)

    if dl_resp.status_code != 200:
        print(f"[docker_converter] 다운로드 실패: {dl_resp.status_code}")
        return None

    # Content-Disposition 에서 파일명 추출
    cd = dl_resp.headers.get("content-disposition", "")
    md_filename = pdf_path.stem + f"_{engine.capitalize()}_Full.md"
    if "filename=" in cd:
        md_filename = cd.split("filename=")[-1].strip().strip('"')

    md_path = output_dir / md_filename
    with md_path.open("wb") as f:
        for chunk in dl_resp.iter_content(chunk_size=8192):
            f.write(chunk)

    print(f"[docker_converter] 완료: {md_path}")
    return md_path


# ── CLI ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Docker PDF → Markdown 변환기")
    parser.add_argument("pdf", help="변환할 PDF 파일 경로")
    parser.add_argument(
        "--engine",
        choices=["docling", "marker"],
        default="docling",
        help="변환 엔진 (기본: docling)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="출력 디렉토리 (기본: PDF와 동일 폴더)",
    )
    args = parser.parse_args()

    pdf_path   = Path(args.pdf).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else None

    if not pdf_path.exists():
        print(f"파일을 찾을 수 없습니다: {pdf_path}")
        sys.exit(1)

    result = convert_pdf(pdf_path, engine=args.engine, output_dir=output_dir)
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
