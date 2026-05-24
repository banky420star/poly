"""Polymarket Paper Odds Bot — live paper trading loop with real market data."""
import asyncio
import signal
import sys
from datetime import datetime, timezone

from app.config import load_config
from app.data.feature_store import FeatureStore
from app.data.market_state import MarketState
from app.agents.probability_agent import ProbabilityAgent
from app.agents.odds_agent import OddsAgent
from app.agents.entry_timing_agent import EntryTimingAgent
from app.agents.exit_flip_agent import ExitFlipAgent
from app.execution.position_manager import PositionManager
from app.execution.paper_executor import PaperExecutor
from app.storage.recorder import Recorder
from app.utils.logging_utils import setup_logger, log_json

from app.clients.exchange_price_ws import BinancePriceFeed
from app.clients.polymarket_client import PolymarketClient
from app.clients.polymarket_ws import PolymarketWsClient
from app.discovery.market_discovery import MarketDiscovery

logger = None  # Set in run()
running = False


def build_features_for_market(
    market: MarketState,
    feature_store: FeatureStore,
    binance_feed: BinancePriceFeed,
    poly_ws: PolymarketWsClient,
) -> object | None:
    """Build a FeatureSnapshot for a single market, using live data."""
    spot = binance_feed.get(market.symbol)
    if spot is None:
        return None

    up_book = poly_ws.get(market.up_token_id)
    down_book = poly_ws.get(market.down_token_id)

    if up_book is None or down_book is None:
        return None

    return feature_store.build(market, spot, up_book, down_book)


async def run_loop(
    cfg,
    feature_store: FeatureStore,
    probability_agent: ProbabilityAgent,
    odds_agent: OddsAgent,
    entry_agent: EntryTimingAgent,
    exit_agent: ExitFlipAgent,
    positions: PositionManager,
    paper: PaperExecutor,
    binance_feed: BinancePriceFeed,
    poly_ws: PolymarketWsClient,
    poly_rest: PolymarketClient,
    discovery: MarketDiscovery,
    recorder: Recorder,
    markets: list[MarketState],
    http_client,
    loop_num: int,
):
    global logger

    # 1. Refresh market discovery every 5 loops
    if loop_num % 5 == 0 or not markets:
        markets.clear()
        markets.extend(await discovery.discover(http_client))

        # Subscribe to new tokens
        all_tokens = []
        for m in markets:
            all_tokens.append(m.up_token_id)
            all_tokens.append(m.down_token_id)
        poly_ws.subscribe(all_tokens)

    if not markets:
        logger.warning("No markets discovered yet")
        return

    # 2. REST fallback: fetch order books for markets with stale/missing WS data
    stale_tokens = []
    for m in markets:
        if poly_ws.get(m.up_token_id) is None:
            stale_tokens.append(m.up_token_id)
        if poly_ws.get(m.down_token_id) is None:
            stale_tokens.append(m.down_token_id)

    if stale_tokens:
        await poly_rest.fetch_all_books(stale_tokens)
        # Update WS cache with REST results for any missing books
        for token_id in stale_tokens:
            book = await poly_rest.fetch_order_book(token_id)
            if book and token_id not in poly_ws.books:
                poly_ws.books[token_id] = book

    # 3. Pipeline: features -> probability -> odds -> entry for each market
    decisions = []
    for market in markets:
        features = build_features_for_market(market, feature_store, binance_feed, poly_ws)
        if features is None:
            continue

        prediction = probability_agent.predict(features)
        odds = odds_agent.calculate(features, prediction)
        decision = entry_agent.decide(features, prediction, odds)

        payload = {
            "market_id": market.market_id,
            "symbol": market.symbol,
            "spot": features.spot,
            "price_to_beat": features.price_to_beat,
            "seconds_remaining": features.seconds_remaining,
            "prob_up": prediction.prob_up,
            "prob_down": prediction.prob_down,
            "confidence": prediction.confidence,
            "best_side": odds.best_side,
            "adjusted_edge": odds.adjusted_edge,
            "decision": decision.action,
            "reason": decision.reason,
        }
        log_json(logger, "decision", payload)
        recorder.write("decision", payload)

        if decision.action.startswith("BUY_"):
            decisions.append((market, decision, features, prediction))

    # 4. Execute entries (max 1 per loop for safety)
    if decisions:
        market, decision, features, prediction = decisions[0]

        # Check we don't already have a position in this market
        if not positions.get(market.market_id):
            result = paper.execute_entry(market.market_id, decision)
            log_json(logger, "paper_entry", result)
            recorder.write("paper_entry", result)

            if result["status"] == "PAPER_FILLED":
                logger.info("ENTRY: %s %s @ %.4f $%.2f",
                           market.symbol, decision.side, decision.limit_price, result["size_usd"])

    # 5. Check exits for open positions
    for market_id, position in list(positions.positions.items()):
        # Find matching market
        market = next((m for m in markets if m.market_id == market_id), None)
        if market is None:
            continue

        features = build_features_for_market(market, feature_store, binance_feed, poly_ws)
        if features is None:
            continue

        prediction = probability_agent.predict(features)
        exit_decision = exit_agent.decide(position, features, prediction)

        recorder.write("exit_check", {
            "market_id": market_id,
            "action": exit_decision.action,
            "reason": exit_decision.reason,
        })

        if exit_decision.action in ("TAKE_PROFIT", "CUT_LOSS", "EXIT_AND_FLIP"):
            result = paper.execute_exit(market_id, exit_decision.exit_price, exit_decision.action)
            log_json(logger, "paper_exit", result)
            recorder.write("paper_exit", result)
            logger.info("EXIT: %s PnL=$%.4f reason=%s",
                       market_id, result.get("pnl", 0), exit_decision.action)

        if exit_decision.action == "EXIT_AND_FLIP" and exit_decision.flip_side:
            # Enter the opposite side
            flip_features = features  # Same features, opposite side
            flip_prediction = prediction
            flip_odds = odds_agent.calculate(flip_features, flip_prediction)
            flip_decision = entry_agent.decide(flip_features, flip_prediction, flip_odds)

            if flip_decision.action.startswith("BUY_"):
                result = paper.execute_entry(market_id, flip_decision)
                log_json(logger, "paper_flip_entry", result)
                recorder.write("paper_flip_entry", result)
                logger.info("FLIP ENTRY: %s %s @ %.4f", market.symbol, flip_decision.side, flip_decision.limit_price)


async def run():
    global logger, running

    cfg = load_config()
    logger = setup_logger()

    # Initialize components
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
    recorder = Recorder()

    # Initialize clients
    binance_feed = BinancePriceFeed(symbols=cfg.raw.get("symbols", ["BTC", "ETH", "SOL"]))
    poly_ws = PolymarketWsClient()
    poly_rest = PolymarketClient()
    discovery = MarketDiscovery(
        symbols=cfg.raw.get("symbols", ["BTC", "ETH", "SOL"]),
        timeframes=cfg.raw.get("markets", {}).get("timeframes", ["5m", "15m"]),
    )

    http_client = None
    try:
        import httpx
        http_client = httpx.AsyncClient(timeout=30.0)

        # Start data feeds
        await binance_feed.start()
        await poly_rest.start()
        await poly_ws.start()

        logger.info("Paper Odds Bot starting with symbols: %s", cfg.raw.get("symbols"))
        print(f"\n  PAPER ODDS BOT — LIVE DATA FEEDS")
        print(f"  Bankroll: ${cfg.raw['risk']['bankroll_usd']}")
        print(f"  Symbols: {', '.join(cfg.raw.get('symbols', []))}")
        print(f"  Ctrl+C to stop\n")

        markets: list[MarketState] = []
        running = True
        loop_num = 0

        while running:
            loop_num += 1
            try:
                await run_loop(
                    cfg, feature_store, probability_agent, odds_agent,
                    entry_agent, exit_agent, positions, paper,
                    binance_feed, poly_ws, poly_rest, discovery,
                    recorder, markets, http_client, loop_num,
                )

                # Status line
                total_pnl = sum(
                    t.get("pnl", 0) for t in paper.closed_trades
                )
                print(f"  Loop #{loop_num} | Markets: {len(markets)} | "
                      f"Positions: {len(positions.positions)} | "
                      f"Closed PnL: ${total_pnl:.2f} | "
                      f"Cash: ${paper.bankroll_usd - sum(p.cost_usdc for p in positions.positions.values()):.2f}")

            except Exception as e:
                logger.exception("Loop %d error: %s", loop_num, e)

            await asyncio.sleep(5)

    finally:
        logger.info("Shutting down...")
        await binance_feed.stop()
        await poly_ws.stop()
        await poly_rest.close()
        if http_client:
            await http_client.aclose()
        logger.info("Shutdown complete")


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def stop():
        global running
        running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
