from __future__ import annotations

import argparse
import json
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .features import clip
from .store import DEFAULT_DB, Store

FEATURE_NAMES = [
    "query_ad_jaccard", "brand_match", "cate_l1_match", "cate_l2_match",
    "hist_ctr", "hist_cvr", "quality", "log_price", "city_tier",
    "hour_sin", "hour_cos", "is_search", "emb_sim", "position",
]
DEFAULT_ARTIFACT = Path(__file__).resolve().parents[2] / "data" / "ctr_cvr_model.json"


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return np.where(x >= 0, 1.0 / (1.0 + np.exp(-x)), np.exp(x) / (1.0 + np.exp(x)))


def _auc(y: np.ndarray, p: np.ndarray) -> float | None:
    pos = int(y.sum()); neg = len(y) - pos
    if not pos or not neg:
        return None
    order = np.argsort(p)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(y) + 1)
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


def _logloss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-7, 1 - 1e-7)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def _fit_lr(x: np.ndarray, y: np.ndarray, epochs: int = 800, lr: float = 0.06, l2: float = 0.02) -> tuple[np.ndarray, float]:
    if len(np.unique(y)) < 2:
        raise ValueError("labels need both positive and negative samples")
    w = np.zeros(x.shape[1], dtype=float)
    prevalence = float(y.mean())
    b = math.log(max(prevalence, 1e-5) / max(1 - prevalence, 1e-5))
    pos_weight = min(20.0, (len(y) - y.sum()) / max(y.sum(), 1.0))
    sample_weight = np.where(y == 1, pos_weight, 1.0)
    sample_weight /= sample_weight.mean()
    for _ in range(epochs):
        pred = _sigmoid(x @ w + b)
        err = (pred - y) * sample_weight
        w -= lr * ((x.T @ err) / len(y) + l2 * w)
        b -= lr * float(err.mean())
    return w, b


def _feature_dict(row: dict[str, Any]) -> dict[str, float]:
    raw = json.loads(row.get("features_json") or "{}")
    out: dict[str, float] = {}
    for key, value in raw.items():
        name = key[2:] if key.startswith("f_") else key
        if name in FEATURE_NAMES and isinstance(value, (int, float)):
            out[name] = float(value)
    # Older organic snapshots can still teach matching/price/embedding signals.
    aliases = {
        "text_rel": "query_ad_jaccard", "cate_match": "cate_l2_match",
        "emb": "emb_sim", "sku_price": "_price",
    }
    for old, new in aliases.items():
        if old in raw and isinstance(raw[old], (int, float)):
            if new == "_price": out["log_price"] = math.log1p(max(float(raw[old]), 0.0))
            else: out[new] = float(raw[old])
    out["position"] = float(row.get("position", -1))
    out["is_search"] = 1.0 if row.get("scene") == "search" else out.get("is_search", 0.0)
    return out


def build_datasets(store: Store, attribution_days: int = 7) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY ts,id").fetchall()]
    impressions = [r for r in rows if r["event_type"] == "impress" and r.get("scene") in ("search", "feed")]
    clicks = [r for r in rows if r["event_type"] == "click"]
    orders = [r for r in rows if r["event_type"] == "order"]
    window = attribution_days * 86400

    def matches(action: dict[str, Any], imp: dict[str, Any]) -> bool:
        if action["user_id"] != imp["user_id"] or action["ts"] < imp["ts"] or action["ts"] > imp["ts"] + window:
            return False
        same_item = (action.get("sku_id") and action["sku_id"] == imp["sku_id"]) or (action.get("spu_id") and action["spu_id"] == imp["spu_id"])
        same_request = bool(action.get("request_id") and action["request_id"] == imp["request_id"])
        return same_item and (same_request or not action.get("request_id"))

    feats = [_feature_dict(r) for r in impressions]
    x = np.asarray([[f.get(name, 0.0) for name in FEATURE_NAMES] for f in feats], dtype=float)
    y_ctr = np.asarray([1.0 if any(matches(c, imp) for c in clicks) else 0.0 for imp in impressions])
    clicked_idx = np.flatnonzero(y_ctr == 1)
    x_cvr = x[clicked_idx]
    y_cvr = np.asarray([1.0 if any(matches(o, impressions[i]) for o in orders) else 0.0 for i in clicked_idx])
    stats = {
        "impressions": len(impressions), "clicks_attributed": int(y_ctr.sum()),
        "cvr_samples": len(y_cvr), "orders_attributed": int(y_cvr.sum()),
    }
    return x, y_ctr, x_cvr, y_cvr, stats


def train(store: Store, artifact_path: Path | str = DEFAULT_ARTIFACT) -> dict[str, Any]:
    path = Path(artifact_path)
    run_id = "model_" + uuid.uuid4().hex[:12]
    store.add_model_run(run_id, "training", str(path))
    try:
        x_ctr, y_ctr, x_cvr, y_cvr, stats = build_datasets(store)
        if len(x_ctr) < 20 or len(np.unique(y_ctr)) < 2:
            raise ValueError("CTR 样本不足：至少需要 20 次曝光且同时有点击/未点击")
        if len(x_cvr) < 3 or len(np.unique(y_cvr)) < 2:
            raise ValueError("CVR 样本不足：至少需要 3 次已点击曝光且同时有支付/未支付")
        mean = x_ctr.mean(axis=0)
        scale = x_ctr.std(axis=0); scale[scale < 1e-8] = 1.0
        ctr_x = (x_ctr - mean) / scale
        cvr_x = (x_cvr - mean) / scale
        ctr_w, ctr_b = _fit_lr(ctr_x, y_ctr)
        cvr_w, cvr_b = _fit_lr(cvr_x, y_cvr)
        ctr_p = _sigmoid(ctr_x @ ctr_w + ctr_b)
        cvr_p = _sigmoid(cvr_x @ cvr_w + cvr_b)
        metrics = {
            **stats,
            "ctr_auc": _auc(y_ctr, ctr_p), "ctr_logloss": _logloss(y_ctr, ctr_p),
            "cvr_auc": _auc(y_cvr, cvr_p), "cvr_logloss": _logloss(y_cvr, cvr_p),
        }
        artifact = {
            "version": 1, "run_id": run_id, "trained_at": time.time(),
            "feature_names": FEATURE_NAMES, "mean": mean.tolist(), "scale": scale.tolist(),
            "ctr": {"weights": ctr_w.tolist(), "bias": ctr_b},
            "cvr": {"weights": cvr_w.tolist(), "bias": cvr_b}, "metrics": metrics,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        store.finish_model_run(run_id, "ready", metrics, len(x_ctr))
        return artifact
    except Exception as exc:
        store.finish_model_run(run_id, "failed", {"error": str(exc)}, 0)
        raise


@dataclass
class TrainedCtrCvrModel:
    feature_names: list[str]
    mean: np.ndarray
    scale: np.ndarray
    ctr_weights: np.ndarray
    ctr_bias: float
    cvr_weights: np.ndarray
    cvr_bias: float
    run_id: str = ""

    @classmethod
    def load(cls, path: Path | str = DEFAULT_ARTIFACT) -> "TrainedCtrCvrModel":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            data["feature_names"], np.asarray(data["mean"]), np.asarray(data["scale"]),
            np.asarray(data["ctr"]["weights"]), float(data["ctr"]["bias"]),
            np.asarray(data["cvr"]["weights"]), float(data["cvr"]["bias"]), data.get("run_id", ""),
        )

    def predict(self, feats: dict[str, float]) -> tuple[float, float]:
        values = np.asarray([float(feats.get(k, 0.0)) for k in self.feature_names])
        x = (values - self.mean) / self.scale
        pctr = float(_sigmoid(np.asarray([x @ self.ctr_weights + self.ctr_bias]))[0])
        pcvr = float(_sigmoid(np.asarray([x @ self.cvr_weights + self.cvr_bias]))[0])
        return clip(pctr), clip(pcvr)


def load_model_if_available(path: Path | str = DEFAULT_ARTIFACT) -> TrainedCtrCvrModel | None:
    try:
        return TrainedCtrCvrModel.load(path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Train CTR/CVR logistic models from soutui events")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output", default=str(DEFAULT_ARTIFACT))
    args = parser.parse_args()
    artifact = train(Store(args.db), args.output)
    print(json.dumps({"run_id": artifact["run_id"], "metrics": artifact["metrics"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
