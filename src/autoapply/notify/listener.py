"""폰에서 보낸 메시지를 받는다. 운영 명령과 개발 지시를 다른 통로로 가른다.

## 두 통로로 가르는 이유

    운영 명령   /status /quota /blocked /pause /resume /targets
                정해진 동작만 한다. 코드를 건드리지 않는다.

    개발 지시   그 외 자유 텍스트
                control_queue에 쌓인다. 코드를 고치게 되므로 전용 브랜치에서만
                작업하고 main에 닿지 않는다.

같은 채널로 받되 처리 경로를 나눈다. 자유 텍스트를 그 자리에서 실행하면
봇 토큰이 곧 파이프라인 제어권이 된다.

## 발신자 검증

`telegram_chat_id`와 일치하는 채팅에서 온 것만 처리한다. 봇 이름은 공개되어
누구나 말을 걸 수 있고, 그 메시지가 큐에 들어가면 남의 지시가 사용자 저장소에서
실행된다. 다른 chat_id는 조용히 버린다 — 응답하면 봇이 살아있다는 것만 알려준다.
"""

from __future__ import annotations

import html
import logging
import sqlite3
from typing import Any, Callable

import httpx

from .. import tasks
from ..db import get_setting, now, set_setting
# 모듈째 들여온다. 이름만 가져오면(`from .telegram import notify`) 콜백 처리에서
# 쓰는 `telegram.answer_callback`이 NameError로 죽는다 — 실제로 '건너뛰기'
# 버튼이 그 상태였다. 버튼을 눌렀는데 아무 일도 안 일어나는 실패라 눈에 안 띈다.
from . import telegram
from .telegram import API, S_CHAT, S_TOKEN, _creds, notify

log = logging.getLogger(__name__)

_CHITCHAT = {"hi", "hello", "hey", "watsup", "whatsup", "ㅎㅇ", "안녕", "테스트", "test", "ok", "ㅇㅇ"}

OFFSET_KEY = "telegram_update_offset"
PAUSE_KEY = "pipeline_paused"

HELP = (
    "<b>운영 명령</b>\n"
    "/status  현재 상태\n"
    "/quota   오늘 남은 지원 한도\n"
    "/blocked 막힌 이유\n"
    "/targets 지원 대기열\n"
    "/running 지금 도는 작업 (브라우저를 누가 쥐고 있는지도)\n"
    "/stop    도는 작업 중단 요청(한 단계 안에 멈춤). 하나만: <code>/stop 수집</code> · "
    "안 멈추면 <code>/stop 강제</code>\n"
    "/pause   자동지원 정지 (도는 작업은 안 건드림 — 그건 /stop)\n"
    "/resume  재개\n"
    "/queue   개발 지시 큐\n\n"
    "<b>작성 가이드 (Opus 5가 편집)</b>\n"
    "/guide 지시문        가이드를 지시대로 고친다 (직전 대화를 이어받음)\n"
    "/guide 되돌리기      마지막 백업으로 되돌린다\n"
    "/guide 세션클리어    이어가던 대화를 끊는다\n\n"
    "<b>수정 요청 원장 (직접 편집, LLM 없음)</b>\n"
    "/revlog              목록\n"
    "/revlog edit N 내용  N번을 고친다\n"
    "/revlog delete N     N번을 지운다\n\n"
    "<b>수동 실행 (언제든, 스케줄 무관)</b>\n"
    "/apply [건수]        지원준비를 지금 바로 (수집은 낮 12시에 따로). 기본 1건\n\n"
    "<b>자가복구</b>\n"
    "/errors              고장 큐 (🌐 표시는 바깥 사정 — 안 고칩니다)\n"
    "/plan                지금 수정 계획을 세운다 (<code>/improve</code>와 같음)\n"
    "/plans               계획 목록과 상태\n"
    "/reverts             자동으로 main에 들어간 커밋들\n"
    "/revert &lt;해시&gt;       그 커밋을 되돌린다\n\n"
    "<i>고장이 나면 스스로 계획을 세웁니다. 위험도가 낮고 검증이 통과하면 "
    "승인 없이 main에 반영하고 커밋 메시지를 보냅니다. 그 밖은 승인 버튼을 "
    "보내고, 브랜치에만 커밋합니다.\n"
    "자가복구가 도는 동안 자동지원은 멈췄다가 끝나면 재개됩니다.</i>\n\n"
    "<b>개발 지시</b>\n"
    "그 외 아무 말이나 보내면 개발 큐에 쌓입니다. <code>/plan</code>을 눌러야 "
    "계획이 만들어집니다."
)


# 자가복구가 진행 중이라 자동지원을 붙잡아 둔 상태. 사람이 누른 /pause와
# **다른 열쇠**를 쓴다. 하나로 합치면 자가복구가 끝나며 푸는 순간 사람이
# 걸어둔 정지까지 같이 풀린다 — 사람은 자기가 멈춰둔 줄 알고 있는데 지원이
# 나가는 상태가 되고, 그건 되돌릴 수 없다.
FIX_HOLD_KEY = "pipeline_hold_fix"

# 붙잡아 둔 지 이 시간이 지나면 무시한다. 수행 프로세스가 풀지 못하고 죽으면
# (kill -9, 맥 재부팅) 표식만 남아 파이프라인이 영영 멈춘다. 사람이 눈치채기
# 전까지 며칠이 갈 수 있는 종류의 고장이라 시한을 둔다.
FIX_HOLD_STALE_HOURS = 12


def hold_for_fix(conn: sqlite3.Connection, plan_id: int | None, note: str = "") -> None:
    """자가복구가 도는 동안 자동지원을 붙잡는다.

    고장이 난 채로 지원을 계속 내보내면, 고치는 중에도 같은 고장으로 자리가
    소모된다. 최악은 절반만 채워진 이력서가 실제로 제출되는 것이다.
    """
    import json as _json

    set_setting(conn, FIX_HOLD_KEY, _json.dumps(
        {"plan_id": plan_id, "since": now(), "note": note[:120]}, ensure_ascii=False
    ))


def release_fix_hold(conn: sqlite3.Connection) -> None:
    set_setting(conn, FIX_HOLD_KEY, "")


def fix_hold(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """지금 걸린 자가복구 보류. 오래된 것은 여기서 스스로 걷어낸다."""
    import json as _json
    from datetime import datetime

    raw = get_setting(conn, FIX_HOLD_KEY, "")
    if not raw:
        return None
    try:
        info = _json.loads(raw)
        elapsed = (
            datetime.now().astimezone() - datetime.fromisoformat(info["since"])
        ).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        release_fix_hold(conn)
        return None

    if elapsed >= FIX_HOLD_STALE_HOURS:
        log.warning("자가복구 보류가 %.0f시간째 — 걷어낸다 (수행이 풀지 못하고 죽은 듯)", elapsed)
        release_fix_hold(conn)
        return None
    info["hours"] = elapsed
    return info


def is_paused(conn: sqlite3.Connection) -> bool:
    """자동지원을 지금 내보내도 되나. 두 가지 이유로 멈춘다.

    호출부(`night-cycle`, `cycle-apply`)가 이 함수 하나만 보므로, 자가복구
    보류를 여기 얹으면 두 경로가 자동으로 같이 멈춘다.
    """
    if get_setting(conn, PAUSE_KEY, "0") == "1":
        return True
    return fix_hold(conn) is not None


def pause_reason(conn: sqlite3.Connection) -> str:
    """왜 멈췄는지 한 줄. 사람이 /status에서 이유를 못 보면 /resume만 누르게 된다."""
    if get_setting(conn, PAUSE_KEY, "0") == "1":
        return "사람이 정지시킴 (/resume 으로 해제)"
    held = fix_hold(conn)
    if held:
        plan = f" #{held['plan_id']}" if held.get("plan_id") else ""
        return f"자가복구 진행 중{plan} — 고쳐지면 자동으로 재개됩니다"
    return ""


def _fetch(conn: sqlite3.Connection, wait: int = 0) -> list[dict[str, Any]]:
    """수신. wait>0이면 롱폴링 — 새 메시지가 올 때까지 서버가 붙잡고 있는다.

    wait=0은 사이클에서 한 번 훑고 끝낼 때 쓴다. 그 방식만 쓰면 응답이 다음
    사이클(2시간)까지 밀린다 — 실제로 그래서 답장이 한참 뒤에 갔다.
    """
    token, _ = _creds(conn)
    offset = get_setting(conn, OFFSET_KEY, "")
    params: dict[str, Any] = {
        "timeout": wait,
        "allowed_updates": '["message","callback_query"]',
    }
    if offset:
        params["offset"] = int(offset) + 1
    # 타임아웃을 단계별로 나눈다. 통짜 timeout=wait+20은 연결이 멎었을 때
    # 45초를 통째로 날리고, 그동안 버튼 누름을 못 받는다. 실측: 두 번 연속
    # 멎어 90초가 비었고, 그 사이 눌린 버튼의 callback_query가 만료돼
    # answerCallbackQuery가 400을 냈다 — 사람 눈에는 "눌러도 반응이 없다"다.
    #
    # read는 서버가 붙잡고 있는 시간(wait)에 여유만 더한다. connect가 짧은
    # 이유는 붙는 데 오래 걸리는 건 이미 문제라서다 — 기다릴 게 아니라 다시 건다.
    r = httpx.get(
        API.format(token=token, method="getUpdates"),
        params=params,
        timeout=httpx.Timeout(connect=8, read=wait + 8, write=8, pool=8),
    )
    r.raise_for_status()
    return r.json().get("result", [])


# ── 운영 명령 ────────────────────────────────────────────────────

def _cmd_status(conn) -> str:
    from ..agent import status

    s = status(conn)
    q = s["quota"]
    return (
        f"수집 {s['jobs']} · 통과 {s['passed']} · 지원가능 {s['actionable']}\n"
        f"선점 {s['claimed']} · 제출 {s['submitted']} · 실패 {s['failed']}\n"
        f"오늘 한도 {q['used_today']}/{q['max_per_day']}"
        + (f"\n⏸ <b>정지 상태</b> — {pause_reason(conn)}" if is_paused(conn) else "")
    )


def _cmd_quota(conn) -> str:
    from ..agent import quota

    q = quota(conn)
    return f"오늘 {q['used_today']}/{q['max_per_day']}건 사용 · {q['remaining_today']}건 남음"


def _cmd_blocked(conn) -> str:
    from ..screening import summarize_blockers

    rows = summarize_blockers(conn)[:6]
    return "왜 막혔나\n" + "\n".join(f"· {b['code']} {b['count']}건" for b in rows)


def _cmd_targets(conn) -> str:
    from ..agent import next_targets

    ts = next_targets(5, conn)
    if not ts:
        return "지원 대기열이 비어 있습니다."
    return "지원 대기열\n" + "\n".join(
        f"· {t['fit_score']}점 {t['company'][:14]} — {t['title'][:26]}" for t in ts
    )


def _cmd_running(conn) -> str:
    """지금 도는 긴 작업. 브라우저를 누가 쥐고 있는지도 같이 본다 —
    "왜 지원 버튼이 안 먹지"의 답이 대개 여기 있다."""
    from ..runner.lock import describe as lock_desc, holder
    from ..tasks import active, describe

    rows = active(conn)
    lines = ["작업이 없습니다."] if not rows else [f"· {describe(t)}" for t in rows]
    who = holder()
    lines.append(
        f"\n🔒 브라우저: {lock_desc(who)}" if who else "\n🔒 브라우저: 비어 있음"
    )
    return "<b>지금 도는 작업</b>\n" + "\n".join(lines) + (
        "\n\n멈추려면 <code>/stop</code>" if rows else ""
    )


def _cmd_stop(conn: sqlite3.Connection, rest: str) -> str:
    """도는 작업에 중단을 요청한다.

    죽이지 않고 표시만 남기는 게 기본이다 — 브라우저를 반쯤 만진 채 끊기면
    절반만 채워진 이력서가 계정에 남는다. 각 루프가 **안전한 경계**에서 표시를
    보고 스스로 접는다. 그래서 즉시 멎지는 않지만, 늦어지는 폭은 한 단계다:

        수집        공고 한 건 / 상세 조회 한 건
        지원준비    조립 전 · 조립 중 · 등록 전 · 지원 폼 전

    지원준비에서 유일하게 안 보는 구간이 이력서 등록(fill)이다. 편집기는
    자동저장이라 중간에 끊으면 절반만 채워진 이력서가 남는다.

    그래도 안 멈추면 `/stop 강제` — SIGTERM을 보낸다. 그 자리에서 끊기므로
    만들다 만 이력서가 남을 수 있다.

    **무엇을 멈출지 고를 수 있다.** `/stop 수집`, `/stop 지원준비`, `/stop 12`.
    수집(수십 분)과 지원준비는 같이 도는 일이 잦아서, 하나를 접으려다 둘 다
    날리면 다시 수십 분이다 — 실제로 두 번 그렇게 날렸다. 인자가 없으면
    전부지만, 그때는 무엇을 멈췄는지 목록으로 되돌려준다.
    """
    from ..tasks import describe, request_stop, select

    words = rest.split()
    force = any(w in ("강제", "force", "kill", "-f") for w in words)
    match = " ".join(w for w in words if w not in ("강제", "force", "kill", "-f")).strip()

    if match and not select(conn, match):
        return (
            f"'{html.escape(match)}'에 해당하는 작업이 없습니다.\n"
            "<code>/running</code>으로 지금 도는 것을 먼저 보세요."
        )

    stopped = request_stop(conn, force=force, match=match)
    if not stopped:
        return "도는 작업이 없습니다. (<code>/pause</code>는 자동지원 자체를 멈춥니다)"

    lines = "\n".join(f"· {describe(t)}" for t in stopped)
    if force:
        return (
            f"⛔️ <b>강제 종료 신호를 보냈습니다</b> ({len(stopped)}건)\n{lines}\n\n"
            "<i>만들다 만 이력서가 남았을 수 있습니다 — /running 으로 확인하세요.</i>"
        )
    return (
        f"⏹ <b>중단을 요청했습니다</b> ({len(stopped)}건)\n{lines}\n\n"
        "<i>지금 하던 <b>한 단계</b>를 끝내고 멈춥니다. 받은 데까지는 저장됩니다.\n"
        "안 멈추면 <code>/stop 강제</code>. 하나만 멈추려면 "
        "<code>/stop 수집</code>처럼 종류나 번호를 붙이세요.</i>"
    )


def _cmd_pause(conn) -> str:
    set_setting(conn, PAUSE_KEY, "1")
    return "⏸ 자동지원을 정지했습니다. 수집·판정은 계속 돕니다."


def _cmd_resume(conn) -> str:
    """사람이 거는 재개. 자가복구 보류도 같이 푼다 — 사람에게는 늘 빠져나갈
    길이 있어야 한다. 보류가 남아 있는데 /resume이 안 들으면, 왜 안 도는지
    모르는 채로 밤이 지나간다."""
    held = fix_hold(conn)
    set_setting(conn, PAUSE_KEY, "0")
    if held:
        release_fix_hold(conn)
        plan = f" #{held['plan_id']}" if held.get("plan_id") else ""
        return (
            f"▶️ 자동지원을 재개했습니다.\n"
            f"<i>진행 중이던 자가복구{plan} 보류도 같이 풀었습니다 — "
            "고장이 안 고쳐진 상태로 지원이 나갈 수 있습니다.</i>"
        )
    return "▶️ 자동지원을 재개했습니다."


def _cmd_queue(conn) -> str:
    rows = conn.execute(
        "SELECT id, status, substr(text,1,40) AS t FROM control_queue ORDER BY id DESC LIMIT 8"
    ).fetchall()
    if not rows:
        return "개발 지시 큐가 비어 있습니다."
    return "개발 지시 큐\n" + "\n".join(f"#{r['id']} [{r['status']}] {r['t']}" for r in rows)


# ── 작성 가이드 / 원장 ───────────────────────────────────────────────

def _fmt_entry(e: str) -> str:
    return e[2:] if e.startswith("- ") else e


def _cmd_guide(conn: sqlite3.Connection, rest: str) -> str:
    """가이드(resume-guide.md) 수정. Opus 5가 편집하지만, 방아쇠는 사람이 당긴다 —
    한 공고의 특수한 요구가 자동으로 규칙이 되면 이후 모든 이력서가 조용히 오염된다.
    """
    if not rest:
        return (
            "사용법: <code>/guide 지시문</code>\n"
            "되돌리기: <code>/guide 되돌리기</code>\n"
            "대화 끊기: <code>/guide 세션클리어</code>\n\n"
            "예: <i>/guide 영업 공고엔 인프라 경험을 빼라는 규칙을 §7-1에 추가</i>\n\n"
            "<i>지시는 직전 대화를 이어받습니다(\"그거 말고 그 앞부분도\" 가능). "
            "화제가 바뀌면 세션클리어로 끊으세요.</i>"
        )

    revert = rest in ("되돌리기", "revert", "복구")
    clear_session = rest in ("세션클리어", "세션 클리어", "초기화", "새로시작", "clear")

    # Opus 5 호출은 수 분 걸릴 수 있어 별도 프로세스로 돌린다 — 수신 루프가
    # 막히면 그동안 온 다른 메시지(버튼 포함)를 못 받는다. 결과는 그 프로세스가
    # 폰으로 직접 알린다 (cli.py의 _guide → _tell).

    from ..paths import CODE_ROOT

    argv = [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "guide"]
    if clear_session:
        argv += ["-", "--clear-session"]  # instruction은 안 쓰이지만 위치인자라 채워야 한다
    elif revert:
        argv += ["되돌리기", "--revert"]
    else:
        argv += [rest]
    tasks.spawn(argv, log_name="guide")

    if clear_session:
        return "🧹 가이드 대화를 끊습니다…"
    if revert:
        return "↩️ 가이드를 마지막 백업으로 되돌립니다…"
    return (
        f"🛠 가이드 수정을 시작합니다 (Opus 5).\n<i>{html.escape(rest[:150])}</i>\n\n"
        "끝나면 변경 내용을 diff로 보내드립니다."
    )


def _cmd_revlog(conn: sqlite3.Connection, rest: str) -> str:
    """원장(수정 요청 로그)을 사람이 직접 본다/고친다/지운다. LLM을 거치지 않는다 —
    요약이 지시를 잘못 옮겼을 때 그 줄은 이후 모든 이력서 프롬프트에 실려 나가므로,
    파일을 열지 않고도 그 자리에서 바로잡을 수 있어야 한다.
    """
    from .. import assemble

    parts = rest.split(maxsplit=2)
    verb = parts[0].lower() if parts else ""

    if not rest or verb in ("목록", "list"):
        entries = assemble.log_entries()
        if not entries:
            return "📒 원장이 비어 있습니다."
        lines = [f"{i}. {html.escape(_fmt_entry(e))}" for i, e in enumerate(entries, 1)]
        return (
            "📒 <b>수정 요청 원장</b>\n" + "\n".join(lines) +
            "\n\n고치기: <code>/revlog edit N 새내용</code>\n"
            "지우기: <code>/revlog delete N</code>"
        )

    if verb in ("delete", "지우기") and len(parts) >= 2 and parts[1].isdigit():
        n = int(parts[1])
        try:
            old = assemble.log_edit(n, None)
        except IndexError as e:
            return f"❌ {e}"
        return f"🗑 원장 {n}번을 지웠습니다.\n<code>{html.escape(_fmt_entry(old))}</code>"

    if verb in ("edit", "고치기") and len(parts) >= 3 and parts[1].isdigit():
        n, new_text = int(parts[1]), parts[2]
        try:
            old = assemble.log_edit(n, new_text)
        except IndexError as e:
            return f"❌ {e}"
        entries = assemble.log_entries()
        now_line = entries[n - 1] if 1 <= n <= len(entries) else ""
        return (
            f"✏️ 원장 {n}번을 고쳤습니다.\n"
            f"전: <code>{html.escape(_fmt_entry(old))}</code>\n"
            f"후: <code>{html.escape(_fmt_entry(now_line))}</code>"
        )

    return (
        "사용법:\n"
        "<code>/revlog</code> — 목록\n"
        "<code>/revlog edit N 새내용</code> — N번 수정\n"
        "<code>/revlog delete N</code> — N번 삭제"
    )


# ── 수동 트리거 (스케줄 무관, 사람이 부를 때만) ──────────────────────

def _cmd_apply_start(conn: sqlite3.Connection, rest: str) -> str:
    """언제든 사람이 부르면 그 자리에서 지원준비를 돌린다.

    수집은 하지 않는다(2026-08-16부터 분리) — 이미 모아둔 대기열
    (`v_actionable`)에서만 고른다. 최신 공고가 필요하면 `cli.py scrape`가
    낮 12시에 따로 돈다.

    새벽 자동 루프와 같은 `night-cycle`을 쓰지만 즉시 알림 모드다 — 깨어있을
    때 직접 부른 것이므로 9시까지 미룰 이유가 없다. 건당 브라우저를 띄우고
    이력서를 조립하므로 수 분 걸린다. 서브프로세스로 돌려 수신 루프를 막지
    않는다(`/guide`와 같은 이유).
    """
    n = rest.strip()
    target = int(n) if n.isdigit() else 1
    if n and not n.isdigit():
        return "사용법: <code>/apply [건수]</code>  (숫자만, 생략하면 1건)"

    # **정지 상태를 여기서 먼저 본다.** 예전에는 안 보고 "🚀 시작합니다"라고
    # 답한 뒤 띄웠고, night-cycle은 안에서 is_paused에 걸려 0초 만에 죽었다.
    # 그 출력은 /dev/null이었으므로 사람에게는 "시작한다더니 영영 아무 일도
    # 안 일어난다"로 보인다 — running_tasks #15·#19·#21이 시작·종료 시각이
    # 같은 0초짜리로 남아 있다. 시작했다고 답하고 안 하는 것이 제일 나쁘다.
    if is_paused(conn):
        return _offer_resume_and_start(conn, target)

    from ..paths import CODE_ROOT

    tasks.spawn(
        [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "night-cycle", "--target", str(target)],
        log_name="apply",
    )
    return (
        f"🚀 지원 준비를 시작합니다 — 목표 {target}건.\n"
        "<i>판정 → 조립까지, 건당 수 분씩 걸립니다. 준비되는 대로 "
        "바로바로 사진을 보내드립니다.</i>"
    )


def _offer_resume_and_start(conn: sqlite3.Connection, target: int) -> str:
    """정지 중이라고 알리고, 한 번에 풀고 시작할 버튼을 준다.

    자동으로 풀지 않는 이유: 정지를 건 주체가 둘이고 무게가 다르다. 사람이 건
    `/pause`는 "지금 나가면 안 된다"는 판단이고, 자가복구 보류는 "고장 난 채로
    내보내지 마라"다. `/apply`가 알아서 풀면 **되돌릴 수 없는 지원이 사람도
    시스템도 원하지 않은 상태에서 나간다.**

    그렇다고 거절만 하면 예전 증상이 반쯤 남는다 — 왜 안 되는지 알아도
    `/resume`을 따로 치고 `/apply`를 다시 쳐야 한다. 버튼 하나로 합친다.
    """
    held = fix_hold(conn)
    why = pause_reason(conn)
    warn = (
        "\n\n<i>⚠️ 자가복구가 진행 중입니다 — 고장이 안 고쳐진 상태로 "
        "지원이 나갈 수 있습니다.</i>" if held else ""
    )
    telegram.notify_with_buttons(
        conn,
        f"⏸ <b>자동지원이 정지 상태입니다</b> — 지금 누르면 아무 일도 안 일어납니다.\n"
        f"<i>{html.escape(why)}</i>{warn}\n\n"
        f"풀고 목표 {target}건을 시작하려면 아래를 누르세요.",
        [[{"text": f"▶️ 재개하고 {target}건 시작", "callback_data": f"resumerun:{target}"}]],
    )
    return ""  # 위에서 직접 보냈다. drain이 빈 답장은 안 보낸다.


def _cmd_errors(conn: sqlite3.Connection, rest: str) -> str:
    """고장 큐. external도 같이 보여준다 — 무엇이 '바깥 사정'으로 분류됐는지
    사람이 봐야 오분류를 잡는다. 조용히 걸러진 것은 아무도 못 고친다."""
    from .. import errors

    rows = errors.summary(conn, 10)
    if not rows:
        return "🐛 고장 큐가 비어 있습니다."
    icon = {"open": "🔴", "planned": "🟡", "fixing": "🔧", "fixed": "✅", "dismissed": "➖"}
    lines = []
    for r in rows:
        tag = "🌐" if r["class"] == "external" else icon.get(r["status"], "•")
        n = f" ×{r['count']}" if r["count"] > 1 else ""
        lines.append(
            f"{tag} #{r['id']} {html.escape(r['exc_type'] or '')}{n}\n"
            f"    <i>{html.escape((r['message'] or '')[:60])}</i>"
        )
    return (
        "<b>고장 큐</b>  (🌐=바깥 사정, 안 고침)\n" + "\n".join(lines)
        + "\n\n계획을 세우려면 <code>/plan</code>"
    )


def _cmd_plans(conn: sqlite3.Connection, rest: str) -> str:
    rows = conn.execute(
        "SELECT id, title, risk, auto, status, branch, commit_sha FROM fix_plans "
        "ORDER BY id DESC LIMIT 8"
    ).fetchall()
    if not rows:
        return "📋 수정 계획이 없습니다."
    icon = {"pending": "⏳", "running": "🔧", "done": "✅", "demoted": "🟡",
            "rejected": "❌", "failed": "💥", "reverted": "↩️"}
    lines = []
    for r in rows:
        where = "main" if r["commit_sha"] else (r["branch"] or "")
        lines.append(
            f"{icon.get(r['status'], '•')} #{r['id']} [{r['risk']}] "
            f"{html.escape((r['title'] or '')[:44])}\n    <i>{r['status']} · {where}</i>"
        )
    return "<b>수정 계획</b>\n" + "\n".join(lines)


def _cmd_reverts(conn: sqlite3.Connection, rest: str) -> str:
    """자동으로 main에 들어간 것들. 사람이 나중에 훑어볼 수 있어야 한다 —
    자는 동안 반영된 걸 확인할 방법이 없으면 자동반영을 믿을 수 없다."""
    from .. import orchestrator

    rows = orchestrator.recent_auto_commits(conn)
    if not rows:
        return "↩️ 자동반영된 커밋이 없습니다."
    lines = [
        f"· <code>{r['commit_sha']}</code> #{r['id']} "
        f"{html.escape((r['title'] or '')[:40])}"
        + (" ↩️되돌림" if r["status"] == "reverted" else "")
        for r in rows
    ]
    return (
        "<b>자동반영된 커밋</b>\n" + "\n".join(lines)
        + "\n\n되돌리려면 <code>/revert &lt;해시&gt;</code>"
    )


def _cmd_revert(conn: sqlite3.Connection, rest: str) -> str:
    from .. import orchestrator

    sha = rest.strip()
    if not sha:
        return "사용법: <code>/revert &lt;커밋해시&gt;</code>  (<code>/reverts</code>로 목록)"
    r = orchestrator.revert(conn, sha)
    if not r.get("ok"):
        return f"❌ 되돌리기 실패 — {html.escape(str(r.get('reason'))[:200])}"
    return (
        f"↩️ <code>{sha}</code>를 되돌렸습니다.\n"
        f"되돌림 커밋 <code>{r['commit']}</code>\n"
        "<i>되돌림도 커밋으로 남습니다.</i>"
    )


def _cmd_plan(conn: sqlite3.Connection, rest: str) -> str:
    """고장 큐를 읽어 계획을 세운다. 위험도가 low면 승인 없이 바로 반영된다."""

    from ..paths import CODE_ROOT

    tasks.spawn(
        [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "plan", "--limit", "1"],
        log_name="plan",
    )
    return (
        "🧭 수정 계획을 세웁니다 (Opus 5).\n"
        "<i>위험도가 낮으면 승인 없이 바로 고치고 커밋 메시지를 보내드립니다. "
        "그 밖은 승인 버튼을 보내드립니다.</i>\n\n"
        "<i>계획을 세우는 동안 자동지원은 잠시 멈춥니다.</i>"
    )


def _cmd_improve(conn: sqlite3.Connection, rest: str) -> str:
    """개발 지시 큐 + 자체진단을 지금 처리한다.

    더는 시간이 되면 자동으로 안 돈다 — 문제가 자체진단됐을 때(새벽 루프 끝)
    아니면 이 명령으로 사람이 부를 때만 돈다. 큐에 쌓아둔 지시(자유 텍스트로
    보낸 것)도 이 명령을 눌러야 실제로 처리가 시작된다.
    """

    from ..paths import CODE_ROOT

    tasks.spawn(
        [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "improve", "--limit", "1"],
        log_name="improve",
    )
    return "🔧 자기개선을 시작합니다 — 전용 브랜치에서 작업하고 끝나면 알려드립니다."


COMMANDS: dict[str, Callable[[sqlite3.Connection], str]] = {
    "/status": _cmd_status,
    "/quota": _cmd_quota,
    "/blocked": _cmd_blocked,
    "/targets": _cmd_targets,
    "/running": _cmd_running,
    "/pause": _cmd_pause,
    "/resume": _cmd_resume,
    "/queue": _cmd_queue,
    "/help": lambda conn: HELP,
    "/start": lambda conn: HELP,
}


def _handle(conn: sqlite3.Connection, text: str) -> str:
    stripped = text.strip()
    cmd = stripped.split()[0].lower()

    # /guide, /revlog는 뒤에 자유 텍스트(지시문·항목번호)가 붙으므로 COMMANDS의
    # (conn) -> str 시그니처로는 못 다룬다. 여기서 먼저 가로챈다.
    if cmd == "/guide":
        return _cmd_guide(conn, stripped[len(cmd):].strip())
    if cmd == "/revlog":
        return _cmd_revlog(conn, stripped[len(cmd):].strip())
    if cmd == "/apply":
        return _cmd_apply_start(conn, stripped[len(cmd):].strip())
    if cmd == "/improve":
        return _cmd_improve(conn, stripped[len(cmd):].strip())
    if cmd == "/plan":
        return _cmd_plan(conn, stripped[len(cmd):].strip())
    if cmd == "/errors":
        return _cmd_errors(conn, stripped[len(cmd):].strip())
    if cmd == "/plans":
        return _cmd_plans(conn, stripped[len(cmd):].strip())
    if cmd == "/reverts":
        return _cmd_reverts(conn, stripped[len(cmd):].strip())
    if cmd == "/revert":
        return _cmd_revert(conn, stripped[len(cmd):].strip())
    if cmd == "/stop":
        return _cmd_stop(conn, stripped[len(cmd):].strip())

    # 명령어를 몰라서 그냥 "/"만 보내는 경우 — "모르는 명령입니다"를 앞세우지
    # 않고 바로 도움말을 보여준다. /help와 동작이 같다.
    if cmd == "/":
        return HELP

    if cmd in COMMANDS:
        try:
            return COMMANDS[cmd](conn)
        except Exception as e:  # noqa: BLE001
            log.warning("명령 %s 실패: %s", cmd, e)
            return f"명령 처리 실패: {type(e).__name__}"

    if text.strip().startswith("/"):
        return f"모르는 명령입니다.\n\n{HELP}"

    # 잡담은 할 일이 아니다. 문턱이 없으면 "Hi" 한 마디에 코딩 에이전트가
    # 브랜치를 파고 뭔가를 구현하려 든다 — 실제로 큐에 그렇게 쌓였다.
    body = text.strip()
    if len(body) < 12 or body.lower() in _CHITCHAT:
        return (
            "그건 개발 지시로 보기엔 짧습니다. 무엇을 고칠지 한 문장으로 적어주세요.\n"
            "예: <i>원티드 레시피가 제출 버튼을 못 찾으면 스크린샷을 텔레그램으로 보내줘</i>\n\n"
            f"{HELP}"
        )

    # 자유 텍스트 = 개발 지시. 여기서 실행하지 않고 큐에만 넣는다.
    cur = conn.execute(
        "INSERT INTO control_queue (received_at, text) VALUES (?,?)", (now(), text.strip())
    )
    conn.commit()
    return (
        f"📥 개발 지시 #{cur.lastrowid} 접수.\n"
        "자동으로는 안 돕니다 — <code>/improve</code>를 보내면 그때 "
        "<b>전용 브랜치</b>에서 작업하고 결과를 알려드립니다. main에는 닿지 않습니다."
    )


def _source_stamp() -> float:
    """소스 파일들의 최신 수정시각. 상주 리스너가 옛 코드를 물고 있는지 재는 자.

    파이썬은 모듈을 다시 읽지 않는다 — cli.py를 고쳐 커밋해도 이미 뜬 리스너
    프로세스는 기동 시점 코드로 계속 돈다(실측: 승인 버튼 콜백 분기가 없는
    구판이 떠 있어 승인이 "모르는 버튼입니다"로 떨어졌다). data/·profile/처럼
    **실행 중에 바뀌는 경로는 절대 포함하지 않는다** — 포함하면 자기가 쓴
    파일 때문에 데몬이 계속 재시작한다.
    """
    from ..paths import CODE_ROOT

    paths = [CODE_ROOT / "cli.py", *(CODE_ROOT / "src" / "autoapply").glob("**/*.py")]
    stamps = [p.stat().st_mtime for p in paths if p.exists()]
    return max(stamps) if stamps else 0.0


def watch(conn: sqlite3.Connection, *, wait: int = 25) -> None:
    """상시 대기하며 즉시 응답한다. 별도 launchd 에이전트가 이걸 돌린다.

    롱폴링이라 유휴 시 비용이 거의 없다 — 연결 하나를 열어두고 서버가 새
    메시지를 밀어줄 때까지 기다린다. 폴링 간격을 줄이는 것과 다르다.

    끊겨도 죽지 않는다. 네트워크가 잠깐 나가거나 맥이 잠들었다 깨어나는 일이
    잦으므로, 예외는 삼키고 잠시 뒤 다시 붙는다. KeepAlive가 프로세스를
    살려주지만 그때마다 재시작하면 로그가 지저분해진다.

    소스가 바뀌면 다르다 — 그건 코드가 낡았다는 신호라 스스로 물러난다.
    `com.autoapply.listen.plist`의 KeepAlive가 곧바로 새 프로세스를 띄우고,
    그게 새 코드를 문다. 여기서 `return`이 안전한 이유:
    (a) 수신 offset은 drain()이 매 업데이트마다 저장하므로 못 받은 메시지는
        다음 프로세스가 이어받는다.
    (b) 버튼이 띄운 서브프로세스(`fix-run` 등)는 `start_new_session=True`라
        부모가 나가도 산다.
    (c) 그 사이 눌린 버튼은 텔레그램이 다음 getUpdates에 다시 밀어준다.
    """
    import time

    log.info("텔레그램 대기 시작 (롱폴링 %d초)", wait)
    stamp = _source_stamp()
    while True:
        try:
            drain(conn, wait=wait)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("수신 중 오류 — 10초 뒤 재시도: %s", e)
            time.sleep(10)
            continue

        fresh = _source_stamp()
        if fresh > stamp:
            log.info("소스가 바뀌었다 — 새 코드로 다시 뜬다 (launchd KeepAlive)")
            return


def drain(conn: sqlite3.Connection, *, reply: bool = True, wait: int = 0) -> dict[str, Any]:
    """쌓인 메시지를 처리한다. wait>0이면 새 메시지를 기다린다."""
    try:
        updates = _fetch(conn, wait)
    except Exception as e:  # noqa: BLE001
        # WARNING인 이유: 수신이 멎은 동안 누른 버튼은 만료돼 사라진다.
        # INFO로 묻어두면 "왜 반응이 없지"를 로그에서 못 찾는다.
        log.warning("텔레그램 수신 실패 — 즉시 재시도: %s", e)
        return {"received": 0, "reason": str(e)[:120]}

    my_chat = get_setting(conn, S_CHAT, "")
    handled, ignored = 0, 0

    for u in updates:
        set_setting(conn, OFFSET_KEY, str(u["update_id"]))

        # 버튼 누름 — 되돌릴 수 없는 동작의 승인 게이트다.
        cb = u.get("callback_query")
        if cb:
            chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
            if chat_id != my_chat:
                log.warning("등록되지 않은 chat_id %s 의 버튼 무시", chat_id)
                ignored += 1
                continue
            _handle_callback(conn, cb)
            handled += 1
            continue

        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not text:
            continue
        # 등록된 채팅이 아니면 조용히 버린다. 응답조차 하지 않는다.
        if chat_id != my_chat:
            log.warning("등록되지 않은 chat_id %s 의 메시지 무시", chat_id)
            ignored += 1
            continue

        # 수정요청을 눌러둔 상태면, 이 메시지는 명령이 아니라 **수정 지시**다.
        pending = get_setting(conn, AWAITING_KEY, "")
        if pending:
            set_setting(conn, AWAITING_KEY, "")
            if text.strip() in ("취소", "cancel", "/cancel"):
                answer = f"수정요청을 취소했습니다 — 공고 {pending}"
            else:
                _start_revision(conn, pending, text)
                answer = (
                    f"✏️ 공고 {pending} 재작성을 시작합니다.\n"
                    f"<i>{text[:120]}</i>\n\n다 되면 다시 검토 요청을 보냅니다."
                )
            handled += 1
            if reply:
                notify(conn, answer)
            continue

        answer = _handle(conn, text)
        handled += 1
        # 빈 답장은 안 보낸다. 명령이 스스로 버튼 달린 메시지를 보냈다는 뜻이다
        # (`/apply`가 정지 상태에서 그렇게 한다). 빈 문자열로 sendMessage를
        # 부르면 텔레그램이 400을 낸다.
        if reply and answer:
            notify(conn, answer)

    return {"received": len(updates), "handled": handled, "ignored": ignored}


# 수정요청을 누른 뒤 사람의 다음 메시지를 기다리는 상태. 값은 공고 id.
AWAITING_KEY = "awaiting_revision"


def _drop_job(conn: sqlite3.Connection, job_id: str) -> None:
    """지원 대상에서 영구히 뺀다. 사람이 아니라고 한 자리는 다시 묻지 않는다."""
    conn.execute("UPDATE jobs SET dropped_at=? WHERE id=?", (now(), job_id))
    conn.commit()


def _start_revision(conn: sqlite3.Connection, job_id: str, feedback: str) -> None:
    """수정 요청을 반영해 다시 만든다. 브라우저를 띄우고 수 분이 걸리므로
    별도 프로세스로 돌린다 — 수신 루프가 막히면 다음 지시를 못 받는다."""

    from ..paths import CODE_ROOT

    tasks.spawn(
        [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "revise", job_id, feedback],
        log_name="revise",
    )


def _handle_fix_callback(conn: sqlite3.Connection, cb_id: str, data: str) -> None:
    """수정 계획 승인/거절.

    거절이 단순히 "안 함"으로 끝나면 안 된다 — 자가복구가 붙잡아 둔 자동지원
    보류가 그대로 남아 파이프라인이 영영 안 돈다. 거절도 **푸는 동작**이다.
    """

    from ..paths import CODE_ROOT

    try:
        _, verb, raw_id = data.split(":", 2)
        plan_id = int(raw_id)
    except (ValueError, IndexError):
        telegram.answer_callback(conn, cb_id, "잘못된 버튼입니다")
        return

    row = conn.execute("SELECT status, title FROM fix_plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        telegram.answer_callback(conn, cb_id, "없는 계획입니다")
        return
    if row["status"] not in ("pending", "demoted"):
        telegram.answer_callback(conn, cb_id, f"이미 {row['status']} 상태입니다")
        notify(conn, f"ℹ️ 계획 #{plan_id}은 이미 <b>{row['status']}</b> 상태입니다.")
        return

    if verb == "no":
        conn.execute(
            "UPDATE fix_plans SET status='rejected', decided_at=? WHERE id=?", (now(), plan_id)
        )
        conn.commit()
        release_fix_hold(conn)
        telegram.answer_callback(conn, cb_id, "거절했습니다")
        notify(
            conn,
            f"❌ 계획 #{plan_id}을 거절했습니다 — <i>{html.escape(row['title'] or '')}</i>\n"
            "<i>자동지원 보류를 풀었습니다. 고장은 큐에 그대로 남습니다.</i>",
        )
        return

    conn.execute(
        "UPDATE fix_plans SET status='approved', decided_at=? WHERE id=?", (now(), plan_id)
    )
    conn.commit()
    telegram.answer_callback(conn, cb_id, "수행을 시작합니다")
    notify(conn, f"🔧 계획 #{plan_id} 수행을 시작합니다 — 끝나면 알려드립니다.")

    # 수행은 코딩 에이전트라 수 분~수십 분이다. 수신 루프를 막으면 그동안 온
    # 다른 버튼을 못 받는다(/guide·/apply와 같은 이유).
    tasks.spawn(
        [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "fix-run", str(plan_id)],
        log_name="fix-run",
    )


def _handle_callback(conn: sqlite3.Connection, cb: dict[str, Any]) -> None:
    """승인 버튼 처리. `apply:<job_id>` 면 실제로 제출한다.

    **여기가 되돌릴 수 없는 지점이다.** 그래서 버튼을 누른 사람이 등록된
    사용자인지 위에서 먼저 확인하고, 처리 결과를 반드시 폰으로 되돌려준다 —
    눌렀는데 아무 반응이 없으면 다시 누르게 되고, 그건 중복지원 시도가 된다.
    """
    data = cb.get("data") or ""
    cb_id = cb.get("id", "")

    # 정지를 풀고 그 자리에서 지원준비를 시작한다. `/apply`가 정지 상태에서
    # 내놓는 버튼이다 — 사람이 정지를 풀겠다고 명시적으로 누른 지점이라
    # 자동으로 푸는 것과 다르다.
    if data.startswith("resumerun:"):
        target = data.split(":", 1)[1]
        telegram.answer_callback(conn, cb_id, "재개하고 시작합니다")
        notify(conn, _cmd_resume(conn))
        notify(conn, _cmd_apply_start(conn, target))
        return

    if data.startswith("skip:"):
        telegram.answer_callback(conn, cb_id, "넘어갑니다")
        notify(conn, f"➖ 건너뜀 — 공고 {data.split(':', 1)[1]}")
        return

    # 폐기 — 이 자리는 아니다. 다시 올라오지 않게 막는다. 건너뛰기와 다른 점이
    # 이것이다: 건너뛰기는 다음 사이클에 같은 공고를 또 올려 같은 판단을
    # 반복시켰다.
    if data.startswith("drop:"):
        job_id = data.split(":", 1)[1]
        _drop_job(conn, job_id)
        telegram.answer_callback(conn, cb_id, "지원 대상에서 제외했습니다")
        notify(conn, f"🗑 폐기 — 공고 {job_id}는 다시 올리지 않습니다")
        return

    # 수정요청 — 자리는 맞는데 내용이 아니다. 무엇을 고칠지 받아야 하므로
    # 여기서 끝내지 않고 다음 메시지를 기다린다.
    if data.startswith("revise:"):
        job_id = data.split(":", 1)[1]
        set_setting(conn, AWAITING_KEY, job_id)
        telegram.answer_callback(conn, cb_id, "무엇을 고칠까요?")
        notify(
            conn,
            f"✏️ <b>공고 {job_id} 수정요청</b>\n"
            "무엇을 고칠지 이 채팅에 그대로 적어주세요.\n"
            "<i>예: 인프라 경험 말고 백엔드 API 설계를 앞에 세워줘</i>\n\n"
            "취소하려면 <code>취소</code> 라고 보내세요.",
        )
        return

    # 수정 계획 승인 — 코드를 고치기 시작하는 지점이다. 결과는 브랜치에만 남고
    # main에는 안 닿는다(자동반영은 위험도 low일 때만, 승인 없이 따로 간다).
    if data.startswith("fix:"):
        _handle_fix_callback(conn, cb_id, data)
        return

    if data.startswith("revert:"):
        sha = data.split(":", 1)[1]
        telegram.answer_callback(conn, cb_id, "되돌리는 중…")
        notify(conn, _cmd_revert(conn, sha))
        return

    if not data.startswith(("submit:", "apply:")):
        # 정상 분기라면 여기 안 온다 — 리스너가 이 버튼을 모르는 구판일 수
        # 있다는 뜻이라 로그 없이 넘기면 sendMessage가 안 나가는 채로 묻힌다
        # (실측: 승인 버튼이 이 자리로 떨어졌는데 로그가 한 줄도 없었다).
        log.warning("모르는 콜백 data=%r — 리스너가 옛 코드일 수 있다", data)
        telegram.answer_callback(conn, cb_id, "모르는 버튼입니다 (리스너 재시작 필요할 수 있음)")
        return

    # submit — 준비 때 만들어둔 이력서로 제출만 한다(권장).
    # apply  — 조립부터 다시 한다(예전 경로. 검토한 것과 달라질 수 있다).
    verb, job_id = data.split(":", 1)
    cmd = "submit" if verb == "submit" else "autoapply"
    telegram.answer_callback(conn, cb_id, "제출을 시작합니다")
    notify(conn, f"⏳ 공고 {job_id} 제출 중…")

    # 제출은 브라우저를 띄우고 수 분이 걸린다. 수신 루프를 막지 않도록
    # 별도 프로세스로 돌리고, 결과는 그 프로세스가 폰으로 알린다.
    from ..paths import CODE_ROOT

    try:
        argv = [str(CODE_ROOT / ".venv/bin/python"), "cli.py", cmd, job_id]
        if cmd == "autoapply":
            argv.append("--live")
        tasks.spawn(argv, log_name="submit")
    except Exception as e:  # noqa: BLE001
        log.warning("제출 실행 실패: %s", e)
        notify(conn, f"❌ 제출 실행 실패 — {type(e).__name__}")
