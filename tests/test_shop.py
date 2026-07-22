from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui.events import log_event, log_impressions
from soutui.shop import ShopError, ShopService
from soutui.store import Store
from soutui.commerce import CommerceEngine
from soutui.catalog import sample_user


def _fresh_shop(tmp_path: Path) -> ShopService:
    store = Store(tmp_path / "t.db")
    store.reset_seed()
    return ShopService(store)


def test_cart_checkout_dec_stock(tmp_path: Path):
    shop = _fresh_shop(tmp_path)
    sku_id = "sku_sbux_50"
    before = shop.store.get_stock(sku_id)
    shop.add_to_cart("u_demo", sku_id, 2)
    cart = shop.cart_view("u_demo")
    assert cart["count"] == 2
    result = shop.checkout("u_demo")
    assert result.order_id.startswith("o_")
    assert shop.store.get_stock(sku_id) == before - 2
    assert shop.store.cart_count("u_demo") == 0
    order = shop.get_order(result.order_id)
    assert order is not None
    assert order["total"] == result.total


def test_checkout_empty_cart_fails(tmp_path: Path):
    shop = _fresh_shop(tmp_path)
    try:
        shop.checkout("u_demo")
        assert False, "should raise"
    except ShopError as e:
        assert "空" in str(e)


def test_add_cart_over_stock_fails(tmp_path: Path):
    shop = _fresh_shop(tmp_path)
    sku_id = "sku_coffee_pro"
    stock = shop.store.get_stock(sku_id)
    try:
        shop.add_to_cart("u_demo", sku_id, stock + 5)
        assert False, "should raise"
    except ShopError:
        pass


def test_impress_and_click_events(tmp_path: Path):
    store = Store(tmp_path / "e.db")
    store.reset_seed()
    engine = CommerceEngine()
    items, trace = engine.search(sample_user(), "咖啡", page_size=4, explain=True)
    n = log_impressions(
        store,
        user_id="u_demo",
        request_id=trace.request_id if trace else "r1",
        scene="search",
        query="咖啡",
        items=items,
    )
    assert n == len(items)
    log_event(
        store,
        event_type="click",
        user_id="u_demo",
        request_id=trace.request_id if trace else "r1",
        spu_id=items[0].spu.spu_id,
        sku_id=items[0].sku.sku_id,
        scene="search",
    )
    evs = store.list_events(limit=50)
    types = {e["event_type"] for e in evs}
    assert "impress" in types
    assert "click" in types
    impress = [e for e in evs if e["event_type"] == "impress"]
    assert impress[0]["sku_id"]
    assert impress[0]["request_id"]


def test_sync_engine_stock_uses_bulk_lookup(tmp_path: Path):
    store = Store(tmp_path / "bulk.db")
    store.reset_seed()
    engine = CommerceEngine()
    calls = 0
    original = store.get_skus

    def counted(sku_ids):
        nonlocal calls
        calls += 1
        return original(sku_ids)

    store.get_skus = counted  # type: ignore[method-assign]
    ShopService(store).sync_engine_stock(engine)
    assert calls == 1
