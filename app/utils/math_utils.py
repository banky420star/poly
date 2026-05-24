import math


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sigmoid(x: float) -> float:
    x = clamp(x, -20, 20)
    return 1 / (1 + math.exp(-x))
