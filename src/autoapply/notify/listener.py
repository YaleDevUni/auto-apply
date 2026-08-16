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

import logging
import sqlite3
from typing import Any, Callable

import httpx

from ..db import get_setting, now, set_setting
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
    "<b>개발 지시</b>\n"
    "그 외 아무 말이나 보내면 개발 큐에 쌓입니다.\n"
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
    r = httpx.get(
        API.format(token=token, method="getUpdates"), params=params, timeout=wait + 20
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
    cmd = text.strip().split()[0].lower()
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
        "다음 실행 때 <b>전용 브랜치</b>에서 작업하고 결과를 알려드립니다. "
        "main에는 닿지 않습니다."
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
        log.info("텔레그램 수신 건너뜀: %s", e)
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

        answer = _handle(conn, text)
        handled += 1
        if reply:
            notify(conn, answer)

    return {"received": len(updates), "handled": handled, "ignored": ignored}


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
