from dataclasses import dataclass

from app.data.feature_store import FeatureSnapshot
from app.agents.odds_agent import OddsDecision
from app.agents.probability_agent import Prediction


@dataclass
class EntryDecision:
    action: str
    side: str | None
    limit_price: float | None
    reason: list[str]


class EntryTimingAgent:
    def __init__(self, config: dict):
        self.config = config

    def decide(
        self,
        features: FeatureSnapshot,
        prediction: Prediction,
        odds: OddsDecision,
    ) -> EntryDecision:
        risk_cfg = self.config["risk"]
        market_cfg = self.config["markets"]
        odds_cfg = self.config["odds"]

        if features.seconds_remaining < int(market_cfg["min_seconds_remaining_entry"]):
            return EntryDecision(
                action="NO_TRADE",
                side=None,
                limit_price=None,
                reason=["Too little time remaining"],
            )

        spread = features.up_spread if odds.best_side == "UP" else features.down_spread

        if spread > float(risk_cfg["max_spread"]):
            return EntryDecision(
                action="NO_TRADE",
                side=None,
                limit_price=None,
                reason=["Spread too wide"],
            )

        if odds.fair_probability < float(odds_cfg["min_probability"]):
            return EntryDecision(
                action="NO_TRADE",
                side=None,
                limit_price=None,
                reason=["Fair probability too low"],
            )

        if prediction.confidence < float(odds_cfg["min_confidence"]):
            return EntryDecision(
                action="WATCH",
                side=None,
                limit_price=None,
                reason=["Confidence too low"],
            )

        if odds.adjusted_edge >= odds.required_edge:
            return EntryDecision(
                action=f"BUY_{odds.best_side}_NOW",
                side=odds.best_side,
                limit_price=odds.market_ask,
                reason=[
                    f"Adjusted edge {odds.adjusted_edge:.4f} >= required {odds.required_edge:.4f}"
                ],
            )

        return EntryDecision(
            action="WAIT_FOR_BETTER_PRICE",
            side=odds.best_side,
            limit_price=odds.max_entry_price,
            reason=[
                f"Current ask {odds.market_ask:.4f} above max entry {odds.max_entry_price:.4f}"
            ],
        )
