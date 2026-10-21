"""`contexttrail refresh`: a graph built under older rules read again under today's, by code, with no model call."""
import json
from dataclasses import replace

import pytest

from contexttrail import cli
from contexttrail.diagram import request_turns
from contexttrail.model import SourceRecord, empty_graph
from contexttrail.refresh import RETIRED_REASON, refresh, summary_lines
from contexttrail.render import graph_summary, status_labels
from contexttrail.schema import is_request, link_forks, retired, story_view
from contexttrail.util import FlowError


def record(key, role, text, *, session="root-1", provider="codex", at="2026-10-01T10:00:00Z", **fields):
    return SourceRecord(key, provider, session, role, text, {"kind": "jsonl", "path": f"/logs/{session}.jsonl", "line": 1},
                        recorded_at=at, **fields)


def event(key, kind, status, source, *, actor="assistant", basis="explicit_statement", title=None):
    return {"id": key, "kind": kind, "status": status, "title": title or key, "summary": f"{key} summary", "actor": actor,
            "basis": basis, "session_ids": [source.session_id], "worktree_ids": [], "recorded_at": source.recorded_at,
            "occurred_at": None, "evidence_ids": [f"e_{key}"]}


def turn(left, right):
    return {"id": f"turn-{right}", "from_event_id": left, "to_event_id": right, "relation": "follows",
            "basis": "structural", "evidence_ids": [f"e_{left}"], "rationale": "dialog order", "active": True,
            "origin": "dialog_turn"}


def publish_old(store, cited: dict, edges=()):
    """A graph as an older version published it: from the records as they were read then."""
    graph = empty_graph(store.scope_id)
    graph["events"] = [item[0] for item in cited.values()]
    graph["edges"] = list(edges)
    evidence = {f"e_{key}": {"id": f"e_{key}", "source_id": source.source_id, "start_line": 1, "end_line": 1,
                             "quote": source.content.splitlines()[0], "source": source.metadata()}
                for key, (_, source) in cited.items()}
    store.ingest([source for _, source in cited.values()])
    return store.publish(graph, [], {}, evidence, expected_version=0)


def test_refresh_reads_who_wrote_each_event_and_its_step_again_from_the_records_read_today(laboratory):
    _, store, engine, records, _ = laboratory
    old_agent = {"kind": "subagent", "parent_session_id": "root-1", "agent_nickname": "Dirac"}
    said = record("said", "assistant", "The plots are drawn.", session="sub-1", lineage=old_agent,
                  authoring={"model": "gpt-6-luna", "effort": "high"})
    test = record("test", "tool_call", "Tool: exec_command\n{\"cmd\": \"pytest -q\"}", tool_call_id="c1")
    passed = record("passed", "tool_result", "3 passed", tool_call_id="c1")
    publish_old(store, {"said": (event("said", "outcome", "reported_complete", said), said),
                        "passed": (event("passed", "outcome", "observed_success", passed, basis="tool_record",
                                         title="Check passed: pytest -q"), passed)})
    before = store.graph()
    assert before["events"][0]["authoring"][0]["agent"] == "Dirac" and "step" not in before["events"][1]
    # Today's reader records the agent's path (its task) and what kind of call the run was.
    records[:] = [replace(said, lineage={**old_agent, "agent_path": "/root/plots"}),
                  replace(test, step={"hint": "test"}), replace(passed, step={"hint": "test"})]
    result = refresh(engine)
    assert result["status"] == "refreshed" and result["runner_calls"] == 0
    assert result["counts"]["agents_renamed"] == 1 and result["counts"]["steps_changed"] == 1
    after = store.graph()
    assert after["version"] == before["version"] + 1
    by_id = {e["id"]: e for e in after["events"]}
    assert by_id["said"]["authoring"] == [{"tool": "codex", "model": "gpt-6-luna", "effort": "high",
                                           "role": "subagent", "agent": "plots"}]
    assert by_id["passed"]["step"] == "check"
    # Nothing a model wrote changes, and the time of the last analysis stays: the refresh has its own.
    for old in before["events"]:
        assert {k: by_id[old["id"]][k] for k in ("title", "summary", "status", "kind")} == \
               {k: old[k] for k in ("title", "summary", "status", "kind")}
    assert after["analyzed_at"] == before["analyzed_at"] and after["refreshed_at"]
    assert summary_lines(result)[0] == f"그래프 v{before['version']} → v{after['version']} (지금 규칙으로 다시 읽음, AI 호출 없음)"


def test_refresh_sets_aside_requests_that_are_not_the_persons_under_todays_rules(laboratory):
    _, store, engine, records, _ = laboratory
    compact = record("compact", "user", "/compact", at="2026-10-01T10:00:00Z")
    plugins = record("plugins", "user", "<recommended_plugins>\n- figma\n</recommended_plugins>", at="2026-10-01T10:01:00Z")
    asked = record("asked", "user", "Fix the parser, please.", at="2026-10-01T10:02:00Z")
    briefed = record("briefed", "user", "Draw the plots.", session="sub-1", at="2026-10-01T10:03:00Z",
                     lineage={"kind": "subagent", "parent_session_id": "root-1"})
    gone = record("gone", "user", "A message from a transcript since deleted.", session="old-1", at="2026-10-01T09:00:00Z")
    work = record("work", "assistant", "Compacted; the parser is next.", at="2026-10-01T10:04:00Z")
    cited = {key: (event(key, "question", "asked", source, actor="user"), source)
             for key, source in (("compact", compact), ("plugins", plugins), ("asked", asked), ("briefed", briefed),
                                 ("gone", gone))}
    cited["work"] = (event("work", "decision", "adopted", work), work)
    publish_old(store, cited, [turn("compact", "work")])
    records[:] = [compact, plugins, asked, briefed, work]  # the deleted transcript is not read any more
    result = refresh(engine)
    assert sorted(result["retired"]) == ["briefed", "compact", "plugins"]
    assert result["counts"]["requests_unread"] == 1
    graph = store.graph()
    by_id = {e["id"]: e for e in graph["events"]}
    for key in ("compact", "plugins", "briefed"):
        assert by_id[key]["status"] == "withdrawn" and retired(by_id[key]) and not is_request(by_id[key])
        assert by_id[key]["invalidation"]["reason"] == RETIRED_REASON and by_id[key]["invalidation"]["by"] == "refresh"
    assert by_id["asked"]["status"] == "asked" and is_request(by_id["asked"])
    assert by_id["gone"]["status"] == "asked" and "invalidation" not in by_id["gone"]  # its records were not read
    # Its relations stay, but no view takes it for a request.
    assert next(edge for edge in graph["edges"] if edge["id"] == "turn-work")["active"]
    assert sorted(key for key, _ in request_turns(graph) if key) == ["asked", "gone"]
    assert status_labels(graph)["compact"] == "요청 아님 · 지금 규칙으로는 사람의 요청이 아님"
    assert graph_summary(graph)["questions_unanswered"] == 2
    story = story_view(graph)
    assert {e["id"] for e in story["events"]} >= {"asked", "gone"}
    assert not {e["id"] for e in story["events"]} & {"compact", "plugins", "briefed"}
    assert "compact" not in story["homes"] and story["unplaced"] == 0
    assert link_forks(graph, lambda _: None) == 0
    # One set aside under a rule since dropped is given back its status once its message reads as the person's.
    asked_again = {**by_id["asked"], "status": "withdrawn",
                   "invalidation": {"reason": RETIRED_REASON, "by": "refresh", "status_before": "asked"}}
    graph["events"] = [asked_again if e["id"] == "asked" else e for e in graph["events"]]
    store.publish(graph, [], {}, {}, expected_version=graph["version"])
    again = refresh(engine)
    assert again["restored"] == ["asked"] and again["retired"] == []
    restored = next(e for e in store.graph()["events"] if e["id"] == "asked")
    assert restored["status"] == "asked" and "invalidation" not in restored


def test_refresh_hangs_a_sub_agents_work_under_its_delegation_graph_wide(laboratory):
    _, store, engine, records, _ = laboratory
    asked = record("asked", "user", "Have the plots drawn separately.", at="2026-10-01T10:00:00Z")
    spawn = record("spawn", "tool_call", "Tool: spawn_agent\n{\"task_name\": \"plots\"}", tool_call_id="c1",
                   at="2026-10-01T10:01:00Z")
    lineage = {"kind": "subagent", "parent_session_id": "root-1", "agent_path": "/root/plots"}
    drawn = record("drawn", "assistant", "The plots are drawn.", session="sub-1", lineage=lineage, at="2026-10-01T10:02:00Z")
    publish_old(store, {"asked": (event("asked", "question", "asked", asked, actor="user"), asked),
                        "spawn": (event("spawn", "action", "applied", spawn, basis="tool_record"), spawn),
                        "drawn": (event("drawn", "outcome", "reported_complete", drawn), drawn)},
                [turn("asked", "spawn"), turn("asked", "drawn")])
    assert "delegated_by" not in next(e for e in store.graph()["events"] if e["id"] == "drawn")
    # Today's scan marks the call as the delegation that started the agent at /root/plots.
    records[:] = [asked, replace(spawn, step={"hint": "delegate", "agent": "/root/plots", "to": "plots"}), drawn]
    result = refresh(engine)
    assert result["counts"]["delegation_links_added"] == 1 and result["counts"]["delegations_paired"] == 1
    assert result["counts"]["turn_links_replaced"] == 1
    graph = store.graph()
    by_id = {e["id"]: e for e in graph["events"]}
    assert by_id["spawn"]["step"] == "delegate" and by_id["spawn"]["delegated_to"] == "plots"
    assert by_id["drawn"]["delegated_by"] == "spawn"
    links = {(e["from_event_id"], e["to_event_id"]): e for e in graph["edges"]}
    assert links[("spawn", "drawn")]["origin"] == "delegation" and not links[("asked", "drawn")]["active"]
    # What a fresh publish of the same graph would give: publishing it again changes nothing.
    fresh = store.derived(graph)
    assert {k: v for k, v in fresh.items() if k != "version"} == {k: v for k, v in graph.items() if k != "version"}


def test_a_second_refresh_publishes_nothing_and_neither_is_the_screens_last_check(laboratory):
    _, store, engine, records, _ = laboratory
    asked = record("asked", "user", "Fix the parser, please.")
    publish_old(store, {"asked": (event("asked", "question", "asked", asked, actor="user"), asked)})
    records[:] = [asked]
    store.set_meta("last_check", {"status": "complete", "at": "earlier"})
    version = store.latest_version()
    result = refresh(engine)
    assert result["status"] == "noop" and result["graph_version"] == version == store.latest_version()
    assert summary_lines(result) == [f"바뀐 것 없음: 그래프 v{version}은 이미 지금 규칙대로입니다. AI 호출 없음."]
    assert store.get_meta("last_check") == {"status": "complete", "at": "earlier"}
    with store.connection() as db:
        runs = [(row["status"], json.loads(row["manifest"])["trigger"]) for row in db.execute("SELECT * FROM analysis_runs")]
    assert runs == [("noop", "refresh")]
    assert store.llm_calls() == []


def test_refresh_is_refused_while_an_analysis_runs(laboratory):
    _, store, engine, records, _ = laboratory
    with store.analyze_lock():
        with pytest.raises(FlowError, match="이미 분석이 진행 중"):
            refresh(engine)
    assert store.latest_version() == 0


def test_refresh_of_a_folder_with_no_state_creates_none(tmp_path, capsys):
    folder = tmp_path / "plain"
    folder.mkdir()
    assert cli.main(["refresh", str(folder)]) == 0
    assert "상태가 없습니다" in capsys.readouterr().out
    assert not (folder / ".contexttrail").exists()
