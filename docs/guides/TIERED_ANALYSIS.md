# 분석 호출과 로컬 평가

개발 alpha다. 실제 Codex·Claude CLI로 픽스처와 실제 프로젝트를 측정한 기록은 `docs/plans/PERFORMANCE_PLAN.md`와 `docs/plans/PIPELINE_V2.md` §8에 있고, 의미 품질은 아직 사람이 확인한 단위가 적다(README의 "What has been measured"). 모델은 ContextTrail의 기본 이름(Codex `gpt-luna`, Claude `haiku`)이나 사용자가 고른 이름으로 지정하며, 버전이 없는 Codex 이름은 설치된 Codex의 목록에서 그 계열의 최신 모델로 바꾼다.

## 1. 프로젝트 범위 선별은 AI보다 먼저

`contexttrail analyze .`는 현재 폴더를 `Scope.resolve()`로 정규화한다. Git이 있으면 공통 Git 디렉터리와 확인된 worktree를 연결하며 하위 폴더 지정은 유지한다. Codex의 `sessions`와 `archived_sessions` 아래 JSONL을 재귀 탐색한다. 날짜 폴더나 파일명의 프로젝트 문자열은 귀속 근거가 아니다.

`parse_codex()`는 `session_meta.cwd`를 초기 경로로 사용하고 `turn_context.cwd`로 갱신한다. 개별 메시지의 명시 경로, 도구 인자의 `workdir`/`cwd`가 있으면 레코드 수준에서 반영한다. 상대 workdir은 알려진 절대 경로에 대해서만 해석한다. 도구 결과는 대응하는 call ID의 경로를 따른다. 확인 가능한 경로가 없으면 이름·본문 키워드로 추측하지 않는다. `Scope.includes()`는 경로 구성요소를 비교하므로 `/app`과 `/app-backup`을 혼동하지 않는다.

Claude Code 기록은 행마다 적힌 `cwd`로, opencode 세션은 `session.directory`로(bash 호출의 `workdir`이 있으면 그 호출은 그 경로로) 같은 규칙에 따라 귀속한다. 세션이 다른 경로에서 시작했더라도 중간에 분석 대상 디렉터리의 작업을 했다면 그 레코드는 선택할 수 있다. 첫 metadata만 보고 세션 전체를 버리는 최적화는 하지 않는다. 초기 스캔은 계정 폴더의 여러 파일을 로컬에서 읽을 수 있으나, 범위 밖 본문을 LLM 분류 입력으로 보내지 않는다.

```bash
contexttrail scan /path/to/project
# runner_calls: 0
# codex_selection: files_examined / files_with_selected_records / unattributed_records / transcript_files_reused
# sources: 도구마다 읽은 위치, 출처(flag/saved/environment/default), 파일·기록 수
```

**한계:** 이것은 기록된 경로의 구조적 귀속 판정이다. 한 메시지 안의 다른 프로젝트 언급을 의미적으로 지워 주는 기능은 아니다. shell 문자열 내부의 `cd`나 임의 절대 파일 경로를 실행·해석해 프로젝트 전체 귀속을 추측하지 않는다. 기록에 없는 경로 이동, 삭제된 worktree, 별도 clone의 연결은 보장하지 않는다. 첫 스캔은 각 도구 폴더의 기록 파일을 모두 읽는다. 그 뒤로는 이 프로젝트의 기록이 없던 파일 중 크기와 수정 시각이 그대로인 것은 다시 읽지 않는다(`ScanCache`, `scan`의 `transcript_files_reused`).

## 2. 분석 호출: 단위마다 한 번 (2026-10-20부터)

```bash
contexttrail analyze . --runner codex --model gpt-sol --extract-effort high --workers 2 --max-calls 50
```

단계별 모델(추출·통합·재검토)로 나누던 v1 파이프라인은 지웠다(`docs/plans/PIPELINE_V2.md`). 이제 작업 단위마다 모델 호출은 **이야기 호출 하나**다.

- 코드가 먼저 작업을 기록한다: 도구 호출에서 수정·검사(결과 포함)·커밋·다른 실행을 찾고, 검사 명령이 고친 파일 모두를 이름으로 가리키면 그 수정을 검증한 것으로 잇는다.
- 모델은 도구 출력을 읽지 않는다. 사람과 어시스턴트가 쓴 글을 코드가 나눈 블록(B1…), 사람의 요청(Q1…), 작업 개요(K1…, R1…: 수정 뒤 실행의 명령과 마지막 줄 포함), 기존 사건만 읽고, 작업 이름·이야기 사건(목표, 결정과 적힌 이유, 결론, 방향 전환)·요청마다의 답·수정마다 시험한 실행을 답한다. 근거는 블록 ID로만 인용하고, 정확한 원문 줄은 코드가 넣는다.
- 모델이 주장한 검증과 답은 기록과 맞을 때만 남는다(결과를 아는 실행이 수정 뒤에 있고 그 뒤 어시스턴트가 보고했을 때, 요청 뒤 같은 세션의 답글을 인용할 때). 나머지는 그 단위의 한계로 남는다.
- 게시는 코드가 한다(모델 호출 없음). 답이 끝내 검사에 떨어지면 작업 기록만 남기고, 아무것도 통과하지 못하면 그 단위를 건너뛴다.

`--model`은 Runner 모델이고, `--extract-model`·`--extract-effort`는 이야기 호출의 모델과 추론 수준이다(이름은 예전 추출 호출에서 이어 왔다). 아무것도 주지 않으면 CLI 자체 기본값이 아니라 ContextTrail의 Runner 기본 모델(Codex `gpt-luna`는 설치된 Codex의 최신 Luna로 바꿔서, Claude `haiku`)을 이름으로 지정한다. 옵션은 프로젝트 상태에 보존한다. 이용 불가능한 모델이나 인증·한도 오류에서 다른 모델/공급자로 몰래 전환하지 않는다. Codex에서는 `gpt-sol`이 더 정확하지만 토큰당 가격이 Luna의 약 20배다(`docs/DECISIONS.md` 2026-10-20).

`--workers`는 여러 단위의 이야기 호출을 같은 기준 그래프로 나란히 보내고, 게시는 입력 계획 순서로 직렬 수행한다. 기본 worker는 1, 허용 범위는 1~8이다. 실행당 기본 host call 상한은 30, eval은 20이다. 처리할 작업 단위 수(`--units N` 또는 확인 창에서 입력)를 정하면 상한은 단위당 3회(`CALLS_PER_UNIT`: 이야기 호출, 수리, 읽을 수 없는 답의 재요청)이고, `--max-calls`를 주면 그 값이 우선한다. 둘 다 그 실행에만 적용하고 저장하지 않는다. 아직 보내지 않은 단위는 다음 명시적 실행에서 이어서 처리한다. 검사에 떨어져 건너뛴 단위는 기록이나 설정이 바뀌기 전에는 다시 보내지 않는다(`unit_failures`). 같은 입력으로 다시 보내려면 `--retry-failed`를 준다. 자동 rate-limit 회피나 다른 계정 전환은 없다.

## 3. LLM Ops

```bash
contexttrail ops /path/to/project
contexttrail ops /path/to/project --run-id run_...
contexttrail ops /path/to/project --details
```

SQLite `llm_calls`에 role/model 요청값/worker/run/unit/attempt, prompt·schema·input hash, 근거·repair round, routing 이유, 결과 상태와 제공된 usage를 기록한다. 원문 전체나 인증 토큰, 비공개 내부 추론을 기본 trace에 저장하지 않는다. 실제 모델명이 보고되지 않으면 unknown이며 요청 alias를 실제 모델명으로 간주하지 않는다.

`host_calls`는 우리 코드가 Runner task를 호출한 횟수다. CLI 내부의 모든 provider model turn 수와 동일하다고 보장하지 않는다. 토큰 통계가 없는 호출은 missing으로 남기며 과금액을 만들어 내지 않는다. 호출 비율은 원문 비율이나 토큰 절감 비율이 아니다. 전체 prompt의 문자량/사용 가능한 usage와 품질을 함께 비교해야 한다.

호출 ledger는 **로컬**이다. LangSmith 계정·서버나 별도 사용자 대시보드는 필요하지 않다. 기존 인증을 사용하는 CLI의 실행/전송/계정 제한은 종전 Runner 계약을 따른다. 현재 구현은 파일 기반 인증만 연결하며 API-key 환경변수를 따로 주입하는 새 Runner는 추가하지 않았다.

## 4. 평가 실행

```bash
# 합성 데이터, 외부 LLM 호출 없음
contexttrail eval --fixture demo --runner mock --workers 2 --output /tmp/pf-eval-offline

# 설치·인증된 CLI로 작은 동일 합성 사례를 실제 평가
contexttrail eval --fixture demo --runner codex --model gpt-luna \
  --workers 2 --max-calls 20 --yes --output /tmp/pf-eval-live
```

Live는 자료 전송·해당 계정 사용량을 수반하므로 명시적 `--yes`가 필요하다. Mock은 demo에만 쓸 수 있다. 실제 프로젝트에 Mock을 붙여 의미 분석에 성공한 것처럼 표시하지 않는다.

각 실험은 새/빈 디렉터리의 별도 DB, 동일 frozen fixture, 초기 graph version 0에서 시작한다. `fixture.json`, `report.json`, `calls.json`, `flow.json`, `review.md`, 호출마다 나란히 보는 `review.html`과 그 바탕인 호출별 검증 전 원래 응답(`call-review/`, 통과·거절 모두)과 그 실험의 상태 DB가 남는다. 성공한 첫 실행 후 같은 입력을 다시 열어 no-op을 검사하며, 실패한 live trial을 자동으로 재시도하지 않는다.

A/B는 같은 fixture에 대해 서로 다른 **빈 output 경로**를 지정한다. 출력의 `source_snapshot_id`·`fixture_digest`·`initial_graph_version`을 비교한다. 모델별 호출/문자량/usage와 사람이 정의한 기대 사건/상태/근거/관계를 함께 확인한다. 정답 fixture의 일부 검사에 통과했다는 사실은 일반적 의미 정확도나 비용 절감의 실측이 아니다.

### 자신의 실제 기록으로 작은 fixture 만들기

소스 트리의 `scripts/make_eval_fixture.py`는 먼저 프로젝트 경로로 선별하고, 명시한 세션의 작은 구간만 내보낸다. production DB나 원본 기록을 수정하지 않는다.

```bash
python scripts/make_eval_fixture.py /path/to/project \
  --session SESSION_ID --start 0 --max-records 20 --output /tmp/my-fixture.json
contexttrail eval --fixture /tmp/my-fixture.json --runner codex \
  --max-calls 20 --yes --output /tmp/pf-real-eval
```

fixture는 민감한 원문을 포함한다. 내용을 직접 보고 앞뒤 맥락, call/result, fragment 완결성을 확인하고 `expectations`를 추가한다. 빈 기대 목록으로 실행하면 의미 정확도는 `not_scored`다. fixture의 원래 locator는 보존용 metadata이며 실제 파일/임의 revision을 읽을 권한을 주지 않는다.

## 5. 검증되지 않은 것

이야기 호출의 측정은 `docs/plans/PIPELINE_V2.md` §8에 있다(두 프로젝트, Haiku·Luna·Sol: 단위당 시간·입력 토큰·비용, 요청의 답과 변경의 검사 연결 수, 같은 코드의 반복 실행 편차). 아직 재지 않은 것: 큰 프로젝트를 처음부터 끝까지 분석한 결과를 사람이 확인한 것, 독립 CLI를 나란히 돌릴 때의 계정 제한, 계정마다 쓸 수 있는 모델의 차이, Linux bubblewrap 안의 Claude Runner.
