from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui.ranker import LogisticHeuristicModel, Ranker
from soutui.catalog import sample_ads, sample_user
from soutui.models import QueryContext, Scene


def test_ctr_cvr_model_protocol():
    model = LogisticHeuristicModel()
    feats = {"bias": 1.0, "hist_ctr": 0.02, "hist_cvr": 0.05, "quality": 1.0}
    pctr, pcvr = model.predict(feats)
    assert 0 < pctr < 1
    assert 0 < pcvr < 1


def test_ranker_uses_model():
    ads = sample_ads()
    user = sample_user()
    ctx = QueryContext(scene=Scene.SEARCH, query="跑鞋", slot_count=4)
    ranker = Ranker(model=LogisticHeuristicModel())
    scored = ranker.score(user, ctx, ads[0], ["search_bm25"])
    assert scored.pctr > 0
    assert scored.pcvr > 0
    assert "f_hist_ctr" in scored.score_detail or "rank_ecpm" in scored.score_detail
