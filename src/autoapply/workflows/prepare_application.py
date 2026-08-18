"""조립 → 이력서 등록 → 지원 폼. 기본은 dry-run이라 제출하지 않는다.

**중단 경계가 네 곳 있다**(`tasks.check`). 편집기는 자동저장이라 `fill()` 안에서
끊으면 절반만 채워진 이력서가 계정에 남는다 — 그래서 들어가기 전에 마지막으로
묻고, 안에서는 안 본다. `set_stage("filling")`을 **시작할 때** 적는 것도 같은
이유다(끝난 뒤에 적으면 "채우다 죽었다"가 아무 데도 안 남는다).

이 순서는 재배열하면 안 된다.

`cli.py`에서 그대로 옮겼다 — 동작은 한 줄도 바꾸지 않았다.
"""

from __future__ import annotations

import json
import logging

from .. import tasks
from . import context, submit_application

log = logging.getLogger(__name__)

def resume_title(job: dict) -> str:
    """공고별 이력서 제목. 제목이 곧 지원 폼에서 이력서를 고르는 열쇠다.

    회사명을 앞에 둔다. 목록에서 사람이 훑을 때 '어느 자리에 낸 것'인지가
    먼저 보여야 하기 때문이다. 50자 제한이 있어 뒤를 자른다.
    """
    company = (job.get("company") or "").strip()
    title = (job.get("title") or "").strip()
    name = f"{company} {title}".strip() or "박예일 이력서"
    return name[:50]


def run(job_id: int, *, resume_url: str | None, live: bool, reuse: bool = True) -> dict:
    """조립 → 등록 → 지원. 각 단계가 다음 단계의 입력을 만든다.

    이력서 제목이 고리다. 편집기에 채운 뒤 그 제목을 읽어 지원 레시피에 넘기므로,
    `config.applicability.resumes` 매핑이 더는 필요 없다 — 트랙별로 미리 만들어둔
    이력서를 고르는 게 아니라 공고마다 만들기 때문이다.

    조립이 `ok=False`(필수요건 근거 없음/검수 반려)면 여기서 멈춘다. 맞지 않는
    자리에 이름을 남기는 것보다 안 내는 게 낫다.
    """
    from .. import assemble
    from ..runner import resume_editor
    from ..runner.lock import browser_lock

    # 무엇보다 먼저 "여기 이미 냈나"를 본다. 아래 조립은 LLM 3~4회에 수 분이
    # 걸리는데, 이미 지원한 자리면 그게 전부 버려진다(지원 폼에서야 막힌다).
    # 페이지 한 번 여는 값으로 그걸 다 아낀다.
    # 중단 표시를 **단계 경계마다** 본다. 예전에는 night-cycle의 while 루프
    # 꼭대기에서만 봤다 — 한 건이 조립(LLM)과 등록(브라우저)까지 묶여 있어
    # /stop을 눌러도 수 분 뒤에야 멈췄고, 사람 눈에는 "안 먹는다"로 보였다
    # (실측 2026-08-17: cancel_at 07:00:40 → 실제 종료 07:01:44).
    #
    # 여기서 접어도 남는 것이 없다: 아직 브라우저에 아무것도 안 썼다.
    tasks.check("지원준비 시작 전")
    already = context.skip_if_already_applied(job_id)
    if already:
        return already

    # 지난번에 어디까지 갔나. 끊긴 자리에서 이어받는다 — 처음부터 다시 하면
    # 조립(LLM)을 또 하고, 더 나쁘게는 **같은 공고용 이력서를 하나 더 만든다.**
    # 자세한 재사용 규칙은 assemble.progress의 주석에 있다.
    prog = assemble.progress(job_id) if reuse else {"resumable": False}
    if prog["resumable"]:
        log.info("공고 %s — %s 단계에서 이어받는다 (이력서 %r 재사용, 새로 안 만든다)",
                 job_id, prog["stage"], prog["resume_title"])
        tasks.check("지원 폼 진입 전")
        job = context.job(job_id, resume_title=prog["resume_title"], require_resume=True)
        with browser_lock("지원준비", label=f"공고 {job_id}"):
            result = submit_application.apply_with(job, live=live)
        assemble.set_stage(job_id, "prepared")
        reg = assemble.registration(job_id)
        return {
            "company": context.job(job_id).get("company"),
            "이어받음": prog["stage"],
            "resume": {"title": prog["resume_title"], "url": prog["resume_url"],
                       "shot": None, "skills": len(json.loads(reg.get("skills") or "[]")
                                                   if isinstance(reg.get("skills"), str)
                                                   else reg.get("skills") or [])},
            "apply": result,
        }

    tasks.check("이력서 조립 전")
    built = assemble.build_editor_json(job_id)
    if not built["ok"]:
        return {
            "stopped": "이력서 조립 단계에서 중단",
            "이유": f"필수요건 미충족 {built['required_gaps']}건",
            "gaps": [g["text"][:70] for g in built["gaps"] if g.get("level") == "필수"],
        }

    # 사본메이커에서 시작한다. 어느 사본을 쓸지는 공고가 정한다 — 사본마다
    # 용도가 다르고(개발자용·데브옵스·AX·영업), 그 결이 곧 이력서의 뼈대다.
    job_row = context.job(job_id)
    template = resume_editor.pick_template(job_row)
    new_title = resume_title(job_row)
    logging.getLogger(__name__).info("사본 선택: %s → %r", template, new_title)

    # 등록과 지원을 **한 덩어리로** 잡는다. 둘 사이를 남에게 내주면 이력서를
    # 만들어둔 채 다른 작업이 탭을 가져가고, 돌아왔을 땐 지원 폼이 아닌 화면에서
    # 셀렉터를 찾다 타임아웃으로 죽는다. 아래 fill/apply의 browser()는 같은
    # 프로세스라 재진입으로 그냥 통과한다.
    # **여기가 마지막 안전 경계다.** fill() 안에서는 중단을 안 본다 —
    # 편집기는 자동저장이라 중간에 끊으면 절반만 채워진 이력서가 계정에
    # 남고, 그건 안 만드느니만 못하다. 그래서 들어가기 전에 마지막으로 묻는다.
    tasks.check("이력서 등록 전")
    assemble.set_stage(job_id, "assembled")
    # `filling`은 **시작할 때** 적는다. 다른 단계와 반대인데, 이유는 채우다
    # 끊긴 이력서를 다음 실행이 재사용하면 절반짜리가 그대로 나가기 때문이다.
    # 끝난 뒤에 적으면 "채우다 죽었다"가 아무 데도 안 남는다.
    assemble.set_stage(job_id, "filling")
    with browser_lock("지원준비", label=f"공고 {job_id}"):
        filled = resume_editor.fill(
            built["data"], resume_url=resume_url,
            template=None if resume_url else template,
            new_title=new_title, job_id=job_id, dry_run=False,
        )

        # 등록 결과를 기록해두고, 지원 단계는 DB에서 읽는다. 화면에서 제목을 읽는
        # 것은 한 번뿐이고, 그 뒤로는 편집기 상태가 바뀌어도 흔들리지 않는다.
        title = filled.get("title") or assemble.registered_title(job_id)
        if not title:
            return {"stopped": "이력서 제목을 읽지 못함 — 어느 이력서를 낼지 정할 수 없다"}
        assemble.record_registration(
            job_id, resume_title=title,
            resume_url=filled.get("url", ""), skills=filled.get("skills") or [],
            report=resume_editor.fill_report(filled),
        )
        # 여기까지 왔으면 플랫폼에 **완성된** 이력서가 실제로 있다. 이 뒤에
        # 무엇이 죽어도 다음 실행은 이걸 재사용하고 새로 만들지 않는다.
        assemble.set_stage(job_id, "registered")

        # 이력서는 이미 만들어져 등록됐다. 여기서 접어도 반쯤 만든 것은
        # 안 남는다 — 지원 폼은 dry-run이라 바깥세상에 아무것도 안 낸다.
        tasks.check("지원 폼 진입 전")
        job = context.job(job_id, resume_title=title, require_resume=True)
        result = submit_application.apply_with(job, live=live)
    assemble.set_stage(job_id, "prepared")
    return {
        "company": built["company"],
        # shot은 원티드 편집기 화면을 새로고침(=저장 확인) 후 찍은 실물 사진이다
        # (resume_editor.fill 안에서 촬영). 폰으로 보내는 미리보기는 이걸 쓴다 —
        # 로컬에서 그려낸 이미지가 아니라 실제 그 페이지를 보여줘야 한다.
        "resume": {"title": filled["title"], "url": filled["url"],
                   "lost": filled.get("lost"), "skills": len(filled.get("skills", [])),
                   "shot": filled.get("shot"), "data": built["data"]},
        "apply": result,
    }
