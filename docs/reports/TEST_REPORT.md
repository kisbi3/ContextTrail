> 아래 내용은 초기 릴리스의 역사적 기록입니다. 모델 계층과 현재 사용 방법은 [TIERED_ANALYSIS.md](../guides/TIERED_ANALYSIS.md)를 참고하세요.

# 시험 보고서 · 0.1.0a2

2026-09-23. 아래는 이 전달물에 대해 실제 수행한 시험이다. 실제 사용자 프로젝트·AI 모델의 의미 정확도·모든 단말 호환성 측정이 아니다.

## 최종 자동 회귀 결과

```text
91 passed
```

실행 명령:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q --junitxml=docs/reports/artifacts/test-results.xml
```

기존 81개 회귀에 실제 archive에서 발견한 compact summary, replacement_history, tool-search, content attachment, verified subagent lineage, relocated sidecar, long-record fragmentation, binary marker, LLM Ops ledger 사례를 합성 fixture로 고정해 10개 회귀를 추가했다.

## 사용자 제공 실제 archive 형식 검증

Claude Code 40 JSONL + Codex 116 JSONL, 합계 156개 파일을 읽어 31,748개 정규화 record를 만들었다. 별도 real-log 검사 16/16이 통과했고 parser limitation은 0이었다. raw archive는 배포물에 포함하지 않는다. 세부 결과는 [REAL_LOG_VALIDATION.md](REAL_LOG_VALIDATION.md)를 참조한다.

이 검증은 **실제 LLM 의미 분석이 아니다.** 실제 archive로 parser·lineage·fragmentation·planner/store/Harness/Ops를 검증했고, 모델 호출 단계는 의미 사건을 생성하지 않는 offline structured Runner를 사용했다.

## 실제 확인한 항목

- engine: no-data/no-op의 Runner 미생성, 동일 입력 재호출0, empty 추출 완료, append 입력 제한, 기존ID 보존, 수정 의존성, 소실 발췌 보존, 통합 실패 후 추출 재사용, 잘못된 인용/상태 차단, DB rollback·CAS·lock.
- sources/Git: scope 경계, Codex mirrored event 중복 제거, JSONL append/partial 경계, archive 이동, 역할/tool 연결, Claude blocks/sidecar, summary 구분, symlink/경로 탈출, real worktree, 과거/현재 diff, 악성 외부 diff/textconv/fsmonitor 미실행, 특수문자 export 경로 제외.
- Harness/Runner: 허용 source ID·줄 범위, 제공된 근거 의존성, 보존 범위 밖 읽기 거부, 추가 근거 왕복 상한, headless argv 계약·JSON wrapper·금지 이벤트 거부, subprocess stdin/no-shell/timeout/cancellation. **argv unit test이지 실제 CLI 실행이 아니다.**
- render/UI: branch/join/cycle, 추정 점선, ASCII, 100개 이상 사건 번호, control/HTML/Mermaid 삽입 차단, 한국어 cell slicing. PTY에서 TERM 6개 조합, 100×30→52×18 크기 변경, 키 조작·정상 종료.
- HTTP/CLI: loopback bind, token 미인증 차단, Host/Origin, CSRF·명시확인·중복 POST, GET0 calls, graph/event/evidence 버전 일치, offline assets/CSP, export·overwrite/source/DB/symlink 보호.

## 별도 패키지·표시 시험

`pip wheel --no-deps --no-build-isolation`로 `0.1.0a2` wheel을 생성했다. wheel을 별도 target directory에 설치하고 그 경로를 우선 import하여 `python -m projectflow --version`과 합성 demo를 실행했다. 현재 컨테이너의 검증된 `jsonschema`·`wcwidth` dependency set을 사용했으며, 새 머신에서 패키지 저장소로 의존성을 내려받는 설치는 시험하지 않았다.

별도 빈 venv에서 `--no-deps`로 wheel만 설치한 smoke는 의도대로 `wcwidth` 미설치 때문에 시작되지 않았다. 이는 wheel의 dependency metadata가 잘못됐다는 의미가 아니라 `--no-deps` 환경에 runtime dependency를 설치하지 않은 결과다. dependency를 사용할 수 있는 격리 target에서는 `0.1.0a2` 버전 확인과 demo 실행이 정상 동작했다.

브라우저 asset JavaScript는 `node --check`로 구문을 검사했다. HTTP endpoint와 SVG XML은 자동 시험했다. 생성된 SVG를 CairoSVG로 정적 렌더링하고 한글 표시·분기·합류 배치를 확인했다. 중간 노드를 관통하는 긴 연결은 외곽 경로로 수정했다. **이것은 실제 Chromium/Firefox의 DOM·클릭·레이아웃 시험이 아니다.** 브라우저 실행 파일이 없어 해당 시험은 수행하지 않았다.

## 실행 환경

- OS: `Linux-6.18.44-x86_64-with-glibc2.41`
- Python: `3.13.5`
- Git: `git version 2.47.3`
- Runtime dependencies: `jsonschema 4.26.0`, `wcwidth 0.7.0`
- Test runner: `pytest 9.0.2`
- Codex CLI: 미설치. Claude Code CLI: 미설치. bubblewrap: 미설치. 실제 인증/model call: 미실행.

## 실제 환경에서 남은 검증

1. 사용자 서버의 두 CLI 각각: 설치·native auth·bwrap user namespace·필수 flags·단순 structured smoke. 이어서 도구/설정 차단 부정 시험과 두 출처 혼합 실제 분석.
2. 사용자 평가: 실제 프로젝트의 실패·전환·관측/보고 구분, 사건 누락·잘못된 관계·worktree 오귀속, 비용·속도·장기 증분의 유용성. 실제 기록의 사전 공유를 요구하지 않는다.
3. 실제 터미널·SSH·tmux/screen·IDE와 실제 브라우저: 한글 폰트·폭·키보드·협소 화면·포트 포워딩·서로 같은 버전의 근거 확인. TERM simulation을 이 시험의 완료로 취급하지 않는다.
4. 대형·장기 기록, 비표준 CLI 형식, 인증 refresh·조직 policy, 네트워크 파일시스템 등 문서에서 미지원/미검증으로 표시한 항목.

## 검증 중 수정한 사항

100개 이상 사건의 번호 잘림, 특수문자 export 경로 제외, Codex 금지 tool 시작 이벤트의 차단, Runner 인스턴스 재사용 시 호출 집계, 다른 명시적 갱신의 저장 graph TUI 반영, 긴 SVG 연결의 중간 노드 관통을 수정했다. 테스트 실행도 현재 shell의 PYTHONPATH에 의존하지 않도록 PTY 자식 환경을 명시했다. 최종 회귀 실행에서 위 테스트는 모두 통과했다.

원본 JUnit 결과는 [test-results.xml](artifacts/test-results.xml)에 포함했다. 합성 데모의 출력은 `examples/`와 [demo-noop.txt](artifacts/demo-noop.txt)에서 확인할 수 있다.
