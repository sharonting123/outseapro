from __future__ import annotations

from .models import BidType, RankedAd

# 二价清算最小浮动，避免并列时零价
_EPSILON = 1e-4


def diversify(
    ranked: list[RankedAd],
    slot_count: int,
    max_per_brand: int = 2,
    max_per_cate: int = 3,
) -> list[RankedAd]:
    """滑动窗口多样性：限制同品牌/同二级类目扎堆。"""
    picked: list[RankedAd] = []
    brand_cnt: dict[str, int] = {}
    cate_cnt: dict[str, int] = {}
    for item in ranked:
        b = item.ad.brand
        c = item.ad.cate_l2
        if brand_cnt.get(b, 0) >= max_per_brand:
            continue
        if cate_cnt.get(c, 0) >= max_per_cate:
            continue
        picked.append(item)
        brand_cnt[b] = brand_cnt.get(b, 0) + 1
        cate_cnt[c] = cate_cnt.get(c, 0) + 1
        if len(picked) >= slot_count * 2:
            break
    return picked


def gsp_auction(candidates: list[RankedAd], slot_count: int) -> list[RankedAd]:
    """按 rank_ecpm 排序占坑，再做二价清算写入 charge。

    排序与扣费必须拆开：
    - 排序：看 rank_ecpm = pCTR×pCVR×bid×Q×1000（OCPC/OCPM）
    - 扣费：点击后 / 曝光后按 GSP 清算，不等于广告主设的 bid
    """
    ordered = sorted(candidates, key=lambda x: x.rank_ecpm, reverse=True)
    winners = ordered[:slot_count]
    # 需要下一名做二价；若刚好卡在 slot 边界，用 slot 外下一名
    pool = ordered

    for i, item in enumerate(winners):
        nxt = pool[i + 1] if i + 1 < len(pool) else None
        item.charge = _clearing_price(item, nxt)
        item.bid_price = item.charge
        item.rank = i + 1
        item.score_detail["charge"] = item.charge
        item.score_detail["charge_unit"] = 1.0 if item.charge_unit == "per_click" else 0.0
        if nxt is not None:
            item.score_detail["next_rank_ecpm"] = nxt.rank_ecpm
    return winners


def _clearing_price(winner: RankedAd, nxt: RankedAd | None) -> float:
    """二价扣费。

    OCPC（主流 OCPX / 按点击计费优化转化）
    ------------------------------------
    排序分 density = pCTR × pCVR × Q
    点击扣费 charge_cpc = next.rank_score / density_own + ε
                      = next.rank_ecpm / (pCTR×pCVR×Q×1000) + ε

    口语里常说「下一名预估转化价值 ÷ 自己的 pCVR」，那是不完整的 shorthand；
    完整 GSP 分母是赢家自己的排序密度 pCTR×pCVR×Q。

    平台再通过冷启动探索、成本调控等让长期平均转化成本趋近目标 bid，
    单次点击/单次转化成本会上下浮动——本函数只实现拍卖清算核。

    OCPM（按千次曝光计费优化转化）
    --------------------------------
    charge_cpm = next.rank_ecpm / Q_own + ε
    （千次扣费；同样不等于目标转化 bid）

    CPC / CPM 手动出价
    ------------------
    CPC: next.rank_ecpm / (pCTR×Q×1000) + ε
    CPM: next.rank_ecpm / Q + ε
    """
    bt = winner.ad.bid_type
    pctr = max(winner.pctr, 1e-8)
    pcvr = max(winner.pcvr, 1e-8)
    q = max(winner.q_factor, 1e-8)

    if nxt is None:
        # 末位：保留价示意（半价地板），真实系统有 reserve price / 成本调控
        if bt in (BidType.OCPC, BidType.CPC):
            return max(winner.ad.bid * 0.15, _EPSILON)
        return max(winner.rank_ecpm * 0.5, _EPSILON)

    next_score = nxt.rank_ecpm  # 已含 next 的 Q

    if bt == BidType.OCPC:
        # 点击扣费；期望：charge ≈ 使 avg CPA 趋近 bid，但单次不等于 bid
        density = pctr * pcvr * q
        charge = next_score / (density * 1000.0) + _EPSILON
        # 软上限：避免异常高价；真实系统由出价上限/成本保护截断
        return min(charge, winner.ad.bid * 3.0)

    if bt == BidType.OCPM:
        charge = next_score / q + _EPSILON
        return min(charge, winner.ad.bid * pctr * pcvr * 1000.0 * 3.0)

    if bt == BidType.CPC:
        density = pctr * q
        charge = next_score / (density * 1000.0) + _EPSILON
        return min(charge, winner.ad.bid)

    if bt == BidType.CPM:
        charge = next_score / q + _EPSILON
        return min(charge, winner.ad.bid)

    raise ValueError(f"unsupported bid_type: {bt}")


def expected_cpa(item: RankedAd) -> float:
    """由点击扣费反推期望单次转化成本（仅 OCPC 有意义）。

    E[CPA] ≈ charge_per_click / pCVR
    长期应被调控到接近目标 bid；单次请求上可偏离。
    """
    if item.charge_unit != "per_click":
        return float("nan")
    return item.charge / max(item.pcvr, 1e-8)
