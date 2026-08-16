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
from .normalize import canonical_key
from .paths import RECIPE_DIR
from .screening import evaluate_applicability, screen
from . import tasks


def _gap_counts(conn) -> dict[int, int]:
    """이력서를 조립해본 공고의 필수 미충족 건수. 판정이 blocker로 쓴다."""
    return {
        r["job_id"]: r["required_gaps"]
        for r in conn.execute(
            "SELECT job_id, required_gaps FROM resume_builds WHERE required_gaps > 0"
        ).fetchall()
    }


SETTLED = ("claimed", "submitted", "external")


def _settled(conn) -> tuple[set[str], set[tuple[str, str]]]:
    """다시 지원할 일이 없는 자리. (canonical_key 집합, (플랫폼, 공고id) 집합)

    우리가 낸 것(claimed/submitted)과 사람이 예전에 직접 낸 것(external)이
    같이 들어간다. 대기열(`v_actionable`)이 쓰는 규칙과 **같은 목록**이다 —
    한쪽만 고치면 "대기열엔 안 뜨는데 상세는 매일 받는" 상태가 된다.

    **두 가지로 본다.** canonical_key는 회사명+제목의 해시라 플랫폼이 달라도
    같은 자리를 잡아주지만, **제목이 바뀌면 키도 바뀐다.** 원장의 키는 적을
    때 값으로 굳어 있고 수집은 매번 새 제목으로 키를 다시 계산하므로, 회사가
    공고 제목을 한 글자만 고쳐도 그 자리가 '처음 보는 자리'로 돌아온다
    (실측 2026-08-16: 공고 4110의 제목을 고쳤더니 키가 갈려 상세 조회가
    다시 나갔다). 플랫폼 공고 id는 그런 일이 없으므로 같이 본다.
    """
    keys, ids = set(), set()
    for r in conn.execute(
        "SELECT l.canonical_key AS k, j.platform AS p, j.platform_job_id AS pid "
        "FROM apply_ledger l LEFT JOIN jobs j ON j.id = l.job_id "
        f"WHERE l.status IN ({','.join('?' * len(SETTLED))})",
        SETTLED,
    ):
        if r["k"]:
            keys.add(r["k"])
        if r["p"] and r["pid"]:
            ids.add((r["p"], str(r["pid"])))
    return keys, ids


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
    gaps = _gap_counts(conn)
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

            # 중단은 **여기서 던지지 않고 표시만 남긴다.** 예외로 튀어나가면
            # 이미 받아둔 공고가 통째로 버려진다 — 수 분을 들여 받은 것을
            # 버리는 건 중단의 목적이 아니다. 받은 데까지는 저장하고 접는다.
            stopped = False

            # --- 1차: 목록 데이터만으로 판정 ---
            #
            # 중단 확인은 **건 사이**에서만 한다. 요청 하나를 반쯤 받은 채로
            # 접으면 그 공고는 있지도 없지도 않은 상태가 된다.
            staged: list[tuple[JobPosting, dict[str, Any]]] = []
            for job in adapter.fetch():
                if tasks.cancelled():
                    stopped = True
                    log.warning("[%s] 중단 요청 — 목록 수집을 %d건에서 멈춘다",
                                platform, counts["found"])
                    break
                counts["found"] += 1
                staged.append((job, screen(job, cfg)))

            passed = [(j, v) for j, v in staged if v["verdict"] == "pass"]
            passed.sort(key=lambda t: t[1]["fit_score"], reverse=True)

            # 이미 끝난 자리는 **상세를 받지 않는다.**
            #
            # 목록 API에는 "이건 빼고 줘"가 없으므로 목록에 실려 오는 것 자체는
            # 못 막는다. 실제 비용은 그다음이다 — 상세 조회는 공고 하나당 요청
            # 하나이고, 여기가 수집 시간의 거의 전부다. 게다가 detail_limit(800)
            # 자리를 하나 차지하므로, 다시 낼 일 없는 공고가 **진짜 후보를
            # 밀어낸다.**
            #
            # canonical_key는 목록 데이터(회사명+제목)만으로 만들어진다 —
            # 상세를 받아봐야 아는 값이 아니라서 받기 전에 거를 수 있다.
            settled_keys, settled_ids = _settled(conn)
            fresh = [
                (j, v) for j, v in passed
                if canonical_key(j.company, j.title) not in settled_keys
                and (j.platform, str(j.platform_job_id)) not in settled_ids
            ]
            counts["settled_skipped"] = len(passed) - len(fresh)
            log.info(
                "[%s] 수집 %d건 → 1차 통과 %d건 (제외 %d건) · 이미 끝난 자리 %d건은 "
                "상세 조회 생략",
                platform, counts["found"], len(passed),
                counts["found"] - len(passed), counts["settled_skipped"],
            )

            # --- 2차: 통과분만 상세 조회 ---
            enrich = getattr(adapter, "enrich", None)
            if not stopped and enrich and s.get("fetch_detail", True):
                for n, (job, _) in enumerate(fresh[: s.get("detail_limit", 120)]):
                    # 여기가 제일 긴 구간이다(상세 800건 = 수 분~수십 분).
                    # 중단을 눌렀을 때 실제로 멈추는 자리도 대개 여기다.
                    if tasks.cancelled():
                        stopped = True
                        log.warning("[%s] 중단 요청 — 상세 조회를 %d건에서 멈춘다", platform, n)
                        break
                    try:
                        enrich(job)
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "상세 조회 실패 %s/%s: %s", platform, job.platform_job_id, exc
                        )

            # --- 저장 + 지원가능성 판정 ---
            # 중단됐어도 **여기는 끝까지 돈다.** 네트워크는 이미 다 쓴 뒤라
            # 남은 건 로컬 판정뿐이고(수천 건이어도 초 단위), 저장을 건너뛰면
            # 받아온 것이 전부 사라진다.
            for job, first in staged:
                final = screen(job, cfg) if job.description else first
                job_id, action = upsert_job(conn, job.to_db())
                save_screening(conn, job_id, final)
                counts[action] += 1

                if final["verdict"] != "pass":
                    counts["excluded"] += 1
                    continue

                appl = evaluate_applicability(
                    job, final, cfg, recipe_dir=RECIPE_DIR, session_ok=session_ok,
                    job_id=job_id, gap_counts=gaps,
                )
                save_applicability(conn, job_id, appl)
                if appl["actionable"]:
                    counts["actionable"] += 1

            conn.commit()
            if stopped:
                error = "중단됨(사람 요청) — 받은 데까지 저장"

    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        log.exception("[%s] 파이프라인 실패", platform)

    finish_run(conn, run_id, **counts, error=error)
    conn.close()
    return {"platform": platform, **counts, "error": error,
            "stopped": bool(error and error.startswith("중단됨"))}


def run_all(
    platforms: list[str] | None = None,
    session_ok: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    cfg = effective_config()
    # --platform으로 명시하면 그게 이긴다. 아니면 config의 enabled 목록을 따른다.
    # 어댑터가 있다고 다 수집하지 않는다 — 레시피가 없는 플랫폼을 수집하면
    # 지원도 못 하면서 판정 통계만 오염시킨다(NO_RECIPE가 통과분을 지배).
    targets = platforms or cfg.get("scrape", {}).get("platforms") or list(REGISTRY)
    unknown = [p for p in targets if p not in REGISTRY]
    if unknown:
        raise ValueError(f"모르는 플랫폼: {unknown}")
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
    gaps = _gap_counts(conn)
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
            appl = evaluate_applicability(
                job, result, cfg, recipe_dir=RECIPE_DIR,
                job_id=row["id"], gap_counts=gaps,
            )
            save_applicability(conn, row["id"], appl)
            if appl["actionable"]:
                stats["actionable"] += 1

    conn.commit()
    conn.close()
    return stats
