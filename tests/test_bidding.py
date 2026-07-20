from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui.auction import _clearing_price, expected_cpa, gsp_auction
from soutui.models import Ad, BidType, RankedAd
from soutui.ranker import Ranker, quality_factor


def _ad(**kwargs) -> Ad:
    base = dict(
        ad_id="x",
        campaign_id="c",
        advertiser_id="a",
        title="t",
        brand="b",
        cate_l1="l1",
        cate_l2="l2",
        bid_type=BidType.OCPC,
        bid=100.0,
        quality_score=1.0,
        conv_stability=1.0,
        neg_feedback=0.0,
    )
    base.update(kwargs)
    return Ad(**base)


def _ranked(ad: Ad, pctr: float, pcvr: float, q: float) -> RankedAd:
    vip = pctr * pcvr * ad.bid if ad.bid_type in (BidType.OCPC, BidType.OCPM) else pctr * ad.bid
    if ad.bid_type in (BidType.CPM,):
        vip = ad.bid / 1000.0
    rank_ecpm = vip * q * 1000.0
    return RankedAd(
        ad=ad,
        recall_sources=["test"],
        pctr=pctr,
        pcvr=pcvr,
        q_factor=q,
        value_per_imp=vip,
        rank_ecpm=rank_ecpm,
        paced_rank_ecpm=rank_ecpm,
        pacing_factor=1.0,
        ecpm=rank_ecpm,
        charge=0.0,
        charge_unit="per_click" if ad.bid_type in (BidType.CPC, BidType.OCPC) else "per_mille",
        bid_price=0.0,
    )


def test_quality_factor_penalizes_neg_feedback():
    good = _ad(quality_score=1.0, conv_stability=1.0, neg_feedback=0.0)
    bad = _ad(quality_score=1.0, conv_stability=1.0, neg_feedback=0.3)
    assert quality_factor(good) > quality_factor(bad)


def test_rank_ecpm_includes_q():
    ad = _ad(bid_type=BidType.OCPC, bid=100.0)
    vip, with_q = Ranker.rank_value(ad, pctr=0.1, pcvr=0.2, q=0.5)
    assert math.isclose(vip, 0.1 * 0.2 * 100.0)
    assert math.isclose(with_q, vip * 0.5 * 1000.0)


def test_high_bid_low_q_can_lose_to_low_bid_high_q():
    high_bid_low_q = _ranked(_ad(ad_id="h", bid=200.0), pctr=0.05, pcvr=0.05, q=0.2)
    low_bid_high_q = _ranked(_ad(ad_id="l", bid=80.0), pctr=0.05, pcvr=0.05, q=1.0)
    assert low_bid_high_q.rank_ecpm > high_bid_low_q.rank_ecpm


def test_ocpc_charge_is_gsp_not_bid():
    w = _ranked(_ad(ad_id="w", bid_type=BidType.OCPC, bid=100.0), pctr=0.1, pcvr=0.2, q=1.0)
    n = _ranked(_ad(ad_id="n", bid_type=BidType.OCPC, bid=80.0), pctr=0.08, pcvr=0.15, q=1.0)
    charge = _clearing_price(w, n)
    # charge = next.rank_ecpm / (pCTR*pCVR*Q*1000)
    expected = n.rank_ecpm / (0.1 * 0.2 * 1.0 * 1000.0)
    assert math.isclose(charge, expected + 1e-4, rel_tol=1e-6)
    assert charge != w.ad.bid


def test_ocpc_expected_cpa_near_next_value_ratio():
    winners = gsp_auction(
        [
            _ranked(_ad(ad_id="w", bid=100.0), 0.1, 0.2, 1.0),
            _ranked(_ad(ad_id="n", bid=80.0), 0.08, 0.15, 1.0),
        ],
        slot_count=1,
    )
    w = winners[0]
    # E[CPA] = charge / pCVR，不等于 bid，但有限
    e_cpa = expected_cpa(w)
    assert e_cpa > 0
    assert e_cpa != w.ad.bid


def test_ocpm_charges_per_mille():
    w = _ranked(_ad(ad_id="w", bid_type=BidType.OCPM, bid=50.0), 0.1, 0.1, 1.0)
    n = _ranked(_ad(ad_id="n", bid_type=BidType.OCPM, bid=40.0), 0.08, 0.1, 1.0)
    w.charge_unit = "per_mille"
    charge = _clearing_price(w, n)
    assert math.isclose(charge, n.rank_ecpm / 1.0 + 1e-4, rel_tol=1e-6)
