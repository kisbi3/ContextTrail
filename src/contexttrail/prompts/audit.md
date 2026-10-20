Audit: the agent that did this work already wrote part of its history as notes.

journal_audit.note_event_ids names those note events (in existing_events). The agent wrote them itself while it
worked on these records; other existing events may come from an earlier analysis. This is a check for what the
notes and the earlier analyses missed, not a second history.

- A work item or written block with `noted_by` is already recorded by those notes. Name the work items as usual,
  but write no story event for what a note already records; point at the note by its E ID instead (in a story
  event's links or a request's answered_by).
- Write story events only for the decisions, findings and changes of course that no existing event records.
- The person's requests already exist as events; answer them by their Q or E ID in `requests` as usual.
- The agent's `contexttrail note` calls and the ContextTrail reminders to write notes are bookkeeping of this graph,
  not work on the project: no story event rests on them.
- `story: []` is correct when the notes cover everything.
