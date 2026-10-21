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
    motive = {**turn("compact", "work"), "id": "motive", "relation": "motivates", "basis": "inferred",
              "rationale": "the summary named the parser"}
    motive.pop("origin")
    publish_old(store, cited, [turn("compact", "work"), motive])
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
    # Its relations stay (a relation of its own reaches the work, so the turn link is not moved), but no view takes it
    # for a request.
    assert all(edge["active"] for edge in graph["edges"] if edge["id"] in ("turn-work", "motive"))
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


REPLY = ('<send_user_message_question_reply>\n[{"questionItemId":"q","question":"Which paper?","answer":"%s"}]\n'
         '</send_user_message_question_reply>')


def test_the_persons_answer_to_the_agents_question_is_their_decision_or_left_as_it_is_never_set_aside(laboratory):
    _, store, engine, records, _ = laboratory
    asked = record("asked", "user", "Reproduce the paper's results.", at="2026-10-01T10:00:00Z")
    offer = record("offer", "tool_call", 'Tool: request_user_input\n{"questions": [{"title": "Which paper?", '
                   '"options": ["Donier (2015)", "Sato (2023)"]}]}', at="2026-10-01T10:01:00Z")
    picked = record("picked", "user", REPLY % "Donier (2015)", at="2026-10-01T10:02:00Z")
    work = record("work", "assistant", "Reproducing Donier (2015).", at="2026-10-01T10:03:00Z")
    free = record("free", "user", REPLY % "Whichever is shorter", at="2026-10-01T10:04:00Z")
    cited = {key: (event(key, "question", "asked", source, actor="user"), source)
             for key, source in (("asked", asked), ("picked", picked), ("free", free))}
    cited["work"] = (event("work", "decision", "adopted", work), work)
    publish_old(store, cited, [turn("picked", "work")])  # an older version took each answer for a request
    records[:] = [asked, offer, picked, work, free]
    result = refresh(engine)
    assert result["retired"] == [] and result["chosen"] == ["picked"]
    assert result["counts"]["choices_made_decisions"] == 1
    graph = store.graph()
    by_id = {e["id"]: e for e in graph["events"]}
    # One of the options the agent offered: the person's decision, as a fresh publish makes it.
    choice = by_id["picked"]
    assert (choice["kind"], choice["status"], choice["actor"]) == ("decision", "adopted", "user")
    assert (choice["title"], choice["summary"]) == ("Donier (2015)", "Which paper? → Donier (2015)")
    assert not is_request(choice) and not retired(choice)
    # Its relations stay, and it hangs in the turn of the request it continues.
    links = {(e["from_event_id"], e["to_event_id"]): e for e in graph["edges"]}
    assert links[("picked", "work")]["active"]
    assert links[("asked", "picked")]["origin"] == "dialog_turn" and links[("asked", "picked")]["active"]
    # A free answer is the person's words: left as it is.
    assert (by_id["free"]["kind"], by_id["free"]["status"]) == ("question", "asked") and not retired(by_id["free"])
    assert refresh(engine)["status"] == "noop"


def test_work_under_a_sub_agents_or_unattended_runs_prompt_set_aside_hangs_as_a_fresh_publish_hangs_it(laboratory):
    from contexttrail.analysis import link_request_turns
    from contexttrail.schema import record_evidence
    _, store, engine, records, _ = laboratory
    asked = record("asked", "user", "Have the plots drawn.", at="2026-10-01T10:00:00Z")
    sub = {"kind": "subagent", "parent_session_id": "root-1"}
    briefed = record("briefed", "user", "Draw the plots.", session="sub-1", lineage=sub, at="2026-10-01T10:01:00Z")
    drawn = record("drawn", "assistant", "The plots are drawn.", session="sub-1", lineage=sub, at="2026-10-01T10:02:00Z")
    alone = {"kind": "unattended", "agent_type": "codex exec"}
    prompt = record("prompt", "user", "Summarise the log.", session="exec-1", lineage=alone, at="2026-10-01T10:03:00Z")
    summed = record("summed", "assistant", "The log is summarised.", session="exec-1", lineage=alone,
                    at="2026-10-01T10:04:00Z")
    cited = {key: (event(key, "question", "asked", source, actor="user"), source)
             for key, source in (("asked", asked), ("briefed", briefed), ("prompt", prompt))}
    cited.update({key: (event(key, "outcome", "reported_complete", source), source)
                  for key, source in (("drawn", drawn), ("summed", summed))})
    publish_old(store, cited, [turn("briefed", "drawn"), turn("prompt", "summed")])
    records[:] = [asked, briefed, drawn, prompt, summed]
    result = refresh(engine)
    assert sorted(result["retired"]) == ["briefed", "prompt"] and result["counts"]["turn_links_moved"] == 2
    graph = store.graph()
    links = {edge["id"]: edge for edge in graph["edges"]}
    # What a fresh publish of the same records gives: the sub-agent's work in its parent's turn, the unattended
    # run's work under no message of the person's.
    pool = {r.source_id: r for r in records}
    fresh, _ = link_request_turns({"events": [cited["asked"][0], cited["drawn"][0], cited["summed"][0]], "edges": []},
                                  {"events": [], "edges": []}, pool, [], {f"e_{key}": {**record_evidence(source),
                                                                                       "id": f"e_{key}"}
                                                                          for key, source in pool.items()},
                                  lambda ids: {}, "run")
    expected = {(e["id"], e["from_event_id"], e["to_event_id"], e["rationale"]) for e in fresh["edges"]}
    active_turns = {(e["id"], e["from_event_id"], e["to_event_id"], e["rationale"]) for e in graph["edges"]
                    if e["active"] and e.get("origin") == "dialog_turn"}
    assert active_turns == expected and {(f, t) for _, f, t, _ in expected} == {("asked", "drawn")}
    new = next(key for key, _, _, _ in expected)
    assert links["turn-drawn"]["invalidation"]["replaced_by"] == new and not links["turn-drawn"]["active"]
    assert not links["turn-summed"]["active"] and "replaced_by" not in links["turn-summed"]["invalidation"]
    assert refresh(engine)["status"] == "noop"
    # A brief read as the person's again (here: its record no longer a sub-agent's) is given back its own links.
    records[:] = [asked, replace(briefed, lineage={}), drawn, prompt, summed]
    again = refresh(engine)
    assert again["restored"] == ["briefed"]
    links = {edge["id"]: edge for edge in store.graph()["edges"]}
    assert links["turn-drawn"]["active"] and "invalidation" not in links["turn-drawn"]
    assert not links[new]["active"] and links[new]["invalidation"]["replaced_by"] == "turn-drawn"
    assert refresh(engine)["status"] == "noop"


def test_a_later_piece_of_a_long_message_is_read_with_its_first_and_alone_is_counted_apart(laboratory):
    _, store, engine, records, _ = laboratory

    def piece(key, text, of, index, at):
        return record(key, "user", text, at=at, lineage={"fragment_of": of, "fragment_index": index,
                                                          "fragment_count": 2})
    plugins = [piece("plugins-1", "<recommended_plugins>\n- figma", "plugins", 1, "2026-10-01T10:00:00Z"),
               piece("plugins-2", "- slack\n</recommended_plugins>", "plugins", 2, "2026-10-01T10:00:00Z")]
    words = [piece("words-1", "Fix the parser, " + "please " * 3, "words", 1, "2026-10-01T10:01:00Z"),
             piece("words-2", "and its tests.", "words", 2, "2026-10-01T10:01:00Z")]
    tail = piece("tail-2", "the rest of a message whose first piece no event cites", "tail", 2, "2026-10-01T10:02:00Z")
    graph = empty_graph(store.scope_id)
    graph["events"] = [{**event(key, "question", "asked", pieces[0], actor="user"),
                        "evidence_ids": [f"e_{r.source_id}" for r in pieces]}
                       for key, pieces in (("plugins", plugins), ("words", words), ("tail", [tail]))]
    evidence = {f"e_{r.source_id}": {"id": f"e_{r.source_id}", "source_id": r.source_id, "start_line": 1,
                                     "end_line": 1, "quote": r.content.splitlines()[0], "source": r.metadata()}
                for r in (*plugins, *words, tail)}
    store.ingest([*plugins, *words, tail])
    store.publish(graph, [], {}, evidence, expected_version=0)
    records[:] = [*plugins, *words, tail]
    result = refresh(engine)
    assert result["retired"] == ["plugins"]
    assert result["counts"]["requests_piece_only"] == 1 and result["counts"]["requests_unread"] == 0
    by_id = {e["id"]: e for e in store.graph()["events"]}
    assert is_request(by_id["words"]) and is_request(by_id["tail"])
    assert any("뒷부분만 인용" in line for line in summary_lines(result))
