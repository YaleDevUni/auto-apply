#!/usr/bin/env python3
"""자가복구 에이전트의 손을 묶는다. PreToolUse 훅으로 Bash·Edit·Write를 검사한다.

## 왜 프롬프트가 아니라 훅인가

"이런 건 하지 마라"를 시스템 프롬프트에 적는 것으로는 부족하다. 프롬프트는
설득이고, 훅은 차단이다. 이 에이전트는 사람이 자는 동안 도는데, 그때 잘못
지운 DB나 잘못 나간 지원서를 되돌릴 사람이 없다.

## 왜 프로젝트 settings.json이 아닌가

`.claude/settings.json`에 넣으면 **사용자의 대화형 세션까지** 같이 묶인다.
사람이 자기 저장소에서 `git push`를 못 하게 되는 건 말이 안 된다. 그래서
`--settings .claude/agent-guard.json`으로 띄우는 에이전트에게만 건다.

## 막지 않는 것

`git commit`과 브랜치 만들기는 에이전트의 일이다. main에 무엇이 들어가는지는
이 훅이 아니라 `orchestrator`의 커밋 직전 검사가 통제한다 — 훅은 "무엇을 할 수
있나", 그 검사는 "무엇이 main에 갈 자격이 있나"를 본다. 층이 다르다.

차단은 exit code 2로 알린다. stderr에 적은 사유가 모델에게 그대로 전달되므로,
"왜 막혔는지"와 "그럼 어떻게 하라"를 같이 적는다 — 이유를 모르면 에이전트는
같은 것을 표현만 바꿔 계속 시도한다.
"""

from __future__ import annotations

import json
import re
import sys

# ── Bash 차단 규칙 ────────────────────────────────────────────────
# (정규식, 사유, 대안)
BASH_RULES: list[tuple[str, str, str]] = [
    # DB 파괴 — 수집물은 다시 받으면 되지만 apply_ledger는 중복지원 방어선이다.
    # 그게 사라지면 이미 지원한 자리에 다시 지원한다.
    (r"\brm\b[^|;&]*\.db\b", "DB 파일 삭제",
     "DB는 지우지 않는다. 스키마를 바꿔야 하면 db.py의 MIGRATIONS에 추가하라."),
    (r"\brm\s+(-\w*\s+)*-?\w*[rf]\w*\s", "rm -rf",
     "파일을 지우지 말고 코드를 고쳐라. 정말 필요하면 계획에 적어 승인을 받아라."),
    (r"\bsqlite3\b[^|;&]*\b(drop|delete\s+from|alter\s+table|attach|vacuum)\b",
     "sqlite로 직접 파괴적 SQL 실행",
     "읽기 조회(SELECT)만 하라. 스키마 변경은 db.py의 SCHEMA/MIGRATIONS로 한다."),
    # 절대경로(/Users/…/data/)와 상대경로(data/) 둘 다 잡아야 한다. 앞에 슬래시를
    # 요구하면 `echo x > data/jobs.db` 가 그대로 빠져나간다 — 실측으로 놓쳤다.
    (r">\s*[^|;&]*\b(data|profile)/", "data/ 또는 profile/ 파일 덮어쓰기",
     "그 폴더는 개인 데이터와 원장이다. 코드만 고쳐라."),
    (r"\b(truncate|mkfs|dd\s+if=|shred)\b", "파괴적 시스템 명령", "쓰지 않는다."),

    # 실제 지원 — 되돌릴 수 없다. 사람 실명으로 나간 지원서는 회수가 안 된다.
    (r"--live\b", "실제 지원 제출(--live)",
     "검증은 dry-run으로만 한다. --live는 사람이 폰에서 버튼을 눌러야 나간다."),
    (r"cli\.py\s+submit\b", "실제 지원 제출(submit)",
     "제출은 사람의 승인 버튼으로만 나간다. dry-run(`cli.py apply <id>`)으로 검증하라."),
    (r"cli\.py\s+autoapply\b", "조립부터 제출까지의 체인(autoapply)",
     "이 체인은 제출까지 간다. `cli.py resume <id>` 나 `cli.py apply <id>`로 검증하라."),

    # git — 바깥으로 나가는 것과 되돌릴 수 없는 것
    (r"\bgit\s+push\b", "git push",
     "바깥으로 내보내지 않는다. 브랜치에 커밋까지만 하라."),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard",
     "사람의 미커밋 작업을 날릴 수 있다. 되돌리려면 새 커밋으로 되돌려라."),
    (r"\bgit\s+clean\b", "git clean", "추적 안 되는 파일을 날린다. 쓰지 않는다."),
    (r"\bgit\s+(switch|checkout)\s+(main|master)\b", "main으로 전환",
     "main은 건드리지 않는다. 자동반영은 검증을 통과한 뒤 호출자가 처리한다."),
    (r"\bgit\s+(merge|cherry-pick|rebase|filter-branch)\b", "브랜치 합치기·다시쓰기",
     "합치는 것은 호출자나 사람의 몫이다. 브랜치에 커밋만 하라."),
    (r"\bgit\s+branch\s+-[Dd]\b", "브랜치 삭제", "브랜치를 지우지 않는다."),
    (r"--no-verify\b", "검사 우회(--no-verify)", "우회하지 말고 원인을 고쳐라."),

    # 시스템 — 이 저장소 밖으로 나가는 것
    (r"\b(sudo|launchctl|systemctl|shutdown|reboot|pmset)\b", "시스템 조작",
     "스케줄·전원·권한은 사람이 다룬다. schedule/ 아래 파일을 고치는 것까지만 하라."),
    (r"\bchmod\s+(-\w+\s+)*777\b", "chmod 777", "권한을 넓히지 않는다."),
    (r"\bkill(all)?\s+-9\b", "SIGKILL",
     "강제 종료는 반쯤 만진 이력서를 남긴다. tasks.request_stop을 쓴다."),

    # 개인정보가 바깥으로 나가는 것
    (r"\b(curl|wget|http|nc|ncat)\b[^|;&]*(-d|--data|-F|-T|--upload|-X\s*(POST|PUT))",
     "바깥으로 데이터 전송",
     "이 저장소에는 실제 이력서와 계정 정보가 있다. 아무것도 업로드하지 않는다."),
    (r"credentials\.json", "계정 정보 파일 접근",
     "계정 정보는 읽지 않는다. 필요한 값은 config.yaml의 키 이름으로만 다뤄라."),
    (r"\bbase64\b[^|;&]*\|\s*(sh|bash|python)", "인코딩된 코드 실행", "쓰지 않는다."),

    # 자기 자신
    (r"\.claude/(hooks/guard\.py|agent-guard\.json)", "하네스 자신을 조작",
     "이 파일들은 안전장치다. 고쳐야 한다고 판단되면 계획에 적어 사람에게 알려라."),
]

# ── Edit/Write 차단 대상 ──────────────────────────────────────────
PATH_RULES: list[tuple[str, str, str]] = [
    (r"\.db($|[-.])", "DB 파일 직접 편집",
     "스키마는 db.py의 SCHEMA/MIGRATIONS로 바꾼다."),
    (r"(^|/)profile/", "개인 데이터(profile/) 편집",
     "실제 이력서와 계정 정보다. 자동화가 고쳐 쓰지 않는다."),
    (r"(^|/)\.git/", ".git 내부 편집", "git 내부는 건드리지 않는다."),
    (r"\.claude/(hooks/guard\.py|agent-guard\.json)", "하네스 자신을 편집",
     "안전장치다. 고쳐야 한다면 계획에 적어 사람에게 알려라."),
]


def _deny(rule: tuple[str, str, str]) -> None:
    _, reason, alternative = rule
    print(
        f"차단됨 — {reason}.\n{alternative}\n"
        "(이 저장소의 자가복구 하네스가 막았다. 다른 표현으로 우회하지 말고 "
        "위 대안을 따르거나, 정말 필요하면 계획 본문에 왜 필요한지 적어라.)",
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        # 훅이 입력을 못 읽었다. 통과시킨다 — 하네스 자신의 고장으로 자가복구가
        # 통째로 멈추면, 고장 났을 때 아무것도 못 고치는 상태가 된다.
        return 0

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        for rule in BASH_RULES:
            if re.search(rule[0], command, re.IGNORECASE):
                _deny(rule)
        return 0

    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        for rule in PATH_RULES:
            if re.search(rule[0], path, re.IGNORECASE):
                _deny(rule)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
