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
import json
import logging
import sys

from src.autoapply import agent, pipeline
from src.autoapply.adapters import REGISTRY
from src.autoapply.db import connect
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

    hp = sub.add_parser("health", help="파이프라인 이상 감지 (LLM 0회)")
    hp.add_argument("--no-notify", action="store_true", help="텔레그램 알림 없이 확인만")
    hp.add_argument("--history", action="store_true", help="최근 스냅샷 추이")
    sub.add_parser("status", help="상태 요약")
    sub.add_parser("where", help="경로 확인")

    tsp = sub.add_parser("telegram-setup", help="텔레그램 알림 연결")
    tsp.add_argument("token", help="@BotFather에게 받은 봇 토큰")

    sub.add_parser("notify-login", help="세션 끊김 알림 수동 트리거 (쿨다운 무시하지 않음)")

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
    elif args.cmd == "browser-login":
        from src.autoapply.runner import login

        _out(login())
    elif args.cmd == "capture":
        from src.autoapply.runner import capture

        _out(capture(_job(args.job_id), click=args.click))
    elif args.cmd == "apply":
        _out(_apply(args.job_id, live=args.live, headless=args.headless))
    return 0


def _job(job_id: int) -> dict:
    """레시피가 쓸 공고 정보. 트랙에 맞는 이력서 제목까지 붙여서 돌려준다."""
    from src.autoapply.config import effective_config

    conn = connect()
    try:
        row = conn.execute(
            """SELECT j.id AS job_id, j.platform, j.url, j.company, j.title, s.track
               FROM jobs j LEFT JOIN screening s ON s.job_id = j.id
               WHERE j.id=?""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"공고 {job_id}가 없다")
        job = dict(row)
    finally:
        conn.close()

    resumes = effective_config().get("applicability", {}).get("resumes", {})
    job["resume"] = (resumes.get(job["platform"], {}) or {}).get(job["track"])
    if not job["resume"]:
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
    from src.autoapply.runner import LoginRequired, run

    job = _job(job_id)
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
    else:
        # 눌렀는데 완료 화면을 못 봤다. 실제로 접수됐을 수 있으므로 자리를 놓지 않는다.
        agent.mark_failed(ledger, result["error"] or "제출 확인 실패", release=False)
    result["ledger"] = ledger
    return result


if __name__ == "__main__":
    raise SystemExit(main())
