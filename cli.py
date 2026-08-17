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
from contextlib import closing

from src.autoapply import agent, llm, pipeline, tasks
from src.autoapply.adapters import REGISTRY
from src.autoapply.db import connect
from src.autoapply.notify import report, telegram
from src.autoapply.paths import describe
from src.autoapply.workflows import (
    context, night_cycle, prepare_application, revise_application,
    submit_application,
)


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
    match args.cmd:
        case "scrape":
            _cmd_scrape(args)
        case "reevaluate":
            _out(pipeline.reevaluate())
        case "targets":
            _out(agent.next_targets(args.limit))
        case "blocked":
            _out(agent.blocked_summary())
        case "quota":
            _out(agent.quota())
        case "llm-cost":
            _out(llm.cost_report(args.job_id))
        case "browser-open":
            _out(_browser_open())
        case "browser-restart":
            from src.autoapply.runner.session import restart_resident

            ok = restart_resident()
            _out({"재시작": "성공" if ok else "실패 — 20초 안에 CDP가 안 뜸"})
        case "revise":
            _out(revise_application.run(args.job_id, args.feedback))
        case "guide":
            _out(_guide(args.instruction, revert=args.revert, clear_session=args.clear_session))
        case "revlog":
            _out(_revlog(edit=args.edit, delete=args.delete, text=args.text))
        case "builds":
            from src.autoapply import assemble

            _out(assemble.builds_log(args.limit))
        case "health":
            from src.autoapply import health

            _out(health.history() if args.history else health.run(notify=not args.no_notify))
        case "status":
            _out(agent.status())
        case "where":
            _out(describe())
        case "telegram-setup":
            with closing(connect()) as conn:
                _out(telegram.setup(conn, args.token))
        case "telegram-commands":
            with closing(connect()) as conn:
                telegram.set_commands(conn)
            _out({"등록": [c for c, _ in telegram.BOT_COMMANDS]})
        case "notify-login":
            _out(agent.notify_login_required())
        case "session-check":
            from src.autoapply.runner import probe

            result = probe.describe(args.platform)
            _out(result)
            if args.notify and any(v == "죽음" for v in result.values()):
                _out(agent.notify_login_required())
        case "listen":
            from src.autoapply.notify import listener

            with closing(connect()) as conn:
                if args.watch:
                    listener.watch(conn)
                else:
                    _out(listener.drain(conn))
        case "improve" | "plan":
            # improve는 plan의 옛 이름이다. 승인 없이 곧장 코드를 고치던 경로가
            # 계획→(위험도)→반영 으로 바뀌었고, 두 이름을 둘 이유가 없어 합쳤다.
            from src.autoapply import orchestrator

            if args.list:
                with closing(connect()) as conn:
                    _out(orchestrator.gather(conn))
            else:
                _out(orchestrator.plan(limit=args.limit))
        case "fix-run":
            from src.autoapply import orchestrator

            _out(orchestrator.execute(plan_id=args.plan_id))
        case "errors":
            from src.autoapply import errors

            with closing(connect()) as conn:
                _out(errors.summary(conn, args.limit))
        case "plans":
            from src.autoapply import orchestrator

            with closing(connect()) as conn:
                _out(orchestrator.recent_plans(conn))
        case "fix-revert":
            from src.autoapply import orchestrator

            with closing(connect()) as conn:
                _out(orchestrator.revert(conn, args.sha))
        case "resumes":
            _out(_resumes_report(cleanup=args.cleanup))
        case "browser-login":
            from src.autoapply.runner import login

            _out(login())
        case "capture":
            from src.autoapply.runner import capture

            _out(capture(context.job(args.job_id), click=args.click))
        case "resume":
            from src.autoapply import assemble

            result = assemble.build(args.job_id, max_rounds=args.rounds)
            body = result.pop("resume")
            _out(result)
            if args.print:
                print("\n" + body)
        case "autoapply":
            _out(prepare_application.run(args.job_id, resume_url=args.resume_url, live=args.live))
        case "cycle-apply":
            _out(night_cycle.cycle_apply(args.limit, defer=args.defer))
        case "flush-notify":
            _out(report.flush())
        case "night-cycle":
            _out(night_cycle.run(args.target, defer=args.defer))
        case "submit":
            _out(submit_application.submit_registered(args.job_id))
        case "apply":
            _out(submit_application.apply_job(args.job_id, live=args.live, headless=args.headless))
    return 0


def _cmd_scrape(args: argparse.Namespace) -> None:
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


def _resumes_report(*, cleanup: bool) -> dict:
    from src.autoapply.runner import resume_editor

    if cleanup:
        return resume_editor.cleanup(dry_run=False)
    return {
        "보호(편집·삭제 금지)": sorted(resume_editor.protected_titles()),
        "정리 예정": resume_editor.cleanup(dry_run=True),
        "안내": "실제로 지우려면 --cleanup. 로컬 사본(profile/generated)은 남습니다",
    }


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


def _guide(instruction: str, *, revert: bool, clear_session: bool = False) -> dict:
    """작성 가이드를 고치거나, 마지막 백업으로 되돌리거나, 이어가던 대화를 끊는다.

    텔레그램 수신 루프가 서브프로세스로 이 커맨드를 부른다 — Opus 5 호출은
    수 분 걸릴 수 있어 수신을 막으면 안 되기 때문이다. 그래서 결과는
    반환값이 아니라 폰으로 보낸다; 반환값은 터미널에서 직접 돌릴 때를 위한 것.
    """
    from src.autoapply import guide

    if clear_session:
        result = guide.clear_session()
        report.tell(_guide_message(result, revert=False, clear_session=True))
        return result

    result = guide.revert() if revert else guide.edit(instruction)
    report.tell(_guide_message(result, revert=revert, clear_session=False))
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
            report.tell(f"⏹ <b>중단</b> — <code>{html.escape(' '.join(sys.argv[1:])[:100])}</code>\n"
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
            report.tell(
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
                report.tell(
                    f"❌ <b>cli.py 처리 안 된 오류</b>\n<code>{html.escape(cmd)}</code>\n"
                    f"<i>{type(e).__name__}: {str(e)[:300]}</i>"
                )
            except Exception:  # noqa: BLE001
                pass
        raise
