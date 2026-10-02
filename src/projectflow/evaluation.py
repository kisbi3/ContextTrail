"""Local, opt-in evaluation artifacts. No telemetry server and no production DB writes."""
from __future__ import annotations

import dataclasses
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .analysis import AnalysisConfig, Engine, _evidence_ids, tool_steps
from .demo import CASES, FixtureRunner
from .eval_review import EvalCallRecorder, render_eval_review
from .git_context import Scope
from .model import Snapshot, SourceRecord
from .render import export_text
from .runners import CLIRunner
from .schema import KINDS, RELATIONS, STATUSES
from .store import Store
from .util import FlowError, digest, dumps, ident, private_dir


def error_kinds(message: str) -> set[str]:
    """The code-written part of a validation error: the Korean text before each ':' (never model text)."""
    kinds = set()
    for part in message.split("; "):
        head = part.split(":", 1)[0].strip()
        if head and len(head) <= 80 and re.search("[가-힣]", head) and not re.search(r"[{}\"'`]", head):
            kinds.add(head)
    return kinds


def summarize_calls(calls: list[dict]) -> dict:
    roles = Counter(c['metadata'].get('routing_role', c['stage']) for c in calls)
    statuses = Counter(c['status'] for c in calls)
    by_model: dict[str, dict] = {}
    tokens: dict[str, int] = {}
    usage_known = 0
    trace_statuses = Counter(c['details'].get('langsmith_trace') for c in calls
                             if c['details'].get('langsmith_trace'))
    for call in calls:
        meta, details = call['metadata'], call['details']
        key = meta.get('requested_model') or meta.get('model') or '(CLI default / unknown)'
        group = by_model.setdefault(key, {'calls': 0, 'input_chars': 0, 'duration_ms': 0,
                                        'actual_models_reported': [], 'calls_by_effort': {}})
        group['calls'] += 1
        effort = meta.get('reasoning_effort') or '(CLI default)'
        group['calls_by_effort'][effort] = group['calls_by_effort'].get(effort, 0) + 1
        group['input_chars'] += meta.get('input_chars', 0)
        group['duration_ms'] += details.get('duration_ms', 0)
        if details.get('actual_model') and details['actual_model'] not in group['actual_models_reported']:
            group['actual_models_reported'].append(details['actual_model'])
        usage = details.get('usage')
        if isinstance(usage, dict):
            usage_known += 1
            # Do not combine cached and non-cached provider metrics or fabricate bills.
            for name, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    tokens[name] = tokens.get(name, 0) + value
    by_role: dict[str, dict] = {}
    for call in calls:
        group = by_role.setdefault(call['metadata'].get('routing_role', call['stage']),
                                   {'calls': 0, 'duration_ms': 0, 'output_chars': 0, 'input_tokens': 0, 'output_tokens': 0})
        group['calls'] += 1
        group['duration_ms'] += call['details'].get('duration_ms', 0)
        group['output_chars'] += call['details'].get('output_chars', 0) or 0
        usage = call['details'].get('usage') if isinstance(call['details'].get('usage'), dict) else {}
        group['input_tokens'] += usage.get('input_tokens', 0) or 0
        group['output_tokens'] += usage.get('output_tokens', 0) or 0
        if call['status'] == 'validation_error':
            for kind in error_kinds(call['details'].get('error', '')):
                group.setdefault('validation_error_kinds', {})[kind] = group.get('validation_error_kinds', {}).get(kind, 0) + 1
        for item in call['details'].get('citation_normalization_audit') or []:
            if isinstance(item, dict) and isinstance(item.get('mode'), str):
                modes = group.setdefault('normalization_modes', {})
                modes[item['mode']] = modes.get(item['mode'], 0) + 1
        for section, chars in (call['details'].get('output_quote_chars') or {}).items():
            group.setdefault('quote_chars', {})[section] = group.get('quote_chars', {}).get(section, 0) + chars
    return {'host_calls': len(calls), 'calls_by_role': dict(roles), 'call_statuses': dict(statuses),
            'by_role': by_role,
            'by_requested_model': by_model, 'usage_available_calls': usage_known,
            'usage_missing_calls': len(calls) - usage_known,
            'provider_usage_sums': tokens or None, 'billed_cost': None,
            'langsmith_trace_statuses': dict(trace_statuses),
            'escalated_units': len({c['unit_id'] for c in calls
                                   if c['metadata'].get('routing_role') == 'escalation'}),
            'validation_error_calls': statuses.get('validation_error', 0),
            'repair_calls': sum(c['metadata'].get('repair_round', 0) > 0 for c in calls),
            'evidence_request_calls': statuses.get('needs_evidence', 0),
            'citation_normalizations': sum(c['details'].get('citation_normalizations', 0) for c in calls),
            'validation_error_kinds': dict(Counter(kind for c in calls if c['status'] == 'validation_error'
                                                   for kind in error_kinds(c['details'].get('error', '')))),
            'quote_mismatch_categories': dict(Counter(
                item['category'] for c in calls
                for item in (c['details'].get('quote_mismatch_audit') or []) if 'category' in item)),
            'quote_repeat_shapes': [item['shape'] for c in calls
                                    for item in (c['details'].get('quote_mismatch_audit') or []) if 'shape' in item],
            'notes': ['host_calls는 CLI task invocation 수이며 provider 내부 model turn 수나 실제 과금액이 아닙니다.',
                      'model은 요청값/alias입니다. actual_models_reported가 비었으면 실제 모델을 확인하지 못했습니다'
                      ' (Codex는 응답에 사용한 모델을 보고하지 않습니다).',
                      'call 비율을 원문/토큰 절감 비율로 해석하지 마세요. 의미 품질은 별도 평가가 필요합니다.']}


def review_summary(graph: dict) -> dict:
    """How often delta review ran and what it did, without issue text."""
    history = graph.get("semantic_review_history", [])
    return {"units": len(history),
            "statuses": dict(Counter(item.get("status") for item in history)),
            "resolutions": dict(Counter(r.get("status") for item in history for r in item.get("resolutions", []))),
            "signals": dict(Counter(i.get("signal") for item in history for i in item.get("issues", [])))}


def call_timeline(calls: list[dict]) -> list[dict]:
    """Expose a local call sequence without prompt, response, or raw error text."""
    usage_keys = ("input_tokens", "output_tokens", "cached_input_tokens",
                  "cache_write_input_tokens", "reasoning_output_tokens")
    timeline = []
    for call in calls:
        meta, details = call["metadata"], call["details"]
        usage = details.get("usage")
        safe_usage = ({key: usage[key] for key in usage_keys
                       if isinstance(usage.get(key), (int, float)) and not isinstance(usage[key], bool)}
                      if isinstance(usage, dict) else None)
        timeline.append({"run_id": call["run_id"], "unit_id": call["unit_id"],
                         "stage": call["stage"], "role": meta.get("routing_role", call["stage"]),
                         "attempt": call["attempt"], "status": call["status"],
                         "started_at": call["started_at"], "finished_at": call["finished_at"],
                         "duration_ms": details.get("duration_ms"),
                         "requested_model": meta.get("requested_model") or meta.get("model"),
                         "actual_model": details.get("actual_model"),
                         "reasoning_effort": meta.get("reasoning_effort"),
                         "input_chars": meta.get("input_chars"), "usage": safe_usage,
                         "langsmith_trace": details.get("langsmith_trace"),
                         "read_round": meta.get("read_round", 0),
                         "repair_round": meta.get("repair_round", 0),
                         "routing_reasons": meta.get("routing_reasons", [])})
    return timeline


def demo_fixture() -> dict:
    records, expected = [], []
    for i, case in enumerate(CASES):
        text, kind, title, status, actor, _ = case
        role = 'tool_result' if actor == 'tool' else actor
        records.append({'source_id': f'eval-s{i}', 'provider': 'claude' if i >= 4 else 'codex',
                        'session_id': 'eval-claude' if i >= 4 else 'eval-codex', 'role': role,
                        'content': text, 'cwd': '/fixture/project', 'worktree_id': 'wt_fixture',
                        'tool_call_id': f'eval-call-{i}' if role == 'tool_result' else None,
                        'recorded_at': f'2026-09-22T10:0{i}:00Z',
                        'locator': {'kind': 'frozen_fixture', 'line': i + 1}})
        expected.append({'label': f'event{i}', 'title_contains': title, 'status': status,
                         'kind': kind, 'actor': actor, 'source_ids': [f'eval-s{i}']})
    return {'format': 'projectflow-eval-v1', 'name': 'synthetic-storage-revision', 'records': records,
            'expectations': {'events': expected, 'relations': [
                {'from': 'event3', 'to': 'event4', 'relation': 'motivates'}]},
            'notes': '合成 / synthetic fixture. Mock passes do not measure LLM understanding.'}


def load_fixture(value: str) -> dict:
    if value == 'demo':
        return demo_fixture()
    path = Path(value).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_000_000:
        raise FlowError('평가 fixture는 symlink가 아닌 8 MB 이하 JSON 파일이어야 합니다.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise FlowError('평가 fixture JSON을 읽을 수 없습니다.') from exc
    if not isinstance(data, dict) or data.get('format') != 'projectflow-eval-v1':
        raise FlowError('fixture format은 projectflow-eval-v1이어야 합니다.')
    if not isinstance(data.get('records'), list) or not 1 <= len(data['records']) <= 2000:
        raise FlowError('fixture에는 1~2000개의 고정된 records가 필요합니다.')
    return data


def fixture_records(data: dict) -> list[SourceRecord]:
    allowed = {f.name for f in dataclasses.fields(SourceRecord)} - {'pinned_hash'}
    records = []
    seen = set()
    for item in data['records']:
        if not isinstance(item, dict) or set(item) - allowed:
            raise FlowError('fixture record에 알 수 없는 필드가 있습니다.')
        try:
            record = SourceRecord(**item)
        except (TypeError, ValueError) as exc:
            raise FlowError('fixture SourceRecord 필수 필드가 잘못되었습니다.') from exc
        if not isinstance(record.source_id, str) or not record.source_id or record.source_id in seen:
            raise FlowError('fixture source_id는 고유한 비어 있지 않은 문자열이어야 합니다.')
        if not isinstance(record.content, str) or not record.content.strip():
            raise FlowError('fixture content는 비어 있지 않은 문자열이어야 합니다.')
        if record.role not in {'user', 'assistant', 'tool_call', 'tool_result', 'metadata', 'git'}:
            raise FlowError('fixture role이 지원 범위 밖입니다.')
        if record.provider not in {'codex', 'claude', 'git'} or not isinstance(record.locator, dict):
            raise FlowError('fixture provider/locator가 잘못되었습니다.')
        # The eval loader never reads paths in locator or executes log commands.
        # Disable revision-file capabilities: a fixture cannot grant filesystem access.
        record.locator = {'kind': 'frozen_fixture', 'original_locator': record.locator}
        records.append(record)
        seen.add(record.source_id)
    return records


_STDIN_SESSION = re.compile(r"write_stdin\(\s*\{[^}]*?session_id\s*:\s*(\d+)")


def fixture_integrity(records: list[SourceRecord]) -> dict:
    """Flag a hand-cut fixture that drops a tool result or the record opening a polled session.

    Missing halves make the model infer links it cannot cite, so they are reported, not fatal.
    """
    calls = {r.tool_call_id: r.source_id for r in records if r.role == 'tool_call' and r.tool_call_id}
    results = {r.tool_call_id: r.source_id for r in records if r.role == 'tool_result' and r.tool_call_id}
    without_result = sorted(source_id for call_id, source_id in calls.items() if call_id not in results)
    without_call = sorted(source_id for call_id, source_id in results.items() if call_id not in calls)
    outputs = [r.content for r in records if r.role == 'tool_result']
    unresolved = []
    for record in records:
        if record.role != 'tool_call':
            continue
        for match in _STDIN_SESSION.finditer(record.content):
            opened = re.compile(r'\\?"session_id\\?"\s*:\s*' + match.group(1) + r'\b')
            if not any(opened.search(text) for text in outputs):
                unresolved.append({'source_id': record.source_id, 'session_id': int(match.group(1))})
    limitations = []
    if without_result:
        limitations.append(f"fixture에 결과가 없는 도구 호출 {len(without_result)}건: {', '.join(without_result)}")
    if without_call:
        limitations.append(f"fixture에 호출이 없는 도구 결과 {len(without_call)}건: {', '.join(without_call)}")
    if unresolved:
        limitations.append("fixture에 시작 기록이 없는 실행 세션 참조: " + ", ".join(
            f"{item['source_id']}(session {item['session_id']})" for item in unresolved))
    return {'tool_calls_without_result': without_result, 'tool_results_without_call': without_call,
            'unresolved_stdin_sessions': unresolved, 'complete': not limitations, 'limitations': limitations}


def validate_expectations(expectations: Any, source_ids: set[str]) -> None:
    """Validate the scoring configuration before incurring any model usage."""
    if expectations is None:
        return
    if not isinstance(expectations, dict) or set(expectations) - {
            "events", "relations", "forbidden_events", "forbidden_relations"}:
        raise FlowError("expectations에는 events/relations/forbidden_events/forbidden_relations 배열만 허용합니다.")
    for key, items in expectations.items():
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise FlowError(f"expectations.{key}는 object 배열이어야 합니다.")
    labels = set()
    for item in expectations.get("events", []):
        if set(item) - {"label", "title_contains", "status", "kind", "actor", "source_ids", "source_ids_any"}:
            raise FlowError("event expectation에 알 수 없는 필드가 있습니다.")
        if not isinstance(item.get("label"), str) or not item["label"].strip():
            raise FlowError("event expectation에는 label이 필요합니다.")
        # Model titles vary; a source-anchored expectation may omit title_contains.
        if "title_contains" in item or not (item.get("source_ids") or item.get("source_ids_any")):
            if not isinstance(item.get("title_contains"), str) or not item["title_contains"].strip():
                raise FlowError("event expectation에는 title_contains 또는 source_ids가 필요합니다.")
        if item["label"] in labels:
            raise FlowError("event expectation label은 중복될 수 없습니다.")
        labels.add(item["label"])
        for key in ("status", "kind", "actor"):
            if key in item and (not isinstance(item[key], str) or not item[key].strip()):
                raise FlowError(f"event expectation {key}는 비어 있지 않은 문자열이어야 합니다.")
        if item.get("status", STATUSES[0]) not in STATUSES or item.get("kind", KINDS[0]) not in KINDS:
            raise FlowError("event expectation의 status/kind가 지원 상태와 다릅니다.")
        # source_ids must all be cited; source_ids_any needs one of them, for an event whose
        # evidence is spread over several records any of which shows it.
        for key in ("source_ids", "source_ids_any"):
            ids = item.get(key, [])
            if not isinstance(ids, list) or any(not isinstance(i, str) or i not in source_ids for i in ids):
                raise FlowError(f"event expectation {key}는 fixture의 실제 ID여야 합니다.")
        if "source_ids_any" in item and not item["source_ids_any"]:
            raise FlowError("event expectation source_ids_any는 비어 있을 수 없습니다.")
    for item in expectations.get("relations", []):
        # `relation` may list alternatives when more than one reading is correct.
        allowed = item.get("relation") if isinstance(item.get("relation"), list) else [item.get("relation")]
        if set(item) != {"from", "to", "relation"} or not isinstance(item["from"], str) or not isinstance(
                item["to"], str) or not allowed or not all(isinstance(v, str) for v in allowed):
            raise FlowError("relation expectation에는 from/to 문자열과 relation 문자열(또는 문자열 배열)이 필요합니다.")
        if item["from"] not in labels or item["to"] not in labels or not set(allowed) <= set(RELATIONS):
            raise FlowError("relation expectation의 사건 label/관계가 잘못되었습니다.")
    # A forbidden relation without `to` forbids that relation from the event to anything,
    # e.g. a `verifies` from a change that no run actually exercised.
    for item in expectations.get("forbidden_relations", []):
        if not {"from", "relation"} <= set(item) <= {"from", "to", "relation"} or not all(
                isinstance(v, str) for v in item.values()):
            raise FlowError("forbidden_relations에는 from/relation(과 선택적 to) 문자열이 필요합니다.")
        if item["from"] not in labels or item.get("to", item["from"]) not in labels or item["relation"] not in RELATIONS:
            raise FlowError("forbidden_relations의 사건 label/관계가 잘못되었습니다.")
    for item in expectations.get("forbidden_events", []):
        if not item or set(item) - {"title_contains", "status", "kind", "actor", "basis"} or any(
                not isinstance(v, str) or not v.strip() for v in item.values()):
            raise FlowError("forbidden_events에는 비어 있지 않은 사건 조건이 필요합니다.")


def check_expectations(graph: dict, evidence: dict, expectations: Any) -> dict:
    if not expectations or (isinstance(expectations, dict) and not any(expectations.values())):
        return {'checks': [], 'passed': None, 'failed': None,
                'semantic_quality': 'not_scored; human expectations were not supplied'}
    if not isinstance(expectations, dict):
        raise FlowError('expectations는 JSON object여야 합니다.')
    checks, matched, used = [], {}, set()
    for expected in expectations.get('events', []):
        if not isinstance(expected, dict) or not expected.get('label') or not (
                expected.get('title_contains') or expected.get('source_ids') or expected.get('source_ids_any')):
            raise FlowError('event expectation에는 label과 title_contains 또는 source_ids가 필요합니다.')
        hits = [e for e in graph['events'] if expected.get('title_contains', '') in e['title']]
        valid = []
        for event in hits:
            source_ids = {evidence[i]['source_id'] for i in event['evidence_ids'] if i in evidence}
            if all(event.get(k) == expected[k] for k in ('status', 'kind', 'actor') if k in expected) and \
                    set(expected.get('source_ids', [])) <= source_ids and \
                    ('source_ids_any' not in expected or set(expected['source_ids_any']) & source_ids):
                valid.append(event)
        passed = len(valid) == 1 and valid[0]['id'] not in used
        if passed:
            matched[expected['label']] = valid[0]['id']
            used.add(valid[0]['id'])
        checks.append({'type': 'event', 'expected': expected, 'passed': passed,
                       'matched_event_ids': [e['id'] for e in valid]})
    for expected in expectations.get('relations', []):
        left, right = matched.get(expected.get('from')), matched.get(expected.get('to'))
        allowed = expected['relation'] if isinstance(expected['relation'], list) else [expected['relation']]
        passed = bool(left and right and any(e['active'] and e['from_event_id'] == left and
                     e['to_event_id'] == right and e['relation'] in allowed for e in graph['edges']))
        checks.append({'type': 'relation', 'expected': expected, 'passed': passed})
    for forbidden in expectations.get('forbidden_relations', []):
        left, right = matched.get(forbidden['from']), matched.get(forbidden.get('to', forbidden['from']))
        # Unmatched endpoints make the absence unverifiable, which is not a pass.
        hits = [e['id'] for e in graph['edges'] if e['active'] and e['from_event_id'] == left and
                e['relation'] == forbidden['relation'] and ('to' not in forbidden or e['to_event_id'] == right)]
        checks.append({'type': 'forbidden_relation', 'expected': forbidden,
                       'passed': bool(left and right) and not hits, 'matched_edge_ids': hits})
    for forbidden in expectations.get('forbidden_events', []):
        hits = [e for e in graph['events'] if all(
            forbidden[k] in e['title'] if k == 'title_contains' else e.get(k) == forbidden[k]
            for k in forbidden)]
        checks.append({'type': 'forbidden_event', 'expected': forbidden, 'passed': not hits})
    return {'checks': checks, 'passed': sum(c['passed'] for c in checks),
            'failed': sum(not c['passed'] for c in checks),
            'unmatched_events_for_human_review': [e['id'] for e in graph['events'] if e['id'] not in used],
            'semantic_quality': 'limited expectation checks only; not a general accuracy score'}


# A title longer than this is probably a sentence where a short statement of the outcome would do.
TITLE_CHARS = 40


def style_checks(graph: dict, evidence: dict[str, dict], records: list) -> dict:
    """How readable the graph is, as counts: title length, and events resting only on read-only calls.

    Counts only, for comparing runs; nothing here fails an eval.
    """
    steps = tool_steps(records)
    reads = {step[key] for step in steps if step["hint"] == "read" for key in ("call", "result") if step[key]}
    titles = [len(event["title"]) for event in graph["events"]]
    only_reads = [event["id"] for event in graph["events"] if event.get("actor") != "user" and event["evidence_ids"]
                  and all(evidence.get(i, {}).get("source_id") in reads for i in event["evidence_ids"])]
    return {"events": len(titles), "title_chars_mean": round(sum(titles) / len(titles), 1) if titles else 0,
            "titles_over_limit": sum(n > TITLE_CHARS for n in titles), "title_limit": TITLE_CHARS,
            "events_citing_only_reads": len(only_reads)}


def run_eval(fixture: str, output: Path, runner_name: str, config: AnalysisConfig, *,
             yes: bool = False, model: str | None = None, timeout: float = 600,
             progress: Callable[[str], None] | None = None) -> dict:
    data = load_fixture(fixture)
    if runner_name != 'mock' and not yes:
        raise FlowError('실제 CLI 평가는 개인 계정 사용량과 자료 전송을 수반합니다. --yes로 명시적으로 동의하세요.')
    if runner_name == 'mock' and fixture != 'demo':
        raise FlowError('mock은 demo 합성 fixture만 지원합니다. 실제 자료의 의미 평가인 것처럼 실행하지 않습니다.')
    config.validate()
    records = fixture_records(data)
    validate_expectations(data.get("expectations"), {r.source_id for r in records})
    integrity = fixture_integrity(records)
    snapshot = Snapshot(records)
    if output.is_symlink():
        raise FlowError('평가 output symlink는 허용하지 않습니다.')
    output = output.expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FlowError('평가는 새 디렉터리 또는 빈 디렉터리에서만 시작합니다. production 상태와 A/B 결과를 혼합하지 않습니다.')
    private_dir(output)
    scope_id = ident('eval_', snapshot.id)
    scope = Scope(output, None, None, '', [output], output / 'state', scope_id)
    store = Store(scope.state_dir, scope.id)
    config.runner_name, config.base_model = runner_name, model
    engine = Engine(scope, store, config)
    engine.scan = lambda: snapshot
    engine.review_capture = EvalCallRecorder(output)
    factory = FixtureRunner if runner_name == 'mock' else lambda: CLIRunner(runner_name, model=model, timeout=timeout)
    first = engine.analyze(factory, update=progress)
    calls = store.llm_calls(first['run_id'])
    # A failed live trial must not silently be re-run and consume another budget.
    second = None
    if first['status'] == 'complete':
        def forbid_call():
            raise FlowError('평가의 동일 입력 재실행에서 Runner 생성이 감지됐습니다.')
        second = engine.analyze(forbid_call)
    graph = store.graph()
    evidence = store.evidence_many(_evidence_ids(graph))
    report = {'format': 'projectflow-eval-report-v1', 'name': data.get('name'),
              'fixture_digest': digest(data), 'source_snapshot_id': snapshot.id,
              'initial_graph_version': 0, 'mode': 'synthetic_mock' if runner_name == 'mock' else 'live_cli',
              'config': {k: str(v) if isinstance(v, Path) else v for k, v in dataclasses.asdict(config).items()},
              'first_run': {k: v for k, v in first.items() if k != 'graph'},
              'noop_check': {'status': second['status'], 'runner_calls': second['runner_calls']} if second else None,
              'ops': summarize_calls(calls), 'semantic_review': review_summary(graph), 'expectations': check_expectations(graph, evidence, data.get('expectations')),
              'style': {**style_checks(graph, evidence, snapshot.records), 'output_language': config.output_language},
              'fixture_integrity': integrity,
              'limitations': ['유효한 JSON/인용/기대 사건 검사만으로 의미적 정답을 보장하지 않습니다.',
                              '토큰/사용량 미제공은 unknown이며 실제 결제액을 추정하지 않습니다.',
                              *integrity['limitations']]}
    files = {'fixture.json': dumps(data, pretty=True), 'report.json': dumps(report, pretty=True),
             'calls.json': dumps(calls, pretty=True), 'flow.json': dumps({'graph': graph, 'evidence': evidence}, pretty=True),
             'review.md': export_text(graph, evidence, 'md')}
    for name, text in files.items():
        path = output / name
        path.write_text(text, encoding='utf-8')
        path.chmod(0o600)
    render_eval_review(output)
    return report
