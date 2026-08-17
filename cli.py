#!/usr/bin/env python3
"""autoapply CLI — cron이 부르는 진입점.

    python cli.py scrape                # 수집 + 두 축 판정
    python cli.py scrape --platform wanted
    python cli.py reevaluate            # 재수집 없이 재판정 (요청 0회)
    python cli.py targets --limit 5     # 에이전트가 지원할 목록
    python cli.py blocked               # 왜 막혔는지
    python cli.py status                # 한 줄 요약
    python cli.py where                 # 경로 확인
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sys

from src.autoapply import agent, llm, pipeline, tasks
from src.autoapply.adapters import REGISTRY
from src.autoapply.db import connect, now
from src.autoapply.notify import telegram
from src.autoapply.paths import describe


def _out(obj) -> None:
    """에이전트가 파이프로 받아 파싱한다. 항상 JSON으로 낸다."""
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    p = argparse.ArgumentParser(prog="autoapply")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scrape", help="수집 + 판정")
    sp.add_argument("--platform", choices=list(REGISTRY), action="append")
    sp.add_argument(
        "--session",
        action="append",
        default=[],
        metavar="PLATFORM=0|1",
        help="플랫폼별 로그인 세션 유효 여부 수동 지정 (예: --session wanted=1)",
    )
    sp.add_argument(
        "--check-session",
        action="store_true",
        help="수집 전에 브라우저로 세션 생사를 실제 확인한다 (플랫폼당 ~10초)",
    )

    ssp = sub.add_parser("session-check", help="로그인 세션이 살아있는지 실제로 확인")
    ssp.add_argument("--platform", action="append")
    ssp.add_argument("--notify", action="store_true", help="죽었으면 텔레그램으로 알린다")

    sub.add_parser("reevaluate", help="재수집 없이 재판정")

    tp = sub.add_parser("targets", help="지원 가능한 공고 목록")
    tp.add_argument("--limit", type=int, default=10)

    sub.add_parser("blocked", help="막힌 이유 집계")
    sub.add_parser("quota", help="오늘 남은 지원 가능 건수")

    lcp = sub.add_parser("llm-cost", help="LLM 토큰·비용 — 공고별 또는 최근 전체")
    lcp.add_argument("job_id", type=int, nargs="?", help="생략하면 최근 공고 20건 요약")

    sub.add_parser("browser-open", help="상주 브라우저 창을 띄운다 (한 번만 하면 된다)")
    sub.add_parser("browser-restart", help="상주 브라우저를 껐다 켠다 (렌더러가 멎었을 때)")

    rv = sub.add_parser("revise", help="수정 요청을 반영해 이력서를 다시 만들고 재검토 요청")
    rv.add_argument("job_id", type=int)
    rv.add_argument("feedback")

    gp = sub.add_parser("guide", help="작성 가이드를 지시대로 고친다 (Opus 5, 백업 남김)")
    gp.add_argument("instruction", help="예: 영업 공고엔 인프라 경험을 빼라는 규칙을 §7-1에 추가")
    gp.add_argument("--revert", action="store_true", help="마지막 백업으로 되돌린다")
    gp.add_argument(
        "--clear-session", action="store_true",
        help="이어가던 대화를 끊는다. 다음 지시는 새 대화로 시작한다")

    lgp = sub.add_parser("revlog", help="수정 요청 원장 보기 / 고치기")
    lgp.add_argument("--edit", type=int, metavar="N", help="N번 항목을 고친다")
    lgp.add_argument("--delete", type=int, metavar="N", help="N번 항목을 지운다")
    lgp.add_argument("--text", default="", help="--edit 과 함께 쓸 새 내용")

    bp = sub.add_parser("builds", help="이력서 조립·등록 기록 (왜 미완인지)")
    bp.add_argument("--limit", type=int, default=8)

    hp = sub.add_parser("health", help="파이프라인 이상 감지 (LLM 0회)")
    hp.add_argument("--no-notify", action="store_true", help="텔레그램 알림 없이 확인만")
    hp.add_argument("--history", action="store_true", help="최근 스냅샷 추이")
    sub.add_parser("status", help="상태 요약")
    sub.add_parser("where", help="경로 확인")

    tsp = sub.add_parser("telegram-setup", help="텔레그램 알림 연결")
    sub.add_parser("telegram-commands", help='텔레그램 "/" 자동완성 목록 다시 등록')
    tsp.add_argument("token", help="@BotFather에게 받은 봇 토큰")

    sub.add_parser("notify-login", help="세션 끊김 알림 수동 트리거 (쿨다운 무시하지 않음)")
    lp = sub.add_parser("listen", help="폰에서 온 메시지 처리 (운영 명령 + 개발 지시 접수)")
    lp.add_argument(
        "--watch", action="store_true",
        help="상시 대기하며 즉시 응답한다 (롱폴링). 없으면 한 번 훑고 끝낸다",
    )

    cq = sub.add_parser("improve", help="자가복구 계획을 세운다 (plan의 별칭)")
    cq.add_argument("--limit", type=int, default=1)
    cq.add_argument("--list", action="store_true", help="할 일만 보고 실행하지 않는다")

    plp = sub.add_parser(
        "plan", help="고장 큐·지시를 읽어 수정 계획을 세운다. low면 바로 반영")
    plp.add_argument("--limit", type=int, default=1)
    plp.add_argument("--list", action="store_true", help="할 일만 보고 실행하지 않는다")

    fxp = sub.add_parser("fix-run", help="승인된 계획을 수행한다")
    fxp.add_argument("plan_id", type=int)

    erp = sub.add_parser("errors", help="고장 큐")
    erp.add_argument("--limit", type=int, default=12)

    sub.add_parser("plans", help="수정 계획 목록")

    rvp = sub.add_parser("fix-revert", help="자동반영된 커밋을 되돌린다")
    rvp.add_argument("sha")

    rsp = sub.add_parser("resumes", help="플랫폼 이력서 목록 / 정리")
    rsp.add_argument(
        "--cleanup", action="store_true",
        help="기준을 넘은 이력서를 실제로 지운다. 기본은 무엇이 지워질지만 보여준다",
    )

    sub.add_parser("browser-login", help="로그인용 창을 띄운다 (사람이 직접 로그인)")

    cp = sub.add_parser("capture", help="지원 폼 DOM을 떠서 레시피 작성 근거를 만든다")
    cp.add_argument("job_id", type=int)
    cp.add_argument(
        "--click",
        action="append",
        default=[],
        metavar="SELECTOR",
        help="덤프 전에 누를 셀렉터 (폼을 여는 용도). 여러 번 지정 가능",
    )

    rp = sub.add_parser("resume", help="공고에 맞춘 이력서 조립 (작성 → 검수 → 재작성)")
    rp.add_argument("job_id", type=int)
    rp.add_argument("--rounds", type=int, default=2, help="재작성 포함 최대 라운드 (기본 2)")
    rp.add_argument("--print", action="store_true", help="본문을 화면에 출력")

    ap2 = sub.add_parser(
        "autoapply", help="조립 → 이력서 등록 → 지원까지 한 번에. 기본은 dry-run")
    ap2.add_argument("job_id", type=int)
    ap2.add_argument("--resume-url", help="기존 이력서를 재사용한다 (없으면 새로 만든다)")
    ap2.add_argument(
        "--live", action="store_true",
        help="실제로 제출한다. 되돌릴 수 없다 — dry-run 스크린샷을 먼저 확인할 것")

    cyc = sub.add_parser(
        "cycle-apply", help="대기열 상위 N건을 dry-run으로 준비한다 (제출 안 함)")
    cyc.add_argument("--limit", type=int, default=1)
    cyc.add_argument(
        "--defer", action="store_true",
        help="알림을 바로 안 보내고 쌓아둔다. flush-notify가 나중에 보낸다")

    sub.add_parser("flush-notify", help="쌓인 지원 준비 알림을 순서대로 보낸다")

    ncp = sub.add_parser(
        "night-cycle",
        help="수집 → 지원준비를 목표건수 또는 대기열 소진까지 반복 (제출 안 함)")
    ncp.add_argument("--target", type=int, default=30)
    ncp.add_argument(
        "--defer", action="store_true",
        help="알림을 즉시 안 보내고 쌓아둔다 (새벽 자동실행용). "
             "사람이 텔레그램으로 직접 부를 땐 안 씀 — 바로 받아야 한다")

    sbp = sub.add_parser(
        "submit", help="이미 등록된 이력서로 제출만 한다 (조립·입력 없음)")
    sbp.add_argument("job_id", type=int)

    apr = sub.add_parser("apply", help="레시피 실행. 기본은 dry-run(제출 안 함)")
    apr.add_argument("job_id", type=int)
    apr.add_argument(
        "--live",
        action="store_true",
        help="실제로 제출한다. 되돌릴 수 없다 — dry-run 스크린샷을 확인한 뒤에만 쓸 것",
    )
    apr.add_argument("--headless", action="store_true")

    args = p.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr
    )

    # 오래 도는 명령만 '지금 도는 작업'으로 등록한다. 폰에서 /running으로 보고
    # /stop으로 멈출 수 있는 대상이 이 목록이다. 짧게 끝나는 조회 명령까지
    # 넣으면 목록이 순간 쓰레기로 차서 정작 볼 것을 못 본다.
    kind = _TASK_KINDS.get(args.cmd)
    if kind:
        with tasks.running(kind, _task_label(args)):
            return _dispatch(args)
    return _dispatch(args)


# 명령 → 사람이 읽을 이름. /running·/stop 메시지에 그대로 나간다.
_TASK_KINDS = {
    "scrape": "공고수집",
    "night-cycle": "지원준비",
    "cycle-apply": "지원준비",
    "autoapply": "지원준비",
    "submit": "제출",
    "apply": "지원 폼 실행",
    "improve": "계획수립",
    "plan": "계획수립",
    "fix-run": "자가복구",
    "revise": "이력서 재작성",
    "resumes": "이력서 정리",
    "reevaluate": "재판정",
}


def _task_label(args: argparse.Namespace) -> str:
    if getattr(args, "job_id", None) is not None:
        return f"공고 {args.job_id}"
    if getattr(args, "target", None) is not None:
        return f"목표 {args.target}건"
    if getattr(args, "platform", None):
        return ",".join(args.platform)
    return ""


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "scrape":
        session_ok = {}
        if args.check_session:
            from src.autoapply.runner import check_all

            session_ok.update(check_all(args.platform))
        # 수동 지정이 탐침 결과를 덮는다. 사람이 아는 게 더 정확한 경우가 있다.
        for item in args.session:
            k, _, v = item.partition("=")
            session_ok[k.strip()] = v.strip() not in ("0", "false", "no", "")
        results = pipeline.run_all(args.platform, session_ok or None)
        _out(results)
        agent.notify_login_required()  # 세션 끊긴 플랫폼이 있으면 알린다 (쿨다운 적용)

        # 중간에 멈춘 수집으로는 건강을 판정하지 않는다. 상세를 덜 받았으니
        # NO_DETAIL이 통과분을 지배하고, actionable도 같이 주저앉는다 —
        # 전부 "중단했으니 당연한" 값인데 경보는 "망가졌다"고 폰에 울린다.
        # 실측(2026-08-16): 22건에서 멈춘 수집이 "NO_DETAIL이 통과분의 78%"
        # 경보를 띄웠다.
        if any(r.get("stopped") for r in results):
            print("⏹ 중단된 수집이라 건강 판정은 건너뛴다", file=sys.stderr)
        else:
            from src.autoapply import health

            result = health.run()  # 조용히 망가진 게 있으면 알린다
            for f in result.get("findings", []):
                print(f"⚠️  {f['message']}", file=sys.stderr)
    elif args.cmd == "reevaluate":
        _out(pipeline.reevaluate())
    elif args.cmd == "targets":
        _out(agent.next_targets(args.limit))
    elif args.cmd == "blocked":
        _out(agent.blocked_summary())
    elif args.cmd == "quota":
        _out(agent.quota())
    elif args.cmd == "llm-cost":
        _out(_llm_cost(args.job_id))
    elif args.cmd == "browser-open":
        _out(_browser_open())
    elif args.cmd == "browser-restart":
        from src.autoapply.runner.session import restart_resident

        ok = restart_resident()
        _out({"재시작": "성공" if ok else "실패 — 20초 안에 CDP가 안 뜸"})
    elif args.cmd == "revise":
        _out(_revise(args.job_id, args.feedback))
    elif args.cmd == "guide":
        _out(_guide(args.instruction, revert=args.revert, clear_session=args.clear_session))
    elif args.cmd == "revlog":
        _out(_revlog(edit=args.edit, delete=args.delete, text=args.text))
    elif args.cmd == "builds":
        conn = connect()
        try:
            rows = conn.execute(
                """SELECT b.job_id, j.company, b.resume_title, b.completeness,
                          b.required_gaps, b.fill_report, b.built_at,
                          b.stage, b.stage_at, b.stage_error
                   FROM resume_builds b LEFT JOIN jobs j ON j.id = b.job_id
                   ORDER BY b.built_at DESC LIMIT ?""",
                (args.limit,),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            rep = json.loads(r["fill_report"] or "{}")
            out.append({
                "job_id": r["job_id"], "회사": r["company"],
                # 어디까지 갔나. 'filling'이 남아 있으면 채우다 끊긴 것이라
                # 다음 실행이 그 이력서를 버리고 새로 만든다.
                "단계": r["stage"], "단계시각": r["stage_at"],
                **({"단계오류": r["stage_error"][:120]} if r["stage_error"] else {}),
                "이력서": r["resume_title"], "완성도": r["completeness"],
                "필수미충족": r["required_gaps"],
                "저장안됨": rep.get("lost") or [],
                "플랫폼이 요구": rep.get("platform_todo") or [],
                "스킬누락": rep.get("skills_skipped") or [],
                "작성완료": rep.get("finalized"),
            })
        _out(out)
    elif args.cmd == "health":
        from src.autoapply import health

        _out(health.history() if args.history else health.run(notify=not args.no_notify))
    elif args.cmd == "status":
        _out(agent.status())
    elif args.cmd == "where":
        _out(describe())
    elif args.cmd == "telegram-setup":
        conn = connect()
        _out(telegram.setup(conn, args.token))
        conn.close()
    elif args.cmd == "telegram-commands":
        conn = connect()
        telegram.set_commands(conn)
        _out({"등록": [c for c, _ in telegram.BOT_COMMANDS]})
        conn.close()
    elif args.cmd == "notify-login":
        _out(agent.notify_login_required())
    elif args.cmd == "session-check":
        from src.autoapply.runner import probe

        result = probe.describe(args.platform)
        _out(result)
        if args.notify and any(v == "죽음" for v in result.values()):
            _out(agent.notify_login_required())
    elif args.cmd == "listen":
        from src.autoapply.notify import listener

        conn = connect()
        try:
            if args.watch:
                listener.watch(conn)
            else:
                _out(listener.drain(conn))
        finally:
            conn.close()
    elif args.cmd in ("improve", "plan"):
        # improve는 plan의 옛 이름이다. 승인 없이 곧장 코드를 고치던 경로가
        # 계획→(위험도)→반영 으로 바뀌었고, 두 이름을 둘 이유가 없어 합쳤다.
        from src.autoapply import orchestrator

        if args.list:
            conn = connect()
            _out(orchestrator.gather(conn))
            conn.close()
        else:
            _out(orchestrator.plan(limit=args.limit))
    elif args.cmd == "fix-run":
        from src.autoapply import orchestrator

        _out(orchestrator.execute(plan_id=args.plan_id))
    elif args.cmd == "errors":
        from src.autoapply import errors

        conn = connect()
        try:
            _out(errors.summary(conn, args.limit))
        finally:
            conn.close()
    elif args.cmd == "plans":
        conn = connect()
        try:
            _out([dict(r) for r in conn.execute(
                "SELECT id, created_at, title, risk, auto, status, branch, commit_sha "
                "FROM fix_plans ORDER BY id DESC LIMIT 12"
            )])
        finally:
            conn.close()
    elif args.cmd == "fix-revert":
        from src.autoapply import orchestrator

        conn = connect()
        try:
            _out(orchestrator.revert(conn, args.sha))
        finally:
            conn.close()
    elif args.cmd == "resumes":
        from src.autoapply.runner import resume_editor

        if args.cleanup:
            _out(resume_editor.cleanup(dry_run=False))
        else:
            _out({
                "보호(편집·삭제 금지)": sorted(resume_editor.protected_titles()),
                "정리 예정": resume_editor.cleanup(dry_run=True),
                "안내": "실제로 지우려면 --cleanup. 로컬 사본(profile/generated)은 남습니다",
            })
    elif args.cmd == "browser-login":
        from src.autoapply.runner import login

        _out(login())
    elif args.cmd == "capture":
        from src.autoapply.runner import capture

        _out(capture(_job(args.job_id), click=args.click))
    elif args.cmd == "resume":
        from src.autoapply import assemble

        result = assemble.build(args.job_id, max_rounds=args.rounds)
        body = result.pop("resume")
        _out(result)
        if args.print:
            print("\n" + body)
    elif args.cmd == "autoapply":
        _out(_autoapply(args.job_id, resume_url=args.resume_url, live=args.live))
    elif args.cmd == "cycle-apply":
        _out(_cycle_apply(args.limit, defer=args.defer))
    elif args.cmd == "flush-notify":
        _out(_flush_notifications())
    elif args.cmd == "night-cycle":
        _out(_night_cycle(args.target, defer=args.defer))
    elif args.cmd == "submit":
        _out(_submit(args.job_id))
    elif args.cmd == "apply":
        _out(_apply(args.job_id, live=args.live, headless=args.headless))
    return 0


def _night_cycle(target: int, *, defer: bool = False) -> dict:
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
    from src.autoapply.db import connect as _connect
    from src.autoapply.notify.listener import is_paused, pause_reason

    log = logging.getLogger(__name__)

    conn = _connect()
    try:
        if is_paused(conn):
            return {"skipped": f"일시정지 — {pause_reason(conn)}"}
    finally:
        conn.close()

    from src.autoapply import errors

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
        r = _cycle_apply(1, defer=defer)
        n = r.get("prepared", 0)
        if n == 0:
            log.info("night-cycle: 대기열 소진 (%d/%d 준비, %s)",
                      prepared, target, r.get("reason", "알 수 없음"))
            break
        attempted += n
        stall = False
        for it in r.get("items", []):
            # _report_prepared와 같은 기준으로 판정한다. apply 하위의 error를
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
        from src.autoapply import orchestrator
        conn = _connect()
        try:
            self_diagnosed = [it["title"] for it in orchestrator.self_items(conn)]
        finally:
            conn.close()
        if self_diagnosed:
            log.info("night-cycle: 자체진단 %d건 — improve 호출", len(self_diagnosed))
            from src.autoapply.paths import CODE_ROOT
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
        _report_circuit_break(tripped[0], tripped[1], prepared, attempted)
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


# 같은 오류가 이만큼 반복되면 대기열이 남아 있어도 접는다. 셋인 이유: 한 번은
# 그 공고만의 사정일 수 있고, 두 번은 우연일 수 있다. 세 번이면 다음 공고에서도
# 같은 자리에서 죽는다 — 그 시점부터 남은 대기열은 전부 같은 실패다.
CIRCUIT_BREAK_REPEATS = 3


def _report_circuit_break(key: str, count: int, prepared: int, attempted: int) -> None:
    """왜 대기열이 남았는데 멈췄는지 폰에 알린다.

    이 알림이 없으면 "목표 5건인데 2건만 하고 끝났다"가 대기열 소진과 구별되지
    않는다. 둘은 정반대다 — 하나는 할 일이 없는 것이고 하나는 망가진 것이다.
    """
    import html

    from src.autoapply.db import connect as _c
    from src.autoapply.notify import telegram

    conn = _c()
    try:
        telegram.notify(
            conn,
            f"🛑 <b>같은 오류가 {count}번 — 지원준비를 접습니다</b>\n\n"
            f"<i>{html.escape(key[:250])}</i>\n\n"
            f"준비 {prepared}건 / 시도 {attempted}건에서 멈췄습니다. "
            "대기열은 남아 있지만 다음 공고도 같은 자리에서 죽습니다.\n"
            "<i>고장 큐에 쌓였습니다 — <code>/errors</code> 로 보고 "
            "<code>/plan</code> 으로 수정 계획을 세울 수 있습니다.</i>",
        )
    finally:
        conn.close()


def _cycle_apply(limit: int, *, defer: bool = False) -> dict:
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
    from src.autoapply.db import connect as _connect
    from src.autoapply.notify.listener import is_paused, pause_reason

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
            r = _autoapply(t["job_id"], resume_url=None, live=False)
        except tasks.Cancelled as e:
            # 사람이 멈춘 것을 한 공고의 실패로 삼키면 다음 공고로 넘어간다 —
            # 멈추라고 한 사람 눈에는 그게 "안 멈춘다"다. 위로 올려 최상단이
            # "⏹ 중단"으로 답하게 한다.
            _note_stage_failure(t["job_id"], f"중단: {e}")
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
            _note_stage_failure(t["job_id"], r["error"])
        out.append({"job_id": t["job_id"], "company": t["company"], **r})
        _report_prepared(t, r, defer=defer)
    # prepared는 "몇 건을 손댔나"이지 "몇 건이 준비됐나"가 아니다 — night-cycle이
    # 이 값으로 대기열 소진만 판정하고, 성공 여부는 items를 보고 따로 센다.
    return {
        "prepared": len(out),
        "already_applied": sum(1 for r in out if r.get("already_applied")),
        "items": out,
    }


def _note_stage_failure(job_id: int, error: str) -> None:
    """단계 실패 기록은 절대 지원준비를 막지 않는다 — 기록하다 죽으면
    원래 실패가 무엇이었는지가 통째로 사라진다."""
    try:
        from src.autoapply import assemble

        assemble.note_failure(job_id, error)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).debug("단계 실패 기록 실패(무시): %s", e)


def _queue_notification(
    conn, *, job_id: int | None, caption: str, photo_path: str | None, buttons: list | None
) -> None:
    """지금 보내지 않고 쌓아둔다. `flush-notify`가 나중에 순서대로 보낸다."""
    conn.execute(
        "INSERT INTO pending_notifications (job_id, caption, photo_path, buttons, created_at) "
        "VALUES (?,?,?,?,?)",
        (job_id, caption, photo_path, json.dumps(buttons) if buttons else None, now()),
    )
    conn.commit()


def _flush_notifications() -> dict:
    """쌓인 지원 준비 알림을 순서대로 보낸다. 새벽 루프가 만든 것을 아침에 한 번에 본다.

    사진이 없으면 텍스트로만 보낸다 — `send_photo`가 실패(파일 삭제 등)해도
    캡션은 반드시 도착해야 무엇이 있었는지 안다.
    """
    from src.autoapply.notify import telegram

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM pending_notifications WHERE sent_at IS NULL ORDER BY id"
        ).fetchall()
        sent = 0
        for r in rows:
            buttons = json.loads(r["buttons"]) if r["buttons"] else None
            ok = bool(
                r["photo_path"]
                and telegram.send_photo(conn, r["photo_path"], r["caption"], buttons)
            )
            if not ok:
                # 버튼을 떨어뜨리면 안 된다. 승인 요청이 새벽에 쌓였다가
                # 아침에 **누를 것 없이** 도착한다 — 사진이 없는 알림(수정 계획
                # 승인 등)이 정확히 그 경우다.
                if buttons:
                    telegram.notify_with_buttons(conn, r["caption"], buttons)
                else:
                    telegram.notify(conn, r["caption"])
            conn.execute(
                "UPDATE pending_notifications SET sent_at=? WHERE id=?", (now(), r["id"])
            )
            sent += 1
        conn.commit()
        if sent:
            telegram.notify(conn, f"🌙 새벽 사이에 {sent}건 준비됐습니다 — 위에서부터 검토해주세요.")
        return {"sent": sent}
    finally:
        conn.close()


def _report_prepared(target: dict, result: dict, *, defer: bool = False) -> None:
    """준비 결과를 폰으로 보낸다(또는 defer=True면 나중에 보낼 큐에 쌓는다).

    성공이면 **스크린샷**, 실패면 이유. 실패가 로그에만 남으면 아침에 로그를
    뒤져야 안다 — 실제로 06시 사이클이 클릭 타임아웃으로 통째로 실패했는데
    그렇게 발견했다. 무인 운영에서 "아무 일도 안 일어남"과 "망가져서 못 함"은
    겉보기에 같다.
    """
    from src.autoapply.db import connect as _c
    from src.autoapply.notify import telegram

    conn = _c()
    try:
        head = f"{target['fit_score']}점 · {target['company']} — {target['title'][:40]}"
        apply_res = result.get("apply") or {}
        err = result.get("error") or apply_res.get("error") or result.get("stopped")

        # 이미 지원한 자리는 **실패가 아니다.** 같은 붉은 글씨로 보내면 사람이
        # 고쳐야 할 것과 그냥 넘어간 것이 구분되지 않는다. 사진도 버튼도 없이
        # 한 줄만 보낸다 — 판단할 게 없는 알림이다.
        if result.get("already_applied"):
            caption = (
                f"⏭ <b>이미 지원한 공고</b>\n{head}\n"
                "<i>예전에 직접 지원하신 자리입니다. 준비하지 않고 대기열에서 뺐습니다.</i>"
            )
            if defer:
                _queue_notification(
                    conn, job_id=target.get("job_id"), caption=caption,
                    photo_path=None, buttons=None,
                )
            else:
                telegram.notify(conn, caption)
            return

        if err:
            caption = f"❌ <b>지원 준비 실패</b>\n{head}\n<i>{str(err)[:200]}</i>"
            if defer:
                _queue_notification(
                    conn, job_id=target.get("job_id"), caption=caption,
                    photo_path=None, buttons=None,
                )
            else:
                telegram.notify(conn, caption)
            return

        # 폰으로 보내는 사진은 실제 원티드 화면이어야 한다. 로컬에서 그려낸
        # 이미지는 실제로 어떻게 보일지 안 알려준다 — 사람이 판단하는 건
        # "이게 정말 이렇게 나갈까"이지 우리가 그린 문서가 아니다.
        #
        # 편집기 화면(resume.shot)을 우선한다 — 새로고침 후(=저장 확인 후)
        # 찍은 것이라 실제로 저장된 내용이 보인다. 그게 없으면(스크린샷 실패
        # 등) 지원 폼 화면(apply.evidence)으로 대신한다 — 그것도 실제 화면이다.
        shot = (result.get("resume") or {}).get("shot") or apply_res.get("evidence")

        # 폰으로 보내기 전에 화면을 한 번 읽는다. 사람이 사진을 보고 판단하는
        # 것과 같은 층위를 기계가 먼저 훑어, 명백한 문제는 캡션에 적어 보낸다.
        verdict = ""
        if shot:
            from src.autoapply import vision

            # 무엇을 보라고 할지가 중요하다. 전체 페이지 스크린샷에서 우측
            # 지원 패널은 작게 잡히므로, 개별 입력값까지 확인하라고 하면
            # "안 보인다"는 오탐이 난다. 사람이 사진으로 판단할 수 있는 수준 —
            # 제출 버튼이 눌릴 상태인가, 이력서가 골라졌나 — 만 묻는다.
            v = vision.verify(
                shot,
                "이력서 문서: 이름·간단 소개·경력·학력·스킬이 채워져 있고, "
                "문장이 중간에 끊기거나 빈 섹션이 없어야 한다.",
                context="지원에 제출될 이력서 문서",
                job_id=target.get("job_id"),
            )
            if v["ok"] is False and v["issues"]:
                verdict = "\n⚠️ " + "\n⚠️ ".join(i.lstrip("- ")[:70] for i in v["issues"][:3])
            elif v["ok"]:
                verdict = "\n✅ 화면 점검 이상 없음"

        from src.autoapply import assemble as _asm

        reg = _asm.registration(target["job_id"], conn)
        links = []
        if reg.get("resume_url"):
            links.append(f'📝 <a href="{reg["resume_url"]}">이력서 보기</a>')
        if target.get("url"):
            links.append(f'🔗 <a href="{target["url"]}">공고 보기</a>')

        # 이력서 본문으로는 못 하는 요구(성적증명서 첨부, 포트폴리오 파일 별도
        # 업로드, 이메일 송부 등)는 여기서만 알린다. 본문에 적으면 하지 않은 일을
        # 했다고 쓰는 것이 되고, 승인 화면 밖에서는 사람이 볼 자리가 없다.
        caption = (
            f"📄 <b>지원 준비됨</b>\n{head}{verdict}"
            + _asm.review_block(_asm.review_notes(target["job_id"], conn))
            + _asm.todo_block(_asm.manual_todos(target["job_id"], conn))
            + "\n\n"
            + ("  ·  ".join(links) + "\n\n" if links else "")
            + "<i>이력서는 이미 만들어져 있습니다. 승인하면 그대로 제출합니다.</i>"
        )
        # 세 갈래다. '건너뛰기'만 있던 때는 같은 공고가 다음 사이클에 또 올라와
        # 같은 판단을 반복하게 했다. 거절에도 종류가 있다 —
        #   폐기   이 자리는 아니다. 다시 올리지 마라
        #   수정   자리는 맞는데 내용이 아니다. 고쳐서 다시 가져와라
        buttons = [
            [{"text": "✅ 승인 (제출)", "callback_data": f"submit:{target['job_id']}"}],
            [
                {"text": "🗑 폐기", "callback_data": f"drop:{target['job_id']}"},
                {"text": "✏️ 수정요청", "callback_data": f"revise:{target['job_id']}"},
            ],
        ]
        if defer:
            _queue_notification(
                conn, job_id=target.get("job_id"), caption=caption, photo_path=shot,
                buttons=buttons,
            )
        elif not (shot and telegram.send_photo(conn, shot, caption, buttons)):
            telegram.notify(conn, caption)
    finally:
        conn.close()


def _submit(job_id: int) -> dict:
    """준비 때 등록해둔 이력서로 제출만 한다.

    승인 시점에 다시 조립하지 않는 이유: 사람이 검토한 것과 나가는 것이
    달라진다. 조립은 준비 단계에서 끝났고, 여기서는 그 이력서를 고를 뿐이다.
    """
    from src.autoapply import assemble
    from src.autoapply.runner import resume_editor

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

    job = _job(job_id, resume_title=reg["resume_title"])
    return _apply_with(job, live=True)


def _browser_open() -> dict:
    """상주 브라우저를 띄우고 **그대로 둔다.**

    이걸 한 번 돌리고 창을 숨기면(⌘H) 그 뒤로는 새 창이 뜨지 않는다.
    이후 모든 작업은 이 창에 붙어서 돈다.

    headless로는 못 한다 — 원티드가 CloudFront 단에서 403을 준다(실측).
    창을 화면 밖으로 보내는 것도 macOS가 되돌린다. 남은 답이 이것이다.
    """
    from src.autoapply.runner.session import CDP_URL, _spawn_resident, resident_owner

    who, pid, cmd = resident_owner()
    if who == "ours":
        return {"이미 떠 있음": CDP_URL, "할 일": "없음 — 그대로 쓰면 된다"}
    if who == "foreign":
        # 우리 것이 아닌 크롬이 포트를 잡고 있으면 붙지도, 죽이지도 않는다.
        # 사람이 쓰는 창일 수 있다 — 실제로 그 창에서 우리 작업이 돌아 사고가 났다.
        return {
            "막힘": f"CDP {CDP_URL}를 다른 브라우저가 잡고 있다 (pid {pid})",
            "명령줄": cmd[:200],
            "할 일": "그 크롬을 닫고 다시 실행하세요. 닫기 전까지는 실행마다 새 창이 뜹니다.",
        }

    if _spawn_resident():
        return {"띄웠다": CDP_URL,
                "다음": "이 창을 ⌘H로 숨기세요. 앞으로 새 창은 안 뜹니다."}
    return {"실패": "브라우저가 20초 안에 안 떴다"}


def _revise(job_id: int, feedback: str) -> dict:
    """사람의 수정 요청을 반영해 다시 만들고, 검토를 다시 요청한다.

    캐시를 건너뛰고 조립부터 다시 한다 — 피드백은 입력이 바뀐 것이라서
    캐시된 결과를 재사용하면 요청이 반영되지 않는다.

    앞서 만든 이력서는 플랫폼에 그대로 두고 새로 만든다. 지우려면 브라우저를
    한 번 더 띄워야 하고, 그 사이에 실패하면 아무것도 없는 상태가 된다.
    정리는 `resumes --cleanup`이 나중에 한다.
    """
    from src.autoapply import assemble

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
        _tell(f"📒 <b>원장 기록</b>\n<code>{html.escape(entry)}</code>\n"
              f"<i>틀렸으면 {assemble.REVISION_LOG.name} 에서 그 줄을 고치세요.</i>")
    except Exception as e:  # noqa: BLE001
        log.warning("원장 기록 실패(재작성은 계속): %s", e)

    built = assemble.build_editor_json(job_id, feedback=feedback)
    if not built["ok"]:
        _tell(f"❌ <b>재작성 중단</b> — 공고 {job_id}\n"
              f"필수요건 미충족 {built['required_gaps']}건\n"
              "<i>수정 요청이 사실 저장소에 없는 내용을 요구했을 수 있습니다.</i>")
        return {"stopped": "재작성 후에도 필수요건 미충족", "gaps": built["required_gaps"]}

    # **이어받지 않는다.** 재작성은 "이번엔 다르게 써 달라"는 요청이므로,
    # 지난번에 등록해둔 이력서를 재사용하면 지시가 통째로 무시된 채 옛 이력서가
    # "재작성됨"으로 폰에 다시 올라간다 — 사람은 고쳐진 줄 알고 승인한다.
    result = _autoapply(job_id, resume_url=None, live=False, reuse=False)
    target = _job(job_id)
    target["fit_score"] = target.get("fit_score") or 0
    _report_prepared(
        {**target, "job_id": job_id, "fit_score": _fit_score(job_id)},
        result,
    )
    return {"revised": job_id, "resume": result.get("resume")}


def _guide(instruction: str, *, revert: bool, clear_session: bool = False) -> dict:
    """작성 가이드를 고치거나, 마지막 백업으로 되돌리거나, 이어가던 대화를 끊는다.

    텔레그램 수신 루프가 서브프로세스로 이 커맨드를 부른다 — Opus 5 호출은
    수 분 걸릴 수 있어 수신을 막으면 안 되기 때문이다. 그래서 결과는
    반환값이 아니라 폰으로 보낸다; 반환값은 터미널에서 직접 돌릴 때를 위한 것.
    """
    from src.autoapply import guide

    if clear_session:
        result = guide.clear_session()
        _tell(_guide_message(result, revert=False, clear_session=True))
        return result

    result = guide.revert() if revert else guide.edit(instruction)
    _tell(_guide_message(result, revert=revert, clear_session=False))
    return result


def _guide_message(result: dict, *, revert: bool, clear_session: bool = False) -> str:
    if clear_session:
        return ("🧹 가이드 대화를 끊었습니다. 다음 지시는 새 대화로 시작합니다."
                if result.get("cleared") else "가이드 대화가 이미 비어 있습니다.")

    if revert:
        if result.get("ok"):
            return f"↩️ 가이드를 되돌렸습니다 — {html.escape(result['restored'])}"
        return f"❌ 되돌리기 실패 — {html.escape(result.get('reason', ''))}"

    if not result.get("ok"):
        return f"❌ <b>가이드 수정 실패</b>\n{html.escape(result.get('reason', ''))}"

    lines = [f"✅ <b>가이드 수정 {result['edits']}건</b>"]
    lines += [f"· {html.escape(w)}" for w in result.get("why", [])]
    if result.get("note"):
        lines.append(f"\n📝 {html.escape(result['note'])}")
    if result.get("diff"):
        lines.append(f"\n<pre>{html.escape(result['diff'][:1200])}</pre>")
    lines.append("\n되돌리려면: <code>/guide 되돌리기</code>")
    return "\n".join(lines)


def _revlog(*, edit: int | None, delete: int | None, text: str) -> dict:
    """원장 항목을 본다 / 고친다 / 지운다. LLM을 거치지 않는다 — 요약이 지시를
    잘못 옮겼을 때 파일을 열지 않고 바로 바로잡을 수 있어야 한다."""
    from src.autoapply import assemble

    if delete is not None:
        old = assemble.log_edit(delete, None)
        return {"deleted": delete, "was": old}

    if edit is not None:
        old = assemble.log_edit(edit, text)
        entries = assemble.log_entries()
        now_line = entries[edit - 1] if 1 <= edit <= len(entries) else ""
        return {"edited": edit, "was": old, "now": now_line}

    entries = assemble.log_entries()
    return {"entries": entries, "count": len(entries)}


def _fit_score(job_id: int) -> int:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT fit_score FROM screening WHERE job_id=?", (job_id,)
        ).fetchone()
        return int(row["fit_score"]) if row and row["fit_score"] is not None else 0
    finally:
        conn.close()


def _tell(text: str) -> None:
    """폰으로 한 줄 보낸다. 실패해도 흐름을 멈추지 않는다."""
    from src.autoapply.notify import telegram

    conn = connect()
    try:
        telegram.notify(conn, text)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("알림 실패: %s", e)
    finally:
        conn.close()


def _llm_cost(job_id: int | None) -> dict:
    """LLM 호출 비용을 job_id로 묶어 보여준다.

    write·review·to_editor_json·portfolio_match·summary_ensure가 로그
    파일 여기저기 흩어져 있어, "이 공고 하나에 LLM을 얼마나 썼나"를
    답하려면 이 집계가 필요하다(`llm_calls` 테이블, `llm.py _log_cost` 참고).
    """
    conn = connect()
    try:
        if job_id is not None:
            rows = conn.execute(
                """SELECT phase, COUNT(*) AS calls,
                          SUM(COALESCE(input_tokens,0) + COALESCE(cache_read_tokens,0)
                              + COALESCE(cache_write_tokens,0)) AS in_tok,
                          SUM(COALESCE(cache_read_tokens,0)) AS cr,
                          SUM(COALESCE(cache_write_tokens,0)) AS cw,
                          SUM(output_tokens) AS out_tok, SUM(cost_usd) AS cost
                   FROM llm_calls WHERE job_id=? GROUP BY phase ORDER BY phase""",
                (job_id,),
            ).fetchall()
            if not rows:
                return {"job_id": job_id, "안내": "기록 없음 — 아직 조립 안 했거나 이 기능 이전 데이터"}
            total = conn.execute(
                """SELECT COUNT(*) AS calls,
                          SUM(COALESCE(input_tokens,0) + COALESCE(cache_read_tokens,0)
                              + COALESCE(cache_write_tokens,0)) AS in_tok,
                          SUM(COALESCE(cache_read_tokens,0)) AS cr,
                          SUM(COALESCE(cache_write_tokens,0)) AS cw,
                          SUM(output_tokens) AS out_tok, SUM(cost_usd) AS cost
                   FROM llm_calls WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            return {
                "job_id": job_id,
                "phases": [
                    {"phase": r["phase"], "호출": r["calls"], "입력토큰": r["in_tok"],
                     "캐시읽기": r["cr"], "캐시쓰기": r["cw"],
                     "출력토큰": r["out_tok"], "비용_usd": round(r["cost"] or 0, 4)}
                    for r in rows
                ],
                "합계": {"호출": total["calls"], "입력토큰": total["in_tok"],
                        "캐시읽기": total["cr"], "캐시쓰기": total["cw"],
                        "출력토큰": total["out_tok"], "비용_usd": round(total["cost"] or 0, 4)},
                # 입력 토큰은 단가가 셋으로 갈린다(기본 입력 / 캐시 쓰기 / 캐시 읽기).
                # 그래서 토큰 합으로 비용을 되짚지 말고 비용은 cost_usd를 본다.
                "주의": "입력토큰은 캐시읽기·캐시쓰기를 포함한 합. 단가가 달라 토큰으로 비용을 환산하면 안 된다",
            }

        rows = conn.execute(
            """SELECT l.job_id, j.company, j.title, COUNT(*) AS calls,
                      SUM(COALESCE(l.input_tokens,0) + COALESCE(l.cache_read_tokens,0)
                          + COALESCE(l.cache_write_tokens,0)) AS in_tok,
                      SUM(l.output_tokens) AS out_tok,
                      SUM(l.cost_usd) AS cost, MAX(l.called_at) AS last
               FROM llm_calls l LEFT JOIN jobs j ON j.id = l.job_id
               WHERE l.job_id IS NOT NULL
               GROUP BY l.job_id ORDER BY last DESC LIMIT 20"""
        ).fetchall()
        return {
            "최근_20건": [
                {"job_id": r["job_id"], "회사": r["company"], "공고": r["title"],
                 "호출": r["calls"], "입력토큰": r["in_tok"], "출력토큰": r["out_tok"],
                 "비용_usd": round(r["cost"] or 0, 4)}
                for r in rows
            ],
        }
    finally:
        conn.close()


def _resume_title(job: dict) -> str:
    """공고별 이력서 제목. 제목이 곧 지원 폼에서 이력서를 고르는 열쇠다.

    회사명을 앞에 둔다. 목록에서 사람이 훑을 때 '어느 자리에 낸 것'인지가
    먼저 보여야 하기 때문이다. 50자 제한이 있어 뒤를 자른다.
    """
    company = (job.get("company") or "").strip()
    title = (job.get("title") or "").strip()
    name = f"{company} {title}".strip() or "박예일 이력서"
    return name[:50]


def _autoapply(job_id: int, *, resume_url: str | None, live: bool, reuse: bool = True) -> dict:
    """조립 → 등록 → 지원. 각 단계가 다음 단계의 입력을 만든다.

    이력서 제목이 고리다. 편집기에 채운 뒤 그 제목을 읽어 지원 레시피에 넘기므로,
    `config.applicability.resumes` 매핑이 더는 필요 없다 — 트랙별로 미리 만들어둔
    이력서를 고르는 게 아니라 공고마다 만들기 때문이다.

    조립이 `ok=False`(필수요건 근거 없음/검수 반려)면 여기서 멈춘다. 맞지 않는
    자리에 이름을 남기는 것보다 안 내는 게 낫다.
    """
    from src.autoapply import assemble
    from src.autoapply.runner import resume_editor
    from src.autoapply.runner.lock import browser_lock

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
    already = _skip_if_already_applied(job_id)
    if already:
        return already

    # 지난번에 어디까지 갔나. 끊긴 자리에서 이어받는다 — 처음부터 다시 하면
    # 조립(LLM)을 또 하고, 더 나쁘게는 **같은 공고용 이력서를 하나 더 만든다.**
    # 자세한 재사용 규칙은 assemble.progress의 주석에 있다.
    prog = assemble.progress(job_id) if reuse else {"resumable": False}
    if prog["resumable"]:
        log = logging.getLogger(__name__)
        log.info("공고 %s — %s 단계에서 이어받는다 (이력서 %r 재사용, 새로 안 만든다)",
                 job_id, prog["stage"], prog["resume_title"])
        tasks.check("지원 폼 진입 전")
        job = _job(job_id, resume_title=prog["resume_title"], require_resume=True)
        with browser_lock("지원준비", label=f"공고 {job_id}"):
            result = _apply_with(job, live=live)
        assemble.set_stage(job_id, "prepared")
        reg = assemble.registration(job_id)
        return {
            "company": _job(job_id).get("company"),
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
    job_row = _job(job_id)
    template = resume_editor.pick_template(job_row)
    new_title = _resume_title(job_row)
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
        job = _job(job_id, resume_title=title, require_resume=True)
        result = _apply_with(job, live=live)
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


def _skip_if_already_applied(job_id: int) -> dict | None:
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
    from src.autoapply.runner import apply as apply_mod
    from src.autoapply.runner.lock import BrowserBusy

    log = logging.getLogger(__name__)
    job = _job(job_id)
    try:
        state = apply_mod.preflight(job)
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
        "company": job.get("company"),
        "url": job.get("url"),
        "기록": "apply_ledger status=external (대기열에서 영구 제외, 제출 건수·한도에는 안 셈)",
    }


def _job(job_id: int, *, resume_title: str | None = None, require_resume: bool = False) -> dict:
    """레시피가 쓸 공고 정보. 이력서 제목까지 붙여서 돌려준다.

    require_resume는 **제출 직전에만** 켠다. 이력서를 만들기 전에 부를 때는
    제목이 아직 없는 게 정상이라, 여기서 멈추면 만들 기회 자체가 없어진다.
    """
    from src.autoapply.config import effective_config

    from src.autoapply import portfolio as portfolio_match

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


def _apply(job_id: int, *, live: bool, headless: bool) -> dict:
    """dry-run은 선점하지 않는다. 아무것도 제출하지 않으니 자리를 잡을 이유가 없고,
    잡으면 상한만 태우고 그 자리를 다시 못 건드리게 된다.

    live일 때만 선점한다 — 그리고 선점이 실패하면(중복이거나 상한) 실행하지 않는다.
    """
    return _apply_with(_job(job_id, require_resume=True), live=live, headless=headless)


def _notify_submitted(job: dict, result: dict) -> None:
    """제출 결과를 폰으로 알린다. 버튼을 눌렀는데 아무 소식이 없으면
    다시 누르게 되고, 그건 중복지원 시도가 된다."""
    from src.autoapply.db import connect as _c
    from src.autoapply.notify import telegram

    conn = _c()
    try:
        text = (
            f"✅ <b>제출 완료</b>\n{job.get('company')} — {str(job.get('title'))[:40]}\n"
            f"<i>원장에 기록됨. 같은 자리는 다시 나가지 않습니다.</i>"
        )
        shot = result.get("evidence")
        if not (shot and telegram.send_photo(conn, shot, text)):
            telegram.notify(conn, text)
    finally:
        conn.close()


def _apply_with(job: dict, *, live: bool, headless: bool = False) -> dict:
    """공고 정보가 이미 준비된 경우의 지원 실행. autoapply 체인이 이걸 쓴다."""
    from src.autoapply.runner import LoginRequired, run

    job_id = job["job_id"]
    if not live:
        return run(job, live=False, headless=headless)

    # 제출 직전에 한 번 더 본다. 승인 버튼은 몇 시간 전 알림에서도 눌릴 수 있고,
    # 그 사이 사람이 원티드에서 직접 냈을 수 있다. 선점(claim) **전에** 봐야
    # 한다 — 선점부터 하면 그 자리가 오늘 한도 한 칸을 먹는다.
    already = _skip_if_already_applied(job_id)
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
        _notify_submitted(job, result)
        _delete_submitted_resume(job_id, job.get("resume") or "")
    else:
        # 눌렀는데 완료 화면을 못 봤다. 실제로 접수됐을 수 있으므로 자리를 놓지 않는다.
        agent.mark_failed(ledger, result["error"] or "제출 확인 실패", release=False)
    result["ledger"] = ledger
    return result


def _delete_submitted_resume(job_id: int, title: str) -> None:
    """제출 직후 그 이력서를 플랫폼에서 지운다. 지원이력에서 여전히 접근
    가능하므로 목록에 남겨둘 이유가 없다 — 남겨두면 max_keep을 금방 채운다.

    로컬 사본이 있을 때만 지운다. 없으면 그게 유일한 기록이라
    `resume_editor.cleanup()`과 같은 규칙으로 건드리지 않는다. 실패해도 지원
    자체는 이미 끝났으므로 흐름을 막지 않는다 — 다음 `resumes --cleanup`이
    나이·개수 기준으로 결국 치운다.
    """
    from src.autoapply.paths import RESUME_OUT_DIR

    log = logging.getLogger(__name__)
    if not title:
        return
    if not list(RESUME_OUT_DIR.glob(f"{job_id}-*.json")):
        log.info("로컬 사본이 없어 제출 이력서를 남겨둔다: %s", title)
        return
    try:
        from src.autoapply.runner import resume_editor

        deleted = resume_editor.delete_after_submit(title)
        log.info("제출 이력서 삭제 %s: %s", "성공" if deleted else "실패", title)
    except Exception as e:  # noqa: BLE001
        log.warning("제출 이력서 삭제 실패(무시, 다음 정리가 치운다): %s", e)


if __name__ == "__main__":
    # 텔레그램이 이 CLI를 subprocess.Popen(stdout=DEVNULL, stderr=STDOUT)로
    # 띄운다 — 수신 루프를 막지 않으려고 fire-and-forget으로 돌리는데, 그 말은
    # 죽어도 어디에도 안 남는다는 뜻이다. 실제로 그렇게 사라진 실패가 있었다
    # (/guide 요청이 확인 메시지만 가고 결과가 안 옴 — 원인을 로그에서 못 찾음).
    # 여기서 잡아 최소한 "뭔가 죽었다"는 사실만이라도 폰으로 남긴다.
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except tasks.Cancelled as e:  # 사람이 멈춘 것 — 고장이 아니다
        try:
            _tell(f"⏹ <b>중단</b> — <code>{html.escape(' '.join(sys.argv[1:])[:100])}</code>\n"
                  f"<i>{html.escape(str(e)[:200])}</i>")
        except Exception:  # noqa: BLE001
            pass
        print(json.dumps({"cancelled": str(e)}, ensure_ascii=False, indent=2))
        raise SystemExit(0) from None
    except Exception as e:  # 브라우저 경합은 고장이 아니다 — 안내하고 조용히 끝낸다
        from src.autoapply.runner.lock import BrowserBusy

        if not isinstance(e, BrowserBusy):
            raise
        cmd = " ".join(sys.argv[1:])[:150]
        try:
            _tell(
                f"🔒 <b>브라우저가 사용 중</b> — <code>{html.escape(cmd)}</code>는 시작하지 못했습니다.\n"
                f"지금 도는 작업: <i>{html.escape(str(e))}</i>\n\n"
                "끝나면 다시 눌러주세요. 지금 멈추려면 <code>/stop</code>."
            )
        except Exception:  # noqa: BLE001
            pass
        print(json.dumps({"skipped": "브라우저 사용 중", "holder": str(e)},
                         ensure_ascii=False, indent=2))
        raise SystemExit(0) from None
    except BaseException as e:  # noqa: BLE001
        # 여기가 모든 명령의 마지막 그물이다. 예전에는 폰으로 한 줄 보내고
        # 끝이라 그 메시지를 놓치면 고장이 어디에도 안 남았다. 이제 큐에
        # 적는다 — errors.record()가 분류(바깥 사정 / 우리 문제)와 알림까지
        # 맡고, 우리 문제면 계획 수립을 발동한다.
        cmd = " ".join(sys.argv[1:])[:150]
        try:
            from src.autoapply import errors

            conn = connect()
            try:
                errors.record(conn, kind="cli", exc=e, command=cmd)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            # 기록조차 실패하면 최소한 폰에는 남긴다. 여기서 또 터지면
            # 원래 고장이 무엇이었는지가 통째로 사라진다.
            try:
                _tell(
                    f"❌ <b>cli.py 처리 안 된 오류</b>\n<code>{html.escape(cmd)}</code>\n"
                    f"<i>{type(e).__name__}: {str(e)[:300]}</i>"
                )
            except Exception:  # noqa: BLE001
                pass
        raise
