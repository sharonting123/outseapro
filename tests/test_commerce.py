from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui.catalog import sample_user
from soutui.commerce import CommerceEngine
from soutui.mixer import ItemType
from soutui.models import QueryContext, Scene


def test_search_returns_organic_and_ads():
    engine = CommerceEngine()
    items, _ = engine.search(sample_user(), "跑鞋", page_size=10)
    assert items
    assert ItemType.ORGANIC in {x.item_type for x in items}
    assert any(x.is_ad for x in items)
    for x in items:
        assert x.spu.spu_id
        assert x.sku.sku_id
        assert x.sku.spu_id == x.spu.spu_id
        if x.is_ad:
            assert x.disclosure == "广告"
            assert x.ad_id


def test_search_dedupes_spu_between_organic_and_ad():
    engine = CommerceEngine()
    items, _ = engine.search(sample_user(), "跑鞋", page_size=10)
    spu_ids = [x.spu.spu_id for x in items]
    assert len(spu_ids) == len(set(spu_ids))


def test_feed_has_cards():
    engine = CommerceEngine()
    items, _ = engine.feed(sample_user(), page_size=10)
    assert len(items) >= 5
    assert any(not x.is_ad for x in items)
    assert all(x.sku.sku_id for x in items)


def test_trace_emits_pipeline_steps():
    engine = CommerceEngine()
    items, trace = engine.search(sample_user(), "跑鞋", page_size=8, step_delay=0)
    assert items
    assert trace is not None
    stages = {e.stage for e in trace.events}
    assert "organic" in stages
    assert "recall" in stages or "ads" in stages
    assert "auction" in stages
    assert "mix" in stages
    assert any(e.formula for e in trace.events)


def test_organic_only_products_in_search_pool():
    """李宁/安踏无广告，应出现在自然召回池。"""
    engine = CommerceEngine()
    ctx = QueryContext(scene=Scene.SEARCH, query="跑鞋", slot_count=10)
    organic = engine.organic.search(sample_user(), ctx, top_k=20)
    ids = {r.spu.spu_id for r in organic}
    assert "spu_li_ning" in ids or "spu_anta" in ids


def test_search_coffee_not_dominated_by_shoes():
    """搜咖啡时，兴趣里的跑鞋不能压过咖啡商品。"""
    engine = CommerceEngine()
    items, _ = engine.search(sample_user(), "咖啡", page_size=10)
    titles = [x.spu.title for x in items]
    assert any("咖啡" in t for t in titles)
    organic = [x for x in items if not x.is_ad]
    assert organic
    for x in organic[:3]:
        bag = f"{x.spu.title}{x.spu.cate_l2}{' '.join(x.spu.keywords)}"
        assert "咖啡" in bag
    assert all("跑鞋" not in x.spu.title for x in organic)


def test_spu_sku_pick_by_query_attr():
    """搜「白」时 Nike 应落到白色 SKU。"""
    engine = CommerceEngine()
    ctx = QueryContext(scene=Scene.SEARCH, query="nike 白", slot_count=10)
    organic = engine.organic.search(sample_user(), ctx, top_k=10)
    nike = next((r for r in organic if r.spu.spu_id == "spu_nike_zoom"), None)
    assert nike is not None
    assert nike.sku.attrs.get("颜色") == "白"
