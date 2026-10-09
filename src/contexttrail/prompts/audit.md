Audit: the agent that did this work already wrote part of its history as notes.

The input has journal_audit.note_event_ids. Those existing events (origin: note, shown in
existing_events with the lines they cite in existing_evidence) were written by the agent itself
while it worked on these records. Other existing events may come from an earlier analysis. This
run is a check for what the notes and the earlier analyses missed, not a second history.

- Extraction: return candidates only for decisions, changes, results and open questions that no
  existing event already records. Do not repeat an existing event. A candidate that would restate
  one is left out; if you are unsure whether a note covers it, keep the candidate and give it an
  existing_event_matches entry naming that event. A file edit that no existing event cites still
  needs an event; an edit a note already cites does not.
- A result (a run, a test, a commit) is missing when no existing event cites its tool result;
  it may check a change a note recorded, so connect it to that note event.
- Relations may start or end at an existing event, including a note. Add them to a new event only.
- The user's messages already have request events; do not write them again.
- The agent's `contexttrail note` calls, their output (a note stored, queued or refused) and the
  ContextTrail reminders to write notes are bookkeeping of this graph, not work on the project:
  return no candidate for them and relate no event to them.
- An empty result is correct when the notes cover everything: set event_candidates to [].
- Integration: leave every existing event, relation and open item as it is. Do not use
  events_to_update, edges_to_invalidate or open_items_to_resolve, and do not reuse the id of an
  existing open item. Add only the new events, relations and open items the candidates describe;
  a candidate that repeats an existing event is a duplicate of it.
