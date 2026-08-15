"""SQLite 스키마 및 접근 레이어. ORM 없이 raw SQL로 투명하게 유지한다.

## v1과 무엇이 다른가

v1의 `applications.status`(미확인/관심/작성중/지원완료…)는 **사람이 UI에서 훑는**
칸반이었다. 완전 자동화에서는 그 상태기계가 필요 없다. 대신 두 가지가 필요하다.

1. **적합도와 지원가능성을 분리한다.** `screening`은 "이 공고가 나한테 맞나",
   `applicability`는 "이걸 자동으로 지원할 수 있나"를 답한다. 둘은 직교한다 —
   130점인데 외부 ATS라 손도 못 대는 공고가 있고, 70점인데 원클릭인 공고가 있다.
   에이전트는 둘 다 알아야 움직인다.

2. **중복지원을 DB가 막는다.** 사람이 안 보는 상태에서 같은 자리에 두 번 지원하면
   회복이 안 된다. `apply_ledger.canonical_key`에 UNIQUE를 걸어, 애플리케이션 로직이
   버그를 내도 두 번째 INSERT가 sqlite 레벨에서 터지게 한다. 방어를 코드가 아니라
   스키마에 두는 이유다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ─────────────────────────── 수집 ───────────────────────────

-- 수집된 공고 원본. v1 스키마를 그대로 계승한다 (세 플랫폼으로 검증된 형태).
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,             -- wanted | saramin | jasoseol
    platform_job_id TEXT NOT NULL,
    url             TEXT NOT NULL,
    company         TEXT NOT NULL,
    company_norm    TEXT,                      -- (주)·공백 제거한 정규화 이름
    title           TEXT NOT NULL,
    title_norm      TEXT,                      -- 상투어 제거한 정규화 제목
    category        TEXT,
    location        TEXT,
    employment_type TEXT,
    experience_req  TEXT,
    education_req   TEXT,
    salary          TEXT,
    deadline        TEXT,                      -- ISO8601, 상시채용이면 NULL
    posted_at       TEXT,
    description     TEXT,
    image_url       TEXT,
    image_path      TEXT,
    raw_json        TEXT,                      -- 플랫폼 원본 payload
    content_hash    TEXT,
    -- 같은 자리가 여러 플랫폼에 올라온 것을 묶는다. 지우지 않고 대표만 가리킨다.
    canonical_key   TEXT,                      -- company_norm + title_norm 해시
    closed_at       TEXT,
    close_reason    TEXT,
    miss_count      INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE (platform, platform_job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_platform  ON jobs(platform);
CREATE INDEX IF NOT EXISTS idx_jobs_deadline  ON jobs(deadline);
CREATE INDEX IF NOT EXISTS idx_jobs_closed    ON jobs(closed_at);
CREATE INDEX IF NOT EXISTS idx_jobs_canonical ON jobs(canonical_key);

-- ─────────────────────────── 판정 ───────────────────────────

-- 축 1: 적합도. "이 공고가 나한테 맞나"
-- Stage 0 하드컷 + Stage 1 스코어. 전부 순수 문자열 연산이라 LLM 호출이 0이다.
CREATE TABLE IF NOT EXISTS screening (
    job_id          INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    verdict         TEXT NOT NULL,             -- pass | excluded
    exclude_code    TEXT,                      -- LICENSE_REQUIRED 등
    exclude_label   TEXT,
    exclude_hits    TEXT,                      -- JSON: 걸린 키워드
    track           TEXT,                      -- dev | mes_erp | pm | presales | biz
    track_label     TEXT,
    fit_score       INTEGER DEFAULT 0,
    score_detail    TEXT,                      -- JSON: 항목별 점수 내역
    screened_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screening_verdict ON screening(verdict);
CREATE INDEX IF NOT EXISTS idx_screening_score   ON screening(fit_score DESC);

-- 축 2: 지원가능성. "이걸 자동으로 지원할 수 있나"
--
-- 적합도와 분리하는 이유: 점수가 높다고 지원할 수 있는 게 아니고, 지원할 수 있다고
-- 점수가 높은 게 아니다. 에이전트는 blockers를 읽고 "지금은 못 한다"를 알아야 하며,
-- requires를 읽고 "지원하려면 자소서 3개를 먼저 써야 한다"를 알아야 한다.
CREATE TABLE IF NOT EXISTS applicability (
    job_id       INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    actionable   INTEGER NOT NULL DEFAULT 0,   -- 1이면 지금 당장 자동지원 가능
    channel      TEXT,                         -- platform_form | external_ats
                                               -- | email | image_only | unknown
    apply_url    TEXT,                         -- 실제로 폼이 있는 URL
    blockers     TEXT NOT NULL DEFAULT '[]',   -- JSON: [{code, label, detail}]
    requires     TEXT NOT NULL DEFAULT '{}',   -- JSON: {essays, documents, login}
    confidence   REAL NOT NULL DEFAULT 0.0,    -- 0.0~1.0. 판정을 얼마나 믿는가
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applicability_actionable ON applicability(actionable);

-- ─────────────────────── 지원 원장 ───────────────────────

-- 지원했다는 사실의 단일 진실 공급원.
--
-- canonical_key에 UNIQUE가 걸려 있다. 같은 회사의 같은 자리는 자소설·사람인·원티드에
-- 각각 올라오지만 지원은 한 번뿐이어야 한다. 에이전트 코드가 버그를 내도 두 번째
-- INSERT가 sqlite에서 IntegrityError로 터진다 — 방어선을 코드가 아니라 스키마에 둔다.
--
-- 'claimed' 상태를 먼저 INSERT하고 실제 지원을 시도한다. 지원 도중에 프로세스가
-- 죽어도 claimed 행이 남아 재시도 때 같은 자리를 다시 건드리지 않는다.
CREATE TABLE IF NOT EXISTS apply_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL UNIQUE,        -- ← 중복지원 하드 가드
    job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    company       TEXT NOT NULL,
    title         TEXT NOT NULL,
    platform      TEXT NOT NULL,
    status        TEXT NOT NULL,               -- claimed | submitted | failed | abandoned
    claimed_at    TEXT NOT NULL,
    submitted_at  TEXT,
    evidence_path TEXT,                        -- 제출 증적 (스크린샷·본문 스냅샷)
    error         TEXT,
    agent_run_id  INTEGER REFERENCES agent_runs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON apply_ledger(status);
CREATE INDEX IF NOT EXISTS idx_ledger_job    ON apply_ledger(job_id);

-- ─────────────────────── 실행 로그 ───────────────────────

-- 에이전트 실행 1회. 사람이 지켜보지 않으므로 이 로그가 유일한 책임 추적 수단이다.
CREATE TABLE IF NOT EXISTS agent_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,                -- scrape | evaluate | apply
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    considered   INTEGER NOT NULL DEFAULT 0,
    acted        INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_kind ON agent_runs(kind, started_at DESC);

-- 수집 실행 로그
CREATE TABLE IF NOT EXISTS scrape_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    found       INTEGER DEFAULT 0,
    inserted    INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    excluded    INTEGER DEFAULT 0,
    error       TEXT
);

-- 기업규모 캐시. 회사 하나당 한 번만 조회하고 계속 재사용한다.
CREATE TABLE IF NOT EXISTS companies (
    name_norm    TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    size_code    TEXT,                         -- large | midsize | small | startup
                                               -- | public | foreign | unknown
    size_label   TEXT,
    source       TEXT,
    checked_at   TEXT NOT NULL
);

-- 플랫폼에 저장된 이력서 목록 캐시.
--
-- 지원할 때마다 브라우저를 띄워 목록을 훑을 수는 없다(건당 15초). 대신 한 번
-- 훑어 여기 넣고, 지원 루프는 이 표만 읽는다. 실측으로 확인된 필요성:
-- 원티드 계정에 이력서가 15개 있었고 전부 개발 직군이었다 — 어느 것을 낼지,
-- 애초에 낼 것이 있기는 한지 판단하려면 목록이 구조화돼 있어야 한다.
--
-- status가 '작성 중'인 이력서는 절대 제출하면 안 된다. 미완성 이력서가 나간다.
CREATE TABLE IF NOT EXISTS platform_resumes (
    platform     TEXT NOT NULL,
    resume_id    TEXT NOT NULL,             -- 플랫폼이 주는 불투명 id
    title        TEXT NOT NULL,
    memo         TEXT,                      -- 부제/메모 (원티드는 포지션 리뷰명)
    status       TEXT,                      -- 작성 완료 | 작성 중 | 업로드 완료
    is_default   INTEGER NOT NULL DEFAULT 0,
    modified_at  TEXT,                      -- 플랫폼이 보여주는 수정일
    track        TEXT,                      -- 어느 트랙용인지. 판정 결과를 적어둔다
    track_by     TEXT,                      -- rule | llm | manual — 누가 정했나
    synced_at    TEXT NOT NULL,
    PRIMARY KEY (platform, resume_id)
);
CREATE INDEX IF NOT EXISTS idx_resumes_track ON platform_resumes(platform, track);

-- 파이프라인 건강 스냅샷. 이상 감지는 '지금 이상한가'가 아니라 '어제와 다른가'로
-- 판정하는 항목이 있어서(수집 급감, actionable 붕괴) 직전 값을 남겨둬야 한다.
CREATE TABLE IF NOT EXISTS health_snapshots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    metrics  TEXT NOT NULL              -- JSON: jobs/passed/actionable/blockers/...
);

-- 폰에서 보낸 개발/아키텍처 지시. 운영 명령과 다른 큐를 쓴다.
--
-- 운영 명령(/status, /pause)은 정해진 동작을 즉시 수행한다. 이쪽은 자유 텍스트라
-- 코드를 고치게 되는데, 그건 검증 오라클이 없는 영역이다. 그래서 절대 main에
-- 닿지 않는다 — 전용 브랜치에 커밋하고 사람이 보고 판단한다.
CREATE TABLE IF NOT EXISTS control_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  TEXT NOT NULL,
    text         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed|skipped
    branch       TEXT,
    result       TEXT,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_control_status ON control_queue(status, id);

-- 전역 설정. 텔레그램 봇 토큰/채팅 id 등, 코드에 박으면 안 되는 값들.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- ─────────────────────── 에이전트 조회면 ───────────────────────

-- 에이전트는 이 뷰 하나만 읽는다. 다섯 테이블을 머릿속에서 JOIN하게 하지 않는다.
--
-- 정렬은 점수순이 아니라 (마감임박, 점수) 순이다. 완전 자동화에서는 오늘 밤에
-- 닫히는 95점짜리가 상시채용 130점짜리보다 급하다.
CREATE VIEW IF NOT EXISTS v_actionable AS
SELECT
    j.id            AS job_id,
    j.platform,
    j.company,
    j.title,
    j.url,
    j.deadline,
    j.canonical_key,
    s.track_label,
    s.fit_score,
    a.channel,
    a.apply_url,
    a.blockers,
    a.requires,
    a.confidence,
    CAST(julianday(j.deadline) - julianday('now') AS INTEGER) AS days_left
FROM jobs j
JOIN screening     s ON s.job_id = j.id
JOIN applicability a ON a.job_id = j.id
WHERE j.closed_at IS NULL
  AND s.verdict = 'pass'
  AND a.actionable = 1
  -- 이미 지원했거나 지원 시도를 선점한 자리는 나오지 않는다.
  AND j.canonical_key NOT IN (
      SELECT canonical_key FROM apply_ledger
      WHERE status IN ('claimed', 'submitted')
  )
  -- 같은 canonical_key가 여러 플랫폼에 있으면 job_id가 가장 작은 것만 대표로 낸다.
  AND j.id = (
      SELECT MIN(j2.id) FROM jobs j2
      JOIN applicability a2 ON a2.job_id = j2.id
      WHERE j2.canonical_key = j.canonical_key
        AND j2.closed_at IS NULL
        AND a2.actionable = 1
  )
ORDER BY
    CASE WHEN j.deadline IS NULL THEN 1 ELSE 0 END,  -- 마감일 있는 것 먼저
    j.deadline ASC,
    s.fit_score DESC;

-- 막힌 이유를 집계해서 본다. "왜 아무것도 지원 안 했지?"에 답하는 뷰다.
CREATE VIEW IF NOT EXISTS v_blocked AS
SELECT
    j.id AS job_id, j.platform, j.company, j.title, j.url, j.deadline,
    s.fit_score, a.channel, a.blockers
FROM jobs j
JOIN screening     s ON s.job_id = j.id
JOIN applicability a ON a.job_id = j.id
WHERE j.closed_at IS NULL
  AND s.verdict = 'pass'
  AND a.actionable = 0
ORDER BY s.fit_score DESC;
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ─────────────────────────── 쓰기 ───────────────────────────


def upsert_job(conn: sqlite3.Connection, job: dict[str, Any]) -> tuple[int, str]:
    """공고를 저장한다. 반환: (job_id, 'inserted'|'updated')"""
    ts = now()
    row = conn.execute(
        "SELECT id FROM jobs WHERE platform=? AND platform_job_id=?",
        (job["platform"], job["platform_job_id"]),
    ).fetchone()

    fields = (
        job.get("url", ""),
        job.get("company", ""),
        job.get("company_norm"),
        job.get("title", ""),
        job.get("title_norm"),
        job.get("category"),
        job.get("location"),
        job.get("employment_type"),
        job.get("experience_req"),
        job.get("education_req"),
        job.get("salary"),
        job.get("deadline"),
        job.get("posted_at"),
        job.get("description", ""),
        job.get("image_url"),
        job.get("image_path"),
        json.dumps(job.get("raw", {}), ensure_ascii=False),
        job.get("content_hash"),
        job.get("canonical_key"),
    )

    if row is None:
        cur = conn.execute(
            """INSERT INTO jobs
               (platform, platform_job_id, url, company, company_norm, title, title_norm,
                category, location, employment_type, experience_req, education_req,
                salary, deadline, posted_at, description, image_url, image_path,
                raw_json, content_hash, canonical_key, first_seen_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job["platform"], job["platform_job_id"], *fields, ts, ts),
        )
        return cur.lastrowid, "inserted"

    conn.execute(
        """UPDATE jobs SET url=?, company=?, company_norm=?, title=?, title_norm=?,
             category=?, location=?, employment_type=?, experience_req=?, education_req=?,
             salary=?, deadline=?, posted_at=?, description=?, image_url=?, image_path=?,
             raw_json=?, content_hash=?, canonical_key=?, last_seen_at=?
           WHERE id=?""",
        (*fields, ts, row["id"]),
    )
    return row["id"], "updated"


def save_screening(conn: sqlite3.Connection, job_id: int, result: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO screening
             (job_id, verdict, exclude_code, exclude_label, exclude_hits,
              track, track_label, fit_score, score_detail, screened_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             verdict=excluded.verdict, exclude_code=excluded.exclude_code,
             exclude_label=excluded.exclude_label, exclude_hits=excluded.exclude_hits,
             track=excluded.track, track_label=excluded.track_label,
             fit_score=excluded.fit_score, score_detail=excluded.score_detail,
             screened_at=excluded.screened_at""",
        (
            job_id,
            result["verdict"],
            result.get("exclude_code"),
            result.get("exclude_label"),
            json.dumps(result.get("exclude_hits", []), ensure_ascii=False),
            result.get("track"),
            result.get("track_label"),
            result.get("fit_score", 0),
            json.dumps(result.get("score_detail", {}), ensure_ascii=False),
            now(),
        ),
    )


def save_applicability(conn: sqlite3.Connection, job_id: int, result: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO applicability
             (job_id, actionable, channel, apply_url, blockers, requires,
              confidence, evaluated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             actionable=excluded.actionable, channel=excluded.channel,
             apply_url=excluded.apply_url, blockers=excluded.blockers,
             requires=excluded.requires, confidence=excluded.confidence,
             evaluated_at=excluded.evaluated_at""",
        (
            job_id,
            1 if result.get("actionable") else 0,
            result.get("channel"),
            result.get("apply_url"),
            json.dumps(result.get("blockers", []), ensure_ascii=False),
            json.dumps(result.get("requires", {}), ensure_ascii=False),
            float(result.get("confidence", 0.0)),
            now(),
        ),
    )


def start_run(conn: sqlite3.Connection, platform: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (platform, started_at) VALUES (?,?)", (platform, now())
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, **counts: Any) -> None:
    conn.execute(
        """UPDATE scrape_runs SET finished_at=?, found=?, inserted=?, updated=?,
             excluded=?, error=? WHERE id=?""",
        (
            now(),
            counts.get("found", 0),
            counts.get("inserted", 0),
            counts.get("updated", 0),
            counts.get("excluded", 0),
            counts.get("error"),
            run_id,
        ),
    )
    conn.commit()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, now()),
    )
    conn.commit()
