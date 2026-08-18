"""읽기 전용 조회 함수를 **실제로 부른다.**

`test_dispatch_routing.py`는 하위 호출을 전부 가짜로 바꾸므로 "cli가 올바른
함수를 부르는가"만 본다. 그 함수 **안에서** 죽는 것은 못 본다.

실제로 그렇게 새어나간 게 있었다. `_llm_cost`를 `llm.py`로 옮기면서 `connect`
import를 빠뜨렸는데(이 파일의 다른 함수들은 함수-지역 import를 쓴다), 라우팅
테스트는 `cost_report`를 통째로 가짜로 바꾸므로 초록불이었다. `cli.py llm-cost`를
손으로 돌려서야 `NameError`가 나왔다.

여기 있는 것은 전부 **SELECT만 하는 함수**다. 쓰기·전송·브라우저는 넣지 않는다 —
테스트가 폰으로 알림을 보내거나 원장을 건드리면 안 된다.
"""

from __future__ import annotations

import pytest

from src.autoapply import agent, assemble, health, llm, orchestrator
from src.autoapply.db import connect
from src.autoapply.workflows import context


def test_llm_cost_report_all():
    out = llm.cost_report(None)
    assert "최근_20건" in out


def test_llm_cost_report_one_job():
    out = llm.cost_report(1)
    assert out["job_id"] == 1
    assert "phases" in out or "안내" in out


def test_builds_log():
    rows = assemble.builds_log(2)
    assert isinstance(rows, list)
    for r in rows:
        assert "job_id" in r and "단계" in r


def test_recent_plans():
    conn = connect()
    try:
        rows = orchestrator.recent_plans(conn)
    finally:
        conn.close()
    assert isinstance(rows, list)
    for r in rows:
        assert {"id", "status", "risk"} <= set(r)


@pytest.mark.parametrize("fn", [agent.quota, agent.status, agent.blocked_summary])
def test_agent_reads(fn):
    assert isinstance(fn(), dict)


def test_health_runs_without_notifying():
    """`notify=False`가 아니면 이 테스트가 폰을 울린다."""
    out = health.run(notify=False)
    assert "findings" in out


def test_skip_if_already_applied_resolves_job_lookup():
    """지역변수 `job`이 모듈 함수 `job()`을 가리면 여기서 잡힌다.

    없는 공고라 SELECT 한 번 뒤 SystemExit로 끝난다 — preflight(브라우저)에는
    닿지 않는다. 이름이 다시 가려지면 UnboundLocalError가 나 실패한다.
    """
    with pytest.raises(SystemExit):
        context.skip_if_already_applied(999999)


def test_next_targets():
    conn = connect()
    try:
        rows = agent.next_targets(2, conn)
    finally:
        conn.close()
    assert isinstance(rows, list)
