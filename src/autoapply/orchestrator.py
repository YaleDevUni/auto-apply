"""자기개선 오케스트레이터 — 시스템이 스스로 망가진 곳을 고친다.

## 두 파이프라인

    A. 지원 파이프라인   수집 → 판정 → 이력서 → dry-run → 제출
                         자율 실행. 코드를 고치지 않는다.

    B. 개선 오케스트레이터 (이 모듈)
                         A가 망가진 것을 감지 → 원인 분석 → 수정 → 검증 → 보고
                         코드를 고친다. 그래서 브랜치에서만 움직인다.

B의 입력은 둘이다:

    자체 진단   health 이상, 지원 실패 누적, blocker 편중, 레시피 없음
    사람 지시   폰에서 보낸 한 줄 (control_queue)

사람 지시가 우선이다. 시스템이 스스로 찾은 문제보다 사람이 아는 문제가 더 급한
경우가 대부분이고, 무엇보다 사람이 방향을 바꾸려는데 자기 할 일을 먼저 하는
오케스트레이터는 통제 불가능하다.

## main에 절대 닿지 않는다

코드 수정은 **검증 오라클이 없는 영역**이다. 레시피는 dry-run 스크린샷으로 맞는지
볼 수 있지만, `applicability.py`를 고쳤을 때 그게 맞는지 확인할 방법이 없다.
틀리면 엉뚱한 회사에 지원이 나간다. 그래서:

    1. 전용 브랜치(auto/<id>)에서만 작업한다
    2. push하지 않는다
    3. 끝나면 원래 브랜치로 돌아온다
    4. 작업 트리가 더러우면 아예 시작하지 않는다

사람이 브랜치를 보고 병합한다. 자는 동안 보낸 한 줄이 검증 없이 main에 들어가는
일은 없다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
from typing import Any

from .config import effective_config
from .db import connect, now
from .llm import UsageLimited, _raise_if_limited
from .notify import telegram
from .paths import CODE_ROOT

log = logging.getLogger(__name__)

# 코딩 에이전트에게 허용하는 도구. 바깥으로 뭘 보내는 도구는 주지 않는다.
AGENT_TOOLS = "Read,Edit,Write,Grep,Glob,Bash"

# 에이전트가 증적 스크린샷을 읽을 수 있어야 한다. 화면을 안 보고 셀렉터를 고치는
# 것은 추측이고, 이 프로젝트에서 추측으로 고친 것은 대부분 틀렸다.
EVIDENCE_HINT = (
    "실패 증적 스크린샷이 주어지면 반드시 Read 도구로 먼저 읽어라. "
    "화면을 보지 않고 셀렉터를 바꾸지 마라."
)

SYSTEM = (
    "당신은 이 저장소를 유지보수하는 개발자입니다. 저장소의 CLAUDE.md와 README를 "
    "먼저 읽고 설계 의도를 파악한 뒤 작업하십시오.\n"
    "원칙: 정규식·단순 파싱으로 되는 구간에 LLM을 넣지 마십시오. "
    "되돌릴 수 없는 동작(실제 지원 제출)은 절대 실행하지 마십시오. "
    "지시가 모호하면 가장 보수적인 해석을 택하고 무엇을 가정했는지 마지막에 적으십시오.\n"
    "가능하면 변경을 검증하는 명령을 실제로 실행해 결과를 확인하십시오. "
    "커밋은 하지 마십시오 — 호출자가 합니다.\n" + EVIDENCE_HINT
)


# ─────────────────────── 할 일 모으기 ───────────────────────


def _human_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, text FROM control_queue WHERE status='queued' ORDER BY id"
    ).fetchall()
    return [
        {"source": "human", "queue_id": r["id"], "title": r["text"][:60], "task": r["text"]}
        for r in rows
    ]


def _self_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """A 파이프라인이 남긴 신호를 할 일로 바꾼다. LLM 호출 0회로 판단한다."""
    items: list[dict[str, Any]] = []

    # 1) 지원 실패가 쌓였다 = 레시피가 현실과 안 맞는다
    fails = conn.execute(
        """SELECT platform, COUNT(*) AS n, MAX(error) AS err, MAX(evidence_path) AS shot
           FROM apply_ledger WHERE status='failed' GROUP BY platform"""
    ).fetchall()
    for f in fails:
        if f["n"] >= 2:
            items.append({
                "source": "self",
                "title": f"{f['platform']} 지원 실패 {f['n']}건",
                "task": (
                    f"{f['platform']} 플랫폼 지원이 {f['n']}건 실패했다. "
                    f"마지막 오류: {f['err']}\n"
                    f"증적 스크린샷: {f['shot']}\n\n"
                    f"**먼저 위 스크린샷을 Read 도구로 읽어라.** 화면이 어떻게 달라졌는지 "
                    "보지 않고 셀렉터를 고치면 추측일 뿐이다.\n\n"
                    f"recipes/{f['platform']}.json 이 현재 화면과 맞는지 확인하고 고쳐라. "
                    "고친 뒤 `python cli.py apply <job_id>` (dry-run, 제출 안 함)로 검증하라. "
                    "--live 는 절대 쓰지 마라."
                ),
            })

    # 2) health 이상 징후 — 직전 스냅샷 기준
    from .health import check, collect

    cfg = effective_config().get("health", {})
    prev_row = conn.execute(
        "SELECT metrics FROM health_snapshots ORDER BY id DESC LIMIT 1 OFFSET 1"
    ).fetchone()
    prev = json.loads(prev_row["metrics"]) if prev_row else None
    for f in check(collect(conn), prev, cfg):
        if f["code"] in ("BLOCKER_DOMINANT", "NO_PASS", "PLATFORM_EMPTY"):
            items.append({
                "source": "self",
                "title": f["message"][:60],
                "task": (
                    f"이상 감지: {f['message']}\n{f['detail']}\n\n"
                    "원인이 진짜 필터인지 오탐인지 판단하라. 오탐이면 판정 로직을 고치고, "
                    "정상이면 아무것도 고치지 말고 왜 정상인지만 보고하라. "
                    "`python cli.py blocked` 와 `python cli.py health --no-notify` 로 확인할 수 있다."
                ),
            })

    return items


def gather(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """사람 지시가 항상 먼저다."""
    return _human_items(conn) + _self_items(conn)


# ─────────────────────── 실행 ───────────────────────


def _git(*args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=CODE_ROOT, capture_output=True, text=True, check=False)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {p.stderr.strip()[:200]}")
    return p.stdout.strip()


def run(conn: sqlite3.Connection | None = None, *, limit: int = 1) -> dict[str, Any]:
    """할 일을 모아 처리한다. 기본 1건인 이유: 브랜치가 쌓이면 사람이 검토를 못 한다."""
    own = conn is None
    conn = conn or connect()
    try:
        if not shutil.which("claude"):
            return {"processed": 0, "reason": "claude CLI 없음"}
        if _git("status", "--porcelain"):
            return {"processed": 0, "reason": "작업 트리 변경 있음 — 다음 실행으로 미룸"}

        items = gather(conn)[:limit]
        if not items:
            return {"processed": 0, "reason": "할 일 없음"}

        origin = _git("rev-parse", "--abbrev-ref", "HEAD")
        return {"processed": len(items), "items": [_one(conn, it, origin) for it in items]}
    finally:
        if own:
            conn.close()


def _one(conn: sqlite3.Connection, item: dict[str, Any], origin: str) -> dict[str, Any]:
    qid = item.get("queue_id")
    slug = f"q{qid}" if qid else f"self{_git('rev-parse', '--short', 'HEAD')}"
    branch = f"auto/{slug}"
    result: dict[str, Any] = {"source": item["source"], "title": item["title"], "branch": branch}

    if qid:
        conn.execute(
            "UPDATE control_queue SET status='running', started_at=?, branch=? WHERE id=?",
            (now(), branch, qid),
        )
        conn.commit()

    try:
        _git("switch", "-c", branch)
        out = _agent(item["task"])

        if _git("status", "--porcelain"):
            _git("add", "-A")
            _git("commit", "-q", "-m",
                 f"[auto/{slug}] {item['title']}\n\n"
                 f"출처: {item['source']}. 검토 후 병합할 것 — 자동 검증되지 않았다.")
            result.update(status="done", diff=_git("show", "--stat", "--oneline", "HEAD")[:600])
        else:
            result.update(status="skipped", diff="변경 없음")
        result["note"] = out[-600:]
    except UsageLimited as e:
        # 사용 한도. 실패로 기록하면 한도가 풀린 뒤에도 이 일을 다시 못 한다.
        # 큐로 되돌리고 조용히 물러난다 — 다음 스케줄 실행이 이어받는다.
        log.info("사용 한도 도달 — #%s 를 큐로 되돌린다", qid)
        result.update(status="requeued", note=str(e)[:300], diff="")
    except Exception as e:  # noqa: BLE001
        log.warning("오케스트레이터 작업 실패 (%s): %s", item["title"], e)
        result.update(status="failed", note=str(e)[:400], diff="")
    finally:
        # 무슨 일이 있어도 원래 브랜치로 돌아온다. 다음 사이클의 수집·판정이
        # 검증 안 된 브랜치 위에서 돌면 안 된다.
        _git("switch", origin, check=False)
        if qid:
            # requeued면 다시 queued로. 다음 실행이 처음부터 한다.
            final = "queued" if result["status"] == "requeued" else result["status"]
            conn.execute(
                "UPDATE control_queue SET status=?, result=?, finished_at=? WHERE id=?",
                (final, f"{result.get('diff','')}\n\n{result.get('note','')}"[:2000],
                 None if final == "queued" else now(), qid),
            )
            conn.commit()

    _report(conn, result, item)
    return result


def _agent(task: str) -> str:
    cfg = effective_config().get("llm", {})
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT")}
    cmd = [
        "claude", "-p", task,
        "--model", cfg.get("control_model", cfg.get("model", "claude-sonnet-5")),
        "--append-system-prompt", SYSTEM,
        "--allowed-tools", AGENT_TOOLS,
        "--permission-mode", "acceptEdits",
        "--no-session-persistence",
    ]
    p = subprocess.run(
        cmd, cwd=CODE_ROOT, capture_output=True, text=True,
        timeout=cfg.get("control_timeout_sec", 1800), check=False, env=env,
    )
    out = (p.stdout or "") + (p.stderr or "")
    # 한도는 고장이 아니다. 여기서 구분해야 큐로 되돌릴 수 있다.
    _raise_if_limited(out)
    return out.strip()


def _report(conn: sqlite3.Connection, r: dict[str, Any], item: dict[str, Any]) -> None:
    if r["status"] == "requeued":
        telegram.notify(
            conn,
            f"⏳ 사용 한도 도달 — <i>{item['title']}</i> 는 다음 실행으로 미룹니다.",
        )
        return
    icon = {"done": "🔧", "skipped": "➖", "failed": "❌"}.get(r["status"], "•")
    src = "폰 지시" if item["source"] == "human" else "자체 감지"
    lines = [f"{icon} <b>{src}</b> — {item['title']}", "", f"브랜치 <code>{r['branch']}</code>"]
    if r.get("diff"):
        lines += ["", f"<pre>{r['diff'][:450]}</pre>"]
    if r["status"] == "done":
        lines += ["", "검토 후 병합하세요. main에는 반영되지 않았습니다."]
    telegram.notify(conn, "\n".join(lines))
