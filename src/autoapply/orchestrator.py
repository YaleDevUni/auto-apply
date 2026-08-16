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


def _vision_brief(shot: str | None, question: str) -> str:
    """스크린샷을 **미리 읽어** 글로 바꾼다. 그 글을 할 일에 박아 넣는다.

    에이전트에게 경로만 주고 "읽어라"라고 하는 것과 다르다. 그건 읽을지 말지가
    에이전트에게 달렸고, 실제로 안 읽고 셀렉터를 추측으로 고친 적이 있다.
    여기서 먼저 읽어 텍스트로 만들면 **읽지 않을 수가 없다** — 할 일 본문이
    이미 화면 이야기다.

    싸기도 하다. 판독 한 번은 몇 초, 몇 센트다. 추측으로 고친 레시피가 다음
    사이클을 통째로 날리는 것보다 훨씬 싸다.

    실패하면 빈 문자열을 준다. 비전이 없다고 자가개선이 멈추면 안 된다 —
    스크린샷 경로는 어차피 할 일에 같이 들어간다.
    """
    if not shot:
        return ""
    from pathlib import Path as _P

    if not _P(shot).exists():
        return ""

    try:
        from . import vision

        if not vision.available():
            return ""
        out = vision.ask(shot, question, ignore=vision.DEFAULT_IGNORE)
    except Exception as e:  # noqa: BLE001
        log.info("증적 판독 건너뜀: %s", e)
        return ""

    return f"\n\n[화면 판독 — 위 스크린샷을 먼저 읽은 결과]\n{out.strip()[:1200]}\n"


def _editor_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """이력서 편집기 쪽 고장을 할 일로 바꾼다.

    이게 없으면 편집기가 망가져도 자가개선이 모른다. `apply_ledger`에는
    **지원 폼** 실패만 남고, 편집기는 그 앞 단계라 실패해도 거기 안 남는다.
    실제로 완성도가 71%에서 멈추던 몇 주 동안 오케스트레이터는 아무 신호도
    못 받았다 — 사람이 화면을 열어보고서야 알았다.

    신호는 `fill_report`다. 같은 증상이 **2건 이상** 쌓였을 때만 올린다.
    한 건은 그 공고만의 사정일 수 있고, 반복되면 레시피가 현실과 어긋난 것이다.
    """
    rows = conn.execute(
        """SELECT b.job_id, b.fill_report, b.completeness, j.platform
           FROM resume_builds b JOIN jobs j ON j.id = b.job_id
           WHERE b.fill_report IS NOT NULL AND b.fill_report != ''
           ORDER BY b.built_at DESC LIMIT 20"""
    ).fetchall()

    # 증상을 플랫폼 단위로 **한 덩어리**로 모은다. 쪼개면 안 되는 이유:
    # 71% 정체는 '저장 안 됨'·'플랫폼이 미입력'·'작성 완료 안 눌림'이 한꺼번에
    # 나타났지만 원인은 하나였다. 따로 올리면 같은 고장에 브랜치를 넷 파고
    # 에이전트를 네 번 돌린다 — 그리고 넷 다 같은 곳을 고치려 든다.
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            rep = json.loads(r["fill_report"] or "{}")
        except json.JSONDecodeError:
            continue

        found: list[str] = []
        if rep.get("lost"):
            found.append(f"입력했는데 저장 안 됨: {', '.join(rep['lost'][:4])}")
        if rep.get("platform_todo"):
            found.append(f"플랫폼이 미입력이라고 함: {rep['platform_todo'][0][:60]}")
        if rep.get("finalized") is False:
            found.append("'작성 완료'가 눌리지 않음")
        if len(rep.get("skills_skipped") or []) >= 3:
            found.append(f"스킬 {len(rep['skills_skipped'])}개 등록 실패")
        if not found:
            continue

        e = seen.setdefault(
            r["platform"], {"jobs": [], "symptoms": {}, "platform": r["platform"], "shot": ""}
        )
        e["jobs"].append(r["job_id"])
        e["shot"] = e["shot"] or (rep.get("shot") or "")
        for f in found:
            key = f.split(":")[0]
            e["symptoms"].setdefault(key, f)
            e[key] = e.get(key, 0) + 1

    items: list[dict[str, Any]] = []
    for platform, e in seen.items():
        # 한 건은 그 공고만의 사정일 수 있다. 반복돼야 레시피 문제다.
        if len(e["jobs"]) < 2:
            continue
        lines = "\n".join(f"  - {v}" for v in e["symptoms"].values())
        brief = _vision_brief(
            e.get("shot"),
            "이 화면은 이력서 편집기를 자동으로 채운 직후다.\n"
            "보이는 것만 사실대로 적어라:\n"
            "1. 완성도 퍼센트와 그 옆 체크리스트에서 **체크가 안 된 항목**\n"
            "2. 빨간 별표(*)가 붙었는데 값이 없는 칸의 이름\n"
            "3. 오류·경고 문구가 있으면 그대로\n"
            "4. '작성 완료' 버튼이 보이는지, 눌릴 수 있어 보이는지\n"
            "추측하지 마라.",
        )
        items.append({
            "source": "self",
            "title": f"{platform} 이력서 편집기 이상 {len(e['jobs'])}건",
            "task": (
                f"{platform} 이력서 편집기에서 이상이 {len(e['jobs'])}건 반복됐다.\n"
                f"증상:\n{lines}\n"
                f"해당 공고: {', '.join(str(j) for j in e['jobs'][:6])}\n"
                + (f"편집기 화면: {e['shot']}\n" if e.get("shot") else "")
                + f"{brief}\n"
                "이 증상들은 **한 원인의 여러 얼굴일 가능성이 높다.** 각각을 따로 "
                "고치려 들지 말고 공통 원인을 먼저 찾아라.\n\n"
                f"`python cli.py builds --limit 10` 으로 기록을 먼저 보라. "
                f"recipes/{platform}.json 의 editor 섹션이 현재 화면과 맞는지 "
                "확인하고 고쳐라.\n\n"
                "**주의**: 사본(copy) 경로에서는 학력·링크·언어를 일부러 건너뛴다. "
                "그 섹션이 비었다는 신고는 고장이 아니다 — resume_editor.COPY_STEPS를 "
                "먼저 확인하라.\n"
                "검증은 `python cli.py resume <job_id>` (dry-run)로 한다."
            ),
        })
    return items


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
            brief = _vision_brief(
                f["shot"],
                "이 화면은 채용 지원 폼을 자동으로 채우다 실패한 순간이다.\n"
                "지금 화면에 **무엇이 보이는지** 사실만 적어라:\n"
                "1. 어떤 페이지인가 (지원 폼 / 로그인 / 오류 / 목록 중 무엇인가)\n"
                "2. 오류 메시지·경고 배너가 있으면 그 문구 그대로\n"
                "3. 버튼과 입력칸의 이름 (자동화가 찾아야 할 것들)\n"
                "4. 로그인이 풀린 정황이 있는가\n"
                "추측하지 말고 보이는 것만 적어라.",
            )
            items.append({
                "source": "self",
                "title": f"{f['platform']} 지원 실패 {f['n']}건",
                "task": (
                    f"{f['platform']} 플랫폼 지원이 {f['n']}건 실패했다. "
                    f"마지막 오류: {f['err']}\n"
                    f"증적 스크린샷: {f['shot']}\n\n"
                    f"**먼저 위 스크린샷을 Read 도구로 읽어라.** 화면이 어떻게 달라졌는지 "
                    "보지 않고 셀렉터를 고치면 추측일 뿐이다."
                    f"{brief}\n"
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
    return _human_items(conn) + _self_items(conn) + _editor_items(conn)


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
