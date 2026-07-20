from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui.bid_controller import BidController
from soutui.models import Ad, BidType, RankedAd


def _ad(**kwargs) -> Ad:
    base = dict(
        ad_id="a1",
        campaign_id="c1",
        advertiser_id="adv",
        title="t",
        brand="b",
        cate_l1="l1",
        cate_l2="l2",
        bid_type=BidType.OCPC,
        bid=100.0,
    )
    base.update(kwargs)
    return Ad(**base)


def _winner(ad: Ad, pctr: float, pcvr: float, charge: float) -> RankedAd:
    return RankedAd(
        ad=ad,
        recall_sources=["test"],
        pctr=pctr,
        pcvr=pcvr,
        q_factor=1.0,
        value_per_imp=1.0,
        rank_ecpm=1000.0,
        ecpm=1000.0,
        charge=charge,
        charge_unit="per_click",
        bid_price=charge,
        paced_rank_ecpm=1000.0,
        pacing_factor=1.0,
        effective_bid=ad.bid,
        bid_multiplier=1.0,
    )


def test_non_ocpx_not_adjusted():
    ctrl = BidController()
    ad = _ad(bid_type=BidType.CPC, bid=2.0)
    ctrl.register_ad(ad)
    eff, mult = ctrl.effective_bid(ad)
    assert eff == 2.0 and mult == 1.0


def test_high_actual_cpa_lowers_multiplier():
    ctrl = BidController(smoothing=1.0, min_conversions_to_adjust=0.5)
    ad = _ad(bid=100.0)
    ctrl.register_ad(ad)
    # spend=100, conversions=0.5 → actual CPA=200 > target 100 → multiplier < 1
    ctrl.record_impression_feedback("c1", spend=100.0, clicks=2.0, conversions=0.5)
    assert ctrl.get("c1").bid_multiplier < 1.0
    eff, mult = ctrl.effective_bid(ad)
    assert eff < ad.bid
    assert mult == ctrl.get("c1").bid_multiplier


def test_low_actual_cpa_raises_multiplier():
    ctrl = BidController(smoothing=1.0, min_conversions_to_adjust=0.5)
    ad = _ad(bid=100.0)
    ctrl.register_ad(ad)
    # spend=20, conversions=0.5 → actual CPA=40 < target 100 → multiplier > 1
    ctrl.record_impression_feedback("c1", spend=20.0, clicks=1.0, conversions=0.5)
    assert ctrl.get("c1").bid_multiplier > 1.0


def test_cold_start_no_adjust():
    ctrl = BidController(min_conversions_to_adjust=5.0)
    ad = _ad(bid=100.0)
    ctrl.register_ad(ad)
    ctrl.record_impression_feedback("c1", spend=100.0, clicks=1.0, conversions=0.5)
    assert ctrl.get("c1").bid_multiplier == 1.0


def test_record_winners_drives_tuning():
    ctrl = BidController(smoothing=1.0, min_conversions_to_adjust=0.1)
    ad = _ad(bid=50.0)
    ctrl.register_ad(ad)
    # pCTR=0.5, charge=20 → spend=10; conv=0.5*0.1=0.05 → CPA=200 >> 50
    # Need more conversions for adjust; accumulate
    for _ in range(30):
        ctrl.record_winners([_winner(ad, pctr=0.5, pcvr=0.1, charge=20.0)])
    assert ctrl.get("c1").conversions >= 0.1
    assert ctrl.get("c1").bid_multiplier < 1.0


def test_effective_bid_used_in_rank_value():
    from soutui.ranker import Ranker

    ad = _ad(bid_type=BidType.OCPC, bid=100.0)
    _, raw = Ranker.rank_value(ad, 0.1, 0.2, 1.0, bid=100.0)
    _, tuned = Ranker.rank_value(ad, 0.1, 0.2, 1.0, bid=50.0)
    assert tuned < raw
    assert abs(raw / tuned - 2.0) < 1e-6
