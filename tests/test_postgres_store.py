from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from soutui.auth import register
from soutui.events import log_event
from soutui.payments import FakePaymentProvider
from soutui.shop import ShopService
from soutui.store import Store
from soutui.training import TrainedCtrCvrModel, train
from scripts.migrate_sqlite_to_postgres import migrate


PG_URL = os.getenv("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL is not configured")


def test_postgres_commerce_and_training_round_trip():
    store = Store(PG_URL)
    store.reset_seed()
    suffix = uuid.uuid4().hex[:8]
    user = register(store, f"pg-{suffix}@example.com", "postgres-pass-1", "Postgres Buyer")
    shop = ShopService(store)
    before = store.get_stock("sku_sbux_50")
    shop.add_to_cart(user["user_id"], "sku_sbux_50", 2, request_id="pg-checkout")
    payment = shop.start_payment(
        user["user_id"], FakePaymentProvider(),
        success_url="https://example.com/success", cancel_url="https://example.com/cancel",
    )
    assert store.get_stock("sku_sbux_50") == before - 2
    assert shop.complete_payment(payment["order_id"], payment["payment_session_id"])
    assert not shop.complete_payment(payment["order_id"], payment["payment_session_id"])

    for i in range(60):
        sku = f"pg_training_{suffix}_{i}"
        req = f"pg_req_{suffix}_{i}"
        features = {
            "query_ad_jaccard": (i % 10) / 10,
            "cate_l2_match": float(i % 2),
            "hist_ctr": 0.01 + (i % 8) / 100,
            "hist_cvr": 0.02 + (i % 5) / 100,
            "quality": 0.7 + (i % 4) / 10,
            "log_price": 3.0 + (i % 6) / 3,
            "emb_sim": (i % 7) / 7,
        }
        log_event(store, event_type="impress", user_id=user["user_id"], request_id=req, scene="search", spu_id=sku, sku_id=sku, position=i % 12, features=features)
        if i % 4 == 0:
            log_event(store, event_type="click", user_id=user["user_id"], request_id=req, scene="search", spu_id=sku, sku_id=sku)
            if i % 8 == 0:
                log_event(store, event_type="order", user_id=user["user_id"], request_id=req, scene="search", spu_id=sku, sku_id=sku)

    artifact = train(store, artifact_path=None)
    assert artifact["metrics"]["clicks_attributed"] >= 15
    persisted = store.latest_model_artifact()
    model = TrainedCtrCvrModel.from_dict(persisted)
    pctr, pcvr = model.predict({"quality": 1.0, "query_ad_jaccard": 0.8})
    assert 0 < pctr < 1 and 0 < pcvr < 1


def test_sqlite_data_migrates_transactionally(tmp_path):
    source = Store(tmp_path / "source.db")
    suffix = uuid.uuid4().hex[:8]
    user = register(source, f"migration-{suffix}@example.com", "migration-pass-1", "Migrated Buyer")
    log_event(
        source,
        event_type="impress",
        user_id=user["user_id"],
        request_id=f"migration-{suffix}",
        scene="feed",
        spu_id="spu_sbux",
        sku_id="sku_sbux_50",
        features={"quality": 0.8},
    )

    counts = migrate(tmp_path / "source.db", PG_URL, replace=True)
    target = Store(PG_URL)
    try:
        assert counts["users"] == 1
        assert counts["events"] == 1
        assert target.get_user_by_email(f"migration-{suffix}@example.com") is not None
        assert target.list_events(limit=10)[0]["request_id"] == f"migration-{suffix}"
    finally:
        target.close()
