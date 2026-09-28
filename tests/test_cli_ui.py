import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
import pytest
from projectflow.cli import main
from projectflow.git_context import Scope
from projectflow.store import Store
from projectflow.ui import token_usage_label


def test_stage_effort_is_saved_but_skipping_review_is_per_run(tmp_path):
    from projectflow.cli import _options, parser
    store = Store(tmp_path / 'state', 'scope')
    first = _options(parser().parse_args(['analyze', '.', '--integrate-effort', 'xhigh', '--no-review']), store)
    assert first.integrate_effort == 'xhigh' and first.semantic_review is False
    again = _options(parser().parse_args(['analyze', '.']), store)
    assert again.integrate_effort == 'xhigh' and again.semantic_review is True
    assert again.extract_effort == 'medium'


def test_developer_tracing_flags_work_but_are_not_advertised(capsys):
    from projectflow.cli import parser
    assert parser().parse_args(['analyze', '.', '--langsmith']).langsmith_enabled
    with pytest.raises(SystemExit):
        parser().parse_args(['analyze', '--help'])
    assert 'langsmith' not in capsys.readouterr().out.lower()


def test_demo_cli_noop_export_without_cli(tmp_path,capsys):
    directory=tmp_path/'demo'
    assert main(['demo','--path',str(directory),'--no-tui'])==0
    folder=directory/'sample-project'
    assert main(['analyze',str(folder),'--no-tui'])==0
    assert 'Runner 호출: 0' in capsys.readouterr().out
    for kind in ['md','mmd','json']:
        output=tmp_path/f'export.{kind}'
        assert main(['export',str(folder),'--format',kind,'--output',str(output)])==0
        assert output.stat().st_mode&0o077==0
        if kind=='json':
            data=json.loads(output.read_text());assert 'graph' in data
    assert main(['view',str(folder),'--no-tui','--ascii'])==0
    assert main(['scan',str(folder)])==0


def test_graph_command_shows_project_and_eval_in_terminal(tmp_path, capsys):
    directory=tmp_path/'demo'
    assert main(['demo','--path',str(directory),'--no-tui'])==0
    folder=directory/'sample-project'
    assert main(['graph',str(folder)])==0
    project_output=capsys.readouterr().out
    assert '사건' in project_output and '┌─ [01] 결정' in project_output
    # labelled arrows between boxes; the failed check leads to the fix, so it stays in the story column
    assert '│ 검증' in project_output and '│ 동기' in project_output and '[01] 후속 ▶' in project_output
    assert 'AI 호출 없음' in project_output

    evaluation=tmp_path/'eval'
    assert main(['eval','--fixture','demo','--runner','mock','--output',str(evaluation)])==0
    capsys.readouterr()
    assert main(['graph',str(evaluation)])==0
    output=capsys.readouterr().out
    assert 'ContextTrail' in output and 'AI 호출 없음' in output
    assert "{'evidence_ids'" not in output
    assert '근거 ' in output and '관계' in output


@pytest.mark.parametrize('columns',[52,110])
def test_graph_command_opens_curses_browser_on_tty(tmp_path, columns):
    directory=tmp_path/'demo'
    assert main(['demo','--path',str(directory),'--no-tui'])==0
    master,slave=pty.openpty()
    fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',28,columns,0,0))
    process=subprocess.Popen([sys.executable,'-m','projectflow','graph',str(directory/'sample-project')],
                             stdin=slave,stdout=slave,stderr=slave,
                             env={**os.environ,'TERM':'xterm-256color','LANG':'C.UTF-8',
                                  'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')+os.pathsep+os.environ.get('PYTHONPATH','')},
                             close_fds=True)
    os.close(slave)
    output=bytearray()
    try:
        start=time.monotonic(); sent=False
        while time.monotonic()-start<6 and process.poll() is None:
            if select.select([master],[],[],.1)[0]:
                try: output.extend(os.read(master,65536))
                except OSError: break
            if b'CONTEXTTRAIL' in output and not sent:
                os.write(master,b'j\tjjq')
                sent=True
        process.wait(timeout=2)
        assert process.returncode==0,output.decode('utf-8','replace')
        assert b'CONTEXTTRAIL' in output and b'Traceback' not in output
    finally:
        if process.poll() is None: process.kill();process.wait()
        os.close(master)


def test_analysis_token_footer_counts_only_reported_usage(tmp_path):
    store = Store(tmp_path/'state', 'test_scope')
    store.start_llm_call('call_1', 'run_1', 'unit_1', 'extract', 1, {})
    store.finish_llm_call('call_1', 'complete', {'usage': {'input_tokens': 120, 'output_tokens': 30,
                                                          'cached_input_tokens': 40, 'reasoning_output_tokens': 10}})
    store.start_llm_call('call_2', 'run_1', 'unit_1', 'integrate', 1, {})
    store.finish_llm_call('call_2', 'failed', {})
    totals = store.llm_token_totals()
    assert totals == (150, 1, 2)
    assert token_usage_label(*totals) == '분석 토큰 150+ (1/2 호출)'


def test_export_protects_database_and_original_source(tmp_path,capsys):
    directory=tmp_path/'demo';main(['demo','--path',str(directory),'--no-tui'])
    folder=directory/'sample-project';scope=Scope.resolve(folder)
    original=directory/'fixture-codex/sessions/demo.jsonl'
    assert main(['export',str(folder),'--format','json','--output',str(scope.state_dir/'state.sqlite'),'--force'])==1
    assert main(['export',str(folder),'--format','json','--output',str(original),'--force'])==1
    target=tmp_path/'output.json';target.symlink_to(original)
    assert main(['export',str(folder),'--format','json','--output',str(target),'--force'])==1


@pytest.mark.parametrize('term,ascii_only',[('xterm-256color',False),('screen-256color',False),('tmux-256color',False),('xterm-256color',True),('linux',True),('vt100',True)])
def test_curses_pty_resize_and_exit(tmp_path,capsys,term,ascii_only):
    directory=tmp_path/'demo';main(['demo','--path',str(directory),'--no-tui'])
    master,slave=pty.openpty()
    fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',30,100,0,0))
    args=[sys.executable,'-m','projectflow','view',str(directory/'sample-project')]
    if ascii_only:args.append('--ascii')
    process=subprocess.Popen(args,stdin=slave,stdout=slave,stderr=slave,env={**os.environ,'TERM':term,'LANG':'C.UTF-8', 'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')+os.pathsep+os.environ.get('PYTHONPATH','')},close_fds=True)
    os.close(slave);output=bytearray();start=time.monotonic();sent=False;resized=False
    try:
        while time.monotonic()-start<6 and process.poll() is None:
            if select.select([master],[],[],.1)[0]:
                try: output.extend(os.read(master,65536))
                except OSError:break
            elapsed=time.monotonic()-start
            if b'CONTEXTTRAIL' in output and not resized:
                fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',18,52,0,0));process.send_signal(signal.SIGWINCH)
                os.write(master,b'j\tj\tk');resized=True
            if resized and elapsed>.8 and not sent:
                os.write(master,b'q');sent=True
        process.wait(timeout=2)
        assert process.returncode==0,output.decode('utf-8','replace')
        assert b'CONTEXTTRAIL' in output and b'Traceback' not in output
    finally:
        if process.poll() is None:process.kill();process.wait()
        os.close(master)


def test_flow_panel_keeps_the_status_when_a_title_is_too_long():
    from projectflow.ui import FlowPanels
    graph = {"events": [{"id": "a", "kind": "action", "status": "applied", "title": "아주 긴 제목 " * 8,
                         "summary": "", "actor": "assistant", "evidence_ids": []}],
             "edges": [], "open_items": []}
    panels = FlowPanels(graph, lambda _: None)
    line, event_id = panels.lines[0]
    runs = panels._runs(line, event_id, 0, 50)
    text = "".join(part for part, _ in runs)
    assert text.endswith("/ 변경 적용 · 미검증") and "…" in text
    panels.left = 8  # scrolled sideways: the whole line is kept for scrolling
    assert "…" not in "".join(part for part, _ in panels._runs(line, event_id, 0, 50))


@pytest.mark.parametrize('mouse', [True, False])
def test_new_keys_mouse_and_korean_quit_in_a_real_terminal(tmp_path, mouse):
    directory=tmp_path/'demo'
    assert main(['demo','--path',str(directory),'--no-tui'])==0
    master,slave=pty.openpty()
    fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',32,120,0,0))
    args=[sys.executable,'-m','projectflow','graph',str(directory/'sample-project')]+([] if mouse else ['--no-mouse'])
    process=subprocess.Popen(args,stdin=slave,stdout=slave,stderr=slave,
                             env={**os.environ,'TERM':'xterm-256color','LANG':'C.UTF-8',
                                  'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')+os.pathsep+os.environ.get('PYTHONPATH','')},
                             close_fds=True)
    os.close(slave)
    click=lambda button,x,y: b'\x1b[M'+bytes([32+button,33+x,33+y])
    keys=[b'?',b'x',b'!',b'/',b'SQLite',b'\n',b'n',b'e',b'z',b'\x1b',b'J',b'l',b'h',b'1',b'\x7f',
          click(0,30,8),click(3,30,8),click(64,30,8),click(65,30,20),'ㅓ'.encode(),'ㅂ'.encode()]
    output=bytearray()
    try:
        start=time.monotonic(); sent=False
        while time.monotonic()-start<8 and process.poll() is None:
            if select.select([master],[],[],.1)[0]:
                try: output.extend(os.read(master,65536))
                except OSError: break
            if b'CONTEXTTRAIL' in output and not sent:
                for key in keys:
                    os.write(master,key); time.sleep(.08)
                sent=True
        process.wait(timeout=2)
        assert process.returncode==0,output.decode('utf-8','replace')  # ㅂ quit like Q
        assert b'Traceback' not in output
        assert (b'\x1b[?1000h' in output)==mouse  # click reports only, and none with --no-mouse
        assert '키 도움말'.encode() in output and "검색 'SQLite'".encode() in output
    finally:
        if process.poll() is None: process.kill();process.wait()
        os.close(master)


def test_no_command_opens_the_current_or_given_folder_without_ai(tmp_path, monkeypatch, capsys):
    from projectflow.cli import _default_command
    directory=tmp_path/'demo'
    assert main(['demo','--path',str(directory),'--no-tui'])==0
    folder=directory/'sample-project'
    capsys.readouterr()
    monkeypatch.chdir(folder)
    assert main([])==0  # not a terminal here, so the saved flow is printed
    assert 'JSON 저장 채택' in capsys.readouterr().out
    assert main([str(folder),'--ascii'])==0 and 'JSON 저장 채택' in capsys.readouterr().out
    assert _default_command(['--no-tui'])==['view','--no-tui']
    assert _default_command(['graph','.'])==['graph','.']
    assert _default_command(['--version'])==['--version']
    assert _default_command(['typo-command'])==['typo-command']  # not a folder: argparse reports it
