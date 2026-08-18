"""텔레그램 알림 — 무인 실행 중 사람이 필요한 순간에 폰으로 알린다.

v1에는 **승인 게이트**(지원 직전에 폰으로 물어보고 버튼을 기다림)가 있었다.
v2는 완전 자동화가 목표라 그 게이트를 기본값으로 넣지 않는다 — 대신 에이전트가
스스로 못 넘는 지점(로그인 세션 끊김 등)에서 **일방향 알림**을 보낸다.

    승인 게이트: "지원해도 될까요?" → 사람이 답할 때까지 멈춰서 기다린다
    이 알림:     "여기서 막혔어요"    → 보내고 계속 진행한다 (막힌 것만 스킵)

되돌릴 수 없는 결정(지원 제출) 앞에서 승인 게이트가 필요해지면 v1의
`notify/telegram.py`에 있는 `request_approval()`을 그대로 옮겨오면 된다 —
그 설계도 이미 검증돼 있다.

## 설정

1. 텔레그램에서 @BotFather 에게 `/newbot` → 토큰을 받는다
2. 만든 봇에게 아무 메시지나 한 번 보낸다 (봇이 먼저 말을 걸 수 없다)
3. `python cli.py telegram-setup <토큰>` 을 실행하면 chat_id를 찾아 DB에 저장한다

토큰은 코드에 박지 않는다. 환경변수(`TELEGRAM_BOT_TOKEN`)가 있으면 그걸 우선한다.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from ..db import get_setting, set_setting

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

S_TOKEN = "telegram_bot_token"
S_CHAT = "telegram_chat_id"

# 텔레그램이 거부하는 길이. 넘기면 400 Bad Request다.
TEXT_LIMIT = 4096
PHOTO_CAPTION_LIMIT = 1024

# 실제로 쓰는 값은 상한이 아니라 이 여유값이다. 두 가지 이유가 있다:
#
# 1. 텔레그램은 길이를 **UTF-16 코드유닛**으로 센다. 이 저장소의 알림은 거의
#    매번 앞에 이모지(📄 ⚠️ ...)가 붙고, 이모지 하나가 파이썬 `len()`으로는
#    1인데 텔레그램에게는 2다. `len(text) == 4096`이 400을 받을 수 있다.
# 2. 자른 자리에 생략 표시(`…N자 생략`)를 덧붙이므로 그 자리도 남겨야 한다.
SAFE_TEXT = 3900

_TAG_RE = re.compile(r"<[^>]+>")

# 텔레그램 봇 토큰 모양(`<봇id 숫자>:<35자 안팎 문자열>`)을 로그·기록에서 가린다.
#
# 이 토큰은 요청 URL 경로(`/bot<token>/getMethod`)에 그대로 박히고, `getUpdates`
# 롱폴링(`listener.watch`, 25초 간격)이 그 URL로 계속 요청을 보낸다. `cli.py`가
# `logging.basicConfig(level=INFO)`를 켜두므로 httpx가 요청마다 남기는 INFO 로그
# ("HTTP Request: GET https://.../bot<token>/getUpdates ...")가 그대로
# `listen.err.log`에 평문으로 쌓인다. `errors.record()`가 저장하는 예외
# 메시지·traceback·명령행(`telegram-setup <token>` 같은)에도 같은 문제가 있다 —
# 한 자리에서 가려서 두 경로 모두 막는다.
_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")


def mask_token(text: str) -> str:
    """텍스트 안의 텔레그램 봇 토큰을 가린다. 토큰이 없으면 그대로 돌려준다."""
    return _TOKEN_RE.sub("<TELEGRAM_TOKEN>", text or "")


def _clip(text: str, limit: int = SAFE_TEXT) -> str:
    """상한을 넘는 본문을 **HTML이 깨지지 않는 자리**에서 자른다.

    단순 `text[:limit]`으로는 부족하다 — 우리는 `parse_mode='HTML'`로 보내므로
    `<pre>`나 `&lt;` 한가운데가 잘리면 텔레그램이 다시 400(can't parse entities)을
    돌려주고, 그러면 자른 보람도 없이 알림이 사라진다.

    정규식이 필요한 일이 아니라 `str.rfind`만 쓴다.
    """
    text = text or ""
    if len(text) <= limit:
        return text

    head = text[:limit]

    # 줄 단위로 물러난다. 단 너무 많이 버려야 하면(줄바꿈 없는 한 줄짜리 긴
    # 본문) 하드컷을 쓴다 — 안 그러면 거의 전부를 버린다.
    nl = head.rfind("\n")
    if nl >= limit // 2:
        head = head[:nl]

    # 태그 반토막(`<b` / `<pre`)과 엔티티 반토막(`&lt`)을 버린다.
    lt = head.rfind("<")
    if lt > head.rfind(">"):
        head = head[:lt]
    amp = head.rfind("&")
    if amp > head.rfind(";"):
        head = head[:amp]

    return f"{head}\n<i>…{len(text) - len(head)}자 생략</i>"


def _strip_html(text: str) -> str:
    """HTML 파싱이 실패했을 때 평문으로 다시 보내기 위한 변환.

    태그를 걷어낸 뒤 `html.unescape()`까지 하는 이유: 상류가 사용자·플랫폼
    문자열을 `html.escape()`로 감싸 넘기므로, 그냥 태그만 지우면 평문 알림에
    `&lt;script&gt;` 같은 게 그대로 보인다.
    """
    return html.unescape(_TAG_RE.sub("", text or ""))


class TelegramNotConfigured(RuntimeError):
    pass


def _creds(conn: sqlite3.Connection) -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or get_setting(conn, S_TOKEN)
    chat = os.environ.get("TELEGRAM_CHAT_ID") or get_setting(conn, S_CHAT)
    if not token or not chat:
        raise TelegramNotConfigured(
            "텔레그램이 설정되지 않았다.  python cli.py telegram-setup <봇토큰>"
        )
    return token, chat


def _call(
    token: str, method: str, *, retries: int = 2, retry_wait: float = 1.5, **payload: Any
) -> dict[str, Any]:
    """텔레그램 API를 부른다. 연결 오류는 짧게 재시도한다.

    실측(2026-08-16): 사람이 보낸 명령에 답장하려던 sendMessage가
    "Connection reset by peer"로 실패했다. `notify()`는 실패를 삼키고
    포기하는 게 설계 의도지만(본 파이프라인을 막지 않으려고), 그 대가로
    **그 답장이 그 자리에서 영영 사라졌다** — 헬스 알림처럼 "놓쳐도 다음에
    또 온다"가 아니라 일회성 응답이라 재발송 기회가 없다. 몇 초 재시도로
    넘어갈 수 있는 흔한 일시 오류이므로 여기서 먼저 흡수한다.

    httpx.HTTPError만 재시도한다 — 연결 오류·타임아웃·5xx가 여기 걸린다.
    `ok: false` 응답(잘못된 토큰 등 영구 실패)은 재시도해도 안 풀리므로 뺀다.
    같은 이유로 **400 같은 4xx도 재시도하지 않는다**(429는 예외 — 그건 기다리면
    풀린다). 본문이 너무 길거나 HTML이 깨진 요청은 몇 번을 다시 보내도 같은 답을
    받고, 3초만 버린 뒤 호출부의 폴백을 그만큼 늦춘다.
    """
    import time

    r = None
    for attempt in range(retries + 1):
        try:
            r = httpx.post(API.format(token=token, method=method), json=payload, timeout=30)
            r.raise_for_status()
            break
        except httpx.HTTPError as e:
            if (
                isinstance(e, httpx.HTTPStatusError)
                and 400 <= e.response.status_code < 500
                and e.response.status_code != 429
            ):
                raise
            if attempt < retries:
                time.sleep(retry_wait)
                continue
            raise
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method} 실패: {data}")
    return data["result"]


# 텔레그램 클라이언트가 "/"만 쳤을 때 보여주는 자동완성 목록.
#
# API 제약: command 필드는 영문 소문자·숫자·밑줄만 허용한다(1~32자) — 그래서
# 한글 명령은 여기 못 넣는다. /apply는 원래 한글 이름(/지원시작)도 같이
# 받았으나, 실제 제출은 이 명령이 하지 않고(텔레그램에서 되는 건 준비까지 —
# 제출은 항상 폰 승인 버튼을 눌러야 나간다) 두 이름을 둘 이유가 없어 영문
# 하나로 정리했다(listener.py `_handle`).
# 설명(description)은 한글을 그대로 쓸 수 있다.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("status", "현재 상태"),
    ("quota", "오늘 남은 지원 한도"),
    ("blocked", "막힌 이유"),
    ("targets", "지원 대기열"),
    ("running", "지금 도는 작업"),
    ("stop", "도는 작업 중단 (강제: /stop 강제)"),
    ("apply", "지원준비를 지금 바로 (수집은 낮 12시에 따로)"),
    ("errors", "고장 큐"),
    ("plan", "지금 수정 계획을 세운다"),
    ("plans", "수정 계획 목록과 상태"),
    ("reverts", "자동반영된 커밋 목록"),
    ("improve", "수정 계획 (plan의 옛 이름)"),
    ("pause", "자동지원 정지"),
    ("resume", "재개"),
    ("queue", "개발 지시 큐"),
    ("guide", "작성 가이드 수정 (뒤에 지시문을 붙여서)"),
    ("revlog", "수정 요청 원장 (목록/edit/delete)"),
    ("help", "명령어 전체 도움말"),
]


def set_commands(conn: sqlite3.Connection) -> bool:
    """텔레그램 네이티브 "/" 자동완성 목록을 등록한다.

    setMyCommands를 한 번도 안 부르면 "/"를 쳐도 아무 목록도 안 뜬다 —
    봇이 명령을 처리 못 하는 게 아니라, 텔레그램 클라이언트가 뭐가 있는지
    몰라서다. 멱등이라 몇 번을 다시 불러도 안전하다(매번 전체를 덮어쓴다).
    """
    token, _ = _creds(conn)
    _call(
        token, "setMyCommands",
        commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS],
    )
    return True


def setup(conn: sqlite3.Connection, token: str) -> dict[str, Any]:
    """봇에게 보낸 마지막 메시지에서 chat_id를 찾아 저장한다."""
    me = _call(token, "getMe")
    r = httpx.get(API.format(token=token, method="getUpdates"), timeout=30).json()
    chats = [
        u["message"]["chat"] for u in r.get("result", []) if u.get("message", {}).get("chat")
    ]
    if not chats:
        raise RuntimeError(
            f"@{me['username']} 에게 아무 메시지나 한 번 보낸 뒤 다시 실행할 것. "
            "봇은 먼저 말을 걸 수 없다."
        )
    chat = chats[-1]
    set_setting(conn, S_TOKEN, token)
    set_setting(conn, S_CHAT, str(chat["id"]))
    _call(
        token, "sendMessage", chat_id=chat["id"],
        text=f"연결됐다. 에이전트가 스스로 못 넘는 지점에서 여기로 알린다.\n봇: @{me['username']}",
    )
    try:
        set_commands(conn)
    except Exception as e:  # noqa: BLE001
        log.warning("명령어 자동완성 등록 실패(무시): %s", e)
    return {
        "bot": me["username"],
        "chat_id": chat["id"],
        "chat_name": chat.get("first_name") or chat.get("title", ""),
    }


def send_photo(
    conn: sqlite3.Connection,
    path: str,
    caption: str = "",
    buttons: list[list[dict[str, str]]] | None = None,
) -> bool:
    """스크린샷을 보낸다. 이게 '검토' 단계의 실물이다.

    셀렉터나 로그를 보내는 건 의미가 없다 — 사람은 `button.css-1x2y3z`가 맞는
    버튼인지 알 수 없다. 다 채워진 폼 사진은 알 수 있다. 폰으로 그 사진을 보고
    제출 여부를 판단하는 것이 이 파이프라인의 사람 몫이다.

    캡션은 텔레그램 제한(1024자)에 맞춰 자른다.
    """
    p = Path(path)
    if not p.exists():
        log.info("보낼 스크린샷이 없다: %s", path)
        return False
    try:
        token, chat = _creds(conn)
        data = {
            "chat_id": chat,
            "caption": _clip(caption, PHOTO_CAPTION_LIMIT),
            "parse_mode": "HTML",
        }
        if buttons:
            # 되돌릴 수 없는 동작 앞의 게이트다. 사람이 사진을 보고 누른다.
            data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        with p.open("rb") as fh:
            r = httpx.post(
                API.format(token=token, method="sendPhoto"),
                data=data,
                files={"photo": (p.name, fh, "image/png")},
                timeout=60,
            )
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except TelegramNotConfigured:
        log.info("텔레그램 미설정 — 사진 전송 건너뜀")
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("사진 전송 실패(무시): %s", e)
        return False


def notify(conn: sqlite3.Connection, text: str) -> bool:
    """일방향 알림. 실패해도 본 작업(수집·판정)을 막지 않는다.

    반환값은 성공 여부다 — 실패를 조용히 삼키되, 호출부가 로그에 남길 수 있게 알려준다.

    이 함수가 실패하면 **사람에게 알릴 다른 통로가 없다**(폰 명령의 응답,
    `cli.py`의 보고가 여기로 나간다). 그래서 세 가지를 이 자리에서 감당한다:

    - 상한 4096은 UTF-16 기준이라 `SAFE_TEXT`로 여유를 둔다(`_clip` 주석 참조)
    - **잘려서라도 보내는 편이 사라지는 것보다 낫다** — 400을 받으면 그 알림은
      호출부가 재발송하지 않으므로 그 자리에서 영영 없어진다
    - HTML이 깨져 400이 나면 `parse_mode`를 빼고 평문으로 한 번 더 보낸다.
      서식이 빠진 알림은 읽을 수 있지만, 안 온 알림은 읽을 수 없다
    """
    try:
        token, chat = _creds(conn)
        _call(
            token, "sendMessage", chat_id=chat, text=_clip(text),
            parse_mode="HTML", disable_web_page_preview=True,
        )
        return True
    except TelegramNotConfigured:
        log.info("텔레그램 미설정 — 알림 건너뜀: %s", text[:50])
        return False
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 400:
            log.warning("텔레그램 알림 실패(무시): %s", e)
            return False
        try:
            _call(
                token, "sendMessage", chat_id=chat, text=_clip(_strip_html(text)),
                disable_web_page_preview=True,
            )
        except Exception as e2:  # noqa: BLE001
            log.warning("텔레그램 알림 실패(무시): %s", e2)
            return False
        log.warning("텔레그램 400(%s) — 평문으로 재전송함", e)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("텔레그램 알림 실패(무시): %s", e)
        return False


def notify_with_buttons(
    conn: sqlite3.Connection, text: str, buttons: list[list[dict[str, str]]]
) -> bool:
    """버튼이 붙은 텍스트 알림. `send_photo`의 사진 없는 판이다.

    승인 게이트가 늘 사진을 갖고 있는 건 아니다 — 지원서 검토는 채워진 폼
    사진으로 판단하지만, 수정 계획 승인은 글로 판단한다. 그때 `notify()`를
    쓰면 버튼이 통째로 사라져서 **누를 것이 없는 승인 요청**이 도착한다.

    길이·HTML 처리는 `notify()`와 같다. 여기가 특히 걸리기 쉬운 이유: 수정 계획
    승인 본문은 커밋 메시지를 `<pre>...</pre>`로 감싸 여러 줄에 걸치므로, 하드컷은
    그 태그를 반으로 가른다. 평문 폴백에서도 `reply_markup`은 반드시 유지한다 —
    버튼이 이 함수의 존재 이유다.
    """
    try:
        token, chat = _creds(conn)
        _call(
            token, "sendMessage", chat_id=chat, text=_clip(text),
            parse_mode="HTML", disable_web_page_preview=True,
            reply_markup={"inline_keyboard": buttons},
        )
        return True
    except TelegramNotConfigured:
        log.info("텔레그램 미설정 — 알림 건너뜀: %s", text[:50])
        return False
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 400:
            log.warning("버튼 알림 실패(무시): %s", e)
            return False
        try:
            _call(
                token, "sendMessage", chat_id=chat, text=_clip(_strip_html(text)),
                disable_web_page_preview=True,
                reply_markup={"inline_keyboard": buttons},
            )
        except Exception as e2:  # noqa: BLE001
            log.warning("버튼 알림 실패(무시): %s", e2)
            return False
        log.warning("텔레그램 400(%s) — 평문으로 재전송함(버튼 유지)", e)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("버튼 알림 실패(무시): %s", e)
        return False


def answer_callback(conn: sqlite3.Connection, callback_id: str, text: str = "") -> None:
    """버튼을 누른 뒤 폰에 뜨는 로딩 표시를 걷어낸다. 실패해도 무시한다."""
    try:
        token, _ = _creds(conn)
        httpx.post(
            API.format(token=token, method="answerCallbackQuery"),
            json={"callback_query_id": callback_id, "text": text[:200]},
            timeout=20,
        )
    except Exception as e:  # noqa: BLE001
        log.info("콜백 응답 실패(무시): %s", e)
