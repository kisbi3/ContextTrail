import os
import json
import sys
import threading
import time
import pytest
from contexttrail.runners.cli_runner import (CLIRunner, claude_reported_model, execute, newest_in_family,
                                            parse_codex_output, parse_claude_output)
from contexttrail.schema import EXTRACT_SCHEMA, DELTA_SCHEMA
from contexttrail.util import FlowError, Cancelled


def test_codex_structured_wrapper():
    text='\n'.join(json.dumps(x) for x in [
        {'type':'item.completed','item':{'type':'agent_message','text':'{"ok":true}'}},
        {'type':'turn.completed','usage':{'input_tokens':12}}])
    value,usage=parse_codex_output(text)
    assert value=={'ok':True} and usage['input_tokens']==12


def test_live_schemas_type_constant_properties():
    # Codex's structured-output endpoint rejects const-only object properties.
    def visit(schema):
        if isinstance(schema, dict):
            if 'const' in schema:
                assert schema.get('type') == ('boolean' if isinstance(schema['const'], bool) else 'string')
            for value in schema.values():
                visit(value)
        elif isinstance(schema, list):
            for value in schema:
                visit(value)

    visit(EXTRACT_SCHEMA)
    visit(DELTA_SCHEMA)
    assert "oneOf" not in json.dumps(EXTRACT_SCHEMA)
    read_schema = EXTRACT_SCHEMA["properties"]["read_requests"]["items"]
    assert read_schema["type"] == "object"
    assert set(read_schema["required"]) == {
        "kind", "ids", "start_line", "end_line", "query", "unit_id"}


@pytest.mark.parametrize('text',['not json','[]','{"type":"error"}','{"type":"item.started","item":{"type":"command_execution"}}'])
def test_codex_invalid_or_forbidden_event(text):
    with pytest.raises(FlowError): parse_codex_output(text)


def test_claude_structured_wrapper_and_unknown_usage():
    assert parse_claude_output('{"structured_output":{"ok":true}}')==({'ok':True},None)
    assert parse_claude_output('{"result":"{\\"ok\\":true}"}')[0]=={'ok':True}
    with pytest.raises(FlowError): parse_claude_output('{"is_error":true}')


def test_codex_argv_has_read_only_and_disabled_tools():
    runner=CLIRunner('codex'); runner.executable='/usr/bin/codex'
    runner.features={'shell_tool','unified_exec','hooks','plugins'}
    args=runner.build_cli({})
    assert args[-1]=='-' and '--ephemeral' in args
    assert args[args.index('--sandbox')+1]=='read-only'
    for feature in runner.features:
        assert any(args[i:i+2]==['--disable',feature] for i in range(len(args)))
    assert not any('bypass' in a or 'danger-full-access' in a for a in args)


def test_claude_argv_disables_builtin_mcp_and_config():
    runner=CLIRunner('claude'); runner.executable='/usr/bin/claude'
    args=runner.build_cli({})
    assert args[args.index('--tools')+1]==''
    assert args[args.index('--setting-sources')+1]==''
    assert '--safe-mode' in args and '--restricted' in args and '--strict-mcp-config' in args
    assert '--no-session-persistence' in args


def test_runner_always_names_its_model_and_passes_effort():
    codex=CLIRunner('codex',effort='high'); codex.executable='/usr/bin/codex'
    args=codex.build_cli({})
    assert args[args.index('--model')+1]=='gpt-sol' and args[-1]=='-'
    assert any(args[i:i+2]==['-c','model_reasoning_effort="high"'] for i in range(len(args)))
    claude=CLIRunner('claude',effort='medium'); claude.executable='/usr/bin/claude'
    args=claude.build_cli({})
    assert args[args.index('--model')+1]=='sonnet' and args[args.index('--effort')+1]=='medium'
    assert CLIRunner('codex',model='chosen').model=='chosen'
    plain=CLIRunner('claude'); plain.executable='/usr/bin/claude'
    assert '--effort' not in plain.build_cli({})
    with pytest.raises(FlowError): CLIRunner('codex',effort='extreme')


def test_codex_family_name_resolves_to_the_newest_listed_version():
    catalog=json.dumps({'models':[{'slug':'gpt-6-sol','visibility':'list'},{'slug':'gpt-6.1-sol','visibility':'list'},
        {'slug':'gpt-5.6-sol','visibility':'list'},{'slug':'gpt-7-sol','visibility':'hide'},
        {'slug':'gpt-6.2-luna','visibility':'list'},{'slug':'gpt-6.10-solar','visibility':'list'}]})
    assert newest_in_family(catalog,'sol')=='gpt-6.1-sol'
    assert newest_in_family(catalog,'luna')=='gpt-6.2-luna'
    assert newest_in_family(catalog,'astra') is None and newest_in_family('not json','sol') is None
    seen=[]
    def run(command):
        seen.append(command); return 0, catalog, ''
    codex=CLIRunner('codex'); codex.executable='/usr/bin/codex'
    assert codex._resolve_family(run)=='gpt-6.1-sol' and seen==[['/usr/bin/codex','debug','models','--bundled']]
    pinned=CLIRunner('codex',model='gpt-6-sol'); pinned.executable='/usr/bin/codex'
    assert pinned._resolve_family(run)=='gpt-6-sol' and len(seen)==1
    claude=CLIRunner('claude'); claude.executable='/usr/bin/claude'
    assert claude._resolve_family(run)=='sonnet' and len(seen)==1
    with pytest.raises(FlowError,match='gpt-<버전>-sol'):
        codex._resolve_family(lambda command: (1,'',''))


def test_claude_reports_the_model_that_answered():
    text=json.dumps({'structured_output':{'ok':True},'modelUsage':{
        'claude-haiku-4-5':{'outputTokens':12},'claude-sonnet-5':{'outputTokens':900}}})
    assert claude_reported_model(text)=='claude-sonnet-5'
    assert claude_reported_model('{"structured_output":{}}') is None
    assert claude_reported_model('not json') is None


def test_missing_sandbox_cannot_fall_back(monkeypatch,tmp_path):
    monkeypatch.setattr('contexttrail.runners.cli_runner.sys.platform','linux')
    monkeypatch.setattr('contexttrail.runners.cli_runner.shutil.which',lambda name: None)
    runner=CLIRunner('codex');runner.executable='/usr/bin/codex'
    with pytest.raises(FlowError,match='bubblewrap'):
        runner.sandbox_command(tmp_path,tmp_path,['codex'])


def test_missing_macos_sandbox_cannot_fall_back(monkeypatch,tmp_path):
    monkeypatch.setattr('contexttrail.runners.cli_runner.sys.platform','darwin')
    monkeypatch.setattr('contexttrail.runners.cli_runner.shutil.which',lambda name: None)
    runner=CLIRunner('codex');runner.executable='/usr/bin/codex'
    with pytest.raises(FlowError,match='sandbox-exec'):
        runner.sandbox_command(tmp_path,tmp_path,['codex'])


def test_macos_profile_excludes_other_home_files(tmp_path, monkeypatch):
    monkeypatch.setattr('contexttrail.runners.cli_runner.sys.platform','darwin')
    credential=tmp_path/'private-auth.json';credential.write_text('synthetic')
    monkeypatch.setenv('CODEX_HOME',str(tmp_path))
    work=tmp_path/'work';output=tmp_path/'out'
    work.mkdir();output.mkdir()
    runner=CLIRunner('codex');runner.executable='/usr/bin/codex'
    profile=runner._macos_profile(work,output,work.parent/'home')
    assert str(credential) not in profile
    assert '(subpath "'+str(tmp_path.resolve())+'")' not in profile
    assert '(subpath "'+str(work.resolve())+'")' in profile
    assert '(subpath "'+str(output.resolve())+'")' in profile
    assert '(allow mach-lookup (global-name-regex #"^com\\.apple\\."))' in profile
    assert '(allow mach-lookup)' not in profile


def test_macos_codex_runtime_reads_only_matching_native_package(tmp_path, monkeypatch):
    monkeypatch.setattr('contexttrail.runners.cli_runner.platform.machine', lambda: 'x86_64')
    monkeypatch.setattr('contexttrail.runners.cli_runner.shutil.which', lambda name: None)
    vendor = tmp_path / 'node_modules' / '@openai'
    cli = vendor / 'codex'
    binary = cli / 'bin' / 'codex.js'
    binary.parent.mkdir(parents=True)
    binary.write_text('synthetic')
    native = vendor / 'codex-darwin-x64'
    native.mkdir()
    unrelated = vendor / 'other-package'
    unrelated.mkdir()
    runner = CLIRunner('codex')
    runner.executable = str(binary)
    roots = runner._macos_runtime_roots()
    assert cli in roots and native in roots
    assert unrelated not in roots and vendor not in roots
    native.rmdir()
    native.symlink_to(unrelated)
    assert native not in runner._macos_runtime_roots()


@pytest.mark.parametrize('name',['codex','claude'])
def test_macos_cli_paths_use_real_private_workdir(name,tmp_path):
    runner=CLIRunner(name);runner.executable='/usr/bin/'+name
    work=str((tmp_path/'work').resolve());output=str((tmp_path/'out').resolve())
    command=runner.build_cli({},work=work,output=output)
    assert any(work+'/system.md' in part for part in command)
    assert '/work/system.md' not in command
    if name=='codex':
        assert work+'/schema.json' in command
        assert output+'/result.json' in command


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox-exec only')
def test_macos_sandbox_denies_sibling_files_and_allows_output(tmp_path, monkeypatch):
    secret=tmp_path/'synthetic-secret';secret.write_text('synthetic')
    home=tmp_path/'auth';home.mkdir()
    (home/'auth.json').write_text('synthetic')
    monkeypatch.setenv('CODEX_HOME',str(home))
    runner=CLIRunner('codex');runner.executable='/usr/bin/codex'
    work=tmp_path/'work';output=tmp_path/'out'
    work.mkdir();output.mkdir()
    env=runner._sandbox_env(work,output)
    link=work.parent/'home'/'outside-link';link.symlink_to(secret)
    for command in (['/bin/test','-r',str(secret)], ['/usr/bin/touch',str(secret)]):
        code,_,_=execute(runner.sandbox_command(work,output,command),env=env,cwd=work)
        assert code != 0
    code,_,_=execute(runner.sandbox_command(work,output,['/usr/bin/touch',str(link)]),env=env,cwd=work)
    assert code != 0
    code,_,_=execute(runner.sandbox_command(work,output,['/usr/bin/touch',str(output/'created')]),env=env,cwd=work)
    assert code == 0 and (output/'created').exists()


def test_execute_stdin_and_no_shell(tmp_path):
    marker=tmp_path/'not-created'
    code,out,err=execute([sys.executable,'-c','import sys; print(sys.stdin.read())'],input_text=f'$(touch {marker})')
    assert code==0 and '$(touch' in out and not marker.exists()


def test_execute_timeout_terminates_process_group():
    start=time.monotonic()
    with pytest.raises(FlowError,match='timeout'):
        execute([sys.executable,'-c','import time; time.sleep(30)'],timeout=.1)
    assert time.monotonic()-start<4


def test_execute_cancellation():
    event=threading.Event(); event.set()
    with pytest.raises(Cancelled): execute([sys.executable,'-c','import time; time.sleep(30)'],cancel=event)


def test_macos_codex_profile_allows_only_global_preference_reads(tmp_path, monkeypatch):
    monkeypatch.setattr('contexttrail.runners.cli_runner.sys.platform','darwin')
    (tmp_path/'auth.json').write_text('synthetic')
    monkeypatch.setenv('CODEX_HOME',str(tmp_path))
    work=tmp_path/'work';output=tmp_path/'out'
    work.mkdir();output.mkdir()
    runner=CLIRunner('codex');runner.executable='/usr/bin/codex'
    profile=runner._macos_profile(work,output,work.parent/'home')
    assert '(allow user-preference-read (preference-domain "kCFPreferencesAnyApplication"))' in profile
    assert '(allow user-preference-read)' not in profile and 'user-preference-write' not in profile
    assert f'(ipc-posix-name "apple.cfprefs.{os.getuid()}v1")' in profile
    assert '(allow ipc-posix-shm-read-data)' not in profile and 'ipc-posix-shm-write' not in profile


def test_failed_call_reports_the_providers_reason():
    from contexttrail.runners.cli_runner import cli_error
    codex = ('{"type":"thread.started"}\n'
             '{"type":"error","message":"Your workspace is out of credits. Add credits to continue."}\n'
             '{"type":"turn.failed","error":{"message":"Your workspace is out of credits. Add credits to continue."}}')
    assert cli_error("codex", codex) == "Your workspace is out of credits. Add credits to continue."
    assert cli_error("claude", '{"type":"result","is_error":true,"result":"Credit balance is too low"}') == \
        "Credit balance is too low"
    assert cli_error("codex", "not json\n") is None


def test_a_codex_follow_up_resumes_the_session_with_only_what_is_new():
    from contexttrail.runners.cli_runner import Thread, codex_thread_id
    runner=CLIRunner('codex'); runner.executable='/usr/bin/codex'; runner.features={'shell_tool','hooks'}
    first=runner.build_cli({},persist=True)
    assert '--ephemeral' not in first and first[first.index('--sandbox')+1]=='read-only'
    resumed=runner.build_cli({},resume='thread-1')
    assert resumed[-2:]==['thread-1','-'] and resumed[resumed.index('exec')+1]=='resume'
    assert any(resumed[i:i+2]==['-c','sandbox_mode="read-only"'] for i in range(len(resumed)))
    assert any(resumed[i:i+2]==['--disable','hooks'] for i in range(len(resumed)))
    assert codex_thread_id('{"type":"thread.started","thread_id":"abc"}\n{"type":"turn.started"}')=='abc'

    class Directory:
        name='/nowhere'
        def cleanup(self): pass
    thread=Thread(Directory())
    base={'stage':'extract','data':{'records':[1,2]}}
    assert thread.follow_up(base) is None                       # nothing to resume yet
    assert thread.answered(base,'abc',{'input_tokens':100,'cached_input_tokens':0}) == {'input_tokens':100,'cached_input_tokens':0}
    asked={**base,'evidence_rounds':[['round one']]}
    assert thread.follow_up(asked)=={'follow_up':thread.follow_up(asked)['follow_up'],'new_evidence_rounds':[['round one']]}
    # A resumed session reports its running total; the call's own usage is the difference.
    assert thread.answered(asked,'abc',{'input_tokens':230,'cached_input_tokens':98}) == {'input_tokens':130,'cached_input_tokens':98}
    final={**asked,'evidence_rounds':[['round one'],['round two']],'evidence_requests':'None left'}
    delta=thread.follow_up(final)
    assert delta['new_evidence_rounds']==[['round two']] and delta['evidence_requests']=='None left'
    # Changed records (a trimmed request) cannot continue: a new session.
    assert thread.follow_up({**asked,'data':{'records':[1]}}) is None
    thread.reset()
    assert thread.follow_up(asked) is None


def test_a_later_stage_of_the_same_unit_continues_the_codex_session_with_its_whole_request():
    from contexttrail.runners.cli_runner import STAGE_FOLLOW_UP, Thread

    class Directory:
        name = '/nowhere'
        def cleanup(self): pass
    thread = Thread(Directory())
    extract = {'stage': 'extract', 'instructions': 'extract…', 'data': {'records': [1, 2]}}
    thread.answered(extract, 'abc', {'input_tokens': 100}, 'common')
    integrate = {'stage': 'integrate', 'instructions': 'integrate…', 'data': {'candidates': [1]}}
    assert thread.follow_up(integrate, 'common') == {'follow_up': STAGE_FOLLOW_UP, **integrate}
    thread.answered(integrate, 'abc', {'input_tokens': 250}, 'common')
    # The delta review is an integrate request with a review instruction: a later stage too.
    review = {**integrate, 'review_instruction': 'patch…', 'review_issues': ['x']}
    assert thread.follow_up(review, 'common') == {'follow_up': STAGE_FOLLOW_UP, **review}
    # The same stage asked differently, or other instructions (the light reading), start a new session.
    assert thread.follow_up({**integrate, 'data': {'candidates': [2]}}, 'common') is None
    assert thread.follow_up(review, 'light instructions') is None
