import json
import time
import requests
from datetime import datetime, timezone

def generate_signal():
    """
    Dummy inference script to write ai_signal.json.
    """
    try:
        now_ts = int(time.time())
        interval = 300
        ts = (now_ts // interval) * interval
        
        market_slug = f"btc-updown-5m-{ts}"
        resp = requests.get(f"https://gamma-api.polymarket.com/markets?slug={market_slug}&limit=1")
        data = resp.json()
        
        if not data:
            raise ValueError(f"Market not found on CLOB API: {market_slug}")
            
        market = data[0]
        token_ids = json.loads(market['clobTokenIds'])
        outcomes = json.loads(market['outcomes'])
        
        # Find the YES or UP token ID
        if "Up" in outcomes:
            yes_idx = outcomes.index("Up")
            outcome_str = "Up"
        else:
            yes_idx = outcomes.index("Yes")
            outcome_str = "Yes"
            
        token_id = token_ids[yes_idx]
        
    except Exception as e:
        print(f"Error fetching market: {e}")
        token_id = "123"
        outcome_str = "YES"
        market_slug = f"btc-updown-5m-{int(time.time())}"
        
    now = datetime.now(timezone.utc).isoformat()
    signal = {
        "timestamp": now,
        "model": "lstm_ppo_v1",
        "market_slug": market_slug,
        "token_id": token_id,
        "outcome": outcome_str.upper(),
        "action": "BUY",
        "order_type": "MAKER",
        "limit_price": 0.47,
        "shares": 5,
        "current_price": 0.47,
        "lstm_fair_prob": 0.54,
        "edge": 0.07,
        "ppo_confidence": 0.78,
        "ttl_ms": 1500,
        "reason": f"{outcome_str.upper()} underpriced by 7 cents, depth acceptable, expiry safe"
    }

    with open("../ai_signal.json", "w") as f:
        json.dump(signal, f, indent=2)
    
    print(f"[{now}] Wrote shadow signal to ai_signal.json for {market_slug} ({token_id})")

if __name__ == "__main__":
    print("Starting continuous AI signal generator...")
    while True:
        try:
            generate_signal()
        except Exception as e:
            print(f"Signal generator error: {e}")
        time.sleep(5)
