import http.client
import json
import re
import threading
import pytest
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.webview import LocalViewer


@pytest.fixture
def viewer(laboratory):
    _,store,engine,records,make=laboratory
    records.append(make(CASES[0][0]));engine.analyze(FixtureRunner)
    calls=[];gate=threading.Event()
    def refresh():
        calls.append('called');gate.wait(2);return {'status':'noop','runner_calls':0}
    obj=LocalViewer(store,refresh,port=0).start()
    yield obj,calls,gate
    gate.set()
    if obj.refresh_thread: obj.refresh_thread.join(3)
    obj.close()


def request(obj,method,path,*,auth=True,headers=None,body=None):
    connection=http.client.HTTPConnection('127.0.0.1',obj.port,timeout=3)
    heads={'Authorization':'Bearer '+obj.token} if auth else {}
    heads.update(headers or {})
    connection.request(method,path,body=body,headers=heads)
    result=connection.getresponse();status=result.status;data=result.read();response_headers=dict(result.getheaders());connection.close()
    return status,data,response_headers


def test_local_bind_token_and_no_ai_on_get(viewer):
    obj,calls,_=viewer
    assert obj.server.server_address[0]=='127.0.0.1'
    assert request(obj,'GET','/graph',auth=False)[0]==401
    assert request(obj,'GET','/graph')[0]==200
    assert request(obj,'GET','/graph')[0]==200
    assert not calls
    assert obj.token in obj.url().split('#')[1]
    assert obj.token not in obj.url().split('#')[0]


def test_browser_event_evidence_same_version(viewer):
    obj,calls,_=viewer
    graph=json.loads(request(obj,'GET','/graph')[1])['graph'];event=graph['events'][0]
    result=json.loads(request(obj,'GET','/events/'+event['id']+'?version='+str(graph['version']))[1])
    assert result['version']==graph['version'] and result['evidence'][0]['quote']==CASES[0][0]
    evidence_id=event['evidence_ids'][0]
    assert request(obj,'GET','/evidence/'+evidence_id)[0]==200
    assert request(obj,'GET','/evidence/../../etc/passwd')[0]==404
    assert not calls


def test_html_assets_offline_and_csp(viewer):
    obj,_,_=viewer
    status,data,headers=request(obj,'GET','/',auth=False)
    assert status==200 and b'cdn.' not in data
    assert "default-src 'none'" in headers['Content-Security-Policy']
    assert headers['Cache-Control']=='no-store'
    assert request(obj,'GET','/app.js',auth=False)[0]==200
    assert request(obj,'GET','/graph.svg')[0]==200


@pytest.mark.parametrize('headers',[{'Host':'evil.example'},{'Origin':'https://evil.example'},{'Host':'localhost:1'}])
def test_host_and_origin_protection(viewer,headers):
    obj,calls,_=viewer
    assert request(obj,'GET','/graph',headers=headers)[0]==403
    assert not calls


def test_refresh_requires_all_guards_and_is_deduplicated(viewer):
    obj,calls,gate=viewer
    headers={'Origin':f'http://127.0.0.1:{obj.port}','Content-Type':'application/json','X-Projectflow-Action':'refresh'}
    assert request(obj,'POST','/refresh',body='{"confirm":true}')[0]==403
    assert request(obj,'POST','/refresh',auth=False,headers=headers,body='{"confirm":true}')[0]==403
    assert request(obj,'POST','/refresh',headers=headers,body='{}')[0]==400
    assert not calls
    assert request(obj,'POST','/refresh',headers=headers,body='{"confirm":true}')[0]==202
    assert request(obj,'POST','/refresh',headers=headers,body='{"confirm":true}')[0]==409
    assert len(calls)==1
    gate.set()


def test_english_screen_serves_english_page_and_labels(viewer):
    from contexttrail import i18n
    from contexttrail.render import event_detail
    i18n.set_language("en")
    obj,calls,_=viewer
    status,data,_=request(obj,'GET','/',auth=False)
    page=data.decode('utf-8')
    assert status==200 and '<html lang="en">' in page and 'Analyze changes' in page
    assert not re.search('[가-힣]',page)
    graph=json.loads(request(obj,'GET','/graph')[1])['graph']
    event=json.loads(request(obj,'GET','/events/'+graph['events'][0]['id'])[1])['event']
    assert event['status_label'] and not re.search('[가-힣]',event['status_label'])
    texts=[text for text,_ in event_detail(graph,event['id'],obj.store.evidence)]
    assert 'What happened' in texts and not any(re.search('[가-힣]',text) for text in texts if 'Source evidence' in text)
    assert not calls


def test_the_page_opens_on_the_story_view_which_only_draws_what_the_page_imports(viewer):
    import xml.etree.ElementTree as ET
    obj,calls,_=viewer
    status,data,_=request(obj,'GET','/graph.svg?view=story')
    assert status==200 and b'data-event-id' in data
    allowed={'svg','defs','marker','path','g','rect','text'}  # the page's own whitelist (app.js)
    document=ET.fromstring(data)
    assert {node.tag.split('}')[1] for node in document.iter()}<=allowed
    assert not any(name.startswith('on') or name in ('href','style') for node in document.iter() for name in node.attrib)
    assert request(obj,'GET','/graph.svg?view=other')[1]==request(obj,'GET','/graph.svg')[1]  # anything else: every step
    page=request(obj,'GET','/',auth=False)[1].decode()
    script=request(obj,'GET','/app.js',auth=False)[1].decode()
    assert 'id="view"' in page and '"/graph.svg?view=story"' in script and '"story"' in script
    assert not calls


def test_the_story_svg_escapes_what_the_model_wrote_and_names_each_agent_once():
    from contexttrail.diagram import story_svg
    from contexttrail.schema import story_view
    events=[{"id":"q","kind":"question","status":"asked","title":"<script>alert(1)</script> 해 주세요","summary":"","actor":"user",
             "basis":"explicit_statement","evidence_ids":[],"session_ids":["s"],"recorded_at":"10:00"},
            {"id":"d","kind":"decision","status":"adopted","title":"맡김","summary":"","actor":"assistant",
             "basis":"explicit_statement","evidence_ids":[],"session_ids":["s"],"recorded_at":"10:01"},
            {"id":"h","kind":"action","status":"applied","title":"위임","summary":"","actor":"assistant","basis":"tool_record",
             "step":"delegate","delegated_to":"plots & <b>","evidence_ids":[],"session_ids":["s"],"recorded_at":"10:02"},
            {"id":"w","kind":"outcome","status":"reported_complete","title":"그림 \"완성\"","summary":"","actor":"assistant",
             "basis":"explicit_statement","authoring":[{"tool":"codex","role":"subagent","agent":"plots & <b>"}],
             "delegated_by":"h","evidence_ids":[],"session_ids":["s2"],"recorded_at":"10:03"}]
    edges=[{"id":"e1","from_event_id":"q","to_event_id":"d","relation":"answers","basis":"explicit","active":True,"evidence_ids":[]},
           {"id":"e2","from_event_id":"d","to_event_id":"h","relation":"motivates","basis":"inferred","active":True,"evidence_ids":[]}]
    out=story_svg(story_view({"version":1,"events":events,"edges":edges}))
    assert "<script>" not in out and "&lt;script&gt;" in out and "<b>" not in out
    assert out.count("↳ plots &amp; &lt;b&gt;: 그림 &quot;완성&quot;")==1 or out.count("↳ plots &amp; &lt;b&gt;: 그림 \"완성\"")==1
    assert 'data-event-id="h"' not in out and 'data-event-id="w"' not in out  # folded into the decision's box
    assert ">답변<" in out  # neighbours joined by their relation


def test_the_story_svg_keeps_one_meaning_for_a_dashed_line_and_lights_a_folded_step():
    from contexttrail.diagram import story_svg
    from contexttrail.schema import story_view
    def event(key, kind, status, at, session="s", **more):
        return {"id": key, "kind": kind, "status": status, "title": key, "summary": "", "actor": "assistant",
                "basis": "explicit_statement", "evidence_ids": [], "session_ids": [session], "recorded_at": at, **more}
    events = [event("g", "goal", "adopted", "10:00"), event("d", "decision", "adopted", "10:01", title="결정￿ 하나"),
              event("e", "action", "applied", "10:02", basis="tool_record"), event("x", "decision", "adopted", "10:03"),
              event("o", "goal", "adopted", "10:04", session="other")]
    edges = [{"id": "e1", "from_event_id": "g", "to_event_id": "d", "relation": "motivates", "basis": "inferred",
              "active": True, "evidence_ids": []},
             {"id": "e2", "from_event_id": "d", "to_event_id": "e", "relation": "motivates", "basis": "explicit",
              "active": True, "evidence_ids": []}]
    out = story_svg(story_view({"version": 1, "events": events, "edges": edges}))
    assert "￿" not in out and 'aria-label="02 · 결정 하나 ·' in out  # the whole title, nothing XML cannot carry
    assert out.count("stroke-dasharray") == 1 and "동기·추정" in out  # dashed means inferred, as on the rest of the page
    assert out.count("marker-end") == 1  # d → x share no relation: no line between them
    assert "흐름 1/2" in out and "흐름 2/2" in out  # the other session is a flow of its own
    assert 'data-folds="e"' in out  # the edit folded into the decision that led to it
    work_only = story_view({"version": 1, "events": [events[2]], "edges": []})
    assert 'data-event-id="e"' in story_svg(work_only)  # work with no box of its session is a box of its own


def test_the_story_svg_draws_every_relation_between_neighbours_and_wraps_its_empty_message():
    from contexttrail import i18n
    from contexttrail.diagram import story_svg
    from contexttrail.schema import story_view
    def event(key, kind, at, **more):
        return {"id": key, "kind": kind, "status": "adopted", "title": key, "summary": "", "actor": "assistant",
                "basis": "explicit_statement", "evidence_ids": [], "session_ids": ["s"], "recorded_at": at, **more}
    events = [event("a", "goal", "10:00"), event("b", "decision", "10:01")]
    edges = [{"id": "e1", "from_event_id": "a", "to_event_id": "b", "relation": "motivates", "basis": "inferred",
              "active": True, "evidence_ids": []},
             {"id": "e2", "from_event_id": "a", "to_event_id": "b", "relation": "revises", "basis": "explicit",
              "active": True, "evidence_ids": []}]
    out = story_svg(story_view({"version": 1, "events": events, "edges": edges}))
    assert "동기·추정·수정" in out and "stroke-dasharray" not in out  # one explicit relation: a solid line
    assert 'class="story"' in out
    i18n.set_language("en")
    view = story_view({"version": 1, "events": [event("w", "action", "10:02", basis="tool_record")], "edges": []})
    empty = story_svg({**view, "events": [], "hidden": 1})
    lines = [line for line in empty.split("<text")[1:] if 'font-size="15"' in line]
    assert len(lines) >= 2 and "every step" in empty  # wrapped onto lines that fit, the pointer to the button kept


def test_unsupported_methods_get_the_pages_headers_and_no_product_banner(viewer):
    obj,calls,_=viewer
    for method in ('PUT','DELETE','OPTIONS','HEAD'):
        status,_,headers=request(obj,method,'/graph')
        assert status in (405,501) and "default-src 'none'" in headers['Content-Security-Policy']
        assert headers['Cache-Control']=='no-store' and 'ProjectFlow' not in headers.get('Server','')
        assert 'Python' not in headers.get('Server','')
    assert not calls


def test_serve_prints_its_link_at_once_and_stops_on_sigterm(tmp_path):
    """Under a host or nohup stdout is a pipe: the link must arrive while the server runs, and terminate() (SIGTERM)
    must stop it like Ctrl-C."""
    import os, signal, subprocess, sys
    folder = tmp_path / "project"; folder.mkdir()
    previous = signal.getsignal(signal.SIGHUP)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # as nohup leaves it
    try:
        from contexttrail.cli import main
        main(["status", str(folder)])
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN  # nohup's choice is kept
    finally:
        signal.signal(signal.SIGHUP, previous)
    env = {**os.environ, "CONTEXTTRAIL_LANGUAGE": "en"}
    errors = (tmp_path / "serve.err").open("wb")
    process = subprocess.Popen([sys.executable, "-X", "faulthandler", "-m", "contexttrail", "serve", str(folder), "--port", "0"],
                               stdout=subprocess.PIPE, stderr=errors, env=env)
    try:
        import select, time
        output, deadline = b"", time.monotonic() + 30
        # A deadline, so a link stuck in the buffer (the defect this guards) fails the test instead of hanging it.
        while b"Access URL" not in output and time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
            if ready:
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                output += chunk
        lines = output.decode("utf-8", "replace").splitlines()
        # Fails only on macOS CI so far (still running, nothing printed): a server that never printed its link
        # is aborted, so faulthandler writes where it waits to stderr.
        if not any("Access URL" in line for line in lines) and process.poll() is None:
            process.send_signal(signal.SIGABRT)
            process.wait(timeout=10)
        assert any("Access URL" in line and "#token=" in line for line in lines), (
            f"exit {process.poll()}, stdout {output[-2000:]!r}, stderr {(tmp_path / 'serve.err').read_bytes()[-4000:]!r}")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0  # stopped like Ctrl-C, not killed by the signal (-15)
    finally:
        if process.poll() is None:
            process.kill()
        errors.close()


def test_the_page_starts_no_analysis_without_a_runner_and_its_consent(viewer):
    """The page asks no plan, so the server checks what a terminal would have asked: a runner and consent to it."""
    obj,calls,_=viewer
    blocked={'ok':False,'runner':'codex','model':'gpt-luna','reason':'Sending to codex was never consented to.'}
    obj.refresh_ready=lambda: blocked
    headers={'Origin':f'http://127.0.0.1:{obj.port}','Content-Type':'application/json','X-Projectflow-Action':'refresh'}
    payload=json.loads(request(obj,'GET','/graph')[1])
    assert payload['can_refresh'] is False and payload['refresh_blocked']==blocked['reason']
    assert payload['refresh_with']=={'runner':'codex','model':'gpt-luna'}
    status,body,_=request(obj,'POST','/refresh',headers=headers,body='{"confirm":true}')
    assert status==409 and 'never consented' in json.loads(body)['error'] and not calls


def test_the_freshness_phrase_is_counted_again_after_a_scan_elsewhere(viewer, monkeypatch):
    from contexttrail import freshness, webview
    obj,_,_=viewer
    counted=[]
    monkeypatch.setattr(freshness, "SCANNED_AT_SECONDS", 0)
    monkeypatch.setattr(webview, "check_from_store", lambda scope, store: counted.append(1) or None)
    obj.scope=object()
    version=obj.store.graph()["version"]
    obj.freshness(version); obj.freshness(version)
    assert len(counted)==1  # once per graph version and scan
    obj.store.set_meta("source_index", {"scanned_at": "2026-10-21T00:00:00Z", "files": {}})
    obj.freshness(version)
    assert len(counted)==2  # a scan run in another process changes what the graph lags


def test_the_page_learns_the_latest_version_and_an_event_reads_only_its_sources(viewer, monkeypatch):
    obj,_,_=viewer
    payload=json.loads(request(obj,'GET','/graph')[1])
    assert payload['latest_version']==obj.store.graph()['version']
    monkeypatch.setattr(obj.store,'sources',lambda: (_ for _ in ()).throw(AssertionError('whole table read')))
    event=obj.store.graph()['events'][0]['id']
    assert request(obj,'GET','/events/'+event)[0]==200


@pytest.fixture
def story_viewer(laboratory):
    """A viewer over the demo story: two requests, a proposal, two edits each checked by a test run, the report."""
    from dataclasses import replace
    from contexttrail.demo import story_steps, step_text
    _,store,engine,records,make=laboratory
    for n, step in enumerate(story_steps()):
        text = step_text(step, CASES)
        if step['role'] == 'tool_call':
            text = f"Tool: {step['tool']}\n{text}"
        record = make(text, key=f's{n}', role=step['role'], provider=step['session'], session=f"demo-{step['session']}")
        records.append(replace(record, recorded_at=f'2026-09-22T10:{n:02d}:00Z', tool_call_id=step.get('call')))
    engine.analyze(FixtureRunner)
    obj=LocalViewer(store,None,port=0).start()
    yield obj
    obj.close()


ALLOWED_SVG = {'svg','defs','marker','path','g','rect','text'}  # the page's own whitelist (app.js)


def test_every_step_is_the_terminal_flow_drawn_under_the_pages_whitelist(story_viewer):
    import xml.etree.ElementTree as ET
    from contexttrail.schema import folded_view
    obj=story_viewer
    status,data,headers=request(obj,'GET','/graph.svg?view=all')
    assert status==200 and headers['Content-Type'].startswith('image/svg+xml')
    document=ET.fromstring(data)
    assert {node.tag.split('}')[1] for node in document.iter()}<=ALLOWED_SVG
    assert not any(name.startswith('on') or name in ('href','style') for node in document.iter() for name in node.attrib)
    assert document.attrib['class']=='flow'
    boxes=[node for node in document.iter() if 'data-event-id' in node.attrib]
    drawn={node.attrib['data-event-id'] for node in boxes}
    folded={key for node in boxes for key in node.attrib.get('data-folds','').split()}
    graph=folded_view(obj.store.graph())
    assert {event['id'] for event in graph['events']}<=drawn|folded  # every step has its box, or lights the one it folded into
    assert all(node.attrib.get('tabindex')=='0' and node[0].tag.endswith('rect') for node in boxes)
    script=request(obj,'GET','/app.js',auth=False)[1].decode()
    assert '"/graph.svg?view=all"' in script
    assert request(obj,'GET','/graph.svg?view=all')[1]==data


def test_a_drawing_is_made_once_per_version_view_and_language(story_viewer, monkeypatch):
    from contexttrail import i18n, webview
    obj=story_viewer
    calls=[]
    for name in ('flow_svg','story_svg'):
        def counted(*args, _name=name, _draw=getattr(webview,name), **kwargs):
            calls.append(_name);return _draw(*args,**kwargs)
        monkeypatch.setattr(webview,name,counted)
    first=request(obj,'GET','/graph.svg?view=all')[1]
    assert request(obj,'GET','/graph.svg?view=all')[1]==first and calls==['flow_svg']
    request(obj,'GET','/graph.svg?view=story');request(obj,'GET','/graph.svg?view=story')
    assert calls==['flow_svg','story_svg']
    i18n.set_language('en')
    request(obj,'GET','/graph.svg?view=all');request(obj,'GET','/graph.svg?view=all')
    assert calls==['flow_svg','story_svg','flow_svg']


JUMP_STUBS = """
let view="story", drawing=0, shown="story", boxes=new Map(), events=new Map([["folded",{}]]), selected=null, saved=0;
const labels=[], L={switched:"switched"};
function viewLabel(){labels.push(view);}
function saveView(){saved++;}
function notice(){}
async function select(id){selected=id;}
async function draw(){++drawing;if(FAIL)throw new Error("bad drawing");shown=view;return true;}
"""


def run_jump(fail):
    import shutil, subprocess
    from importlib.resources import files
    node=shutil.which('node')
    if node is None:
        pytest.skip('node is not installed')
    script=files('contexttrail').joinpath('assets/app.js').read_text()
    jump=re.search(r"^async function jump\(id\) \{.*?^\}$",script,re.M|re.S).group(0)
    program=(f"const FAIL={'true' if fail else 'false'};"+JUMP_STUBS+jump+
             '\njump("folded").then(()=>null,e=>e.message).then(error=>'
             'console.log(JSON.stringify({error,view,labels,selected,saved})));')
    out=subprocess.run([node,'-e',program],capture_output=True,text=True,timeout=20,check=True).stdout
    return json.loads(out)


def test_a_jump_whose_drawing_fails_leaves_the_button_on_the_view_still_shown():
    assert run_jump(fail=True)=={'error':'bad drawing','view':'story','labels':['all','story'],'selected':None,'saved':0}
    assert run_jump(fail=False)=={'error':None,'view':'all','labels':['all'],'selected':'folded','saved':1}


def test_the_every_step_svg_cuts_no_text_and_keeps_lines_apart_from_wide_glyphs():
    import xml.etree.ElementTree as ET
    from contexttrail.diagram import flow_svg
    title=("결합을 추가적으로 수정하기 위해서는 완전히 더 세부적으로 쪼개야 할 것 같단 말이지? 그렇다면 모델도 "
           "sol -> luna로 바꿀 필요가 있어 보이는데 <b>&</b> " + "x"*150)
    events=[{"id":"q","kind":"question","status":"asked","title":title,"summary":"","actor":"user",
             "basis":"explicit_statement","evidence_ids":[],"session_ids":["s"],"recorded_at":"2026-09-22T10:00:00Z"},
            {"id":"d","kind":"decision","status":"adopted","title":"나눈다","summary":"이유가 있다. 둘째 문장.","actor":"assistant",
             "basis":"explicit_statement","evidence_ids":[],"session_ids":["s"],"recorded_at":"2026-09-22T10:01:00Z"}]
    edges=[{"id":"e1","from_event_id":"q","to_event_id":"d","relation":"answers","basis":"explicit","active":True,"evidence_ids":[]}]
    out=flow_svg({"version":1,"events":events,"edges":edges})
    document=ET.fromstring(out)
    texts=[node.text or '' for node in document.iter() if node.tag.endswith('text')]
    joined=''.join(c for c in ''.join(texts) if c not in ' \u00a0' and not '\u2500'<=c<='\u25ff')  # borders out
    assert title.replace(' ','') in joined and '…' not in joined  # wrapped over lines, never cut
    assert '<b>' not in out and '&lt;b&gt;' in out
    wide=lambda text: any('가'<=c<='힣' for c in text)
    line=lambda text: any('─'<=c<='◿' for c in text)
    assert not any(wide(text) and line(text) for text in texts)  # a border after Hangul starts at its own cell
    widths={round(float(node.attrib['textLength'])/len(node.text)) for node in document.iter()
            if node.tag.endswith('text') and 'textLength' in node.attrib}
    assert widths=={8}  # narrow text fills exactly its cells (8.4 px each)


def test_the_page_lists_requests_with_their_times_and_the_turn_of_each_event(story_viewer):
    from contexttrail.schema import folded_view
    obj=story_viewer
    payload=json.loads(request(obj,'GET','/graph')[1])
    graph=payload['graph']
    numbers={event['id']:n for n,event in enumerate(graph['events'],1)}
    requests=payload['requests']
    assert len(requests)==2 and all(set(item)=={'id','number','title','at'} for item in requests)
    assert [item['number'] for item in requests]==sorted(numbers[item['id']] for item in requests)
    assert requests[0]['at']=='2026-09-22T10:00:00Z'
    assert all(payload['turn_of'][item['id']]==n for n,item in enumerate(requests))
    later=[key for key,n in payload['turn_of'].items() if n==1]
    assert all(numbers[key]>=requests[1]['number'] for key in later)
    assert payload['run_state'] and payload['run_state']!=graph['analysis_status']  # in the screen language
    assert [event['id'] for event in graph['events']]==[event['id'] for event in folded_view(obj.store.graph())['events']]


def test_the_detail_puts_the_events_own_evidence_first_and_groups_its_relations(story_viewer):
    from contexttrail import i18n
    obj=story_viewer
    graph=json.loads(request(obj,'GET','/graph')[1])['graph']
    change=next(e for e in graph['events'] if e['kind']=='action' and any(
        edge['active'] and edge['relation']=='verifies' and edge['from_event_id']==e['id'] for edge in graph['edges']))
    data=json.loads(request(obj,'GET','/events/'+change['id'])[1])
    event=data['event']
    assert event['number']==[e['id'] for e in graph['events']].index(change['id'])+1
    assert event['kind_label']=='변경' and event['basis_label']=='도구 기록' and event['status_tone'] in ('ok','warn','fail','plain')
    own=[item['own'] for item in data['evidence']]
    assert own and own==sorted(own,reverse=True)  # its own evidence before its relations'
    assert all(item['role_label'] in ('도구 호출','도구 결과','사용자 발화','어시스턴트 응답') for item in data['evidence'])
    groups=[item['group'] for item in data['relations']]
    assert 'checks' in groups and groups==sorted(groups,key=['checks','links','structure'].index)
    check=next(item for item in data['relations'] if item['group']=='checks')
    assert check['other_title'] and check['other_status'] and check['relation_label']=='검증' and check['direction']=='out'
    turn=[item for item in data['relations'] if item.get('origin')=='dialog_turn']
    assert all(item['group']=='structure' and item['origin_label']=='대화 순서' for item in turn)
    i18n.set_language('en')
    data=json.loads(request(obj,'GET','/events/'+change['id'])[1])
    labels=[data['event'][key] for key in ('kind_label','actor_label','basis_label','status_label')]
    labels+=[item[key] for item in data['relations'] for key in ('relation_label','basis_label','origin_label','other_status')]
    labels+=[item[key] for item in data['evidence'] for key in ('role_label','provider_label')]
    assert not any(re.search('[가-힣]',label) for label in labels)
    assert 'explicit_statement' not in labels and 'tool_record' not in labels and 'tool_call' not in labels


def test_limitations_are_grouped_by_their_head_the_largest_first():
    from contexttrail.webview import limitation_groups
    messages=["Codex unparsed record type world_state, 1 records not sent to analysis (10/18/a.jsonl#6c): holds it",
              "Codex unparsed record type world_state, 5 records not sent to analysis (10/18/b.jsonl#7d): holds it",
              "Claude unsupported record types 2be9.jsonl: attachment:date",
              "Claude unsupported record types 3cf0.jsonl: attachment:date",
              "Claude unsupported record types 4d01.jsonl: attachment:date",
              "No verifies edges: the run named no file",
              "A sentence the analysis wrote, with no head at all.",
              "A long sentence before its colon is no head, since it says what happened in many words: here",
              "A sentence the analysis wrote, with no head at all."]
    groups=limitation_groups(messages)
    assert [(g['head'],g['count']) for g in groups]==[
        ("Claude unsupported record types …",3),
        ("Codex unparsed record type world_state, N records not sent to analysis",2),
        ("No verifies edges",1), (None,2)]
    assert groups[-1]['items'][0]=="A sentence the analysis wrote, with no head at all."  # a repeat listed once
    assert sum(g['count'] for g in groups)==len(set(messages))


def test_a_scan_warning_head_longer_than_the_cap_is_still_a_head():
    from contexttrail.webview import LIMITATION_HEAD_CHARS, limitation_groups
    messages=[f"Claude unparsed record type system:stop_hook_summary, {n} records not sent to analysis (10/18/{n}.jsonl#6c): "
              "a hook's summary" for n in (2,7)]+["A sentence the analysis wrote, with no head at all."]
    head="Claude unparsed record type system:stop_hook_summary, N records not sent to analysis"
    assert len(head)>LIMITATION_HEAD_CHARS
    assert [(g['head'],g['count']) for g in limitation_groups(messages)]==[(head,2),(None,1)]


def test_the_page_has_request_navigation_and_search_in_both_languages(viewer):
    from contexttrail import i18n
    obj,_,_=viewer
    page=request(obj,'GET','/',auth=False)[1].decode()
    assert all(f'id="{name}"' in page for name in ('prev','next','latest','turn','search','matches','notice'))
    script=request(obj,'GET','/app.js',auth=False)[1].decode()
    assert 'textContent=' in script and 'innerHTML' not in script  # every string is set as text
    i18n.set_language('en')
    page=request(obj,'GET','/',auth=False)[1].decode()
    assert not re.search('[가-힣]',page)
