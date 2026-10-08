# 다음 작업 (2026-10-08 기준)

이 문서는 대화 압축 뒤 이어서 할 일을 모은다. 결정과 배경은 `docs/DECISIONS.md`에 있다. 끝난 항목은 이 문서에서 지우고 DECISIONS에 기록한다. 2026-10-08에 2026-09-29판을 정리했다: 끝난 항목을 지우고, 낡은 이름(`projectflow`)과 테스트 수를 걷어냈다(2026-09-27 평가 표는 5절에 원문 그대로 남겼다. README와 `docs/README.md`가 이 문서를 증거로 가리킨다).

## 현재 상태

- 자동 테스트 526개 통과, 1개 건너뜀(macOS 전용 `sandbox-exec`). Linux(cloud, Python 3.13)에서 확인했고 CI는 ubuntu·macOS × Python 3.11–3.13에서 `scripts/test.sh -q`를 돈다.
- 개발 알파 `0.1.0a6`. 실행 중인 에이전트가 직접 적는 note(`contexttrail note`)와 분석 보강 `analyze --audit`까지 구현됐다(`docs/plans/LIVE_JOURNAL.md`). note는 세 도구에서 실제 확인했고, `--audit`는 Mock만 확인했다.
- 설치된 명령과 스킬에 반영하려면 소유자가 로컬에서 `./install.sh`를 다시 실행해야 한다.

## 1. 로컬에서만 할 수 있는 것 (실제 기록·CLI·계정 필요)

- **`--audit` 실모델 확인:** `contexttrail scan --audit`로 계획을 보고 `analyze --audit --session <id> --units 3`을 한 번 돌린다. 볼 것은 note를 되풀이하는 중복률, 빠뜨린 편집 발견 여부, 단위당 호출·토큰이 추정보다 작은지(`LIVE_JOURNAL.md` §11.6). 감사 요청에 새 프롬프트가 붙으므로 CONTRIBUTING의 평가 기록(`PERFORMANCE_PLAN.md` 또는 DECISIONS)도 이때 남긴다.
- **F단계 평가:** 같은 세션을 분석기로 돌린 결과와 note를 비교한다(`LIVE_JOURNAL.md` §6).
- **Codex 재확인:** 크레딧을 채운 뒤, 마지막 수정(대기열·`--verifies` 거부) 뒤의 실행을 한 번 본다.
- **플러그인과 훅 실제 확인**, `./install.sh` 재설치.
- **`file-history-delta` 모양 확인:** 2026-10-08에 `IGNORED_NO_ANALYSIS_VALUE`에 넣었지만 실제 로그의 내용은 보지 못했다. 서사에 쓸 내용이 있으면 `KNOWN_UNPARSED`로 옮긴다.

## 2. 코드로 할 수 있는 것

- **TUI에서 감사 시작:** 지금은 `analyze --audit`만 있다. `status`/`find`는 감사 안 한 note 세션 수를 알려 준다(2026-10-08).
- **감사 비용 보정:** `calibration`은 `audit_` 단위를 뺀다. 감사 실행이 쌓이면 따로 보정한다.
- **단위 예산의 남은 것** (`docs/plans/UNIT_BUDGET_AND_SPLIT.md` §7.9): 애매한 셸 명령의 모델 분류(실측상 우선순위 낮음), 조각 사이 얇은 도구 단계 요약, 압축 줄 형식. 예산을 넘는 단위의 다시 쪼개기(건너뛰기 대신)는 아직 없다.
- **단위 탓 실패의 건너뛰기는 구현됐다**(2026-10-08, `PERFORMANCE_PLAN.md` §16). 실제 모델로 어떤 종류가 얼마나 건너뛰어지는지는 `contexttrail ops`와 `status`의 `failed_units`로 로컬에서 본다. 통합 실패 단위의 초안 발행은 보류.
- **요청 항목 상자:** 한 메시지에 요청이 여러 개면 항목 상자로 나누고 각자 해당 부분을 인용한다(2026-09-26 승인). 프롬프트 변경과 실제 평가가 필요하다.
- **소유자 결정 대기:** 사용자 메시지만 인용한 `actor=user` `goal`/`proposal`을 답변 관계가 없어도 `question`/`asked`로 고칠지. 지금은 `answers` 관계가 있을 때만 고친다(`retype_answered_user_goals`).
- **전체 이력 분석 순서:** 기본(오래된 것부터)을 바꿀지는 소유자가 정한다. 큰 프로젝트는 수십 시간이 걸려 `--session current`나 note를 주로 쓰는 쪽을 권했다.

## 3. 오픈소스 공개 준비에 남은 것

- **개인 흔적:** 테스트의 `jaesung`, 내부 평가 보고서(`docs/reports/`), DECISIONS의 실사용 기록, 오래된 파일 해시 목록 `SOURCE_MANIFEST.json`을 공개 저장소에 남길지 소유자가 정한다.
- **README 한계 명시:** 속도(단위당 수 분), 평가 기대 결과의 흔들림(같은 설정에서 8/18 대 16/18), Linux에서 Claude Runner를 bubblewrap으로 돌린 적이 없다는 점. README에 이미 있는 항목은 `README.md`의 검증 표(Platform validation)를 보고 빠진 것만 더한다.
- 이력 재작성이나 강제 푸시는 소유자가 요청할 때만 한다.

## 4. 확인하지 않은 것

- **Codex 안에서 `/contexttrail-update`:** Codex 격리 안에서 `sandbox-exec`와 네트워크를 쓸 수 있는지.
- **한글 입력 상태 키(`ui.latin`):** 실제 터미널에서.
- **opencode 서브에이전트로 테스트 보고를 받는 절차** (2026-09-29 기록, 그때 환경 기준): tmux 세션 `opencode`에서 작업 파일(임시 폴더, 규칙·단계·보고 형식)을 읽게 하고 결과는 보고 파일로 받는다. 저장소 파일 수정, 임시 폴더 밖 쓰기, `~/.codex` `~/.claude` `~/.local/share/opencode` `.env` 전사본 읽기, `analyze`·실제 Runner 평가·`doctor --smoke`·`install.sh`·`install-commands` 실행은 금지한다. 보고는 신뢰하지 말고 같은 명령(`scripts/test.sh -q`, 전후 `git status`)으로 다시 확인한다.
- **테스트 환경을 새로 만들 때:** `python3.13 -m venv <dir>` 뒤 `<dir>/bin/pip install -e '.[dev]'`, 실행은 `PYTHON=<dir>/bin/python scripts/test.sh -q`.

## 5. 기록: 2026-09-27 실제 평가 (원문 보존)

> 2026-10-08 주: 아래는 당시 기록 그대로다. 명령의 `python -m projectflow`는 지금 `contexttrail eval`이고, "다음 평가"의 통합 프롬프트 수정은 DECISIONS 2026-09-27 "실제 평가 뒤 통합 프롬프트 되돌림"에 반영됐으며 그 뒤의 재평가는 `PERFORMANCE_PLAN.md`에 있다.

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
