import html
import copy
import json
import threading
import time
from pathlib import Path

import pytest

from contexttrail.analysis import AnalysisConfig
from contexttrail.cli import main
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.evaluation import call_timeline, demo_fixture, run_eval, summarize_calls, fixture_records
from contexttrail.util import Cancelled, FlowError


def fail_publish_once(engine):
    """The unit's extraction is saved, then the run stops before its graph is published (once)."""
    integrate = engine._integrate_unit
    def fail(*args, **kwargs):
        engine._integrate_unit = integrate
        raise FlowError('publish unavailable')
    engine._integrate_unit = fail


def test_the_story_call_runs_on_the_extract_model(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    engine.config.extract_model = 'mock-small'
    result = engine.analyze(FixtureRunner)
    assert result['status'] == 'complete'
    calls = store.llm_calls()
    assert [(c['stage'], c['metadata']['requested_model']) for c in calls] == [('story', 'mock-small')]
    assert all(c['details']['actual_model'] is None for c in calls)


def test_parallel_extract_overlap_and_serial_graph_publish(laboratory):
    _, store, engine, records, make = laboratory
    engine.config.extract_workers = 2
    records.extend([make(CASES[0][0], key='a', session='one'), make(CASES[1][0], key='b', session='two', role='assistant')])
    barrier = threading.Barrier(2)
    active, peak = 0, 0
    lock = threading.Lock()
    class Parallel(FixtureRunner):
        def run(self, task, schema, cancel):
            nonlocal active, peak
            if task['stage'] == 'story':
                with lock:
                    active += 1; peak = max(peak, active)
                barrier.wait(timeout=3)
                time.sleep(.025)
                with lock: active -= 1
            return super().run(task, schema, cancel)
    result = engine.analyze(Parallel)
    assert result['status'] == 'complete', result
    assert peak == 2
    # The person's message is a request titled by its words; the assistant's alternative is the story's event.
    assert [e['title'] for e in result['graph']['events']] == [CASES[0][0], CASES[1][2]]
    assert result['graph']['version'] == 2
    assert len({c['metadata']['worker_id'] for c in store.llm_calls() if c['stage'] == 'story'}) == 2
    assert engine.analyze(lambda: pytest.fail('noop'))['runner_calls'] == 0


def test_the_story_call_runs_at_its_reasoning_effort(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    class Effortful(FixtureRunner):
        effort = None
    assert engine.config.extract_effort == 'medium'  # default
    engine.config.extract_effort = 'high'
    assert engine.analyze(Effortful)['status'] == 'complete'
    assert [(c['metadata']['routing_role'], c['metadata']['reasoning_effort']) for c in store.llm_calls()] == [
        ('extract', 'high')]


def test_infrastructure_failure_does_not_trigger_model_fallback(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    class Fail(FixtureRunner):
        def run(self, task, schema, cancel):
            raise FlowError('timeout/account failure')
    result = engine.analyze(Fail)
    assert result['status'] == 'failed'
    assert len(store.llm_calls()) == 1


def test_a_short_unit_takes_one_call_and_is_not_sent_again(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make('감사합니다.'))
    result = engine.analyze(FixtureRunner)
    assert result['status'] == 'complete' and result['runner_calls'] == 1
    assert engine.analyze(lambda: pytest.fail('cached empty'))['status'] == 'noop'


def test_extraction_is_reused_after_a_publish_failure(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    fail_publish_once(engine)
    failed = engine.analyze(FixtureRunner)
    assert failed['status'] == 'failed' and store.units()[0]['status'] == 'extracted'
    second = engine.analyze(FixtureRunner)
    assert second['status'] == 'complete' and second['reused_extractions'] == 1
    assert store.llm_calls(second['run_id']) == []


def test_pending_cache_is_invalidated_on_extract_model_change(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    engine.config.extract_model = 'small-A'
    fail_publish_once(engine)
    engine.analyze(FixtureRunner)
    engine.config.extract_model = 'small-B'
    second = engine.analyze(FixtureRunner)
    assert second['status'] == 'complete' and second['reused_extractions'] == 0
    assert second['runner_calls'] == 1
    # Integrated units remain untouched after subsequent configuration changes.
    engine.config.extract_model = 'small-C'
    assert engine.analyze(lambda: pytest.fail('integrated unchanged'))['status'] == 'noop'


def test_global_call_cap_across_parallel_workers(laboratory):
    _, store, engine, records, make = laboratory
    engine.config.extract_workers, engine.config.max_calls = 2, 1
    records.extend([make(CASES[0][0], key='a', session='one'), make(CASES[4][0], key='b', session='two')])
    # The second worker asks for its call only once the first has its own, so the one call is the first unit's.
    first_call = threading.Event()
    prepare = engine._prepare_extraction
    def prepare_after_first(unit, *args, **kwargs):
        if unit['sources'] == ['b']:
            assert first_call.wait(timeout=3)
        return prepare(unit, *args, **kwargs)
    engine._prepare_extraction = prepare_after_first
    class First(FixtureRunner):
        def run(self, task, schema, cancel):
            first_call.set()
            return super().run(task, schema, cancel)
    first = engine.analyze(First)
    # One call between the two workers; the second unit is not read with no call left.
    assert first['runner_calls'] == 1
    assert len(store.llm_calls()) == 1
    assert first['status'] == 'partial' and first['completed_units'] == 1
    engine.config.max_calls = 10
    second = engine.analyze(FixtureRunner)
    assert second['status'] == 'complete' and second['completed_units'] == 1


def test_parallel_cancel_preserves_no_publication(laboratory):
    _, store, engine, records, make = laboratory
    engine.config.extract_workers = 2
    records.extend([make('none', key='a', session='one'), make('none', key='b', session='two')])
    cancel = threading.Event()
    class Stop(FixtureRunner):
        def run(self, task, schema, event):
            event.set()
            raise Cancelled('stop')
    result = engine.analyze(Stop, cancel=cancel)
    assert result['status'] == 'cancelled'
    assert store.graph()['version'] == 0
    assert not any(c['status'] == 'running' for c in store.llm_calls())


def test_parallel_singleton_runner_is_rejected(laboratory):
    _, _, engine, records, make = laboratory
    engine.config.extract_workers = 2
    records.extend([make('none', key='a', session='one'), make('none', key='b', session='two')])
    class Slow(FixtureRunner):
        def run(self, task, schema, event):
            time.sleep(.05)
            return super().run(task, schema, event)
    singleton = Slow()
    result = engine.analyze(lambda: singleton)
    assert result['status'] in {'partial', 'failed'}
    assert '독립 인스턴스' in result['error']


def test_ops_reports_unknown_usage_and_no_fabricated_cost(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    engine.analyze(FixtureRunner)
    report = summarize_calls(store.llm_calls())
    assert report['usage_missing_calls'] == 1 and report['provider_usage_sums'] is None
    assert report['billed_cost'] is None
    assert sum(group['calls'] for group in report['by_role'].values()) == report['host_calls']
    assert all({'calls', 'duration_ms', 'output_chars', 'input_tokens', 'output_tokens'} <= set(group)
               for group in report['by_role'].values())


def test_output_quote_chars_counts_sizes_per_section():
    from contexttrail.analysis import output_quote_chars
    output = {'events_to_add': [{'evidence': [{'quote': 'abcd'}, {'quote': 'ef'}]}],
              'change_attributions': [{'evidence': [{'quote': 'xyz'}]}], 'limitations': ['not a quote']}
    assert output_quote_chars(output) == {'events_to_add': 6, 'change_attributions': 3}


def test_ops_count_why_the_quotes_did_not_match():
    # A ledger row as an answer whose quote was not in its cited lines leaves it (the reason, never the quote).
    call = {'stage': 'story', 'status': 'validation_error', 'metadata': {'routing_role': 'extract'},
            'details': {'error': 'quote not found uniquely in the cited lines: src_1:1-1 (0 matches)',
                        'quote_mismatch_audit': [{'category': 'not_found', 'source_id': 'src_1'}]}}
    assert summarize_calls([call])["quote_mismatch_categories"] == {"not_found": 1}


def test_ops_timeline_excludes_raw_inputs_outputs_and_errors():
    call = {'run_id': 'run_test', 'unit_id': 'unit_test', 'stage': 'extract', 'attempt': 1,
            'status': 'validation_error', 'started_at': '2026-01-01T00:00:00Z',
            'finished_at': '2026-01-01T00:00:01Z',
            'metadata': {'routing_role': 'extract', 'input_chars': 42, 'prompt': 'secret input'},
            'details': {'duration_ms': 1000, 'error': 'secret output',
                        'usage': {'input_tokens': 10, 'private_field': 'secret usage'}}}
    result = call_timeline([call])
    assert result[0]['stage'] == 'extract' and result[0]['status'] == 'validation_error'
    assert result[0]['usage'] == {'input_tokens': 10}
    assert 'secret' not in json.dumps(result)


def test_eval_frozen_fixture_and_independent_ab_states(tmp_path):
    cfg = AnalysisConfig(extract_model='small', extract_workers=2)
    first = run_eval('demo', tmp_path/'A', 'mock', cfg)
    second = run_eval('demo', tmp_path/'B', 'mock', AnalysisConfig(extract_model='strong'))
    assert first['source_snapshot_id'] == second['source_snapshot_id']
    assert first['fixture_digest'] == second['fixture_digest']
    assert first['initial_graph_version'] == second['initial_graph_version'] == 0
    # Every expectation of the synthetic demo holds: the requests, the alternative, each edit verified by the
    # check that tried it, and the report answering the second request.
    expected = demo_fixture()['expectations']
    assert first['expectations']['passed'] == second['expectations']['passed'] == (
        len(expected['events']) + len(expected['relations']))
    assert first['expectations']['failed'] == second['expectations']['failed'] == 0
    assert first['noop_check'] == {'status': 'noop', 'runner_calls': 0}
    assert (tmp_path/'A/state/state.sqlite').is_file() and (tmp_path/'B/state/state.sqlite').is_file()
    for path in (tmp_path/'A').glob('*.json'):
        assert path.stat().st_mode & 0o077 == 0


def test_eval_requires_explicit_live_consent(tmp_path):
    with pytest.raises(FlowError, match='동의'):
        run_eval('demo', tmp_path/'eval', 'codex', AnalysisConfig())
    assert not (tmp_path/'eval').exists()


def test_eval_refuses_nonempty_output(tmp_path):
    (tmp_path/'old').write_text('preserve')
    with pytest.raises(FlowError, match='새 디렉터리'):
        run_eval('demo', tmp_path, 'mock', AnalysisConfig())
    assert (tmp_path/'old').read_text() == 'preserve'


def test_fixture_cannot_enable_revision_path_reads():
    fixture = demo_fixture()
    fixture['records'][0]['provider'] = 'git'
    fixture['records'][0]['locator'] = {'kind':'commit_diff','root':'/private','commit':'abc'}
    records = fixture_records(fixture)
    assert records[0].locator['kind'] == 'frozen_fixture'


def test_eval_and_ops_cli(tmp_path, capsys):
    out = tmp_path/'eval'
    # Workers side by side see no unit before them.
    assert main(['eval', '--runner', 'mock', '--output', str(out), '--workers', '2',
                 '--extract-model', 'small']) == 0
    report = json.loads((out/'report.json').read_text())
    assert report['mode'] == 'synthetic_mock' and report['expectations']['failed'] == 0
    assert main(['demo','--path',str(tmp_path/'demo'),'--no-tui']) == 0
    assert main(['ops',str(tmp_path/'demo/sample-project')]) == 0
    assert 'billed_cost' in capsys.readouterr().out
    assert main(['ops',str(tmp_path/'demo/sample-project'),'--details']) == 0
    timeline = json.loads(capsys.readouterr().out)
    assert timeline['summary']['host_calls'] == len(timeline['calls'])
    # One story call per unit, and no other.
    assert timeline['calls'] and {call['stage'] for call in timeline['calls']} == {'story'}
    assert len(timeline['calls']) == len({call['unit_id'] for call in timeline['calls']})


def test_eval_preview_explains_full_prompt_without_model_calls(tmp_path, capsys):
    output = tmp_path / 'preview'
    assert main(['eval', '--fixture', 'demo', '--preview', '--output', str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['runner_calls'] == 0
    assert len(result['units']) >= 1
    page = (output / 'input-preview.html').read_text()
    assert 'system 지시문' in page
    assert '쓴 글 written' in page
    assert '작업 개요 work' in page
    assert '기존 사건 선택 이유' in page
    assert '입력이 만들어지는 순서' in page
    assert '출력 JSON Schema' in page
    task = json.loads((output / 'unit-01-task.json').read_text())
    assert task['data']['written'] and task['data']['work']
    assert html.escape(task['system'].splitlines()[0]) in page


@pytest.mark.parametrize('kwargs', [{'extract_workers':0},{'extract_workers':9},{'max_calls':0}])
def test_invalid_routing_configuration(kwargs):
    with pytest.raises(FlowError): AnalysisConfig(**kwargs).validate()


def test_cached_extra_evidence_is_visible_on_resume(laboratory):
    from contexttrail.schema import EvidenceValidator
    _, store, engine, records, make = laboratory
    records.append(make('old context', key='old'))
    engine.analyze(FixtureRunner)
    records.append(make(CASES[0][0], key='new'))
    fail_publish_once(engine)
    engine.analyze(FixtureRunner)
    unit = next(u for u in store.units() if u['status'] == 'extracted')
    old = records[0]
    validator = EvidenceValidator({'old': old}, {'old': [(1, 1)]})
    citation = {'source_id':'old','start_line':1,'end_line':1,'quote':'old context'}
    validator.citations([citation])
    cached = unit['result']
    cached['payload']['event_candidates'][0]['evidence'].append(citation)
    cached['evidence'].update(validator.evidence)
    deps = {**unit['dependencies'], 'old':old.content_hash}
    store.save_unit(unit['id'], unit['sources'], deps, 'extracted', cached)
    result = engine.analyze(FixtureRunner)
    assert result['status'] == 'complete', result
    assert result['reused_extractions'] == 1 and result['runner_calls'] == 0


def _fixture_record(source_id, role, content, call_id=None):
    return {'source_id': source_id, 'provider': 'codex', 'session_id': 'fixture-session', 'role': role,
            'content': content, 'cwd': '/fixture/project', 'worktree_id': 'wt_fixture',
            'tool_call_id': call_id, 'recorded_at': '2026-09-25T06:02:28Z',
            'locator': {'kind': 'jsonl', 'line': 1}}


def test_fixture_integrity_flags_dropped_result_and_unopened_session():
    from contexttrail.evaluation import fixture_integrity
    cut = fixture_records({'records': [
        _fixture_record('call-a', 'tool_call', 'Tool: exec\nexec_command({cmd:"pytest -q"})', 'a'),
        _fixture_record('call-b', 'tool_call', 'Tool: exec\ntools.write_stdin({session_id:65470,chars:""})', 'b'),
        _fixture_record('result-b', 'tool_result', '{"exit_code":0,"output":"205 passed"}', 'b')]})
    report = fixture_integrity(cut)
    assert report['tool_calls_without_result'] == ['call-a']
    assert report['unresolved_stdin_sessions'] == [{'source_id': 'call-b', 'session_id': 65470}]
    assert not report['complete'] and len(report['limitations']) == 2
    whole = fixture_records({'records': [
        _fixture_record('call-a', 'tool_call', 'Tool: exec\nexec_command({cmd:"pytest -q"})', 'a'),
        _fixture_record('result-a', 'tool_result', 'CHECK 0\n{"session_id":65470,"output":"..."}', 'a'),
        _fixture_record('call-b', 'tool_call', 'Tool: exec\ntools.write_stdin({session_id:65470,chars:""})', 'b'),
        _fixture_record('result-b', 'tool_result', '{"exit_code":0,"output":"205 passed"}', 'b')]})
    assert fixture_integrity(whole) == {'tool_calls_without_result': [], 'tool_results_without_call': [],
                                        'unresolved_stdin_sessions': [], 'complete': True, 'limitations': []}


def test_source_anchored_expectation_needs_no_title(monkeypatch, tmp_path):
    from contexttrail.evaluation import validate_expectations
    with pytest.raises(FlowError, match='title_contains 또는 source_ids'):
        validate_expectations({'events': [{'label': 'bare'}]}, set())
    data = demo_fixture()
    first = data['expectations']['events'][0]
    data['expectations'] = {'events': [{'label': 'first', 'source_ids': first['source_ids'],
                                        'status': first['status']}]}
    # A hand-cut fixture that lost the call of a tool result.
    data['records'] = [record for record in data['records'] if record['source_id'] != 'eval-s9']
    monkeypatch.setattr('contexttrail.evaluation.load_fixture', lambda _: data)
    report = run_eval('demo', tmp_path / 'eval', 'mock', AnalysisConfig())
    assert report['expectations']['passed'] == 1 and report['expectations']['failed'] == 0
    # The report says the fixture has a tool result without its call instead of hiding it.
    assert report['fixture_integrity']['tool_results_without_call'] == ['eval-s10']
    assert set(report['fixture_integrity']['limitations']) <= set(report['limitations'])


def test_forbidden_relation_flags_an_overclaimed_check(monkeypatch, tmp_path):
    from contexttrail.evaluation import validate_expectations
    with pytest.raises(FlowError, match='forbidden_relations'):
        validate_expectations({'events': [{'label': 'a', 'source_ids': ['s']}],
                               'forbidden_relations': [{'from': 'a', 'relation': 'causes'}]}, {'s'})
    data = demo_fixture()
    events = data['expectations']['events']
    data['expectations'] = {
        'events': events + [{'label': 'ghost', 'source_ids': ['eval-s0'], 'status': 'withdrawn'}],
        'relations': [{'from': 'edit_json', 'to': 'test_failed', 'relation': 'verifies'},
                      {'from': 'edit_json', 'to': 'test_failed', 'relation': ['follows', 'verifies']}],
        'forbidden_relations': [{'from': 'report', 'relation': 'verifies'},
                                {'from': 'edit_json', 'to': 'test_failed', 'relation': 'verifies'},
                                {'from': 'ghost', 'relation': 'verifies'}]}
    monkeypatch.setattr('contexttrail.evaluation.load_fixture', lambda _: data)
    report = run_eval('demo', tmp_path / 'eval', 'mock', AnalysisConfig())
    checks = [c for c in report['expectations']['checks'] if c['type'] in ('relation', 'forbidden_relation')]
    assert [c['passed'] for c in checks] == [True, True, True, False, False]
    assert checks[3]['matched_edge_ids']


def test_event_expectation_can_accept_any_of_several_sources(monkeypatch, tmp_path):
    from contexttrail.evaluation import validate_expectations
    with pytest.raises(FlowError, match='source_ids_any'):
        validate_expectations({'events': [{'label': 'a', 'source_ids_any': ['missing']}]}, {'s'})
    with pytest.raises(FlowError, match='source_ids'):
        validate_expectations({'events': [{'label': 'a', 'source_ids_any': []}]}, {'s'})
    data = demo_fixture()
    data['expectations'] = {'events': [
        {'label': 'either', 'source_ids_any': ['eval-s0', 'eval-s1'], 'status': 'asked'},
        {'label': 'neither', 'source_ids_any': ['eval-s5', 'eval-s6'], 'status': 'adopted'}]}
    monkeypatch.setattr('contexttrail.evaluation.load_fixture', lambda _: data)
    report = run_eval('demo', tmp_path / 'eval', 'mock', AnalysisConfig())
    assert [c['passed'] for c in report['expectations']['checks']] == [True, False]


def test_validation_error_kinds_keep_only_the_code_written_prefix():
    from contexttrail.evaluation import error_kinds
    message = ("quote not found uniquely in the cited lines: src_1:2-2 (0 matches); "
               "JSON schema error: events_to_add/0/title: '모델이 쓴 제목' is too long; "
               "no candidate evidence to fill an item left without evidence: edges_to_add tmp:e1")
    assert error_kinds(message) == {"quote not found uniquely in the cited lines", "JSON schema error",
                                    "no candidate evidence to fill an item left without evidence"}
    assert error_kinds("'모델이 쓴 제목' is too long") == set()
    assert error_kinds("events_to_add/0/title: 'a title' is too long") == set()  # a path is not a kind
    assert error_kinds("src_1:2-2") == set()
    call = {'stage': 'integrate', 'status': 'validation_error', 'metadata': {'routing_role': 'integrate'},
            'details': {'error': 'tool_record basis cites no tool call or result.'}}
    report = summarize_calls([call])
    assert report['by_role']['integrate']['validation_error_kinds'] == {'tool_record basis cites no tool call or result.': 1}
    call = {'stage': 'integrate', 'status': 'complete', 'metadata': {'routing_role': 'integrate'},
            'details': {'citation_normalization_audit': [
                {'mode': 'tool_evidence_restored_from_candidates', 'events': 1},
                {'mode': 'whitespace_normalized', 'source_id': 's1'}, {'mode': 'whitespace_normalized'}]}}
    assert summarize_calls([call])['by_role']['integrate']['normalization_modes'] == {
        'tool_evidence_restored_from_candidates': 1, 'whitespace_normalized': 2}


def test_expectation_breakdown_separates_unmatched_endpoints_from_wrong_relations():
    from contexttrail.evaluation import check_expectations
    def event(eid, source):
        return {"id": eid, "title": eid, "kind": "action", "status": "applied", "actor": "assistant",
                "evidence_ids": [f"x_{eid}"], "_source": source}
    events = [event("a", "s1"), event("b1", "s2"), event("b2", "s2"), event("c", "s3"), event("d", "s3x")]
    evidence = {f"x_{e['id']}": {"source_id": e["_source"]} for e in events}
    edges = [{"id": "r1", "from_event_id": "a", "to_event_id": "c", "relation": "motivates", "active": True}]
    graph = {"events": events, "edges": edges}
    expectations = {"events": [{"label": "A", "source_ids": ["s1"]}, {"label": "B", "source_ids": ["s2"]},
                               {"label": "C", "source_ids": ["s3"]}, {"label": "C2", "source_ids": ["s3"]},
                               {"label": "M", "source_ids": ["none"]}],
                    "relations": [{"from": "A", "relation": "motivates", "to": "C"},
                                  {"from": "A", "relation": "verifies", "to": "C"},
                                  {"from": "A", "relation": "motivates", "to": "B"}],
                    "forbidden_relations": [{"from": "B", "relation": "verifies"}]}
    result = check_expectations(graph, evidence, expectations)
    assert result["breakdown"] == {
        "events": {"passed": 2, "total": 5, "missing": 1, "split": 1, "merged": 1},
        "relations": {"passed": 1, "scored": 2, "unverifiable": 1},
        "forbidden_relations": {"passed": 0, "scored": 0, "unverifiable": 1}}
