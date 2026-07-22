from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from .events import log_event
from .payments import PaymentError, PaymentProvider
from .store import Store, get_store


class ShopError(Exception):
    pass


@dataclass
class CheckoutResult:
    order_id: str
    total: float
    items: list[dict[str, Any]]


class ShopService:
    """购物车 + 模拟下单（按 sku 扣库存）。"""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store or get_store()

    def add_to_cart(self, user_id: str, sku_id: str, qty: int = 1, *, request_id: str = "") -> dict[str, Any]:
        sku = self.store.get_sku(sku_id)
        if not sku:
            raise ShopError("SKU 不存在")
        if qty <= 0:
            raise ShopError("数量必须 > 0")
        if sku["stock"] < qty:
            # 允许累加到购物车，但单次加购不超过现货
            raise ShopError(f"库存不足（剩余 {sku['stock']}）")
        new_qty = self.store.cart_add(user_id, sku_id, qty, request_id)
        log_event(
            self.store,
            event_type="add_cart",
            user_id=user_id,
            request_id=request_id,
            spu_id=sku["spu_id"],
            sku_id=sku_id,
            extra={"qty": qty, "cart_qty": new_qty},
        )
        return {"sku_id": sku_id, "qty": new_qty, "cart_count": self.store.cart_count(user_id)}

    def set_cart_qty(self, user_id: str, sku_id: str, qty: int, *, request_id: str = "") -> dict[str, Any]:
        sku = self.store.get_sku(sku_id)
        if not sku:
            raise ShopError("SKU 不存在")
        if qty > sku["stock"]:
            raise ShopError(f"库存不足（剩余 {sku['stock']}）")
        self.store.cart_set(user_id, sku_id, qty, request_id)
        return {"sku_id": sku_id, "qty": max(0, qty), "cart_count": self.store.cart_count(user_id)}

    def cart_view(self, user_id: str) -> dict[str, Any]:
        items = self.store.cart_list(user_id)
        lines = []
        total = 0.0
        for it in items:
            sub = float(it["price"]) * int(it["qty"])
            total += sub
            lines.append(
                {
                    "spu_id": it["spu_id"],
                    "sku_id": it["sku_id"],
                    "title": it["title"],
                    "brand": it["brand"],
                    "attrs": it["attrs"],
                    "attr_text": " / ".join(f"{k}:{v}" for k, v in (it["attrs"] or {}).items()),
                    "price": it["price"],
                    "stock": it["stock"],
                    "qty": it["qty"],
                    "subtotal": round(sub, 2),
                }
            )
        return {"items": lines, "total": round(total, 2), "count": sum(x["qty"] for x in lines)}

    def checkout(self, user_id: str, *, request_id: str = "") -> CheckoutResult:
        cart = self.cart_view(user_id)
        if not cart["items"]:
            raise ShopError("购物车为空")

        # 预校验库存
        for it in cart["items"]:
            sku = self.store.get_sku(it["sku_id"])
            if not sku or sku["stock"] < it["qty"]:
                raise ShopError(f"{it['title']} 库存不足")

        order_items = []
        for it in cart["items"]:
            ok = self.store.dec_stock(it["sku_id"], it["qty"])
            if not ok:
                raise ShopError(f"{it['title']} 扣库存失败，请重试")
            order_items.append(
                {
                    "spu_id": it["spu_id"],
                    "sku_id": it["sku_id"],
                    "title": it["title"],
                    "attrs": it["attrs"],
                    "price": it["price"],
                    "qty": it["qty"],
                }
            )

        order_id = "o_" + uuid.uuid4().hex[:12]
        total = float(cart["total"])
        self.store.create_order(order_id, user_id, total, order_items, time.time())
        self.store.cart_clear(user_id)

        for it in order_items:
            log_event(
                self.store,
                event_type="order",
                user_id=user_id,
                request_id=request_id,
                spu_id=it["spu_id"],
                sku_id=it["sku_id"],
                extra={"order_id": order_id, "qty": it["qty"], "price": it["price"]},
            )

        return CheckoutResult(order_id=order_id, total=total, items=order_items)

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        return self.store.get_order(order_id)

    def start_payment(
        self,
        user_id: str,
        provider: PaymentProvider,
        *,
        success_url: str,
        cancel_url: str,
    ) -> dict[str, Any]:
        order_id = "o_" + uuid.uuid4().hex[:12]
        try:
            order = self.store.reserve_cart_order(order_id, user_id, time.time())
        except ValueError as exc:
            raise ShopError(str(exc)) from exc
        try:
            session = provider.create_checkout(order, success_url, cancel_url)
            self.store.set_payment_session(order_id, provider.name, session.session_id)
        except Exception as exc:
            self.store.cancel_order(order_id, restore_cart=True)
            if isinstance(exc, PaymentError):
                raise ShopError(str(exc)) from exc
            raise
        return {
            "order_id": order_id,
            "checkout_url": session.checkout_url,
            "payment_session_id": session.session_id,
            "total": order["total"],
        }

    def complete_payment(self, order_id: str, payment_session_id: str = "") -> bool:
        changed = self.store.mark_order_paid(order_id, payment_session_id)
        if not changed:
            return False
        order = self.store.get_order(order_id)
        if order:
            for it in order["items"]:
                log_event(
                    self.store,
                    event_type="order",
                    user_id=order["user_id"],
                    request_id=it.get("request_id", ""),
                    spu_id=it["spu_id"],
                    sku_id=it["sku_id"],
                    extra={
                        "order_id": order_id,
                        "qty": it["qty"],
                        "price": it["price"],
                        "payment_provider": order.get("payment_provider", ""),
                    },
                )
        return True

    def sync_engine_stock(self, engine: Any) -> None:
        """把 DB 库存同步回内存 CommerceEngine.skus。"""
        rows = self.store.get_skus([sku.sku_id for sku in engine.skus])
        for sku in engine.skus:
            row = rows.get(sku.sku_id)
            if row:
                sku.price = float(row["price"])
                sku.stock = int(row["stock"])
                sku.sales = int(row["sales"])
