from __future__ import annotations

from collections import defaultdict

from .features import InvertedIndex, build_user_embedding, cosine, jaccard, tokenize
from .models import Ad, QueryContext, Scene, User


class RecallEngine:
    """多路召回：搜索倒排 / 兴趣类目 / 向量相似 / 热度兜底。"""

    def __init__(self, ads: list[Ad], index: InvertedIndex | None = None) -> None:
        self.ads = {a.ad_id: a for a in ads}
        self.index = index or InvertedIndex()
        if index is None:
            for ad in ads:
                self.index.add(ad)
        self._cate_index: dict[str, list[Ad]] = defaultdict(list)
        for ad in ads:
            self._cate_index[ad.cate_l1].append(ad)
            self._cate_index[ad.cate_l2].append(ad)
        self._hot = sorted(ads, key=lambda a: a.hist_ctr * a.hist_cvr * a.quality_score, reverse=True)

    def recall(self, user: User, ctx: QueryContext, top_k: int = 300) -> dict[str, list[str]]:
        """返回 ad_id → recall source 列表。"""
        hits: dict[str, list[str]] = defaultdict(list)

        def add(ad: Ad, source: str) -> None:
            if source not in hits[ad.ad_id]:
                hits[ad.ad_id].append(source)

        if ctx.scene == Scene.SEARCH and ctx.query.strip():
            for ad, _score in self.index.search(ctx.query, top_k=top_k):
                add(ad, "search_bm25")
            # query rewrite：仅扩展与当前 query 有交集的历史词，避免串台
            q_tokens = set(tokenize(ctx.query))
            for q in user.recent_queries[:5]:
                if not (set(tokenize(q)) & q_tokens):
                    continue
                for ad, _ in self.index.search(q, top_k=50):
                    add(ad, "query_expand")

            # 搜索场景：兴趣/向量只作为相关候选上的软补充，且必须与 query 文本有交集
            q = ctx.query.strip().lower()
            for cate in list(user.interests) + list(user.recent_cates):
                for ad in self._cate_index.get(cate, [])[:80]:
                    bag = f"{ad.title} {ad.brand} {ad.cate_l1} {ad.cate_l2} {' '.join(ad.keywords)}".lower()
                    if q in bag or (q_tokens and q_tokens & set(tokenize(bag))):
                        add(ad, "interest_cate")

            uemb = build_user_embedding(user)
            sims = [(ad, cosine(uemb, ad.embedding)) for ad in self.ads.values()]
            sims.sort(key=lambda x: x[1], reverse=True)
            for ad, score in sims[:120]:
                if score <= 0.05:
                    continue
                bag = f"{ad.title} {ad.brand} {ad.cate_l1} {ad.cate_l2} {' '.join(ad.keywords)}".lower()
                if q in bag or (q_tokens and q_tokens & set(tokenize(bag))):
                    add(ad, "emb_sim")

            for ad in self._hot[:50]:
                bag = f"{ad.title} {ad.brand} {ad.cate_l1} {ad.cate_l2} {' '.join(ad.keywords)}".lower()
                if q in bag or (q_tokens and q_tokens & set(tokenize(bag))):
                    add(ad, "hot")
        else:
            # 兴趣 / 类目召回（推荐）
            for cate in list(user.interests) + list(user.recent_cates):
                for ad in self._cate_index.get(cate, [])[:80]:
                    add(ad, "interest_cate")

            uemb = build_user_embedding(user)
            sims = []
            for ad in self.ads.values():
                sims.append((ad, cosine(uemb, ad.embedding)))
            sims.sort(key=lambda x: x[1], reverse=True)
            for ad, score in sims[:120]:
                if score > 0.05:
                    add(ad, "emb_sim")

            if ctx.scene == Scene.FEED:
                utags = set(user.interests) | set(user.recent_cates)
                for ad in self.ads.values():
                    if jaccard(utags, set(ad.tags) | {ad.cate_l1, ad.cate_l2}) > 0.2:
                        add(ad, "tag_jaccard")

            for ad in self._hot[:50]:
                add(ad, "hot")

        # 频控：今日曝光过多的广告降权删除
        filtered = {}
        for ad_id, sources in hits.items():
            shown = user.freq_cap_today.get(ad_id, 0)
            if shown >= 5:
                continue
            filtered[ad_id] = sources
        return filtered
