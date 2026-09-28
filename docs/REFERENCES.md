# 구현 근거와 기술 참고

## 제품 요구의 기준

`PRD.md`와 `SPEC.md`는 제품 범위와 구현 계약의 기준이다. 기술 자료로 제품 범위나 구현 성과를 대신 정하지 않았다.

## 실행기 옵션 참고 — 2026-09-22 조회

외부 공식 문서는 CLI 옵션 선택을 위한 참고다. 문서에 옵션이 있다는 것과 사용자 서버에서 해당 조합이 동작한다는 것은 다르다. 런타임은 --help/capability 확인을 수행하며 live smoke 및 권한 부정 시험이 남아 있다.

- OpenAI Codex non-interactive mode: https://developers.openai.com/codex/noninteractive/ (조회 시 https://learn.chatgpt.com/docs/non-interactive-mode 로 연결)
- OpenAI Codex configuration reference: https://developers.openai.com/codex/config-reference/ (조회 시 https://learn.chatgpt.com/docs/config-file/config-reference 로 연결)
- Anthropic Claude Code CLI reference: https://code.claude.com/docs/en/cli-reference
- Anthropic Claude Code settings: https://code.claude.com/docs/en/settings

Codex의 exec/JSON schema/ephemeral 실행과 feature flags, Claude의 print/schema/tool restriction/setting sources/safe·restricted mode를 확인하는 데 사용했다. 아직 실제 binary·계정으로 그 조합을 시험하지 않았다.

나머지 Git·Mermaid·SSH·로그 경로 관련 설계 참고는 원본 SPEC §15의 링크를 유지한다. 자체 문자/SVG renderer는 full Mermaid 라이브러리의 구현을 복제한 것이 아니라 이 프로그램이 생성하는 제한된 그래프를 표시하는 코드다.
