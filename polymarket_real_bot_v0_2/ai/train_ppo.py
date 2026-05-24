import os
from stable_baselines3 import PPO
from poly_env import PolymarketEnv

def train():
    env = PolymarketEnv()
    
    # Use a small network for fast training/testing
    policy_kwargs = dict(net_arch=[64, 64])
    
    model = PPO("MlpPolicy", env, verbose=1, policy_kwargs=policy_kwargs, n_steps=256, batch_size=64)
    
    print("Starting PPO training on Polymarket ticks...")
    model.learn(total_timesteps=10000)
    
    os.makedirs("../models", exist_ok=True)
    model.save("../models/ppo_policy")
    print("Saved model to ../models/ppo_policy.zip")

if __name__ == "__main__":
    train()
