"""세션이 살아있는지 실제로 확인한다.

쿠키 파일이 있는지 보는 것으로는 알 수 없다 — 만료된 쿠키도 파일에는 남아 있다.
유일하게 확실한 방법은 **로그인해야만 보이는 페이지로 실제로 이동해보고**
로그인 페이지로 튕기는지 확인하는 것이다.

이 모듈이 있기 전까지 `LOGIN_REQUIRED`는 `scrape --session wanted=0`으로 사람이
직접 알려줘야만 켜졌다. 즉 세션이 죽어도 시스템은 모르고, 다음 지원 시도가
실패할 때까지 조용히 잘못된 판정을 냈다. 무인 운영에서 그건 치명적이다 —
아무도 안 보고 있으니까.

브라우저를 한 번 띄우므로 공짜가 아니다(플랫폼당 10초쯤). 그래서 매 판정마다가
아니라 수집 시작 전에 한 번만 부른다.

## headless로 돌리면 안 된다

실측: 원티드는 headless 브라우저에 **403(CloudFront 차단)**을 준다. 그런데 URL은
요청한 그대로 남아서, URL만 보는 판정은 403 페이지를 보고 '세션 살아있음'이라고
답한다 — 탐침이 조용히 거짓말을 한다. 그래서 기본값이 headless=False다.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..paths import RECIPE_DIR
from .apply import load_recipe
from .session import browser

log = logging.getLogger(__name__)


def check_session(platform: str, *, headless: bool = False) -> bool | None:
    """반환: True(살아있음) / False(죽음) / None(확인 불가 — 판단하지 않는다).

    None을 별도로 두는 이유: 레시피에 탐침 URL이 없거나 브라우저가 안 뜬 것을
    '세션 죽음'으로 처리하면, 멀쩡한 세션에 대해 사람을 불러대게 된다.
    모르는 것과 죽은 것은 다르다.
    """
    try:
        recipe = load_recipe(platform)
    except Exception as e:  # noqa: BLE001
        log.info("%s 레시피 없음 — 세션 확인 건너뜀 (%s)", platform, e)
        return None

    url = recipe.get("session_probe_url")
    alive_sel = recipe.get("session_alive_selector")
    dead = recipe.get("login_dead_pattern", "")
    if not url or not (alive_sel or dead):
        log.info("%s 레시피에 탐침 설정 없음 — 세션 확인 건너뜀", platform)
        return None

    try:
        with browser(headless=headless, kind="세션 점검", label=platform) as s:
            s.goto(url)
            s.page().wait_for_timeout(3000)

            # 1) 로그인 페이지로 튕겼으면 확실히 죽었다.
            if dead and re.search(dead, s.url()):
                log.info("%s 세션 죽음 — 로그인 페이지 (%s)", platform, s.url())
                return False

            # 2) 로그인 상태에서만 도달하는 URL이면 살아있다.
            #
            # DOM 셀렉터보다 이쪽을 먼저 본다. 원티드 /cv는 로그인이면 /cv/list로,
            # 아니면 /cv/intro로 리다이렉트한다 — 렌더링 타이밍과 무관하게
            # 결정되므로 느린 환경(launchd 콜드스타트)에서도 흔들리지 않는다.
            alive_url = recipe.get("session_alive_url")
            if alive_url and alive_url in s.url():
                log.info("%s 세션 살아있음 (%s)", platform, s.url())
                return True

            # 3) 보조 신호 — 로그인해야만 존재하는 요소
            if alive_sel and s.exists(alive_sel, timeout_ms=12000):
                log.info("%s 세션 살아있음 — 요소 확인 (%s)", platform, s.url())
                return True

            # 4) 전부 아니면 **모르는 것**이지 죽은 게 아니다.
            #
            # 실측(2026-08-16 03시 스케줄 실행): 로그인이 멀쩡한데 launchd 환경에서
            # 브라우저 콜드스타트가 느려 셀렉터를 제때 못 찾았고, 그걸 '죽음'으로
            # 단정해 47건이 전부 LOGIN_REQUIRED로 막혔다. 증거의 부재를 부재의
            # 증거로 다룬 셈이다. 모르면 모른다고 해야 판정이 오염되지 않는다.
            log.warning(
                "%s 세션 확인 불가 — 로그인 페이지도 아니고 기대 요소도 못 찾음 (%s)",
                platform, s.url(),
            )
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("%s 세션 확인 실패 — 판단 보류: %s", platform, e)
        return None


def check_all(platforms: list[str] | None = None, *, headless: bool = True) -> dict[str, bool]:
    """확인된 것만 돌려준다. None(확인 불가)은 결과에서 빠진다 —
    applicability는 session_ok에 없는 플랫폼을 '모름'으로 다루므로 그게 맞다."""
    if platforms is None:
        platforms = [p.stem for p in RECIPE_DIR.glob("*.json")]

    out: dict[str, bool] = {}
    for p in platforms:
        result = check_session(p, headless=headless)
        if result is not None:
            out[p] = result
    return out


def describe(platforms: list[str] | None = None, *, headless: bool = True) -> dict[str, Any]:
    """CLI용. 확인 불가까지 포함해 사람이 읽을 수 있게 낸다."""
    if platforms is None:
        platforms = [p.stem for p in RECIPE_DIR.glob("*.json")]
    return {
        p: {True: "살아있음", False: "죽음", None: "확인 불가"}[check_session(p, headless=headless)]
        for p in platforms
    }
