"""`_dispatch`가 각 명령을 올바른 함수에 올바른 인자로 넘기는지.

**이 파일의 용도는 이동(refactoring) 중의 오배선 탐지다.** cli.py에서 workflow를
`src/autoapply/workflows/`로 옮길 때 가장 흔한 사고가 두 가지다:

    1. 인자 순서/이름이 슬쩍 바뀐다 (`defer`가 positional로 넘어가는 등)
    2. 옮긴 뒤 cli.py가 옛 함수를 계속 부른다 (또는 새 함수를 안 부른다)

둘 다 dry-run 한 번으로는 안 드러나고 새벽에 드러난다.

`test_cli_surface.py`와 달리 **이 파일은 이동 커밋에서 patch 대상 이름이 바뀐다.**
그건 정상이다 — 바뀌는 것이 한 줄(patch 대상)뿐이고 단정(무슨 인자로 불렸나)은
그대로여야 한다. 단정까지 바꿔야 한다면 그건 이동이 아니라 동작 변경이다.
"""

from __future__ import annotations

import argparse

import pytest

import cli


@pytest.fixture
def spy(monkeypatch, capsys):
    """`cli.<name>`을 기록용 가짜로 바꾸고, 호출 인자를 돌려준다."""
    calls: list[tuple] = []

    def install(name: str, *, returns=None):
        def fake(*args, **kwargs):
            calls.append((args, kwargs))
            return returns if returns is not None else {"fake": name}

        monkeypatch.setattr(cli, name, fake)

    def dispatch(**namespace_kwargs) -> None:
        cli._dispatch(argparse.Namespace(**namespace_kwargs))
        capsys.readouterr()  # _out()의 JSON은 여기서 볼 게 아니다

    install.dispatch = dispatch
    install.calls = calls
    return install


# ── Step 1~4에서 옮겨지는 함수들 ────────────────────────────────────────
# 이동 후에는 patch 대상이 workflows/notify 쪽으로 바뀐다.

def test_night_cycle(spy):
    """run.sh와 폰 /apply가 둘 다 이 경로로 온다. defer가 키워드여야 한다."""
    spy("_night_cycle")
    spy.dispatch(cmd="night-cycle", target=30, defer=True)
    assert spy.calls == [((30,), {"defer": True})]


def test_cycle_apply(spy):
    spy("_cycle_apply")
    spy.dispatch(cmd="cycle-apply", limit=1, defer=False)
    assert spy.calls == [((1,), {"defer": False})]


def test_autoapply_passes_live_through(spy):
    """`live`가 키워드로 안 넘어가면 dry-run이 실제 제출이 된다."""
    spy("_autoapply")
    spy.dispatch(cmd="autoapply", job_id=7, resume_url=None, live=False)
    assert spy.calls == [((7,), {"resume_url": None, "live": False})]


def test_autoapply_live_true(spy):
    spy("_autoapply")
    spy.dispatch(cmd="autoapply", job_id=7, resume_url="https://x", live=True)
    assert spy.calls == [((7,), {"resume_url": "https://x", "live": True})]


def test_apply_passes_live_and_headless(spy):
    spy("_apply")
    spy.dispatch(cmd="apply", job_id=7, live=False, headless=False)
    assert spy.calls == [((7,), {"live": False, "headless": False})]


def test_submit(spy):
    spy("_submit")
    spy.dispatch(cmd="submit", job_id=7)
    assert spy.calls == [((7,), {})]


def test_flush_notify(monkeypatch, capsys):
    # Step "report" 이동: cli._flush_notifications → notify.report.flush
    calls = []
    monkeypatch.setattr(cli.report, "flush", lambda *a, **k: calls.append((a, k)) or {})
    cli._dispatch(argparse.Namespace(cmd="flush-notify"))
    capsys.readouterr()
    assert calls == [((), {})]


def test_revise(spy):
    spy("_revise")
    spy.dispatch(cmd="revise", job_id=7, feedback="짧게")
    assert spy.calls == [((7, "짧게"), {})]


def test_llm_cost(spy):
    spy("_llm_cost", returns={})
    spy.dispatch(cmd="llm-cost", job_id=None)
    assert spy.calls == [((None,), {})]


def test_browser_open(spy):
    spy("_browser_open")
    spy.dispatch(cmd="browser-open")
    assert spy.calls == [((), {})]


def test_guide(spy):
    spy("_guide")
    spy.dispatch(cmd="guide", instruction="지시", revert=False, clear_session=False)
    assert spy.calls == [(("지시",), {"revert": False, "clear_session": False})]


def test_revlog(spy):
    spy("_revlog")
    spy.dispatch(cmd="revlog", edit=None, delete=3, text="")
    assert spy.calls == [((), {"edit": None, "delete": 3, "text": ""})]


# ── 인프라로 그냥 위임하는 것들 (이동 대상 아님) ────────────────────────
# 여기가 깨지면 이동이 아니라 배선을 잘못 건드린 것이다.

@pytest.mark.parametrize("cmd,target,namespace,expected", [
    ("targets", "next_targets", {"limit": 5}, ((5,), {})),
    ("blocked", "blocked_summary", {}, ((), {})),
    ("quota", "quota", {}, ((), {})),
    ("status", "status", {}, ((), {})),
    ("notify-login", "notify_login_required", {}, ((), {})),
])
def test_agent_delegation(monkeypatch, capsys, cmd, target, namespace, expected):
    calls = []
    monkeypatch.setattr(cli.agent, target,
                        lambda *a, **k: calls.append((a, k)) or {"fake": target})
    cli._dispatch(argparse.Namespace(cmd=cmd, **namespace))
    capsys.readouterr()
    assert calls == [expected]


def test_reevaluate_delegates_to_pipeline(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli.pipeline, "reevaluate",
                        lambda *a, **k: calls.append((a, k)) or {})
    cli._dispatch(argparse.Namespace(cmd="reevaluate"))
    capsys.readouterr()
    assert calls == [((), {})]


def test_scrape_runs_pipeline_then_health(monkeypatch, capsys):
    """수집 뒤 건강 판정이 붙는다 — 단, 중단된 수집으로는 판정하지 않는다."""
    from src.autoapply import health

    order = []
    monkeypatch.setattr(cli.pipeline, "run_all",
                        lambda *a, **k: order.append("scrape") or [{"stopped": False}])
    monkeypatch.setattr(cli.agent, "notify_login_required", lambda: order.append("notify"))
    monkeypatch.setattr(health, "run", lambda **k: order.append("health") or {"findings": []})

    cli._dispatch(argparse.Namespace(cmd="scrape", platform=None, session=[], check_session=False))
    capsys.readouterr()
    assert order == ["scrape", "notify", "health"]


def test_stopped_scrape_skips_health(monkeypatch, capsys):
    """실측(2026-08-16): 22건에서 멈춘 수집이 '망가졌다'고 폰에 헛경보를 울렸다."""
    from src.autoapply import health

    order = []
    monkeypatch.setattr(cli.pipeline, "run_all",
                        lambda *a, **k: order.append("scrape") or [{"stopped": True}])
    monkeypatch.setattr(cli.agent, "notify_login_required", lambda: None)
    monkeypatch.setattr(health, "run", lambda **k: order.append("health") or {"findings": []})

    cli._dispatch(argparse.Namespace(cmd="scrape", platform=None, session=[], check_session=False))
    capsys.readouterr()
    assert "health" not in order, "중단된 수집으로 건강을 판정했다 — 헛경보가 나간다"


def test_dispatch_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(cli.agent, "status", lambda: {})
    assert cli._dispatch(argparse.Namespace(cmd="status")) == 0
    capsys.readouterr()
