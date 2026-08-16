"""브라우저는 한 번에 한 작업만 쓴다.

## 왜 필요한가 (2026-08-16 실측)

상주 창을 쓰기 시작하면서 브라우저가 **공유 자원**이 됐다. 예전에는 실행마다
자기 창을 띄웠으니 서로 몰라도 됐는데, 지금은 모든 실행이 같은 창의 같은 탭에
붙는다. 그런데 이 저장소는 긴 작업을 전부 `subprocess.Popen`으로 fire-and-forget
한다(텔레그램 리스너의 `/apply`, 승인 버튼, `/improve`, 재작성, launchd의
`night-cycle`·`scrape`). 서로의 존재를 아무도 모른다.

그래서 수집이 도는 중에 폰에서 지원 버튼을 누르면, 뒤에 온 쪽이 같은 탭을
가져가 다른 URL로 이동시킨다. 앞의 작업은 자기가 보던 화면이 사라진 채로
셀렉터를 기다리다 타임아웃으로 죽는다 — 실패 메시지는 "요소를 못 찾음"이라
진짜 원인(다른 프로세스가 탭을 몰고 갔다)이 어디에도 안 남는다.

## 어떻게 막나

파일 잠금(flock) 하나로 직렬화한다. 잠금은 **프로세스 밖에서** 유지돼야 한다 —
경쟁하는 쪽이 스레드가 아니라 별개의 프로세스라 파이썬 안의 락으로는 안 된다.

기다리는 시간은 짧게 잡고(기본 30초), 못 잡으면 `BrowserBusy`로 **빨리 실패**
한다. 오래 기다리게 하면 폰에서 버튼을 누른 사람은 아무 응답 없는 몇 분을 보게
되고, 그 사이 또 누른다. 대신 누가 무엇을 잡고 있는지 실패 메시지에 담아
"지금 수집 중이니 끝나고 다시 누르세요"라고 말할 수 있게 한다.

같은 프로세스 안에서는 재진입해도 된다(`_depth`). `_autoapply`처럼 이력서 등록과
지원 실행을 **한 덩어리로** 잡아야 하는 흐름이 있어서다 — 그 둘 사이를 남에게
내주면 이력서를 만들어놓고 다른 작업이 탭을 가져간 뒤에 지원이 도는 꼴이 된다.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from ..paths import PROFILE_DIR

log = logging.getLogger(__name__)

LOCK_PATH = PROFILE_DIR / ".browser.lock"
DEFAULT_WAIT_SEC = 30

_fd: int | None = None
_depth = 0


class BrowserBusy(RuntimeError):
    """다른 작업이 브라우저를 쓰는 중이다. 재시도로 넘을 수 있는 실패다."""

    def __init__(self, holder: dict[str, Any]):
        self.holder = holder
        super().__init__(describe(holder))


def _read_holder() -> dict[str, Any]:
    """잠금을 쥔 쪽이 남긴 표식. 못 읽어도 흐름을 막지 않는다."""
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8") or "{}")
    except Exception:  # noqa: BLE001
        return {}


def describe(holder: dict[str, Any]) -> str:
    kind = holder.get("kind") or "알 수 없는 작업"
    label = holder.get("label") or ""
    pid = holder.get("pid")
    since = holder.get("since") or ""
    mins = ""
    try:
        delta = datetime.now().astimezone() - datetime.fromisoformat(since)
        mins = f" · {int(delta.total_seconds() // 60)}분째"
    except Exception:  # noqa: BLE001
        pass
    return f"{kind}{(' ' + label) if label else ''} (pid {pid}{mins})"


def holder() -> dict[str, Any]:
    """지금 브라우저를 쓰는 작업. 아무도 안 쓰면 빈 dict.

    잠금을 시험 삼아 잡아본다(비차단). 잡히면 아무도 안 쓰는 것이다 —
    표식 파일만 읽으면 비정상 종료로 남은 옛 표식을 현재 사용자로 착각한다.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return {}
    except OSError:
        return _read_holder()
    finally:
        os.close(fd)


@contextmanager
def browser_lock(
    kind: str, *, label: str = "", wait_sec: int | None = None
) -> Iterator[None]:
    """브라우저를 쓰는 동안 잡는다. 못 잡으면 `BrowserBusy`."""
    global _fd, _depth
    import time

    if _depth > 0:  # 같은 프로세스 안 재진입 — 이미 우리가 쥐고 있다
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
        return

    if wait_sec is None:
        wait_sec = _configured_wait()

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(0, wait_sec)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                other = _read_holder()
                os.close(fd)
                log.warning("브라우저가 사용 중이라 시작하지 못했다 — %s", describe(other))
                raise BrowserBusy(other) from None
            time.sleep(1)

    mark = {
        "pid": os.getpid(),
        "kind": kind,
        "label": label,
        "since": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(mark, ensure_ascii=False).encode())
        os.fsync(fd)
    except Exception as e:  # noqa: BLE001
        log.debug("잠금 표식 기록 실패(무시): %s", e)

    _fd, _depth = fd, 1
    log.info("브라우저 잠금 획득 — %s", describe(mark))
    try:
        yield
    finally:
        _depth -= 1
        try:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception as e:  # noqa: BLE001
            log.debug("잠금 해제 중 무시된 오류: %s", e)
        os.close(fd)
        _fd = None


def _configured_wait() -> int:
    try:
        from ..config import effective_config

        return int((effective_config().get("browser") or {}).get("lock_wait_sec", DEFAULT_WAIT_SEC))
    except Exception:  # noqa: BLE001
        return DEFAULT_WAIT_SEC
