#!/usr/bin/env python3
"""Synthetic, scripted stage handoff walkthrough. ZERO real model/subagent calls.

Usage after installing Project Flow:
  python scripts/prelive_walkthrough.py --output /tmp/pf-prelive-walkthrough

Inputs/outputs here are intentionally captured because ALL source data is synthetic.
Normal product Ops logging remains metadata-only. Never substitute private logs here.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from projectflow.analysis import AnalysisConfig, Engine, _evidence_ids
from projectflow.demo import CASES, FixtureRunner
from projectflow.evaluation import summarize_calls
from projectflow.git_context import Scope
from projectflow.model import Snapshot, SourceRecord
from projectflow.store import Store
from projectflow.util import FlowError, digest, dumps, private_dir


def walkthrough(output: Path) -> dict:
    output = output.expanduser()
    if output.is_symlink():
        raise FlowError('symlink output is not allowed')
    output = output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FlowError('Use a new or empty output directory.')
    private_dir(output)
    scope = Scope(output, None, None, '', [output], output/'state', 'synthetic-prelive')
    store = Store(scope.state_dir, scope.id)
    # This walks the extract/integrate handoff. The lone revision below has nothing to revise,
    # which would add a delta review; that path is covered by the Studio tests instead.
    config = AnalysisConfig(extract_model='scripted-small', integrate_model='scripted-integrator',
                            escalation_model='scripted-reviewer', max_calls=8, semantic_review=False,
                            integrate_output='full')
    engine = Engine(scope, store, config)
    def record(key: str, content: str, minute: int) -> SourceRecord:
        return SourceRecord(key,'claude','synthetic-session','user',content,
                            {'kind':'synthetic_fixture'}, recorded_at=f'2026-09-23T00:{minute:02}:00Z',
                            cwd='/synthetic/project',worktree_id='synthetic-wt')
    records = [record('additional-context','UNQUOTED_READ_SENTINEL: 별도 관찰 기록. 실제 사용자 자료가 아님.',0)]
    records.extend(record(f'neutral-{i}',f'합성 중립 문장 {i}',i+1) for i in range(8))
    engine.scan = lambda: Snapshot(copy.deepcopy(records))
    # Establish an already-processed previous slice, using no semantic model.
    setup = engine.analyze(FixtureRunner)
    if setup['status'] != 'complete':
        raise FlowError('Synthetic setup failed.')
    records.append(record('target',CASES[4][0],10))
    captured = []
    class ScriptedStage(FixtureRunner):
        name = 'scripted-handoff-test-double'
        def run(self, task, schema, cancel):
            response = copy.deepcopy(super().run(task,schema,cancel))
            if task['stage']=='extract' and self.model=='scripted-small' and not task.get('evidence_rounds'):
                response.update(status='needs_evidence',event_candidates=[],read_requests=[{
                    'kind':'read_records','ids':['additional-context'],'start_line':1,'end_line':1,
                    'query':None,'unit_id':None}])
            if task['stage']=='integrate' and 'repair' not in task:
                # Deliberate invalid quote: real validator must reject and request one repair.
                response['events_to_add'][0]['evidence'][0]['quote']='INTENTIONALLY_INVALID_QUOTE'
            captured.append({'requested_role_model':self.model,'stage':task['stage'],
                             'task':copy.deepcopy(task),'schema':schema,'response':copy.deepcopy(response)})
            return response
    result = engine.analyze(ScriptedStage)
    calls = store.llm_calls(result['run_id'])
    if result['status']!='complete' or len(calls)!=4:
        raise FlowError(f'Scripted walkthrough expected four calls; got {len(calls)} ({[c["metadata"].get("routing_role") for c in calls]}), status={result["status"]}.')
    repeat = engine.analyze(lambda: (_ for _ in ()).throw(FlowError('Unexpected repeated model invocation.')))
    if repeat['status']!='noop' or repeat['runner_calls']!=0:
        raise FlowError('No-op check failed.')
    private_dir(output/'calls')
    for index,item in enumerate(captured,1):
        for key in ('task','schema','response'):
            path=output/'calls'/f'{index:02}-{item["stage"]}-{key}.json'
            path.write_text(dumps(item[key],pretty=True),encoding='utf-8'); path.chmod(0o600)
    graph=store.graph()
    evidence=store.evidence_many(_evidence_ids(graph))
    report={
        'mode':'synthetic_scripted_walkthrough_NOT_live_LLM_NOT_subagent_review',
        'real_model_calls':0,'independent_llm_reviewers':0,
        'source_snapshot_id':Snapshot(records).id,
        'setup_host_calls':setup['runner_calls'],
        'walkthrough':{k:v for k,v in result.items() if k!='graph'},
        'stages':[{'stage':c['stage'],'role':c['metadata']['routing_role'],
                   'status':c['status'],'read_round':c['metadata']['read_round'],
                   'repair_round':c['metadata']['repair_round']} for c in calls],
        'ops':summarize_calls(calls),'noop':{'status':repeat['status'],'host_calls':repeat['runner_calls']},
        'checks':{'additional_read_forwarded_to_extraction':any('UNQUOTED_READ_SENTINEL' in dumps(c['task']) for c in captured if c['stage']=='extract'),'invalid_quote_rejected':True,
                  'bounded_repair_used':True,'only_final_valid_graph_published':graph['version']==2,
                  'all_referenced_evidence_exported':set(_evidence_ids(graph))<=set(evidence)},
        'caveat':'Test doubles are programmed to produce these candidates. This is not a measurement of LLM semantic accuracy.'}
    for name,data in {'report.json':report,'ops-calls.json':calls,'flow.json':{'graph':graph,'evidence':evidence}}.items():
        path=output/name;path.write_text(dumps(data,pretty=True),encoding='utf-8');path.chmod(0o600)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=walkthrough(args.output)
        print(dumps({k:result[k] for k in ('mode','real_model_calls','stages','noop','checks')},pretty=True))
    except (FlowError,OSError) as exc:
        raise SystemExit(str(exc))
