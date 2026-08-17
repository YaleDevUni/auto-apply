"""새벽 사이클 — 목표건수 또는 대기열 소진까지 지원준비를 반복한다.

수집은 여기서 안 한다. `cli.py scrape`가 별도 스케줄(정오)로 돈다.

**서킷브레이커가 여기 있다.** 실패는 목표를 못 채우므로 루프를 못 멈춘다 —
claude 하나가 죽으면 대기열이 빌 때까지 계속 다음 공고로 갔다(실측
2026-08-17: 앞에 actionable 122건). 같은 오류가 세 번이면 접는다.

`cli.py`에서 그대로 옮겼다 — 동작은 한 줄도 바꾸지 않았다.
"""

from __future__ import annotations

import logging

from .. import agent, llm, tasks
from ..notify import report
from . import prepare_application

log = logging.getLogger(__name__)

# 같은 오류가 이만큼 반복되면 대기열이 남아 있어도 접는다. 셋인 이유: 한 번은
# 그 공고만의 사정일 수 있고, 두 번은 우연일 수 있다. 세 번이면 다음 공고에서도
# 같은 자리에서 죽는다 — 그 시점부터 남은 대기열은 전부 같은 실패다.
CIRCUIT_BREAK_REPEATS = 3


def run(target: int, *, defer: bool = False) -> dict:
    """지원준비(dry-run)를 목표건수 또는 대기열 소진까지 반복한다.

    수집은 여기서 하지 않는다 — `cli.py scrape`가 별도 스케줄(정오, 하루 한 번)로
    돈다. 지원준비는 이미 수집·판정된 대기열(`v_actionable`)에서만 고른다.
    둘을 묶어뒀을 때는 `/apply`처럼 준비만 급히 부르고 싶을 때도 매번
    전체 수집(수 분)이 같이 돌아 목적과 안 맞았다 — 수집은 하루에 한 번이면
    충분하고, 준비는 그보다 훨씬 자주(또는 즉시) 부를 일이 있다.

    `cycle-apply`를 limit=1로 반복 호출하는 이유: 그쪽이 이미 스킵·재사용·
    알림 로직을 갖고 있다. 여기서 다시 구현하면 두 경로가 갈라져 한쪽만
    고치는 버그가 난다.
    """
    from ..db import connect as _connect
    from ..notify.listener import is_paused, pause_reason

    log = logging.getLogger(__name__)

    conn = _connect()
    try:
        if is_paused(conn):
            return {"skipped": f"일시정지 — {pause_reason(conn)}"}
    finally:
        conn.close()

    from .. import errors

    prepared, attempted, items = 0, 0, []
    seen_ids: set[int] = set()
    stopped_by_human = False
    skipped_applied = 0   # 이미 지원한 자리 — 목표에도, 실패에도 안 넣는다
    # 같은 오류가 몇 번 났나. 지문(숫자·경로를 지운 메시지)으로 센다 —
    # 공고 id만 다른 같은 고장이 매번 다른 오류로 보이면 셀 수가 없다.
    fail_counts: dict[str, int] = {}
    tripped: tuple[str, int] | None = None
    while prepared < target:
        # 건과 건 사이가 접기 좋은 자리다. 한 건은 이력서 등록까지 묶여 있어
        # 중간에 끊으면 절반만 채워진 이력서가 계정에 남는다.
        if tasks.cancelled():
            stopped_by_human = True
            log.warning("night-cycle: 중단 요청 — %d/%d 준비하고 멈춘다", prepared, target)
            break
        r = cycle_apply(1, defer=defer)
        n = r.get("prepared", 0)
        if n == 0:
            log.info("night-cycle: 대기열 소진 (%d/%d 준비, %s)",
                      prepared, target, r.get("reason", "알 수 없음"))
            break
        attempted += n
        stall = False
        for it in r.get("items", []):
            # report.prepared와 같은 기준으로 판정한다. apply 하위의 error를
            # 안 보면(예전 코드가 그랬다) RecipeError 같은 실패가 성공으로
            # 잡혀 목표에 못 미친 채 "목표 도달"로 잘못 멈춘다 — 실측으로 잡음
            # (공고 9, RecipeError인데 최초 코드는 ok=True로 셌다).
            apply_err = (it.get("apply") or {}).get("error")
            ok = not (it.get("error") or it.get("stopped") or apply_err)
            if ok:
                prepared += 1
            elif not it.get("already_applied"):
                # **실패는 목표를 못 채우므로 루프를 못 멈춘다.** 예전에는 멈추는
                # 조건이 셋뿐이었다 — 사람 중단 / 대기열 소진 / 같은 job_id 재등장.
                # 그래서 claude 하나가 죽으면 대기열이 빌 때까지 계속 다음 공고로
                # 갔다(실측 2026-08-17: actionable 122건이 그 앞에 있었다).
                # 같은 오류가 세 번 났으면 다음 공고에서도 같은 자리에서 죽는다.
                key = errors.normalize(str(it.get("error") or apply_err))
                fail_counts[key] = fail_counts.get(key, 0) + 1
                if fail_counts[key] >= CIRCUIT_BREAK_REPEATS:
                    tripped = (key, fail_counts[key])
            # 이미 지원한 자리는 목표 건수에 넣지 않는다(ok=False라 이미 안 센다).
            # 다만 실패로 세지도 않는다 — 고칠 게 없는 정상 동작이다. 그래서
            # 따로 세어 보고에만 남긴다.
            if it.get("already_applied"):
                skipped_applied += 1
            items.append({"job_id": it["job_id"], "company": it.get("company"), "ok": ok,
                          **({"이미지원": True} if it.get("already_applied") else {})})
            # 실패한 건이 24시간 스킵 보호를 못 받는 경우(빌드 자체가 조기에
            # 죽어 resume_builds에 안 남는 경우)가 있다 — 그러면 next_targets가
            # 같은 job_id를 계속 다시 준다. 같은 id가 두 번 나오면 무한루프
            # 신호이므로 즉시 멈춘다. 대기열 소진과 달리 이건 "고장"이다.
            if it["job_id"] in seen_ids:
                log.warning("night-cycle: 공고 %s가 반복돼 멈춘다 (24시간 스킵 실패로 추정)",
                            it["job_id"])
                stall = True
            seen_ids.add(it["job_id"])
        if stall or tripped:
            break

    # 자기개선은 더는 시간이 되면 자동으로 안 돈다 — 문제가 자체진단됐을 때만.
    # LLM 호출 없는 DB 조회라 매번 불러도 싸다. 실제로 뭔가 있을 때만
    # improve(코딩 에이전트)를 별도 프로세스로 띄운다 — 여기서 기다리면
    # night-cycle 자체가 브랜치 작업 시간만큼 늘어진다.
    self_diagnosed: list[str] = []
    try:
        from .. import orchestrator
        conn = _connect()
        try:
            self_diagnosed = [it["title"] for it in orchestrator.self_items(conn)]
        finally:
            conn.close()
        if self_diagnosed:
            log.info("night-cycle: 자체진단 %d건 — improve 호출", len(self_diagnosed))
            from ..paths import CODE_ROOT
            tasks.spawn(
                [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "improve", "--limit", "1"],
                log_name="improve",
            )
    except Exception as e:  # noqa: BLE001
        log.warning("자체진단 확인 실패(무시): %s", e)

    if stopped_by_human:
        reason = "중단됨(사람 요청)"
    elif tripped:
        reason = f"같은 오류 {tripped[1]}번 — 전면 중지"
        report.circuit_break(tripped[0], tripped[1], prepared, attempted)
    elif prepared >= target:
        reason = "목표 도달"
    else:
        reason = "대기열 소진"
    return {
        "target": target,
        "prepared": prepared,
        "attempted": attempted,
        "이미지원이라 건너뜀": skipped_applied,
        "stopped_reason": reason,
        "self_diagnosed": self_diagnosed,
        "items": items,
    }


def cycle_apply(limit: int, *, defer: bool = False) -> dict:
    """대기열 상위 N건을 dry-run으로 준비한다.

    **cron에서는 절대 제출하지 않는다.** 이력서를 조립해 등록하고 지원 폼까지
    채운 뒤 스크린샷만 남긴다. 사람이 그 사진을 보고 `apply <id> --live` 로
    제출한다 — 되돌릴 수 없는 행동에 사람의 확인을 남겨두는 지점이다.

    한 사이클에 기본 1건인 이유: 건당 브라우저를 띄우고 이력서를 조립하므로
    2분 안팎이 든다. 여러 건을 몰아 돌리면 사이클이 길어지고, 무엇보다
    사람이 검토할 스크린샷이 한 번에 쌓여 실질 검토가 안 된다. `night-cycle`은
    이 한계를 limit=1로 여러 번 부르는 식으로 우회하고, `defer=True`로
    알림은 쌓아뒀다가 나중에 한 번에 보낸다.
    """
    from ..db import connect as _connect
    from ..notify.listener import is_paused, pause_reason

    conn = _connect()
    try:
        if is_paused(conn):
            return {"skipped": f"일시정지 — {pause_reason(conn)}"}
        # 최근 준비한 공고는 건너뛴다. dry-run은 선점하지 않으므로 이게 없으면
        # 매 사이클 같은 1위만 다시 준비하고 대기열이 줄지 않는다.
        targets = agent.next_targets(limit, conn, skip_prepared_hours=24)
    finally:
        conn.close()

    if not targets:
        return {"prepared": 0, "reason": "대기열 비어 있음"}

    out = []
    for t in targets:
        try:
            # 준비도 **매번 사본에서 시작한다.** 이력서 하나를 덮어쓰며
            # 재사용하던 경로는 2026-08-16에 지웠다 — 아껴지는 것보다 깨지는
            # 게 훨씬 컸다:
            #
            #   · resume_url을 넘기면 `pick_template`이 통째로 무시된다.
            #     영업 공고에 개발 이력서를 내지 않으려고 트랙별 사본메이커를
            #     만들어놨는데, 무인 사이클은 그 분기를 한 번도 안 탔다.
            #   · 재사용 대상이 '박예일 기본'으로 굳어 있었다. 스킬은 사본이
            #     비어 있다는 전제로 **추가만** 하므로(prune 없음), 같은
            #     이력서를 계속 덮어쓰면 공고마다 스킬이 쌓여 엉킨다.
            #   · 실제로 그 상태로 공고 33에 제출까지 나갔다(apply_ledger id=4,
            #     resume_title='박예일 기본').
            #
            # 쌓이는 이력서는 `resumes --cleanup`이 치운다 —
            # `made_resumes`에 우리가 만든 것이 남으므로 근거가 있다.
            r = prepare_application.run(t["job_id"], resume_url=None, live=False)
        except tasks.Cancelled as e:
            # 사람이 멈춘 것을 한 공고의 실패로 삼키면 다음 공고로 넘어간다 —
            # 멈추라고 한 사람 눈에는 그게 "안 멈춘다"다. 위로 올려 최상단이
            # "⏹ 중단"으로 답하게 한다.
            note_stage_failure(t["job_id"], f"중단: {e}")
            raise
        except (llm.UsageLimited, llm.LoginExpired, llm.ClaudeUnavailable):
            # **다음 공고로 넘어가지 않는다.** 이력서는 LLM 없이 못 만들므로
            # 다음 공고도 같은 자리에서 죽는다. 여기서 삼켜 한 건의 실패로
            # 만들면 서킷브레이커가 세 번 셀 때까지 세 건을 더 태운다 —
            # 셀 것도 없이 확실한 실패다.
            #
            # 위로 올리면 night-cycle이 통째로 접히고 cli.py 최상단이
            # errors.record로 원인을 폰에 보낸다(한도인지 로그인 만료인지까지).
            raise
        except Exception as e:  # noqa: BLE001
            r = {"error": f"{type(e).__name__}: {e}"}
            # 어느 단계에서 죽었는지를 남긴다. 다음 실행이 이어받을지 말지를
            # stage로 정하므로, 실패 사유가 그 옆에 붙어 있어야 "왜 여기서
            # 이어받는가"를 사람이 읽을 수 있다.
            note_stage_failure(t["job_id"], r["error"])
        out.append({"job_id": t["job_id"], "company": t["company"], **r})
        report.prepared(t, r, defer=defer)
    # prepared는 "몇 건을 손댔나"이지 "몇 건이 준비됐나"가 아니다 — night-cycle이
    # 이 값으로 대기열 소진만 판정하고, 성공 여부는 items를 보고 따로 센다.
    return {
        "prepared": len(out),
        "already_applied": sum(1 for r in out if r.get("already_applied")),
        "items": out,
    }


def note_stage_failure(job_id: int, error: str) -> None:
    """단계 실패 기록은 절대 지원준비를 막지 않는다 — 기록하다 죽으면
    원래 실패가 무엇이었는지가 통째로 사라진다."""
    try:
        from .. import assemble

        assemble.note_failure(job_id, error)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).debug("단계 실패 기록 실패(무시): %s", e)
