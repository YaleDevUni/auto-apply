"""지원을 실행하기 전에 이 공고에 대해 알아야 하는 사실.

`prepare_application`과 `submit_application`이 **둘 다** 쓴다. 한쪽에 두면
다른 쪽이 그쪽을 import하게 되고, 그러면 두 workflow가 서로를 알게 된다.

`cli.py`에서 그대로 옮겼다 — 동작은 한 줄도 바꾸지 않았다.
"""

from __future__ import annotations

import logging

from .. import agent
from ..db import connect

log = logging.getLogger(__name__)

def job(job_id: int, *, resume_title: str | None = None, require_resume: bool = False) -> dict:
    """레시피가 쓸 공고 정보. 이력서 제목까지 붙여서 돌려준다.

    require_resume는 **제출 직전에만** 켠다. 이력서를 만들기 전에 부를 때는
    제목이 아직 없는 게 정상이라, 여기서 멈추면 만들 기회 자체가 없어진다.
    """
    from ..config import effective_config

    from .. import portfolio as portfolio_match

    conn = connect()
    try:
        row = conn.execute(
            """SELECT j.id AS job_id, j.platform, j.url, j.company, j.title,
                      j.description, s.track
               FROM jobs j LEFT JOIN screening s ON s.job_id = j.id
               WHERE j.id=?""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"공고 {job_id}가 없다")
        job = dict(row)

        pf_row = conn.execute(
            "SELECT portfolio_title FROM resume_builds WHERE job_id=?", (job_id,)
        ).fetchone()
    finally:
        conn.close()

    # 방금 만든 이력서 제목이 있으면 그걸 쓴다. 없을 때만 config 매핑으로 넘어간다
    # (이력서를 미리 만들어두고 트랙별로 고르던 예전 방식).
    resumes = effective_config().get("applicability", {}).get("resumes", {})
    job["resume"] = resume_title or (resumes.get(job["platform"], {}) or {}).get(job["track"])

    # 조립 단계(assemble.build_editor_json)가 고른 포트폴리오. portfolio는
    # 표시·저장용(NFC 그대로), portfolio_nfd는 지원 레시피 셀렉터용이다 —
    # 원티드가 업로드형 문서 파일명만 유니코드 NFD로 렌더링해서 그대로 쓰면
    # text-is() 매칭이 조용히 실패한다(portfolio.py 모듈 docstring 참고).
    job["portfolio"] = pf_row["portfolio_title"] if pf_row else None
    job["portfolio_nfd"] = portfolio_match.to_selector_text(job["portfolio"])
    if not job["resume"] and require_resume:
        # 여기서 멈추는 게 낫다. 이력서를 못 정한 채 진행하면 레시피가
        # 자리표시자를 못 채우거나, 더 나쁘게는 엉뚱한 이력서를 고른다.
        raise SystemExit(
            f"트랙 '{job['track']}'에 쓸 이력서가 지정되지 않았다.\n"
            f"config.yaml의 applicability.resumes.{job['platform']}.{job['track']} 에 "
            f"플랫폼에 저장된 이력서 제목을 그대로 적을 것."
        )
    return job


def skip_if_already_applied(job_id: int) -> dict | None:
    """이미 지원한 자리면 원장에 적고 건너뛸 이유를 돌려준다. 아니면 None.

    `apply_ledger`는 **우리가 낸 것만** 안다. 이 파이프라인을 쓰기 전에 사람이
    직접 낸 지원은 어디에도 없어서, 그런 자리가 대기열 상위에 그대로 올라온다
    (실측: 공고 303920 젠스타파트너스 — 화면에 '지원완료' 버튼이 떠 있다).

    한 번 발견하면 원장에 `external`로 적어 **다시는 올라오지 않게** 한다.
    매번 확인하면 대기열에 옛 지원이 쌓일수록 확인만 하다 끝난다.

    확인 자체가 실패하면(브라우저 사용 중, 세션 끊김, 셀렉터 낡음) 막지 않고
    진행한다 — 모르는 것을 '지원함'으로 단정하면 멀쩡한 자리를 조용히 버린다.
    반대 방향은 시간만 쓰고 지원 폼에서 걸린다.
    """
    from ..runner import apply as apply_mod
    from ..runner.lock import BrowserBusy

    # 이 함수 안에서 `job`을 지역변수로 쓰면 모듈 함수 `job()`이 가려져
    # 호출 시점에 UnboundLocalError가 난다. 그래서 `job_row`다.
    job_row = job(job_id)
    try:
        state = apply_mod.preflight(job_row)
    except BrowserBusy:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("이미 지원했는지 확인 실패(무시하고 진행): %s", e)
        return None

    if state is not True:
        return None

    agent.record_external(job_id)
    return {
        "already_applied": True,
        "stopped": "이미 지원한 공고 — 준비하지 않는다",
        "company": job_row.get("company"),
        "url": job_row.get("url"),
        "기록": "apply_ledger status=external (대기열에서 영구 제외, 제출 건수·한도에는 안 셈)",
    }
