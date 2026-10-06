# 세 도구가 같이 쓰는 프로젝트 기억 — Claude Code · Codex · opencode

**상태: 2026-10-06 작성, 같은 날 A–F와 B2 모두 구현됨.** 남은 확인은 12절 끝에. 외부 도구의 형식은 공식 문서에서 확인한 것과 확인 필요로 표시한 것을 나눴다(9절). 진행 기록은 12절.

## 0. 한 문장

ContextTrail을 "지난 일을 보여 주는 도구"에서 **"도구가 아니라 프로젝트를 따라다니는 기억"**으로 바꾼다. Claude Code, Codex, opencode 중 무엇으로 프로젝트를 열든 같은 그래프를 읽고, 세 도구의 기록이 모두 그 그래프에 들어가며, 그래프가 얼마나 오래됐는지 에이전트가 안다.

왜: Claude Code의 memory, Codex의 memory는 각자 자기 도구 안에 갇혀 있다. 도구를 번갈아 쓰는 사람에게는 "다른 도구에서 무슨 결정을 했는지"를 증거와 함께 보여 주는 것이 없다. 이 빈자리가 ContextTrail의 포지셔닝이다.

## 1. 지금 있는 것과 없는 것

| | 상태 | 비고 |
| --- | --- | --- |
| 저장 위치가 도구와 무관 | 있음 | `<git-common-dir>/contexttrail/<scope-key>/`. 같은 폴더면 같은 그래프 |
| 읽기 경로가 도구와 무관 | 있음 | `find`/`show` CLI(`agent_view.py`), MCP 아님 |
| 분석 중에도 읽기 가능 | 있음 | SQLite WAL + 분석 lock |
| Claude Code 기록 입력 | 있음 | `sources/local.py` |
| Codex 기록 입력 | 있음 | `sources/local.py` |
| opencode 기록 입력 | 있음(2026-10-06) | `sources/opencode.py`. 설계와 확인한 사실은 `OPENCODE_SOURCE.md` |
| Claude Code·Codex 스킬 설치 | 있음 | `agent_commands.py`: `~/.claude/skills`, `~/.agents/skills`, `~/.codex/prompts` |
| opencode 스킬·명령 설치 | 있음(2026-10-06) | `~/.config/opencode/commands/contexttrail-*.md`; 스킬은 `~/.claude/skills`를 통해 보인다(5절) |
| 그래프가 오래됐는지 아는 방법 | 있음(2026-10-06) | `status`, `find` 첫 줄, TUI·브라우저 상단: 마지막 scan 기준 미분석 기록 수 + scan 이후 세션·기록 수(바뀐 파일만 파싱). `freshness.py` |
| 세션이 끝나면 저절로 갱신 | 있음, 프로젝트별 opt-in(2026-10-06) | `auto_update.py`: `auto-update --enable`, `install-hooks` |
| 스킬의 Runner | 저장된 것, 없으면 묻기(2026-10-06) | `scan --json`의 `runner`(6절) |
| 어떤 모델·effort가 그 기록을 만들었는지 | 없음 | `OPENCODE_SOURCE.md` 2절 |

## 2. 지킬 원칙

1. **쓰는 손은 하나.** 그래프에 쓰는 경로는 분석(모델이 트랜스크립트를 읽고 인용으로 뒷받침한 결과)뿐이다. 에이전트가 그래프에 직접 메모를 쓰는 두 번째 경로는 만들지 않는다. 에이전트가 말한 것은 어차피 트랜스크립트에 남아 다음 분석에 들어간다. 이 원칙이 "모든 항목에 증거가 있다"를 지킨다.
2. **분석은 명시적 요청에만.** 훅으로 돌리는 자동 갱신은 사용자가 프로젝트마다 켜는 선택 사항이고, 한 번 켠 것이 곧 명시적 요청이다. 켜지 않은 프로젝트에서 훅은 아무것도 하지 않는다.
3. **입력에 대해 읽기 전용.** opencode DB도 `mode=ro`, 허용 목록의 테이블만. 자격 테이블과 `auth.json`은 열지 않는다.
4. **도구 중립은 CLI로.** 세 도구 모두 셸을 갖고 있으니 CLI면 충분하다. MCP 서버는 만들지 않는다.
5. **오래된 것은 오래됐다고 말한다.** 에이전트가 옛 그래프를 새것처럼 쓰지 않도록, 읽기 경로마다 "마지막 분석 뒤에 쌓인 기록"을 적는다. 추정하지 않고 센 값만 적는다.
6. **호스트 도구를 막지 않는다.** 훅 명령은 항상 즉시 종료 코드 0으로 끝나고, 실제 분석은 분리된 백그라운드 프로세스가 한다. 실패는 상태 폴더의 로그에만 남긴다.

## 3. 단계 A — opencode 입력

설계는 `docs/plans/OPENCODE_SOURCE.md`를 따른다(파서 `sources/opencode.py`, 범위 귀속은 `session.directory`로만, `parent_id`로 하위 에이전트, `patch` 파트는 `hint=edit`, 허용 목록 테이블만 `SELECT`, 합성 DB로만 테스트). 거기에 더해 이번에 확인·결정한 것:

- **데이터 디렉터리:** `$XDG_DATA_HOME/opencode` → `~/.local/share/opencode`(opencode 소스로 확인; `OPENCODE_DATA_DIR`는 opencode에 없다). DB 파일은 채널에 따라 `opencode.db` 또는 `opencode-<channel>.db`, `OPENCODE_DB`로 바꿀 수 있어 폴더의 `opencode*.db` 전부를 읽는다. ContextTrail 쪽 옵션은 `--opencode-home` / 저장 옵션 `opencode_home`.
- **`content_hash` 불변:** 모델·effort(`authoring`) 필드는 해시에 넣지 않는다. 이미 통합된 단위가 다시 보내지면 안 된다. 기존 두 파서에 `authoring`을 먼저 넣는 순서는 `OPENCODE_SOURCE.md` 5절대로 하되, 이 단계의 목표는 **opencode 기록이 그래프에 들어가는 것**이므로 `authoring` 표시(화면·`show`)는 뒤로 미뤄도 된다.
- **자기 실행 제외:** opencode 안에서 ContextTrail을 돌린 세션은 Codex·Claude와 같은 규칙으로 뺀다(`metadata.source`, 작업 폴더 이름 `contexttrail-run-`).
- **WAL 읽기:** opencode가 켜져 있는 동안 `mode=ro`로 열어 읽을 수 있고 파일이 바뀌지 않는다(확인). 복사 방안은 필요 없었다.
- **`scan` 출력**에 소스별 개수(codex/claude/opencode)를 넣어, 세 도구가 다 잡히는지 한눈에 보이게 한다.

완료 기준: 합성 opencode DB 하나로 `scan`이 세션·하위 에이전트·압축 경계를 Codex·Claude와 같은 형태로 내고, 실제 DB로 `scan`을 돌렸을 때(모델 호출 없음) 범위 밖 세션이 0건 섞이며, 테스트가 DB 파일을 바꾸지 않는다(mtime·해시 동일). **충족(2026-10-06):** 이 저장소에서 11개 세션 중 4개 선택·7개 범위 밖·0개 미귀속, 레코드 1,936개, 전부 cwd가 이 저장소, 읽기 뒤 DB·WAL·SHM 변화 없음, 파싱 0.4초. 테스트 14개.

## 4. 단계 B — 신선도

### B1. 얼마나 오래됐는지 보여 주기 (모델 호출 없음)

**구현됨(2026-10-06, `freshness.py`).** 전체 파싱이 47초라 아래의 "비용을 먼저 잰다"의 두 번째 길을 택했다: `Engine.scan`이 파일 색인(`source_index`: 파일별 읽은 길이·mtime, opencode는 범위 안 세션의 `time_updated`)을 저장하고, `status`·`find`는 새로 생기거나 바뀐 파일만 파싱해 저장소에 없는 레코드를 센다. 값은 둘이다: 마지막 scan 기준 미분석 기록 수(저장소에서), scan 이후 세션·기록 수(바뀐 파일에서). `find`는 바뀐 파일이 128 MB를 넘으면 파싱하지 않고 파일 수만 말한다. 이 저장소에서 `status` 0.9초(그중 확인 0.28초). 대기 작업 단위 수는 계획을 세워야 나오므로(전체 파싱 필요) 넣지 않았다. 하위 에이전트 파일만 바뀐 경우는 부모 연결 검증 없이는 세지 않는다(다음 scan에서 잡힌다).

- 새 명령 `contexttrail status [folder]`: 그래프 버전·분석 기준 시각과 함께 **마지막 분석 뒤에 쌓인 것**을 센다 — 소스별 세션 수, 기록 수, 가장 최근 기록 시각, 대기 중인 작업 단위 수(`Engine.preview_plan`의 값). `--json` 지원.
- `find`의 첫 줄(`ContextTrail · graph vN (status) · analyzed as of …`)에 같은 값을 짧게 붙인다: `· 3 sessions not analyzed (newest 2h ago)`. 0이면 `· up to date`.
- `contexttrail-context` 스킬 문구에 규칙을 추가한다: 첫 줄이 미분석 기록을 말하면 답 앞에 그 사실을 한 줄로 말하고, 질문이 그 기간의 일이면 그래프로 답하지 말고 갱신이 필요하다고 말한다.
- **비용을 먼저 잰다.** 지금 이 값을 얻는 유일한 길은 `scan`과 같은 전체 파싱이다. 측정 프로젝트(수천 세션) 기준으로 2초를 넘으면 `find`에는 넣지 않고 `status`에만 두거나, 상태 폴더에 파일 단위 색인(경로·크기·mtime·마지막으로 본 기록 수)을 두어 바뀐 파일만 다시 읽는다. 색인은 `ingest`와 같은 트랜잭션에서 갱신한다.
- 브라우저 뷰와 TUI 상단에도 같은 한 줄을 보여 준다(`R`로 갱신하라는 안내와 함께).

### B2. 세션이 끝나면 저절로 갱신 (선택, 프로젝트별 opt-in)

**구현됨(2026-10-06, `auto_update.py`, 테스트 9개).** 아래 구조대로다. 다른 점: 대기 판단은 작업 단위 수가 아니라 B1의 값(마지막 scan 기준 미분석 기록 + scan 이후 기록, 예산 초과면 바뀐 파일 수)이고, 상태 폴더가 없는 프로젝트에서는 아무것도 만들지 않는다. Codex는 `notify` 대신 lifecycle hooks(`~/.codex/hooks.json`, `Stop`, stdin JSON에 `cwd`·`session_id`)를 쓴다. `ops`에서 자동 실행만 고르는 필터는 넣지 않았고, 실행 원장(`analysis_runs.manifest.trigger`)에만 남는다.

**구조:** 훅은 모두 하나의 숨은 명령 `contexttrail auto-update`를 부른다. 이 명령은

1. stdin의 JSON(또는 인자)에서 작업 폴더를 읽고, 존재하는 디렉터리인지 확인한 뒤 `Scope.resolve`로 상태 폴더를 찾는다. 셸을 거치지 않는다(인자 배열).
2. 그 프로젝트에 `auto_update` 설정이 저장돼 있지 않으면 **아무것도 하지 않고 0으로 끝난다.** 켜는 명령은 사용자가 프로젝트 안에서 직접 치는 `contexttrail auto-update --enable --runner <codex|claude> --units N [--cooldown 15m] [--max-runs-per-day K]`이고, 이것이 그 프로젝트의 동의(`consent:<runner>`)를 함께 기록한다. `--disable`로 끈다.
3. 다음 중 하나면 조용히 0으로 끝난다: 분석 lock이 잡혀 있음, 대기 단위 0개(B1의 값), 마지막 자동 실행 뒤 cooldown이 안 지남, 하루 상한 도달, 호스트 도구가 ContextTrail 자신의 실행(`contexttrail-run-`)임.
4. 아니면 `analyze <folder> --runner <saved> --yes --no-tui --brief --units N`을 **분리된 백그라운드 프로세스**(새 세션, stdin 닫음, stdout·stderr는 상태 폴더의 `auto-update.log`, 0600)로 띄우고 즉시 0으로 끝난다.
5. 실행 결과는 기존 `runs`·`llm_calls` 원장에 `trigger: hook` 표식과 함께 남는다. `ops`에서 자동 실행만 골라 볼 수 있어야 한다.

**설치:** `contexttrail install-hooks [--claude] [--codex] [--opencode]`. `install-commands`와 분리한다(명령 설치는 비용이 없고, 훅은 돈이 드는 분석을 부르기 때문). 사용자 파일은 `--force` 없이 덮어쓰지 않는다. 관리 표식(`_MANAGED_MARKER`)을 같은 방식으로 쓴다.

| 도구 | 어디에 | 어떤 사건 | 확인 상태 |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/settings.json`의 `hooks` | `Stop`(턴마다; `async: true`로 비차단) — `SessionEnd`는 모든 훅이 1.5초를 나눠 쓰므로 분석을 띄우기엔 부적합하지만 분리 실행이면 가능 | 공식 문서 확인. stdin JSON에 `session_id`, `cwd`, `transcript_path`, `hook_event_name` |
| Codex | `~/.codex/hooks.json`의 `Stop` (config.toml `hooks` 표도 같은 형식) | `Stop` | 공식 config 레퍼런스가 hooks 표와 사건 목록(SessionStart·SessionEnd·Stop·…)을 확인. stdin JSON의 `session_id`·`cwd`·`transcript_path`·`stop_hook_active`와 파일 위치·동기 실행·`async` 미지원은 서드파티 가이드(codex.danielvaughan.com, 2026-04) 기준. `notify`는 쓰지 않는다 |
| opencode | `~/.config/opencode/plugins/contexttrail.ts` | `event` 훅의 `session.idle` | 공식 문서·`packages/plugin/src/index.ts`로 확인: `PluginInput`에 `directory`·`worktree`·`$`가 있다. 플러그인은 이벤트 payload를 쓰지 않고 자기 `directory`로 `auto-update --folder`를 부른다. 분리 실행은 ContextTrail 쪽(`start_new_session`)이 맡으므로 `$`의 동작과 무관하다 |

`Stop`을 고른 이유: 세션을 몇 시간씩 열어 두는 사람이 많아 `SessionEnd`만으로는 늦고, cooldown과 "대기 단위 0이면 종료"가 있으면 턴마다 불려도 비용이 늘지 않는다.

**안전 규칙:** 훅 명령은 `cwd`를 신뢰하지 않는다(존재 확인, 디렉터리 확인, 상태 폴더는 `Scope`가 정한 곳만). 로그에 트랜스크립트 내용을 쓰지 않는다. 백그라운드 분석은 기존 sandbox 경로 그대로다. 호스트가 Codex 샌드박스 안이면 `sandbox-exec`가 안 될 수 있다(`NEXT_STEPS.md` 3절) — 이 경우도 로그에만 남기고 0으로 끝난다.

**하지 않는 것:** 훅이 사용자에게 묻거나, 호스트 도구의 턴을 막거나(`decision: block`), 호스트 대화에 문맥을 주입하는 것. `Stop`의 `additionalContext`로 "그래프 갱신됨"을 알리는 것은 나중에 검토한다.

## 5. 단계 C — opencode에서 쓰기

- **스킬은 이미 보인다.** opencode는 `~/.claude/skills/*/SKILL.md`와 `~/.agents/skills/*/SKILL.md`도 읽는다(공식 문서). 지금 `install-commands`가 설치한 두 스킬이 opencode에도 그대로 나타난다. 문구에서 `/contexttrail-update`(Claude) · `$contexttrail-update`(Codex) 분기에 opencode용 `/contexttrail-update`를 더하고, `host="opencode"`를 추가한다.
- **명령:** `~/.config/opencode/commands/contexttrail-update.md`, `contexttrail-context.md`(frontmatter `description`, 본문에 `$ARGUMENTS`). 사용자가 `/contexttrail-update 3`처럼 명시적으로 부르는 길이다.
- **명시적 호출만 허용하는 장치가 없다.** opencode에는 Codex의 `allow_implicit_invocation: false`나 Claude의 `disable-model-invocation`에 해당하는 것이 문서에 없다. opencode가 `~/.claude/skills/contexttrail-update`를 스스로 고를 수 있다는 뜻이다. **구현(2026-10-06):** opencode 문서상 `permission`은 도구별로 패턴 키의 중첩 객체를 받고 `skill` 항목은 스킬 이름과 맞춘다(`deny`는 에이전트에게 숨김, `ask`는 승인 요구). 그래서 `install-commands`가 `{"permission": {"skill": {"contexttrail-update": "ask"}}}`를 `~/.config/opencode/opencode.json`에 넣으라고 안내만 한다(파일은 고치지 않음). 이 설정이 실제로 그렇게 동작하는지는 opencode를 띄워 확인하지 않았다(F에서). 원래 (3)으로 적었던 "`--yes`가 있어도 동의 키가 없으면 묻는다"는 **틀렸다**: `--yes`가 곧 동의 기록이다. 그러므로 opencode에서의 보호는 `permission` 설정과 스킬 본문의 첫 문장뿐이다.
- ~~테스트: `install_agent_commands`의 대상 경로에 opencode 둘을 더하고, 사용자 파일 보호(`--force`)가 같은지 확인.~~ 끝(`tests/test_agent_commands.py`, 대상 9개).

## 6. 단계 D — Runner를 호스트에 묶지 않기

- 스킬의 update 본문은 "Runner는 Codex뿐"이라고 말한다. 2026-10-06의 자기 분석은 Claude Runner로 했고 잘 됐다. 사용자는 셋 중 어느 구독을 쓸지 고를 수 있어야 한다.
- 저장: 프로젝트 옵션의 `runner`는 이미 `analyze --runner X`를 줄 때 저장되고 있었다(`cli._options`; 성공 여부와 무관). `scan --json`에 `runner`(저장된 값 또는 null)를 넣었다(2026-10-06).
- 스킬 문구(2026-10-06): "저장된 runner가 있으면 그것, 없으면 사용자에게 Codex와 Claude 중 무엇으로 할지 묻는다. opencode는 Runner가 아니다." `analyze . --runner <runner> --yes …`.
- 호스트 안에서 Runner CLI를 못 돌리는 경우(Codex 샌드박스 안에서 `sandbox-exec` 등)의 문구는 그대로 둔다.

## 7. 단계 E — 포지셔닝

- README 첫 문단과 `docs/PRD.md` 머리말: "Claude Code·Codex·opencode가 같이 쓰는 프로젝트 기억. 각 도구의 기록을 하나의 증거 연결 그래프로 모으고, 어느 도구에서든 `find`/`show`로 읽는다." 영어 README도 같은 문장.
- `_DESCRIPTIONS["context"]`(에이전트가 스킬을 고를 때 보는 설명)에 "다른 도구에서 한 일도 포함"을 넣는다. 설명 문구는 모델이 읽는 텍스트이므로 변경 뒤 스킬 선택이 달라지는지 각 도구에서 한 번씩 본다(평가는 아님).
- `docs/DECISIONS.md`에 2절의 원칙(쓰는 손 하나, 훅은 opt-in, MCP 아님)을 결정으로 적는다.
- `CLAUDE.md`: 세 번째 소스, `status`, `auto-update`, `install-hooks`, 저장 옵션 `runner`·`auto_update`·`opencode_home`.
- 노출 순서: 영어권 노출(Show HN)은 A와 B1이 들어간 뒤로 미룬다. 첫 문장이 바뀌기 때문이다.
- **구현(2026-10-06):** README 첫 문단과 설치 절, `_DESCRIPTIONS["context"]`, `docs/DECISIONS.md`, `docs/PRD.md` 머리말, CLAUDE.md. 스킬 설명 문구가 바뀐 뒤 각 도구의 스킬 선택이 달라지는지는 아직 보지 않았다.

## 8. 단계 F — 검증

- **합성 fixture:** ~~세 도구가 섞인 프로젝트를 새로 만든다~~ → 기존 `demo` fixture의 레코드를 Codex(0–3)·opencode(4)·Claude Code(5–6)로 나눴다(`evaluation.DEMO_PROVIDERS`, `examples/eval-demo.json`). 기대 관계 `event3 → event4 motivates`가 Codex→opencode를 가로지른다. Mock Runner로 CI에서 돈다(2026-10-06).
- **실제 평가(소유자 동의 뒤):** 같은 fixture를 Codex와 Claude Runner로 한 번씩 — 아직 안 했다. 레코드 내용은 그대로라 v4 수치와의 비교는 provider 표시 변화만 본다.
- **실제 프로젝트 확인(2026-10-06):** 이 저장소의 opencode 세션 `ses_f1a2f7be…`(탐색 하위 에이전트, 4 단위)를 `analyze --session … --runner claude --yes --units 4`로 분석: 호출 11(추출 5·통합 6, 검증 실패 1회 복구), 모델 시간 93초, 전체 2.5분, 그래프 v7. 이 Claude Code 세션에서 `find`로 opencode 사건 4개를 읽었고(근거 표시 `opencode · 도구 결과 · 세션 ses_f1a2`), 첫 줄이 미분석 기록 수를 말했다. README "실제 프로젝트" 절에 적음.
- **읽기 전용 테스트:** 합성 WAL DB(테스트)와 실제 DB(수동)에서 `scan` 전후 mtime·해시 동일. 끝.
- **훅 테스트(끝, `tests/test_auto_update.py`):** (a) 설정 없는 프로젝트·상태 폴더 없는 프로젝트는 아무것도 안 함, (b) lock 중, (c) 대기 없음, (d) cooldown, (e) 하루 상한, (f) 띄운 프로세스가 자기 세션을 갖고 훅 명령이 끝난 뒤에도 산다, (g) 잘못된 `cwd`·JSON 아님·상대 경로에 0으로 끝남, 훅 파일 병합·보존·`--force`, 원장의 `trigger`.

## 9. 구현 전에 확인할 것

1. ~~opencode 데이터 디렉터리 환경 변수.~~ 확인: `XDG_DATA_HOME`만, `OPENCODE_DATA_DIR`는 없음. DB 이름은 채널별, `OPENCODE_DB`로 변경 가능(`OPENCODE_SOURCE.md` 1절).
2. ~~opencode `permission` 설정으로 스킬 하나를 `deny`/`ask`로 둘 수 있는지.~~ 문서상 가능(`permission.skill`의 이름 패턴). 실제 동작 확인은 F로.
3. ~~opencode 플러그인 컨텍스트.~~ `directory`·`worktree` 있음(소스 확인). `session.idle`이 하위 에이전트 세션에도 오는지는 미확인이나 플러그인이 `directory`만 쓰므로 영향 없음(하위 에이전트 idle에도 한 번 더 불릴 뿐이고 cooldown이 막는다).
4. ~~Codex `notify`.~~ 쓰지 않는다. Codex lifecycle hooks 존재는 공식 레퍼런스로, payload·파일 위치는 서드파티 가이드로 확인. **hooks를 켜는 feature flag가 필요한지 미확인.**
5. ~~Claude `async: true`.~~ 공식 문서: 비차단, 훅 프로세스는 분리되나 자식은 부모 종료 시 살아남지 않는다고 적혀 있다 → ContextTrail이 `start_new_session=True`로 손자 프로세스를 띄워 자기 세션을 갖게 했다(테스트 f).
6. ~~`OPENCODE_SOURCE.md` 4절의 7가지.~~ 확인, 그 문서 4절에 적음(Codex·Claude 레코드의 모델·effort만 남음).
7. ~~측정 프로젝트에서 B1의 전체 파싱 시간.~~ 측정(2026-10-06, 이 저장소, 세션 파일 수천 개): Codex+Claude JSONL 파싱 47.6초, opencode 0.4초, `scan` 전체 74초. **2초를 한참 넘으므로 B1은 전체 파싱을 쓰지 않는다** — 마지막 scan의 파일 색인(경로·크기·mtime·선택 레코드 수)과 비교해 새로 생기거나 바뀐 파일만 파싱하고, opencode는 범위 안 세션의 `time_updated`만 질의한다.

## 10. 순서와 양

| 순서 | 단계 | 양(대략) | 의존 |
| --- | --- | --- | --- |
| 1 | A opencode 입력 | 2–3일. 가장 큼 | 9절 1·6 |
| 2 | B1 신선도 표시 | 1일 | 9절 7 |
| 3 | C opencode 설치 | 반나절 | 9절 2 |
| 4 | D Runner 저장 | 반나절 | — |
| 5 | E 포지셔닝 | 반나절 | A, B1 |
| 6 | F 검증 | 1일 + 실제 평가 | A–D |
| 7 | B2 자동 갱신 | 1–2일 | 9절 3·4·5, B1 |

B2를 마지막에 둔 이유: 돈이 드는 분석을 사람이 모르게 띄우는 기능이라, 나머지가 안정된 뒤에 넣는 편이 맞다. B1만으로도 "오래된 문맥을 새것처럼 쓰는" 문제는 막힌다.

## 11. 하지 않는 것

- 에이전트가 그래프에 직접 쓰는 API나 "메모 추가" 명령.
- MCP 서버.
- 도구 사이의 실시간 동기화(파일 감시, 데몬). 상태는 SQLite 하나이고, 읽는 쪽이 매번 열면 된다.
- 훅이 사용자 몰래 다른 프로젝트까지 분석하는 것. 설정은 프로젝트마다.
- opencode를 분석 Runner로 쓰는 것(`OPENCODE_SOURCE.md`의 결정).

## 12. 진행 기록

- 2026-10-06 F·B2 완료: 세 도구 혼합 fixture, 실제 opencode 세션 라이브 분석(위 8절), `auto_update.py`(`auto-update`, `install-hooks`), `--trigger hook`, 테스트 474개.
- 2026-10-06 실제 확인(소유자: "돈이 들어도 괜찮으니 직접 진행"):
  - opencode `permission.skill`: `opencode.jsonc`에 `{"permission": {"skill": {"contexttrail-update": "ask"}}}`를 넣고 `opencode run`으로 스킬을 부르게 하니 "permission requested: skill (contexttrail-update); auto-rejecting"으로 막혔다. 동작 확인.
  - Codex hooks: `codex features list`에서 `hooks stable true`(기본 켜짐). 파일 형식은 공식 문서대로 최상위 `{"hooks": {"Stop": [...]}}`여야 해서 설치기를 고쳤다(처음엔 `{"Stop": …}`로 써서 무시됐다). 새 hooks.json은 TUI의 "Hooks need review" 프롬프트에서 신뢰해야 돈다. 신뢰 뒤 한 턴에 `hook: cooldown`이 로그에 남았다.
  - Claude Code: 대화형 세션에서 `Stop` 훅이 돌아 `hook: cooldown`을 남겼다. `claude -p`에서는 디버그 로그에 "Registering async hook … (Stop)" 뒤 "Hook Stop cancelled"가 찍힌다(비대화형 종료 시 비동기 훅 취소, 문서와 일치).
  - opencode 플러그인: `opencode run` 한 번에 `session.idle`로 `auto-update`가 불려 1단위 분석을 띄웠고(3 호출, 그래프 v8) 끝까지 돌았다.
  - 이 저장소에 자동 갱신을 켜 두었다(runner claude, 1단위, cooldown 15분, 하루 8회). 끄려면 `contexttrail auto-update --disable`.
  - 실제 평가(`eval --fixture demo`): Claude Runner 7호출·3단위·사건 7개, Codex Runner 7호출·3단위·사건 7개, 둘 다 `complete`, noop 확인 통과. 기대 검사는 Claude 1/8, Codex 0/8 — 기대가 Mock용 제목 문자열(`title_contains`)이라 실제 모델의 제목과 맞지 않는 것이지 파이프라인 실패가 아니다(상태·근거 검사는 통과). 이 fixture의 역할은 Mock으로 도는 CI 검사라 기대를 다시 쓰지 않기로 했다(소유자, 2026-10-06). 실제 정확도는 실제 로그 fixture(repairfix·installer)로 잰다.
- **남은 일:** 영어권 노출(Show HN). 도구 간 관계의 실제 정확도가 궁금해지면 이 저장소의 섞인 세션(opencode·Claude Code·Codex)으로 실제 로그 fixture를 하나 만들 수 있다.
- 2026-10-06 C·D·E 완료: opencode 명령 파일 둘, `OPENCODE_PERMISSION_HINT`, `scan --json`의 `runner`, 스킬 본문의 runner 선택 단계와 도구 간 문구, README·DECISIONS·PRD·CLAUDE.md.
- 2026-10-06 B1 완료: `freshness.py`(`build_index`·`check`·`summary`·`status_lines`), `contexttrail status [--json]`, `find` 첫 줄과 `--json`의 `freshness`, TUI 1행과 브라우저 상태줄(그래프 버전당 한 번 계산), context 스킬의 규칙 한 줄. 테스트 465개.
- 2026-10-06 A 완료: `sources/opencode.py`, `collect_logs(opencode_home=)`, `AnalysisConfig.opencode_home`, `--opencode-home`, `scan`의 소스별 개수, 도구 이름 표(`edit`/`write`/`read`/`bash`/`task`), `render.PROVIDER`, 평가 fixture의 provider 허용. 테스트 459개 통과. 9절 1·6·7 확인.

### 2026-10-07 점검

소유자가 "더 개선할 것이 있나"라고 물어 실제 저장소의 상태로 다시 봤다.

- **훅 자동 갱신은 오래된 것부터 처리한다.** 이 저장소에는 미분석 559단위가 있어 하루 8회·1단위씩으로는 최근 세션이 두 달 뒤에나 차례가 온다. "방금 끝난 세션만 분석"하는 방식은 소유자가 "오래된 작업을 기억 못해 분류를 잘못할 수 있다"고 짚었고 맞는 말이다: 통합 단계는 현재 그래프와 비교해 중복·수정·관계를 정하므로, 앞선 맥락이 없으면 같은 목표를 새 사건으로 세우거나 재시도를 첫 시도로 적을 수 있다. 시간순은 유지한다. 밀린 기록은 한 번 수동으로 따라잡는 것이 답이고 비용 결정은 소유자 몫으로 남긴다.
- **구현한 것(0.1.0a6):** 훅이 띄우는 자식을 `auto-update --run`으로 바꿔 종료 코드를 기록하고 연속 3회 실패 시 멈춤(`--status`에 표시, `--enable`로 재시작); `find`/`status`의 바뀐 파일 파싱 결과를 scan·저장 기록 기준으로 캐시(2.0 s → 0.3 s); `scan --json` 수용(스킬 본문이 그렇게 부르는데 a5는 거부했다).
- **a6 릴리스:** 버전·CHANGELOG 올림. PyPI 배포는 `v0.1.0a6` 태그 push로 publish 워크플로가 하므로 태그는 소유자가 올린다.
- README는 이미 영어다(계획서 초기의 "영어 README 필요"는 틀린 전제였다).
- **훅 실패 멈춤을 실제로 확인하다가 진짜 버그를 찾았다.** 새 자식 경로로 띄운 분석이 "분석 입력 예산 초과"로 실패했고(기록됨: `마지막 실행 결과: 실패 (exit 1)`), 저장된 추출로 통합 요청을 오프라인에서 다시 만들어 보니 259k자(예산 240k). 코드가 만드는 초안 delta가 후보마다 근거를 세 번(항목·resolution·attribution) 실었고, 검토 요청의 제안 delta도 같았다. `schema.delta_for_model`로 후보 자신의 근거와 똑같은 사본은 빼고 보여 주도록 바꿨다(코드는 전체 delta를 유지, 출력 스키마는 여전히 근거 필수). 같은 단위: 통합 196k, 검토 228k로 완료(v13). 검토 요청이 예산의 95%라 여유가 적다. 후보가 더 많은 단위는 다시 넘을 수 있으므로, 다음 개선 후보는 요청이 예산을 넘을 때 `context_only`를 줄이거나 단위를 다시 자르는 예산 인식 처리다.

### 2026-10-07 모델 입력 줄이기

소유자: "LLM 자원을 아끼기 위해서도 중요하니 과하게 많이 요청하는 부분을 면밀히 살펴보자." 이 저장소의 호출 기록 43건과 저장된 추출로 재구성한 요청을 재서, 아낄 양이 큰 순서로 여섯 가지를 고쳤다(커밋 7619ba2, 6a8214f, d3af0f9).

1. 검토 요청에서 기록 문맥(`context_only`)과 manifest 제외(patch 모드만). 검토는 14단위 중 13번 돌았고 3번만 항목을 바꿨다.
2. 통합 요청에서 후보 중복 제거: 초안의 변경 없는 항목은 id로만, 후보가 이미 든 인용은 `quote_in_candidates`로 표시.
3. 인용은 저장 시 해당 줄 전체로 정규화되므로(focus 보존) 모델에게는 인용된 부분만 300자 안에서 보여 줌(`shown_quote`, `candidates_for_model`). 저장된 인용 159개 중 30개가 1,000자 초과, 최대 14,474자였다.
4. 보여 주는 줄은 1,500자에서 자름(`shown_line`; 한 줄짜리 JSON 결과가 head/tail을 그대로 통과하던 문제).
5. 근거 메타데이터의 빈 값·기본값 제거. 6. manifest 사건 색인 100→60.

| 요청 | 이전 중앙값(기록 43건) | 이후 실측(v17 단위) |
|---|---|---|
| 추출 | 191k | 165k |
| 통합 | 168k | 101k |
| 검토 | 159k | 48k |

예산을 넘겼던 12후보 단위는 오프라인 재구성으로 추출 153k→110k, 통합 259k→73k, 검토 228k→59k. 모두 모델이 보는 입력 변경이라 Mock 평가(8/8)와 실제 단위 3개로 확인했다. 다음 후보: 검토 자체를 건너뛸 조건(연결할 변경 사건이 없는 결과), 추출 요청의 `context_only`(한 단위에서 38k), 요청이 예산을 넘을 때의 예산 인식 처리.
