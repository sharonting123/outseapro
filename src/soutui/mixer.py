from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .catalog import pick_sku, skus_by_spu
from .features import jaccard, tokenize
from .models import QueryContext, RankedAd, Scene, Sku, Spu
from .organic import RankedProduct
from .trace import emit


class ItemType(str, Enum):
    ORGANIC = "organic"
    AD = "ad"


@dataclass
class FeedItem:
    """混排后的结果卡片：一款 SPU + 一个落地 SKU。"""

    item_type: ItemType
    position: int
    spu: Spu
    sku: Sku
    score: float
    is_ad: bool
    ad_id: str = ""
    campaign_id: str = ""
    charge: float = 0.0
    charge_unit: str = ""
    rank_ecpm: float = 0.0
    disclosure: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def product(self) -> Spu:
        """兼容旧字段。"""
        return self.spu


@dataclass
class MixerPolicy:
    """混排策略。"""

    page_size: int = 10
    ad_slots_search: tuple[int, ...] = (1, 4, 7)
    ad_slots_feed: tuple[int, ...] = (2, 5, 8)
    min_query_relevance: float = 0.15
    dedupe_spu: bool = True  # 同一 SPU 自然与广告只出一次


class Mixer:
    """自然结果 + 广告 → 最终 Feed（按 spu_id 去重，卡片带 sku）。"""

    def __init__(self, policy: MixerPolicy | None = None) -> None:
        self.policy = policy or MixerPolicy()

    def merge(
        self,
        organic: list[RankedProduct],
        ads: list[RankedAd],
        ctx: QueryContext,
        spus_by_id: dict[str, Spu],
        skus: list[Sku] | None = None,
    ) -> list[FeedItem]:
        policy = self.policy
        page_size = min(policy.page_size, ctx.slot_count) if ctx.slot_count else policy.page_size
        slots = policy.ad_slots_search if ctx.scene == Scene.SEARCH else policy.ad_slots_feed
        sku_map = skus_by_spu(skus or [])

        emit(
            "mix",
            "【混排】合并自然/广告（SPU 去重，SKU 落地）",
            formula="广告占位 → 同 spu_id 去重 → 填自然；卡片 = SPU + pick(SKU)",
            detail={
                "organic": len(organic),
                "ads_in": len(ads),
                "ad_slots": list(slots),
                "page_size": page_size,
            },
        )

        eligible_ads: list[RankedAd] = []
        filtered_rel = 0
        for ad_item in ads:
            sid = ad_item.ad.spu_id
            if not sid or sid not in spus_by_id:
                continue
            if ctx.scene == Scene.SEARCH and ctx.query.strip():
                rel = self._ad_query_relevance(ad_item, ctx.query)
                if rel < policy.min_query_relevance:
                    filtered_rel += 1
                    continue
                ad_item.score_detail["mixer_query_rel"] = rel
            eligible_ads.append(ad_item)

        if filtered_rel:
            emit(
                "mix",
                f"【混排】相关性过滤掉 {filtered_rel} 条广告",
                formula=f"mixer_query_rel ≥ {policy.min_query_relevance}",
                detail={"eligible_ads": len(eligible_ads), "filtered": filtered_rel},
            )

        used_spus: set[str] = set()
        feed: list[FeedItem | None] = [None] * page_size
        ad_iter = iter(eligible_ads)

        for pos in slots:
            if pos >= page_size:
                continue
            ad_item = next(ad_iter, None)
            while ad_item is not None:
                sid = ad_item.ad.spu_id
                if policy.dedupe_spu and sid in used_spus:
                    ad_item = next(ad_iter, None)
                    continue
                spu = spus_by_id[sid]
                sku = pick_sku(
                    sku_map.get(sid, []),
                    query=ctx.query,
                    preferred_sku_id=ad_item.ad.sku_id,
                )
                if sku is None:
                    ad_item = next(ad_iter, None)
                    continue
                used_spus.add(sid)
                feed[pos] = FeedItem(
                    item_type=ItemType.AD,
                    position=pos,
                    spu=spu,
                    sku=sku,
                    score=ad_item.paced_rank_ecpm or ad_item.rank_ecpm,
                    is_ad=True,
                    ad_id=ad_item.ad.ad_id,
                    campaign_id=ad_item.ad.campaign_id,
                    charge=ad_item.charge,
                    charge_unit=ad_item.charge_unit,
                    rank_ecpm=ad_item.rank_ecpm,
                    disclosure="广告",
                    extra={
                        "pctr": ad_item.pctr,
                        "pcvr": ad_item.pcvr,
                        "bid_multiplier": ad_item.bid_multiplier,
                        "pacing_factor": ad_item.pacing_factor,
                        "recall": ad_item.recall_sources,
                        "spu_id": sid,
                        "sku_id": sku.sku_id,
                        "attrs": sku.attrs,
                        **ad_item.score_detail,
                    },
                )
                break

        org_iter = iter(organic)
        for pos in range(page_size):
            if feed[pos] is not None:
                continue
            while True:
                item = next(org_iter, None)
                if item is None:
                    break
                sid = item.spu.spu_id
                if policy.dedupe_spu and sid in used_spus:
                    continue
                used_spus.add(sid)
                feed[pos] = FeedItem(
                    item_type=ItemType.ORGANIC,
                    position=pos,
                    spu=item.spu,
                    sku=item.sku,
                    score=item.score,
                    is_ad=False,
                    extra={
                        "recall": item.recall_sources,
                        "spu_id": sid,
                        "sku_id": item.sku.sku_id,
                        "attrs": item.sku.attrs,
                        **item.score_detail,
                    },
                )
                break

        result = [x for x in feed if x is not None]
        emit(
            "mix",
            f"【混排完成】{len(result)} 张卡片（广告 {sum(1 for x in result if x.is_ad)}）",
            formula="去重键 = spu_id；展示价/库存 = sku",
            detail={
                "positions": [
                    {
                        "pos": x.position,
                        "type": "广告" if x.is_ad else "自然",
                        "spu_id": x.spu.spu_id,
                        "sku_id": x.sku.sku_id,
                        "title": x.spu.title[:14],
                        "attrs": x.sku.attr_text(),
                    }
                    for x in result
                ]
            },
        )
        return result

    @staticmethod
    def _ad_query_relevance(ad_item: RankedAd, query: str) -> float:
        ad = ad_item.ad
        q = tokenize(query)
        bag = tokenize(" ".join([ad.title, ad.brand, *ad.keywords, ad.cate_l1, ad.cate_l2]))
        rel = jaccard(q, bag)
        if ad.brand and ad.brand.lower() in query.lower():
            rel += 0.3
        return rel
