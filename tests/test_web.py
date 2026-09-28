import http.client
import json
import threading
import pytest
from projectflow.demo import CASES, FixtureRunner
from projectflow.webview import LocalViewer


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
