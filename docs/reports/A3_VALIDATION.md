# Project Flow 0.1.0a3 — 프로젝트 선별·모델 라우팅 검증

날짜: 2026-09-23. 기존 0.1.0a2 소스를 수정하고 아래 검사를 이 환경에서 실행했다. 실제 계정 CLI 의미 분석과 비용 절감은 검증하지 않았다.

## 1. 사용자의 Codex `06.zip` 재검사

분석 대상 경로: `/home/<user>/dev/<project>`. Git 저장소 자체는 업로드되지 않았으므로 archive 검사는 이 경로와 하위 폴더만 허용한 명시적 Scope를 사용했다. 다른 clone/worktree 연결을 추측하지 않았다.

| 측정 | 값 |
|---|---:|
| 검사한 JSONL 파일 | 116 |
| 범위 내 원문이 하나 이상 선택된 파일 | 81 |
| 선택된 원문이 없는 파일 | 35 |
| 선택된 정규화 레코드 | 15,521 |
| 실제 LLM 호출 | 0 |

선택된 레코드의 cwd는 `/home/<user>/dev/<project>` 15,513개와 그 하위 `harness` 8개였다. 정규화 레코드에는 tool call/result, metadata, 요약 및 긴 원문의 fragments가 포함되므로 이 수를 사용자 메시지 수로 해석하면 안 된다.

첫 session metadata만 보면 `/home/<user>/<project>`였던 한 파일에서도 대상 경로의 정규화 레코드 88개를 선택했다. 범위 안에 해당하는 원문 단위는 86개, 밖에 해당하는 단위는 171개였다. 원문 분할 때문에 정규화 레코드 수와 scope 결정 횟수가 다를 수 있다. 해당 파일은 `06/24/rollout-2026-06-24T11-12-18-<session-uuid>.jsonl`다.

날짜 경로는 재귀 파일 탐색의 대상이지 프로젝트 판정 근거가 아니다. session/turn/record/tool 경로를 먼저 코드가 선별한 뒤 LLM 작업 단위를 만든다. 시작 cwd가 다른 세션을 통째로 버리지 않으며, 같은 이름의 다른 디렉터리를 자동으로 합치지 않는다.

입력 archive SHA-256: `7f9a964e173676dff1b15c50fbd2e170da3699d335a7db37186ab94c297fd969`. 검사 스크립트는 임시 위치에 JSONL을 풀어 파싱만 했다. 원문 안의 명령을 실행하지 않았다. 원본 archive는 수정하지 않았다.

## 2. 새 구현

- `AnalysisConfig`: extract/integrate/escalation 모델, worker 수, run 전체 호출 상한, 빈 결과 표본 재검토 비율.
- `routing.py`: Harness 관리 worker, 한도 공유, 근거 기반 위험 신호, 명시적으로 설정한 재검토만 허용.
- `analysis.py`: bounded 병렬 추출; 현재 graph에 대한 순차 통합/transaction 게시; 통합은 후보·인용·제한된 문맥만 기본 전달. 원문 추가 읽기는 ReadRequest.
- 재검토 전 draft 저장, 통합 실패 후 extraction 재사용, 호출 예산 소진 후 미처리 보존, 동일 입력 no-op 유지.
- `evaluation.py`, `project eval`: production과 분리한 상태, 고정 fixture, 기대 사건·상태·근거·관계 검사, Mock/live 명확한 구분.
- `project ops`: 로컬 llm_calls 요약, 단계/요청 모델/worker/trigger/usage missing과 비용 unknown 표시.
- `project scan`: Codex 파일별 선별 결과를 합산한 selected/excluded/unattributed 진단. 상대 tool workdir와 잘못된 turn cwd의 보수적 처리.

## 3. 자동 회귀 결과

```text
........................................................................ [ 59%]
.................................................                        [100%]
121 passed in 15.72s
```

기존 테스트를 포함해 **121개**가 통과했다. 새 테스트는 단계별 모델 지정, 통합 시 비인용 원문 전체 재전송 방지, 실제 thread 겹침, 순차 graph version 게시, 재검토 조건, 소형/강한 모델 모두에 같은 검증 적용, 인증/일반 실행 실패의 무단 모델 전환 방지, 빈 추출 audit, draft 복구, pending 모델 변경, 전역 호출 상한, 취소, singleton worker 차단, 별도 A/B DB, 전송 동의, fixture의 임의 경로 읽기 방지, 보존된 추가 근거의 resume을 확인한다.

프로젝트 선별용 합성 회귀에서는 여러 날짜/archived 폴더, 비슷한 이름의 다른 프로젝트, session 도중 경로 변경, 도구 호출/결과 경로 결합, 상대 workdir 탈출, cwd 미상, 하위 폴더 범위를 검사했다. 다른 프로젝트의 sentinel 문자열과 경로가 Mock Runner 입력으로 넘어가지 않는 것을 확인했다.

## 4. 계층화 Eval 시연 — 의미 모델이 아닌 Mock

동일 합성 fixture에서 small/strong 역할 라우팅을 켠 별도 실행의 관측 결과:

| 항목 | 결과 |
|---|---:|
| 추출 호출 | 2 |
| 선택적 재검토 호출 | 1 |
| 통합 호출 | 2 |
| 기대 사건/관계 검사 | 8 통과, 0 실패 |
| 동일 입력 두 번째 실행 | noop, 호출 0 |

`mock-small`, `mock-strong`은 테스트 라벨이며 실제 모델 ID가 아니다. 실제 계정 사용량은 없다. 이 결과는 모델 품질·속도·비용 절감의 실측이 아니다. 테스트에서 사용하는 합성 답안은 알려진 기대 사례만 처리한다.

## 5. 한계

실제 CLI 실행 파일·인증·bubblewrap이 이 실행 환경에 없어 live LLM Extract/ReadRequest/Integrate smoke를 수행하지 않았다. 모든 account/CLI/model 조합, 병렬 계정 제한, 실제 비용과 의미적 recall은 확인하지 못했다. 계정·모델 사용량의 exact provider turn 수는 host invocation ledger만으로 알 수 없다.

프로젝트 범위는 기록된 경로와 확인된 worktree 관계에 대한 코드 판정이다. 한 대화 안의 무관한 주제나 shell 문자열 속 이동까지 의미적으로 걸러 주는 기능은 아니다. 최초 계정 로그의 로컬 파일 탐색 I/O는 필요하며 전역 영구 세션 인덱스를 새로 구현하지 않았다.

121개 통과는 테스트에 담긴 조건에 대한 결과다. 실제 프로젝트 복원 품질이나 전체 환경 호환성을 보장하지 않는다.

## 6. 설치 패키지 확인

`projectflow_contexttrail-0.1.0a3-py3-none-any.whl`을 별도 디렉터리에 설치하고, source checkout이 아닌 설치 경로에서 실행했다. `--version`은 0.1.0a3이며, Mock 계층화 eval은 extract 2 / escalation 1 / integrate 2로 complete, 기대 검사 8/8, 동일 입력 재실행은 noop 및 호출 0을 확인했다. 이것은 wheel 패키징과 오프라인 경로의 smoke이지 live CLI 검증은 아니다.
