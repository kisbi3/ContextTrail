import json
from pathlib import Path

from projectflow.analysis import AnalysisConfig, Engine
from projectflow.demo import FixtureRunner
from projectflow.git_context import Scope
from projectflow.sources import collect_logs
from projectflow.sources.local import parse_codex
from projectflow.store import Store


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))


def meta(cwd, sid='session'):
    return {'type':'session_meta','payload':{'id':sid,'cwd':str(cwd)}}


def msg(text):
    return {'type':'response_item','payload':{'type':'message','role':'user',
            'content':[{'type':'input_text','text':text}]}}


def call(workdir, cid='call'):
    return {'type':'response_item','payload':{'type':'function_call','name':'exec_command',
            'call_id':cid,'arguments':json.dumps({'cmd':'echo only-a-log', 'workdir':str(workdir)})}}


def result(text, cid='call'):
    return {'type':'response_item','payload':{'type':'function_call_output','call_id':cid,'output':text}}


def test_calendar_folders_do_not_determine_project_or_llm_inputs(tmp_path):
    folder=tmp_path/'project';folder.mkdir()
    other=tmp_path/'project-backup';other.mkdir()
    home=tmp_path/'codex'
    write(home/'sessions/2020/01/01/old.jsonl',[meta(folder,'old'),msg('IN_SCOPE_OLD')])
    write(home/'sessions/2026/09/23/new.jsonl',[meta(other,'new'),msg('DO_NOT_SEND_OTHER_PROJECT')])
    write(home/'archived_sessions/archived.jsonl',[meta(folder,'archived'),msg('IN_SCOPE_ARCHIVED')])
    scope=Scope.resolve(folder)
    store=Store(scope.state_dir,scope.id)
    engine=Engine(scope,store,AnalysisConfig(codex_home=home,claude_home=tmp_path/'missing'))
    snap=engine.scan()
    assert {r.content for r in snap.records} == {'IN_SCOPE_OLD','IN_SCOPE_ARCHIVED'}
    assert len([f for f in snap.files if f['selection']['status']=='excluded']) == 1
    runner=FixtureRunner()
    assert engine.analyze(lambda:runner)['status']=='complete'
    assert 'DO_NOT_SEND_OTHER_PROJECT' not in str(runner.tasks)
    assert 'project-backup' not in str(runner.tasks)


def test_session_switch_into_scope_not_discarded_by_first_metadata(tmp_path):
    root=tmp_path/'app';root.mkdir()
    path=tmp_path/'rollout.jsonl'
    rows=[meta(tmp_path/'outside'),msg('outside'),
          {'type':'turn_context','payload':{'cwd':str(root)}},msg('inside'),
          {'type':'turn_context','payload':{'cwd':str(tmp_path/'outside')}},msg('outside-again')]
    write(path,rows)
    snap=parse_codex(path,Scope.resolve(root))
    assert [r.content for r in snap.records] == ['inside']
    assert snap.files[0]['selection']['scope_decisions'] == {'selected':1,'outside_scope':2,'unattributed':0}


def test_explicit_tool_workdir_and_result_follow_same_origin(tmp_path):
    root=tmp_path/'app';root.mkdir()
    path=tmp_path/'rollout.jsonl'
    write(path,[meta(root),call(tmp_path/'outside','out'),result('OUTSIDE_OUTPUT','out'),
                call(root,'in'),{'type':'turn_context','payload':{'cwd':str(tmp_path/'outside')}},
                result('INSIDE_OUTPUT','in')])
    records=parse_codex(path,Scope.resolve(root)).records
    assert [r.tool_call_id for r in records] == ['in','in']
    assert records[-1].content == 'INSIDE_OUTPUT'


def test_relative_workdir_cannot_fall_back_to_project(tmp_path):
    root=tmp_path/'app';root.mkdir()
    path=tmp_path/'rollout.jsonl'
    write(path,[meta(root),call('../secret'),result('DO_NOT_SEND')])
    snap=parse_codex(path,Scope.resolve(root))
    assert not snap.records
    assert snap.files[0]['selection']['scope_decisions']['outside_scope'] == 2


def test_missing_cwd_not_inferred_from_filename_or_text(tmp_path):
    root=tmp_path/'app';root.mkdir()
    path=tmp_path/'app/rollout-app.jsonl'
    write(path,[{'type':'session_meta','payload':{'id':'unknown'}},msg(str(root))])
    snap=parse_codex(path,Scope.resolve(root))
    assert not snap.records
    assert snap.files[0]['selection']['scope_decisions']['unattributed'] == 1


def test_invalid_turn_cwd_clears_stale_scope(tmp_path):
    root=tmp_path/'app';root.mkdir()
    path=tmp_path/'rollout.jsonl'
    write(path,[meta(root),msg('yes'),{'type':'turn_context','payload':{'cwd':'relative-unknown'}},msg('NO')])
    snap=parse_codex(path,Scope.resolve(root))
    assert [r.content for r in snap.records] == ['yes']
    assert any('attribution unclear' in s for s in snap.limitations)


def test_subfolder_does_not_expand_to_whole_repository(tmp_path):
    root=tmp_path/'project';sub=root/'component';sub.mkdir(parents=True)
    path=tmp_path/'rollout.jsonl'
    write(path,[meta(root),msg('WHOLE_PROJECT'),call(sub),result('SUBFOLDER_ONLY')])
    records=parse_codex(path,Scope.resolve(sub)).records
    assert [r.role for r in records] == ['tool_call','tool_result']
    assert records[-1].content == 'SUBFOLDER_ONLY'
