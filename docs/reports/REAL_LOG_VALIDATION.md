# 실제 로그 archive 검증 · 0.1.0a2

2026-09-23. 사용자가 제공한 Claude Code project archive와 Codex monthly sessions archive의 **형식·정규화·증분 처리**를 검증했다. 원본 archive는 배포물에 포함하지 않는다. 이 검증은 실제 Claude/Codex LLM의 의미 분석 정확도 시험이 아니다.

## 범위와 결과

- 입력: Claude Code JSONL 40개, Codex JSONL 116개, 합계 156개.
- 정규화 결과: 31,748 records. Claude 16,227 / Codex 15,521.
- 역할: user 1,197 / assistant 4,555 / tool_call 12,522 / tool_result 12,801 / metadata 673.
- derivation: original 31,536 / summary 212.
- 회귀 검사: 16/16 통과. parser limitations 0.
- 긴 자료: 204개 원본이 539 fragment로 분리됐고 fragment 최대 길이는 32,000자.
- binary payload: 37개를 Base64 본문 대신 media type·encoded length·SHA-256 marker로 정규화.

## 이전 실제-log 결함의 재검증

| 항목 | 이전 문제 | 0.1.0a2 결과 |
| --- | --- | --- |
| Claude compact summary | 사용자 발언으로 오분류 | 53/53 원문 행을 `metadata + summary`로 보존 |
| Codex compaction | `replacement_history` 누락 | in-scope 59/59 보존 |
| Claude subagent | 연결된 15개 transcript 누락 | 15/15 부모 call/result 검증 후 lineage와 함께 포함 |
| Claude sidecar | archive 이동 시 절대경로 때문에 누락 | 참조된 11/11을 같은 session의 portable 경로로 재연결 |
| Codex tool search | call/output 누락 | in-scope 44/44 보존 |
| Claude attachment | file/edited_text_file 내용 누락 | 147/147 metadata snapshot으로 보존 |
| Codex subagent parent | `parent_thread_id` 소실 | 5/5 session의 parent lineage 보존 |
| 긴/Base64 기록 | 48k 한도 때문에 보류 | text는 stable fragment, binary는 marker 처리 |

## Engine·Ops 확인

실제 archive에서 이전에 3.7MB 규모였던 한 sidecar의 정규화 fragment 119개를 offline structured Runner로 Engine에 통과시켰다. 최초 실행은 `complete`, pending 0이었고 동일 입력 재실행은 `noop`이었다. 118번의 모델 단계 호출이 local `llm_calls` ledger에 기록되었으며 각 항목에 stage/attempt, runner/model/version, prompt/schema/input digest, 입력 크기, read/repair round, 상태, output digest/크기, duration, Runner가 제공한 usage가 연결된다.

**제한:** 이 시험의 Runner는 의미 사건을 생성하지 않는 offline Runner다. 따라서 parser → planner → store → Harness → LLM Ops instrumentation의 동작을 검증하지만 실제 Codex/Claude 모델이 사건·관계를 올바르게 추출하는지는 검증하지 않는다.
