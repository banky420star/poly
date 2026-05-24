import json
from pathlib import Path
from datetime import datetime, timezone


class Recorder:
    def __init__(self, path: str = "logs/snapshots.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, payload: dict):
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }

        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
