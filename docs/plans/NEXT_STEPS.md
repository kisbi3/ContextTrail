# 다음 작업 (2026-09-27 기준)

이 문서는 대화 압축 뒤 이어서 할 일을 모은다. 결정된 내용과 배경은 `docs/DECISIONS.md`의 2026-09-27 항목들에 있다: "에이전트가 CLI로 쓰는 ContextTrail", "분석 속도와 요약", "실패 호출 줄이기", "라이선스 파일". 끝난 항목은 이 문서에서 지우고 DECISIONS에 기록한다.

## 현재 상태

- 자동 테스트 350개 통과(2026-09-28 갱신): `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 <venv>/bin/python -m pytest -q`.
  `scripts/test.sh`는 2026-09-28에 `import pytest, projectflow`가 되는 인터프리터를 고르도록 고쳤다 — langgraph가 없는 python을 고르는 문제가 있었다.
  CI: `.github/workflows/test.yml`이 ubuntu·macOS × Python 3.11–3.13에서 같은 명령을 돌린다.
- 2026-09-27에 넣은 것은 실제 평가를 한 번 거쳤고 품질이 떨어졌다(1절): 영어 프롬프트, 출력 언어, 사건 선별과 제목·요약 규칙, 추론 수준 medium, 읽기 결과 짧게, 다음 단위 미리 추출, 실패 호출 줄이기(답변받은 사용자 목표, 짧은 인용, 통과한 후보 살리기).
- 설치된 명령에 반영하려면 소유자가 `./install.sh`를 다시 실행해야 한다.

## 1. 실제 평가 — 2026-09-27 결과와 다음 단계

2026-09-27 변경을 모두 넣고 sol로 한 번씩 돌렸다(출력은 이 세션 임시 폴더 `installer-0927`, `repairfix-0927`).

| | 기대 결과 | 호출 | 검증 실패 | 토큰 | 시간 | 사건 | 제목 평균 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| installer 09-26 | 15/18 | 4 | 1 | 18.1만 | 8.3분 | | |
| installer 09-27 | **8/18** | 3 | 0 | 16.2만 | 6.3분 | 11 | 16자 |
| repairfix 09-26 | 15/19 | 4 | 1 | 21.7만 | 12.4분 | | |
| repairfix 09-27 | **13/19** | 3 | 0 | 19.8만 | 10.0분 | 18 | 17.5자 |

- 좋아진 것: 검증 실패와 복구 호출이 0, 시간 약 20% 감소, 제목이 한국어이고 짧다.
- 나빠진 것:
  - installer: 추출이 서로 다른 실행으로 확인된 두 변경(managed_commands, install_script)을 한 사건으로 합쳤고, 계획 사건을 둘로 나눴다. 기대 관계 대부분이 이 때문에 연쇄로 떨어졌다. precheck 수정 → 전체 테스트 `verifies`(금지 관계)도 생겼다.
  - repairfix: 추출 후보에는 관측 성공 결과가 9개였는데 통합 뒤 6개가 남아 live6 완료 결과가 빠졌다.
- 이전부터 떨어지던 것: agent_tests 결과, 사용자 요청을 `goal`/adopted로 만든 사건(repairfix 18번, 09-26에도 실패).
- **추론 수준을 분리한 재평가(소유자 동의):** 통합·재검토만 high로 되돌려 다시 돌렸다(`installer-0927-high`, `repairfix-0927-high`).

  | | 기대 결과 | 호출 | 검증 실패 | 토큰 | 시간 |
  | --- | --- | --- | --- | --- | --- |
  | installer high | 16/18 | 3 | 0 | 18.8만 | 7.6분 |
  | repairfix high | 12/19 | 3 | 0 | 19.9만 | 11.2분 |

  - installer의 8→16 차이는 추출 결과가 달라서 생겼다. 추출은 두 번 모두 medium이었다. 이번에는 두 변경을 나눠 냈다. 추출이 실행마다 크게 흔들린다는 뜻이고, 통합 추론 수준 때문이 아니다.
  - repairfix는 medium 13, high 12로 차이가 없다. 두 실행 모두 통합이 관측 결과 후보 2개를 `excluded`로 뺐다. 원인은 09-27에 `integrate.md` 8번에 넣은 "둘러보기만 한 후보는 제외" 문장이다. 이 문장을 지우고, 사건 선별은 추출에서 끝났으니 사소하다는 이유로 제외하지 말고 실행 결과는 각자 사건으로 둔다고 바꿨다. 이 수정은 아직 실제 평가 전이다.
  - 추론 수준은 medium을 유지한다. high는 시간과 토큰만 더 들었다.
- **다음 평가(소유자 동의 필요):** 통합 프롬프트 수정 확인. 기본 설정으로 두 평가를 다시 돌린다(약 35만 토큰, 16분).
- **제안(소유자 결정 필요):** 사용자 메시지만 인용한 `actor=user` `goal`/`proposal`은 답변 관계가 없어도 `question`/`asked`로 고친다. 프롬프트 규칙상 사용자 메시지는 질문 또는 이전 제안을 받아들인 결정뿐이고, 목표는 받아들임이 아니기 때문이다.
- **명령:** fixture 폴더의 평가 명령은 `.claude/settings.local.json`에서 허용돼 있다. 출력은 세션 임시 폴더에 둔다. 평가 출력은 원문을 담고 있어 민감하다. 결과는 `report.json`의 기대 결과·ops·style과 상태 DB의 개수만 읽는다. `calls.json`을 열려는 시도는 개인정보로 막혔다.
  ```bash
  PYTHONPATH=src <venv>/bin/python -m projectflow eval --fixture <eval-dir>/installer-expected-v2.json --runner codex --max-calls 14 --yes --output <scratchpad>/installer-0927
  PYTHONPATH=src <venv>/bin/python -m projectflow eval --fixture <eval-dir>/repairfix-expected.json --runner codex --max-calls 14 --yes --output <scratchpad>/repairfix-0927
  ```

## 2. 오픈소스 공개 준비

- **라이선스:** 끝났다. `LICENSE`(MIT, "Jaesung Kim"), `pyproject.toml`의 `license`·`license-files`·`authors`, `THIRD_PARTY_NOTICES.md`. 한글 이름 표기나 철자가 다르면 `LICENSE`와 `pyproject.toml` 두 곳을 고친다.
- **이름 통일:** 패키지 `projectflow-contexttrail`, import `projectflow`, 제품명 "Project Flow"가 섞여 있다. 공개 전에 `contexttrail`로 통일할지 소유자가 정해야 한다. 공개 후에 바꾸면 설치가 깨진다.
- **git 저장소와 CI:** 현재 폴더는 git 저장소가 아니다. 저장소 생성, Linux·macOS 테스트 CI, GitHub 공개는 소유자가 요청할 때만 한다.
- **영어 README와 화면 문구:** 화면·CLI 문구는 전부 한국어 하드코딩이다(`ui.py`, `render.py`, `cli.py`, `agent_view.py` 등). 시스템 로캘을 따라 한국어/영어를 나누는 작업을 아직 시작하지 않았다. 모델에게 가는 검증 오류 문구도 이때 함께 정리한다.
- **개인 흔적:** `측정 프로젝트`, 테스트의 `jaesung`, 내부 평가 보고서(`docs/reports/`), DECISIONS의 실사용 기록, 이전 전달물의 `SOURCE_MANIFEST.json`(오래된 파일 해시 목록, 어디서도 읽지 않음)을 공개 저장소에 남길지 정한다.
- **README 한계 명시:** 속도(단위당 수 분), 평가 기대 결과 약 15/19, Linux bubblewrap 경로 재검증 필요.

## 3. 이전부터 남은 항목

- **요청 항목 상자:** 한 메시지에 요청이 여러 개면 항목 상자로 나누고 각자 해당 부분을 인용한다(2026-09-26 승인). 프롬프트 변경과 실제 평가가 필요하다.
- **Codex 안에서 `/contexttrail-update`:** Codex 격리 안에서 `sandbox-exec`와 네트워크를 쓸 수 있는지 확인하지 않았다.
- **한글 입력 상태 키:** 실제 터미널에서 확인하지 않았다.
- **전체 이력 분석:** 빨라져도 `측정 프로젝트` 전체(1,480단위)는 수십 시간이다. `--session current`를 주로 쓰는 쪽을 권했다. 기본 순서(오래된 것부터)를 바꿀지는 소유자가 정한다.
