"""조립된 이력서(MD)를 PDF로 만든다. 새 의존성 없이 Playwright의 print-to-PDF를 쓴다.

## 왜 PDF인가

원티드는 지원 시 **플랫폼에 저장된 이력서를 고르는** 방식이고, 그 목록에는
직접 작성한 것과 업로드한 파일이 함께 들어간다(실측: `Resume2025YeilPark.pdf ·
업로드 완료`가 선택 가능한 항목으로 있었다). 즉 업로드가 등록 경로로 동작한다.

원티드 자체 이력서 편집기를 폼 자동화로 채우는 방법도 있지만 필드가 많아
레시피가 깨지기 쉽다. 업로드는 스텝 하나다.

## 왜 마크다운 렌더러를 안 쓰나

이력서는 표도 코드블록도 없는 평문 + 불릿이다. 전용 라이브러리를 넣을 이유가
없고, 넣으면 스타일을 그쪽 기본값에 맞춰야 한다. 필요한 변환은 네 가지뿐이다.
"""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path

from .paths import RESUME_OUT_DIR

log = logging.getLogger(__name__)

# A4 한 장에 담기게 하는 값들. 이력서는 여백이 넓으면 분량이 부풀어 보인다.
CSS = """
@page { size: A4; margin: 14mm 15mm; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
  font-size: 10pt; line-height: 1.55; color: #111; margin: 0;
}
h1 { font-size: 17pt; margin: 0 0 2pt; letter-spacing: -0.02em; }
h2 {
  font-size: 10.5pt; margin: 15pt 0 5pt; padding-bottom: 3pt;
  border-bottom: 1px solid #ccc; letter-spacing: 0.02em;
}
.contact { color: #555; font-size: 9pt; margin-bottom: 1pt; }
.tagline { font-size: 10.5pt; color: #000; font-weight: 600; margin: 6pt 0 0; }
.item { font-weight: 600; margin: 9pt 0 2pt; }
.sub { color: #555; font-size: 9pt; margin: 0 0 3pt; }
ul { margin: 0 0 0 0; padding-left: 13pt; }
li { margin: 1.5pt 0; }
p { margin: 2pt 0; }
a { color: #111; text-decoration: none; }
code { font-family: ui-monospace, Menlo, monospace; font-size: 9pt; }
"""

_URL = re.compile(r"(https?://[^\s]+)")
_CODE = re.compile(r"`([^`]+)`")
# '총 9개월' 같은 요약 줄. 조직명 자리에 오지만 조직명이 아니다.
_SUMMARY = re.compile(r"^총\s|^\d{4}\.\d{2}")

# 섹션 제목으로 승격할 줄. 가이드 §2 출력 포맷이 고정이라 열거로 충분하다.
SECTIONS = (
    "핵심역량", "경력", "개인 프로젝트", "학력", "스킬",
    "AI 활용", "기타 / 외국어", "기타", "외국어", "링크", "자격증", "수상",
)

# 회사·학교 이름이 등장하는 섹션. 여기서만 블록 첫 줄을 조직명으로 승격한다.
ORG_SECTIONS = ("경력", "학력", "개인 프로젝트")


def _inline(text: str) -> str:
    out = html.escape(text)
    out = _CODE.sub(r"<code>\1</code>", out)
    return _URL.sub(r'<a href="\1">\1</a>', out)


def to_html(md: str, *, title: str = "이력서") -> str:
    """가이드 §2 포맷에 맞춘 최소 변환. 범용 마크다운을 처리하지 않는다."""
    lines = md.splitlines()
    body: list[str] = []
    in_list = False
    # 빈 줄로 나뉜 블록의 첫 줄은 조직명(회사·학교)이다. 그 아래 날짜·고용형태와
    # 같은 회색으로 깔면 계층이 무너져 훑을 때 회사가 안 보인다.
    block_start = True

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            body.append("</ul>")
            in_list = False

    for i, raw in enumerate(lines):
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            close_list()
            block_start = True
            continue

        # 불릿 — 들여쓰기 여부와 무관하게 같은 목록으로 다룬다
        if stripped.startswith(("·", "-", "•")):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{_inline(stripped.lstrip('·-• ').strip())}</li>")
            continue

        close_list()

        if i == 0:  # 첫 줄은 이름
            body.append(f"<h1>{_inline(stripped)}</h1>")
        elif stripped in SECTIONS:
            body.append(f"<h2>{_inline(stripped)}</h2>")
            # 조직이 나오는 섹션만 제목 직후를 블록 시작으로 본다. 스킬·링크
            # 섹션의 첫 줄까지 굵게 만들면 오히려 계층이 흐려진다.
            block_start = stripped in ORG_SECTIONS
            continue
        elif i <= 3 and ("@" in stripped or stripped.replace("-", "").isdigit()):
            body.append(f'<p class="contact">{_inline(stripped)}</p>')
        elif i <= 4:
            body.append(f'<p class="tagline">{_inline(stripped)}</p>')
        # 들여쓴 줄 = 프로젝트 제목
        elif raw.startswith(("  ", "\t")):
            body.append(f'<p class="item">{_inline(stripped)}</p>')
        # 블록 첫 줄 = 조직명. 단 '총 9개월' 같은 요약 줄은 제외한다.
        elif block_start and not _SUMMARY.match(stripped):
            body.append(f'<p class="item">{_inline(stripped)}</p>')
        else:
            body.append(f'<p class="sub">{_inline(stripped)}</p>')

        block_start = False

    close_list()
    return (
        f"<!doctype html><html lang=ko><head><meta charset=utf-8>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body>{''.join(body)}</body></html>"
    )


# --- 아래 두 함수(to_pdf, render_latest)는 현재 쓰이지 않는다 (2026-08-16) ---
# PDF 업로드로 이력서를 등록하는 경로를 노렸으나, 원티드 파일 선택은 OS 대화상자를
# 띄워서 자동화가 닿지 않는다. 대신 '사본 만들기'로 완성된 이력서를 복제하는
# 경로를 쓴다(config.yaml의 resumes.copy_from).
#
# 지우지 않고 남기는 이유: 사람인·자소설은 업로드형 지원이 흔하고, 그때 이
# 변환기가 그대로 필요하다(config.yaml — saramin/jasoseol 어댑터는 검증만 되고
# 레시피는 아직 없음). json_to_markdown/preview_image는 조립 결과를 로컬에서
# 그려 미리보기로 보내던 용도였는데, cli.py가 실제 원티드 편집기 화면 스크린샷을
# 쓰도록 바뀌며(커밋 "지원 준비 알림 사진을 실제 원티드 화면으로 바꾼다") 마지막
# 호출부를 잃어 완전히 죽은 코드가 됐다 — 그래서 이 둘은 제거했다.
def to_pdf(md_path: Path | str, pdf_path: Path | str | None = None) -> Path:
    """MD를 A4 PDF로 굽는다. Chromium이 이미 있으므로 추가 설치가 없다."""
    from playwright.sync_api import sync_playwright

    md_path = Path(md_path)
    pdf_path = Path(pdf_path) if pdf_path else md_path.with_suffix(".pdf")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    doc = to_html(md_path.read_text(encoding="utf-8"), title=md_path.stem)
    tmp = md_path.with_suffix(".html")
    tmp.write_text(doc, encoding="utf-8")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(tmp.as_uri(), wait_until="load")
                page.pdf(path=str(pdf_path), format="A4", print_background=True)
            finally:
                browser.close()
    finally:
        tmp.unlink(missing_ok=True)

    log.info("PDF 생성: %s (%.0fKB)", pdf_path, pdf_path.stat().st_size / 1024)
    return pdf_path


def render_latest(job_id: int) -> Path:
    """`cli.py resume`가 만든 MD를 찾아 PDF로 굽는다."""
    matches = sorted(RESUME_OUT_DIR.glob(f"{job_id}-*.md"))
    if not matches:
        raise FileNotFoundError(f"공고 {job_id}의 조립된 이력서가 없다 — cli.py resume {job_id}")
    return to_pdf(matches[-1])
