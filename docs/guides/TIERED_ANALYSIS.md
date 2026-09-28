# 모델 계층화와 로컬 평가 · 0.1.0a3

이 버전은 구현·Mock 검증된 개발 alpha다. 실제 계정의 모델 접근권한·CLI sandbox 호환성과 의미 품질은 아직 실측하지 않았다. 모델 이름을 가상의 최신 ID로 하드코딩하지 않으며, 사용자가 설치된 CLI에서 접근 가능한 ID를 선택한다.

## 1. 프로젝트 범위 선별은 AI보다 먼저

`project analyze .`는 현재 폴더를 `Scope.resolve()`로 정규화한다. Git이 있으면 공통 Git 디렉터리와 확인된 worktree를 연결하며 하위 폴더 지정은 유지한다. Codex의 `sessions`와 `archived_sessions` 아래 JSONL을 재귀 탐색한다. 날짜 폴더나 파일명의 프로젝트 문자열은 귀속 근거가 아니다.

`parse_codex()`는 `session_meta.cwd`를 초기 경로로 사용하고 `turn_context.cwd`로 갱신한다. 개별 메시지의 명시 경로, 도구 인자의 `workdir`/`cwd`가 있으면 레코드 수준에서 반영한다. 상대 workdir은 알려진 절대 경로에 대해서만 해석한다. 도구 결과는 대응하는 call ID의 경로를 따른다. 확인 가능한 경로가 없으면 이름·본문 키워드로 추측하지 않는다. `Scope.includes()`는 경로 구성요소를 비교하므로 `/app`과 `/app-backup`을 혼동하지 않는다.

세션이 다른 경로에서 시작했더라도 중간에 분석 대상 디렉터리의 작업을 했다면 그 레코드는 선택할 수 있다. 첫 metadata만 보고 세션 전체를 버리는 최적화는 하지 않는다. 초기 스캔은 계정 폴더의 여러 파일을 로컬에서 읽을 수 있으나, 범위 밖 본문을 LLM 분류 입력으로 보내지 않는다.

```bash
project scan /path/to/project
# runner_calls: 0
# codex_selection: examined / selected / excluded / unattributed
```

**한계:** 이것은 기록된 경로의 구조적 귀속 판정이다. 한 메시지 안의 다른 프로젝트 언급을 의미적으로 지워 주는 기능은 아니다. shell 문자열 내부의 `cd`나 임의 절대 파일 경로를 실행·해석해 프로젝트 전체 귀속을 추측하지 않는다. 기록에 없는 경로 이동, 삭제된 worktree, 별도 clone의 연결은 보장하지 않는다. 최초 전체 파일 탐색 I/O를 제거하는 전역 색인은 아직 구현하지 않았다.

## 2. 모델·worker 선택

```bash
# 아래 두 환경변수에는 해당 CLI에서 접근 가능한 모델 ID를 넣는다.
project analyze . --runner codex \
  --extract-model "$EXTRACT_MODEL" \
  --integrate-model "$INTEGRATE_MODEL" \
  --workers 2 --max-calls 50
```

`--model`은 공통 요청값이다. 단계별 값이 있으면 해당 단계가 우선한다. 둘 다 없으면 CLI 자체 기본값이 아니라 ContextTrail의 Runner 기본 모델(Codex `gpt-6-sol`, Claude `sonnet`)을 이름으로 지정한다. 단계별 추론 수준은 `--extract-effort`·`--integrate-effort`·`--escalation-effort`로 정한다. 옵션은 프로젝트 상태에 보존한다. 조건부 의미 재검토는 기본으로 실행하며, 재검토 모델은 `--escalation-model`로 설정한다(없으면 Runner 기본 모델). 이용 불가능한 모델이나 인증·한도 오류에서 다른 모델/공급자로 몰래 전환하지 않는다.

- `extract`: 정규화된 새 원문에서 분류와 사건·근거 추출을 한 번에 수행한다.
- 검증 복구: 추출 계약이 제한된 수리 뒤에도 실패한 경우에만, 설정된 경우 재검토 모델로 구조적 복구를 시도한다. 유효한 초안은 의미 검토를 위해 다시 추출하지 않는다.
- `integrate`: 후보와 정확한 인용 근거, 제한된 기존 맥락을 받아 기존 그래프에 적용할 Delta를 만든다. **새 원문 전체를 기본으로 다시 전달하지 않는다.** 필요하면 같은 ReadRequest 계약으로 추가 자료를 요청한다.
- 의미 재검토: GraphDelta 이후 구체적인 review issue가 생긴 경우에만 통합 결과를 재검토한다.

병렬화하는 것은 작은 추출 작업이다. 최대 `workers`개를 같은 기준 그래프로 추출한 뒤, 통합·게시를 입력 계획 순서로 직렬 수행한다. 모든 worker가 그래프를 동시에 수정하지 않는다. 원본 CLI의 네이티브 subagent 기능은 켜지 않는다. worker별 독립 Runner 객체/CLI 실행과 host ledger를 사용한다.

기본 worker는 1, 허용 범위는 1~8이다. 처음에는 1~2로 실제 계정 한도와 품질을 확인한다. 실행당 기본 host call 상한은 30, eval은 20이다. 처리할 작업 단위 수(`--units N` 또는 확인 창에서 입력)를 정하면 상한은 단위당 6회(`CALLS_PER_UNIT`)이고, `--max-calls`를 주면 그 값이 우선한다. 둘 다 그 실행에만 적용하고 저장하지 않는다(2026-09-26·27 변경, DECISIONS 참조). extraction/integration/escalation의 추가 근거 요청과 repair 호출도 같은 상한에 포함된다. 완료되지 않은 단위는 다음 사용자의 명시적 실행에서 이어서 처리한다. 자동 rate-limit 회피나 다른 계정 전환은 없다.

## 3. 검증 복구와 의미 재검토

추출 결과는 구조·인용 계약에 실패한 경우에만 제한된 수리와, 설정된 경우 재검토 모델에 의한 검증 복구를 받을 수 있다. timeout·인증·권한·일반 Runner 오류는 다른 모델을 호출하지 않는다. 계약을 통과한 추출 초안은 통합 단계로 전달되며, 선택적 의미 검토는 실제 GraphDelta가 만들어진 뒤 구체적인 `review_issues`를 기준으로 수행한다. 검토 호출 실패는 게시를 막고, 성공해도 미해결 이슈는 감사 자료에 남긴다.

## 4. LLM Ops

```bash
project ops /path/to/project
project ops /path/to/project --run-id run_...
project ops /path/to/project --details
```

SQLite `llm_calls`에 role/model 요청값/worker/run/unit/attempt, prompt·schema·input hash, 근거·repair round, routing 이유, 결과 상태와 제공된 usage를 기록한다. 원문 전체나 인증 토큰, 비공개 내부 추론을 기본 trace에 저장하지 않는다. 실제 모델명이 보고되지 않으면 unknown이며 요청 alias를 실제 모델명으로 간주하지 않는다.

`host_calls`는 우리 코드가 Runner task를 호출한 횟수다. CLI 내부의 모든 provider model turn 수와 동일하다고 보장하지 않는다. 토큰 통계가 없는 호출은 missing으로 남기며 과금액을 만들어 내지 않는다. 호출 비율은 원문 비율이나 토큰 절감 비율이 아니다. 전체 prompt의 문자량/사용 가능한 usage와 품질을 함께 비교해야 한다.

호출 ledger는 **로컬**이다. LangSmith 계정·서버나 별도 사용자 대시보드는 필요하지 않다. 기존 인증을 사용하는 CLI의 실행/전송/계정 제한은 종전 Runner 계약을 따른다. 현재 구현은 파일 기반 인증만 연결하며 API-key 환경변수를 따로 주입하는 새 Runner는 추가하지 않았다.

## 5. 평가 실행

```bash
# 합성 데이터, 외부 LLM 호출 없음
project eval --fixture demo --runner mock --workers 2 \
  --extract-model mock-small --integrate-model mock-strong \
  --escalation-model mock-strong --output /tmp/pf-eval-offline

# 설치·인증된 CLI로 작은 동일 합성 사례를 실제 평가
project eval --fixture demo --runner codex \
  --extract-model "$EXTRACT_MODEL" --integrate-model "$INTEGRATE_MODEL" \
  --escalation-model "$INTEGRATE_MODEL" \
  --workers 2 --max-calls 20 --yes --output /tmp/pf-eval-live
```

Live는 자료 전송·해당 계정 사용량을 수반하므로 명시적 `--yes`가 필요하다. Mock은 demo에만 쓸 수 있다. 실제 프로젝트에 Mock을 붙여 의미 분석에 성공한 것처럼 표시하지 않는다.

각 실험은 새/빈 디렉터리의 별도 DB, 동일 frozen fixture, 초기 graph version 0에서 시작한다. `fixture.json`, `report.json`, `calls.json`, `flow.json`, `review.md`, `state/state.sqlite`가 남는다. 성공한 첫 실행 후 같은 입력을 다시 열어 no-op을 검사하며, 실패한 live trial을 자동으로 재시도하지 않는다.

A/B는 같은 fixture에 대해 서로 다른 **빈 output 경로**를 지정한다. 출력의 `source_snapshot_id`·`fixture_digest`·`initial_graph_version`을 비교한다. 모델별 호출/문자량/usage와 사람이 정의한 기대 사건/상태/근거/관계를 함께 확인한다. 정답 fixture의 일부 검사에 통과했다는 사실은 일반적 의미 정확도나 비용 절감의 실측이 아니다.

### 자신의 실제 기록으로 작은 fixture 만들기

소스 ZIP의 helper는 먼저 프로젝트 경로로 선별하고, 명시한 세션의 작은 구간만 내보낸다. production DB나 원본 기록을 수정하지 않는다.

```bash
python scripts/make_eval_fixture.py /path/to/project \
  --session SESSION_ID --start 0 --max-records 20 --output /tmp/my-fixture.json
project eval --fixture /tmp/my-fixture.json --runner codex \
  --max-calls 20 --yes --output /tmp/pf-real-eval
```

fixture는 민감한 원문을 포함한다. 내용을 직접 보고 앞뒤 맥락, call/result, fragment 완결성을 확인하고 `expectations`를 추가한다. 빈 기대 목록으로 실행하면 의미 정확도는 `not_scored`다. fixture의 원래 locator는 보존용 metadata이며 실제 파일/임의 revision을 읽을 권한을 주지 않는다.

## 6. 검증되지 않은 것

실제 Codex·Claude 계정별 모델 접근, 독립 CLI의 병렬 계정 제한, 실제 출력의 모델/usage metadata, 위험 규칙의 누락률, 모델 계층화의 비용·지연·의미 품질 효과는 live 평가가 필요하다. 이번 버전은 구조와 안전성 회귀를 검증했으며 이러한 성과를 실측했다고 주장하지 않는다.
