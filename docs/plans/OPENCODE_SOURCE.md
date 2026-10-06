# opencode 기록 지원과 모델·effort 기록

**상태: 수집(3절) 구현됨, 2026-10-06, `sources/opencode.py` + `tests/test_opencode_source.py`. 모델·effort 기록(2절)은 미구현.** 확인한 것은 4절 아래에 적었다.

ContextTrail은 지금 Codex와 Claude Code 기록을 읽는다. 여기에 opencode를 세 번째 수집 대상으로 넣는다. 결정된 것은 두 가지다.

1. opencode는 **수집 대상**이다. 분석 Runner는 계속 Codex이고, opencode를 Runner로 쓰지 않는다(Claude와 같은 취급).
2. 세 도구 모두에서 **어떤 모델이, 어떤 effort로** 그 답을 냈는지가 중요하다. 이를 기록에 남기고 사건 근거에서 보이게 한다.

---

## 1. opencode 저장소 구조 (이 머신에서 확인, 내용은 읽지 않음)

경로: `$XDG_DATA_HOME/opencode`, 없으면 `~/.local/share/opencode` (opencode 소스 `packages/core/src/global.ts`가 `xdg-basedir`로 정한다. `OPENCODE_DATA_DIR`는 opencode에 없고 서드파티 도구의 변수였다). DB 파일 이름은 설치 채널에 따라 `opencode.db`(latest·beta·prod) 또는 `opencode-<channel>.db`이고 `OPENCODE_DB`로 바꿀 수 있다(`packages/core/src/database/database.ts`). ContextTrail은 `--opencode-home`(저장 옵션 `opencode_home`)이 가리키는 폴더의 `opencode*.db` 전부와 `OPENCODE_DB`를 읽는다. 이 계획을 포함하는 상위 계획: `docs/plans/SHARED_CONTEXT_STORE.md`.

| 항목 | 내용 |
| --- | --- |
| `opencode.db` (+ `-wal`, `-shm`) | SQLite(WAL). 대화 기록의 본체 |
| `tool-output/`, `snapshot/`, `repos/`, `log/` | 큰 도구 출력·스냅샷·로그로 보이나 역할은 미확인 |
| `auth.json` | 인증 정보. **절대 읽지 않는다** |

DB 테이블: `session`, `message`, `part`, `project`, `project_directory`, `todo`, `permission`, `event`, `workspace`, `session_message`, `session_input`, `session_context_epoch`, `session_share` 외 계정·자격 관련 테이블(`account`, `account_state`, `control_account`, `credential`).

**읽을 테이블:** `session`, `message`, `part`, `project`, `project_directory`. 계정·자격 테이블과 `auth.json`은 열지도 않는다.

### 1.1 `session`

`id`, `project_id`, `parent_id`(하위 에이전트 세션), `directory`(작업 디렉터리), `path`, `title`, `agent`, `model`, `cost`, `tokens_input/output/reasoning/cache_read/cache_write`, `time_created`, `time_updated`, `time_compacting`(압축 시각), `time_archived`.

### 1.2 `message`

행마다 `id`, `session_id`, `time_created`, `time_updated`, `data`(JSON). `data`의 키(값은 보지 않음):

`role`, `time`, `agent`, `model`, `summary`, `parentID`, `mode`, `variant`, `path`, `cost`, `tokens`, `modelID`, `providerID`, `finish`, `error`

- **사용자 메시지**의 `model`은 객체 `{providerID, modelID, variant}`다.
- **어시스턴트 메시지**는 `providerID`, `modelID`, `variant`를 최상위에 둔다.
- 이 머신의 1개 세션에서 `variant`는 `max`였고, opencode 화면의 effort 표시(`max`)와 일치했다. `variant`가 곧 effort(추론 수준)라는 것은 이 한 건으로만 확인했다. 공식 문서 확인 필요.

### 1.3 `part`

행마다 `id`, `message_id`, `session_id`, `data`(JSON). `data.type` 종류와 이 머신의 개수:

| type | 개수 | 예상 대응 |
| --- | ---: | --- |
| `tool` | 813 | `tool_call` + `tool_result` |
| `step-start` / `step-finish` | 735 / 719 | 단계 경계. 토큰·비용·종료 이유를 담을 가능성 |
| `reasoning` | 317 | 추론 텍스트(수집 여부 정책 필요) |
| `text` | 264 | 사람·어시스턴트 텍스트 |
| `patch` | 143 | 파일 변경. `hint=edit`에 해당 |

`tool` 파트의 내부 구조(입력, 출력, 상태, 호출 ID)와 `patch`의 형식은 아직 보지 않았다.

---

## 2. 모델·effort를 기록하는 방법

### 2.1 지금 코드가 기록하는 것

- `model.SourceRecord`에는 모델·effort 필드가 없다.
- Codex 파서는 `turn_context`에서 `cwd`와 Git 정보만 읽는다(`sources/local.py`의 `turn_context` 처리). 이 레코드에 모델·추론 수준이 들어 있는지는 실제 로그 형식으로 확인해야 한다.
- Claude 파서는 하위 에이전트 매니페스트에서만 `model`을 읽는다. 일반 어시스턴트 레코드의 모델은 버려진다(확인 필요).
- 분석 호출(`llm_calls` 원장)은 이미 모델과 effort를 호출마다 기록한다. **이건 분석 Runner의 것**이고, 여기서 말하는 것은 **기록된 대화를 만든 모델**이다. 둘을 섞지 않는다.

### 2.2 제안

- 레코드마다 `authoring: {provider, model, effort}`를 붙인다. 사람 메시지에는 그때 선택된 모델 설정을 붙인다(opencode는 `message.data.model`).
- 값은 **원문에서 코드가 읽은 그대로** 저장한다. 모델이 쓰지 않는다. 사건의 출처 필드(`recorded_at`, 세션 ID 등)를 인용된 소스에서 읽는 방식과 같다.
- 사건 상세에는 근거 레코드들의 모델·effort 집합을 보여 준다. 한 사건이 여러 모델에 걸치면 나열한다.
- 값이 없으면 `null`이다. 추측해서 채우지 않는다.
- `find`/`show`(에이전트 CLI)의 `--json`에도 넣는다.

### 2.3 반드시 지킬 것: `content_hash`를 바꾸지 않는다

- 새 필드는 **내용 해시에 들어가지 않아야 한다.** 들어가면 이미 통합된 단위의 소스 해시가 달라져 전부 다시 분석 대상이 된다("이미 끝난 것은 다시 보내지 않는다" 불변식 위반).
- 이미 저장된 기록에는 소급 적용하지 않는다. 다시 수집(`ingest`)할 때 새 필드만 채워지는지 확인한다.

### 2.4 왜 중요한가 (소유자 요구)

같은 결정이라도 어떤 모델이 어떤 추론 수준으로 내렸는지에 따라 근거의 무게가 다르다. 또 이번 평가들이 보였듯 추론 수준이 결과를 좌우할 수 있다. 나중에 "이 판단은 어떤 설정에서 나왔나"를 그래프에서 되짚을 수 있어야 한다.

---

## 3. 파서 설계

- 새 파일 `sources/opencode.py`, `sources/local.py`와 같은 `Snapshot`/`SourceRecord` 계약.
- **범위 귀속:** `session.directory`(그리고 `project_directory`)의 경로로만 판단한다. 키워드 추측 금지. 범위 밖 내용은 모델에 보내지 않는다.
- **하위 에이전트:** `session.parent_id`로 부모와 잇는다(기존 하위 에이전트 처리와 같은 규칙).
- **압축:** `session.time_compacting`과 요약 메시지를 압축 경계로 다룬다.
- **파일 변경:** `patch` 파트는 확실한 편집(`hint=edit`)으로 다루고 `required_citations` 대상에 넣는다.
- **큰 출력:** 32k 자 단위 분할(`segment_record`), 바이너리는 다이제스트 표식으로 바꾼다. `tool-output/`의 역할을 확인한 뒤 정한다.
- **사람 메시지:** `role=user`이면서 하네스가 주입한 텍스트가 아닌 것만 `is_user_prompt`다. opencode의 주입 텍스트 형식은 확인이 필요하다.
- **읽기 전용 열기:** DB는 `mode=ro`로 연다. opencode가 실행 중이면 WAL 파일을 쓰지 않고 읽을 수 있는지 확인한다. 안 되면 DB·WAL를 임시 폴더로 복사해서 읽는 방안을 검토한다(복사본은 0700/0600, 사용 후 삭제).
- **미지원 형식:** 알 수 없는 `part.data.type`은 조용히 버리지 않고 `sources/local.py`의 분류(`IGNORED_NO_ANALYSIS_VALUE` / `KNOWN_UNPARSED` / 미지원)를 따른다.

### 3.1 보안

- opencode DB에는 계정·자격 테이블이 함께 있다. 파서는 **허용 목록**의 테이블만 `SELECT`한다.
- `docs/SECURITY.md`를 구현 전에 읽고, opencode 수집 항목을 추가한다(AGENTS.md: 격리·인증·브라우저 엔드포인트를 바꾸기 전에 검토).
- 테스트는 **합성 opencode.db**를 임시 폴더에 만들어서만 한다. 실제 DB를 테스트에 쓰지 않는다.

---

## 4. 확인한 것 (2026-10-06, opencode 1.18.33 소스와 이 머신의 DB 구조)

1. `variant`는 모델 변형의 이름이고 공식 문서상 추론 수준(`reasoningEffort`·`thinking`)을 담는 설정 묶음이다(`low`/`high`/`max` 등). effort로 읽되 제공자마다 뜻이 다를 수 있다.
2. 파트 구조(`packages/schema/src/v1/session.ts`): `tool`은 `callID`·`tool`·`state{status: pending|running|completed|error, input, output|error, metadata, time}`; `patch`는 `hash`·`files[]`; `step-finish`는 `reason`·`cost`·`tokens`·`snapshot`; `text`는 `synthetic`·`ignored`·`metadata` 플래그; `compaction`은 `auto`·`overflow`·`tail_start_id`; `subtask`는 `prompt`·`description`·`agent`; `file`은 `mime`·`filename`·`url`; 그 밖에 `reasoning`, `step-start`, `snapshot`, `agent`, `retry`.
3. 데이터 폴더는 1절. 환경 변수는 `XDG_DATA_HOME`과 `OPENCODE_DB`뿐.
4. opencode가 켜진 채로 `mode=ro`로 열어 읽을 수 있고, 읽은 뒤 DB·WAL·SHM의 mtime·해시가 같았다(실제 DB, 합성 WAL DB 테스트).
5. Codex `turn_context`·Claude 어시스턴트 레코드의 모델·effort: 미확인(2절 구현 때).
6. `reasoning` 파트는 Codex·Claude와 같이 수집하지 않는다.
7. 주입 텍스트: 사용자 역할의 `text` 파트에 `synthetic: true`(도구 결과에서 꺼낸 첨부), `ignored: true`(모델에 보내지 않은 텍스트), `metadata.compaction_continue: true`(자동 압축 뒤 opencode가 쓰는 후속 메시지)로 표시된다. 셋 다 `metadata` 레코드로 다룬다. 압축은 사용자 메시지의 `compaction` 파트(경계) 다음에 `summary: true`인 어시스턴트 메시지(요약)로 남고, 이전 메시지는 DB에 그대로 있다. 하위 에이전트는 `session.parent_id`와 부모의 `task` 도구 `metadata.sessionId`로 잇는다. `user` 메시지의 `summary`는 그 턴의 파일 diff 목록이지 압축 요약이 아니다.

## 5. 단계

1. ~~4절의 확인.~~ 끝. `authoring`은 뒤로 미뤘다(상위 계획 3절: 먼저 기록이 그래프에 들어가는 것이 목표).
2. ~~`sources/opencode.py`와 합성 DB 테스트.~~ 끝.
3. ~~범위 귀속·하위 에이전트·압축 경계 테스트, 읽기 전용 테스트(파일 변경 없음).~~ 끝.
4. ~~`scan`으로 opencode 세션을 계획에 포함.~~ 끝(이 저장소: 11개 중 4개 세션, 1,936 레코드, 0.4초). `analyze --session current`는 opencode가 셸에 세션 ID 환경 변수를 주는지 확인하지 못해 아직 Codex·Claude만 된다.
5. 화면·브라우저·`find`/`show`에 모델·effort 표시 — 미구현(2절과 함께).
6. ~~README, `docs/SECURITY.md`, CLAUDE.md 갱신.~~ 끝.
