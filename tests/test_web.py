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
    assert "모든 단계를 보세요" in story_svg(work_only)


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
    empty = story_svg(story_view({"version": 1, "events": [event("w", "action", "10:02", basis="tool_record")], "edges": []}))
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
    process = subprocess.Popen([sys.executable, "-m", "contexttrail", "serve", str(folder), "--port", "0"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    try:
        import select, time
        output, deadline = b"", time.monotonic() + 15
        # A deadline, so a link stuck in the buffer (the defect this guards) fails the test instead of hanging it.
        while b"Access URL" not in output and time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
            if ready:
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                output += chunk
        lines = output.decode("utf-8", "replace").splitlines()
        assert any("Access URL" in line and "#token=" in line for line in lines)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0  # stopped like Ctrl-C, not killed by the signal (-15)
    finally:
        if process.poll() is None:
            process.kill()


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
