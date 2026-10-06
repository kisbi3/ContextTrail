# Security Policy

ContextTrail reads private conversation logs and runs the user's own Codex or Claude CLI inside a sandbox. Its threat model, trust boundary and isolation design are described in [`docs/SECURITY.md`](docs/SECURITY.md) (Korean). In short: the tool never modifies the analysed project, transcripts or Git metadata; model output is treated as untrusted data; CLIs run under bubblewrap (Linux) or a deny-default `sandbox-exec` profile (macOS) with credentials bind-mounted read-only; and the browser view is loopback-only behind an access token.

## Supported versions

This is a development alpha. Only the latest commit on `main` receives fixes.

## Reporting a vulnerability

Please report vulnerabilities privately, not in a public issue:

- Use GitHub's private vulnerability reporting on this repository ("Report a vulnerability" under the Security tab), or
- email kimjeasung0523@gmail.com with the subject `ContextTrail security`.

Include the version or commit, the platform, steps to reproduce, and what the issue lets an attacker do. You will get an acknowledgement within 7 days. Please allow a reasonable time for a fix before public disclosure.

Reports we especially want:

- any path by which analysed content (transcripts, Git data, model output) can execute as instructions, HTML, SQL or terminal escapes;
- any way the sandboxed CLI can read outside the allowed mounts or write to the project, credentials or the user's home;
- any way the browser view can be reached without the access token or from a non-loopback address;
- any write to the analysed project, transcripts or Git refs/index/config.

Please do not send real transcripts or credentials in a report. A synthetic reproduction is enough.
