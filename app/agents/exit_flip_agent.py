from dataclasses import dataclass

from app.agents.path_feasibility_agent import PathFeasibilityAgent
from app.agents.probability_agent import Prediction
from app.data.feature_store import FeatureSnapshot
from app.execution.position_manager import Position


@dataclass
class ExitDecision:
    action: str
    exit_price: float | None
    flip_side: str | None
    reason: list[str]


class ExitFlipAgent:
    def __init__(self, config: dict):
        self.config = config
        self.path_agent = PathFeasibilityAgent()

    def decide(
        self,
        position: Position,
        features: FeatureSnapshot,
        prediction: Prediction,
    ) -> ExitDecision:
        current_side = position.side
        opposite_side = "DOWN" if current_side == "UP" else "UP"

        exit_bid = features.up_bid if current_side == "UP" else features.down_bid
        opposite_ask = features.down_ask if current_side == "UP" else features.up_ask

        current_fair = prediction.prob_up if current_side == "UP" else prediction.prob_down
        opposite_fair = prediction.prob_down if current_side == "UP" else prediction.prob_up

        pnl_pct = (exit_bid - position.avg_entry) / position.avg_entry

        current_overpriced = exit_bid > current_fair + 0.04
        opposite_edge = opposite_fair - opposite_ask
        path_feasible = self.path_agent.can_finish_side(features, opposite_side)

        min_flip_seconds = int(self.config["markets"]["min_seconds_remaining_flip"])

        if pnl_pct >= 0.08 and current_overpriced:
            if (
                features.seconds_remaining >= min_flip_seconds
                and path_feasible
                and opposite_edge >= 0.06
            ):
                return ExitDecision(
                    action="EXIT_AND_FLIP",
                    exit_price=exit_bid,
                    flip_side=opposite_side,
                    reason=[
                        "Current side profitable and overpriced",
                        "Opposite side has edge",
                        "Path feasible",
                    ],
                )

            return ExitDecision(
                action="TAKE_PROFIT",
                exit_price=exit_bid,
                flip_side=None,
                reason=[
                    "Current side profitable and overpriced",
                    "Opposite side not strong enough for flip",
                ],
            )

        if pnl_pct <= -0.05 and current_fair < 0.45:
            return ExitDecision(
                action="CUT_LOSS",
                exit_price=exit_bid,
                flip_side=None,
                reason=[
                    "Position losing",
                    "Current fair probability deteriorated",
                ],
            )

        return ExitDecision(
            action="HOLD",
            exit_price=None,
            flip_side=None,
            reason=["No exit condition met"],
        )
