"""Host-controlled model routing. No provider-native agent spawning or hidden fallback."""
from __future__ import annotations

import threading
from typing import Any, Callable

from .i18n import tr
from .util import FlowError

ROUTING_VERSION = "tiered-v2-handoff"


class TaskValidationError(FlowError):
    """A bounded schema/evidence task failed; eligible for an explicitly configured review.

    `output` is the last answer the model gave, so what is valid in it can still be kept.
    """

    def __init__(self, message: str, output: dict | None = None):
        super().__init__(message)
        self.output = output


class CallBudget:
    def __init__(self, maximum: int):
        self.maximum, self.started = maximum, 0
        self._lock = threading.Lock()

    def reserve(self) -> None:
        with self._lock:
            if self.started >= self.maximum:
                raise FlowError(tr(f"LLM 호출 상한({self.maximum})에 도달했습니다. 저장된 추출은 다음 명시적 실행에서 재사용합니다.",
                                   f"The LLM call cap ({self.maximum}) was reached. Saved extractions are reused by the next explicit run."))
            self.started += 1


class RunnerPool:
    """One mutable CLI adapter per thread/role, with a global host-call cap.

    SQLite is not held open across calls. A factory used concurrently must return
    distinct adapters. A sequential singleton is kept compatible with old tests.
    """
    def __init__(self, factory: Callable[[], Any], config: Any):
        self.factory, self.config = factory, config
        self.budget = CallBudget(config.max_calls)
        self._runners: dict[tuple[int, str], Any] = {}
        self._owners: dict[int, int] = {}
        self._lock = threading.Lock()
        self.is_mock = False
        self._parallel: bool | None = None

    def parallel_safe(self) -> bool:
        """Whether the factory makes a new adapter per call; one shared adapter serves one thread."""
        if self._parallel is None:
            with self._lock:
                self._parallel = self.factory() is not self.factory()
        return self._parallel

    def get(self, role: str) -> Any:
        key = (threading.get_ident(), role)
        with self._lock:
            if key in self._runners:
                return self._runners[key]
            runner = self.factory()
            owner = self._owners.get(id(runner))
            if owner is not None and owner != key[0]:
                raise FlowError(tr("병렬 Runner factory는 worker마다 독립 인스턴스를 반환해야 합니다.",
                                   "A parallel runner factory must return an independent instance per worker."))
            self._owners[id(runner)] = key[0]
            shared = id(runner) in {id(r) for r in self._runners.values()}
            requested = getattr(self.config, role + "_model", None)
            if requested:
                if shared and getattr(runner, "model", None) != requested:
                    raise FlowError(tr("서로 다른 단계 모델에는 독립 Runner 인스턴스가 필요합니다.",
                                       "Different stage models need independent runner instances."))
                runner.model = requested
            # Only CLI adapters take a reasoning effort; synthetic runners have no such knob.
            effort = getattr(self.config, role + "_effort", None)
            if effort and hasattr(runner, "effort"):
                if shared and runner.effort != effort:
                    raise FlowError(tr("서로 다른 단계 추론 수준에는 독립 Runner 인스턴스가 필요합니다.",
                                       "Different stage reasoning efforts need independent runner instances."))
                runner.effort = effort
            self._runners[key] = runner
            self.is_mock = self.is_mock or bool(getattr(runner, "is_mock", False))
        # Native preflight may be slow; do not serialize independent workers here.
        runner.preflight()
        return runner

