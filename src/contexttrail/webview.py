from __future__ import annotations

import hmac
import json
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, quote, urlsplit

from .agent_view import reference
from .i18n import language, tr
from .render import (ACTOR, BASIS, LINK_BASIS, PROVIDER, RELATION, ROLE, RUN_STATE, agents_label, analysis_label,
                     evidence_excerpt, kind_label, mermaid, status_labels, status_tones)
from .diagram import flow_svg, request_turns, story_svg
from .schema import folded_view, story_view
from .freshness import check_from_store, scanned_at
from .freshness import summary as freshness_summary
from .store import Store
from .util import FlowError, dumps


# The page ships in Korean; the English screen gets the same markup with these texts swapped.
# Keys are the exact Korean texts in `assets/index.html`, so a stale entry fails `page_html`.
PAGE_TEXT_EN = {
    "ContextTrail · 근거 보기": "ContextTrail · evidence view",
    "프로젝트의 흐름과 근거": "The project's flow and evidence",
    "변경분 분석": "Analyze changes",
    "터미널에서 안내한 접근 주소로 열어 주세요.": "Open this page with the access link shown in the terminal.",
    "진행 흐름": "Flow",
    "노드를 선택하면 오른쪽에 원문이 표시됩니다.": "Select a node to see its source text on the right.",
    "사건과 근거": "Event and evidence",
    "사건을 선택하세요.": "Select an event.",
    "분석 범위와 한계": "Scope and limitations",
    "저장된 그래프를 보고 있습니다. 페이지 새로고침은 AI 분석을 실행하지 않습니다. 핵심 보기의 점선 관계는 추정이고, 모든 단계의 점선(┊)은 시간 순서일 뿐 원인이 아닙니다.":
        "You are viewing the saved graph. Reloading the page runs no AI analysis. A dashed relation in the story view "
        "is inferred; a dotted line (┊) in every step is only the order in time, never a cause.",
}


def page_html(text: str, lang: str | None = None) -> str:
    """`index.html` in the screen language: the `lang` attribute and, for English, the static texts."""
    lang = lang or language()
    if lang == "ko":
        return text
    for korean, english in PAGE_TEXT_EN.items():
        if korean not in text:
            raise FlowError(f"page text not found: {korean}")
        text = text.replace(korean, english)
    return text.replace('<html lang="ko">', f'<html lang="{lang}">', 1)


def page_requests(graph: dict) -> tuple[list[dict], dict[str, int]]:
    """The person's requests in the order the views number them (`diagram.request_turns` of the drawn graph): each
    with its number in the whole list, title and time (ISO; the page shows it in local time), and the request each
    event's turn belongs to (an index into that list; events in no request's turn are left out)."""
    number = {event["id"]: n for n, event in enumerate(graph["events"], 1)}
    by_id = {event["id"]: event for event in graph["events"]}
    requests: list[dict] = []
    turn_of: dict[str, int] = {}
    for key, keys in request_turns(graph):
        if key is None:
            continue
        event = by_id[key]
        for member in keys:
            turn_of[member] = len(requests)
        requests.append({"id": key, "number": number[key], "title": event["title"],
                         "at": event.get("occurred_at") or event.get("recorded_at")})
    return requests, turn_of


# A limitation's head is the text before its first ": " when that is this short (read as `_limitation_key`); longer
# text before a colon is a sentence, not a head.
LIMITATION_HEAD_CHARS = 80


def _limitation_key(head: str) -> str:
    """A head with what changes from file to file read as one: a file in parentheses dropped, a file name as `…`,
    every number as N."""
    head = re.sub(r"\s*\([^()]*[/#][^()]*\)", "", head)
    head = re.sub(r"\S+\.(?:jsonl|zst|db|json)\b", "…", head)
    return re.sub(r"\d+", "N", head)


def limitation_groups(messages: list[str]) -> list[dict]:
    """Limitations grouped by their head (`_limitation_key`: `Codex unparsed record type world_state, N records not
    sent to analysis`), the largest group first, then the messages with no head: [{"head": text or None, "count",
    "items"}]. A repeated message is listed once."""
    groups: dict[str | None, list[str]] = {}
    heads: dict[str | None, set[str]] = {}
    for message in dict.fromkeys(message for message in messages if isinstance(message, str) and message.strip()):
        head, colon, _ = message.partition(": ")
        key = _limitation_key(head) if colon else None
        key = key if key and len(key) <= LIMITATION_HEAD_CHARS else None
        groups.setdefault(key, []).append(message)
        heads.setdefault(key, set()).add(head)
    order = sorted((key for key in groups if key is not None), key=lambda key: -len(groups[key]))

    def named(key: str) -> str:
        # The head as written when every message shares it and it is short; else the key.
        written = heads[key]
        return next(iter(written)) if len(written) == 1 and len(next(iter(written))) <= LIMITATION_HEAD_CHARS else key

    shown = [{"head": named(key), "count": len(groups[key]), "items": groups[key]} for key in order]
    if None in groups:
        shown.append({"head": None, "count": len(groups[None]), "items": groups[None]})
    return shown


def event_page(graph: dict, event_id: str, store: Store) -> dict:
    """What the page's detail shows for one event, in the screen language: the event with its labels (kind, actor,
    basis, status and its tone, who did the work, the analysis model), its own evidence first and then that of its
    relations (`own`), each with its role and tool named, and its relations grouped as the terminal's detail lists
    them: the checks (`checks`), the other links (`links`) and the dialog-order and delegation links (`structure`),
    each naming the event at its other end with its number, title and status."""
    numbers = {event["id"]: n for n, event in enumerate(graph["events"], 1)}
    by_id = {event["id"]: event for event in graph["events"]}
    event = by_id.get(event_id)
    if not event:
        raise FlowError(tr("사건 없음", "no such event"))
    labels, tones = status_labels(graph), status_tones(graph)
    relations = []
    for edge in graph["edges"]:
        if not edge["active"] or event_id not in (edge["from_event_id"], edge["to_event_id"]):
            continue
        outgoing = edge["from_event_id"] == event_id
        other = by_id.get(edge["to_event_id"] if outgoing else edge["from_event_id"])
        if other is None:
            continue
        origin = edge.get("origin")
        relations.append({**edge, "relation_label": RELATION.get(edge["relation"], edge["relation"]),
                          "origin_label": (tr("대화 순서", "dialog order") if origin == "dialog_turn" else
                                           tr("위임", "delegation") if origin == "delegation" else
                                           tr("갈라진 세션", "forked session") if origin == "fork" else ""),
                          "basis_label": LINK_BASIS.get(edge.get("basis"), edge.get("basis") or ""),
                          "group": ("checks" if edge["relation"] == "verifies" else
                                    "structure" if origin in ("dialog_turn", "delegation") else "links"),
                          "direction": "out" if outgoing else "in", "other_id": other["id"],
                          "other_number": numbers[other["id"]], "other_title": other["title"],
                          "other_status": labels[other["id"]], "other_tone": tones[other["id"]]})
    rank = {"checks": 0, "links": 1, "structure": 2}
    relations.sort(key=lambda item: (rank[item["group"]], item["other_number"]))
    own = list(dict.fromkeys(event["evidence_ids"]))
    evidence = store.evidence_many(own + [i for edge in relations for i in edge["evidence_ids"] if i not in own])
    # Only the cited records (the whole table took 0.3-0.9 s a click, growing with history).
    sources = store.sources_by_id({item["source_id"] for item in evidence.values()})
    for item in evidence.values():
        source = sources.get(item["source_id"])
        item["original_state"] = ("missing" if not source or not source["available"] else
                                  "changed" if source["content_hash"] != item["content_hash"] else "available_at_last_scan")
        item["excerpt"] = evidence_excerpt(item)
        item["own"] = item["id"] in own
        cited = item.get("source") or {}
        item["role_label"] = ROLE.get(cited.get("role"), cited.get("role") or "")
        item["provider_label"] = PROVIDER.get(cited.get("provider"), cited.get("provider") or "")
    ordered = sorted(evidence.values(), key=lambda item: (not item["own"], own.index(item["id"]) if item["own"] else 0))
    open_items = [item for item in graph.get("open_items", []) if isinstance(item, dict)
                  and event_id in item.get("related_event_ids", [])]
    shown = {**event, "number": numbers[event_id], "status_label": labels[event_id], "status_tone": tones[event_id],
             "kind_label": kind_label(event), "actor_label": ACTOR.get(event.get("actor"), event.get("actor") or ""),
             "basis_label": BASIS.get(event.get("basis"), event.get("basis") or ""), "agents": agents_label(event),
             "analysis_label": analysis_label(event), "reference": reference(graph, event_id)}
    return {"version": graph["version"], "event": shown, "evidence": ordered, "relations": relations,
            "open_items": open_items}


class LocalViewer:
    def __init__(self, store: Store, refresh=None, *, port: int = 8765, scope=None, refresh_ready=None):
        self.store, self.refresh_callback, self.scope = store, refresh, scope
        # Whether the page may start an analysis now, asked again at every request: {"ok", "runner", "model",
        # "reason"}. The page asks no plan (docs/DECISIONS.md), so it starts one only with a runner chosen and
        # the person's consent for it already given in a terminal.
        self.refresh_ready = refresh_ready or (lambda: {"ok": True})
        self._fresh: tuple[tuple, str] | None = None
        # The last drawings, by graph version, view and screen language: every step takes about 1.5 s to lay out
        # on a 1,200-event graph, and the page asks for it again on each view switch and reload.
        self._drawings: dict[tuple, str] = {}
        self.token = secrets.token_urlsafe(32)
        self.refresh_thread = None
        self.refresh_lock = threading.Lock()
        self.refresh_result: dict | None = None
        self.server = None
        self.thread = None
        self.requested_port = port

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def freshness(self, version: int) -> str:
        """What the graph does not hold yet, counted once per graph version; empty without a scope."""
        if self.scope is None:
            return ""
        key = (version, scanned_at(self.store))
        if self._fresh is None or self._fresh[0] != key:
            self._fresh = (key, freshness_summary(check_from_store(self.scope, self.store)))
        return self._fresh[1]

    def drawing(self, graph: dict, story: bool) -> str:
        key = (graph["version"], story, language())
        if key not in self._drawings:
            text = story_svg(story_view(graph)) if story else flow_svg(graph)
            self._drawings = {**dict(list(self._drawings.items())[-3:]), key: text}
        return self._drawings[key]

    def url(self, version: int | None = None, event_id: str | None = None) -> str:
        fragment = "token=" + quote(self.token)
        if version is not None and version > 0:
            fragment += f"&v={version}"
        if event_id:
            fragment += "&event=" + quote(event_id)
        return f"http://127.0.0.1:{self.port}/#{fragment}"

    def start(self) -> "LocalViewer":
        viewer = self
        class Handler(BaseHTTPRequestHandler):
            server_version, sys_version = "ContextTrail", ""
            def log_message(self, *args):
                pass  # Never put tokens or source data in HTTP access logs.

            def send_error(self, code, message=None, explain=None):
                # The stock error page (unsupported methods, bad requests) carries none of the page's headers.
                self.close_connection = True
                self.send_data(code, {"error": str(message or code)})

            def do_HEAD(self):
                self.send_error(405, "method not allowed")

            def send_data(self, code: int, data, content_type: str = "application/json; charset=utf-8"):
                raw = data.encode("utf-8") if isinstance(data, str) else dumps(data).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(raw)

            def host_ok(self) -> bool:
                allowed = {f"127.0.0.1:{viewer.port}", f"localhost:{viewer.port}"}
                origin = self.headers.get("Origin")
                return self.headers.get("Host") in allowed and (not origin or origin in {"http://" + h for h in allowed})

            def authorized(self) -> bool:
                return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + viewer.token)

            def graph(self) -> dict:
                values = parse_qs(urlsplit(self.path).query).get("version", [])
                if values:
                    try:
                        return viewer.store.graph(int(values[0]))
                    except ValueError as exc:
                        raise FlowError(tr("잘못된 버전", "invalid version")) from exc
                return viewer.store.graph()

            def do_GET(self):
                if not self.host_ok():
                    return self.send_data(403, {"error": "host/origin denied"})
                path = urlsplit(self.path).path
                assets = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                          "/style.css": ("style.css", "text/css; charset=utf-8")}
                if path in assets:
                    name, content_type = assets[path]
                    text = files("contexttrail").joinpath("assets", name).read_text(encoding="utf-8")
                    return self.send_data(200, page_html(text) if name == "index.html" else text, content_type)
                if not self.authorized():
                    return self.send_data(401, {"error": "access token required"})
                try:
                    graph = folded_view(self.graph())
                    if path == "/graph":
                        refreshing = bool(viewer.refresh_thread and viewer.refresh_thread.is_alive())
                        ready = viewer.refresh_ready() if viewer.refresh_callback is not None else {"ok": False}
                        last_check = viewer.store.get_meta("last_check", {})
                        requests, turn_of = page_requests(graph)
                        limits = limitation_groups([*(graph.get("limitations") or []), *(graph.get("input_limitations") or []),
                                                    *(last_check.get("limitations") or [])])
                        return self.send_data(200, {"graph": graph, "last_check": last_check,
                            "freshness": "" if refreshing else viewer.freshness(graph["version"]), "refreshing": refreshing,
                            "refresh_result": viewer.refresh_result, "can_refresh": bool(ready.get("ok")),
                            "refresh_with": {key: ready.get(key) for key in ("runner", "model") if ready.get(key)},
                            "refresh_blocked": ready.get("reason"), "latest_version": viewer.store.latest_version(),
                            "requests": requests, "turn_of": turn_of, "limitation_groups": limits,
                            "run_state": RUN_STATE.get(last_check.get("status") or graph.get("analysis_status") or "no_data",
                                                       last_check.get("status") or graph.get("analysis_status") or "")})
                    if path == "/graph.svg":
                        # The story view (the page's default) or every step; any other value is every step.
                        story = parse_qs(urlsplit(self.path).query).get("view", [""])[0] == "story"
                        return self.send_data(200, viewer.drawing(graph, story), "image/svg+xml; charset=utf-8")
                    if path == "/graph.mmd":
                        return self.send_data(200, mermaid(graph), "text/plain; charset=utf-8")
                    if path.startswith("/events/"):
                        return self.send_data(200, event_page(graph, path[len("/events/"):], viewer.store))
                    if path.startswith("/evidence/"):
                        evidence_id = path[len("/evidence/"):]
                        ids = {i for key in ("events", "edges", "open_items") for item in graph[key]
                               for i in item.get("evidence_ids", [])}
                        evidence = viewer.store.evidence(evidence_id) if evidence_id in ids else None
                        if not evidence:
                            raise FlowError(tr("근거 없음", "no such evidence"))
                        return self.send_data(200, evidence)
                    return self.send_data(404, {"error": "not found"})
                except FlowError as exc:
                    return self.send_data(404, {"error": str(exc)})

            def do_POST(self):
                origin = self.headers.get("Origin")
                if not self.host_ok() or not origin or not self.authorized() or self.headers.get("X-Projectflow-Action") != "refresh":
                    return self.send_data(403, {"error": "refresh authorization denied"})
                if urlsplit(self.path).path != "/refresh" or not viewer.refresh_callback:
                    return self.send_data(404, {"error": "refresh unavailable"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 128 or self.headers.get("Content-Type") != "application/json":
                        raise ValueError()
                    payload = json.loads(self.rfile.read(length))
                    if payload != {"confirm": True}:
                        raise ValueError()
                except (ValueError, TypeError):
                    return self.send_data(400, {"error": "explicit confirmation required"})
                ready = viewer.refresh_ready()
                if not ready.get("ok"):
                    return self.send_data(409, {"error": ready.get("reason") or "analysis is not available here"})
                with viewer.refresh_lock:
                    if viewer.refresh_thread and viewer.refresh_thread.is_alive():
                        return self.send_data(409, {"error": "already refreshing"})
                    def run():
                        try:
                            result = viewer.refresh_callback()
                            viewer.refresh_result = {k: v for k, v in result.items() if k != "graph"}
                        except FlowError as exc:
                            viewer.refresh_result = {"status": "failed", "error": str(exc)}
                        except Exception as exc:  # what the terminal is told to look at; never the access token
                            print(f"contexttrail: browser analysis failed: {type(exc).__name__}", file=sys.stderr, flush=True)
                            viewer.refresh_result = {"status": "failed", "error": tr("갱신 실패; 터미널에서 상태를 확인하세요.",
                                                                                  "Update failed; check the terminal for its state.")}
                    viewer.refresh_result = None
                    viewer.refresh_thread = threading.Thread(target=run, name="contexttrail-refresh", daemon=True)
                    viewer.refresh_thread.start()
                return self.send_data(202, {"status": "started"})
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", self.requested_port), Handler)
        except OSError:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="contexttrail-viewer", daemon=True)
        self.thread.start()
        return self

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
