#!/bin/bash
# launchd에 등록한다. 저장소 경로를 plist에 박아 넣고 ~/Library/LaunchAgents로 복사.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$HOME/Library/LaunchAgents" "$REPO/data/logs"

# 커밋 훅을 건다. core.hooksPath는 .git/config에 들어가는 값이라 저장소에
# 딸려오지 않는다 — 새로 클론하면 훅이 꺼진 채로 시작한다. 여기서 켠다.
#
# 이 훅이 막는 것: 심볼릭 링크 커밋과 data/·profile/ 추적. 2026-08-17에
# 워크트리에서 만든 심볼릭 링크가 커밋에 실려 갔고, 병합하면서 git이 그
# 링크를 진짜 디렉터리 자리에 체크아웃해 jobs.db와 이력서 원본을 지웠다.
git -C "$REPO" config core.hooksPath .githooks
echo "커밋 훅 등록: $(git -C "$REPO" config --get core.hooksPath)"

# scrape — 12:00 한 번. 수집 + 판정만
# night  — 02:00 한 번. 지원준비를 목표건수/대기열 소진까지(수집은 안 함). 알림은 안 보냄
# flush  — 09:00 한 번. 밤사이 쌓인 알림을 몰아서 보냄
# listen — 상시 대기. 폰에서 온 메시지·버튼에 즉시 답한다
#
# 예전 com.autoapply.cycle(2시간마다)은 폐기됐다 — 남아 있으면 night/flush와
# 겹쳐 돈다. 처음 설치하는 자리에서 확실히 내린다.
launchctl bootout "gui/$UID/com.autoapply.cycle" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.autoapply.cycle.plist"

for LABEL in com.autoapply.scrape com.autoapply.night com.autoapply.flush com.autoapply.listen; do
  DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
  sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" "$REPO/schedule/$LABEL.plist" > "$DEST"
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$DEST"
  echo "등록됨: $LABEL"
done

echo
echo "해제:   launchctl bootout gui/$UID/<라벨>"
echo "즉시실행: launchctl kickstart -p gui/$UID/com.autoapply.night"
echo "로그:   tail -f $REPO/data/logs/scrape.out.log $REPO/data/logs/night.out.log $REPO/data/logs/flush.out.log $REPO/data/logs/listen.out.log"
