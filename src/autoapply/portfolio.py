"""공고에 맞는 포트폴리오를 에이전트가 고른다.

원티드 지원 폼은 이력서와 포트폴리오를 **같은 첨부파일 목록**에서 체크한다
(실측 2026-08-16, 공고 362772/유에스소프트) — 별도 섹션이 아니다. 그래서
지원 코드 쪽은 이력서(`job.resume`)와 똑같은 패턴으로 다룬다.

어느 포트폴리오를 낼지는 규칙이 아니라 이 모듈이 공고 본문을 읽고 고른다.
트랙(대분류)보다 세밀한 결이라 키워드로는 못 가른다 — 같은 '개발' 트랙 안에
AX/풀스택/데브옵스 공고가 섞여 있다.

## 유니코드 NFD 함정 (실측 2026-08-16)

원티드는 업로드형 문서(포트폴리오 PDF)의 파일명만 유니코드 NFD(분해형)로
렌더링한다. 사람이 직접 지은 이력서 제목(사본메이커 등)은 NFC(조합형)다 —
아마 macOS 파일시스템이 NFD로 저장한 원본 파일명을 그대로 통과시키기
때문으로 보인다. Playwright의 `text-is()`는 바이트 단위 비교라, config.yaml에
NFC로 적어둔 제목을 정규화 없이 그대로 셀렉터에 넣으면 화면에서 못 찾는다 —
그런데 그 스텝이 optional이라 **에러도 안 나고 조용히 건너뛰어진다**. 사람은
포트폴리오가 빠진 채 지원됐다는 걸 알 방법이 없다.

그래서 저장·표시용 제목(NFC)과 레시피 셀렉터용 제목(NFD)을 분리한다.
`to_selector_text()`가 그 변환을 맡는다.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from . import llm

log = logging.getLogger(__name__)

NO_MATCH = "없음"

PROMPT = """아래 [채용공고]에 가장 잘 맞는 포트폴리오를 [포트폴리오 목록]에서 하나 고르라.

규칙:
- 반드시 [포트폴리오 목록]에 있는 제목을 글자 하나 틀리지 않고 그대로 출력한다.
- 여러 개가 비슷하게 맞으면 더 좁고 구체적으로 맞는 쪽을 고른다.
- 어느 것도 맞지 않으면(예: 개발과 무관한 공고) 정확히 `{no_match}` 한 줄만 출력한다.
- 제목 한 줄 외에 어떤 설명도, 따옴표도 붙이지 않는다.

# 포트폴리오 목록
{listing}

# 채용공고
{company} — {title}

{description}
"""


def catalog(cfg: dict[str, Any]) -> list[dict[str, str]]:
    return (cfg.get("portfolios") or {}).get("catalog") or []


def match(job: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """공고에 맞는 포트폴리오 제목을 고른다. 맞는 게 없으면 None.

    반환값은 항상 catalog에 있는 제목 그대로이거나 None이다 — 모델이 다른
    문자열을 내면(변형하거나 지어내면) 매칭 실패로 보고 None으로 되돌린다.
    잘못된 제목을 그대로 넘기면 화면에서 못 찾아 optional 스텝이 조용히
    건너뛰므로, 여기서 걸러야 최소한 로그로 남길 수 있다.
    """
    items = catalog(cfg)
    if not items:
        return None

    listing = "\n".join(f"- {it['title']}: {(it.get('summary') or '').strip()}" for it in items)
    prompt = PROMPT.format(
        no_match=NO_MATCH,
        listing=listing,
        company=job.get("company") or "",
        title=job.get("title") or "",
        description=(job.get("description") or "")[:3000],
    )

    raw = llm.ask(prompt).strip().strip('"').strip("'")
    valid = {it["title"] for it in items}
    if raw in valid:
        return raw
    if raw != NO_MATCH:
        log.warning("포트폴리오 매칭 응답이 목록에 없음 — 미매칭으로 처리: %r", raw[:80])
    return None


def to_selector_text(title: str | None) -> str | None:
    """지원 레시피 셀렉터용 NFD 정규화. 저장·표시용 제목(NFC)은 건드리지 않는다."""
    if not title:
        return None
    return unicodedata.normalize("NFD", title)
