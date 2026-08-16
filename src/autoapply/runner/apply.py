"""레시피를 실행해 지원 폼을 채운다.

## dry-run이 기본값인 이유

제출은 되돌릴 수 없고, 회사에 남는 건 사용자 본인의 이름이다. 그래서
`live=True`를 **명시하지 않으면 submit 스텝은 실행되지 않는다.** 나머지는 전부
실제로 한다 — 페이지 열고, 폼 채우고, 파일 올리고, 스크린샷을 남긴다.

이 설계의 실질적 이득: 레시피가 맞는지 검증하는 유일한 방법이 "사람이 사진을
보는 것"이 된다. 셀렉터 JSON을 텔레그램으로 보내 승인받는 건 무의미하다 —
사람은 `button.css-1x2y3z`가 맞는 버튼인지 알 수 없다. 다 채워진 폼 스크린샷은
알 수 있다.

## 레시피 형식 (recipes/<platform>.json)

    {
      "platform": "wanted",
      "version": 1,
      "login_dead_pattern": "id\\\\.wanted\\\\.co\\\\.kr|/login",
      "steps": [
        {"do": "goto",   "url": "{job.url}"},
        {"do": "click",  "sel": "button:has-text('지원하기')"},
        {"do": "fill",   "sel": "input[name='name']", "value": "{profile.name}"},
        {"do": "upload", "sel": "input[type=file]",   "path": "{profile.resume_pdf}"},
        {"do": "expect", "sel": "text=지원서 확인"},
        {"do": "submit", "sel": "button:has-text('지원하기')"}
      ],
      "success_any": ["text=지원이 완료", "text=지원 완료"]
    }

`optional: true`가 붙은 스텝은 셀렉터가 없어도 넘어간다. 나머지는 실패하면
그 자리에서 멈춘다 — 폼을 반쯤 채운 채로 제출하는 것보다 안 하는 게 낫다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import EVIDENCE_DIR, PROFILE_DIR, RECIPE_DIR
from .session import LoginRequired, PlaywrightSession, browser

log = logging.getLogger(__name__)

# 되돌릴 수 없는 스텝. dry-run에서는 이것만 건너뛴다.
IRREVERSIBLE = {"submit"}


class RecipeError(RuntimeError):
    """레시피가 현실과 안 맞는다. 화면이 바뀌었거나 레시피가 틀렸다."""


def load_recipe(platform: str) -> dict[str, Any]:
    path = RECIPE_DIR / f"{platform}.json"
    if not path.exists():
        raise RecipeError(f"레시피 없음: {path}  —  cli.py capture 로 폼을 먼저 뜬다")
    return json.loads(path.read_text(encoding="utf-8"))


def _profile() -> dict[str, str]:
    """profile/profile.json — 이름·연락처처럼 값이 고정된 것들."""
    path = PROFILE_DIR / "profile.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _render(template: str, job: dict[str, Any]) -> str:
    """{job.url} / {profile.name} 치환. 값이 없으면 그대로 터뜨린다.

    빈 문자열로 조용히 넘어가면 이름 없는 지원서가 나간다. 멈추는 게 낫다.
    """
    out = template
    for prefix, src in (("job", job), ("profile", _profile())):
        for key, val in src.items():
            out = out.replace(f"{{{prefix}.{key}}}", "" if val is None else str(val))
    if "{" in out and "}" in out:
        raise RecipeError(f"치환되지 않은 자리표시자: {out}")
    return out


def applied_state(s: PlaywrightSession, job: dict[str, Any], recipe: dict[str, Any]) -> bool | None:
    """이 공고에 **이미 지원했는지** 본다. True / False / None(모름).

    페이지를 여기서 연다 — 호출부가 어디에 있든 같은 답을 주게 하려는 것이다.

    왜 필요한가: `apply_ledger`는 **우리가 낸 것만** 안다. 이 파이프라인을
    쓰기 전에 사람이 직접 낸 지원은 어디에도 없어서, 그런 자리가 대기열에
    그대로 올라온다. 그러면 이력서 조립(LLM 3~4회)과 사본 등록까지 다 한 뒤
    지원 폼에서야 막힌다 — 돈과 시간을 다 쓰고 나서다.

    모르면 모른다고 한다(None). 판정을 '지원함'으로 기울이면 멀쩡한 자리를
    조용히 버리는데, 그건 이 파이프라인이 존재하는 이유를 깎아먹는다.
    반대 방향(모르는데 진행)은 지원 폼에서 걸리므로 손해가 시간뿐이다.
    """
    cfg = recipe.get("applied_check") or {}
    if not cfg:
        return None

    s.goto(_render(cfg.get("url", "{job.url}"), job))
    s.assert_logged_in(recipe.get("login_dead_pattern", ""))
    s.page().wait_for_timeout(int(cfg.get("wait_ms", 3500)))

    for sel in cfg.get("applied_any") or []:
        if s.page().locator(sel).count():
            log.info("이미 지원한 공고 — %s (%s)", job.get("job_id"), sel)
            return True
    for sel in cfg.get("open_any") or []:
        if s.page().locator(sel).count():
            return False

    log.warning(
        "지원 여부를 못 읽었다 — 지원완료 표시도, 지원 패널도 없다 (공고 %s, %s). "
        "레시피 applied_check가 낡았을 수 있다",
        job.get("job_id"), s.url(),
    )
    return None


def preflight(
    job: dict[str, Any],
    *,
    session: PlaywrightSession | None = None,
    headless: bool = False,
) -> bool | None:
    """`applied_state`를 브라우저 하나 열어서 확인한다(세션을 주면 그걸 쓴다)."""
    recipe = load_recipe(job["platform"])
    if not (recipe.get("applied_check") or {}):
        return None
    if session is not None:
        return applied_state(session, job, recipe)
    with browser(headless=headless, kind="지원여부 확인",
                 label=f"공고 {job.get('job_id', '')}") as s:
        return applied_state(s, job, recipe)


def run(
    job: dict[str, Any],
    *,
    live: bool = False,
    session: PlaywrightSession | None = None,
    headless: bool = False,
) -> dict[str, Any]:
    """공고 하나에 레시피를 실행한다.

    live=False(기본)면 submit 직전까지만 하고 스크린샷을 남긴다.
    반환: {ok, submitted, evidence, steps_done, error}
    """
    recipe = load_recipe(job["platform"])  # 브라우저를 띄우기 전에 확인한다
    if session is not None:
        return _run_with(session, job, live, recipe)
    with browser(headless=headless, kind="제출" if live else "지원 폼 확인",
                 label=f"공고 {job.get('job_id', '')}") as s:
        return _run_with(s, job, live, recipe)


def _run_with(
    s: PlaywrightSession, job: dict[str, Any], live: bool, recipe: dict[str, Any]
) -> dict[str, Any]:
    dead = recipe.get("login_dead_pattern", "")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shot = EVIDENCE_DIR / f"{job['platform']}-{job['job_id']}-{stamp}.png"

    done: list[str] = []
    result: dict[str, Any] = {
        "job_id": job["job_id"],
        "company": job.get("company"),
        "live": live,
        "ok": False,
        "submitted": False,
        "evidence": str(shot),
        "steps_done": done,
        "error": None,
    }

    try:
        for i, step in enumerate(recipe["steps"]):
            act = step["do"]

            if act in IRREVERSIBLE and not live:
                log.info("[dry-run] %d:%s 건너뜀 — 제출하지 않는다", i, act)
                done.append(f"{i}:{act}(skipped)")
                break

            _step(s, step, job)
            done.append(f"{i}:{act}")

            # 스텝마다 세션 생사를 본다. 폼을 반쯤 채운 뒤 로그인 페이지로
            # 튕기는 경우가 실제로 있다 — 그때 submit이 엉뚱한 곳을 누른다.
            s.assert_logged_in(dead)

            if act == "submit":
                result["submitted"] = _confirm(s, recipe)

        result["ok"] = result["submitted"] or not live
    except LoginRequired as e:
        result["error"] = f"LOGIN_REQUIRED: {e}"
        raise_after = e
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        raise_after = None
    else:
        raise_after = None

    # 성공이든 실패든 증적은 남긴다. 실패 화면이 레시피 수정의 유일한 단서다.
    try:
        s.screenshot(shot)
    except Exception as e:  # noqa: BLE001
        log.warning("스크린샷 실패: %s", e)
        result["evidence"] = None

    if raise_after is not None:
        raise raise_after
    return result


def _click(s: PlaywrightSession, sel: str, *, optional: bool, submit: bool) -> None:
    """오버레이를 걷어내고 누른다. 필요하면 force로.

    실측(2026-08-16 06시 사이클): `지원하기`가 DOM에 있는데 클릭이 15초 타임아웃으로
    죽어 사이클이 통째로 실패했다. 원티드는 팝업 뒤에 투명 백드롭
    (role=presentation)을 깔고, 그게 남아 있으면 클릭이 거기 먹힌다.

    submit은 force를 쓰지 않는다. 되돌릴 수 없는 동작이라, 정말 눌리는 상태에서만
    눌러야 한다 — 가려진 버튼을 강제로 누르면 무엇을 눌렀는지 알 수 없다.
    """
    page = s.page()
    for _ in range(3):
        if not page.locator('[role="presentation"]').count():
            break
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

    try:
        s.click(sel)
        return
    except Exception as e:  # noqa: BLE001
        if submit:
            raise
        log.info("일반 클릭 실패 (%s) — force로 재시도: %s", sel, type(e).__name__)

    try:
        page.locator(sel).first.click(force=True)
    except Exception as e:  # noqa: BLE001
        if optional:
            log.info("선택 스텝 클릭 실패 — 건너뜀: %s", sel)
            return
        raise RecipeError(f"클릭 실패: {sel} ({type(e).__name__})") from e


def _step(s: PlaywrightSession, step: dict[str, Any], job: dict[str, Any]) -> None:
    act = step["do"]
    # 셀렉터에도 치환을 건다. 어느 이력서를 고를지가 공고마다 다르므로
    # {job.resume} 같은 자리표시자가 sel 안에 들어온다.
    sel = _render(step["sel"], job) if step.get("sel") else ""
    optional = bool(step.get("optional"))

    if act == "goto":
        s.goto(_render(step["url"], job))
        return

    if sel and not s.exists(sel, timeout_ms=step.get("timeout_ms", 8000)):
        if optional:
            log.info("선택 스텝 건너뜀 (요소 없음): %s", sel)
            return
        raise RecipeError(f"요소를 찾지 못함: {sel}  —  화면이 바뀌었을 수 있다")

    if act == "click" or act == "submit":
        _click(s, sel, optional=optional, submit=(act == "submit"))
    elif act == "check":
        s.check(sel)
    elif act == "fill":
        s.fill(sel, _render(step["value"], job))
    elif act == "upload":
        path = _render(step["path"], job)
        if not Path(path).exists():
            raise RecipeError(f"업로드할 파일이 없다: {path}")
        s.upload(sel, path)
    elif act == "expect":
        pass  # 위의 exists 검사가 곧 단언이다
    else:
        raise RecipeError(f"모르는 스텝: {act}")


def _confirm(s: PlaywrightSession, recipe: dict[str, Any]) -> bool:
    """제출이 **접수됐는지** 확인한다. 버튼을 눌렀다는 것과는 다르다."""
    for sel in recipe.get("success_any", []):
        if s.exists(sel, timeout_ms=10000):
            return True
    return False
