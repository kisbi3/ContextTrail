# ContextTrail 문서

GitHub에 공개하는 사용자 문서와 검증 기록을 정리했다. 비공개 개발 노트와 실제 작업공간에서 만든 원시 감사 자료는 저장소에서 제외한다.

| 찾는 내용 | 문서 |
| --- | --- |
| 제품 목표와 범위 | [PRD](PRD.md) |
| 분석·저장 계약 | [SPEC](SPEC.md) |
| 실행·자료 보안 | [SECURITY](SECURITY.md) |
| 기술 참고 | [REFERENCES](REFERENCES.md) |
| 구현 검토 작업 | [REVIEW_TASKS](plans/REVIEW_TASKS.md) |

## 사용과 개발

- [Studio 실행 그래프](guides/STUDIO_ARCHITECTURE.md): CLI와 Studio가 공유하는 실제 분석 흐름, 단계별 출력, 실행 모드.
- [LLM Ops](guides/LLM_OPS.md): 호출 기록, 사용량, LangSmith 추적.
- [모델 계층화와 평가](guides/TIERED_ANALYSIS.md): 모델 역할과 작은 fixture 평가.

## 계획

- [독립 검토 작업 카드](plans/REVIEW_TASKS.md): 별도 검토를 위한 작업 범위. 실제 검토 실행 기록은 아니다.

## 검증 기록

- [a2 실제 로그 형식 검증](reports/REAL_LOG_VALIDATION.md): 합성 회귀 사례로 보강한 입력 형식 점검.
- [초기 구현 상태](reports/IMPLEMENTATION_STATUS.md), [초기 시험 보고](reports/TEST_REPORT.md).
- 합성 데모와 자동 시험 결과는 [`reports/artifacts/`](reports/artifacts/)에서 확인할 수 있다.

프로젝트 설치와 명령 사용은 [최상위 README](../README.md)를 따른다. 보고서의 과거 수치는 현재 전체 프로젝트의 분석 품질을 보증하지 않는다.
