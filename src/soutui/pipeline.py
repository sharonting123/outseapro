from __future__ import annotations

from collections import Counter

from .auction import diversify, expected_cpa, gsp_auction
from .bid_controller import BidController
from .budget import BudgetTracker
from .features import InvertedIndex
from .models import Ad, QueryContext, RankedAd, User
from .ranker import Ranker
from .ranker import CtrCvrModel
from .recall import RecallEngine
from .trace import emit


class AdsEngine:
    """搜推广告全链路：召回 → 精排 → oCPX调价 → 预算/pacing → 多样性 → GSP。"""

    def __init__(
        self,
        ads: list[Ad],
        budget_tracker: BudgetTracker | None = None,
        bid_controller: BidController | None = None,
        model: CtrCvrModel | None = None,
    ) -> None:
        self.ads = {a.ad_id: a for a in ads}
        self.index = InvertedIndex()
        for ad in ads:
            self.index.add(ad)
        self.recall_engine = RecallEngine(ads, index=self.index)
        self.ranker = Ranker(model=model)
        self.budget = budget_tracker or BudgetTracker()
        self.bid_controller = bid_controller or BidController()
        self.bid_controller.register_ads(ads)

    def recommend(self, user: User, ctx: QueryContext, *, record_spend: bool = True) -> list[RankedAd]:
        emit(
            "ads",
            "【广告】进入广告引擎",
            formula="召回 → 精排(pCTR/pCVR) → oCPX调价 → pacing → 多样性 → GSP",
            detail={"candidate_ads": len(self.ads), "slot_count": ctx.slot_count},
        )

        hits = self.recall_engine.recall(user, ctx)
        src_cnt = Counter(s for sources in hits.values() for s in sources)
        emit(
            "recall",
            f"【广告召回】多路召回得到 {len(hits)} 条候选",
            formula="BM25倒排 ∪ query扩展 ∪ 兴趣类目 ∪ 向量相似 ∪ 热度兜底 − 频控",
            detail={
                "hit_count": len(hits),
                "by_source": dict(src_cnt),
                "top_ids": list(hits.keys())[:8],
            },
        )

        scored: list[RankedAd] = []
        minute = int(ctx.extra.get("minute", 0))
        skipped_budget = 0

        for ad_id, sources in hits.items():
            ad = self.ads[ad_id]
            if not self.budget.is_eligible(ad.campaign_id, ctx.hour, minute):
                skipped_budget += 1
                continue

            eff_bid, mult = self.bid_controller.effective_bid(ad)
            item = self.ranker.score(
                user,
                ctx,
                ad,
                sources,
                effective_bid=eff_bid,
                bid_multiplier=mult,
            )
            pacing = self.budget.pacing_factor(ad.campaign_id, ctx.hour, minute)
            item.pacing_factor = pacing
            item.score_detail["raw_rank_ecpm"] = item.rank_ecpm
            item.paced_rank_ecpm = item.rank_ecpm * pacing
            item.score_detail["pacing_factor"] = pacing
            item.score_detail["paced_rank_ecpm"] = item.paced_rank_ecpm
            b = self.budget.get(ad.campaign_id)
            if b is not None:
                item.score_detail["budget_remaining"] = b.remaining
            scored.append(item)

        scored.sort(key=lambda x: x.paced_rank_ecpm, reverse=True)
        top = scored[:5]
        emit(
            "rank",
            f"【精排+调价+pacing】打分 {len(scored)} 条（预算过滤掉 {skipped_budget}）",
            formula="rank_eCPM = pCTR × pCVR × bid × Q × 1000；paced = rank_eCPM × pacing",
            detail={
                "scored": len(scored),
                "skipped_budget": skipped_budget,
                "top": [
                    {
                        "ad_id": x.ad.ad_id,
                        "title": x.ad.title[:24],
                        "pctr": round(x.pctr, 4),
                        "pcvr": round(x.pcvr, 4),
                        "bid_mult": round(x.bid_multiplier, 3),
                        "pacing": round(x.pacing_factor, 3),
                        "paced_ecpm": round(x.paced_rank_ecpm, 2),
                    }
                    for x in top
                ],
            },
        )

        diversified = diversify(scored, slot_count=ctx.slot_count)
        emit(
            "diversity",
            f"【多样性】{len(scored)} → {len(diversified)}（限同品牌/类目扎堆）",
            formula="max_per_brand=2, max_per_cate=3",
            detail={"before": len(scored), "after": len(diversified)},
        )

        pool = sorted(diversified, key=lambda x: x.paced_rank_ecpm, reverse=True)
        for item in pool:
            item.rank_ecpm = item.paced_rank_ecpm
            item.ecpm = item.paced_rank_ecpm
        winners = gsp_auction(pool, slot_count=ctx.slot_count)

        for w in winners:
            w.score_detail["expected_cpa"] = expected_cpa(w)

        emit(
            "auction",
            f"【GSP 竞价】胜出 {len(winners)} 条广告位",
            formula="排序看 paced_eCPM；扣费≈ next_eCPM / (pCTR×pCVR×Q) （二价，≠出价）",
            detail={
                "winners": [
                    {
                        "rank": w.rank,
                        "ad_id": w.ad.ad_id,
                        "title": w.ad.title[:24],
                        "ecpm": round(w.rank_ecpm, 2),
                        "charge": round(w.charge, 4),
                        "unit": w.charge_unit,
                    }
                    for w in winners
                ]
            },
        )

        if record_spend:
            self.budget.record_winners(winners)
            self.bid_controller.record_winners(winners)
        return winners
