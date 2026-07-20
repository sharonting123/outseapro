from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Iterable

from .models import Ad, QueryContext, User


_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")


def tokenize(text: str) -> list[str]:
    text = (text or "").lower().strip()
    if not text:
        return []
    return _TOKEN_RE.findall(text)


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def clip(x: float, lo: float = 1e-6, hi: float = 1.0 - 1e-6) -> float:
    return max(lo, min(hi, x))


class InvertedIndex:
    """简单倒排：token → ad_id 列表，用于搜索召回。"""

    def __init__(self) -> None:
        self._postings: dict[str, set[str]] = defaultdict(set)
        self._ads: dict[str, Ad] = {}

    def add(self, ad: Ad) -> None:
        self._ads[ad.ad_id] = ad
        bag = list(ad.keywords) + [ad.title, ad.brand, ad.cate_l1, ad.cate_l2]
        for token in tokenize(" ".join(bag)):
            self._postings[token].add(ad.ad_id)

    def search(self, query: str, top_k: int = 200) -> list[tuple[Ad, float]]:
        tokens = tokenize(query)
        if not tokens:
            return []
        scores: Counter[str] = Counter()
        for t in tokens:
            for ad_id in self._postings.get(t, ()):
                scores[ad_id] += 1.0
        ranked = []
        for ad_id, tf in scores.most_common(top_k):
            ad = self._ads[ad_id]
            # BM25-lite：词命中率 + 类目/品牌精确加分
            hit = tf / max(len(tokens), 1)
            bonus = 0.0
            qset = set(tokens)
            if any(tokenize(k) and set(tokenize(k)) <= qset for k in ad.keywords):
                bonus += 0.25
            if ad.brand and ad.brand.lower() in query.lower():
                bonus += 0.35
            ranked.append((ad, hit + bonus))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked


def build_user_embedding(user: User, dim: int = 16) -> tuple[float, ...]:
    """把兴趣/类目哈希到稠密向量，便于推荐相似召回。"""
    vec = [0.0] * dim
    for token in list(user.interests) + list(user.recent_cates):
        h = hash(token) % dim
        vec[h] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return tuple(v / norm for v in vec)


def context_features(user: User, ctx: QueryContext, ad: Ad) -> dict[str, float]:
    q_tokens = tokenize(ctx.query)
    ad_tokens = tokenize(" ".join([ad.title, ad.brand, *ad.keywords]))
    user_cates = set(user.recent_cates) | set(user.interests)
    return {
        "query_ad_jaccard": jaccard(q_tokens, ad_tokens),
        "brand_match": 1.0 if ctx.query and ad.brand.lower() in ctx.query.lower() else 0.0,
        "cate_l1_match": 1.0 if ad.cate_l1 in user_cates else 0.0,
        "cate_l2_match": 1.0 if ad.cate_l2 in user_cates else 0.0,
        "hist_ctr": ad.hist_ctr,
        "hist_cvr": ad.hist_cvr,
        "quality": ad.quality_score,
        "log_price": math.log1p(max(ad.price, 0.0)),
        "city_tier": float(user.city_tier),
        "hour_sin": math.sin(2 * math.pi * ctx.hour / 24),
        "hour_cos": math.cos(2 * math.pi * ctx.hour / 24),
        "is_search": 1.0 if ctx.scene.value == "search" else 0.0,
        "emb_sim": cosine(build_user_embedding(user), ad.embedding),
    }
