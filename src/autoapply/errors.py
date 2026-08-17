"""고장을 큐에 남긴다. 그리고 고칠 것과 바깥 사정을 가른다.

## 왜 필요한가

예전에는 에러가 **사라졌다.** `cli.py` 최상단 핸들러가 "처리 안 된 오류" 한 줄을
폰으로 보내고 끝이었다 — 그 메시지를 못 보면 그 고장은 어디에도 안 남는다.
`orchestrator.self_items()`가 읽는 신호는 미리 정의한 몇 가지 증상뿐이라
**처음 보는 고장은 못 잡는다.**

새벽 2~6시에 뭔가 깨지면 그 밤이 통째로 날아가고, 사람은 자고 있고, 같은 고장이
다음 밤에도 똑같이 난다. 이 모듈이 그 고리를 끊는 첫 칸이다.

## 분류에 LLM을 쓰지 않는다

예외형과 메시지 패턴이면 충분하다. 저장소 원칙이기도 하지만, 더 실질적인
이유는 **고장 났을 때 도는 코드**라는 것이다. LLM 호출은 그 자체가 실패하고
(한도·네트워크), 그러면 고장을 기록하려다 고장이 난다.

    external    바깥 사정. 폰으로 보고만 하고 큐에 안 넣는다.
                사이트가 죽은 것을 우리가 고칠 수는 없다.
    actionable  우리 코드·레시피 문제. 큐에 쌓여 계획 대상이 된다.

## 지문(fingerprint)으로 묶는다

같은 고장이 100번 나도 큐는 한 줄이고 `count`만 는다. 이게 없으면 반복 에러가
큐를 도배해서, 쌓인 것을 읽고 계획을 세우는 일 자체가 무의미해진다. 폰 알림도
같은 이유로 새 지문일 때만 나간다 — 같은 에러에 폰이 100번 울리면 그 알림은
전부 무시된다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import traceback as _tb
from datetime import datetime
from typing import Any

from .db import now

log = logging.getLogger(__name__)

# 바깥 사정임을 알리는 예외형. 이름으로 본다 — httpx를 여기서 import하면
# 고장 기록이 httpx 설치 여부에 매이고, 그건 이 모듈이 가장 튼튼해야 할
# 상황에서 가장 약해지는 배선이다.
EXTERNAL_TYPES = {
    "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "RemoteProtocolError", "ReadError", "WriteError", "NetworkError",
    "ConnectionResetError", "ConnectionAbortedError", "ConnectionRefusedError",
    "BrokenPipeError", "gaierror", "socket.gaierror", "TimeoutError",
    "SSLError", "SSLEOFError", "ProxyError",
}

# 바깥 사정임을 알리는 메시지 조각. 소문자로 비교한다.
EXTERNAL_SIGNS = (
    "connection reset", "connection aborted", "connection refused",
    "temporary failure in name resolution", "name or service not known",
    "net::err_", "err_internet_disconnected", "err_name_not_resolved",
    "err_connection_", "502 bad gateway", "503 service unavailable",
    "504 gateway", "bad gateway", "service unavailable", "gateway timeout",
    "server error '5", "max retries exceeded",
)

# 전용 처리 경로가 이미 있는 것들. 고장이 아니거나, 고장이어도 다른 데서 다룬다.
#   BrowserBusy   경합 — 안내하고 조용히 끝낸다 (cli.py)
#   LoginRequired 플랫폼(원티드) 로그인 — 사람을 부른다 (agent.notify_login_required)
#   Cancelled     사람이 멈춘 것
SKIP_TYPES = {"BrowserBusy", "LoginRequired", "Cancelled", "SystemExit",
              "KeyboardInterrupt", "TelegramNotConfigured"}

# **LLM을 쓸 수 없는 상태.** 큐에 안 넣는 건 SKIP과 같지만, 조용히 버리지 않고
# 원인을 반드시 폰에 보낸다.
#
# 예전에 UsageLimited가 SKIP_TYPES에 있었다. 주석은 "전용 경로가 있다
# (orchestrator._one)"였는데 그 함수는 진작 사라졌고, 남아 있는 전용 경로는
# orchestrator.plan()/execute() 둘뿐이다. **이력서 작성 경로(llm.ask)에는
# 없었다** — 한도에 걸리면 cli.py 최상단까지 올라와 여기서 조용히 버려지고
# 프로세스가 죽었다. 폰에는 한 글자도 안 갔고, 로그도 /dev/null이었다.
# 실측(2026-08-17 새벽): 사람이 아침에 "왜 아무것도 안 했지"로 알았다.
#
# 큐에 안 넣는 이유는 그대로다 — 여기에 고칠 코드가 없다. 기다리거나(한도)
# 다시 로그인해야(만료) 풀린다. 그래서 계획 에이전트를 띄우면 그 에이전트도
# 같은 이유로 죽는다.
BLOCKED_TYPES = {"UsageLimited", "LoginExpired", "ClaudeUnavailable"}

# 같은 이유로 몇 시간에 한 번만 깨운다. 한 사이클이 공고 여러 건을 도는 동안
# 같은 한도에 여러 번 걸리므로, 쿨다운이 없으면 한 번의 한도가 폰을 도배한다.
BLOCKED_ALERT_COOLDOWN_HOURS = 1.0

# 같은 지문이어도 이 시간이 지나면 external을 다시 보고한다. 오늘 새로 난
# 장애는 지난주에 같은 지문을 본 적이 있어도 알려야 한다.
EXTERNAL_RENOTIFY_HOURS = 6


def classify(exc_type: str, message: str) -> str:
    """external(바깥 사정) 인가 actionable(우리 문제) 인가.

    Playwright TimeoutError가 이 함수에서 가장 까다롭다. 같은 예외형이 두 가지
    전혀 다른 뜻으로 온다:

        waiting for selector / expect  →  우리 레시피가 화면과 어긋났다 (고칠 것)
        goto / net::ERR_               →  사이트를 못 열었다 (바깥 사정)

    예외형만 보고 external로 넘기면 **레시피 고장이 영원히 큐에 안 들어온다** —
    이 저장소에서 가장 자주 깨지는 것이 셀렉터인데 그게 통째로 안 보이게 된다.
    그래서 메시지를 먼저 본다.
    """
    low = (message or "").lower()

    # 셀렉터 대기 실패는 우리 문제다. 예외형 판정보다 먼저 본다.
    if "waiting for selector" in low or "waiting for locator" in low:
        return "actionable"
    if "expect" in low and "timeout" in low and "exceeded" in low:
        return "actionable"

    if any(sign in low for sign in EXTERNAL_SIGNS):
        return "external"
    if exc_type in EXTERNAL_TYPES:
        return "external"
    return "actionable"


# 지문을 만들기 전에 지우는 것들. 같은 고장이 매번 다른 숫자·경로를 달고 오면
# 지문이 갈려 큐가 도배된다 — 공고 id, 시각, 메모리 주소, 임시 경로가 전부
# 그렇다.
_NOISE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "#"),                  # 메모리 주소
    (re.compile(r"/[\w./\-가-힣]+"), "/P"),                 # 경로
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][\d:.+\-]+"), "#"),  # 타임스탬프
    (re.compile(r"\b[0-9a-f]{8,}\b"), "#"),                # 해시·id
    (re.compile(r"\d+"), "#"),                             # 남은 숫자 전부
    (re.compile(r"\s+"), " "),
]


def normalize(message: str) -> str:
    text = message or ""
    for pattern, repl in _NOISE:
        text = pattern.sub(repl, text)
    return text.strip()[:300]


def fingerprint(kind: str, exc_type: str, message: str) -> str:
    raw = f"{kind}|{exc_type}|{normalize(message)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _hours_since(iso: str) -> float:
    try:
        return (datetime.now().astimezone() - datetime.fromisoformat(iso)).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return 1e9


def record(
    conn: sqlite3.Connection,
    *,
    kind: str,
    exc: BaseException | None = None,
    exc_type: str = "",
    message: str = "",
    command: str = "",
    context: dict[str, Any] | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    """고장 하나를 기록한다. 분류·묶기·알림·발동을 여기서 다 한다.

    **이 함수는 절대 예외를 던지지 않는다.** 부르는 자리가 전부 이미 뭔가
    잘못된 상황(최상단 except 핸들러, 지원 실패 처리)이라, 여기서 또 터지면
    원래 고장이 무엇이었는지가 통째로 사라진다.
    """
    try:
        return _record(
            conn, kind=kind, exc=exc, exc_type=exc_type, message=message,
            command=command, context=context, notify=notify,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("고장 기록 실패(무시): %s", e)
        return {"recorded": False, "reason": str(e)[:200]}


def _record(
    conn: sqlite3.Connection,
    *,
    kind: str,
    exc: BaseException | None,
    exc_type: str,
    message: str,
    command: str,
    context: dict[str, Any] | None,
    notify: bool,
) -> dict[str, Any]:
    if exc is not None:
        exc_type = exc_type or type(exc).__name__
        message = message or str(exc)
        tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
    else:
        tb = ""

    # 봇 토큰이 DB(error_queue)에도, 뒤이은 폰 알림에도 평문으로 남지 않게 가린다.
    # command에는 `telegram-setup <token>` 같은 CLI 인자가 그대로 들어올 수 있다.
    from .notify.telegram import mask_token

    message = mask_token(message)
    tb = mask_token(tb)
    command = mask_token(command)

    if exc_type in SKIP_TYPES:
        return {"recorded": False, "reason": f"{exc_type}은 전용 경로가 있다"}

    # LLM을 못 쓰는 상태 — 큐에 넣을 것은 없지만 **원인은 반드시 알린다.**
    if exc_type in BLOCKED_TYPES:
        return {
            "recorded": False, "blocked": exc_type,
            "notified": _notify_blocked(conn, exc_type, message, command) if notify else False,
        }

    klass = classify(exc_type, message)
    fp = fingerprint(kind, exc_type, message)
    stamp = now()

    row = conn.execute("SELECT * FROM error_queue WHERE fingerprint=?", (fp,)).fetchone()
    if row is None:
        cur = conn.execute(
            """INSERT INTO error_queue
                 (fingerprint, class, kind, command, exc_type, message, traceback,
                  context, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (fp, klass, kind, command[:300], exc_type, (message or "")[:2000], tb,
             json.dumps(context or {}, ensure_ascii=False), stamp, stamp),
        )
        conn.commit()
        item_id, count, is_new = int(cur.lastrowid), 1, True
        stale = True
    else:
        item_id, count, is_new = int(row["id"]), int(row["count"]) + 1, False
        stale = _hours_since(row["last_seen"]) >= EXTERNAL_RENOTIFY_HOURS
        # 한 번 고쳤는데 또 났다 = 다시 열린 고장이다. dismissed(사람이 아니라고
        # 한 것)는 되살리지 않는다 — 그건 사람의 결정이다.
        reopen = row["status"] in ("fixed", "planned")
        conn.execute(
            "UPDATE error_queue SET count=?, last_seen=?, message=?, traceback=?"
            + (", status='open'" if reopen else "")
            + " WHERE id=?",
            (count, stamp, (message or "")[:2000], tb, item_id),
        )
        conn.commit()

    result = {
        "recorded": True, "id": item_id, "fingerprint": fp, "class": klass,
        "count": count, "new": is_new,
    }

    # 알림: 새 지문이면 늘, 아니면 external만 쿨다운을 두고 다시 알린다.
    # actionable을 반복 알리지 않는 이유는 이미 계획이 잡혀 있어서다 —
    # 같은 고장을 두 번 알려도 사람이 할 일이 늘지 않는다.
    if notify and (is_new or (klass == "external" and stale)):
        result["notified"] = _notify(conn, klass, kind, exc_type, message, command, count)

    if klass == "actionable" and is_new:
        result["planning"] = _trigger_plan(conn, command)
    return result


# 무엇이 막았고, 사람이 무엇을 해야 푸는가. 셋의 회복 방식이 전부 다르다 —
# 한 줄로 뭉뚱그리면 "기다리면 되나 로그인해야 하나"를 폰에서 못 가린다.
_BLOCKED_TEXT = {
    "UsageLimited": (
        "⏳ <b>Claude 사용 한도</b> — 자동지원을 멈춥니다",
        "시간이 지나면 풀립니다. 다음 새벽 사이클이 이어서 합니다.",
    ),
    "LoginExpired": (
        "🔑 <b>Claude 로그인 만료</b> — 기다려도 안 풀립니다",
        "터미널에서 <code>claude</code> 실행 후 <code>/login</code>.\n"
        "무인 실행용으로는 <code>claude setup-token</code>으로 1년짜리 토큰을 받아 "
        "launchd plist의 <code>CLAUDE_CODE_OAUTH_TOKEN</code>에 넣으면 "
        "이 만료를 안 겪습니다.",
    ),
    "ClaudeUnavailable": (
        "❓ <b>claude CLI를 찾을 수 없습니다</b>",
        "PATH 문제입니다. run.sh를 안 거치는 launchd 잡은 plist의 "
        "<code>EnvironmentVariables/PATH</code>를 따로 챙겨야 합니다.",
    ),
}


def _notify_blocked(
    conn: sqlite3.Connection, exc_type: str, message: str, command: str
) -> bool:
    """LLM을 못 쓰는 이유를 폰에 보낸다. 같은 이유로는 쿨다운 안에서 한 번만.

    쿨다운이 필요한 이유: 한 사이클이 공고 여러 건을 도는 동안 같은 한도에
    매번 걸린다. 그대로 보내면 한 번의 한도가 폰을 수십 통으로 도배하고,
    도배된 알림은 통째로 무시된다 — 정작 다음에 진짜 고장이 왔을 때 안 읽힌다.
    """
    import html

    from .db import get_setting, set_setting
    from .notify import telegram

    key = f"llm_blocked_last_alert:{exc_type}"
    last = get_setting(conn, key, "")
    if last and _hours_since(last) < BLOCKED_ALERT_COOLDOWN_HOURS:
        return False

    head, how = _BLOCKED_TEXT.get(
        exc_type, (f"⛔️ <b>{html.escape(exc_type)}</b>", "원인을 확인해 주세요.")
    )
    lines = [head, ""]
    if command:
        lines.append(f"<code>{html.escape(command[:120])}</code>")
    # 원인 원문을 그대로 붙인다. 이게 없으면 "한도"라는 사실만 알고 언제
    # 풀리는지를 모른다 — claude가 내는 문구에 리셋 시각이 들어 있다.
    lines.append(f"<i>{html.escape((message or '').strip()[:300])}</i>")
    lines.append(f"\n{how}")

    if telegram.notify(conn, "\n".join(lines)):
        set_setting(conn, key, now())
        return True
    return False


def _notify(
    conn: sqlite3.Connection, klass: str, kind: str, exc_type: str,
    message: str, command: str, count: int,
) -> bool:
    import html

    from .notify import telegram

    if klass == "external":
        head = "🌐 <b>바깥 사정</b> — 고칠 것이 없습니다"
        tail = "\n<i>사이트·네트워크 쪽입니다. 큐에 넣지 않았습니다.</i>"
    else:
        head = "🐛 <b>고장</b> — 큐에 넣었습니다"
        tail = "\n<i>계획을 세워 다시 알려드립니다.</i>"

    lines = [head, ""]
    if command:
        lines.append(f"<code>{html.escape(command[:120])}</code>")
    lines.append(f"<i>{html.escape(exc_type)}: {html.escape((message or '')[:250])}</i>")
    if count > 1:
        lines.append(f"\n{count}번째 발생")
    lines.append(tail)
    return telegram.notify(conn, "\n".join(lines))


def _trigger_plan(conn: sqlite3.Connection, command: str) -> str:
    """새 고장이 들어왔으니 계획을 세운다 — 단, 빗장 셋을 지난 뒤에만.

    빗장이 없으면 고장 하나가 계획 세션 하나를 띄우고, 그 세션이 실패하면
    그것도 고장으로 기록돼 또 세션을 띄운다. 무인 운영에서 그 고리는 밤새 돈다.
    """
    # 계획·수행 자신이 낸 고장으로 다시 계획을 세우지 않는다. 이게 없으면
    # 계획 에이전트가 죽을 때마다 새 계획 에이전트가 뜬다.
    first = (command or "").split()[0] if command else ""
    if first in ("plan", "fix-run", "improve"):
        return "건너뜀(계획·수행 자신의 고장)"

    # 'demoted'를 빠뜨리면 안 된다. 자동반영이 관문에 걸려 내려온 계획은 브랜치에
    # 수정을 갖고 사람의 승인을 기다리는 상태인데, 같은 고장이 다시 나면 그걸
    # 못 보고 새 계획을 또 띄운다 — 같은 곳을 고치는 브랜치가 계속 쌓인다.
    pending = conn.execute(
        "SELECT COUNT(*) FROM fix_plans "
        "WHERE status IN ('pending','running','approved','demoted')"
    ).fetchone()[0]
    if pending:
        return f"건너뜀(처리 중인 계획 {pending}건)"

    from .tasks import active

    if any("계획수립" in (t["kind"] or "") for t in active(conn)):
        return "건너뜀(이미 계획 수립 중)"

    from .paths import CODE_ROOT
    from .tasks import spawn

    if spawn([str(CODE_ROOT / ".venv/bin/python"), "cli.py", "plan"], log_name="plan") is None:
        return "실행 실패"
    return "시작함"


def open_items(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """계획이 읽을 고장들. 자주 난 것이 먼저다."""
    rows = conn.execute(
        """SELECT * FROM error_queue
           WHERE status='open' AND class='actionable'
           ORDER BY count DESC, last_seen DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["context"] = json.loads(d.get("context") or "{}")
        except json.JSONDecodeError:
            d["context"] = {}
        out.append(d)
    return out


def summary(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """/errors 가 보여주는 목록. external도 같이 본다 — 무엇이 바깥 사정으로
    분류됐는지 사람이 확인할 수 있어야 오분류를 잡는다."""
    rows = conn.execute(
        """SELECT id, fingerprint, class, kind, exc_type, message, count, status, last_seen
           FROM error_queue
           ORDER BY (status='open') DESC, last_seen DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark(conn: sqlite3.Connection, ids: list[int], status: str, plan_id: int | None = None) -> None:
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE error_queue SET status=?, plan_id=? WHERE id IN ({marks})",
        [status, plan_id, *ids],
    )
    conn.commit()
