"""파이프라인이 조용히 망가진 것을 알아챈다. LLM 호출 0회.

## 왜 필요한가

이 프로젝트에서 잡은 버그 셋을 전부 **사람이** 찾았다. 시스템은 셋 다 정상이라고
보고하고 있었다:

    IMAGE_ONLY 628건 오탐      통과 702건의 89%가 한 blocker에 몰림
    ESSAY_REQUIRED 52건        폼에 없는 문항을 본문에서 찾아 막음
    이력서 nth=0 오선택        트랙과 무관하게 첫 이력서가 나감

앞의 둘은 **숫자만 봐도 이상했다.** 한 blocker가 통과분의 대부분을 막고 있으면
그건 필터가 아니라 버그일 가능성이 높다. 나눗셈 몇 번이면 잡힌다.

사람이 안 보는 시스템에서 "아무 일도 안 일어남"과 "망가져서 아무 일도 못 함"은
겉보기에 같다. 그 둘을 가르는 게 이 모듈의 전부다.

## 왜 LLM을 쓰지 않는가

경보는 재현 가능해야 한다. 같은 숫자에 다른 답이 나오면 경보를 믿을 수 없고,
믿을 수 없는 경보는 결국 무시된다. 그리고 이 판정은 전부 비율 계산이다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from .config import effective_config
from .db import connect, now, rows_to_dicts

log = logging.getLogger(__name__)

# 설계상 크게 나오는 게 정상인 blocker. 기준선이지 고장이 아니다.
DEFAULT_IGNORE = ("SCORE_BELOW_BAR", "RESUME_MISSING")


def collect(conn: sqlite3.Connection) -> dict[str, Any]:
    """지금 상태를 숫자로 찍는다. 스냅샷으로 저장돼 다음 실행과 비교된다."""
    one = lambda q, *a: conn.execute(q, a).fetchone()[0]  # noqa: E731

    blockers: dict[str, int] = {}
    rows = conn.execute(
        """SELECT a.blockers FROM applicability a
           JOIN screening s ON s.job_id = a.job_id
           JOIN jobs j ON j.id = a.job_id
           WHERE s.verdict='pass' AND a.actionable=0 AND j.closed_at IS NULL"""
    ).fetchall()
    for r in rows:
        for b in json.loads(r["blockers"] or "[]"):
            blockers[b["code"]] = blockers.get(b["code"], 0) + 1

    by_platform = {
        r["platform"]: r["n"]
        for r in conn.execute(
            "SELECT platform, COUNT(*) AS n FROM jobs WHERE closed_at IS NULL GROUP BY platform"
        ).fetchall()
    }

    return {
        "at": now(),
        "jobs": one("SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL"),
        "passed": one("SELECT COUNT(*) FROM screening WHERE verdict='pass'"),
        "actionable": one("SELECT COUNT(*) FROM v_actionable"),
        "by_platform": by_platform,
        "blockers": blockers,
        "recent_failures": one(
            "SELECT COUNT(*) FROM apply_ledger WHERE status='failed'"
        ),
    }


def _finding(code: str, msg: str, detail: str = "") -> dict[str, str]:
    return {"code": code, "message": msg, "detail": detail}


def check(
    curr: dict[str, Any], prev: dict[str, Any] | None, cfg: dict[str, Any]
) -> list[dict[str, str]]:
    """이상 징후를 낸다. 비어 있으면 정상이다."""
    out: list[dict[str, str]] = []
    ignore = set(cfg.get("ignore_blockers", DEFAULT_IGNORE))
    passed = curr["passed"] or 0

    # 1) 한 blocker가 통과분을 지배한다 = 필터가 아니라 버그일 가능성
    #    IMAGE_ONLY 628/702(89%)를 이 규칙이 잡았을 것이다.
    dom = cfg.get("blocker_dominance", 0.7)
    if passed:
        for code, n in sorted(curr["blockers"].items(), key=lambda kv: -kv[1]):
            if code in ignore:
                continue
            ratio = n / passed
            if ratio > dom:
                out.append(
                    _finding(
                        "BLOCKER_DOMINANT",
                        f"{code} 하나가 통과분의 {ratio:.0%}를 막고 있음",
                        f"{n}건 / 통과 {passed}건 — 필터가 아니라 오탐일 수 있음",
                    )
                )

    # 2) 아무것도 통과하지 못함 = 수집이나 판정이 깨졌다
    if curr["jobs"] and not passed:
        out.append(
            _finding("NO_PASS", "수집은 됐는데 적합 판정이 0건", f"수집 {curr['jobs']}건")
        )

    # 3) 플랫폼 하나가 통째로 사라짐 = 어댑터가 깨졌다
    for platform, before in (prev or {}).get("by_platform", {}).items():
        after = curr["by_platform"].get(platform, 0)
        if before >= 20 and after == 0:
            out.append(
                _finding(
                    "PLATFORM_EMPTY",
                    f"{platform} 수집이 0건 — 어댑터가 깨졌을 수 있음",
                    f"직전 {before}건 → 0건",
                )
            )

    # 4) 지원 가능 건수 급감 = 판정 로직이나 레시피가 깨졌다
    drop = cfg.get("actionable_drop", 0.9)
    if prev and prev.get("actionable", 0) >= 10:
        before, after = prev["actionable"], curr["actionable"]
        if after < before * (1 - drop):
            out.append(
                _finding(
                    "ACTIONABLE_COLLAPSE",
                    f"자동지원 가능 건수가 {before} → {after}로 급감",
                    "판정 기준이나 레시피가 깨졌는지 확인 필요",
                )
            )

    # 5) 지원 실패가 쌓인다 = 레시피가 현실과 안 맞는다
    limit = cfg.get("consecutive_failures", 3)
    grew = curr["recent_failures"] - (prev or {}).get("recent_failures", 0)
    if curr["recent_failures"] >= limit and grew > 0:
        out.append(
            _finding(
                "FAILURES_PILING",
                f"지원 실패 {curr['recent_failures']}건 (직전 대비 +{grew})",
                "레시피가 깨졌을 수 있음 — evidence 스크린샷 확인",
            )
        )

    return out


def run(conn: sqlite3.Connection | None = None, *, notify: bool = True) -> dict[str, Any]:
    """수집·판정 뒤에 부른다. 스냅샷을 남기고 이상하면 알린다."""
    own = conn is None
    conn = conn or connect()
    try:
        cfg = effective_config().get("health", {})
        if not cfg.get("enabled", True):
            return {"skipped": "disabled"}

        curr = collect(conn)
        prev_row = conn.execute(
            "SELECT metrics FROM health_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev = json.loads(prev_row["metrics"]) if prev_row else None

        findings = check(curr, prev, cfg)

        conn.execute(
            "INSERT INTO health_snapshots (taken_at, metrics) VALUES (?,?)",
            (curr["at"], json.dumps(curr, ensure_ascii=False)),
        )
        conn.commit()

        sent = False
        if findings and notify:
            sent = _alert(conn, findings, cfg)

        return {"findings": findings, "notified": sent, "metrics": curr}
    finally:
        if own:
            conn.close()


def _alert(conn: sqlite3.Connection, findings: list[dict[str, str]], cfg: dict) -> bool:
    """같은 징후로 반복해서 깨우지 않는다. 무시되는 경보는 없는 것과 같다."""
    from .db import get_setting, set_setting
    from .notify import telegram

    hours = cfg.get("cooldown_hours", 12)
    due = [f for f in findings if _cooldown_passed(conn, get_setting, f["code"], hours)]
    if not due:
        return False

    lines = ["⚠️ <b>파이프라인 이상</b>", ""]
    for f in due:
        lines.append(f"· {f['message']}")
        if f["detail"]:
            lines.append(f"  <i>{f['detail']}</i>")

    if telegram.notify(conn, "\n".join(lines)):
        for f in due:
            set_setting(conn, f"health_last_alert:{f['code']}", now())
        return True
    return False


def _cooldown_passed(conn, get_setting, code: str, hours: float) -> bool:
    from datetime import datetime, timezone

    raw = get_setting(conn, f"health_last_alert:{code}", "")
    if not raw:
        return True
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(raw)).total_seconds()
    return elapsed / 3600 >= hours


def history(conn: sqlite3.Connection | None = None, limit: int = 10) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute(
            "SELECT taken_at, metrics FROM health_snapshots ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = rows_to_dicts(rows)
        for r in out:
            m = json.loads(r.pop("metrics"))
            r.update({k: m[k] for k in ("jobs", "passed", "actionable")})
        return out
    finally:
        if own:
            conn.close()
