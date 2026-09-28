import copy
import xml.etree.ElementTree as ET
import pytest
from projectflow.demo import CASES, FixtureRunner
from projectflow.render import mermaid, parse_safe_mermaid, terminal_graph, svg
from projectflow.util import FlowError, safe_text, cell_slice


def sample(laboratory):
    _, _, engine, records, make = laboratory
    records += [make(c[0], key=f's{i}', role='tool_result' if c[4]=='tool' else c[4]) for i,c in enumerate(CASES)]
    return engine.analyze(FixtureRunner)['graph']


def test_branches_and_join_preserved(laboratory):
    graph=sample(laboratory)
    rows=terminal_graph(graph)
    text='\n'.join(r[0] for r in rows)
    assert '├──' in text and '└──' in text and '합류/되돌아감' in text
    assert {e['id'] for e in graph['events']} <= {r[1] for r in rows}
    assert '완료 보고·미검증' in text and '관측 실패' in text
    nodes,edges=parse_safe_mermaid(mermaid(graph))
    assert len(nodes)==7 and len(edges)==len(graph['edges'])


def test_ascii_fallback_keeps_flow(laboratory):
    text='\n'.join(r[0] for r in terminal_graph(sample(laboratory),ascii_only=True))
    assert '+--' in text and '`--' in text and '합류/되돌아감' in text
    assert not any(c in text for c in '├└│→↗')


def test_cycle_and_inferred_edge(laboratory):
    graph=sample(laboratory)
    edge=copy.deepcopy(graph['edges'][0]); edge.update(id='cyclic',from_event_id=graph['events'][-1]['id'],to_event_id=graph['events'][0]['id'],basis='inferred')
    graph['edges'].append(edge)
    text='\n'.join(r[0] for r in terminal_graph(graph))
    assert '추정' in text and '..>' in text
    assert len(terminal_graph(graph))<30
    assert 'stroke-dasharray' in svg(graph)


def test_more_than_99_events(laboratory):
    graph=sample(laboratory); prototype=graph['events'][0]
    graph['events']=[{**prototype,'id':f'e{i}','title':f'사건 {i}'} for i in range(125)]; graph['edges']=[]
    assert any('[100] 사건 99' in text for text,_ in terminal_graph(graph))


def test_user_markup_and_control_codes_never_execute(laboratory):
    graph=sample(laboratory)
    graph['events'][0]['title']='\x1b[31m<script>alert(1)</script>" ] --> BAD\x1b]52;c;Zm9v\x07'
    mmd=mermaid(graph)
    assert '<script>' not in mmd and '\x1b' not in mmd
    document=ET.fromstring(svg(graph))
    assert not any(node.tag.endswith('script') for node in document.iter())
    assert 'onclick=' not in svg(graph)
    assert '<script>' not in svg(graph)


def test_general_mermaid_directives_are_refused():
    with pytest.raises(FlowError): parse_safe_mermaid('flowchart TB\n%%{init: {}}%%')


def test_korean_cell_width_and_ansi_removal():
    assert cell_slice('가나다 abc',0,4)=='가나'
    assert safe_text('a\x1b[31mb\x1b[0m\x1b]52;c;c2VjcmV0\x07c')=='abc'


def test_long_evidence_shows_cited_focus_with_context():
    from projectflow.render import EXCERPT_CONTEXT, evidence_excerpt
    quote = "A" * 500 + "CITED PART" + "B" * 500
    item = {"quote": quote, "focus": [[500, 510]]}
    excerpt = evidence_excerpt(item)
    assert excerpt == "…" + "A" * EXCERPT_CONTEXT + "CITED PART" + "B" * EXCERPT_CONTEXT + "…"
    assert evidence_excerpt({"quote": quote}) is None
    assert evidence_excerpt({"quote": "short CITED", "focus": [[6, 11]]}) is None
    assert evidence_excerpt({"quote": quote, "focus": [[900, 5000]]}) is None
