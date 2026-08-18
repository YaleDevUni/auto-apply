"""공고에 맞춰 이력서를 조립한다. 작성 → 검수 → (필요시) 1회 재작성.

## 왜 두 역할이 한 루프인가

계획서는 '이력서 어셈블러'와 '최종 검수'를 별도 에이전트로 뒀는데, 나누면 검수
결과가 어디로도 돌아가지 않는다. 검수가 누락을 찾았으면 다시 쓰는 게 전부이므로
한 루프 안의 두 역할로 본다.

    작성 → 검수 → 통과 → 끝
                → 반려 → 재작성 → 검수 → 통과/포기

## 재작성 한도가 2인 이유

실측: 소요시간 ≈ 2.3초(CLI 부팅) + 출력토큰/78. 즉 **출력 길이에 선형**이고,
재작성 한 번이 곧 시간 2배다. v1이 자소서 한 건에 98초, 나쁜 모델로는 618초가
걸린 원인이 이 루프였다(재작성 2회). 한도를 두지 않으면 무인 실행에서 한 건이
파이프라인 전체를 잡아먹는다. 초과하면 사람에게 넘긴다.

## 사실을 지어내지 않게 하는 장치

가이드(profile/resume/*.md)가 유일한 사실 출처다. 근거 없는 요건은 지어내지 말고
`[확인필요]`로 표시하게 하고, 그 표시가 남아 있으면 자동지원을 막는다 —
확인 안 된 문장이 사용자 이름으로 나가는 것보다 안 나가는 게 낫다.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import llm
from . import portfolio as portfolio_match
from . import tasks
from .config import effective_config
from .db import connect, now
from .paths import RESUME_OUT_DIR, RESUME_SRC_DIR, REVISION_LOG

log = logging.getLogger(__name__)

UNVERIFIED = re.compile(r"\[확인필요[^\]]*\]")

# 작성자가 이력서 뒤에 붙이는 자기보고 섹션. **제출물이 아니다.**
# 저장 전에 반드시 떼어낸다 — 붙은 채로 업로드하면 "이 회사가 요구하는 걸
# 나는 못 한다"는 목록을 그 회사에 제출하게 된다.
GAP_HEADING = re.compile(r"^#{1,3}\s*대응\s*근거\s*없음\s*$", re.MULTILINE)


def split_report(text: str) -> tuple[str, list[dict[str, str]]]:
    """(제출용 본문, 대응 근거 없는 요건 목록)으로 가른다.

    각 항목은 {level: 필수|우대|미상, text: ...}. level이 '필수'인 게 쌓이면
    적합도 점수가 과대평가됐다는 신호다 — 점수는 공고에 그 키워드가 있는지만
    세고, 사용자가 실제로 할 수 있는지와 대조하지 않기 때문이다.
    """
    m = GAP_HEADING.search(text)
    if not m:
        return text.strip(), []

    body = text[: m.start()].strip()
    gaps: list[dict[str, str]] = []
    for ln in text[m.end():].splitlines():
        ln = ln.strip()
        if not ln.startswith(("-", "·", "•")):
            continue
        item = ln.lstrip("-·• ").strip()
        level = "미상"
        for tag in ("필수", "우대"):
            if item.startswith(f"[{tag}]"):
                level, item = tag, item[len(tag) + 2:].strip()
                break
        gaps.append({"level": level, "text": item})
    return body, gaps


class NoGuide(RuntimeError):
    pass


def load_guide() -> str:
    """profile/resume/ 의 모든 .md를 합쳐 사실 저장소로 넘긴다.

    수정 원장(revision-log.md)은 여기 섞지 않는다. 원장이 폴더 안으로 옮겨져도
    걸러지도록 이름으로 한 번 더 막는다 — 원장은 '지난번 요청'이고 가이드는
    '규칙'이라, 같은 무게로 읽히면 한 공고의 특수한 요구가 규칙이 된다.
    """
    if not RESUME_SRC_DIR.is_dir():
        raise NoGuide(f"이력서 원본 디렉터리가 없다: {RESUME_SRC_DIR}")
    files = [f for f in sorted(RESUME_SRC_DIR.rglob("*.md")) if f.name != REVISION_LOG.name]
    if not files:
        raise NoGuide(f"{RESUME_SRC_DIR} 에 .md 파일이 없다")
    return "\n\n".join(f"<!-- {f.name} -->\n{f.read_text(encoding='utf-8')}" for f in files)


# ── 수정 요청 원장 ────────────────────────────────────────────────────────
#
# 사람이 "이건 빼줘"라고 하면 그 지시는 재작성 한 번에만 쓰이고 사라졌다.
# 같은 공고에 두 번째 요청이 오면 첫 번째를 잊었고, 다음 공고에서는 같은 말을
# 다시 해야 했다.
#
# 원장은 지시 하나를 한 줄로 남긴다. **지시만 남기면 안 된다** — "인프라 빼줘"는
# 그 공고가 프론트엔드였기 때문에 옳은 말이지, 언제나 옳은 말이 아니다.
# 그래서 지시와 함께 **왜 그때 그게 맞았는지**를 공고 근거로 붙인다.
#
# 반복되는 지적은 원장이 아니라 resume-guide.md 로 올라가야 한다. 그건 사람이
# 정한다 — 한 공고의 특수한 요구가 규칙이 되면 이후 모든 이력서가 조용히 오염된다.

LOG_HEADER = """# 수정 요청 원장

사람이 재작성을 요청할 때마다 한 줄씩 쌓인다. 이력서를 쓸 때 작성 가이드와 함께 읽는다.

각 줄은 **지시 + 그때 그게 맞았던 이유(공고 근거)** 다. 근거가 없으면 다음 공고에서
잘못 적용된다. 같은 지적이 반복되면 그건 여기가 아니라 `resume/resume-guide.md`에
규칙으로 올라가야 한다는 신호다.

"""

# 프롬프트에 넣을 최대 줄 수. 원장 자체는 계속 쌓이고, 읽을 때만 최근 것을 자른다.
LOG_READ_LIMIT = 60

def _log_entries() -> list[str]:
    if not REVISION_LOG.exists():
        return []
    return [ln.rstrip() for ln in REVISION_LOG.read_text(encoding="utf-8").splitlines()
            if ln.startswith("- ")]


def log_entries() -> list[str]:
    """원장의 항목 줄만. 번호는 이 목록의 1-based 인덱스다."""
    return _log_entries()


def _write_entries(entries: list[str]) -> None:
    """머리말은 보존하고 항목만 갈아끼운다."""
    head = LOG_HEADER
    if REVISION_LOG.exists():
        body = REVISION_LOG.read_text(encoding="utf-8")
        kept = [ln for ln in body.splitlines() if not ln.startswith("- ")]
        head = "\n".join(kept).rstrip() + "\n\n"
    REVISION_LOG.write_text(head + "\n".join(entries) + "\n", encoding="utf-8")


def log_edit(n: int, text: str | None) -> str:
    """n번 항목을 고치거나(text) 지운다(None). 고쳐진/지워진 줄을 돌려준다.

    사람이 폰에서 직접 손대는 통로다. 요약이 지시를 잘못 옮겼을 때 그 줄은
    앞으로 모든 이력서 프롬프트에 실려 나가므로, 파일을 열지 않고도 고칠 수
    있어야 한다.
    """
    entries = _log_entries()
    if not 1 <= n <= len(entries):
        raise IndexError(f"{n}번 항목이 없다 (현재 {len(entries)}건)")

    old = entries[n - 1]
    if text is None:
        entries.pop(n - 1)
    else:
        # 날짜·공고 머리는 유지하고 뒤의 내용만 바꾼다. 사람이 고치고 싶은 건
        # 요약이지 어느 공고였는지가 아니다.
        head, sep, _ = old.partition(" — ")
        entries[n - 1] = f"{head}{sep}{text.strip()}" if sep else f"- {text.strip()}"

    _write_entries(entries)
    return old


def load_revision_log(job_id: int | None = None) -> str:
    """프롬프트에 넣을 원장 블록. 없으면 빈 문자열.

    이 공고에 이미 받은 지시는 따로 떼어 앞에 놓는다. 섞어두면 모델이 남의
    공고 지시를 이 공고에 적용하거나, 반대로 방금 받은 지시를 흘려보낸다.
    """
    entries = _log_entries()
    if not entries:
        return ""

    mine: list[str] = []
    if job_id is not None:
        mark = f"#{job_id} "
        mine = [e for e in entries if mark in e]

    recent = [e for e in entries if e not in mine][-LOG_READ_LIMIT:]
    out = "\n# 수정 요청 원장\n"

    if mine:
        out += (
            "\n## 이 공고에 이미 받은 지시 — 전부 지킨다\n"
            "아래는 같은 공고에서 사람이 이미 요청한 것이다. 이번 지시만 반영하고\n"
            "이걸 되돌리면 사람이 같은 말을 또 해야 한다.\n"
            + "\n".join(mine) + "\n"
        )

    if recent:
        out += (
            "\n## 다른 공고에서 받은 지시\n"
            "각 줄은 `지시. 근거` 형태다. 근거를 보고 적용 여부를 정한다:\n"
            "- 근거가 공고 내용이면 → **이 공고에도 같은 근거가 성립할 때만** 적용한다.\n"
            "- 근거가 `취향 — 공고와 무관` 이면 → 공고를 가리지 않고 적용한다.\n"
            + "\n".join(recent) + "\n"
        )
    return out


def _condense(job: dict[str, Any], feedback: str) -> str:
    """지시를 공고 근거와 함께 한두 줄로 줄인다.

    본 조립과 같은 호출에 태우지 않고 따로 부르는 이유: 편집기 JSON 스키마에
    필드를 하나 더 얹으면 그 내용이 이력서 본문으로 새어 나온 전례가 있다
    (간단 소개 맨 앞에 판단 근거가 그대로 찍혔다). 원장은 제출물이 아니므로
    제출물을 만드는 호출과 섞지 않는다.
    """
    cfg = effective_config().get("llm", {})
    prompt = f"""사람이 이력서 재작성을 요청했다. 그 지시가 이것이다:

    「{feedback}」

이걸 원장에 남길 **한 줄**로 정리하라. 지시가 이미 짧으면 거의 그대로 옮긴다 —
줄일 것이 없다고 판단해 되묻지 마라. 위 따옴표 안이 지시의 전부이고, 그것으로 충분하다.
지시에 없는 내용을 보태거나 다른 지시로 바꾸지 않는다.

형식 — 이 형식만 출력한다. 앞뒤에 다른 말을 붙이지 않는다:
{{지시 요약}}. {{공고 근거}}

근거는 **지시가 무엇을 건드리는지**로 갈린다. 둘 중 하나다:

(가) **무엇을 쓸지** — 어떤 경험·기술·프로젝트를 넣고 뺄지, 무엇을 앞세울지.
     → 공고 근거를 쓴다. [채용공고]에서 **실제로 읽히는 사실**로,
       그 자리가 무엇을 요구하는지 / 어떤 기술이 요건에 있고 없는지를 근거로 든다.
     → 이 지시가 옳은 이유는 이 공고가 그런 자리이기 때문이므로, 근거 없이는 남기면 안 된다.

(나) **어떻게 쓸지** — 문장·분량·표기·형식.
     → 정확히 `취향 — 공고와 무관` 이라고만 쓴다. 공고에서 근거를 찾지 마라.

애매하면 물어라: 「다른 공고였다면 이 지시가 달라졌을까?」 달라지면 (가)다.

**근거를 지어내지 마라.** 공고에서 아무 사실이나 끌어와 붙이면 안 된다.
근거는 *이력서를 그렇게 써야 하는 이유*여야지, 공고 자체의 특징이면 안 된다.

그 외:
- 전체 200자 이내, 줄바꿈 없이 한 줄.

아래는 **형식 예시일 뿐이다. 예시의 내용을 가져다 쓰지 마라** — 위 따옴표 안의
지시만 정리한다. 지시가 한 단어여도 그 한 단어를 정리하지, 예시로 바꾸지 않는다.

(가) `공고와 무관한 인프라 경험 제외 요청. 프론트엔드 개발직이고 요구·우대·기술스택 어디에도 인프라 없음`
(가) `해외 영업 경험을 앞에. 수출 담당 자리이고 주요업무가 해외 바이어 발굴`
(나) `간단 소개를 더 짧게. 취향 — 공고와 무관`
(나) `문장 끝을 명사형으로 통일. 취향 — 공고와 무관`
     ↑ 공고 본문의 문체를 근거로 대면 안 된다. 이력서 문체는 어느 공고에서나 같다.

# 채용공고
{_jd_block(job)[:3000]}"""
    out = llm.ask(
        prompt, model=cfg.get("review_model", "claude-haiku-4-5-20251001"),
        job_id=job.get("id"), phase="revision_summary",
    )
    return " ".join(out.strip().splitlines()).strip()[:220]


# 요약 대신 사람에게 되묻거나 설명을 늘어놓은 응답. 짧은 지시에서 실제로 나왔다
# ("수정 요청 내용이 제공되지 않았습니다"). 이런 줄이 원장에 박히면 지워질 때까지
# 모든 이력서 프롬프트에 실려 나가므로, 통과시키느니 원문을 남긴다.
_META = re.compile(r"제공되지|제공된 \[|알려주시|해드리|드리겠|확인할 수 없|필요한데")


def append_revision(job_id: int, feedback: str, job: dict[str, Any] | None = None) -> str:
    """수정 요청을 원장에 한 줄 남기고 그 줄을 돌려준다.

    재작성 **전에** 부른다. 그래야 이번 지시도 원장에 들어간 상태로 조립되고,
    다음 요청이 와도 이번 것이 남아 있다.

    요약에 실패해도 원장 기록을 건너뛰지 않는다 — 원문 그대로라도 남기는 편이
    아무것도 안 남기는 것보다 낫다. 여기서 예외를 올리면 재작성 자체가 막힌다.
    """
    job = job or load_job(job_id)
    raw = " ".join(feedback.split())[:220]
    try:
        line = _condense(job, feedback)
        if _META.search(line) or not line:
            log.warning("원장 요약이 되묻는 응답이라 원문으로 남긴다: %s", line[:80])
            line = f"{raw} (요약 실패 — 근거 미확인)"
    except Exception as e:  # noqa: BLE001
        log.warning("원장 요약 실패, 원문으로 남긴다: %s", e)
        line = f"{raw} (요약 실패 — 근거 미확인)"

    track = job.get("track_label") or "-"
    entry = (f"- {now()[:10]} · #{job_id} {job['company']} {job['title'][:40]}"
             f"({track}) — {line}")

    REVISION_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not REVISION_LOG.exists():
        REVISION_LOG.write_text(LOG_HEADER, encoding="utf-8")
    with REVISION_LOG.open("a", encoding="utf-8") as f:
        f.write(entry + "\n")

    log.info("원장 기록: %s", entry)
    return entry


def load_job(job_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            """SELECT j.id, j.company, j.title, j.url, j.description, j.category,
                      j.experience_req, j.education_req, j.employment_type,
                      s.track, s.track_label, s.fit_score
               FROM jobs j LEFT JOIN screening s ON s.job_id = j.id
               WHERE j.id = ?""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"공고 {job_id}가 없다")
        return dict(row)
    finally:
        if own:
            conn.close()


def _jd_block(job: dict[str, Any]) -> str:
    return (
        f"회사: {job['company']}\n"
        f"포지션: {job['title']}\n"
        f"고용형태: {job.get('employment_type') or '-'}\n"
        f"경력요건: {job.get('experience_req') or '-'}\n"
        f"학력요건: {job.get('education_req') or '-'}\n"
        f"판정 트랙: {job.get('track_label') or '-'} (적합도 {job.get('fit_score')}점)\n\n"
        f"[공고 본문]\n{job.get('description') or ''}"
    )


def write(job: dict[str, Any], guide: str, feedback: str = "") -> str:
    """가이드 §1 처리 순서를 그대로 지시한다. 규칙은 가이드가 갖고 있으므로 옮겨 적지 않는다."""
    fix = ""
    if feedback:
        fix = (
            "\n\n# 재작성 지시\n"
            "직전 초안에서 아래 문제가 지적되었다. 그것만 고치고 나머지는 유지하라.\n"
            f"{feedback}\n"
        )

    prompt = f"""아래 [작성 가이드]의 지침을 그대로 따라 [채용공고]에 맞춘 이력서를 작성하라.

가이드의 §1 처리 순서, §2 출력 포맷, §4 문장 규칙, §5 일관성 체크리스트,
§7 직무별 강조 우선순위를 모두 적용한다.

절대 규칙:
- 가이드 §3 사실 저장소에 없는 수치·기술·프로젝트를 만들지 않는다.
- 공고가 요구하는데 근거가 없는 항목은 지어내지 말고, 이력서 본문 뒤에
  `## 대응 근거 없음` 섹션으로 따로 보고한다. 항목마다 공고에서 그것이
  **필수요건이면 `- [필수]`**, 우대사항이면 `- [우대]` 로 시작한다.
  이 구분은 자동지원 여부를 가르는 데 쓰이므로 공고 문구에 충실하게 판단한다.
- 이력서 본문만 출력한다. 설명·머리말·코드펜스를 붙이지 않는다.

# 작성 가이드
{guide}

# 채용공고
{_jd_block(job)}
{load_revision_log(job.get("id"))}{fix}"""
    return llm.ask(prompt, job_id=job.get("id"), phase="write")


def review(job: dict[str, Any], guide: str, resume: str) -> dict[str, Any]:
    """치명적 누락만 잡는다. 문체 취향으로 반려하면 재작성 루프만 돌고 안 끝난다."""
    cfg = effective_config().get("llm", {})
    prompt = f"""아래 이력서가 채용공고와 작성 가이드를 지켰는지 검수하라.

**치명적 문제만** 지적한다. 문체 취향·사소한 표현은 지적하지 않는다.
**`skills` 배열은 검수 대상이 아니다** — 코드가 따로 대조한다. 지적하지 마라.
치명적 문제란 다음뿐이다:
1. 가이드 §3 사실 저장소에 없는 사실이 들어감 (날조)
2. 가이드 §5 확정값 표와 다른 수치·기간 표기
3. 공고의 필수요건에 대응하는 경험이 있는데 이력서에서 빠짐
4. 가이드 §2 포맷 위반 (기간 누락, `· 사용기술:` 줄 누락)

출력 형식 — 다른 말은 하지 마라:
- 문제가 없으면 정확히 `OK` 한 줄만.
- 있으면 문제당 한 줄씩, `- ` 로 시작하는 목록만.

# 작성 가이드
{guide}

# 채용공고
{_jd_block(job)}

# 검수 대상 이력서
{resume}"""

    out = llm.ask(
        prompt, model=cfg.get("review_model", "claude-haiku-4-5-20251001"),
        job_id=job.get("id"), phase="review",
    ).strip()
    ok = out.upper().startswith("OK")
    issues = [] if ok else [ln.strip() for ln in out.splitlines() if ln.strip().startswith("-")]
    return {"ok": ok, "issues": issues, "raw": out}


# ── 편집기 JSON 검수 ──────────────────────────────────────────────────────
#
# `review()`는 마크다운 경로(build)에만 붙어 있었다. **실제 제출물을 만드는 경로는
# `build_editor_json`인데 거기엔 보는 눈이 없었다** — 실측으로 확인했다: 저장소
# 통틀어 `phase='review'` 2회, `phase='to_editor_json'` 36회.
#
# 그 사각지대에서 실제로 나간 것들(2026-08-17 벤치마크):
#   · 사실 저장소에 없는 `Spring Boot`가 스킬로 등록됨 (공고 121)
#   · 써본 적 없는 제품의 사용 후기가 본문에 들어감 (공고 119, 그 공고는
#     "허위사실이 있는 경우 합격 취소"를 명시하고 있었다)
#   · 경력 도메인을 `위탁판매` → `부동산 위탁판매`로 늘림 (공고 4110)
#
# 셋 다 형식 문제가 아니라 **가이드 전문과 대조해야 보이는 문제**다. 그래서 이
# 검수만 모델을 따로 둔다(`llm.editor_review_model`).

# 검수가 붙이는 등급. 자동지원을 막는 것은 `날조` 하나뿐이다 —
# 나머지로도 막으면 문체 지적 하나에 파이프라인이 선다.
FABRICATION = "날조"
REVIEW_LEVELS = (FABRICATION, "불일치", "누락", "형식")

# 등급표는 `- [날조] …` 로 달라고 했지만 모델은 마크다운을 얹어서 온다:
# 실측 — Sonnet 5는 `- \`[날조]\` …` 처럼 백틱으로 감쌌다. 그걸 못 읽으면 전부
# '형식'으로 떨어지고, **자동지원을 막는 유일한 등급인 '날조'가 통째로 사라진다.**
# 첫 A/B에서 이 파싱 하나 때문에 Sonnet이 못 잡은 것으로 집계됐다(실제로는 잡았다).
LEVEL_TAG = re.compile(
    r"^[-*\s]*[`*_\s]*\[(?P<level>" + "|".join(REVIEW_LEVELS) + r")\][`*_\s]*(?P<text>.*)$"
)


def unknown_skills(data: dict[str, Any], guide: str) -> list[str]:
    """가이드에 없는 스킬을 **코드로** 골라낸다. LLM에게 물을 일이 아니다.

    §3.5는 고정된 단어 목록이라 문자열 대조로 끝난다. 그런데 실측(2026-08-17
    검수 A/B)에서 두 모델 다 여기서 헛발질했다:

        Haiku   §3.5에 **있는** `Express`를 "없다"며 날조로 지목
        Sonnet  §3.5에 **있는** `Docker`·`AWS`를 "본문에 근거 문장이 없다"며 날조로 지목

    반대 방향도 났다 — 정작 없는 `Spring Boot`는 두 모델 다 잡았지만, 같은 답에
    오탐 5건이 섞여 오면 그 목록은 못 쓴다. 대조는 코드가 하고, LLM에게는 대조로는
    안 되는 것(산문 속 경험 날조)만 맡긴다.

    비교는 가이드 **전문**에 그 단어가 나오는지로 본다. §3.5 목록만 보면
    `Tesseract OCR`처럼 경력 설명에만 있는 기술이 전부 날조로 잡힌다.
    """
    low = guide.lower()
    return [
        sk for sk in (data.get("skills") or [])
        if isinstance(sk, str) and sk.strip() and sk.strip().lower() not in low
    ]


def review_editor(
    job: dict[str, Any], guide: str, data: dict[str, Any], *, model: str | None = None
) -> dict[str, Any]:
    """조립된 편집기 JSON을 사실 저장소와 대조한다.

    마크다운이 아니라 JSON을 그대로 보여준다. 사람이 읽을 문서로 바꿔서 보여주면
    어느 필드에 문제가 있는지가 흐려지고, 그러면 지적을 받아도 어디를 고칠지 모른다.
    """
    cfg = effective_config().get("llm", {})
    body = json.dumps(
        {k: v for k, v in data.items() if not k.startswith("_") and k != "gaps"},
        ensure_ascii=False, indent=2,
    )
    # 가이드·채용공고를 프롬프트 맨 앞에 둔다. build_editor_json(_editor_prompt)도
    # 같은 두 블록을 맨 앞에 두므로, 같은 공고를 조립한 직후 검수하면 프롬프트
    # 캐시 프리픽스가 겹쳐 가이드(실측 22,757토큰짜리 대부분)를 다시 쓰지 않고
    # 읽기로 받는다. 지시문을 앞에 두면 조립/검수 지시문이 서로 달라 첫 바이트부터
    # 어긋나고, 그러면 가이드가 똑같아도 캐시가 안 맞는다.
    prompt = f"""# 작성 가이드
{guide}

# 채용공고
{_jd_block(job)}

---

아래 [이력서 JSON]이 [작성 가이드]의 사실 저장소를 벗어나지 않았는지 검수하라.

**치명적 문제만** 지적한다. 문체 취향·사소한 표현은 지적하지 않는다.
**`skills` 배열은 검수 대상이 아니다** — 코드가 따로 대조한다. 지적하지 마라.
등급을 반드시 붙인다:

- `[날조]` 가이드 §3 사실 저장소에 **없는** 수치·기술·경험·도메인이 들어갔다.
  · 경력의 도메인·범위를 공고에 맞춰 늘렸다 (예: `위탁판매` → `부동산 위탁판매`)
  · **직접 겪어야 쓸 수 있는 서술을 지어냈다** — 제품 사용 후기, 서비스 개선 제안,
    방문기 등. 공고가 요구했더라도 §3에 그 경험이 없으면 지어낸 것이다
  · 개인 프로젝트를 회사 경력의 성과로 넣었다
- `[불일치]` 가이드 §5 확정값 표와 다른 수치·기간
- `[누락]` 공고의 **필수요건**에 대응하는 경험이 §3에 있는데 이력서에서 빠졌다
- `[형식]` 사고 과정·작업 노트가 본문에 남았다, `· 사용기술:` 줄 누락 등

판단 기준은 하나다: **이 문장을 뒷받침하는 근거가 [작성 가이드] 안에 있는가?**
없으면 [날조]다. "상식적이니 괜찮다"는 이유로 넘어가지 마라 — 이 이력서는 사람 이름으로
실제 제출된다.

출력 형식 — 다른 말은 하지 마라:
- 문제가 없으면 정확히 `OK` 한 줄만.
- 있으면 문제당 한 줄씩, `- [등급] 어느 필드의 무엇이 왜 문제인지` 형태로.

# 이력서 JSON
{body}"""

    out = llm.ask(
        prompt,
        model=model or cfg.get("editor_review_model", "claude-sonnet-5"),
        job_id=job.get("id"), phase="review_editor",
    ).strip()

    issues: list[dict[str, str]] = []
    if not out.upper().startswith("OK"):
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln.startswith("-"):
                continue
            m = LEVEL_TAG.match(ln)
            if m:
                level, text = m.group("level"), m.group("text").strip()
            else:
                level, text = "형식", ln.lstrip("- ").strip()
            issues.append({"level": level, "text": text})

    fabricated = [i for i in issues if i["level"] == FABRICATION]
    return {"ok": not issues, "issues": issues, "fabricated": fabricated, "raw": out}


def _drop_unknown_skills(data: dict[str, Any], guide: str) -> list[str]:
    """가이드에 없는 스킬을 지우고, 지운 것을 돌려준다."""
    bad = unknown_skills(data, guide)
    if not bad:
        return []
    log.warning("가이드에 없는 스킬 %d개 제거: %s", len(bad), bad)
    data["skills"] = [sk for sk in (data.get("skills") or []) if sk not in bad]
    return bad


def _review_and_fix(
    data: dict[str, Any], job: dict[str, Any], guide: str, job_id: int, feedback: str
) -> list[str]:
    """산문을 검수하고, 날조가 있으면 **한 번만** 다시 쓴다. 남은 지적을 돌려준다.

    한 번인 이유는 값이다 — 재작성은 조립 전체를 다시 받는 것이라 건당 30초·
    출력 2,700토큰이다. 두 번 돌려도 세 번째가 필요해지는 종류의 문제(근거가 정말
    없는 요건)는 재작성으로 안 풀린다. 그건 사람이 볼 일이다.

    검수가 실패해도(한도·네트워크) 조립을 막지 않는다. 검수는 있으면 좋은 눈이지
    제출물을 만드는 단계가 아니다 — 여기서 예외를 올리면 이력서 한 건이 통째로 날아간다.
    """
    try:
        verdict = review_editor(job, guide, data)
    except Exception as exc:  # noqa: BLE001
        log.warning("검수 실패(무시하고 진행): %s", exc)
        return []

    if not verdict["fabricated"]:
        if verdict["issues"]:
            log.info("검수 지적 %d건 — 날조는 없어 재작성하지 않는다", len(verdict["issues"]))
        return []

    log.info("검수가 날조 %d건 지적 — 1회 재작성", len(verdict["fabricated"]))
    tasks.check("검수 후 재작성 전")
    hint = "\n".join(f"- {i['text']}" for i in verdict["fabricated"])
    try:
        raw = llm.ask(
            _editor_prompt(job, guide, job_id, feedback, review_notes=hint),
            job_id=job_id, phase="to_editor_json_fix",
        )
        fixed = _parse_json(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("재작성 실패 — 첫 조립을 유지하고 지적만 남긴다: %s", exc)
        return [i["text"] for i in verdict["fabricated"]]

    _strip_reasoning(fixed)
    _drop_manual_fields(fixed)
    _strip_unknown_links(fixed, guide)
    _ensure_summary_length(fixed, job, guide)
    _strip_reasoning(fixed)
    _strip_unknown_links(fixed, guide)
    _normalize_todos(fixed)
    _drop_unknown_skills(fixed, guide)
    data.clear()
    data.update(fixed)

    try:
        again = review_editor(job, guide, data)
    except Exception as exc:  # noqa: BLE001
        log.warning("재검수 실패(무시): %s", exc)
        return []
    if again["fabricated"]:
        log.warning("재작성 뒤에도 날조 지적 %d건 — 사람에게 넘긴다", len(again["fabricated"]))
    return [i["text"] for i in again["fabricated"]]


def build(
    job_id: int,
    *,
    max_rounds: int = 2,
    conn: sqlite3.Connection | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """공고 하나에 대한 이력서를 완성한다.

    반환: {job_id, company, resume, rounds, issues, unverified, path, ok}
    `ok=False`면 자동지원에 쓰면 안 된다 — 사람이 봐야 한다.
    """
    guide = load_guide()
    job = load_job(job_id, conn)

    resume = write(job, guide)
    issues: list[str] = []
    rounds = 1

    for _ in range(max_rounds - 1):
        verdict = review(job, guide, resume)
        issues = verdict["issues"]
        if verdict["ok"]:
            break
        log.info("검수 반려 %d건 — 재작성", len(issues))
        resume = write(job, guide, feedback="\n".join(issues))
        rounds += 1
    else:
        # 마지막 라운드 결과도 검수는 해야 한다. 통과 못 하면 사람에게 넘긴다.
        verdict = review(job, guide, resume)
        issues = verdict["issues"]

    # 제출물과 자기보고를 가른다. 저장되는 파일은 제출물만이다.
    body, gaps = split_report(resume)
    unverified = UNVERIFIED.findall(body)

    path = None
    if save:
        RESUME_OUT_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w가-힣]+", "_", job["company"])[:24]
        path = RESUME_OUT_DIR / f"{job_id}-{safe}.md"
        path.write_text(body, encoding="utf-8")

    # 필수요건인데 근거가 없는 항목이 쌓이면 자동지원하지 않는다.
    #
    # 실측(에프앤에프 Java/Spring, 132점 최고점): Java·Spring 실무, Oracle,
    # ERP 전부 근거 없음이었다. 적합도 점수는 공고에 그 키워드가 있는지만 세고
    # 사용자가 실제로 할 수 있는지와 대조하지 않아 과대평가된 것이다.
    # 어셈블러가 공고를 읽으면서 만든 이 목록이 그 사각지대를 메운다.
    required = [g for g in gaps if g["level"] == "필수"]
    max_gaps = effective_config().get("applicability", {}).get("max_required_gaps", 2)
    overqualified_gap = len(required) > max_gaps

    return {
        "job_id": job_id,
        "company": job["company"],
        "title": job["title"],
        "rounds": rounds,
        "ok": not issues and not unverified and not overqualified_gap,
        "issues": issues,
        "unverified": unverified,
        # 공고가 요구하는데 사실 저장소에 근거가 없는 항목. 제출물엔 없다.
        "gaps": gaps,
        "required_gaps": len(required),
        "blocked_by_gaps": overqualified_gap,
        "chars": len(body),
        "path": str(path) if path else None,
        "resume": body,
    }


# ─────────────────── 원티드 편집기용 구조화 출력 ───────────────────
#
# 원티드 이력서 편집기는 필드가 나뉜 폼이다(이름/연락처/간단소개/경력/학력/
# 스킬/링크). 마크다운 한 덩어리로는 채울 수 없다.
#
# 'AI 활용 경험' 칸은 스키마에 없다. 사람이 사본메이커에 직접 등록해 둔 값이고
# 사본이 그대로 들고 온다 — 생성하면 매번 다른 문장이 50자 칸에 덮어써진다.
#
# 마크다운을 파싱해 쪼개지 않고 처음부터 JSON으로 받는 이유: 파싱은 문장 형태가
# 조금만 바뀌어도 깨지는데, 그 깨짐이 조용하다. 잘못 쪼개진 채로 폼에 들어가면
# 회사명 칸에 직무가 들어간 이력서가 제출된다.

EDITOR_SCHEMA = """{
  "headline": "한 줄 타이틀 (공고에 맞춰 매번 다시 씀)",
  "summary": "간단 소개. 가이드 §2-1의 네 덩어리를 그 순서대로 넣는다. 덩어리 사이에는 빈 줄 하나.\n  ① 첫 줄: 한 문장 요약 (무엇을 하는 개발자인가). 이 공고의 1순위 역량이 이 줄에 들어간다\n  ② '· ' 로 시작하는 핵심역량 불릿 4개. 각 한 줄, 태도가 아니라 역량/경험 단위\n  ③ 가이드 §8의 [대표 프로젝트] 블록 — **공고에 잘 맞는 개인 프로젝트가 있을 때만.** 없으면 이 덩어리는 통째로 없다. 붙인다면 100~300자. '[대표 프로젝트] 이름 — 한 줄 소개' + '· ' 불릿 1~2개(성과 수치 필수) + 마지막 줄에 URL 하나. URL은 가이드 §3.4 표의 값을 그대로 옮긴다 — 조합해 만들지 않는다. 비공개 프로젝트는 링크가 없으므로 이 블록에 쓰지 않는다\n  ④ 가이드 §9 — 공고가 자기소개·지원동기·산출물 링크 등을 따로 요구했을 때만. 요구가 없으면 이 덩어리는 통째로 없다\n  전체 **550~850자**. 원티드가 400자 미만이면 이력서 완성도를 깎으므로 반드시 넘긴다. 줄바꿈(\\n)을 실제로 넣는다 — 한 문단으로 붙이지 않는다",
  "experiences": [
    {"company": "회사명", "job_role": "직무", "business_title": "직책",
     "start": "YYYY.MM", "end": "YYYY.MM",
     "achievements": [
       {"title": "주요 성과 제목",
        "start": "YYYY.MM", "end": "YYYY.MM",
        "detail": "· 로 시작하는 불릿 2~4개. 마지막 줄은 '· 사용기술: A, B, C'"}
     ]}
  ],
  "_note_achievement_period": "achievements의 start/end는 그 프로젝트를 수행한 기간이며 반드시 소속 경력의 재직기간 안에 들어가야 한다. 가이드 §3에 기간이 적힌 항목은 그 값을 그대로 쓴다.",
  "_note_experience_scope": "experiences에는 **그 회사에서 한 일만** 쓴다. 개인 프로젝트를 성과로 넣거나, 회사에서 한 일의 근거·산출물로 개인 저장소를 가리키지 않는다 — 회사 코드가 아니다. 개인 프로젝트는 summary의 [대표 프로젝트] 블록에서만 다룬다.",
  "educations": [
    {"school": "학교명", "major": "전공 및 학위", "start": "YYYY.MM", "end": "YYYY.MM",
     "detail": "이수 과목 또는 연구 내용"}
  ],
  "languages": [{"name": "영어", "level": "고급 비즈니스 레벨"}],
  "_note_language": "level은 원티드 선택지 문구를 그대로 써야 한다: 유창함 | 고급 비즈니스 레벨 | 비즈니스 레벨 | 일상 회화. 다른 표현을 쓰면 선택지를 못 찾아 비워둔 채 지나간다. 가이드에 '영어 유창'만 있으면 '고급 비즈니스 레벨'로 적는다.",
  "skills": ["스킬", "나열"],
  "links": [{"name": "GitHub", "url": "https://..."}],
  "gaps": [{"level": "필수|우대", "text": "공고 요건 중 근거가 없는 항목"}],
  "manual_todos": ["공고가 요구하는데 이력서 본문으로는 충족할 수 없는 것만. 첨부·업로드·별도 송부·사전 응시처럼 사람이 직접 해야 하는 것이다(성적증명서·졸업증명서 첨부, 자격증 사본, 이메일 별도 송부, 회사 자체 양식 작성 등). 각 항목은 60자 이내 한 줄. 글로 쓸 수 있는 요구는 여기가 아니라 summary ④블록에 넣되, **§3 사실 저장소에 없는 경험을 요구하는 서술(제품 사용 후기·개선 제안·방문기 등)은 짧아도 여기에 넣는다** — 지어내면 허위기재다. **포트폴리오 첨부는 넣지 않는다** — 파이프라인이 공고에 맞는 파일을 골라 지원 폼에서 자동으로 첨부한다. 없으면 빈 배열"]
}"""


# 개발 트랙. 이 밖의 트랙은 §7-1 재해석 규칙을 강제한다.
DEV_TRACKS = {"dev"}


def _track_block(job: dict[str, Any]) -> str:
    """비개발 공고면 재해석을 **프롬프트에서** 못 박는다.

    가이드에 §7-1을 써두는 것만으로는 안 지켜졌다. 실측: 영업 공고(biz)에
    낙관적 락·RabbitMQ·NestJS가 그대로 들어갔고, 스킬 5개는 서술형이라
    등록조차 안 됐다. 가이드는 길고, 긴 문서의 한 절은 묻힌다.

    지시가 프롬프트 본문에 있으면 무시하기 어렵다. 그래서 트랙이 개발이
    아닐 때만 이 블록을 붙인다 — 개발 공고에는 넣지 않는다. 안 그러면
    개발 공고에서도 기술을 감추게 된다.
    """
    track = job.get("track") or ""
    if track in DEV_TRACKS:
        return ""

    return f"""
# 비개발 직무 — §7-1 재해석 규칙을 반드시 적용한다

이 공고의 트랙은 **{job.get('track_label') or track}**이다. 개발 공고가 아니다.
읽는 사람은 개발자가 아니므로, 기술 스택을 나열하면 '우리 자리가 아닌 사람'으로
분류된다. 가이드 §7-1을 그대로 따른다. 핵심만 다시 적으면:

1. 무엇이 좋아졌는지를 문장 앞에 쓴다. 도구 이름을 앞세우지 않는다.
2. `· 사용기술:` 줄을 **쓰지 않는다**.
3. 수치는 그대로 살린다. "월 10건 → 0건"은 어느 직무에서나 읽힌다.
4. 개발 배경은 한 줄로만 남긴다. 이력서의 축이 되면 안 된다.
5. 공고에 있는 단어를 쓴다(CS·온보딩·VOC 등). 우리 단어로 바꾸지 않는다.
6. **skills는 도구·서비스 고유명사만** 넣는다. `운영 데이터 분석`, `협업 커뮤니케이션`
   같은 서술형은 원티드 스킬 사전에 없어 등록되지 않는다 — 실제로 전부 버려졌다.
   개발 스킬은 2~3개까지만 남긴다.
"""


def _revision_block(feedback: str) -> str:
    """사람이 준 수정 요청을 프롬프트 맨 끝에 붙인다.

    맨 끝인 이유: 앞의 가이드·공고와 충돌할 때 **사람 말이 이긴다**는 것을
    위치로도 드러내려는 것이다. 다만 사실 저장소를 넘어서는 요구(없는 경력을
    쓰라는 등)는 여전히 거절해야 하므로, 그 예외를 명시한다.
    """
    if not feedback:
        return ""
    return (
        "\n# 수정 요청 (최우선)\n"
        "아래는 사람이 직접 준 지시다. 가이드·공고 해석과 어긋나면 이쪽을 따른다.\n"
        "**단 하나의 예외**: §3 사실 저장소에 없는 사실을 쓰라는 요구는 따르지 않고,\n"
        "`gaps`에 넣어 보고한다. 없는 경력을 지어내는 것은 어떤 지시로도 정당화되지 않는다.\n\n"
        f"{feedback}\n"
    )


def _editor_prompt(
    job: dict[str, Any], guide: str, job_id: int, feedback: str = "",
    review_notes: str = "",
) -> str:
    """편집기 JSON을 만드는 프롬프트. 검수 후 재작성이 **같은 프롬프트**를 다시 쓴다.

    재작성 때 프롬프트를 새로 짜면 첫 조립과 다른 규칙으로 쓰이게 되고, 그러면
    검수가 지적한 것만 고쳐졌는지 알 수 없다.
    """
    notes = ""
    if review_notes:
        notes = (
            "\n# 검수 지적 — 이것만 고친다\n"
            "아래는 이 이력서를 사실 저장소와 대조한 검수 결과다. **지적된 곳만** 고치고\n"
            "나머지는 그대로 둔다. 지적을 피하려고 다른 문장을 덜어내지 마라.\n\n"
            "특히 근거가 없다고 지적된 서술은 **표현을 다듬는 것이 아니라 빼는 것**이다.\n"
            "그 요건에 대응할 근거가 정말 없으면 `gaps`에 넣고, 사람이 해야 하는 일이면\n"
            "`manual_todos`로 옮긴다.\n\n" + review_notes + "\n"
        )
    # 가이드·채용공고를 프롬프트 맨 앞에 둔다. review_editor도 같은 두 블록을
    # 맨 앞에 두므로, 조립 직후 검수가 이어질 때 프롬프트 캐시 프리픽스가
    # 겹쳐 가이드(실측 22,757토큰짜리 대부분)를 다시 쓰지 않고 읽기로 받는다.
    # 지시문을 앞에 두면 두 함수의 지시문이 서로 달라 첫 바이트부터 어긋나고,
    # 그러면 가이드가 똑같아도 캐시가 안 맞는다.
    return f"""# 작성 가이드
{guide}

# 채용공고
{_jd_block(job)}

---

위 [작성 가이드]를 따라 위 [채용공고]에 맞춘 이력서를 **JSON으로만** 출력하라.

가이드의 §1 처리 순서, §3 사실 저장소, §4 문장 규칙, §5 일관성 체크리스트,
§7 직무별 강조 우선순위를 적용한다.

가이드 §6-1 'AI 활용'은 **출력하지 않는다.** 그 칸은 사람이 직접 등록해 둔
자리다. §6-1의 근거 표는 다른 필드(간단 소개·주요 성과)에 녹이는 용도로만 쓴다.

**순서는 직무명이 아니라 [채용공고]의 자격요건이 정한다(§7-0).** 먼저 자격요건·
우대사항·주요업무·기술스택에 나온 기술/역량을 전부 나열하고, 각각에 §3 사실 저장소의
근거를 붙인 뒤, **필수요건을 많이 덮는 경험부터** 앞에 놓는다. §7-4 기본값 표는 공고에
요건이 거의 안 적혀 있을 때만 본다.

직무명이 '백엔드'여도 자격요건에 AWS·Docker·CI/CD가 있으면 그건 백엔드 기본값 행이
아니다 — 인프라 경험((D) 배포 자동화 / (E) AWS 마이그레이션)을 1~2순위로 끌어올린다.
자격요건에 적혀 있는데 §3에 근거가 있는 항목이 이력서에 안 나타나면, 그건 그 공고의
절반을 버린 것이다.

**한 분야로 초점을 맞춘다.** 위 정렬의 1순위를 축으로 삼고 2·3순위는 보조로만 쓴다.
요건이 셋 이상 갈리면(백엔드+인프라+AI) 필수요건을 가장 많이 덮는 것 하나를 축으로
잡고 나머지는 그 경험의 불릿 안에 녹인다. 세 축을 나란히 세우지 않는다.

이 공고와 **매우 무관한 경험은 뺀다.** 애매하면 넣는다.

**대표 프로젝트 블록(§8)은 공고에 잘 맞는 개인 프로젝트가 있을 때만 넣는다.** 지원 시
포트폴리오 PDF가 함께 첨부되므로 개인 프로젝트는 거기서 이미 보인다 — 억지로 갖다 붙이면
공고와 상관없는 저장소가 간단 소개 한복판을 차지한다. 애매하면 넣지 않는다.
넣는다면 URL은 §3.4 표의 값을 **글자 그대로** 옮기고, 비공개 프로젝트는 링크가 없으므로
이 블록에 쓰지 않는다.

**공고가 이 가이드에 없는 제출 요구를 했으면 §9대로 가른다.** 자기소개·지원동기·
산출물 링크·블로그처럼 글로 쓸 수 있는 것은 summary ④블록에 넣고, 증명서 첨부·
별도 송부·사전과제처럼 사람이 해야 하는 것은 `manual_todos`에 적는다.
**가르는 기준은 분량이 아니라 근거다**: 그 문장을 쓰려면 §3에 없는 경험이 필요하면
(제품 사용 후기, 서비스 개선 제안, 방문기 등) 짧아도 `manual_todos`다 — 써본 적 없는
제품의 후기를 지어내는 것은 허위기재이고, 실제로 그렇게 나갈 뻔했다. 첨부하지 않은 것을
"첨부하였습니다"라고 쓰지 않는다. **포트폴리오 첨부는 `manual_todos`에 넣지 않는다** —
파이프라인이 공고에 맞는 포트폴리오를 골라 지원 폼에서 자동으로 첨부한다.

**사고 과정을 출력하지 않는다.** "공고는 ~이므로 §7 조합으로 구성했다" 같은 문장은
이력서 내용이 아니다. 실측에서 그런 문장이 간단 소개 맨 앞에 그대로 들어갔다.
어느 필드에도 판단 근거·구성 의도를 적지 않는다.

**문장 규칙(§4)은 모든 필드에 예외 없이 적용한다.** 특히:
- 명사형 종결로 통일한다. `~했습니다 / ~있습니다` 를 쓰지 않는다.
  ✅ `동시 충돌 리포트 월 10건 → 0건으로 감소`
  ❌ `충돌을 0건으로 줄인 경험이 있습니다`
- 주어를 생략한다. "저는" 을 쓰지 않는다.
- 수치는 개선 전/후를 함께 쓴다.
- 한 불릿에 한 가지만 담는다.

절대 규칙:
- §3 사실 저장소에 없는 수치·기술·프로젝트를 만들지 않는다.
- 공고가 요구하는데 근거가 없는 항목은 `gaps`에 넣는다. 지어내지 않는다.
- JSON 외에 어떤 설명도 출력하지 않는다. 코드펜스도 붙이지 않는다.

스키마:
{EDITOR_SCHEMA}
{load_revision_log(job_id)}{_track_block(job)}{_revision_block(feedback)}{notes}"""


def build_editor_json(
    job_id: int,
    *,
    conn: sqlite3.Connection | None = None,
    save: bool = True,
    max_age_hours: float = 72,
    feedback: str = "",
) -> dict[str, Any]:
    """원티드 편집기 폼에 그대로 넣을 수 있는 형태로 조립한다.

    max_age_hours 안에 만든 결과가 있으면 재사용한다. 사이클이 반복될 때마다
    같은 공고에 LLM을 다시 부르면 시간(건당 40초)과 비용이 그대로 곱해진다.
    공고 본문과 이력서 가이드는 하루 사이에 잘 바뀌지 않는다.
    """
    # 사람이 고쳐 달라고 했으면 캐시를 쓰면 안 된다. 캐시는 "같은 입력이면
    # 같은 결과"라는 전제 위에 서 있는데, 피드백은 입력이 바뀐 것이다.
    if not feedback:
        cached = _load_cached(job_id, max_age_hours, conn)
        if cached is not None:
            log.info("공고 %s — 최근 조립 결과 재사용 (LLM 호출 0회)", job_id)
            return cached

    guide = load_guide()
    job = load_job(job_id, conn)

    prompt = _editor_prompt(job, guide, job_id, feedback)

    raw = llm.ask(prompt, job_id=job_id, phase="to_editor_json")
    data = _parse_json(raw)
    _strip_reasoning(data)
    _drop_manual_fields(data)
    # 보강도 LLM 호출이다(llm.timeout_sec 기본 900초). 여기서 한 번 더 보면
    # /stop이 최대 15분을 덜 기다린다. 아직 아무것도 저장하지 않았으므로
    # 접어도 남는 것이 없다.
    # 지어낸 URL을 **보강 전에** 걷어낸다. 순서가 반대면, 링크 줄이 지워지면서
    # 글자수가 최소치 아래로 떨어져도 다시 받을 기회가 없다. 먼저 지우면
    # 보강이 "링크 없음"을 보고 §3.4 표의 진짜 주소로 다시 채운다.
    _strip_unknown_links(data, guide)
    tasks.check("이력서 조립 중")
    _ensure_summary_length(data, job, guide)
    _strip_reasoning(data)  # 보강 응답에도 섞일 수 있다
    _strip_unknown_links(data, guide)  # 보강도 지어낼 수 있다. 저장 전에 한 번 더
    todos = _normalize_todos(data)

    # ── 스킬: 코드가 대조하고 코드가 지운다 ──
    # 오탐이 0이라(실측 4/4) 사람에게 물을 것이 없다. 지적으로 남기지 않고 지운다 —
    # 원티드 스킬 사전에 없는 단어는 어차피 등록도 안 된다.
    notes: list[str] = []
    for sk in _drop_unknown_skills(data, guide):
        notes.append(f"가이드에 없어 제거한 스킬: {sk}")

    # ── 산문: LLM 검수 1회 + 날조가 남으면 재작성 1회 ──
    # **차단하지 않는다.** 실측 A/B에서 두 모델 다 4건에 1건씩 잘못된 날조를 달았고
    # (Haiku는 §5 확정값을, Sonnet은 더 겸손하게 쓴 표현을), 그걸 차단에 쓰면 멀쩡한
    # 이력서가 선다. 되돌릴 수 없는 지점(제출) 앞에는 이미 사람이 있으므로,
    # 판단이 갈리는 지적은 그 사람에게 보낸다(승인 캡션의 ⚠️).
    fix_notes = _review_and_fix(data, job, guide, job_id, feedback)
    notes.extend(fix_notes)

    gaps = data.get("gaps") or []
    required = [g for g in gaps if g.get("level") == "필수"]
    cfg = effective_config()
    max_gaps = cfg.get("applicability", {}).get("max_required_gaps", 2)

    # 이력서와 별개로, 공고에 맞는 포트폴리오를 고른다. 실패해도(LLM 오류 등)
    # 조립 자체를 막을 이유는 없다 — 포트폴리오는 있으면 붙이는 부가물이지
    # required_gaps처럼 지원 여부를 가르는 조건이 아니다.
    try:
        portfolio_title = portfolio_match.match(job, cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("포트폴리오 매칭 실패(무시): %s", exc)
        portfolio_title = None

    path = None
    if save:
        RESUME_OUT_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w가-힣]+", "_", job["company"])[:24]
        path = RESUME_OUT_DIR / f"{job_id}-{safe}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    _save_build(
        job_id, len(required) <= max_gaps, required, gaps, conn,
        track=job.get("track"), headline=data.get("headline"),
        portfolio_title=portfolio_title, manual_todos=todos, review_notes=notes,
    )

    return {
        "job_id": job_id,
        "company": job["company"],
        "title": job["title"],
        "ok": len(required) <= max_gaps,
        "required_gaps": len(required),
        "gaps": gaps,
        "manual_todos": todos,
        "review_notes": notes,
        "path": str(path) if path else None,
        "portfolio_title": portfolio_title,
        "data": data,
    }


# 모델이 이력서 내용 대신 판단 근거를 적을 때 쓰는 표현들.
# 실측: 간단 소개 첫 문단이 "공고는 ... §7 \"풀스택/DevOps\" 조합으로 구성."이었다.
REASONING = re.compile(
    r"(공고는|채용공고는|이 공고)[^\n]*(구성|매칭|선택|판단|배치)[^\n]*[.。]"
    r"|§\s*\d"
    r"|가이드\s*§"
    r"|(우선순위|사실 저장소)[^\n]*(따라|기준으로)[^\n]*[.。]"
)

# 사람이 사본메이커에 직접 등록한 필드. 조립 결과에 있으면 안 된다 —
# 남아 있으면 편집기 채우기가 그 값을 덮어쓸 통로가 생긴다.
MANUAL_FIELDS = ("ai_usage",)

# 간단 소개 길이. 네 덩어리(요약·핵심역량·대표 프로젝트·공고별 요구)가 들어가므로
# 예전 450자로는 대표 프로젝트 블록(100~300자)이 들어갈 자리가 없다.
SUMMARY_MIN = 550
SUMMARY_MAX = 850
# 보강 결과를 받아들이는 상한. 목표(SUMMARY_MAX)보다 넉넉히 둔다 — 조금 넘겼다고
# 되돌리면 짧은 원문이 그대로 남아 완성도가 깎인다.
SUMMARY_HARD_MAX = 1000

# 간단 소개 안의 블록 머리. 판단 근거로 오인해 지우면 안 되는 자리다 —
# 지원동기는 "이 공고의 ...를 하고 싶다"처럼 REASONING 패턴과 겹치는 문장이 정상이다.
SUMMARY_BLOCK_HEAD = ("[", "·")

# 다만 **가이드를 입에 올리는 순간 예외가 아니다.** 실측(공고 142): 모델이
#   `[대표 프로젝트] 없이, 링크는 §3.4 표에 없는 조합 링크를 만들 수 없어 …`
# 라고 써서, `[`로 시작한다는 이유만으로 면제를 받아 간단 소개 맨 앞에 그대로 남았다.
# 이력서 본문에 `§`나 '가이드'가 나올 일은 없다 — 그건 무조건 사고 과정이다.
GUIDE_TALK = re.compile(r"§|가이드|사실 저장소|이 공고는[^\n]*근거가 없")

# 모델이 이력서 대신 **자기 작업 노트**를 적을 때 쓰는 표현. 실측 3건(공고 142/266/310):
#   "595자로 범위 내. 최종 출력."
#   "…경험을 이 공고 자격요건에 맞춰 구체화한 버전."
#   "…운영 경험을 불릿에 구체적으로 녹여 작성."
# REASONING은 "(이 공고)…(구성|매칭|…)" 꼴만 잡아서 이런 문장을 통과시켰다.
# 공통점은 **글자수·출력·작성 행위 자체를 말한다**는 것이다 — 이력서 문장은 그러지 않는다.
WORK_NOTE = re.compile(
    r"\d+\s*자[^\n]{0,12}(범위|이내|내외|맞춤|채움|채웁|맞췄)"
    r"|최종\s*출력"
    r"|맞춰\s*(구체화|작성)한?\s*(버전|안)?"
    r"|불릿에[^\n]{0,12}(녹여|담아)\s*작성"
    r"|^-{3,}$",
    re.MULTILINE,
)

URL_RE = re.compile(r"https?://[^\s)\]>,]+")


def _strip_reasoning(data: dict[str, Any]) -> None:
    """판단 근거 문장을 걷어낸다.

    프롬프트로 "사고 과정을 쓰지 마라"고 해도 새어 나온다. 그게 이력서 맨 앞에
    들어가면 읽는 사람에게 곧바로 보인다 — 프롬프트에 의존하지 않고 코드로 막는다.

    문단 단위로 검사한다. 근거 문장은 보통 첫 문단에 통째로 오고, 본문 불릿에는
    섞이지 않는다.

    `[대표 프로젝트]`·`[지원동기]` 블록은 건드리지 않는다. 지원동기는 공고를 근거로
    쓰는 것이 정상이라 REASONING 패턴("이 공고의 …를 …")과 겹치는데, 그걸 판단
    근거로 오인해 지우면 공고가 요구한 항목이 통째로 사라진다.

    **단, 가이드를 입에 올리는 문단은 그 면제를 못 받는다**(`GUIDE_TALK`). 머리표만
    보고 면제하면 "[대표 프로젝트] 없이, §3.4 표에 없어서…" 같은 자기설명이 그대로
    통과한다 — 실제로 그렇게 새어 나갔다.
    """
    for key in ("summary",):
        text = data.get(key)
        if not isinstance(text, str) or not text.strip():
            continue
        blocks = [b for b in text.split("\n\n")]
        kept = [
            b for b in blocks
            if not (
                GUIDE_TALK.search(b)
                # 작업 노트는 불릿을 달지 않는다. 불릿 문단은 본문이므로 건드리지 않는다.
                or (WORK_NOTE.search(b) and not b.lstrip().startswith("·"))
                or (REASONING.search(b) and not b.strip().startswith(SUMMARY_BLOCK_HEAD))
            )
        ]
        if len(kept) != len(blocks):
            log.info("%s에서 판단 근거 문단 %d개 제거", key, len(blocks) - len(kept))
        data[key] = "\n\n".join(kept).strip()


def _strip_unknown_links(data: dict[str, Any], guide: str) -> None:
    """가이드에 없는 URL을 간단 소개에서 걷어낸다.

    §8이 요구하는 대표 프로젝트 링크는 **사실 저장소에 적힌 값 그대로**여야 한다.
    모델은 저장소 이름을 보고 프리픽스를 붙여 URL을 만들어내는데, 그렇게 만든 주소는
    맞을 때도 있고 404일 때도 있다. 사용자 이름으로 나가는 이력서에 죽은 링크가
    실리면 확인할 방법이 없다 — 없는 링크가 틀린 링크보다 낫다.

    지우는 단위는 **그 줄에 내용이 남는지**로 가른다. `· 소스코드: <주소>` 처럼
    주소가 곧 그 줄의 전부면 줄째 지운다 — URL만 빼면 꼬리표만 남는다. 반대로
    문장 안에 주소가 섞여 있으면 주소만 뺀다. 줄째 지우는 규칙 하나로 밀면,
    모델이 간단 소개를 한 줄로 내놓았을 때 **본문 전체가 사라진다.**
    """
    text = data.get("summary")
    if not isinstance(text, str) or "http" not in text:
        return

    dropped: list[str] = []
    kept_lines: list[str] = []
    for line in text.splitlines():
        bad = [u for u in URL_RE.findall(line) if u.rstrip("/.,)") not in guide]
        if not bad:
            kept_lines.append(line)
            continue
        dropped.extend(bad)
        rest = line
        for u in bad:
            rest = rest.replace(u, "")
        # 주소를 빼고 남는 것이 꼬리표뿐이면 줄을 버린다.
        if len(rest.strip(" -·•:,.\t")) >= 20:
            kept_lines.append(rest.rstrip())

    if not dropped:
        return
    log.warning("가이드에 없는 URL %d개를 간단 소개에서 제거: %s", len(dropped), dropped[:3])
    data["summary"] = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()


# 파이프라인이 이미 하는 일. 사람 할 일 목록에 오르면 안 된다 —
# 포트폴리오는 `portfolio_match`가 공고를 읽고 골라 지원 폼에서 자동으로 첨부한다.
# 할 일 없는 안내가 매번 뜨면 그 목록 전체가 무시되고, 진짜 할 일까지 묻힌다.
AUTOMATED_TODO = re.compile(r"포트폴리오|portfolio", re.IGNORECASE)


def _normalize_todos(data: dict[str, Any]) -> list[str]:
    """`manual_todos`를 문자열 목록으로 고른다.

    모델이 `{"item": ...}` 꼴로 주기도 해서 한 번 편다. 폰 승인 캡션은 1024자
    제한이라 여기서 개수·길이를 자른다 — 보낼 때 자르면 무엇이 잘렸는지 안 남는다.
    """
    raw = data.get("manual_todos") or []
    if isinstance(raw, str):
        raw = [raw]
    todos: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            item = item.get("text") or item.get("item") or ""
        text = " ".join(str(item).split()).strip("-·• ")
        # 스키마의 설명문을 그대로 베껴 오는 경우가 있다. 그건 할 일이 아니다.
        if not text or "빈 배열" in text or len(text) > 120:
            continue
        if AUTOMATED_TODO.search(text):
            log.info("포트폴리오는 파이프라인이 자동 첨부 — 할 일에서 제외: %s", text[:40])
            continue
        todos.append(text[:80])
    data["manual_todos"] = todos[:5]
    return data["manual_todos"]


def todo_block(todos: list[str]) -> str:
    """폰 승인 캡션에 붙일 '사람이 직접 할 일' 블록. 없으면 빈 문자열.

    이력서 본문으로는 충족할 수 없는 요구(증명서 첨부 등)를 여기서만 알린다.
    본문에 적으면 하지 않은 일을 했다고 쓰는 것이 된다.
    """
    if not todos:
        return ""
    lines = "\n".join(f"• {t[:60]}" for t in todos[:3])
    more = f"\n<i>외 {len(todos) - 3}건</i>" if len(todos) > 3 else ""
    return f"\n\n🖐 <b>이력서로는 안 되는 요구</b>\n{lines}{more}"


def _drop_manual_fields(data: dict[str, Any]) -> None:
    """사람이 직접 등록한 필드를 조립 결과에서 지운다.

    스키마에서 뺐어도 모델은 가이드 §6-1을 읽고 `ai_usage`를 되살려 넣는다.
    프롬프트에 의존하지 않고 코드로 막는다 — 값이 남아 있으면 편집기가 채울
    통로가 열리고, 그 칸(50자)은 사람이 문장을 확정해 둔 자리다.
    """
    for key in MANUAL_FIELDS:
        if key in data:
            log.info("%s는 사람이 등록하는 필드 — 조립 결과에서 제거", key)
            data.pop(key, None)


def _ensure_summary_length(data: dict[str, Any], job: dict[str, Any], guide: str) -> None:
    """간단 소개가 짧으면 **그 필드만** 다시 받는다.

    원티드는 400자 미만이면 이력서 완성도를 깎는다. 그런데 프롬프트로 글자수를
    지시해도 잘 안 지켜진다(실측: 450자를 요구했는데 314자). 지시를 더 세게 쓰는
    대신 코드가 재고 모자라면 다시 받는다 — 이 프로젝트의 다른 검증들과 같은
    방식이다.

    전체를 다시 만들지 않는 이유: 출력 토큰에 시간이 선형이라 전체 재생성은
    비싸다. v1이 자소서 한 건에 98초 걸린 원인이 그 통짜 재생성 루프였다.
    """
    summary = (data.get("summary") or "").strip()
    if len(summary) >= SUMMARY_MIN:
        return

    log.info("간단 소개가 %d자 — %d자 이상으로 보강 요청", len(summary), SUMMARY_MIN)
    out = llm.ask(
        f"""아래 [현재 간단 소개]를 {SUMMARY_MIN}~{SUMMARY_MAX}자로 고쳐라.

구조 — 덩어리 사이에 빈 줄 하나:
① 첫 줄: 한 문장 요약
② `· ` 로 시작하는 핵심역량 불릿 4개
③④ `[...]` 로 시작하는 블록이 이미 있으면 그대로 둔다. **없으면 만들지 않는다** —
   특히 [대표 프로젝트]를 새로 지어 붙이지 마라. 없는 건 없는 게 맞다

규칙:
- 불릿 개수를 늘리지 말고 **각 불릿의 내용을 구체화**한다
  (맡은 범위, 사용 기술, 결과 수치).
- ③의 URL은 [사실 저장소] §3.4 표에 **적힌 그대로** 옮긴다. 저장소 이름에
  프리픽스를 붙여 만들지 마라 — 없는 주소가 만들어진다. 표에 `비공개`라고 적힌
  프로젝트는 링크가 없으므로 ③에 쓰지 않는다.
- [사실 저장소]에 없는 내용을 만들지 않는다.
- 명사형 종결. `~습니다` 금지. 주어 생략.
- 결과 텍스트만 출력한다. 설명·코드펜스 금지.

# 현재 간단 소개
{summary}

# 사실 저장소
{guide}

# 채용공고
{_jd_block(job)}""",
        job_id=job.get("id"), phase="summary_ensure",
    ).strip()

    out = _restore_blocks(summary, out)
    if SUMMARY_MIN <= len(out) <= SUMMARY_HARD_MAX:
        data["summary"] = out
    else:
        log.warning("보강 결과가 %d자 — 원문을 유지한다", len(out))


def _restore_blocks(old: str, new: str) -> str:
    """보강이 통째로 날린 `[...]` 블록을 되붙인다.

    보강은 간단 소개 **전체**를 새로 받는다. 그래서 ④블록(§9, 공고가 따로 요구한
    자기소개·지원동기)이 조용히 사라질 수 있는데, 그건 공고가 명시적으로 요구한
    항목이라 없어지면 서류 검토도 못 받는다. "그대로 둬라"는 지시로는 부족하다 —
    이 저장소가 프롬프트 대신 코드로 막아온 것들과 같은 부류다.

    머리표(`[지원동기]` 등)로 대조한다. 내용은 바뀌어도 되지만 블록 자체가
    없어지면 안 된다.
    """
    missing = []
    for block in old.split("\n\n"):
        head = block.strip().split("\n", 1)[0].strip()
        if not head.startswith("[") or "]" not in head:
            continue
        marker = head[: head.index("]") + 1]
        if marker not in new:
            missing.append(block.strip())
    if not missing:
        return new
    log.info("보강이 지운 블록 %d개를 되붙인다: %s",
             len(missing), [m.split("\n", 1)[0][:20] for m in missing])
    return (new.rstrip() + "\n\n" + "\n\n".join(missing)).strip()


def _parse_json(raw: str) -> dict[str, Any]:
    """코드펜스가 붙어 오는 경우가 있어 한 번 벗겨본다. 그 이상은 고치지 않는다 —
    형식을 못 지킨 응답을 억지로 살리면 무엇이 잘못됐는지 안 보이게 된다."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON을 찾지 못했다: {text[:200]}")

    # strict=False — 문자열 안의 날것 개행을 허용한다.
    # 이력서 본문은 여러 줄이라 모델이 \n 대신 실제 개행을 넣는 일이 잦고,
    # 엄격 모드는 거기서 통째로 실패한다. 내용은 멀쩡한데 파싱만 못 하는 것이라
    # 재생성(40초)을 시키느니 받아들이는 쪽이 맞다.
    return json.loads(text[start : end + 1], strict=False)


def _save_build(
    job_id: int,
    ok: bool,
    required: list,
    gaps: list,
    conn: sqlite3.Connection | None,
    *,
    track: str | None = None,
    headline: str | None = None,
    portfolio_title: str | None = None,
    manual_todos: list[str] | None = None,
    review_notes: list[str] | None = None,
) -> None:
    """조립 결과를 남긴다.

    두 가지로 쓰인다:
    1. 판정이 `REQUIREMENT_GAP` blocker를 세울 때 (required_gaps)
    2. 지원 단계가 **어느 이력서를 낼지 정할 때** (resume_title)

    2번이 중요하다. 예전에는 편집기 화면에서 제목을 읽어 넘겼는데, 그 제목이
    있는 자리의 문구가 상태에 따라 바뀌어("기본 이력서 설정" → "기본 이력서")
    조립과 지원을 잇는 고리가 통째로 끊겼다. 기록해두면 화면에 의존하지 않는다.

    portfolio_title도 같은 이유로 여기 둔다 — 지원 단계가 이력서 제목과
    같은 자리에서 읽어 레시피에 넘긴다.

    manual_todos(이력서로는 충족 못 하는 공고 요구)도 여기 둔다. 승인 알림은
    조립과 **다른 프로세스**에서 만들어지므로(cycle-apply → 폰), 메모리로는 못
    넘긴다. 캐시된 조립을 재사용해도 같은 안내가 나가야 한다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            """INSERT INTO resume_builds
                 (job_id, ok, required_gaps, gaps, track, headline, portfolio_title,
                  manual_todos, review_notes, built_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 ok=excluded.ok, required_gaps=excluded.required_gaps,
                 gaps=excluded.gaps, track=excluded.track,
                 headline=excluded.headline, portfolio_title=excluded.portfolio_title,
                 manual_todos=excluded.manual_todos,
                 review_notes=excluded.review_notes,
                 built_at=excluded.built_at""",
            (job_id, 1 if ok else 0, len(required),
             json.dumps(gaps, ensure_ascii=False), track, headline, portfolio_title,
             json.dumps(manual_todos or [], ensure_ascii=False),
             json.dumps(review_notes or [], ensure_ascii=False), now()),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def _load_cached(
    job_id: int, max_age_hours: float, conn: sqlite3.Connection | None
) -> dict[str, Any] | None:
    """최근 조립 결과가 있으면 그대로 돌려준다. 없으면 None."""
    from datetime import datetime, timezone

    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT ok, required_gaps, gaps, portfolio_title, manual_todos, review_notes, built_at "
            "FROM resume_builds WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        # built_at은 보통 오프셋을 갖지만, 손으로 넣은 행에는 없을 수 있다.
        # 없는 값을 UTC로 가정하면 최대 9시간 오차가 나므로 로컬로 본다.
        stamp = datetime.fromisoformat(row["built_at"])
        if stamp.tzinfo is None:
            stamp = stamp.astimezone()
        age = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600
        if age > max_age_hours:
            return None

        job = load_job(job_id, conn)
        safe = re.sub(r"[^\w가-힣]+", "_", job["company"])[:24]
        path = RESUME_OUT_DIR / f"{job_id}-{safe}.json"
        if not path.exists():
            return None

        return {
            "job_id": job_id,
            "company": job["company"],
            "title": job["title"],
            "ok": bool(row["ok"]),
            "required_gaps": row["required_gaps"],
            "gaps": json.loads(row["gaps"] or "[]"),
            "manual_todos": json.loads(row["manual_todos"] or "[]"),
            "review_notes": json.loads(row["review_notes"] or "[]"),
            "path": str(path),
            "portfolio_title": row["portfolio_title"],
            "cached": True,
            "data": json.loads(path.read_text(encoding="utf-8")),
        }
    finally:
        if own:
            conn.close()


# ─────────────────── 스크린샷 비전 점검 ───────────────────
#
# 판단 자체는 vision 모듈이 한다. 여기서는 "이 이력서에서 무엇을 확인할 것인가"만
# 만든다 — 같은 도구를 지원 준비·레시피 수복·오케스트레이터도 쓴다.


def verify_screenshot(shot: str, data: dict[str, Any]) -> dict[str, Any]:
    """조립한 이력서가 편집기 화면에 제대로 반영됐는지 본다."""
    from . import vision

    exps = data.get("experiences") or []
    ach = (exps[0].get("achievements") or [{}])[0] if exps else {}
    intent = "\n".join(
        x for x in [
            "이름: 박예일",
            f"간단 소개 첫 줄: {(data.get('summary') or '').splitlines()[0][:60]}"
            if data.get("summary") else "",
            f"회사: {exps[0].get('company')}" if exps else "",
            f"주요 성과: {ach.get('title')}" if ach.get("title") else "",
            "학력: " + ", ".join(e.get("school", "") for e in (data.get("educations") or [])),
            "스킬: " + ", ".join((data.get("skills") or [])[:8]),
            "링크: " + ", ".join((l or {}).get("name", "") for l in (data.get("links") or [])),
            "언어: " + ", ".join(
                f"{(l or {}).get('name')} {(l or {}).get('level')}"
                for l in (data.get("languages") or [])
            ),
        ] if x
    )
    return vision.verify(shot, intent, context="채용 플랫폼의 이력서 편집 화면")


# 지원준비 한 건이 지나는 단계. 순서가 곧 진행도다 — 뒤엣것에 도달했으면
# 앞엣것은 다 끝난 것이다.
STAGES = ("assembled", "filling", "registered", "prepared")


def set_stage(
    job_id: int, stage: str, *, error: str = "", conn: sqlite3.Connection | None = None
) -> None:
    """이 공고가 어디까지 갔는지 적는다.

    **단계를 마친 직후에** 적는다. 시작할 때 적으면 "끝났다"와 "하다 죽었다"가
    같은 값으로 남아, 다음 실행이 안 끝난 일을 끝난 것으로 읽는다. 유일한
    예외가 `filling`인데, 그건 일부러 **시작할 때** 적는다 — 채우다 끊긴 이력서를
    다음 실행이 재사용하면 절반짜리가 그대로 나가기 때문이다(아래 progress 참고).
    """
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            "UPDATE resume_builds SET stage=?, stage_at=?, stage_error=? WHERE job_id=?",
            (stage, now(), error[:500], job_id),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def note_failure(job_id: int, error: str, conn: sqlite3.Connection | None = None) -> None:
    """왜 여기서 멈췄는지만 적는다. **단계는 안 옮긴다.**

    실패했다고 stage를 뒤로 되돌리거나 앞으로 밀면 안 된다 — 다음 실행이
    "어디까지 실제로 끝냈나"를 그 값으로 판단하기 때문이다. 실패는 진행이
    아니라 진행에 붙는 메모다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            "UPDATE resume_builds SET stage_at=?, stage_error=? WHERE job_id=?",
            (now(), (error or "")[:500], job_id),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        log.debug("단계 실패 기록 실패(무시): %s", e)
    finally:
        if own:
            conn.close()


def progress(job_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """어디서 이어받을지 정하는 데 필요한 것만 낸다.

    ## 왜 이어받나

    예전에는 실패하거나 중단되면 그냥 **다음 공고로 넘어갔고**, 같은 공고를
    다시 잡으면 이력서 조립(LLM 1~2회)부터 통째로 다시 했다. 더 나쁜 건
    등록까지 끝난 뒤에 끊긴 경우다 — 다시 하면 사본에서 **새 이력서를 또
    만든다.** 계정에 같은 공고용 이력서가 두 개 쌓이고, 어느 것이 나갈지
    지원 폼에서 알 수 없게 된다.

    ## 무엇을 재사용하고 무엇을 버리나

        registered  재사용한다. 완성된 이력서가 플랫폼에 실제로 있다.
        prepared    (같다)
                    fill()을 **다시 부르지 않는다** — 사본 스킬 채우기는
                    "비어 있다"를 전제로 추가만 하므로(prune 없음), 이미
                    채워진 이력서를 다시 채우면 스킬이 두 번 들어간다.

        filling     버린다. 채우다 끊긴 이력서라 절반만 채워져 있을 수 있고,
                    그게 그대로 제출되는 것이 이 파이프라인 최악의 결과다.
                    새로 만든다 — 버려지는 이력서는 made_resumes에 남아
                    `resumes --cleanup`이 치운다(만들자마자 적으므로 채우다
                    실패한 것도 우리 것으로 잡힌다).

        assembled   조립 결과는 어차피 72시간 캐시(_load_cached)가 재사용한다.
                    여기서 따로 할 일은 없다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT stage, stage_at, stage_error, resume_title, resume_url "
            "FROM resume_builds WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            return {"stage": "", "resume_title": "", "resume_url": "", "resumable": False}
        stage = row["stage"] or ""
        # 제목이 없으면 등록됐다고 볼 수 없다. 지원 단계가 그 제목으로 이력서를
        # 고르므로, 표식만 있고 제목이 없으면 이어받아도 무엇을 낼지 모른다.
        # prepared도 재사용한다. 지원 폼까지 갔다는 것은 이력서가 **완성된 채로**
        # 플랫폼에 있다는 뜻이다 — 다시 부르면 폼과 스크린샷만 새로 만들면 되고,
        # 이력서를 또 만들 이유가 없다.
        resumable = stage in ("registered", "prepared") and bool(row["resume_title"])
        return {
            "stage": stage,
            "stage_at": row["stage_at"] or "",
            "stage_error": row["stage_error"] or "",
            "resume_title": row["resume_title"] or "",
            "resume_url": row["resume_url"] or "",
            "resumable": resumable,
        }
    finally:
        if own:
            conn.close()


def registered_title(job_id: int, conn: sqlite3.Connection | None = None) -> str:
    """이 공고로 등록해둔 이력서 제목. 없으면 빈 문자열."""
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT resume_title FROM resume_builds WHERE job_id=?", (job_id,)
        ).fetchone()
        return (row["resume_title"] or "") if row else ""
    finally:
        if own:
            conn.close()


def record_registration(
    job_id: int,
    *,
    resume_title: str,
    resume_url: str = "",
    skills: list[str] | None = None,
    report: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """플랫폼에 등록된 이력서를 기록한다.

    준비 단계에서 공고용 이력서를 실제로 만들어 등록해두고, 제출은 그것을
    고르기만 한다. 그래서 승인 시점에 다시 조립·입력할 필요가 없다 —
    사람이 검토한 그 이력서가 그대로 나간다.

    화면에서 제목을 매번 읽지 않는 이유도 있다. 제목이 있는 자리의 문구가
    상태에 따라 바뀌어("기본 이력서 설정" → "기본 이력서") 고리가 끊긴 적이 있다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            """UPDATE resume_builds
               SET resume_title=?, resume_url=?, skills=?, fill_report=?, completeness=?
               WHERE job_id=?""",
            (resume_title, resume_url, json.dumps(skills or [], ensure_ascii=False),
             json.dumps(report or {}, ensure_ascii=False),
             (report or {}).get("completeness"), job_id),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def registration(job_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """이 공고로 등록해둔 이력서 정보. 없으면 빈 값."""
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT resume_title, resume_url, headline, portfolio_title "
            "FROM resume_builds WHERE job_id=?",
            (job_id,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        if own:
            conn.close()


def manual_todos(job_id: int, conn: sqlite3.Connection | None = None) -> list[str]:
    """이 공고에서 사람이 직접 해야 하는 일. 없으면 빈 목록.

    승인 알림을 만드는 쪽이 읽는다. 조립 결과(JSON 파일)가 아니라 DB에서 읽는
    이유는 승인 알림이 조립과 다른 프로세스에서 만들어지기 때문이다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT manual_todos FROM resume_builds WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row or not row["manual_todos"]:
            return []
        try:
            items = json.loads(row["manual_todos"])
        except json.JSONDecodeError:
            return []
        return [str(x) for x in items] if isinstance(items, list) else []
    finally:
        if own:
            conn.close()


def review_notes(job_id: int, conn: sqlite3.Connection | None = None) -> list[str]:
    """검수가 남긴 지적(과 제거된 스킬). 승인 화면에 띄울 것이다."""
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT review_notes FROM resume_builds WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row or not row["review_notes"]:
            return []
        try:
            items = json.loads(row["review_notes"])
        except json.JSONDecodeError:
            return []
        return [str(x) for x in items] if isinstance(items, list) else []
    finally:
        if own:
            conn.close()


def review_block(notes: list[str]) -> str:
    """승인 캡션의 검수 블록. 없으면 빈 문자열.

    `todo_block`과 갈라 둔다 — 저쪽은 **사람이 할 일**이고 이쪽은 **사람이 볼 것**이다.
    섞으면 "내가 뭘 해야 하나"와 "이게 나가도 되나"가 한 목록에 붙는다.
    """
    if not notes:
        return ""
    lines = "\n".join(f"• {n[:70]}" for n in notes[:3])
    more = f"\n<i>외 {len(notes) - 3}건</i>" if len(notes) > 3 else ""
    return f"\n\n⚠️ <b>검수 지적 — 확인해주세요</b>\n{lines}{more}"


def submitted_titles(conn: sqlite3.Connection | None = None) -> set[str]:
    """제출에 쓰인 이력서 제목. 지우기 전에 로컬 사본이 있는지 확인하는 데 쓴다."""
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute(
            """SELECT b.resume_title FROM resume_builds b
               JOIN apply_ledger l ON l.job_id = b.job_id
               WHERE l.status='submitted' AND b.resume_title IS NOT NULL"""
        ).fetchall()
        return {r["resume_title"] for r in rows if r["resume_title"]}
    finally:
        if own:
            conn.close()


def builds_log(limit: int = 8) -> list[dict[str, Any]]:
    """조립·등록 기록 — 어디까지 갔고 왜 미완인지.

    `cli.py builds`가 쓴다. 'filling'이 남아 있으면 채우다 끊긴 것이라
    다음 실행이 그 이력서를 버리고 새로 만든다(`progress`의 재사용 규칙).
    """
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT b.job_id, j.company, b.resume_title, b.completeness,
                      b.required_gaps, b.fill_report, b.built_at,
                      b.stage, b.stage_at, b.stage_error
               FROM resume_builds b LEFT JOIN jobs j ON j.id = b.job_id
               ORDER BY b.built_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        rep = json.loads(r["fill_report"] or "{}")
        out.append({
            "job_id": r["job_id"], "회사": r["company"],
            "단계": r["stage"], "단계시각": r["stage_at"],
            **({"단계오류": r["stage_error"][:120]} if r["stage_error"] else {}),
            "이력서": r["resume_title"], "완성도": r["completeness"],
            "필수미충족": r["required_gaps"],
            "저장안됨": rep.get("lost") or [],
            "플랫폼이 요구": rep.get("platform_todo") or [],
            "스킬누락": rep.get("skills_skipped") or [],
            "작성완료": rep.get("finalized"),
        })
    return out
