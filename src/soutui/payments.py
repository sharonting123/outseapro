from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol


class PaymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaymentSession:
    session_id: str
    checkout_url: str


class PaymentProvider(Protocol):
    name: str

    def create_checkout(self, order: dict[str, Any], success_url: str, cancel_url: str) -> PaymentSession: ...

    def parse_webhook(self, payload: bytes, signature: str) -> dict[str, Any]: ...


class StripePaymentProvider:
    """Stripe Checkout adapter. No secret means payments fail closed, never fake-paid."""

    name = "stripe"

    def __init__(self, secret_key: str | None = None, webhook_secret: str | None = None) -> None:
        self.secret_key = secret_key or os.getenv("STRIPE_SECRET_KEY", "")
        self.webhook_secret = webhook_secret or os.getenv("STRIPE_WEBHOOK_SECRET", "")

    @property
    def configured(self) -> bool:
        return bool(self.secret_key and self.webhook_secret)

    def create_checkout(self, order: dict[str, Any], success_url: str, cancel_url: str) -> PaymentSession:
        if not self.secret_key:
            raise PaymentError("支付尚未配置：缺少 STRIPE_SECRET_KEY")
        import stripe

        stripe.api_key = self.secret_key
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                line_items=[
                    {
                        "price_data": {
                            "currency": "cny",
                            "unit_amount": int(round(float(it["price"]) * 100)),
                            "product_data": {"name": it["title"], "metadata": {"sku_id": it["sku_id"]}},
                        },
                        "quantity": int(it["qty"]),
                    }
                    for it in order["items"]
                ],
                metadata={"order_id": order["order_id"], "user_id": order["user_id"]},
                client_reference_id=order["order_id"],
                success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=cancel_url,
            )
        except Exception as exc:
            raise PaymentError(f"创建支付会话失败：{exc}") from exc
        return PaymentSession(str(session.id), str(session.url))

    def parse_webhook(self, payload: bytes, signature: str) -> dict[str, Any]:
        if not self.webhook_secret:
            raise PaymentError("支付 Webhook 尚未配置")
        import stripe

        try:
            event = stripe.Webhook.construct_event(payload, signature, self.webhook_secret)
        except Exception as exc:
            raise PaymentError("Webhook 签名校验失败") from exc
        obj = event["data"]["object"]
        metadata = dict(obj.get("metadata") or {})
        return {
            "type": event["type"],
            "session_id": obj.get("id", ""),
            "order_id": metadata.get("order_id") or obj.get("client_reference_id", ""),
            "payment_status": obj.get("payment_status", ""),
        }


class FakePaymentProvider:
    """Deterministic provider for automated tests only; never selected by application config."""

    name = "test"

    def create_checkout(self, order: dict[str, Any], success_url: str, cancel_url: str) -> PaymentSession:
        return PaymentSession("cs_test_" + order["order_id"], success_url + "?session_id=cs_test")

    def parse_webhook(self, payload: bytes, signature: str) -> dict[str, Any]:
        raise NotImplementedError
