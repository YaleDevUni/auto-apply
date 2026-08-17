"""CLI 표면은 프로세스 경계 계약이다 — 깨지면 자동 지원이 조용히 멈춘다.

`cli.py`는 사람만 부르는 게 아니다. 네 곳이 **문자열로** 부른다:

    schedule/run.sh:47,52,56,59,63,72     launchd 새벽/정오 잡
    schedule/listen.sh:41                  상주 리스너
    notify/listener.py:358,458,579,601     폰 명령 → subprocess spawn
    orchestrator.py:96,475,482             자가개선 검증 허용목록 (shell=False)

이 중 어느 하나가 파싱에 실패하면 종료코드만 남고 **아무도 안 본다** — 리스너가
6시간 죽어 있었는데 로그가 0바이트였던 2026-08-17 사고가 정확히 그 모양이었다.

그래서 이 파일은 `cli.py` 내부를 어디로 옮기든 **한 줄도 안 바뀌어야 한다.**
바뀌어야 한다면 그건 외부 계약이 깨졌다는 뜻이고, 그때는 부르는 쪽 네 곳을
같이 고쳐야 한다.

실제 진입 경로(`sys.argv` → `main()` → Namespace)를 그대로 탄다. 파서만 따로
꺼내 검사하면 `main()`이 argv를 다루는 방식이 바뀌어도 안 걸린다.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager

import pytest

import cli


@pytest.fixture
def parse(monkeypatch):
    """argv를 넣으면 `_dispatch`가 받았을 Namespace를 돌려준다. 실행은 안 한다."""
    seen: dict = {}

    def fake_dispatch(args):
        seen["args"] = args
        return 0

    @contextmanager
    def fake_running(kind, label=""):
        # 진짜 tasks.running은 DB에 running_tasks 행을 만든다. 테스트가
        # 운영 DB를 건드리면 폰의 /running 목록이 쓰레기로 찬다.
        seen["kind"] = kind
        seen["label"] = label
        yield None

    monkeypatch.setattr(cli, "_dispatch", fake_dispatch)
    monkeypatch.setattr(cli.tasks, "running", fake_running)

    def run(*argv: str) -> dict:
        monkeypatch.setattr(sys, "argv", ["cli.py", *argv])
        assert cli.main() == 0
        return seen

    return run


# ── 외부가 실제로 부르는 명령줄 ──────────────────────────────────────────
# 근거를 주석에 남긴다. 이 표에서 지우려면 부르는 쪽을 먼저 고쳐야 한다.
EXTERNAL = [
    # schedule/run.sh
    (["listen"], {"cmd": "listen", "watch": False}),
    (["scrape", "--check-session"], {"cmd": "scrape", "check_session": True, "session": []}),
    (["night-cycle", "--target", "30", "--defer"],
     {"cmd": "night-cycle", "target": 30, "defer": True}),
    (["resumes", "--cleanup"], {"cmd": "resumes", "cleanup": True}),
    (["flush-notify"], {"cmd": "flush-notify"}),
    (["quota"], {"cmd": "quota"}),
    # schedule/listen.sh:41 — 상주 리스너. 이게 깨지면 폰 명령이 통째로 사라진다.
    (["listen", "--watch"], {"cmd": "listen", "watch": True}),
    # notify/listener.py:358-364 — instruction은 위치인자라 세 갈래 모두 채워 보낸다.
    # `cli.py guide`(인자 없이)는 exit 2다. 부르는 쪽이 그걸 알고 채우고 있다.
    (["guide", "-", "--clear-session"],
     {"cmd": "guide", "instruction": "-", "clear_session": True, "revert": False}),
    (["guide", "되돌리기", "--revert"],
     {"cmd": "guide", "instruction": "되돌리기", "revert": True, "clear_session": False}),
    (["guide", "§7-1에 규칙 추가"],
     {"cmd": "guide", "instruction": "§7-1에 규칙 추가", "revert": False, "clear_session": False}),
    (["night-cycle", "--target", "3"], {"cmd": "night-cycle", "target": 3, "defer": False}),
    (["plan", "--limit", "1"], {"cmd": "plan", "limit": 1, "list": False}),
    (["improve", "--limit", "1"], {"cmd": "improve", "limit": 1, "list": False}),
    # errors.py:373
    (["plan"], {"cmd": "plan", "limit": 1}),
    # orchestrator.py 검증 허용목록 (읽기·dry-run만)
    (["resume", "283"], {"cmd": "resume", "job_id": 283, "rounds": 2, "print": False}),
    (["apply", "283"], {"cmd": "apply", "job_id": 283, "live": False, "headless": False}),
    (["builds", "--limit", "10"], {"cmd": "builds", "limit": 10}),
    (["health", "--no-notify"], {"cmd": "health", "no_notify": True, "history": False}),
    (["blocked"], {"cmd": "blocked"}),
    (["status"], {"cmd": "status"}),
    (["targets"], {"cmd": "targets", "limit": 10}),
]


@pytest.mark.parametrize("argv,expected", EXTERNAL, ids=[" ".join(a) for a, _ in EXTERNAL])
def test_external_callers_still_parse(parse, argv, expected):
    args = parse(*argv)["args"]
    for key, want in expected.items():
        got = getattr(args, key.replace("-", "_"))
        assert got == want, f"{' '.join(argv)} → {key}={got!r}, 기대 {want!r}"


# ── 서브커맨드 전수 ──────────────────────────────────────────────────────
# 이름이 사라지거나 필수 인자가 늘면 여기서 걸린다.
ALL_COMMANDS = [
    (["scrape"], {}),
    (["session-check"], {"notify": False}),
    (["reevaluate"], {}),
    (["targets"], {}),
    (["blocked"], {}),
    (["quota"], {}),
    (["llm-cost"], {"job_id": None}),
    (["llm-cost", "7"], {"job_id": 7}),
    (["browser-open"], {}),
    (["browser-restart"], {}),
    (["revise", "7", "짧게"], {"job_id": 7, "feedback": "짧게"}),
    (["guide", "지시"], {"instruction": "지시", "revert": False, "clear_session": False}),
    (["revlog"], {"edit": None, "delete": None, "text": ""}),
    (["builds"], {"limit": 8}),
    (["health"], {}),
    (["status"], {}),
    (["where"], {}),
    (["telegram-setup", "TOKEN"], {"token": "TOKEN"}),
    (["telegram-commands"], {}),
    (["notify-login"], {}),
    (["listen"], {}),
    (["improve"], {"limit": 1}),
    (["plan"], {}),
    (["fix-run", "4"], {"plan_id": 4}),
    (["errors"], {"limit": 12}),
    (["plans"], {}),
    (["fix-revert", "abc123"], {"sha": "abc123"}),
    (["resumes"], {"cleanup": False}),
    (["browser-login"], {}),
    (["capture", "7"], {"job_id": 7, "click": []}),
    (["resume", "7"], {}),
    (["autoapply", "7"], {"job_id": 7, "resume_url": None, "live": False}),
    (["cycle-apply"], {"limit": 1, "defer": False}),
    (["flush-notify"], {}),
    (["night-cycle"], {"target": 30, "defer": False}),
    (["submit", "7"], {"job_id": 7}),
    (["apply", "7"], {}),
]


@pytest.mark.parametrize("argv,expected", ALL_COMMANDS, ids=[" ".join(a) for a, _ in ALL_COMMANDS])
def test_every_subcommand_parses(parse, argv, expected):
    args = parse(*argv)["args"]
    assert args.cmd == argv[0]
    for key, want in expected.items():
        assert getattr(args, key) == want, f"{' '.join(argv)} → {key}"


@pytest.mark.parametrize("cmd", ["apply", "autoapply"])
def test_live_is_never_the_default(parse, cmd):
    """제출은 되돌릴 수 없다(§7.2). `--live`를 안 주면 실제로 나가면 안 된다."""
    assert parse(cmd, "7")["args"].live is False, f"{cmd}의 기본이 live다 — 제출 경계가 깨졌다"
    assert parse(cmd, "7", "--live")["args"].live is True, f"{cmd} --live가 안 먹는다"


# ── /running·/stop 대상 목록 ────────────────────────────────────────────
def test_long_running_commands_are_registered():
    """폰에서 /running으로 보이고 /stop으로 멈출 수 있는 대상.

    여기서 빠지면 그 작업은 **폰에서 멈출 방법이 없다.** 조회 명령을 넣으면
    목록이 순식간에 차서 정작 볼 것을 못 본다 — 양쪽 다 고장이다.
    """
    assert cli._TASK_KINDS == {
        "scrape": "공고수집",
        "night-cycle": "지원준비",
        "cycle-apply": "지원준비",
        "autoapply": "지원준비",
        "submit": "제출",
        "apply": "지원 폼 실행",
        "improve": "계획수립",
        "plan": "계획수립",
        "fix-run": "자가복구",
        "revise": "이력서 재작성",
        "resumes": "이력서 정리",
        "reevaluate": "재판정",
    }


@pytest.mark.parametrize("argv,kind,label", [
    (["night-cycle", "--target", "30"], "지원준비", "목표 30건"),
    (["autoapply", "7"], "지원준비", "공고 7"),
    (["apply", "7"], "지원 폼 실행", "공고 7"),
    (["scrape", "--platform", "wanted"], "공고수집", "wanted"),
    (["submit", "7"], "제출", "공고 7"),
])
def test_task_label_shown_on_phone(parse, argv, kind, label):
    """폰에 뜨는 이름. job_id > target > platform 순으로 고른다."""
    seen = parse(*argv)
    assert seen["kind"] == kind
    assert seen["label"] == label


def test_short_commands_are_not_registered(parse):
    """조회 명령은 '지금 도는 작업'에 안 들어간다."""
    seen = parse("status")
    assert "kind" not in seen
