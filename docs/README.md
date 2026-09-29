# ContextTrail 문서

GitHub에 공개하는 사용자 문서와 검증 기록을 정리했다. 실제 프로젝트 이름과 절대 경로는 문서에서 가렸고, 집계 수치는 그대로 남겼다. 아직 공개하지 않은 것은 진행 중인 작업 계획뿐이다.

| 찾는 내용 | 문서 |
| --- | --- |
| 제품 목표와 범위 | [PRD](PRD.md) |
| 분석·저장 계약 | [SPEC](SPEC.md) |
| 실행·자료 보안 | [SECURITY](SECURITY.md) |
| 기술 참고 | [REFERENCES](REFERENCES.md) |

## 사용과 개발

- [Studio 실행 그래프](guides/STUDIO_ARCHITECTURE.md): CLI와 Studio가 공유하는 실제 분석 흐름, 단계별 출력, 실행 모드.
- [LLM Ops](guides/LLM_OPS.md): 호출 기록, 사용량, LangSmith 추적.
- [모델 계층화와 평가](guides/TIERED_ANALYSIS.md): 모델 역할과 작은 fixture 평가.

## 검증 기록 — 측정한 값의 출처

최상위 README의 "Measured so far" 표는 아래 문서에서 나온 값만 옮겼다. 확인이 필요하면 원본을 보라.

- [실제 CLI 평가](reports/TWO_CALL_LIVE_EVAL_2026-09-25.md): macOS 실제 LLM 호출 검증. WorkUnit 1개, 14개 기록, 27,230자 수동 대조.
- [Linux 실측](reports/LINUX_LIVE_EVAL_2026-09-28.md): Ubuntu 24.04 + bubblewrap + 실제 Codex CLI로 외부 프로젝트를 처음 끝까지 실행한 기록. 격리 사전 점검, 실인증, 1단위 분석, 읽기 전용 검증 포함.
- [세션 기록 2026-09-28](reports/SESSION_2026-09-28.md): 증거 공개·CI·성능·Linux 실측을 한 세션에서 처리한 기록. 그 과정에서 잘못 판단한 것 세 건도 함께 남겼다.
- [사전 실측 감사](reports/PRELIVE_AUDIT.md): 실제 계정 호출 전 단계별 결과. 실행한 독립 검토와 LLM 호출은 0회.
- [a3 검증](reports/A3_VALIDATION.md): 아카이브 156개 파일, 선택 레코드 31,748개, 파싱 limitation 0.
- [설계·평가 이력](DECISIONS.md):dated 결정 기록. 측정한 것과 하지 않은 것을 함께 적었고, 되돌린 변경도 남겼다.
- [측정값 요약과 남은 일](plans/NEXT_STEPS.md): 평가 기대 결과, 호출 수, 토큰, 소요 시간 표.
- [a2 로그 형식 검증](reports/REAL_LOG_VALIDATION.md), [초기 구현 상태](reports/IMPLEMENTATION_STATUS.md), [초기 시험 보고](reports/TEST_REPORT.md).
- 합성 데모와 자동 시험 결과는 [`reports/artifacts/`](reports/artifacts/)에서 확인할 수 있다.

## 계획

- [단위 입력 예산 산정과 분할](plans/UNIT_BUDGET_AND_SPLIT.md): Linux 실측에서 발견된 결함. 대기 단위 167개 중 76개(46%)가 입력 예산 초과로 처리되지 못한다. 측정 근거는 [`artifacts/unit-budget-measurement-2026-09-29.json`](reports/artifacts/unit-budget-measurement-2026-09-29.json).
- [독립 검토 작업 카드](plans/REVIEW_TASKS.md): 별도 검토를 위한 작업 범위. 실제 검토 실행 기록은 아니다.

프로젝트 설치와 명령 사용은 [최상위 README](../README.md)를 따른다. 보고서의 과거 수치는 현재 전체 프로젝트의 분석 품질을 보증하지 않는다.
