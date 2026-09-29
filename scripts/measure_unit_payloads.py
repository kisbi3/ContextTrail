"""Zero-AI measurement of extract payloads and cut points. Writes counts and sizes only (rows.json, sim.json).

Usage: measure_unit_payloads.py OUT_DIR SAMPLE PROJECT_DIR STATE_COPY_DIR [CODEX_HOME CLAUDE_HOME]
STATE_COPY_DIR holds a COPY of the project state.sqlite; the real state is never opened.
The simulation half (finest B cuts, greedy to 60% of task_chars) has only run against a tiny
synthetic project so far: check its output before trusting it.
"""
import sys, json, time, threading, dataclasses, random, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from projectflow.git_context import Scope
from projectflow.store import Store
from projectflow import analysis as A
from projectflow.util import dumps
from projectflow.model import is_user_prompt

HERE = Path(sys.argv[1]); SAMPLE = int(sys.argv[2]) if len(sys.argv) > 2 else 20
PROJECT = sys.argv[3]; STATE = Path(sys.argv[4]); CODEX = sys.argv[5] if len(sys.argv) > 5 else None; CLAUDE = sys.argv[6] if len(sys.argv) > 6 else None
scope = Scope.resolve(PROJECT)
scope = dataclasses.replace(scope, state_dir=STATE)
store = Store(scope.state_dir, scope.id)
cfg = A.AnalysisConfig(**({"codex_home": Path(CODEX), "claude_home": Path(CLAUDE)} if CODEX else {}))
engine = A.Engine(scope, store, cfg)
t0 = time.time()
snap = engine.scan()
store.ingest(snap.records); store.acknowledge_environment_context(snap.records)
issues = []
plans, _, _ = engine._plan_units(snap, issues)
pool = {r.source_id: r for r in snap.records}
lang = engine.resolve_language(snap)
graph = store.graph()
print("records", len(snap.records), "pending units", len(plans), "scan+plan s", round(time.time() - t0, 1), flush=True)

def alt_lines(item):  # lines as one numbered string instead of per-line objects
    if "lines" not in item: return item
    return {**item, "lines": "\n".join(f"{l['line']}| {l['text']}" for l in item["lines"])}

def assemble(source_ids, unit_id):
    assigned = [pool[i] for i in source_ids]
    h = A.Harness(None, pool, graph, store, cfg, threading.Event(), unit_id=unit_id)
    ctx = h.context(assigned)
    unit = {"id": unit_id, "sources": list(source_ids)}
    data, _ = A.extract_request_data(unit, snap.id, assigned, h, ctx, [])
    task = A.IdAliases().wire(A.build_task("extract", data, lang))
    return assigned, h, ctx, data, task

def keysizes(data):
    return {k: len(dumps(v)) for k, v in data.items()}

def classify_context(assigned, h, ctx):
    call_keys = {(r.provider, r.session_id, r.tool_call_id) for r in assigned if r.tool_call_id}
    cited_sids = set()
    for e in ctx["existing_events"]:
        for i in e["evidence_ids"]:
            if i in h.saved: cited_sids.add(h.saved[i]["source_id"])
    cited_ranges = {}
    for i, it in h.saved.items():
        cited_ranges.setdefault(it["source_id"], []).append((it["start_line"], it["end_line"]))
    out = {}
    around = 0
    for item in ctx["context_only"]:
        sid = item["source_id"]; rec = pool.get(sid)
        if item.get("context_reason") == "same_worktree_nearby_time": kind = "cross"
        elif sid in cited_sids: kind = "cited"
        elif rec is not None and rec.tool_call_id and (rec.provider, rec.session_id, rec.tool_call_id) in call_keys: kind = "same_call"
        else: kind = "previous_turn"
        d = out.setdefault(kind, {"n": 0, "chars": 0, "alt_chars": 0})
        d["n"] += 1; d["chars"] += len(dumps(item)); d["alt_chars"] += len(dumps(alt_lines(item)))
        if kind == "cited":
            rs = [(a, b) for a, b in cited_ranges.get(sid, []) if True]
            keep = [l for l in item["lines"] if any(a - 5 <= l["line"] <= b + 5 for a, b in rs)]
            around += len(dumps(alt_lines({**item, "lines": keep})))
    out["_cited_around5_alt_chars"] = around
    return out

def boundaries(ordered):
    """safe boundaries by kind, same rule as _session_unit_chunks; kinds: natural/turn/run_result/other"""
    steps = A.tool_steps(ordered)
    run_results = {s["result"] for s in steps if s["hint"] == "run" and s["result"]}
    last = {}
    for i, r in enumerate(ordered):
        for kind, v in (("tool", r.tool_call_id), ("fragment", r.lineage.get("fragment_of"))):
            if v: last[(kind, str(v))] = i
    res = {}; open_until = -1; blocked = 0
    for i, r in enumerate(ordered):
        for kind, v in (("tool", r.tool_call_id), ("fragment", r.lineage.get("fragment_of"))):
            if v: open_until = max(open_until, last[(kind, str(v))])
        b = i + 1
        if b == len(ordered): continue
        if open_until >= b: blocked += 1; continue
        nxt = ordered[b]
        t0_, t1_ = A._record_timestamp(r.recorded_at), A._record_timestamp(nxt.recorded_at)
        gap = (t1_ - t0_) if t0_ is not None and t1_ is not None else None
        if nxt.lineage.get("kind") == "compaction" or (gap is not None and gap >= 3 * 3600): k = "natural"
        elif A.is_user_prompt(nxt): k = "turn"
        elif r.source_id in run_results: k = "run_result"
        else: k = "other"
        res[b] = k
    return res, blocked

results = []
overs = 0
for n, unit in enumerate(plans, 1):
    assigned, h, ctx, data, task = assemble(unit["sources"], unit["id"])
    total = len(dumps(task)); over = total > cfg.task_chars; overs += over
    ks = keysizes(data)
    raw = sum(len(r.content) for r in assigned)
    nr = data["new_records"]
    row = {"n": n, "records": len(assigned), "raw_chars": raw,
           "unit_cost": sum(A.unit_cost(r) for r in assigned), "payload": total, "over": over,
           "keys": ks, "new_records_alt": len(dumps([alt_lines(x) for x in nr])),
           "context_only_alt": len(dumps([alt_lines(x) for x in ctx["context_only"]])),
           "ctx_classes": classify_context(assigned, h, ctx)}
    steps = A.tool_steps(assigned)
    hint_of = {}
    for st in steps:
        hint_of[st["call"]] = st["hint"]
        if st["result"]: hint_of[st["result"]] = st["hint"]
    by_class = {}
    for r in assigned:
        cls = ("user_prompt" if A.is_user_prompt(r) else r.role) + ("/" + hint_of[r.source_id] if r.source_id in hint_of else "")
        d = by_class.setdefault(cls, {"n": 0, "raw_chars": 0, "unit_cost": 0})
        d["n"] += 1; d["raw_chars"] += len(r.content); d["unit_cost"] += A.unit_cost(r)
    row["by_class"] = by_class
    bnd, blocked = boundaries(assigned)
    kinds = {}
    for k in bnd.values(): kinds[k] = kinds.get(k, 0) + 1
    row["boundaries"] = {"safe_by_kind": kinds, "blocked": blocked}
    results.append((unit, row))
    if n % 20 == 0: print("assembled", n, round(time.time() - t0, 1), flush=True)

rows = [r for _, r in results]
json.dump({"rows": rows}, open(HERE / "rows.json", "w"))
print("over limit:", overs, "of", len(rows), flush=True)

# B-rule simulation on a sample: finest cut at natural/turn/run_result boundaries, then greedy to 60% target.
TARGET = int(cfg.task_chars * 0.6)
over_units = [(u, r) for u, r in results if r["over"]]
under_units = [(u, r) for u, r in results if not r["over"] and r["records"] > 20]
random.seed(1)
sample = random.sample(over_units, min(SAMPLE, len(over_units))) + random.sample(under_units, min(SAMPLE // 3, len(under_units)))
sim = []
for unit, row in sample:
    ordered = [pool[i] for i in unit["sources"]]
    bnd, _ = boundaries(ordered)
    cuts = sorted(b for b, k in bnd.items() if k in ("natural", "turn", "run_result"))
    edges = [0] + cuts + [len(ordered)]
    segs = [ordered[a:b] for a, b in zip(edges, edges[1:])]
    finest = []
    for s in segs:
        _, _, _, d, t = assemble([r.source_id for r in s], unit["id"] + "_f")
        finest.append({"records": len(s), "raw": sum(len(r.content) for r in s), "payload": len(dumps(t))})
    pieces, cur = [], []
    for s in segs:
        trial = cur + s
        if cur and len(dumps(assemble([r.source_id for r in trial], unit["id"] + "_g")[4])) > TARGET:
            pieces.append(cur); cur = list(s)
        else:
            cur = trial
    if cur: pieces.append(cur)
    pp = [len(dumps(assemble([r.source_id for r in p], unit["id"] + "_g")[4])) for p in pieces]
    kd = {}
    for (_, _, c, d, t) in [assemble([r.source_id for r in pieces[0]], unit["id"] + "_k")]:
        kd = keysizes(d)
    sim.append({"orig_records": row["records"], "orig_payload": row["payload"], "over": row["over"],
                "finest_pieces": len(segs), "finest_payload_max": max(x["payload"] for x in finest),
                "finest_payload_min": min(x["payload"] for x in finest),
                "greedy_pieces": len(pieces), "greedy_payloads": pp, "greedy_first_keys": kd})
    print("sim", len(sim), "/", len(sample), round(time.time() - t0, 1), flush=True)
json.dump({"target": TARGET, "sim": sim}, open(HERE / "sim.json", "w"))
print("DONE", round(time.time() - t0, 1))
