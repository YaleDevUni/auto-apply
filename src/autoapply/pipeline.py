"""수집 → 두 축 판정 → 저장.

리소스 절감의 핵심 순서 (v1에서 검증된 순서를 그대로 지킨다):
  1) 목록 API/HTML만으로 적합도 판정 (요청 1회당 수십~수백 건 처리)
  2) 통과한 공고만 상세 요청           ← 여기서 요청 수가 결정된다
  3) 본문이 붙은 상태로 재판정 → 최종 점수
  4) 통과분에 한해 지원가능성 판정

지원가능성을 마지막에 두는 이유: 적합도에서 떨어진 공고는 지원할 일이 없으므로
판정 비용을 쓸 이유가 없다. 다만 통과분은 **전부** 판정한다 — 에이전트가
"막힌 이유"를 물었을 때 답하려면 blocker가 기록돼 있어야 한다.
"""

from __future__ import annotations

import logging
from typing import Any

from .adapters import REGISTRY
from .adapters.base import Adapter, JobPosting
from .config import effective_config
from .db import (
    connect,
    finish_run,
    save_applicability,
    save_screening,
    start_run,
    upsert_job,
)
from .http import Fetcher
from .paths import RECIPE_DIR
from .screening import evaluate_applicability, screen

log = logging.getLogger(__name__)


def run_platform(
    platform: str,
    cfg: dict[str, Any] | None = None,
    session_ok: dict[str, bool] | None = None,
) -> dict[str, Any]:
    cfg = cfg or effective_config()
    s = cfg["scrape"]
    adapter_cls = REGISTRY[platform]

    conn = connect()
    run_id = start_run(conn, platform)
    counts = {"found": 0, "inserted": 0, "updated": 0, "excluded": 0, "actionable": 0}
    error: str | None = None

    try:
        with Fetcher(
            delay=s["request_delay_sec"],
            timeout=s["timeout_sec"],
            retries=s["max_retries"],
        ) as fetcher:
            adapter: Adapter = adapter_cls(fetcher, cfg)

            # --- 1차: 목록 데이터만으로 판정 ---
            staged: list[tuple[JobPosting, dict[str, Any]]] = []
            for job in adapter.fetch():
                counts["found"] += 1
                staged.append((job, screen(job, cfg)))

            passed = [(j, v) for j, v in staged if v["verdict"] == "pass"]
            passed.sort(key=lambda t: t[1]["fit_score"], reverse=True)
            log.info(
                "[%s] 수집 %d건 → 1차 통과 %d건 (제외 %d건, 상세요청은 통과분만)",
                platform, counts["found"], len(passed), counts["found"] - len(passed),
            )

            # --- 2차: 통과분만 상세 조회 ---
            enrich = getattr(adapter, "enrich", None)
            if enrich and s.get("fetch_detail", True):
                for job, _ in passed[: s.get("detail_limit", 120)]:
                    try:
                        enrich(job)
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "상세 조회 실패 %s/%s: %s", platform, job.platform_job_id, exc
                        )

            # --- 저장 + 지원가능성 판정 ---
            for job, first in staged:
                final = screen(job, cfg) if job.description else first
                job_id, action = upsert_job(conn, job.to_db())
                save_screening(conn, job_id, final)
                counts[action] += 1

                if final["verdict"] != "pass":
                    counts["excluded"] += 1
                    continue

                appl = evaluate_applicability(
                    job, final, cfg, recipe_dir=RECIPE_DIR, session_ok=session_ok
                )
                save_applicability(conn, job_id, appl)
                if appl["actionable"]:
                    counts["actionable"] += 1

            conn.commit()

    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        log.exception("[%s] 파이프라인 실패", platform)

    finish_run(conn, run_id, **counts, error=error)
    conn.close()
    return {"platform": platform, **counts, "error": error}


def run_all(
    platforms: list[str] | None = None,
    session_ok: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    cfg = effective_config()
    targets = platforms or list(REGISTRY)
    return [run_platform(p, cfg, session_ok) for p in targets]


def reevaluate() -> dict[str, int]:
    """재수집 없이 저장된 공고 전체를 다시 판정한다.

    필터 기준(config.yaml)을 고쳤거나, 서류를 새로 준비했거나, 로그인 세션이
    살아났을 때 쓴다. 네트워크 요청 0회, LLM 호출 0회다.
    """
    from .config import load_config

    load_config.cache_clear()
    cfg = effective_config()
    conn = connect()
    rows = conn.execute("SELECT * FROM jobs WHERE closed_at IS NULL").fetchall()
    stats = {"pass": 0, "excluded": 0, "actionable": 0}

    for row in rows:
        job = JobPosting(
            platform=row["platform"],
            platform_job_id=row["platform_job_id"],
            url=row["url"],
            company=row["company"],
            title=row["title"],
            category=row["category"],
            location=row["location"],
            employment_type=row["employment_type"],
            experience_req=row["experience_req"],
            education_req=row["education_req"],
            deadline=row["deadline"],
            description=row["description"] or "",
            image_url=row["image_url"],
            image_path=row["image_path"],
        )
        result = screen(job, cfg)
        save_screening(conn, row["id"], result)
        stats[result["verdict"]] += 1

        if result["verdict"] == "pass":
            appl = evaluate_applicability(job, result, cfg, recipe_dir=RECIPE_DIR)
            save_applicability(conn, row["id"], appl)
            if appl["actionable"]:
                stats["actionable"] += 1

    conn.commit()
    conn.close()
    return stats
