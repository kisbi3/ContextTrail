# 다음 작업 (2026-09-29 기준)

이 문서는 대화 압축 뒤 이어서 할 일을 모은다. 결정된 내용과 배경은 `docs/DECISIONS.md`의 2026-09-27 항목들에 있다: "에이전트가 CLI로 쓰는 ContextTrail", "분석 속도와 요약", "실패 호출 줄이기", "라이선스 파일". 끝난 항목은 이 문서에서 지우고 DECISIONS에 기록한다.

## 2026-10-06 추가: 세 도구가 같이 쓰는 프로젝트 기억

opencode 입력, 신선도 표시, opencode 설치, Runner 저장, 포지셔닝, 선택적 자동 갱신 훅의 계획은 `docs/plans/SHARED_CONTEXT_STORE.md`에 있다. 2절의 "이름 통일"·"영어 README와 화면 문구"·라이선스는 끝났다(PyPI `contexttrail` 0.1.0a5, 화면·모델 입력 영어화, `docs/DECISIONS.md` 2026-10-06).

## 현재 상태

- 자동 테스트 363개 통과(2026-09-28 갱신): `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 <venv>/bin/python -m pytest -q`.
  `scripts/test.sh`는 2026-09-28에 `import pytest, projectflow`가 되는 인터프리터를 고르도록 고쳤다 — langgraph가 없는 python을 고르는 문제가 있었다.
  CI: `.github/workflows/test.yml`이 ubuntu·macOS × Python 3.11–3.13에서 같은 명령을 돌린다.
- 2026-09-27에 넣은 것은 실제 평가를 한 번 거쳤고 품질이 떨어졌다(1절): 영어 프롬프트, 출력 언어, 사건 선별과 제목·요약 규칙, 추론 수준 medium, 읽기 결과 짧게, 다음 단위 미리 추출, 실패 호출 줄이기(답변받은 사용자 목표, 짧은 인용, 통과한 후보 살리기).
- 설치된 명령에 반영하려면 소유자가 `./install.sh`를 다시 실행해야 한다.

## 0. 2026-09-29 검수 뒤 새 항목

- **단위 예산 결함(최우선):** 실제로 보내는 입력이 예산보다 커서 실행 전체가 중단된다. 측정과 원인은 `docs/plans/UNIT_BUDGET_AND_SPLIT.md`, 검수 뒤 정정과 결정은 그 문서 7절. 작업 순서: (1) 실제 입력 크기 측정과 계산 고치기 (2) 저장된 `parsed` 단위 다시 묶기 (3) 실행 결과 뒤에서 자르고 얇은 요약 붙이기 (4) 문맥 줄이기(`context_only`, `manifest`) (5) 단위별 격리 (6) 되돌려 깨지는지 확인하는 테스트. 코드는 소유자 승인 전에 시작하지 않는다. 먼저 AI 없이 하는 측정(7.5절)을 할지 정해야 한다.
- **`file-history-delta` 분류 누락:** Claude 로그의 이 형식이 건너뛰기 목록에는 있지만 `IGNORED_NO_ANALYSIS_VALUE`, `KNOWN_UNPARSED` 어디에도 없어 새 "미지원" 경고를 만든다. 작은 코드 수정과 테스트. `queue-operation` 주석의 숫자(473)도 측정값(중복 338, 사람이 입력한 것 25)과 어긋난다.
- **opencode 지원과 모델·effort 기록:** 계획은 `docs/plans/OPENCODE_SOURCE.md`. 미구현.
- **opencode를 하위 에이전트처럼 쓰는 절차(테스트 보고용):** tmux 세션 `opencode`(화면 하단이 `Build · Space Bunny Alpha OpenRouter · max`). 작업 파일(임시 폴더, 규칙과 단계와 보고 형식 포함)을 쓰고, `tmux send-keys -l`로 한 줄 지시를 보내 그 파일을 읽게 한 뒤 Enter. 결과는 보고 파일로 받는다. 규칙: 저장소 파일 수정 금지, 임시 폴더 밖에 쓰기 금지, `~/.codex` `~/.claude` `~/.local/share/opencode` `.env` 전사본 `.projectflow`를 읽지 않기, `analyze`·실제 Runner 평가·`doctor --smoke`·`install.sh`·`install-commands` 실행 금지. 모델·effort 값은 opencode가 화면에 보이는 대로 보고하게 하고 내가 화면과 대조한다. opencode가 외부 폴더 권한을 물으면 그때그때 한 번만 허용한다. 보고는 신뢰하지 말고 내가 같은 명령으로 다시 확인한다(테스트 363개, 실행 전후 `git status` 동일).
- **테스트 환경:** `/private/tmp/contexttrail-prelive-venv`는 macOS의 임시 폴더 정리로 망가졌다. 새로 만들 때는 `python3.13 -m venv <dir>` 뒤 `<dir>/bin/pip install -e '.[dev]'`, 실행은 `PYTHON=<dir>/bin/python scripts/test.sh -q`.

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
- **git 저장소와 CI:** 저장소와 CI(`.github/workflows/test.yml`)는 이미 있다. 이력 재작성이나 강제 푸시는 소유자가 요청할 때만 한다.
- **호스트 이름 노출: 해결(2026-09-29).** 소유자 결정으로 이력을 다시 써서 강제 푸시했다(`main` = `243e6ce`). GitHub에 캐시된 옛 커밋 URL은 GitHub 지원만 지울 수 있다.
- **영어 README와 화면 문구:** 화면·CLI 문구는 전부 한국어 하드코딩이다(`ui.py`, `render.py`, `cli.py`, `agent_view.py` 등). 시스템 로캘을 따라 한국어/영어를 나누는 작업을 아직 시작하지 않았다. 모델에게 가는 검증 오류 문구도 이때 함께 정리한다.
- **개인 흔적:** `측정 프로젝트`, 테스트의 `jaesung`, 내부 평가 보고서(`docs/reports/`), DECISIONS의 실사용 기록, 이전 전달물의 `SOURCE_MANIFEST.json`(오래된 파일 해시 목록, 어디서도 읽지 않음)을 공개 저장소에 남길지 정한다.
- **README 한계 명시:** 속도(단위당 수 분), 평가 기대 결과 약 15/19, Linux bubblewrap 경로 재검증 필요.

## 3. 이전부터 남은 항목

- **요청 항목 상자:** 한 메시지에 요청이 여러 개면 항목 상자로 나누고 각자 해당 부분을 인용한다(2026-09-26 승인). 프롬프트 변경과 실제 평가가 필요하다.
- **Codex 안에서 `/contexttrail-update`:** Codex 격리 안에서 `sandbox-exec`와 네트워크를 쓸 수 있는지 확인하지 않았다.
- **한글 입력 상태 키:** 실제 터미널에서 확인하지 않았다.
- **전체 이력 분석:** 빨라져도 `측정 프로젝트` 전체(1,480단위)는 수십 시간이다. `--session current`를 주로 쓰는 쪽을 권했다. 기본 순서(오래된 것부터)를 바꿀지는 소유자가 정한다.
