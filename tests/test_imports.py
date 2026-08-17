"""모든 모듈이 import되는가 — 그리고 순환 참조가 새 경로로 터지지 않는가.

이 저장소는 함수-지역(lazy) import가 많다. `cli.py`에만 8곳이고,
`orchestrator.py`와 `notify/listener.py`는 **서로를** 지역 import한다:

    orchestrator.py:528,727  →  from .notify.listener import hold_for_fix, release_fix_hold
    notify/listener.py:540,558  →  from .. import orchestrator

이건 우연이 아니라 순환 의존이 실재한다는 증거다. 리팩터링으로 코드를 새 모듈에
옮기면서 무심코 top-level import로 끌어올리면 그 순환이 **새 경로로 터진다.**
그리고 지역 import는 그 코드가 실행될 때까지 아무것도 안 알려준다 — 새벽 3시에야
드러난다.

여기서는 각 모듈을 **깨끗한 프로세스에서 하나씩** import한다. 한 프로세스에서
전부 import하면 앞서 로드된 모듈이 순환을 가려준다(sys.modules 캐시가 두 번째
방문을 그냥 통과시킨다).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "autoapply"


def _module_names() -> list[str]:
    names = []
    for path in sorted(PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(parts))
    return names


MODULES = _module_names()


def test_found_the_package():
    """경로 계산이 틀리면 0개를 import하고 전부 통과한다 — 그게 최악이다."""
    assert len(MODULES) >= 25, f"모듈을 {len(MODULES)}개만 찾았다 — 경로 계산이 틀렸다"
    assert "src.autoapply.cli" not in MODULES  # cli.py는 루트에 있다
    for expected in ("src.autoapply.db", "src.autoapply.agent", "src.autoapply.orchestrator",
                     "src.autoapply.notify.listener", "src.autoapply.runner.session"):
        assert expected in MODULES


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_alone(module):
    """깨끗한 프로세스에서 이 모듈만 import한다."""
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"{module} import 실패:\n{proc.stderr[-2000:]}"


def test_cli_imports_alone():
    proc = subprocess.run(
        [sys.executable, "-c", "import cli"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"cli.py import 실패:\n{proc.stderr[-2000:]}"


def test_circular_pair_stays_lazy():
    """`orchestrator` ↔ `listener` 순환이 아직 지역 import로 격리돼 있는가.

    둘 중 하나라도 top-level import로 올라오면 이 테스트가 먼저 알려준다.
    고치는 방법은 "지역 import로 되돌린다"가 아니라 "순환을 실제로 끊는다"이지만,
    그건 이번 회차의 범위가 아니라 NEXT.md에 있다.
    """
    orch = (PKG / "orchestrator.py").read_text(encoding="utf-8")
    listener = (PKG / "notify" / "listener.py").read_text(encoding="utf-8")

    for name, src, needle in [
        ("orchestrator.py", orch, "from .notify.listener import"),
        ("notify/listener.py", listener, "from .. import orchestrator"),
    ]:
        for line in src.splitlines():
            if needle in line:
                assert line.startswith((" ", "\t")), (
                    f"{name}의 `{needle}`가 top-level로 올라왔다 — "
                    f"순환 import가 터질 자리다"
                )
