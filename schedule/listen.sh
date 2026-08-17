#!/bin/bash
# 상주 텔레그램 리스너의 진입점 (launchd: com.autoapply.listen).
#
# plist가 `.venv/bin/python`을 **직접** 가리키면 안 된다. 2026-08-17에 그것 때문에
# 리스너가 6시간 죽어 있었고, 아무도 몰랐다:
#
#   12:02  .venv가 사라진 상태에서 잡이 bootstrap 됐다
#          → launchd의 posix_spawn이 ENOENT로 실패 → `last exit code = 78: EX_CONFIG`
#          → **stdout/stderr 로그는 0바이트**. 프로세스가 뜨질 않았으니 쓸 주체가 없다.
#             로그만 보면 "조용히 잘 돌고 있다"와 구분이 안 된다.
#   12:06  .venv가 다시 생겼다. 그런데 KeepAlive는 못 살렸다 —
#          launchd가 없는 실행파일에 감시를 걸어둔 채 penalty box에 넣어놨고,
#          `runs = 418`이 되도록 같은 자리에서 죽었다.
#          `launchctl kickstart`로도 안 돌아왔다. bootout + bootstrap이라야 살았다.
#   18:16  사람이 알아챘다. 폰에서 보낸 명령 4건이 그동안 응답 없이 쌓여 있었다.
#
# `/bin/bash`는 사라지지 않는다. 여기서 한 겹 받으면 무엇이 없든 **로그에 글자가
# 남고**, 종료코드가 launchd의 스폰 실패가 아니라 이 스크립트의 것이 된다.
# 감시자(schedule/watchdog.sh)가 그걸 보고 알린다.
#
# PATH를 여기서 export 하는 이유는 run.sh와 같다 — 이 리스너가 subprocess로
# 띄우는 것들(/guide, /지원시작, /improve, 수정요청 재작성)이 이걸 물려받는다.
# launchd 기본 PATH엔 claude CLI가 없어서, 예전에 /guide 요청 하나가
# "claude CLI를 찾을 수 없다"로 조용히 사라졌다.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$REPO" || { echo "저장소로 못 들어감: $REPO" >&2; exit 1; }
mkdir -p data/logs

PY="$REPO/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "=== $(date '+%F %T') 리스너 못 뜸: $PY 가 없거나 실행 불가 ===" >&2
  echo "    복구: cd $REPO && uv venv && uv pip install -e ." >&2
  # KeepAlive가 30초마다 다시 부른다. 고쳐지면 다음 시도에서 그냥 뜬다 —
  # launchd가 없는 파일을 직접 물고 있을 때와 달리, 이 경로는 자기치유가 된다.
  exit 1
fi

exec "$PY" cli.py listen --watch
