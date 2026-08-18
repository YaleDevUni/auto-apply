"""모든 모듈이 import되는가 — 그리고 순환 참조가 새 경로로 터지지 않는가.

이 저장소는 함수-지역(lazy) import가 많다(`cli.py`에만 8곳). `orchestrator.py`와
`notify/listener.py`는 예전엔 **서로를** 지역 import해서 순환을 이뤘는데(자가복구
보류 상태를 listener가 갖고 orchestrator가 빌려 쓰고, listener의 /reverts·/revert가
거꾸로 orchestrator를 빌려 쓰는 구조), 그 상태(`FIX_HOLD_KEY`/`hold_for_fix`/
`release_fix_hold`/`fix_hold`)를 orchestrator.py로 옮겨 순환 자체를 없앴다 —
이제 listener → orchestrator 한 방향뿐이다(`test_orchestrator_listener_cycle_is_gone`).

지역 import 자체는 여전히 저장소 곳곳에 있고, 그건 그 코드가 실행될 때까지
아무것도 안 알려준다는 위험이 그대로다 — 새벽 3시에야 드러난다.

여기서는 각 모듈을 **깨끗한 프로세스에서 하나씩** import한다. 한 프로세스에서
전부 import하면 앞서 로드된 모듈이 순환을 가려준다(sys.modules 캐시가 두 번째
방문을 그냥 통과시킨다).
"""

from __future__ import annotations

import ast
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


def test_orchestrator_listener_cycle_is_gone():
    """`orchestrator` ↔ `notify.listener` 순환 참조가 실제로 끊겼는가.

    자가복구 보류 상태를 orchestrator.py로 옮겨서 listener → orchestrator
    한 방향만 남았다. 되돌아가지 않도록 두 가지를 지킨다: orchestrator.py가
    notify.listener를 (top-level이든 지역이든) 다시 참조하지 않는다, 그리고
    두 모듈을 어느 순서로 먼저 import해도 — 순환이 있었다면 순서에 따라
    ImportError가 나거나 부분 초기화된 모듈이 잡힌다 — 깨끗한 프로세스에서
    죽지 않는다.
    """
    orch = (PKG / "orchestrator.py").read_text(encoding="utf-8")
    assert "notify.listener" not in orch and "notify import listener" not in orch, (
        "orchestrator.py가 다시 notify.listener를 참조한다 — 순환이 되돌아왔다"
    )

    for order in (
        "import autoapply.orchestrator; import autoapply.notify.listener",
        "import autoapply.notify.listener; import autoapply.orchestrator",
    ):
        proc = subprocess.run(
            [sys.executable, "-c", order], cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, f"`{order}` 실패:\n{proc.stderr[-2000:]}"


def test_prepare_application_calls_qualified_apply_with():
    """`workflows/prepare_application.py`가 `apply_with`를 맨 이름으로 부르지 않는가.

    실측(2026-08-18): `cli.py`에서 workflows/로 옮기며(7b05c73) `submit_application.
    apply_with(...)`가 `apply_with(...)`로 남았다 — `submit_application`만 모듈
    수준으로 import돼 있어 파이썬은 이걸 미정의 전역으로 본다. import 시점에는
    안 죽고(함수 본문 안이라 지연 바인딩) **실제 지원준비가 이력서 등록까지
    끝난 뒤 지원 폼 진입 직전**에야 `NameError: name 'apply_with' is not defined`로
    죽는다 — 실제 night-cycle 실행(공고 35·37·48)에서 매번 그 자리에서
    재현했다. import 테스트(`test_module_imports_alone`)로는 못 잡는 부류다.

    AST로 `run()` 함수 안의 `Call` 노드를 훑어 이름이 그냥 `apply_with`인
    호출이 없는지(반드시 `submit_application.apply_with`처럼 속성 접근이어야
    한다) 확인한다. 브라우저·DB·LLM에 전혀 닿지 않는 순수 정적 검사다.
    """
    src = (PKG / "workflows" / "prepare_application.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    run_fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run"
    )
    bare_calls = [
        n.func.id for n in ast.walk(run_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "apply_with"
    ]
    assert not bare_calls, (
        "prepare_application.run()이 apply_with를 맨 이름으로 부른다 — "
        "submit_application.apply_with(...)로 한정해야 한다 (NameError 재발)"
    )
