#!/bin/bash
# launchd가 부르는 진입점. 여기서 실패해도 조용히 끝난다 — 다음 스케줄이 이어받는다.
#
# launchd는 로그인 셸을 거치지 않아 PATH가 최소한이다. claude CLI가 있는 곳을
# 명시하지 않으면 "claude를 찾을 수 없음"으로 매번 죽는다.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$REPO" || exit 1
mkdir -p data/logs

PY="$REPO/.venv/bin/python"
echo "=== $(date '+%F %T') 실행 시작 ==="

# 세션 확인 → 수집·판정 → 이상 감지 (여기까지 LLM 0회)
"$PY" cli.py scrape --check-session || echo "scrape 실패 (다음 실행이 이어받는다)"

echo "=== $(date '+%F %T') 종료 / 남은 지원 한도 ==="
"$PY" cli.py quota
