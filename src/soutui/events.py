from __future__ import annotations

import time
from typing import Any

from .mixer import FeedItem
from .store import Store, get_store


def log_impressions(
    store: Store | None,
    *,
    user_id: str,
    request_id: str,
    scene: str,
    query: str,
    items: list[FeedItem],
) -> int:
    """列表曝光：冻结特征快照，供后续 CTR/CVR 训练。"""
    store = store or get_store()
    ts = time.time()
    n = 0
    for it in items:
        feats = {k: v for k, v in (it.extra or {}).items() if isinstance(v, (int, float, str, bool))}
        # 嵌套结构单独放
        nested = {k: v for k, v in (it.extra or {}).items() if k not in feats}
        pctr = it.extra.get("pctr") if it.extra else None
        pcvr = it.extra.get("pcvr") if it.extra else None
        if isinstance(pctr, (int, float)):
            pctr = float(pctr)
        else:
            pctr = None
        if isinstance(pcvr, (int, float)):
            pcvr = float(pcvr)
        else:
            pcvr = None
        store.insert_event(
            event_type="impress",
            user_id=user_id,
            request_id=request_id,
            scene=scene,
            query=query,
            spu_id=it.spu.spu_id,
            sku_id=it.sku.sku_id,
            position=it.position,
            is_ad=it.is_ad,
            ad_id=it.ad_id,
            pctr=pctr,
            pcvr=pcvr,
            features=feats,
            extra={"score": it.score, "nested": _safe_jsonable(nested)},
            ts=ts,
        )
        n += 1
    return n


def log_event(
    store: Store | None,
    *,
    event_type: str,
    user_id: str,
    request_id: str = "",
    scene: str = "",
    query: str = "",
    spu_id: str = "",
    sku_id: str = "",
    position: int = -1,
    is_ad: bool = False,
    ad_id: str = "",
    pctr: float | None = None,
    pcvr: float | None = None,
    features: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> int:
    store = store or get_store()
    return store.insert_event(
        event_type=event_type,
        user_id=user_id,
        request_id=request_id,
        scene=scene,
        query=query,
        spu_id=spu_id,
        sku_id=sku_id,
        position=position,
        is_ad=is_ad,
        ad_id=ad_id,
        pctr=pctr,
        pcvr=pcvr,
        features=features,
        extra=extra,
        ts=time.time(),
    )


def _safe_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _safe_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_jsonable(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)
