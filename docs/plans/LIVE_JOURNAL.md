# 에이전트가 직접 적는 그래프 (live journal)

상태: 계획 (2026-10-07). 소유자 제안: "별도로 분석을 돌리지 말고, 작업한 AI가 ContextTrail을 써서 그래프를 만들게 하자."

## 1. 왜

지금의 비용 구조는 "이미 그 일을 한 모델이 있는데, 다른 모델이 기록을 처음부터 다시 읽는다"는 데서 나온다. 2026-10-07 절감 뒤에도 작업 단위 하나에 추출 14만 자, 통합 9만 자, 검토 5~6만 자를 보낸다(`docs/plans/SHARED_CONTEXT_STORE.md` §2026-10-07). 이 저장소에는 미분석 단위가 550개 남아 있고, 훅이 하루 8단위씩 따라잡는 구조로는 두 달이 걸린다.

작업을 한 에이전트는 그 자리에서 무엇을 왜 했는지 알고 있다. 사건 하나를 적는 데 드는 것은 출력 수백 토큰이다. 같은 사건을 분석기가 만들려면 입력 7만 토큰을 보낸다. 백 배 차이다.

## 2. 지킬 것

- **인용 불변식은 그대로.** 에이전트가 적어도 모든 사건과 관계는 기록에서 따온 인용을 달고, 코드가 그 인용을 세션 기록과 대조한 뒤에만 저장한다. 인용이 없거나 기록에 없으면 거부한다(`schema.EvidenceValidator`, 지금과 같은 검사).
- **자기 보고 편향은 상태 규칙으로 막는다.** `observed_success`/`observed_failure`는 도구 결과(`tool_result`) 인용이 있어야 하고, 없으면 `reported_complete`/`reported_failure`로만 남는다. 어시스턴트 문장만 인용한 "완료"는 검증된 결과가 아니다(`CLAUDE.md` 불변식과 같다).
- **분석은 명시 요청에만.** 에이전트가 적는 것은 모델 호출이 아니라 CLI에 기록을 넣는 일이다. 그래도 사용자가 스킬을 설치하거나 훅을 켠 뒤에만 일어난다. 끄면 아무것도 적지 않는다.
- **입력은 읽기 전용.** 세션 기록 파일과 opencode DB는 지금처럼 읽기만 한다. 적는 곳은 ContextTrail의 상태 디렉터리뿐이다.
- **한 쓰기 경로.** 저널도 분석기도 같은 `draft_delta` → `apply_delta` → `publish` → `link_request_turns`를 지난다. 저장 형식, 표시, `find`/`show`는 바뀌지 않는다.

## 3. 명령: `contexttrail note`

에이전트가 부르는 단 하나의 명령. 모델 호출 없음.

```
contexttrail note --kind outcome --status observed_failure \
  --title "concurrent write test failed" \
  --summary "JSONDecodeError under two writers" \
  --quote "concurrent write test: FAILED — JSONDecodeError" \
  [--verifies ev_… | --revises ev_… | --answers ev_… | --motivates ev_…] \
  [--session current] [--json]
```

- `--quote`는 반복 가능. 각 인용은 **현재 세션의 기록**에서 찾는다. 세션은 `--session current`(기본)로, 환경변수 `CLAUDE_CODE_SESSION_ID` / `CODEX_THREAD_ID` / `OPENCODE_SESSION_ID`에서 읽는다(`cli._session`; Claude Code 안에서 Codex 변수까지 보이는 경우가 있으므로 `CLAUDECODE`가 있으면 Claude 쪽을 우선한다).
- 세션 기록 읽기는 `freshness`의 부분 파싱을 쓴다: 그 세션의 파일 하나(Claude·Codex JSONL) 또는 DB의 그 세션만 파싱한다. 지금 세션 파일은 2초 안에 읽힌다(이 저장소 측정). 읽은 레코드는 `store.ingest`로 넣어 source id를 얻는다.
- 인용 → (source_id, start_line, end_line)은 코드가 찾는다. 레코드 본문에서 정확히 한 번 나오면 그 줄, 여러 번이면 가장 최근 레코드, 없으면 거부하고 비슷한 줄 몇 개를 보여 준다(`Harness.nearest_lines`와 같은 힌트). 에이전트는 줄 번호를 모르므로 요구하지 않는다.
- 사건 하나 = 후보 하나 → `draft_delta`로 delta → `EvidenceValidator.apply_delta`(지금의 모든 검사: 인용 대조, 상태 규칙, 관계 basis) → 통과하면 `publish`로 그래프 버전 하나 증가 → `link_request_turns`가 대화 순서로 묶는다. 실패하면 사유를 한 줄로 돌려주고 아무것도 저장하지 않는다.
- 관계: 에이전트가 `--verifies` 등으로 대상 사건 id를 줄 때만 명시 관계를 만든다(basis `explicit`, 인용은 그 사건의 인용과 같이 검사). 안 주면 지금처럼 코드가 대화 순서(`follows`, `structural`)로만 묶는다. `inferred`는 만들지 않는다.
- 출력: 저장된 `ev_…@vN` 한 줄(`--json`이면 사건·버전·검증 결과). 이 형식은 이미 `y` 키와 브라우저 버튼이 복사하는 참조와 같다.
- `contexttrail note --list`: 이 세션에 적힌 사건. 세션 끝 훅이 "안 적은 게 있나" 물을 때 에이전트가 먼저 본다.
- 잠금: `publish`는 `analyze.lock` 아래에서 짧게 돈다. 분석이 돌고 있으면 기다리지 않고 "분석 중, 잠시 뒤 다시"로 거부한다(훅 안에서 멈추면 안 된다).

## 4. 세 도구에 끼워넣기

공통: 스킬 `contexttrail-note`(에이전트가 언제 무엇을 적는지)와 세션 끝 훅(안 적었으면 한 번 되돌리기). 둘 다 사용자가 설치할 때만 켜진다. `install-commands`와 `install-hooks`가 설치하고, `--force` 규칙은 지금과 같다.

### 4.1 스킬 본문의 규칙

적는 시점: 사용자가 요청을 하거나 결정을 내릴 때(`question`/`decision`), 파일을 고치고 나서(`action`/`revision`, 편집 도구 호출이나 패치 결과를 인용), 테스트·빌드·명령을 돌리고 결과를 봤을 때(`outcome`, 결과 출력을 인용, 그것이 어느 변경을 검증했는지 `--verifies`), 커밋했을 때(`outcome`, 커밋 결과 인용). 한 턴에 여러 개를 적어도 된다. 모르는 것은 적지 않는다. 인용은 화면에서 본 그대로 복사한다. 거부되면 인용을 고쳐 한 번만 다시 시도한다.

### 4.2 Claude Code

- 스킬: `~/.claude/skills/contexttrail-note/SKILL.md`. 모델이 스스로 고를 수 있어야 하므로 `disable-model-invocation`은 쓰지 않는다(update 스킬과 반대).
- 세션 끝: 기존 `Stop` 훅(`contexttrail auto-update`)이 설정에 따라 JSON을 돌려준다. 확인한 사실(code.claude.com/docs/en/hooks, 2026-10-07): 입력에 `session_id`, `transcript_path`, `cwd`, `stop_hook_active`, `last_assistant_message`가 오고, 출력 `{"decision": "block", "reason": …}`이면 Claude가 멈추지 않고 그 이유를 보고 계속하며, `hookSpecificOutput.additionalContext`로 문맥을 더 줄 수 있다. `stop_hook_active`가 참이면 이미 한 번 되돌린 것이므로 다시 막지 않는다.
- 되돌리는 조건: 이 프로젝트에 저널이 켜져 있고, `stop_hook_active`가 거짓이고, 이 세션에 편집이나 실행 결과가 있는데 `note`가 하나도 없을 때. `reason`은 "이 세션의 결정·변경·결과를 `contexttrail note`로 적고 끝내라"는 한 줄. 세션당 한 번.
- `claude -p`에서는 비동기 훅이 종료 시 취소되므로(2026-10-06 확인) 되돌리기는 대화형 세션에서만 작동한다. 저널 훅은 `async`가 아니어야 한다(되돌리려면 결과를 기다려야 한다). 자동 분석 훅은 비동기, 저널 훅은 동기로 **둘을 분리**한다.

### 4.3 Codex

- 스킬: `~/.codex/skills/contexttrail-note/`(`agents/openai.yaml`에서 암묵 호출 허용).
- 세션 끝: 확인한 사실(learn.chatgpt.com/docs/hooks, 2026-10-07): `Stop` 훅이 `decision: "block"`과 `reason`을 돌려주면 "Codex가 계속하며 자동으로 이어지는 프롬프트를 만든다". 입력에 `session_id`, `cwd`, `transcript_path`, `turn_id`. `SessionStart`/`UserPromptSubmit`의 `additionalContext`로 개발자 문맥을 더할 수도 있다. 새 hooks.json은 TUI에서 신뢰해야 돈다(2026-10-06 확인).
- 루프 방지: Codex 입력에 `stop_hook_active`에 해당하는 필드가 문서에 없으므로, 상태 디렉터리에 "이 세션에 되돌린 적 있음"을 적어 두고 한 번만 막는다.

### 4.4 opencode

- 명령: `~/.config/opencode/commands/contexttrail-note.md`는 사용자가 칠 때만 돌므로 규칙은 **스킬**로 둔다. opencode는 `~/.claude/skills`도 읽는다(2026-10-06 확인)고 했으므로 Claude Code 스킬이 그대로 보인다. 확인 필요: opencode가 스킬을 스스로 고르는 조건.
- 세션 ID: 플러그인의 `shell.env` 훅(`input: {cwd, sessionID?, callID?}`, `output: {env}`; 타입 확인 2026-10-07)으로 도구 셸에 `OPENCODE_SESSION_ID`를 넣는다. 이것이 없으면 `--session current`가 opencode에서 동작할 수 없다.
- 세션 끝: 플러그인 `session.idle`에서 이 세션에 `note`가 없으면 SDK `client.session.prompt({path: {id}, body: {parts: [{type: "text", text: …}]}})`로 "적고 끝내라"를 한 번 보낸다. 확인 필요: 플러그인 안에서 실행 중인 인스턴스에 `session.prompt`를 부를 수 있는지(문서는 메서드만 확인), idle → prompt → idle 루프를 세션당 1회로 막는 방법(상태 디렉터리에 기록).
- 자동 분석 플러그인과 같은 파일에 넣되 설정으로 따로 켠다.

## 5. 분석기의 새 역할

- **백필.** 에이전트가 없던 과거 기록은 분석기만 만들 수 있다. `analyze`는 그대로 둔다.
- **저널이 있는 세션은 분석 계획에서 뺀다(기본).** 같은 세션을 두 번 적지 않는다. 저널 사건이 있는 세션의 레코드는 `processed`로 표시한다. 다만 저널은 에이전트가 적은 것만 있으므로, 빠진 것을 찾는 **감사** 실행(`analyze --audit`)을 2단계로 남긴다: 그 세션을 다시 읽어 기존 사건과 대조하고 누락 후보만 더한다. 처음 구현에서는 하지 않는다.
- 자동 갱신 훅(`auto-update`)은 저널이 없는 세션에만 분석을 띄운다.

## 6. 평가

- Mock: 합성 세션 기록에 `note`를 넣어 그래프가 분석기 결과와 같은 형태(사건·관계·검증 상태)로 나오는지. 거부 경로(인용 없음, 도구 결과 없는 `observed_success`, 모르는 대상 id).
- 실제: 이 저장소의 세션 하나(이 계획을 쓴 세션)를 에이전트가 적게 하고, 같은 세션을 분석기로 돌려 사건 수·관계·검증 상태를 비교한다. 비용은 note 수와 출력 토큰으로 센다. 비교표를 README의 측정 절에 넣는다.

## 7. 단계

A. `note` 명령: 세션 찾기, 인용 → 줄, 후보 → delta → 검사 → 발행, `--list`, `--json`. 테스트.
B. Claude Code: 스킬 본문, 동기 `Stop` 훅의 되돌리기(설정 `journal` 켜기/끄기), 이 세션으로 실제 확인.
C. Codex: 스킬, `Stop` 되돌리기, 한 번 확인.
D. opencode: `shell.env`, `session.idle` 프롬프트, 확인 필요 항목 해소.
E. 분석 계획에서 저널 세션 제외, 자동 갱신과의 분담, 문서(README·CLAUDE.md·SECURITY).
F. 평가(§6)와 측정. 그 뒤에 `--audit`.

## 8. 열린 결정

- note마다 바로 발행할지, 세션 끝에 묶어 발행할지. **바로 발행**을 택한다: `find`가 곧바로 보고, 잠금이 짧고, 묶는 상태를 따로 둘 필요가 없다. 그래프 버전이 자주 오르지만 비용은 없다.
- 저널 세션을 분석기에서 뺄지. **뺀다**(§5). 되돌리고 싶으면 `--audit`.
- 사용자 요청(`question`)을 에이전트가 적게 할지, 지금처럼 코드가 `add_user_requests`로 만들지. **코드가 만든다**: 사용자 메시지는 기록에 있고 코드가 더 정확하다. 에이전트는 결정·변경·결과만 적는다.

## 9. 비용

note 하나: 출력 200~400토큰, 스킬 본문이 문맥에 1~2k토큰. 세션당 note 5~15개. 분석기 단위 하나(입력 7만 토큰)와 비교하면 세션 전체를 적어도 그 1/10 아래다. 대가는 작업 세션의 문맥을 조금 쓰는 것과, 에이전트가 적는 데 드는 몇 초다.
