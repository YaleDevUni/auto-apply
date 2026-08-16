#!/bin/bash
# launchd 진입점. 모드 인자로 무엇을 할지 정한다 — "지금 몇 시지?"를
# 스크립트가 다시 추측하지 않는다.
#
#   scrape 12:00  수집 + 두 축 판정. 하루 한 번이면 충분하다 — 지원준비와
#                 시각을 분리해서, /apply로 급히 지원준비만 부를 때 매번
#                 수 분짜리 전체 수집이 같이 안 돌게 했다(2026-08-16).
#   night  02:00  지원준비를 목표건수/대기열 소진까지 반복(수집은 안 함,
#                 정오에 이미 모아둔 대기열에서 고른다).
#                 알림은 안 보낸다(쌓아만 둠). 이력서 정리도 여기서 하루 한 번.
#   flush  09:00  밤사이 쌓인 알림을 한 번에 보낸다.
#
# 과거엔 무거운 단계를 "몇 시냐"로 걸었다가(3/9/15시) launchd가 짝수 시각에만
# 깨우는 것과 어긋나 **수집이 한 번도 실행되지 않았다.** 이번엔 시각을 여기
# 다시 적지 않는다 — plist가 어느 모드로 부를지 인자로 넘기고, 이 스크립트는
# 그 인자만 본다. 두 곳에 시각을 따로 적을 일 자체가 없다.
#
# 자기개선(improve)은 더는 여기서 시간이 되면 자동으로 안 돈다. night-cycle이
# 끝에서 자체진단(자체진단 신호가 있을 때만) 스스로 호출하고, 그 외엔 사람이
# 텔레그램 /improve로 부를 때만 돈다 — 상주 리스너(com.autoapply.listen)가
# 그건 처리한다.
#
# launchd는 로그인 셸을 안 거쳐 PATH가 최소한이다. claude CLI 경로를 명시하지
# 않으면 매 실행 죽는다.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$REPO" || exit 1
mkdir -p data/logs

PY="$REPO/.venv/bin/python"
MODE="${1:?사용법: run.sh night|flush}"
echo "=== $(date '+%F %T') [$MODE] 시작 ==="

# 뚜껑 닫아도 안 자게 해뒀지만 그건 어댑터에 꽂혀 있을 때 얘기다. 배터리로
# 돌면 유휴 1분 뒤 그냥 잠들 수 있어 정시 실행을 보장 못 한다 — 차라리
# 시작 전에 걸러서 "돌다 만 상태"가 안 생기게 한다.
if ! pmset -g ps | head -1 | grep -q "AC Power"; then
  echo "=== $(date '+%F %T') [$MODE] 배터리 전원 — 건너뜀 (충전기 연결 시에만 실행) ==="
  exit 0
fi

# 상주 리스너가 실시간으로 받지만, 혹시 죽어 있었을 경우의 안전망 —
# 쌓인 게 있으면 여기서 한 번 걷어간다. 운영 명령만 즉시 처리하고,
# 개발 지시는 큐에 넣기만 한다(자동으로 돌리지 않는다).
"$PY" cli.py listen || echo "listen 실패 (건너뜀)"

case "$MODE" in
  scrape)
    echo "--- 수집 + 판정 ---"
    "$PY" cli.py scrape --check-session || echo "scrape 실패 (내일 재시도)"
    ;;
  night)
    echo "--- 지원준비 (목표 30건 또는 대기열 소진까지, 알림은 9시에) ---"
    "$PY" cli.py night-cycle --target 30 --defer || echo "night-cycle 실패 (다음날 이어받는다)"

    echo "--- 이력서 정리 ---"
    "$PY" cli.py resumes --cleanup || echo "정리 실패 (다음날 재시도)"
    ;;
  flush)
    echo "--- 새벽에 준비된 것들 보내기 ---"
    "$PY" cli.py flush-notify || echo "flush-notify 실패"
    ;;
  *)
    echo "모르는 모드: $MODE (scrape, night, flush 중 하나)" >&2
    exit 1
    ;;
esac

echo "=== $(date '+%F %T') [$MODE] 종료 ==="
"$PY" cli.py quota
