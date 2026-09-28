> 아래 내용은 초기 릴리스의 역사적 기록입니다. 모델 계층과 현재 사용 방법은 [TIERED_ANALYSIS.md](../guides/TIERED_ANALYSIS.md)를 참고하세요.

# 구현 상태 · 0.1.0a2

2026-09-23. `구현`은 코드가 존재한다는 뜻, `자동 확인`은 기록한 시험 범위를 통과했다는 뜻이다. 어느 것도 전체 제품 정확성이나 모든 실제 환경 지원을 뜻하지 않는다.

## PRD 요구별 상태

| 요구 | 제공한 구현 | 확인한 범위 / 남은 검증 |
| --- | --- | --- |
| FR-01 범위·worktree | realpath, 공통 Git dir, subfolder scope, worktree provenance, 보존된 과거 root 연결 | 실제 임시 Git/worktree/subfolder 자동 시험. 삭제 worktree·비표준 프로젝트 이동의 완전한 매트릭스 미실시 |
| FR-02 두 입력 | Codex response/compaction/tool-search·Claude message/compact-summary/attachment/sidecar/subagent, tool ID, native ID, lineage, stable fragment | 사용자 제공 실제 archive 156 JSONL에서 compact summary 53행, Codex compaction 59행, verified Claude subagent 15개, sidecar 11개, attachment 147개, tool-search 44행, Codex parent lineage 5개 회귀 확인. 미확인 형식은 여전히 경고/제외 |
| FR-03 Git 근거 | commit/base 고정, staged/unstaged 관측 분리, 안전한 diff, exports 제외 | 실제 local Git 시험. 최근50 commits 기본, untracked 제외, merge 첫 parent만. 임의 revision 비교 UI 없음 |
| FR-04 AI 흐름·근거 | 공통 prompt, extract→integrate, ReadRequest, GraphDelta, 출처/인용 검증 | Mock fixture와 손상/위조 응답 차단. 실제 AI 의미 품질·연구 표본 평가는 남음 |
| FR-05 두 Runner | Codex·Claude headless adapters, capability preflight, bwrap, native auth path mount | argv/output wrapper/cancel/fail-closed 자동 시험. **실제 CLI 지원 완료 아님** |
| FR-06 저장 | SQLite graphs/evidence/sources/work units/runs | 재실행, 발췌 보존, 원자성 시험. 전체 raw log backup 아님 |
| FR-07 재사용 | source/dependency hashes, 단계 ledger, empty extraction cache | no-op 0 calls·runner 변경 no-reanalysis·무관 구간 재사용 시험 |
| FR-08 증분 | 신규 레코드 분할, context_only, 수정 의존성 재검토, saved extraction reuse | append·수정·소실·부분 JSON·통합 실패 복구 시험. 대형 snapshot 튜닝·명시적 범위 재분석 CLI 미제공 |
| FR-09 수동 갱신 | analyze/R/브라우저 POST만 분석, view/GET/export zero AI | explicit 동작과 중복 lock 시험. 상시 감시 없음 |
| FR-10 터미널 | curses, Unicode/ASCII 실제 문자 graph, join/cycle refs, scroll/select/resize | 6 PTY/TERM 조합 시험. **실제 Windows Terminal·IDE·SSH·tmux·screen 시험 아님** |
| FR-11 상세 | loopback HTTP, SVG graph, 사건/관계·원문·diff, version pin, 토큰/CSRF | HTTP/API·SVG XML·JS syntax 시험. 실제 브라우저 visual/interaction 미검증; full Mermaid runtime 미사용 |
| FR-12 export | JSON/MMD/MD, 원문·관계·근거 index, overwrite protection | 실제 CLI 내보내기/재읽기·권한 시험. Mermaid 공식 renderer의 모든 호환성 미확인 |
| FR-13 복구 | flock, cancellation, 단계 재사용, CAS/DB transaction | 이전 graph 보존, DB trigger failure rollback, 실행기 timeout/cancel 시험 |
| FR-14 읽기 전용 | 안전 Git, 제한된 file reads, source injection 격리, CLI sandbox | Git 실행 부작용·manifest 밖 읽기·display 공격 시험. **실제 CLI 권한 부정 시험 필수로 남음** |
| FR-15 내부 관측 분리 | local run/unit별 LLM call ledger(prompt/schema/input/output digest, Runner/model/version, stage/attempt, bounded read/repair, duration, 제공된 usage), no raw prompt/CoT dashboard | 실제 fragmented record offline 처리 118회에서 ledger 연결 확인. external tracing/LangSmith는 미구현·비필수; 실제 CLI usage/model 보고 정확성은 live smoke 필요 |

## SPEC 수용 시험과의 연결

| 시험군 | 자동 회귀에서 다룬 항목 | 아직 부족한 범위 |
| --- | --- | --- |
| AT-01,09,10 | provider 범위·tool/result·부분 기록·real worktree | 실제 버전 로그, parallel provenance의 의미 평가 |
| AT-02,03,04 | 동일 output schema와 합성 개발 흐름 | 두 실제 CLI × 혼합 기록, 실제 개발·연구 판단 품질 |
| AT-05~08,13,14,24 | no-op·append·empty·수정·소실·단계복구·CAS·설정변경 | 장기 고용량·모든 fork/rewind/truncate 경계 조합 |
| AT-11,12 | commit/current 구분, Git-only 변화 hash 감지 | 실제 모델이 기존 주장 상태를 올바르게 수정하는지 |
| AT-15,16,17 | 인용 차단, flock, GET/export 무분석, explicit POST dedupe | 동일 actual Runner에서 모든 경쟁·중단 경계 |
| AT-18,19 | branching/merge/cycle/ASCII·6 PTY·HTTP version/evidence | 실제 단말·SSH forwarding·브라우저 렌더링 가독성 |
| AT-20~23 | 저장 발췌, sidecar denial, export 제외, injection/CSRF, 무외부 trace | 실제 자체 CLI 로그 생성/비저장 보장, actual malicious settings |

**릴리스 판정: 전체 SPEC §13 출시 기준 미충족.** 이는 실제 모델의 성능이 나쁘다는 평가가 아니라 시험을 수행하지 않은 부분을 명시한 것이다. 사용자는 먼저 demo/doctor로 실행 경로를 확인하고 자신의 서버·프로젝트에서 직접 평가할 수 있다.

## 명시적 구현 편차

- Textual 후보 대신 Python 표준 curses 채택.
- full Mermaid JS 대신 자체 safe-subset 문자/SVG renderer. 무제한 Mermaid 편집/입력 기능 없음.
- source raw snapshot은 분석 프로세스 메모리에 고정하고, CLI 작업 디렉터리에는 schema/system 파일만 둔다. 실제 본문은 stdin으로 제공한다. 전체 raw 원문을 임시 파일에 따로 복제하지 않는다.
- 상세 화면은 보존된 원문 발췌·관련 관계를 제공한다. 사용자가 임의 주변 줄을 펼치는 UI는 없고, 추가 과거 원문은 AI Harness의 허용 요청으로만 읽는다.
- production 외부 tracing, 이미지 터미널 프로토콜, 사용자 교정 UI, background watch, 새 Runner·직접 API·MCP는 없다.
- 자동 단위 전체를 통과한 경우에만 그 단위를 게시한다. 독립성을 판단하지 못하는 후속 단위를 실패 구간과 무관하다고 가정하지 않는다.
- 종료코드: complete/noop/no_data 0, failed 1, partial 2, cancelled 130. TUI는 상태를 화면에 표시하고 정상 UI 종료는 0이다.
