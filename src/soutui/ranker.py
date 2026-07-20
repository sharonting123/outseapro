from __future__ import annotations

from typing import Protocol, runtime_checkable

from .features import clip, context_features, sigmoid
from .models import Ad, BidType, QueryContext, RankedAd, User


# 精排权重：可解释 LR，生产可替换为 Wide&Deep / DIN / 在线学习模型
PCTR_WEIGHTS = {
    "bias": -3.2,
    "query_ad_jaccard": 2.4,
    "brand_match": 1.1,
    "cate_l1_match": 0.8,
    "cate_l2_match": 1.0,
    "hist_ctr": 18.0,
    "quality": 0.6,
    "emb_sim": 1.2,
    "is_search": 0.25,
    "hour_sin": 0.05,
    "hour_cos": 0.05,
}


PCVR_WEIGHTS = {
    "bias": -2.8,
    "hist_cvr": 12.0,
    "cate_l2_match": 0.7,
    "quality": 0.5,
    "log_price": -0.15,
    "emb_sim": 0.9,
    "city_tier": -0.08,
}


def _linear(weights: dict[str, float], feats: dict[str, float]) -> float:
    score = weights.get("bias", 0.0)
    for k, w in weights.items():
        if k == "bias":
            continue
        score += w * feats.get(k, 0.0)
    return score


@runtime_checkable
class CtrCvrModel(Protocol):
    """CTR/CVR 预估协议：后续可用 Sklearn/Torch 实现替换。"""

    def predict(self, feats: dict[str, float]) -> tuple[float, float]:
        """返回 (pctr, pcvr)，已 clip 到 (0,1)。"""
        ...


class LogisticHeuristicModel:
    """当前默认：可解释手工 LR（非训练模型）。"""

    def __init__(
        self,
        pctr_weights: dict[str, float] | None = None,
        pcvr_weights: dict[str, float] | None = None,
    ) -> None:
        self.pctr_weights = pctr_weights or PCTR_WEIGHTS
        self.pcvr_weights = pcvr_weights or PCVR_WEIGHTS

    def predict(self, feats: dict[str, float]) -> tuple[float, float]:
        pctr = clip(sigmoid(_linear(self.pctr_weights, feats)))
        pcvr = clip(sigmoid(_linear(self.pcvr_weights, feats)))
        return pctr, pcvr

    def logits(self, feats: dict[str, float]) -> tuple[float, float]:
        return _linear(self.pctr_weights, feats), _linear(self.pcvr_weights, feats)


def quality_factor(ad: Ad) -> float:
    """排序修正系数 Q。

    真实信息流 OCPX 排序不是裸的 pCTR×pCVR×bid，还会乘质量分：
    - 素材质量 quality_score
    - 账户转化稳定性 conv_stability
    - 负反馈惩罚（差评/跳过/卸载）

    Q 过低会直接压掉排序 eCPM，即使出价很高也难赢量。
    """
    neg_penalty = max(0.15, 1.0 - 2.5 * max(ad.neg_feedback, 0.0))
    q = ad.quality_score * ad.conv_stability * neg_penalty
    return clip(q, lo=0.05, hi=1.5)


class Ranker:
    """pCTR / pCVR 精排 + 排序 eCPM 估值（不含二价扣费）。

    注意分层：
    1) value_per_imp = pCTR × pCVR × bid
       —— 单次曝光预估转化价值（OCPC/OCPM 共用估值核）
    2) rank_ecpm = value_per_imp × Q × 1000
       —— 仅用于排序抢曝光，不是扣费金额
    3) 扣费在 auction.gsp_clearing 里按计费类型做二价清算
    """

    def __init__(
        self,
        pctr_weights: dict[str, float] | None = None,
        pcvr_weights: dict[str, float] | None = None,
        model: CtrCvrModel | None = None,
    ) -> None:
        self.model: CtrCvrModel = model or LogisticHeuristicModel(pctr_weights, pcvr_weights)
        # 兼容旧属性
        if isinstance(self.model, LogisticHeuristicModel):
            self.pctr_weights = self.model.pctr_weights
            self.pcvr_weights = self.model.pcvr_weights
        else:
            self.pctr_weights = pctr_weights or PCTR_WEIGHTS
            self.pcvr_weights = pcvr_weights or PCVR_WEIGHTS

    def score(
        self,
        user: User,
        ctx: QueryContext,
        ad: Ad,
        recall_sources: list[str],
        *,
        effective_bid: float | None = None,
        bid_multiplier: float = 1.0,
    ) -> RankedAd:
        feats = context_features(user, ctx, ad)
        pctr, pcvr = self.model.predict(feats)

        if "search_bm25" in recall_sources:
            pctr = clip(pctr * 1.08)
        if "hot" in recall_sources and len(recall_sources) == 1:
            pctr = clip(pctr * 0.85)

        q = quality_factor(ad)
        bid_for_rank = ad.bid if effective_bid is None else effective_bid
        value_per_imp, rank_ecpm = self.rank_value(ad, pctr, pcvr, q, bid=bid_for_rank)
        charge_unit = "per_click" if ad.bid_type in (BidType.CPC, BidType.OCPC) else "per_mille"

        detail: dict[str, float] = {
            "q_factor": q,
            "value_per_imp": value_per_imp,
            "rank_ecpm": rank_ecpm,
            "target_bid": ad.bid,
            "effective_bid": bid_for_rank,
            "bid_multiplier": bid_multiplier,
            **{f"f_{k}": float(v) for k, v in feats.items()},
        }
        if isinstance(self.model, LogisticHeuristicModel):
            pl, vl = self.model.logits(feats)
            detail["pctr_logit"] = pl
            detail["pcvr_logit"] = vl

        return RankedAd(
            ad=ad,
            recall_sources=list(recall_sources),
            pctr=pctr,
            pcvr=pcvr,
            q_factor=q,
            value_per_imp=value_per_imp,
            rank_ecpm=rank_ecpm,
            ecpm=rank_ecpm,
            charge=0.0,  # 拍卖清算后写入
            charge_unit=charge_unit,
            bid_price=0.0,
            effective_bid=bid_for_rank,
            bid_multiplier=bid_multiplier,
            score_detail=detail,
        )

    @staticmethod
    def rank_value(
        ad: Ad,
        pctr: float,
        pcvr: float,
        q: float,
        *,
        bid: float | None = None,
    ) -> tuple[float, float]:
        """计算排序用估值，明确区分出价类型。

        bid: 排序用出价。oCPX 传入 effective_bid；默认用广告主目标 bid。

        Returns:
            value_per_imp: 单次曝光期望价值（未乘 Q）
            rank_ecpm: 排序千次价值 = value_per_imp * Q * 1000
        """
        use_bid = ad.bid if bid is None else bid
        if ad.bid_type == BidType.CPC:
            value_per_imp = pctr * use_bid
        elif ad.bid_type == BidType.CPM:
            value_per_imp = use_bid / 1000.0
        elif ad.bid_type in (BidType.OCPC, BidType.OCPM):
            # 转化优化：单曝光转化概率 × 有效转化出价
            value_per_imp = pctr * pcvr * use_bid
        else:
            raise ValueError(f"unsupported bid_type: {ad.bid_type}")

        rank_ecpm = value_per_imp * q * 1000.0
        return value_per_imp, rank_ecpm
