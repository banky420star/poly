import torch
import torch.nn as nn
import pandas as pd
import json

class EdgeLSTM(nn.Module):
    def __init__(self, input_size=15, hidden_size=64, num_layers=2):
        super().__init__()
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers, batch_first=True)
        self.fc = nn.Linear(hidden_size, 1) # Predicts future_fair_probability

    def forward(self, x):
        out, _ = self.lstm(x)
        out = self.fc(out[:, -1, :])
        return torch.sigmoid(out) # Probability between 0 and 1

def load_data(filepath="training_ticks.jsonl"):
    # Load and preprocess data
    # Features: yes_bid, yes_ask, no_bid, no_ask, yes_bid_depth, yes_ask_depth,
    # no_bid_depth, no_ask_depth, spread, mid, btc_return_5s, btc_return_15s,
    # btc_return_30s, momentum, seconds_to_expiry
    pass

if __name__ == "__main__":
    print("LSTM training script. Requires data gathering first.")
