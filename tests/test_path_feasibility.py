from app.agents.path_feasibility_agent import PathFeasibilityAgent
from app.data.feature_store import FeatureSnapshot


def test_path_feasible_when_already_winning():
    features = FeatureSnapshot(
        market_id="m1",
        symbol="BTC",
        spot=101,
        price_to_beat=100,
        distance_to_target=1,
        seconds_remaining=30,
        velocity_30s=0.1,
        volatility_60s=1,
        up_bid=0.5,
        up_ask=0.52,
        down_bid=0.48,
        down_ask=0.5,
        up_spread=0.02,
        down_spread=0.02,
    )

    agent = PathFeasibilityAgent()
    assert agent.can_finish_side(features, "UP") is True
