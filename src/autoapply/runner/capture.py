"""지원 폼의 실제 DOM을 떠서 레시피 작성 근거를 만든다.

레시피를 상상해서 쓰면 안 된다. 셀렉터는 화면을 보고 쓰는 것이고, 원티드 폼은
로그인해야 보인다. 그래서 순서가 이렇다:

    1. cli.py browser-login       사람이 한 번 로그인한다 (프로필에 세션이 남는다)
    2. cli.py capture <job_id>    폼을 떠서 JSON + 스크린샷으로 남긴다
    3. recipes/wanted.json 작성   2번 결과를 보고 셀렉터를 고른다
    4. cli.py apply <job_id>      dry-run으로 채워보고 스크린샷 확인
    5. cli.py apply <job_id> --live

캡처는 아무것도 제출하지 않는다. 읽기만 한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import BROWSER_DIR, EVIDENCE_DIR
from .session import PlaywrightSession, browser

# 페이지에서 폼 요소를 훑어 셀렉터 후보와 함께 뱉는다.
# id > name > placeholder > 텍스트 순으로 안정적인 셀렉터를 고른다.
_JS = """() => {
  const pick = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${el.name}"]`;
    if (el.placeholder) return `${el.tagName.toLowerCase()}[placeholder="${el.placeholder}"]`;
    const t = (el.innerText || '').trim().split('\\n')[0];
    if (t && t.length < 30) return `${el.tagName.toLowerCase()}:has-text("${t}")`;
    return null;
  };
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const out = [];
  document.querySelectorAll('input, textarea, select, button, [role=button], a[href*=apply]')
    .forEach((el) => {
      if (!vis(el)) return;
      out.push({
        tag: el.tagName.toLowerCase(),
        type: el.type || null,
        selector: pick(el),
        name: el.name || null,
        id: el.id || null,
        placeholder: el.placeholder || null,
        text: (el.innerText || el.value || '').trim().slice(0, 60) || null,
        required: el.required || null,
      });
    });
  return {url: location.href, title: document.title, elements: out};
}"""


def capture(
    job: dict[str, Any], *, headless: bool = False, click: list[str] | None = None
) -> dict[str, Any]:
    with browser(headless=headless) as s:
        return _capture_with(s, job, click or [])


def _capture_with(
    s: PlaywrightSession, job: dict[str, Any], click: list[str]
) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = EVIDENCE_DIR / f"capture-{job['platform']}-{job['job_id']}-{stamp}"

    s.goto(job["url"])
    s.page().wait_for_timeout(2000)

    # 지원 폼은 '지원하기'를 눌러야 나온다. 여는 것뿐이라 되돌릴 수 있다 —
    # 제출은 이 함수가 절대 하지 않는다.
    for sel in click:
        s.click(sel)
        s.page().wait_for_timeout(2500)
    snap = s.page().evaluate(_JS)

    shot = Path(f"{base}.png")
    s.screenshot(shot)
    dump = Path(f"{base}.json")
    dump.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "url": snap["url"],
        "title": snap["title"],
        "elements": len(snap["elements"]),
        "json": str(dump),
        "screenshot": str(shot),
        # 로그인 페이지로 튕겼는지는 URL이 말해준다. 여기서 판단하지 않고
        # 그대로 돌려준다 — 캡처는 관찰만 하고 판정하지 않는다.
    }


def login(*, headless: bool = False, url: str = "https://id.wanted.co.kr/") -> dict[str, str]:
    """사람이 로그인할 창을 띄우고 닫을 때까지 기다린다.

    자동화가 대신 로그인하지 않는다 — 원티드는 OAuth만 제공하고, 그건 사람의
    권한이다. 이 함수가 하는 일은 **로그인 상태가 저장될 프로필로 창을 여는 것**
    뿐이다. 여기서 로그인해두면 이후 모든 러너 실행이 그 세션을 재사용한다.

    ## 구글 로그인은 여기서 막힌다

    실측: 구글은 CDP로 제어되는 브라우저에서 "브라우저 또는 앱이 안전하지 않을 수
    있습니다"로 로그인을 거부한다. session.py에서 자동화 흔적을 지우지만 구글은
    그것만 보는 게 아니라서 통과를 보장할 수 없다.

    **카카오나 이메일 로그인을 쓴다.** 원티드 세션 쿠키만 프로필에 생기면 그 다음은
    어느 제공자로 받았든 똑같이 동작한다 — 로그인 제공자는 한 번 쓰고 버리는 통로다.
    """
    # 상주 창이 프로필을 잡고 있으면 두 번째 Chrome은 아예 못 뜬다(프로필 잠금).
    # 그럴 땐 그 창에 붙어서 로그인 페이지로 보낸다 — 사람은 숨겨둔 창을
    # 다시 꺼내(Dock의 Chrome 클릭) 로그인하면 된다.
    import httpx

    from .session import CDP_URL

    resident = False
    try:
        httpx.get(f"{CDP_URL}/json/version", timeout=2)
        resident = True
    except Exception:  # noqa: BLE001
        pass

    s = PlaywrightSession(headless=headless, hidden=resident)
    s.start()
    s.goto(url)
    if resident:
        print("\n※ 상주 브라우저 창에 로그인 페이지를 열었습니다.")
        print("  숨겨두셨다면 Dock에서 Chrome을 눌러 창을 꺼내세요.\n", flush=True)
    print(
        "브라우저에서 로그인하세요.\n"
        "  → 구글은 자동화 브라우저를 차단합니다. 카카오 또는 이메일을 쓰세요.\n"
        "끝나면 이 터미널에서 Enter...",
        flush=True,
    )
    try:
        input()
    finally:
        where = s.url()
        s.close()
    return {"프로필": str(BROWSER_DIR), "마지막 URL": where}
