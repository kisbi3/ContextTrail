"""Deterministic SYNTHETIC fixtures. Not an AI substitute, not a production Runner."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .schema import DELTA_ITEM_ARRAYS, EVENT_FIELDS, PATCH_ARRAYS, patch_key
from .util import FlowError, dumps


CASES = [
    ("우선 JSON 파일로 저장하자.", "decision", "JSON 저장 채택", "adopted", "user", "explicit_statement"),
    ("SQLite도 검토할 수 있습니다.", "proposal", "SQLite 대안 제안", "proposed", "assistant", "explicit_statement"),
    ("JSON 저장 코드 patch 적용 완료.", "action", "JSON 저장 구현", "applied", "tool", "tool_record"),
    ("동시 쓰기 테스트: FAILED — JSONDecodeError", "outcome", "동시 쓰기 테스트 실패", "observed_failure", "tool", "tool_record"),
    ("동시 쓰기 때문에 SQLite로 바꾸자.", "revision", "SQLite로 전환 결정", "adopted", "user", "explicit_statement"),
    ("SQLite로 변경했고 테스트도 통과했습니다.", "outcome", "SQLite 변경 완료 보고", "reported_complete", "assistant", "explicit_statement"),
    ("SQLite 동시 쓰기 테스트: 4 passed", "outcome", "SQLite 동시 쓰기 확인", "observed_success", "tool", "tool_record"),
]


def review_patch_answer(proposed: dict, answer: dict) -> dict:
    """The fixture's own answer as a patch over the proposal: only what it adds, changes or drops."""
    patch = {}
    for array in PATCH_ARRAYS:
        before = {patch_key(array, item): item for item in proposed[array]}
        patch[array] = [copy.deepcopy(item) for item in answer[array]
                        if before.get(patch_key(array, item)) != item]
    return {"status": answer["status"], "read_requests": [], "snapshot_id": answer["snapshot_id"],
            "base_graph_version": answer["base_graph_version"],
            "review_resolutions": copy.deepcopy(answer["review_resolutions"]),
            "limitations": copy.deepcopy(answer["limitations"]), "patch": patch,
            "remove": [{"operation": array, "item_id": item["id"]} for array in DELTA_ITEM_ARRAYS
                       for item in proposed[array] if item["id"] not in {x["id"] for x in answer[array]}]}


class FixtureRunner:
    name, version, model, is_mock = "synthetic-fixture", "fixture-v1", None, True

    def __init__(self):
        self.calls = 0
        self.tasks = []

    def preflight(self):
        return {"runner": self.name, "live_model_test": False}

    def run(self, task, schema, cancel):
        self.calls += 1
        self.tasks.append(copy.deepcopy(task))
        data = task["data"]
        if task["stage"] == "extract":
            candidates = []
            for record in data["new_records"]:
                text = "\n".join(line["text"] for line in record["lines"])
                matching = next((case for case in CASES if case[0] == text), None)
                if not matching:
                    continue
                _, kind, title, status, actor, basis = matching
                candidates.append({"id": "tmp:" + record["source_id"], "kind": kind, "title": title,
                    "summary": text, "status": status, "actor": actor, "basis": basis,
                    "session_ids": [record["session_id"]] if record.get("session_id") else [],
                    "worktree_ids": [record["worktree_id"]] if record.get("worktree_id") else [],
                    "recorded_at": record.get("recorded_at"), "occurred_at": None,
                    "evidence": [{"source_id": record["source_id"], "start_line": 1,
                                  "end_line": len(record["lines"]), "quote": text}]})
            return {"status": "complete", "read_requests": [], "snapshot_id": data["snapshot_id"],
                    "unit_id": data["unit_id"], "event_candidates": candidates, "edge_candidates": [],
                    "existing_event_matches": [], "open_items": [], "limitations": [], "unprocessed_record_ids": []}
        output = {"status": "complete", "read_requests": [], "snapshot_id": data["snapshot_id"],
                  "base_graph_version": data["base_graph_version"], "events_to_add": [], "events_to_update": [],
                  "edges_to_add": [], "edges_to_invalidate": [], "open_items_to_upsert": [],
                  "open_items_to_resolve": [], "candidate_resolutions": [], "change_attributions": [],
                  "review_issues": [],
                  "review_resolutions": [{"issue_id": issue["id"], "status": "resolved",
                      "reason": "원문에서 수정 대상(JSON 저장 채택)을 찾아 revises로 이었습니다."
                                if issue.get("signal") == "unlinked_revision" else
                                "합성 리뷰 규칙에서 제안 변경과 원문이 일치합니다.",
                      "evidence": issue["evidence"]} for issue in data.get("review_issues", [])],
                  "limitations": ["이 그래프는 합성 fixture와 Mock Runner로 생성했습니다. 실제 AI 복원 정확도를 나타내지 않습니다."]}
        candidates = data["validated_candidates"]["event_candidates"]
        existing = data["existing_events"]
        evidence = data.get("existing_evidence", {})
        for candidate in candidates:
            sid = candidate["evidence"][0]["source_id"]
            match = next((event for event in existing if any(evidence.get(i, {}).get("source_id") == sid
                                                             for i in event["evidence_ids"])), None)
            if match:
                output["events_to_update"].append({"id": match["id"], "reason": "수정된 원문을 다시 반영합니다.",
                    "evidence": candidate["evidence"], "changes": {key: candidate[key] for key in EVENT_FIELDS}})
            else:
                output["events_to_add"].append(candidate)
            output["candidate_resolutions"].append({"candidate_id": candidate["id"], "candidate_kind": "event",
                "disposition": "updated" if match else "added", "target_ids": [match["id"] if match else candidate["id"]],
                "reason": "합성 fixture의 고정 통합 규칙", "evidence": candidate["evidence"]})
        # Fixture-only known relations, supported by the fixture's explicit statements.
        all_events = existing + output["events_to_add"]
        by_title = {event["title"]: event for event in all_events}
        pairs = [("JSON 저장 채택", "SQLite 대안 제안", "follows"),
                 ("JSON 저장 채택", "JSON 저장 구현", "follows"),
                 ("JSON 저장 구현", "동시 쓰기 테스트 실패", "verifies"),
                 ("동시 쓰기 테스트 실패", "SQLite로 전환 결정", "motivates"),
                 ("JSON 저장 채택", "SQLite로 전환 결정", "revises"),
                 ("SQLite 대안 제안", "SQLite로 전환 결정", "follows"),
                 ("SQLite로 전환 결정", "SQLite 변경 완료 보고", "follows"),
                 ("SQLite 변경 완료 보고", "SQLite 동시 쓰기 확인", "follows")]
        added_ids = {e["id"] for e in output["events_to_add"]}
        # The first integration misses the revision's target, as a real model sometimes does;
        # the code signal then sends it to review, which draws the link.
        reviewing_revision = any(issue.get("signal") == "unlinked_revision"
                                 for issue in data.get("review_issues", []))
        for n, (left, right, relation) in enumerate(pairs):
            if relation == "revises" and not reviewing_revision:
                continue
            if left in by_title and right in by_title and by_title[right]["id"] in added_ids:
                target = by_title[right]
                output["edges_to_add"].append({"id": f"tmp:edge{n}", "from_event_id": by_title[left]["id"],
                    "to_event_id": target["id"], "relation": relation,
                    "basis": "explicit" if relation == "motivates" else "structural",
                    "evidence": target["evidence"], "rationale": "합성 fixture에 명시된 순서/전환 이유", "active": True})
        candidate_ids = [item["id"] for key in ("event_candidates", "edge_candidates", "open_items")
                         for item in data["validated_candidates"][key]]
        for operation in ("events_to_add", "events_to_update", "edges_to_add", "edges_to_invalidate",
                          "open_items_to_upsert", "open_items_to_resolve"):
            for item in output[operation]:
                # The candidate an item was added from, else one citing the same source.
                matching = next((candidate["id"] for candidate in candidates if candidate["id"] == item["id"]),
                                next((candidate["id"] for candidate in candidates
                                      if candidate["evidence"][0]["source_id"] in
                                      {citation["source_id"] for citation in item.get("evidence", [])}), None))
                output["change_attributions"].append({"operation": operation, "item_id": item["id"],
                    "candidate_ids": [matching or candidate_ids[0]],
                    "reason": "합성 fixture의 고정 통합 규칙", "evidence": item.get("evidence") or candidates[0]["evidence"]})
        if data.get("evidence_policy"):
            # A mock that follows the reuse policy: only evidence a candidate carries is left out.
            # Edges the fixture invents have no candidate behind them, so they keep their quote.
            targets = {target for item in output["candidate_resolutions"] for target in item["target_ids"]}
            for item in output["candidate_resolutions"] + output["change_attributions"]:
                item["evidence"] = []
            for item in [*output["events_to_add"], *output["events_to_update"]]:
                if item["id"] in targets:
                    item["evidence"] = []
        # A review that was asked for a patch answers only the items it changes.
        if "patch" in schema["properties"]:
            return review_patch_answer(data.get("proposed_graph_delta") or data["draft_graph_delta"], output)
        return output


def create_demo(directory: Path) -> tuple[Path, Path, Path]:
    directory = directory.expanduser().resolve()
    if directory.exists() and any(directory.iterdir()):
        raise FlowError("demo는 비어 있거나 새 디렉터리에서만 생성할 수 있습니다.")
    folder, codex_home, claude_home = directory / "sample-project", directory / "fixture-codex", directory / "fixture-claude"
    folder.mkdir(parents=True, exist_ok=True)
    codex = codex_home / "sessions" / "demo.jsonl"
    claude = claude_home / "projects" / "demo" / "demo-claude.jsonl"
    codex.parent.mkdir(parents=True)
    claude.parent.mkdir(parents=True)
    records = [{"type": "session_meta", "payload": {"id": "demo-codex", "cwd": str(folder)}}]
    for n, case in enumerate(CASES[:4]):
        text, _, _, _, actor, _ = case
        payload = ({"type": "function_call_output", "call_id": f"demo-call-{n}", "output": text}
                   if actor == "tool" else {"type": "message", "role": actor, "content": [{"type": "input_text" if actor == "user" else "output_text", "text": text}]})
        records.append({"type": "response_item", "timestamp": f"2026-09-22T10:0{n}:00Z", "payload": payload})
    codex.write_text("\n".join(dumps(r) for r in records) + "\n", encoding="utf-8")
    records = []
    for n, case in enumerate(CASES[4:6], 4):
        text, _, _, _, actor, _ = case
        records.append({"type": actor, "uuid": f"demo-record-{n}", "sessionId": "demo-claude",
                        "cwd": str(folder), "timestamp": f"2026-09-22T10:0{n}:00Z",
                        "message": {"role": actor, "content": [{"type": "text", "text": text}]}})
    claude.write_text("\n".join(dumps(r) for r in records) + "\n", encoding="utf-8")
    (directory / "DEMO_ONLY.txt").write_text("합성 데이터입니다. 실제 사용자 프로젝트나 AI 분석 결과가 아닙니다.\n", encoding="utf-8")
    return folder, codex_home, claude_home
