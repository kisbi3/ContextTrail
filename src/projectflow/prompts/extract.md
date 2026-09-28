Task: extract project event and relation candidates from the assigned new records.

The input has unit_id, snapshot_id, the new records, earlier context marked context_only, an
outline of related existing events, and a manifest of evidence you can read.

Code also sends two lists taken from the new records. Both are for finding things; an event's
evidence is always a quote from the records.
- user_requests: this unit's user messages. Make one actor=user event per message.
- tool_steps: tool calls in order, with call (the call record), result (the result record),
  tool, hint, target (file or command) and result_head (first line of the result).
  hint comes only from the tool name and patch markers: edit changed a file, read only read,
  run is any other execution.
  - Every edit call must appear in some event's evidence (a quote from its call or result);
    code checks this. Several edits may form one change event, but split them when different
    runs checked them.
  - A run's result is the evidence for a result (outcome) event. Do not miss test, install or
    script results.
  - A read is normally not an event. Its result is sent as a short head only; read the rest with
    read_records if a claim needs it.
- Long tool results, injected instructions and summary records arrive as head and tail only. The
  lines between are listed in that record's omitted (start_line to end_line). If a judgement or
  a quote needs them, request that source_id and line range with read_records. Never quote lines
  you have not seen.

1. Read the assigned new records and work out what this stretch was trying to solve.
2. Group continuous work toward one goal, but keep failures, changes of method and withdrawals as
   separate events. Split a change from the run that checked it, and changes checked by different runs.
3. Extract goal/question/proposal/decision/action/outcome/revision events as needed, following
   the event rules: leave out looking around that did not matter.
4. For claims of implementation, runs or success, check the related diff or tool result.
   Make each run result an outcome event and link it from the change it checked with verifies.
   Without evidence leave the status reported/unknown.
   If an event answered a question, link it from the question with answers.
5. If the records refer to a past decision, read the related existing event and the source lines
   you need. Do not recreate context_only material as new events.
6. Give each relation candidate the evidence that supports the link and whether it is explicit or inferred.
7. Collect unfinished work, conflicts, material you could not access and unprocessed records.

Return:
- status and read_requests
- Use a limited `search_events` request only when no related event is listed. Put the query and the
  current unit_id in it, and read a found event's evidence separately with read_existing_event.
- unit_id, snapshot_id
- event_candidates, edge_candidates
- existing_event_matches
- open_items, limitations, unprocessed_record_ids

Do not modify the existing graph. With no new events, return empty candidate arrays.

ID contract:
- New event_candidates, edge_candidates and open_items each get a unique id with the `tmp:` prefix.
- from_event_id/to_event_id of edge_candidates and related_event_ids of open_items refer only to
  this answer's candidate IDs or existing event IDs actually present in the input.
- In existing_event_matches, candidate_id is one of this answer's event candidates and
  existing_event_id an existing event ID present in the input.
- edge_candidates have active true.

8. Inputs with the same fragment_of are consecutive pieces of one record. Do not split or duplicate
   events at piece boundaries.
9. Compaction/summary records may guide you but are not direct evidence like the original words or tool results.
10. Attribute subagent records to the delegated agent's actions and results, not to the parent user or parent assistant.
