# LLM Ops 계약 · 0.1.0a3

Project Flow는 재현성·증분 재사용·오류 진단을 위한 로컬 실행 ledger를 기본으로 사용한다. 원문과 graph가 제품의 기준이며 외부 tracing 서비스는 필수 의존성이 아니다. `--langsmith`는 분석 구조를 설계하는 개발자용 추적이다. 배포하거나 다른 사용자가 쓰는 기능이 아니므로 명령 도움말에 나타나지 않는다.

## 분석 파이프라인

```text
Source snapshot
  → WorkUnit 계획
  → Extract LLM
      ↔ bounded ReadRequest (최대 2 round, round당 최대 3 request)
      ↘ validation error → bounded repair (최대 1회)
  → Integrate LLM (후보가 있을 때만)
  → schema/evidence/snapshot/base-version 검증
  → 사용자 발화 노드·대화 차례 연결 (코드, 모델 호출 없음)
  → SQLite transaction publish
```

모델에게 보내는 입력에서는 긴 ID(`src_…`, `ev_…`, `edge_…` 등 24자리)를 `S12`, `E3`, `L2` 같은 짧은 ID로 바꾼다. 문자열 값 전체가 ID일 때만 바꾸므로 원문 안에 적힌 ID는 그대로다. 응답은 검증 전에 원래 ID로 되돌리고, 저장·검증·호출 기록은 모두 원래 ID를 쓴다. `input_chars`·`input_digest`는 실제로 보낸 입력 기준이다. 평가의 `call-review/`에는 대응표(`id_aliases`)도 남는다.

추출 입력에는 원문 외에 코드가 만든 두 목록이 붙는다. `user_requests`는 이 단위의 사용자 발화, `tool_steps`는 도구 호출마다 edit·read·run 힌트와 대상 파일·명령, 결과 첫 줄이다. 파일을 바꾼 것이 확실한 호출(`edit`, 실패 결과 제외)은 모두 추출된 사건의 근거에 들어가야 하며, 빠지면 검증 오류로 수리 기회를 쓴다.

같은 완료 input은 재호출하지 않는다. 이미 저장된 extraction은 integration 실패 뒤에도 재사용한다. append는 새 records와 실제 제공한 context/evidence dependency만 처리한다.

## 각 LLM 호출에 저장하는 관측값

- run_id, unit_id, stage, attempt, 시작/종료 시각, status
- runner, runner_version, adapter_version, model(=requested_model), reasoning_effort
- actual_model: Claude가 응답에 보고한 모델. Codex는 모델을 보고하지 않아 비어 있다
- prompt_hash, schema_hash, input_digest, input_chars
- read_round, repair_round
- output_digest, output_chars, duration_ms
- Runner가 실제 제공한 usage (없으면 추정하지 않음)
- validation/runner failure의 제한된 error type/message

기본 ledger에는 raw full prompt, 전체 model output, stdout/stderr, 인증정보, 비공개 chain-of-thought를 저장하지 않는다. 필요한 근거와 구조화된 분석 결과는 별도의 source/evidence/work-unit/graph 테이블에 저장한다.

고정 fixture의 `project eval`은 별도 임시 평가 디렉터리의 `call-review/`에 정확한 task·schema·구조화 응답을 저장한다. 일반 project scope의 ledger에는 적용하지 않는다. `review.html`은 모델 후보, 제출한 인용, 실제 원문 줄, 검증 실패를 함께 보여주며 `project review EVAL_DIR`로 재생성할 수 있다. 원본 모델 응답이 없는 과거 평가는 오류가 가리키는 원문 줄까지만 보여준다. 로컬 검토는 LangSmith 전송과 독립적이다.

## 모델과 추론 수준

Runner는 모델을 항상 이름으로 지정한다. 격리 환경은 사용자의 CLI 설정 파일을 가져가지 않으므로, 지정하지 않으면 CLI 자체 기본값이 버전·계정에 따라 조용히 바뀔 수 있기 때문이다. 기본값은 `runners/cli_runner.py`의 `DEFAULT_MODELS`(Codex `gpt-sol`, Claude `sonnet`)이고 `--model`이 우선한다. Codex는 버전이 붙은 이름만 받으므로, 버전 없는 이름(`gpt-sol`, `gpt-luna`)은 첫 호출 전 점검에서 설치된 Codex에 들어 있는 모델 목록(`codex debug models --bundled`, 격리 안에서 실행)의 그 계열 최신 버전으로 바꿔 지정한다(2026-10-10 기준 `gpt-6.1-sol`). 호출 기록에는 바꾼 이름이 남는다. 찾지 못하면 다른 모델로 넘어가지 않고 멈춘다. 추론 수준은 역할별로 지정하며 기본은 모두 `medium`이다(2026-09-27 전에는 통합·재검토 `high`). Codex에는 `-c model_reasoning_effort=…`, Claude에는 `--effort`로 전달한다. `project eval` 보고서의 `by_requested_model.calls_by_effort`와 `project ops --details`의 `reasoning_effort`로 확인한다.

변경안 재검토는 기본으로 실행한다. 신호는 확인 대상이 없는 관측 결과(`unlinked_observed_outcome`), 수정 대상이 없는 수정(`unlinked_revision`), 기존 사건 갱신, 기존 관계 무효화다. 새 관측 결과마다 신호를 내지 않는다. 인용은 코드가 이미 확인하고, 코드가 알 수 없는 것은 빠진 연결이기 때문이다. `--no-review`는 이번 실행에서만 끈다.

## 개발용 LangSmith 추적

`python -m pip install -e '.[langsmith]'`로 SDK를 설치하고, 별도로 `LANGSMITH_API_KEY`를 환경변수에 설정한다. `--langsmith --langsmith-project NAME`은 새 extract/integrate/repair/추가근거 LLM 호출을 그 LangSmith 프로젝트에 보낸다. `LANGSMITH_PROJECT` 환경변수도 기본 프로젝트 이름으로 사용할 수 있다. 키는 Runner 샌드박스에 전달하지 않으며 로컬 호출 ledger에도 저장하지 않는다. trace는 실제 실행 그래프를 그대로 따른다. 루트 `ContextTrail analysis` 아래에 LangGraph 노드(`scan_sources`, `extract_model_and_validate`, `publish_result` 등)가 있고, 모델 노드 아래에 `build_*_request`, 모델 호출 `Project Flow extract/integrate`, `validate_*_contract`, `validate_*_claims`, `read_requested_evidence`, `prepare_repair_request`가 실행 순서대로 붙는다. `--langsmith`만 주면 각 단계에는 개수·상태·ID만 담긴다. 예를 들어 후보 사건 수와 상태별 개수, 인용 정규화 방식별 개수, 모델 호출의 입력·출력 digest·문자 수·토큰 수, `publish_result`의 `graph_summary`(사건·관계 종류별 개수, 검증된 변경·미검증 변경, 확인 대상이 연결되지 않은 실행 결과, 답변된 질문 수)를 볼 수 있다. 검증에 실패한 단계는 원문 값이 지워진 오류 줄과 함께 표시된다.

`--langsmith-content`를 함께 주면 각 호출의 구조화된 **전체 task와 schema, 모델의 구조화 응답**을 포함한다. task에는 선택된 대화·코드·도구 출력 및 추가로 읽은 근거가 포함될 수 있다. 사용자가 전송 범위를 확인해야 한다. 옵션은 실행할 때마다 지정하며 scope 설정에 저장하지 않는다. `project view`와 `project ops`는 LangSmith 호출이 없고, 이미 완료된 호출은 재생하거나 소급 전송하지 않는다. trace 전송이 실패하면 로컬 ledger의 `langsmith_trace=failed`와 오류 종류를 확인할 수 있으며 분석 결과의 유효성은 독립적으로 처리한다. `--max-calls`는 모델 호출 상한이다.

## Studio 개발 그래프

`langgraph.json`의 `contexttrail_analysis`가 `project analyze`·`project eval`과 같은 실행 그래프를 제공한다. 기본 Studio 입력 `{}`은 합성 fixture와 Mock Runner를 사용하며 실제 계정을 호출하지 않는다. 각 단위는 `prepare_run → scan_sources → plan_work_units → select_unit → prepare_extract_input → extract_model_and_validate → validate_candidates → prepare_integrate_input → integrate_model_and_validate → route_semantic_review → [semantic_review_model_validate] → summarize_graph_changes → publish_result → advance_unit`을 반복한다. 구조 계약에 실패한 경우의 bounded extraction recovery는 extraction 노드 내부에서 수행된다. 요청 task·후보와 인용·GraphDelta 및 게시 전 변경을 각 노드 상태에서 확인할 수 있다. 추가 근거 읽기와 수리 시도는 모델 노드 아래 trace에 나타난다. Studio에서 `mode=eval` 또는 `mode=live`를 명시하면 서버에 고정된 입력으로 실제 Codex CLI를 호출한다. CLI는 같은 그래프에서 설정된 worker 수만큼 병렬 추출할 수 있다. `--langsmith --langsmith-content`를 지정한 CLI 실행은 그래프 노드 trace도 남긴다. 상세 동작과 Studio 입력 상한은 [Studio 설계](STUDIO_ARCHITECTURE.md)를 본다.

## 아직 live 검증이 필요한 것

실제 Codex/Claude CLI가 보고하는 model/version/usage의 일관성, 실제 계정에서의 구조화 출력·추가 근거 왕복·repair/cancel, 그리고 같은 snapshot에 대한 prompt 변경 전후 의미 회귀 비교는 live Runner smoke/evaluation 단계에서 확인해야 한다. LangSmith 연결은 2026-09-26 실제 API 키로 합성 데모를 보내 확인했다. `--langsmith`만 준 실행은 26개 실행이 `ContextTrail analysis` 트리로 저장됐고 원문 7개 중 0개가 남았다. `--langsmith-content`는 7개 모두 포함했다.

## a3 추가

단계별 모델/worker, routing 이유, 요청 모델과 확인 가능한 실제 모델의 구분, 공통 호출 상한, 로컬 `project ops`/`project eval`을 추가했다. 상세 계약·명령·아직 실측하지 않은 사항은 [TIERED_ANALYSIS.md](TIERED_ANALYSIS.md)를 기준으로 한다. 예전의 `model` 필드는 요청값이다. 실제 모델명이 제공됐다고 해석하지 않는다.
