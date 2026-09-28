# ContextTrail Studio 실행 그래프

## 실행 모드

`contexttrail_analysis`는 하나의 LangGraph 실행 그래프다. 터미널의 `project analyze`, `project eval`도 이 그래프를 직접 실행한다. 기본 Studio 입력 `{}`은 합성 기록과 Mock Runner를 사용한다. `{"mode":"eval","confirm_live":true}`는 `CONTEXTTRAIL_STUDIO_EVAL_FIXTURE`에 고정된 작은 평가 JSON을 실제 Codex로 처리해 임시 상태에 게시한다. `{"mode":"live","confirm_live":true}`는 `CONTEXTTRAIL_STUDIO_SCOPE`에 고정된 실제 프로젝트를 읽고 Codex CLI를 호출해 기존 프로젝트의 ContextTrail SQLite 상태에 게시한다. Claude 기록은 입력으로 수집하지만 Claude 모델은 호출하지 않는다. 이 문서는 현재 실행 코드를 설명한다.

Studio의 상자와 화살표는 이 LangGraph의 실제 실행 단계다. LangSmith trace는 그 실행에서 발생한 노드와 모델 요청·응답을 기록한다. 터미널에서 별도로 실행한 `project analyze` 호출을 Studio 그래프가 자동으로 가져오거나 재생하지는 않는다.

| 단계 | 실행 주체 | 실제 동작 |
| --- | --- | --- |
| `prepare_run` | 호스트 | 합성 fixture 생성 또는 서버 고정 프로젝트·평가 fixture와 실행 상한 확인 |
| `scan_sources` | 호스트 | scope lock, 실제 로그·Git 수집, source ingest, run ledger 시작 |
| `plan_work_units` | 호스트 | 실제 `Engine._plan_units`; 기존 처리 단위 재사용·무효화 포함 |
| `select_unit` | 호스트 | 처리할 단위의 source ID 선택 |
| `prepare_extract_input` | 호스트 | 실제 `Harness.context`와 `build_task`로 첫 추출 요청을 구성. `request`·`response_schema`·새 원문/주변 문맥 목록과 기존 사건 선택 이유를 상태에 표시. 검증된 추출 재사용 시 요청 없음 |
| `extract_model_and_validate` | Codex 또는 Mock + 호스트 | 실제 `Engine._extract_unit`; 구조화 후보 추출과 근거 검증. CLI의 worker가 2개 이상이면 첫 노드 방문에서 같은 graph snapshot을 기준으로 배치를 병렬 추출하고 뒤 방문에서는 준비된 결과를 소비 |
| `validate_candidates` | 호스트 | 최종 추출 후보를 원문·줄 범위·출처와 다시 대조. 사건마다 제목·종류·상태·근거 인용을 `candidate_audit`에 표시 |
| `prepare_integrate_input` | 호스트 | 추출 후보의 제목·요약·기존 사건 참조를 검색 단서로 사용하고 최신 저장 그래프를 기준으로 통합 요청을 구성. `request`·`response_schema`·기존 사건 선택 이유를 표시하고 후보가 없으면 모델 호출 생략을 표시 |
| `integrate_model_and_validate` | Codex 또는 Mock + 호스트 | 실제 `Engine._integrate_unit`; 기존 그래프와 후보 통합·검증 |
| `summarize_graph_changes` | 호스트 | 모델의 `GraphDelta`와 게시 전 추가·수정 사건 및 관계를 `graph_change_audit`에 표시 |
| `publish_result` | 호스트 | source hash·graph version 검증 뒤 원자적 게시 |
| `advance_unit`, `finish_run` | 호스트 | 다음 단위 또는 run 완료·부분 완료 기록 |

실제 Codex 실행에서 `Codex structured response` 하위 trace는 각 호출의 task, schema, 구조화 응답을 담는다. 모델 노드 안에는 `build_extract_request`/`build_integrate_request`, `validate_*_contract`, `validate_*_claims`, 필요할 때 `read_requested_evidence`와 `prepare_repair_request`가 실제 수행 작업의 하위 trace로 기록된다. `prepare_extract_input.request`는 해당 단위 첫 추출 호출의 정확한 요청이며, 후속 근거 읽기·수리 호출은 각 호출의 하위 trace에서 확인한다. `candidate_audit`에서 후보의 원문 인용을 보고, `graph_change_audit`에서 게시 직전의 그래프 변경을 본다. 조건부 통합 후 의미 검토는 실제 `route_semantic_review`와 선택적 `semantic_review_model_validate` 노드로 표시된다. 합성 실행의 `Synthetic model response (Mock, no LLM)`은 실제 모델 호출이 아니다. 모델의 비공개 내부 사고 과정은 어느 trace에도 없다.

## 실제 프로젝트 실행

서버를 로컬 주소에 바인딩하고 프로젝트 또는 평가 fixture 경로를 **서버 환경변수**에 고정한다. Studio 입력에는 파일 경로를 넣지 않는다. 실제 Codex 분석은 `mode=eval` 또는 `mode=live`와 `confirm_live=true`가 필요하다. 기본 `max_units=1`, `max_calls=10`이고 허용 상한은 각각 10과 50이다. 이 한도는 해당 Studio 실행 전체에 적용된다. 프로젝트 모드에서 남은 단위가 있으면 저장된 그래프 상태는 `partial`이고, 다시 실행하면 다음 미처리 단위를 계획한다. 평가 모드는 매번 새 임시 상태로 시작한다.

실제 모드는 scope lock을 잡아 터미널의 같은 scope 분석과 충돌하지 않게 한다. 서버 프로세스가 단계 사이에 재시작되면 메모리에 둔 실행 세션이 사라진다. 이 경우 이전 실행을 재개하지 말고 새 Studio 실행을 시작한다. SQLite에 게시된 완료 단위는 증분 계획이 재사용한다.

Studio 서버에는 사용자 인증이 없고, 실제 분석 입력과 근거가 LangSmith로 전달될 수 있다. 개발용 로컬 서버에서만 실행한다. 민감한 원문을 자동으로 가리지 않는다.

## 터미널과 Studio에서 보이는 범위

두 경로는 같은 실행 그래프와 `Engine`의 수집·분할·추출·통합·검증, `Store`의 게시 함수를 사용한다. 터미널 경로는 설정된 worker 수(1~8)로 배치 추출하고 Studio의 실제 모드는 한 번에 한 단위씩 처리한다. 추가 근거 요청과 응답 수리 반복은 모델 노드 내부에서 실행되어 별도 Studio 상자로 나타나지 않지만, 각각 하위 trace에 표시된다. worker가 2개 이상인 CLI 실행에서는 배치 요청을 각 worker에서 만들므로 `prepare_extract_input`에 단일 요청을 표시하지 않고, 개별 `build_extract_request` trace를 확인해야 한다. 검증 전 실패로 인한 재검토도 추출 노드 내부에서 이루어질 수 있다.

터미널 실행을 나중에 Studio UI의 과거 실행으로 가져오지는 않는다. 터미널에서 `--langsmith --langsmith-content`를 명시한 새 실행은 같은 그래프의 노드 trace를 LangSmith에 보낸다. `--langsmith`만 주면 같은 노드·단계·모델 호출 트리를 보내되, 모든 상태와 입출력은 개수·상태·ID만 남긴 메타데이터로 줄여 보낸다. 기본 터미널 실행에서는 외부 trace를 보내지 않는다. 추출 계약 검증 복구는 현재 실제 별도 LangGraph 노드가 아니라 추출 모델 노드 내부 동작이다.
