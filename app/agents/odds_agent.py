from dataclasses import dataclass

from app.data.feature_store import FeatureSnapshot
from app.agents.probability_agent import Prediction


@dataclass
class OddsDecision:
    best_side: str
    fair_probability: float
    market_ask: float
    execution_cost: float
    adjusted_edge: float
    max_entry_price: float
    required_edge: float


class OddsAgent:
    def __init__(self, config: dict):
        self.config = config

    def required_edge(self, features: FeatureSnapshot, spread: float) -> float:
        odds_cfg = self.config["odds"]

        base = float(odds_cfg["base_required_edge"])

        if features.seconds_remaining < 60:
            base = max(base, float(odds_cfg["late_market_required_edge"]))

        if spread > 0.03:
            base += 0.02

        return base

    def calculate(self, features: FeatureSnapshot, prediction: Prediction) -> OddsDecision:
        up_execution_cost = features.up_spread / 2
        down_execution_cost = features.down_spread / 2

        up_required_edge = self.required_edge(features, features.up_spread)
        down_required_edge = self.required_edge(features, features.down_spread)

        up_edge = prediction.prob_up - features.up_ask - up_execution_cost
        down_edge = prediction.prob_down - features.down_ask - down_execution_cost

        if up_edge >= down_edge:
            required = up_required_edge
            return OddsDecision(
                best_side="UP",
                fair_probability=prediction.prob_up,
                market_ask=features.up_ask,
                execution_cost=up_execution_cost,
                adjusted_edge=up_edge,
                max_entry_price=prediction.prob_up - required - up_execution_cost,
                required_edge=required,
            )

        required = down_required_edge
        return OddsDecision(
            best_side="DOWN",
            fair_probability=prediction.prob_down,
            market_ask=features.down_ask,
            execution_cost=down_execution_cost,
            adjusted_edge=down_edge,
            max_entry_price=prediction.prob_down - required - down_execution_cost,
            required_edge=required,
        )
