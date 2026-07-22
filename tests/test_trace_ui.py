from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient

from soutui import api
from soutui.auth import ADMIN_COOKIE_NAME, hash_password
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
    assert "/admin/login" in paths
    assert "/admin/logout" in paths
    assert "/admin/trace/stream" in paths
    assert "/trace/stream" not in paths
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


def test_admin_algorithm_backend_does_not_accept_merchant_session(tmp_path, monkeypatch):
    store = Store(tmp_path / "admin-auth.db")
    store.create_user("merchant_test", "merchant@example.com", "unused", "商家", "merchant")
    token = "merchant-session"
    store.create_session(token, "merchant_test", time.time() + 60)
    monkeypatch.setattr(api, "get_store", lambda: store)

    with TestClient(api.app) as client:
        client.cookies.set("soutui_session", token)
        response = client.get("/admin", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login?next=/admin"

    with TestClient(api.app) as client:
        client.cookies.set("soutui_session", token)
        stream_response = client.get("/admin/trace/stream")

    assert stream_response.status_code == 401


def test_admin_algorithm_backend_allows_admin(tmp_path, monkeypatch):
    store = Store(tmp_path / "admin-ok.db")
    store.create_admin("admin_test", "admin@example.com", hash_password("admin-test-pass"), "管理员")
    token = "admin-session"
    store.create_admin_session(token, "admin_test", time.time() + 60)
    engine = CommerceEngine()
    shop = ShopService(store)
    monkeypatch.setattr(api, "get_store", lambda: store)
    monkeypatch.setattr(api, "get_shop", lambda: shop)
    monkeypatch.setattr(api, "get_engine", lambda: engine)

    with TestClient(api.app) as client:
        client.cookies.set(ADMIN_COOKIE_NAME, token, path="/admin")
        response = client.get("/admin")

    assert response.status_code == 200
    assert "管理后台 · 算法日志" in response.text


def test_admin_login_uses_separate_supabase_backed_identity(tmp_path, monkeypatch):
    store = Store(tmp_path / "admin-login.db")
    store.create_user("merchant_same", "owner@example.com", "unused", "商家", "merchant")
    store.create_admin("admin_same", "owner@example.com", hash_password("separate-admin-pass"), "平台管理员")
    monkeypatch.setattr(api, "get_store", lambda: store)

    with TestClient(api.app) as client:
        response = client.post(
            "/admin/login",
            data={"email": "owner@example.com", "password": "separate-admin-pass", "next": "/admin"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    assert ADMIN_COOKIE_NAME in response.cookies
    assert store.get_user_by_email("owner@example.com")["role"] == "merchant"
    assert store.get_admin_by_email("owner@example.com")["admin_id"] == "admin_same"


def test_algorithm_probe_returns_trace_without_using_storefront_request(monkeypatch):
    engine = CommerceEngine()
    monkeypatch.setattr(api, "get_engine", lambda: engine)
    items, trace = api._run_algorithm_probe("search", "槐花蜜", 8)
    assert items
    assert trace is not None
    assert trace.events
    assert any("槐花蜜" in item.spu.title for item in items)
