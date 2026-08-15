"""축 2 — 지원가능성. "이걸 자동으로 지원할 수 있나"

적합도(rules.py)와 분리한 이유는 두 판정이 직교하기 때문이다. 130점인데 외부 ATS라
손도 못 대는 공고가 있고, 70점인데 원클릭인 공고가 있다. 하나의 점수로 뭉개면
에이전트는 "왜 지원 안 했는지"를 설명할 수 없다.

## 실측 근거 (2026-08-15, 원티드)

로그아웃 상태에서 `/wd/{id}` → `지원하기`를 누르면 `id.wanted.co.kr`로 튄다.
로그인 수단은 **OAuth뿐이다** — Kakao / Apple / Google / 이메일. 비밀번호를 저장해
자동 로그인하는 경로가 아예 없다는 뜻이고, 여기서 설계가 하나 확정된다:

    로그인은 자동화 대상이 아니라 '한 번 해두고 재사용하는 상태'다.

사람이 브라우저 프로필에 한 번 로그인해두면 에이전트는 그 프로필을 계속 쓴다.
세션이 죽으면 에이전트는 **멈추고 사람을 부른다**. 로그인을 시도하지 않는다.
그래서 LOGIN_REQUIRED는 '고칠 수 있는 blocker'가 아니라 '사람을 호출하는 신호'다.

## blocker와 requirement의 차이

blocker는 *지금 못 한다*, requirement는 *하려면 이게 먼저다*.
자소서 문항은 상황에 따라 둘 다 될 수 있다 — 에이전트가 자소서를 쓸 수 있게
설정했으면 requirement(먼저 써라)고, 아니면 blocker(사람이 써야 한다)다.
config의 `applicability.essays.autowrite`가 그 스위치다.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..adapters.base import JobPosting

# 자사 채용 시스템(ATS) 호스트. 여기로 튀면 플랫폼 폼이 아니라 남의 폼이다.
# 회사마다 폼이 달라 레시피 없이는 자동화가 성립하지 않는다.
KNOWN_ATS = (
    "greetinghr.com", "greeting.works", "recruiter.co.kr", "midasitcareer.com",
    "career.co.kr", "jobkorea.co.kr", "incruit.com", "applyin.co.kr",
    "gohr.co.kr", "aplim.co.kr", "workable.com", "greenhouse.io",
    "lever.co", "ashbyhq.com", "smartrecruiters.com", "taleo.net",
)

# 본문에서 자소서 문항을 세는 신호
ESSAY_MARKERS = re.compile(
    r"자기소개서|자소서|지원동기|입사후\s*포부|성장과정|경험을\s*기술|"
    r"기술해\s*주(세요|십시오)|서술하(세요|시오)|\d{3,4}자\s*(이내|내외)",
)

# 제출용 첨부서류 신호. 파일을 미리 준비해두지 않으면 폼 중간에 막힌다.
DOC_MARKERS = {
    "졸업증명서": re.compile(r"졸업증명"),
    "성적증명서": re.compile(r"성적증명"),
    "경력증명서": re.compile(r"경력증명"),
    "자격증사본": re.compile(r"자격증\s*사본|자격증\s*제출"),
    "포트폴리오": re.compile(r"포트폴리오\s*(제출|첨부|필수)"),
    "어학성적": re.compile(r"토익|토플|오픽|opic|텝스|어학\s*성적"),
}


def _block(code: str, label: str, detail: str = "") -> dict[str, str]:
    return {"code": code, "label": label, "detail": detail}


def _days_left(deadline: str | None) -> int | None:
    if not deadline:
        return None
    try:
        d = datetime.fromisoformat(deadline.replace("Z", "+00:00")).date()
    except ValueError:
        return None
    return (d - date.today()).days


def _is_external(url: str | None) -> str | None:
    """외부 ATS 호스트면 그 호스트명을 돌려준다."""
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    for ats in KNOWN_ATS:
        if host.endswith(ats):
            return host
    return None


def _count_essays(text: str) -> int:
    """문항 수를 어림한다. 정확할 필요는 없다 — 0인지 아닌지가 먼저다.

    글자수 제한('500자 이내')이 문항 하나당 한 번 나오는 패턴을 이용한다.
    셀 수 없으면 마커가 있는 한 최소 1을 돌려준다. 0으로 잘못 세면 에이전트가
    '자소서 없음'으로 믿고 폼 중간에서 막히는데, 그게 더 나쁜 실패다.
    """
    limits = re.findall(r"\d{3,4}자\s*(?:이내|내외)", text)
    if limits:
        return len(limits)
    numbered = re.findall(r"^\s*(?:\d|[①-⑩])[.)]\s*\S+", text, re.MULTILINE)
    if numbered and ESSAY_MARKERS.search(text):
        return len(numbered)
    return 1 if ESSAY_MARKERS.search(text) else 0


def _recipe_exists(recipe_dir: Path, platform: str) -> bool:
    return (recipe_dir / f"{platform}.json").exists()


@lru_cache(maxsize=8)
def _recipe(recipe_dir: Path, platform: str) -> dict[str, Any] | None:
    """레시피를 읽는다. 판정이 폼의 실제 모양을 알아야 하는 경우가 있다.

    구체적으로 자소서 문항: 본문에 '자기소개서'가 있다고 자동지원이 막히는 게
    아니다. 원티드는 지원 폼에 문항 입력란이 **없고** 이력서 문서 하나만 받는다.
    그 경우 자기소개서 언급은 '이력서에 담을 내용'이지 '폼에서 막히는 지점'이
    아니다. 레시피만이 그 차이를 안다.
    """
    path = recipe_dir / f"{platform}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def evaluate(
    job: JobPosting,
    screening: dict[str, Any],
    cfg: dict[str, Any],
    *,
    recipe_dir: Path,
    session_ok: dict[str, bool] | None = None,
    job_id: int | None = None,
    gap_counts: dict[int, int] | None = None,
) -> dict[str, Any]:
    """공고 하나의 자동지원 가능 여부를 판정한다.

    session_ok — 플랫폼별 로그인 세션 유효 여부. 에이전트가 실행 전에 확인해서
    넘긴다. None이면 '모른다'로 보고 LOGIN_REQUIRED를 달지 않되 confidence를 깎는다.
    """
    ap = cfg.get("applicability", {})
    session_ok = session_ok or {}
    text = job.searchable_text()

    blockers: list[dict[str, str]] = []
    requires: dict[str, Any] = {}
    confidence = 1.0

    # ── 채널 판별 ──────────────────────────────────────────────
    # 어디에 폼이 있는가. 이게 정해져야 나머지 판정이 의미를 갖는다.
    external_host = _is_external(job.url) or _is_external(
        job.raw.get("employment_page_url")
    )
    if external_host:
        channel = "external_ats"
        apply_url = job.raw.get("employment_page_url") or job.url
    elif job.image_path:
        # 본문 자리에 이미지가 있어 로컬로 받아둔 공고(주로 자소설닷컴).
        # 텍스트가 없으니 요건도 문항도 읽어낼 수 없다.
        #
        # 판별 기준은 image_path다. image_url이 아니다 — 원티드는 공고마다
        # 표지 썸네일 URL이 붙어서, image_url로 판별하면 멀쩡한 텍스트 공고
        # 수백 건이 전부 '이미지형'으로 오분류된다(실측: 702건 중 628건 오탐).
        # image_path는 어댑터가 '본문이 이미지'라고 판단해 실제로 받아온 것만 채운다.
        channel = "image_only"
        apply_url = job.url
    else:
        channel = "platform_form"
        apply_url = job.url

    # ── blocker 판정 ───────────────────────────────────────────

    # 1) 점수 하한. 완전 자동화의 마지막 안전장치다.
    #    사람이 안 보는 상태에서 낮은 점수 공고에 이름을 남기면 회복이 안 된다.
    min_score = ap.get("min_fit_score", 60)
    if screening.get("fit_score", 0) < min_score:
        blockers.append(
            _block("SCORE_BELOW_BAR", "자동지원 최소 점수 미달",
                   f"{screening.get('fit_score', 0)}점 < 기준 {min_score}점")
        )

    # 2) 마감. 스크리닝에서도 걸리지만 여기서도 본다 — 스크리닝 이후에
    #    날짜가 지났을 수 있고, 마감 당일 자정 직전 지원은 실패 위험이 크다.
    left = _days_left(job.deadline)
    if left is not None:
        if left < 0:
            blockers.append(_block("CLOSED", "마감됨", job.deadline or ""))
        elif left < ap.get("min_days_left", 0):
            blockers.append(
                _block("CLOSING_TOO_SOON", "마감 임박 — 자동지원 위험",
                       f"{left}일 남음")
            )

    # 3) 채널별 구조적 제약
    if channel == "external_ats":
        blockers.append(
            _block("EXTERNAL_ATS", "외부 채용시스템으로 이동", external_host or "")
        )
        confidence = min(confidence, 0.6)
    elif channel == "image_only":
        # 비전으로 읽으면 풀 수 있다. 그래서 blocker이되 '에이전트가 해소 가능'으로 표시한다.
        blockers.append(
            _block("IMAGE_ONLY", "이미지형 공고 — 본문 텍스트 없음",
                   job.image_path or job.image_url or "")
        )
        confidence = min(confidence, 0.4)

    # 4) 로그인 세션. 실측상 세 플랫폼 모두 지원에는 로그인이 필요하다.
    #    원티드는 OAuth뿐이라 자동 로그인 경로가 존재하지 않는다 — 사람을 부른다.
    if job.platform in session_ok:
        if not session_ok[job.platform]:
            blockers.append(
                _block("LOGIN_REQUIRED", "로그인 세션 없음 — 사람이 한 번 로그인해야 함",
                       f"{job.platform}: OAuth 전용, 자동 로그인 불가")
            )
        requires["login"] = True
    else:
        requires["login"] = True
        confidence = min(confidence, 0.7)  # 세션 상태를 모른다

    # 5) 폼 레시피. 없으면 어디를 눌러야 할지 모른다.
    if channel == "platform_form" and not _recipe_exists(recipe_dir, job.platform):
        blockers.append(
            _block("NO_RECIPE", "지원 폼 레시피 없음",
                   f"{recipe_dir / (job.platform + '.json')}")
        )

    # 5-b) 이력서는 이제 공고마다 조립해서 등록한다.
    #
    # 예전에는 트랙별로 미리 만들어둔 이력서를 config에서 골랐고, 없으면
    # RESUME_MISSING으로 막았다. 지금은 `autoapply` 체인이 공고를 읽고 조립해
    # 편집기에 등록한 뒤 그 제목으로 고르므로 미리 준비해둘 필요가 없다.
    # 대신 조립이 실패하는 경우(필수요건 근거 부족)를 아래 5-c가 막는다.
    if channel == "platform_form":
        requires["resume"] = "공고별 조립"

    # 5-c) 이력서를 실제로 조립해봤더니 필수요건 근거가 없더라 — 되먹임
    #
    # 적합도 점수는 공고에 그 키워드가 있는지만 세고 사용자가 실제로 할 수 있는지와
    # 대조하지 않는다. 실측: 132점 최고점 공고(Java/Spring)가 Java 실무·Oracle·ERP
    # 전부 근거 없음이었다. 어셈블러가 공고를 읽으며 만든 이 숫자가 그 사각지대를
    # 메운다. 한 번 조립해본 공고에만 붙으므로, 첫 시도가 알아내고 이후 판정이
    # 그 공고를 목록에서 뺀다.
    n_gaps = (gap_counts or {}).get(job_id, 0) if job_id else 0
    max_gaps = ap.get("max_required_gaps", 2)
    if n_gaps > max_gaps:
        blockers.append(
            _block("REQUIREMENT_GAP", "필수요건 대응 근거 부족",
                   f"이력서 조립 시 필수 미충족 {n_gaps}건 > 기준 {max_gaps}건")
        )

    # 6) 자소서 문항 — 설정에 따라 blocker이거나 requirement다
    essay_cfg = ap.get("essays", {})
    n_essays = _count_essays(job.description)
    if job.raw.get("has_resume"):  # 자소설닷컴이 명시적으로 주는 플래그
        n_essays = max(n_essays, 1)
    # 폼에 문항 입력란이 있는 플랫폼인지 레시피에 물어본다. 없으면(원티드)
    # 자기소개서 언급은 '이력서 문서에 담을 내용'이지 폼에서 막히는 지점이 아니다.
    #
    # 실측(2026-08-15): 이 구분이 없을 때 원티드 52건이 ESSAY_REQUIRED로 막혔는데
    # 전부 "제출서류: 이력서, 자기소개서, 포트폴리오" 같은 안내문이었다. 실제
    # 지원 폼에는 문항 칸이 없다 — 이력서 하나 고르고 제출이 전부다.
    recipe = _recipe(recipe_dir, job.platform)
    form_has_essays = recipe.get("form_essays", True) if recipe else True

    if n_essays:
        requires["essays"] = n_essays
        max_auto = essay_cfg.get("max_autowrite", 3)

        if not form_has_essays:
            # 막지 않는다. 이력서를 조립할 때 반영할 요구사항으로만 남긴다.
            requires["essay_in_document"] = True
        elif not essay_cfg.get("autowrite", False):
            blockers.append(
                _block("ESSAY_REQUIRED", "자소서 문항 있음 — 자동작성 꺼져 있음",
                       f"약 {n_essays}문항")
            )
        elif n_essays > max_auto:
            blockers.append(
                _block("ESSAY_TOO_MANY", "자소서 문항이 자동작성 한도 초과",
                       f"{n_essays}문항 > 한도 {max_auto}")
            )

    # 7) 첨부서류. 미리 준비 안 돼 있으면 폼 중간에서 막힌다.
    docs = [name for name, pat in DOC_MARKERS.items() if pat.search(text)]
    if docs:
        requires["documents"] = docs
        have = set(ap.get("available_documents", []))
        missing = [d for d in docs if d not in have]
        if missing:
            blockers.append(
                _block("DOC_MISSING", "요구 서류 미보유", ", ".join(missing))
            )

    # 8) 본문 미확보.
    #
    # 파이프라인은 요청 수를 아끼려고 상위 detail_limit건만 상세를 받는다. 나머지는
    # 목록 정보만 있고 본문이 비어 있는데, 그 상태로는 자소서 문항도 첨부서류도
    # 판정할 수 없다 — 위 6·7번이 "없음"으로 나온 게 정말 없어서인지 못 읽어서인지
    # 구분이 안 된다.
    #
    # 요건을 읽지도 않고 지원하는 것은 무인 시스템이 해서는 안 되는 일이라
    # 낮은 confidence가 아니라 blocker로 세운다. 해소법은 명확하다: 상세를 받아오면
    # 풀린다(detail_limit을 올리거나 그 공고만 다시 조회).
    min_desc = ap.get("min_description_chars", 200)
    if len(job.description) < min_desc and channel != "image_only":
        blockers.append(
            _block("NO_DETAIL", "공고 본문 미확보 — 요건 확인 불가",
                   f"본문 {len(job.description)}자 < 기준 {min_desc}자")
        )
        confidence = min(confidence, 0.5)

    return {
        "actionable": not blockers,
        "channel": channel,
        "apply_url": apply_url,
        "blockers": blockers,
        "requires": requires,
        "confidence": round(confidence, 2),
    }


def summarize_blockers(conn) -> list[dict[str, Any]]:
    """왜 아무것도 지원 안 했는지 집계한다. 무인 운영의 첫 번째 디버깅 창구."""
    rows = conn.execute(
        "SELECT blockers FROM applicability WHERE actionable = 0"
    ).fetchall()
    tally: dict[str, int] = {}
    for r in rows:
        for b in json.loads(r["blockers"] or "[]"):
            tally[b["code"]] = tally.get(b["code"], 0) + 1
    return sorted(
        ({"code": k, "count": v} for k, v in tally.items()),
        key=lambda d: d["count"],
        reverse=True,
    )
