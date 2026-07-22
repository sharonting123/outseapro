from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient

from soutui import api
from soutui.commerce import CommerceEngine
from soutui.shop import ShopService
from soutui.store import Store


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


def test_admin_algorithm_backend_route_and_template_exist():
    paths = {route.path for route in api.app.routes}
    assert "/admin" in paths
    assert "/merchant/algorithm" not in paths
    template = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "soutui"
        / "web"
        / "templates"
        / "algorithm_logs.html"
    ).read_text(encoding="utf-8")
    assert "管理后台 · 算法日志" in template
    assert 'action="{{ base_path }}/admin"' in template
    assert "algorithm-record" in template


def test_admin_algorithm_backend_rejects_merchant(tmp_path, monkeypatch):
    store = Store(tmp_path / "admin-auth.db")
    store.create_user("merchant_test", "merchant@example.com", "unused", "商家", "merchant")
    token = "merchant-session"
    store.create_session(token, "merchant_test", time.time() + 60)
    monkeypatch.setattr(api, "get_store", lambda: store)

    with TestClient(api.app) as client:
        client.cookies.set("soutui_session", token)
        response = client.get("/admin")

    assert response.status_code == 403
    assert response.json()["detail"] == "没有权限"


def test_admin_algorithm_backend_allows_admin(tmp_path, monkeypatch):
    store = Store(tmp_path / "admin-ok.db")
    store.create_user("admin_test", "admin@example.com", "unused", "管理员", "admin")
    token = "admin-session"
    store.create_session(token, "admin_test", time.time() + 60)
    engine = CommerceEngine()
    shop = ShopService(store)
    monkeypatch.setattr(api, "get_store", lambda: store)
    monkeypatch.setattr(api, "get_shop", lambda: shop)
    monkeypatch.setattr(api, "get_engine", lambda: engine)

    with TestClient(api.app) as client:
        client.cookies.set("soutui_session", token)
        response = client.get("/admin")

    assert response.status_code == 200
    assert "管理后台 · 算法日志" in response.text


def test_algorithm_probe_returns_trace_without_using_storefront_request(monkeypatch):
    engine = CommerceEngine()
    monkeypatch.setattr(api, "get_engine", lambda: engine)
    items, trace = api._run_algorithm_probe("search", "槐花蜜", 8)
    assert items
    assert trace is not None
    assert trace.events
    assert any("槐花蜜" in item.spu.title for item in items)
