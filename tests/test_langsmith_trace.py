import json
from datetime import datetime, timezone

import pytest

from contexttrail.analysis import AnalysisConfig, Engine
from contexttrail.demo import FixtureRunner
from contexttrail.langsmith_trace import LangSmithTracer
from contexttrail.util import FlowError


def test_langsmith_requires_key_before_model_call(monkeypatch):
    monkeypatch.delenv('LANGSMITH_API_KEY', raising=False)
    with pytest.raises(FlowError, match='LANGSMITH_API_KEY'):
        LangSmithTracer(client_factory=lambda **_: None)


@pytest.mark.parametrize('include_content', [False, True])
def test_langsmith_trace_content_is_explicit(monkeypatch, include_content):
    monkeypatch.setenv('LANGSMITH_API_KEY', 'synthetic-key')
    seen = {}
    class FakeClient:
        def __init__(self, **kwargs):
            seen['client_options'] = kwargs
        def create_run(self, **kwargs):
            seen['run'] = kwargs
        def flush(self, **kwargs):
            seen['flushed'] = True
        def close(self):
            pass
    tracer = LangSmithTracer('synthetic-project', include_content=include_content,
                            client_factory=FakeClient)
    stamp = datetime.now(timezone.utc)
    tracer.record(call_id='llm_' + '1' * 32, run_id='run_test', unit_id='unit_test', stage='extract',
                  status='complete', task={'system': 'secret prompt'}, schema={'type': 'object'},
                  output={'result': 'secret response'},
                  metadata={'input_digest': 'digest', 'input_chars': 100, 'requested_model': None},
                  details={'output_digest': 'output_digest', 'output_chars': 30,
                           'usage': {'input_tokens': 10, 'output_tokens': 5}},
                  started_at=stamp, finished_at=stamp)
    assert seen['flushed'] and seen['run']['project_name'] == 'synthetic-project'
    assert seen['run']['outputs']['usage_metadata']['total_tokens'] == 15
    serialized = json.dumps(seen['run'], default=str)
    assert ('secret prompt' in serialized) is include_content
    assert ('secret response' in serialized) is include_content
    assert 'synthetic-key' not in serialized


@pytest.mark.parametrize('trace_fails', [False, True])
def test_analysis_records_extract_and_integrate_in_langsmith(laboratory, monkeypatch, trace_fails):
    scope, store, lab_engine, records, make = laboratory
    records.append(make('우선 JSON 파일로 저장하자.'))
    captured = []
    class FakeTracer:
        def __init__(self, *args, **kwargs):
            pass
        def record(self, **kwargs):
            captured.append((kwargs['stage'], kwargs['status']))
            if trace_fails:
                raise RuntimeError('synthetic transport failure')
    monkeypatch.setattr('contexttrail.analysis.LangSmithTracer', FakeTracer)
    engine = Engine(scope, store, AnalysisConfig(langsmith_enabled=True))
    engine.scan = lab_engine.scan
    result = engine.analyze(FixtureRunner)
    assert result['status'] in {'complete', 'partial'}
    assert ('extract', 'complete') in captured
    assert ('integrate', 'complete') in captured
    expected = 'failed' if trace_fails else 'sent'
    assert all(c['details'].get('langsmith_trace') == expected for c in store.llm_calls())


@pytest.mark.parametrize('include_content', [False, True])
def test_run_trace_follows_the_real_nodes_and_hides_text_unless_asked(laboratory, monkeypatch, include_content):
    import langsmith
    from langchain_core.tracers.langchain import wait_for_all_tracers
    from contexttrail.demo import CASES
    scope, store, lab_engine, records, make = laboratory
    records.append(make(CASES[0][0]))
    monkeypatch.setenv('LANGSMITH_API_KEY', 'synthetic-key')
    sent = {}
    def create_run(self, name, inputs, run_type, **kwargs):
        sent.setdefault(str(kwargs.get('id')), {}).update(name=name, inputs=inputs, run_type=run_type, **kwargs)
    def update_run(self, run_id, **kwargs):
        sent.setdefault(str(run_id), {}).update({k: v for k, v in kwargs.items() if v is not None})
    # Capture at the SDK boundary, after any metadata-only filtering has run.
    monkeypatch.setattr(langsmith.Client, 'create_run', create_run)
    monkeypatch.setattr(langsmith.Client, 'update_run', update_run)
    engine = Engine(scope, store, AnalysisConfig(langsmith_enabled=True, langsmith_include_content=include_content,
                                                 langsmith_project='synthetic-project'))
    engine.scan = lab_engine.scan
    assert engine.analyze(FixtureRunner)['status'] == 'complete'
    wait_for_all_tracers()
    names = {run['name'] for run in sent.values()}
    assert {'ContextTrail analysis', 'scan_sources', 'extract_model_and_validate', 'build_extract_request',
            'validate_extract_claims', 'publish_result', 'ContextTrail extract'} <= names
    # Each model call sits under the node that made it, in the same trace and project.
    call = next(run for run in sent.values() if run['name'] == 'ContextTrail extract')
    parent = sent[str(call['parent_run_id'])]
    assert parent['name'] == 'extract_model_and_validate' and call['trace_id'] == parent['trace_id']
    assert call['project_name'] == 'synthetic-project'
    publish = next(run for run in sent.values() if run['name'] == 'publish_result')
    assert publish['outputs']['graph_summary']['events'] == 1
    serialized = json.dumps(sent, default=str, ensure_ascii=False)
    assert (CASES[0][0] in serialized) is include_content
    assert ('JSON 저장 채택' in serialized) is include_content
    assert 'synthetic-key' not in serialized


def test_metadata_only_keeps_counts_and_codes_but_drops_text():
    from contexttrail.langsmith_trace import metadata_only, scrub_error
    state = {'mode': 'cli', 'unit_id': 'unit_' + 'a' * 16, 'title': 'secret title', 'actor': 'jaesung',
             'events': [{'actor': 'user'}, {'actor': 'secretname'}],
             'event_candidates': [{'id': 'tmp:secret_plan', 'kind': 'action', 'status': 'applied',
                                   'summary': 'secret summary'}],
             'evidence': {f'evi_{n:032x}': {'quote': 'secret quote'} for n in range(20)},
             'limitations': ['secret limitation'], 'requested_model': 'gpt-5.1-codex'}
    cleaned = metadata_only(state)
    assert 'secret' not in json.dumps(cleaned, ensure_ascii=False) and 'jaesung' not in json.dumps(cleaned)
    assert cleaned['event_candidates'] == {'count': 1, 'by_kind': {'action': 1}, 'by_status': {'applied': 1}}
    assert cleaned['evidence'] == {'count': 20} and cleaned['limitations'] == {'count': 1}
    assert cleaned['requested_model'] == 'gpt-5.1-codex' and 'actor' not in cleaned
    assert cleaned['events'] == {'count': 2, 'by_actor': {'user': 1}}
    assert scrub_error("Traceback\nFlowError: title: 'secret' is too long: tmp:secret_plan") == \
        'FlowError: title: … is too long: tmp:…'
