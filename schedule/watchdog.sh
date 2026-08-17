#!/bin/bash
# 상주 리스너가 죽었는지 5분마다 보고, 죽었으면 되살리고 폰으로 알린다.
# (launchd: com.autoapply.watchdog)
#
# ## 왜 KeepAlive로 부족한가
#
# 2026-08-17에 실측한 것: launchd가 실행파일을 못 찾아 스폰 자체에 실패하면
# (`last exit code = 78: EX_CONFIG`) KeepAlive는 그 상태에서 **못 빠져나온다.**
# 실행파일이 돌아온 뒤에도 418번 재시도하고 418번 같은 자리에서 죽었다.
# `launchctl kickstart`도 안 통했고 bootout + bootstrap이라야 살아났다.
# 그동안 stdout/stderr는 0바이트였다 — 프로세스가 안 떴으니 쓸 게 없다.
# **로그가 조용한 것과 잘 도는 것이 똑같이 보였다.** 6시간 뒤 사람이 알아챘다.
#
# 그래서 이 감시자는 두 가지를 다르게 한다:
#   1. `/bin/bash`가 ProgramArguments[0]이다. 저장소가 통째로 없어져도 스폰은
#      성공하고, 무엇이 없는지가 로그에 글자로 남는다.
#   2. 파이썬·httpx를 안 쓴다. sqlite3와 curl은 macOS에 원래 있다.
#      감시자가 지켜야 하는 고장이 바로 "`.venv`가 없어졌다"인데, 감시자가
#      그 `.venv`를 필요로 하면 같이 죽는다.
#
# 배터리로 돌 때도 건너뛰지 않는다(run.sh와 다른 점). 롱폴링은 유휴 비용이
# 거의 없고, 리스너가 죽어 있으면 폰에서 보낸 명령이 그냥 사라진다.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
DB="$REPO/data/jobs.db"
GUI="gui/$(id -u)"
LABEL="com.autoapply.listen"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# 심장박동이 이만큼 낡았으면 프로세스가 살아 있어도 멎은 걸로 본다.
# 롱폴링 한 바퀴가 25초이므로 매우 넉넉한 값이다.
STALE_SEC=600
# 같은 경보를 반복해서 보내지 않는다.
ALERT_COOLDOWN=1800
# 이 감시자 자신의 실행 간격(plist의 StartInterval과 맞춘다).
TICK=300

NOW=$(date +%s)
log() { echo "$(date '+%F %T') $*"; }

# --- DB 접근 (없어도 감시자는 계속 돈다) ----------------------------------
q() { [[ -f "$DB" ]] && /usr/bin/sqlite3 -readonly "$DB" "$1" 2>/dev/null || true; }
put() {
  [[ -f "$DB" ]] || return 0
  /usr/bin/sqlite3 "$DB" \
    "INSERT INTO settings (key,value,updated_at)
     VALUES ('$1','$2',strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
     ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at" 2>/dev/null || true
}

# settings에서 읽은 값이 에폭 초인지 본다. 형식이 바뀐 옛 값이 남아 있으면
# 산술이 통째로 죽으므로(set -u 아래에서 특히), 숫자가 아니면 없는 셈 친다.
epoch() { [[ "$1" =~ ^[0-9]+$ ]] && echo "$1" || echo ""; }

# --- 폰으로 알림 (파이썬 경유 안 함) --------------------------------------
alert() {
  local text="$1" key="watchdog_last_alert" last
  last=$(epoch "$(q "SELECT value FROM settings WHERE key='$key'")")
  if [[ -n "$last" ]] && (( NOW - last < ALERT_COOLDOWN )); then
    log "경보 억제 (마지막 발송 $((NOW - last))초 전): $text"
    return 0
  fi
  local token chat
  token=$(q "SELECT value FROM settings WHERE key='telegram_bot_token'")
  chat=$(q "SELECT value FROM settings WHERE key='telegram_chat_id'")
  if [[ -z "$token" || -z "$chat" ]]; then
    log "텔레그램 자격증명 없음 — 경보 못 보냄: $text"
    return 0
  fi
  if /usr/bin/curl -s -m 20 -o /dev/null \
      -X POST "https://api.telegram.org/bot$token/sendMessage" \
      --data-urlencode "chat_id=$chat" --data-urlencode "text=$text"; then
    put "$key" "$NOW"
    log "경보 발송: $text"
  else
    log "경보 발송 실패 (다음 틱에 재시도): $text"
  fi
}

running() {
  /bin/launchctl print "$GUI/$LABEL" 2>/dev/null | grep -qE '^[[:space:]]*state = running'
}

# --- 맥이 자고 있었나 -----------------------------------------------------
# 잠들었다 깨면 리스너 프로세스는 멀쩡한데 심장박동만 낡아 있다. 그걸
# 고장으로 읽으면 깰 때마다 헛되이 재시작하고 헛경보를 보낸다.
# 별도 API를 쓰지 않고 **이 감시자가 몇 틱을 건너뛰었는지**로 잰다 —
# 자는 동안엔 감시자도 안 돌았으니 그 공백이 곧 수면 시간이다.
LAST_TICK=$(epoch "$(q "SELECT value FROM settings WHERE key='watchdog_last_tick'")")
WOKE=0
if [[ -n "$LAST_TICK" ]] && (( NOW - LAST_TICK > TICK * 3 )); then
  WOKE=1
  log "감시자 공백 $((NOW - LAST_TICK))초 — 맥이 자고 있었다고 본다. 심장박동 판정은 건너뛴다"
fi
put "watchdog_last_tick" "$NOW"

# --- 판정 -----------------------------------------------------------------
REASON=""
if [[ ! -f "$PLIST" ]]; then
  log "plist가 없다: $PLIST — schedule/install.sh를 돌려야 한다"
  alert "🚨 리스너 감시자: $PLIST 가 없습니다. schedule/install.sh 를 실행하세요."
  exit 0
fi

if ! running; then
  REASON="프로세스가 안 떠 있음 (launchctl state != running)"
else
  HB=$(epoch "$(q "SELECT value FROM settings WHERE key='listen_heartbeat'")")
  if [[ -z "$HB" ]]; then
    # 심장박동을 안 남기는 구판이 떠 있을 수 있다. 프로세스가 살아 있으면
    # 그것만으로 통과시킨다 — 없는 신호를 고장으로 읽지 않는다.
    log "정상: 프로세스 떠 있음 (심장박동 기록 없음 — 구판 리스너일 수 있다)"
    exit 0
  elif (( WOKE == 0 && NOW - HB > STALE_SEC )); then
    REASON="심장박동이 $((NOW - HB))초째 멎음 (프로세스는 떠 있으나 진행이 없음)"
  fi
fi

if [[ -z "$REASON" ]]; then
  log "정상"
  exit 0
fi

# --- 복구 -----------------------------------------------------------------
# kickstart를 안 쓴다. 2026-08-17에 penalty box에 빠진 잡은 kickstart로 안
# 돌아왔고 bootout + bootstrap이라야 살아났다. 어차피 죽은 잡이므로 바로 그걸 한다.
log "리스너 고장: $REASON — 되살린다"
/bin/launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
sleep 2
/bin/launchctl bootstrap "$GUI" "$PLIST" 2>&1 || true
sleep 5

if running; then
  log "복구 성공"
  alert "🔁 텔레그램 리스너가 죽어 있어 되살렸습니다.
사유: $REASON
그동안 폰에서 보낸 명령이 밀렸을 수 있습니다 — 답이 없던 게 있으면 다시 보내세요."
else
  log "복구 실패 — 여전히 안 뜬다"
  alert "🚨 텔레그램 리스너가 죽었고 자동 복구도 실패했습니다.
사유: $REASON
확인: tail -50 $REPO/data/logs/listen.err.log"
fi
