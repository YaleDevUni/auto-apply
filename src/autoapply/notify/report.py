"""지원 준비 결과를 사람에게 전달한다 — 알림 조립과 전송 큐.

`telegram.py`가 "어떻게 보내는가"라면 여기는 **"무엇을 어떤 모양으로 보내는가"**다.
CLAUDE.md §11이 notification composition을 CLI에서 빼라고 지목한 자리이고,
§21의 anti-pattern("CLI가 DB + browser + LLM + Telegram을 모두 직접 호출")에서
Telegram 조각이 여기로 온다.

`cli.py`에서 그대로 옮겼다 — 동작은 한 줄도 바꾸지 않았다.
"""

from __future__ import annotations

import html
import json
import logging

from ..db import connect, now

log = logging.getLogger(__name__)


def queue(
    conn, *, job_id: int | None, caption: str, photo_path: str | None, buttons: list | None
) -> None:
    """지금 보내지 않고 쌓아둔다. `flush-notify`가 나중에 순서대로 보낸다."""
    conn.execute(
        "INSERT INTO pending_notifications (job_id, caption, photo_path, buttons, created_at) "
        "VALUES (?,?,?,?,?)",
        (job_id, caption, photo_path, json.dumps(buttons) if buttons else None, now()),
    )
    conn.commit()


def flush() -> dict:
    """쌓인 지원 준비 알림을 순서대로 보낸다. 새벽 루프가 만든 것을 아침에 한 번에 본다.

    사진이 없으면 텍스트로만 보낸다 — `send_photo`가 실패(파일 삭제 등)해도
    캡션은 반드시 도착해야 무엇이 있었는지 안다.
    """
    from . import telegram

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM pending_notifications WHERE sent_at IS NULL ORDER BY id"
        ).fetchall()
        sent = 0
        for r in rows:
            buttons = json.loads(r["buttons"]) if r["buttons"] else None
            ok = bool(
                r["photo_path"]
                and telegram.send_photo(conn, r["photo_path"], r["caption"], buttons)
            )
            if not ok:
                # 버튼을 떨어뜨리면 안 된다. 승인 요청이 새벽에 쌓였다가
                # 아침에 **누를 것 없이** 도착한다 — 사진이 없는 알림(수정 계획
                # 승인 등)이 정확히 그 경우다.
                if buttons:
                    telegram.notify_with_buttons(conn, r["caption"], buttons)
                else:
                    telegram.notify(conn, r["caption"])
            conn.execute(
                "UPDATE pending_notifications SET sent_at=? WHERE id=?", (now(), r["id"])
            )
            sent += 1
        conn.commit()
        if sent:
            telegram.notify(conn, f"🌙 새벽 사이에 {sent}건 준비됐습니다 — 위에서부터 검토해주세요.")
        return {"sent": sent}
    finally:
        conn.close()


def prepared(target: dict, result: dict, *, defer: bool = False) -> None:
    """준비 결과를 폰으로 보낸다(또는 defer=True면 나중에 보낼 큐에 쌓는다).

    성공이면 **스크린샷**, 실패면 이유. 실패가 로그에만 남으면 아침에 로그를
    뒤져야 안다 — 실제로 06시 사이클이 클릭 타임아웃으로 통째로 실패했는데
    그렇게 발견했다. 무인 운영에서 "아무 일도 안 일어남"과 "망가져서 못 함"은
    겉보기에 같다.
    """
    from . import telegram

    conn = connect()
    try:
        head = (
            f"{target['fit_score']}점 · {html.escape(str(target['company']))} — "
            f"{html.escape(str(target['title'])[:40])}"
        )
        apply_res = result.get("apply") or {}
        err = result.get("error") or apply_res.get("error") or result.get("stopped")

        # 이미 지원한 자리는 **실패가 아니다.** 같은 붉은 글씨로 보내면 사람이
        # 고쳐야 할 것과 그냥 넘어간 것이 구분되지 않는다. 사진도 버튼도 없이
        # 한 줄만 보낸다 — 판단할 게 없는 알림이다.
        if result.get("already_applied"):
            caption = (
                f"⏭ <b>이미 지원한 공고</b>\n{head}\n"
                "<i>예전에 직접 지원하신 자리입니다. 준비하지 않고 대기열에서 뺐습니다.</i>"
            )
            if defer:
                queue(
                    conn, job_id=target.get("job_id"), caption=caption,
                    photo_path=None, buttons=None,
                )
            else:
                telegram.notify(conn, caption)
            return

        if err:
            caption = f"❌ <b>지원 준비 실패</b>\n{head}\n<i>{html.escape(str(err)[:200])}</i>"
            if defer:
                queue(
                    conn, job_id=target.get("job_id"), caption=caption,
                    photo_path=None, buttons=None,
                )
            else:
                telegram.notify(conn, caption)
            return

        # 폰으로 보내는 사진은 실제 원티드 화면이어야 한다. 로컬에서 그려낸
        # 이미지는 실제로 어떻게 보일지 안 알려준다 — 사람이 판단하는 건
        # "이게 정말 이렇게 나갈까"이지 우리가 그린 문서가 아니다.
        #
        # 편집기 화면(resume.shot)을 우선한다 — 새로고침 후(=저장 확인 후)
        # 찍은 것이라 실제로 저장된 내용이 보인다. 그게 없으면(스크린샷 실패
        # 등) 지원 폼 화면(apply.evidence)으로 대신한다 — 그것도 실제 화면이다.
        shot = (result.get("resume") or {}).get("shot") or apply_res.get("evidence")

        # 폰으로 보내기 전에 화면을 한 번 읽는다. 사람이 사진을 보고 판단하는
        # 것과 같은 층위를 기계가 먼저 훑어, 명백한 문제는 캡션에 적어 보낸다.
        verdict = ""
        if shot:
            from .. import vision

            # 무엇을 보라고 할지가 중요하다. 전체 페이지 스크린샷에서 우측
            # 지원 패널은 작게 잡히므로, 개별 입력값까지 확인하라고 하면
            # "안 보인다"는 오탐이 난다. 사람이 사진으로 판단할 수 있는 수준 —
            # 제출 버튼이 눌릴 상태인가, 이력서가 골라졌나 — 만 묻는다.
            v = vision.verify(
                shot,
                "이력서 문서: 이름·간단 소개·경력·학력·스킬이 채워져 있고, "
                "문장이 중간에 끊기거나 빈 섹션이 없어야 한다.",
                context="지원에 제출될 이력서 문서",
                job_id=target.get("job_id"),
            )
            if v["ok"] is False and v["issues"]:
                verdict = "\n⚠️ " + "\n⚠️ ".join(i.lstrip("- ")[:70] for i in v["issues"][:3])
            elif v["ok"]:
                verdict = "\n✅ 화면 점검 이상 없음"

        from .. import assemble as _asm

        reg = _asm.registration(target["job_id"], conn)
        links = []
        if reg.get("resume_url"):
            links.append(f'📝 <a href="{reg["resume_url"]}">이력서 보기</a>')
        if target.get("url"):
            links.append(f'🔗 <a href="{target["url"]}">공고 보기</a>')

        # 이력서 본문으로는 못 하는 요구(성적증명서 첨부, 포트폴리오 파일 별도
        # 업로드, 이메일 송부 등)는 여기서만 알린다. 본문에 적으면 하지 않은 일을
        # 했다고 쓰는 것이 되고, 승인 화면 밖에서는 사람이 볼 자리가 없다.
        caption = (
            f"📄 <b>지원 준비됨</b>\n{head}{verdict}"
            + _asm.review_block(_asm.review_notes(target["job_id"], conn))
            + _asm.todo_block(_asm.manual_todos(target["job_id"], conn))
            + "\n\n"
            + ("  ·  ".join(links) + "\n\n" if links else "")
            + "<i>이력서는 이미 만들어져 있습니다. 승인하면 그대로 제출합니다.</i>"
        )
        # 세 갈래다. '건너뛰기'만 있던 때는 같은 공고가 다음 사이클에 또 올라와
        # 같은 판단을 반복하게 했다. 거절에도 종류가 있다 —
        #   폐기   이 자리는 아니다. 다시 올리지 마라
        #   수정   자리는 맞는데 내용이 아니다. 고쳐서 다시 가져와라
        buttons = [
            [{"text": "✅ 승인 (제출)", "callback_data": f"submit:{target['job_id']}"}],
            [
                {"text": "🗑 폐기", "callback_data": f"drop:{target['job_id']}"},
                {"text": "✏️ 수정요청", "callback_data": f"revise:{target['job_id']}"},
            ],
        ]
        if defer:
            queue(
                conn, job_id=target.get("job_id"), caption=caption, photo_path=shot,
                buttons=buttons,
            )
        elif not (shot and telegram.send_photo(conn, shot, caption, buttons)):
            telegram.notify(conn, caption)
    finally:
        conn.close()


def tell(text: str) -> None:
    """폰으로 한 줄 보낸다. 실패해도 흐름을 멈추지 않는다."""
    from . import telegram

    conn = connect()
    try:
        telegram.notify(conn, text)
    except Exception as e:  # noqa: BLE001
        log.warning("알림 실패: %s", e)
    finally:
        conn.close()


def circuit_break(key: str, count: int, prepared: int, attempted: int) -> None:
    """왜 대기열이 남았는데 멈췄는지 폰에 알린다.

    이 알림이 없으면 "목표 5건인데 2건만 하고 끝났다"가 대기열 소진과 구별되지
    않는다. 둘은 정반대다 — 하나는 할 일이 없는 것이고 하나는 망가진 것이다.
    """
    import html

    from . import telegram

    conn = connect()
    try:
        telegram.notify(
            conn,
            f"🛑 <b>같은 오류가 {count}번 — 지원준비를 접습니다</b>\n\n"
            f"<i>{html.escape(key[:250])}</i>\n\n"
            f"준비 {prepared}건 / 시도 {attempted}건에서 멈췄습니다. "
            "대기열은 남아 있지만 다음 공고도 같은 자리에서 죽습니다.\n"
            "<i>고장 큐에 쌓였습니다 — <code>/errors</code> 로 보고 "
            "<code>/plan</code> 으로 수정 계획을 세울 수 있습니다.</i>",
        )
    finally:
        conn.close()
