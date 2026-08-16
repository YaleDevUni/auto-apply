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
    sub.add_parser("listen", help="폰에서 온 메시지 처리 (운영 명령 + 개발 지시 접수)")

    cq = sub.add_parser("improve", help="자기개선 오케스트레이터 — 전용 브랜치에서만 작업")
    cq.add_argument("--limit", type=int, default=1)
    cq.add_argument("--list", action="store_true", help="할 일만 보고 실행하지 않는다")

    sub.add_parser("resumes", help="플랫폼에 저장된 이력서 목록 (읽기 전용)")

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
    elif args.cmd == "listen":
        from src.autoapply.notify import listener

        conn = connect()
        _out(listener.drain(conn))
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
        _out({
            "미리보기용": preview or "미지정",
            "주의": "삭제는 직접 하세요 — 계정 데이터를 지우는 건 되돌릴 수 없습니다",
            "목록": resume_editor.list_resumes(),
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
        _out(_cycle_apply(args.limit))
    elif args.cmd == "apply":
        _out(_apply(args.job_id, live=args.live, headless=args.headless))
    return 0


def _cycle_apply(limit: int) -> dict:
    """대기열 상위 N건을 dry-run으로 준비한다.

    **cron에서는 절대 제출하지 않는다.** 이력서를 조립해 등록하고 지원 폼까지
    채운 뒤 스크린샷만 남긴다. 사람이 그 사진을 보고 `apply <id> --live` 로
    제출한다 — 되돌릴 수 없는 행동에 사람의 확인을 남겨두는 지점이다.

    한 사이클에 기본 1건인 이유: 건당 브라우저를 띄우고 이력서를 조립하므로
    2분 안팎이 든다. 여러 건을 몰아 돌리면 사이클이 길어지고, 무엇보다
    사람이 검토할 스크린샷이 한 번에 쌓여 실질 검토가 안 된다.
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
            r = _autoapply(t["job_id"], resume_url=_preview_resume_url(), live=False)
        except Exception as e:  # noqa: BLE001
            r = {"error": f"{type(e).__name__}: {e}"}
        out.append({"job_id": t["job_id"], "company": t["company"], **r})
        _report_prepared(t, r)
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


def _report_prepared(target: dict, result: dict) -> None:
    """준비 결과를 폰으로 보낸다. 성공이면 **스크린샷**, 실패면 이유.

    실패가 로그에만 남으면 아침에 로그를 뒤져야 안다 — 실제로 06시 사이클이
    클릭 타임아웃으로 통째로 실패했는데 그렇게 발견했다. 무인 운영에서
    "아무 일도 안 일어남"과 "망가져서 못 함"은 겉보기에 같다.
    """
    from src.autoapply.db import connect as _c
    from src.autoapply.notify import telegram

    conn = _c()
    try:
        head = f"{target['fit_score']}점 · {target['company']} — {target['title'][:40]}"
        apply_res = result.get("apply") or {}
        err = result.get("error") or apply_res.get("error") or result.get("stopped")

        if err:
            telegram.notify(conn, f"❌ <b>지원 준비 실패</b>\n{head}\n<i>{str(err)[:200]}</i>")
            return

        shot = apply_res.get("evidence")
        caption = (
            f"📄 <b>지원 준비됨</b>\n{head}\n\n"
            f"확인 후 제출:\n<code>python cli.py autoapply {target['job_id']} --live</code>\n\n"
            f"<i>제출 시 이 공고용으로 이력서를 다시 채운 뒤 넣습니다.</i>"
        )
        if not (shot and telegram.send_photo(conn, shot, caption)):
            telegram.notify(conn, caption)
    finally:
        conn.close()


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

    filled = resume_editor.fill(built["data"], resume_url=resume_url, dry_run=False)
    _remember_preview_resume(filled.get("url", ""))
    if not filled["title"]:
        return {"stopped": "이력서 제목을 읽지 못함 — 어느 이력서를 낼지 정할 수 없다"}

    job = _job(job_id, resume_title=filled["title"])
    result = _apply_with(job, live=live)
    return {
        "company": built["company"],
        "resume": {"title": filled["title"], "url": filled["url"],
                   "lost": filled.get("lost"), "skills": len(filled.get("skills", []))},
        "apply": result,
    }


def _job(job_id: int, *, resume_title: str | None = None) -> dict:
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

    # 방금 만든 이력서 제목이 있으면 그걸 쓴다. 없을 때만 config 매핑으로 넘어간다
    # (이력서를 미리 만들어두고 트랙별로 고르던 예전 방식).
    resumes = effective_config().get("applicability", {}).get("resumes", {})
    job["resume"] = resume_title or (resumes.get(job["platform"], {}) or {}).get(job["track"])
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
    return _apply_with(_job(job_id), live=live, headless=headless)


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
    else:
        # 눌렀는데 완료 화면을 못 봤다. 실제로 접수됐을 수 있으므로 자리를 놓지 않는다.
        agent.mark_failed(ledger, result["error"] or "제출 확인 실패", release=False)
    result["ledger"] = ledger
    return result


if __name__ == "__main__":
    raise SystemExit(main())
