from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .features import clip
from .models import Ad, BidType

if TYPE_CHECKING:
    from .models import RankedAd


@dataclass
class CampaignBidState:
    """计划级 oCPX 投放反馈，用于自动调价。"""

    campaign_id: str
    target_cpa: float  # 广告主设置的目标转化出价（= ad.bid）
    spend: float = 0.0
    clicks: float = 0.0
    conversions: float = 0.0
    bid_multiplier: float = 1.0

    @property
    def actual_cpa(self) -> float | None:
        if self.conversions <= 0:
            return None
        return self.spend / self.conversions


@dataclass
class BidController:
    """oCPX 自动调价：让长期实际 CPA 趋近目标 bid。

    原理（比例控制 + 平滑）：
    - 统计计划级 spend / conversions → actual_cpa
    - bid_multiplier ≈ clip(target_cpa / actual_cpa)
    - 排序用 effective_bid = target_bid × bid_multiplier

    注意：
    - 这是平台侧调控「排序出价」，不改广告主后台填的目标 bid
    - 扣费仍走 GSP；multiplier 影响拿量，间接拉平成本
    - 转化样本不足时不调价（冷启动）
    """

    campaigns: dict[str, CampaignBidState] = field(default_factory=dict)
    min_multiplier: float = 0.5
    max_multiplier: float = 2.0
    min_conversions_to_adjust: float = 1.0
    smoothing: float = 0.3

    def register(self, campaign_id: str, target_cpa: float) -> None:
        if campaign_id not in self.campaigns:
            self.campaigns[campaign_id] = CampaignBidState(
                campaign_id=campaign_id,
                target_cpa=target_cpa,
            )
        else:
            self.campaigns[campaign_id].target_cpa = target_cpa

    def register_ad(self, ad: Ad) -> None:
        if ad.bid_type in (BidType.OCPC, BidType.OCPM):
            self.register(ad.campaign_id, ad.bid)

    def register_ads(self, ads: list[Ad]) -> None:
        for ad in ads:
            self.register_ad(ad)

    def get(self, campaign_id: str) -> CampaignBidState | None:
        return self.campaigns.get(campaign_id)

    def effective_bid(self, ad: Ad) -> tuple[float, float]:
        """返回 (effective_bid, bid_multiplier)。非 oCPX 不调价。"""
        if ad.bid_type not in (BidType.OCPC, BidType.OCPM):
            return ad.bid, 1.0
        state = self.campaigns.get(ad.campaign_id)
        if state is None:
            return ad.bid, 1.0
        return ad.bid * state.bid_multiplier, state.bid_multiplier

    def recompute_multiplier(self, campaign_id: str) -> float:
        state = self.campaigns.get(campaign_id)
        if state is None:
            return 1.0
        if state.conversions < self.min_conversions_to_adjust:
            return state.bid_multiplier

        actual = state.actual_cpa
        if actual is None or actual <= 0:
            return state.bid_multiplier

        # 实际 CPA 偏高 → 降出价；偏低 → 提出价
        raw = clip(state.target_cpa / actual, self.min_multiplier, self.max_multiplier)
        state.bid_multiplier = (1 - self.smoothing) * state.bid_multiplier + self.smoothing * raw
        state.bid_multiplier = clip(state.bid_multiplier, self.min_multiplier, self.max_multiplier)
        return state.bid_multiplier

    def record_impression_feedback(
        self,
        campaign_id: str,
        *,
        spend: float,
        clicks: float = 0.0,
        conversions: float = 0.0,
    ) -> None:
        state = self.campaigns.get(campaign_id)
        if state is None:
            return
        state.spend += max(spend, 0.0)
        state.clicks += max(clicks, 0.0)
        state.conversions += max(conversions, 0.0)
        self.recompute_multiplier(campaign_id)

    def record_winners(self, winners: list[RankedAd], *, use_expected: bool = True) -> None:
        """根据胜出广告累计反馈并调价。

        use_expected=True：用 pCTR/pCVR 期望值（demo/单测稳定）
        """
        for w in winners:
            if w.ad.bid_type not in (BidType.OCPC, BidType.OCPM):
                continue
            if self.campaigns.get(w.ad.campaign_id) is None:
                continue

            if w.charge_unit == "per_click":
                imp_cost = w.pctr * w.charge
                clicks = w.pctr
                convs = w.pctr * w.pcvr
            else:
                imp_cost = w.charge / 1000.0
                clicks = w.pctr
                convs = w.pctr * w.pcvr

            if not use_expected:
                # 预留：真实采样路径
                pass

            self.record_impression_feedback(
                w.ad.campaign_id,
                spend=imp_cost,
                clicks=clicks,
                conversions=convs,
            )
