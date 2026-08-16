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


def _explain_failure(raw: str) -> tuple[str, dict | None]:
    """실패 원문에서 사람이 읽을 메시지와, 있으면 파싱된 오류 봉투를 낸다.

    claude CLI는 exit!=0일 때도 오류 봉투를 stdout에 JSON으로 낸다(stderr는
    빔). result는 usage 필드들 뒤에 오므로 원문을 그냥 500자로 자르면 result가
    통째로 잘려나간다(실측: result 1208번째 문자, 컷은 500번째). 그래서
    구조가 있으면 result/subtype/api_error_status를 앞으로 꺼내 조립한다.
    JSON이 아니면 지금처럼 원문을 그대로 자른다.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()[:500], None
    if not isinstance(payload, dict):
        return raw.strip()[:500], None
    parts = [str(payload[k]) for k in ("result", "subtype", "api_error_status") if payload.get(k)]
    message = " / ".join(parts) if parts else raw.strip()
    return message[:500], payload


# 파싱된 실패 봉투에서만 보는 영어 한도 문구. 실측: api_error_status:429,
# result:"You've hit your session limit · resets 9:50pm (Asia/Seoul)".
#
# `_LIMIT_MARKERS`에 합치지 않는다 — orchestrator._agent가 코딩 에이전트의
# 출력 전문을 그대로 `_raise_if_limited`에 넘기는데, 거기엔 한도를 논의하는
# 계획문만 있어도 걸린다(재현 확인). 여기 목록은 claude CLI 오류 봉투를
# 실제로 파싱했을 때만 검사하므로 그 오염 경로를 안 탄다.
_LIMIT_MARKERS_PAYLOAD = ("session limit", "hit your")


def _raise_if_limited_payload(payload: dict | None) -> None:
    """파싱된 실패 봉투 버전. `_raise_if_limited`와 나란히, 대신한다."""
    if not payload:
        return
    if payload.get("api_error_status") == 429:
        raise UsageLimited(str(payload.get("result") or "사용 한도")[:300])
    result = str(payload.get("result") or "").lower()
    if any(m in result for m in _LIMIT_MARKERS_PAYLOAD):
        raise UsageLimited(str(payload.get("result"))[:300])


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
        # 이미지가 있는 디렉터리를 모두 열어준다. ASSET_DIR만 열면 증적
        # 스크린샷(EVIDENCE_DIR)을 못 읽어 "파일이 없다"로 조용히 끝난다.
        dirs = {str(ASSET_DIR)} | {str(Path(x).parent) for x in image_paths}
        cmd += ["--allowed-tools", "Read"]
        for d in sorted(dirs):
            cmd += ["--add-dir", d]
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
    job_id: int | None = None,
    phase: str = "",
) -> str:
    """프롬프트를 던지고 텍스트 응답을 받는다. 기본값은 config.yaml의 llm 섹션.

    job_id/phase는 순전히 기록용이다 — "지원 하나당 리소스를 얼마나 쓰는가"에
    답하려고 `llm_calls`에 남긴다(`_log_cost` 참고). 호출 자체 동작에는
    영향을 주지 않는다.
    """
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
        message, fail_payload = _explain_failure(proc.stdout or proc.stderr or "")
        _raise_if_limited_payload(fail_payload)  # 파싱됐으면 이쪽이 먼저다 — 더 정확하다.
        _raise_if_limited(combined)  # 비-JSON 출력 대비 폴백(회귀 방지).
        raise RuntimeError(f"claude 실행 실패 (exit {proc.returncode}): {message}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout.strip()

    if isinstance(payload, dict):
        if payload.get("is_error"):
            _raise_if_limited_payload(payload)
            _raise_if_limited(str(payload.get("result", "")))
            raise RuntimeError(f"claude 오류: {str(payload.get('result'))[:500]}")
        _log_cost(payload, model=model, job_id=job_id, phase=phase)
        return str(payload.get("result", "")).strip()
    return proc.stdout.strip()


def ask_session(
    prompt: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
    timeout: int | None = None,
    system_prompt: str = WRITER_SYSTEM_PROMPT,
    job_id: int | None = None,
    phase: str = "",
) -> dict[str, str]:
    """세션을 이어가며 묻는다. 반복 편집(가이드 수정)처럼 이전 지시를 기억해야
    자연스러운 경우에 쓴다 — 그 외에는 매번 완전히 새 대화인 `ask()`를 쓴다.

    `ask()`는 `--no-session-persistence`로 매 호출을 무상태로 만든다. 이
    함수는 반대로 세션을 남긴다: `session_id`가 있으면 `--resume`으로 이어가고,
    없으면 새 세션을 시작한다(claude 기본값이 알아서 만들고 저장한다).

    반환: {"text": 응답 본문, "session_id": 다음 호출에 넘길 id}.
    """
    if not cli_available():
        raise ClaudeUnavailable(
            "claude CLI를 찾을 수 없습니다. Claude Code가 설치되어 있고 PATH에 있는지 확인하세요."
        )

    cfg = effective_config().get("llm", {})
    model = model or cfg.get("model", "claude-sonnet-5")
    timeout = timeout or cfg.get("timeout_sec", 900)

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--system-prompt", system_prompt,
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "",
        "--allowed-tools", "",
    ]
    if session_id:
        cmd += ["--resume", session_id]

    log.info(
        "claude 호출(세션 %s) — 프롬프트 %d자",
        session_id[:8] if session_id else "새로",
        len(prompt),
    )

    try:
        proc = subprocess.run(
            cmd, cwd=INSTANCE_ROOT, capture_output=True, text=True, timeout=timeout,
            check=False, env=_child_env(0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"claude 호출이 {timeout}초 안에 끝나지 않았습니다. "
            "config.yaml의 llm.timeout_sec를 늘리거나 더 빠른 모델을 쓰세요."
        ) from exc

    if proc.returncode != 0:
        combined = (proc.stdout or "") + (proc.stderr or "")
        message, fail_payload = _explain_failure(proc.stdout or proc.stderr or "")
        _raise_if_limited_payload(fail_payload)
        _raise_if_limited(combined)
        raise RuntimeError(f"claude 실행 실패 (exit {proc.returncode}): {message}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"text": proc.stdout.strip(), "session_id": session_id or ""}

    if payload.get("is_error"):
        _raise_if_limited_payload(payload)
        _raise_if_limited(str(payload.get("result", "")))
        raise RuntimeError(f"claude 오류: {str(payload.get('result'))[:500]}")
    _log_cost(payload, model=model, job_id=job_id, phase=phase)
    return {
        "text": str(payload.get("result", "")).strip(),
        "session_id": str(payload.get("session_id") or session_id or ""),
    }


def _log_cost(
    payload: dict, *, model: str = "", job_id: int | None = None, phase: str = ""
) -> None:
    """호출 비용이 눈에 보여야 모델 선택을 판단할 수 있다.

    로그 줄만으로는 "공고 하나에 LLM을 얼마나 썼나"를 나중에 못 묻는다 —
    write·review·to_editor_json·portfolio_match가 로그 파일 여기저기 흩어져
    있어 그때그때 눈으로 세는 수밖에 없었다. `llm_calls`에 job_id와 함께
    남겨두면 `cli.py llm-cost <job_id>`로 바로 합산해 볼 수 있다.

    기록 실패는 호출 자체를 막지 않는다 — 비용 집계가 안 된다고 이력서
    작성이 멈추면 안 된다.
    """
    usage = payload.get("usage") or {}
    tag = f" [{phase or '?'} job={job_id}]" if phase or job_id is not None else ""
    log.info(
        "claude 응답 — 입력 %s토큰 / 출력 %s토큰 / $%s / %ss%s",
        usage.get("input_tokens", "?"),
        usage.get("output_tokens", "?"),
        round(payload.get("total_cost_usd", 0), 4),
        round(payload.get("duration_ms", 0) / 1000, 1),
        tag,
    )
    try:
        from .db import connect, now

        conn = connect()
        try:
            conn.execute(
                """INSERT INTO llm_calls
                     (job_id, phase, model, input_tokens, output_tokens,
                      cost_usd, duration_ms, called_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (job_id, phase or "", model,
                 usage.get("input_tokens"), usage.get("output_tokens"),
                 payload.get("total_cost_usd"), payload.get("duration_ms"), now()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log.debug("토큰 사용 기록 실패(무시): %s", e)
