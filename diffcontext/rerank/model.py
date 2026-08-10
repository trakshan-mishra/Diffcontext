"""
model.py — stage-2 inference. Pure Python stdlib, by contract.

The shipped package declares ``dependencies = []`` and that is a product
promise, not an accident. Training (``benchmarks/rerank/train.py``) may use
numpy and scipy because it runs offline; **this module may import nothing but
the standard library**, and ``tests/test_rerank.py`` asserts that scoring a
candidate never pulls numpy into ``sys.modules``.

The model is a standardized L2-regularized logistic regression:

    p = sigmoid( sum_i coef[i] * (x[i] - mean[i]) / scale[i] + intercept )

`p` is a calibrated estimate of "this candidate is part of the same logical
change", which is what makes the probability cutoff in ``context.selector``
possible — the stage-1 blend's min-max normalized score never could be one.
"""

import json
import math
import os
from typing import Dict, List, Optional, Sequence

from .features import FEATURE_NAMES, QueryContext, extract_features

WEIGHTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weights.json")

# Bump when the feature set or the scoring form changes in a way that makes
# an older weights.json wrong rather than merely stale.
SUPPORTED_VERSION = 1


class RerankModelError(RuntimeError):
    """Raised when weights are missing, malformed, or trained on a
    different feature contract than this code implements."""


def _sigmoid(z: float) -> float:
    # Branch to avoid overflow in exp() for large-magnitude logits.
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


class RerankModel:
    """A loaded reranker. Construct via :meth:`load`."""

    __slots__ = ("feature_names", "mean", "scale", "coef", "intercept", "meta")

    def __init__(
        self,
        feature_names: Sequence[str],
        mean: Sequence[float],
        scale: Sequence[float],
        coef: Sequence[float],
        intercept: float,
        meta: Optional[Dict] = None,
    ):
        n = len(feature_names)
        if not (len(mean) == len(scale) == len(coef) == n):
            raise RerankModelError(
                f"weights arrays disagree: {n} names, {len(mean)} mean, "
                f"{len(scale)} scale, {len(coef)} coef"
            )
        # Feature order is the model contract. A silent mismatch here would
        # score every candidate against the wrong coefficient, which looks
        # like a merely mediocre model rather than a bug — so fail loudly.
        if tuple(feature_names) != tuple(FEATURE_NAMES):
            raise RerankModelError(
                "feature contract mismatch between weights.json and "
                "features.FEATURE_NAMES.\n"
                f"  weights: {list(feature_names)}\n"
                f"  code:    {list(FEATURE_NAMES)}"
            )
        # A zero scale means a constant feature in training; dividing by it
        # yields inf/nan. Training writes 1.0 for those, but defend anyway.
        if any(s == 0.0 for s in scale):
            raise RerankModelError("weights.json contains a zero scale entry")

        self.feature_names = tuple(feature_names)
        self.mean = tuple(float(v) for v in mean)
        self.scale = tuple(float(v) for v in scale)
        self.coef = tuple(float(v) for v in coef)
        self.intercept = float(intercept)
        self.meta = meta or {}

    @classmethod
    def load(cls, path: Optional[str] = None) -> "RerankModel":
        """Load a model from ``weights.json`` (or `path`)."""
        path = path or WEIGHTS_PATH
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except FileNotFoundError:
            raise RerankModelError(
                f"no reranker weights at {path}. Train one with "
                "`python -m benchmarks.rerank.train`, or run with rerank=False."
            )
        except json.JSONDecodeError as exc:
            raise RerankModelError(f"malformed weights at {path}: {exc}")

        version = blob.get("version")
        if version != SUPPORTED_VERSION:
            raise RerankModelError(
                f"weights.json version {version!r} but this build supports "
                f"{SUPPORTED_VERSION}"
            )
        try:
            return cls(
                feature_names=blob["feature_names"],
                mean=blob["mean"],
                scale=blob["scale"],
                coef=blob["coef"],
                intercept=blob.get("intercept", 0.0),
                meta={k: blob[k] for k in
                      ("trained_on", "trained_at", "n_rows", "metrics")
                      if k in blob},
            )
        except KeyError as exc:
            raise RerankModelError(f"weights.json missing key {exc}")

    def score_vector(self, x: Sequence[float]) -> float:
        """Probability for one already-extracted feature vector."""
        if len(x) != len(self.coef):
            raise RerankModelError(
                f"expected {len(self.coef)} features, got {len(x)}"
            )
        z = self.intercept
        for xi, m, s, c in zip(x, self.mean, self.scale, self.coef):
            z += c * (xi - m) / s
        return _sigmoid(z)

    def score_candidates(
        self, ctx: QueryContext, candidates: Sequence[str]
    ) -> Dict[str, float]:
        """Probability for each candidate, extracting features as it goes."""
        return {
            cid: self.score_vector(extract_features(ctx, cid))
            for cid in candidates
        }

    def rerank(
        self, ctx: QueryContext, candidates: Sequence[str]
    ) -> List[str]:
        """`candidates` reordered by descending probability.

        Ties break on the original stage-1 order, so a model with nothing to
        say degrades to the shipped ranking rather than to an arbitrary one.
        """
        scores = self.score_candidates(ctx, candidates)
        order = {cid: i for i, cid in enumerate(candidates)}
        return sorted(candidates, key=lambda c: (-scores[c], order[c]))


_CACHED: Optional[RerankModel] = None


def get_model(path: Optional[str] = None) -> RerankModel:
    """Process-wide cached model load (weights are read-only and ~4 KB)."""
    global _CACHED
    if path is not None:
        return RerankModel.load(path)
    if _CACHED is None:
        _CACHED = RerankModel.load()
    return _CACHED


def is_available(path: Optional[str] = None) -> bool:
    """True when a usable model is on disk. Never raises."""
    try:
        get_model(path)
        return True
    except RerankModelError:
        return False
