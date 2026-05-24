from app.agents.odds_agent import OddsAgent
from app.agents.probability_agent import Prediction
from app.data.feature_store import FeatureSnapshot


def test_odds_agent_detects_up_edge():
    cfg = {
        "odds": {
            "base_required_edge": 0.05,
            "late_market_required_edge": 0.10,
        }
    }

    features = FeatureSnapshot(
        market_id="m1",
        symbol="BTC",
        spot=101,
        price_to_beat=100,
        distance_to_target=1,
        seconds_remaining=120,
        velocity_30s=0.1,
        volatility_60s=1,
        up_bid=0.55,
        up_ask=0.57,
        down_bid=0.43,
        down_ask=0.45,
        up_spread=0.02,
        down_spread=0.02,
    )

    prediction = Prediction(
        prob_up=0.68,
        prob_down=0.32,
        confidence=0.36,
        raw_score=1.0,
        reason=[],
    )

    odds = OddsAgent(cfg).calculate(features, prediction)

    assert odds.best_side == "UP"
    assert odds.adjusted_edge > 0
