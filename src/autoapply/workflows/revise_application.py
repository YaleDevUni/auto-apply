"""재작성 — 사람의 수정 요청을 반영해 다시 만들고 검토를 다시 요청한다.

CLAUDE.md §3.2가 이름까지 적어둔 workflow(`ReviseApplication`)다.

**이어받지 않는다**(`reuse=False`). 재작성은 "이번엔 다르게 써 달라"는 요청이라
지난번 이력서를 재사용하면 지시가 통째로 무시된 채 옛 이력서가 "재작성됨"으로
폰에 다시 올라간다 — 사람은 고쳐진 줄 알고 승인한다.

`cli.py`에서 그대로 옮겼다 — 동작은 한 줄도 바꾸지 않았다.
"""

from __future__ import annotations

import html
import logging

from ..db import connect
from ..notify import report
from . import context, prepare_application

log = logging.getLogger(__name__)


def _fit_score(job_id: int) -> int:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT fit_score FROM screening WHERE job_id=?", (job_id,)
        ).fetchone()
        return int(row["fit_score"]) if row and row["fit_score"] is not None else 0
    finally:
        conn.close()


def run(job_id: int, feedback: str) -> dict:
    """사람의 수정 요청을 반영해 다시 만들고, 검토를 다시 요청한다.

    캐시를 건너뛰고 조립부터 다시 한다 — 피드백은 입력이 바뀐 것이라서
    캐시된 결과를 재사용하면 요청이 반영되지 않는다.

    앞서 만든 이력서는 플랫폼에 그대로 두고 새로 만든다. 지우려면 브라우저를
    한 번 더 띄워야 하고, 그 사이에 실패하면 아무것도 없는 상태가 된다.
    정리는 `resumes --cleanup`이 나중에 한다.
    """
    from .. import assemble

    log = logging.getLogger(__name__)
    log.info("공고 %s 재작성 — %s", job_id, feedback[:80])

    # 원장에 먼저 남긴다. 그래야 이번 지시가 원장에 들어간 상태로 조립되고,
    # 같은 공고에 두 번째 요청이 와도 첫 번째가 남아 있다.
    #
    # 기록한 줄은 폰으로 되돌려준다. 요약이 지시를 잘못 옮기는 경우가 있는데
    # (특히 '짧게' 같은 한 단어), 그 줄은 앞으로 모든 이력서 프롬프트에 실려
    # 나가므로 사람이 바로 알아보고 파일에서 고칠 수 있어야 한다.
    try:
        entry = assemble.append_revision(job_id, feedback)
        report.tell(f"📒 <b>원장 기록</b>\n<code>{html.escape(entry)}</code>\n"
                    f"<i>틀렸으면 {assemble.REVISION_LOG.name} 에서 그 줄을 고치세요.</i>")
    except Exception as e:  # noqa: BLE001
        log.warning("원장 기록 실패(재작성은 계속): %s", e)

    built = assemble.build_editor_json(job_id, feedback=feedback)
    if not built["ok"]:
        report.tell(f"❌ <b>재작성 중단</b> — 공고 {job_id}\n"
                    f"필수요건 미충족 {built['required_gaps']}건\n"
                    "<i>수정 요청이 사실 저장소에 없는 내용을 요구했을 수 있습니다.</i>")
        return {"stopped": "재작성 후에도 필수요건 미충족", "gaps": built["required_gaps"]}

    # **이어받지 않는다.** 재작성은 "이번엔 다르게 써 달라"는 요청이므로,
    # 지난번에 등록해둔 이력서를 재사용하면 지시가 통째로 무시된 채 옛 이력서가
    # "재작성됨"으로 폰에 다시 올라간다 — 사람은 고쳐진 줄 알고 승인한다.
    result = prepare_application.run(job_id, resume_url=None, live=False, reuse=False)
    target = context.job(job_id)
    target["fit_score"] = target.get("fit_score") or 0
    report.prepared(
        {**target, "job_id": job_id, "fit_score": _fit_score(job_id)},
        result,
    )
    return {"revised": job_id, "resume": result.get("resume")}
