"""스크린샷을 읽어 판단한다. 여러 곳에서 같은 도구를 쓴다.

## 왜 공용인가

DOM 대조는 플랫폼마다 셀렉터를 알아야 하고, "내가 넣은 칸"만 본다. 안 건드린
섹션이 비었는지, 값이 엉뚱한 칸에 갔는지, 오류 배너가 떴는지는 못 잡는다.
스크린샷 판독은 그 층을 보고, **플랫폼을 몰라도 된다** — 사람인·자소설에도
그대로 쓴다.

쓰는 곳:

    이력서 조립    조립 결과가 편집기에 제대로 반영됐나
    지원 준비      폰으로 보내기 전에 폼이 제대로 채워졌나
    레시피 수복    셀렉터가 깨졌을 때 화면을 보고 무엇이 달라졌나
    오케스트레이터 실패 증적을 코딩 에이전트에게 넘겨 원인을 보게 한다

## 검증기가 오탐을 내면 무시당한다

무엇을 지적하지 **않을지**가 무엇을 지적할지만큼 중요하다. 플랫폼이 기본으로
깔아둔 빈 템플릿 행을 계속 문제라고 하면(실제로 그랬다) 사람은 곧 목록 전체를
건너뛰게 된다. `ignore`로 그런 것을 명시한다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import llm
from .config import effective_config

log = logging.getLogger(__name__)

# 모델이 이미지를 읽게 하려면 **프롬프트 본문에 경로가 있어야 한다.**
# 이미지를 첨부만 하고 경로를 안 적으면 읽지 않고, 판정이 조용히 비어서 돌아온다.
_HEAD = "먼저 Read 도구로 아래 파일을 읽어라. 읽지 않으면 판단할 수 없다.\n\n    {shot}\n\n"

_VERIFY = """{head}이 이미지는 {context}이다.
아래 [의도한 내용]이 화면에 제대로 반영됐는지 확인하라.

지적할 것 — 이것만:
1. [의도한 내용]에 있는 항목이 화면에서 안 보임 (누락)
2. 값이 엉뚱한 칸에 들어감
3. 문장이 중간에 잘림
4. 오류 메시지·경고 배너

지적하지 말 것:
{ignore}
- 줄바꿈·여백·글꼴 등 미관, 내용의 좋고 나쁨

출력 — 다른 말은 하지 마라:
- 문제가 없으면 정확히 `OK` 한 줄.
- 있으면 문제당 한 줄씩 `- `로 시작하는 목록.

[의도한 내용]
{intent}
"""

DEFAULT_IGNORE = (
    "- **[의도한 내용]에 없는 섹션의 빈 칸.** 플랫폼이 기본으로 깔아둔 템플릿 "
    "행이며 지울 수 없다. 빨간 * 가 붙어 있어도 정상이다."
)


def available() -> bool:
    return llm.cli_available()


def ask(shot: str | Path, question: str, *, ignore: str = "") -> str:
    """스크린샷에 대해 자유롭게 묻는다. 레시피 수복처럼 '무엇이 보이나'가 필요할 때.

    ignore를 안 주면 `verify()`가 막아둔 오탐이 그대로 돌아온다. 실제로 겪었다:
    편집기에 회색으로 깔린 빈 템플릿 행(시험명* 같은)을 '필수 미입력'이라고
    보고해서, 멀쩡한 이력서를 미완성으로 판단할 뻔했다. 화면을 자유롭게 묻는
    질문일수록 **무엇을 무시할지**를 같이 줘야 한다.
    """
    path = Path(shot)
    if not path.exists():
        raise FileNotFoundError(f"스크린샷이 없다: {path}")
    cfg = effective_config().get("llm", {})
    tail = f"\n\n지적하지 말 것:\n{ignore}" if ignore else ""
    return llm.ask(
        _HEAD.format(shot=path) + question + tail,
        image_paths=[path],
        model=cfg.get("vision_model", cfg.get("model", "claude-sonnet-5")),
    ).strip()


def verify(
    shot: str | Path,
    intent: str,
    *,
    context: str = "웹 화면",
    ignore: str = DEFAULT_IGNORE,
) -> dict[str, Any]:
    """의도한 내용이 화면에 반영됐는지 본다.

    반환: {ok, issues, raw}. ok가 None이면 **판단하지 못한 것**이고 실패가 아니다 —
    모르는 것과 틀린 것을 구분한다. 여기서 실패로 처리하면 멀쩡한 결과가
    사람 손으로 넘어간다.
    """
    path = Path(shot)
    if not path.exists():
        return {"ok": None, "reason": "스크린샷 없음", "issues": []}

    try:
        out = ask(
            path,
            _VERIFY.format(head="", context=context, ignore=ignore, intent=intent).lstrip(),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("비전 점검 실패(무시): %s", e)
        return {"ok": None, "reason": str(e)[:150], "issues": []}

    ok = out.upper().startswith("OK")
    issues = [] if ok else [ln.strip() for ln in out.splitlines() if ln.strip().startswith("-")]
    return {"ok": ok, "issues": issues, "raw": out[:800]}
