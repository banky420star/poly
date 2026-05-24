from math import sqrt

from app.data.feature_store import FeatureSnapshot


class PathFeasibilityAgent:
    def can_finish_side(self, features: FeatureSnapshot, side: str) -> bool:
        if side == "UP":
            required_move = features.price_to_beat - features.spot
        else:
            required_move = features.spot - features.price_to_beat

        if required_move <= 0:
            return True

        possible_move = (
            abs(features.velocity_30s) * features.seconds_remaining
            + features.volatility_60s * sqrt(max(features.seconds_remaining, 1))
        )

        return possible_move >= required_move
