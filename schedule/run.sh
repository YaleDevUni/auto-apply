#!/bin/bash
# launchd 진입점. 두 파이프라인을 순서대로 돌린다.
#
#   A. 지원 파이프라인   수집 → 판정 → 이상감지.  무거워서 하루 3번만(04/10/16시).
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
echo "=== $(date '+%F %T') 사이클 시작 ==="

# 무거운 단계는 "몇 시냐"가 아니라 "마지막으로 언제 했냐"로 정한다.
#
# 처음엔 시각으로 걸었다(3/9/15시). launchd는 짝수 시각에만 깨우므로 교집합이
# 없어 **수집과 지원 준비가 스케줄로는 한 번도 실행되지 않았다.** 두 곳에 적힌
# 시각이 어긋나면 조용히 아무 일도 안 일어난다.
#
# 경과 시간으로 보면 그 결합이 사라진다. 맥이 잠들어 몇 번 걸러도 깨어난 뒤
# 한 번은 돈다.
due() {  # due <표시> <최소간격시간>  → 지났으면 0
  local stamp="$REPO/data/logs/.last_$1" hours="$2"
  [[ -f "$stamp" ]] || return 0
  local age=$(( ($(date +%s) - $(stat -f %m "$stamp")) / 3600 ))
  (( age >= hours ))
}
mark() { touch "$REPO/data/logs/.last_$1"; }

# ── 폰에서 온 메시지 수신 (운영 명령 즉시 처리 + 개발 지시 큐 적재)
"$PY" cli.py listen || echo "listen 실패 (건너뜀)"

# ── A: 지원 파이프라인. 무거우므로 지정 시각에만.
if due scrape 6; then
  echo "--- A: 수집·판정 ---"
  "$PY" cli.py scrape --check-session || echo "scrape 실패 (다음 사이클이 이어받는다)"
  mark scrape
else
  echo "--- A: 건너뜀 (마지막 수집 이후 6시간 미만) ---"
fi

# ── 대기열 상위 1건을 dry-run으로 준비. 제출은 하지 않는다.
#
# 수집 시각에만 돌린다. 건당 원티드에 이력서가 하나 생기므로 2시간마다 돌리면
# 하루 12개가 쌓인다. 하루 3건이면 검토할 양으로도 적당하다.
if due apply 6; then
  echo "--- A2: 지원 준비 (dry-run) ---"
  "$PY" cli.py cycle-apply --limit 1 || echo "cycle-apply 실패 (다음 사이클이 이어받는다)"
  mark apply
else
  echo "--- A2: 건너뜀 (마지막 준비 이후 6시간 미만) ---"
fi

# ── 쌓인 이력서 정리. 하루 한 번이면 충분하다.
if due cleanup 24; then
  echo "--- 이력서 정리 ---"
  "$PY" cli.py resumes --cleanup || echo "정리 실패 (다음 사이클)"
  mark cleanup
fi

# ── B: 개선 오케스트레이터. 한 사이클에 1건만 — 브랜치가 쌓이면 검토가 불가능해진다.
echo "--- B: 자기개선 ---"
"$PY" cli.py improve --limit 1 || echo "improve 실패 (다음 사이클이 이어받는다)"

echo "=== $(date '+%F %T') 종료 ==="
"$PY" cli.py quota
