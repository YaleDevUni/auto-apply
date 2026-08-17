# 열린 큐

이 파일은 **아직 안 한 일**만 담는다(2026-08-18에 TODO.md를 접고 합쳤고,
날짜별 서사는 걷어냈다). 완료 목록과 "무엇이 왜 그렇게 됐나"는 git 로그가
갖는다 — 커밋 메시지에 근거와 실측이 들어 있다. 옛 서사가 필요하면
`git show 65b1f85:NEXT.md`.

## 보안 — 우선순위 (2026-08-18 점검)

1. [x] **텔레그램 HTML 인젝션** (2026-08-18 새벽 회차에서 고침) —
       `notify/report.py`(`head` 조립 + `err`), `notify/listener.py`의
       `_cmd_targets()`, `workflows/submit_application.py`의
       `notify_submitted()` 세 곳 모두 `company`/`title`/`err`에
       `html.escape()`를 씌웠다 — 이미 이 패턴을 쓰던 `cli.py`·
       `orchestrator.py`·`errors.py`·`revise_application.py`와 맞췄다.
       검증: `<script>`/`<img src=x onerror=1>`/`A & B <b>bold</b>` 같은
       입력을 각 조립 로직에 직접 넣어 결과 문자열에 원본 태그가 살아
       있지 않고 `&lt;`/`&amp;`로 바뀌는지, 내가 쓴 `<b>`/`<i>` 태그는
       그대로 남는지 확인(`pytest tests/` 153건도 통과, 회귀 없음).
       텔레그램으로 실제 전송은 안 함(무인 새벽 회차라 알림 금지).
2. [ ] 텔레그램 `notify()`에만 길이 제한이 없다 (자세한 내용은 아래
       "알려진 결함" 참고) — 1번을 고쳤어도 긴 제목 하나로 알림이
       죽는 문제 자체는 남아 있다
3. [ ] 봇 토큰이 로그·오류 기록에 평문으로 남는다 (아래 "알려진 결함" 참고)

## 운영 구조 — 다음 회차 (2026-08-18 합의)

리스너(`com.autoapply.listen`, KeepAlive)를 **조율자로 승격**한다. 지금 이
프로세스는 항상 떠 있고 25초마다 도는데 텔레그램 말고는 아무것도 소유하지 않는다.

    1. 운영 손잡이를 settings로 — night.hour / night.target / scrape.hour /
       enabled + 범용 `/set <키> <값>` 명령. 지금은 pause 하나뿐이고 새 손잡이를
       만들 때마다 코드와 텔레그램 명령을 같이 고쳐야 한다
    2. due()를 watch 루프에 — "지났나?"로 판정(동등 비교 아님). calendar plist
       3개(night/scrape/flush) 은퇴. launchd에는 listen과 watchdog만 남는다
    3. 잡 등록 가드 — 같은 kind가 이미 돌면 시작하지 않는다

**잡은 계속 subprocess로 둔다.** 서버가 조율만 소유하고 실행은 안 가져가면
`stage='filling'` 크래시 안전성이 그대로 유지된다 — 프로세스가 죽으면 그 표시가
DB에 남고 다음 실행이 절반짜리 이력서를 버린다. 실행까지 한 프로세스로 흡수하면
그 성질을 의도적으로 다시 만들어야 한다.

3번이 **night-cycle 중복 실행을 구조적으로 없앤다**. 지금 시작 경로가 둘
(launchd `run.sh night`, 폰 `/apply` → `listener.py:458`)이고 서로를 모른다.
시작점이 한 곳으로 모이면 `tasks.active()` 확인 한 줄이면 된다.

후속 후보: 브라우저 순번. 지금은 `scrape`와 `night-cycle`이 같은 flock을 두고
다투고 **진 쪽이 `BrowserBusy`로 그냥 안 돈다** — 큐에 안 들어가고 사라진다.
조율자가 있으면 줄을 세울 수 있다.

FastAPI는 여기에 HTTP 껍데기를 씌우는 것뿐이라 지금은 필요 없다. 폰이 텔레그램으로
이미 닿는다. 웹 UI나 외부 연동이 생기면 그때 얹는다.

## 알려진 결함

- [ ] 텔레그램 `notify()`에만 길이 제한이 없다 — 4096자를 넘기면 400이고
      `notify()`가 예외를 삼켜 **그 알림이 그 자리에서 사라진다**
      (`notify_with_buttons`는 `[:4096]`, `send_photo`는 `[:1024]`).
      링크가 든 캡션이 실패하는 것도 같은 자리로 보인다
- [ ] 봇 토큰이 로그·오류 기록에 평문으로 남는다. `errors.record()`에 마스킹이
      없고, httpx의 정상 INFO 로그가 `getUpdates` URL을 통째로 찍어 25초마다
      `listen.err.log`에 들어간다
- [ ] 상주 브라우저 홀더 프로세스가 샌다 — `session.py`의 `_kill_resident()`는
      CDP 포트를 LISTEN하는 pid(=크롬)만 죽인다. `_spawn_resident()`가 띄운
      파이썬 홀더(`while True: time.sleep(3600)`)는 별개 pid라 아무도 안 죽인다
- [ ] `render.py`(183줄)가 어디서도 import되지 않는다 — 죽은 코드
- [ ] `orchestrator.py` ↔ `notify/listener.py` 순환 참조. 양쪽이 함수-지역
      import로 회피 중이라 top-level로 올리면 터진다
- [ ] 원장 요약이 한 단어 지시('짧게')에서 근거를 지어낼 때가 있다. 기록한 줄을
      폰으로 되돌려주므로 사람이 고칠 수 있지만 스스로는 못 걸러낸다.
      자동 승격은 **폐기**했다 — 한 공고의 특수 요구가 조용히 규칙이 될 위험이
      이득보다 크다. `/revlog`로 보고 `/guide`로 사람이 옮긴다
- [ ] 스킬 정리(prune)를 켜면 스킬이 사라진다. 삭제는 되는데 이후 추가가 안 됨.
      기본 꺼둠 — 사본 스킬은 비어 있어 지금은 추가만 하면 된다
- [ ] 원티드 DB에 없는 스킬(`Qdrant` 등)은 등록 불가. 대체 표기 매핑 필요.
      비개발 트랙은 §7-1이 도구 고유명사만 쓰게 해서 해소됐다(5/5 등록)
- [ ] 링크 2건 이상 추가 안 됨 (사본이 들고 오므로 당장은 안 막힌다)

## 미룬 것

- [ ] `claude setup-token`(1년 OAuth)을 launchd plist의
      `CLAUDE_CODE_OAUTH_TOKEN`에 넣기. 사람이 브라우저 승인을 해야 한다.
      현재 plist 4개 전부 0건
- [ ] 프롬프트 캐시 프리픽스 맞추기 — 조립(`build_editor_json`)과 검수
      (`review_editor`)가 같은 가이드를 쓰는데 선행 바이트가 달라 캐시가 안 맞는다.
      비용의 대부분이 가이드를 캐시에 쓰는 값이다(실측 22,757 / 30,643)
- [ ] repository 계층 — 13개 파일이 각자 SQL을 쓰고 `db.py`가 내주는 헬퍼는
      8개뿐이다. 이번엔 `cli.py`분만 소유 모듈로 돌려보냈다
- [ ] domain 모델 — Job·Application·Resume·Receipt·ReviewDecision이 클래스로
      하나도 없고 전부 `dict`/`sqlite3.Row`다. 유일한 관련 타입이
      `adapters/base.py`의 `JobPosting`인데 그마저 adapter 계층에 있다
- [ ] `cli.py`에 남은 `_browser_open`(runner/session이 소유해야 한다) ·
      `_guide`/`_guide_message` · `_revlog`
- [ ] 자가개선이 실제로 고장을 고치는지 — 아직 진짜 고장으로 안 돌려봤다.
      신호가 뜨는 것과 고쳐지는 것은 다르다
- [ ] 브랜치에 쌓인 수정을 사람이 검토하는 흐름 (지금은 브랜치만 남는다)
- [ ] 새벽 사이클이 끝나면 텔레그램으로 완료 보고



- [ ] presales 트랙 실주행
- [ ] 경력(회사)이 여러 건일 때. 사실 저장소에 1건뿐이라 못 해봄
- [ ] 사람인·자소설 편집기 레시피. 어댑터는 확인됐고 레시피 JSON만 없다
- [ ] `박예일 기본`의 내용 확인 — 재사용 경로가 이 이력서를 여러 공고로
      덮어썼다(스킬은 추가만 하므로 공고별 스킬이 쌓여 있을 수 있다).
      기준 이력서로 다시 쓰려면 사람이 한 번 봐야 한다.
      검증: 스킬 목록과 간단 소개가 특정 공고 냄새 없이 기준값인지 눈으로 확인
- [ ] 원티드 `지원 현황` 목록을 한 번 읽어 예전 지원을 통째로 `external`에
      적는 경로(선택). 페이지 하나로 끝나면 건당 확인이 필요 없어진다.
      검증: 읽은 건수만큼 원장에 external이 생기고 그 자리가 `v_actionable`에서
      사라짐. 사람이 지원한 적 없는 공고는 안 들어감
- [ ] (아이디어, 채택 여부 미정) ChatGPT에서 Vercel `agent-browser` 스킬 검토
      (공유: chatgpt.com/share/6a83386b-a3bc-83ee-b65e-b33b282fb5d6) — 훑어보고
      판단만 함, 코드 변경 없음.
        - snapshot → `@e1` 같은 ref → action 패턴의 브라우저 조작 CLI.
          **실행 엔진 교체 대상은 아니다**: `recipes/*.json` + Playwright가
          이미 같은 걸 결정론적으로 하고 있고(§4 "레시피 기반 폼 입력"은
          LLM 0회 영역), 레시피가 깨졌을 때 화면을 눈으로 살피는 용도라면
          이 세션의 Claude Browser MCP로 이미 되므로 새 의존성을 얹을
          한계효용이 낮다
        - 대화에서 나온 `derive-client`(실제 조작을 하며 네트워크 요청을
          기록해 사이트 내부 API를 역추적) 아이디어는 위 "지원 현황 목록
          읽기" 같은 **읽기 전용** 항목에 한해 재볼 가치는 있음 — 화면
          스크레이핑보다 결정론적일 수 있다. 단 내부 API 역추적은 플랫폼
          ToS·봇탐지 회색지대라 사람 판단 없이 결정하지 않는다. 이 항목이
          실제로 작업될 때 다시 판단