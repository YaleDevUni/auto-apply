"""공통 HTTP 클라이언트. 레이트리밋 + 지수 백오프 재시도 + 브라우저 헤더.

v1에서 그대로 가져왔다. 세 플랫폼 상대로 검증된 코드라 손대지 않는다.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class Fetcher:
    """플랫폼 어댑터가 공유하는 요청기. 인스턴스 하나가 커넥션 풀 하나."""

    def __init__(self, delay: float = 0.7, timeout: float = 20.0, retries: int = 3):
        self.delay = delay
        self.retries = retries
        self._last_request = 0.0
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": UA,
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            },
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.monotonic()

    def get(self, url: str, **kwargs: Any) -> httpx.Response | None:
        last_err: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                resp = self.client.get(url, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"status {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                return resp
            except Exception as exc:  # noqa: BLE001 - 어떤 실패든 재시도 대상
                last_err = exc
                backoff = 1.5**attempt
                log.warning("GET 실패 (%s/%s) %s: %s", attempt + 1, self.retries, url, exc)
                time.sleep(backoff)
        log.error("GET 최종 실패 %s: %s", url, last_err)
        return None

    def get_json(self, url: str, **kwargs: Any) -> Any | None:
        resp = self.get(url, **kwargs)
        if resp is None:
            return None
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            log.error("JSON 파싱 실패 %s: %s", url, exc)
            return None

    def head(self, url: str, **kwargs: Any) -> httpx.Response | None:
        """리다이렉트 종착지 확인용. 외부 ATS 판별에 쓴다."""
        self._throttle()
        try:
            return self.client.head(url, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("HEAD 실패 %s: %s", url, exc)
            return None

    def download(self, url: str, dest) -> bool:
        resp = self.get(url)
        if resp is None:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
