from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui.budget import BudgetTracker, CampaignBudget


def test_exhausted_budget_returns_zero_pacing():
    tracker = BudgetTracker()
    tracker.register(CampaignBudget(campaign_id="c1", daily_budget=100.0, spent_today=100.0))
    assert tracker.pacing_factor("c1", hour=12) == 0.0
    assert not tracker.is_eligible("c1", hour=12)


def test_overspend_throttles_pacing():
    tracker = BudgetTracker()
    # 中午 12 点，应花 50%，实际已花 90%
    tracker.register(CampaignBudget(campaign_id="c1", daily_budget=1000.0, spent_today=900.0))
    factor = tracker.pacing_factor("c1", hour=12)
    assert 0.05 < factor < 1.0


def test_underspend_boosts_pacing():
    tracker = BudgetTracker()
    # 中午 12 点，应花 50%，实际只花 5%
    tracker.register(CampaignBudget(campaign_id="c1", daily_budget=1000.0, spent_today=50.0))
    factor = tracker.pacing_factor("c1", hour=12)
    assert 1.0 < factor <= 1.5


def test_unlimited_budget_no_pacing():
    tracker = BudgetTracker()
    tracker.register(CampaignBudget(campaign_id="c1", daily_budget=0.0, spent_today=999.0))
    assert tracker.pacing_factor("c1", hour=12) == 1.0


def test_record_winners_updates_spent():
    from soutui.models import Ad, BidType, RankedAd

    tracker = BudgetTracker()
    tracker.register(CampaignBudget(campaign_id="c1", daily_budget=500.0, spent_today=0.0))
    ad = Ad(
        ad_id="a1",
        campaign_id="c1",
        advertiser_id="adv",
        title="t",
        brand="b",
        cate_l1="l1",
        cate_l2="l2",
        bid_type=BidType.OCPC,
    )
    item = RankedAd(
        ad=ad,
        recall_sources=[],
        pctr=0.1,
        pcvr=0.2,
        q_factor=1.0,
        value_per_imp=1.0,
        rank_ecpm=1000.0,
        paced_rank_ecpm=1000.0,
        pacing_factor=1.0,
        ecpm=1000.0,
        charge=5.0,
        charge_unit="per_click",
        bid_price=5.0,
    )
    tracker.record_winners([item])
    assert tracker.get("c1").spent_today == 0.5  # 0.1 * 5.0
