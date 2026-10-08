# 에이전트가 직접 적는 그래프 (live journal)

상태: A~E 구현, 세 도구 모두 실제 확인 (2026-10-07). `--audit`는 설계(§11)와 Mock 구현까지(2026-10-08), 실제 모델 실행은 로컬 확인 필요. 남은 것: F(평가), Codex 재확인(크레딧 소진으로 마지막 수정 뒤 미확인). 진행 기록은 §10. 소유자 제안: "별도로 분석을 돌리지 말고, 작업한 AI가 ContextTrail을 써서 그래프를 만들게 하자."

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

### 4.5 모양: ponytail처럼, 뒤에 CLI가 있는 스킬

소유자 질문(2026-10-07): [ponytail](https://github.com/dietrichgebert/ponytail)처럼 스킬로 만들자는 것인가. 그렇다. 에이전트에게 보이는 부분은 ponytail과 같은 모양이다: `SKILL.md` 하나가 언제 무엇을 할지 정하고, 슬래시 명령과 선택적 훅이 붙고, Claude Code·Codex·opencode 각각에 맞게 설치된다. 지금의 `contexttrail-update`/`contexttrail-context`가 이미 그 방식이고 `contexttrail-note`도 같은 자리에 들어간다.

차이는 하나다. ponytail은 프롬프트만으로 된 도구라 스킬 본문이 전부이고 뒤에 코드나 데이터가 없다. 여기서는 스킬이 "적어라"고 시키되 적는 행위는 `contexttrail note` 호출이고, 그 뒤에서 코드가 인용을 세션 기록과 대조해 그래프에 저장한다. **스킬은 껍데기, 본체는 CLI.** 스킬만 배포하면 에이전트가 메모를 남기는 것까지는 되지만 검증도, 저장소도, 세 도구 공유도 없다.

ponytail에서 가져올 것은 설치 방식이다. Claude Code의 플러그인 마켓플레이스(`/plugin marketplace add …`)로 스킬·명령·훅을 한 번에 설치한다. ContextTrail은 지금 `install.sh`와 `install-commands`/`install-hooks`로 홈에 파일을 쓰는데, Claude Code용은 플러그인 묶음(스킬 셋 + Stop 훅 정의)을 저장소에 두어 한 줄로 설치하게 하고, Codex·opencode는 지금 방식으로 둔다. CLI(파이썬 패키지)는 여전히 `pipx install contexttrail`이 필요하므로 플러그인의 스킬 본문이 그 설치를 먼저 확인하게 한다. 단계 B에 "플러그인 묶음"을 넣는다.

## 5. 분석기의 새 역할

- **백필.** 에이전트가 없던 과거 기록은 분석기만 만들 수 있다. `analyze`는 그대로 둔다.
- **저널이 있는 세션은 분석 계획에서 뺀다(기본).** 같은 세션을 두 번 적지 않는다. 저널 사건이 있는 세션의 레코드는 `processed`로 표시한다. 다만 저널은 에이전트가 적은 것만 있으므로, 빠진 것을 찾는 **감사** 실행(`analyze --audit`)을 2단계로 남긴다: 그 세션을 다시 읽어 기존 사건과 대조하고 누락 후보만 더한다. 처음 구현에서는 하지 않는다.
- 자동 갱신 훅(`auto-update`)은 저널이 없는 세션에만 분석을 띄운다.

## 6. 평가

- Mock: 합성 세션 기록에 `note`를 넣어 그래프가 분석기 결과와 같은 형태(사건·관계·검증 상태)로 나오는지. 거부 경로(인용 없음, 도구 결과 없는 `observed_success`, 모르는 대상 id).
- 실제: 이 저장소의 세션 하나(이 계획을 쓴 세션)를 에이전트가 적게 하고, 같은 세션을 분석기로 돌려 사건 수·관계·검증 상태를 비교한다. 비용은 note 수와 출력 토큰으로 센다. 비교표를 README의 측정 절에 넣는다.

## 7. 단계

A. `note` 명령: 세션 찾기, 인용 → 줄, 후보 → delta → 검사 → 발행, `--list`, `--json`. 테스트.
B. Claude Code: 스킬 본문, 동기 `Stop` 훅의 되돌리기(설정 `journal` 켜기/끄기), 저장소 안의 플러그인 묶음(스킬 셋 + 훅 정의, `/plugin marketplace add`로 설치; §4.5), 이 세션으로 실제 확인.
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

## 10. 진행 기록

### 2026-10-07 — A단계: `contexttrail note`

구현: `src/contexttrail/journal.py`, `cli.py`의 `note`, `Store.ingest(partial=True)`, `parse_opencode(session=…)`. 테스트 `tests/test_journal.py`(7개), 전체 489 통과.

계획과 달라진 점:
- **관계 플래그의 방향.** 관계는 앞선 사건에서 뒤 사건으로 가므로(`prompts/common.md`), `--verifies/--revises/--answers/--motivates X`는 모두 X → note다. `--motivates X`는 "X가 이 note의 동기"로 읽는다. 에이전트가 반대로 적은 `verifies`/`answers`는 기존 `orient_relation`이 바로잡는다.
- **도구 결과 없는 `observed_*`는 낮춰 저장하지 않고 거부한다.** 조용히 `reported_*`로 바꾸면 에이전트가 모른 채 넘어간다. 오류 문구가 `reported_complete`/`reported_failure`를 알려 준다.
- **인용 찾기.** 세션 기록 전체에서 가장 최근 레코드를 고른다. 한 레코드 안에 여러 번 나오면 마지막 것의 줄을 대고, 검사기가 그 줄 안에서 기존 규칙(정확한 부분 인용 → 줄 전체 + focus)으로 정한다. 8자 미만은 거부. note 호출 자체와 그 출력은 모든 인용을 담고 있으므로 출처에서 뺀다(`quotable`).
- **사용자 요청은 코드가 만든다**(§8대로): 첫 note 때 `link_request_turns`가 그 세션의 사용자 메시지를 요청 사건으로 더하고, 관계가 없는 note는 그 턴에 대화 순서로 묶인다.
- `analyze --session current`도 같은 세션 판별(`journal.current_session`)을 쓴다. Claude Code 안에서는 `CLAUDECODE`가 있으면 Claude 쪽을 고른다.

실제 확인(읽기만, 발행 없음): 이 세션의 기록 3,790개를 0.94초에 읽었고, 테스트 결과 줄을 인용하면 그 `tool_result`의 해당 줄로 찾았다.

아직 하지 않은 것: 저널 세션을 분석 계획에서 빼기(E)와 처리 표시. 그 전까지는 note를 쓴 세션을 분석기가 다시 읽으면 같은 일이 두 번 적힐 수 있다. 실제 그래프에 note를 쓰는 확인은 B단계(스킬과 함께)에서 한다.

### 2026-10-07 — E, B, C, D: 분석에서 빼기, 스킬, 턴 끝 알림, 플러그인

- **E(분석 쪽).** note를 쓰면 그 세션의 레코드를 처리됨으로 발행하고 `journal_sessions`에 세션을 적는다. 이후 모든 scan에서 `Store.acknowledge_journaled`가 그 세션(과 하위 에이전트 세션)의 새 레코드도 처리됨으로 표시하고, 결과 없는 `parsed` 단위는 대기 레코드가 없으면 보내지 않고 `superseded`로 둔다. freshness도 세지 않는다. 문서 갱신은 아래와 함께.
- **켜기.** 계획의 "저널 켜기"를 `contexttrail note --enable/--disable`로 했다. 꺼진 프로젝트에서는 note가 거부된다. 스킬은 모든 프로젝트에서 보이지만 꺼진 곳에서는 첫 거부 뒤 쓰지 않도록 본문에 적었다.
- **B·C 턴 끝 알림.** `contexttrail note --hook`(동기 `Stop` 훅). 자동 갱신 훅(비동기)과 나란히 `install-hooks`가 Claude Code·Codex에 설치한다. 조건은 계획과 조금 다르다: "세션당 한 번"이 아니라 **마지막 note(또는 마지막 알림) 뒤에 편집·커밋·테스트·Git 변경 호출이 있을 때 한 번**. 세션당 한 번이면 긴 세션의 뒷부분이 적히지 않은 채 분석에서도 빠지기 때문이다. 같은 턴의 재알림은 Claude Code의 `stop_hook_active`와, Codex용으로 상태에 남기는 마지막 알림 시각이 막는다. 시각은 벽시계가 아니라 기록 시각으로 비교한다. 읽기 전용 호출과 일반 `run`은 알림 대상이 아니다.
- **D opencode.** 플러그인이 `shell.env`로 `OPENCODE_SESSION_ID`를 도구 셸에 넣고, `session.idle`에서 `note --hook`의 답이 `block`이면 `client.session.prompt`로 알림을 보낸다. 알림 문구는 `HARNESS_TEXT`에 넣어 사람의 요청으로 잡히지 않게 했다. **실제 opencode에서는 아직 확인하지 않았다**(`shell.env` 훅 이름과 `session.prompt` 호출은 타입 정의 기준).
- **플러그인.** 저장소 루트 `.claude-plugin/marketplace.json`과 `plugins/contexttrail/`(스킬 note·context·update, 훅 두 개). 설치된 스킬과 같은 생성기(`plugin_files`)에서 만들고 테스트가 같은지 확인한다. `claude plugin validate`가 두 manifest 모두 통과. 플러그인 훅은 `contexttrail`이 PATH에 없으면 아무것도 하지 않는다.

**이 저장소에서 실제 확인(Claude Code, 이 세션).** `note --enable` 뒤 다섯 개를 적었다: 변경 둘, 그 변경을 검증한 테스트 결과 둘(`--verifies`), 결정 하나. 그래프 v22 → v27. 첫 note가 이 세션의 사용자 메시지 149개를 요청 사건으로 더했다(§8대로 코드가 만든 것). `show`에서 변경은 "검증: 전체 테스트 497개 통과"로 보인다. 훅을 손으로 불러 보니 note 뒤 작업이 없을 때는 조용했고(1.5초), 테스트를 돌린 뒤에는 `block`을 돌려줬으며, 두 번째 호출은 조용했다.

**처음 실제 사용에서 찾은 버그.** note 호출을 인용 출처에서 빼는 판별이 "명령에 `contexttrail note`라는 글자가 있는 호출"이어서, note 기능 코드를 파일에 쓴 도구 호출까지 빠져 그 코드 줄을 인용할 수 없었다. 셸 명령을 조각으로 나눠 실제로 `note`를 **실행하는** 조각이 있을 때만 빼도록 고쳤다(`runs_note`; `$(…)`, 변수에 담은 프로그램, `python -m` 포함, heredoc 본문 제외).

남은 것: Codex와 opencode에서 실제 확인(Codex는 새 훅을 TUI에서 신뢰해야 돈다), F 평가(같은 세션을 분석기로 돌린 결과와 비교), `--audit`.

### 2026-10-07 — opencode·Codex 실제 확인과 그때 찾은 것

임시 git 프로젝트(스크래치패드)에서 note를 켜고, 각 도구에 note 이야기를 하지 않은 채 작은 작업(함수 추가 + 실행 확인)을 시켰다.

**opencode (1.18.33, 무료 모델).**
- `shell.env`로 넣은 `OPENCODE_SESSION_ID`가 도구 셸에 보였다.
- `opencode run`은 세션이 idle이 되자마자 인스턴스를 닫아 플러그인의 알림이 나가지 못한다. TUI처럼 계속 떠 있는 `opencode serve`에서는 됐다: idle → `note --hook`이 `block` → `client.session.prompt`로 알림 → 에이전트가 `contexttrail-note` 스킬을 불러 note 두 개를 적었고, 그 다음 idle에는 다시 알리지 않았다.
- 찾은 것 1: 그 서버를 Claude Code 셸에서 띄워 `CLAUDECODE`·`CLAUDE_CODE_SESSION_ID`를 물려받았고, `--session current`가 Claude 세션을 골랐다. 고침: 플러그인이 호출마다 넣는 `OPENCODE_SESSION_ID`가 가장 구체적이므로 먼저, 그리고 보이는 세션 중 **이 프로젝트에 기록이 있는 첫 세션**을 쓴다(`current_in_project`).
- 찾은 것 2: 실행 결과가 `2` 한 글자라 8자 하한에 걸려 `observed_*`로 적지 못하고 `reported_complete`로 내려갔다. 고침: 도구 출력의 **한 줄 전체**와 같으면 짧아도 받는다(가장 최근 것).
- 찾은 것 3: 명령을 인용하고 `observed_success`를 주자 "observed status needs original tool_result evidence"만 나왔다. 고침: 어디서 찾았는지와 무엇을 인용해야 하는지 말한다.
- opencode는 `~/.claude/skills`와 `~/.agents/skills`를 모두 읽어 같은 이름의 스킬에 경고를 낸다(전부터 update·context도 그랬다). 동작에는 영향 없다.

**Codex (`codex exec --dangerously-bypass-hook-trust`, workspace-write).** 이 플래그는 훅 신뢰를 이 실행에만 건너뛴다. `hooks.json`에는 ContextTrail 훅 둘뿐인 것을 확인하고 썼다.
- `Stop` 훅의 `block`으로 Codex가 이어서 스킬을 읽고 `note`를 불렀다.
- 찾은 것 4: Codex 기본 샌드박스는 `.git`을 읽기 전용으로 두는데 상태가 `.git/contexttrail`에 있어 note가 `Operation not permitted`로 실패했다. 고침: **대기열.** 상태를 쓸 수 없으면 `note`는 인용까지 확인한 뒤 사용자 임시 디렉터리(`$TMPDIR/contexttrail-notes/<scope>/`, 0700/0600)에 넣고 `q_…` ID를 준다. 샌드박스 밖에서 도는 턴 끝 훅이 대기열을 오래된 순으로 모든 검사와 함께 저장하고, 실패한 것이 있으면 그 이유로 한 번 되돌린다. 뒤 note는 `--verifies q_…`로 앞의 대기 note에 연결할 수 있다(저장 때 실제 ID로 바뀐다). 분석이 잠금을 잡고 있으면 다음 훅까지 남겨 둔다. 다시 실행해 보니 변경 note가 대기열을 거쳐 저장됐다(v3).
- 찾은 것 5: 에이전트가 결과 줄(`div ok 2.0`)을 인용했는데, 같은 줄을 되풀이한 자기 메시지가 더 최근이라 그쪽이 잡혀 `observed_success`가 거부됐다. 고침: `observed_*`면 도구 출력부터 찾는다. 같은 Codex 세션 기록에 다시 적어 보니 도구 결과를 인용해 `verifies`까지 저장됐다(v4).
- 찾은 것 6: 에이전트가 `reported_complete`에 `--verifies`를 붙였고, 대기열 저장 때 거부됐는데 Codex는 같은 턴에 두 번째 되돌림을 하지 않아 이유가 전달되지 않았다. 고침: 그 조합은 note를 적을 때 바로 거부하고, 대기열 거부 이유는 `note --list`에도 보인다.
- 마지막 수정 뒤의 Codex 재실행은 "workspace is out of credits"로 하지 못했다. 고친 동작은 단위 테스트와 위 기록 재생으로만 확인했다.

**Claude Code (`claude -p`, 같은 임시 프로젝트, 2026-10-07).** note 이야기 없이 함수 추가와 실행 확인을 시켰다. 턴이 끝나자 동기 `Stop` 훅의 `block`이 "Stop hook feedback"으로 들어갔고, Claude가 `contexttrail-note` 스킬을 스스로 불러 note 두 개(변경, 그 변경을 `verifies`로 검증한 관측 성공 결과)를 적었다. 적은 뒤의 훅 호출은 조용했다. 비동기 훅은 `claude -p` 종료 때 취소되지만(2026-10-06) 이 동기 훅은 `-p`에서도 돈다. 이로써 세 도구 모두에서 알림 → 스킬 → note 저장이 확인됐다.

### 2026-10-07 — 플러그인 설치 실제 확인과 그때 찾은 것

새 임시 git 프로젝트에서 README 그대로 GitHub 마켓플레이스를 추가하고(`claude plugin marketplace add kisbi3/ContextTrail`, 이 프로젝트에만 `--scope local`) `contexttrail@contexttrail`을 설치했다. 클론·검증·설치가 되고, `claude plugin details`에 스킬 3개(`context`·`note`·`update`)와 `Stop` 훅이 보였다(상시 비용 약 264토큰). `claude -p`의 init에도 플러그인과 `contexttrail:note` 등이 올라왔다. 사용자 설정의 훅을 빼고(`--setting-sources project,local`) 플러그인 훅만으로 돌렸다.
- 찾은 것 7: Claude가 Edit 대신 `printf … >> calc.py`로 고쳐 그 호출이 `run`으로 분류됐고, 알림이 나가지 않았다. 고침: 셸 명령이 파일을 쓰면(따옴표 밖의 `>`·`>>`, `/dev/null`과 `2>&1`은 빼고; `sed -i`·`tee`·`mv`·`cp`·`rm`·`touch`·`mkdir`·`git apply` 등) 남길 작업으로 센다(`writes_files`). 다시 돌리니 알림 → Claude가 플러그인 스킬 `contexttrail:note`를 불러 변경과 그 변경을 `verifies`로 검증한 결과를 적었다. 알림 문구는 두 스킬 이름을 모두 말한다.
- 찾은 것 8: `install-hooks --claude`와 플러그인을 둘 다 깔면 같은 훅이 둘 돌아 알림이 두 번 가고, 대기열을 두 번 저장할 수 있다. 고침: 훅 본문을 프로젝트별 파일 잠금(`_hook_lock`) 안에서 돌려 둘째는 기다린 뒤 이미 알린 것을 본다. 둘 다 깐 상태로 실제로 돌려 알림은 한 번이었다.
- 찾은 것 9: heredoc으로 쓴 코드 한 줄(`def neg(a):`)을 인용한 변경 note가 "applied status is only for … citing a patch or diff"로 거부됐다. Claude가 끝 메시지에서 그 코드를 되풀이해 그쪽이 더 최근이었기 때문이다. 고침: `applied`면 도구 호출·결과부터 찾는다(찾은 것 5와 같은 방식). 그 세션 기록에 다시 적어 보니 `applied`로 저장됐다.

### 2026-10-08 — Linux 확인과 `git fetch` 오탐

- **Linux.** cloud 환경(Linux, Python 3.13)에서 전체 테스트가 506 통과, 1 건너뜀(macOS 전용 `sandbox-exec`). Linux에서만 실패하는 것은 없었다.
- 찾은 것 10: 상태 확인만 한 턴에도 알림이 갔다. `git fetch`가 분석기에서 `vcs`로 분류되고 `WORK_HINTS`가 `vcs`를 남길 작업으로 세기 때문이다. 고침은 note 쪽(`journal._changes_git_state`)에서만: `vcs` 호출 중 작업 트리·브랜치·원격을 바꾸는 Git 하위 명령이 하나도 없으면 센다. 뺀 것은 로컬 ref를 쓰는 refspec(`origin main:main`)과 `--update-head-ok`가 없는 `fetch`뿐이다. `git remote update`는 원래 `vcs`가 아니다. `analysis.command_kind`/`_vcs_mutates`는 그대로이므로 분석기의 `tool_steps` 입력은 바뀌지 않는다.

## 11. `analyze --audit` 설계 (2026-10-08)

note가 있는 세션은 분석에서 빠진다(§5). 에이전트가 적지 않은 것은 그래프에 없다. `--audit`는 그 세션을 다시 읽어 **note와 이전 분석이 적지 않은 것만 더하는** 실행이다. 기존 사건은 읽기만 하고 바꾸지 않는다. 사용자가 명시적으로 부를 때만 돈다(자동 갱신 훅은 감사를 하지 않는다).

### 11.1 무엇을 읽나

- **감사 대상 세션**: `Store.journaled_sessions()`의 세션과 그 하위 에이전트 세션(`acknowledge_journaled`와 같은 규칙). `--session ID|current`면 그 세션과 하위 세션만. note가 없는 세션을 지정하면 거부한다("note가 없는 세션: `analyze --session`으로 일반 분석").
- **감사 대상 기록**: 그 세션들의 기록 중 **통합이 끝난 단위(`integrated`)의 sources에 없는 것**. 곧 (a) note를 쓰기 전에 분석기가 이미 처리한 기록은 다시 읽지 않고, (b) 감사를 마친 기록도 다시 읽지 않는다. 처리 표시(`processed_hash`)는 쓰지 않는다: note가 세션 전체를 처리됨으로 표시하므로 구분이 안 된다. 감사 뒤에 기록 내용이 바뀌어도(마지막 메시지가 길어지는 경우 등) 다시 감사하지 않는다. Git 기록은 세션이 아니므로 감사 대상이 아니다(일반 분석이 그대로 다룬다).
- **단위**: 일반 분석과 같은 `_session_unit_chunks`(세션·worktree 단위, 압축·3시간 공백에서 끊기, 비용·페이로드 상한, 안전한 경계). 단위 id는 `audit_` 접두사(`ident("audit_", …)`)라서 같은 기록을 묶은 일반 단위와 겹치지 않는다. 같은 `work_units` 표에 저장하고 상태 흐름(`parsed → extracted/draft → integrated`)과 추출 재사용(라우팅 서명·문맥 다이제스트)도 같다. **일반 계획은 `audit_` 단위를 건드리지 않는다**(재계획·supersede·교정 모두 제외). 보정(`calibration`)에서도 뺀다: 감사 단위는 입력이 달라 일반 단위의 비용 추정을 흐린다.

### 11.2 모델에게 note를 어떻게 보여 주나

- 추출 입력은 일반과 같고(`new_records`, `tool_steps`, `user_requests`, `existing_*`), 감사일 때만 `journal_audit`가 더해진다: `{"note_event_ids": [...]}` — 이 단위의 기록을 인용한 note 사건의 id. 사건 내용은 따로 복사하지 않고 `existing_events`(제목·요약·상태·`origin: "note"`)와 `existing_evidence`(인용한 줄)가 보여 준다. 그 사건들이 문맥 선택의 12개 상한에 밀리지 않도록 note id를 단서(`clues`)로 주고 `Harness.keep_events`로 상한을 넘겨 모두 싣는다(상한은 최대 40개, 넘으면 오래된 것부터 줄이고 `input_limitations`에 쓴다).
- 지시문은 `prompts/audit.md`(영어)를 **감사일 때만** `instructions` 뒤에 붙인다(`build_task`가 `data`에 `journal_audit`가 있을 때만). 일반 요청의 바이트는 바뀌지 않으므로 끝난 단위가 다시 나가지 않는다. 내용: 이 기록은 작업한 에이전트가 이미 note로 적었다 / note가 다루지 않은 결정·변경·결과만 후보로 / note를 되풀이하는 후보는 만들지 말고 확신이 없으면 `existing_event_matches` / 기존 사건을 바꾸지 않고 새 사건과 관계만 더한다(관계는 note 사건에도 이을 수 있다) / 사용자 메시지는 이미 요청 사건이 있다.
- 통합 요청에도 같은 `journal_audit`와 지시문이 붙는다(통합자가 note를 바꾸지 않도록).
- **의미 검토(semantic review)는 감사에서 끈다.** 감사는 값싼 그물이어야 하고, 검토 신호가 남는 사건은 `semantic_review_audit.status = skipped_disabled`로 기록된다.

### 11.3 중복을 어떻게 막나

1. **구성상**: 이미 분석된 기록과 감사를 마친 기록은 읽지 않는다(11.1).
2. **필수 인용**: 일반 분석은 "파일을 확실히 고친 호출은 어느 사건이 인용해야 한다"(`required_citations`)를 요구한다. 감사에서는 **이미 그래프의 어느 사건이 인용한 호출은 요구하지 않는다.** 그렇지 않으면 note가 적은 편집마다 중복 후보를 강요한다. 거꾸로, 어느 사건도 인용하지 않은 편집은 여전히 인용해야 한다 — 에이전트가 빠뜨린 편집을 찾는 것이 감사의 핵심이다.
3. **모델**: 11.2의 지시문과 `existing_event_matches`. 추출이 후보 하나를 정확히 한 기존 사건에 대응시키면 `draft_delta`가 그 후보를 중복으로 처리하고 관계를 기존 사건으로 옮긴다(기존 경로).
4. **코드(추출 뒤)**: 후보의 종류가 note 사건과 같고 인용한 줄(source_id, 시작·끝 줄)이 그 note 사건의 것과 정확히 같으면 코드가 `existing_event_matches`를 더해 중복으로 처리한다(감사 건수에 센다). 줄이 겹치기만 하는 경우는 건드리지 않는다: 한 줄에 서로 다른 두 결과가 있을 수 있다.
5. **코드(통합 검사)**: 감사의 delta는 **더하기만** 한다. `events_to_update`, `edges_to_invalidate`, `open_items_to_resolve`가 비어 있지 않거나 `open_items_to_upsert`가 기존 항목의 id를 쓰면 거부하고 한 번의 수리로 되돌린다(영어 한 줄). note 사건과 간선에는 `origin: "note"`가 있고, 감사가 더한 사건·간선에는 `origin: "audit"`를 붙인다(대화 순서 간선의 `dialog_turn`은 그대로). `origin`은 화면에 영향을 주지 않는다.
6. 사용자 메시지: 감사에서는 `add_user_requests`를 하지 않는다. note의 첫 발행이 `link_request_turns`로 이미 모든 사용자 메시지의 요청 사건을 만들었으므로, 코드가 같은 메시지의 요청 후보를 또 더하면 중복이 된다. 요청 사건이 없는 메시지(note 뒤에 생긴 것)는 발행 직전 `link_request_turns`가 일반 경로로 더한다.

### 11.4 기록과 어떻게 맞물리나

- `journal_sessions`와 `acknowledge_journaled`는 그대로다: 감사는 이 목록을 읽기만 한다. 감사가 발행할 때 단위의 기록을 처리됨으로 표시하지만 이미 처리됨이다.
- 발행은 일반과 같은 `publish`(단위를 `integrated`로)다. 그래프의 `analysis_status`/`coverage`는 **바꾸지 않는다**: 감사는 일반 분석의 남은 기록을 말해 주지 않는다. `--session`의 "순서 밖 사건" 표시도 하지 않는다(감사는 일반 분석이 다루지 않는 기록이다).
- 실행 결과의 상태는 감사 단위가 남았는지로 정한다(`records_waiting`): 다 했으면 `complete`, 아니면 `partial`(종료 코드 2). 일반 분석의 남은 기록 때문에 `partial`이 되지 않는다.
- 같은 세션에 note를 더 쓴 뒤 다시 감사하면, 새로 생긴 기록(감사 단위에 없는 것)만 새 단위로 읽는다. 이미 감사한 기록의 단위는 그대로다.

### 11.5 비용 안내와 동의

- 같은 `consent(snapshot, plan)` 경로다. `plan`에 `audit: true`와 대상 세션 수가 들어가고, `plan_text`는 맨 앞에 "감사 (note가 있는 N개 세션을 다시 읽음)"을 붙인다. 숫자(단위·호출 상한·입력 토큰·시간)는 일반 분석과 같은 추정이다(문자 수 ×3, 과거 단위 3개 이상이면 보정). 감사 단위는 note가 대부분을 덮으면 추출 호출 한 번으로 끝나므로(후보가 없으면 통합 호출이 없다) 실제는 이 추정보다 작다. 이 문서의 숫자는 로컬 측정 전까지 추정이다.
- `--units N`, `--max-calls`, `--yes`, 저장된 `consent:<runner>`는 그대로다. 호출 상한은 단위당 6(최대치)이다. `scan --audit`는 모델 없이 감사 계획을 보여 준다(`runner_calls: 0`).
- 감사는 `--trigger hook`과 함께 쓸 수 없다(자동 갱신은 감사를 하지 않는다).

### 11.6 하지 않는 것, 로컬에서 확인할 것

- 하지 않음: 감사가 note의 상태나 내용을 고치는 일, note가 없는 세션의 감사, 감사 결과의 신뢰도 점수.
- 로컬 확인 필요: 실제 모델(Codex/Claude)로 한 세션을 감사해 (a) note를 되풀이하는 후보가 얼마나 나오는지(중복률), (b) 빠뜨린 편집을 실제로 찾는지, (c) 단위당 호출·토큰이 추정보다 작은지. F단계 평가와 한 번에 한다.

### 2026-10-08 — `analyze --audit` 구현 (Mock만)

§11대로 구현했다. 변경: `analysis.py`(`AnalysisConfig.audit`, `_plan_audit_units`, `audit_notes`/`uncited_edits`/`match_noted_duplicates`/`audit_guard`, `Harness.keep_events`, `build_task`의 감사 지시문, `plan_text`), `studio_graph.py`(계획의 `audit` 필드, 발행 때 `origin: "audit"`, 상태·`analysis_status`·`coverage` 처리), `cli.py`(`analyze`/`scan`의 `--audit`), `prompts/audit.md`, `demo.py`(FixtureRunner가 감사를 안다: note가 인용한 기록은 후보로 쓰지 않고, 추출이 중복으로 대응시킨 후보는 더하지 않고, 갱신하지 않는다). 테스트 `tests/test_audit.py` 17개, 전체 524 통과·1 건너뜀(macOS 전용).

설계와 달라진 점·알아둘 것:
- **통합 요청은 기본(`draft`)에서 패치 호출이다.** 그래프가 비어 있지 않으면 초안 발행이 없으므로, 후보가 있는 감사 단위는 추출 1 + 통합 1회다. 후보가 없으면(note가 모두 덮은 경우) 추출 1회로 끝난다. 테스트 conftest가 `integrate_output="full"`을 기본으로 바꾸므로 감사 테스트는 기본값(`draft`)을 명시하고, `full`에서도 가드가 같게 동작하는지 따로 본다.
- **중복 막기 4번(코드)** 는 종류가 같고 인용한 줄이 정확히 같을 때만 건드린다. 줄이 겹치기만 하면 건드리지 않는다.
- 감사 결과 상태는 `records_waiting`(남은 감사 기록)으로 정한다. 일반 분석의 남은 기록은 세지 않는다.
- `calibration`은 `audit_` 단위를 뺀다. 감사 비용 보정은 감사 실행이 쌓인 뒤에 따로 만든다(지금은 일반 단위 기준 추정).
- `status`/`find`의 "감사 안 한 note 세션 N개"는 같은 날 더했다(`freshness.unaudited`, 저장소만 읽는다). 아직 안 한 것: TUI에서 감사를 시작하는 키.

**로컬 확인 필요(이 환경에는 실제 기록·CLI가 없다).** §11.6의 (a) 중복률, (b) 빠뜨린 편집 발견, (c) 호출·토큰이 추정보다 작은지. 같은 세션으로 F단계 평가(분석기 결과와 note 비교)를 하면서 `contexttrail scan --audit`로 계획을 보고 `contexttrail analyze --audit --session <id> --units 3`로 한 번 돌려 본다.
