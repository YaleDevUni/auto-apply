"""원티드 이력서 편집기를 채운다.

## 왜 파일 업로드가 아니라 편집기인가

원티드 자체 이력서는 매칭·추천에 쓰이고, 업로드 파일에는 없는 구조화 필드
(**AI 활용 경험**)가 있다. 그리고 실측상 파일 업로드는 동의 모달 뒤에서도
파일 선택기가 열리지 않았다 — 합성 클릭으로는 안 되는 핸들러로 보인다.

단, 그 **AI 활용 경험 칸은 사람이 사본메이커에 직접 등록해 둔 값**이다.
사본이 그대로 들고 오므로 자동화는 읽지도 쓰지도 않는다(`MANUAL` 참조).

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

from ..config import effective_config
from .session import PlaywrightSession, browser

log = logging.getLogger(__name__)


class ProtectedResume(RuntimeError):
    """건드리면 안 되는 이력서를 열었다. 채우기 전에 멈춘다.

    2026-08-16 실측: 무인 사이클이 '박예일 기본'을 재사용하며 덮어썼고
    (`preview_resume_url` 재사용 경로), 그 상태로 공고 33에 **실제 제출**까지
    나갔다(apply_ledger id=4). 사본메이커·기본 이력서는 모든 이력서의 원본이라
    한 번 오염되면 그 뒤 만드는 모든 사본이 같이 오염된다.

    그래서 '실수로 열었을 때 조용히 채우는' 경로를 남겨두지 않는다. 예외로
    던져 그 자리에서 멈춘다.
    """


def protected_titles() -> set[str]:
    """편집·삭제 금지 이력서. config의 `resumes.protect`가 근거다."""
    return {
        str(t).strip()
        for t in ((effective_config().get("resumes") or {}).get("protect") or [])
        if str(t).strip()
    }


def _stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")


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


def _date_is_empty(page, nth: int) -> bool:
    """그 날짜 칸이 아직 비었는가. 비면 'YYYY.MM'이라는 자리표시자가 보인다."""
    btns = _date_buttons(page)
    if nth >= btns.count():
        return False
    try:
        return "YYYY" in (btns.nth(nth).inner_text() or "")
    except Exception:  # noqa: BLE001
        return False


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
# 제목이 아닌데 제목 자리에 나타나는 줄. 메모가 비어 있으면 안내문이 그
# 자리에 그려져서, 거르지 않으면 '더보기를 눌러 메모를 입력하세요.'가
# 이력서 제목으로 잡힌다(실제로 잡혔다).
BADGES = {"기본 이력서", "기본", "더보기를 눌러 메모를 입력하세요."}

SKILL_ACTIVATOR = "text=직무 스킬"
SKILL_INPUT = 'input[placeholder*="보유 스킬"]'
# 이미 등록된 스킬 칩. 안내 문구가 사라진 뒤 입력칸을 다시 여는 통로다.
SKILL_CHIP = '[class*="Skill"] button, [class*="skill"] button'


ACH_ADD = 'button:has-text("주요 성과 추가")'
ACH_DETAIL = 'textarea[placeholder^="업무 경험을 성과"]'
ACH_TITLE = 'input[name="title"]'


# 행에 호버하면 오른쪽에 나타나는 휴지통. 클래스가 해시가 아니라 의미 있는
# 접두사라 잡을 수 있다(ResumeItem_ResumeItem__와 같은 방식).
ROW_DELETE = 'button[class*="BtnDelete_BtnDelete__"]'


def _delete_row(page, anchor_sel: str, nth: int) -> bool:
    """anchor_sel의 nth번째 행을 지운다. 되돌릴 수 없다.

    행 자체를 가리키는 셀렉터가 없다. 그 행의 입력칸에 호버하면 오른쪽에
    휴지통이 뜨고, 그것을 **같은 높이에 있는 것**으로 골라야 한다 — 페이지
    전체에서 고르면 다른 섹션의 휴지통을 누른다.
    """
    anchors = page.locator(anchor_sel)
    if nth >= anchors.count():
        return False

    target = anchors.nth(nth)
    target.scroll_into_view_if_needed()
    page.wait_for_timeout(400)
    target.hover()
    page.wait_for_timeout(900)

    box = target.bounding_box()
    if not box:
        return False

    btns = page.locator(ROW_DELETE)
    for k in range(btns.count()):
        b = btns.nth(k)
        try:
            if not b.is_visible():
                continue
            bb = b.bounding_box()
            if not bb or abs(bb["y"] - box["y"]) > 120:
                continue
        except Exception:  # noqa: BLE001
            continue
        b.click(force=True)
        page.wait_for_timeout(1200)
        # 확인 모달이 뜨면 승인한다. 부른 시점에 이미 결정된 일이다.
        for label in ("삭제", "확인"):
            ok = page.locator(f'[role=dialog] button:has-text("{label}")')
            if ok.count():
                ok.first.click(force=True)
                page.wait_for_timeout(1200)
                break
        return True

    log.info("삭제 버튼을 못 찾았다: %s nth=%d", anchor_sel, nth)
    return False


def _prune_rows(page, anchor_sel: str, keep: int, *, label: str = "행") -> int:
    """keep개만 남기고 뒤에서부터 지운다. 반환은 지운 개수.

    뒤에서부터 지우는 이유: 앞에서 지우면 남은 행의 인덱스가 밀려서, 다음 차례에
    엉뚱한 행을 지운다.

    사본에서 시작하면 템플릿이 들고 온 성과가 공고와 안 맞을 수 있다. 덮어쓰지
    않고 남겨두면 지원하는 자리와 무관한 경력이 이력서에 그대로 실린다 —
    맞지 않는 내용을 붙여 내느니 지우는 편이 낫다.
    """
    removed = 0
    for i in range(page.locator(anchor_sel).count() - 1, keep - 1, -1):
        if _delete_row(page, anchor_sel, i):
            removed += 1
            log.info("%s %d번째 삭제", label, i)
        else:
            break
    return removed


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

    # 칸이 내용보다 많으면 남는 칸을 지운다. 사본이 들고 온 성과는 이 공고를
    # 보고 고른 게 아니라, 그대로 두면 안 맞는 경력이 실려 나간다.
    surplus = page.locator(ACH_DETAIL).count() - len(items)
    if surplus > 0:
        _prune_rows(page, ACH_DETAIL, len(items), label="주요 성과")

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


def _chip_labels(page) -> list[str]:
    """스킬 영역에 떠 있는 칩 이름들.

    **삭제 아이콘(svg)을 조건으로 걸면 안 된다.** 그 아이콘은 입력칸이 열려야
    생긴다 — 닫힌 상태에서 읽으면 칩이 하나도 안 잡히고, 그래서 입력칸을 여는
    폴백(기존 칩 클릭)이 통째로 실패했다.

    범위는 스킬 입력칸이 속한 ActiveBox, 없으면 '스킬' 제목 다음의 ActiveBox다.
    페이지 전체로 넓히면 사이드바 버튼('이력서 리뷰')까지 칩으로 잡혀, 지우려고
    20번 헛클릭하고 성공으로 보고했다.
    """
    raw = page.evaluate(
        """(inputSel) => {
            // 범위는 **항상 '스킬' 제목 기준**이다. 입력칸 기준으로 잡으면
            // 입력칸이 열린 뒤 다른 ActiveBox(날짜 칸 등)를 가리켜, 칩 대신
            // 'YYYY.MM'이 잡힌다.
            const head = [...document.querySelectorAll('*')].find(
                e => e.children.length === 0 && (e.innerText || '').trim() === '스킬');
            if (!head) return [];
            const y = head.getBoundingClientRect().top + window.scrollY;
            const scope = [...document.querySelectorAll('div[class*=ActiveBox]')]
                .find(b => b.getBoundingClientRect().top + window.scrollY >= y - 10);
            if (!scope) return [];
            return [...scope.querySelectorAll('button')]
                .filter(b => b.querySelector('span span'))
                .map(b => (b.querySelector('span span').innerText || '').trim());
        }""",
        _sel("skill.input", SKILL_INPUT),
    )
    return [x for x in raw if x and "\n" not in x and len(x) <= 40]


def _prune_skills(page, wanted: list[str], present: list[str] | None = None) -> list[str]:
    """이번 공고에 없는 스킬 칩을 걷어낸다.

    미리보기 이력서를 재사용하므로 추가만 하면 **이전 공고의 스킬이 그대로 남아
    섞인다.** 공고별 맞춤 이력서인데 그러면 맞춤의 의미가 없다 — 비전 점검이
    "JSON에 없는 Python, NestJS가 화면에 있다"로 잡아낸 문제다.

    칩은 버튼이고 그 안의 svg가 삭제 아이콘이다. 입력칸이 열려 있어야 보인다.
    변형 표기(AWS(EC2, S3) → AWS)로 등록됐을 수 있으므로 원본과 변형을 모두
    보존 대상으로 본다.
    """
    keep = {v for sk in wanted for v in _skill_variants(sk)}

    # 지울 목록은 **입력칸을 열기 전에 읽어둔 것**을 쓴다. 입력칸이 열리면
    # 레이아웃이 바뀌어 같은 범위 계산이 날짜 칸('YYYY.MM')을 가리킨다.
    chips = present if present is not None else _chip_labels(page)
    todo = [x for x in chips if x not in keep]
    removed: list[str] = []

    for label in todo:
        target = page.locator(f'button:has(span:text-is("{label}"))').first
        if not target.count():
            continue
        try:
            target.locator("svg").first.click(force=True)
            page.wait_for_timeout(700)
        except Exception as e:  # noqa: BLE001
            log.info("칩 삭제 실패 — 건너뜀: %s (%s)", label, type(e).__name__)
            continue

        # 눌렀다고 지워진 게 아니다. 실제로 사라졌는지 본다.
        if page.locator(f'button:has(span:text-is("{label}"))').count():
            log.info("칩이 그대로 남았다: %s", label)
            continue
        removed.append(label)
    return removed


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
    # 입력칸을 열기 전에 읽어야 한다 — 열면 레이아웃이 바뀌어 범위 계산이 어긋난다.
    existing = _chip_labels(page)

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

    # **기본값 꺼짐.** 삭제는 확실히 되는데 그 뒤 추가가 저장되지 않아, 켜면
    # 스킬이 통째로 비는 상태가 됐다(실측: 11개 → 1개). 누적보다 손실이 나쁘다.
    #
    # 원인 미확인. 삭제 후 추가 경로에서 저장 요청이 안 나가는 것으로 보이며,
    # 날짜 칸에서 겪은 것과 같은 부류일 가능성이 있다 — 같은 섹션의 다른 칸을
    # 건드려야 PATCH가 나가는 구조. 확인 전까지는 켜지 않는다.
    if _sel("skill.prune", "") == "on":
        removed = _prune_skills(page, skills[:limit], present=existing)
        if removed:
            log.info("이전 공고 스킬 %d개 제거: %s", len(removed), ", ".join(removed[:6]))

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
        try:
            # 짧게 건다. 새 이력서에는 링크 행이 없을 수 있는데, 기본 15초를
            # 그대로 쓰면 링크 하나 때문에 전체 조립이 실패한다.
            act.click(force=True, timeout=5000)
        except Exception as e:  # noqa: BLE001
            log.info("링크 행을 열지 못했다 — 링크 없이 진행: %s", type(e).__name__)
            return added
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
            try:
                # 링크는 있으면 좋고 없어도 지원은 나간다. 여기서 기본 15초를
                # 쓰면 링크 하나 때문에 이력서 전체가 실패한다 — 실제로 그랬다.
                box.click(force=True, timeout=5000)
                box.fill(val)
                box.press("Tab")
            except Exception as e:  # noqa: BLE001
                log.info("링크 입력 실패 — 건너뜀: %s (%s)", name, type(e).__name__)
                return added
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

# 사람이 사본메이커에 직접 써 넣은 값. 사본이 그대로 들고 오므로 우리가 다시
# 쓸 이유가 없고, 써서도 안 된다 — 50자 칸이라 LLM이 쓸 때마다 문장이 바뀌고
# 잘렸다. 셀렉터는 남겨둔다(존재 확인·자가수복용). 쓰지 않을 뿐이다.
MANUAL = ("ai_usage",)


def _fields(platform: str = "wanted") -> dict[str, str]:
    """레시피에 정의된 필드맵. 없는 항목은 기본값으로 채운다."""
    return {**FIELDS, **(_editor_cfg(platform).get("fields") or {})}


def _section_filled(page, field: str) -> bool:
    """그 섹션의 첫 칸에 값이 남아 있는가. 사본이 들고 온 것을 지키는지 본다."""
    loc = page.locator(_fields().get(field, ""))
    if not loc.count():
        return False
    try:
        return bool((loc.first.input_value() or "").strip())
    except Exception:  # noqa: BLE001
        return False


def audit(page, data: dict[str, Any], *, steps: tuple[str, ...] | None = None) -> dict[str, Any]:
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

    # 우리가 안 쓴 섹션을 조립 JSON과 대조하면 안 된다. 사본이 들고 온 학력은
    # 사람이 직접 넣은 값이고, LLM이 쓴 표기와 한 글자만 달라도 '누락'이 된다.
    # 대신 **여전히 차 있는지**만 본다 — 우리가 망가뜨렸는지가 진짜 질문이다.
    wrote = set(steps or ALL_STEPS)

    exps = data.get("experiences") or []
    edus = data.get("educations") or []
    if exps:
        has("회사", exps[0].get("company"))
        ach = (exps[0].get("achievements") or [{}])[0]
        has("주요성과", ach.get("title"))

    if "education" in wrote:
        for i, ed in enumerate(edus[:2]):
            has(f"학교{i}", ed.get("school"))
    else:
        checks["학력 보존"] = _section_filled(page, "edu_school")

    has("간단소개", (data.get("summary") or "").splitlines()[0] if data.get("summary") else None)
    if data.get("skills"):
        checks["스킬"] = any(sk[:10] in body for sk in data["skills"][:5])

    if "links" in wrote:
        if data.get("links"):
            has("링크", (data["links"][0] or {}).get("name"))
        if data.get("languages"):
            has("언어", (data["languages"][0] or {}).get("level"))
    else:
        checks["링크 보존"] = "http" in body

    # 플랫폼이 표시하는 완성도와 미입력 안내도 같이 담는다. "무엇이 남았나"를
    # 플랫폼이 직접 말해주므로, 우리 판단보다 이쪽이 근거로 강하다.
    text = page.inner_text("body")
    pct = re.search(r"(\d{1,3})\s*%", text)
    todo = [ln.strip() for ln in text.splitlines() if "입력해주세요" in ln][:4]

    missing = [k for k, ok in checks.items() if not ok]
    return {
        "checks": checks,
        "missing": missing,
        "ok": not missing,
        "completeness": int(pct.group(1)) if pct else None,
        "platform_todo": todo,
    }


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


# 편집기 상단에 제목과 나란히 오는 고정 문구들. 제목이 아니다.
#
# '포지션 맞춤 이력서 리뷰'는 조각('포지션'/'맞춤 리뷰'/'이력서 리뷰')으로도,
# 이어붙은 한 줄로도 나온다. 왼쪽 리뷰 패널이 열려 있으면 사이드바 탭이
# 조각으로, 패널 제목이 한 줄로 — 같은 화면에 두 꼴이 동시에 나온다.
TITLE_CHROME = ("이전 페이지", "기본 이력서 설정", "기본 이력서", "한국어", "영어",
                "작성 완료", "작성 중", "포지션", "맞춤 리뷰", "이력서 리뷰",
                "포지션 맞춤 이력서 리뷰")

# 제목이 사는 곳. 클래스는 해시가 붙지만 앞머리는 안 바뀐다.
EDITOR_HEADER = 'header[class*="ResumeHeader_"]'


def read_title(page) -> str:
    """편집기 상단의 이력서 제목을 읽는다.

    지원 레시피가 이 제목으로 이력서를 고르므로(`li:has(span:text-is(...))`),
    조립 → 등록 → 지원을 잇는 고리가 여기다. 못 읽으면 어느 이력서를 낼지
    정할 수 없으므로 빈 문자열을 돌려주고 호출부가 멈춘다.

    앵커 하나('기본 이력서 설정')에 기대면 안 된다 — 그 이력서가 기본으로
    지정되면 문구가 '기본 이력서'로 바뀌어 앵커가 사라진다. 고정 문구를
    걷어내고 남는 첫 줄을 제목으로 본다.

    ## body가 아니라 header에서 읽는다 (2026-08-17 실측)

    예전에는 `body` 앞 12줄을 봤다. 그 12줄 안에 **편집기 바깥 것**이 섞여
    들어온다: 왼쪽 'AI 이력서 리뷰' 패널이 열려 있으면 그 패널 제목
    '포지션 맞춤 이력서 리뷰'가 8번째 줄에 들어앉는다. 그게 그대로 이력서
    제목으로 기록됐고, 지원 단계에서 그 이름의 이력서를 목록에서 찾다가
    `RecipeError: 요소를 찾지 못함`으로 죽었다 — 이력서는 멀쩡히 등록돼
    있는데도 그랬다. 헤더 안에서 읽으면 패널이 뜨든 말든 상관이 없다.

    ## 길이로 자르지 않는다

    같은 실측에서 진짜 제목('노타(Nota) [인턴] [NetsPresso] AI Software
    Enginee #16', 52자)이 옛 `len(ln) > 40` 조건에 걸려 통째로 버려졌다.
    제목은 회사명과 공고 제목을 이어 붙여 만들므로 40자는 늘 넘는다. 길이는
    제목인지 아닌지를 가르는 근거가 아니다.
    """
    hdr = page.locator(EDITOR_HEADER).first
    if hdr.count():
        for ln in (hdr.inner_text() or "").splitlines():
            ln = ln.strip()
            if ln and ln not in TITLE_CHROME:
                return ln

    # 헤더를 못 찾는 경우(레이아웃 개편 등)의 그물. body를 보되 같은 규칙을
    # 쓴다 — 길이로 자르지 않고 고정 문구만 걷어낸다.
    log.info("편집기 헤더를 못 찾았다 — body 앞부분에서 제목을 찾는다")
    for ln in page.inner_text("body").splitlines()[:12]:
        ln = ln.strip()
        if ln and ln not in TITLE_CHROME:
            return ln
    return ""


def open_editor(
    s: PlaywrightSession,
    *,
    resume_url: str | None = None,
    template: str | None = None,
    new_title: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """편집기를 연다. (url, 메타) 를 돌려준다.

    경로가 셋이다. 우선순위대로:

        resume_url  그 이력서를 연다 (재사용·디버깅)
        template    사본을 만들어 이름을 붙이고 연다  ← 기본 경로
        (없음)      '새 이력서 작성'으로 빈 이력서를 만든다  ← 폴백

    빈 이력서 경로를 지우지 않고 남겨둔 이유: 사본메이커가 계정에서 사라지면
    (사람이 지우거나 이름이 바뀌면) 사본 경로가 통째로 막힌다. 그때 파이프라인이
    멈추는 것보다 느리더라도 도는 편이 낫다.
    """
    if resume_url:
        s.goto(resume_url)
        s.page().wait_for_timeout(4000)
        return s.url(), {"source": "reuse"}

    if template:
        s.goto(CV_URL)
        page = s.page()
        page.wait_for_timeout(4000)
        made = prepare_from_template(
            page, template=template, new_title=new_title or template,
        )
        if made.get("ok"):
            return made["url"], {"source": "copy", **made}
        log.warning("사본 경로 실패 — 빈 이력서로 넘어간다: %s", made.get("reason"))

    s.goto(CV_URL)
    s.page().wait_for_timeout(3500)
    s.click('button:has-text("새 이력서 작성") >> nth=0')
    s.page().wait_for_timeout(6000)
    return s.url(), {"source": "blank"}


ALL_STEPS = ("text", "experience", "education", "dates", "selects", "skills", "links")

# 사본에서 시작할 때 손대는 단계. 학력·링크·언어·연락처는 사본이 이미 갖고 온다.
#
# 이게 왜 중요한가: 새 이력서를 처음부터 채우면 완성도가 71%에서 멈췄다. 원인은
# 학력(입력은 되는데 저장이 안 됨)과 링크(입력칸 TimeoutError) 두 곳이었고,
# 둘 다 **공고와 무관하게 매번 똑같은 값**이다. 매번 다시 채울 이유가 없었다.
# 사본은 그 두 섹션을 이미 완성된 상태로 들고 오므로 실패할 일 자체가 없어진다.
#
# 공고마다 달라지는 것만 남는다: 간단 소개, 경력 성과, 스킬.
COPY_STEPS = ("text", "experience", "dates", "selects", "skills")


def fill(
    data: dict[str, Any],
    *,
    resume_url: str | None = None,
    template: str | None = None,
    new_title: str | None = None,
    job_id: int | str = "x",
    dry_run: bool = True,
    headless: bool = False,
    only: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """조립된 JSON을 편집기에 채운다.

    dry_run=True(기본)면 입력하지 않고 필드 존재만 확인한다. 편집기는 자동
    저장이라 입력 자체가 되돌리기 어렵다 — 셀렉터가 맞는지 먼저 확인한다.
    """
    with browser(headless=headless, kind="이력서 작성",
                 label=f"공고 {job_id}") as s:
        url, origin = open_editor(
            s, resume_url=resume_url, template=template, new_title=new_title,
        )
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

        # 원본을 열었으면 여기서 멈춘다. 아래 한 줄만 지나가면 자동저장이라
        # 되돌릴 수 없다 — 사본메이커가 오염되면 이후 모든 이력서가 오염된다.
        if title in protected_titles():
            raise ProtectedResume(
                f"보호 이력서를 열었다: {title!r} ({url}) — 채우지 않고 멈춘다. "
                "사본 경로(template)로 열어야 한다."
            )

        if dry_run:
            return {
                "url": url, "title": title, "dry_run": True, "origin": origin,
                "found": found, "missing": missing,
                "note": "입력하지 않음. 셀렉터 확인만 했다.",
            }

        # 사본에서 왔으면 학력·링크·언어는 이미 완성돼 있다. 손대면 되레
        # 저장이 안 되거나 타임아웃으로 실패하던 자리다 — 지나간다.
        steps = only or (COPY_STEPS if origin["source"] == "copy" else ALL_STEPS)

        # 단일 필드부터. 계정이 채워주는 값(PREFILLED)과 사람이 사본메이커에
        # 직접 넣은 값(MANUAL, 'AI 활용 경험')은 건드리지 않는다.
        for key in ("summary",) if "text" in steps else ():
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
        # 재직기간은 사본이 들고 온 그대로 둔다 — 회사에 다닌 기간은 공고에
        # 따라 달라지지 않는다. 날짜 피커는 실패 확률이 높은 조작이라, 바뀔 일
        # 없는 값을 다시 넣는 건 위험만 늘린다.
        #
        # 성과 기간은 다르다. 성과는 공고마다 골라 다시 쓰는 항목이라 날짜도
        # 그때그때 달라진다. 여기는 매번 설정한다.
        keep_dates = origin["source"] == "copy"

        def put_tenure(slot: int, value: str) -> bool:
            """재직기간 전용. 이미 값이 있으면 손대지 않는다."""
            if keep_dates and not _date_is_empty(p, slot):
                return True
            return _set_date(p, slot, value)

        dates: dict[str, bool] = {}
        if "dates" in steps:
            achs = [a for a in ((exps[0].get("achievements") if exps else []) or [])
                    if a.get("title")][:4]
            slots = _date_slots(len(achs), len(edus))
            if exps:
                e0 = exps[0]
                if e0.get("start"):
                    dates["exp_start"] = put_tenure(slots["exp_start"], e0["start"])
                if e0.get("end"):
                    dates["exp_end"] = put_tenure(slots["exp_end"], e0["end"])
                for i, a in enumerate(achs):
                    if a.get("start"):
                        dates[f"ach{i}_start"] = _set_date(p, slots[f"ach{i}_start"], a["start"])
                    if a.get("end"):
                        dates[f"ach{i}_end"] = _set_date(p, slots[f"ach{i}_end"], a["end"])
            # 학력 날짜는 학력 단계를 켰을 때만 만진다. 사본에는 이미 들어
            # 있고, 재학기간은 공고에 따라 달라지는 값이 아니다. 그런데도
            # 손대면 저장이 안 되는 자리라 완성도만 도로 깎였다.
            for j, ed in enumerate(edus if "education" in steps else []):
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
        # 졸업 상태도 학력이다. 사본이 이미 갖고 있다.
        if "selects" in steps and "education" in steps and edus:
            selects["졸업 상태"] = _set_select(p, "졸업 상태", "졸업")

        skills, skills_skipped = (
            _fill_skills(p, data.get("skills") or []) if "skills" in steps else ([], [])
        )
        # 링크·어학은 **빈 이력서 폴백에서만** 돈다. 사본에는 이미 들어 있고,
        # 링크 입력칸은 TimeoutError로 못 잡던 자리다(완성도 71%의 절반이 이것).
        # 폴백을 지우지 않는 이유는 사본메이커가 계정에서 사라질 수 있어서다.
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

        # 스킬은 `_fields()`에 없는 칩 UI라 위 대조에서 빠진다 — 그래서 prune
        # 켰을 때의 "11개 → 1개" 손실(NEXT.md)이 실측 전까지 안 보였다. prune은
        # 계속 꺼둔 채로, 여기서만 새로고침 후 칩을 다시 읽어 관찰한다.
        # `ok`에는 안 넣는다 — 손실의 원인(저장 타이밍 vs 다른 문제)이 아직
        # 미확인이라, 지금 넣으면 원인 모를 실패로 하위 워크플로를 막을 수 있다.
        skills_persisted: list[str] = []
        skills_lost: list[str] = []
        if "skills" in steps and skills:
            after_reload_chips = set(_chip_labels(p))
            skills_persisted = [sk for sk in skills if sk in after_reload_chips]
            skills_lost = [sk for sk in skills if sk not in after_reload_chips]
            if skills_lost:
                log.warning(
                    "스킬 %d/%d개가 새로고침 후 사라짐 — 추가는 됐는데 저장은 안 된 "
                    "것으로 보임: %s",
                    len(skills_lost), len(skills), ", ".join(skills_lost[:10]),
                )

        # 화면을 남긴다. 나중에 "왜 이렇게 됐나"를 물을 때 그때의 화면이
        # 없으면 추측밖에 못 한다 — 자가개선 에이전트가 셀렉터를 추측으로
        # 고친 적이 있고, 그 고침은 틀렸다.
        shot = ""
        try:
            from ..paths import EVIDENCE_DIR

            EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
            shot = str(EVIDENCE_DIR / f"editor-{job_id}-{_stamp()}.png")
            p.screenshot(path=shot, full_page=True)
        except Exception as e:  # noqa: BLE001
            log.info("편집기 화면 저장 실패(무시): %s", e)
            shot = ""

        # 화면 수준 점검. 값 대조가 통과해도 섹션이 미완성일 수 있다.
        page_audit = audit(p, data, steps=tuple(steps))
        if not page_audit["ok"]:
            log.warning("화면에서 확인되지 않는 항목: %s", page_audit["missing"])

        # 검증 뒤에 '작성 완료'를 누른다. 누르면 편집 화면을 벗어나 입력칸이
        # 사라지므로, 먼저 누르면 위의 대조를 할 수 없다.
        finalized = finalize(p)

        return {
            "url": url, "title": title, "dry_run": False, "origin": origin,
            "steps": list(steps),
            "filled": filled, "missing": missing,
            "persisted": persisted, "lost": lost,
            "dates": dates, "selects": selects, "finalized": finalized,
            "audit": page_audit, "shot": shot,
            "skills": skills, "skills_skipped": skills_skipped,
            "skills_persisted": skills_persisted, "skills_lost": skills_lost,
            "links": links, "languages": langs,
            "ok": not lost,
            "prefilled_skipped": list(PREFILLED),
        }


def pick_template(job: dict[str, Any]) -> str:
    """공고에 맞는 사본메이커를 고른다.

    사본마다 용도가 다르다(개발자용·데브옵스·AX·영업). 트랙만으로는 갈리지
    않는 결이 있어서 — '개발'이라는 한 트랙 안에 인프라 공고와 LLM 공고가 같이
    들어온다 — 키워드를 트랙보다 먼저 본다. 키워드가 안 걸리면 트랙, 그것도
    없으면 기본값이다.
    """
    cfg = (effective_config().get("resumes") or {}).get("copy_from") or {}
    text = " ".join(
        str(job.get(k) or "") for k in ("title", "company", "description", "position")
    ).lower()

    for template, words in (cfg.get("by_keyword") or {}).items():
        if any(str(w).lower() in text for w in words or []):
            return template

    by_track = cfg.get("by_track") or {}
    return by_track.get(job.get("track") or "") or cfg.get("default") or ""


TITLE_MAX = 50  # 원티드 제목 입력칸의 maxlength


def unique_title(base: str) -> str:
    """겹치지 않는 이력서 제목. 뒤에 카운터를 붙인다.

    같은 이름을 허용하면 지원 폼에서 이력서를 고를 수 없다 — 셀렉터가
    `li:has(span:text-is("{제목}"))`이라 둘 중 무엇을 집는지 알 수 없고,
    그러면 무엇을 제출했는지도 모른다.

    번호는 목록을 세어 정하지 않고 DB 카운터에서 받는다. 화면 파싱이 맞다는
    데 기대지 않으려는 것이다 — 실제로 메모 안내문을 제목으로 읽은 적이 있다.
    지운 번호도 재사용하지 않으므로 기록과 어긋나지 않는다.

    번호 자리는 50자 제한 안에서 확보한다. 잘라내는 쪽은 본문이다 — 번호를
    잘라내면 애초에 붙이는 의미가 없다.
    """
    from ..db import connect, next_seq

    conn = connect()
    try:
        seq = next_seq(conn, "resume_seq")
    finally:
        conn.close()

    suffix = f" #{seq}"
    return base[: TITLE_MAX - len(suffix)].rstrip() + suffix


def prepare_from_template(
    page, *, template: str, new_title: str
) -> dict[str, Any]:
    """사본을 만들고 이름을 붙인 뒤 편집기를 연다.

    반환한 url이 그 이력서의 유일한 손잡이다. 제목은 사람이 읽으라고 붙이는
    것이고, 같은 이름이 두 개 생겨도 url은 겹치지 않는다.
    """
    wanted = unique_title(new_title)

    copy_title = duplicate_resume(page, template)
    if not copy_title:
        return {"ok": False, "reason": f"사본 실패: {template}"}

    renamed = rename_resume(page, copy_title, wanted)
    title = wanted if renamed else copy_title

    row = page.locator(f'{ROW}:has(span:text-is("{title}"))').first
    if not row.count():
        return {"ok": False, "reason": f"사본을 목록에서 못 찾았다: {title}"}
    row.click()
    page.wait_for_timeout(4500)

    _remember_made(title, page.url, template)
    return {"ok": True, "title": title, "url": page.url,
            "template": template, "renamed": renamed}


def _remember_made(title: str, url: str, template: str) -> None:
    """우리가 만든 이력서를 남긴다. 정리할 때 '우리 것'의 근거가 된다.

    실패해도 흐름을 멈추지 않는다 — 기록이 없으면 그 이력서가 안 지워질 뿐이고,
    안 지워지는 것은 잘못 지우는 것보다 훨씬 가볍다.
    """
    from ..db import connect, now

    try:
        conn = connect()
        try:
            with conn:
                conn.execute(
                    """INSERT OR REPLACE INTO made_resumes
                       (title, url, job_id, template, created_at) VALUES (?,?,?,?,?)""",
                    (title, url, None, template, now()),
                )
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log.info("이력서 생성 기록 실패(무시): %s", e)


def _open_row_menu(page, title: str):
    """목록 행의 ⋯ 메뉴를 연다. 못 열면 None."""
    row = page.locator(f'{ROW}:has(span:text-is("{title}"))').first
    if not row.count():
        log.info("목록에 없다: %s", title)
        return None
    row.scroll_into_view_if_needed()
    page.wait_for_timeout(400)
    row.hover()
    page.wait_for_timeout(700)
    btn = row.locator("button")
    if not btn.count():
        log.info("⋯ 버튼이 없다(기본 이력서일 수 있다): %s", title)
        return None
    btn.first.click(force=True)
    page.wait_for_timeout(1200)
    return row


def duplicate_resume(page, source_title: str) -> str | None:
    """사본을 만든다. 새로 생긴 이력서의 제목을 돌려준다.

    원티드는 사본을 '{원본} 사본'으로 만들고 **바로 '작성 완료' 상태**로 둔다.
    빈 이력서를 만들어 전부 채우는 것과 결정적으로 다른 점이다 — 완성에 필요한
    필수 섹션이 이미 다 차 있으므로, 우리가 실패할 수 있는 표면이 줄어든다.

    제목으로 새 사본을 찾는다. id를 돌려주지 않기 때문인데, 목록 스냅샷을
    앞뒤로 비교하면 무엇이 새로 생겼는지는 확실히 안다.
    """
    before = {r["title"] for r in list_resumes_on(page)}
    if not _open_row_menu(page, source_title):
        return None

    item = page.locator("text=사본 만들기").first
    if not item.count():
        log.warning("'사본 만들기' 메뉴가 없다: %s", source_title)
        page.keyboard.press("Escape")
        return None
    item.click(force=True)
    page.wait_for_timeout(3000)

    page.goto(CV_URL)
    page.wait_for_timeout(4500)
    new = [r["title"] for r in list_resumes_on(page) if r["title"] not in before]
    if not new:
        log.warning("사본이 생기지 않았다: %s", source_title)
        return None
    log.info("사본 생성: %s → %s", source_title, new[0])
    return new[0]


def rename_resume(page, old_title: str, new_title: str) -> bool:
    """이력서 제목을 바꾼다.

    제목이 곧 지원 폼에서 이력서를 고르는 열쇠다(`li:has(span:text-is(...))`).
    사본은 전부 '{원본} 사본'이라 그대로 두면 같은 이름이 여럿 생기고, 그러면
    무엇을 제출하는지 알 수 없게 된다. 만들자마자 공고별 이름으로 바꾼다.
    """
    if not _open_row_menu(page, old_title):
        return False

    item = page.locator("text=이력서 제목 변경").first
    if not item.count():
        log.warning("'이력서 제목 변경' 메뉴가 없다: %s", old_title)
        page.keyboard.press("Escape")
        return False
    item.click(force=True)
    page.wait_for_timeout(1500)

    # 입력칸이 input이 아닐 수 있다. 무엇으로 그렸든 값을 넣을 수 있는 것을 찾는다.
    box = page.locator(
        "[role=dialog] input, [role=dialog] textarea, [role=dialog] [contenteditable='true']"
    ).first
    if not box.count():
        log.warning("제목 입력칸을 못 찾았다")
        page.keyboard.press("Escape")
        return False

    box.click()
    page.keyboard.press("Meta+A")
    page.keyboard.press("Backspace")
    box.type(new_title[:50], delay=20)
    page.wait_for_timeout(400)

    save = page.locator('[role=dialog] button:has-text("저장")').first
    if not save.count():
        page.keyboard.press("Escape")
        return False
    save.click(force=True)
    page.wait_for_timeout(2500)

    page.goto(CV_URL)
    page.wait_for_timeout(4000)
    ok = any(r["title"] == new_title for r in list_resumes_on(page))
    if not ok:
        log.warning("제목이 바뀌지 않았다: %s → %s", old_title, new_title)
    return ok


def list_resumes(*, headless: bool = False) -> list[dict[str, str]]:
    """플랫폼에 저장된 이력서 목록을 읽는다. 읽기만 한다.

    삭제는 이 도구가 하지 않는다 — 계정의 데이터를 지우는 건 되돌릴 수 없고,
    사람이 판단할 일이다. 무엇이 미리보기용이고 무엇이 안 쓰이는지 보여주는
    데까지가 여기 몫이다.

    파싱은 파이썬에서 한다. JS 안에서 개행으로 쪼개려면 이스케이프가 파이썬
    문자열과 JS 문자열을 두 번 거쳐야 해서 조용히 깨진다.
    """
    with browser(headless=headless, kind="이력서 목록 조회") as s:
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


# 목록의 이력서 행. 클래스가 해시가 아니라 의미 있는 접두사라 잡을 수 있다.
ROW = 'div[class*="ResumeItem_ResumeItem__"]'
ROW_MENU_ITEM = "이력서 삭제"


def delete_resume(page, title: str) -> bool:
    """목록에서 이력서 하나를 지운다. 되돌릴 수 없다.

    행에 호버하면 ⋯ 버튼이 나오고, 누르면 '미리보기 / AI 이력서 리뷰 /
    메모 설정 / 이력서 삭제' 메뉴가 열린다. 기본 이력서에는 삭제가 없다.

    호출부가 **무엇을 지울지 먼저 판단해야 한다.** 이 함수는 판단하지 않는다 —
    제출에 쓰인 이력서를 지우면 그 기록이 사라진다.
    """
    # 같은 제목이 여럿일 수 있다. '하나 줄었나'로 판정해야 한다 — '제목이
    # 사라졌나'로 보면 쌍둥이가 남아 있을 때 성공을 실패로 읽는다(실제로 읽었다).
    before = sum(1 for r in list_resumes_on(page) if r["title"] == title)
    if not before:
        log.info("목록에 없다: %s", title)
        return False

    if not _open_row_menu(page, title):
        return False

    item = page.locator(f'text={ROW_MENU_ITEM}').first
    if not item.count():
        log.info("삭제 메뉴가 없다: %s", title)
        page.keyboard.press("Escape")
        return False
    item.click(force=True)
    page.wait_for_timeout(1500)

    # 확인 모달이 뜨면 승인한다. 이 함수를 부른 시점에 이미 결정된 일이다.
    for label in ("삭제", "확인"):
        ok = page.locator(f'[role=dialog] button:has-text("{label}")')
        if ok.count():
            ok.first.click(force=True)
            page.wait_for_timeout(1500)
            break

    page.reload()
    page.wait_for_timeout(3500)
    after = sum(1 for r in list_resumes_on(page) if r["title"] == title)
    gone = after < before
    if not gone:
        log.warning("삭제되지 않았다: %s (%d건 그대로)", title, after)
    return gone


def _our_titles() -> set[str]:
    """파이프라인이 만든 이력서 제목. 이 밖의 것은 정리 대상이 아니다."""
    from ..db import connect

    conn = connect()
    try:
        titles = {r["title"] for r in conn.execute("SELECT title FROM made_resumes")}
        # 이 표가 생기기 전에 만든 것도 우리 것이다. resume_builds에 남은
        # 최신 기록을 같이 본다.
        titles |= {
            r["resume_title"]
            for r in conn.execute(
                "SELECT resume_title FROM resume_builds WHERE resume_title IS NOT NULL"
            )
            if r["resume_title"]
        }
        return {t for t in titles if t}
    finally:
        conn.close()


def cleanup(*, dry_run: bool = True, headless: bool = False) -> dict[str, Any]:
    """오래되거나 넘치는 이력서를 플랫폼에서 지운다.

    **웹에서만 지우고 로컬 사본은 남긴다.** `profile/generated/`의 JSON·MD가
    "무엇을 보냈는지"의 기록이므로, 플랫폼 목록은 작업 공간으로만 본다.

    **우리가 만든 것만 지운다.** 이게 첫 번째 규칙이다.

    처음엔 '오래되었거나 개수를 넘은 것'으로만 골랐는데, 그 기준은 *누가
    만들었는지*를 묻지 않는다. 그래서 사람이 직접 만든 이력서를 지웠다
    ([포지션 리뷰] 코르카 Software Engineer, 2026-08-16). 지원 기록에도 없고
    로컬 사본도 없어서 되살릴 수 없었다.

    나이와 개수는 **무엇을 지울지 정하는 기준이 아니라, 우리 것 중에서
    무엇부터 지울지 정하는 기준**이다. 순서가 뒤바뀌면 남의 물건을 버린다.

    지우지 않는 것:
      - `resume_builds`에 없는 것 — 우리가 만들지 않았다. 무엇인지 모른다
      - 기본 이력서 (삭제 버튼 자체가 없다)
      - config의 `resumes.protect`에 적힌 제목
      - 제출에 쓰였는데 **로컬 사본이 없는** 것 — 그건 유일한 기록이다

    dry_run이 기본값이다. 되돌릴 수 없는 동작이라 무엇이 지워질지 먼저 보여준다.
    """
    from datetime import date

    from ..assemble import submitted_titles
    from ..config import effective_config
    from ..paths import RESUME_OUT_DIR

    cfg = effective_config().get("resumes", {})
    keep_n = int(cfg.get("max_keep", 12))
    max_age = int(cfg.get("max_age_days", 14))
    protect = set(cfg.get("protect") or [])
    submitted = submitted_titles()
    local = {p.stem.split("-", 1)[-1] for p in RESUME_OUT_DIR.glob("*.json")}
    ours = _our_titles()

    with browser(headless=headless, kind="이력서 정리") as s:
        s.goto(_sel("url", CV_URL))
        page = s.page()
        page.wait_for_timeout(5500)
        items = list_resumes_on(page)

        candidates = []
        skipped_not_ours = []
        for i, it in enumerate(items):
            title = it["title"]
            if title in protect or it.get("is_default"):
                continue
            # 우리가 만든 기록이 없으면 손대지 않는다. 사람이 만든 것일 수도,
            # 플랫폼이 만든 것일 수도 있고([포지션 리뷰] 같은), 어느 쪽이든
            # 우리가 판단할 근거가 없다.
            if title not in ours:
                skipped_not_ours.append(title)
                continue
            # 제출에 쓰인 것은 로컬 사본이 있을 때만 지운다
            if title in submitted and not local:
                continue
            too_old = _age_days(it.get("modified", ""), date.today()) > max_age
            over_cap = i >= keep_n
            if too_old or over_cap:
                candidates.append({
                    "title": title,
                    "reason": "오래됨" if too_old else "개수 초과",
                    "submitted": title in submitted,
                })

        if dry_run:
            return {"dry_run": True, "total": len(items),
                    "would_delete": candidates, "우리 것 아님(건너뜀)": skipped_not_ours,
                    "기록만 남은 것": _stale_records({it["title"] for it in items})}

        deleted = [c["title"] for c in candidates if delete_resume(page, c["title"])]
        # 목록을 다시 뜨는 것과 같은 효과: 방금 지운 것까지 빼고 대조한다.
        on_platform = {it["title"] for it in items} - set(deleted)
        return {"dry_run": False, "total": len(items), "deleted": deleted,
                "failed": [c["title"] for c in candidates if c["title"] not in deleted],
                "우리 것 아님(건너뜀)": skipped_not_ours,
                "기록 정리": _prune_records(on_platform)}


def _stale_records(on_platform: set[str]) -> list[str]:
    """플랫폼에 없는데 `made_resumes`에 남아 있는 제목."""
    from ..db import connect

    conn = connect()
    try:
        titles = {r["title"] for r in conn.execute("SELECT title FROM made_resumes")}
    finally:
        conn.close()
    return sorted(titles - on_platform)


def _prune_records(on_platform: set[str]) -> dict[str, Any]:
    """사라진 이력서의 기록을 지운다.

    사람이 플랫폼에서 직접 지우는 일이 있다(개발 중 만든 시험 이력서 정리).
    그러면 `made_resumes`에는 있는데 계정에는 없는 제목이 남고, 다음 사람이
    그 목록을 보고 "아직 남아 있나?" 하고 헷갈린다. 근거로 쓸 수 없는 기록은
    지우는 게 낫다 — 그 이력서는 이미 없으므로 소유 근거가 필요 없다.

    `resume_builds`는 건드리지 않는다. 거기는 "무엇을 만들어 무엇을 냈는지"의
    이력이고, 이력서가 사라졌다고 그 사실이 사라지는 게 아니다.
    """
    from ..db import connect

    stale = _stale_records(on_platform)
    if not stale:
        return {"지운 기록": []}
    conn = connect()
    try:
        with conn:
            conn.executemany("DELETE FROM made_resumes WHERE title=?", [(t,) for t in stale])
    finally:
        conn.close()
    log.info("플랫폼에 없는 이력서 기록 %d건 정리", len(stale))
    return {"지운 기록": stale}


def delete_after_submit(title: str, *, headless: bool = False) -> bool:
    """제출 직후 그 이력서를 지운다. 플랫폼 지원이력에서 여전히 접근 가능하므로
    목록에 남겨둘 이유가 없다.

    `cleanup()`처럼 나이·개수로 고르지 않는다 — 방금 제출한 그 제목 하나만
    지운다. 호출부(`cli.py`)가 로컬 사본이 있는지 먼저 확인하고 부른다;
    여기서는 판단하지 않는다(`delete_resume`와 같은 원칙).
    """
    with browser(headless=headless, kind="제출 이력서 삭제", label=title) as s:
        s.goto(_sel("url", CV_URL))
        page = s.page()
        page.wait_for_timeout(5500)
        return delete_resume(page, title)


def list_resumes_on(page) -> list[dict[str, str]]:
    """열려 있는 목록 페이지에서 이력서를 읽는다(브라우저를 새로 띄우지 않는다)."""
    body = page.inner_text("body")
    idx = body.find("내 이력서 리스트")
    lines = [x.strip() for x in body[idx:].splitlines() if x.strip()] if idx >= 0 else []
    out: list[dict[str, str]] = []
    for i, line in enumerate(lines):
        if not STATUS.fullmatch(line):
            continue
        window = lines[max(0, i - 3):i]
        title = next((x for x in reversed(window) if not STATUS.fullmatch(x) and x not in BADGES), "")
        out.append({
            "title": title,
            "status": line,
            "modified": lines[i + 1] if i + 1 < len(lines) else "",
            "is_default": any(x in BADGES for x in window),
        })
    return out


def _age_days(stamp: str, today) -> int:
    """'26.08.16' 형식의 수정일을 오늘과의 차이(일)로 바꾼다. 못 읽으면 0."""
    try:
        y, m, d = (int(x) for x in stamp.split("."))
        from datetime import date as _d

        return (today - _d(2000 + y, m, d)).days
    except Exception:  # noqa: BLE001
        return 0


def fill_report(result: dict[str, Any]) -> dict[str, Any]:
    """채우기 결과를 조회 가능한 형태로 압축한다.

    실패가 로그 문자열로만 남으면 사후에 못 쓴다 — "왜 71%에서 멈췄나"를
    답하려면 어느 섹션이 어떻게 실패했는지가 데이터로 있어야 한다.

    구분이 중요하다:
      filled   넣었다고 보고된 것
      lost     넣었는데 새로고침 후 사라진 것  ← 저장이 안 된 것
      missing  칸 자체가 없던 것
    이 셋을 뭉개면 "입력 실패"와 "저장 실패"를 구분할 수 없고, 둘은 원인이 다르다.
    """
    dates = result.get("dates") or {}
    return {
        "sections": {
            "text": [k for k in result.get("filled", {}) if k == "summary"],
            "experience": [k for k in result.get("filled", {}) if k.startswith("exp")],
            "education": [k for k in result.get("filled", {}) if k.startswith("edu")],
            "achievements": result.get("filled", {}).get("achievements"),
            "skills": len(result.get("skills") or []),
            "links": len(result.get("links") or []),
            "languages": len(result.get("languages") or []),
        },
        "dates_ok": sum(1 for v in dates.values() if v),
        "dates_total": len(dates),
        "lost": result.get("lost") or [],
        "missing": result.get("missing") or [],
        "skills_skipped": result.get("skills_skipped") or [],
        "audit_missing": (result.get("audit") or {}).get("missing") or [],
        "platform_todo": (result.get("audit") or {}).get("platform_todo") or [],
        "finalized": result.get("finalized"),
        "completeness": (result.get("audit") or {}).get("completeness"),
        "shot": result.get("shot") or "",
    }
