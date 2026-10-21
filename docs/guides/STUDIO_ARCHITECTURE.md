# ContextTrail Studio 실행 그래프

## 실행 모드

`contexttrail_analysis`는 하나의 LangGraph 실행 그래프다(`langgraph.json` → `studio_graph.py:graph`). 터미널의 `contexttrail analyze`, `contexttrail eval`도 이 그래프를 직접 실행한다(모드 `cli`). Studio에서 쓸 수 있는 모드는 다음과 같다.

- **기본 입력 `{}`:** 합성 기록과 Mock Runner를 쓴다.
- **`{"mode":"eval","confirm_live":true}`:** `CONTEXTTRAIL_STUDIO_EVAL_FIXTURE`에 고정된 작은 평가 JSON을 실제 Codex로 처리하고, 결과를 임시 상태에 게시한다.
- **`{"mode":"live","confirm_live":true}`:** `CONTEXTTRAIL_STUDIO_SCOPE`에 고정된 실제 프로젝트를 읽고 Codex CLI를 호출한 뒤, 그 프로젝트의 ContextTrail SQLite 상태에 게시한다.

Studio의 실제 모드는 Codex Runner만 쓴다. Claude Code·opencode 기록은 입력으로 수집하지만 Claude 모델은 호출하지 않는다. 터미널의 `analyze`는 `--runner claude`도 쓸 수 있다. 이 문서는 현재 실행 코드를 설명한다(2026-10-20부터 단위마다 모델 호출이 하나인 파이프라인, `docs/plans/PIPELINE_V2.md`).

Studio의 상자와 화살표는 이 LangGraph의 실제 실행 단계다. LangSmith trace에는 그 실행의 노드와 모델 요청·응답이 남는다. 터미널에서 따로 실행한 `contexttrail analyze`를 Studio 그래프가 자동으로 가져오거나 재생하지는 않는다.

| 단계 | 실행 주체 | 실제 동작 |
| --- | --- | --- |
| `prepare_run` | 호스트 | 합성 모드에서는 fixture를 만들고, 실제 모드에서는 서버에 고정된 프로젝트·평가 fixture와 실행 상한(`max_units` 1~10, `max_calls` 1~50)을 확인한다. CLI 모드에서는 중단된 run을 정리하고 run ledger를 시작한다 |
| `scan_sources` | 호스트 | 로그·Git을 수집해 source를 ingest한다. Studio의 실제 모드에서는 여기서 scope lock을 잡고 run ledger를 시작한다(CLI에서는 lock을 `run_engine`이 그래프 실행 전에 잡는다) |
| `classify_steps` | 호스트 | 도구 호출을 코드로 edit/read/commit/test/vcs/run 등으로 분류하고, 그 개수만 상태에 남긴다(내용 없음, 모델 호출 없음) |
| `plan_work_units` | 호스트 | 실제 `Engine._plan_units`를 실행한다. 저장된 단위의 재사용·무효화, 실패한 단위의 보류(`_hold_back_failed`)도 포함한다 |
| `select_unit` | 호스트 | 처리할 단위의 source ID를 고른다 |
| `prepare_extract_input` | 호스트 | `Engine._prepare_extraction`과 `Engine.story_input`으로 이야기 요청을 만든다. 상태에는 Runner가 첫 시도에 받는 그대로의 `request`(짧은 ID 포함)와 `response_schema`(`light.STORY_SCHEMA`), 새 기록, 기존 사건, 문맥 선택 이유를 남긴다. 저장된 추출이 같은 라우팅 서명을 가지면 요청을 만들지 않는다(`request`가 null). 다만 모델 노드는 문맥 digest까지 같을 때만 그 추출을 재사용하므로, 문맥이 바뀌었으면 여기 표시되지 않은 새 이야기 호출이 나간다 |
| `extract_model_and_validate` | Codex 또는 Mock + 호스트 | 실제 `Engine._extract_unit`. 이야기 호출 한 번(답이 검사에 떨어지면 수리 한 번, 읽을 수 없는 답은 재요청 한 번)을 보내고, `light.story_extraction`이 답을 후보 사건·관계로 바꿔 근거를 검증한다. CLI의 worker가 2개 이상이면 첫 방문에서 같은 그래프를 기준으로 배치를 나란히 추출하고, 뒤 방문은 준비된 결과를 쓴다 |
| `validate_candidates` | 호스트 | 최종 후보를 원문·줄 범위·출처와 다시 대조한다. 사건마다 제목·종류·상태·근거 인용을 `candidate_audit`에 남긴다 |
| `publish_draft` | 호스트 | 모델 호출 없이 코드가 후보를 최신 그래프에 대한 변경으로 만들어 검사와 함께 적용한다(`Engine._integrate_unit`: `schema.draft_delta` → `apply_delta`) |
| `link_request_turns` | 호스트 | 모델 호출 없음. 사람의 메시지마다 요청 사건을 두고, 새 사건을 그 대화 차례에 잇는다(`follows`, `dialog_turn`) |
| `summarize_graph_changes` | 호스트 | 게시 전 추가·수정된 사건과 관계, 무효가 된 관계를 `graph_change_audit`에 남긴다 |
| `publish_result` | 호스트 | 기준 graph version이 그대로인지 확인한 뒤, 코드가 정하는 필드(`store.derive`)를 그래프 전체에서 다시 읽고 원자적으로 게시한다. 내용 해시가 그대로인 기록만 처리됨으로 표시한다. 새 사건에 분석 모델을 적고, 단위 요약·인용되지 않은 보고 블록·순서 밖 분석 표시를 남긴다 |
| `advance_unit`, `finish_run` | 호스트 | 다음 단위로 넘어가거나 run의 완료·부분 완료를 기록한다 |

`prepare_extract_input`부터 `publish_result`까지는 단위 수준 노드다. 이야기 답이 수리 뒤에도 검사에 떨어지면 단위는 실패하지 않고, 코드가 찾은 작업만 남는다. 단위가 실패하는 경우는 셋이다. 그 작업까지 검사를 통과하지 못한 경우, 초안이 검사에 떨어진 경우, 요청이 입력 예산을 넘은 경우다. CLI 실행(터미널의 `analyze`, `eval`)에서는 그런 단위를 `unit_failures`에 기록하고 건너뛰며, 남은 노드는 지나가고 다음 단위로 간다(`_skippable`). Studio의 합성·eval·live 모드에서는 이런 실패도 run을 멈춘다. Runner 오류, 호출 한도, 취소, 알 수 없는 예외는 어느 모드에서나 run을 멈춘다.

Studio의 실제 Codex 실행에서는 `Codex structured response` 하위 trace에 각 호출의 task, schema, 구조화 응답이 남는다. `prepare_extract_input.request`는 그 단위의 첫 이야기 호출이 받는 정확한 요청이다. 수리 호출은 같은 Runner 대화를 이어서 보낸다. `candidate_audit`에서는 후보의 원문 인용을, `graph_change_audit`에서는 게시 직전의 그래프 변경을 본다. 합성 실행의 `Synthetic model response (Mock, no LLM)`은 실제 모델 호출이 아니다. 모델의 비공개 내부 사고 과정은 어느 trace에도 없다.

## 실제 프로젝트 실행

서버는 로컬 주소에 바인딩한다. 프로젝트나 평가 fixture 경로는 **서버 환경변수**에 고정하고, Studio 입력에는 파일 경로를 넣지 않는다. 기록 위치가 기본값과 다르면 `CONTEXTTRAIL_STUDIO_CODEX_HOME`, `CONTEXTTRAIL_STUDIO_CLAUDE_HOME`, `CONTEXTTRAIL_STUDIO_OPENCODE_HOME`을 쓴다.

실제 Codex 분석에는 `mode=eval` 또는 `mode=live`와 `confirm_live=true`가 필요하다. 기본값은 `max_units=1`, `max_calls=10`이고 허용 상한은 각각 10과 50이며, 이 한도는 그 Studio 실행 전체에 걸린다. 프로젝트 모드에서 남은 단위가 있으면 저장된 그래프는 `partial`로 남고, 다시 실행하면 다음 미처리 단위를 계획한다. 평가 모드는 매번 새 임시 상태에서 시작한다.

```bash
python -m pip install -e '.[dev,studio]'
dotenv -f /path/to/private/.env run -- env -u OPENAI_API_KEY \
  CONTEXTTRAIL_STUDIO_SCOPE=/path/to/project \
  LANGSMITH_TRACING=true LANGSMITH_PROJECT=contexttrail-dev \
  langgraph dev --no-browser --host 127.0.0.1 --port 2025
```

실제 모드는 scope lock을 잡아 터미널의 같은 scope 분석과 겹치지 않는다. 단계 사이에 서버 프로세스가 재시작되면 메모리에 둔 실행 세션이 사라지므로, 이전 실행을 재개하지 말고 새 Studio 실행을 시작한다. SQLite에 게시된 완료 단위는 증분 계획이 재사용한다.

Studio 서버에는 사용자 인증이 없고, 실제 분석 입력과 근거가 LangSmith로 전달될 수 있다. 개발용 로컬 서버에서만 실행한다. 원문 중 알려진 형식의 비밀 값만 읽을 때 가려지고(`[secret hidden]`), 그 밖의 민감한 원문은 가려지지 않는다.

## 터미널과 Studio에서 보이는 범위

두 경로는 같은 실행 그래프를 쓴다. `Engine`의 수집·분할·이야기 호출·검증과 `Store`의 게시 함수도 같다. 터미널 경로는 설정된 worker 수(1~8)로 배치 추출하고, Studio의 실제 모드는 한 번에 한 단위씩 처리한다. 수리 호출은 모델 노드 안에서 실행되므로 별도 Studio 상자로는 나타나지 않는다. worker가 2개 이상인 CLI 실행에서는 각 worker가 배치 요청을 만들기 때문에 `prepare_extract_input`에 요청 하나를 따로 보여 주지 않는다. 단위마다의 정확한 요청은 `--langsmith-content`로 보낸 `ContextTrail story` 호출 trace에서 본다.

터미널 실행을 나중에 Studio UI의 과거 실행으로 가져오지는 않는다. 터미널에서 `--langsmith`로 실행하면 trace가 LangSmith로 간다.

- **`--langsmith --langsmith-content`:** 같은 그래프의 노드 trace와 호출마다의 `ContextTrail <stage>` 기록을 보낸다.
- **`--langsmith`만:** 같은 노드·단계·모델 호출 트리를 보내되, 모든 상태와 입출력은 개수·상태·ID만 남긴 메타데이터로 줄인다.
- **기본 터미널 실행:** 외부로 trace를 보내지 않는다.

두 옵션은 개발자용이며 명령 도움말에 나오지 않는다(`docs/guides/LLM_OPS.md`).
