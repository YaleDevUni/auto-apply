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

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from ..db import get_setting, set_setting

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

S_TOKEN = "telegram_bot_token"
S_CHAT = "telegram_chat_id"


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
    """
    import time

    r = None
    for attempt in range(retries + 1):
        try:
            r = httpx.post(API.format(token=token, method=method), json=payload, timeout=30)
            r.raise_for_status()
            break
        except httpx.HTTPError:
            if attempt < retries:
                time.sleep(retry_wait)
                continue
            raise
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method} 실패: {data}")
    return data["result"]


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
        data = {"chat_id": chat, "caption": caption[:1024], "parse_mode": "HTML"}
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
    """
    try:
        token, chat = _creds(conn)
        _call(
            token, "sendMessage", chat_id=chat, text=text,
            parse_mode="HTML", disable_web_page_preview=True,
        )
        return True
    except TelegramNotConfigured:
        log.info("텔레그램 미설정 — 알림 건너뜀: %s", text[:50])
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("텔레그램 알림 실패(무시): %s", e)
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
