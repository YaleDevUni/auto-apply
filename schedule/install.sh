#!/bin/bash
# launchd에 등록한다. 저장소 경로를 plist에 박아 넣고 ~/Library/LaunchAgents로 복사.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.autoapply.cycle"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/data/logs"
sed "s|__REPO__|$REPO|g" "$REPO/schedule/$LABEL.plist" > "$DEST"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$DEST"
echo "등록됨: $DEST"
launchctl print "gui/$UID/$LABEL" | grep -E "state|runs" || true
echo
echo "해제:   launchctl bootout gui/$UID/$LABEL"
echo "즉시실행: launchctl kickstart -p gui/$UID/$LABEL"
echo "로그:   tail -f $REPO/data/logs/cycle.out.log"
