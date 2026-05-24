from dataclasses import dataclass

from app.data.feature_store import FeatureSnapshot
from app.agents.fundamental_agent import FundamentalAgent
from app.utils.math_utils import sigmoid


@dataclass
class Prediction:
    prob_up: float
    prob_down: float
    confidence: float
    raw_score: float
    reason: list[str]


class ProbabilityAgent:
    def __init__(self):
        self.fundamental = FundamentalAgent()

    def predict(self, features: FeatureSnapshot) -> Prediction:
        raw_score = self.fundamental.score(features)

        prob_up = sigmoid(raw_score)
        prob_down = 1 - prob_up

        confidence = abs(prob_up - 0.5) * 2

        reason = [
            f"distance_to_target={features.distance_to_target:.2f}",
            f"velocity_30s={features.velocity_30s:.4f}",
            f"volatility_60s={features.volatility_60s:.4f}",
            f"seconds_remaining={features.seconds_remaining}",
        ]

        return Prediction(
            prob_up=prob_up,
            prob_down=prob_down,
            confidence=confidence,
            raw_score=raw_score,
            reason=reason,
        )
