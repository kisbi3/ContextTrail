# 목표 파이프라인 (LangGraph) — 현재와 바뀔 곳

**상태: 설계. 표의 "현재" 열은 `studio_graph.py`의 실제 노드이고, "새로/변경" 표시는 아직 구현하지 않았다(구현 여부는 마지막 열).** 그림: `docs/reports/artifacts/target-pipeline.png`. LangGraph/LangSmith는 소유자가 파이프라인을 설계하며 보는 용도이고 배포 기능이 아니다.

원칙(2026-09-29 소유자 결정, `UNIT_BUDGET_AND_SPLIT.md` §7.9): 코드가 결정적인 뼈대(경계, 분류, 요청·커밋 같은 노드)를 만들고, 모델은 묶기·이름 붙이기·관계 맺기를 한다. 조각(단위)은 모델 입력 묶음이지 노드나 엣지가 아니다.

## 1. 실행 순서

| # | 노드 | 주체 | 하는 일 | 상태 |
| --- | --- | --- | --- | --- |
| 1 | `prepare_run` | 코드 | 실행 준비, 한도 확인 | 현재 |
| 2 | `scan_sources` | 코드 | 기록·Git 수집, ingest | 현재 |
| 3 | `classify_steps` | 코드 (+ 애매한 것만 모델) | 도구 호출을 edit/read/commit/test/vcs/run으로 분류. 모델은 `run`을 올리기만 하고, 결과는 명령 해시별로 저장 | 코드 부분 구현(`analysis.classify_steps` 개수 집계 + 이 노드, `project scan`의 `steps`). 모델 부분은 만들지 않음: 애매한 `run` 비율(`steps.ambiguous_run_share`)을 `scan`으로 먼저 재야 함 |
| 4 | `plan_work_units` | 코드 | 안전한 경계에서 자르기. 우선순위: 압축·공백 > commit 뒤 > 사용자 메시지 앞 > 실행 결과 뒤 > 그 밖. 조립된 페이로드 기준 `task_chars`의 약 60%까지 병합 | 구현(규칙 B, 창은 페이로드 추정치 기준). 추정 상수는 실측 한 건으로 맞춘 값이라 검증 필요. 저장된 `parsed` 단위는 다시 묶음 |
| 5 | `select_unit` | 코드 | 처리할 단위 선택 | 현재 |
| 6 | `prepare_extract_input` | 코드 | 새 기록 + 줄인 문맥 + `tool_steps` 조립. 감사 기록(`context_selection`)은 모델에 보내지 않는다 | 일부 구현(`model_context`). 문맥 축소 `--context-mode lean`(인용 ±5줄, 직전 기록 2개, 사건 12개, 작은 manifest)이 2026-09-29부터 기본값. 평가(Sol, 1회씩): 입력 토큰 −28~−52%, 시간 −30~−35%, 기대 결과 차이는 잡음 범위. `--context-mode full`로 되돌림 |
| 7 | `extract_model_and_validate` | 모델 + 코드 | 후보와 인용 추출, 검증, 수리 1회 | 현재 |
| 8 | `validate_candidates` | 코드 | 인용을 원문과 대조 | 현재 |
| 9 | `seed_code_nodes` | 코드 | 코드가 확실히 아는 노드(사용자 요청, commit, 관측된 실행 결과)를 먼저 만들고 모델이 제목·요약·관계를 채우게 함 | 새로. 설계 전. 사용자 요청은 지금 `add_user_requests`가 추출 안에서 함 |
| 10 | `prepare_integrate_input` | 코드 | 후보 + 기존 그래프로 통합 입력 조립 | 현재 |
| 11 | `integrate_model_and_validate` | 모델 + 코드 | 기존 그래프와 통합, `verifies` 등 관계 | 현재 |
| 12 | `route_semantic_review` → `semantic_review_model_validate` | 코드 → 모델 | 의미 신호가 있을 때만 재검토 | 현재 |
| 13 | `link_request_turns` | 코드 | 요청 턴 연결(`follows`, 구조적) | 현재 |
| 14 | `summarize_graph_changes` → `publish_result` | 코드 | 변경 요약, 원자적 게시 | 현재 |
| 15 | `advance_unit` → (`select_unit` \| `finish_run`) | 코드 | 다음 단위 또는 종료 | 현재 |

## 2. 새 단계가 지켜야 할 것

- `classify_steps` 결과와 컷 결과는 세션·기록 해시로만 정해져야 한다. 같은 입력이면 같은 단위 ID여야 "이미 끝난 단위는 다시 보내지 않는다"가 유지된다.
- 이미 저장된 `parsed`/`extracted`/통합된 단위는 다시 자르지 않는다. 새 규칙은 아직 처리하지 않은 뒤쪽에만 적용한다(`UNIT_BUDGET_AND_SPLIT.md` §7.1).
- 모델이 분류에 관여하더라도 후보 밖의 경계와 코드가 확실한 edit을 바꾸지 못한다. 호출이 실패하면 코드 결과를 쓴다.
- 조각 사이 `verifies`는 통합 단계가 기존 그래프를 보고 맺는다. 선행 추출(`_prefetch_next`)이 앞 조각 통합 전에 다음 조각을 추출하므로, 편집→실행→결과는 한 조각에 두고 그 뒤에서 자른다.
- 노드를 추가하면 `studio_graph.py`와 `docs/guides/STUDIO_ARCHITECTURE.md`를 함께 고치고, 이 문서의 "상태" 열을 갱신한다.

## 3. 구현 순서

1. `classify_steps` 노드로 분리 — 완료(개수만 상태에 기록).
2. `plan_work_units`에 컷 규칙 B와 조립 페이로드 기준 예산(§7.6) — 규칙 B는 완료, 예산은 추정치(계획 단계에서 조립 페이로드를 직접 재는 것은 남음).
3. `prepare_extract_input`의 문맥 축소 — 구현, 2026-09-29 기본값을 lean으로 전환.
4. `seed_code_nodes` 설계와 구현.
