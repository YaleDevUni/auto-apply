# Autoapply — AI 작업 가이드

이 저장소를 수정하는 AI coding agent를 위한 **원칙**이다.

구조·파일 책임·현재 동작은 **코드를 읽어서 파악한다.** 이 문서에 적지 않는다 —
적으면 반드시 낡고, 낡은 지도는 없는 지도보다 나쁘다. 여기 남긴 것은
**코드를 아무리 읽어도 알 수 없는 것**뿐이다: 왜 이렇게 하기로 했고, 무엇을
깨면 안 되는가.

지난 결정의 근거와 실측은 `NEXT.md`에 있다. 원칙이 현재 구현과 충돌하면 기존
동작을 임의로 깨지 말고 변경 이유와 영향 범위를 먼저 밝힌다.

---

## 1. 무엇을 하는 시스템인가

```
채용공고 수집 → 적합도 판정 → 지원가능성 판정 → 이력서 조립
   → 플랫폼 이력서 등록 → 지원 폼 준비 → 사람 검토 → 제출 → 증적/원장 기록
```

- 수집·정규화·규칙 판정·중복 방지·폼 실행은 가능한 한 **결정론적으로** 처리한다.
- LLM은 비정형 생성/판단이 정말 필요한 구간에만 쓴다.
- 최종 제출은 **되돌릴 수 없는 외부 side effect**다.
- 플랫폼마다 다른 동작은 adapter/recipe 경계 안에 가둔다.
- 실패는 조용히 삼키지 않는다. 상태·원장·증적·오류 큐 중 하나에 남긴다.

---

## 2. 안전 불변조건 — 기능보다 우선한다

### 2.1 제출은 원장을 거친다

```
preflight → claim → live submit → mark_submitted / mark_failed
```

`canonical_key` 중복은 최종적으로 DB UNIQUE 제약이 방어한다.

### 2.2 dry-run이 기본이다

`live`가 **명시**되어야 실제로 제출한다. dry-run은 브라우저를 쓰지만 지원
자리를 선점하지 않는다.

### 2.3 사람 승인 경계를 없애지 않는다

```
준비된 지원서 → 실제 화면 증적 → 승인 / 폐기 / 수정요청 → 제출
```

자동화 편의를 위해 이 경계를 우회하는 경로를 만들지 않는다.

### 2.4 로그인 실패는 retry 대상이 아니다

```
로그인 페이지로 확실히 이동됨        → 죽음
로그인 상태에서만 되는 URL 확인됨    → 살아있음
둘 다 아님                          → 확인 불가
```

**확인 불가를 만료로 단정하지 않는다.** 자동 로그인/비밀번호 저장 경로를 새로
만들지 않는다.

### 2.5 사람이 만든 리소스를 추측으로 지우지 않는다

자동 cleanup은 소유 근거(`made_resumes` 등)가 있는 것만 대상으로 한다.
**삭제보다 보존이 안전하다.**

### 2.6 사람의 결정과 시스템의 재판정을 같은 값에 담지 않는다

```
system  → applicability (재계산됨)
human   → dropped_at / review decision (덮어쓰면 안 됨)
```

### 2.7 재실행이 중복 side effect를 내면 안 된다

이 단계가 다시 실행되면 — DB 중복 기록? 이력서 중복 생성? 동일 공고 재지원?
quota 중복 차감? 같은 승인 요청 재전송?

**제출 단계는 retry가 곧 중복지원이다.** 버튼을 눌렀는데 완료 화면을 못 봤다면
실제 제출 여부가 불명확하므로, 자리를 놓고 재시도하지 않는다.

---

## 3. 계층 — 코드가 말해주지 않는 구분

```
CLI / Telegram / API  →  Workflow  →  Agents · Services  →  Domain
                              ↑                                ↓
                        Orchestrator            Adapters · Infrastructure
```

**Workflow** = "이 업무를 어떤 단계로 수행하는가"
**Orchestrator** = "현재 상태에서 다음에 무엇을 수행하는가"

이 둘을 합치지 않는다. (현재 `orchestrator.py`는 **자기개선 전용**이며 런타임
지원 workflow와 별개다.)

**Workflow는 진입점을 모른다.** `print`/`sys.exit`/`argparse.Namespace`를 쓰지
않는다. argparse도 HTTP도 같은 함수를 부를 수 있어야 한다.

**CLI에 두어도 되는 것**: argparse, 검증, 라우팅, JSON 출력, exit code, 얇은 wrapper.
**CLI에 두면 안 되는 것**: workflow, DB 쿼리, 브라우저 실행, 플랫폼 로직,
알림 조립, retry/서킷브레이커, 상태 전이.

> 판단 기준: 함수가 `argparse` 없이도 의미가 있다면 CLI 밖에 있어야 한다.

**Agent는 LLM을 뜻하지 않는다.** 목표를 독립적으로 수행하고 결과를 반환하는
실행 단위다. 이름만 보고 LLM을 넣지 않는다.

**Adapter**는 플랫폼 차이를 숨긴다. 전체 workflow·quota 정책·승인 정책은 모른다.
**플랫폼의 HTML 문구가 domain으로 올라오지 않게 한다.**

**Recipe**는 플랫폼 동작을 선언적으로 표현한다. business policy를 넣지 않는다.

```
좋음: "이 selector를 클릭한다"
나쁨: "점수가 80 이상이면 이 selector를 클릭한다"
```

---

## 4. 결정론 우선

코드로 정확히 표현할 수 있는 것을 LLM으로 대체하지 않는다.

**LLM 0회를 지키는 영역**: 수집, 정규화/canonical key, 하드컷·기본 적합도,
blocker 판단, 중복 판정, quota·claim, 레시피 기반 폼 입력, 저장 확인,
명확한 제출 완료 확인.

**LLM을 고려할 수 있는 영역**: 자기소개서/서술형 생성, 이미지형 공고 판독,
애매한 적합도, 예상 못 한 비정형 질문의 semantic mapping, recipe가 깨졌을 때의
visual fallback, 오류 원인 분석.

> LLM을 넣는 이유는 "자동화하기 편해서"가 아니라 **"규칙으로 안정적으로
> 표현하기 어려워서"**여야 한다.

LLM을 쓸 때는 왜 필요한지가 코드에 드러나야 하고, 입출력 계약·실패 가능성·
사용량 기록·deterministic fallback을 함께 고려한다. 대량 공고 처리 단계에
불필요한 호출을 추가하지 않는다.

---

## 5. 실패의 의미를 구분한다

```
retryable              일시적 네트워크·transient timeout
deterministic failure  같은 조건이면 계속 실패 (schema/selector/recipe)
external constraint    로그인 만료, 외부 ATS, 서비스 장애
human required         사람만 처리 가능
suspicious / unknown   상태를 확실히 판단할 수 없음
```

**unknown을 failure로 변환하지 않는다.**

그리고 **runtime recovery ≠ code modification.** 한 공고에서 실패했다고 곧바로
코드를 고치지 않는다. 반복 증거와 재현 가능성을 확인한다.

---

## 6. 가장 작은 소유 계층에서 고친다

```
Wanted selector 버그   → Wanted adapter/recipe
Quota 버그            → application/ledger 정책
Resume prompt 버그    → resume/LLM 계층
Telegram 형식 버그    → notify
Workflow 순서 버그    → workflow/orchestrator
CLI argument 버그     → CLI
```

하위 계층 문제를 상위 계층에서 우회하지 않는다.

**플랫폼이 바뀌었을 때의 순서**: recipe/adapter → platform-specific service →
정말 필요할 때만 shared layer. Orchestrator·domain·CLI 수정은 마지막 수단이다.
deterministic parser로 풀리는 문제에 LLM을 추가하지 않는다.

---

## 7. 자기개선

```
오류 감지 → 원인 분류 → 수정 계획 → 위험도 확인 → 승인된 변경 → 검증 → commit/revert
```

AI가 발견한 문제를 **바로 main에 임의 반영하지 않는다.** 자동반영은 위험도가
낮고, 검증 명령이 실제로 실행돼 통과했고, 결정론 관문을 지났을 때만이다.

---

## 8. 사람의 피드백

```
job-specific instruction  현재 공고의 근거가 있을 때만 적용
general preference        공고와 무관한 작성 취향
```

job-specific 지시를 일반 규칙으로 **자동 승격하지 않는다.** 반복되는 지적은
사람이 검토한 뒤 `resume-guide` 같은 일반 규칙으로 올린다.

---

## 9. 검증

```
unit → integration → dry-run → 실제 브라우저 확인 → (필요할 때만) live submit
```

브라우저 자동화를 바꿨다면: 입력한 값과 저장된 값이 같은가 / 예상한 이력서가
선택됐는가 / 스크린샷이 실제 화면인가 / 제출 전 승인 경계가 유지되는가 /
완료 화면이나 명확한 receipt가 확인되는가.

**증적은 실제 플랫폼 화면을 우선한다.** 로컬에서 그려낸 이미지가 아니다.

그리고 **검증으로 돌리면 안 되는 것을 구분한다.** 실행하면 폰으로 알림이
나가거나 이력서가 조립되는 명령은 검증이 아니라 실행이다.

---

## 10. Anti-patterns

```
❌ CLI가 DB + browser + LLM + Telegram을 직접 호출
❌ adapter를 우회해 플랫폼 코드를 여러 곳에 복제
❌ selector 문제를 orchestrator 변경으로 해결
❌ deterministic rule을 LLM prompt로 대체
❌ 모든 실패를 catch 후 성공처럼 반환
❌ login failure를 무한 retry
❌ submit uncertainty를 release 후 즉시 재제출
❌ 사람 승인 없이 irreversible action 실행
❌ 사람이 만든 리소스를 추측해서 삭제
❌ 한 공고의 특수 지시를 전역 rule로 자동 승격
❌ 코드 수정이 필요한 문제와 runtime retry 문제를 동일 처리
```

---

## 11. 변경 전 확인

1. domain 문제인가, workflow 문제인가, platform 문제인가?
2. 기존 abstraction으로 해결되는가?
3. 이 로직이 CLI에 있을 이유가 있는가?
4. 특정 플랫폼에만 해당하는가?
5. LLM이 정말 필요한가?
6. 재실행해도 안전한가?
7. ledger/quota/approval 불변조건을 깨지 않는가?
8. 실패 시 상태를 복구하거나 사람에게 전달할 수 있는가?
9. 기존 테스트/dry-run으로 검증 가능한가?
10. 새 abstraction이 실제로 반복되는 책임을 제거하는가?

새 파일을 만들기 전에: 기존 모듈에 넣을 수 없는가? 어느 boundary에 속하는가?
무엇을 **몰라야** 하는가? 외부 side effect가 있는가? 외부 시스템 없이 테스트할
수 있는가? — 답이 명확하지 않으면 파일부터 만들지 않는다. boundary를 먼저 정한다.

---

## 12. 개발 후

- 기능 단위로 커밋한다. 커밋 메시지에는 **무엇을 왜**를 남긴다.
- 검증을 실제로 돌리고, 돌리지 않은 것은 돌리지 않았다고 적는다.
- 결정의 근거와 실측은 `NEXT.md`에 남긴다. 낡은 항목은 닫는다.

---

## 13. 마지막 원칙

목표는 AI가 더 많은 일을 하는 것이 아니다.

```
더 많은 자동화 + 더 작은 변경 범위 + 더 강한 재현성
             + 더 명확한 실패 의미 + 더 안전한 외부 side effect
```

새 기능을 구현할 때 마지막으로 확인한다:

> **"이 변경이 다음 플랫폼 변경에서 어떤 파일을 건드리게 만들 것인가?"**

답이 **플랫폼 adapter/recipe의 작은 범위**가 아니라 CLI·orchestrator·domain
전체라면, 먼저 abstraction을 다시 검토한다.
