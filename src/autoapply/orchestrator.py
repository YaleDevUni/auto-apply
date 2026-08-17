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

## 계획과 수행을 다른 세션으로 가른다

    plan()      읽기 전용. 무엇이 왜 깨졌고 어떻게 고칠지 + 위험도를 낸다
    execute()   그 계획만 읽고 새 세션에서 고친다

세션을 가르는 이유는 **승인 지연**이다. 새벽 3시에 만든 계획의 승인 버튼은
아침에나 눌린다 — 한 세션이 그동안 떠 있을 수 없다. 계획을 `fix_plans`에
굳혀두면 계획 세션은 그 자리에서 끝나고, 승인이 왔을 때 새 프로세스가
이어받는다. 덤으로 계획 세션이 코드를 뒤지며 쌓은 컨텍스트(수십 파일)가
수행 세션에 안 딸려온다.

## main에 무엇이 가고 무엇이 안 가나

예전에는 "코드 수정은 main에 절대 안 닿는다"였다. 그 규칙에는 대가가 있었다 —
새벽에 셀렉터가 깨지면 브랜치에 수정이 쌓일 뿐, **다음 사이클은 여전히 깨진
코드로 돈다.** 사람이 아침에 병합할 때까지 그 밤이 통째로 날아간다.

지금은 위험도로 가른다:

    low + 검증 통과 + 결정론 검사 통과   →  main에 커밋. 다음 사이클이 고쳐진 채 돈다
    그 밖 전부                          →  브랜치에만. 사람이 보고 병합한다

**위험도는 에이전트의 자기 채점이다.** 낮게 매기고 틀리는 경우를 막는 건 그
점수가 아니라 실행 결과다. 그래서 자동반영에는 조건 셋이 다 필요하다 —
계획이 low라고 말했고, 계획에 적힌 **검증 명령이 실제로 통과**했고,
`_auto_gate()`의 결정론 검사를 지났을 때만 간다.

## 자동반영이라도 절대 안 건드리는 세 곳

`_auto_gate()`가 커밋 직전 diff를 훑어 막는다. 셋 다 틀리면 **바깥세상에
되돌릴 수 없는 결과**를 남긴다 — 되돌릴 수 있는 코드 실수와는 무게가 다르다.

    제출 경로     엉뚱한 회사에 지원이 나간다
    폭주 방어선   200곳에 연달아 지원이 나간다
    중복 방어선   같은 곳에 두 번 지원한다

## 도는 동안 자동지원을 붙잡는다

고장 난 채로 지원을 계속 내보내면 고치는 중에도 같은 고장으로 자리가 소모된다.
`hold_for_fix()`로 붙잡고 끝나면 푼다. 사람이 건 `/pause`와 **다른 열쇠**를
쓴다 — 하나로 합치면 자가복구가 끝나며 푸는 순간 사람이 걸어둔 정지까지 같이
풀린다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from typing import Any

from .config import effective_config
from .db import connect, now
from .llm import LoginExpired, UsageLimited, raise_if_limited_failure
from .notify import telegram
from .paths import CODE_ROOT

log = logging.getLogger(__name__)

# 계획은 **읽기만** 한다. Edit/Write를 주지 않는 이유는 단순하다 — 계획 단계에서
# 코드가 바뀌면 그 뒤의 위험도 판정과 검증이 전부 헛것이 된다. 무엇을 고칠지
# 정하는 눈과 고치는 손은 갈라야 한다.
PLANNER_TOOLS = "Read,Grep,Glob,Bash"
FIXER_TOOLS = "Read,Edit,Write,Grep,Glob,Bash"

# 하네스. 띄우는 에이전트에게만 건다 — 프로젝트 settings.json에 넣으면 사람의
# 대화형 세션까지 같이 묶여서, 자기 저장소에서 git push를 못 하게 된다.
GUARD_SETTINGS = ".claude/agent-guard.json"

# 검증 명령으로 허용하는 것. 계획이 문자열로 준 명령을 그대로 셸에 넘기면
# 계획 에이전트가 사실상 임의 실행 권한을 갖는다 — 하네스는 에이전트의 Bash를
# 막지 이 자리를 막지 않는다. 그래서 cli.py의 **읽기·dry-run 명령만** 통과시킨다.
VERIFY_ALLOWED = re.compile(
    r"^(?:\.venv/bin/)?python3?\s+cli\.py\s+"
    r"(resume|apply|builds|health|status|blocked|targets|quota|errors|llm-cost|where)\b"
)

# 에이전트가 증적 스크린샷을 읽을 수 있어야 한다. 화면을 안 보고 셀렉터를 고치는
# 것은 추측이고, 이 프로젝트에서 추측으로 고친 것은 대부분 틀렸다.
EVIDENCE_HINT = (
    "실패 증적 스크린샷이 주어지면 반드시 Read 도구로 먼저 읽어라. "
    "화면을 보지 않고 셀렉터를 바꾸지 마라."
)

_COMMON = (
    "당신은 이 저장소를 유지보수하는 개발자입니다. 저장소의 CLAUDE.md와 README를 "
    "먼저 읽고 설계 의도를 파악한 뒤 작업하십시오.\n"
    "원칙: 정규식·단순 파싱으로 되는 구간에 LLM을 넣지 마십시오. "
    "되돌릴 수 없는 동작(실제 지원 제출)은 절대 실행하지 마십시오.\n" + EVIDENCE_HINT
)

PLANNER_SYSTEM = (
    _COMMON
    + "\n당신은 **계획만** 세웁니다. 코드를 고치지 마십시오 — 편집 도구가 없습니다.\n"
    "최근 커밋을 확인하고 중복 수정하지않도록 주의하십시오"
    "원인을 추측하지 말고 실제로 파일을 읽어 확인하십시오. 근거 없이 쓴 계획은 "
    "수행 단계에서 그대로 잘못된 수정이 됩니다.\n\n"
    "위험도(risk)를 정직하게 매기십시오. 이 값으로 사람 승인 없이 반영할지가 "
    "갈립니다:\n"
    "  low    무엇이 잘못됐는지 확실하고, 고칠 범위가 좁고, 틀려도 dry-run 검증에서 "
    "걸린다. 셀렉터·타임아웃·문구 수정이 대개 여기다\n"
    "  medium 원인은 알겠으나 판정·조립 로직처럼 결과를 기계적으로 확인하기 어려운 곳\n"
    "  high   원인이 불확실하거나, 지원 제출·중복차단·한도처럼 틀리면 바깥세상에 "
    "되돌릴 수 없는 결과를 남기는 곳\n\n"
    "확신이 없으면 낮게 매기지 마십시오. low로 잘못 매긴 수정은 사람이 자는 동안 "
    "그대로 반영됩니다."
)

FIXER_SYSTEM = (
    _COMMON
    + "\n승인된 계획을 그대로 수행하십시오. 계획에 없는 것을 덤으로 고치지 마십시오 — "
    "무엇이 무엇을 깨뜨렸는지 못 가리게 됩니다.\n"
    "수행 중 계획이 틀렸다는 것을 알게 되면, 억지로 맞추지 말고 무엇이 달랐는지 "
    "마지막에 적고 최소한의 수정만 하십시오.\n"
    "커밋은 하지 마십시오 — 호출자가 합니다."
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


def self_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """A 파이프라인이 남긴 신호를 할 일로 바꾼다. LLM 호출 0회로 판단한다.

    `gather()`가 내부에서 쓰지만, `night-cycle`도 "improve를 자동으로 부를지"
    판단하는 데 이 함수 하나만 필요하다(사람 지시 큐는 안 본다 — 그건 사람이
    `/improve`로 직접 불러야 처리된다). 그래서 공개 함수로 둔다.
    """
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


def _error_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """고장 큐를 할 일로 바꾼다.

    `self_items()`와 겹치지 않는다. 저쪽은 **미리 정의한 증상**(플랫폼별 지원실패
    2건 이상, fill_report 이상)을 보고, 이쪽은 **처음 보는 고장**을 본다. 실제로
    이 저장소에서 잡힌 고장의 상당수가 저쪽 정의에 안 걸리는 종류였다.
    """
    from . import errors

    items: list[dict[str, Any]] = []
    for e in errors.open_items(conn):
        ctx = e.get("context") or {}
        shot = ctx.get("evidence_path") or ""
        brief = _vision_brief(
            shot,
            "이 화면은 자동화가 실패한 순간이다. 보이는 것만 사실대로 적어라:\n"
            "1. 어떤 페이지인가\n2. 오류·경고 문구가 있으면 그대로\n"
            "3. 버튼과 입력칸의 이름\n4. 로그인이 풀린 정황이 있는가\n추측하지 마라.",
        )
        items.append({
            "source": "error",
            "error_id": e["id"],
            "title": f"{e['exc_type']}: {(e['message'] or '')[:50]}",
            "task": (
                f"고장이 {e['count']}번 났다 (지문 {e['fingerprint']}).\n"
                f"명령: {e['command']}\n"
                f"예외: {e['exc_type']}: {e['message']}\n"
                + (f"공고: {ctx['job_id']}\n" if ctx.get("job_id") else "")
                + (f"증적 화면: {shot}\n" if shot else "")
                + f"{brief}\n"
                f"트레이스백:\n{(e.get('traceback') or '')[-2000:]}\n\n"
                "트레이스백이 가리키는 파일을 **실제로 읽고** 원인을 확인하라. "
                "추측으로 고치지 마라."
            ),
        })
    return items


def gather(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """사람 지시가 항상 먼저다.

    시스템이 스스로 찾은 문제보다 사람이 아는 문제가 더 급한 경우가 대부분이고,
    무엇보다 사람이 방향을 바꾸려는데 자기 할 일을 먼저 하는 오케스트레이터는
    통제 불가능하다.
    """
    return _human_items(conn) + _error_items(conn) + self_items(conn) + _editor_items(conn)


# ─────────────────────── 실행 ───────────────────────


def _git(*args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=CODE_ROOT, capture_output=True, text=True, check=False)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {p.stderr.strip()[:200]}")
    return p.stdout.strip()


def _role_cfg(role: str) -> dict[str, Any]:
    """모델·effort는 config.yaml에서 바꾼다. 기본은 둘 다 Opus 5 / effort 높음.

    둘을 따로 두는 이유: 나중에 한쪽만 내리고 싶어진다. 계획은 비싸도 정확해야
    하고, 계획이 정확하면 수행은 더 싼 모델로도 된다.
    """
    cfg = effective_config().get("orchestrator", {}).get(role, {})
    default_timeout = 1800 if role == "planner" else 3600
    return {
        "model": cfg.get("model", "claude-opus-5"),
        "effort": cfg.get("effort", "high"),
        "timeout": int(cfg.get("timeout_sec", default_timeout)),
    }


def _agent(task: str, *, role: str, system: str, tools: str) -> str:
    """코딩 에이전트 한 번. 하네스를 반드시 걸고 부른다.

    `--settings`로 거는 이유는 `.claude/settings.json`에 넣으면 사람의 대화형
    세션까지 같이 묶이기 때문이다. 사람이 자기 저장소에서 git push를 못 하게
    되는 건 말이 안 된다.
    """
    cfg = _role_cfg(role)
    # 중첩 실행 표식을 지운다. 이게 남으면 자식 claude가 자기를 부모 세션의
    # 일부로 여겨 엉뚱하게 동작한다.
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT")}
    cmd = [
        "claude", "-p", task,
        "--model", cfg["model"],
        "--effort", cfg["effort"],
        "--append-system-prompt", system,
        "--allowed-tools", tools,
        "--settings", GUARD_SETTINGS,
        "--permission-mode", "acceptEdits",
        "--no-session-persistence",
    ]
    log.info("%s 에이전트 시작 (model=%s, effort=%s)", role, cfg["model"], cfg["effort"])
    p = subprocess.run(
        cmd, cwd=CODE_ROOT, capture_output=True, text=True,
        timeout=cfg["timeout"], check=False, env=env,
    )
    out = (p.stdout or "") + (p.stderr or "")
    # exit 코드를 안 보면 **실패한 실행의 출력이 그대로 계획이 된다.** 실측:
    # 월 지출 한도에 걸린 claude가 exit 1 + 한 줄 안내문만 내는데, 그것이
    # `_parse_plan`을 지나 "계획을 읽지 못했습니다"라는 high 계획 #4로 저장되고
    # 승인 대기에 올라 파이프라인을 붙잡았다(fix hold까지 걸린 채로).
    #
    # 한도는 고장이 아니다 — 여기서 갈라야 `plan()`이 자리를 큐에 돌려놓고
    # 다음 기회에 다시 한다. 판정은 llm 쪽과 같은 함수를 쓴다.
    if p.returncode != 0:
        message = raise_if_limited_failure(p.stdout or "", p.stderr or "")
        raise RuntimeError(f"{role} 에이전트 실패 (exit {p.returncode}): {message}")
    # 성공 출력은 더 이상 한도로 안 본다. 여기 오는 것은 코딩 에이전트의 출력
    # 전문이라 "rate limit", "사용 한도"를 **논의하는 계획문**이 그대로 걸린다
    # (재현 확인). 그러면 멀쩡한 계획이 한도로 오인돼 통째로 버려진다. 한도는
    # exit≠0으로 오므로(실측) 위 분기가 전부 잡는다.
    return out.strip()


# ─────────────────────── 계획 ───────────────────────

PLAN_FORMAT = """
마지막에 아래 JSON을 ```json 코드블록 하나로 출력하라. 설명은 그 앞에 쓴다.

```json
{
  "title": "한 줄 제목",
  "cause": "실제로 파일을 읽고 확인한 원인",
  "files": ["고칠 파일 경로"],
  "steps": ["무엇을 어떻게 고칠지 단계별로"],
  "verify": "python cli.py resume 283",
  "risk": "low",
  "risk_reason": "왜 그 위험도인지"
}
```

`verify`는 **실제로 돌릴 수 있는 한 줄 명령**이어야 한다. 다음만 쓸 수 있다:
`python cli.py` 의 resume / apply / builds / health / status / blocked / targets /
quota / errors / llm-cost / where. `--live` 는 절대 쓰지 마라 — 실제 지원이 나간다.
검증할 방법이 마땅치 않으면 verify를 빈 문자열로 두고 risk를 medium 이상으로 매겨라.
"""


def _parse_plan(text: str) -> dict[str, Any]:
    """계획 JSON을 꺼낸다. 못 꺼내면 사람이 보게 만든다.

    파싱 실패를 low로 흘려보내면 안 된다 — 무엇을 하겠다는 건지 기계가 못 읽은
    계획이 승인 없이 main에 반영되는 것이 최악이다. 그래서 실패는 high로 굳힌다.
    """
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not blocks:
        # 코드블록 없이 낸 경우까지는 봐준다 — 마지막 중괄호 덩어리를 시도한다.
        blocks = re.findall(r"(\{[^{}]*\"risk\"[^{}]*\})", text, re.DOTALL)
    for raw in reversed(blocks):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        risk = str(d.get("risk", "high")).lower()
        return {
            "title": str(d.get("title") or "제목 없음")[:120],
            "cause": str(d.get("cause") or "")[:2000],
            "files": d.get("files") or [],
            "steps": d.get("steps") or [],
            "verify": str(d.get("verify") or "").strip(),
            "risk": risk if risk in ("low", "medium", "high") else "high",
            "risk_reason": str(d.get("risk_reason") or "")[:500],
            "parsed": True,
        }
    return {
        "title": "계획을 읽지 못했습니다 — 사람이 확인 필요",
        "cause": "", "files": [], "steps": [], "verify": "",
        "risk": "high", "risk_reason": "계획 JSON 파싱 실패", "parsed": False,
    }


def plan(conn: sqlite3.Connection | None = None, *, limit: int = 1) -> dict[str, Any]:
    """할 일 하나를 골라 계획을 세운다. 코드는 건드리지 않는다.

    한 번에 하나만 하는 이유는 예전 `run()`과 같다 — 여러 개를 동시에 하면
    무엇이 무엇을 깨뜨렸는지 못 가린다. 게다가 계획마다 승인이 필요하므로
    여러 개를 한꺼번에 올리면 폰이 도배된다.
    """
    from .notify.listener import hold_for_fix, release_fix_hold

    own = conn is None
    conn = conn or connect()
    try:
        if not shutil.which("claude"):
            return {"planned": 0, "reason": "claude CLI 없음"}

        items = gather(conn)[:limit]
        if not items:
            return {"planned": 0, "reason": "할 일 없음"}
        item = items[0]

        # 고장 난 채로 지원을 계속 내보내지 않는다. 계획 단계부터 붙잡는 이유는
        # 계획이 수 분 걸리고 그 사이에도 새벽 루프가 자리를 소모하기 때문이다.
        hold_for_fix(conn, None, item["title"])

        try:
            out = _agent(
                f"{item['task']}\n\n{PLAN_FORMAT}",
                role="planner", system=PLANNER_SYSTEM, tools=PLANNER_TOOLS,
            )
        except (UsageLimited, LoginExpired) as e:
            # 알림 문구를 여기서 다시 쓰지 않는다. 무엇이 막았고 무엇을 해야
            # 푸는지는 errors 쪽에 한 벌만 있어야 한다 — 두 벌이면 로그인
            # 만료를 "기다리면 풀립니다"로 안내하는 쪽이 반드시 생긴다.
            from . import errors

            release_fix_hold(conn)
            errors.record(conn, kind="plan", exc=e, command="plan")
            return {"planned": 0, "reason": type(e).__name__}
        except Exception as e:  # noqa: BLE001
            release_fix_hold(conn)
            log.warning("계획 수립 실패: %s", e)
            telegram.notify(conn, f"❌ 계획 수립 실패 — <i>{type(e).__name__}: {str(e)[:200]}</i>")
            return {"planned": 0, "reason": str(e)[:200]}

        parsed = _parse_plan(out)
        plan_id = _save_plan(conn, item, parsed, out)
        hold_for_fix(conn, plan_id, parsed["title"])
        _mark_sources(conn, item, "planned", plan_id)

        # low는 사람을 깨우지 않고 바로 고친다. 그게 이 기능의 목적이다 —
        # 새벽에 깨져도 아침엔 고쳐진 채로 돌아 있어야 한다.
        if parsed["risk"] == "low":
            log.info("위험도 low — 승인 없이 수행한다 (계획 #%s)", plan_id)
            conn.execute("UPDATE fix_plans SET auto=1 WHERE id=?", (plan_id,))
            conn.commit()
            return {"planned": 1, "plan_id": plan_id, "risk": "low",
                    "execute": execute(conn, plan_id)}

        _request_approval(conn, plan_id, parsed)
        return {"planned": 1, "plan_id": plan_id, "risk": parsed["risk"], "awaiting": True}
    finally:
        if own:
            conn.close()


def _save_plan(
    conn: sqlite3.Connection, item: dict[str, Any], parsed: dict[str, Any], raw: str
) -> int:
    sources = [{"source": item["source"], "queue_id": item.get("queue_id"),
                "error_id": item.get("error_id"), "title": item["title"]}]
    cur = conn.execute(
        """INSERT INTO fix_plans
             (created_at, sources, title, cause, files, steps, verify, raw, risk)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (now(), json.dumps(sources, ensure_ascii=False), parsed["title"], parsed["cause"],
         json.dumps(parsed["files"], ensure_ascii=False),
         json.dumps(parsed["steps"], ensure_ascii=False),
         parsed["verify"], raw[-8000:], parsed["risk"]),
    )
    conn.commit()
    return int(cur.lastrowid)


def _mark_sources(
    conn: sqlite3.Connection, item: dict[str, Any], status: str, plan_id: int | None
) -> None:
    """계획/수행 결과를 원래 출처에 되돌려 적는다. 이게 없으면 같은 고장으로
    계획이 계속 다시 만들어진다."""
    if item.get("error_id"):
        from . import errors

        errors.mark(conn, [int(item["error_id"])], status, plan_id)
    if item.get("queue_id"):
        conn.execute(
            "UPDATE control_queue SET status=? WHERE id=?",
            ("running" if status == "planned" else status, item["queue_id"]),
        )
        conn.commit()


# ─────────────────────── 자동반영 관문 ───────────────────────

# 자동반영이 절대 손대면 안 되는 곳. 계획이 low라고 말해도 여기 걸리면 내린다.
#
# 셋 다 틀렸을 때 **바깥세상에 되돌릴 수 없는 결과**를 남긴다는 공통점이 있다.
# 코드 실수는 되돌리면 그만이지만, 나간 지원서는 회수가 안 된다.
AUTO_FORBIDDEN_PATHS = (
    "src/autoapply/agent.py",        # claim() — 중복·폭주 방어선
    "src/autoapply/db.py",           # apply_ledger UNIQUE — 스키마 방어선
    "src/autoapply/runner/apply.py",  # 제출 경로
    "config.yaml",                   # limits
)

AUTO_FORBIDDEN_PATTERNS = (
    (r"max_per_(day|run)", "일일·회차 지원 상한"),
    (r"--live|mark_submitted|def _submit", "실제 제출 경로"),
    (r"canonical_key", "중복지원 차단 키"),
    (r"def claim|_claimed_today", "선점 관문"),
)


def _auto_gate(diff: str) -> tuple[bool, str]:
    """이 변경을 승인 없이 main에 넣어도 되나. 결정론 검사다.

    위험도는 에이전트의 자기 채점이라, 낮게 매기고 틀리는 경우를 여기서 막는다.
    막히면 실패가 아니라 **강등**이다 — 브랜치에 남기고 사람에게 묻는다.
    """
    files = re.findall(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE)
    for f in files:
        if f in AUTO_FORBIDDEN_PATHS:
            return False, f"{f}는 지원·중복·한도 방어선이 있는 파일"

    # 파일이 허용 목록이어도 내용이 방어선을 건드릴 수 있다(옮겨온 코드 등).
    changed = "\n".join(
        ln for ln in diff.splitlines() if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
    )
    for pattern, label in AUTO_FORBIDDEN_PATTERNS:
        if re.search(pattern, changed):
            return False, f"방어선을 건드림 — {label}"
    return True, ""


def _run_verify(command: str) -> tuple[bool, str]:
    """계획이 적어준 검증 명령을 실제로 돌린다.

    임의 문자열을 셸에 넘기지 않는다. 그러면 계획 에이전트가 사실상 임의 실행
    권한을 갖는다 — 하네스는 **에이전트의 Bash**를 막지 이 자리를 막지 않는다.
    허용 목록에 없으면 실행하지 않고 "검증 못 함"으로 돌려보내며, 그건 곧
    자동반영 자격 없음이다.
    """
    import shlex

    cmd = (command or "").strip()
    if not cmd:
        return False, "검증 명령이 없다"
    if "--live" in cmd:
        return False, "검증 명령에 --live가 있다 — 실행하지 않는다"
    # shell=False로 돌리므로 `;`나 `|`가 실제로 두 번째 명령이 되지는 않는다.
    # 그래도 막는 이유는 그런 문자열이 왔다는 것 자체가 계획이 셸을 기대했다는
    # 뜻이고, 그 계획은 검증이 무엇을 확인하는지 잘못 알고 있다는 신호다.
    if re.search(r"[;&|`$><]|\$\(", cmd):
        return False, f"검증 명령에 셸 문법이 섞여 있다: {cmd[:80]}"
    if not VERIFY_ALLOWED.match(cmd):
        return False, f"허용되지 않은 검증 명령: {cmd[:80]}"

    argv = shlex.split(cmd)
    if argv[0].startswith("python"):
        argv[0] = str(CODE_ROOT / ".venv/bin/python")
    try:
        p = subprocess.run(
            argv, cwd=CODE_ROOT, capture_output=True, text=True, timeout=1800, check=False
        )
    except subprocess.TimeoutExpired:
        return False, "검증 명령이 30분 안에 끝나지 않았다"
    tail = ((p.stdout or "") + (p.stderr or ""))[-800:]
    return p.returncode == 0, tail


def _backup_db() -> str:
    """수행 전에 DB를 복사해 둔다. 하네스가 뚫려도 되돌릴 수 있는 마지막 줄이다."""
    from pathlib import Path

    from .paths import DATA_DIR, DB_PATH

    if not DB_PATH.exists():
        return ""
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"jobs-{now().replace(':', '').replace('-', '')[:15]}.db"
    shutil.copy2(DB_PATH, dest)

    keep = sorted(backup_dir.glob("jobs-*.db"))[:-5]
    for old in keep:
        Path(old).unlink(missing_ok=True)
    return str(dest)


# ─────────────────────── 수행 ───────────────────────


def execute(conn: sqlite3.Connection | None = None, plan_id: int = 0) -> dict[str, Any]:
    """승인됐거나 low로 판정된 계획을 실제로 수행한다.

    계획 세션과 **다른 프로세스**에서 도는 것이 정상이다 — 승인 버튼은 몇 시간
    뒤에 눌리고, 그때 이 함수만 계획을 읽고 시작한다.
    """
    from .notify.listener import release_fix_hold

    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM fix_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            return {"error": f"계획 {plan_id}이 없다"}
        if row["status"] in ("done", "rejected", "running"):
            return {"skipped": f"이미 {row['status']} 상태"}

        if _git("status", "--porcelain"):
            # 사람의 미커밋 작업 위에 에이전트를 풀어놓지 않는다.
            telegram.notify(
                conn, f"⏸ 계획 #{plan_id} 수행을 미룹니다 — 작업 트리에 커밋 안 된 변경이 있습니다."
            )
            return {"skipped": "작업 트리 변경 있음"}

        origin = _git("rev-parse", "--abbrev-ref", "HEAD")
        branch = f"auto/fix{plan_id}"
        conn.execute(
            "UPDATE fix_plans SET status='running', branch=? WHERE id=?", (branch, plan_id)
        )
        conn.commit()

        backup = _backup_db()
        result: dict[str, Any] = {"plan_id": plan_id, "branch": branch, "backup": backup}

        try:
            _git("switch", "-c", branch)
            out = _agent(_fixer_task(row), role="fixer", system=FIXER_SYSTEM, tools=FIXER_TOOLS)
            result["note"] = out[-800:]

            if not _git("status", "--porcelain"):
                result.update(status="skipped", reason="에이전트가 아무것도 바꾸지 않았다")
            else:
                _git("add", "-A")
                _git("commit", "-q", "-m", _commit_message(row))
                result.update(_land(conn, row, origin, branch))
        except (UsageLimited, LoginExpired) as e:
            # 한도도 로그인 만료도 고장이 아니다 — 계획을 살려두고 물러난다.
            # (푸는 방법은 다르지만, 여기서 할 일은 "계획을 버리지 않는다"로 같다.)
            from . import errors

            errors.record(conn, kind="fix-run", exc=e, command=f"fix-run {plan_id}")
            result.update(status="requeued", reason=f"{type(e).__name__}: {str(e)[:180]}")
            conn.execute(
                "UPDATE fix_plans SET status=? WHERE id=?",
                ("approved" if row["auto"] == 0 else "pending", plan_id),
            )
            conn.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("수행 실패 (계획 #%s): %s", plan_id, e)
            result.update(status="failed", reason=f"{type(e).__name__}: {str(e)[:300]}")
        finally:
            # 무슨 일이 있어도 원래 브랜치로 돌아온다. 다음 사이클의 수집·판정이
            # 검증 안 된 브랜치 위에서 돌면 안 된다.
            _git("switch", origin, check=False)

        if result.get("status") != "requeued":
            conn.execute(
                "UPDATE fix_plans SET status=?, result=?, finished_at=?, commit_sha=? WHERE id=?",
                (result.get("status", "failed"), json.dumps(result, ensure_ascii=False)[:4000],
                 now(), result.get("commit_sha"), plan_id),
            )
            conn.commit()
            _resolve_sources(conn, row, result)

        # 고쳤든 못 고쳤든 파이프라인은 놓아준다. 붙잡은 채로 두면 다음 새벽이
        # 통째로 안 돈다 — 고장 하나가 파이프라인 전체를 인질로 잡는 꼴이다.
        release_fix_hold(conn)
        _report_result(conn, row, result)
        return result
    finally:
        if own:
            conn.close()


def _land(
    conn: sqlite3.Connection, row: sqlite3.Row, origin: str, branch: str
) -> dict[str, Any]:
    """커밋된 수정을 어디에 착지시킬지 정한다. main이냐 브랜치냐.

    main으로 가는 길은 둘이다:

        low 자동   위험도 low + 검증 명령 실제 통과 + 결정론 관문 통과
        사람 승인  row["status"]=="approved" — 폰에서 ✅를 눌렀다

    사람 승인 뒤에도 브랜치에만 남기고 또 `git merge`를 기다리던 예전 방식은
    이중 관문이었다 — 승인이 이미 "이 수정을 반영해라"라는 결정인데, 그 결정을
    실제로 반영하는 손동작만 사람에게 남겨둔 것이다. 이제 승인되면 그 자리에서
    main까지 간다. 단, 결정론 관문(`_auto_gate`)은 승인 여부와 무관하게
    **무조건** 건다 — 사람은 계획 **문구**를 승인했을 뿐 diff를 본 게 아니라서,
    지원·중복·한도 방어선은 그대로 지켜야 한다. 방어선에 걸리면 강등이고,
    강등된 계획도 다시 승인하면(재시도) 여기를 다시 탄다.
    """
    diff = _git("show", "HEAD", "--unified=0")
    stat = _git("show", "--stat", "--oneline", "HEAD")[:600]

    approved = row["status"] == "approved"
    if row["risk"] != "low" and not approved:
        return {"status": "done", "landed": "branch", "stat": stat,
                "reason": f"위험도 {row['risk']} — 승인 대기"}

    gate_ok, gate_reason = _auto_gate(diff)
    if not gate_ok:
        return {"status": "demoted", "landed": "branch", "stat": stat,
                "reason": f"자동반영 불가: {gate_reason}"}

    # 검증 명령은 low 자동반영에는 필수다 — 사람 검토가 없으니 그게 유일한
    # 증거다. 사람이 이미 승인한 medium/high는 명령이 없어도(계획이 "검증할
    # 방법이 마땅치 않다"고 적어 risk를 올렸을 수 있다) 막지 않는다 — 승인
    # 자체가 그 자리를 대신한다.
    if row["verify"]:
        verify_ok, verify_out = _run_verify(row["verify"])
        if not verify_ok:
            return {"status": "demoted", "landed": "branch", "stat": stat,
                    "reason": f"검증 실패: {verify_out[-300:]}"}
    elif not approved:
        return {"status": "demoted", "landed": "branch", "stat": stat,
                "reason": "검증 명령이 없다"}
    else:
        verify_out = "(사람 승인 — 자동 검증 명령 없음)"

    # 관문을 지났다. main으로 보낸다 — 다음 사이클이 고쳐진 채 돈다.
    _git("switch", origin)
    _git("merge", "--ff-only", branch)
    sha = _git("rev-parse", "--short", "HEAD")
    log.info("반영 — %s 를 %s 에 병합 (%s)", branch, origin, sha)
    return {"status": "done", "landed": "main", "stat": stat, "commit_sha": sha,
            "verify": verify_out[-300:]}


def _fixer_task(row: sqlite3.Row) -> str:
    steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(json.loads(row["steps"] or "[]"), 1))
    files = ", ".join(json.loads(row["files"] or "[]"))
    return (
        f"승인된 수정 계획을 수행하라.\n\n"
        f"제목: {row['title']}\n"
        f"원인: {row['cause']}\n"
        f"고칠 파일: {files}\n"
        f"단계:\n{steps}\n\n"
        f"끝나면 이 명령이 통과해야 한다: {row['verify'] or '(검증 명령 없음)'}\n\n"
        "계획에 없는 것을 덤으로 고치지 마라. 커밋은 하지 마라 — 호출자가 한다."
    )


def _commit_message(row: sqlite3.Row) -> str:
    """폰으로 나가는 것이 이 메시지다. 무엇을 했는지가 아니라 왜 그랬는지를 적는다."""
    steps = json.loads(row["steps"] or "[]")
    body = "\n".join(f"- {s}" for s in steps[:6])
    return (
        f"{row['title']}\n\n"
        f"{row['cause']}\n\n"
        f"{body}\n\n"
        f"자가복구 계획 #{row['id']} (위험도 {row['risk']}). "
        f"검증: {row['verify'] or '없음'}"
    )


# ─────────────────────── 보고 ───────────────────────


def _resolve_sources(conn: sqlite3.Connection, row: sqlite3.Row, result: dict[str, Any]) -> None:
    """고장 큐에서 뺀다. **main에 실제로 반영됐을 때만** fixed로 끈다.

    브랜치에만 있는 수정은 아직 아무것도 고치지 않은 것이다 — 다음 사이클은
    여전히 깨진 코드로 돈다. 그걸 fixed로 끄면 같은 고장이 다시 나도 "이미
    고쳤다"며 큐에 안 올라온다.
    """
    from . import errors

    landed = result.get("landed") == "main" and result.get("status") == "done"
    status = "fixed" if landed else "open"
    for src in json.loads(row["sources"] or "[]"):
        if src.get("error_id"):
            errors.mark(conn, [int(src["error_id"])], status, row["id"])
        if src.get("queue_id"):
            conn.execute(
                "UPDATE control_queue SET status=?, result=?, finished_at=? WHERE id=?",
                ("done" if landed else "queued", result.get("stat", "")[:2000],
                 now() if landed else None, src["queue_id"]),
            )
            conn.commit()


def _request_approval(conn: sqlite3.Connection, plan_id: int, parsed: dict[str, Any]) -> bool:
    """승인을 요청한다. 새벽이면 아침까지 쌓아둔다.

    새벽 3시에 폰을 울려도 답이 안 온다 — 그럴 거면 울리지 않는 편이 낫다.
    `pending_notifications`에 쌓고 09:00 `flush-notify`가 순서대로 보낸다.
    지원 준비 알림이 쓰던 배선을 그대로 쓴다.
    """
    import html
    from datetime import datetime

    steps = "\n".join(f"  {i}. {html.escape(str(s)[:100])}"
                      for i, s in enumerate(parsed["steps"][:5], 1))
    icon = {"medium": "🟡", "high": "🔴"}.get(parsed["risk"], "🟢")
    text = (
        f"{icon} <b>수정 계획 #{plan_id}</b> — 승인이 필요합니다\n\n"
        f"<b>{html.escape(parsed['title'])}</b>\n"
        f"<i>{html.escape(parsed['cause'][:300])}</i>\n\n"
        f"{steps}\n\n"
        f"고칠 파일: <code>{html.escape(', '.join(parsed['files'][:5]) or '미정')}</code>\n"
        f"검증: <code>{html.escape(parsed['verify'] or '없음')}</code>\n"
        f"위험도 <b>{parsed['risk']}</b> — {html.escape(parsed['risk_reason'][:200])}\n\n"
        "<i>승인하면 전용 브랜치에서 작업한 뒤, 방어선 검사를 지나면 그 자리에서 "
        "main에 바로 반영합니다. 방어선(제출·중복·한도)에 걸리면 브랜치로 내려 "
        "다시 확인을 요청합니다.</i>"
    )
    buttons = [[
        {"text": "✅ 승인", "callback_data": f"fix:ok:{plan_id}"},
        {"text": "❌ 거절", "callback_data": f"fix:no:{plan_id}"},
    ]]

    hour = datetime.now().astimezone().hour
    if 2 <= hour < 9:
        conn.execute(
            "INSERT INTO pending_notifications (caption, buttons, created_at) VALUES (?,?,?)",
            (text, json.dumps(buttons, ensure_ascii=False), now()),
        )
        conn.commit()
        log.info("새벽이라 승인 요청을 09시로 미룬다 (계획 #%s)", plan_id)
        return False
    return telegram.notify_with_buttons(conn, text, buttons)


def _report_result(conn: sqlite3.Connection, row: sqlite3.Row, result: dict[str, Any]) -> None:
    """수행 결과를 폰으로. **diff가 아니라 커밋 메시지를 보낸다** — 폰 화면에서
    diff를 읽는 것은 검토가 아니다."""
    import html

    status = result.get("status")
    if status == "requeued":
        telegram.notify(conn, f"⏳ 사용 한도 — 계획 #{row['id']}은 다음 실행으로 미룹니다.")
        return

    if status == "done" and result.get("landed") == "main":
        why = ("검증 명령이 실제로 통과했습니다." if row["verify"]
               else "승인하신 계획입니다(검증 명령은 없었습니다).")
        telegram.notify_with_buttons(
            conn,
            f"🔧 <b>반영했습니다</b> — main에 병합됨\n\n"
            f"<pre>{html.escape(_commit_message(row)[:700])}</pre>\n"
            f"커밋 <code>{result.get('commit_sha')}</code>\n\n"
            f"<i>{why} 다음 사이클부터 고쳐진 코드로 돕니다.</i>",
            [[{"text": "↩️ 되돌리기", "callback_data": f"revert:{result.get('commit_sha')}"}]],
        )
        return

    icon = {"done": "🌿", "demoted": "🟡", "skipped": "➖", "failed": "❌"}.get(status, "•")
    if status == "demoted" and row["status"] == "approved":
        head = "승인하셨지만 방어선에 걸려 반영을 내렸습니다 — 확인이 필요합니다"
    else:
        head = {
            "done": "브랜치에 두었습니다 — 아직 승인되지 않았습니다",
            "demoted": "자동반영을 내렸습니다 — 확인이 필요합니다",
            "skipped": "바뀐 것이 없습니다",
            "failed": "수행 실패",
        }.get(status, status)
    telegram.notify(
        conn,
        f"{icon} <b>계획 #{row['id']}</b> — {head}\n"
        f"<i>{html.escape(row['title'])}</i>\n\n"
        + (f"브랜치 <code>{result.get('branch')}</code>\n" if result.get("branch") else "")
        + (f"사유: {html.escape(str(result.get('reason'))[:300])}\n" if result.get("reason") else "")
        + "\n<i>고장은 큐에 그대로 남아 있습니다 — main에 반영돼야 해결로 칩니다.</i>",
    )


def revert(conn: sqlite3.Connection, sha: str) -> dict[str, Any]:
    """자동반영된 커밋을 되돌린다. 되돌림도 커밋이라 이력이 남는다.

    reset이 아니라 revert인 이유: 무엇을 되돌렸는지 나중에 읽을 수 있어야 하고,
    reset은 그 사이에 들어온 다른 커밋까지 날린다.
    """
    if not re.fullmatch(r"[0-9a-f]{6,40}", sha or ""):
        return {"ok": False, "reason": "커밋 해시 형식이 아니다"}
    if _git("status", "--porcelain"):
        return {"ok": False, "reason": "작업 트리에 커밋 안 된 변경이 있다"}
    try:
        _git("revert", "--no-edit", sha)
    except RuntimeError as e:
        _git("revert", "--abort", check=False)
        return {"ok": False, "reason": str(e)[:300]}
    new_sha = _git("rev-parse", "--short", "HEAD")
    conn.execute("UPDATE fix_plans SET status='reverted' WHERE commit_sha=?", (sha,))
    conn.commit()
    return {"ok": True, "reverted": sha, "commit": new_sha}


def recent_auto_commits(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, title, risk, commit_sha, status, finished_at FROM fix_plans
           WHERE commit_sha IS NOT NULL ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_plans(conn: sqlite3.Connection, limit: int = 12) -> list[dict[str, Any]]:
    """최근 수정 계획 — `cli.py plans`가 쓴다.

    `fix_plans`는 이 모듈이 쓰고 이 모듈이 읽는다. CLI가 직접 SELECT 하면
    상태값(pending/approved/demoted/done)의 뜻이 두 곳으로 갈린다.
    """
    return [dict(r) for r in conn.execute(
        "SELECT id, created_at, title, risk, auto, status, branch, commit_sha "
        "FROM fix_plans ORDER BY id DESC LIMIT ?", (limit,)
    )]
