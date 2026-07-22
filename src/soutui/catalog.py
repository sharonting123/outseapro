from __future__ import annotations

import hashlib
import math
from typing import Iterable

from .models import Ad, BidType, Sku, Spu, User
from .budget import BudgetTracker, CampaignBudget


def _emb(seed: str, dim: int = 16) -> tuple[float, ...]:
    raw = hashlib.md5(seed.encode()).digest()
    vals = [((raw[i % len(raw)] / 255.0) * 2 - 1) for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in vals)) or 1.0
    return tuple(v / norm for v in vals)


def sample_catalog() -> tuple[list[Spu], list[Sku]]:
    """示例目录：SPU（款）+ 多 SKU（规格）。

    搜推召回/去重按 spu_id；展示价/库存落到选中的 sku_id。
    """
    spu_rows = [
        ("spu_nike_zoom", "Nike 跑鞋 Air Zoom", "Nike", "运动", "跑鞋", 4.8, ("跑鞋", "nike", "air zoom")),
        ("spu_adidas_light", "Adidas 超轻跑鞋", "Adidas", "运动", "跑鞋", 4.7, ("跑鞋", "adidas")),
        ("spu_noname_shoe", "低质杂牌跑鞋", "NoName", "运动", "跑鞋", 3.2, ("跑鞋",)),
        ("spu_apple_case", "Apple iPhone 保护壳", "Apple", "数码", "手机配件", 4.6, ("iphone", "保护壳")),
        ("spu_hw_buds", "华为 FreeBuds", "HUAWEI", "数码", "耳机", 4.7, ("耳机", "freebuds")),
        ("spu_sbux", "星巴克咖啡券", "Starbucks", "餐饮", "咖啡", 4.5, ("咖啡", "星巴克")),
        ("spu_mask", "美妆面膜套装", "PerfectDiary", "美妆", "面膜", 4.4, ("面膜", "美妆")),
        ("spu_book", "儿童绘本礼盒", "Poplar", "图书", "童书", 4.9, ("绘本", "儿童")),
        ("spu_li_ning", "李宁云跑鞋", "李宁", "运动", "跑鞋", 4.6, ("跑鞋", "李宁")),
        ("spu_anta", "安踏氢跑鞋", "安踏", "运动", "跑鞋", 4.5, ("跑鞋", "安踏")),
        ("spu_xiaomi_buds", "小米 Buds 5", "Xiaomi", "数码", "耳机", 4.5, ("耳机", "小米")),
        ("spu_sony_wh", "Sony WH-1000XM5", "Sony", "数码", "耳机", 4.8, ("耳机", "降噪")),
        ("spu_kindle", "Kindle Paperwhite", "Amazon", "图书", "电子书", 4.7, ("电子书", "kindle")),
        ("spu_coffee_machine", "半自动咖啡机", "DeLonghi", "餐饮", "咖啡", 4.6, ("咖啡机", "咖啡")),
        ("spu_honey_acacia", "甄选槐花蜜", "甄选蜂场", "食品", "蜂蜜", 0.0, ("蜂蜜", "槐花蜜", "洋槐蜜")),
        ("spu_honey_jujube", "甄选枣花蜜", "甄选蜂场", "食品", "蜂蜜", 0.0, ("蜂蜜", "枣花蜜")),
        ("spu_honey_wildflower", "甄选百花蜜", "甄选蜂场", "食品", "蜂蜜", 0.0, ("蜂蜜", "百花蜜", "野花蜜")),
        ("spu_honey_linden", "甄选椴树蜜", "甄选蜂场", "食品", "蜂蜜", 0.0, ("蜂蜜", "椴树蜜")),
        ("spu_royal_jelly", "鲜蜂王浆", "甄选蜂场", "食品", "蜂产品", 0.0, ("蜂王浆", "蜂产品", "鲜蜂王浆")),
        ("spu_honey_gift", "蜂蜜组合礼盒", "甄选蜂场", "食品", "蜂蜜礼盒", 0.0, ("蜂蜜", "蜂蜜礼盒", "礼盒")),
    ]
    spus: list[Spu] = []
    for sid, title, brand, l1, l2, rating, kws in spu_rows:
        spus.append(
            Spu(
                spu_id=sid,
                title=title,
                brand=brand,
                cate_l1=l1,
                cate_l2=l2,
                rating=rating,
                keywords=kws,
                tags=(l1, l2, brand.lower()),
                embedding=_emb(f"{brand}-{l2}"),
            )
        )

    # sku 规格矩阵：鞋类多色多码；数码多色；券/图书少规格
    sku_specs: list[tuple[str, str, float, int, int, dict[str, str]]] = [
        ("sku_nike_blk_41", "spu_nike_zoom", 899.0, 800, 4200, {"颜色": "黑", "尺码": "41"}),
        ("sku_nike_blk_42", "spu_nike_zoom", 899.0, 1200, 5100, {"颜色": "黑", "尺码": "42"}),
        ("sku_nike_wht_42", "spu_nike_zoom", 929.0, 600, 2700, {"颜色": "白", "尺码": "42"}),
        ("sku_adi_blk_42", "spu_adidas_light", 699.0, 900, 5200, {"颜色": "黑", "尺码": "42"}),
        ("sku_adi_blu_43", "spu_adidas_light", 719.0, 500, 4600, {"颜色": "蓝", "尺码": "43"}),
        ("sku_nn_red_42", "spu_noname_shoe", 99.0, 200, 500, {"颜色": "红", "尺码": "42"}),
        ("sku_case_clear", "spu_apple_case", 199.0, 3000, 9000, {"款式": "透明"}),
        ("sku_case_silicone", "spu_apple_case", 249.0, 1500, 6000, {"款式": "硅胶"}),
        ("sku_hw_white", "spu_hw_buds", 799.0, 2000, 12000, {"颜色": "陶瓷白"}),
        ("sku_hw_black", "spu_hw_buds", 799.0, 1800, 10000, {"颜色": "亮黑色"}),
        ("sku_sbux_50", "spu_sbux", 50.0, 5000, 5000, {"面额": "50元"}),
        ("sku_sbux_100", "spu_sbux", 95.0, 3000, 3000, {"面额": "100元"}),
        ("sku_mask_5", "spu_mask", 129.0, 2000, 7000, {"规格": "5片装"}),
        ("sku_mask_10", "spu_mask", 199.0, 1200, 4000, {"规格": "10片装"}),
        ("sku_book_std", "spu_book", 168.0, 800, 6000, {"套装": "标准版"}),
        ("sku_li_blk_42", "spu_li_ning", 459.0, 1500, 8000, {"颜色": "黑", "尺码": "42"}),
        ("sku_li_wht_43", "spu_li_ning", 479.0, 900, 7000, {"颜色": "白", "尺码": "43"}),
        ("sku_anta_blk_42", "spu_anta", 399.0, 2000, 10000, {"颜色": "黑", "尺码": "42"}),
        ("sku_anta_org_41", "spu_anta", 419.0, 1100, 8000, {"颜色": "橙", "尺码": "41"}),
        ("sku_mi_white", "spu_xiaomi_buds", 399.0, 3000, 15000, {"颜色": "白色"}),
        ("sku_mi_blue", "spu_xiaomi_buds", 399.0, 2000, 10000, {"颜色": "蓝色"}),
        ("sku_sony_blk", "spu_sony_wh", 2299.0, 500, 6000, {"颜色": "黑"}),
        ("sku_sony_plt", "spu_sony_wh", 2399.0, 300, 3000, {"颜色": "铂金银"}),
        ("sku_kindle_8g", "spu_kindle", 998.0, 400, 4000, {"容量": "8G"}),
        ("sku_kindle_16g", "spu_kindle", 1188.0, 250, 3000, {"容量": "16G"}),
        ("sku_coffee_std", "spu_coffee_machine", 2499.0, 80, 1200, {"版本": "标准版"}),
        ("sku_coffee_pro", "spu_coffee_machine", 2999.0, 40, 800, {"版本": "Pro"}),
        ("sku_acacia_250", "spu_honey_acacia", 29.9, 100, 0, {"净含量": "250g", "包装": "玻璃瓶"}),
        ("sku_acacia_500", "spu_honey_acacia", 49.9, 100, 0, {"净含量": "500g", "包装": "玻璃瓶"}),
        ("sku_acacia_1000", "spu_honey_acacia", 89.9, 60, 0, {"净含量": "1kg", "包装": "家庭装"}),
        ("sku_jujube_500", "spu_honey_jujube", 55.9, 100, 0, {"净含量": "500g", "包装": "玻璃瓶"}),
        ("sku_jujube_twin", "spu_honey_jujube", 99.0, 50, 0, {"净含量": "500g×2", "包装": "组合装"}),
        ("sku_wildflower_500", "spu_honey_wildflower", 39.9, 120, 0, {"净含量": "500g", "包装": "挤压瓶"}),
        ("sku_wildflower_1000", "spu_honey_wildflower", 69.9, 80, 0, {"净含量": "1kg", "包装": "家庭装"}),
        ("sku_linden_500", "spu_honey_linden", 59.9, 80, 0, {"净含量": "500g", "包装": "玻璃瓶"}),
        ("sku_linden_twin", "spu_honey_linden", 109.0, 40, 0, {"净含量": "500g×2", "包装": "组合装"}),
        ("sku_royal_jelly_100", "spu_royal_jelly", 79.0, 60, 0, {"净含量": "100g", "储存": "冷藏"}),
        ("sku_royal_jelly_200", "spu_royal_jelly", 139.0, 40, 0, {"净含量": "200g", "储存": "冷藏"}),
        ("sku_honey_gift_2", "spu_honey_gift", 119.0, 50, 0, {"规格": "500g×2瓶", "包装": "礼盒"}),
        ("sku_honey_gift_4", "spu_honey_gift", 219.0, 30, 0, {"规格": "500g×4瓶", "包装": "礼盒"}),
    ]
    skus = [
        Sku(sku_id=kid, spu_id=sid, price=price, stock=stock, sales=sales, attrs=attrs)
        for kid, sid, price, stock, sales, attrs in sku_specs
    ]
    return spus, skus


def sample_products() -> list[Spu]:
    """兼容旧 API：返回 SPU 列表。"""
    spus, _ = sample_catalog()
    return spus


def sample_skus(spus: list[Spu] | None = None) -> list[Sku]:
    all_spus, skus = sample_catalog()
    if spus is None:
        return skus
    want = {s.spu_id for s in spus}
    return [k for k in skus if k.spu_id in want]


def skus_by_spu(skus: Iterable[Sku]) -> dict[str, list[Sku]]:
    out: dict[str, list[Sku]] = {}
    for sku in skus:
        out.setdefault(sku.spu_id, []).append(sku)
    return out


def pick_sku(
    skus: list[Sku],
    *,
    query: str = "",
    preferred_sku_id: str = "",
) -> Sku | None:
    """为 SPU 选一个展示/可购 SKU。

    优先级：指定 sku_id → query 命中规格属性 → 有货里销量最高 → 任意有货 → None。
    """
    if not skus:
        return None
    if preferred_sku_id:
        for s in skus:
            if s.sku_id == preferred_sku_id and s.stock > 0:
                return s
    in_stock = [s for s in skus if s.stock > 0]
    pool = in_stock or list(skus)
    q = (query or "").strip()
    if q:
        matched = [s for s in pool if any(v and v in q for v in s.attrs.values())]
        if matched:
            return max(matched, key=lambda s: s.sales)
    return max(pool, key=lambda s: (s.stock > 0, s.sales, -s.price))


def spu_sales(skus: list[Sku]) -> int:
    return sum(s.sales for s in skus)


def spu_stock(skus: list[Sku]) -> int:
    return sum(s.stock for s in skus)


def spu_min_price(skus: list[Sku]) -> float:
    priced = [s.price for s in skus if s.stock > 0] or [s.price for s in skus]
    return min(priced) if priced else 0.0


def sample_ads(spus: list[Spu] | None = None, skus: list[Sku] | None = None) -> list[Ad]:
    """广告创意绑 SPU；可选绑默认 SKU。"""
    if spus is None or skus is None:
        spus, skus = sample_catalog()
    by_spu = {s.spu_id: s for s in spus}
    by_group = skus_by_spu(skus)
    specs = [
        ("a1", "spu_nike_zoom", "sku_nike_blk_42", BidType.OCPC, 80.0, 1.1, 1.0, 0.02),
        ("a2", "spu_adidas_light", "sku_adi_blk_42", BidType.OCPC, 70.0, 1.0, 0.95, 0.03),
        ("a3", "spu_noname_shoe", "sku_nn_red_42", BidType.OCPC, 200.0, 0.4, 0.5, 0.25),
        ("a4", "spu_apple_case", "sku_case_clear", BidType.OCPM, 50.0, 1.05, 1.0, 0.01),
        ("a5", "spu_hw_buds", "sku_hw_white", BidType.OCPC, 60.0, 0.95, 0.9, 0.04),
        ("a6", "spu_sbux", "sku_sbux_50", BidType.CPC, 1.2, 1.0, 1.0, 0.02),
        ("a7", "spu_mask", "sku_mask_5", BidType.OCPC, 40.0, 0.9, 0.85, 0.05),
        ("a8", "spu_book", "sku_book_std", BidType.CPM, 25.0, 1.0, 1.0, 0.01),
        ("a9", "spu_honey_acacia", "sku_acacia_500", BidType.OCPC, 30.0, 1.0, 1.0, 0.0),
        ("a10", "spu_honey_wildflower", "sku_wildflower_500", BidType.OCPC, 25.0, 1.0, 1.0, 0.0),
        ("a11", "spu_honey_gift", "sku_honey_gift_2", BidType.CPC, 1.0, 1.0, 1.0, 0.0),
    ]
    ads: list[Ad] = []
    for ad_id, sid, kid, bt, bid, q, stab, neg in specs:
        p = by_spu[sid]
        sku = next((s for s in by_group.get(sid, []) if s.sku_id == kid), None)
        ads.append(
            Ad(
                ad_id=ad_id,
                campaign_id=f"c_{ad_id}",
                advertiser_id=f"adv_{p.brand}",
                title=p.title,
                brand=p.brand,
                cate_l1=p.cate_l1,
                cate_l2=p.cate_l2,
                spu_id=sid,
                sku_id=kid,
                keywords=p.keywords,
                bid_type=bt,
                bid=bid,
                quality_score=q,
                conv_stability=stab,
                neg_feedback=neg,
                hist_ctr=0.035 if p.cate_l2 == "跑鞋" else 0.02,
                hist_cvr=0.06 if bt in (BidType.OCPC, BidType.OCPM) else 0.04,
                price=sku.price if sku else 99.0,
                stock=sku.stock if sku else 0,
                embedding=p.embedding,
                tags=p.tags,
            )
        )
    return ads


def sample_budgets(ads: list[Ad] | None = None) -> BudgetTracker:
    ads = ads or sample_ads()
    tracker = BudgetTracker()
    for ad in ads:
        tracker.register(CampaignBudget(campaign_id=ad.campaign_id, daily_budget=500.0, spent_today=0.0))
    if tracker.get("c_a1"):
        tracker.get("c_a1").spent_today = 480.0
    if tracker.get("c_a8"):
        tracker.get("c_a8").spent_today = 500.0
    return tracker


def sample_user() -> User:
    return User(
        user_id="u_demo",
        gender="male",
        age_bucket="25-34",
        city_tier=1,
        interests=("食品", "蜂蜜", "蜂产品"),
        recent_cates=("蜂蜜", "蜂产品"),
        recent_queries=("蜂蜜", "槐花蜜"),
        freq_cap_today={"a8": 5},
    )
