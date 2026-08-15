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


def _call(token: str, method: str, **payload: Any) -> dict[str, Any]:
    r = httpx.post(API.format(token=token, method=method), json=payload, timeout=30)
    r.raise_for_status()
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


def send_photo(conn: sqlite3.Connection, path: str, caption: str = "") -> bool:
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
        with p.open("rb") as fh:
            r = httpx.post(
                API.format(token=token, method="sendPhoto"),
                data={"chat_id": chat, "caption": caption[:1024], "parse_mode": "HTML"},
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
