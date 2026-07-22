from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from .catalog import pick_sku, spu_sales, skus_by_spu
from .features import InvertedIndex, build_user_embedding, cosine, jaccard, tokenize
from .models import QueryContext, Scene, Sku, Spu, User
from .trace import emit


@dataclass
class RankedProduct:
    """自然结果：按 SPU 排序，并带上选中的可售 SKU。"""

    spu: Spu
    sku: Sku
    score: float
    recall_sources: list[str]
    score_detail: dict[str, float]

    @property
    def product(self) -> Spu:
        """兼容旧字段。"""
        return self.spu


class OrganicEngine:
    """自然商品：SPU 召回/精排，再选默认 SKU。"""

    def __init__(self, spus: list[Spu], skus: list[Sku]) -> None:
        self.spus = {s.spu_id: s for s in spus}
        self.skus_map = skus_by_spu(skus)
        self.index = InvertedIndex()
        for spu in spus:
            self.index.add(_SpuAsAd(spu, self.skus_map.get(spu.spu_id, [])))  # type: ignore[arg-type]
        self._cate_index: dict[str, list[Spu]] = defaultdict(list)
        for spu in spus:
            self._cate_index[spu.cate_l1].append(spu)
            self._cate_index[spu.cate_l2].append(spu)
        self._hot = sorted(
            spus,
            key=lambda s: spu_sales(self.skus_map.get(s.spu_id, [])) * s.rating,
            reverse=True,
        )

    def search(self, user: User, ctx: QueryContext, top_k: int = 50) -> list[RankedProduct]:
        emit(
            "organic",
            "【自然搜索】SPU 召回",
            formula="query 倒排(SPU) → 相关门控精排 → 为每个 SPU 选可售 SKU",
            detail={"query": ctx.query, "spu_count": len(self.spus)},
        )
        hits: dict[str, list[str]] = defaultdict(list)
        q = ctx.query.strip()
        q_tokens = set(tokenize(q))

        if q:
            for ad_like, _ in self.index.search(q, top_k=max(top_k * 3, 50)):
                hits[ad_like.ad_id].append("search_bm25")
            for hist in user.recent_queries[:5]:
                htoks = set(tokenize(hist))
                if not htoks or not (htoks & q_tokens):
                    continue
                for ad_like, _ in self.index.search(hist, top_k=30):
                    if "query_expand" not in hits[ad_like.ad_id]:
                        hits[ad_like.ad_id].append("query_expand")

            for spu in self.spus.values():
                bag = self._spu_text_bag(spu)
                if q in bag or (q_tokens and q_tokens <= set(tokenize(bag))):
                    if "search_bm25" not in hits[spu.spu_id] and "text_match" not in hits[spu.spu_id]:
                        hits[spu.spu_id].append("text_match")

        ranked = self._rank(user, ctx, hits, top_k, search_mode=True)
        emit(
            "organic",
            f"【自然精排】SPU {len(hits)} → Top-{len(ranked)}，并选定 SKU",
            formula="按 spu_id 排序；sku = pick(有货 ∩ query规格 ∩ 销量)",
            detail={
                "hits": len(hits),
                "top": [
                    {
                        "spu_id": r.spu.spu_id,
                        "sku_id": r.sku.sku_id,
                        "title": r.spu.title[:16],
                        "attrs": r.sku.attr_text(),
                        "score": round(r.score, 3),
                    }
                    for r in ranked[:5]
                ],
            },
        )
        return ranked

    def recommend(self, user: User, ctx: QueryContext, top_k: int = 50) -> list[RankedProduct]:
        emit(
            "organic",
            "【自然推荐】兴趣/向量/标签/热度召回（SPU）",
            formula="interest_cate ∪ emb_sim ∪ tag_jaccard ∪ hot → pick SKU",
            detail={"spu_count": len(self.spus)},
        )
        hits: dict[str, list[str]] = defaultdict(list)
        for cate in list(user.interests) + list(user.recent_cates):
            for spu in self._cate_index.get(cate, [])[:60]:
                hits[spu.spu_id].append("interest_cate")

        uemb = build_user_embedding(user)
        sims = [(spu, cosine(uemb, spu.embedding)) for spu in self.spus.values()]
        sims.sort(key=lambda x: x[1], reverse=True)
        for spu, s in sims[:80]:
            if s > 0.05 and "emb_sim" not in hits[spu.spu_id]:
                hits[spu.spu_id].append("emb_sim")

        utags = set(user.interests) | set(user.recent_cates)
        for spu in self.spus.values():
            if jaccard(utags, set(spu.tags) | {spu.cate_l1, spu.cate_l2}) > 0.2:
                if "tag_jaccard" not in hits[spu.spu_id]:
                    hits[spu.spu_id].append("tag_jaccard")

        for spu in self._hot[:30]:
            if "hot" not in hits[spu.spu_id]:
                hits[spu.spu_id].append("hot")

        ranked = self._rank(user, ctx, hits, top_k, search_mode=False)
        emit(
            "organic",
            f"【自然精排】SPU {len(hits)} → Top-{len(ranked)}",
            formula="score = 1.5·cate + 0.6·emb + 0.3·sales + 0.2·rating + 0.9·cold_start；再选 SKU",
            detail={
                "hits": len(hits),
                "top": [
                    {
                        "spu_id": r.spu.spu_id,
                        "sku_id": r.sku.sku_id,
                        "title": r.spu.title[:16],
                        "score": round(r.score, 3),
                    }
                    for r in ranked[:5]
                ],
            },
        )
        return ranked

    def _spu_text_bag(self, spu: Spu) -> str:
        parts = [spu.title, spu.brand, spu.cate_l1, spu.cate_l2, *spu.keywords, *spu.tags]
        for sku in self.skus_map.get(spu.spu_id, []):
            parts.extend(sku.attrs.values())
        return " ".join(parts)

    @staticmethod
    def _text_relevance(spu: Spu, bag: str, query: str, q_tokens: list[str]) -> float:
        if not query.strip():
            return 0.0
        bag_l = bag.lower()
        q = query.lower().strip()
        rel = jaccard(q_tokens, tokenize(bag)) if q_tokens else 0.0
        if q in bag_l:
            rel = max(rel, 0.9)
        elif q_tokens and all(t in bag_l for t in q_tokens):
            rel = max(rel, 0.65)
        if spu.cate_l2 and spu.cate_l2.lower() in q:
            rel = max(rel, 0.85)
        if spu.brand and spu.brand.lower() in q:
            rel += 0.35
        return min(rel, 1.5)

    def _rank(
        self,
        user: User,
        ctx: QueryContext,
        hits: dict[str, list[str]],
        top_k: int,
        *,
        search_mode: bool = False,
    ) -> list[RankedProduct]:
        q_tokens = tokenize(ctx.query)
        ranked: list[RankedProduct] = []
        for sid, sources in hits.items():
            spu = self.spus[sid]
            skus = self.skus_map.get(sid, [])
            sku = pick_sku(skus, query=ctx.query)
            if sku is None:
                continue
            if spu_sales(skus) < 0:
                continue
            # 整款无货则不出
            if all(s.stock <= 0 for s in skus):
                continue

            bag = self._spu_text_bag(spu)
            text_rel = self._text_relevance(spu, bag, ctx.query, q_tokens)
            cate_match = 1.0 if spu.cate_l2 in (user.recent_cates + user.interests) else 0.0
            sales_score = math.log1p(spu_sales(skus)) / 12.0
            rating_score = (spu.rating - 3.0) / 2.0
            emb = cosine(build_user_embedding(user), spu.embedding)
            cold_start_score = 1.0 if spu_sales(skus) == 0 and cate_match else 0.0

            if search_mode and ctx.query.strip():
                if text_rel < 0.15 and "search_bm25" not in sources and "text_match" not in sources:
                    continue
                if text_rel < 0.08:
                    continue
                score = (
                    5.0 * text_rel
                    + 0.3 * cate_match
                    + 0.25 * sales_score
                    + 0.15 * rating_score
                    + 0.2 * emb
                    + (0.4 if "search_bm25" in sources or "text_match" in sources else 0.0)
                )
            elif ctx.scene == Scene.FEED:
                score = (
                    1.5 * cate_match
                    + 0.6 * emb
                    + 0.3 * sales_score
                    + 0.2 * rating_score
                    + 0.9 * cold_start_score
                )
            else:
                score = (
                    2.5 * text_rel
                    + 0.8 * cate_match
                    + 0.6 * sales_score
                    + 0.4 * rating_score
                    + 0.7 * emb
                    + (0.3 if "search_bm25" in sources else 0.0)
                )
            ranked.append(
                RankedProduct(
                    spu=spu,
                    sku=sku,
                    score=score,
                    recall_sources=list(sources),
                    score_detail={
                        "text_rel": text_rel,
                        "cate_match": cate_match,
                        "sales_score": sales_score,
                        "rating_score": rating_score,
                        "emb": emb,
                        "cold_start_score": cold_start_score,
                        "sku_price": sku.price,
                        "sku_stock": float(sku.stock),
                    },
                )
            )
        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked[:top_k]


class _SpuAsAd:
    """让 SPU(+SKU 属性词) 能复用 InvertedIndex。"""

    def __init__(self, spu: Spu, skus: list[Sku]) -> None:
        self.ad_id = spu.spu_id
        self.title = spu.title
        self.brand = spu.brand
        self.cate_l1 = spu.cate_l1
        self.cate_l2 = spu.cate_l2
        extra = tuple(v for sku in skus for v in sku.attrs.values())
        self.keywords = tuple(spu.keywords) + extra
