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

from src.autoapply import agent, pipeline
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

    sub.add_parser("browser-open", help="상주 브라우저 창을 띄운다 (한 번만 하면 된다)")

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
    tsp.add_argument("token", help="@BotFather에게 받은 봇 토큰")

    sub.add_parser("notify-login", help="세션 끊김 알림 수동 트리거 (쿨다운 무시하지 않음)")
    lp = sub.add_parser("listen", help="폰에서 온 메시지 처리 (운영 명령 + 개발 지시 접수)")
    lp.add_argument(
        "--watch", action="store_true",
        help="상시 대기하며 즉시 응답한다 (롱폴링). 없으면 한 번 훑고 끝낸다",
    )

    cq = sub.add_parser("improve", help="자기개선 오케스트레이터 — 전용 브랜치에서만 작업")
    cq.add_argument("--limit", type=int, default=1)
    cq.add_argument("--list", action="store_true", help="할 일만 보고 실행하지 않는다")

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

    if args.cmd == "scrape":
        session_ok = {}
        if args.check_session:
            from src.autoapply.runner import check_all

            session_ok.update(check_all(args.platform))
        # 수동 지정이 탐침 결과를 덮는다. 사람이 아는 게 더 정확한 경우가 있다.
        for item in args.session:
            k, _, v = item.partition("=")
            session_ok[k.strip()] = v.strip() not in ("0", "false", "no", "")
        _out(pipeline.run_all(args.platform, session_ok or None))
        agent.notify_login_required()  # 세션 끊긴 플랫폼이 있으면 알린다 (쿨다운 적용)

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
    elif args.cmd == "browser-open":
        _out(_browser_open())
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
                          b.required_gaps, b.fill_report, b.built_at
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
    elif args.cmd == "improve":
        from src.autoapply import orchestrator

        if args.list:
            conn = connect()
            _out(orchestrator.gather(conn))
            conn.close()
        else:
            _out(orchestrator.run(limit=args.limit))
    elif args.cmd == "resumes":
        from src.autoapply.db import connect as _c, get_setting
        from src.autoapply.runner import resume_editor

        conn = _c()
        preview = get_setting(conn, "preview_resume_url", "")
        conn.close()
        if args.cleanup:
            _out(resume_editor.cleanup(dry_run=False))
        else:
            _out({
                "미리보기용": preview or "미지정",
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
    """수집 한 번 → 지원준비(dry-run)를 목표건수 또는 대기열 소진까지 반복한다.

    수집을 준비마다 반복하지 않는다. 같은 새벽 시간대엔 새 공고가 계속 올라오지
    않으므로, 한 번 모아둔 대기열을 `next_targets`가 더 못 주는 시점이 곧
    "더 지원할 게 없다"는 판정이다 — 거기서 멈춘다.

    `cycle-apply`를 limit=1로 반복 호출하는 이유: 그쪽이 이미 스킵·재사용·
    알림 로직을 갖고 있다. 여기서 다시 구현하면 두 경로가 갈라져 한쪽만
    고치는 버그가 난다.
    """
    from src.autoapply import health
    from src.autoapply.db import connect as _connect
    from src.autoapply.notify.listener import is_paused
    from src.autoapply.runner import check_all

    log = logging.getLogger(__name__)

    conn = _connect()
    try:
        if is_paused(conn):
            return {"skipped": "일시정지 상태 (텔레그램 /resume 으로 해제)"}
    finally:
        conn.close()

    session_ok = check_all()
    scraped = pipeline.run_all(session_ok=session_ok)
    agent.notify_login_required()
    findings = health.run().get("findings", [])

    prepared, attempted, items = 0, 0, []
    seen_ids: set[int] = set()
    while prepared < target:
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
            items.append({"job_id": it["job_id"], "company": it.get("company"), "ok": ok})
            # 실패한 건이 24시간 스킵 보호를 못 받는 경우(빌드 자체가 조기에
            # 죽어 resume_builds에 안 남는 경우)가 있다 — 그러면 next_targets가
            # 같은 job_id를 계속 다시 준다. 같은 id가 두 번 나오면 무한루프
            # 신호이므로 즉시 멈춘다. 대기열 소진과 달리 이건 "고장"이다.
            if it["job_id"] in seen_ids:
                log.warning("night-cycle: 공고 %s가 반복돼 멈춘다 (24시간 스킵 실패로 추정)",
                            it["job_id"])
                stall = True
            seen_ids.add(it["job_id"])
        if stall:
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
            import subprocess
            from src.autoapply.paths import CODE_ROOT
            subprocess.Popen(
                [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "improve", "--limit", "1"],
                cwd=str(CODE_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("자체진단 확인 실패(무시): %s", e)

    return {
        "target": target,
        "prepared": prepared,
        "attempted": attempted,
        "stopped_reason": "목표 도달" if prepared >= target else "대기열 소진",
        "scraped": scraped,
        "health_findings": findings,
        "self_diagnosed": self_diagnosed,
        "items": items,
    }


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
    from src.autoapply.notify.listener import is_paused

    conn = _connect()
    try:
        if is_paused(conn):
            return {"skipped": "일시정지 상태 (텔레그램 /resume 으로 해제)"}
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
            # 무인 사이클에서는 미리보기용 이력서 하나를 덮어쓰며 재사용한다.
            #
            # 사본 경로가 들어온 뒤로 '작성 완료'는 정상 동작한다(공고 1025가
            # `#9` 제목으로 실제 제출됨). 그래도 재사용하는 이유는 남았다:
            # dry-run은 사람이 사진으로 판단하는 용도라 제출물이 아니고,
            # 건마다 새로 만들면 **승인되지 않은 공고의 이력서까지 계정에 쌓인다.**
            # 제출은 승인 시점에 그 공고용으로 다시 채운다(`autoapply --live`).
            r = _autoapply(t["job_id"], resume_url=_preview_resume_url(), live=False)
        except Exception as e:  # noqa: BLE001
            r = {"error": f"{type(e).__name__}: {e}"}
        out.append({"job_id": t["job_id"], "company": t["company"], **r})
        _report_prepared(t, r, defer=defer)
    return {"prepared": len(out), "items": out}


def _preview_resume_url() -> str | None:
    """미리보기에 재사용할 이력서 URL. 없으면 None(새로 만든다).

    건마다 새 이력서를 만들면 원티드 계정에 쌓인다. 미리보기는 사람이 사진으로
    보고 판단하는 용도라 하나를 덮어쓰며 재사용해도 된다 — **제출 시점에
    그 공고용으로 다시 채우기 때문이다**(`autoapply --live`).
    """
    from src.autoapply.db import connect as _c, get_setting

    conn = _c()
    try:
        return get_setting(conn, "preview_resume_url", "") or None
    finally:
        conn.close()


def _remember_preview_resume(url: str) -> None:
    from src.autoapply.db import connect as _c, get_setting, set_setting

    conn = _c()
    try:
        if url and not get_setting(conn, "preview_resume_url", ""):
            set_setting(conn, "preview_resume_url", url)
    finally:
        conn.close()


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

        caption = (
            f"📄 <b>지원 준비됨</b>\n{head}{verdict}\n\n"
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

    reg = assemble.registration(job_id)
    if not reg.get("resume_title"):
        return {"stopped": "등록된 이력서가 없다 — 먼저 준비(cycle-apply)해야 한다"}

    job = _job(job_id, resume_title=reg["resume_title"])
    return _apply_with(job, live=True)


def _browser_open() -> dict:
    """상주 브라우저를 띄우고 **그대로 둔다.**

    이걸 한 번 돌리고 창을 숨기면(⌘H) 그 뒤로는 새 창이 뜨지 않는다.
    이후 모든 작업은 이 창에 붙어서 돈다.

    headless로는 못 한다 — 원티드가 CloudFront 단에서 403을 준다(실측).
    창을 화면 밖으로 보내는 것도 macOS가 되돌린다. 남은 답이 이것이다.
    """
    import subprocess

    from src.autoapply.runner.session import CDP_URL
    from src.autoapply.paths import CODE_ROOT

    import httpx

    try:
        httpx.get(f"{CDP_URL}/json/version", timeout=2)
        return {"이미 떠 있음": CDP_URL, "할 일": "없음 — 그대로 쓰면 된다"}
    except Exception:  # noqa: BLE001
        pass

    # 자식 프로세스로 띄우고 이 명령은 바로 끝난다. 브라우저는 남는다.
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
    import time as _t

    for _ in range(20):
        _t.sleep(1)
        try:
            httpx.get(f"{CDP_URL}/json/version", timeout=2)
            return {"띄웠다": CDP_URL,
                    "다음": "이 창을 ⌘H로 숨기세요. 앞으로 새 창은 안 뜹니다."}
        except Exception:  # noqa: BLE001
            continue
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

    result = _autoapply(job_id, resume_url=None, live=False)
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


def _resume_title(job: dict) -> str:
    """공고별 이력서 제목. 제목이 곧 지원 폼에서 이력서를 고르는 열쇠다.

    회사명을 앞에 둔다. 목록에서 사람이 훑을 때 '어느 자리에 낸 것'인지가
    먼저 보여야 하기 때문이다. 50자 제한이 있어 뒤를 자른다.
    """
    company = (job.get("company") or "").strip()
    title = (job.get("title") or "").strip()
    name = f"{company} {title}".strip() or "박예일 이력서"
    return name[:50]


def _autoapply(job_id: int, *, resume_url: str | None, live: bool) -> dict:
    """조립 → 등록 → 지원. 각 단계가 다음 단계의 입력을 만든다.

    이력서 제목이 고리다. 편집기에 채운 뒤 그 제목을 읽어 지원 레시피에 넘기므로,
    `config.applicability.resumes` 매핑이 더는 필요 없다 — 트랙별로 미리 만들어둔
    이력서를 고르는 게 아니라 공고마다 만들기 때문이다.

    조립이 `ok=False`(필수요건 근거 없음/검수 반려)면 여기서 멈춘다. 맞지 않는
    자리에 이름을 남기는 것보다 안 내는 게 낫다.
    """
    from src.autoapply import assemble
    from src.autoapply.runner import resume_editor

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

    filled = resume_editor.fill(
        built["data"], resume_url=resume_url,
        template=None if resume_url else template,
        new_title=new_title, job_id=job_id, dry_run=False,
    )
    _remember_preview_resume(filled.get("url", ""))

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

    job = _job(job_id, resume_title=title, require_resume=True)
    result = _apply_with(job, live=live)
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


def _job(job_id: int, *, resume_title: str | None = None, require_resume: bool = False) -> dict:
    """레시피가 쓸 공고 정보. 이력서 제목까지 붙여서 돌려준다.

    require_resume는 **제출 직전에만** 켠다. 이력서를 만들기 전에 부를 때는
    제목이 아직 없는 게 정상이라, 여기서 멈추면 만들 기회 자체가 없어진다.
    """
    from src.autoapply.config import effective_config

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
    finally:
        conn.close()

    # 방금 만든 이력서 제목이 있으면 그걸 쓴다. 없을 때만 config 매핑으로 넘어간다
    # (이력서를 미리 만들어두고 트랙별로 고르던 예전 방식).
    resumes = effective_config().get("applicability", {}).get("resumes", {})
    job["resume"] = resume_title or (resumes.get(job["platform"], {}) or {}).get(job["track"])
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
    except BaseException as e:  # noqa: BLE001
        cmd = " ".join(sys.argv[1:])[:150]
        try:
            _tell(
                f"❌ <b>cli.py 처리 안 된 오류</b>\n<code>{html.escape(cmd)}</code>\n"
                f"<i>{type(e).__name__}: {str(e)[:300]}</i>"
            )
        except Exception:  # noqa: BLE001
            pass
        raise
