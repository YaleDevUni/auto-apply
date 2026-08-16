#!/bin/bash
# launchd에 등록한다. 저장소 경로를 plist에 박아 넣고 ~/Library/LaunchAgents로 복사.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$HOME/Library/LaunchAgents" "$REPO/data/logs"

# cycle  — 2시간마다 깨어나 수집·판정·자기개선
# listen — 상시 대기. 폰에서 온 메시지에 즉시 답한다
#          (사이클에서만 받으면 답장이 최대 2시간 밀린다)
for LABEL in com.autoapply.cycle com.autoapply.listen; do
  DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
  sed "s|__REPO__|$REPO|g" "$REPO/schedule/$LABEL.plist" > "$DEST"
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$DEST"
  echo "등록됨: $LABEL"
done

echo
echo "해제:   launchctl bootout gui/$UID/<라벨>"
echo "즉시실행: launchctl kickstart -p gui/$UID/com.autoapply.cycle"
echo "로그:   tail -f $REPO/data/logs/cycle.out.log $REPO/data/logs/listen.out.log"
