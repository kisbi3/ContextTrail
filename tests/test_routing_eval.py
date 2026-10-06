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


def test_stage_models_are_distinct_and_integration_uses_quotes(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    # A long unrelated record must not be automatically copied into integration.
    records.append(make('UNCITED_SENTINEL-' * 500, key='uncited', role='assistant'))
    engine.config.extract_model = 'mock-small'
    engine.config.integrate_model = 'mock-strong'
    runners = []
    def factory():
        r = FixtureRunner(); runners.append(r); return r
    result = engine.analyze(factory)
    assert result['status'] == 'complete'
    calls = store.llm_calls()
    assert [c['metadata']['requested_model'] for c in calls] == ['mock-small', 'mock-strong']
    task = next(t for r in runners for t in r.tasks if t['stage'] == 'integrate')
    assert 'new_records' not in task['data']
    assert 'UNCITED_SENTINEL' not in str(task)
    assert task['data']['candidate_evidence']
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
            if task['stage'] == 'extract':
                with lock:
                    active += 1; peak = max(peak, active)
                barrier.wait(timeout=3)
                time.sleep(.025)
                with lock: active -= 1
            return super().run(task, schema, cancel)
    result = engine.analyze(Parallel)
    assert result['status'] == 'complete', result
    assert peak == 2
    assert [e['title'] for e in result['graph']['events']] == [CASES[0][2], CASES[1][2]]
    assert result['graph']['version'] == 2
    assert len({c['metadata']['worker_id'] for c in store.llm_calls() if c['stage'] == 'extract'}) == 2
    assert len({c['metadata']['worker_id'] for c in store.llm_calls() if c['stage'] == 'integrate'}) == 1
    assert engine.analyze(lambda: pytest.fail('noop'))['runner_calls'] == 0


def test_valid_revision_candidate_skips_extract_semantic_review(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[4][0]))
    engine.config.extract_model = 'small'
    engine.config.escalation_model = 'strong'
    engine.config.integrate_model = 'strong'
    result = engine.analyze(FixtureRunner)
    assert result['status'] == 'complete'
    calls = store.llm_calls()
    # Extraction does not escalate. A lone revision has nothing to revise, so the delta is reviewed.
    assert [c['metadata']['routing_role'] for c in calls] == ['extract', 'integrate', 'integrate_review']
    assert not store.units()[0]['result']['escalated']


def test_no_escalation_without_selected_model(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    assert engine.analyze(FixtureRunner)['runner_calls'] == 2
    assert not any(c['metadata']['routing_role'] == 'escalation' for c in store.llm_calls())


def test_each_stage_runs_at_its_reasoning_effort(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    class Effortful(FixtureRunner):
        effort = None
    assert (engine.config.extract_effort, engine.config.integrate_effort) == ('medium', 'medium')  # defaults
    engine.config.integrate_effort = 'high'
    assert engine.analyze(Effortful)['status'] == 'complete'
    assert [(c['metadata']['routing_role'], c['metadata']['reasoning_effort']) for c in store.llm_calls()] == [
        ('extract', 'medium'), ('integrate', 'high')]


def test_validation_failure_escalates_but_cannot_bypass_validation(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    engine.config.extract_model, engine.config.escalation_model = 'small', 'strong'
    class WrongSmall(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if self.model == 'small' and task['stage'] == 'extract':
                output['event_candidates'][0]['evidence'][0]['quote'] = 'invented'
            return output
    result = engine.analyze(WrongSmall)
    assert result['status'] == 'complete', result
    calls = store.llm_calls()
    assert [c['status'] for c in calls[:3]] == ['validation_error', 'validation_error', 'complete']
    assert calls[2]['metadata']['routing_reasons'] == ['bounded_validation_failure']
    assert result['runner_calls'] == 4


def test_infrastructure_failure_does_not_trigger_model_fallback(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    engine.config.escalation_model = 'strong'
    class Fail(FixtureRunner):
        def run(self, task, schema, cancel):
            raise FlowError('timeout/account failure')
    result = engine.analyze(Fail)
    assert result['status'] == 'failed'
    assert len(store.llm_calls()) == 1


def test_empty_output_does_not_trigger_extract_semantic_review(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make('감사합니다.'))
    engine.config.escalation_model = 'strong'
    result = engine.analyze(FixtureRunner)
    assert result['status'] == 'complete' and result['runner_calls'] == 1
    assert engine.analyze(lambda: pytest.fail('cached empty'))['status'] == 'noop'


def test_extraction_is_reused_after_post_delta_review_failure(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    first = engine.analyze(FixtureRunner)
    assert first['status'] == 'complete'
    records[0] = make(CASES[4][0])
    engine.config.escalation_model = 'strong'
    class FailReview(FixtureRunner):
        def run(self, task, schema, cancel):
            if task['stage'] == 'integrate' and task['data'].get('review_issues'):
                raise FlowError('review unavailable')
            return super().run(task, schema, cancel)
    failed = engine.analyze(FailReview)
    assert failed['status'] == 'failed' and store.units()[0]['status'] == 'extracted'
    second = engine.analyze(FixtureRunner)
    assert second['status'] == 'complete' and second['reused_extractions'] == 1
    assert [c['metadata']['routing_role'] for c in store.llm_calls(second['run_id'])] == ['integrate', 'integrate_review']


def test_pending_cache_is_invalidated_on_extract_model_change(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    engine.config.extract_model = 'small-A'
    class FailIntegration(FixtureRunner):
        def run(self, task, schema, cancel):
            if task['stage'] == 'integrate': raise FlowError('integration unavailable')
            return super().run(task, schema, cancel)
    engine.analyze(FailIntegration)
    engine.config.extract_model = 'small-B'
    second = engine.analyze(FixtureRunner)
    assert second['status'] == 'complete' and second['reused_extractions'] == 0
    assert second['runner_calls'] == 2
    # Integrated units remain untouched after subsequent configuration changes.
    engine.config.extract_model = 'small-C'
    assert engine.analyze(lambda: pytest.fail('integrated unchanged'))['status'] == 'noop'


def test_global_call_cap_across_parallel_workers(laboratory):
    _, store, engine, records, make = laboratory
    engine.config.extract_workers, engine.config.max_calls = 2, 1
    records.extend([make('none', key='a', session='one'), make('none', key='b', session='two')])
    first = engine.analyze(FixtureRunner)
    assert first['runner_calls'] == 1
    assert len(store.llm_calls()) == 1
    assert first['status'] in {'partial', 'failed'}
    engine.config.max_calls = 10
    second = engine.analyze(FixtureRunner)
    assert second['status'] == 'complete'
    assert second['runner_calls'] <= 1


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
    assert report['usage_missing_calls'] == 2 and report['provider_usage_sums'] is None
    assert report['billed_cost'] is None
    assert sum(group['calls'] for group in report['by_role'].values()) == report['host_calls']
    assert all({'calls', 'duration_ms', 'output_chars', 'input_tokens', 'output_tokens'} <= set(group)
               for group in report['by_role'].values())
    integrate = report['by_role']['integrate']['quote_chars']
    assert integrate['candidate_resolutions'] > 0 and integrate['change_attributions'] > 0


def test_output_quote_chars_counts_sizes_per_section():
    from contexttrail.analysis import output_quote_chars
    output = {'events_to_add': [{'evidence': [{'quote': 'abcd'}, {'quote': 'ef'}]}],
              'change_attributions': [{'evidence': [{'quote': 'xyz'}]}], 'limitations': ['not a quote']}
    assert output_quote_chars(output) == {'events_to_add': 6, 'change_attributions': 3}


def test_review_summary_counts_statuses_without_issue_text():
    from contexttrail.evaluation import review_summary
    graph = {'semantic_review_history': [
        {'status': 'not_needed', 'issues': [], 'resolutions': []},
        {'status': 'reviewed', 'issues': [{'signal': 'unlinked_revision', 'question': 'secret'}],
         'resolutions': [{'status': 'modified', 'reason': 'secret'}]}]}
    summary = review_summary(graph)
    assert summary == {'units': 2, 'statuses': {'not_needed': 1, 'reviewed': 1},
                       'resolutions': {'modified': 1}, 'signals': {'unlinked_revision': 1}}
    assert 'secret' not in str(summary)


def test_ops_count_why_the_quotes_did_not_match(laboratory):
    _, store, engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    class BadQuote(FixtureRunner):
        def run(self, task, schema, cancel):
            output = super().run(task, schema, cancel)
            if task["stage"] == "extract" and "repair" not in task:
                output["event_candidates"][0]["evidence"][0]["quote"] = "우선 JSON 파일로 저장하겠습니다"
            return output
    engine.analyze(lambda: BadQuote())
    assert summarize_calls(store.llm_calls())["quote_mismatch_categories"] == {"not_found": 1}


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
    cfg = AnalysisConfig(extract_model='small', integrate_model='strong', extract_workers=2)
    first = run_eval('demo', tmp_path/'A', 'mock', cfg)
    second = run_eval('demo', tmp_path/'B', 'mock', AnalysisConfig(extract_model='strong', integrate_model='strong'))
    assert first['source_snapshot_id'] == second['source_snapshot_id']
    assert first['fixture_digest'] == second['fixture_digest']
    assert first['initial_graph_version'] == second['initial_graph_version'] == 0
    assert first['expectations']['failed'] == second['expectations']['failed'] == 0
    assert first['noop_check'] == {'status': 'noop', 'runner_calls': 0}
    assert (tmp_path/'A/state/state.sqlite').is_file() and (tmp_path/'B/state/state.sqlite').is_file()
    for path in (tmp_path/'A').glob('*.json'):
        assert path.stat().st_mode & 0o077 == 0


def test_reuse_evidence_shrinks_the_integrate_quotes_and_keeps_the_graph(tmp_path):
    full = run_eval('demo', tmp_path/'full', 'mock', AnalysisConfig())
    reuse = run_eval('demo', tmp_path/'reuse', 'mock', AnalysisConfig(integrate_evidence='reuse'))
    assert full['first_run']['status'] == reuse['first_run']['status'] == 'complete'
    assert (reuse['expectations']['passed'], reuse['expectations']['failed']) == (
        full['expectations']['passed'], 0)
    quoted = lambda report, role: sum(report['ops']['by_role'][role].get('quote_chars', {}).values())
    for role in ('integrate', 'integrate_review'):
        assert quoted(reuse, role) < quoted(full, role)
    assert not {'events_to_add', 'candidate_resolutions', 'change_attributions'} & set(
        reuse['ops']['by_role']['integrate'].get('quote_chars', {}))
    # The same graph and the same citations, whatever the integrator had to write.
    def claims(name):
        data = json.loads((tmp_path/name/'flow.json').read_text())
        graph, evidence = data['graph'], data['evidence']
        titles = {event['id']: event['title'] for event in graph['events']}
        return ([(e['title'], e['kind'], e['status'], e['evidence_ids']) for e in graph['events']],
                sorted((titles.get(e['from_event_id'], e['from_event_id']),
                        titles.get(e['to_event_id'], e['to_event_id']), e['relation'], e['active'],
                        tuple(e['evidence_ids'])) for e in graph['edges']),
                sorted((item['source_id'], item['start_line'], item['end_line'], item['quote'])
                       for item in evidence.values()))
    assert claims('reuse') == claims('full')


def test_review_patch_shrinks_the_review_answer_and_keeps_the_graph(tmp_path):
    full = run_eval('demo', tmp_path/'full', 'mock', AnalysisConfig(review_output='full'))
    patch = run_eval('demo', tmp_path/'patch', 'mock', AnalysisConfig())
    assert full['first_run']['status'] == patch['first_run']['status'] == 'complete'
    assert (patch['expectations']['passed'], patch['expectations']['failed']) == (
        full['expectations']['passed'], 0)
    chars = lambda report: report['ops']['by_role']['integrate_review']['output_chars']
    assert 0 < chars(patch) < chars(full)
    assert full['semantic_review']['statuses'] == patch['semantic_review']['statuses']
    def claims(name):
        data = json.loads((tmp_path/name/'flow.json').read_text())
        graph, evidence = data['graph'], data['evidence']
        titles = {event['id']: event['title'] for event in graph['events']}
        return ([(e['title'], e['kind'], e['status'], e['evidence_ids']) for e in graph['events']],
                sorted((titles.get(e['from_event_id'], e['from_event_id']),
                        titles.get(e['to_event_id'], e['to_event_id']), e['relation'], e['active'],
                        tuple(e['evidence_ids'])) for e in graph['edges']),
                sorted((item['source_id'], item['start_line'], item['end_line'], item['quote'])
                       for item in evidence.values()))
    assert claims('patch') == claims('full')


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
    assert main(['eval', '--runner', 'mock', '--output', str(out), '--workers', '2',
                 '--extract-model', 'small', '--integrate-model', 'strong']) == 0
    report = json.loads((out/'report.json').read_text())
    assert report['mode'] == 'synthetic_mock' and report['expectations']['failed'] == 0
    assert main(['demo','--path',str(tmp_path/'demo'),'--no-tui']) == 0
    assert main(['ops',str(tmp_path/'demo/sample-project')]) == 0
    assert 'billed_cost' in capsys.readouterr().out
    assert main(['ops',str(tmp_path/'demo/sample-project'),'--details']) == 0
    timeline = json.loads(capsys.readouterr().out)
    assert timeline['summary']['host_calls'] == len(timeline['calls'])
    # The next unit's extraction may start while the first is integrated.
    stages = [call['stage'] for call in timeline['calls']]
    assert stages[0] == 'extract' and 'integrate' in stages


def test_eval_preview_explains_full_prompt_without_model_calls(tmp_path, capsys):
    output = tmp_path / 'preview'
    assert main(['eval', '--fixture', 'demo', '--preview', '--output', str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['runner_calls'] == 0
    assert len(result['units']) >= 1
    page = (output / 'input-preview.html').read_text()
    assert '공통 system 지시문' in page
    assert '새 기록 new_records' in page
    assert '기존 사건 선택 이유' in page
    assert '입력이 만들어지는 순서' in page
    assert '출력 JSON Schema' in page
    task = json.loads((output / 'unit-01-task.json').read_text())
    assert task['data']['new_records']
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
    class FailIntegration(FixtureRunner):
        def run(self, task, schema, cancel):
            if task['stage'] == 'integrate': raise FlowError('stop before publish')
            return super().run(task, schema, cancel)
    engine.analyze(FailIntegration)
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
    assert result['reused_extractions'] == 1 and result['runner_calls'] == 1


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
    monkeypatch.setattr('contexttrail.evaluation.load_fixture', lambda _: data)
    report = run_eval('demo', tmp_path / 'eval', 'mock', AnalysisConfig())
    assert report['expectations']['passed'] == 1 and report['expectations']['failed'] == 0
    # The synthetic demo has tool results without calls; the report says so instead of hiding it.
    assert report['fixture_integrity']['tool_results_without_call']
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
        'relations': [{'from': 'event2', 'to': 'event3', 'relation': 'verifies'},
                      {'from': 'event2', 'to': 'event3', 'relation': ['follows', 'verifies']}],
        'forbidden_relations': [{'from': 'event5', 'relation': 'verifies'},
                                {'from': 'event2', 'to': 'event3', 'relation': 'verifies'},
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
        {'label': 'either', 'source_ids_any': ['eval-s0', 'eval-s1'], 'status': 'adopted'},
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
