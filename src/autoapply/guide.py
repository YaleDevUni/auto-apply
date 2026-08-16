"""작성 가이드(resume-guide.md)를 폰에서 고친다. Opus 5가 편집한다.

## 왜 전체 재작성이 아니라 부분 치환인가

가이드는 **사실 저장소**다. 여기 없는 수치·기술은 이력서에 못 쓴다. 모델에게
"고쳐서 전문을 출력하라"고 하면 고치라고 한 곳 말고도 바뀐다 — 문장을 다듬고,
중복이라 판단한 줄을 지우고, 요약한다. 그렇게 사실 하나가 사라지면 이후 모든
이력서에서 그 경력이 조용히 빠지고, **아무도 알아채지 못한다.**

그래서 모델은 `{old, new}` 쌍만 낸다. 적용은 코드가 한다:

    old 를 못 찾으면      → 거절 (모델이 원문을 지어낸 것이다)
    old 가 2번 이상 나오면 → 거절 (어디를 고칠지 정해지지 않았다)

바꾸라고 지목한 자리 밖은 한 글자도 안 바뀐다는 게 코드로 보장된다.

## 왜 사람이 방아쇠를 당기나

원장(revision-log.md)에 같은 지적이 쌓이면 규칙으로 올릴 때가 된 것이지만,
그 판단을 자동으로 하면 한 공고의 특수한 요구가 규칙이 된다. 규칙이 된 순간
이후 모든 이력서에 적용되고, 잘못된 규칙일수록 티가 안 난다. 그래서 승격은
사람이 폰에서 시작한다.

## 왜 세션을 이어가나

처음엔 호출마다 완전히 새 대화였다(`llm.ask()`, `--no-session-persistence`).
실제로 써보니 "그거 말고 그 앞부분도" 같은 이어지는 지시를 매번 못 알아듣고
지시 하나하나가 완결된 문장이어야 했다 — 대화가 아니라 매번 처음 보는
사람에게 다시 설명하는 느낌이었다.

그래서 가이드 편집만 `llm.ask_session()`으로 세션을 이어간다. session_id를
`settings` 표에 저장해뒀다가 다음 호출에 `--resume`으로 넘긴다. 화제가
바뀌면(다른 섹션 얘기로 넘어가거나 오래 지나서) 낡은 맥락이 새 지시 해석에
계속 끼어들 수 있으니, 그럴 땐 사람이 `clear_session()`으로 끊는다 — 자동
만료를 두지 않은 이유는 "얼마나 지나면 무관해지는가"를 정할 근거가 없어서다.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any

from . import llm
from .assemble import load_revision_log
from .config import effective_config
from .db import connect, get_setting, now, set_setting
from .paths import PROFILE_DIR, RESUME_SRC_DIR

log = logging.getLogger(__name__)

GUIDE_PATH = RESUME_SRC_DIR / "resume-guide.md"

# 백업은 RESUME_SRC_DIR **밖**에 둔다. 안에 두면 load_guide()의 rglob("*.md")가
# 백업까지 사실 저장소로 합쳐서, 옛 규칙과 새 규칙이 동시에 프롬프트에 들어간다.
BACKUP_DIR = PROFILE_DIR / "guide-backups"

SESSION_KEY = "guide_session_id"

_HEADING = re.compile(r"^#{1,4} .+$", re.MULTILINE)

PROMPT = """작성 가이드를 아래 지시대로 고쳐라. **바꿀 자리만** 짚어서 낸다.

# 지시
{instruction}

출력 — JSON만. 코드펜스도 설명도 붙이지 않는다:
{{"edits": [{{"old": "원문 그대로", "new": "바꿀 내용", "why": "한 줄"}}],
  "note": "지시를 반영할 수 없었다면 그 이유. 없으면 빈 문자열"}}

규칙:
- `old`는 [작성 가이드]에 **있는 그대로** 옮긴다. 공백·줄바꿈까지 같아야 하고,
  기억으로 쓰지 말고 아래 본문에서 찾아 그대로 복사한다.
- `old`는 파일 안에서 **한 번만 나오는** 범위로 잡는다. 짧으면 앞뒤 줄을 붙여 늘린다.
- 새 규칙을 덧붙이는 것이면 `old`를 그 규칙이 들어갈 자리의 기존 줄로 잡고,
  `new`에 그 줄 + 새 내용을 함께 넣는다.
- **사실을 지우지 마라.** §3 사실 저장소의 수치·기술·기간·프로젝트는 사람이
  명시적으로 지우라고 한 게 아니면 건드리지 않는다. 문장을 다듬지도 않는다.
- 지시와 무관한 곳은 손대지 않는다. 편집은 꼭 필요한 만큼만.
- 지시가 사실 저장소에 없는 사실을 쓰라는 것이면 편집하지 말고 `note`에 적는다.

# 작성 가이드
{guide}
"""


def _apply(text: str, edits: list[dict[str, str]]) -> tuple[str, list[str]]:
    """치환을 적용한다. 하나라도 어긋나면 전부 적용하지 않는다.

    부분 적용은 최악이다 — 규칙 절반만 바뀐 가이드가 남고, 실패했다는 사실은
    메시지에만 있어서 다음 이력서부터 앞뒤가 안 맞는 지시를 따르게 된다.
    """
    out, applied = text, []
    for i, e in enumerate(edits, 1):
        old, new = e.get("old", ""), e.get("new", "")
        if not old:
            raise ValueError(f"{i}번 편집에 old가 없다")
        hits = out.count(old)
        if hits == 0:
            raise ValueError(f"{i}번 편집의 old를 가이드에서 못 찾았다: {old[:60]!r}")
        if hits > 1:
            raise ValueError(f"{i}번 편집의 old가 {hits}곳에 있다 — 범위를 넓혀야 한다")
        out = out.replace(old, new, 1)
        applied.append(e.get("why") or f"{old[:40]}…")
    return out, applied


def _session_id() -> str:
    conn = connect()
    try:
        return get_setting(conn, SESSION_KEY, "")
    finally:
        conn.close()


def _remember_session(session_id: str) -> None:
    conn = connect()
    try:
        set_setting(conn, SESSION_KEY, session_id)
    finally:
        conn.close()


def clear_session() -> dict[str, Any]:
    """가이드 수정 세션을 끊는다. 다음 지시는 새 대화로 시작한다.

    자동 만료가 없으므로 화제가 바뀌었을 때 사람이 직접 부른다 — 안 끊으면
    낡은 맥락("아까 그거")이 다음 지시 해석에 계속 끼어든다.
    """
    had = bool(_session_id())
    _remember_session("")
    return {"ok": True, "cleared": had}


def _diff(before: str, after: str, limit: int = 1800) -> str:
    lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="before", tofile="after", lineterm="", n=1))
    body = "\n".join(lines[2:])  # 파일명 두 줄은 뺀다
    return body[:limit] + ("\n… (생략)" if len(body) > limit else "")


def edit(instruction: str) -> dict[str, Any]:
    """지시대로 가이드를 고친다. 되돌릴 수 있게 백업을 먼저 남긴다."""
    if not GUIDE_PATH.exists():
        return {"ok": False, "reason": f"가이드가 없다: {GUIDE_PATH}"}

    before = GUIDE_PATH.read_text(encoding="utf-8")
    cfg = effective_config().get("llm", {})
    model = cfg.get("guide_model", "claude-opus-5")

    # 원장을 함께 준다. "이 지적이 세 번 나왔으니 규칙으로 올려줘" 같은 지시는
    # 원장을 봐야 무슨 지적이었는지 알 수 있다.
    prompt = PROMPT.format(instruction=instruction, guide=before) + load_revision_log()

    session_id = _session_id()
    try:
        resp = llm.ask_session(prompt, session_id=session_id or None, model=model, phase="guide_edit")
    except RuntimeError:
        # 저장해둔 session_id가 낡았을 수 있다(로컬 세션 기록이 정리됐거나
        # 다른 이유로 --resume이 실패). 한 번은 새 세션으로 다시 시도한다 —
        # 여기서 그냥 실패시키면 세션이 한 번 꼬인 뒤로 /guide 자체가 계속 죽는다.
        if not session_id:
            raise
        log.warning("가이드 세션(%s…) 재개 실패 — 새 세션으로 재시도", session_id[:8])
        _remember_session("")
        resp = llm.ask_session(prompt, session_id=None, model=model, phase="guide_edit")
    _remember_session(resp["session_id"])
    raw = resp["text"]

    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"응답을 JSON으로 못 읽었다: {e}", "raw": raw[:300]}

    edits = data.get("edits") or []
    note = (data.get("note") or "").strip()
    if not edits:
        return {"ok": False, "reason": note or "고칠 곳을 찾지 못했다"}

    try:
        after, applied = _apply(before, edits)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}

    # 제목이 사라졌다는 건 섹션이 통째로 날아갔다는 뜻이다. 부분 치환에서는
    # 나올 일이 아니지만, 나왔다면 지시를 잘못 읽은 것이므로 쓰지 않는다.
    lost = set(_HEADING.findall(before)) - set(_HEADING.findall(after))
    if lost:
        return {"ok": False, "reason": f"섹션이 사라진다: {sorted(lost)[:3]}"}

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"resume-guide-{now()[:19].replace(':', '')}.md"
    backup.write_text(before, encoding="utf-8")
    GUIDE_PATH.write_text(after, encoding="utf-8")

    log.info("가이드 수정 %d건 — 백업 %s", len(applied), backup.name)
    return {
        "ok": True,
        "edits": len(applied),
        "why": applied,
        "note": note,
        "backup": str(backup),
        "diff": _diff(before, after),
        "delta": len(after) - len(before),
    }


def revert(backup_name: str = "") -> dict[str, Any]:
    """마지막(또는 지정한) 백업으로 되돌린다."""
    backups = sorted(BACKUP_DIR.glob("resume-guide-*.md")) if BACKUP_DIR.is_dir() else []
    if not backups:
        return {"ok": False, "reason": "백업이 없다"}
    pick = next((b for b in backups if b.name == backup_name), backups[-1])
    GUIDE_PATH.write_text(pick.read_text(encoding="utf-8"), encoding="utf-8")
    # 파일이 대화가 모르는 상태로 바뀌었다. 세션이 살아 있으면 모델이 이미
    # 지워진 내용을 계속 참조하게 된다 — 되돌린 시점에 대화도 같이 끊는다.
    _remember_session("")
    return {"ok": True, "restored": pick.name}
