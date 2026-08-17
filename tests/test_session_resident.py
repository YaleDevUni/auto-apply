"""상주 브라우저 홀더 프로세스 pid 추적·정리.

실측(2026-08-18): CDP_PORT를 죽여도(_kill_resident) 그 부모인 파이썬 홀더
(`while True: sleep(3600)`)는 별도 pid라 살아남는다 — 확인 시점에 일요일
오후부터 쌓인 홀더가 8개, 전부 크롬은 죽고 Playwright driver만 매달려 있었다.
이 테스트는 실제 서브프로세스를 띄우지 않고 subprocess.Popen/run과
_cmdline()만 가짜로 바꿔 그 짝짓기·정리 로직만 본다.
"""

from __future__ import annotations

import subprocess

import pytest

from src.autoapply.runner import session


@pytest.fixture(autouse=True)
def _holder_file(tmp_path, monkeypatch):
    """모듈 전역 pid 파일을 테스트마다 새 임시 경로로 돌린다."""
    path = tmp_path / ".browser_holder_pid"
    monkeypatch.setattr(session, "_HOLDER_PID_FILE", path)
    return path


def test_remember_and_read_holder_roundtrip(_holder_file):
    session._remember_holder(12345)
    assert _holder_file.read_text(encoding="utf-8") == "12345"
    assert session._remembered_holder() == 12345


def test_remembered_holder_missing_file_returns_zero():
    assert session._remembered_holder() == 0


def test_spawn_resident_records_holder_pid_even_when_wait_fails(monkeypatch, _holder_file):
    """대기 루프가 실패해 False를 반환해도, 그 시점에 이미 살아있는
    홀더의 pid는 기록돼 있어야 한다 — 안 그러면 그 홀더는 아무도 못 죽인다.
    """

    class FakeProc:
        pid = 99999

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(session, "resident_owner", lambda: ("none", 0, ""))

    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_: None)

    ok = session._spawn_resident(wait_sec=1)

    assert ok is False
    assert session._remembered_holder() == 99999


def test_kill_holder_kills_when_marker_matches(monkeypatch, _holder_file):
    session._remember_holder(555)
    monkeypatch.setattr(session, "_cmdline", lambda pid: f"python -c ...{session._HOLDER_MARKER}...")
    killed = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: killed.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    session._kill_holder()

    assert killed == [["kill", "555"]]
    assert not _holder_file.exists()  # 처리 후 표시는 지운다


def test_kill_holder_skips_when_pid_was_recycled(monkeypatch, _holder_file):
    """pid가 재활용돼 우리 마커가 없는 무관한 프로세스면 죽이지 않는다."""
    session._remember_holder(555)
    monkeypatch.setattr(session, "_cmdline", lambda pid: "/usr/bin/some-other-daemon")
    killed = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: killed.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    session._kill_holder()

    assert killed == []
    assert not _holder_file.exists()  # 확인은 끝났으니 낡은 표시는 지운다


def test_kill_holder_noop_when_no_pid_recorded(monkeypatch, _holder_file):
    killed = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: killed.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    session._kill_holder()  # 파일 자체가 없다

    assert killed == []


def test_kill_resident_kills_both_chrome_and_holder_when_ours(monkeypatch, tmp_path, _holder_file):
    session._remember_holder(555)
    monkeypatch.setattr(session, "_TAB_FILE", tmp_path / ".browser_tab")  # 실제 프로필 경로를 안 건드림
    monkeypatch.setattr(session, "resident_owner", lambda: ("ours", 111, "chrome --user-data-dir=x"))
    monkeypatch.setattr(session, "_cmdline", lambda pid: session._HOLDER_MARKER)
    killed = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: killed.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    session._kill_resident()

    assert ["kill", "111"] in killed  # 크롬
    assert ["kill", "555"] in killed  # 홀더


def test_kill_resident_still_cleans_up_holder_when_foreign(monkeypatch, _holder_file):
    """포트를 남이 잡고 있어 크롬은 못 죽여도, 우리 홀더가 남아있으면 정리는 한다."""
    session._remember_holder(555)
    monkeypatch.setattr(session, "resident_owner", lambda: ("foreign", 222, "other-chrome"))
    monkeypatch.setattr(session, "_cmdline", lambda pid: session._HOLDER_MARKER)
    killed = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: killed.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    session._kill_resident()

    assert killed == [["kill", "555"]]  # 크롬(222)은 안 건드리고 홀더만
