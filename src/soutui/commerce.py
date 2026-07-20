from __future__ import annotations

from .catalog import sample_ads, sample_budgets, sample_catalog
from .mixer import FeedItem, Mixer, MixerPolicy
from .models import Ad, QueryContext, Scene, Sku, Spu, User
from .organic import OrganicEngine
from .pipeline import AdsEngine
from .budget import BudgetTracker
from .trace import AlgoTrace, start_trace
from .ranker import CtrCvrModel
from .training import load_model_if_available


class CommerceEngine:
    """完整搜推：SPU 召回精排 + SKU 落地 + 广告 + 混排。"""

    def __init__(
        self,
        spus: list[Spu] | None = None,
        skus: list[Sku] | None = None,
        ads: list[Ad] | None = None,
        budget_tracker: BudgetTracker | None = None,
        mixer_policy: MixerPolicy | None = None,
        model: CtrCvrModel | None = None,
    ) -> None:
        if spus is None or skus is None:
            spus, skus = sample_catalog()
        self.spus = spus
        self.skus = skus
        self.spus_by_id = {s.spu_id: s for s in self.spus}
        self.ads = ads or sample_ads(self.spus, self.skus)
        self.organic = OrganicEngine(self.spus, self.skus)
        self.ads_engine = AdsEngine(self.ads, budget_tracker=budget_tracker or sample_budgets(self.ads), model=model)
        self.mixer = Mixer(mixer_policy)

        # 兼容旧属性名
        self.products = self.spus
        self.products_by_id = self.spus_by_id

    def search(
        self,
        user: User,
        query: str,
        *,
        page_size: int = 10,
        hour: int = 12,
        step_delay: float = 0.0,
        explain: bool = True,
    ) -> tuple[list[FeedItem], AlgoTrace | None]:
        if not explain:
            ctx = QueryContext(scene=Scene.SEARCH, query=query, slot_count=page_size, hour=hour)
            organic = self.organic.search(user, ctx, top_k=page_size * 3)
            ads = self.ads_engine.recommend(user, ctx)
            return self.mixer.merge(organic, ads, ctx, self.spus_by_id, self.skus), None

        with start_trace("search", query, step_delay=step_delay) as trace:
            ctx = QueryContext(scene=Scene.SEARCH, query=query, slot_count=page_size, hour=hour)
            organic = self.organic.search(user, ctx, top_k=page_size * 3)
            ads = self.ads_engine.recommend(user, ctx)
            feed = self.mixer.merge(organic, ads, ctx, self.spus_by_id, self.skus)
            return feed, trace

    def feed(
        self,
        user: User,
        *,
        page_size: int = 10,
        hour: int = 12,
        step_delay: float = 0.0,
        explain: bool = True,
    ) -> tuple[list[FeedItem], AlgoTrace | None]:
        if not explain:
            ctx = QueryContext(scene=Scene.FEED, query="", slot_count=page_size, hour=hour)
            organic = self.organic.recommend(user, ctx, top_k=page_size * 3)
            ads = self.ads_engine.recommend(user, ctx)
            return self.mixer.merge(organic, ads, ctx, self.spus_by_id, self.skus), None

        with start_trace("feed", "", step_delay=step_delay) as trace:
            ctx = QueryContext(scene=Scene.FEED, query="", slot_count=page_size, hour=hour)
            organic = self.organic.recommend(user, ctx, top_k=page_size * 3)
            ads = self.ads_engine.recommend(user, ctx)
            feed = self.mixer.merge(organic, ads, ctx, self.spus_by_id, self.skus)
            return feed, trace


def build_default_engine() -> CommerceEngine:
    from .store import get_store
    spus, skus = get_store().load_catalog()
    return CommerceEngine(spus=spus, skus=skus, model=load_model_if_available())
