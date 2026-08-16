"""Claude Code CLI를 서브프로세스로 호출한다. 별도 API 키 없이 기존 구독으로 동작.

## 왜 플래그를 잔뜩 붙이는가

claude CLI는 **코딩 에이전트 하네스**다. 아무 옵션 없이 부르면 매 호출마다
시스템 프롬프트(약 11k 토큰)·툴 정의·사용자 MCP 서버·플러그인·설정이 전부
얹힌다. 우리가 필요한 건 텍스트 생성 한 번뿐이므로 전부 떼어낸다.
v1에서 실측으로 입력 16,498 → 2,577 토큰(84% 감소)이 확인된 구성이다.

    --system-prompt        하네스 시스템 프롬프트를 작업용으로 통째 교체
    --strict-mcp-config    사용자가 붙여둔 MCP 서버 무시
    --mcp-config {}        빈 MCP 설정
    --setting-sources ""   사용자/프로젝트 설정·플러그인·스킬 로딩 안 함
    --allowed-tools ""     도구 없이 순수 텍스트 생성 (이미지 공고만 Read 허용)
    MAX_THINKING_TOKENS    확장 사고 예산

이미지형 공고(자소설닷컴에 많음)는 저장된 이미지 경로를 프롬프트에 넣고 Read
도구만 허용해 Claude가 직접 읽게 한다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .config import effective_config
from .paths import ASSET_DIR, INSTANCE_ROOT

log = logging.getLogger(__name__)

WRITER_SYSTEM_PROMPT = (
    "당신은 한국어 채용 서류를 대신 작성하는 전문 작성자입니다. "
    "사용자가 준 작성 규칙과 출력 형식을 정확히 지키고, "
    "요청받은 것 외에는 어떤 설명도 출력하지 마십시오."
)

# Claude Code 세션 안에서 자식 프로세스를 띄우면 이 변수들이 상속되어
# "nested session" 가드에 걸린다. 독립 프로세스이므로 걷어내고 실행한다.
_NESTING_VARS = ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT")


class ClaudeUnavailable(RuntimeError):
    pass


class UsageLimited(RuntimeError):
    """구독 사용 한도에 걸렸다. 재시도로 못 넘고, 시간이 지나야 풀린다.

    이걸 일반 오류와 구분하는 이유: 한도는 **고장이 아니다.** 실패로 기록하고
    자리를 붙잡아 두면 한도가 풀린 뒤에도 그 공고를 다시 못 건드린다.
    오케스트레이터는 이 예외를 받으면 자리를 놓아주고 조용히 멈춘 뒤,
    다음 스케줄 실행(새벽)에 이어서 한다.
    """


# 한도에 걸렸을 때 나오는 문구. **성공 응답에는 절대 없는 것만** 넣는다.
#
# 처음엔 "429"도 넣었다가 성공 응답을 한도로 오판했다 — 응답 JSON의 토큰 수나
# 소요시간 숫자에 그 문자열이 우연히 섞인다("duration_api_ms":31656 같은 곳).
# 그대로 뒀으면 오케스트레이터가 멀쩡한 작업을 영원히 큐로 되돌렸을 것이다.
_LIMIT_MARKERS = (
    "usage limit",
    "rate limit",
    "limit reached",
    "too many requests",
    "사용 한도",
    "한도에 도달",
)


def _raise_if_limited(text: str) -> None:
    """실패한 호출에서만 부른다. 성공 응답 본문을 훑으면 오탐이 난다."""
    low = text.lower()
    if any(m.lower() in low for m in _LIMIT_MARKERS):
        raise UsageLimited(text.strip()[:300])


def cli_available() -> bool:
    return shutil.which("claude") is not None


def _child_env(thinking_tokens: int) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _NESTING_VARS}
    env["MAX_THINKING_TOKENS"] = str(thinking_tokens)
    return env


def build_command(
    prompt: str,
    *,
    image_paths: list[Path] | None = None,
    model: str,
    system_prompt: str = WRITER_SYSTEM_PROMPT,
) -> list[str]:
    """실제로 실행되는 명령. 프롬프트 검증 도구가 이 함수를 그대로 쓴다."""
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--no-session-persistence",
        "--system-prompt", system_prompt,
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "",
    ]
    if image_paths:
        cmd += ["--allowed-tools", "Read", "--add-dir", str(ASSET_DIR)]
    else:
        cmd += ["--allowed-tools", ""]
    return cmd


def ask(
    prompt: str,
    *,
    image_paths: list[Path] | None = None,
    model: str | None = None,
    timeout: int | None = None,
    thinking_tokens: int | None = None,
    system_prompt: str = WRITER_SYSTEM_PROMPT,
) -> str:
    """프롬프트를 던지고 텍스트 응답을 받는다. 기본값은 config.yaml의 llm 섹션."""
    if not cli_available():
        raise ClaudeUnavailable(
            "claude CLI를 찾을 수 없습니다. Claude Code가 설치되어 있고 PATH에 있는지 확인하세요."
        )

    cfg = effective_config().get("llm", {})
    model = model or cfg.get("model", "claude-sonnet-5")
    timeout = timeout or cfg.get("timeout_sec", 900)
    thinking_tokens = cfg.get("thinking_tokens", 0) if thinking_tokens is None else thinking_tokens

    cmd = build_command(
        prompt, image_paths=image_paths, model=model, system_prompt=system_prompt
    )
    log.info(
        "claude 호출 (model=%s, 프롬프트 %d자, 이미지 %d개)",
        model, len(prompt), len(image_paths or []),
    )

    try:
        proc = subprocess.run(
            cmd, cwd=INSTANCE_ROOT, capture_output=True, text=True, timeout=timeout,
            check=False, env=_child_env(thinking_tokens),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"claude 호출이 {timeout}초 안에 끝나지 않았습니다. "
            "config.yaml의 llm.timeout_sec를 늘리거나 더 빠른 모델을 쓰세요."
        ) from exc

    # 한도 판정은 **실패했을 때만** 한다. 성공 응답 본문에는 토큰 수·소요시간이
    # 들어 있어 무엇을 찾든 우연히 걸릴 수 있다.
    if proc.returncode != 0:
        combined = (proc.stdout or "") + (proc.stderr or "")
        _raise_if_limited(combined)  # 한도는 고장이 아니다. 별도로 다룬다.
        raise RuntimeError(
            f"claude 실행 실패 (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:500]}"
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout.strip()

    if isinstance(payload, dict):
        if payload.get("is_error"):
            _raise_if_limited(str(payload.get("result", "")))
            raise RuntimeError(f"claude 오류: {str(payload.get('result'))[:500]}")
        _log_cost(payload)
        return str(payload.get("result", "")).strip()
    return proc.stdout.strip()


def _log_cost(payload: dict) -> None:
    """호출 비용이 눈에 보여야 모델 선택을 판단할 수 있다."""
    usage = payload.get("usage") or {}
    log.info(
        "claude 응답 — 입력 %s토큰 / 출력 %s토큰 / $%s / %ss",
        usage.get("input_tokens", "?"),
        usage.get("output_tokens", "?"),
        round(payload.get("total_cost_usd", 0), 4),
        round(payload.get("duration_ms", 0) / 1000, 1),
    )
