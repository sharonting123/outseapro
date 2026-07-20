from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Iterator


_current: ContextVar["AlgoTrace | None"] = ContextVar("algo_trace", default=None)


@dataclass
class TraceEvent:
    request_id: str
    seq: int
    stage: str
    title: str
    formula: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "seq": self.seq,
            "stage": self.stage,
            "title": self.title,
            "formula": self.formula,
            "detail": self.detail,
            "ts": self.ts,
        }


class TraceHub:
    """全局广播：算法步骤 → SSE 订阅者。"""

    def __init__(self) -> None:
        self._subs: list[Queue] = []
        self._lock = threading.Lock()
        self.history: list[dict[str, Any]] = []
        self._history_max = 200

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.history.append(event)
            if len(self.history) > self._history_max:
                self.history = self.history[-self._history_max :]
            dead: list[Queue] = []
            for q in self._subs:
                try:
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            for q in dead:
                if q in self._subs:
                    self._subs.remove(q)

    def subscribe(self) -> Queue:
        q: Queue = Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


HUB = TraceHub()


@dataclass
class AlgoTrace:
    """单次请求的算法轨迹。"""

    request_id: str
    scene: str
    query: str = ""
    events: list[TraceEvent] = field(default_factory=list)
    step_delay: float = 0.0  # >0 时放慢，方便边跑边看
    _seq: int = 0

    def step(
        self,
        stage: str,
        title: str,
        *,
        formula: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._seq += 1
        evt = TraceEvent(
            request_id=self.request_id,
            seq=self._seq,
            stage=stage,
            title=title,
            formula=formula,
            detail=detail or {},
            ts=time.time(),
        )
        self.events.append(evt)
        payload = {"type": "step", **evt.to_dict()}
        HUB.publish(payload)
        if self.step_delay > 0:
            time.sleep(self.step_delay)

    def done(self, extra: dict[str, Any] | None = None) -> None:
        HUB.publish(
            {
                "type": "done",
                "request_id": self.request_id,
                "scene": self.scene,
                "query": self.query,
                "steps": len(self.events),
                **(extra or {}),
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "scene": self.scene,
            "query": self.query,
            "events": [e.to_dict() for e in self.events],
        }


def get_trace() -> AlgoTrace | None:
    return _current.get()


def emit(stage: str, title: str, *, formula: str = "", detail: dict[str, Any] | None = None) -> None:
    t = get_trace()
    if t is not None:
        t.step(stage, title, formula=formula, detail=detail)


@contextmanager
def start_trace(
    scene: str,
    query: str = "",
    *,
    step_delay: float = 0.0,
    request_id: str | None = None,
) -> Iterator[AlgoTrace]:
    trace = AlgoTrace(
        request_id=request_id or uuid.uuid4().hex[:10],
        scene=scene,
        query=query,
        step_delay=step_delay,
    )
    token = _current.set(trace)
    try:
        emit(
            "start",
            f"开始处理「{scene}」请求",
            formula="request → organic ∥ ads → mixer → feed",
            detail={"query": query, "request_id": trace.request_id},
        )
        yield trace
        trace.done()
    finally:
        _current.reset(token)


def iter_sse(q: Queue, *, heartbeat: float = 15.0) -> Iterator[str]:
    """把 Queue 事件转成 SSE 文本帧。"""
    while True:
        try:
            evt = q.get(timeout=heartbeat)
            import json

            yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        except Empty:
            yield ": ping\n\n"
