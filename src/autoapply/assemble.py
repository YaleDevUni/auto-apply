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

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import llm
from .config import effective_config
from .db import connect
from .paths import RESUME_OUT_DIR, RESUME_SRC_DIR

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
    """profile/resume/ 의 모든 .md를 합쳐 사실 저장소로 넘긴다."""
    if not RESUME_SRC_DIR.is_dir():
        raise NoGuide(f"이력서 원본 디렉터리가 없다: {RESUME_SRC_DIR}")
    files = sorted(RESUME_SRC_DIR.rglob("*.md"))
    if not files:
        raise NoGuide(f"{RESUME_SRC_DIR} 에 .md 파일이 없다")
    return "\n\n".join(f"<!-- {f.name} -->\n{f.read_text(encoding='utf-8')}" for f in files)


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
{fix}"""
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
