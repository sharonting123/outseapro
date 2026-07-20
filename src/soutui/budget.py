from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import dataclass, field

if TYPE_CHECKING:
    from .models import RankedAd


@dataclass
class CampaignBudget:
    """计划级日预算与当日已消耗（单位：元）。"""

    campaign_id: str
    daily_budget: float = 0.0  # 0 表示不限预算
    spent_today: float = 0.0

    @property
    def remaining(self) -> float:
        if self.daily_budget <= 0:
            return float("inf")
        return max(self.daily_budget - self.spent_today, 0.0)

    @property
    def exhausted(self) -> bool:
        return self.daily_budget > 0 and self.spent_today >= self.daily_budget


@dataclass
class BudgetTracker:
    """预算状态 + 匀速 pacing 系数。

    匀速 pacing 思路：
    - 按当天已过去时间比例，计算「应花到多少」expected_spend
    - 花太快 → pacing < 1，压低 rank_eCPM，少拿量
    - 花太慢 → pacing > 1，抬高 rank_eCPM，多拿量
    - 预算耗尽 → 直接过滤，不再参与竞价
    """

    campaigns: dict[str, CampaignBudget] = field(default_factory=dict)
    min_pacing: float = 0.05
    max_pacing: float = 1.5
    overspend_tolerance: float = 1.05
    underspend_tolerance: float = 0.95

    def register(self, budget: CampaignBudget) -> None:
        self.campaigns[budget.campaign_id] = budget

    def get(self, campaign_id: str) -> CampaignBudget | None:
        return self.campaigns.get(campaign_id)

    def day_progress(self, hour: int, minute: int = 0) -> float:
        elapsed_minutes = hour * 60 + minute
        return max(elapsed_minutes / 1440.0, 1.0 / 1440.0)

    def expected_spend(self, campaign_id: str, hour: int, minute: int = 0) -> float:
        b = self.campaigns.get(campaign_id)
        if b is None or b.daily_budget <= 0:
            return float("inf")
        return b.daily_budget * self.day_progress(hour, minute)

    def pacing_factor(self, campaign_id: str, hour: int, minute: int = 0) -> float:
        b = self.campaigns.get(campaign_id)
        if b is None or b.daily_budget <= 0:
            return 1.0
        if b.exhausted:
            return 0.0

        expected = self.expected_spend(campaign_id, hour, minute)
        spent = max(b.spent_today, 0.0)

        if spent <= 0:
            return min(self.max_pacing, 1.2)

        ratio = expected / spent
        if spent > expected * self.overspend_tolerance:
            return max(self.min_pacing, min(1.0, ratio))
        if spent < expected * self.underspend_tolerance:
            return max(1.0, min(self.max_pacing, ratio))
        return 1.0

    def is_eligible(self, campaign_id: str, hour: int, minute: int = 0) -> bool:
        return self.pacing_factor(campaign_id, hour, minute) > 0

    def estimate_impression_cost(self, item: RankedAd) -> float:
        if item.charge_unit == "per_click":
            return item.pctr * item.charge
        return item.charge / 1000.0

    def record_delivery(self, campaign_id: str, estimated_cost: float) -> None:
        b = self.campaigns.get(campaign_id)
        if b is None:
            return
        b.spent_today += max(estimated_cost, 0.0)

    def record_winners(self, winners: list[RankedAd]) -> None:
        for w in winners:
            self.record_delivery(w.ad.campaign_id, self.estimate_impression_cost(w))
