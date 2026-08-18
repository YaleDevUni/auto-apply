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
2. [x] **텔레그램 400 — 길이/HTML로 알림이 사라진다** (2026-08-18 새벽 회차에서
       `notify()`에 `text[:4096]`, 낮 회차에서 그 하드컷의 두 구멍을 막음) —
       긴 텍스트를 보내면 텔레그램이 400을 돌려주고 광범위 except가 삼켜서 그
       알림이 흔적 없이 사라지는 문제였다. 하드컷만으로는 부족했던 이유 둘:
       (a) 텔레그램은 길이를 **UTF-16 코드유닛**으로 세므로 이모지가 붙은
       `len()==4096` 본문이 여전히 400을 받는다 → 실제로 쓰는 값을
       `SAFE_TEXT=3900`으로 내렸다. (b) `parse_mode='HTML'`이라 4096 하드컷이
       `<pre>`나 `&lt;` 한가운데를 갈라 다시 400(can't parse entities)이 난다 →
       `_clip()`이 줄 경계로 물러난 뒤 태그·엔티티 반토막을 버리고 `…N자 생략`을
       붙인다(`str.rfind`만, 정규식 없음). 그래도 400이면 `parse_mode`를 빼고
       `_strip_html()`로 **평문 한 번 더** 보낸다 — 서식 빠진 알림은 읽을 수
       있지만 안 온 알림은 읽을 수 없다. 세 발송 함수를 같은 헬퍼로 통일했고
       (`notify` / `notify_with_buttons` / `send_photo`의 caption 1024),
       버튼 폴백은 `reply_markup`을 반드시 유지한다. `_call()`은 4xx(429 제외)를
       더 이상 재시도하지 않는다 — 영구 실패라 3초만 버리고 폴백을 늦춘다.
       검증: `_clip`/`_strip_html` 순수 확인 5케이스(줄경계 물러남, 줄바꿈 없는
       한 줄은 하드컷, 태그·엔티티 반토막 제거, 생략 글자수 일치),
       httpx.post 모킹으로 400→평문폴백(호출 2회)·폴백도 400→False·5xx는 3회
       재시도·429는 재시도·버튼 폴백의 reply_markup 유지·짧은 본문 무변경 확인.
       **실물 전송 3건 확인**(폰): 5000자 → 200 + `…자 생략`, 태그가 잘리는
       본문 → 400 후 평문 재전송 200(로그에 '평문으로 재전송함'), 4096 미만
       HTML 알림 → 그대로 200. pytest 161건 통과(회귀 없음).
       남은 구멍: `<b>`처럼 **열린 채 잘린** 태그는 `_clip`이 못 고친다(반토막만
       본다) — 그건 평문 폴백이 받는다. 폴백까지 실패하면 지금처럼 False다.
3. [x] **봇 토큰이 로그·오류 기록에 평문으로 남는다** (2026-08-18 새벽 회차에서
       고침) — `notify/telegram.py`에 `mask_token()`을 두고 두 지점에서
       썼다: `cli.py`의 `logging.basicConfig` 핸들러에 redact 필터로 걸어
       httpx의 `getUpdates` 요청 INFO 로그(`listen.err.log`에 25초마다
       쌓이던 것)를 가리고, `errors._record()`에서 message/traceback/command를
       DB(error_queue)에 넣기 전에 마스킹해 그 뒤 이어지는 폰 알림 본문까지
       같이 가렸다.
       검증: `mask_token()` 단위 확인, `errors.record()`를 격리된 임시 DB로
       실제로 돌려 토큰이 든 예외+command가 세 컬럼 모두 마스킹돼 저장되는지
       확인, httpx 스타일 INFO 로그 레코드를 같은 필터로 흘려 출력에 토큰이
       없는지 확인. pytest 153건 통과(회귀 없음).
       함정: 첫 검증에서 `connect()` 기본 인자가 실제 운영 DB임을 놓치고
       테스트 로우를 실제 `data/jobs.db`에 심었고, `_trigger_plan()`이 실제
       `cli.py plan` 서브프로세스까지 띄웠다 — 그 자리에서 프로세스를 죽이고
       테스트 로우 삭제, `tasks.active()`로 죽은 running_tasks 행 정리로
       복구(fix_plans에 새 계획은 안 생겼다). 이후엔 `connect(tempfile 경로)`로
       완전히 격리해서 재검증했다. **다음에 `errors.record()`를 실 DB로
       테스트할 일이 있으면 반드시 임시 경로를 인자로 명시할 것.**

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

- [x] ~~`render.py`(183줄)가 어디서도 import되지 않는다 — 죽은 코드~~
      (2026-08-18 새벽 회차에서 확인) — 코드/설정 점검 결과 **삭제 대상이
      아니다.** `src/autoapply/render.py:139-149` 자체 주석이 이미 사유를
      적어뒀다: `to_pdf`/`render_latest`(마크다운 → HTML → PDF 변환)는
      사람인·자소설이 업로드형 지원이라 그 레시피를 만들 때 그대로 필요하고,
      `config.yaml:63-64`가 "어댑터 검증됨 / 레시피 만들면 켤 것"이라고 그
      상태를 확인해준다. 지금 아무 데서도 안 부르는 건 맞지만(grep으로 재확인,
      테스트도 없음) 그건 사람인/자소설 레시피가 아직 없어서지 죽은 코드라서가
      아니다. 이번 회차에서는 그 판단을 재확인만 하고 코드는 안 건드렸다 —
      삭제는 사람인/자소설 레시피를 영영 안 만들기로 확정한 뒤에나 재고할 것.
      pytest 161건 통과(회귀 없음, 애초에 코드 변경이 없었음).
- [x] ~~텔레그램 `notify()`에만 길이 제한이 없다~~ — 위 보안 섹션 2번에서
      고침(2026-08-18 새벽 회차 + 낮 회차의 HTML 안전 절단·평문 폴백)
- [ ] 텔레그램 400 — **링크가 든 캡션**이 전송 실패(README '남은 것'의 그 항목).
      길이 원인은 위 보안 2번에서 고쳤다. 링크 쪽은 `<a href="...">` HTML 앵커
      파싱 실패로 보이고 그렇다면 같은 평문 폴백이 덮는다(400 → parse_mode 없이
      재전송). 다만 **실제로 실패했던 링크 캡션으로 재현해 확인하기 전까지
      미검증**이다 — 이번 회차의 실물 확인은 길이/태그깨짐 세 건뿐이고,
      `send_photo`는 캡션을 `_clip`으로만 통일했을 뿐 폴백이 없다(사진 재전송은
      같은 파일을 다시 올리는 일이라 이 회차 범위 밖). 다음에 다룰 사람이
      실패한 캡션 원문을 찾아 `notify()`/`send_photo` 어느 쪽이었는지부터 가릴 것
- [x] ~~봇 토큰이 로그·오류 기록에 평문으로 남는다~~ — 위 보안 섹션 3번에서
      고침(2026-08-18 새벽 회차)
- [x] ~~상주 브라우저 홀더 프로세스가 샌다~~ (2026-08-18 새벽 회차에서 고침) —
      `_spawn_resident()`가 Popen 직후 자기 pid를 `.browser_holder_pid`에
      남기고, `_kill_resident()`가 크롬을 죽인 뒤(또는 남의 것이라 못 죽여도)
      항상 `_kill_holder()`로 그 pid를 마커(`PlaywrightSession(hidden=False)`)
      확인 후 같이 정리한다. 실측: 지난 일요일 오후부터 쌓인 스트레이 홀더
      8개 발견(전부 크롬은 죽고 Playwright driver만 매달려 있었다) — 이
      회차에서 마커 확인 후 직접 kill로 정리함.
      검증: `tests/test_session_resident.py` 8건 신설(pid 기록/조회, 대기
      실패해도 홀더 pid는 남는지, 마커 일치 시 kill, pid 재활용으로 마커
      없을 때 skip, 파일 없을 때 noop, ours/foreign 양쪽에서 홀더를 같이
      정리하는지). pytest 161건 통과(153+8, 회귀 없음).
- [x] ~~`orchestrator.py` ↔ `notify/listener.py` 순환 참조~~ (2026-08-18
      낮 회차에서 고침) — 순환의 실체는 자가복구 보류 상태
      (`FIX_HOLD_KEY`/`hold_for_fix`/`release_fix_hold`/`fix_hold`)가
      `notify/listener.py`에 있는데 `orchestrator.py`(`plan()`/`execute()`)가
      그걸 빌려 쓰고, 거꾸로 listener의 `/reverts`·`/revert`가
      `orchestrator.recent_auto_commits`/`revert`를 빌려 쓰는 두 방향
      의존이었다. 그 상태는 본질적으로 자가복구(orchestrator) 소유이므로
      통째로 `orchestrator.py`로 옮겼다 — 이제 `notify/listener.py`가
      `orchestrator`를 top-level import하는 한 방향만 남았고
      `orchestrator.py`는 `notify.listener`를 전혀 모른다.
      검증: `tests/test_imports.py`의 `test_circular_pair_stays_lazy`(순환이
      지역 import로 격리돼 있는지 확인하던 낡은 가정)를
      `test_orchestrator_listener_cycle_is_gone`으로 교체 —
      orchestrator.py가 notify.listener를 다시 참조하지 않는지 정적으로
      확인하고, 두 모듈을 어느 순서로 먼저 import해도(깨끗한 서브프로세스)
      죽지 않는지 실제로 실행해 확인. pytest 161건 통과(160+1, 회귀 없음).
- [ ] 원장 요약이 한 단어 지시('짧게')에서 근거를 지어낼 때가 있다. 기록한 줄을
      폰으로 되돌려주므로 사람이 고칠 수 있지만 스스로는 못 걸러낸다.
      자동 승격은 **폐기**했다 — 한 공고의 특수 요구가 조용히 규칙이 될 위험이
      이득보다 크다. `/revlog`로 보고 `/guide`로 사람이 옮긴다
- [ ] 스킬 정리(prune)를 켜면 스킬이 사라진다. 삭제는 되는데 이후 추가가 안 됨.
      기본 꺼둠 — 사본 스킬은 비어 있어 지금은 추가만 하면 된다.
      (2026-08-18 낮 회차) prune은 계속 꺼둔 채로 관찰성만 추가했다 —
      `runner/resume_editor.py:fill()`의 새로고침 검증 블록이 텍스트 필드만
      대조하고 스킬 칩은 대조 대상이 아니었다(`_fields()`에 없는 칩 UI라서).
      그래서 예전 "11개 → 1개" 손실이 실측 전까지 안 보였다 — 이제 새로고침
      후 `_chip_labels()`로 칩을 다시 읽어 추가한 스킬 중 몇 개가 실제로
      남았는지 `skills_persisted`/`skills_lost`로 반환하고, 잃은 게 있으면
      로그를 남긴다. `ok`(대조 성공 여부)는 안 바꿨다 — 손실 원인이 아직
      미확인이라 지금 넣으면 원인 모를 실패로 하위 워크플로를 막을 수 있다.
      **여전히 안 한 것**: 이 코드로 실제 원인 확인(삭제→추가 경로가 학력
      날짜 칸과 같은 "같은 섹션의 다른 칸을 건드려야 PATCH가 나가는" 부류인지)
      — `fill(dry_run=False)`는 이 파일 자체 주석대로 "되돌리기 어려운
      동작"(사람의 실제 원티드 이력서를 직접 고친다)이라 이 회차에서는 실
      브라우저로 돌려보지 않았다. 다음에 다룰 사람이 dry_run=False로 한 번
      돌려 `skills_lost`가 비어 있는지 확인하는 게 다음 단계다. prune은
      그 확인 전까지 계속 꺼둔다.
      검증: `py_compile` 통과, pytest 161건 통과(회귀 없음 — 이 모듈은
      원래 단위테스트가 없다, 실 브라우저로만 검증되는 모듈). 실 브라우저
      확인은 안 함(위 이유).
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