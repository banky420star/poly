import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class AppConfig:
    mode: str
    raw: dict


def load_config(path: str = "configs/default.yaml") -> AppConfig:
    load_dotenv()

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    mode = os.getenv("MODE", raw.get("mode", "paper"))

    return AppConfig(
        mode=mode,
        raw=raw,
    )
