"""ML-based signal generator using RL and feature engineering.

This module provides:
1. Market feature extraction from Polymarket data
2. A trained RL policy that scores market opportunities
3. An ensemble that combines ML signals with Grok analysis
"""

import logging
from typing import Dict, List, Optional
import numpy as np

logger = logging.getLogger("ml_signals")


class MarketFeatureExtractor:
    """Extract numeric features from Polymarket market data for ML models."""

    @staticmethod
    def extract(market: Dict) -> np.ndarray:
        """Convert a market dict into a normalized feature vector."""
        features = []

        # Price features
        yes_price = float(market.get("yes_price", 0.5))
        no_price = float(market.get("no_price", 1.0 - yes_price))
        features.append(yes_price)
        features.append(no_price)
        features.append(yes_price + no_price - 1.0)  # arb signal

        # Volume features (log-scaled)
        volume = float(market.get("volume", 0))
        features.append(np.log1p(volume) / 20.0)

        # Time features
        days_left = market.get("days_left")
        if days_left is not None and days_left > 0:
            features.append(min(float(days_left) / 365.0, 1.0))
            features.append(1.0 / max(float(days_left), 0.01))  # urgency
        else:
            features.append(0.5)
            features.append(0.0)

        # Reference price skew
        ref_price = float(market.get("ref_price", 0))
        if ref_price > 0:
            features.append((ref_price - yes_price) / max(yes_price, 0.01))
        else:
            features.append(0.0)

        # Volatility proxy (spread)
        spread = abs(yes_price - 0.5)
        features.append(spread)

        # Binary features
        features.append(1.0 if market.get("neg_risk", False) else 0.0)

        return np.array(features, dtype=np.float32)


class MLSignalGenerator:
    """Generates trade signals using a simple scoring ensemble."""

    def __init__(self):
        self.feature_extractor = MarketFeatureExtractor()
        self._weights = None

    def score_market(self, market: Dict) -> float:
        """Score a market from -1.0 (strong SELL) to 1.0 (strong BUY)."""
        feats = self.feature_extractor.extract(market)

        # Simple heuristic scoring (placeholder for trained RL policy)
        yes_price = feats[0]
        arb_signal = feats[2]  # yes + no - 1.0
        volume_score = feats[3]
        days_left = feats[4]
        urgency = feats[5]
        skew = feats[6]
        spread = feats[7]

        score = 0.0

        # Pair arb: if sum < 1.0, buy both (positive score)
        if arb_signal < -0.02:
            score += 0.3

        # Directional skew from reference price
        score += np.tanh(skew) * 0.25

        # Fade extremes: if price > 0.85, fade (sell)
        if yes_price > 0.85:
            score -= 0.2
        elif yes_price < 0.15:
            score += 0.2

        # Near expiry: reduce conviction
        if days_left < 1.0 / 24.0:  # < 1 hour
            score *= 0.5

        # Low volume = low confidence
        score *= min(volume_score * 3.0, 1.0)

        return float(np.clip(score, -1.0, 1.0))

    def generate_signal(self, market: Dict) -> Optional[Dict]:
        """Generate a trade signal from market data."""
        score = self.score_market(market)
        if abs(score) < 0.1:
            return None

        side = "BUY" if score > 0 else "SELL"
        confidence = min(abs(score), 0.85)
        yes_price = float(market.get("yes_price", 0.5))

        return {
            "side": side,
            "confidence": confidence,
            "score": score,
            "yes_price": yes_price,
            "reason": f"ML score={score:.3f}",
        }


class SignalEnsemble:
    """Combines ML signals with Grok AI analysis for final trade decisions."""

    def __init__(self, ml_weight: float = 0.5, grok_weight: float = 0.5):
        self.ml = MLSignalGenerator()
        self.ml_weight = ml_weight
        self.grok_weight = grok_weight

    def combine(self, market: Dict, grok_analysis: Optional[Dict] = None) -> Dict:
        """Merge ML + Grok signals into a unified decision."""
        ml_signal = self.ml.generate_signal(market)

        result = {
            "ml_signal": ml_signal,
            "grok_analysis": grok_analysis,
            "final_side": "HOLD",
            "final_confidence": 0.0,
        }

        ml_score = ml_signal["score"] if ml_signal else 0.0

        grok_score = 0.0
        if grok_analysis:
            direction = grok_analysis.get("edge_direction", "NONE")
            conf = grok_analysis.get("confidence", 0.0)
            est_prob = grok_analysis.get("estimated_probability", 0.5)
            yes_price = float(market.get("yes_price", 0.5))

            if direction == "YES" and est_prob > yes_price:
                grok_score = conf * (est_prob - yes_price) / max(yes_price, 0.01)
            elif direction == "NO" and est_prob < yes_price:
                grok_score = -conf * (yes_price - est_prob) / max(yes_price, 0.01)

        combined = ml_score * self.ml_weight + grok_score * self.grok_weight
        result["combined_score"] = combined

        if abs(combined) > 0.15:
            result["final_side"] = "BUY" if combined > 0 else "SELL"
            result["final_confidence"] = min(abs(combined), 0.90)

        return result
