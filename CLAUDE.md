# Autoapply — AI 작업 가이드

이 문서는 이 저장소를 수정하는 AI coding agent를 위한 **프로젝트 헌법**이다.
README가 사용법을 설명한다면, 이 문서는 **구조를 왜 이렇게 유지해야 하는지와 변경 시 무엇을 지켜야 하는지**를 설명한다.

이 문서의 규칙은 현재 구현보다 우선하는 장기 설계 원칙이다. 다만 실제 코드와 충돌하는 경우에는 기존 동작을 임의로 깨지 말고, 변경 이유와 영향 범위를 먼저 명확히 한다.

---

## 1. Project Identity

Autoapply는 다음 흐름을 자동화한다.

```text
채용공고 수집
    → 적합도 판정
    → 지원가능성 판정
    → 이력서 조립
    → 플랫폼 이력서 등록
    → 지원 폼 준비
    → 사람 검토
    → 제출
    → 증적/원장 기록
```

핵심 특성:

- 수집·정규화·규칙 판정·중복 방지·폼 실행 등은 가능한 한 결정론적으로 처리한다.
- LLM은 비정형 생성/판단이 정말 필요한 구간에만 사용한다.
- 최종 제출은 되돌릴 수 없는 외부 side effect이므로 명시적인 안전 경계를 둔다.
- 플랫폼마다 다른 동작은 adapter/recipe 경계 안에 가둔다.
- 사람의 결정과 시스템의 재판정 결과는 같은 상태값에 섞지 않는다.
- 실패는 조용히 삼키지 않고 상태, 원장, 증적 또는 오류 큐 중 하나에 남긴다.

---

# 2. Core Architecture

전체 구조의 개념적 방향은 다음과 같다.

```text
CLI / Telegram / API
        │
        ▼
    Command Layer
        │
        ▼
      Workflow
        │
        ▼
    Orchestrator
        │
   ┌────┴────┐
   ▼         ▼
 Agents    Services
   │         │
   └────┬────┘
        ▼
      Domain
        │
   ┌────┴─────────────┐
   ▼                  ▼
Adapters          Infrastructure
   │                  │
Browser/Platform   DB / LLM / Files / Queue
```

현재 저장소는 이 구조를 완전히 분리한 상태가 아니다. 특히 `cli.py`에는 workflow와 DB 접근이 일부 남아 있다. 새 코드를 작성할 때는 위 구조를 **목표 방향**으로 사용한다.

---

# 3. Definitions

## 3.1 Command

외부 입력을 내부 workflow로 연결하는 얇은 진입점.

예:

```text
scrape
cycle-apply
night-cycle
autoapply
submit
revise
```

Command의 책임:

- argument parsing
- 입력 검증
- workflow 호출
- 결과 포맷팅
- exit code 처리

Command가 직접 해서는 안 되는 것:

- 복잡한 DB query
- 브라우저 조작
- 플랫폼 selector 처리
- 여러 단계를 묶는 업무 로직
- LLM prompt 실행

**판단 기준:** 함수가 `argparse` 없이도 의미가 있다면 CLI 파일 밖에 있을 가능성이 높다.

---

## 3.2 Workflow

하나의 업무 목표를 끝까지 수행하는 실행 흐름.

예:

```text
PrepareApplication
SubmitApplication
ReviseApplication
NightApplicationCycle
```

Workflow는 여러 agent/service/adapter를 순서대로 조합할 수 있다.

Workflow의 책임:

- 업무 단계 순서
- 단계 사이의 상태 전달
- 중단/재개 지점
- 실패 처리 정책
- human approval boundary 호출

Workflow는 플랫폼의 구체적인 selector나 브라우저 세부사항을 알지 않는다.

---

## 3.3 Orchestrator

**Orchestrator는 "무엇을 다음에 실행할지" 결정한다.**

```text
현재 상태
  + 정책
  + 실행 결과
        ↓
   다음 action 결정
```

Orchestrator가 직접 해서는 안 되는 것:

- Playwright 클릭
- 플랫폼별 selector 판단
- SQL 작성
- LLM prompt 직접 관리
- 개별 폼 필드 입력

Orchestrator가 할 수 있는 것:

- 어떤 workflow/agent를 실행할지 결정
- retry 가능한 실패와 사람 개입이 필요한 실패 구분
- 상태 전이에 따라 다음 단계를 선택
- 실행 중단/재개 정책 적용
- recovery workflow로 넘기기

### 중요한 구분

```text
Workflow = "이 업무를 어떤 단계로 수행할 것인가"
Orchestrator = "현재 상태에서 다음에 무엇을 수행할 것인가"
```

이 둘을 합치지 않는다.

---

## 3.4 Agent

**Agent는 LLM을 뜻하지 않는다.**

Agent는 특정 목표를 독립적으로 수행하고 결과를 반환하는 실행 단위다.

예:

```text
ScreeningAgent
ResumeAgent
ApplicationAgent
VerificationAgent
RecoveryAgent
```

Agent는 다음 형태를 지향한다.

```python
result = agent.run(context)
```

결과에는 최소한 다음 중 필요한 것을 명시적으로 담는다.

```text
success
status
reason
artifacts / evidence
retryable
requires_human
```

### LLM 사용 원칙

Agent 내부에 LLM이 있을 수 있지만 필수는 아니다.

```text
ScreeningAgent       → deterministic rules
ApplicationAgent    → browser + recipe
VerificationAgent   → rules + optional vision
ResumeAgent         → LLM
RecoveryAgent       → rules + optional LLM
```

**Agent라는 이름만 보고 LLM을 추가하지 않는다.**

---

## 3.5 Adapter

Adapter는 플랫폼/외부 시스템의 차이를 내부 추상화 뒤에 숨긴다.

현재 대상 예:

```text
Wanted
Saramin
Jasoseol
```

Adapter가 담당하는 것:

- 플랫폼별 수집
- 플랫폼별 지원 흐름
- 플랫폼별 데이터 형식 변환
- 플랫폼 특유의 동작/응답 해석

Adapter가 담당하지 않는 것:

- 전체 지원 workflow
- 전역 quota 정책
- 사람 승인 정책
- 일반적인 Application 상태 모델

### 플랫폼 변경 원칙

플랫폼의 HTML/DOM/receipt/form selector가 변경되었다면 먼저 다음 순서로 생각한다.

```text
recipe / platform adapter
        ↓
platform-specific service
        ↓
정말 필요한 경우에만 shared layer
```

Orchestrator, domain, CLI를 수정하는 것은 마지막 수단이다.

---

# 4. Domain Boundaries

핵심 도메인 개념은 장기적으로 다음을 명시적으로 유지한다.

```text
Job
Candidate
Resume
Application
ApplicationResult
Receipt
ReviewDecision
```

특히 `Application`과 `Receipt`는 플랫폼마다 표현 방식이 달라도 내부에서는 공통 개념으로 유지한다.

예:

```text
Wanted DOM
   ↓
WantedReceiptParser
   ↓
Receipt
   ↓
ApplicationResult
```

**플랫폼의 HTML 문구가 domain layer로 올라오지 않게 한다.**

---

# 5. Pipeline vs Workflow vs Orchestrator

세 개를 혼동하지 않는다.

### Pipeline

데이터를 단계적으로 변환한다.

```text
collect
 → normalize
 → screening
 → applicability
 → persist
```

현재 `pipeline.py`가 담당하는 성격이다.

### Workflow

하나의 업무를 수행한다.

```text
prepare application
 → build resume
 → register resume
 → fill form
 → capture evidence
```

### Orchestrator

현재 상태를 보고 workflow/agent를 무엇부터 실행할지 정한다.

```text
resume missing → PrepareApplication
form ready     → HumanReview
approved       → SubmitApplication
session dead   → HumanRequired
```

---

# 6. Deterministic First

코드로 정확하게 표현할 수 있는 것은 LLM으로 대체하지 않는다.

현재 원칙적으로 LLM 0회인 영역:

- 공고 수집
- 정규화/canonical key 생성
- 하드컷 및 기본 적합도 계산
- 지원가능성 blocker 판단
- 중복 판정
- quota 및 claim
- 레시피 기반 폼 입력
- 저장 여부 확인
- 제출 완료 화면의 명확한 확인

LLM을 고려할 수 있는 영역:

- 자기소개서/서술형 생성
- 이미지형 공고 판독
- 애매한 적합도 판단
- 예상하지 못한 비정형 질문의 semantic mapping
- recipe가 깨졌을 때의 visual fallback
- 오류 원인 분석/코드 수정 계획

**LLM을 넣는 이유는 "자동화하기 편해서"가 아니라 "규칙으로 안정적으로 표현하기 어려워서"여야 한다.**

---

# 7. Safety Invariants

다음은 기능 구현보다 우선하는 불변조건이다.

## 7.1 Application ledger

모든 실제 제출은 application ledger의 관문을 거친다.

```text
preflight
  ↓
claim
  ↓
run live submit
  ↓
mark_submitted / mark_failed
```

`canonical_key`의 중복 방지는 최종적으로 DB의 UNIQUE 제약이 방어한다.

---

## 7.2 Dry-run is the default

```text
dry-run → 실제 브라우저 작업 가능 / submit은 실행하지 않음
live     → 실제 제출
```

dry-run은 지원 자리를 선점하지 않는다.

실제 제출은 다음을 모두 고려한다.

- 중복 지원 여부
- quota
- login/session 상태
- 사람 승인 여부
- 제출 후 evidence 기록

---

## 7.3 Human approval boundary

최종 제출은 되돌릴 수 없는 외부 side effect다.

사람 검토가 필요한 현재 운영 경계:

```text
준비된 지원서
 → 스크린샷/evidence
 → 승인 / 폐기 / 수정요청
 → 제출
```

자동화 편의를 위해 이 경계를 임의로 제거하지 않는다.

---

## 7.4 Login failure

로그인 세션 만료는 일반 retry 대상으로 취급하지 않는다.

현재 원칙:

```text
로그인 페이지로 확실히 이동됨 → 죽음
로그인 상태에서만 접근 가능한 URL 확인 → 살아있음
둘 다 아님 → 확인 불가
```

확인 불가를 로그인 만료로 단정하지 않는다.

사람에게 로그인을 요구해야 하는 경우 자동 로그인/비밀번호 저장 경로를 새로 만들지 않는다.

---

## 7.5 Resume ownership

자동 cleanup은 `made_resumes` 등 소유 근거가 있는 이력서만 대상으로 한다.

사람이 만든 이력서나 원본 이력서를 추측해서 삭제하지 않는다.

**삭제보다 보존이 안전하다.**

---

## 7.6 Human decision vs system calculation

사람의 폐기/수정 결정과 `applicability` 같은 시스템 재판정 결과를 같은 상태에 저장하지 않는다.

예:

```text
system calculation → applicability
human decision     → dropped_at / review decision
```

재판정이 사람의 결정을 덮어쓰면 안 된다.

---

# 8. Failure Semantics

모든 실패를 같은 것으로 취급하지 않는다.

### Retryable

일시적인 네트워크 오류, transient timeout 등.

### Deterministic failure

같은 조건이면 계속 실패하는 schema/selector/recipe 오류.

### External constraint

로그인 만료, 외부 ATS, 서비스 장애 등.

### Human required

사람만 처리할 수 있는 로그인/최종 판단.

### Suspicious / unknown

상태를 확실히 판단할 수 없는 경우.

특히 **unknown을 failure로 변환하지 않는다.**

---

# 9. Retry / Idempotency

재실행해도 중복 외부 side effect가 발생하지 않는 방향을 우선한다.

확인해야 할 것:

```text
이 단계가 다시 실행되면?
  ├─ DB 중복 기록?
  ├─ 이력서 중복 생성?
  ├─ 동일 공고 재지원?
  ├─ quota 중복 차감?
  └─ 사람에게 동일 승인 요청 재전송?
```

특히 제출 단계는 **retry가 곧 duplicate application이 될 수 있다.**

제출 버튼을 눌렀지만 완료 화면을 못 본 경우에는 실제 제출 여부가 불명확할 수 있으므로 단순 release 후 재시도하지 않는다.

---

# 10. CLI Rules

현재 `cli.py`는 기능이 많이 성장해 있으며 일부 workflow/DB/browser logic을 포함한다. 새 코드는 이 패턴을 더 확장하지 않는다.

### CLI에 두어도 되는 것

- argparse 정의
- argument validation
- command routing
- JSON output
- exit code
- 아주 얇은 compatibility wrapper

### CLI에서 빼야 하는 것

- 긴 workflow
- DB business query
- 브라우저 실행
- 플랫폼 logic
- notification composition
- retry/circuit breaker
- application state transition

목표 형태:

```python
args = parse_args()
result = command.run(args)
output(result)
```

---

# 11. Current CLI Refactoring Direction

현재 `cli.py`가 특히 큰 책임을 가진 부분은 다음 성격이다.

```text
_dispatch()
_night_cycle()
_autoapply()
_report_prepared()
_apply_with()
```

향후 책임을 다음 방향으로 이동한다.

```text
cli/commands/
    → command routing

workflows/
    → night-cycle / prepare / submit / revise

services/
    → application / resume / notification 조합

notify/
    → Telegram message/button delivery

repositories or domain services/
    → DB access
```

**리팩터링의 목표는 줄 수를 줄이는 것이 아니라 책임 경계를 복구하는 것이다.**

---

# 12. Platform Change Protocol

새 플랫폼을 추가하거나 기존 플랫폼의 지원/receipt 동작이 바뀌었을 때:

1. 기존 adapter/recipe로 재현한다.
2. 변경이 platform-specific인지 확인한다.
3. 가능한 가장 좁은 platform boundary에서 수정한다.
4. 공통 domain/API 계약은 가능한 한 유지한다.
5. deterministic parser/selector로 해결 가능한 문제에 LLM을 추가하지 않는다.
6. receipt/status 변화는 내부 `ApplicationResult`/`Receipt`로 변환해 격리한다.
7. dry-run에서 실제 화면/evidence를 검증한다.
8. live submit 전에 중복/ledger/approval 경계를 다시 확인한다.

### 이상적인 변경 범위

```text
Wanted receipt 변경
        ↓
platform/wanted/receipt.py
        ↓
테스트
```

이지,

```text
Wanted receipt 변경
        ↓
CLI 수정
        ↓
Orchestrator 수정
        ↓
Agent 수정
```

이 되어서는 안 된다.

---

# 13. Recipe Rules

Recipe는 플랫폼별 동작을 선언적으로 표현하는 핵심 자산이다.

Recipe가 담당할 수 있는 것:

- goto
- click
- fill
- upload
- expect/check
- screenshot
- submit boundary

Recipe에 business policy를 넣지 않는다.

예:

```text
좋음: "이 selector를 클릭한다"
나쁨: "점수가 80 이상이면 이 selector를 클릭한다"
```

business policy는 workflow/agent/domain에서 결정하고 recipe는 그 결정을 플랫폼 동작으로 표현한다.

---

# 14. Browser Session Rules

브라우저 session은 교체 가능한 실행 backend로 취급한다.

현재 `Session` abstraction을 유지하며, Playwright/CDP 등 구체 구현이 상위 workflow로 새어나가지 않게 한다.

브라우저를 사용하는 모든 긴 작업은:

- 충돌/동시 접근을 방지한다.
- 단계 경계에서 취소 가능성을 확인한다.
- 가능한 경우 실제 저장 결과를 읽어 다시 대조한다.
- 제출 여부가 확실하지 않은 경우 추측하지 않는다.

---

# 15. Evidence First

외부 side effect가 있는 작업은 가능하면 사람이 검증할 수 있는 증적을 남긴다.

예:

```text
지원 준비
 → 실제 화면 screenshot
 → review
 → approval
 → live submit
 → evidence + ledger
```

로컬에서 만든 문서 이미지보다 실제 플랫폼 화면의 증적을 우선한다.

---

# 16. LLM Rules

LLM 호출은 가능한 한 목적별로 분리하고 결과를 구조화한다.

LLM을 호출할 때:

- 왜 LLM이 필요한지 코드에 드러나야 한다.
- 입력과 출력 계약을 정의한다.
- 실패 가능성을 고려한다.
- 사용량/비용을 기록할 수 있어야 한다.
- deterministic fallback이 있다면 그 경로를 우선 고려한다.

LLM을 사용하지 않아도 되는 것을 LLM에 보내지 않는다.

특히 대량 공고 처리 단계에서 불필요한 LLM 호출을 추가하지 않는다.

---

# 17. Self-improvement Rules

현재 시스템은 오류 큐 → 계획 → 실행 → revert 형태의 자기개선 경로를 가진다.

자기개선의 원칙:

```text
오류 감지
  ↓
원인 분류
  ↓
수정 계획
  ↓
위험도 확인
  ↓
승인된 변경 실행
  ↓
검증
  ↓
commit / revert
```

AI가 발견한 문제를 바로 main에 임의 반영하지 않는다.

특히 다음은 구분한다.

```text
runtime recovery
    ≠
code modification
```

한 공고에서 실패했다고 곧바로 코드를 변경하지 않는다. 반복 증거와 재현 가능성을 확인한다.

---

# 18. Human Feedback Rules

사람의 수정 요청은 두 종류를 구분한다.

### Job-specific instruction

현재 공고의 근거가 있을 때만 적용.

### General preference

공고와 무관하게 어떻게 작성할지에 대한 취향.

job-specific 지시를 일반 규칙으로 승격하지 않는다.

반복적으로 등장하는 지적은 사람이 검토한 뒤 `resume-guide` 같은 일반 규칙으로 승격한다.

---

# 19. Change Protocol for AI Agents

코드를 수정하기 전에 다음 질문을 순서대로 확인한다.

1. 이 문제는 domain 문제인가, workflow 문제인가, platform 문제인가?
2. 이미 존재하는 abstraction으로 해결할 수 있는가?
3. 이 로직은 CLI에 있을 이유가 있는가?
4. 이 로직은 특정 플랫폼에만 해당하는가?
5. LLM이 정말 필요한가?
6. 재실행해도 안전한가?
7. DB/ledger/quota/approval invariant를 깨지 않는가?
8. 실패 시 상태를 복구하거나 사람에게 전달할 수 있는가?
9. 기존 테스트/dry-run으로 검증 가능한가?
10. 새 abstraction을 추가한다면 반복되는 책임을 실제로 제거하는가?

---

# 20. Smallest Owning Layer

버그 수정은 **문제를 실제로 소유한 가장 작은 계층**에서 한다.

예:

```text
Wanted selector bug
    → Wanted adapter/recipe

Quota bug
    → application/ledger policy

Resume prompt bug
    → resume/LLM layer

Telegram formatting bug
    → notify

Workflow ordering bug
    → workflow/orchestrator

CLI argument bug
    → CLI
```

하위 계층 문제를 상위 계층에서 우회하지 않는다.

---

# 21. Anti-patterns

다음 패턴을 새 코드에서 만들지 않는다.

```text
❌ CLI가 DB + browser + LLM + Telegram을 모두 직접 호출
❌ adapter를 우회해 플랫폼별 코드를 여러 곳에 복제
❌ selector 문제를 orchestrator 변경으로 해결
❌ deterministic rule을 LLM prompt로 대체
❌ 모든 실패를 catch 후 성공처럼 반환
❌ login failure를 무한 retry
❌ submit uncertainty를 release 후 바로 재제출
❌ 사람 승인 없이 irreversible action 실행
❌ 사람이 만든 리소스를 "아마 우리가 만든 것"이라고 추측해서 삭제
❌ 하나의 job에서 받은 특수 지시를 전역 rule로 자동 승격
❌ 코드 수정이 필요한 문제와 runtime retry 문제를 동일하게 처리
```

---

# 22. Testing Strategy

기본 순서:

```text
unit
  ↓
integration
  ↓
dry-run
  ↓
real browser verification
  ↓
live submit (필요한 경우에만)
```

특히 browser automation 변경은 다음을 확인한다.

- 입력한 값과 저장된 값이 동일한가
- 예상한 이력서가 선택됐는가
- screenshot이 실제 화면을 보여주는가
- submit 이전에 approval boundary가 유지되는가
- 완료 화면 또는 명확한 receipt가 확인되는가

---

# 23. Current Runtime Constraints

현재 구현에서 중요한 운영 제약:

- 브라우저는 resident CDP session을 재사용하는 방향이다.
- 로그인은 사람의 OAuth 세션에 의존하며 자동 로그인하지 않는다.
- dry-run이 기본이며 `live`가 명시되어야 실제 제출한다.
- Telegram은 운영 검토/알림 창구다.
- 지원준비는 screenshot/evidence를 통해 사람 검토가 가능해야 한다.
- `launchd` 기반 주기 실행이 존재한다.
- 현재 플랫폼에는 Wanted, Saramin, Jasoseol adapter가 있다.

현재 남은 검증/운영 과제는 TODO/NEXT를 기준으로 확인한다. 문서의 오래된 예시 숫자를 현재 상태로 간주하지 않는다.

---

# 24. File/Directory Responsibility Map

현재 코드 기준으로 주요 책임은 대략 다음과 같다.

```text
cli.py
  CLI entrypoint / compatibility layer

pipeline.py
  collection → screening → applicability → persist

agent.py
  application ledger / claim / quota / submission state gate

assemble.py
  resume generation / review / editor JSON / registration state

screening/
  fit + applicability decision logic

adapters/
  platform-specific integration

runner/
  browser/session/recipe execution

vision.py
  screenshot-based verification

notify/
  Telegram and human interaction transport

errors.py
  failure recording / issue queue

orchestrator.py
  self-improvement planning/execution; keep distinct from runtime application workflow

config.yaml
  operational rules and thresholds that should be configurable rather than hard-coded

recipes/
  platform interaction recipes
```

이 책임표는 **현재 코드의 사실**과 **향후 구조의 목표**를 구분해서 이해한다.

---

# 25. When Adding a New Component

새 파일/클래스를 만들기 전에 다음 질문에 답한다.

```text
1. 이 책임을 기존 모듈에 넣을 수 없는가?
2. 이 책임은 어느 boundary에 속하는가?
3. 이 코드가 알아야 하는 것은 어디까지인가?
4. 이 코드가 몰라야 하는 것은 무엇인가?
5. 외부 side effect가 있는가?
6. 재실행은 안전한가?
7. 테스트에서 외부 시스템 없이 검증할 수 있는가?
```

답이 명확하지 않으면 파일부터 만들지 않는다. 먼저 boundary를 정한다.

---

# 26. Final Principle

이 저장소를 확장할 때 가장 중요한 목표는 **AI가 더 많은 일을 하는 것**이 아니다.

목표는:

```text
더 많은 자동화
    +
더 작은 변경 범위
    +
더 강한 재현성
    +
더 명확한 실패 의미
    +
더 안전한 외부 side effect
```

이다.

새 기능을 구현할 때 항상 다음 질문을 마지막으로 확인한다.

> **"이 변경이 다음 플랫폼 변경에서 어떤 파일을 건드리게 만들 것인가?"**

가능하면 답은 **플랫폼 adapter/recipe의 작은 범위**여야 한다.

그 답이 CLI, orchestrator, domain 전체를 건드리는 구조라면 먼저 abstraction을 다시 검토한다.
