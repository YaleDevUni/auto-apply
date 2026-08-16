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

    # 필드마다 maxlength가 다르다. 넘겨서 넣으면 브라우저가 **조용히 자른다.**
    # 실측: 'AI 활용 경험'은 50자 제한인데 365자를 넣어 49자만 저장됐고,
    # 화면에도 잘린 채로 보였다. 넣기 전에 재고, 잘릴 것이면 알린다.
    limit = loc.evaluate("el => el.maxLength")
    if isinstance(limit, int) and 0 < limit < len(value):
        log.warning(
            "필드가 %d자 제한인데 %d자를 넣으려 한다 — 잘라서 넣는다 (%s)",
            limit, len(value), selector,
        )
        value = value[:limit]

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

    # 한 글자씩 치면 드롭다운이 다시 그려지는 사이 입력이 씹힌다
    # (실측: "Good Things Consignment" → "...Consignmet", 재시도하면 또 다르게 잘림).
    # 값을 한 번에 넣고 input 이벤트만 따로 쏜다 — React가 값을 읽는 경로는
    # 네이티브 setter라 이 방식이 아니면 상태가 갱신되지 않는다.
    loc.evaluate(
        """(el, v) => {
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, v);
            el.dispatchEvent(new Event('input', {bubbles: true}));
        }""",
        value,
    )
    page.wait_for_timeout(2400)

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
    """열려 있는 드롭다운·피커를 닫고 포커스를 뺀다.

    처음엔 `[role=presentation]` 개수로 백드롭이 남았는지 판단했는데 틀렸다 —
    스킬 칩이 전부 role=presentation이라 스킬을 채우고 나면 73개가 잡힌다.
    개수는 신호가 아니다. 그냥 Escape를 누르고 포커스를 빼는 게 맞다.

    포커스까지 빼는 이유: 스킬 입력칸이 열린 채로 남으면 그 팝업이 다음 섹션
    (링크) 클릭을 가로챈다. 실제로 그래서 링크가 계속 안 열렸다.
    """
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    page.wait_for_timeout(300)


def _flush(page, field: str, value: Any, *, nth: int = 0) -> None:
    """같은 항목의 텍스트 칸을 다시 건드려 저장을 깨운다.

    실측(2026-08-16): 학력 종료일을 피커로 바꾸면 화면에는 반영되는데
    **저장 요청이 아예 나가지 않는다.** 새로고침하면 서버 값(오늘 날짜)이 돌아온다.
    같은 학력 항목의 다른 칸을 편집하면 그때 `PATCH .../educations/{id}`가 나가고
    날짜까지 함께 저장된다.

    날짜 피커가 자체적으로 저장을 트리거하지 않는 구조라, 값을 바꾼 뒤 형제
    필드를 한 번 두드려주는 게 유일한 방법이다.
    """
    if not value or field not in _fields():
        return
    try:
        _set(page, f"{_fields()[field]} >> nth={nth}", str(value))
    except Exception as e:  # noqa: BLE001
        log.warning("저장 유발 실패 (%s): %s", field, e)


def _date_buttons(page):
    """날짜 버튼 **전체**를 순서대로 준다.

    `button:has-text("YYYY.MM")`만 쓰면 아직 안 채운 버튼만 잡혀서, 하나 채울
    때마다 목록이 줄고 인덱스가 밀린다. 실측에서 학력 날짜가 주요성과 칸에
    들어간 원인이 이것이다. 채워진 것(2023.08)도 함께 매칭해 순서를 고정한다.
    """
    return page.locator("button").filter(
        has_text=re.compile(_sel("date_button_pattern", r"^(YYYY\.MM|\d{4}\.\d{2})$"))
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

    # 피커에 '확인' 버튼이 있으면 눌러야 커밋된다. 없는 피커도 있어 조건부다.
    # 실측: 학력 종료일이 연·월을 골라도 이전 값(2026.08)에서 안 바뀌었고,
    # _set_date는 True를 돌려주고 있었다 — 눌렀다고 반영된 게 아니다.
    ok = dlg.locator('button:text-is("확인")')
    if ok.count():
        ok.first.click()
        page.wait_for_timeout(900)

    # 실제로 반영됐는지 대조한다. 반영 안 됐으면 True를 돌려주면 안 된다.
    shown = _date_buttons(page).nth(nth).inner_text().strip()
    if shown != f"{year}.{int(month):02d}":
        log.warning("날짜가 반영되지 않았다: %s 기대 %s.%s", shown, year, month)
        return False
    return True


def _set_select(page, placeholder: str, value: str, *, exact: bool = True) -> bool:
    """원티드 커스텀 셀렉트(재직 형태·졸업 상태)를 고른다.

    <select>가 아니라 div[data-role=select-render-wrapper]다. 값이 안 정해진
    상태에서는 안쪽 span이 placeholder 문구를 들고 있어서 그걸로 찾는다.
    """
    _dismiss(page)
    wrap_sel = _sel("select_wrapper", '[data-role="select-render-wrapper"]')
    ph_sel = _sel("select_placeholder", '[data-role="select-placeholder"]')
    wrap = page.locator(f'{wrap_sel}:has({ph_sel}:text-is("{placeholder}"))')
    if not wrap.count():
        log.info("셀렉트를 못 찾음(이미 선택됐을 수 있다): %s", placeholder)
        return False
    wrap.first.click()
    page.wait_for_timeout(1200)

    # 옵션 텍스트는 span 안에 있고 그 span은 클릭을 안 받는다.
    # 실제로 눌러야 하는 건 li/[role=option] 쪽이다.
    patterns = (
        (f'[role=option]:has-text("{value}")', f'li:has-text("{value}")')
        if not exact
        else (f'[role=option]:has-text("{value}")', f'li:has-text("{value}")')
    )
    for sel in patterns:
        opt = page.locator(sel)
        if opt.count():
            opt.first.click(force=True)
            page.wait_for_timeout(800)
            return True

    log.warning("셀렉트 옵션 %s 를 못 찾음 (%s)", value, placeholder)
    page.keyboard.press("Escape")
    return False


def _skill_variants(skill: str) -> list[str]:
    """원티드 스킬 DB가 받아들일 만한 표기 후보를 순서대로 만든다.

    가이드는 사람이 읽는 문서라 `AWS(EC2, S3)`처럼 괄호로 세부를 적는다.
    원티드는 단일 토큰만 받아 그대로는 등록되지 않는다. 가이드를 플랫폼 표기에
    맞춰 고치는 건 방향이 거꾸로이므로 여기서 변환한다.

        AWS(EC2, S3)  →  ["AWS(EC2, S3)", "AWS", "EC2", "S3"]
        Node.js       →  ["Node.js"]
    """
    out = [skill]
    base = re.sub(r"\s*[(（].*", "", skill).strip()
    if base and base != skill:
        out.append(base)
    inner = re.search(r"[(（]([^)）]*)[)）]", skill)
    if inner:
        out += [t.strip() for t in re.split(r"[,·/]", inner.group(1)) if t.strip()]
    # 중복 제거하되 순서는 유지한다 — 앞쪽이 더 정확한 표기다
    seen: set[str] = set()
    return [v for v in out if not (v in seen or seen.add(v))]


STATUS = re.compile(r"작성 (완료|중)|업로드 완료")
# 제목 자리에 끼어드는 배지들
BADGES = {"기본 이력서", "기본"}

SKILL_ACTIVATOR = "text=직무 스킬"
SKILL_INPUT = 'input[placeholder*="보유 스킬"]'
# 이미 등록된 스킬 칩. 안내 문구가 사라진 뒤 입력칸을 다시 여는 통로다.
SKILL_CHIP = '[class*="Skill"] button, [class*="skill"] button'


ACH_ADD = 'button:has-text("주요 성과 추가")'
ACH_DETAIL = 'textarea[placeholder^="업무 경험을 성과"]'
ACH_TITLE = 'input[name="title"]'


def _fill_achievements(page, achievements: list[dict], limit: int = 4) -> int:
    """주요 성과를 여러 건 채운다. 반환은 실제로 채운 건수.

    성과 칸은 '주요 성과 추가'로 하나씩 만든다. 만들기만 하고 못 채우면 빈 칸이
    남아 이력서가 지저분해지므로, 채울 내용이 있는 만큼만 만든다.

    개수의 근거는 detail textarea다. `input[name="title"]`은 어학 '시험명'도
    같은 name을 쓰기 때문에 그것만으로는 성과 칸을 셀 수 없다 — 잘못 세면
    시험명 칸에 프로젝트 제목이 들어간다.
    """
    items = [a for a in achievements if a.get("title")][:limit]
    if not items:
        return 0

    for _ in range(len(items) - page.locator(ACH_DETAIL).count()):
        add = page.locator(_sel("achievement.add_button", ACH_ADD))
        if not add.count():
            log.info("'주요 성과 추가' 버튼이 없다 — 있는 칸까지만 채운다")
            break
        _dismiss(page)
        add.first.click(force=True)
        page.wait_for_timeout(1400)

    slots = page.locator(ACH_DETAIL).count()
    filled = 0
    for i, ach in enumerate(items[:slots]):
        _set(page, f"{ACH_TITLE} >> nth={i}", str(ach["title"]))
        if ach.get("detail"):
            _set(page, f"{ACH_DETAIL} >> nth={i}", str(ach["detail"]))
        filled += 1
    return filled


# 행에 호버하면 나타나는 '추가' 아이콘. 목록에 보이는 버튼으로는 못 찾는다.
ROW_ADD = 'button[wds-component="icon-button"][data-variant="solid"]'


def _add_rows(page, row_input: str, want: int, limit: int = 4) -> int:
    """경력·학력처럼 '추가' 버튼이 안 보이는 섹션의 칸을 늘린다.

    실측: 화면에 '학력 추가' 버튼이 없어 다건 입력이 막혀 있었다. 진입점은
    **행에 마우스를 올리면 나타나는 아이콘 버튼**이고, 그것도 행 안쪽으로
    범위를 좁혀야 한다 — 페이지 전체로 잡으면 14개가 걸린다.

    반환: 최종 칸 수. 채울 내용이 있는 만큼만 만든다 — 빈 칸이 남으면
    이력서가 지저분해지고, 필수 표시(*)가 붙어 완성도도 깎인다.
    """
    for _ in range(min(want, limit)):
        have = page.locator(row_input).count()
        if have >= min(want, limit):
            break
        row = page.locator(f'xpath=//input[@placeholder="{row_input.split(chr(34))[1]}"]/ancestor::li[1]').last
        if not row.count():
            break
        _dismiss(page)
        row.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        row.hover()
        page.wait_for_timeout(1200)
        btn = row.locator(_sel("row_add_icon", ROW_ADD))
        if not btn.count():
            log.info("행 추가 버튼을 못 찾음: %s", row_input)
            break
        btn.first.click(force=True)
        page.wait_for_timeout(2000)
        if page.locator(row_input).count() <= have:
            log.info("행이 늘지 않았다 — 중단: %s", row_input)
            break
    return page.locator(row_input).count()


def _fill_languages(page, languages: list[dict], limit: int = 3) -> list[str]:
    """언어와 수준을 고른다. 둘 다 커스텀 셀렉트다.

    수준 옵션은 이름 뒤에 설명이 줄바꿈으로 붙는다("고급 비즈니스 레벨\n해당 …").
    그래서 `:text-is()`가 아니라 `:has-text()`로 잡아야 한다 — 완전일치를 쓰면
    아무것도 안 걸리고 조용히 비워둔 채 지나간다.
    """
    done: list[str] = []
    for lang in (languages or [])[:limit]:
        name, level = (lang.get("name") or "").strip(), (lang.get("level") or "").strip()
        if not (name and level):
            continue
        if not _set_select(page, "언어", name, exact=False):
            continue
        if _set_select(page, "수준", level, exact=False):
            done.append(f"{name} {level}")
    return done


def _fill_skills(page, skills: list[str], limit: int = 12) -> tuple[list[str], list[str]]:
    """스킬 칸을 채운다. 원티드 스킬 DB에 있는 것만 등록된다.

    스킬 입력칸은 처음엔 DOM에 없다. 안내 문구를 눌러야 나타나고, 그 문구는
    ActiveBox 오버레이에 덮여 있어 일반 클릭이 먹지 않는다(force 필요).

    없는 스킬은 조용히 건너뛴다 — 원티드가 인정하지 않는 이름을 억지로 넣는 것보다
    빠지는 게 낫다.
    """
    added: list[str] = []
    skipped: list[str] = []
    skill_input = _sel("skill.input", SKILL_INPUT)

    # 스킬 입력칸은 상태에 따라 세 모습이다:
    #   비어 있음      → "내가 가진 직무 스킬..." 안내 문구를 눌러 연다
    #   이미 열림      → 그대로 쓴다
    #   칩만 있고 접힘 → 안내 문구가 사라져 있다. 기존 칩을 눌러 다시 연다
    # 세 번째를 빠뜨려 "입력칸을 못 찾음"으로 계속 실패했다.
    box = page.locator(skill_input).first
    if not box.count():
        # 칩은 클래스가 해시라 셀렉터로 못 잡는다. 넣으려는 스킬 이름으로 찾는다 —
        # 이미 등록된 것이 있으면 그게 화면에 칩으로 떠 있다.
        openers = [page.locator(_sel("skill.activator", SKILL_ACTIVATOR)).first]
        openers += [page.locator(f'span:text-is("{sk}")').first for sk in skills[:limit]]
        for opener in openers:
            if not opener.count():
                continue
            opener.scroll_into_view_if_needed()
            page.wait_for_timeout(500)
            opener.click(force=True)
            page.wait_for_timeout(1600)
            box = page.locator(skill_input).first
            if box.count():
                break

    if not box.count():
        log.warning("스킬 입력칸을 열지 못했다")
        return added, skipped
    box.scroll_into_view_if_needed()

    for skill in skills[:limit]:
        chosen = None
        for variant in _skill_variants(skill):
            box.fill("")
            box.type(variant, delay=45)
            page.wait_for_timeout(1600)
            # 후보는 [role=option]이 아니라 span을 품은 button으로 뜬다.
            # 정확히 일치하는 것만 고른다 — "React"를 치면 "React Native"도 같이 나온다.
            cand = page.locator(_sel("skill.option", 'button:has(span:text-is("{value}"))').replace("{value}", variant))
            if cand.count():
                chosen, opt = variant, cand
                break
        if chosen is None:
            opt = page.locator("button:has(span:text-is(\"__없음__\"))")
        if not opt.count():
            # 원티드 스킬 DB에 없는 이름이다. 억지로 넣을 수 없으므로 건너뛰되
            # 무엇이 빠졌는지 보고한다 — 많이 빠지면 가이드의 스킬 표기를
            # 원티드 표기에 맞춰야 한다는 신호다.
            log.info("스킬 후보 없음 — 건너뜀: %s", skill)
            skipped.append(skill)
            continue
        opt.first.click(force=True)
        page.wait_for_timeout(700)
        added.append(chosen)
    return added, skipped


# 링크 행 컨테이너. 클래스 대부분이 해시(wds-*)인데 이것만 의미 있는 이름이라
# 안내 문구가 바뀌어도(빈 칸 → "GitHub") 계속 잡힌다.
LINK_ACTIVATOR = ".link-view"
LINK_NAME = 'input[placeholder*="링크명"]'
LINK_URL = 'input[placeholder*="https://"]'


def _fill_links(page, links: list[dict], limit: int = 3) -> list[str]:
    """링크(GitHub·포트폴리오)를 채운다.

    링크 입력칸도 스킬처럼 처음엔 DOM에 없다. 안내 문구를 눌러야 나타나고,
    그 문구는 오버레이에 덮여 있어 force 클릭이 필요하다. 스크롤을 먼저 하지
    않으면 클릭이 화면 밖 좌표로 나가 아무 일도 일어나지 않는다.
    """
    added: list[str] = []
    if not links:
        return added

    # 직전 스킬 입력이 열린 채로 남으면 그 팝업이 링크 행 클릭을 가로챈다.
    # 06시 사이클에서 "링크 입력칸이 열리지 않았다"가 난 이유다.
    #
    # Escape도 blur도 그 팝업을 못 닫는다. **다른 섹션 제목을 실제로 클릭**해야
    # 닫힌다 — 원티드 편집기는 "다른 곳을 눌렀다"를 포커스가 아니라 클릭으로 본다.
    _dismiss(page)
    for neutral in ("text=간단 소개", "text=학력", "text=경력"):
        loc = page.locator(neutral).first
        if loc.count():
            try:
                # 짧게 건다. 이 클릭은 팝업을 닫으려는 부수 동작이라
                # 여기서 15초(기본값)를 잡아먹으면 사이클 전체가 느려진다.
                loc.click(force=True, timeout=3000)
                page.wait_for_timeout(900)
                break
            except Exception:  # noqa: BLE001
                continue

    if not page.locator(LINK_NAME).count():
        act = page.locator(_sel("link.row", LINK_ACTIVATOR)).last
        if not act.count():
            log.warning("링크 행을 찾지 못했다")
            return added
        act.scroll_into_view_if_needed()
        page.wait_for_timeout(600)
        act.click(force=True)
        page.wait_for_timeout(1800)

    if not page.locator(LINK_NAME).count():
        log.warning("링크 입력칸이 열리지 않았다")
        return added

    for link in links[:limit]:
        name, url = (link.get("name") or "").strip(), (link.get("url") or "").strip()
        if not (name and url):
            continue
        names, urls = page.locator(LINK_NAME), page.locator(LINK_URL)
        # 마지막(빈) 칸에 넣는다. 앞칸은 이미 채워진 링크다.
        idx = names.count() - 1
        if idx < 0:
            break
        # 링크 칸은 오버레이에 덮여 있어 일반 클릭이 막힌다. force로 넣는다.
        for sel, val in (
            (f"{LINK_NAME} >> nth={idx}", name),
            (f"{LINK_URL} >> nth={min(idx, max(urls.count() - 1, 0))}", url),
        ):
            box = page.locator(sel)
            box.click(force=True)
            box.fill(val)
            box.press("Tab")
            page.wait_for_timeout(400)
        added.append(name)

        # 다음 링크를 넣으려면 빈 행을 먼저 만들어야 한다. '링크 추가' 버튼은
        # 화면에 없고, 학력과 같은 방식(행 호버 → 행 안쪽 solid 아이콘)이다.
        #
        # 이게 없을 때 두 번째 링크가 첫 번째를 덮어썼다. `_fill_links`는 넣으려
        # 시도한 이름을 그대로 돌려주므로 반환값만 보면 성공처럼 보였다 —
        # 화면 점검(audit)이 없었으면 계속 몰랐을 결함이다.
        if link is not links[:limit][-1]:
            before = page.locator(LINK_NAME).count()
            row = page.locator(_sel("link.row", LINK_ACTIVATOR)).last
            if row.count():
                row.scroll_into_view_if_needed()
                page.wait_for_timeout(400)
                row.hover()
                page.wait_for_timeout(1000)
                btn = row.locator(_sel("row_add_icon", ROW_ADD))
                if btn.count():
                    btn.first.click(force=True)
                    page.wait_for_timeout(1500)
            if page.locator(LINK_NAME).count() <= before:
                log.info("링크 행을 더 만들지 못했다 — %d개까지만 넣는다", len(added))
                break
    return added


# YYYY.MM 버튼의 화면상 순서. 편집기 레이아웃이 고정이라 순번으로 잡는다.
# (섹션 제목이 h2/h3가 아니어서 DOM으로 소속을 찾을 수 없다)
def _date_slots(n_achievements: int, n_educations: int = 1) -> dict[str, int]:
    """날짜 버튼 순서. 앞 섹션이 늘면 뒤가 그만큼 밀린다.

        0,1                      경력 재직기간
        2 .. 2+2a-1              성과 기간   (성과 1건당 2개)
        2+2a .. 2+2a+2e-1        학력 재학기간 (학력 1건당 2개)
        그 뒤                     수상·어학 등

    고정 인덱스로 잡으면 값이 엉뚱한 칸에 들어간다. 실제로 학력을 4,5로 박아뒀다가
    두 번째 성과 기간 칸에 졸업일이 들어갔고, 학력 2건일 때는 두 번째 학력 기간이
    통째로 비었다(비전 점검이 잡았다).
    """
    a = max(n_achievements, 1)
    e = max(n_educations, 1)
    slots = {"exp_start": 0, "exp_end": 1}
    for i in range(a):
        slots[f"ach{i}_start"] = 2 + 2 * i
        slots[f"ach{i}_end"] = 3 + 2 * i
    base = 2 + 2 * a
    for j in range(e):
        slots[f"edu{j}_start"] = base + 2 * j
        slots[f"edu{j}_end"] = base + 2 * j + 1
    # 이전 이름과의 호환 (학력 1건 기준)
    slots["edu_start"], slots["edu_end"] = slots["edu0_start"], slots["edu0_end"]
    return slots


# 자동완성이 붙은 필드. 일반 입력으로는 확정되지 않는다.
AUTOCOMPLETE = ("exp_company", "edu_school", "edu_major")

CV_URL = "https://www.wanted.co.kr/cv"

def _editor_cfg(platform: str = "wanted") -> dict[str, Any]:
    """레시피의 editor 섹션. 셀렉터는 데이터이므로 JSON에서 읽는다.

    자가수복 에이전트가 고칠 수 있는 것은 레시피(JSON)뿐이다 — dry-run
    스크린샷이라는 검증 오라클이 있기 때문이다. 셀렉터가 파이썬에 박혀 있으면
    원티드가 UI를 바꿀 때 사람이 붙어야 한다.

    레시피에 editor가 없으면 아래 기본값을 쓴다(구버전 레시피 호환).
    """
    from .apply import load_recipe

    try:
        return load_recipe(platform).get("editor") or {}
    except Exception:  # noqa: BLE001
        return {}


def _sel(key: str, default: str, platform: str = "wanted") -> str:
    cfg = _editor_cfg(platform)
    node: Any = cfg
    for part in key.split("."):
        node = (node or {}).get(part) if isinstance(node, dict) else None
    return node if isinstance(node, str) and node else default


# 기본값. 레시피의 editor 섹션이 이걸 덮는다.
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


def _fields(platform: str = "wanted") -> dict[str, str]:
    """레시피에 정의된 필드맵. 없는 항목은 기본값으로 채운다."""
    return {**FIELDS, **(_editor_cfg(platform).get("fields") or {})}


def audit(page, data: dict[str, Any]) -> dict[str, Any]:
    """조립한 값이 **화면에 실제로 보이는지** 확인한다.

    필드 값 대조(`input_value()`)와 다른 층위다. 그쪽은 "내가 넣은 칸"만 보므로,
    안 건드린 섹션이 비었거나 저장이 반쯤 된 상태를 못 잡는다. 여기서는 렌더된
    본문 텍스트에서 핵심 값을 찾는다 — 사람이 화면을 훑는 것과 같은 층위다.

    완성도 %를 쓰지 않는 이유: '작성 완료'를 누르면 그 패널이 사라진다.
    초안에서만 보이는 신호라 최종 점검에는 못 쓴다.

    스크린샷을 비전 모델로 읽는 방법도 있지만, 이 화면은 텍스트가 그대로
    나오므로 그쪽이 더 싸고 정확하다. 스크린샷은 사람의 최종 판단용이다.
    """
    # innerText에는 input·textarea의 **값이 들어가지 않는다.** 본문만 읽으면
    # 셀렉트가 렌더한 라벨(언어 등)만 잡히고 나머지는 전부 실패로 나온다.
    # 실제 화면에 담긴 것 = 본문 텍스트 + 모든 입력칸의 현재 값.
    haystack = page.inner_text("body") + "\n" + "\n".join(
        page.evaluate(
            """() => [...document.querySelectorAll('input,textarea')]
                 .filter(e => { const r = e.getBoundingClientRect();
                                return r.width > 0 && r.height > 0; })
                 .map(e => e.value || '')"""
        )
    )
    body = haystack
    checks: dict[str, bool] = {}

    def has(label: str, value: str | None, n: int = 12) -> None:
        if value:
            checks[label] = value.strip()[:n] in body

    exps = data.get("experiences") or []
    edus = data.get("educations") or []
    if exps:
        has("회사", exps[0].get("company"))
        ach = (exps[0].get("achievements") or [{}])[0]
        has("주요성과", ach.get("title"))
    for i, ed in enumerate(edus[:2]):
        has(f"학교{i}", ed.get("school"))
    has("간단소개", (data.get("summary") or "").splitlines()[0] if data.get("summary") else None)
    if data.get("skills"):
        checks["스킬"] = any(sk[:10] in body for sk in data["skills"][:5])
    if data.get("links"):
        has("링크", (data["links"][0] or {}).get("name"))
    if data.get("languages"):
        has("언어", (data["languages"][0] or {}).get("level"))

    missing = [k for k, ok in checks.items() if not ok]
    return {"checks": checks, "missing": missing, "ok": not missing}


def finalize(page) -> bool:
    """'작성 완료'를 눌러 이력서를 제출 가능 상태로 만든다.

    완성도가 100%여도 이걸 안 누르면 상태가 '작성 중'으로 남고, 지원 패널의
    이력서 목록에서 고를 수 없다. 실측에서 체인이 정확히 여기서 끊겼다 —
    편집기는 다 채워졌는데 지원 단계에서 이력서 선택이 안 됐다.

    제출(지원)과 다르다. 되돌릴 수 있고, 다시 편집하면 된다.
    """
    _dismiss(page)
    btn = page.locator(_sel("finalize_button", 'button:has-text("작성 완료")'))
    if not btn.count():
        log.info("'작성 완료' 버튼이 없다 — 이미 완료 상태일 수 있다")
        return False
    btn.first.click(force=True)
    page.wait_for_timeout(3000)
    return True


def read_title(page) -> str:
    """편집기 상단의 이력서 제목을 읽는다.

    지원 레시피가 이 제목으로 이력서를 고르므로(`li:has(span:text-is(...))`),
    조립 → 등록 → 지원을 잇는 고리가 여기다. 제목을 못 읽으면 어느 이력서를
    낼지 정할 수 없으므로 빈 문자열을 돌려주고 호출부가 멈추게 한다.
    """
    return page.evaluate(
        """() => {
            const anchor = [...document.querySelectorAll('*')]
                .find(e => e.children.length === 0 && (e.innerText||'').trim() === '기본 이력서 설정');
            if (!anchor) return '';
            let row = anchor.closest('div');
            for (let i = 0; i < 4 && row; i++) {
                const btn = [...row.querySelectorAll('button')]
                    .map(b => (b.innerText||'').trim())
                    .filter(t => t && t !== '기본 이력서 설정');
                if (btn.length) return btn[0];
                row = row.parentElement;
            }
            return '';
        }"""
    ).strip()


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


ALL_STEPS = ("text", "experience", "education", "dates", "selects", "skills", "links")


def fill(
    data: dict[str, Any],
    *,
    resume_url: str | None = None,
    dry_run: bool = True,
    headless: bool = False,
    only: tuple[str, ...] | None = None,
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

        for key, sel in _fields().items():
            exists = p.locator(sel).count() > 0
            found[key] = exists
            if not exists:
                missing.append(key)

        title = read_title(p)

        if dry_run:
            return {
                "url": url, "title": title, "dry_run": True,
                "found": found, "missing": missing,
                "note": "입력하지 않음. 셀렉터 확인만 했다.",
            }

        steps = only or ALL_STEPS

        # 단일 필드부터. 계정이 채워주는 값은 건드리지 않는다.
        for key in ("summary", "ai_usage") if "text" in steps else ():
            value = data.get(key)
            if value and found.get(key):
                _set(p, _fields()[key], str(value))
                filled[key] = str(value)[:40]

        # 경력 — 첫 칸만 채운다. 칸을 새로 만들지 않는다.
        exps = data.get("experiences") or []
        if "experience" in steps and exps and found.get("exp_company"):
            e = exps[0]
            for key, src in (
                ("exp_company", "company"),
                ("exp_job_role", "job_role"),
                ("exp_business_title", "business_title"),
            ):
                if e.get(src) and found.get(key):
                    setter = _set_autocomplete if key in AUTOCOMPLETE else _set
                    setter(p, _fields()[key], str(e[src]))
                    filled[key] = str(e[src])[:40]

            n_ach = _fill_achievements(p, e.get("achievements") or [])
            if n_ach:
                filled["achievements"] = f"{n_ach}건"

        # 학력 — 같은 원칙
        edus = data.get("educations") or []
        if "education" in steps and edus and found.get("edu_school"):
            slots = _add_rows(p, _fields()["edu_school"], len(edus))
            for n, ed in enumerate(edus[:slots]):
                for key, src in (
                    ("edu_school", "school"),
                    ("edu_major", "major"),
                    ("edu_detail", "detail"),
                ):
                    if not (ed.get(src) and found.get(key)):
                        continue
                    sel = f"{_fields()[key]} >> nth={n}"
                    setter = _set_autocomplete if key in AUTOCOMPLETE else _set
                    setter(p, sel, str(ed[src]))
                    filled[f"{key}{n}"] = str(ed[src])[:40]

        # 날짜 — 버튼+피커라 입력 필드와 처리가 다르다. 뒤에 몰아서 한다
        # (피커가 열려 있으면 다른 필드 클릭이 가려진다).
        dates: dict[str, bool] = {}
        if "dates" in steps:
            achs = [a for a in ((exps[0].get("achievements") if exps else []) or [])
                    if a.get("title")][:4]
            slots = _date_slots(len(achs), len(edus))
            if exps:
                e0 = exps[0]
                if e0.get("start"):
                    dates["exp_start"] = _set_date(p, slots["exp_start"], e0["start"])
                if e0.get("end"):
                    dates["exp_end"] = _set_date(p, slots["exp_end"], e0["end"])
                for i, a in enumerate(achs):
                    if a.get("start"):
                        dates[f"ach{i}_start"] = _set_date(p, slots[f"ach{i}_start"], a["start"])
                    if a.get("end"):
                        dates[f"ach{i}_end"] = _set_date(p, slots[f"ach{i}_end"], a["end"])
            for j, ed in enumerate(edus):
                if ed.get("start"):
                    dates[f"edu{j}_start"] = _set_date(p, slots[f"edu{j}_start"], ed["start"])
                if ed.get("end"):
                    dates[f"edu{j}_end"] = _set_date(p, slots[f"edu{j}_end"], ed["end"])
                # 날짜 피커는 저장을 트리거하지 않는다. 같은 항목의 텍스트 칸을
                # 건드려야 PATCH가 나간다.
                _flush(p, "edu_detail", ed.get("detail"), nth=j)

        # 필수 셀렉트. 비면 완성도가 안 올라가고 지원 시 반려될 수 있다.
        selects: dict[str, bool] = {}
        if "selects" in steps and exps:
            selects["재직 형태"] = _set_select(p, "재직 형태", "정규직")
        if "selects" in steps and edus:
            selects["졸업 상태"] = _set_select(p, "졸업 상태", "졸업")

        skills, skills_skipped = (
            _fill_skills(p, data.get("skills") or []) if "skills" in steps else ([], [])
        )
        links = _fill_links(p, data.get("links") or []) if "links" in steps else []
        langs = _fill_languages(p, data.get("languages") or []) if "links" in steps else []

        p.wait_for_timeout(3000)  # 자동 저장이 붙을 시간

        # 넣었다고 저장된 게 아니다. 새로고침 후 실제로 남았는지 확인한다.
        p.reload()
        p.wait_for_timeout(5000)
        persisted = {
            k: bool(p.locator(_fields()[k]).first.input_value().strip())
            for k in filled
            if found.get(k) and k in _fields()
        }
        lost = [k for k, ok in persisted.items() if not ok]

        # 화면 수준 점검. 값 대조가 통과해도 섹션이 미완성일 수 있다.
        page_audit = audit(p, data)
        if not page_audit["ok"]:
            log.warning("화면에서 확인되지 않는 항목: %s", page_audit["missing"])

        # 검증 뒤에 '작성 완료'를 누른다. 누르면 편집 화면을 벗어나 입력칸이
        # 사라지므로, 먼저 누르면 위의 대조를 할 수 없다.
        finalized = finalize(p)

        return {
            "url": url, "title": title, "dry_run": False,
            "filled": filled, "missing": missing,
            "persisted": persisted, "lost": lost,
            "dates": dates, "selects": selects, "finalized": finalized,
            "audit": page_audit, "skills": skills, "skills_skipped": skills_skipped,
            "links": links, "languages": langs,
            "ok": not lost,
            "prefilled_skipped": list(PREFILLED),
        }


def list_resumes(*, headless: bool = False) -> list[dict[str, str]]:
    """플랫폼에 저장된 이력서 목록을 읽는다. 읽기만 한다.

    삭제는 이 도구가 하지 않는다 — 계정의 데이터를 지우는 건 되돌릴 수 없고,
    사람이 판단할 일이다. 무엇이 미리보기용이고 무엇이 안 쓰이는지 보여주는
    데까지가 여기 몫이다.

    파싱은 파이썬에서 한다. JS 안에서 개행으로 쪼개려면 이스케이프가 파이썬
    문자열과 JS 문자열을 두 번 거쳐야 해서 조용히 깨진다.
    """
    with browser(headless=headless) as s:
        s.goto(CV_URL)
        page = s.page()
        page.wait_for_timeout(5500)
        body = page.inner_text("body")

    # 목록은 '내 이력서 리스트' 아래에 제목/상태/날짜가 줄 단위로 이어진다.
    # DOM 구조(li·class)는 해시라 잡히지 않아 본문 텍스트를 읽는다.
    idx = body.find("내 이력서 리스트")
    lines = [x.strip() for x in body[idx:].splitlines() if x.strip()] if idx >= 0 else []

    out: list[dict[str, str]] = []
    for i, line in enumerate(lines):
        if not STATUS.fullmatch(line):
            continue
        # 상태 줄 바로 앞에서 제목을 찾는다. 사이에 '기본 이력서' 배지가 낄 수 있다.
        # '기본 이력서'는 배지지 제목이 아니다. 건너뛰고 그 앞을 제목으로 본다.
        title = next(
            (x for x in reversed(lines[max(0, i - 3):i])
             if not STATUS.fullmatch(x) and x not in BADGES),
            "",
        )
        date = lines[i + 1] if i + 1 < len(lines) else ""
        out.append({"title": title, "status": line, "modified": date})
    return out
