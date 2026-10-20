#!/usr/bin/env python3
"""Developer tool: analyse the same units of a real project with several models, each on its own copy of the state.

A project analyses each unit once, so two models can only be compared on the same units from separate copies of
its state. This copies the project's state once (read-only, SQLite backup), gives every model its own copy, runs
the ordinary analysis (`Engine.analyze`) on each, and writes a comparison: how each unit was published (first
answer, after the repair, salvaged, skipped), calls, time, tokens, events, verified changes, titles outside the
output language, and every unit's event titles side by side.

The project's own state is never opened for writing. It sends the project's records to each model's service,
so it runs only with --yes.

    python scripts/compare_models.py ~/priceimpact --models claude:haiku claude:sonnet codex:gpt-luna codex:gpt-sol \
        --units 5 --output /tmp/ct-compare --yes [--jobs 2] [--usage]
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

PATHS = ("first", "repair", "salvage", "skipped")
SCRIPTS = {"Korean": re.compile("[가-힣]"), "Japanese": re.compile("[ぁ-んァ-ン一-龯]"), "Chinese": re.compile("[一-龯]")}


def slug(spec: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", spec)


def parse_spec(spec: str) -> tuple[str, str]:
    runner, _, model = spec.partition(":")
    if runner not in {"codex", "claude"} or not model:
        raise SystemExit(f"model must be codex:<model> or claude:<model>, got {spec!r}")
    return runner, model


def copy_state(source: Path, target: Path) -> None:
    """SQLite backup of `source` (opened read-only) into `target`; the project's own file is never written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    os.chmod(target, 0o600)


def graph_of(blob) -> dict:
    if isinstance(blob, bytes) and blob[:6] == b"zlib1:":
        blob = zlib.decompress(blob[6:]).decode()
    return json.loads(blob) if blob else {"events": [], "edges": []}


def tokens_of(usage: dict) -> dict:
    """One shape for both CLIs: Claude's input leaves the cache out, Codex's includes the cached part."""
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        fresh, read, write = (usage.get(k, 0) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        reasoning = (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0
        return {"input": fresh + read + write, "cached": read, "cache_write": write,
                "output": usage.get("output_tokens", 0) or 0, "reasoning": reasoning}
    total, cached = usage.get("input_tokens", 0) or 0, usage.get("cached_input_tokens", 0) or 0
    return {"input": total, "cached": cached, "cache_write": 0, "output": usage.get("output_tokens", 0) or 0,
            "reasoning": usage.get("reasoning_output_tokens", 0) or 0}


def unit_path(calls: list[dict], status: str | None) -> str:
    if status != "integrated":
        return "skipped"
    # The unit's story call (`extract` in states analysed before the story call was the only one).
    extract = [c["status"] for c in calls if c["stage"] in ("extract", "story") and c["status"] not in ("cancelled", "running")]
    if extract and extract[0] == "complete":
        return "first"
    return "repair" if "complete" in extract else "salvage"


def summarize(db_path: Path, since: str) -> dict:
    """What the runs started at or after `since` did, read from one state copy."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    runs = [dict(zip(("id", "started", "finished", "status", "details"), row)) for row in db.execute(
        "SELECT id, started_at, finished_at, status, details FROM analysis_runs WHERE started_at >= ? ORDER BY started_at", (since,))]
    run_ids = {r["id"] for r in runs}
    meta = {k: v for k, v in db.execute("SELECT key, value FROM project_meta")}
    language = json.loads(meta.get("output_language") or "null")
    script = SCRIPTS.get(language or "")
    calls = []
    for row in db.execute("SELECT run_id, unit_id, stage, attempt, status, metadata, details FROM llm_calls ORDER BY rowid"):
        if row[0] not in run_ids:
            continue
        metadata, details = json.loads(row[5] or "{}"), json.loads(row[6] or "{}")
        calls.append({"unit": row[1], "stage": row[2], "attempt": row[3], "status": row[4],
                      "model": details.get("actual_model") or metadata.get("model"),
                      "seconds": (details.get("duration_ms") or 0) / 1000, "tokens": tokens_of(details.get("usage") or {}),
                      "error": (details.get("error") or "")[:200]})
    statuses, unit_of = {}, {}
    for unit_id, status, unit_sources in db.execute("SELECT id, status, sources FROM work_units"):
        statuses[unit_id] = status
        for source in json.loads(unit_sources or "[]"):
            unit_of[source] = unit_id
    graph = graph_of((db.execute("SELECT graph FROM graph_versions ORDER BY version DESC LIMIT 1").fetchone() or [None])[0])
    evidence = {row[0]: json.loads(row[1]).get("source_id") for row in db.execute("SELECT id, data FROM evidence_items")}
    db.close()
    new = [e for e in graph["events"] if e.get("created_in_run") in run_ids and e.get("origin") != "note"]
    ids = {e["id"] for e in new}
    edges = [x for x in graph.get("edges", []) if x.get("active", True) and x["to_event_id"] in ids]
    by_id = {e["id"]: e for e in graph["events"]}
    verified = {x["from_event_id"] for x in edges if x["relation"] == "verifies" and by_id.get(x["to_event_id"], {}).get("kind") == "outcome"}
    changes = [e for e in new if e["kind"] in ("action", "revision")]
    units = {}
    for unit_id in dict.fromkeys(c["unit"] for c in calls):
        unit_calls = [c for c in calls if c["unit"] == unit_id]
        total = collections.Counter()
        for c in unit_calls:
            total.update(c["tokens"])
        units[unit_id] = {"path": unit_path(unit_calls, statuses.get(unit_id)), "calls": len(unit_calls),
                          "seconds": round(sum(c["seconds"] for c in unit_calls), 1), "tokens": dict(total),
                          "errors": [c["error"] for c in unit_calls if c["error"]]}
    total = collections.Counter()
    for c in calls:
        total.update(c["tokens"])
    wall = 0.0
    for r in runs:
        if r["finished"]:
            wall += (datetime.fromisoformat(r["finished"]) - datetime.fromisoformat(r["started"])).total_seconds()
    return {
        "runs": [{k: r[k] for k in ("id", "started", "finished", "status")} | {"error": json.loads(r["details"] or "{}").get("error")} for r in runs],
        "units": units, "paths": dict(collections.Counter(u["path"] for u in units.values())),
        "calls": len(calls), "call_status": dict(collections.Counter(c["status"] for c in calls)),
        "models": sorted({c["model"] for c in calls if c["model"]}),
        "wall_seconds": round(wall, 1), "model_seconds": round(sum(c["seconds"] for c in calls), 1), "tokens": dict(total),
        "events": len(new), "kinds": dict(collections.Counter(e["kind"] for e in new)),
        "changes": len(changes), "changes_verified": len(verified & {e["id"] for e in changes}),
        "observed": sum(e["status"].startswith("observed_") for e in new),
        "relations": dict(collections.Counter(x["relation"] + ("/dialog" if x.get("origin") == "dialog_turn" else "") for x in edges)),
        "output_language": language,
        "off_language_titles": [e["title"] for e in new if script and e.get("actor") != "user" and not script.search(e["title"])],
        # An event belongs to the analysed unit whose records it cites (the first such unit in run order).
        "unit_summaries": {unit: entry.get("text") for unit, entry in (graph.get("unit_summaries") or {}).items() if unit in units},
        "event_titles": [{"unit": next((unit_of[src] for src in cited if unit_of.get(src) in units), None),
                          "kind": e["kind"], "status": e["status"], "title": e["title"]}
                         for e in new for cited in [[evidence.get(i) for i in e.get("evidence_ids", []) if evidence.get(i)]]],
    }


def worker(args) -> int:
    """One model on one state copy (a separate process, so several can run side by side)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from contexttrail.analysis import AnalysisConfig, Engine
    from contexttrail.git_context import Scope
    from contexttrail.runners.cli_runner import CLIRunner
    from contexttrail.store import Store

    runner, model = parse_spec(args.worker)
    scope = Scope.resolve(Path(args.project))
    state = Path(args.state)
    if state.resolve() == Path(scope.state_dir).resolve():
        raise SystemExit("refusing to analyse into the project's own state")
    store = Store(state, scope.id)
    values = {"runner_name": runner, "base_model": model, "max_units": args.units, "recent": args.recent}
    if args.effort:
        values["extract_effort"] = args.effort
    engine = Engine(scope, store, AnalysisConfig(**values))
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    clock = time.monotonic()
    result = engine.analyze(lambda: CLIRunner(runner, model=model, timeout=args.timeout),
                            update=lambda line: print(line, file=sys.stderr, flush=True))
    summary = summarize(state / "state.sqlite", started)
    summary.update(spec=args.worker, status=result.get("status"), elapsed=round(time.monotonic() - clock, 1))
    Path(args.result).write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def usage_windows() -> dict:
    """The subscription windows both CLIs report, read with one tiny call each (outside any project)."""
    from tempfile import mkdtemp
    here = mkdtemp(prefix="ct-usage-")
    out = {}
    try:
        proc = subprocess.run(["claude", "-p", "Reply with OK.", "--model", "haiku", "--output-format", "stream-json",
                               "--verbose", "--no-session-persistence"], cwd=here, capture_output=True, text=True, timeout=120)
        for line in proc.stdout.splitlines():
            if '"rate_limit_event"' in line:
                windows = (json.loads(line).get("rate_limit_info") or {}).get("unifiedWindows") or {}
                out["claude"] = {k: (v or {}).get("utilization") for k, v in windows.items()}
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    try:
        sessions = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
        before = set(sessions.rglob("*.jsonl"))
        subprocess.run(["codex", "exec", "--skip-git-repo-check", "-m", "gpt-6-luna", "Reply with OK."],
                       cwd=here, capture_output=True, text=True, timeout=180)
        for path in sorted(set(sessions.rglob("*.jsonl")) - before, key=lambda p: p.stat().st_mtime)[-1:]:
            for line in path.read_text().splitlines():
                if '"rate_limits"' in line:
                    limits = json.loads(line)["payload"]["rate_limits"]
                    out["codex"] = {"five_hour": (limits.get("primary") or {}).get("used_percent"),
                                    "weekly": (limits.get("secondary") or {}).get("used_percent")}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        pass
    return out


def report(results: dict[str, dict], output: Path) -> str:
    specs = list(results)
    def avg(spec, value):
        n = max(1, sum(u["path"] != "skipped" for u in results[spec]["units"].values()))
        return value / n
    lines = ["# Model comparison", "", f"Written {datetime.now().isoformat(timespec='minutes')}. Same starting state for every model; "
             "each ran on its own copy.", "", "| | " + " | ".join(specs) + " |", "|---|" + "---|" * len(specs)]
    def row(label, fn):
        lines.append(f"| {label} | " + " | ".join(fn(s, results[s]) for s in specs) + " |")
    row("status", lambda s, r: f"{r['status']}")
    row("models answering", lambda s, r: ", ".join(r["models"]) or "-")
    row("units published", lambda s, r: f"{sum(u['path'] != 'skipped' for u in r['units'].values())}/{len(r['units'])}")
    for path in PATHS:
        row(f"· {path}", lambda s, r, p=path: str(r["paths"].get(p, 0)))
    row("calls (failed checks)", lambda s, r: f"{r['calls']} ({r['call_status'].get('validation_error', 0)})")
    row("wall s / unit", lambda s, r: f"{avg(s, r['wall_seconds']):.0f}")
    row("model s / unit", lambda s, r: f"{avg(s, r['model_seconds']):.0f}")
    row("input tokens / unit", lambda s, r: f"{avg(s, r['tokens'].get('input', 0)):,.0f} ({100 * r['tokens'].get('cached', 0) / max(1, r['tokens'].get('input', 0)):.0f}% cached)")
    row("output tokens / unit", lambda s, r: f"{avg(s, r['tokens'].get('output', 0)):,.0f} ({100 * r['tokens'].get('reasoning', 0) / max(1, r['tokens'].get('output', 0)):.0f}% reasoning)")
    row("events / unit", lambda s, r: f"{avg(s, r['events']):.1f}")
    row("changes verified", lambda s, r: f"{r['changes_verified']}/{r['changes']}")
    row("observed outcomes", lambda s, r: str(r["observed"]))
    row("causal relations", lambda s, r: str(sum(v for k, v in r["relations"].items() if k in ("motivates", "revises", "produces", "answers"))))
    row("off-language titles", lambda s, r: f"{len(r['off_language_titles'])}/{r['events']}")
    lines += ["", "## Units", "", "| unit | " + " | ".join(specs) + " |", "|---|" + "---|" * len(specs)]
    units = list(dict.fromkeys(u for s in specs for u in results[s]["units"]))
    for unit in units:
        cells = []
        for s in specs:
            u = results[s]["units"].get(unit)
            cells.append("–" if u is None else f"{u['path']} · {u['calls']} calls · {u['seconds']:.0f}s")
        lines.append(f"| `{unit[-8:]}` | " + " | ".join(cells) + " |")
    if any(r.get("unit_summaries") for r in results.values()):
        lines += ["", "## Unit summaries", "", "| unit | " + " | ".join(specs) + " |", "|---|" + "---|" * len(specs)]
        for unit in units:
            lines.append(f"| `{unit[-8:]}` | " + " | ".join((results[s].get("unit_summaries") or {}).get(unit, "–") for s in specs) + " |")
    lines += ["", "## Events of each unit", ""]
    for unit in units + [None]:
        block = []
        for s in specs:
            titles = [e for e in results[s]["event_titles"] if e["unit"] == unit]
            if titles:
                block += [f"**{s}**", ""] + [f"- {e['kind']}/{e['status']}: {e['title']}" for e in titles] + [""]
        if block:
            lines += [f"### `{unit[-8:]}`" if unit else "### not placed in a unit", ""] + block
    text = "\n".join(lines)
    (output / "report.md").write_text(text)
    (output / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    return text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("project", type=Path)
    parser.add_argument("--models", nargs="+", metavar="RUNNER:MODEL")
    parser.add_argument("--units", type=int, default=5)
    parser.add_argument("--base", type=Path, help="start every model from a copy of this state file instead of the project's own")
    parser.add_argument("--recent", type=int, help="take the N most recent sessions instead of the oldest units")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jobs", type=int, default=1, help="models analysed side by side (keep 1 with --usage)")
    parser.add_argument("--usage", action="store_true", help="read the subscription windows before and after each model")
    parser.add_argument("--effort", help="reasoning effort of the analysis call (default: the runner's)")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--yes", action="store_true", help="send the project's records to each model's service")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--state", help=argparse.SUPPRESS)
    parser.add_argument("--result", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args)
    if not args.models or not args.output:
        parser.error("--models and --output are required")
    if not args.yes:
        parser.error("this sends the project's records to each model's service; add --yes to run")
    specs = list(dict.fromkeys(args.models))
    for spec in specs:
        parse_spec(spec)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from contexttrail.git_context import Scope
    scope = Scope.resolve(args.project)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    own = args.base.expanduser().resolve() if args.base else Path(scope.state_dir) / "state.sqlite"
    if args.base and not own.is_file():
        parser.error(f"--base: no state file at {own}")
    base = output / "base" / "state.sqlite"
    if own.is_file():
        copy_state(own, base)
    else:
        base.parent.mkdir(parents=True, exist_ok=True)
    print(f"base state: {'copy of ' + str(own) if own.is_file() else 'empty (the project has none)'}", file=sys.stderr)

    def run(spec: str) -> tuple[str, dict]:
        folder = output / slug(spec)
        state = folder / "state"
        if base.is_file():
            copy_state(base, state / "state.sqlite")
        else:
            state.mkdir(parents=True, exist_ok=True)
        before = usage_windows() if args.usage else None
        command = [sys.executable, str(Path(__file__).resolve()), str(args.project), "--worker", spec, "--state", str(state),
                   "--result", str(folder / "result.json"), "--units", str(args.units), "--timeout", str(args.timeout)]
        if args.recent:
            command += ["--recent", str(args.recent)]
        if args.effort:
            command += ["--effort", args.effort]
        with open(folder / "run.log", "w") as log:
            code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
        result = json.loads((folder / "result.json").read_text()) if (folder / "result.json").is_file() else {
            "spec": spec, "status": f"worker exit {code}", "units": {}, "paths": {}, "calls": 0, "call_status": {}, "models": [],
            "wall_seconds": 0, "model_seconds": 0, "tokens": {}, "events": 0, "changes": 0, "changes_verified": 0,
            "observed": 0, "relations": {}, "off_language_titles": [], "event_titles": []}
        if args.usage:
            result["usage"] = {"before": before, "after": usage_windows()}
        print(f"{spec}: {result['status']} · {sum(u['path'] != 'skipped' for u in result['units'].values())} units", file=sys.stderr)
        return spec, result

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        results = dict(pool.map(run, specs))
    print(report({s: results[s] for s in specs}, output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
