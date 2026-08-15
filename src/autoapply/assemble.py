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
  "ai_usage": "AI 활용 경험. 가이드 §6-1을 기준으로 쓰되 이 공고에 맞춰 조정",
  "skills": ["스킬", "나열"],
  "links": [{"name": "GitHub", "url": "https://..."}],
  "gaps": [{"level": "필수|우대", "text": "공고 요건 중 근거가 없는 항목"}]
}"""


def build_editor_json(
    job_id: int, *, conn: sqlite3.Connection | None = None, save: bool = True
) -> dict[str, Any]:
    """원티드 편집기 폼에 그대로 넣을 수 있는 형태로 조립한다."""
    guide = load_guide()
    job = load_job(job_id, conn)

    prompt = f"""아래 [작성 가이드]를 따라 [채용공고]에 맞춘 이력서를 **JSON으로만** 출력하라.

가이드의 §1 처리 순서, §3 사실 저장소, §4 문장 규칙, §5 일관성 체크리스트,
§6-1 AI 활용, §7 직무별 강조 우선순위를 적용한다.

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
{_jd_block(job)}"""

    raw = llm.ask(prompt)
    data = _parse_json(raw)
    _ensure_summary_length(data, job, guide)

    gaps = data.get("gaps") or []
    required = [g for g in gaps if g.get("level") == "필수"]
    max_gaps = effective_config().get("applicability", {}).get("max_required_gaps", 2)

    path = None
    if save:
        RESUME_OUT_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w가-힣]+", "_", job["company"])[:24]
        path = RESUME_OUT_DIR / f"{job_id}-{safe}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    _save_build(job_id, len(required) <= max_gaps, required, gaps, conn)

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


SUMMARY_MIN = 450


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
    return json.loads(text[start : end + 1])


def _save_build(
    job_id: int, ok: bool, required: list, gaps: list, conn: sqlite3.Connection | None
) -> None:
    """조립 결과를 남긴다. 판정이 다음 실행에서 이걸 읽어 blocker로 쓴다."""
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            """INSERT INTO resume_builds (job_id, ok, required_gaps, gaps, built_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 ok=excluded.ok, required_gaps=excluded.required_gaps,
                 gaps=excluded.gaps, built_at=excluded.built_at""",
            (job_id, 1 if ok else 0, len(required),
             json.dumps(gaps, ensure_ascii=False), now()),
        )
        conn.commit()
    finally:
        if own:
            conn.close()
