"""제출 — 되돌릴 수 없는 외부 side effect가 실제로 일어나는 자리.

원장 관문이 여기 있다(CLAUDE.md §7.1):

    preflight → claim → run → mark_submitted / mark_failed

`live=False`면 선점하지 않는다. dry-run은 아무것도 제출하지 않으므로 자리를
잡을 이유가 없고, 잡으면 상한만 태운다.

`cli.py`에서 그대로 옮겼다 — 동작은 한 줄도 바꾸지 않았다.
"""

from __future__ import annotations

import html
import logging

from .. import agent
from . import context

log = logging.getLogger(__name__)

def apply_with(job: dict, *, live: bool, headless: bool = False) -> dict:
    """공고 정보가 이미 준비된 경우의 지원 실행. autoapply 체인이 이걸 쓴다."""
    from ..runner import LoginRequired, run

    job_id = job["job_id"]
    if not live:
        return run(job, live=False, headless=headless)

    # 제출 직전에 한 번 더 본다. 승인 버튼은 몇 시간 전 알림에서도 눌릴 수 있고,
    # 그 사이 사람이 원티드에서 직접 냈을 수 있다. 선점(claim) **전에** 봐야
    # 한다 — 선점부터 하면 그 자리가 오늘 한도 한 칸을 먹는다.
    already = context.skip_if_already_applied(job_id)
    if already:
        return {"skipped": True, "reason": "이미 지원한 공고", **already}

    ledger = agent.claim(job_id)
    if ledger is None:
        return {"skipped": True, "reason": "이미 처리됐거나 상한 도달", "quota": agent.quota()}

    try:
        result = run(job, live=True, headless=headless)
    except LoginRequired as e:
        # 제출 전에 확실히 실패했다 → 자리를 놓아주고 사람을 부른다.
        agent.mark_failed(ledger, str(e), release=True)
        agent.notify_login_required()
        raise SystemExit(f"로그인 필요: {e}") from e

    if result["submitted"]:
        agent.mark_submitted(ledger, evidence_path=result["evidence"])
        notify_submitted(job, result)
        delete_submitted_resume(job_id, job.get("resume") or "")
    else:
        # 눌렀는데 완료 화면을 못 봤다. 실제로 접수됐을 수 있으므로 자리를 놓지 않는다.
        agent.mark_failed(ledger, result["error"] or "제출 확인 실패", release=False)
    result["ledger"] = ledger
    return result


def apply_job(job_id: int, *, live: bool, headless: bool) -> dict:
    """dry-run은 선점하지 않는다. 아무것도 제출하지 않으니 자리를 잡을 이유가 없고,
    잡으면 상한만 태우고 그 자리를 다시 못 건드리게 된다.

    live일 때만 선점한다 — 그리고 선점이 실패하면(중복이거나 상한) 실행하지 않는다.
    """
    return apply_with(context.job(job_id, require_resume=True), live=live, headless=headless)


def submit_registered(job_id: int) -> dict:
    """준비 때 등록해둔 이력서로 제출만 한다.

    승인 시점에 다시 조립하지 않는 이유: 사람이 검토한 것과 나가는 것이
    달라진다. 조립은 준비 단계에서 끝났고, 여기서는 그 이력서를 고를 뿐이다.
    """
    from .. import assemble
    from ..runner import resume_editor

    reg = assemble.registration(job_id)
    if not reg.get("resume_title"):
        return {"stopped": "등록된 이력서가 없다 — 먼저 준비(cycle-apply)해야 한다"}

    # 보호 이력서(기본·사본메이커)가 등록돼 있으면 내지 않는다. 그건 그 공고에
    # 맞춰 만든 이력서가 아니라 **원본**이다. 재사용 경로(preview_resume_url)가
    # 남긴 기록이 그런 모양이고, 실제로 그 상태로 공고 33에 제출이 나갔다.
    if reg["resume_title"] in resume_editor.protected_titles():
        return {
            "stopped": f"등록된 이력서가 원본이다: {reg['resume_title']}",
            "이유": "공고에 맞춘 사본이 아니라 원본이라 제출하지 않는다",
            "할 일": f"cli.py autoapply {job_id} 로 이 공고용 이력서를 새로 만들 것",
        }

    job = context.job(job_id, resume_title=reg["resume_title"])
    return apply_with(job, live=True)


def notify_submitted(job: dict, result: dict) -> None:
    """제출 결과를 폰으로 알린다. 버튼을 눌렀는데 아무 소식이 없으면
    다시 누르게 되고, 그건 중복지원 시도가 된다."""
    from ..db import connect as _c
    from ..notify import telegram

    conn = _c()
    try:
        text = (
            f"✅ <b>제출 완료</b>\n{html.escape(str(job.get('company')))} — "
            f"{html.escape(str(job.get('title'))[:40])}\n"
            f"<i>원장에 기록됨. 같은 자리는 다시 나가지 않습니다.</i>"
        )
        shot = result.get("evidence")
        if not (shot and telegram.send_photo(conn, shot, text)):
            telegram.notify(conn, text)
    finally:
        conn.close()


def delete_submitted_resume(job_id: int, title: str) -> None:
    """제출 직후 그 이력서를 플랫폼에서 지운다. 지원이력에서 여전히 접근
    가능하므로 목록에 남겨둘 이유가 없다 — 남겨두면 max_keep을 금방 채운다.

    로컬 사본이 있을 때만 지운다. 없으면 그게 유일한 기록이라
    `resume_editor.cleanup()`과 같은 규칙으로 건드리지 않는다. 실패해도 지원
    자체는 이미 끝났으므로 흐름을 막지 않는다 — 다음 `resumes --cleanup`이
    나이·개수 기준으로 결국 치운다.
    """
    from ..paths import RESUME_OUT_DIR

    log = logging.getLogger(__name__)
    if not title:
        return
    if not list(RESUME_OUT_DIR.glob(f"{job_id}-*.json")):
        log.info("로컬 사본이 없어 제출 이력서를 남겨둔다: %s", title)
        return
    try:
        from ..runner import resume_editor

        deleted = resume_editor.delete_after_submit(title)
        log.info("제출 이력서 삭제 %s: %s", "성공" if deleted else "실패", title)
    except Exception as e:  # noqa: BLE001
        log.warning("제출 이력서 삭제 실패(무시, 다음 정리가 치운다): %s", e)
