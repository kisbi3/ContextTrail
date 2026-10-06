import copy
import re
import xml.etree.ElementTree as ET
import pytest
from contexttrail.demo import CASES, FixtureRunner
from contexttrail.render import (MAX_DETOUR_LANES, mermaid, parse_safe_mermaid,
                                 terminal_graph, svg)
from contexttrail.util import FlowError, safe_text, cell_slice


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
    from contexttrail.render import EXCERPT_CONTEXT, evidence_excerpt
    quote = "A" * 500 + "CITED PART" + "B" * 500
    item = {"quote": quote, "focus": [[500, 510]]}
    excerpt = evidence_excerpt(item)
    assert excerpt == "…" + "A" * EXCERPT_CONTEXT + "CITED PART" + "B" * EXCERPT_CONTEXT + "…"
    assert evidence_excerpt({"quote": quote}) is None
    assert evidence_excerpt({"quote": "short CITED", "focus": [[6, 11]]}) is None
    assert evidence_excerpt({"quote": quote, "focus": [[900, 5000]]}) is None


def _chain_with_back_edges(count, nodes=30):
    """A forward chain plus `count` edges that jump back to the first node.

    Those back edges are what force the long-edge detour gutter: they skip
    ranks, so they cannot be drawn as the one-layer S-curve.
    """
    events = [{'id': f'ev_{i}', 'kind': 'action', 'title': f'작업 {i}', 'summary': '',
               'status': 'applied', 'created_at': '2026-01-01T00:00:00Z'}
              for i in range(nodes)]
    edges = [{'id': f'e{i}', 'from_event_id': f'ev_{i}', 'to_event_id': f'ev_{i + 1}',
              'relation': 'follows', 'basis': 'structural', 'active': True,
              'rationale': 'x', 'evidence_ids': []} for i in range(nodes - 1)]
    for i in range(count):
        edges.append({'id': f'b{i}', 'from_event_id': f'ev_{nodes - 1 - i}', 'to_event_id': 'ev_0',
                      'relation': 'motivates', 'basis': 'inferred', 'active': True,
                      'rationale': 'x', 'evidence_ids': []})
    return {'events': events, 'edges': edges, 'version': 1, 'analysis_status': 'partial'}


# A detour path is the only shape written as "V.. H<lane> V.. H<target> V..".
_DETOUR_LANE = re.compile(r'<path d="M[\d.]+ [\d.]+ V-?[\d.]+ H([\d.]+) V-?[\d.]+ H')


def test_detour_lanes_never_overlap():
    """Regression: lanes were `detour_index % 10` while the canvas grew for 10,
    so the 11th detour reused lane 0 and two lines were drawn on top of each
    other. Overlapping paths read as one wrong line, so this asserts distinct
    lanes rather than trusting a modulo bound.
    """
    for back in (1, 10, 11, MAX_DETOUR_LANES):
        lanes = _DETOUR_LANE.findall(svg(_chain_with_back_edges(back)))
        assert len(lanes) == len(set(lanes)), f"{back} back edges reused a lane"
        assert len(lanes) == min(back, MAX_DETOUR_LANES)


def test_detours_beyond_the_lane_budget_are_stated_not_dropped_silently():
    graph = _chain_with_back_edges(MAX_DETOUR_LANES + 6)
    out = svg(graph)
    assert len(_DETOUR_LANE.findall(out)) == MAX_DETOUR_LANES
    assert f'긴 연결 6개는 선이 겹쳐 생략했습니다' in out


def test_detour_lane_count_drives_the_canvas_width():
    """The canvas must grow for every lane actually drawn, or lines fall off it."""
    narrow = svg(_chain_with_back_edges(3))
    wide = svg(_chain_with_back_edges(20))
    width = lambda s: int(re.search(r'width="(\d+)"', s).group(1))
    assert width(wide) > width(narrow)
    lanes = _DETOUR_LANE.findall(wide)
    assert max(float(x) for x in lanes) < width(wide)


def test_under_the_budget_draws_every_connection():
    out = svg(_chain_with_back_edges(5))
    assert '생략했습니다' not in out
