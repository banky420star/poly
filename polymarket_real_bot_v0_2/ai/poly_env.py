import json
import os
import numpy as np
import gymnasium as gym
from gymnasium import spaces

class PolymarketEnv(gym.Env):
    """
    Custom Environment for Polymarket following gymnasium interface.
    """
    metadata = {"render_modes": ["human"]}

    def __init__(self, data_file="../training_ticks.jsonl"):
        super().__init__()
        # 0 = SKIP, 1 = MAKER BUY YES, 2 = MAKER BUY NO, 3 = CANCEL, 4 = EXIT YES, 5 = EXIT NO
        self.action_space = spaces.Discrete(6)

        # Observation: [LSTM edge, YES price, NO price, spread, bid depth, ask depth, 
        #               seconds to expiry, current exposure, open position count, 
        #               recent win rate, recent slippage]
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(11,), dtype=np.float32)

        self.data_file = data_file
        self.ticks = []
        self.current_step = 0
        
        self._load_or_mock_data()

    def _load_or_mock_data(self):
        if os.path.exists(self.data_file):
            with open(self.data_file, 'r') as f:
                for line in f:
                    if line.strip():
                        try:
                            self.ticks.append(json.loads(line))
                        except:
                            pass
                            
        if len(self.ticks) == 0:
            print(f"Warning: {self.data_file} not found or empty. Generating mock data for PPO testing.")
            for i in range(1000):
                self.ticks.append({
                    "yes_price": 0.5 + np.sin(i / 10.0) * 0.1,
                    "no_price": 0.5 - np.sin(i / 10.0) * 0.1,
                    "spread_bps": 200,
                    "yes_ask_depth": 500,
                    "yes_bid_depth": 500,
                    "time_left": 300 - (i % 300),
                    "momentum_signal": 0.5 + np.random.randn() * 0.1
                })

    def _get_obs(self):
        tick = self.ticks[self.current_step]
        # Build 11-dim obs vector
        # [LSTM edge, YES price, NO price, spread, bid depth, ask depth, 
        #  seconds to expiry, current exposure, open position count, 
        #  recent win rate, recent slippage]
        lstm_edge = tick.get("momentum_signal", 0.5) - 0.5
        yes_price = tick.get("yes_price", 0.5)
        no_price = tick.get("no_price", 0.5)
        spread = tick.get("spread_bps", 200) / 10000.0
        bid_depth = tick.get("yes_bid_depth", 100.0)
        ask_depth = tick.get("yes_ask_depth", 100.0)
        time_left = tick.get("time_left", 300)
        exposure = 0.0
        open_pos = 0.0
        win_rate = 0.5
        slippage = 0.0
        
        return np.array([
            lstm_edge, yes_price, no_price, spread, bid_depth, ask_depth,
            time_left, exposure, open_pos, win_rate, slippage
        ], dtype=np.float32)

    def step(self, action):
        obs = self._get_obs()
        reward = 0.0
        
        # Simple dummy reward mechanic based on price action in next step
        current_yes_price = obs[1]
        
        self.current_step += 1
        terminated = self.current_step >= len(self.ticks) - 1
        truncated = False
        
        if not terminated:
            next_obs = self._get_obs()
            next_yes_price = next_obs[1]
            
            # If we bought YES (1) and price went up, reward!
            if action == 1:
                reward = (next_yes_price - current_yes_price) * 100
            # If we bought NO (2) and YES price went down, reward!
            elif action == 2:
                reward = (current_yes_price - next_yes_price) * 100
            # Small penalty for doing nothing to encourage trading (or vice versa)
            elif action == 0:
                reward = -0.001
        
        info = {}
        
        return self._get_obs(), float(reward), terminated, truncated, info

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.current_step = 0
        return self._get_obs(), {}

    def render(self):
        pass
