from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from queue import Empty
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .catalog import pick_sku, sample_user, skus_by_spu, spu_min_price
from .commerce import CommerceEngine, build_default_engine
from .events import log_event, log_impressions
from .mixer import FeedItem
from .shop import ShopError, ShopService
from .store import get_store
from .trace import HUB
from .auth import (
    authenticate,
    authenticate_admin,
    bootstrap_admin,
    bootstrap_merchant,
    current_admin,
    current_user,
    end_admin_session,
    end_session,
    hash_password,
    register,
    require_user,
    start_admin_session,
    start_session,
)
from .payments import PaymentError, StripePaymentProvider
from .training import DEFAULT_ARTIFACT, load_model_if_available, train

WEB_DIR = Path(__file__).resolve().parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

app = FastAPI(title="甄选商城", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

_engine: CommerceEngine | None = None
_shop: ShopService | None = None
_model_checked_at = 0.0

CATE_STYLE = {
    "跑鞋": ("thumb-saffron", "👟"),
    "耳机": ("thumb-caramel", "🎧"),
    "手机配件": ("thumb-clay", "📱"),
    "面膜": ("thumb-rosewood", "✨"),
    "咖啡": ("thumb-cocoa", "☕"),
    "童书": ("thumb-apricot", "📚"),
    "电子书": ("thumb-walnut", "📖"),
    "蜂蜜": ("thumb-honey", "🍯"),
    "蜂产品": ("thumb-royal", "🐝"),
    "蜂蜜礼盒": ("thumb-gift", "🎁"),
}

DEMO_USER = "u_demo"
PAYMENTS = StripePaymentProvider()
BASE_PATH = os.getenv("SOUTUI_BASE_PATH", "").rstrip("/")


def _url(path: str) -> str:
    return BASE_PATH + (path if path.startswith("/") else "/" + path)


def get_engine() -> CommerceEngine:
    global _engine, _model_checked_at
    if _engine is None:
        _engine = build_default_engine()
        ShopService().sync_engine_stock(_engine)
        _model_checked_at = time.monotonic()
    store = get_store()
    refresh_seconds = max(5.0, float(os.getenv("SOUTUI_MODEL_REFRESH_SECONDS", "60")))
    now = time.monotonic()
    if store.is_postgres and now - _model_checked_at >= refresh_seconds:
        _model_checked_at = now
        latest = store.latest_model_run()
        current_run_id = getattr(_engine.ads_engine.ranker.model, "run_id", "")
        if latest and latest["status"] == "ready" and latest["run_id"] != current_run_id:
            model = load_model_if_available(store=store)
            if model is not None:
                _engine.ads_engine.ranker.model = model
    return _engine


def get_shop() -> ShopService:
    global _shop
    if _shop is None:
        _shop = ShopService(get_store())
    return _shop


def _thumb(cate_l2: str) -> tuple[str, str]:
    return CATE_STYLE.get(cate_l2, ("thumb-walnut", "🛒"))


def _card_dict(item: FeedItem, sku_count: int = 1, price_from: float = 0.0) -> dict[str, Any]:
    thumb_class, emoji = _thumb(item.spu.cate_l2)
    return {
        "position": item.position,
        "item_type": item.item_type.value,
        "is_ad": item.is_ad,
        "disclosure": item.disclosure,
        "spu_id": item.spu.spu_id,
        "sku_id": item.sku.sku_id,
        "product_id": item.spu.spu_id,
        "title": item.spu.title,
        "brand": item.spu.brand,
        "price": item.sku.price,
        "price_from": price_from or item.sku.price,
        "attrs": dict(item.sku.attrs),
        "attr_text": item.sku.attr_text(),
        "sku_count": sku_count,
        "cate_l2": item.spu.cate_l2,
        "score": round(item.score, 4),
        "sales": item.sku.sales,
        "rating": item.spu.rating,
        "stock": item.sku.stock,
        "ad_id": item.ad_id,
        "charge": round(item.charge, 4),
        "charge_unit": item.charge_unit,
        "thumb_class": thumb_class,
        "thumb_emoji": emoji,
    }


def _pack_items(items: list[FeedItem], engine: CommerceEngine) -> list[dict[str, Any]]:
    sku_map = skus_by_spu(engine.skus)
    cards = []
    for x in items:
        group = sku_map.get(x.spu.spu_id, [x.sku])
        cards.append(_card_dict(x, sku_count=len(group), price_from=spu_min_price(group)))
    return cards


def _base_ctx(request: Request | None = None, **extra: Any) -> dict[str, Any]:
    shop = get_shop()
    user = current_user(request, shop.store) if request else None
    ctx = {
        "cart_count": shop.store.cart_count(user["user_id"]) if user else 0,
        "current_user": user,
        "payment_configured": PAYMENTS.configured,
        "base_path": BASE_PATH,
        "query": "",
        "scene": "",
        "flash": "",
        "error": "",
    }
    ctx.update(extra)
    return ctx


def _admin_ctx(request: Request, **extra: Any) -> dict[str, Any]:
    ctx = {
        "admin": current_admin(request, get_store()),
        "base_path": BASE_PATH,
        "error": "",
    }
    ctx.update(extra)
    return ctx


class CardOut(BaseModel):
    position: int
    item_type: str
    is_ad: bool
    disclosure: str = ""
    spu_id: str
    sku_id: str
    product_id: str = ""
    title: str
    brand: str
    price: float
    price_from: float = 0.0
    attrs: dict[str, str] = Field(default_factory=dict)
    attr_text: str = ""
    sku_count: int = 1
    cate_l2: str
    score: float
    sales: int = 0
    rating: float = 4.5
    stock: int = 0
    ad_id: str = ""
    charge: float = 0.0
    charge_unit: str = ""


class FeedResponse(BaseModel):
    scene: str
    query: str = ""
    items: list[CardOut]
    organic_count: int
    ad_count: int
    request_id: str = ""
    trace: dict[str, Any] | None = None


class CartBody(BaseModel):
    action: str = "add"  # add | set
    sku_id: str
    qty: int = 1
    request_id: str = ""


class TrackBody(BaseModel):
    event_type: str = "click"
    request_id: str = ""
    scene: str = ""
    query: str = ""
    spu_id: str = ""
    sku_id: str = ""
    position: int = -1
    is_ad: bool = False
    ad_id: str = ""


@app.get("/health")
def health() -> dict[str, Any]:
    model = get_engine().ads_engine.ranker.model
    return {
        "status": "ok",
        "database": "postgresql" if get_store().is_postgres else "sqlite",
        "payment": "configured" if PAYMENTS.configured else "missing_keys",
        "rank_model": type(model).__name__,
    }


@app.on_event("startup")
def startup() -> None:
    store = get_store()
    bootstrap_merchant(store)
    bootstrap_admin(store)


def _event_user(request: Request) -> str:
    user = current_user(request, get_store())
    return user["user_id"] if user else "anonymous"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = Query("/")):
    return templates.TemplateResponse(request, "login.html", _base_ctx(request, next=_url("/") if next == "/" else next))


@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")):
    user = authenticate(get_store(), email, password)
    if not user:
        return templates.TemplateResponse(request, "login.html", _base_ctx(request, next=next, error="邮箱或密码错误"), status_code=400)
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    start_session(get_store(), response, user["user_id"])
    return response


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", _base_ctx(request))


@app.post("/register")
def register_submit(request: Request, email: str = Form(...), display_name: str = Form(...), password: str = Form(...)):
    try:
        user = register(get_store(), email, password, display_name)
    except Exception as exc:
        return templates.TemplateResponse(request, "register.html", _base_ctx(request, error=str(exc)), status_code=400)
    response = RedirectResponse(_url("/"), status_code=303)
    start_session(get_store(), response, user["user_id"])
    return response


@app.post("/logout")
def logout(request: Request):
    response = RedirectResponse(_url("/"), status_code=303)
    end_session(get_store(), request, response)
    return response


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    require_user(request, get_store())
    return templates.TemplateResponse(request, "account.html", _base_ctx(request))


@app.post("/account/password")
def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...)):
    user = require_user(request, get_store())
    if not authenticate(get_store(), user["email"], current_password):
        return templates.TemplateResponse(request, "account.html", _base_ctx(request, error="当前密码错误"), status_code=400)
    try:
        encoded = hash_password(new_password)
    except ValueError as exc:
        return templates.TemplateResponse(request, "account.html", _base_ctx(request, error=str(exc)), status_code=400)
    get_store().update_password(user["user_id"], encoded)
    response = RedirectResponse(_url("/account"), status_code=303)
    start_session(get_store(), response, user["user_id"])
    return response


@app.get("/admin/trace/stream")
async def admin_trace_stream(request: Request) -> StreamingResponse:
    if not current_admin(request, get_store()):
        raise HTTPException(401, "管理员未登录")
    q = HUB.subscribe()

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'hello', 'msg': 'trace connected'}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    evt = await asyncio.to_thread(q.get, True, 12.0)
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except Empty:
                    yield ": ping\n\n"
        finally:
            HUB.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


def _run_feed(page_size: int, hour: int, delay_ms: int, explain: bool):
    engine = get_engine()
    get_shop().sync_engine_stock(engine)
    user = sample_user()
    return engine.feed(
        user,
        page_size=page_size,
        hour=hour,
        step_delay=delay_ms / 1000.0,
        explain=explain,
    )


def _run_search(q: str, page_size: int, hour: int, delay_ms: int, explain: bool):
    engine = get_engine()
    get_shop().sync_engine_stock(engine)
    user = sample_user()
    return engine.search(
        user,
        q,
        page_size=page_size,
        hour=hour,
        step_delay=delay_ms / 1000.0,
        explain=explain,
    )


@app.get("/", response_class=HTMLResponse)
async def home_page(
    request: Request,
    page_size: int = Query(12, ge=1, le=30),
    delay_ms: int = Query(80, ge=0, le=800),
):
    items, trace = await asyncio.to_thread(_run_feed, page_size, 12, delay_ms, True)
    engine = get_engine()
    rid = trace.request_id if trace else ""
    log_impressions(
        get_store(),
        user_id=_event_user(request),
        request_id=rid,
        scene="feed",
        query="",
        items=items,
    )
    cards = _pack_items(items, engine)
    return templates.TemplateResponse(
        request,
        "feed.html",
        _base_ctx(request,
            scene="feed",
            items=cards,
            organic_count=sum(1 for c in cards if not c["is_ad"]),
            ad_count=sum(1 for c in cards if c["is_ad"]),
            request_id=rid,
            trace=trace.as_dict() if trace else None,
        ),
    )


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = Query("跑鞋", min_length=1),
    page_size: int = Query(12, ge=1, le=30),
    delay_ms: int = Query(80, ge=0, le=800),
):
    items, trace = await asyncio.to_thread(_run_search, q, page_size, 12, delay_ms, True)
    engine = get_engine()
    rid = trace.request_id if trace else ""
    log_impressions(
        get_store(),
        user_id=_event_user(request),
        request_id=rid,
        scene="search",
        query=q,
        items=items,
    )
    cards = _pack_items(items, engine)
    return templates.TemplateResponse(
        request,
        "feed.html",
        _base_ctx(request,
            scene="search",
            query=q,
            items=cards,
            organic_count=sum(1 for c in cards if not c["is_ad"]),
            ad_count=sum(1 for c in cards if c["is_ad"]),
            request_id=rid,
            trace=trace.as_dict() if trace else None,
        ),
    )


@app.get("/item/{spu_id}", response_class=HTMLResponse)
async def item_page(
    request: Request,
    spu_id: str,
    sku: str = Query(""),
    rid: str = Query(""),
    pos: int = Query(-1),
    scene: str = Query(""),
):
    engine = get_engine()
    get_shop().sync_engine_stock(engine)
    spu = engine.spus_by_id.get(spu_id)
    if not spu:
        raise HTTPException(404, "商品不存在")
    skus = [s for s in engine.skus if s.spu_id == spu_id]
    selected = pick_sku(skus, preferred_sku_id=sku) or (skus[0] if skus else None)
    if not selected:
        raise HTTPException(404, "无可用规格")

    # 详情曝光
    log_event(
        get_store(),
        event_type="impress",
        user_id=_event_user(request),
        request_id=rid,
        scene=scene or "detail",
        spu_id=spu_id,
        sku_id=selected.sku_id,
        position=pos,
        extra={"page": "detail"},
    )

    thumb_class, emoji = _thumb(spu.cate_l2)
    sku_views = [
        {
            "sku_id": s.sku_id,
            "price": s.price,
            "stock": s.stock,
            "sales": s.sales,
            "attrs": s.attrs,
            "attr_text": s.attr_text(),
        }
        for s in skus
    ]
    return templates.TemplateResponse(
        request,
        "item.html",
        _base_ctx(request,
            scene=scene or "detail",
            spu=spu,
            skus=sku_views,
            selected={
                "sku_id": selected.sku_id,
                "price": selected.price,
                "stock": selected.stock,
            },
            price_from=spu_min_price(skus),
            thumb_class=thumb_class,
            thumb_emoji=emoji,
            request_id=rid,
        ),
    )


@app.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request):
    user = current_user(request, get_store())
    if not user:
        return RedirectResponse(_url("/login?next=") + _url("/cart"), status_code=303)
    cart = get_shop().cart_view(user["user_id"])
    return templates.TemplateResponse(request, "cart.html", _base_ctx(request, scene="cart", cart=cart))


@app.get("/order/{order_id}", response_class=HTMLResponse)
async def order_page(request: Request, order_id: str):
    user = require_user(request, get_store())
    order = get_shop().get_order(order_id)
    if not order or (order["user_id"] != user["user_id"] and user.get("role") not in ("merchant", "admin")):
        raise HTTPException(404, "订单不存在")
    return templates.TemplateResponse(request, "order.html", _base_ctx(request, scene="order", order=order))


@app.post("/api/cart")
async def api_cart(request: Request, body: CartBody):
    user = require_user(request, get_store())
    shop = get_shop()
    try:
        if body.action == "set":
            return shop.set_cart_qty(user["user_id"], body.sku_id, body.qty, request_id=body.request_id)
        return shop.add_to_cart(user["user_id"], body.sku_id, body.qty, request_id=body.request_id)
    except ShopError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/checkout")
async def api_checkout(request: Request):
    user = require_user(request, get_store())
    shop = get_shop()
    try:
        base = str(request.base_url).rstrip("/")
        result = shop.start_payment(
            user["user_id"], PAYMENTS,
            success_url=base + "/payment/success",
            cancel_url=base + "/cart?payment=cancelled",
        )
        shop.sync_engine_stock(get_engine())
        return result
    except ShopError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/track")
async def api_track(request: Request, body: TrackBody):
    log_event(
        get_store(),
        event_type=body.event_type or "click",
        user_id=_event_user(request),
        request_id=body.request_id,
        scene=body.scene,
        query=body.query,
        spu_id=body.spu_id,
        sku_id=body.sku_id,
        position=body.position,
        is_ad=body.is_ad,
        ad_id=body.ad_id,
    )
    return {"ok": True}


@app.get("/api/search", response_model=FeedResponse)
async def api_search(
    request: Request,
    q: str = Query(..., min_length=1),
    page_size: int = Query(12, ge=1, le=30),
    hour: int = Query(12, ge=0, le=23),
    delay_ms: int = Query(0, ge=0, le=800),
    explain: int = Query(1, ge=0, le=1),
) -> FeedResponse:
    items, trace = await asyncio.to_thread(_run_search, q, page_size, hour, delay_ms, bool(explain))
    engine = get_engine()
    rid = trace.request_id if trace else ""
    log_impressions(get_store(), user_id=_event_user(request), request_id=rid, scene="search", query=q, items=items)
    cards = [CardOut(**{k: v for k, v in c.items() if k in CardOut.model_fields}) for c in _pack_items(items, engine)]
    return FeedResponse(
        scene="search",
        query=q,
        items=cards,
        organic_count=sum(1 for c in cards if not c.is_ad),
        ad_count=sum(1 for c in cards if c.is_ad),
        request_id=rid,
        trace=trace.as_dict() if trace else None,
    )


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    try:
        event = PAYMENTS.parse_webhook(payload, request.headers.get("stripe-signature", ""))
    except PaymentError as exc:
        raise HTTPException(400, str(exc)) from exc
    order_id = event.get("order_id", "")
    if event["type"] == "checkout.session.completed" and event.get("payment_status") == "paid":
        get_shop().complete_payment(order_id, event.get("session_id", ""))
        get_shop().sync_engine_stock(get_engine())
    elif event["type"] == "checkout.session.expired":
        get_store().cancel_order(order_id)
        get_shop().sync_engine_stock(get_engine())
    return {"received": True}


@app.get("/payment/success", response_class=HTMLResponse)
def payment_success(request: Request, session_id: str = Query("")):
    user = require_user(request, get_store())
    order = get_store().get_order_by_payment_session(session_id)
    if not order or order["user_id"] != user["user_id"]:
        raise HTTPException(404, "订单不存在")
    return templates.TemplateResponse(request, "payment_result.html", _base_ctx(request, order=order))


@app.get("/merchant", response_class=HTMLResponse)
def merchant_dashboard(request: Request):
    user = require_user(request, get_store(), "merchant")
    products = get_store().merchant_products(user["user_id"])
    orders = get_store().merchant_orders(user["user_id"])
    return templates.TemplateResponse(
        request, "merchant.html",
        _base_ctx(request, scene="merchant", products=products, orders=orders, model_run=get_store().latest_model_run()),
    )


def _run_algorithm_probe(mode: str, query: str, page_size: int):
    """Run an isolated diagnostic without recording impressions or spending live budgets."""
    active = get_engine()
    probe = CommerceEngine(
        spus=active.spus,
        skus=active.skus,
        model=active.ads_engine.ranker.model,
    )
    if mode == "search":
        return probe.search(sample_user(), query, page_size=page_size, step_delay=0.0, explain=True)
    return probe.feed(sample_user(), page_size=page_size, step_delay=0.0, explain=True)


@app.get("/admin", response_class=HTMLResponse)
async def admin_algorithm_logs(
    request: Request,
    mode: str = Query("feed", pattern="^(feed|search)$"),
    q: str = Query("蜂蜜", max_length=80),
    page_size: int = Query(12, ge=1, le=30),
):
    if not current_admin(request, get_store()):
        return RedirectResponse(_url("/admin/login?next=/admin"), status_code=303)
    query = q.strip() or "蜂蜜"
    items, trace = await asyncio.to_thread(_run_algorithm_probe, mode, query, page_size)
    engine = get_engine()
    cards = _pack_items(items, engine)
    return templates.TemplateResponse(
        request,
        "algorithm_logs.html",
        _admin_ctx(
            request,
            mode=mode,
            query=query,
            page_size=page_size,
            items=cards,
            organic_count=sum(1 for card in cards if not card["is_ad"]),
            ad_count=sum(1 for card in cards if card["is_ad"]),
            trace=trace.as_dict(),
        ),
    )


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, next: str = Query("/admin")):
    if current_admin(request, get_store()):
        return RedirectResponse(_url("/admin"), status_code=303)
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        _admin_ctx(request, next=next),
    )


@app.post("/admin/login")
def admin_login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin"),
):
    admin = authenticate_admin(get_store(), email, password)
    if not admin:
        return templates.TemplateResponse(
            request,
            "admin_login.html",
            _admin_ctx(request, next=next, error="管理员邮箱或密码错误"),
            status_code=400,
        )
    admin_root = _url("/admin")
    target = next if next.startswith(admin_root) and not next.startswith("//") else admin_root
    response = RedirectResponse(target, status_code=303)
    start_admin_session(get_store(), response, admin["admin_id"])
    return response


@app.post("/admin/logout")
def admin_logout(request: Request):
    response = RedirectResponse(_url("/admin/login"), status_code=303)
    end_admin_session(get_store(), request, response)
    return response


@app.post("/merchant/sku/{sku_id}")
def merchant_update_sku(request: Request, sku_id: str, price: float = Form(...), stock: int = Form(...)):
    user = require_user(request, get_store(), "merchant")
    if price <= 0 or stock < 0 or not get_store().update_merchant_sku(user["user_id"], sku_id, price, stock):
        raise HTTPException(400, "商品不存在或价格/库存无效")
    global _engine
    _engine = None
    return RedirectResponse(_url("/merchant"), status_code=303)


@app.post("/merchant/products")
def merchant_create_product(
    request: Request,
    spu_id: str = Form(...), sku_id: str = Form(...), title: str = Form(...), brand: str = Form(...),
    cate_l1: str = Form(...), cate_l2: str = Form(...), price: float = Form(...), stock: int = Form(...),
):
    user = require_user(request, get_store(), "merchant")
    try:
        get_store().create_merchant_product(
            user["user_id"], spu_id=spu_id, sku_id=sku_id, title=title, brand=brand,
            cate_l1=cate_l1, cate_l2=cate_l2, price=price, stock=stock,
        )
    except Exception as exc:
        raise HTTPException(400, f"创建商品失败：{exc}") from exc
    global _engine
    _engine = None
    return RedirectResponse(_url("/merchant"), status_code=303)


def _train_and_reload() -> None:
    global _engine, _model_checked_at
    store = get_store()
    train(store, None if store.is_postgres else DEFAULT_ARTIFACT)
    _engine = None
    _model_checked_at = 0.0


@app.post("/merchant/train")
def merchant_train(request: Request, tasks: BackgroundTasks):
    require_user(request, get_store(), "merchant")
    latest = get_store().latest_model_run()
    if latest and latest["status"] == "training":
        raise HTTPException(409, "已有训练任务运行中")
    tasks.add_task(_train_and_reload)
    return RedirectResponse(_url("/merchant"), status_code=303)


@app.get("/api/feed", response_model=FeedResponse)
async def api_feed(
    request: Request,
    page_size: int = Query(12, ge=1, le=30),
    hour: int = Query(12, ge=0, le=23),
    delay_ms: int = Query(0, ge=0, le=800),
    explain: int = Query(1, ge=0, le=1),
) -> FeedResponse:
    items, trace = await asyncio.to_thread(_run_feed, page_size, hour, delay_ms, bool(explain))
    engine = get_engine()
    rid = trace.request_id if trace else ""
    log_impressions(get_store(), user_id=_event_user(request), request_id=rid, scene="feed", query="", items=items)
    cards = [CardOut(**{k: v for k, v in c.items() if k in CardOut.model_fields}) for c in _pack_items(items, engine)]
    return FeedResponse(
        scene="feed",
        query="",
        items=cards,
        organic_count=sum(1 for c in cards if not c.is_ad),
        ad_count=sum(1 for c in cards if c.is_ad),
        request_id=rid,
        trace=trace.as_dict() if trace else None,
    )


# 兼容旧路径
@app.get("/feed")
async def legacy_feed():
    return RedirectResponse(_url("/"))
