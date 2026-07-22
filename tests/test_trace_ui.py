from __future__ import annotations

from pathlib import Path


def test_feed_replays_embedded_request_trace_without_late_sse_subscription():
    template = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "soutui"
        / "web"
        / "templates"
        / "feed.html"
    ).read_text(encoding="utf-8")

    assert "initialTraceEvents" in template
    assert "playTrace(initialTraceEvents)" in template
    assert "new EventSource" not in template
    assert "算法执行日志" in template
