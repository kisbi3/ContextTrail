from __future__ import annotations

import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, quote, urlsplit

from .agent_view import reference
from .i18n import language, tr
from .render import RELATION, evidence_excerpt, mermaid, status_labels, svg
from .freshness import check_from_store
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
    "저장된 그래프를 보고 있습니다. 페이지 새로고침은 AI 분석을 실행하지 않습니다. 점선 관계는 추정입니다.":
        "You are viewing the saved graph. Reloading the page runs no AI analysis. Dashed relations are inferred.",
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


class LocalViewer:
    def __init__(self, store: Store, refresh=None, *, port: int = 8765, scope=None):
        self.store, self.refresh_callback, self.scope = store, refresh, scope
        self._fresh: tuple[int, str] | None = None
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
        if self._fresh is None or self._fresh[0] != version:
            self._fresh = (version, freshness_summary(check_from_store(self.scope, self.store)))
        return self._fresh[1]

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
            server_version = "ProjectFlow"
            def log_message(self, *args):
                pass  # Never put tokens or source data in HTTP access logs.

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
                    graph = self.graph()
                    if path == "/graph":
                        refreshing = bool(viewer.refresh_thread and viewer.refresh_thread.is_alive())
                        return self.send_data(200, {"graph": graph, "last_check": viewer.store.get_meta("last_check", {}),
                            "freshness": "" if refreshing else viewer.freshness(graph["version"]), "refreshing": refreshing,
                            "refresh_result": viewer.refresh_result, "can_refresh": viewer.refresh_callback is not None})
                    if path == "/graph.svg":
                        return self.send_data(200, svg(graph), "image/svg+xml; charset=utf-8")
                    if path == "/graph.mmd":
                        return self.send_data(200, mermaid(graph), "text/plain; charset=utf-8")
                    if path.startswith("/events/"):
                        event = next((e for e in graph["events"] if e["id"] == path[len("/events/"):]), None)
                        if not event:
                            raise FlowError(tr("사건 없음", "no such event"))
                        relations = [e for e in graph["edges"] if e["active"] and
                                     event["id"] in {e["from_event_id"], e["to_event_id"]}]
                        titles = {e["id"]: e["title"] for e in graph["events"]}
                        relations = [{**e, "from_title": titles[e["from_event_id"]],
                                      "to_title": titles[e["to_event_id"]],
                                      "relation_label": RELATION.get(e["relation"], e["relation"])} for e in relations]
                        evidence_ids = event["evidence_ids"] + [i for edge in relations for i in edge["evidence_ids"]]
                        evidence = viewer.store.evidence_many(evidence_ids)
                        sources = viewer.store.sources()
                        for item in evidence.values():
                            source = sources.get(item["source_id"])
                            item["original_state"] = ("missing" if not source or not source["available"] else
                                "changed" if source["content_hash"] != item["content_hash"] else "available_at_last_scan")
                            item["excerpt"] = evidence_excerpt(item)
                        event = {**event, "status_label": status_labels(graph)[event["id"]],
                                 "reference": reference(graph, event["id"])}
                        return self.send_data(200, {"version": graph["version"], "event": event,
                                                   "evidence": list(evidence.values()), "relations": relations})
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
                with viewer.refresh_lock:
                    if viewer.refresh_thread and viewer.refresh_thread.is_alive():
                        return self.send_data(409, {"error": "already refreshing"})
                    def run():
                        try:
                            result = viewer.refresh_callback()
                            viewer.refresh_result = {k: v for k, v in result.items() if k != "graph"}
                        except Exception:
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
