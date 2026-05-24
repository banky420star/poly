from datetime import datetime, timedelta, timezone

from app.config import load_config
from app.data.market_state import MarketState
from app.data.orderbook_state import OrderBookState
from app.data.price_state import PriceState
from app.data.feature_store import FeatureStore
from app.agents.probability_agent import ProbabilityAgent
from app.agents.odds_agent import OddsAgent
from app.agents.entry_timing_agent import EntryTimingAgent
from app.agents.exit_flip_agent import ExitFlipAgent
from app.execution.position_manager import PositionManager
from app.execution.paper_executor import PaperExecutor
from app.storage.recorder import Recorder
from app.utils.logging_utils import setup_logger, log_json


def run_mock():
    cfg = load_config()
    logger = setup_logger()
    recorder = Recorder()

    market = MarketState(
        market_id="mock-btc-15m",
        question="BTC Up or Down 15m",
        slug="btc-up-down-15m",
        symbol="BTC",
        timeframe="15m",
        up_token_id="UP_TOKEN",
        down_token_id="DOWN_TOKEN",
        price_to_beat=68250.0,
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    up_book = OrderBookState(
        token_id="UP_TOKEN",
        best_bid=0.56,
        best_ask=0.58,
        bid_depth=1000,
        ask_depth=1000,
    )

    down_book = OrderBookState(
        token_id="DOWN_TOKEN",
        best_bid=0.42,
        best_ask=0.44,
        bid_depth=1000,
        ask_depth=1000,
    )

    feature_store = FeatureStore()
    probability_agent = ProbabilityAgent()
    odds_agent = OddsAgent(cfg.raw)
    entry_agent = EntryTimingAgent(cfg.raw)
    exit_agent = ExitFlipAgent(cfg.raw)

    positions = PositionManager()
    paper = PaperExecutor(
        position_manager=positions,
        bankroll_usd=float(cfg.raw["risk"]["bankroll_usd"]),
        max_position_pct=float(cfg.raw["risk"]["max_position_pct"]),
    )

    # Feed a small price history so velocity/volatility are not empty.
    mock_prices = [68220, 68230, 68242, 68255, 68268, 68280]

    for i, price in enumerate(mock_prices):
        spot = PriceState(
            symbol="BTC",
            price=float(price),
            timestamp_ms=1_700_000_000_000 + i * 10_000,
            source="mock",
        )

        features = feature_store.build(
            market=market,
            spot=spot,
            up_book=up_book,
            down_book=down_book,
        )

    prediction = probability_agent.predict(features)
    odds = odds_agent.calculate(features, prediction)
    decision = entry_agent.decide(features, prediction, odds)

    payload = {
        "market_id": market.market_id,
        "spot": features.spot,
        "price_to_beat": features.price_to_beat,
        "seconds_remaining": features.seconds_remaining,
        "prob_up": prediction.prob_up,
        "prob_down": prediction.prob_down,
        "confidence": prediction.confidence,
        "best_side": odds.best_side,
        "market_ask": odds.market_ask,
        "adjusted_edge": odds.adjusted_edge,
        "max_entry_price": odds.max_entry_price,
        "decision": decision.action,
        "reason": decision.reason,
    }

    log_json(logger, "decision", payload)
    recorder.write("decision", payload)

    if decision.action.startswith("BUY_"):
        result = paper.execute_entry(market.market_id, decision)
        log_json(logger, "paper_entry", result)
        recorder.write("paper_entry", result)

        position = positions.get(market.market_id)
        exit_decision = exit_agent.decide(position, features, prediction)

        log_json(logger, "exit_check", {
            "action": exit_decision.action,
            "reason": exit_decision.reason,
        })
        recorder.write("exit_check", {
            "action": exit_decision.action,
            "reason": exit_decision.reason,
        })


if __name__ == "__main__":
    run_mock()
