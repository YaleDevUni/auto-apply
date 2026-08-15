"""원티드 이력서 편집기를 채운다.

## 왜 파일 업로드가 아니라 편집기인가

원티드 자체 이력서는 매칭·추천에 쓰이고, 업로드 파일에는 없는 구조화 필드
(**AI 활용 경험**)가 있다. 그리고 실측상 파일 업로드는 동의 모달 뒤에서도
파일 선택기가 열리지 않았다 — 합성 클릭으로는 안 되는 핸들러로 보인다.

## 채우는 순서와 안전장치

편집기는 저장 버튼을 따로 누르지 않고 자동 저장되는 구조다. 그래서 **채우는 것
자체가 되돌리기 어려운 동작**이다. dry_run에서는 아무것도 입력하지 않고
필드가 실제로 있는지만 확인한다 — 셀렉터가 깨졌는지 먼저 알 수 있다.

경력·학력처럼 여러 개인 항목은 '추가' 버튼을 눌러 칸을 만들어야 한다. 지금은
**이미 있는 칸만 채운다.** 칸을 만드는 것까지 자동화하면 실패했을 때 빈 칸이
남은 이력서가 생기는데, 그건 안 만드느니만 못하다.
"""

from __future__ import annotations

import logging
from typing import Any

from .session import PlaywrightSession, browser

log = logging.getLogger(__name__)


def _set(page, selector: str, value: str) -> None:
    """값을 넣고 **포커스를 뺀다.**

    원티드 편집기는 React 제어 입력에 자동저장이 붙어 있다. fill()만 하면 DOM
    값은 바뀌지만 blur/change가 안 나가 상태가 커밋되지 않는다. 실측에서 간단
    소개·AI 활용은 저장됐는데 회사명·학력이 통째로 비어 있던 원인이 이것이다.
    화면상 채워진 것처럼 보여서 더 위험하다 — 빈 이력서가 제출될 수 있다.
    """
    loc = page.locator(selector).first
    loc.click()
    loc.fill(value)
    loc.press("Tab")
    page.wait_for_timeout(350)

def _set_autocomplete(page, selector: str, value: str) -> None:
    """자동완성 드롭다운이 붙은 필드를 채운다.

    회사명·학교명·전공은 그냥 입력하면 저장되지 않는다. 원티드가 자체 DB에서
    후보를 띄우고 **목록에서 고르거나 '직접 입력하기'를 눌러야** 값이 확정된다.
    fill()로 넣으면 화면엔 글자가 보이지만 새로고침하면 사라진다 —
    실측에서 회사명·학교명·전공 셋이 정확히 그렇게 유실됐다.

    타이핑을 한 글자씩 하는 이유: fill()은 입력 이벤트를 한 번에 밀어넣어
    드롭다운 검색이 트리거되지 않는다.
    """
    loc = page.locator(selector).first
    loc.click()
    loc.fill("")
    loc.type(value, delay=55)
    page.wait_for_timeout(2200)

    # 목록에 정확히 일치하는 후보가 있으면 그것을, 없으면 '직접 입력하기'를 고른다.
    exact = page.locator(f'[role=option]:has-text("{value}")')
    direct = page.locator('[role=option]:has-text("직접 입력")')
    target = direct.first if direct.count() else (exact.first if exact.count() else None)
    if target is None:
        log.warning("자동완성 후보를 못 찾음: %s = %s", selector, value)
        loc.press("Tab")
        return
    target.click()
    page.wait_for_timeout(900)


# 자동완성이 붙은 필드. 일반 입력으로는 확정되지 않는다.
AUTOCOMPLETE = ("exp_company", "edu_school", "edu_major")

CV_URL = "https://www.wanted.co.kr/cv"

# 실측(2026-08-16)으로 확인한 편집기 필드. placeholder는 문구가 길어 앞부분만 쓴다.
FIELDS: dict[str, str] = {
    "name": 'input[name="name"]',
    "mobile": 'input[name="mobile"]',
    "email": 'input[name="email"]',
    "summary": 'textarea[placeholder^="채용 담당자들이"]',
    "ai_usage": 'textarea[placeholder^="AI를 업무에"]',
    "exp_company": 'input[placeholder="회사명"]',
    "exp_job_role": 'input[name="job_role"]',
    "exp_business_title": 'input[name="business_title"]',
    "exp_achievement_title": 'input[name="title"]',
    "exp_achievement_detail": 'textarea[placeholder^="업무 경험을 성과"]',
    "edu_school": 'input[placeholder="학교명"]',
    "edu_major": 'input[placeholder="전공 및 학위"]',
    "edu_detail": 'textarea[placeholder^="이수 과목"]',
}

# 계정에서 자동으로 채워지는 값. 덮어쓰지 않는다 — 원티드가 갖고 있는 게 정본이다.
PREFILLED = ("name", "mobile", "email")


def open_editor(s: PlaywrightSession, *, resume_url: str | None = None) -> str:
    """편집기를 연다. resume_url을 주면 그 이력서를, 아니면 새로 만든다.

    새로 만드는 건 클릭 한 번에 이력서가 실제로 생성된다는 뜻이다. 테스트로
    반복하면 계정에 빈 이력서가 쌓이므로 되도록 기존 것을 재사용한다.
    """
    if resume_url:
        s.goto(resume_url)
        s.page().wait_for_timeout(4000)
        return s.url()

    s.goto(CV_URL)
    s.page().wait_for_timeout(3500)
    s.click('button:has-text("새 이력서 작성") >> nth=0')
    s.page().wait_for_timeout(6000)
    return s.url()


def fill(
    data: dict[str, Any],
    *,
    resume_url: str | None = None,
    dry_run: bool = True,
    headless: bool = False,
) -> dict[str, Any]:
    """조립된 JSON을 편집기에 채운다.

    dry_run=True(기본)면 입력하지 않고 필드 존재만 확인한다. 편집기는 자동
    저장이라 입력 자체가 되돌리기 어렵다 — 셀렉터가 맞는지 먼저 확인한다.
    """
    with browser(headless=headless) as s:
        url = open_editor(s, resume_url=resume_url)
        p = s.page()

        found: dict[str, bool] = {}
        filled: dict[str, str] = {}
        missing: list[str] = []

        for key, sel in FIELDS.items():
            exists = p.locator(sel).count() > 0
            found[key] = exists
            if not exists:
                missing.append(key)

        if dry_run:
            return {
                "url": url, "dry_run": True,
                "found": found, "missing": missing,
                "note": "입력하지 않음. 셀렉터 확인만 했다.",
            }

        # 단일 필드부터. 계정이 채워주는 값은 건드리지 않는다.
        for key in ("summary", "ai_usage"):
            value = data.get(key)
            if value and found.get(key):
                _set(p, FIELDS[key], str(value))
                filled[key] = str(value)[:40]

        # 경력 — 첫 칸만 채운다. 칸을 새로 만들지 않는다.
        exps = data.get("experiences") or []
        if exps and found.get("exp_company"):
            e = exps[0]
            for key, src in (
                ("exp_company", "company"),
                ("exp_job_role", "job_role"),
                ("exp_business_title", "business_title"),
            ):
                if e.get(src) and found.get(key):
                    setter = _set_autocomplete if key in AUTOCOMPLETE else _set
                    setter(p, FIELDS[key], str(e[src]))
                    filled[key] = str(e[src])[:40]

            ach = (e.get("achievements") or [{}])[0]
            if ach.get("title") and found.get("exp_achievement_title"):
                _set(p, FIELDS["exp_achievement_title"], str(ach["title"]))
                filled["exp_achievement_title"] = str(ach["title"])[:40]
            if ach.get("detail") and found.get("exp_achievement_detail"):
                _set(p, FIELDS["exp_achievement_detail"], str(ach["detail"]))
                filled["exp_achievement_detail"] = str(ach["detail"])[:40]

        # 학력 — 같은 원칙
        edus = data.get("educations") or []
        if edus and found.get("edu_school"):
            ed = edus[0]
            for key, src in (
                ("edu_school", "school"),
                ("edu_major", "major"),
                ("edu_detail", "detail"),
            ):
                if ed.get(src) and found.get(key):
                    setter = _set_autocomplete if key in AUTOCOMPLETE else _set
                    setter(p, FIELDS[key], str(ed[src]))
                    filled[key] = str(ed[src])[:40]

        p.wait_for_timeout(3000)  # 자동 저장이 붙을 시간

        # 넣었다고 저장된 게 아니다. 새로고침 후 실제로 남았는지 확인한다.
        p.reload()
        p.wait_for_timeout(5000)
        persisted = {
            k: bool(p.locator(FIELDS[k]).first.input_value().strip())
            for k in filled
            if found.get(k)
        }
        lost = [k for k, ok in persisted.items() if not ok]

        return {
            "url": url, "dry_run": False,
            "filled": filled, "missing": missing,
            "persisted": persisted, "lost": lost,
            "ok": not lost,
            "prefilled_skipped": list(PREFILLED),
        }
