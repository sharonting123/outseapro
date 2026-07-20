from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BidType(str, Enum):
    """出价 / 计费类型。

    - CPC:  手动点击出价，按点击计费
    - CPM:  手动千次曝光出价，按曝光计费
    - OCPC: 目标转化出价 + 按点击计费（主流 OCPX 形态）
    - OCPM: 目标转化出价 + 按千次曝光计费

    「OCPX」是转化优化统称；本仓库用 OCPC/OCPM 区分计费底层。
    """

    CPC = "cpc"
    CPM = "cpm"
    OCPC = "ocpc"
    OCPM = "ocpm"


class Scene(str, Enum):
    SEARCH = "search"
    FEED = "feed"


@dataclass(frozen=True)
class User:
    user_id: str
    gender: str = "unknown"
    age_bucket: str = "unknown"
    city_tier: int = 3
    interests: tuple[str, ...] = ()
    recent_cates: tuple[str, ...] = ()
    recent_queries: tuple[str, ...] = ()
    freq_cap_today: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryContext:
    scene: Scene
    query: str = ""
    page: int = 1
    slot_count: int = 8
    request_id: str = ""
    hour: int = 12
    device: str = "android"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Spu:
    """标准产品单元（Standard Product Unit）：一款商品，搜推主粒度。"""

    spu_id: str
    title: str
    brand: str
    cate_l1: str
    cate_l2: str
    rating: float = 4.5
    keywords: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    embedding: tuple[float, ...] = ()

    @property
    def product_id(self) -> str:
        """兼容旧字段名：等同 spu_id。"""
        return self.spu_id


@dataclass
class Sku:
    """库存保有单元（Stock Keeping Unit）：可售规格，独立价格/库存。"""

    sku_id: str
    spu_id: str
    price: float
    stock: int = 0
    sales: int = 0
    attrs: dict[str, str] = field(default_factory=dict)  # 颜色/尺码/容量等

    def attr_text(self) -> str:
        if not self.attrs:
            return ""
        return " / ".join(f"{k}:{v}" for k, v in self.attrs.items())


# 兼容旧名
Product = Spu


@dataclass
class Ad:
    ad_id: str
    campaign_id: str
    advertiser_id: str
    title: str
    brand: str
    cate_l1: str
    cate_l2: str
    spu_id: str = ""  # 投放绑 SPU（同款聚在一起）
    sku_id: str = ""  # 可选：落地具体规格；空则混排时再选默认可售 SKU
    keywords: tuple[str, ...] = ()
    bid_type: BidType = BidType.OCPC
    # CPC: 单次点击出价；OCPC/OCPM: 单次转化目标出价；CPM: 千次曝光出价
    bid: float = 1.0
    quality_score: float = 1.0  # 素材基础质量 ∈ (0, 1+]
    conv_stability: float = 1.0  # 账户历史转化稳定性 ∈ (0, 1+]
    neg_feedback: float = 0.0  # 负反馈率（差评/跳过/卸载），越高越差
    hist_ctr: float = 0.02
    hist_cvr: float = 0.05
    price: float = 99.0
    stock: int = 1000
    embedding: tuple[float, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def product_id(self) -> str:
        """兼容旧字段名：等同 spu_id。"""
        return self.spu_id


@dataclass
class RankedAd:
    ad: Ad
    recall_sources: list[str]
    pctr: float
    pcvr: float
    q_factor: float
    # 单次曝光预估转化价值（未乘 Q、未×1000）= pCTR * pCVR * bid
    value_per_imp: float
    # 排序用 eCPM = value_per_imp * Q * 1000
    rank_ecpm: float
    # 兼容旧字段名：等同 rank_ecpm（pacing 前或后取决于 pipeline 阶段）
    ecpm: float
    # 计费字段：OCPC=点击扣费；OCPM/CPM=千次曝光扣费；CPC=点击扣费
    charge: float
    charge_unit: str  # "per_click" | "per_mille"
    bid_price: float  # 兼容旧字段：等同 charge
    # pacing 修正后的排序 eCPM（预算匀速投放）
    paced_rank_ecpm: float = 0.0
    pacing_factor: float = 1.0
    # oCPX 自动调价：effective_bid = target_bid × bid_multiplier
    effective_bid: float = 0.0
    bid_multiplier: float = 1.0
    score_detail: dict[str, float] = field(default_factory=dict)
    rank: int = 0
