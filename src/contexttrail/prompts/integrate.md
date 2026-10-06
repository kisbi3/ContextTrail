Task: write the GraphDelta that brings the new event candidates into the project's existing history.

Input: base_graph_version, snapshot_id, related existing events, relations and open items, and the
validated candidates with their source evidence.

1. Tell a duplicate extraction of the same records from a new attempt. Do not merge separate runs,
   retries or re-checks even when the topic is the same.
2. Update only affected states: a completion report actually checked, a failure, a withdrawal, an
   alternative adopted. When a new run result checks an existing change, do not give the change a
   success status; add the outcome event and a verifies relation. Link an event that answered an
   existing question with answers. When updating an action/revision that still has an observed
   status from the old contract, change it to a status allowed now, such as applied.
3. Connect new events to the flow they belong to. Without evidence, keep them as a separate branch.
   Link an actor=user message event to the first event it started with motivates and, for a
   question, to its answer or completion report with answers.
4. Check each event's and arrow's claim against the source. Watch for overstated adoption,
   completion, success, verification or cause.
5. Check that the session/worktree and the code state at the time match.
6. Tell a real change of the project's direction from a correction of an earlier AI interpretation.
7. Update only open items that are actually still open in the records.
8. Keep titles and summaries to the event rules (output language, concrete title, summary that
   adds why and effect). Which steps become events was decided at extraction: do not exclude a
   candidate for being minor. Every run result (outcome) stays its own event.

Do not rewrite the whole graph. Return the GraphDelta contract. Keep existing IDs and descriptions
that did not change. Give reasons and evidence for changes, relation invalidations and resolved
open items. If you need more material, return needs_evidence.

`candidate_resolutions` records every input `event_candidates`, `edge_candidates` and `open_items`
item exactly once, each with the original candidate ID and kind, a disposition
(added/updated/duplicate/excluded), the actual GraphDelta or existing graph target ID, a short
reason and a source quote. New events/relations/open items are added and target their GraphDelta
tmp ID. Updating existing content is updated; content already in an existing item is duplicate;
not applied for lack of evidence is excluded. Exclusions also carry a reason and a quote. Do not
add new events that no candidate leads to. A resolution's target_ids are items of the candidate's own
kind: an event candidate targets events. When a new event closes an existing open item, keep the event
candidate's own resolution and put the open item in open_items_to_resolve, attributed to that candidate.

`change_attributions` records every addition, update, invalidation and resolution in the
GraphDelta exactly once. operation and item_id point at the GraphDelta array item, candidate_ids
are the input candidate IDs whose candidate_resolutions target that same item, reason is why, and evidence is an exact quote supporting the change. Do
not return a change of meaning that cannot be tied to a candidate.

`review_issues` optionally records ambiguity or conflict you noticed. Each has a unique ID,
origin=`model_uncertainty`, the stage and target candidate/graph ID, the signal, a question to
check and a source quote. Do not declare a factual error from your own signal alone. In a normal
integration call `review_resolutions` is an empty array. In a review call that passes
review_issues, handle every issue exactly once with a status (resolved/modified/excluded/unresolved),
a reason and a quote. Do not mark an unresolved issue resolved.

ID contract:
- New ids in events_to_add and edges_to_add are unique `tmp:`-prefixed IDs.
- A new open_items_to_upsert id also starts with `tmp:`. Use an existing ID present in the input
  only when updating that existing open item.
- Event IDs referenced by edges_to_add and open_items_to_upsert are existing event IDs in the input
  or IDs from this events_to_add.
- edges_to_add have active true.
- Use `search_events` only when no event in the existing event index fits. Put `query` and the
  current `unit_id` in the request. A search result is not evidence of identity or cause; read the
  found event's preserved evidence with `read_existing_event` before relying on it.
