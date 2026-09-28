# Project Flow 0.1.0a4 — 실제 계정 호출 전 점검

검증일: 2026-09-23. 기준 소스: 제공된 `project-flow-0.1.0a3.zip`. 수정 결과: 개발 alpha `0.1.0a4`.

## 1. 결론과 검증 경계

실제 계정을 쓰기 전에 점검할 문제가 남아 있었다. 기존 121개 테스트가 통과한 상태에서도, 새 경계 조건 검사 17개는 a3에서 실패했다. 해당 문제를 수정하고 추가 방어·호환 검사까지 포함한 회귀는 **150개 통과**였다(2026-09-25 a3 시점). 이 문서의 17/138/150은 그 시점의 이력이고, 현재 스위트는 350개다 — [tests-a4.txt](artifacts/tests-a4.txt). 검사 건수는 독립 버그 수나 의미 정확도 점수가 아니다.

이번 작업은 동일한 assistant의 직접 코드 점검, 실행 가능한 회귀 테스트, 합성 응답 실행기를 통한 단계별 입출력 검사다. **독립 LLM subagent/리뷰어 실행 수는 0, 실제 Codex/Claude 모델 호출 수는 0**이다. 이 환경에 `codex`, `claude`, `bwrap` 실행 파일과 해당 계정 인증이 없으며, 이용 가능한 연결 도구에도 독립 agent를 생성하는 실행 기능이 확인되지 않았다. 역할 이름을 붙인 테스트 객체를 실제 subagent로 세지 않았다.

제품의 Harness-managed worker 구조와 provider-native agent 제한은 유지했다. 이번 점검을 위해 제품의 native subagent를 켜거나 도구 권한을 확대하지 않았다. 두 실제 압축에서는 원문을 읽었을 뿐, 원문에 포함된 명령·코드·테스트를 실행하지 않았다.

## 2. 실행 결과

| 구분 | 실측 |
|---|---|
| 수정 전 기존 전체 회귀 | 121 passed |
| a3에 새 경계 조건을 적용한 최초 결과 | 17 failed |
| 첫 수정 직후 전체 회귀 | 138 passed |
| 추가 방어·metadata·경로 테스트 포함 회귀(a3 시점) | **150 passed** |
| 합성 단계별 walkthrough | 주 실행 5 host calls, 실제 LLM 0회 |
| walkthrough 준비용 별도 합성 실행 | 1 host call; 주 실행 5회에 포함하지 않음 |
| 동일 입력 walkthrough 재실행 | noop, host calls 0 |
| 실제 archive 선택 레코드 | 31,748 = Codex 15,521 + Claude 16,227 |
| 실제 긴 세션 경계 probe | 13개 모두 바로 앞 기록 조회 가능 |
| 원본 ZIP 무결성 | 처리 전후 SHA-256 동일 |

현재 원시 pytest 출력은 [tests-a4.txt](artifacts/tests-a4.txt), [tests-a4.xml](artifacts/tests-a4.xml) — 350 passed. 위 표의 150은 그 이전 a3 시점의 값이라 이 파일과 숫자가 다르다. 추가 검사 코드는 `tests/test_prelive_audit.py`에 있다. a3 최초 실패 출력 묶음은 정리했다. Python 3.13의 이 컨테이너에서 확인했으며 모든 지원 Python/OS/CLI 조합을 시험한 것은 아니다.

## 3. 발견하고 수정한 문제

### 3.1 도구 호출 ID만으로 이전 맥락을 찾던 문제

`Harness.context()`가 같은 `tool_call_id`만 비교하면 서로 다른 세션/공급자의 동명 도구 결과를 이전 맥락에 넣을 수 있었다. 이제 `(provider, session_id, tool_call_id)`를 함께 비교한다. 이는 프로젝트 경로 선별 뒤에 이루어지는 context 결합의 문제이며, 모든 범위 밖 파일이 실제 전송됐다는 실측이 아니다.

관련 검사: `test_tool_context_requires_provider_and_session`의 두 조건.

### 3.2 긴 세션의 뒤쪽 작업에 가까운 원문이 없던 문제

과거 원문 조회 목록이 세션의 처음 250개로 채워질 수 있었다. 세션 끝부분의 작업에서는 바로 앞 기록조차 조회 대상에 없을 수 있었다. 이제 현재 작업과 가까운 동일 공급자/세션 기록을 우선하고, 이전 네 개 기록을 예산 안에서 `context_only`로 제공한다. 신규 처리량으로 중복 계산하지 않는다. 조회 목록은 여전히 250개로 제한되며, 전체 기록에 대한 무제한 의미 검색을 구현한 것은 아니다.

기존 사건의 최근순 정렬에서도 무작위 run UUID 대신 저장 그래프의 안정적인 삽입 순서를 사용한다. 의미적으로 가장 관련 있는 사건을 언제나 찾는다는 보장은 아니다.

관련 검사: late-unit manifest, previous-turn context_only, random-run-UUID ordering. 실제 archive의 13개 긴 세션 끝부분에서도 확인했다.

### 3.3 추가 조회 근거의 재검토 전달·중단 복구 누락

작은 추출기가 읽었지만 인용하지 않은 추가 근거가 재검토 모델 입력에 빠질 수 있었다. 중간 draft 재사용에서도 같은 문제가 있었다. 이제 `inherited_evidence_rounds`로 실제 조회 결과를 다음 단계에 전달한다. draft에는 허용된 조회 요청과 응답 digest를 보존하고, 재시작 시 고정 snapshot에서 다시 읽어 digest를 확인한다. 내용이 바뀌거나 없으면 오래된 draft를 그대로 믿지 않고 재추출한다.

인용하지 않은 추가 원문 전체를 상시 DB trace에 복제하지 않는다. 임시 실제 응답과 영속 dependency manifest를 구분한다. 통합 단계에는 여전히 후보·인용·제한된 맥락을 기본 제공하며 원문 전체를 다시 넣지 않는다.

관련 검사: escalation uncited reads, failed-review resume.

### 3.4 미완료 입력을 complete/noop으로 숨기는 문제

손상 JSONL, 귀속 미상 구간, 입력 한도 초과 같은 진단이 있어도 `complete` 또는 다음 실행 `noop`으로 표시될 수 있었다. 미완료 진단은 최초와 재실행 모두 `partial`로 남긴다. 알려진 네 가지 정보성 Git 안내만 예외로 두고 알 수 없는 진단은 보수적으로 미완료로 처리한다. 같은 오류 안내 때문에 모델을 반복 호출하는 것은 아니다.

관련 검사: incomplete-input first/repeat의 세 조건.

### 3.5 관계 무효화·미해결 항목 해결 근거의 export 누락

관계의 `invalidation` 및 미해결 항목의 `resolution` 안에 있는 evidence ID도 재귀적으로 수집한다. 그래프 JSON에 ID가 있는데 내보내기/조회 evidence 묶음에 본문이 없는 상태를 막는다.

관련 검사: nested evidence export.

### 3.6 Eval 기대 조건을 모델 호출 뒤에 검사하던 문제

fixture `expectations`의 구조·허용 키·사건 상태·관계·참조 ID를 Runner 생성과 출력 디렉터리 생성 전에 검사한다. 잘못된 실험 설정을 발견하기 위해 유료 호출부터 실행하지 않는다. 정답 조건이 비어 있으면 기존처럼 의미 평가는 `not_scored`다.

관련 검사: malformed expectations zero factory calls / no artifacts.

### 3.7 CLI 출력의 실패·미지원 이벤트를 성공으로 수용하던 문제

Codex의 no-tools 분석 출력에서 미지원 item/tool 이벤트를 거부하고 `turn.completed`가 없는 부분 출력을 게시하지 않는다. Claude의 permission denial·비정상 subtype을 정상 결과로 수용하지 않는다. 알려지지 않은 정상 형식도 차단될 수 있으므로 실제 설치 버전과의 호환성 smoke가 여전히 필요하다.

이 검사는 **출력 수용/게시 차단**이다. 모델의 도구 시도 자체가 runtime에서 완전히 차단됐다는 실측으로 바꿔 말하지 않는다. bubblewrap/CLI 안전 옵션 검증은 실제 환경에서 별도로 해야 한다.

관련 검사: unknown Codex events 두 조건, partial stream, Claude denied tool.

### 3.8 조회 오류의 Ops 상태와 미전달 자료의 인용 권한

`needs_evidence` 응답도 snapshot/unit/base graph를 검사하고, 잘못된 요청에서 호출 ledger가 `running`으로 남지 않도록 종료 상태를 기록한다. 조회 결과가 입력 예산을 초과하면 모델에 실제로 전달하지 않은 자료는 제공 목록·dependency·인용 가능 evidence pool에 편입하지 않는다.

관련 검사: malformed read envelope; over-budget reply cannot authorize unseen citation.

### 3.9 원문 시각·부모 관계 변경 감지와 기존 cache 이행

본문이 그대로여도 native record ID/부모 관계/기록 시각이 바뀌면 해석에 영향을 줄 수 있다. v2 content hash에는 해당 native metadata를 포함한다. locator 이동이나 호스트 관측 시각만 달라졌다고 새 의미 기록으로 처리하지 않는다.

a3의 완료 기록은 예전 hash와 현재 본문, 신규 hash 대상 metadata가 모두 일치하는 경우에만 같은 ingest transaction에서 hash/dependency를 이행한다. 그 경우 기존 완료 단위는 모델 호출 없이 재사용한다. 실제 native timestamp 변경까지 무시하는 이행은 하지 않는다. 미완료 draft는 새 routing/adapter 계약 때문에 재처리가 필요할 수 있다.

관련 검사: native metadata 변경, v1 완료 단위 이행 noop, 실제 timestamp 변경 재분석.

### 3.10 Claude subagent 메타파일의 경로·구조 방어

로그 안의 session ID를 안전한 단일 경로 구성요소로 제한하고 메타파일·transcript의 symlink/root 범위를 검사한다. JSON 메타파일은 객체여야 하며 최대 128,000 bytes다. 과대·비객체·root 탈출·symlink 입력을 거부한다. 검증 가능한 정상 부모 호출/결과와 subagent 연결은 계속 허용한다.

관련 검사: metadata symlink, session path escape, nonobject JSON, oversized metadata, normal link.

## 4. 단계별 실행 — 실제 입출력이 남는 합성 테스트

실행:

```bash
python scripts/prelive_walkthrough.py --output /tmp/pf-prelive-walkthrough
```

출력은 새/빈 디렉터리여야 한다. 정상 제품 Ops는 metadata 중심이지만 이 예제의 `calls/`는 **전부 합성 데이터이므로** task/schema/response를 저장한다. private 로그를 이 캡처 스크립트에 대입하는 용도로 설계하지 않았다.

| 순서 | 실제 테스트 실행기의 응답 | 호스트가 확인한 동작 |
|---|---|---|
| 1 | extract: needs_evidence | 허용된 이전 원문 조회 요청 |
| 2 | extract: complete | 추가 원문을 받은 후보 추출 |
| 3 | escalation: complete | 앞 단계가 읽고 인용하지 않은 근거도 전달 |
| 4 | integrate: 틀린 인용 제출 | validation_error, 게시 차단 |
| 5 | integrate repair: complete | 한 번의 제한된 수정 후 유효한 graph 게시 |
| 재실행 | Runner 호출 없음 | noop, 0 host calls |

주 실행은 5 host calls이며, 이미 처리한 이전 구간을 만드는 준비용 합성 호출 1회는 별도다. 전체에서 실제 모델 호출은 0이다. 테스트가 정해진 답안을 생성하므로 “LLM이 의미를 제대로 이해했다”는 검증은 아니다.

결과 파일: `report.json`, `ops-calls.json`, `flow.json`, `calls/*-task.json`, `calls/*-schema.json`, `calls/*-response.json`. `project ops`의 실제 stage/role/read_round/repair_round/status와 대응한다.

## 5. 두 실제 archive 재검사

`/home/<user>/dev/<project>` 및 하위 경로를 허용한 명시적 Scope를 사용했다. Git checkout은 업로드되지 않았으므로 다른 clone/worktree의 연결은 추측하지 않았다.

- JSONL 156개: Claude 40개, Codex 116개.
- 선택 정규화 레코드 31,748개: Claude 16,227개, Codex 15,521개.
- Source ID 중복 없음; 모든 선택 cwd가 지정 범위 안; 이번 입력에서 parser limitations 배열은 비어 있음.
- 13개 긴 세션 끝부분에 대해 바로 앞 기록이 직접 맥락 또는 허용 조회 목록에 포함됨을 확인.
- LLM 호출 0회, 원본 archive SHA-256 처리 전후 동일.

자세한 값은 [archive-audit-a4.json](artifacts/archive-audit-a4.json)에 있다. 이 값들은 구조적 선별/맥락 범위 검사이지 의미적 프로젝트 귀속의 완전성이나 사건 추출 recall이 아니다. 이 corpus에서 진단이 없다는 사실이 모든 미래 로그 형식을 지원한다는 뜻도 아니다.

## 6. 실제 계정 테스트에서 남은 필수 확인

아래는 이 컨테이너에서 완료하지 않은 검증이다. 소규모 live smoke를 실행할 수 있는 개발 alpha를 전달하는 것이며, 전체 로그 분석의 완료 보장은 아니다.

| gate | 통과 기준 |
|---|---|
| 설치 CLI/모델 접근 | 실제 `--help`·버전·계정으로 필수 옵션과 요청 모델을 확인. 미지원 옵션을 조용히 제거하지 않음 |
| runtime 권한 제한 | 원본 프로젝트·무관한 홈 파일·hooks·MCP·shell을 실제 격리 안에서 접근/실행하지 못하는지 확인 |
| 인증 | CLI가 credential을 소비하며 프로그래밍 코드/trace는 인증 본문을 복제하지 않음. 파일 인증과 갱신, keyring 제약 확인 |
| 두 Runner structured output | Codex와 Claude 각각 작은 fixture에서 schema/완료 이벤트/usage 형식을 실측 |
| 의미 품질 | 완료 보고≠성공, 사용자 반박, 실패→방향 전환, 경계 분할·subagent·요약 출처를 사람이 대조 |
| 모델 계층화 | 동일 frozen fixture/초기 graph로 small→strong과 strong-only 비교. 호출 비율을 비용·원문 절감률로 바꾸지 않음 |
| 중단/한도 | workers=1부터 시작; 호출 상한·timeout·취소·재시작·zero-call rerun을 실제 프로세스로 확인 |

현재 Runner는 실제 버전에서 확인해야 할 필수 flag 집합을 요구한다. 예컨대 Claude의 `--restricted`/`--safe-mode` 등은 설치된 바이너리가 지원해야 진행되며, 이 환경에서 해당 조합의 작동을 증명하지 않았다. 보안 flag를 삭제하거나 permission bypass로 회피하지 않는다. CLI 안전 모드 한 개만으로 파일/도구 격리가 완성됐다고 가정하지 않는다.

`max-calls`는 host task invocation 수의 상한이다. CLI 내부 모든 provider turn 수, 구독 사용량 또는 금액의 상한으로 해석하면 안 된다. 실제 model/usage가 보고되지 않으면 unknown/missing을 유지한다.

첫 live 점검 예시(접근 가능한 실제 모델 ID를 사용):

```bash
project doctor --runner codex
project scan /path/to/project

# EXTRACT_MODEL, INTEGRATE_MODEL에는 해당 계정에서 쓸 수 있는 ID를 설정한다.
# output은 새/빈 디렉터리여야 한다. --yes는 외부 전송/계정 사용량에 대한 동의다.
project eval --fixture demo --runner codex \
  --extract-model "$EXTRACT_MODEL" --integrate-model "$INTEGRATE_MODEL" \
  --escalation-model "$INTEGRATE_MODEL" \
  --workers 1 --max-calls 12 --yes --output /tmp/pf-live-codex-a4
```

Claude는 해당 runner를 별도 output 경로로 시험한다. 한 runner가 실패했다고 다른 공급자/계정으로 자동 전환하지 않는다. demo는 합성 자료이며, 이것을 통과한 후 작은 실제 fixture를 사람이 대조해야 한다.

## 7. 독립 subagent 리뷰를 수행할 환경을 위한 작업 카드

[REVIEW_TASKS.md](../plans/REVIEW_TASKS.md)에 source/provenance, extraction/handoff, integration/evidence, Ops/runtime 네 검토 작업을 정의했다. **이 문서가 있다는 것과 실제 독립 agent가 실행됐다는 것은 다르다.** 이번에 실행한 것은 위의 직접 점검과 테스트다. 제품의 provider-native agent 기능을 켤 필요 없이 개발 환경에서 각각 읽기 전용 검토를 맡길 수 있다.

## 8. 재현 자료와 개인정보

배포 소스에는 제품 코드·회귀 검사·합성 walkthrough·문서를 포함한다. 별도 검증 묶음에는 pytest 결과, archive의 수치/식별 metadata, 합성 단계별 입출력과 코드 diff를 포함한다. 실제 대화 본문, 인증 파일, 원본 로그 ZIP은 다시 넣지 않았다. archive 검사 metadata에도 프로젝트 경로/session ID는 포함되므로 비공개 개발 자료로 다룬다.

[TEST_REPORT.md](TEST_REPORT.md), [A3_VALIDATION.md](A3_VALIDATION.md) 등은 이전 버전의 역사적 결과다. 이 파일의 a4 결과로 대체해 읽되, 과거 실측 수치를 소급 수정하지 않았다.


## 9. 설치 wheel에서의 별도 검증

소스 checkout과 분리된 `/mnt/data/project-flow-prelive-audit/installed-a4`에 최종 wheel을 설치했다. 해당 경로에서 import되는 것을 확인하고 `python -m projectflow --version`이 `0.1.0a4`임을 확인했다.

설치본의 계층화 Mock eval은 extract 2 / escalation 1 / integrate 2, 기대 조건 8개 통과·0개 실패, 동일 입력 재실행 noop/0 calls였다. 같은 설치본으로 별도의 5단계 추가근거/잘못된 인용/repair walkthrough도 완료했고, 후속 noop/0 calls를 확인했다. 두 실험은 서로 다른 output/state를 사용한다.

설치본의 doctor는 Codex·Claude 모두 installed=false, bubblewrap=false, live_model_test=not_run으로 보고했다. 이 결과를 설치 환경/실계정 smoke 성공으로 계산하지 않았다. 최종 ZIP과 wheel은 동일한 제품 코드로 생성했으며 배포 manifest에 source hash를 남겼다.
