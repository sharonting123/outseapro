from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui.auth import authenticate, hash_password, register, verify_password
from soutui.events import log_event
from soutui.payments import FakePaymentProvider
from soutui.shop import ShopService
from soutui.store import Store
from soutui.training import TrainedCtrCvrModel, train


def test_catalog_upgrade_is_idempotent_and_preserves_existing_stock(tmp_path: Path):
    db_path = tmp_path / "catalog-upgrade.db"
    first = Store(db_path)
    first.set_stock("sku_acacia_500", 7)
    first.close()

    upgraded = Store(db_path)
    assert upgraded.get_stock("sku_acacia_500") == 7
    assert upgraded.get_sku("sku_honey_gift_4") is not None
    upgraded.close()


def test_password_hash_and_session_user(tmp_path: Path):
    store = Store(tmp_path / "auth.db")
    encoded = hash_password("correct-horse")
    assert verify_password("correct-horse", encoded)
    assert not verify_password("wrong-password", encoded)
    user = register(store, "User@Example.com", "correct-horse", "小甄")
    assert authenticate(store, "user@example.com", "correct-horse")["user_id"] == user["user_id"]
    store.create_session("secret-token", user["user_id"], 4_000_000_000)
    assert store.session_user("secret-token")["email"] == "user@example.com"
    store.update_password(user["user_id"], hash_password("a-new-password"))
    assert store.session_user("secret-token") is None
    assert authenticate(store, "user@example.com", "a-new-password")


def test_real_payment_state_machine_is_idempotent(tmp_path: Path):
    store = Store(tmp_path / "pay.db")
    store.reset_seed()
    shop = ShopService(store)
    user = register(store, "buyer@example.com", "buyer-pass-1", "买家")
    before = store.get_stock("sku_sbux_50")
    shop.add_to_cart(user["user_id"], "sku_sbux_50", 2, request_id="req-buy")
    payment = shop.start_payment(
        user["user_id"], FakePaymentProvider(),
        success_url="https://shop.test/payment/success", cancel_url="https://shop.test/cart",
    )
    order = store.get_order(payment["order_id"])
    assert order["status"] == "pending_payment"
    assert store.get_stock("sku_sbux_50") == before - 2
    assert store.cart_count(user["user_id"]) == 0
    assert shop.complete_payment(order["order_id"], payment["payment_session_id"])
    assert not shop.complete_payment(order["order_id"], payment["payment_session_id"])
    assert store.get_order(order["order_id"])["status"] == "paid"
    assert len([e for e in store.list_events(100, "order") if e["extra_json"].find(order["order_id"]) >= 0]) == 1


def test_cancelled_payment_releases_stock_and_restores_cart(tmp_path: Path):
    store = Store(tmp_path / "cancel.db")
    store.reset_seed()
    shop = ShopService(store)
    user = register(store, "cancel@example.com", "cancel-pass-1", "取消买家")
    before = store.get_stock("sku_sbux_50")
    shop.add_to_cart(user["user_id"], "sku_sbux_50", 1)
    payment = shop.start_payment(user["user_id"], FakePaymentProvider(), success_url="https://x/s", cancel_url="https://x/c")
    assert store.cancel_order(payment["order_id"], restore_cart=True)
    assert not store.cancel_order(payment["order_id"], restore_cart=True)
    assert store.get_stock("sku_sbux_50") == before
    assert store.cart_count(user["user_id"]) == 1


def test_training_builds_loadable_ctr_cvr_artifact(tmp_path: Path):
    store = Store(tmp_path / "train.db")
    for i in range(60):
        sku = f"training_sku_{i}"
        req = f"training_req_{i}"
        feats = {
            "query_ad_jaccard": (i % 10) / 10,
            "cate_l2_match": float(i % 2),
            "hist_ctr": 0.01 + (i % 8) / 100,
            "hist_cvr": 0.02 + (i % 5) / 100,
            "quality": 0.7 + (i % 4) / 10,
            "log_price": 3.0 + (i % 6) / 3,
            "emb_sim": (i % 7) / 7,
        }
        log_event(store, event_type="impress", user_id="train_user", request_id=req, scene="search", spu_id=sku, sku_id=sku, position=i % 12, features=feats)
        if i % 4 == 0:
            log_event(store, event_type="click", user_id="train_user", request_id=req, scene="search", spu_id=sku, sku_id=sku)
            if i % 8 == 0:
                log_event(store, event_type="order", user_id="train_user", request_id=req, scene="search", spu_id=sku, sku_id=sku)
    artifact_path = tmp_path / "model.json"
    artifact = train(store, artifact_path)
    assert artifact["metrics"]["impressions"] == 60
    assert artifact["metrics"]["clicks_attributed"] == 15
    assert artifact["metrics"]["orders_attributed"] > 0
    model = TrainedCtrCvrModel.load(artifact_path)
    pctr, pcvr = model.predict({"query_ad_jaccard": 0.8, "quality": 1.0})
    assert 0 < pctr < 1 and 0 < pcvr < 1
    assert store.latest_model_run()["status"] == "ready"
