"""축 1 — 적합도. 하드컷 + 스코어링.

설계 원칙: LLM 호출 이전에 전부 순수 문자열 연산으로 끝낸다.
제외된 공고는 사유와 함께 DB에 남지만, 어떤 LLM 호출도 발생시키지 않는다.

v1에서 검증된 로직을 그대로 가져왔다. 세 플랫폼 수천 건으로 튜닝된 값이라
근거 없이 손대지 않는다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..adapters.base import JobPosting


def _hits(text: str, keywords: list[str]) -> list[str]:
    return [kw for kw in keywords if kw in text]


def _deadline_passed(deadline: str | None) -> bool:
    if not deadline:
        return False  # 상시채용
    try:
        d = datetime.fromisoformat(deadline.replace("Z", "+00:00")).date()
    except ValueError:
        return False
    return d < date.today()


def detect_track(text: str, cfg: dict[str, Any]) -> tuple[str | None, str | None, list[str]]:
    """가장 많이 매칭된 트랙을 고른다. 동점이면 weight가 높은 쪽."""
    best: tuple[int, int, str] | None = None
    best_hits: list[str] = []

    for key, track in cfg["tracks"].items():
        hits = _hits(text, track["keywords"])
        if not hits:
            continue
        score = (len(hits), track.get("weight", 0))
        if best is None or score > best[:2]:
            best = (*score, key)
            best_hits = hits

    if best is None:
        return None, None, []
    track_key = best[2]
    return track_key, cfg["tracks"][track_key]["label"], best_hits


def screen(job: JobPosting, cfg: dict[str, Any]) -> dict[str, Any]:
    """공고 하나를 판정한다.

    반환 dict:
      verdict: 'pass' | 'excluded'
      exclude_code / exclude_label / exclude_hits
      track / track_label / fit_score / score_detail
    """
    text = job.searchable_text()
    track_key, track_label, track_hits = detect_track(text, cfg)

    # 이 트랙이 무력화하는 하드컷 (예: MES/생산관리 사무직은 TRADE_FIELD 면제)
    overrides: list[str] = []
    if track_key:
        overrides = cfg["tracks"][track_key].get("overrides_hardcut") or []

    # ---------- 하드컷 ----------
    if _deadline_passed(job.deadline):
        return _excluded("CLOSED", cfg["hardcuts"]["CLOSED"]["label"],
                         [job.deadline or ""], track_key, track_label)

    for code, cut in cfg["hardcuts"].items():
        if code == "CLOSED":
            continue

        # always는 트랙 예외로도 못 넘는다. MES 트랙에 TRADE_FIELD를 면제해줬더니
        # '생산오퍼레이터', '주야교대' 같은 현장직까지 통과하던 것을 막는다.
        always_hits = _hits(text, cut.get("always", []))
        if code in overrides and not always_hits:
            continue

        hits = always_hits or _hits(text, cut["keywords"])
        if not hits:
            continue
        # unless 키워드가 있으면 하드컷 취소 (예: '경력 3년'이지만 '신입/경력' 병기).
        # 단 always 신호가 잡혔으면 unless로도 못 되돌린다.
        if not always_hits and cut.get("unless") and _hits(text, cut["unless"]):
            continue
        return _excluded(code, cut["label"], hits[:6], track_key, track_label)

    # 지원 대상 트랙에 아예 안 걸리면 관심 밖 직무로 간주
    if track_key is None:
        return _excluded("OFF_TRACK", "지원 트랙 외", [], None, None)

    # ---------- 적합도 스코어 ----------
    sc = cfg["scoring"]
    detail: dict[str, Any] = {}

    base = cfg["tracks"][track_key].get("weight", 50) // 2
    detail["트랙"] = {"track": track_label, "점수": base, "근거": track_hits[:5]}
    score = base

    stack_hits = _hits(text, sc["keywords_stack"])
    stack_pts = min(len(stack_hits) * sc["stack_bonus"], sc["stack_max"])
    if stack_pts:
        detail["기술스택"] = {"점수": stack_pts, "근거": stack_hits[:8]}
        score += stack_pts

    for label, kw_key, pts_key in (
        ("신입채용", "keywords_newbie", "newbie_bonus"),
        ("영어우대", "keywords_english", "english_bonus"),
        ("원격근무", "keywords_remote", "remote_bonus"),
        ("제조·산업도메인", "keywords_domain", "domain_bonus"),
    ):
        hits = _hits(text, sc[kw_key])
        if hits:
            detail[label] = {"점수": sc[pts_key], "근거": hits[:4]}
            score += sc[pts_key]

    loc_cfg = cfg.get("location", {})
    loc_hits = _hits(text, loc_cfg.get("preferred", []))
    if loc_hits:
        detail["선호지역"] = {"점수": loc_cfg["preferred_bonus"], "근거": loc_hits[:3]}
        score += loc_cfg["preferred_bonus"]

    # 상한을 두지 않는다. 100에서 자르면 상위권이 전부 100으로 뭉개져
    # 110점짜리와 130점짜리를 구분할 수 없다.
    return {
        "verdict": "pass",
        "exclude_code": None,
        "exclude_label": None,
        "exclude_hits": [],
        "track": track_key,
        "track_label": track_label,
        "fit_score": score,
        "score_detail": detail,
    }


def _excluded(
    code: str, label: str, hits: list[str], track: str | None, track_label: str | None
) -> dict[str, Any]:
    return {
        "verdict": "excluded",
        "exclude_code": code,
        "exclude_label": label,
        "exclude_hits": hits,
        "track": track,
        "track_label": track_label,
        "fit_score": 0,
        "score_detail": {},
    }
