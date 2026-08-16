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
    out = llm.ask(prompt, model=cfg.get("review_model", "claude-haiku-4-5-20251001"))
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
    return llm.ask(prompt)


def review(job: dict[str, Any], guide: str, resume: str) -> dict[str, Any]:
    """치명적 누락만 잡는다. 문체 취향으로 반려하면 재작성 루프만 돌고 안 끝난다."""
    cfg = effective_config().get("llm", {})
    prompt = f"""아래 이력서가 채용공고와 작성 가이드를 지켰는지 검수하라.

**치명적 문제만** 지적한다. 문체 취향·사소한 표현은 지적하지 않는다.
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

    out = llm.ask(prompt, model=cfg.get("review_model", "claude-haiku-4-5-20251001")).strip()
    ok = out.upper().startswith("OK")
    issues = [] if ok else [ln.strip() for ln in out.splitlines() if ln.strip().startswith("-")]
    return {"ok": ok, "issues": issues, "raw": out}


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
# AI 활용/스킬/링크). 마크다운 한 덩어리로는 채울 수 없다.
#
# 마크다운을 파싱해 쪼개지 않고 처음부터 JSON으로 받는 이유: 파싱은 문장 형태가
# 조금만 바뀌어도 깨지는데, 그 깨짐이 조용하다. 잘못 쪼개진 채로 폼에 들어가면
# 회사명 칸에 직무가 들어간 이력서가 제출된다.

EDITOR_SCHEMA = """{
  "headline": "한 줄 타이틀 (공고에 맞춰 매번 다시 씀)",
  "summary": "간단 소개. 아래 형식을 정확히 지킨다.\n  · 첫 줄: 한 문장 요약 (무엇을 하는 개발자인가)\n  · 빈 줄 하나\n  · '· ' 로 시작하는 핵심역량 불릿 4개. 가이드 §2의 핵심역량 항목이 이것이다\n  · 각 불릿은 한 줄, 태도가 아니라 역량/경험 단위\n  전체 **450~600자**. 원티드가 400자 미만이면 이력서 완성도를 깎으므로 반드시 넘긴다. 줄바꿈(\\n)을 실제로 넣는다 — 한 문단으로 붙이지 않는다",
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
  "educations": [
    {"school": "학교명", "major": "전공 및 학위", "start": "YYYY.MM", "end": "YYYY.MM",
     "detail": "이수 과목 또는 연구 내용"}
  ],
  "ai_usage": "AI 활용 경험 **한 줄 요약. 50자 이내(엄수).** 원티드 필드가 50자 제한이라 넘기면 잘린다. 상세 내용은 summary 불릿이나 주요 성과에 녹인다",
  "languages": [{"name": "영어", "level": "고급 비즈니스 레벨"}],
  "_note_language": "level은 원티드 선택지 문구를 그대로 써야 한다: 유창함 | 고급 비즈니스 레벨 | 비즈니스 레벨 | 일상 회화. 다른 표현을 쓰면 선택지를 못 찾아 비워둔 채 지나간다. 가이드에 '영어 유창'만 있으면 '고급 비즈니스 레벨'로 적는다.",
  "skills": ["스킬", "나열"],
  "links": [{"name": "GitHub", "url": "https://..."}],
  "gaps": [{"level": "필수|우대", "text": "공고 요건 중 근거가 없는 항목"}]
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

    prompt = f"""아래 [작성 가이드]를 따라 [채용공고]에 맞춘 이력서를 **JSON으로만** 출력하라.

가이드의 §1 처리 순서, §3 사실 저장소, §4 문장 규칙, §5 일관성 체크리스트,
§6-1 AI 활용, §7 직무별 강조 우선순위를 적용한다.

**한 분야로 초점을 맞춘다.** 공고가 요구하는 핵심 역량 하나를 정하고 거기에 맞춰
구성한다. 백엔드·DevOps·인프라·AI를 한 이력서에 고루 담으면 어느 것도 강해 보이지
않는다. §7 우선순위표의 1순위를 축으로 삼고, 2·3순위는 보조로만 쓴다.

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

# 작성 가이드
{guide}

# 채용공고
{_jd_block(job)}
{load_revision_log(job_id)}{_track_block(job)}{_revision_block(feedback)}"""

    raw = llm.ask(prompt)
    data = _parse_json(raw)
    _strip_reasoning(data)
    _clamp_fields(data)
    _ensure_summary_length(data, job, guide)
    _strip_reasoning(data)  # 보강 응답에도 섞일 수 있다

    gaps = data.get("gaps") or []
    required = [g for g in gaps if g.get("level") == "필수"]
    max_gaps = effective_config().get("applicability", {}).get("max_required_gaps", 2)

    path = None
    if save:
        RESUME_OUT_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w가-힣]+", "_", job["company"])[:24]
        path = RESUME_OUT_DIR / f"{job_id}-{safe}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    _save_build(
        job_id, len(required) <= max_gaps, required, gaps, conn,
        track=job.get("track"), headline=data.get("headline"),
    )

    return {
        "job_id": job_id,
        "company": job["company"],
        "title": job["title"],
        "ok": len(required) <= max_gaps,
        "required_gaps": len(required),
        "gaps": gaps,
        "path": str(path) if path else None,
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

# 원티드 편집기 필드 길이 제한. 넘기면 브라우저가 조용히 자른다.
FIELD_LIMITS = {"ai_usage": 50}

SUMMARY_MIN = 450


def _strip_reasoning(data: dict[str, Any]) -> None:
    """판단 근거 문장을 걷어낸다.

    프롬프트로 "사고 과정을 쓰지 마라"고 해도 새어 나온다. 그게 이력서 맨 앞에
    들어가면 읽는 사람에게 곧바로 보인다 — 프롬프트에 의존하지 않고 코드로 막는다.

    문단 단위로 검사한다. 근거 문장은 보통 첫 문단에 통째로 오고, 본문 불릿에는
    섞이지 않는다.
    """
    for key in ("summary", "ai_usage"):
        text = data.get(key)
        if not isinstance(text, str) or not text.strip():
            continue
        blocks = [b for b in text.split("\n\n")]
        kept = [b for b in blocks if not (REASONING.search(b) and not b.strip().startswith("·"))]
        if len(kept) != len(blocks):
            log.info("%s에서 판단 근거 문단 %d개 제거", key, len(blocks) - len(kept))
        data[key] = "\n\n".join(kept).strip()


def _clamp_fields(data: dict[str, Any]) -> None:
    """길이 제한이 있는 필드를 다듬는다.

    'AI 활용 경험'은 50자짜리 **한 줄 요약 칸**이다. 머리말("AI 활용")과 불릿
    기호는 그 50자를 갉아먹으므로 먼저 걷어낸다. 그래도 넘치면 단어 중간이 아니라
    구두점·공백에서 자른다 — 편집기가 자르면 "…채용 자동화 파이"처럼 끊긴다.
    """
    for key, limit in FIELD_LIMITS.items():
        text = data.get(key)
        if not isinstance(text, str) or not text.strip():
            continue

        # 머리말 줄과 불릿 기호 제거 → 한 줄로
        lines = [ln.strip().lstrip("·-• ").strip() for ln in text.splitlines() if ln.strip()]
        lines = [ln for ln in lines if ln not in ("AI 활용", "AI 활용 경험")]
        one = " ".join(lines).strip()

        if len(one) > limit:
            cut = one[:limit]
            # 마지막 구두점이나 공백까지만 남긴다
            for sep in (". ", ", ", " "):
                idx = cut.rfind(sep)
                if idx > limit * 0.5:
                    cut = cut[:idx]
                    break
            one = cut.rstrip(" ,.·")
            log.info("%s가 %d자 — %d자로 줄였다", key, len(text), len(one))

        data[key] = one


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
        f"""아래 [현재 간단 소개]를 {SUMMARY_MIN}~600자로 늘려라.

규칙:
- 구조를 유지한다. 첫 줄 한 문장 요약 + 빈 줄 + `· ` 불릿 4개.
- 불릿 개수를 늘리지 말고 **각 불릿의 내용을 구체화**한다
  (맡은 범위, 사용 기술, 결과 수치).
- [사실 저장소]에 없는 내용을 만들지 않는다.
- 명사형 종결. `~습니다` 금지. 주어 생략.
- 결과 텍스트만 출력한다. 설명·코드펜스 금지.

# 현재 간단 소개
{summary}

# 사실 저장소
{guide}

# 채용공고
{_jd_block(job)}"""
    ).strip()

    if SUMMARY_MIN <= len(out) <= 900:
        data["summary"] = out
    else:
        log.warning("보강 결과가 %d자 — 원문을 유지한다", len(out))


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
) -> None:
    """조립 결과를 남긴다.

    두 가지로 쓰인다:
    1. 판정이 `REQUIREMENT_GAP` blocker를 세울 때 (required_gaps)
    2. 지원 단계가 **어느 이력서를 낼지 정할 때** (resume_title)

    2번이 중요하다. 예전에는 편집기 화면에서 제목을 읽어 넘겼는데, 그 제목이
    있는 자리의 문구가 상태에 따라 바뀌어("기본 이력서 설정" → "기본 이력서")
    조립과 지원을 잇는 고리가 통째로 끊겼다. 기록해두면 화면에 의존하지 않는다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            """INSERT INTO resume_builds
                 (job_id, ok, required_gaps, gaps, track, headline, built_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 ok=excluded.ok, required_gaps=excluded.required_gaps,
                 gaps=excluded.gaps, track=excluded.track,
                 headline=excluded.headline, built_at=excluded.built_at""",
            (job_id, 1 if ok else 0, len(required),
             json.dumps(gaps, ensure_ascii=False), track, headline, now()),
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
            "SELECT ok, required_gaps, gaps, built_at FROM resume_builds WHERE job_id=?",
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
            "path": str(path),
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
            f"AI 활용: {data.get('ai_usage', '')[:50]}",
            "스킬: " + ", ".join((data.get("skills") or [])[:8]),
            "링크: " + ", ".join((l or {}).get("name", "") for l in (data.get("links") or [])),
            "언어: " + ", ".join(
                f"{(l or {}).get('name')} {(l or {}).get('level')}"
                for l in (data.get("languages") or [])
            ),
        ] if x
    )
    return vision.verify(shot, intent, context="채용 플랫폼의 이력서 편집 화면")


def record_registration(
    job_id: int,
    *,
    resume_title: str,
    resume_url: str = "",
    skills: list[str] | None = None,
    report: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """플랫폼에 등록된 결과를 기록한다.

    지원 단계는 이 값으로 이력서를 고른다. 예전에는 편집기 화면에서 제목을
    읽어 곧바로 넘겼는데, 그 자리의 문구가 상태에 따라 바뀌어
    ("기본 이력서 설정" → "기본 이력서") 조립과 지원을 잇는 고리가 끊겼다.
    한 번 기록해두면 그 뒤로는 화면 상태와 무관하다.
    """
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            """UPDATE resume_builds
               SET resume_title=?, resume_url=?, skills=?
               WHERE job_id=?""",
            (resume_title, resume_url, json.dumps(skills or [], ensure_ascii=False), job_id),
        )
        conn.commit()
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
            "SELECT resume_title, resume_url, headline FROM resume_builds WHERE job_id=?",
            (job_id,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        if own:
            conn.close()


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
