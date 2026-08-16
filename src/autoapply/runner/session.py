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


# 창을 숨기는 문제는 결국 **창을 몇 번 띄우느냐**의 문제였다.
#
# 시도했다가 버린 것들(전부 실측):
#   --headless          원티드가 /cv 에서 403
#   --headless=new      CloudFront 403 — "Request blocked"
#   --window-position 음수  macOS가 화면 안으로 되돌린다
#   System Events 로 숨기기  접근성 권한 대기로 시간 초과
#
# 남은 답: **창을 한 번만 띄우고 계속 붙는다.** 사람이 그 창을 한 번 숨기면
# 그 뒤로 새 창이 안 뜨니 방해받지 않는다. 실행마다 브라우저가 뜨고 지는
# 3~5초도 같이 사라진다.
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

# 상주 브라우저를 오래 붙잡고 쓰면(실측 2026-08-16: 5시간 이상, 여러 날에
# 걸친 프로필 재사용 누적) 렌더러가 조용히 맛이 간다 — 이동·클릭은 멀쩡한데
# screenshot()만 타임아웃까지 정확히 그 시간을 다 쓰고 실패한다("느려서"가
# 아니라 새 프레임을 아예 못 만드는 상태다: 15초든 60초든 그 값 그대로,
# about:blank 같은 빈 페이지도 동일 증상, 누적된 서비스워커 72개를 정리해도
# 무관했다). 껐다 켜면 즉시 회복된다(재현 3/3, 2~4초로 정상화). 로그인
# 세션은 디스크 프로필에 남으므로 재시작해도 유지된다.
#
# 그 자리에서 바로 죽이지 않는 이유: screenshot() 실패 시점엔 같은 브라우저를
# 다른 스텝이 아직 쓰고 있을 수 있다(호출부의 뒷정리 등). 대신 여기 표시만
# 남기고, **다음 세션을 새로 시작할 때**(PlaywrightSession.start() 맨 앞 —
# 아직 아무 작업도 안 걸려 있는 안전한 경계) 소비해서 그때 재시작한다.
_RESTART_FLAG = BROWSER_DIR.parent / ".needs_browser_restart"


def flag_needs_restart(reason: str = "") -> None:
    """다음 세션 시작 전에 상주 브라우저를 재시작하라는 표시를 남긴다."""
    _RESTART_FLAG.parent.mkdir(parents=True, exist_ok=True)
    _RESTART_FLAG.write_text(reason or "unknown", encoding="utf-8")


def _consume_restart_flag() -> str | None:
    if not _RESTART_FLAG.exists():
        return None
    reason = _RESTART_FLAG.read_text(encoding="utf-8")
    _RESTART_FLAG.unlink(missing_ok=True)
    return reason


def _kill_resident() -> None:
    """CDP_PORT에 붙은 상주 브라우저 프로세스를 찾아 종료한다."""
    import subprocess

    try:
        out = subprocess.run(
            ["lsof", "-t", "-P", "-n", f"-itcp:{CDP_PORT}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:  # noqa: BLE001
        return
    for pid in out.split():
        try:
            subprocess.run(["kill", pid.strip()], timeout=5, check=False)
        except Exception:  # noqa: BLE001
            pass


def _spawn_resident(*, wait_sec: int = 20) -> bool:
    """상주 브라우저를 자식 프로세스로 띄우고, CDP가 응답할 때까지 기다린다.

    브라우저 프로세스는 이 함수를 부른 프로세스가 끝나도 살아남는다 —
    `while True: sleep`로 자기 자신을 붙잡아두는 자식 파이썬 프로세스가 곧
    브라우저 수명이다(`cli.py _browser_open`과 같은 패턴).
    """
    import subprocess
    import time as _t

    import httpx

    from ..paths import CODE_ROOT

    subprocess.Popen(
        [str(CODE_ROOT / ".venv/bin/python"), "-c",
         "import sys; sys.path.insert(0,'.');"
         "from src.autoapply.runner.session import PlaywrightSession;"
         "s=PlaywrightSession(hidden=False); s.start();"
         "s.goto('https://www.wanted.co.kr/cv');"
         "import time;\n"
         "while True: time.sleep(3600)"],
        cwd=str(CODE_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    for _ in range(wait_sec):
        _t.sleep(1)
        try:
            httpx.get(f"{CDP_URL}/json/version", timeout=2)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def restart_resident() -> bool:
    """상주 브라우저를 껐다 켠다. 실측으로 확인한 렌더러 정지의 유일한 회복법."""
    _kill_resident()
    return _spawn_resident()


def _cleanup_stale_service_workers() -> int:
    """방문마다 하나씩 쌓이고 안 없어지는 service_worker CDP 타깃을 정리한다.

    실측(2026-08-16): 원티드 공고를 열 때마다 braze-service-worker.js가 새
    CDP 타깃으로 하나씩 남는다(71개 확인 후 공고 하나 더 열었더니 72개).
    원래는 ~30초 유휴 후 브라우저가 스스로 종료해야 하는데, CDP가 계속
    붙어 있는 상주 창에서는 그 유휴 종료가 안 도는 것으로 보인다. 스크린샷
    타임아웃의 원인은 아니었다(따로 확인해 배제함) — 별개의 리소스 누수다.

    닫아도 안전하다: 서비스워커는 그 오리진 페이지가 필요할 때 브라우저가
    알아서 다시 등록한다. 오프라인 캐시·구독 같은 데이터는 서비스워커
    "등록"이 아니라 스토리지에 있으므로 스레드를 접는다고 지워지지 않는다.
    """
    import httpx

    try:
        targets = httpx.get(f"{CDP_URL}/json/list", timeout=5).json()
    except Exception:  # noqa: BLE001
        return 0

    closed = 0
    for t in targets:
        if t.get("type") != "service_worker":
            continue
        try:
            httpx.get(f"{CDP_URL}/json/close/{t['id']}", timeout=3)
            closed += 1
        except Exception:  # noqa: BLE001
            pass
    return closed


def _hidden_default() -> bool:
    """설정에서 읽는다. 기본은 숨김 — 사람 화면을 뺏지 않는 쪽이 기본이어야 한다."""
    try:
        from ..config import effective_config

        return bool((effective_config().get("browser") or {}).get("hidden", True))
    except Exception:  # noqa: BLE001
        return True


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
        hidden: bool | None = None,
    ):
        self._headless = headless
        # 창을 화면 밖에 띄운다. headless와 다르다 — 브라우저는 완전히 정상적인
        # 헤디드 Chrome이고, 봇 판정에 걸릴 표면이 늘지 않는다. 원티드는
        # headless를 막으므로 그쪽으로는 갈 수 없다.
        #
        # 무인 운영이 목표라 사람이 쓰는 화면을 계속 뺏으면 안 된다. 로그인처럼
        # 사람이 봐야 하는 순간에만 hidden=False로 부른다.
        self._hidden = _hidden_default() if hidden is None else hidden
        self._dir = user_data_dir
        self._channel = channel
        self._ctx: Any = None
        self._page: Any = None
        self._pw: Any = None
        self._attached: Any = None

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        reason = _consume_restart_flag()
        if reason is not None:
            log.warning("상주 브라우저 재시작 신호 발견(%s) — 재시작 후 이어감", reason[:100])
            if not restart_resident():
                log.warning("상주 브라우저 재시작 실패 — 기존 흐름대로 계속 시도")

        self._dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()

        # 이미 떠 있는 창이 있으면 거기 붙는다. 새로 띄우지 않는다.
        if self._hidden and self._attach():
            return
        opts: dict[str, Any] = dict(
            headless=self._headless,
            viewport={"width": 1440, "height": 900},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            # 자동화 흔적 제거. OAuth 제공자(특히 구글)는 이걸 보고 로그인을 막는다.
            # --enable-automation은 UA와 navigator.webdriver에 자국을 남기고,
            # AutomationControlled는 페이지가 직접 읽을 수 있는 플래그다.
            args=[
                "--disable-blink-features=AutomationControlled",
                # 상주 창으로 쓸 수 있게 CDP를 열어둔다. 다음 실행이 여기 붙는다.
                f"--remote-debugging-port={CDP_PORT}",
            ],
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

    def _attach(self) -> bool:
        """떠 있는 Chrome에 붙는다. 없으면 False."""
        try:
            browser_ = self._pw.chromium.connect_over_cdp(CDP_URL, timeout=3000)
        except Exception:  # noqa: BLE001
            return False

        try:
            self._ctx = browser_.contexts[0] if browser_.contexts else browser_.new_context()
            # 새로 띄울 때와 같은 은폐를 붙는 경우에도 건다. 빠뜨리면 상주
            # 창으로 바꾼 순간부터 조용히 봇 판정 표면이 넓어진다.
            try:
                self._ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
            except Exception:  # noqa: BLE001
                pass
            # 탭을 새로 열지 않고 있는 것을 재사용한다. 탭이 늘면 사람이 그 창을
            # 다시 열었을 때 지저분하고, 새 탭은 앱을 앞으로 끌어낼 수 있다.
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            self._page.set_default_timeout(15000)
            self._attached = browser_
            log.info("떠 있는 브라우저에 연결 (창을 새로 띄우지 않음)")

            # 상주 창은 계속 재사용되므로 방문마다 쌓이는 서비스워커를 여기서
            # 정리한다. 실패해도 연결 자체는 이미 끝났으니 attach를 막지 않는다.
            try:
                closed = _cleanup_stale_service_workers()
                if closed:
                    log.info("쌓인 서비스워커 %d개 정리", closed)
            except Exception as e:  # noqa: BLE001
                log.debug("서비스워커 정리 중 무시된 오류: %s", e)

            return True
        except Exception as e:  # noqa: BLE001
            log.info("연결 실패 — 새로 띄운다: %s", e)
            self._ctx = self._page = None
            return False

    def close(self) -> None:
        # 붙어서 쓴 창은 **닫지 않는다.** 닫으면 다음 실행이 다시 띄우게 되고,
        # 사람이 숨겨둔 창이 사라져 애초의 문제로 돌아간다.
        if getattr(self, "_attached", None) is not None:
            try:
                self._pw.stop()
            except Exception as e:  # noqa: BLE001
                log.debug("세션 정리 중 무시된 오류: %s", e)
            self._ctx = self._page = self._pw = self._attached = None
            return

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
        try:
            self._page.screenshot(path=str(path), full_page=True)
        except Exception:
            # 렌더러가 멎은 상태일 수 있다(모듈 docstring의 _RESTART_FLAG 참고).
            # 이 호출 자체는 못 살리지만, 다음 세션 시작 때 자동으로 회복되게
            # 표시만 남긴다 — 지금 당장 죽이면 다른 스텝이 같은 브라우저를
            # 쓰고 있을 수 있다.
            flag_needs_restart("screenshot timeout")
            raise

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
def browser(*, headless: bool = False, hidden: bool | None = None) -> Iterator[PlaywrightSession]:
    s = PlaywrightSession(headless=headless, hidden=hidden)
    s.start()
    try:
        yield s
    finally:
        s.close()
