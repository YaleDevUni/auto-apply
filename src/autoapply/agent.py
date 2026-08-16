"""자동화 에이전트가 쓰는 인터페이스. 사람용 UI가 아니다.

에이전트의 한 사이클은 이 다섯 개로 끝난다:

    targets = agent.next_targets(limit=5)      # 뭘 지원할까
    for t in targets:
        run = agent.claim(t["job_id"])          # 자리를 선점한다 (중복지원 차단)
        if run is None:
            continue                            # 누가 이미 잡았다
        ...실제 지원 시도...
        agent.mark_submitted(run, evidence=...) # 또는 mark_failed(run, error=...)

`claim`이 이 모듈의 존재 이유다. 지원을 **시도하기 전에** 원장에 선점 행을 넣는다.
지원 도중 프로세스가 죽어도 claimed 행이 남아, 다음 실행이 같은 자리를 다시
건드리지 않는다. 사람이 지켜보지 않는 시스템에서 "죽었다가 살아나서 또 지원"은
반드시 일어나는 일이고, 그때 사과할 사람이 없다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .config import effective_config
from .db import connect, get_setting, now, rows_to_dicts, set_setting
from .notify import telegram
from .screening import summarize_blockers

log = logging.getLogger(__name__)


def next_targets(
    limit: int = 10,
    conn: sqlite3.Connection | None = None,
    *,
    skip_prepared_hours: float = 0,
) -> list[dict[str, Any]]:
    """지금 자동지원 가능한 공고를 급한 순으로 돌려준다.

    정렬은 점수순이 아니라 (마감임박 → 점수) 순이다. 오늘 밤 닫히는 95점짜리가
    상시채용 130점짜리보다 급하다 — 후자는 내일도 있다.

    skip_prepared_hours — 최근 그 시간 안에 이력서를 조립해본 공고는 뺀다.
    dry-run 준비는 선점(claim)을 하지 않으므로, 이게 없으면 사이클마다 같은
    1위만 계속 준비하고 대기열이 소진되지 않는다. 실측: 인졀미가 세 사이클
    연속으로 준비됐다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        if skip_prepared_hours:
            rows = conn.execute(
                """SELECT v.* FROM v_actionable v
                   LEFT JOIN resume_builds b ON b.job_id = v.job_id
                   WHERE b.job_id IS NULL
                      OR (julianday('now') - julianday(b.built_at)) * 24 > ?
                   LIMIT ?""",
                (skip_prepared_hours, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM v_actionable LIMIT ?", (limit,)).fetchall()
        out = rows_to_dicts(rows)
        for r in out:
            r["blockers"] = json.loads(r.get("blockers") or "[]")
            r["requires"] = json.loads(r.get("requires") or "{}")
        return out
    finally:
        if own:
            conn.close()


# 이 프로세스가 이번 실행에서 선점한 건수. cron이 매번 새 프로세스를 띄우므로
# 모듈 전역 = 실행 1회 범위가 맞다. 한 프로세스에서 여러 사이클을 돌린다면
# 사이클 사이에 reset_run_budget()을 부른다.
_run_claims = 0


_warned: set[str] = set()


def reset_run_budget() -> None:
    """실행 단위 카운터를 0으로 되돌린다. 장기 실행 프로세스에서만 필요하다."""
    global _run_claims
    _run_claims = 0
    _warned.clear()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    """상한 경고는 실행당 한 번만. 호출부가 목록을 훑으면 매 건마다 찍힌다."""
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def quota(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """오늘 얼마나 썼고 얼마나 남았는지. claim() 없이도 조회할 수 있다."""
    own = conn is None
    conn = conn or connect()
    try:
        lim = effective_config().get("limits", {})
        per_day = int(lim.get("max_per_day", 10))
        per_run = int(lim.get("max_per_run", 3))
        used = _claimed_today(conn)
        return {
            "date": _local_today(),
            "used_today": used,
            "max_per_day": per_day,
            "remaining_today": max(0, per_day - used) if per_day else None,
            "used_this_run": _run_claims,
            "max_per_run": per_run,
        }
    finally:
        if own:
            conn.close()


def _local_today() -> str:
    """now()가 로컬 오프셋 포함 ISO를 쓰므로 앞 10자가 곧 로컬 날짜다."""
    return now()[:10]


def _claimed_today(conn: sqlite3.Connection) -> int:
    """오늘 선점한 건수.

    claimed_at을 date()로 파싱하지 않고 앞 10자를 문자열 비교한다 — date()는
    ISO 오프셋을 UTC로 환산해서 자정 근처에 날짜가 하루 밀린다.

    주의: mark_failed(release=True)는 행을 지우므로 그 시도는 여기 안 잡힌다.
    의도한 것이다 — 제출 전에 실패한 시도는 바깥세상에 아무것도 남기지 않았다.

    'external'(파이프라인 밖에서 이미 지원한 자리)도 안 센다. 오늘 그걸
    발견했다는 이유로 오늘 낼 수 있는 자리가 줄면, 대기열에 옛 지원이 많을수록
    오늘 아무것도 못 내는 상태가 된다.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM apply_ledger WHERE substr(claimed_at,1,10)=? AND status<>?",
        (_local_today(), EXTERNAL),
    ).fetchone()[0]


def claim(job_id: int, conn: sqlite3.Connection | None = None) -> int | None:
    """지원을 시도하기 전에 자리를 선점한다.

    반환: apply_ledger.id (선점 성공) 또는 None (이미 잡혔거나 **상한에 걸렸음**).

    두 가지를 여기서 막는다. 지원 경로가 반드시 이 함수를 지나기 때문이다.

    1. **중복지원** — canonical_key UNIQUE 제약에 기대어 원자적으로 처리한다.
       두 프로세스가 동시에 같은 자리를 잡으려 하면 한쪽만 INSERT에 성공하고
       다른 쪽은 IntegrityError를 받는다. 애플리케이션 레벨에서
       SELECT-then-INSERT로 확인하면 그 사이에 끼어들 틈이 생긴다.

    2. **폭주** — UNIQUE 제약은 "같은 자리에 두 번"만 막는다. "서로 다른
       200개 자리에 연속 지원"은 못 막는다. 무인 운영에서 그건 루프 버그
       하나면 일어나고, 사용자 실명으로 나간 지원서는 되돌릴 수 없다.
       limits.max_per_day / max_per_run이 그걸 막는다.

    상한에 걸렸을 때 예외를 던지지 않고 None을 돌려주는 이유: 호출부는 이미
    "None이면 넘어간다"를 처리하고 있다. 상한을 새로운 실패 경로로 만들면
    처리를 빼먹은 곳에서 프로세스가 죽고, 그건 우아한 실패가 아니다.
    """
    global _run_claims
    own = conn is None
    conn = conn or connect()
    try:
        lim = effective_config().get("limits", {})
        per_day = int(lim.get("max_per_day", 10))
        per_run = int(lim.get("max_per_run", 3))

        if per_run and _run_claims >= per_run:
            _warn_once("run", "실행 상한 도달 (%d건) — 더 선점하지 않는다", per_run)
            return None
        if per_day and _claimed_today(conn) >= per_day:
            _warn_once("day", "일일 상한 도달 (%d건) — 더 선점하지 않는다", per_day)
            return None

        job = conn.execute(
            "SELECT id, canonical_key, company, title, platform FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise ValueError(f"공고 {job_id}가 없다")
        if not job["canonical_key"]:
            # 키를 못 만든 공고는 중복 판정이 불가능하다. 무인 상태에서
            # 판정 불가를 통과시키면 같은 자리에 여러 번 지원할 수 있다.
            raise ValueError(f"공고 {job_id}에 canonical_key가 없다 — 선점 불가")

        try:
            cur = conn.execute(
                """INSERT INTO apply_ledger
                     (canonical_key, job_id, company, title, platform, status, claimed_at)
                   VALUES (?,?,?,?,?, 'claimed', ?)""",
                (
                    job["canonical_key"], job["id"], job["company"],
                    job["title"], job["platform"], now(),
                ),
            )
            conn.commit()
            _run_claims += 1
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # 같은 canonical_key가 이미 있다 = 이 자리는 이미 처리 중이거나 끝났다
            return None
    finally:
        if own:
            conn.close()


EXTERNAL = "external"


def record_external(job_id: int, conn: sqlite3.Connection | None = None) -> int | None:
    """이 파이프라인 **밖에서** 이미 지원한 자리로 원장에 적는다.

    반환: 새 원장 id, 또는 None(이미 원장에 있음 — 우리가 낸 것이든 아니든).

    왜 원장에 적나: 대기열(`v_actionable`)이 이미 "원장에 있는 canonical_key는
    빼고" 낸다. 같은 규칙에 얹으면 이 자리는 다시는 올라오지 않고, 같은 공고가
    다른 플랫폼에 중복 게시돼 있어도 canonical_key가 같아 같이 걸린다.

    상태를 'submitted'가 아니라 'external'로 두는 이유: 우리가 낸 게 아니다.
    제출 건수·증적·성공률에 섞이면 "이 파이프라인이 무엇을 했나"를 못 읽는다.
    일일 상한(`_claimed_today`)도 이 상태는 세지 않는다 — 오늘 발견했다는
    이유로 오늘 낼 수 있는 자리가 줄면 안 된다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        job = conn.execute(
            "SELECT id, canonical_key, company, title, platform FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise ValueError(f"공고 {job_id}가 없다")
        if not job["canonical_key"]:
            log.warning("공고 %s에 canonical_key가 없다 — 외부 지원 기록 생략", job_id)
            return None
        try:
            cur = conn.execute(
                """INSERT INTO apply_ledger
                     (canonical_key, job_id, company, title, platform, status, claimed_at, error)
                   VALUES (?,?,?,?,?, ?, ?, ?)""",
                (
                    job["canonical_key"], job["id"], job["company"], job["title"],
                    job["platform"], EXTERNAL, now(),
                    "파이프라인 밖에서 이미 지원함(플랫폼 화면에서 확인)",
                ),
            )
            conn.commit()
            log.info("외부 지원으로 기록 — 공고 %s %s", job_id, job["company"])
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None
    finally:
        if own:
            conn.close()


def mark_submitted(
    ledger_id: int, evidence_path: str | None = None, conn: sqlite3.Connection | None = None
) -> None:
    """제출이 **확인된** 뒤에만 부른다.

    버튼을 눌렀다는 것과 접수됐다는 것은 다르다. 완료 화면이나 접수 확인을
    실제로 본 다음에 호출해야, 원장이 현실과 어긋나지 않는다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            """UPDATE apply_ledger
               SET status='submitted', submitted_at=?, evidence_path=?
               WHERE id=?""",
            (now(), evidence_path, ledger_id),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def mark_failed(
    ledger_id: int, error: str, release: bool = False, conn: sqlite3.Connection | None = None
) -> None:
    """지원 실패를 기록한다.

    release=False(기본)면 canonical_key를 계속 붙잡고 있어 재시도하지 않는다.
    무인 운영에서는 이쪽이 기본이어야 한다 — 실패 원인을 모르는 채로 재시도하면
    '실은 제출됐는데 확인만 실패한' 경우에 중복지원이 된다.

    release=True는 제출 이전 단계에서 확실히 실패했을 때만 쓴다
    (예: 페이지 로드 실패, 로그인 만료). 이때만 자리를 놓아준다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT job_id, platform, evidence_path FROM apply_ledger WHERE id=?",
            (ledger_id,),
        ).fetchone()

        if release:
            conn.execute("DELETE FROM apply_ledger WHERE id=?", (ledger_id,))
        else:
            conn.execute(
                "UPDATE apply_ledger SET status='failed', error=? WHERE id=?",
                (error, ledger_id),
            )
        conn.commit()

        # 고장 큐에 남긴다. 원장의 failed 행만으로는 self_items()가 미리 정의한
        # 증상(플랫폼별 2건 이상)에만 걸려서, 처음 보는 실패는 아무 데도 안 남았다.
        # 증적 경로를 같이 넘긴다 — 계획 에이전트가 화면을 읽어야 셀렉터를
        # 추측이 아니라 근거로 고친다.
        from . import errors

        errors.record(
            conn, kind="apply",
            exc_type=type(error).__name__ if isinstance(error, BaseException) else "ApplyFailed",
            message=str(error),
            command=f"apply {row['job_id']}" if row else "apply",
            context={
                "job_id": row["job_id"] if row else None,
                "platform": row["platform"] if row else None,
                "evidence_path": row["evidence_path"] if row else None,
            },
        )
    finally:
        if own:
            conn.close()


def blocked_summary(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """"왜 아무것도 지원 안 했지?"에 답한다. 무인 운영의 첫 디버깅 창구."""
    own = conn is None
    conn = conn or connect()
    try:
        return {
            "blockers": summarize_blockers(conn),
            "top_blocked": rows_to_dicts(
                conn.execute(
                    "SELECT company, title, fit_score, blockers FROM v_blocked LIMIT 15"
                ).fetchall()
            ),
        }
    finally:
        if own:
            conn.close()


def notify_login_required(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """세션 끊김을 텔레그램으로 알린다. 쿨다운을 둬서 매 실행마다 도배하지 않는다.

    실측(2026-08-15, 원티드)으로 확정된 사실: 지원에는 OAuth 로그인이 필요하고
    자동 로그인 경로가 없다. 그래서 LOGIN_REQUIRED는 에이전트가 스스로 못 넘는
    지점이고, 이 함수가 그 지점에서 사람을 부르는 유일한 통로다.

    호출 시점: `applicability.evaluate()`가 session_ok로 세션이 죽었다고 판정한
    공고가 있을 때. 지금은 `scrape --session platform=0`으로 캐스터가 명시적으로
    알려줘야 켜진다 — 세션 생사를 스스로 확인하는 건 apply 러너(Playwright)가
    붙을 때의 몫이다. 그 전까지는 이 함수가 알림 배선을 미리 깔아둔다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        cfg = effective_config().get("notify", {}).get("telegram", {})
        if not cfg.get("enabled", True):
            return {"sent": False, "reason": "disabled"}

        rows = conn.execute(
            "SELECT platform, blockers FROM jobs j "
            "JOIN applicability a ON a.job_id = j.id WHERE a.actionable = 0"
        ).fetchall()

        by_platform: dict[str, int] = {}
        for r in rows:
            if any(b["code"] == "LOGIN_REQUIRED" for b in json.loads(r["blockers"] or "[]")):
                by_platform[r["platform"]] = by_platform.get(r["platform"], 0) + 1

        if not by_platform:
            return {"sent": False, "reason": "no_login_blockers"}

        cooldown_h = cfg.get("cooldown_hours", 6)
        due = _due_platforms(conn, by_platform, cooldown_h)
        if not due:
            return {"sent": False, "reason": "cooldown", "blocked": by_platform}

        lines = ["🔒 <b>로그인 필요</b>", ""]
        for platform, count in due.items():
            lines.append(f"· {platform}: {count}건 막힘 — 다시 로그인해 주세요")
        text = "\n".join(lines)

        sent = telegram.notify(conn, text)
        if sent:
            for platform in due:
                set_setting(conn, f"telegram_last_alert:LOGIN_REQUIRED:{platform}", now())
        return {"sent": sent, "platforms": due}
    finally:
        if own:
            conn.close()


def _due_platforms(
    conn: sqlite3.Connection, counts: dict[str, int], cooldown_h: float
) -> dict[str, int]:
    """마지막 알림에서 cooldown_h 이상 지난 플랫폼만 골라낸다."""
    due: dict[str, int] = {}
    now_dt = datetime.now(timezone.utc)
    for platform, count in counts.items():
        raw = get_setting(conn, f"telegram_last_alert:LOGIN_REQUIRED:{platform}", "")
        if raw:
            elapsed_h = (now_dt - datetime.fromisoformat(raw)).total_seconds() / 3600
            if elapsed_h < cooldown_h:
                continue
        due[platform] = count
    return due


def status(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """전체 상태 한 줄 요약. 스케줄러 로그에 찍기 좋은 형태."""
    own = conn is None
    conn = conn or connect()
    try:
        one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
        return {
            "jobs": one("SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL"),
            "passed": one("SELECT COUNT(*) FROM screening WHERE verdict='pass'"),
            "actionable": one("SELECT COUNT(*) FROM v_actionable"),
            "blocked": one("SELECT COUNT(*) FROM v_blocked"),
            "claimed": one("SELECT COUNT(*) FROM apply_ledger WHERE status='claimed'"),
            "submitted": one("SELECT COUNT(*) FROM apply_ledger WHERE status='submitted'"),
            "failed": one("SELECT COUNT(*) FROM apply_ledger WHERE status='failed'"),
            # 파이프라인 밖에서 이미 지원해 둔 자리. 우리 제출 건수와 섞지 않는다.
            "이미지원(외부)": one("SELECT COUNT(*) FROM apply_ledger WHERE status='external'"),
            "quota": quota(conn),
        }
    finally:
        if own:
            conn.close()
