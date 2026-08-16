"""지금 무엇이 돌고 있나, 그리고 어떻게 멈추나.

## 왜 필요한가

긴 작업은 전부 `subprocess.Popen`으로 띄운다 — 텔레그램 수신 루프를 막지
않으려는 것이다. 그 대가로 **아무도 서로를 모른다.** 수집을 한 번 시작하면
끝날 때까지 손댈 방법이 없었다(공고 800건 상세조회는 수 분~수십 분이다).
"멈춰"라고 말할 상대가 없었기 때문이다.

여기서 도는 작업을 표에 적는다. 그러면 두 가지가 된다:

    /running   지금 뭐가 도는지 본다
    /stop      멈춰달라고 **표시**한다

## 왜 kill이 아니라 표시인가

죽이면 브라우저를 반쯤 만진 상태로 끊긴다 — 이력서를 채우다 멈추면 절반만
채워진 이력서가 계정에 남고, 그건 안 만드느니만 못하다. 그래서 기본은
협조적 중단이다: 각 루프가 안전한 경계(공고 한 건이 끝난 자리)에서 표시를
보고 스스로 접는다. 표시를 봤는데도 안 멈추면 그때 `/stop 강제`로 신호를
보낸다.

## pid를 믿지 않는다

프로세스가 죽으면 `finished_at`을 못 적고 사라진다(kill -9, 맥 재부팅).
그래서 목록을 읽을 때마다 pid가 살아있는지 확인하고, 죽은 것은 그 자리에서
정리한다. 표를 믿는 게 아니라 표 + 실제 프로세스를 대조한다.
"""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .db import connect, now

log = logging.getLogger(__name__)

# 이 프로세스가 맡은 작업의 id. 등록 전에는 None이다.
_task_id: int | None = None
_last_check = 0.0
_last_answer = False


class Cancelled(RuntimeError):
    """사람이 중단을 요청했다. 고장이 아니다 — 여기까지 한 것은 그대로 둔다."""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def register(conn: sqlite3.Connection, kind: str, label: str = "") -> int:
    global _task_id
    cur = conn.execute(
        "INSERT INTO running_tasks (kind, label, pid, started_at) VALUES (?,?,?,?)",
        (kind, label, os.getpid(), now()),
    )
    conn.commit()
    _task_id = int(cur.lastrowid)
    return _task_id


def finish(conn: sqlite3.Connection, task_id: int, status: str = "done") -> None:
    conn.execute(
        "UPDATE running_tasks SET finished_at=?, status=? WHERE id=? AND finished_at IS NULL",
        (now(), status, task_id),
    )
    conn.commit()


@contextmanager
def running(kind: str, label: str = "") -> Iterator[int | None]:
    """이 블록이 도는 동안 '돌고 있음'으로 보이게 한다.

    등록 자체가 실패해도 작업은 계속한다 — 목록에 안 보이는 것이
    작업이 아예 안 도는 것보다 낫다.
    """
    global _task_id

    conn = None
    task_id = None
    try:
        conn = connect()
        task_id = register(conn, kind, label)
    except Exception as e:  # noqa: BLE001
        log.debug("작업 등록 실패(무시): %s", e)

    status = "done"
    try:
        yield task_id
    except Cancelled:
        status = "cancelled"
        raise
    except BaseException:
        status = "failed"
        raise
    finally:
        _task_id = None
        if conn is not None:
            try:
                if task_id is not None:
                    finish(conn, task_id, status)
            except Exception as e:  # noqa: BLE001
                log.debug("작업 종료 기록 실패(무시): %s", e)
            finally:
                conn.close()


def cancelled(*, min_interval: float = 2.0) -> bool:
    """내 작업에 중단 표시가 붙었나.

    루프 안에서 자주 불리므로 최소 간격을 둔다. 수집 루프는 공고 하나당 한 번
    부르는데, 그때마다 sqlite를 열면 확인 비용이 일보다 커진다.
    """
    global _last_check, _last_answer

    if _task_id is None:
        return False
    if _last_answer:
        return True  # 한 번 켜지면 안 꺼진다
    if time.monotonic() - _last_check < min_interval:
        return _last_answer

    _last_check = time.monotonic()
    try:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT cancel_at FROM running_tasks WHERE id=?", (_task_id,)
            ).fetchone()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log.debug("중단 표시 확인 실패(무시): %s", e)
        return False

    _last_answer = bool(row and row["cancel_at"])
    return _last_answer


def check(where: str = "") -> None:
    """중단 표시가 있으면 그 자리에서 접는다. **안전한 경계에서만** 부를 것."""
    if cancelled():
        raise Cancelled(f"사람이 중단을 요청했다{(' — ' + where) if where else ''}")


def active(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """지금 도는 작업. 죽은 채 남은 행은 여기서 정리한다."""
    rows = conn.execute(
        "SELECT * FROM running_tasks WHERE finished_at IS NULL ORDER BY id"
    ).fetchall()
    live, dead = [], []
    for r in rows:
        (live if _alive(r["pid"]) else dead).append(dict(r))
    if dead:
        conn.executemany(
            "UPDATE running_tasks SET finished_at=?, status='vanished' WHERE id=?",
            [(now(), d["id"]) for d in dead],
        )
        conn.commit()
        log.info("죽은 채 남아 있던 작업 %d건 정리", len(dead))
    return live


def select(conn: sqlite3.Connection, match: str = "") -> list[dict[str, Any]]:
    """중단 대상을 고른다. match가 비면 전부.

    고를 수 있어야 하는 이유: 수집(수십 분)과 지원준비는 같이 도는 일이 잦고,
    지원준비 하나를 접으려다 수집까지 날리면 다시 수십 분이다. 실제로 그렇게
    두 번 날렸다.

    id(숫자)와 종류(부분 문자열) 둘 다 받는다 — 폰에서 치기엔 '수집'이 편하고,
    같은 종류가 둘 돌 때는 id가 필요하다.
    """
    rows = active(conn)
    m = match.strip()
    if not m:
        return rows
    if m.isdigit():
        return [r for r in rows if r["id"] == int(m)]
    return [r for r in rows if m in (r["kind"] or "") or m in (r["label"] or "")]


def request_stop(
    conn: sqlite3.Connection, *, force: bool = False, match: str = ""
) -> list[dict[str, Any]]:
    """고른 작업에 중단을 요청한다. force면 신호까지 보낸다."""
    tasks = select(conn, match)
    if not tasks:
        return []
    conn.executemany(
        "UPDATE running_tasks SET cancel_at=? WHERE id=? AND cancel_at IS NULL",
        [(now(), t["id"]) for t in tasks],
    )
    conn.commit()

    for t in tasks:
        if not force:
            continue
        _terminate(int(t["pid"]), t["kind"])
    return tasks


def _terminate(pid: int, kind: str = "") -> None:
    """작업 하나를 끊는다. **프로세스 그룹째** 보낸다.

    자식까지 가야 하는 이유: 자기개선은 `claude` CLI를, 수집은 아무것도 안
    띄우지만 앞으로 늘 수 있다. 부모만 죽이면 자식이 고아로 남아 계속 돌고,
    그건 "멈췄다"고 말한 뒤에도 일이 진행되는 최악의 상태다.

    그룹으로 보내도 안전한 이유: 텔레그램 리스너가 띄우는 모든 작업은
    `start_new_session=True`로 자기 그룹을 갖는다. 그게 아니라면 리스너와
    같은 그룹이라 리스너까지 죽는다 — 그래서 **내 그룹이면 보내지 않는다.**

    SIGKILL은 안 쓴다. finally조차 못 돌면 브라우저 잠금 표식과 반쯤 만진
    이력서가 그대로 남는다.
    """
    try:
        pgid = os.getpgid(pid)
    except Exception:  # noqa: BLE001
        pgid = None

    if pgid is not None and pgid != os.getpgid(0) and pgid == pid:
        try:
            os.killpg(pgid, signal.SIGTERM)
            log.warning("작업 %s(pgid %s)에 SIGTERM — 자식까지", kind, pgid)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("killpg 실패 pgid %s: %s — 단일 pid로 재시도", pgid, e)

    try:
        os.kill(pid, signal.SIGTERM)
        log.warning("작업 %s(pid %s)에 SIGTERM", kind, pid)
    except Exception as e:  # noqa: BLE001
        log.warning("SIGTERM 실패 pid %s: %s", pid, e)


def describe(task: dict[str, Any]) -> str:
    from datetime import datetime

    mins = ""
    try:
        delta = datetime.now().astimezone() - datetime.fromisoformat(task["started_at"])
        mins = f" · {int(delta.total_seconds() // 60)}분째"
    except Exception:  # noqa: BLE001
        pass
    label = f" {task['label']}" if task.get("label") else ""
    flag = " (중단 요청됨)" if task.get("cancel_at") else ""
    return f"{task['kind']}{label} — pid {task['pid']}{mins}{flag}"
