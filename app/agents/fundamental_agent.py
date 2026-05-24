from app.data.feature_store import FeatureSnapshot
from app.utils.math_utils import clamp


class FundamentalAgent:
    def score(self, features: FeatureSnapshot) -> float:
        vol = max(features.volatility_60s, 1e-6)

        distance_score = features.distance_to_target / vol
        velocity_score = features.velocity_30s / vol

        time_weight = clamp(features.seconds_remaining / 300, 0.2, 1.0)

        raw_score = (
            0.65 * distance_score +
            0.35 * velocity_score
        ) * time_weight

        return clamp(raw_score, -3.0, 3.0)
