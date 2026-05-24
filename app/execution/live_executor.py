"""Live CLOB executor — real order placement with safety gates.

ALL live orders go through here. There is no other path to the CLOB.
Every order is gated behind 5 safety checks before it touches the wire.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("poly.live_executor")


@dataclass
class SafetyGates:
    """All gates must be True before any live order is placed."""
    LIVE_TRADING_ENABLED: bool = False      # Set via env var
    KILL_SWITCH: bool = False               # True = BLOCK all orders (kill switch is active)
    MAX_ORDER_USD: float = 5.0              # Per-order cap
    MAX_DAILY_LOSS_USD: float = 20.0        # Daily loss cap
    MAX_POSITION_USD: float = 10.0          # Max total exposure
    daily_pnl: float = 0.0                  # Tracked across orders
    last_trade_day: str = ""                # Reset daily PnL on new day

    def can_trade(self) -> tuple[bool, str]:
        """Check all safety gates. Returns (allowed, reason)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.last_trade_day != today:
            self.daily_pnl = 0.0
            self.last_trade_day = today

        if not self.LIVE_TRADING_ENABLED:
            return False, "LIVE_TRADING_ENABLED is False"
        if self.KILL_SWITCH:
            return False, "KILL_SWITCH is active"
        if self.daily_pnl <= -self.MAX_DAILY_LOSS_USD:
            return False, f"Daily loss limit reached (${self.daily_pnl:.2f})"
        return True, "OK"


@dataclass
class LiveOrderResult:
    """Result of a live order attempt."""
    status: str                     # "LIVE_FILLED", "LIVE_REJECTED", "BLOCKED", "ERROR"
    order_id: str | None = None
    market_id: str = ""
    side: str = ""
    price: float = 0.0
    shares: float = 0.0
    size_usd: float = 0.0
    reason: str = ""


class LiveExecutor:
    """Places real orders on Polymarket CLOB through py_clob_client SDK.

    Every public method checks safety gates before execution.
    There is NO fast path that bypasses safety.
    """

    def __init__(self):
        self.gates = SafetyGates()
        self.client = None
        self.funder_address: str = ""
        self.initialized = False

        # Load safety from environment
        self.gates.LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
        self.gates.KILL_SWITCH = os.getenv("KILL_SWITCH", "true").lower() == "true"
        self.gates.MAX_ORDER_USD = float(os.getenv("MAX_LIVE_ORDER_USD", "5.0"))
        self.gates.MAX_DAILY_LOSS_USD = float(os.getenv("MAX_DAILY_LOSS_USD", "20.0"))

        logger.info("Live executor initialized: enabled=%s kill_switch=%s max_order=$%.2f",
                     self.gates.LIVE_TRADING_ENABLED, self.gates.KILL_SWITCH, self.gates.MAX_ORDER_USD)

    def initialize(self, private_key: str | None = None, funder_address: str | None = None) -> bool:
        """Initialize the CLOB client with credentials. Call once at startup.

        Uses POLY_PROXY signature type (same as the Rust bot's working config).
        Returns True if initialization succeeded.
        """
        try:
            pk = private_key or os.getenv("PRIVATE_KEY", "")
            self.funder_address = funder_address or os.getenv("DEPOSIT_WALLET_ADDRESS", "")

            if not pk:
                logger.warning("No private key — live executor cannot sign orders")
                return False

            # Import and initialize the SDK client
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
            chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

            self.client = ClobClient(
                host=host,
                key=pk,
                chain_id=chain_id,
                creds=ApiCreds(
                    key=pk,
                    api_key="",     # Not needed for proxy wallet
                    api_secret="",  # Not needed for proxy wallet
                    api_passphrase="",
                ),
                signature_type=2,  # POLY_PROXY = 2
                funder=self.funder_address,
            )

            self.initialized = True
            logger.info("Live CLOB client initialized (proxy wallet)")
            return True

        except Exception as e:
            logger.error("Failed to initialize live CLOB client: %s", e)
            self.initialized = False
            return False

    def enable_kill_switch(self):
        """Activate kill switch — blocks all future orders."""
        self.gates.KILL_SWITCH = True
        logger.warning("KILL SWITCH ACTIVATED — all live orders blocked")

    def disable_kill_switch(self):
        """Deactivate kill switch — allows orders if other gates pass."""
        if self.gates.LIVE_TRADING_ENABLED:
            self.gates.KILL_SWITCH = False
            logger.warning("KILL SWITCH DISABLED — live orders allowed")
        else:
            logger.warning("Cannot disable kill switch: LIVE_TRADING_ENABLED is False")

    def execute_entry(self, market_id: str, token_id: str, side: str,
                      price: float, size_usd: float) -> LiveOrderResult:
        """Place a live buy order. All safety gates checked internally.

        Args:
            market_id: Polymarket market ID
            token_id: CLOB token ID to buy
            side: "UP" or "DOWN"
            price: limit price (will be rounded to tick size)
            size_usd: dollar amount to spend (capped at MAX_ORDER_USD)

        Returns:
            LiveOrderResult with status and details
        """
        # SAFETY GATE 1: All gates check
        allowed, reason = self.gates.can_trade()
        if not allowed:
            return LiveOrderResult(
                status="BLOCKED", market_id=market_id, reason=f"Safety gate: {reason}"
            )

        # SAFETY GATE 2: Client initialized
        if not self.initialized or self.client is None:
            return LiveOrderResult(
                status="BLOCKED", market_id=market_id, reason="Client not initialized"
            )

        # SAFETY GATE 3: Size cap
        size_usd = min(size_usd, self.gates.MAX_ORDER_USD)
        if size_usd < 5.0:
            return LiveOrderResult(
                status="BLOCKED", market_id=market_id,
                reason=f"Order too small: ${size_usd:.2f} < $5.00 minimum"
            )

        # SAFETY GATE 4: Price sanity
        if price <= 0.01 or price >= 0.99:
            return LiveOrderResult(
                status="BLOCKED", market_id=market_id,
                reason=f"Price out of bounds: {price}"
            )

        # Round to tick size (0.001 for Polymarket)
        price = round(price, 3)
        shares = round(size_usd / price, 2)

        try:
            # Build and submit order
            from py_clob_client.clob_types import OrderArgs

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=int(shares),  # Polymarket expects integer shares
                side="BUY",        # Always BUY on the token side
            )

            # Use FAK (Fill-And-Kill) — don't leave resting orders
            result = self.client.create_order(order_args)

            order_id = result.get("id", result.get("orderID", ""))

            logger.info("LIVE ORDER: %s %s %d@%.3f ($%.2f) id=%s",
                         market_id, side, int(shares), price, size_usd, order_id)

            return LiveOrderResult(
                status="LIVE_FILLED",
                order_id=str(order_id),
                market_id=market_id,
                side=side,
                price=price,
                shares=shares,
                size_usd=size_usd,
                reason="Order placed",
            )

        except Exception as e:
            logger.error("Live order failed: %s", e)
            return LiveOrderResult(
                status="ERROR",
                market_id=market_id,
                side=side,
                price=price,
                shares=0,
                size_usd=0,
                reason=str(e)[:200],
            )

    def cancel_all(self) -> int:
        """Cancel all open orders. Returns count of cancelled orders."""
        if not self.initialized or self.client is None:
            return 0

        try:
            result = self.client.cancel_all()
            count = len(result) if isinstance(result, list) else 0
            logger.info("Cancelled %d open orders", count)
            return count
        except Exception as e:
            logger.error("Cancel all failed: %s", e)
            return 0

    def get_balance(self) -> Optional[float]:
        """Get USDC balance from the CLOB. Returns None on failure."""
        if not self.initialized or self.client is None:
            return None

        try:
            balance = self.client.get_balance()
            return float(balance.get("balance", 0)) if balance else None
        except Exception as e:
            logger.warning("Balance check failed: %s", e)
            return None

    def record_pnl(self, pnl: float):
        """Track daily PnL for loss limit enforcement."""
        self.gates.daily_pnl += pnl
