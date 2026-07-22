from __future__ import annotations

from pathlib import Path

from soutui import api
from soutui.commerce import CommerceEngine


def test_storefront_has_no_algorithm_log_component():
    template = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "soutui"
        / "web"
        / "templates"
        / "feed.html"
    ).read_text(encoding="utf-8")

    assert "algorithm-record" not in template
    assert "algo-panel" not in template
    assert "initialTraceEvents" not in template
    assert "new EventSource" not in template


def test_algorithm_backend_route_and_template_exist():
    paths = {route.path for route in api.app.routes}
    assert "/merchant/algorithm" in paths
    template = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "soutui"
        / "web"
        / "templates"
        / "algorithm_logs.html"
    ).read_text(encoding="utf-8")
    assert "算法日志后台" in template
    assert "algorithm-record" in template


def test_algorithm_probe_returns_trace_without_using_storefront_request(monkeypatch):
    engine = CommerceEngine()
    monkeypatch.setattr(api, "get_engine", lambda: engine)
    items, trace = api._run_algorithm_probe("search", "槐花蜜", 8)
    assert items
    assert trace is not None
    assert trace.events
    assert any("槐花蜜" in item.spu.title for item in items)
