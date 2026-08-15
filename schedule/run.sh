#!/bin/bash
# launchd 진입점. 두 파이프라인을 순서대로 돌린다.
#
#   A. 지원 파이프라인   수집 → 판정 → 이상감지.  무거워서 하루 3번만.
#   B. 개선 오케스트레이터  폰 지시 + 자체 진단 → 브랜치 작업.  매 사이클.
#
# B를 매번 도는 이유: Claude 사용 한도에 걸린 일은 큐로 되돌아간다. 자주 깨어나야
# 한도가 풀린 시점을 놓치지 않고 이어받는다 — 새벽에도 알아서 진행된다.
#
# launchd는 로그인 셸을 안 거쳐 PATH가 최소한이다. claude CLI 경로를 명시하지
# 않으면 매 실행 죽는다.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$REPO" || exit 1
mkdir -p data/logs

PY="$REPO/.venv/bin/python"
HOUR=$(date +%-H)
echo "=== $(date '+%F %T') 사이클 시작 ==="

# ── 폰에서 온 메시지 수신 (운영 명령 즉시 처리 + 개발 지시 큐 적재)
"$PY" cli.py listen || echo "listen 실패 (건너뜀)"

# ── A: 지원 파이프라인. 무거우므로 지정 시각에만.
if [[ "$HOUR" == "3" || "$HOUR" == "9" || "$HOUR" == "15" ]]; then
  echo "--- A: 수집·판정 ---"
  "$PY" cli.py scrape --check-session || echo "scrape 실패 (다음 사이클이 이어받는다)"
else
  echo "--- A: 건너뜀 (수집 시각 아님) ---"
fi

# ── 대기열 상위 1건을 dry-run으로 준비. 제출은 하지 않는다.
#
# 수집 시각에만 돌린다. 건당 원티드에 이력서가 하나 생기므로 2시간마다 돌리면
# 하루 12개가 쌓인다. 하루 3건이면 검토할 양으로도 적당하다.
if [[ "$HOUR" == "3" || "$HOUR" == "9" || "$HOUR" == "15" ]]; then
  echo "--- A2: 지원 준비 (dry-run) ---"
  "$PY" cli.py cycle-apply --limit 1 || echo "cycle-apply 실패 (다음 사이클이 이어받는다)"
fi

# ── B: 개선 오케스트레이터. 한 사이클에 1건만 — 브랜치가 쌓이면 검토가 불가능해진다.
echo "--- B: 자기개선 ---"
"$PY" cli.py improve --limit 1 || echo "improve 실패 (다음 사이클이 이어받는다)"

echo "=== $(date '+%F %T') 종료 ==="
"$PY" cli.py quota
