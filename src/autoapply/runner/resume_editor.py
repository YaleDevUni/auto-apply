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
import re
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
    _dismiss(page)
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
    _dismiss(page)
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

    # 자동완성은 타이핑 중 글자가 유실되는 일이 있다. 실측에서 회사명이
    # "Good Things Consignment" → "Good Things Consignmet"로 저장됐다.
    # 드롭다운이 다시 그려지는 사이 타이핑한 글자가 유실된다. 실측:
    # "Good Things Consignment" → "Good Things Consignmet" (n 하나가 사라짐).
    # 확정된 뒤에는 일반 입력이 먹으므로 여기서 바로잡는다.
    got = loc.input_value().strip()
    if got != value.strip():
        log.info("자동완성 중 글자 유실 (%r → %r) — 교정한다", value, got)
        loc.fill(value)
        loc.press("Tab")
        page.wait_for_timeout(600)
        if loc.input_value().strip() != value.strip():
            log.warning("교정 실패: %r 로 남았다", loc.input_value().strip())


def _dismiss(page) -> None:
    """열려 있는 드롭다운·피커를 닫는다.

    원티드 편집기는 열린 팝업 뒤에 투명 백드롭(role=presentation)을 깐다.
    그게 남아 있으면 다음 필드 클릭이 백드롭에 먹혀 계속 재시도만 하다 죽는다.
    각 상호작용 전에 한 번 걷어낸다.
    """
    for _ in range(3):
        if not page.locator('[role="presentation"]').count():
            return
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)


def _date_buttons(page):
    """날짜 버튼 **전체**를 순서대로 준다.

    `button:has-text("YYYY.MM")`만 쓰면 아직 안 채운 버튼만 잡혀서, 하나 채울
    때마다 목록이 줄고 인덱스가 밀린다. 실측에서 학력 날짜가 주요성과 칸에
    들어간 원인이 이것이다. 채워진 것(2023.08)도 함께 매칭해 순서를 고정한다.
    """
    return page.locator("button").filter(
        has_text=re.compile(r"^(YYYY\.MM|\d{4}\.\d{2})$")
    )


def _set_date(page, nth: int, value: str) -> bool:
    """YYYY.MM 버튼을 눌러 연도·월을 고른다. value는 "2023.08" 형식.

    날짜는 입력 칸이 아니라 버튼이고, 누르면 1950년부터의 연도 목록이 뜬다.
    연도를 고르면 그 자리에 월 목록이 나타난다. 타이핑으로는 넣을 수 없다.
    """
    try:
        year, month = value.split(".")[:2]
    except ValueError:
        log.warning("날짜 형식이 아니다: %s", value)
        return False

    _dismiss(page)
    btns = _date_buttons(page)
    if btns.count() <= nth:
        return False
    btns.nth(nth).click()
    page.wait_for_timeout(1200)

    dlg = page.locator("[role=dialog]").first
    y = dlg.locator(f'button:text-is("{year}")')
    if not y.count():
        log.warning("연도 %s 를 못 찾음", year)
        page.keyboard.press("Escape")
        return False
    y.first.click()
    page.wait_for_timeout(900)

    m = dlg.locator(f'button:text-is("{int(month)}월")')
    if not m.count():
        log.warning("월 %s 를 못 찾음", month)
        page.keyboard.press("Escape")
        return False
    m.first.click()
    page.wait_for_timeout(900)
    return True


def _set_select(page, placeholder: str, value: str) -> bool:
    """원티드 커스텀 셀렉트(재직 형태·졸업 상태)를 고른다.

    <select>가 아니라 div[data-role=select-render-wrapper]다. 값이 안 정해진
    상태에서는 안쪽 span이 placeholder 문구를 들고 있어서 그걸로 찾는다.
    """
    _dismiss(page)
    wrap = page.locator(
        f'[data-role="select-render-wrapper"]:has([data-role="select-placeholder"]:text-is("{placeholder}"))'
    )
    if not wrap.count():
        log.info("셀렉트를 못 찾음(이미 선택됐을 수 있다): %s", placeholder)
        return False
    wrap.first.click()
    page.wait_for_timeout(1200)

    # 옵션 텍스트는 span 안에 있고 그 span은 클릭을 안 받는다.
    # 실제로 눌러야 하는 건 li/[role=option] 쪽이다.
    for sel in (f'[role=option]:has-text("{value}")', f'li:has-text("{value}")'):
        opt = page.locator(sel)
        if opt.count():
            opt.first.click(force=True)
            page.wait_for_timeout(800)
            return True

    log.warning("셀렉트 옵션 %s 를 못 찾음 (%s)", value, placeholder)
    page.keyboard.press("Escape")
    return False


# YYYY.MM 버튼의 화면상 순서. 편집기 레이아웃이 고정이라 순번으로 잡는다.
# (섹션 제목이 h2/h3가 아니어서 DOM으로 소속을 찾을 수 없다)
DATE_SLOTS = {"exp_start": 0, "exp_end": 1, "edu_start": 4, "edu_end": 5}


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

        # 날짜 — 버튼+피커라 입력 필드와 처리가 다르다. 뒤에 몰아서 한다
        # (피커가 열려 있으면 다른 필드 클릭이 가려진다).
        dates: dict[str, bool] = {}
        if exps:
            e0 = exps[0]
            if e0.get("start"):
                dates["exp_start"] = _set_date(p, DATE_SLOTS["exp_start"], e0["start"])
            if e0.get("end"):
                dates["exp_end"] = _set_date(p, DATE_SLOTS["exp_end"], e0["end"])
        if edus:
            d0 = edus[0]
            if d0.get("start"):
                dates["edu_start"] = _set_date(p, DATE_SLOTS["edu_start"], d0["start"])
            if d0.get("end"):
                dates["edu_end"] = _set_date(p, DATE_SLOTS["edu_end"], d0["end"])

        # 필수 셀렉트. 이게 비면 완성도가 안 올라가고 지원 시 반려될 수 있다.
        selects: dict[str, bool] = {}
        if exps:
            selects["재직 형태"] = _set_select(p, "재직 형태", "정규직")
        if edus:
            selects["졸업 상태"] = _set_select(p, "졸업 상태", "졸업")

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
            "dates": dates, "selects": selects,
            "ok": not lost,
            "prefilled_skipped": list(PREFILLED),
        }
