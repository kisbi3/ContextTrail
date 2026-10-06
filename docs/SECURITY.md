# 보안·실행 모델

## 보호 대상과 신뢰 경계

프로젝트·대화 원문·Git metadata를 분석기가 수정하지 않고, 원문에 들어 있는 지시·명령·HTML·터미널 escape가 실행되지 않도록 한다. LLM 출력은 신뢰하지 않는 구조화 데이터로 취급한다. 인증된 동일 OS 사용자가 프로그램 소스/DB/바이너리를 직접 바꾸는 공격, root 침해, CLI 바이너리 자체의 악성 동작, 네트워크 공급망 침해까지 방어하는 샌드박스 제품은 아니다.

## 호스트 코드

- 로그: 범위가 맞는 기록만 AI 입력에 선택한다. 완전한 JSONL 행까지 snapshot을 고정하고 변경 경쟁·손상·알 수 없는 형식을 표시한다. account의 무관한 대화를 모델에 보내 분류하지 않는다.
- Git: shell 문자열이 아닌 인자 배열, 읽기 연산만 사용한다. inherited `GIT_*`, external diff, textconv, fsmonitor, pager, hooks 경로를 제한한다. Git objects/refs/index/config를 수정하지 않는다.
- Sidecar: 명시적으로 참조되고 허용 디렉터리 안에 있는 tool-result만 읽는다. arbitrary ReadRequest 경로는 허용하지 않는다.
- opencode: 데이터 폴더의 `opencode*.db`를 `mode=ro`·`query_only`로 열고 `session`·`message`·`part` 세 테이블만 질의한다. 같은 파일의 계정·자격 테이블(`account`, `credential` 등)과 `auth.json`은 이름조차 질의에 넣지 않으며, 테스트가 이를 고정한다(`tests/test_opencode_source.py`). 세션의 범위 귀속은 기록된 `directory`로만 한다. 읽기 뒤 DB·WAL·SHM의 mtime과 해시가 같음을 합성 DB와 실제 DB에서 확인했다(2026-10-06). opencode의 `tool-output/`에 있는 잘린 출력 전문은 읽지 않는다.
- 처리: 근거 존재·내용 hash·인용 줄·발췌 일치·상태 enum·참조·source snapshot·graph version을 검증한다. observed 성공/실패에는 원래 tool_result가 필요하다. 이것만으로 의미적 주장이 옳음을 보증하지 않는다.
- 주변 문맥: 같은 worktree에서 시각이 15분 이내인 반대 출처(Git/대화)의 기록을 최대 4개 우선 제공할 수 있다. 이 기록은 `context_only`이며 시간 근접성은 인과관계나 동일 작업의 증거가 아니다. 다른 worktree의 기록은 이 규칙으로 추가하지 않는다.
- 시점 제한: WorkUnit 이후의 같은 세션 기록과 시각상 미래의 교차 출처 기록은 원문 manifest와 추가 읽기 목록에서 제외한다. 고정 revision 파일의 조회 허용 목록도 같은 범위로 제한한다.
- 새 사건의 출처: 추출 후보에는 이번 WorkUnit에 배정된 `new_records`의 인용이 최소 하나 필요하다. `context_only` 원문은 교차 확인 근거로 인용할 수 있지만 그것만으로 새 사건을 만들 수 없다. Git 기록은 별도의 Git WorkUnit에서 `new_records`가 된다.
- 저장: owner-only 디렉터리와 DB, 짧은 SQLite transaction, flock 단일 분석, 완료 결과의 원자적 게시를 사용한다. 원본 전체를 영구 백업하지 않으며 실제 인용 발췌는 삭제된 원본 대신 남을 수 있다.

## 실제 Runner

**필수:** Linux는 `bwrap`, macOS는 시스템 `sandbox-exec`; 공통으로 지원 CLI 옵션과 해당 CLI의 파일 기반 인증. 하나라도 확인할 수 없으면 실행하지 않는다.

격리 프로세스에는 read-only 시스템 런타임, 선별한 CLI 설치 위치, 관리된 `/work` 입력, 별도 `/out`, 빈 임시 홈을 제공한다. 원래 HOME, 프로젝트, SSH 키, 사용자 `AGENTS.md`/`CLAUDE.md`, hooks·plugins·MCP 설정은 그대로 mount하지 않는다. 인증 파일은 해당 경로 하나만 read-only bind하며 프로그램 코드가 그 바이트를 읽어 DB·프롬프트·trace에 복사하지 않는다.

macOS는 파일 시스템 mount namespace가 없어 실제 임시 작업·출력 경로를 사용한다. `sandbox-exec`의 deny-default 프로필에서 시스템 런타임·선택한 CLI 설치 파일·작업/출력/임시 홈 및 인증 파일만 읽도록 허용하고, 쓰기는 출력 디렉터리와 임시 홈으로 제한한다. Codex는 모델 연결에 필요한 Apple 시스템 Mach 서비스 조회를 허용하며 제삼자 서비스 조회는 허용하지 않는다. codex-cli 0.157부터 시작 시 전역 환경설정을 읽지 못하면 실행을 중단하므로, 전역 도메인(`kCFPreferencesAnyApplication`)의 환경설정 읽기와 `cfprefsd`의 읽기 전용 공유 메모리(`apple.cfprefs.daemonv1`, `apple.cfprefs.<uid>v1`)만 추가로 허용한다. 앱별 도메인·환경설정 쓰기·다른 공유 메모리는 허용하지 않는다. 임시 홈의 인증 파일은 원본을 가리키는 symlink이며 원본의 쓰기는 허용하지 않는다. preflight는 범위 밖 합성 파일의 읽기·쓰기와 임시 홈 symlink를 통한 쓰기가 모두 거부되는지 확인한다. macOS 프로필은 Linux `bwrap`과 다른 보안 경계이며 동일한 격리 수준을 주장하지 않는다. Apple의 `sandbox-exec`는 deprecated이므로 제거되거나 정책이 바뀌면 실제 분석은 차단된다.

npm Codex 설치의 실행 패키지와 같은 `@openai` 폴더에 있는 현재 Mac 아키텍처의 `codex-darwin-*` 네이티브 패키지만 추가로 읽는다. `@openai` 전체나 다른 sibling 패키지는 허용하지 않고, native 경로가 symlink이면 추가하지 않는다.

모델 통신을 위해 네트워크는 허용한다(`--share-net`). **네트워크 egress allowlist나 CLI 바이너리 자체의 무결성 감사는 제공하지 않는다.** 환경변수는 최소화하고 사용자 프록시·커스텀 provider·API key 환경변수를 자동 전달하지 않는다.

Codex는 ephemeral structured exec, read-only sandbox, approval never, web_search disabled와 확인된 위험 기능 토글 비활성화를 조합한다. 도구 실행 event가 감지되면 결과를 게시하지 않는다. 이 후속 탐지 자체가 실행 전 권한 통제를 대신하는 것은 아니다.

Claude는 restricted/safe mode, tools 빈 목록, MCP wildcard disallow, strict empty MCP config, 빈 setting sources, 명시적 빈 hooks/plugins 설정과 세션 비저장을 조합한다. 조직의 managed policy나 CLI 버전에 따른 상호작용은 실제 smoke/권한 부정 시험으로 확인해야 한다.

기존 Linux 사전 점검 환경에서는 bwrap·두 CLI가 없어 실제 연동/도구 차단 시험을 수행하지 못했다. macOS 개발 환경에서 두 CLI의 `doctor`와 합성 파일 읽기·쓰기 차단을 확인했다. Codex의 초기 모델 서비스 연결 실패는 macOS Apple Mach 서비스 조회 차단에서 비롯되었으며, 제한된 Apple 서비스 허용 후 합성 구조화 응답이 성공했다. Claude Runner는 사용하지 않고 로그만 수집한다. 악성 도구/설정 차단 시험은 남아 있다. argv 계약과 실패 시 차단 로직의 unit test를 실제 보안 검증으로 혼동하면 안 된다. `doctor --smoke --yes` 성공도 프로젝트 읽기·도구 악용 음성 테스트를 대체하지 않는다.

### 인증·설치 제약

Keyring-only 인증은 미지원이다. read-only 인증 파일에서 credential refresh가 필요하면 실패할 수 있다. 원래 CLI에서 인증을 갱신한다. 일부 npm/nvm/native 설치 경로의 런타임 구조는 추가 호환성 확인이 필요하다. 정확히 어떤 기능이 켜지는지 확인할 수 없는 버전에는 권한 확대·직접 실행 fallback을 하지 않는다.

## 브라우저

Loopback bind, 프로세스별 랜덤 접근 토큰, Authorization header, 정확한 Host/Origin 검사, POST+사용자 확인+별도 action header를 요구한다. URL token은 fragment로 전달하여 서버 요청 로그에 남기지 않는다. 토큰은 브라우저 sessionStorage에 저장된다. 생성 주소를 공유한 사람은 비공개 근거를 볼 수 있으므로 비밀정보로 취급한다.

JS/CSS는 패키지에 포함하고 외부 CDN·폰트를 요청하지 않는다. 원문은 textContent로 표시한다. SVG는 host-generated escaped markup에 추가 요소 whitelist를 적용한다. CSP·no-store·no-referrer를 설정한다. 일반 browser extension, 같은 계정의 공격적 소프트웨어, 사용자가 의도적으로 전달한 토큰까지 통제하지는 않는다.

GET/F5는 분석하지 않는다. explicit POST 요청만 분석하며 기존 scope lock을 사용한다. 실제 SSH 터널은 사용자가 구성하며 자동 공개 서버나 다중 사용자 로그인 서비스를 만들지 않는다.

## 남은 보안 검증

실제 두 CLI를 대상으로 악성 원문/프로젝트 지침/사용자 hooks/MCP·plugins 설치 환경에서 도구가 실행되지 않는지 검사해야 한다. 해당 시점의 CLI 계정·조직 policy와 user namespace 정책도 확인해야 한다. 현재 자동 테스트의 Git hook/external diff·경로 탈출·잘못된 인용·HTTP CSRF·제어문자 차단 결과는 이 live 검증과 분리한다.

기록 속 API key·비밀번호를 자동으로 제거하지 않는다. 본인이 전송할 권한이 있는 프로젝트에만 실제 분석을 실행한다. 저장/export는 민감 자료이며 백업·암호화·삭제 정책은 사용자가 관리한다.

LangSmith 추적은 분석 구조를 설계하는 개발자용 도구이며 기본적으로 꺼져 있고 명령 도움말에도 나타나지 않는다. `--langsmith`를 명시한 새 분석에서만 호스트 프로세스가 trace를 보낸다. `--langsmith`만 주면 실제 실행 그래프의 노드, 노드 안의 단계(요청 작성·계약 검증·주장 검증·추가 근거 읽기·수리 준비), 모델 호출이 하나의 trace 트리로 전송된다. 모든 payload는 전송 직전 `langsmith_trace.metadata_only` 허용 목록을 통과한다. 숫자, 참/거짓, 코드가 정한 상태값, 형식이 정해진 ID·digest, 모델 식별자만 남는다. `actor`처럼 모델이 채우는 값은 정해진 값(user, assistant, tool 등)일 때만 남는다. 목록은 개수와 상태별 개수로 줄이고, 제목·요약·인용·프롬프트·모델 응답 같은 자유 텍스트와 모델이 지은 `tmp:` ID는 버린다. 오류는 마지막 예외 줄만 남기고 따옴표 안의 값과 `tmp:` ID를 지운다. 이 필터는 SDK가 보내기 직전의 `create_run`/`update_run`에서 적용되며, 명시적으로 만든 client가 없으면 그래프 추적 자체를 켜지 않는다. `--langsmith-content`까지 지정하면 같은 LangGraph 실행 노드의 상태와 모델 task·schema·구조화 응답 원문도 trace에 포함한다. 여기에는 사건 후보, 근거 발췌, 게시 그래프, 프로젝트 대화와 코드·도구 출력이 들어갈 수 있으며 비밀정보 자동 제거는 없다. `LANGSMITH_API_KEY`는 환경변수로 받아 호스트의 LangSmith SDK에만 사용하며 CLI Runner 환경이나 로컬 trace payload에 넣지 않는다. 별도 HTTPS `LANGSMITH_ENDPOINT`를 지정할 수 있으며 주소에 내장 인증정보는 허용하지 않는다. 추적 옵션은 scope의 지속 설정에 저장하지 않는다. 모델 호출 메타데이터 전송 실패는 분석 자체를 계속하며 오류 종류만 로컬 ledger에 기록한다.

`project eval --preview`는 Runner를 만들거나 외부로 전송하지 않고 고정 fixture의 원문·모델 입력·schema를 owner-only 출력 디렉터리에 기록한다. `input-preview.html`과 task JSON은 비공개 대화·코드 원문을 포함하므로 공유 자료가 아니다. 후속 단위의 입력은 앞 모델 결과에 따라 변하며 미리보기는 해당 부분을 예상치로 표시한다.

`project eval`은 검토를 위해 매 호출의 정확한 task·schema·검증 전 구조화 응답을 평가 출력의 `call-review/`에 owner-only로 저장하고, 인용 비교가 포함된 `review.html`을 만든다. 실패한 후보도 남는다. 이 자료는 모델 서비스나 LangSmith에 추가 전송하지 않는다. `project review`는 기존 평가 파일만 로컬에서 읽고 HTML을 다시 작성한다. 평가 출력 디렉터리 전체를 비공개 기록으로 다루고 불필요해지면 사용자가 삭제한다. 이전 실행에서 저장하지 않은 응답 원문은 소급 복원할 수 없다.

`project graph`는 저장된 그래프와 근거만 로컬에서 읽는다. 대화 원문 발췌가 터미널 또는 표준 출력에 표시될 수 있다. `install.sh`는 사용자 홈에 Python 환경·CLI 링크와 Codex·Claude Code 개인 명령 파일을 만든다. ContextTrail이 생성한 명령은 재설치 시 갱신하지만 별도로 작성한 기존 명령은 덮어쓰지 않는다. 그 파일의 강제 교체에는 `project install-commands --force`가 필요하다. 설치된 갱신 명령은 사용자가 이름으로 부를 때만 실행되도록 설치한다(Claude Code `disable-model-invocation: true`, Codex `agents/openai.yaml`의 `allow_implicit_invocation: false`). 실행하면 계획을 보여 주고 처리할 작업 단위 수를 사용자에게 받은 뒤 현재 프로젝트를 Codex Runner로 분석한다. 문맥 파악 명령은 `project find`/`project show`로 저장 결과를 읽기만 한다. 다만 그 출력에는 인용 원문이 들어 있고, 명령을 부른 에이전트의 모델 서비스로 전달된다(Claude Code에서 쓰면 Anthropic). 인용은 과거 기록이므로 출력과 스킬 모두 지시가 아닌 자료로 다루라고 명시한다. 터미널 `y` 키는 사건 참조(ID와 그래프 버전)만 클립보드에 넣으며 원문은 넣지 않는다. 로컬 Mac에서는 `pbcopy`, 그 밖에는 OSC 52 터미널 요청을 쓴다.

선택적 `contexttrail_analysis` Studio 앱은 별도 로컬 Agent Server에서 실행한다. 기본 `{}` 입력은 자체 합성 fixture와 Mock Runner만 처리한다. 실제 프로젝트 모드는 서버 시작 시 절대경로 `CONTEXTTRAIL_STUDIO_SCOPE`로 scope를 고정한다. 작은 평가 모드는 서버의 `CONTEXTTRAIL_STUDIO_EVAL_FIXTURE`에 고정된 JSON만 읽고 임시 상태에 게시한다. 둘 다 `confirm_live=true`를 요구한다. Studio 입력으로 임의 프로젝트·fixture 경로를 받지 않는다. 두 모드는 Codex CLI만 호출하고 호출 수와 처리 WorkUnit 수에 상한을 둔다. Studio 실행 상태와 LangSmith trace에는 정확한 모델 요청·응답, 원문 인용, 후보 및 GraphDelta가 포함될 수 있다. 로컬 서버는 인증을 제공하지 않으므로 `127.0.0.1`에만 바인딩하고 이 모드가 필요 없으면 서버를 종료한다. 합성 모드의 Mock 결과는 실제 프로젝트 분석 결과로 취급하지 않는다.
