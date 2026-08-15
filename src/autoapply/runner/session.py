"""브라우저 세션 — 로그인 상태가 사는 곳.

## 왜 프로토콜로 갈랐나

로그인은 자동화 대상이 아니다. 원티드는 지원에 OAuth만 제공하고(카카오/애플/
구글/이메일) 비밀번호로 자동 로그인하는 경로가 없다. 그래서 세션은 **사람이
한 번 만들어 놓고 러너가 재사용하는 상태**여야 한다.

문제는 재사용할 프로필이 어디 있느냐다. 두 가지 선택지가 있고 트레이드오프가
정반대다:

    PlaywrightSession  전용 프로필. 의존성 없고 headless로 돌지만,
                       아무도 그 브라우저를 안 쓰니 세션이 조용히 썩는다.

    AsideSession       사람이 매일 쓰는 브라우저를 그대로 쓴다. 세션이 계속
                       살아있지만, 데스크톱 앱이 떠 있어야 하고 외부 CLI에
                       실행 경로가 묶인다.

지금은 앞엣것으로 간다. 세션이 실제로 썩는 게 확인되면 그때 뒤엣것을 파일 하나로
붙인다 — 레시피가 쓰는 어휘(locator/fill/click/screenshot)가 양쪽 같아서
레시피는 손대지 않아도 된다. 그래서 지금 결정하지 않아도 된다.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

from ..paths import BROWSER_DIR

log = logging.getLogger(__name__)


class LoginRequired(RuntimeError):
    """세션이 죽었다. 재시도로 넘을 수 없다 — 사람을 불러야 한다."""


class Session(Protocol):
    """러너가 브라우저에 요구하는 전부. 이보다 넓히지 않는다."""

    def goto(self, url: str) -> None: ...
    def url(self) -> str: ...
    def fill(self, selector: str, value: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def check(self, selector: str) -> None: ...
    def upload(self, selector: str, path: str) -> None: ...
    def exists(self, selector: str, timeout_ms: int = 3000) -> bool: ...
    def text(self) -> str: ...
    def screenshot(self, path: Path) -> None: ...


class PlaywrightSession:
    """persistent context로 로그인 상태를 프로필 디렉터리에 유지한다.

    headless=False가 기본이다. 원티드·사람인은 headless를 봇으로 판정하는 일이
    잦고, 무엇보다 사람이 같은 프로필에 로그인해야 하므로 창이 보여야 한다.
    """

    def __init__(
        self,
        *,
        headless: bool = False,
        user_data_dir: Path = BROWSER_DIR,
        channel: str | None = "chrome",
    ):
        self._headless = headless
        self._dir = user_data_dir
        self._channel = channel
        self._ctx: Any = None
        self._page: Any = None
        self._pw: Any = None

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        opts: dict[str, Any] = dict(
            headless=self._headless,
            viewport={"width": 1440, "height": 900},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            # 자동화 흔적 제거. OAuth 제공자(특히 구글)는 이걸 보고 로그인을 막는다.
            # --enable-automation은 UA와 navigator.webdriver에 자국을 남기고,
            # AutomationControlled는 페이지가 직접 읽을 수 있는 플래그다.
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        # 번들 Chromium 대신 실제 Chrome을 쓴다. 브랜딩·버전·구성요소가 전부
        # 달라서 봇 판정에서 가장 큰 차이를 만든다.
        if self._channel:
            opts["channel"] = self._channel
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(str(self._dir), **opts)
        except Exception as e:  # noqa: BLE001
            if not self._channel:
                raise
            log.warning("Chrome 실행 실패 (%s) — 번들 Chromium으로 대체", e)
            opts.pop("channel")
            self._ctx = self._pw.chromium.launch_persistent_context(str(self._dir), **opts)

        # webdriver 속성은 플래그만으로는 안 지워진다. 페이지 로드 전에 덮어쓴다.
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.set_default_timeout(15000)

    def close(self) -> None:
        for obj, meth in ((self._ctx, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception as e:  # noqa: BLE001
                    log.debug("세션 정리 중 무시된 오류: %s", e)
        self._ctx = self._page = self._pw = None

    # ── Session 프로토콜 ──────────────────────────────────────

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")

    def url(self) -> str:
        return self._page.url

    def fill(self, selector: str, value: str) -> None:
        self._page.fill(selector, value)

    def click(self, selector: str) -> None:
        self._page.click(selector)

    def check(self, selector: str) -> None:
        """체크박스를 켠다. 이미 켜져 있으면 아무 일도 안 한다.

        click이 아니라 check인 이유: 원티드 이력서 선택은 토글이라 click을 쓰면
        이미 선택된 상태에서 눌러 **꺼버린다.** 그러면 제출 버튼이 비활성화되고,
        운 나쁘면 그걸 모른 채 지나간다. check()는 멱등이라 그 사고가 없다.

        원티드는 실제 input을 svg 뒤에 숨겨놔서 Playwright의 check()가 force로도
        못 누른다. 그래서 연결된 <label>을 누르되, **누르기 전에 상태를 보고**
        이미 켜져 있으면 건너뛴다 — 멱등성은 label 클릭으로도 지킬 수 있다.
        """
        box = self._page.locator(selector).first
        if box.is_checked():
            return

        box_id = box.get_attribute("id")
        if box_id:
            self._page.click(f'label[for="{box_id}"]')
        else:
            box.check(force=True)

        if not box.is_checked():
            raise RuntimeError(f"체크박스를 켜지 못했다: {selector}")

    def upload(self, selector: str, path: str) -> None:
        self._page.set_input_files(selector, path)

    def exists(self, selector: str, timeout_ms: int = 3000) -> bool:
        try:
            self._page.wait_for_selector(selector, timeout=timeout_ms, state="attached")
            return True
        except Exception:  # noqa: BLE001
            return False

    def text(self) -> str:
        return self._page.inner_text("body")

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._page.screenshot(path=str(path), full_page=True)

    # ── 러너가 쓰는 부가 기능 ──────────────────────────────────

    def page(self) -> Any:
        """레시피로 표현 안 되는 것을 직접 만질 때만. 남용하면 레시피가 무의미해진다."""
        return self._page

    def assert_logged_in(self, dead_pattern: str) -> None:
        """현재 URL이 로그인 페이지면 LoginRequired를 던진다.

        세션 생사를 확인하는 **유일한 신뢰할 만한 신호**다. 쿠키가 있는지 보는
        것으로는 알 수 없다 — 만료된 쿠키도 파일에는 남아 있다. 실제로 이동해
        보고 로그인 페이지로 튕기는지 확인하는 게 유일하게 확실하다.
        """
        if dead_pattern and re.search(dead_pattern, self.url()):
            raise LoginRequired(f"로그인 페이지로 튕김: {self.url()}")


@contextmanager
def browser(*, headless: bool = False) -> Iterator[PlaywrightSession]:
    s = PlaywrightSession(headless=headless)
    s.start()
    try:
        yield s
    finally:
        s.close()
