"""Grok AI agent for Polymarket sentiment analysis and market intelligence."""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("grok")

GROK_API_URL = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = "grok-3"


class GrokAgent:
    """Uses Grok (xAI) to analyze Polymarket markets for edge detection."""

    def __init__(self, api_key: str, model: str = GROK_MODEL):
        self.api_key = api_key
        self.model = model
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url="https://api.x.ai",
                timeout=httpx.Timeout(30.0),
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def analyze_market(
        self,
        question: str,
        yes_price: float,
        volume: float,
        days_left: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Get Grok's assessment of a Polymarket market."""
        client = await self._get_client()

        time_context = f"{days_left:.1f} days until resolution." if days_left else "No fixed end date."
        prompt = f"""Analyze this Polymarket prediction market:

Question: {question}
Current YES price: ${yes_price:.4f} (implied probability: {yes_price*100:.1f}%)
24h volume: ${volume:,.0f}
{time_context}

Respond with JSON only:
{{"edge_direction": "YES"|"NO"|"NONE", "confidence": 0.0-1.0, "reasoning": "1-sentence rationale", "estimated_probability": 0.0-1.0, "key_factors": ["factor1", "factor2"]}}"""

        try:
            resp = await client.post("/v1/chat/completions", json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "You are a prediction market analyst. Respond only with valid JSON. Be concise and evidence-based."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 300,
            })
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]

            # Extract JSON from response
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]

            result = json.loads(content)
            return {
                "edge_direction": result.get("edge_direction", "NONE"),
                "confidence": result.get("confidence", 0.5),
                "reasoning": result.get("reasoning", ""),
                "estimated_probability": result.get("estimated_probability", yes_price),
                "key_factors": result.get("key_factors", []),
            }
        except Exception as e:
            logger.error(f"Grok analysis error: {e}")
            return {
                "edge_direction": "NONE",
                "confidence": 0.0,
                "reasoning": f"Error: {e}",
                "estimated_probability": yes_price,
                "key_factors": [],
            }

    async def batch_analyze(
        self,
        markets: List[Dict[str, Any]],
        max_concurrent: int = 5,
    ) -> List[Dict[str, Any]]:
        """Analyze multiple markets, limiting concurrency."""
        import asyncio

        semaphore = asyncio.Semaphore(max_concurrent)

        async def analyze_one(market: Dict) -> Dict:
            async with semaphore:
                analysis = await self.analyze_market(
                    question=market.get("question", ""),
                    yes_price=market.get("yes_price", 0.5),
                    volume=market.get("volume", 0),
                    days_left=market.get("days_left"),
                )
                return {**market, "grok": analysis}

        tasks = [analyze_one(m) for m in markets]
        return await asyncio.gather(*tasks)

    async def quick_pulse(self, topic: str) -> Dict[str, Any]:
        """Quick sentiment check on a topic (no specific market)."""
        client = await self._get_client()

        try:
            resp = await client.post("/v1/chat/completions", json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "You are a market intelligence analyst. Respond with JSON only."},
                    {"role": "user", "content": f"Current market sentiment on: {topic}. Reply with JSON: {{\"sentiment\": \"bullish\"|\"bearish\"|\"neutral\", \"confidence\": 0.0-1.0, \"key_events\": [\"event1\"]}}"},
                ],
                "temperature": 0.3,
                "max_tokens": 200,
            })
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(content)
        except Exception as e:
            logger.error(f"Grok pulse error: {e}")
            return {"sentiment": "neutral", "confidence": 0.0, "key_events": []}
