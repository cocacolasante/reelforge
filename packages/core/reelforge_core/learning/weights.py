"""Score weights: today's hand-set blend, or an offline fit of real
performance (`reelforge fit-weights --apply`). Never trained online.

The fit is ridge regression of a performance target on the four ranking
dimensions, clipped non-negative, normalised to sum 1, then SHRUNK toward
the defaults by n / (n + SHRINK_N) — 50 labels move the weights halfway at
most. Below MIN_LABELS it refuses.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from reelforge_core.learning.dataset import SCORE_FIELDS, Row

DEFAULT_WEIGHTS = {
    "hook_strength": 0.35,
    "narrative_coherence": 0.30,
    "emotional_payoff": 0.20,
    "standalone_clarity": 0.15,
}
MIN_LABELS = 50
SHRINK_N = 50.0
RIDGE_ALPHA = 1.0
WEIGHTS_FORMAT = "w1"


@dataclass(frozen=True)
class LearnedWeights:
    weights: dict[str, float]
    version: str
    n: int
    target: str

    def combine(self, scores) -> float:
        return sum(self.weights[k] * float(getattr(scores, k)) for k in SCORE_FIELDS)


def weights_path(data_dir: Path | None = None) -> Path:
    base = Path(os.environ.get("REELFORGE_DATA_DIR", "/data")) if data_dir is None else data_dir
    return Path(os.environ.get("REELFORGE_WEIGHTS_FILE", base / "learning" / "score_weights.json"))


def fit_weights(rows: list[Row], target: str = "completion_rate") -> LearnedWeights:
    """Fit from labelled rows. Raises ValueError below MIN_LABELS. Pure."""
    import numpy as np

    usable = [r for r in rows if r.targets.get(target) is not None
              and all(k in r.features for k in SCORE_FIELDS)]
    if len(usable) < MIN_LABELS:
        raise ValueError(f"{len(usable)} usable label(s) for {target}; need {MIN_LABELS}")
    X = np.array([[r.features[k] for k in SCORE_FIELDS] for r in usable], dtype=float)
    y = np.array([float(r.targets[target]) for r in usable])
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd
    yc = y - y.mean()
    beta = np.linalg.solve(Z.T @ Z + RIDGE_ALPHA * np.eye(len(SCORE_FIELDS)), Z.T @ yc)
    raw = np.clip(beta / sd, 0.0, None)  # back to score units; no negative weights
    if raw.sum() <= 0:
        fitted = dict(DEFAULT_WEIGHTS)
    else:
        fitted = {k: float(v) for k, v in zip(SCORE_FIELDS, raw / raw.sum())}
    shrink = len(usable) / (len(usable) + SHRINK_N)
    blended = {k: shrink * fitted[k] + (1 - shrink) * DEFAULT_WEIGHTS[k] for k in SCORE_FIELDS}
    total = sum(blended.values())
    weights = {k: round(v / total, 4) for k, v in blended.items()}
    digest = hashlib.sha1(json.dumps([weights, target, len(usable)], sort_keys=True).encode()).hexdigest()[:8]
    return LearnedWeights(weights=weights, version=f"{WEIGHTS_FORMAT}-{digest}", n=len(usable), target=target)


def save_weights(lw: LearnedWeights, path: Path) -> None:
    from reelforge_core.io_utils import write_json_atomic

    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {
        "version": lw.version, "weights": lw.weights, "n": lw.n, "target": lw.target,
        "fitted_at": datetime.now(timezone.utc).isoformat(),
    })


def load_weights(path: Path | None = None) -> LearnedWeights | None:
    """The applied weights, or None (no file, unreadable, or implausible —
    the defaults then apply)."""
    path = path or weights_path()
    try:
        data = json.loads(path.read_text())
        w = {k: float(data["weights"][k]) for k in SCORE_FIELDS}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if any(v < 0 for v in w.values()) or abs(sum(w.values()) - 1.0) > 0.01 or int(data.get("n", 0)) < MIN_LABELS:
        return None
    return LearnedWeights(weights=w, version=str(data.get("version", WEIGHTS_FORMAT)),
                          n=int(data["n"]), target=str(data.get("target", "")))
