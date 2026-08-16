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
    "/pause   자동지원 정지\n"
    "/resume  재개\n"
    "/queue   개발 지시 큐\n\n"
    "<b>작성 가이드 (Opus 5가 편집)</b>\n"
    "/guide 지시문        가이드를 지시대로 고친다\n"
    "/guide 되돌리기      마지막 백업으로 되돌린다\n\n"
    "<b>수정 요청 원장 (직접 편집, LLM 없음)</b>\n"
    "/revlog              목록\n"
    "/revlog edit N 내용  N번을 고친다\n"
    "/revlog delete N     N번을 지운다\n\n"
    "<b>수동 실행 (언제든, 스케줄 무관)</b>\n"
    "/지원시작 [건수]     수집→지원준비를 지금 바로. 기본 1건\n"
    "/improve             개발 지시 큐 + 자체진단을 지금 처리\n\n"
    "<b>개발 지시</b>\n"
    "그 외 아무 말이나 보내면 개발 큐에 쌓입니다. 자동으로는 안 돌고, "
    "문제가 자체진단됐거나 <code>/improve</code>를 눌러야 처리됩니다.\n"
    "브랜치에만 커밋되고 main에는 안 닿습니다."
)


def is_paused(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, PAUSE_KEY, "0") == "1"


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
        + ("\n⏸ <b>정지 상태</b>" if is_paused(conn) else "")
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


def _cmd_pause(conn) -> str:
    set_setting(conn, PAUSE_KEY, "1")
    return "⏸ 자동지원을 정지했습니다. 수집·판정은 계속 돕니다."


def _cmd_resume(conn) -> str:
    set_setting(conn, PAUSE_KEY, "0")
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
            "되돌리기: <code>/guide 되돌리기</code>\n\n"
            "예: <i>/guide 영업 공고엔 인프라 경험을 빼라는 규칙을 §7-1에 추가</i>"
        )

    revert = rest in ("되돌리기", "revert", "복구")

    # Opus 5 호출은 수 분 걸릴 수 있어 별도 프로세스로 돌린다 — 수신 루프가
    # 막히면 그동안 온 다른 메시지(버튼 포함)를 못 받는다. 결과는 그 프로세스가
    # 폰으로 직접 알린다 (cli.py의 _guide → _tell).
    import subprocess

    from ..paths import CODE_ROOT

    argv = [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "guide"]
    argv += (["되돌리기", "--revert"] if revert else [rest])
    subprocess.Popen(
        argv, cwd=str(CODE_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )

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
    """언제든 사람이 부르면 그 자리에서 수집→지원준비를 돌린다.

    새벽 자동 루프와 같은 `night-cycle`을 쓰지만 즉시 알림 모드다 — 깨어있을
    때 직접 부른 것이므로 9시까지 미룰 이유가 없다. 건당 브라우저를 띄우고
    이력서를 조립하므로 수 분 걸린다. 서브프로세스로 돌려 수신 루프를 막지
    않는다(`/guide`와 같은 이유).
    """
    n = rest.strip()
    target = int(n) if n.isdigit() else 1
    if n and not n.isdigit():
        return "사용법: <code>/지원시작 [건수]</code>  (숫자만, 생략하면 1건)"

    import subprocess

    from ..paths import CODE_ROOT

    subprocess.Popen(
        [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "night-cycle", "--target", str(target)],
        cwd=str(CODE_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    return (
        f"🚀 지원 준비를 시작합니다 — 목표 {target}건.\n"
        "<i>수집 → 판정 → 조립까지, 건당 수 분씩 걸립니다. 준비되는 대로 "
        "바로바로 사진을 보내드립니다.</i>"
    )


def _cmd_improve(conn: sqlite3.Connection, rest: str) -> str:
    """개발 지시 큐 + 자체진단을 지금 처리한다.

    더는 시간이 되면 자동으로 안 돈다 — 문제가 자체진단됐을 때(새벽 루프 끝)
    아니면 이 명령으로 사람이 부를 때만 돈다. 큐에 쌓아둔 지시(자유 텍스트로
    보낸 것)도 이 명령을 눌러야 실제로 처리가 시작된다.
    """
    import subprocess

    from ..paths import CODE_ROOT

    subprocess.Popen(
        [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "improve", "--limit", "1"],
        cwd=str(CODE_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    return "🔧 자기개선을 시작합니다 — 전용 브랜치에서 작업하고 끝나면 알려드립니다."


COMMANDS: dict[str, Callable[[sqlite3.Connection], str]] = {
    "/status": _cmd_status,
    "/quota": _cmd_quota,
    "/blocked": _cmd_blocked,
    "/targets": _cmd_targets,
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
    if cmd in ("/지원시작", "/apply_start"):
        return _cmd_apply_start(conn, stripped[len(cmd):].strip())
    if cmd == "/improve":
        return _cmd_improve(conn, stripped[len(cmd):].strip())

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


def watch(conn: sqlite3.Connection, *, wait: int = 25) -> None:
    """상시 대기하며 즉시 응답한다. 별도 launchd 에이전트가 이걸 돌린다.

    롱폴링이라 유휴 시 비용이 거의 없다 — 연결 하나를 열어두고 서버가 새
    메시지를 밀어줄 때까지 기다린다. 폴링 간격을 줄이는 것과 다르다.

    끊겨도 죽지 않는다. 네트워크가 잠깐 나가거나 맥이 잠들었다 깨어나는 일이
    잦으므로, 예외는 삼키고 잠시 뒤 다시 붙는다. KeepAlive가 프로세스를
    살려주지만 그때마다 재시작하면 로그가 지저분해진다.
    """
    import time

    log.info("텔레그램 대기 시작 (롱폴링 %d초)", wait)
    while True:
        try:
            drain(conn, wait=wait)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("수신 중 오류 — 10초 뒤 재시도: %s", e)
            time.sleep(10)


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
        if reply:
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
    import subprocess

    from ..paths import CODE_ROOT

    subprocess.Popen(
        [str(CODE_ROOT / ".venv/bin/python"), "cli.py", "revise", job_id, feedback],
        cwd=str(CODE_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def _handle_callback(conn: sqlite3.Connection, cb: dict[str, Any]) -> None:
    """승인 버튼 처리. `apply:<job_id>` 면 실제로 제출한다.

    **여기가 되돌릴 수 없는 지점이다.** 그래서 버튼을 누른 사람이 등록된
    사용자인지 위에서 먼저 확인하고, 처리 결과를 반드시 폰으로 되돌려준다 —
    눌렀는데 아무 반응이 없으면 다시 누르게 되고, 그건 중복지원 시도가 된다.
    """
    data = cb.get("data") or ""
    cb_id = cb.get("id", "")

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

    if not data.startswith(("submit:", "apply:")):
        telegram.answer_callback(conn, cb_id, "모르는 버튼입니다")
        return

    # submit — 준비 때 만들어둔 이력서로 제출만 한다(권장).
    # apply  — 조립부터 다시 한다(예전 경로. 검토한 것과 달라질 수 있다).
    verb, job_id = data.split(":", 1)
    cmd = "submit" if verb == "submit" else "autoapply"
    telegram.answer_callback(conn, cb_id, "제출을 시작합니다")
    notify(conn, f"⏳ 공고 {job_id} 제출 중…")

    # 제출은 브라우저를 띄우고 수 분이 걸린다. 수신 루프를 막지 않도록
    # 별도 프로세스로 돌리고, 결과는 그 프로세스가 폰으로 알린다.
    import subprocess
    from ..paths import CODE_ROOT

    try:
        argv = [str(CODE_ROOT / ".venv/bin/python"), "cli.py", cmd, job_id]
        if cmd == "autoapply":
            argv.append("--live")
        subprocess.Popen(
            argv,
            cwd=str(CODE_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("제출 실행 실패: %s", e)
        notify(conn, f"❌ 제출 실행 실패 — {type(e).__name__}")
