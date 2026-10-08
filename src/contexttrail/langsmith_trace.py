"""Explicit, host-side LangSmith tracing for ContextTrail runs and model calls.

Traces attach to the code that actually runs: the LangGraph nodes, the harness steps
inside them and each model call. Without `--langsmith-content` every payload passes
`metadata_only` before it leaves the process, so the same trace tree carries counts,
statuses and IDs but no project text.
"""
from __future__ import annotations

import os
import re
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

from .i18n import tr
from .util import FlowError, input_tokens

# Values that may leave the process in a metadata-only trace. Anything else is dropped:
# free text (titles, summaries, quotes, prompts, errors from model output) never passes.
_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_:.-]{0,64}$")
_SAFE_ID = re.compile(r"^(?:[a-z]{2,8}_[0-9a-f]{8,64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_SAFE_ENUM = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/|-]{0,80}$")
_DIGEST = re.compile(r"^[0-9a-f]{16,64}$")
ENUM_KEYS = {"mode", "stage", "status", "kind", "relation", "basis", "provider", "disposition", "operation",
             "origin", "target_kind", "candidate_kind", "routing_role", "validation", "analysis_status",
             "analysis_mode", "actor", "signal", "run_type", "reasoning_effort", "triggered", "routing_reasons",
             "validation_repair_reasons", "review_trigger", "langsmith_trace", "error_type"}
# Fields the model fills freely even though they look like codes: only known values pass,
# so a name written as an actor never reaches the trace.
FIXED_VALUES = {"actor": {"user", "assistant", "tool", "system", "subagent", "git"},
                "signal": {"existing_claim_or_status_update", "existing_relation_invalidation",
                           "unlinked_observed_outcome", "unlinked_revision"}}
NAME_KEYS = {"runner", "model", "requested_model", "actual_model", "runner_version", "adapter_version",
             "extract_model", "integrate_model", "escalation_model", "routing_version"}
# Aggregated per list of records so a trace shows e.g. how many candidates of each status.
_COUNT_FIELDS = ("kind", "status", "relation", "basis", "actor", "stage", "provider", "disposition",
                 "operation", "origin", "signal")


def _safe_string(key: str, value: str) -> str | None:
    if key in FIXED_VALUES:
        return value if value in FIXED_VALUES[key] else None
    if key in ENUM_KEYS and _SAFE_ENUM.match(value):
        return value
    if key in NAME_KEYS and _SAFE_NAME.match(value):
        return value
    if (key == "id" or key.endswith(("_id", "_ids"))) and _SAFE_ID.match(value):
        return value
    if key.endswith(("_digest", "_hash")) and _DIGEST.match(value):
        return value
    return None


def metadata_only(value: Any, key: str = "") -> Any:
    """Keep numbers, flags, code-defined enums and IDs; summarize lists; drop text."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_string(key, value)
    if isinstance(value, dict):
        # A map keyed by record/evidence IDs is reported by size, not entry by entry.
        if len(value) > 12 and sum(bool(_SAFE_ID.match(str(k))) for k in value) > len(value) / 2:
            return {"count": len(value)}
        kept = {}
        for child_key, child in value.items():
            if not isinstance(child_key, str) or not _SAFE_KEY.match(child_key):
                continue
            cleaned = metadata_only(child, child_key)
            if cleaned is not None and cleaned != {} and cleaned != []:
                kept[child_key] = cleaned
        return kept
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if all(isinstance(item, str) for item in items):
            safe = [_safe_string(key, item) for item in items]
            if items and all(safe) and len(items) <= 20:
                return safe
            return {"count": len(items)}
        summary: dict[str, Any] = {"count": len(items)}
        for field in _COUNT_FIELDS:
            counts = Counter(item[field] for item in items if isinstance(item, dict)
                             and isinstance(item.get(field), str) and _safe_string(field, item[field]))
            if counts:
                summary["by_" + field] = dict(counts)
        return summary
    return None


def scrub_error(error: Any) -> str | None:
    """The exception line of a traceback without quoted values or model-chosen IDs."""
    if not error:
        return None
    lines = [line for line in str(error).strip().splitlines() if line.strip()]
    text = lines[-1] if lines else ""
    text = re.sub(r"'[^']*'|\"[^\"]*\"|“[^”]*”|‘[^’]*’", "…", text)
    text = re.sub(r"tmp:[^\s,;)]+", "tmp:…", text)
    return text[:300]


def _metadata_only_kwargs(kwargs: dict) -> dict:
    cleaned = dict(kwargs)
    for field in ("inputs", "outputs"):
        if cleaned.get(field) is not None:
            cleaned[field] = metadata_only(cleaned[field])
    if "error" in cleaned:
        cleaned["error"] = scrub_error(cleaned["error"])
    if cleaned.get("extra") is not None:
        extra = cleaned["extra"] if isinstance(cleaned["extra"], dict) else {}
        cleaned["extra"] = {"metadata": _metadata_fields(extra.get("metadata") or {})}
    for field in ("events", "serialized", "attachments"):
        if field in cleaned:
            cleaned[field] = None
    if cleaned.get("tags") is not None:
        cleaned["tags"] = [tag for tag in cleaned["tags"] if isinstance(tag, str) and _SAFE_NAME.match(tag)]
    return cleaned


def _metadata_fields(metadata: dict) -> dict:
    kept = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not _SAFE_KEY.match(key):
            continue
        if value is None or isinstance(value, (bool, int, float)):
            kept[key] = value
        elif key.startswith(("langgraph_", "ls_", "contexttrail_")) or key in ENUM_KEYS | NAME_KEYS:
            if isinstance(value, str) and _SAFE_NAME.match(value):
                kept[key] = value
            elif isinstance(value, (list, tuple)) and all(isinstance(v, str) and _SAFE_NAME.match(v) for v in value):
                kept[key] = list(value)
        elif isinstance(value, dict):
            nested = metadata_only(value, key)
            if nested:
                kept[key] = nested
    return kept


def metadata_only_client_class() -> type:
    """A LangSmith client whose every create/update passes `metadata_only` first."""
    from langsmith import Client

    class MetadataOnlyClient(Client):
        def create_run(self, name, inputs, run_type, **kwargs):  # noqa: ANN001 - SDK signature
            kwargs = _metadata_only_kwargs({"inputs": inputs, **kwargs})
            return super().create_run(name, kwargs.pop("inputs") or {}, run_type, **kwargs)

        def update_run(self, run_id, **kwargs):  # noqa: ANN001 - SDK signature
            return super().update_run(run_id, **_metadata_only_kwargs(kwargs))

    return MetadataOnlyClient


class LangSmithTracer:
    def __init__(self, project: str | None = None, *, include_content: bool = False,
                 client_factory: Callable[..., Any] | None = None,
                 graph_client_factory: Callable[..., Any] | None = None):
        key = os.environ.get("LANGSMITH_API_KEY")
        if not key:
            raise FlowError(tr("LangSmith 추적에는 LANGSMITH_API_KEY가 필요합니다. 키를 명령 인자로 전달하지 마세요.",
                               "LangSmith tracing requires LANGSMITH_API_KEY. Do not pass the key as a command argument."))
        endpoint = os.environ.get("LANGSMITH_ENDPOINT")
        if endpoint:
            parsed = urlparse(endpoint)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise FlowError(tr("LANGSMITH_ENDPOINT는 인증정보가 없는 HTTPS 주소여야 합니다.",
                                   "LANGSMITH_ENDPOINT must be an HTTPS address without credentials."))
        if client_factory is None:
            try:
                from langsmith import Client
            except ImportError as exc:
                raise FlowError(tr("LangSmith SDK가 없습니다. python -m pip install -e '.[langsmith]'를 실행하세요.",
                                   "The LangSmith SDK is missing. Run python -m pip install -e '.[langsmith]'.")) from exc
            client_factory = Client
        self._key = key
        self._endpoint = endpoint
        self._client_factory = client_factory
        self._graph_client_factory = graph_client_factory
        self.project = project or os.environ.get("LANGSMITH_PROJECT") or "ContextTrail"
        self.include_content = include_content

    def graph_client(self) -> Any:
        """Client for the run's node and step spans; metadata-only unless content was chosen."""
        if self._graph_client_factory is not None:
            factory = self._graph_client_factory
        elif self.include_content:
            from langsmith import Client as factory
        else:
            factory = metadata_only_client_class()
        return factory(api_key=self._key, api_url=self._endpoint, omit_traced_runtime_info=True,
                       timeout_ms=(2000, 5000))

    def record(self, *, call_id: str, run_id: str, unit_id: str, stage: str, status: str,
               task: dict, schema: dict, output: dict | None, metadata: dict, details: dict,
               started_at: datetime, finished_at: datetime) -> None:
        usage = details.get("usage") if isinstance(details.get("usage"), dict) else {}
        read, output_tokens = input_tokens(usage), usage.get("output_tokens")
        known_tokens = read is not None and isinstance(output_tokens, int) and not isinstance(output_tokens, bool)
        usage_metadata = ({"input_tokens": read, "output_tokens": output_tokens,
                           "total_tokens": read + output_tokens} if known_tokens else None)
        inputs = ({"task": task, "schema": schema} if self.include_content else
                  {"input_digest": metadata.get("input_digest"), "input_chars": metadata.get("input_chars"),
                   "request_summary": metadata_only(task.get("data") or {})})
        outputs = ({"response": output} if self.include_content and output is not None else
                   {"output_digest": details.get("output_digest"), "output_chars": details.get("output_chars"),
                    "response_summary": metadata_only(output) if output is not None else None})
        if usage_metadata:
            outputs["usage_metadata"] = usage_metadata
        trace_meta = {"contexttrail_run_id": run_id, "contexttrail_unit_id": unit_id,
                      "stage": stage, "status": status, "requested_model": metadata.get("requested_model"),
                      "reasoning_effort": metadata.get("reasoning_effort"),
                      "actual_model": details.get("actual_model"), "read_round": metadata.get("read_round", 0),
                      "repair_round": metadata.get("repair_round", 0),
                      "content_included": self.include_content}
        if usage_metadata:
            trace_meta["usage_metadata"] = usage_metadata
        run_uuid = uuid.UUID(hex=call_id.removeprefix("llm_"))
        # Inside a traced run the call appears under the node that made it.
        placement = {}
        try:
            from langsmith.run_helpers import get_current_run_tree
            parent = get_current_run_tree()
        except Exception:
            parent = None
        if parent is not None and getattr(parent, "trace_id", None) and getattr(parent, "dotted_order", None):
            placement = {"parent_run_id": parent.id, "trace_id": parent.trace_id,
                         "dotted_order": f"{parent.dotted_order}.{started_at:%Y%m%dT%H%M%S%fZ}{run_uuid}"}
        client = self._client_factory(api_key=self._key, api_url=self._endpoint,
                                      auto_batch_tracing=False, timeout_ms=(2000, 5000),
                                      omit_traced_runtime_info=True)
        try:
            client.create_run(name=f"ContextTrail {stage}", inputs=inputs, outputs=outputs,
                              run_type="llm", project_name=self.project,
                              id=run_uuid, **placement,
                              start_time=started_at, end_time=finished_at,
                              tags=["contexttrail", stage, status], extra={"metadata": trace_meta},
                              error=status if status in {"failed", "validation_error"} else None)
            client.flush(timeout=5)
        finally:
            try:
                client.close()
            except Exception:
                pass
