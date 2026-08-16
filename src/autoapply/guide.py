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
from .db import now
from .paths import PROFILE_DIR, RESUME_SRC_DIR

log = logging.getLogger(__name__)

GUIDE_PATH = RESUME_SRC_DIR / "resume-guide.md"

# 백업은 RESUME_SRC_DIR **밖**에 둔다. 안에 두면 load_guide()의 rglob("*.md")가
# 백업까지 사실 저장소로 합쳐서, 옛 규칙과 새 규칙이 동시에 프롬프트에 들어간다.
BACKUP_DIR = PROFILE_DIR / "guide-backups"

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

    # 원장을 함께 준다. "이 지적이 세 번 나왔으니 규칙으로 올려줘" 같은 지시는
    # 원장을 봐야 무슨 지적이었는지 알 수 있다.
    prompt = PROMPT.format(instruction=instruction, guide=before) + load_revision_log()
    raw = llm.ask(prompt, model=cfg.get("guide_model", "claude-opus-5"))

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
    return {"ok": True, "restored": pick.name}
