"""Pre-live adversarial contracts. Scripted doubles, not independent LLM/subagent runs."""
import copy
import json
import threading

import pytest

from projectflow.analysis import AnalysisConfig, Harness, _evidence_ids
from projectflow.demo import CASES, FixtureRunner
from projectflow.evaluation import run_eval, demo_fixture
from projectflow.model import Snapshot
from projectflow.runners.cli_runner import parse_codex_output, parse_claude_output
from projectflow.util import FlowError


def new_harness(store, records, assigned, config=None, graph=None):
    h = Harness(FixtureRunner(), {r.source_id: r for r in records},
                graph or store.graph(), store, config or AnalysisConfig(), threading.Event())
    return h, h.context(assigned)


@pytest.mark.parametrize('session,provider', [('different-session','claude'), ('session-1','codex')])
def test_tool_context_requires_provider_and_session(laboratory, session, provider):
    _, store, _, records, make = laboratory
    own = make('own result', role='tool_result')
    other = make('CROSS_CONTEXT_SENTINEL', key='other', role='tool_result',session=session,provider=provider)
    own.tool_call_id = other.tool_call_id = 'reused-call-id'
    records.extend([other, own])
    _, context = new_harness(store, records, [own])
    assert 'CROSS_CONTEXT_SENTINEL' not in json.dumps(context['context_only'])


def test_late_unit_manifest_can_read_immediately_previous_record(laboratory):
    _, store, _, records, make = laboratory
    records.extend(make(f'line {i}', key=f's{i}') for i in range(350))
    h, context = new_harness(store, records, [records[-1]])
    assert records[-2].source_id in h.allowed_record_ids
    assert len(context['manifest']['records']) <= 250


def test_boundary_previous_turn_is_visible_but_not_new_work(laboratory):
    _, store, _, records, make = laboratory
    proposal = make('BOUNDARY_PROPOSAL_SENTINEL', key='proposal', role='assistant')
    accepted = make('좋아. 그 방법으로 진행하자.', key='accept')
    records.extend([proposal, accepted])
    _, context = new_harness(store, records, [accepted])
    assert any(r['source_id']=='proposal' and r['context_only'] for r in context['context_only'])


def test_recent_events_are_not_ranked_by_random_run_uuid(laboratory):
    _, store, _, records, make = laboratory
    records.append(make('new turn'))
    graph=store.graph()
    graph['events'] = [dict(id=f'e{i}',title=f'event {i}',session_ids=['session-1'],
                           worktree_ids=[],evidence_ids=[],updated_in_run=('run_z' if i==0 else 'run_a'))
                       for i in range(30)]
    _, context=new_harness(store,records,records,AnalysisConfig(context_events=3),graph)
    assert [e['id'] for e in context['existing_events']] == ['e29','e28','e27']


def setup_review_reader(laboratory):
    _, store, engine, records, make=laboratory
    # Put the additional evidence farther away than the bounded previous-turn context.
    records.append(make('UNQUOTED_READ_SENTINEL',key='extra'))
    records.extend(make(f'neutral {i}',key=f'n{i}') for i in range(8))
    target=make(CASES[4][0],key='target')
    records.append(target)
    engine.config.extract_model='small'
    engine.config.escalation_model='strong'
    tasks=[]
    class Reader(FixtureRunner):
        fail_review=False
        def run(self, task, schema, cancel):
            tasks.append(copy.deepcopy(task))
            output=super().run(task,schema,cancel)
            if task['stage']=='extract' and self.model=='small' and 'evidence_rounds' not in task:
                output.update(status='needs_evidence',event_candidates=[],read_requests=[dict(
                    kind='read_records',ids=['extra'],start_line=1,end_line=1,query=None,unit_id=None)])
            if self.model=='strong':
                if self.fail_review:
                    raise FlowError('intentional review failure')
                # The extra raw text must reach the reviewer, not only its source ID/hash.
                assert 'UNQUOTED_READ_SENTINEL' in json.dumps(task,ensure_ascii=False)
            return output
    # Use _extract_unit directly to exclude 'extra' from assigned raw text.
    from projectflow.routing import RunnerPool
    unit={'id':'unit_target','sources':['target'],'dependencies':{},'status':'parsed','result':None}
    pool={r.source_id:r for r in records}
    snapshot=Snapshot(records)
    return store,engine,Reader,tasks,unit,pool,snapshot,RunnerPool


def test_valid_extraction_with_additional_reads_is_not_semantically_reextracted(laboratory):
    store,engine,Reader,tasks,unit,pool,snapshot,RunnerPool=setup_review_reader(laboratory)
    outcome=engine._extract_unit(unit,pool,store.graph(),snapshot.id,'review-run',
                                RunnerPool(Reader,engine.config),threading.Event(),[])
    assert not outcome['cached']['escalated']
    assert len(store.llm_calls())==2


def test_valid_extraction_is_reused_without_preintegration_review(laboratory):
    store,engine,Reader,tasks,unit,pool,snapshot,RunnerPool=setup_review_reader(laboratory)
    Reader.fail_review=True
    engine._extract_unit(unit,pool,store.graph(),snapshot.id,'run1',RunnerPool(Reader,engine.config),threading.Event(),[])
    saved=store.units()[0]
    assert saved['status']=='extracted'
    # Persist references and hashes, not uncited raw text, in the draft cache.
    assert 'UNQUOTED_READ_SENTINEL' not in json.dumps(saved['result'],ensure_ascii=False)
    Reader.fail_review=False
    outcome=engine._extract_unit(saved,pool,store.graph(),snapshot.id,'run2',RunnerPool(Reader,engine.config),threading.Event(),[])
    assert outcome['reused']
    assert len(store.llm_calls('run2'))==0


@pytest.mark.parametrize('message', ['corrupt JSONL record: fixture.jsonl:2',
                                    'Codex path attribution unclear, 1 records excluded: fixture.jsonl',
                                    'JSONL record over the size limit: fixture.jsonl:2'])
def test_incomplete_input_remains_partial_on_first_and_repeat(laboratory,message):
    _,store,engine,records,make=laboratory
    records.append(make(CASES[0][0]))
    engine.scan=lambda:Snapshot(list(records),[message])
    first=engine.analyze(FixtureRunner)
    assert first['status']=='partial'
    second=engine.analyze(lambda:pytest.fail('unchanged input must not call AI'))
    assert second['status']=='partial'
    assert second['runner_calls']==0


def test_evidence_export_includes_resolution_and_invalidation():
    graph={'events':[{'evidence_ids':['a']}],
           'edges':[{'evidence_ids':['b'],'invalidation':{'evidence_ids':['c']}}],
           'open_items':[{'evidence_ids':['d'],'resolution':{'evidence_ids':['e']}}]}
    assert set(_evidence_ids(graph))==set('abcde')


def test_malformed_expectations_fail_before_any_model_factory(monkeypatch,tmp_path):
    data=demo_fixture()
    data['expectations']={'events':[{'label':'broken_without_title'}]}
    monkeypatch.setattr('projectflow.evaluation.load_fixture',lambda _: data)
    made=[]
    def factory():
        made.append(1)
        return FixtureRunner()
    monkeypatch.setattr('projectflow.evaluation.FixtureRunner',factory)
    with pytest.raises(FlowError):
        run_eval('demo',tmp_path/'bad-eval','mock',AnalysisConfig())
    assert made==[]
    assert not (tmp_path/'bad-eval').exists()


@pytest.mark.parametrize('item_type',['collab_tool_call','image_generation','unexpected_future_tool'])
def test_codex_unknown_tool_event_does_not_pass(item_type):
    text='\n'.join(json.dumps(item) for item in [
        {'type':'item.completed','item':{'type':item_type}},
        {'type':'item.completed','item':{'type':'agent_message','text':'{"ok":true}'}},
        {'type':'turn.completed'}])
    with pytest.raises(FlowError):
        parse_codex_output(text)


def test_codex_partial_stream_cannot_publish():
    with pytest.raises(FlowError):
        parse_codex_output(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{"ok":true}'}}))


def test_claude_denied_tool_attempt_is_not_silent_success():
    with pytest.raises(FlowError):
        parse_claude_output(json.dumps({'type':'result','subtype':'success','is_error':False,
                                       'structured_output':{'ok':True},
                                       'permission_denials':[{'tool_name':'Bash','tool_input':{'command':'NO_EXECUTE'}}]}))


def test_malformed_needs_evidence_finishes_call_ledger(laboratory):
    _,store,engine,records,make=laboratory
    records.append(make(CASES[0][0]))
    class BadEnvelope(FixtureRunner):
        def run(self,task,schema,cancel):
            output=copy.deepcopy(super().run(task,schema,cancel))
            output.update(status='needs_evidence',snapshot_id='wrong-snapshot',read_requests=[])
            return output
    result=engine.analyze(BadEnvelope)
    assert result['status']=='failed'
    assert len(store.llm_calls())==2
    assert all(c['status']=='validation_error' and c['finished_at'] for c in store.llm_calls())


def test_reply_over_budget_does_not_authorize_unsent_citation(laboratory):
    _,store,_,records,make=laboratory
    target=make(CASES[0][0]);other=make('x'*3000,key='other')
    records.extend([target,other])
    class UnsentCitation(FixtureRunner):
        def run(self,task,schema,cancel):
            output=copy.deepcopy(super().run(task,schema,cancel))
            if 'repair' not in task:
                output.update(status='needs_evidence',event_candidates=[],read_requests=[{
                    'kind':'read_records','ids':['other'],'start_line':1,'end_line':1,'query':None,'unit_id':None}])
            else:
                output['event_candidates'][0]['evidence']=[{
                    'source_id':'other','start_line':1,'end_line':1,'quote':other.content}]
            return output
    from projectflow.schema import EvidenceValidator
    h=Harness(UnsentCitation(),{r.source_id:r for r in records},store.graph(),store,
              AnalysisConfig(read_chars=500),threading.Event())
    context=h.context([target])
    data={'unit_id':'budget-unit','snapshot_id':'snapshot-budget',
          'new_records':[h.provide(target.source_id)], **context}
    checker=EvidenceValidator(h.pool,h.provided)
    with pytest.raises(FlowError,match='not provided to the model'):
        h.task('extract',data,lambda o:checker.check_extraction(o,'budget-unit','snapshot-budget',store.graph()))
    assert 'other' not in h.provided


@pytest.mark.parametrize('field,value',[('recorded_at','2026-09-23T00:00:00Z'),('parent_record_id','changed-parent')])
def test_native_provenance_changes_invalidate_source_identity(laboratory,field,value):
    _,_,_,_,make=laboratory
    source=make('same body')
    revised=copy.deepcopy(source);setattr(revised,field,value)
    assert source.content_hash!=revised.content_hash


def test_v1_hash_upgrade_preserves_completed_units_without_calls(laboratory):
    _,store,engine,records,make=laboratory
    source=make(CASES[0][0])
    legacy=copy.deepcopy(source);legacy.pinned_hash=source.legacy_content_hash
    records.append(legacy)
    assert engine.analyze(FixtureRunner)['status']=='complete'
    graph_version=store.graph()['version']
    records[:]=[source]
    second=engine.analyze(lambda:pytest.fail('hash format upgrade must not call model'))
    assert second['status']=='noop' and second['runner_calls']==0
    assert store.graph()['version']==graph_version
    assert store.sources()['s1']['processed_hash']==source.content_hash


def test_v1_hash_upgrade_does_not_hide_changed_native_timestamp(laboratory):
    _,store,engine,records,make=laboratory
    original=make(CASES[0][0])
    legacy=copy.deepcopy(original);legacy.pinned_hash=original.legacy_content_hash
    records.append(legacy)
    assert engine.analyze(FixtureRunner)['status']=='complete'
    revised=copy.deepcopy(original);revised.recorded_at='2026-09-23T00:00:00Z'
    records[:]=[revised]
    second=engine.analyze(FixtureRunner)
    assert second['status']=='complete' and second['runner_calls']>0
    assert any(e['recorded_at']==revised.recorded_at for e in store.graph()['events'])


def test_scripted_walkthrough_has_four_calls_and_zero_real_models(tmp_path):
    import runpy
    from pathlib import Path
    walkthrough=runpy.run_path(str(Path(__file__).parents[1]/'scripts/prelive_walkthrough.py'))['walkthrough']
    report=walkthrough(tmp_path/'walkthrough')
    assert report['real_model_calls']==0 and report['independent_llm_reviewers']==0
    assert len(report['stages'])==4 and all(report['checks'].values())
    assert report['noop']=={'status':'noop','host_calls':0}


def subagent_metadata_fixture(tmp_path,session='session',meta=None):
    from pathlib import Path
    folder=tmp_path/'projects'/'project';folder.mkdir(parents=True)
    entries=[{'sessionId':session,'message':{'content':[{'type':'tool_use','id':'call'}]}},
             {'sessionId':session,'uuid':'launch-result','toolUseResult':{'agentId':'a'},
              'message':{'content':[{'type':'tool_result','tool_use_id':'call'}]}}]
    (folder/'parent.jsonl').write_text('\n'.join(json.dumps(x) for x in entries)+'\n')
    subagents=folder/session/'subagents';subagents.mkdir(parents=True)
    (subagents/'agent-a.jsonl').write_text('{}\n')
    path=subagents/'agent-a.meta.json'
    path.write_text(json.dumps(meta if meta is not None else {'toolUseId':'call','description':'safe'}))
    return folder.parent,path


def test_subagent_metadata_symlink_is_not_read(tmp_path):
    from projectflow.sources.local import _claude_subagent_links
    root,meta=subagent_metadata_fixture(tmp_path)
    secret=tmp_path/'unrelated.json';secret.write_text('{"toolUseId":"call","description":"SECRET_SENTINEL"}')
    meta.unlink();meta.symlink_to(secret)
    assert _claude_subagent_links(root)=={}


def test_subagent_session_id_cannot_escape_root(tmp_path):
    from projectflow.sources.local import _claude_subagent_links
    root,_=subagent_metadata_fixture(tmp_path,session='../../outside')
    assert _claude_subagent_links(root)=={}


def test_subagent_metadata_nonobject_is_skipped(tmp_path):
    from projectflow.sources.local import _claude_subagent_links
    root,_=subagent_metadata_fixture(tmp_path,meta=[])
    assert _claude_subagent_links(root)=={}


def test_subagent_metadata_size_is_bounded(tmp_path):
    from projectflow.sources.local import _claude_subagent_links
    root,_=subagent_metadata_fixture(tmp_path,meta={'toolUseId':'call','description':'x'*130000})
    assert _claude_subagent_links(root)=={}


def test_safe_subagent_metadata_still_connects(tmp_path):
    from projectflow.sources.local import _claude_subagent_links
    root,_=subagent_metadata_fixture(tmp_path)
    result=_claude_subagent_links(root)
    assert len(result)==1 and next(iter(result.values()))['parent_tool_call_id']=='call'
