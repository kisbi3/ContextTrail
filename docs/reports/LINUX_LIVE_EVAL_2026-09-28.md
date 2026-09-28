# Linux 실측 — 2026-09-28

ContextTrail가 **Linux + bubblewrap 격리 + 실제 Codex CLI**로, 자기 자신이 아닌 외부 프로젝트에서 처음 끝까지 실행된 기록이다. 이 문서 이전까지 README는 "Linux 0회"라고 적고 있었다.

수치 원본: [`artifacts/linux-live-eval-2026-09-28.json`](artifacts/linux-live-eval-2026-09-28.json). 본문에는 전사·경로·식별자를 넣지 않았다.

---

## 1. 결론부터

| | |
| --- | --- |
| Linux 격리 사전 점검 | **통과** — `bubblewrap: true` |
| 실제 모델 호출 | **`model_call_succeeded`** |
| 외부 프로젝트 분석 1단위 | **완료** — 8사건 / 9관계 |
| 프로젝트 트리 무결성 | **통과** — `git status` 변화 없음, HEAD 이동 없음, 생성 파일 0 |
| 상태 저장 위치·권한 | `<git-common-dir>/projectflow/<scope-key>/`, 디렉터리 `0700`, DB `0600` |

`docs/SECURITY.md`가 "argv 계약과 실패 시 차단 로직의 unit test를 실제 보안 검증으로 혼동하면 안 된다"고 경고했지만, 실제 실행은 없었다. 지금은 실행 기록이 있다.

---

## 2. 환경

```
Ubuntu 24.04.4 LTS · Linux 7.0.0-31-generic x86_64 · Python 3.12.3
bubblewrap  /usr/bin/bwrap
codex       codex-cli 0.157.1
claude      설치되어 있지 않음 → Claude Runner는 실행하지 않음
unprivileged_userns_clone = 1 · max_user_namespaces = 146109
```

`codex --version`은 macOS와 동일했다. 격리 경로만 달랐다.

---

## 3. 설치가 막혔다 — `install.sh` 개선 필요

```
./install.sh
→ The virtual environment was not created successfully because ensurepip is not available.
  On Debian/Ubuntu systems, you need to install the python3-venv package:
    apt install python3.12-venv
```

Ubuntu 24.04에는 `python3-venv`가 기본 설치되어 있지 않다. `install.sh`는 이걸 미리 확인하지 않고 raw `ensurepip` 오류를 그대로 보여준다. README는 "Python's curses module and venv support are needed"라고만 적어놨다.

이번 실행에서는 `python3 -m venv --without-pip` + `get-pip.py`로 우회했다. 우회 후 첫 실행에서 site-packages에 손상된 바이트코드가 남아 `ValueError: bad marshal data`가 났고, `__pycache__` 삭제로 해결했다. 두 건 모두 ContextTrail 자체 문제는 아니지만, **설치가 막히면 사용자는 우회 방법을 문서에서 찾을 수 없다.**

**할 일:** `install.sh`가 `venv` 지원과 `ensurepip` 가용성을 먼저 검사하고, 없을 때 정확한 명령(`sudo apt install python3.12-venv`)을 한 줄로 안내한다.

---

## 4. 사전 점검과 실제 호출

기본 `doctor`는 모델 호출을 하지 않았다.

```json
{ "bubblewrap": true, "installed": true,
  "auth": "credential_file_present_not_authenticated_tested",
  "live_model_test": false }
```

`--smoke`는 작은 합성 JSON 왕복만 보냈다(프로젝트 데이터 아님).

```json
{ "auth": "model_call_succeeded", "live_model_test": "passed_for_this_invocation" }
```

README §2가 말하는 그대로다 — "자격증명 파일이 있다는 것이 인증 성공을 보장하지 않는다." macOS에서는 이 `auth` 값이 `not_authenticated_tested`에서 넘어가지 못한 채였다.

---

## 5. 입력 규모와 스코프 필터

호스트에 쌓인 로그가 컸다.

```
codex 로그 1,231 파일 / 1,225,814 레코드
claude 로그    47 파일 /    51,449 레코드
```

하지만 이 프로젝트 범위에 들어온 것은 다음과 같다.

```
codex 4,755 · claude 9 · git 518  =  5,282 레코드
```

**128만 건이 5,282건으로 줄었다.** 경로 스코프 필터가 Shared 로그 디렉터리 때문에 실행이 폭발하는 것을 막는다. 대기 단위는 171개.

---

## 6. 실행

```
project analyze . --runner codex --units 1 --yes --no-tui
```

| 단계 | 상태 | 입력 토큰 | 출력 토큰 | 시간 |
| --- | --- | --- | --- | --- |
| extract | **validation_error** | 48,408 | 4,187 | 80초 |
| extract | complete | 51,669 | 3,297 | 63초 |
| integrate | complete | 23,059 | 6,165 | 116초 |
| integrate | complete | 30,807 | 7,369 | 143초 |
| **합계** | | **153,943** | **21,018** | **402초** |

첫 extract가 검증 오류로 실패했고 단위당 복구 예산 안에서 성공했다. **복구 경로는 macOS 자기분석에서 실행된 적이 없었다.** 이건 macOS에서 못 본 경로를 실제로 통과한 값어치다.

전체 15단위 projections는 약 229만 토큰 / 115분이었고, 1단위는 8분 추정이었다. 실제는 402초(6.7분)로 추정보다 빨랐다.

---

## 7. 만들어진 그래프

```
v1 · partial · 사건 8 · 관계 9

[01] 요청  CUDA 커널 이미지 오류 문의              ✓ 요청 · 답변됨
  └ 동기 → [02] 변경  오류 위치와 CUDA 환경 조사   · 진행 중
             └ 결과 → [03] 결과  초기 진단 명령의 샌드박스 실패   ✗ 관측 실패
                        └ 동기 → [04] 수정  진단 명령 재시도      ! 변경 적용 · 미검증
                                   └ 결과 → [05] 결과  패키지 경로와 Python 환경 가정  ✗ 관측 실패
                                              └ 동기 → [06] 결과  저장소 구조와 가상환경 확인 ✓ 관측 성공
                                                          └ 후속 → [07] 결과  PyTorch 빌드와 GTX 1070의 아키텍처 불일치 확인 ✓ 관측 성공
  └ 답변 → [08] 결과  CUDA 오류 원인과 대응책 설명   ! 완료 보고·미검증
```

**관계 근거 분포: `explicit` 5, `structural` 4, `inferred` 0.**

`inferred`가 0건이라는 점이 중요하다. 추정이 아니라 근거로만 연결됐다는 뜻이고, 이건 스키마가 강제한 결과다(`schema.py`의 `basis` 규칙). 사소해 보이지만 "왜 이걸 연결했나"를 물었을 때 답이 없는 그래프와는 다르다.

### 사람이 읽어본 결과

사람이 8개 사건을 인용된 원문 줄과 대조했다. 순서는 실제로 일어난 디버깅 세션과 맞았고, 인용은 원문 그대로였다. 예를 들어 [01]의 인용:

> 다음의 문제를 알려줘:
> 2026-04-14 12:04:34.475 | ERROR | deep_lyapunov.trajectory.generator:226 >> Failed to process prompt 0: CUDA error: no kernel image is available for execution on the device

사람이 알던 프로젝트, 사람이 알던 오류, 사람이 알던 세션이었다.

**단, 이것은 171개 중 1개다.** 파이프라인이 낯선 프로젝트에서도 그럴듯한 인용된 결과를 낸다는 뜻이지, 일반적으로 신뢰할 수 있다는 뜻은 아니다. README의 재현성 공백은 이 실행으로 그대로 남는다.

---

## 8. 읽기 전용 검증 (FR-14)

```
git status  사전 13줄  →  사후 13줄, 완전 동일
git HEAD    a8b59ab... →  동일
작업 트리 내 생성 파일  0
```

`.git/projectflow/` 아래에만 쓰였다. 프로젝트 안의 파일은 하나도 건드리지 않았다.

---

## 9. 관측된 한계

전부 조용히 버려지지 않고 보고됐다. 파서 3분류가 실제로 작동한 증거이기도 하다.

| 종류 | 개수 | 비고 |
| --- | --- | --- |
| 로그 읽기 보류 (FlowError) | 12 | 모두 native session ID 부재로 파일명 식별 |
| `world_state` 분석 미전달 | 4 | AGENTS.md 전문 — 아직 파싱 안 함 |
| `queue-operation` 미전달 | 2 | 대기시킨 요청문 |
| `stop_hook_summary` 미전달 | 1 | 훅 실행 결과 |
| Claude attachment 미지원 | **14종** | 아래가 중요 |
| Git diff 입력 한도 초과 | 5 | |
| Git diff 읽기 실패 | 2 | |
| subagent 범위 제외 | 1 | 부모 연결 미검증 |

**`attachment:environment`와 `attachment:instructions`** 가 다음 작업 후보다. 프로젝트 환경과 지시문이라 "왜 그렇게 했나"의 답일 수 있는데 지금은 안 읽는다. 오늘 넣은 분류가 여기서 실제 판단을 요구하고 있다.

---

## 10. 남는 공백

이 문서가 닫은 것:

- ✅ Linux 실환경 실행
- ✅ bubblewrap 격리 실동작
- ✅ Linux 실인증
- ✅ 외부 프로젝트 의미 복원
- ✅ 읽기 전용 검증
- ✅ 검증 실패 복구 경로

이 문서가 **닫지 않은** 것:

- ❌ **Claude Runner — 여전히 0회.** 이 호스트에 Claude CLI가 없다
- ❌ tool-denial 시험 — canary 탈출 검사만 있고 `~`, `.ssh`, 실제 자격증명 쓰기 차단은 미검증
- ❌ 재현성 — 동일 설정 반복 실행 여전히 0회
- ❌ 전체 규모 — 171개 중 1개
- ❌ 설치 UX — `python3-venv` 미설치 시 막힘
